"""Per-invocation ownership for the portable synchronous PPT renderer (POSIX).

The supervisor owns admission and deadlines. A worker owns Playwright; a small
browser guard keeps Chromium's detached process group owned until it is empty.
Only registered processes are signalled. No imports start or reap processes.
"""

from contextlib import contextmanager
import array
import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time


CLEANUP_SECONDS = 10.0
TRANSIENT_EXIT = 75
CLEANUP_EXIT = 70


class RenderCleanupError(RuntimeError):
    pass


class RenderEnvironmentError(RuntimeError):
    pass


def _positive(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _acquire_render_slot(deadline=None):
    limit = int(os.environ.get("RENDER_GLOBAL_LIMIT", "0") or "0")
    if limit < 0:
        raise ValueError("RENDER_GLOBAL_LIMIT must be nonnegative")
    if not limit:
        return None
    wait = _positive(os.environ.get("RENDER_SLOT_TIMEOUT", "900"), "RENDER_SLOT_TIMEOUT")
    deadline = min(deadline or float("inf"), time.monotonic() + wait)
    directory = Path(os.environ.get("RENDER_LOCK_DIR", "/tmp/ppt_render_slots"))
    directory.mkdir(parents=True, exist_ok=True)
    while time.monotonic() < deadline:
        for index in range(limit):
            fd = os.open(directory / f"slot_{index}.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
            except BaseException:
                os.close(fd)
                raise
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    raise TimeoutError("render admission timed out; configured limit was not bypassed")


def _release_render_slot(fd):
    if fd is not None:
        # Duplicates sent to worker/guards share the flock. Explicit LOCK_UN
        # would release their ownership too, before they finish cleanup.
        os.close(fd)


def _send(connection, message):
    connection.sendall(json.dumps(message).encode() + b"\n")


def _receive(connection):
    data = bytearray()
    while not data.endswith(b"\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise EOFError("render supervisor disconnected")
        data.extend(chunk)
        if len(data) > 16384:
            raise ValueError("oversized render control message")
    return json.loads(data)


_SLOT_COPIES = []


def _connect(role):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    connection.connect(os.environ["_PPT_RENDER_SOCKET"])
    _send(connection, {"event": "register", "role": role, "pid": os.getpid(),
                       "parent": os.getppid(), "token": os.environ["_PPT_RENDER_TOKEN"]})
    data, ancillary, flags, _ = connection.recvmsg(16384, socket.CMSG_SPACE(array.array("i").itemsize))
    if flags & socket.MSG_CTRUNC:
        raise RuntimeError("truncated render admission descriptor")
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptors = array.array("i")
            descriptors.frombytes(payload[:len(payload) - len(payload) % descriptors.itemsize])
            for fd in descriptors:
                os.set_inheritable(fd, False)
                _SLOT_COPIES.append(fd)
    while not data.endswith(b"\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise EOFError("render supervisor disconnected during admission")
        data += chunk
    if json.loads(data).get("event") != "start":
        raise RuntimeError("render supervisor denied process startup")
    connection.settimeout(None)
    return connection


def _kill_own_group():
    # The caller is its own live identity anchor. Never signal an inherited group.
    if os.getpgrp() == os.getpid():
        os.killpg(os.getpid(), signal.SIGKILL)
    os._exit(CLEANUP_EXIT)


def _group_members(pgid, exclude=()):
    import psutil

    members = []
    for process in psutil.process_iter(["pid", "status"]):
        if process.pid in exclude or process.info["status"] == psutil.STATUS_ZOMBIE:
            continue
        try:
            if os.getpgid(process.pid) == pgid:
                # Retain creation time so psutil's signalling checks PID reuse.
                process.create_time()
                members.append(process)
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return members


def _live_identity(identity):
    import psutil

    try:
        return identity.is_running() and identity.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _registered_members(anchor):
    import psutil

    # An orphaned group retains its PGID even after its leader is reaped. If
    # that leader PID has been reused, this registration no longer owns it.
    try:
        if psutil.Process(anchor.pid).create_time() != anchor.create_time():
            return []
    except psutil.NoSuchProcess:
        pass
    return _group_members(anchor.pid)


def _signal_identity(identity, signum):
    import psutil

    try:
        identity.send_signal(signum)  # psutil checks the cached creation time.
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass  # Live survivors are reported after the bounded cleanup wait.


def _kill_orphan_group(anchor, members, deadline):
    import psutil

    # A dead guard cannot anchor killpg. Freeze a verified remaining member so
    # the registered PGID stays owned while killing the group, including forks
    # absent from the initial snapshot. Do not run orphan shutdown handlers.
    for member in members:
        try:
            member.send_signal(signal.SIGSTOP)
            limit = min(deadline, time.monotonic() + 0.2)
            while time.monotonic() < limit and _live_identity(member):
                if member.status() == psutil.STATUS_STOPPED:
                    if member.is_running() and os.getpgid(member.pid) == anchor.pid:
                        observed = _group_members(anchor.pid)
                        if member.is_running() and os.getpgid(member.pid) == anchor.pid:
                            os.killpg(anchor.pid, signal.SIGKILL)
                            return observed
                    break
                time.sleep(0.005)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError, PermissionError):
            continue
        finally:
            _signal_identity(member, signal.SIGKILL)  # Never leave our stopped anchor behind.
    return []


def _drain_own_children():
    deadline = time.monotonic() + 2
    while True:
        members = _group_members(os.getpid(), exclude=(os.getpid(),))
        if not members:
            return True
        for process in members:
            try:
                if os.getpgid(process.pid) == os.getpid():
                    process.send_signal(signal.SIGTERM if time.monotonic() < deadline - 1 else signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception as exc:
                # Disappearance/PID reuse is expected; a live survivor is checked below.
                if type(exc).__name__ not in {"NoSuchProcess", "ZombieProcess"}:
                    raise
        if time.monotonic() >= deadline:
            return not _group_members(os.getpid(), exclude=(os.getpid(),))
        time.sleep(0.05)


def _browser_guard(executable, args):
    if os.getpgrp() != os.getpid():
        raise RuntimeError("browser guard requires Playwright's dedicated process group")
    inherited = []
    for fd in (3, 4):
        try:
            os.fstat(fd)
            inherited.append(fd)
        except OSError:
            pass
    connection = _connect("browser")
    # A callable handler is reset by exec in Chromium; SIG_IGN would be inherited.
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    child = None
    try:
        child = subprocess.Popen([executable, *args], pass_fds=tuple(inherited))
        for fd in inherited:
            os.close(fd)  # Keeping these open suppresses Playwright pipe EOF.
        selector = selectors.DefaultSelector()
        selector.register(connection, selectors.EVENT_READ)
        try:
            while child.poll() is None:
                if selector.select(0.05):
                    _receive(connection)  # EOF or cancellation kills our group below.
                    _kill_own_group()
            clean = _drain_own_children()
            _send(connection, {"event": "done", "clean": clean})
            connection.settimeout(5)
            if _receive(connection).get("event") != "release" or not clean:
                _kill_own_group()
            return child.returncode
        finally:
            selector.close()
    except BaseException as exc:
        if child is None:
            print(f"render browser launch failed: {_error_chain(exc)}", file=sys.stderr, flush=True)
            try:
                _send(connection, {"event": "done", "clean": True})
                connection.settimeout(2)
                if _receive(connection).get("event") == "release":
                    return 127
            except Exception:
                pass
        _kill_own_group()
    finally:
        connection.close()


_CONTROL = None


def _phase(name, seconds, cleanup=False):
    if _CONTROL is not None:
        _send(_CONTROL, {"event": "phase", "name": name, "seconds": seconds, "cleanup": cleanup})


def _error_chain(error):
    messages = []
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        messages.append(f"{type(error).__name__}: {error}")
        error = error.__cause__ or error.__context__
    return "\ncaused by: ".join(messages)


class RenderSession:
    """One worker's driver/browser, with a fresh explicit context for each page."""

    def __init__(self, sync_factory, resolve_browser, launch_args, is_fatal=None):
        self.sync_factory = sync_factory
        self.resolve_browser = resolve_browser
        self.launch_args = launch_args
        self.is_fatal = is_fatal
        self.manager = None
        self.playwright = None
        self.browser = None
        self.cleanup_errors = []
        self.reported_errors = set()

    def _report_before_cleanup(self, error):
        # Emit now: a hung close may force the supervisor to kill this worker
        # before the exception can reach its outer handler.
        if error is not None and id(error) not in self.reported_errors:
            self.reported_errors.add(id(error))
            print(f"render failure before cleanup: {_error_chain(error)}", file=sys.stderr, flush=True)

    def __enter__(self):
        try:
            _phase("start", 60)
            self.manager = self.sync_factory()
            self.playwright = self.manager.start()
            executable = self.resolve_browser(self.playwright)
            wrapper = Path(os.environ["_PPT_RENDER_DIRECTORY"]) / "browser-launch"
            wrapper.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(value) for value in
                [sys.executable, str(Path(__file__).resolve()), "--browser", executable]) + ' "$@"\n')
            wrapper.chmod(0o700)
            _phase("launch", 60)
            try:
                self.browser = self.playwright.chromium.launch(
                    executable_path=str(wrapper), args=self.launch_args, timeout=60000)
            except Exception as exc:
                if self.is_fatal is not None and self.is_fatal(str(exc)):
                    raise RenderEnvironmentError(str(exc)) from exc
                raise
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def renew_page_deadline(self):
        # The supervisor still caps each phase by the invocation's total budget.
        _phase("page", 60)

    @contextmanager
    def page(self, width, height, scale=2):
        self.renew_page_deadline()
        context = self.browser.new_context(
            viewport={"width": width, "height": height}, device_scale_factor=scale)
        try:
            context.set_default_timeout(15000)
            context.set_default_navigation_timeout(30000)
            yield context.new_page()
        finally:
            original = sys.exc_info()[1]
            self._report_before_cleanup(original)
            _phase("context-close", 5, cleanup=True)
            try:
                context.close()
            except Exception as exc:
                self.cleanup_errors.append(f"context.close: {_error_chain(exc)}")
                self._report_before_cleanup(exc)
                raise RenderCleanupError(self.cleanup_errors[-1]) from original

    def __exit__(self, exc_type, exc, traceback):
        self._report_before_cleanup(exc)
        if self.browser is not None:
            _phase("browser-close", 5, cleanup=True)
            try:
                self.browser.close()
            except Exception as error:
                self.cleanup_errors.append(f"browser.close: {_error_chain(error)}")
                self._report_before_cleanup(error)
            self.browser = None
        if self.manager is not None:
            _phase("driver-stop", 5, cleanup=True)
            try:
                self.manager.__exit__(exc_type, exc, traceback)
            except Exception as error:
                self.cleanup_errors.append(f"Playwright stop: {_error_chain(error)}")
                self._report_before_cleanup(error)
            self.manager = None
        if self.cleanup_errors:
            raise RenderCleanupError("; ".join(self.cleanup_errors)) from exc
        return False


def _worker(renderer, args):
    import runpy

    global _CONTROL
    _CONTROL = _connect("worker")
    released = threading.Event()

    def watch_parent():
        try:
            message = _receive(_CONTROL)
            if message.get("event") == "release":
                released.set()
                return
        except BaseException:
            pass
        _kill_own_group()

    threading.Thread(target=watch_parent, daemon=True).start()
    code = 0
    try:
        sys.path.insert(0, str(Path(renderer).resolve().parent))
        sys.argv = [renderer, *args]
        runpy.run_path(renderer, run_name="__render_worker__")["main"]()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except BaseException as exc:
        print(f"render failed: {_error_chain(exc)}", file=sys.stderr)
        code = (CLEANUP_EXIT if isinstance(exc, RenderCleanupError) else
                TRANSIENT_EXIT if type(exc).__name__ == "TargetClosedError" else 1)
    sys.stdout.flush()
    sys.stderr.flush()
    _send(_CONTROL, {"event": "done", "clean": True, "code": code})
    if not released.wait(5):
        _kill_own_group()
    _CONTROL.close()
    return code


class _Supervisor:
    def __init__(self, renderer, args, directory, deadline, stdout, stderr, slot=None):
        import secrets
        import psutil

        self.deadline = deadline
        budget = max(0, deadline - time.monotonic())
        self.force_deadline = deadline - min(3, budget / 5)
        self.work_deadline = self.force_deadline - min(CLEANUP_SECONDS, budget / 5)
        self.stage_deadline = self.work_deadline
        self.stage = "startup"
        self.token = secrets.token_hex(24)
        self.slot = slot
        self.peers = {}
        self.selector = selectors.DefaultSelector()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        address = str(Path(directory) / "control.sock")
        self.listener.bind(address)
        self.listener.listen()
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ)
        environment = dict(os.environ, _PPT_RENDER_SOCKET=address, _PPT_RENDER_TOKEN=self.token,
                           _PPT_RENDER_DIRECTORY=str(directory))
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", renderer, *args],
            env=environment, start_new_session=True, stdout=stdout, stderr=stderr)
        self.worker_identity = psutil.Process(self.process.pid)
        self.worker_identity.create_time()
        self.worker_done = False
        self.cleanup_failed = False

    def _remove(self, connection):
        self.selector.unregister(connection)
        peer = self.peers.pop(connection)
        connection.close()
        return peer

    def _message(self, connection, message):
        import psutil

        peer = self.peers[connection]
        event = message.get("event")
        if event == "register" and "identity" not in peer:
            pid = message.get("pid")
            role = message.get("role")
            if message.get("token") != self.token or role not in {"worker", "browser"}:
                raise RuntimeError("invalid render process registration")
            identity = psutil.Process(pid)
            identity.create_time()
            if os.getpgid(pid) != pid:
                raise RuntimeError("render process is not a group owner")
            if role == "worker" and pid != self.process.pid:
                raise RuntimeError("unexpected render worker")
            if role == "browser" and os.getpgid(identity.ppid()) != self.process.pid:
                raise RuntimeError("browser guard is outside this render worker")
            peer.update(identity=identity, role=role)
            message = json.dumps({"event": "start"}).encode() + b"\n"
            rights = [] if self.slot is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [self.slot]))]
            connection.sendmsg([message], rights)
        elif event == "phase" and peer.get("role") == "worker":
            self.stage = str(message["name"])
            end = self.force_deadline if message.get("cleanup") else self.work_deadline
            self.stage_deadline = min(end, time.monotonic() + _positive(message["seconds"], "phase timeout"))
        elif event == "done" and "identity" in peer:
            if not message.get("clean"):
                raise RenderCleanupError("browser descendants did not exit")
            if peer["role"] == "worker":
                # A driver left behind after a failed start/stop must be removed
                # before this worker releases admission or a retry can start.
                members = _group_members(self.process.pid, exclude=(self.process.pid,))
                if members:
                    raise RenderCleanupError("render worker retained driver processes")
                self.worker_done = True
            _send(connection, {"event": "release"})
            peer["released"] = True
        else:
            raise RuntimeError("unexpected render control message")

    def run(self):
        while True:
            if time.monotonic() >= min(self.deadline, self.stage_deadline):
                raise TimeoutError(f"render {self.stage} timed out")
            for key, _ in self.selector.select(0.05):
                connection = key.fileobj
                if connection is self.listener:
                    connection, _ = self.listener.accept()
                    connection.setblocking(False)
                    self.peers[connection] = {"buffer": bytearray()}
                    self.selector.register(connection, selectors.EVENT_READ)
                    continue
                peer = self.peers[connection]
                data = connection.recv(16384)
                if not data:
                    if "identity" in peer and not peer.get("released"):
                        # Playwright may itself kill its browser group after CDP
                        # disconnect. Verify it is empty; never infer a leak from
                        # the missing guard acknowledgement alone.
                        if peer.get("role") != "browser" or _group_members(peer["identity"].pid):
                            raise RenderCleanupError("registered render process exited without cleanup")
                    self._remove(connection)
                    continue
                peer["buffer"].extend(data)
                if len(peer["buffer"]) > 16384:
                    raise RuntimeError("oversized render control message")
                while b"\n" in peer["buffer"]:
                    line, _, remainder = peer["buffer"].partition(b"\n")
                    peer["buffer"] = bytearray(remainder)
                    self._message(connection, json.loads(line))
            code = self.process.poll()
            if code is not None:
                if not self.worker_done:
                    raise RenderCleanupError("render worker exited before cleanup acknowledgement")
                if any(not p.get("released") for p in self.peers.values() if "identity" in p):
                    raise RenderCleanupError("render worker exited with registered browsers")
                return code

    def close(self):
        # Stop accepting launches; existing guards have to receive START before
        # they can spawn a browser. EOF makes both guards and worker self-reap.
        self.listener.close()
        identities = [p["identity"] for p in self.peers.values()
                      if "identity" in p and not p.get("released")]
        observed = []
        orphan_groups = []
        for identity in [*identities, self.worker_identity]:
            members = _registered_members(identity)
            observed.extend(members)
            if members and not _live_identity(identity):
                orphan_groups.append(identity)
                observed.extend(_kill_orphan_group(identity, members, self.deadline))
        for connection in list(self.peers):
            self._remove(connection)
        identities.append(self.worker_identity)
        for identity in identities:
            try:
                if identity.is_running() and os.getpgid(identity.pid) == identity.pid:
                    os.killpg(identity.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        for identity in observed:
            _signal_identity(identity, signal.SIGTERM)
        limit = min(time.monotonic() + 0.5, self.deadline - 0.05)
        while time.monotonic() < limit and any(_live_identity(p) for p in identities):
            self.process.poll()
            time.sleep(0.02)
        # A guard can disappear during the grace period as well as before it.
        for identity in identities:
            if not _live_identity(identity):
                members = _registered_members(identity)
                if members:
                    orphan_groups.append(identity)
                    observed.extend(members)
                    observed.extend(_kill_orphan_group(identity, members, self.deadline))
        for identity in identities:
            try:
                if identity.is_running() and os.getpgid(identity.pid) == identity.pid:
                    os.killpg(identity.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for identity in observed:
            _signal_identity(identity, signal.SIGKILL)
        try:
            self.process.wait(timeout=max(0.05, self.deadline - time.monotonic()))
        finally:
            self.selector.close()
        # Capture before signalling, not from an unrelated machine-wide name scan.
        tracked = {p.pid: p for p in [*identities, *observed]}
        while time.monotonic() < self.deadline and any(_live_identity(p) for p in tracked.values()):
            time.sleep(min(0.02, max(0, self.deadline - time.monotonic())))
        survivors = [p.pid for p in tracked.values() if _live_identity(p)]
        if survivors:
            raise RenderCleanupError(f"render processes survived cleanup: {survivors}")
        for anchor in orphan_groups:
            while time.monotonic() < self.deadline and _registered_members(anchor):
                time.sleep(min(0.02, max(0, self.deadline - time.monotonic())))
            if _registered_members(anchor):
                raise RenderCleanupError(f"registered orphan group survived cleanup: {anchor.pid}")


def run_renderer(renderer, args, timeout=600):
    """Run one owned render invocation, returning captured output after cleanup."""
    timeout = _positive(timeout, "render timeout")
    deadline = time.monotonic() + timeout
    slot = None
    supervisor = None
    previous = {}
    cancelling = False
    cleaning = False
    code = 1
    error = ""
    output = ""
    errors = ""

    def cancel(signum, frame):
        nonlocal cancelling
        if cancelling or cleaning:
            cancelling = True
            return
        cancelling = True
        raise KeyboardInterrupt(f"render cancelled by signal {signum}")

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, cancel)
    try:
        slot = _acquire_render_slot(deadline)
        with tempfile.TemporaryDirectory(prefix="ppt-render-", dir="/tmp") as directory:
            with tempfile.TemporaryFile(mode="w+b") as stdout, tempfile.TemporaryFile(mode="w+b") as stderr:
                try:
                    attempts = 2 if "--batch" in args else 1 if "--audit-player" in args else 3
                    for attempt in range(attempts):
                        supervisor = _Supervisor(str(renderer), list(args), directory, deadline, stdout, stderr, slot=slot)
                        try:
                            code = supervisor.run()
                        finally:
                            cleaning = True
                            try:
                                supervisor.close()
                            finally:
                                supervisor = None
                                cleaning = False
                        if cancelling:
                            raise KeyboardInterrupt("render cancelled during cleanup")
                        if code != TRANSIENT_EXIT or attempt + 1 == attempts:
                            break
                        if time.monotonic() + CLEANUP_SECONDS >= deadline:
                            raise TimeoutError("render retry has no remaining cleanup budget")
                        Path(directory, "control.sock").unlink()
                finally:
                    stdout.seek(0)
                    stderr.seek(0)
                    output = stdout.read().decode("utf-8", "replace")
                    errors = stderr.read().decode("utf-8", "replace")
    except BaseException as exc:
        code = 130 if isinstance(exc, KeyboardInterrupt) else CLEANUP_EXIT if isinstance(exc, RenderCleanupError) else 1
        error = _error_chain(exc)
    finally:
        cleaning = True
        if supervisor is not None:
            try:
                supervisor.close()
            except Exception as exc:
                error += f"; cleanup failed: {exc}"
                code = CLEANUP_EXIT
        _release_render_slot(slot)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if code != 0 and "--batch" in args:
        # A worker receipt is provisional until supervision and cleanup succeed.
        # Preserve all diagnostics; only withhold the structured success receipt.
        retained = []
        for line in output.splitlines(keepends=True):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                item = None
            if not (isinstance(item, dict) and item.get("status") == "rendered"
                    and item.get("qa") == "not-run"):
                retained.append(line)
        output = "".join(retained)
    return subprocess.CompletedProcess([str(renderer), *args], code, output,
                                       errors + (error + "\n" if error else ""))


def supervise(renderer, args):
    result = run_renderer(renderer, args, os.environ.get("RENDER_JOB_TIMEOUT", "600"))
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    # Use one module identity so workers catch the same RenderCleanupError class.
    sys.modules["render_runtime"] = sys.modules[__name__]
    if sys.argv[1] == "--browser":
        raise SystemExit(_browser_guard(sys.argv[2], sys.argv[3:]))
    if sys.argv[1] == "--worker":
        raise SystemExit(_worker(sys.argv[2], sys.argv[3:]))
    raise SystemExit("internal renderer lifecycle entrypoint")
