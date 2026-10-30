"""Native file-panel delivery and session-owned system-panel focus.

The signed CuaDriver installation is unchanged. A cached helper is compiled from
our bundled Swift source. Only an explicit not_sent result permits Driver retry.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import select
import subprocess
import sys
import tempfile
import threading

_lock = threading.Lock()
_binary = None
_unavailable = None


class PanelReceiverLease:
    """Own one system-panel focus lease; closing the input pipe releases native state."""

    def __init__(self):
        self.process = None
        self.pid = None
        self.token = None
        self.scope = None

    def close(self):
        process, self.process = self.process, None
        self.pid = self.token = self.scope = None
        if process is None:
            return
        try:
            process.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # A stuck tap must not outlive its owning adapter.
            process.kill()
            process.wait(timeout=2)
        finally:
            process.stdout.close()

    def environment(self, pid, timeout, host, window):
        if self.process is not None and (self.scope != (pid, host, window) or self.process.poll() is not None):
            self.close()
        if self.process is None:
            binary = _helper()
            if binary is None:
                return None
            self.token = secrets.randbits(63) or 1
            self.pid = pid
            self.scope = (pid, host, window)
            try:
                self.process = subprocess.Popen(
                    [binary, 'panel-lease', str(pid), str(self.token), str(host), str(window)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True,
                )
                readable, _, _ = select.select([self.process.stdout], [], [], min(timeout, 3))
                report = json.loads(self.process.stdout.readline()) if readable else None
                if not isinstance(report, dict) or report.get('ready') is not True:
                    self.close()
                    return None
            except (OSError, ValueError, TypeError):
                self.close()
                return None
        try:
            self.process.stdin.write('ping\n')
            self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            self.close()
            return None
        return dict(os.environ, BOX_CUA_PANEL_TOKEN=str(self.token),
                    BOX_CUA_PANEL_LEASE_PID=str(self.process.pid))


class PanelFocusSession:
    """Retain host/receiver focus together; release on retarget or session end."""

    def __init__(self):
        self.host = PanelReceiverLease()
        self.receiver = PanelReceiverLease()
        self.identity = None

    def close(self):
        self.receiver.close()
        self.host.close()
        self.identity = None

    def environment(self, host, target, window, timeout, host_window):
        identity = (host, target, window, host_window)
        if self.identity is not None and (host, host_window) != (self.identity[0], self.identity[3]):
            self.close()
        if self.host.environment(host, timeout, host, host_window) is None:
            return None
        environment = (self.host.environment(host, timeout, host, host_window) if target == host
                       else self.receiver.environment(target, timeout, host, host_window))
        if environment is None:
            self.close()
            return None
        self.identity = identity
        return dict(environment, BOX_CUA_PANEL_PID=str(target), BOX_CUA_PANEL_WINDOW=str(window))


def _helper():
    global _binary, _unavailable
    with _lock:
        if _binary is not None or _unavailable is not None:
            return _binary
        source = Path(__file__).with_name('native') / 'panel_keyboard.swift'
        try:
            digest = hashlib.sha256(source.read_bytes() + platform.machine().encode()).hexdigest()[:20]
            directory = Path.home() / 'Library/Caches/Box-Agent/panel-keyboard' / digest
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            binary = directory / 'panel-keyboard'
            if not binary.is_file():
                with tempfile.TemporaryDirectory(dir=directory) as temporary:
                    output = Path(temporary) / 'panel-keyboard'
                    built = subprocess.run(
                        ['/usr/bin/xcrun', 'swiftc', str(source), '-o', str(output)],
                        capture_output=True, text=True, check=False,
                    )
                    if built.returncode:
                        _unavailable = 'native_helper_build_failed'
                        return None
                    os.replace(output, binary)
            _binary = str(binary)
        except OSError:
            _unavailable = 'native_helper_unavailable'
        return _binary


def deliver_panel_key(pid, window_id, key, modifiers, *, timeout, session=None, field_frame=None):
    """Return None outside tested scope, or a structured delivery report.

After launching the helper, malformed output/crashes/timeouts are unknown
outcomes, never permission to send the same key again through the Driver.
"""
    if sys.platform != 'darwin':
        return None
    chord = (key, frozenset(modifiers))
    codes = {('g', frozenset({'cmd', 'shift'})): 5,
             ('return', frozenset()): 36, ('escape', frozenset()): 53}
    if chord not in codes:
        return None
    field_environment = {}
    if field_frame is not None:
        if codes[chord] != 36:
            return None
        field_environment['BOX_CUA_PANEL_FIELD_FRAME'] = json.dumps(field_frame)
    arguments = [str(pid), str(window_id), str(codes[chord])]
    # Resolve without sending input, then re-resolve in the delivery process.
    host_environment = session.host.environment(pid, timeout, pid, window_id) if session is not None else None
    if host_environment is None:
        return {'effect': 'not_sent', 'code': 'panel_lease_unavailable'}
    try:
        report = _invoke(arguments, timeout=timeout,
                         env=dict(host_environment, **field_environment, BOX_CUA_RESOLVE_PANEL='1'))
    except Exception:
        session.close()
        raise
    if report.get('code') != 'panel_target_resolved':
        if session is not None:
            session.close()
        return report
    if (report.get('effect') != 'not_sent'
            or type(report.get('resolved_pid')) is not int or report['resolved_pid'] <= 0
            or type(report.get('receiver_window_id')) is not int or report['receiver_window_id'] <= 0):
        raise RuntimeError('Invalid panel target; no keyboard delivery attempted')
    environment = (session.environment(pid, report['resolved_pid'],
                                       report['receiver_window_id'], timeout, window_id)
                   if session is not None else None)
    if environment is None:
        return {'effect': 'not_sent', 'code': 'panel_lease_unavailable'}
    try:
        result = _invoke(arguments, timeout=timeout, env=dict(environment, **field_environment))
    except Exception:
        session.close()
        raise
    if result['effect'] == 'not_sent' or codes[chord] == 53:
        session.close()
    return result


def _invoke(arguments, *, timeout, allowed_effects=frozenset({'not_sent', 'unverifiable'}), env=None):
    binary = _helper()
    if binary is None:
        return {'effect': 'not_sent', 'code': _unavailable}
    try:
        completed = subprocess.run(
            [binary, *arguments],
            capture_output=True, text=True, timeout=timeout, check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('Panel keyboard timed out; outcome unknown; do not replay') from error
    except OSError:
        return {'effect': 'not_sent', 'code': 'native_helper_launch_failed'}
    try:
        report = json.loads(completed.stdout)
        if (completed.returncode != 0 or not isinstance(report, dict)
                or report.get('effect') not in allowed_effects):
            raise ValueError('invalid helper report')
    except (ValueError, TypeError) as error:
        raise RuntimeError('Panel keyboard outcome unknown; do not replay') from error
    return report


def verify_file_panel_front(pid, window_id, *, timeout):
    """Read-only proof for Driver's layer-0-only bring_to_front rejection."""
    if sys.platform != 'darwin':
        return None
    return _invoke(['verify-panel-front', str(pid), str(window_id)], timeout=timeout,
                   allowed_effects={'not_sent', 'confirmed'})
