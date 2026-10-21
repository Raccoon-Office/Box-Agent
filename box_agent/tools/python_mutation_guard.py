"""Check Python's executed destructive I/O against the owning tool's policy.

The bootstrap is source text so it also works with an external Python launched
by the frozen ACP runtime. Policy stays in the parent; children need no Box-Agent
imports or dependencies. Each process tree has its own authority and endpoint.
"""

from __future__ import annotations

import asyncio
from contextvars import copy_context
import json
import os
from pathlib import Path
import secrets
import socketserver
import tempfile
import threading

from .chat_references import bash_chat_mutation_error, chat_mutation_error
from .shell_inspection import inspect_shell_command


_ENDPOINT = "BOX_AGENT_PYTHON_MUTATION_GUARD"
_MAX_MESSAGE = 128 * 1024

# No project imports: this code runs in the user's Python, not the ACP Python.
_CLIENT_CODE = r'''
import json as _json, os as _os, shlex as _shlex, socket as _socket, sys as _sys

def _check(**request):
    request["token"] = _endpoint["token"]
    try:
        with _socket.create_connection(("127.0.0.1", _endpoint["port"]), timeout=5) as connection:
            connection.sendall((_json.dumps(request) + "\n").encode())
            with connection.makefile("rb") as stream:
                response = _json.loads(stream.readline(131073))
        error = response["error"]
    except Exception as exc:
        raise PermissionError("CHAT_REFERENCE_PROTECTED: Python deletion check unavailable; preserve files") from exc
    if error:
        raise PermissionError(error)

def _path(value, directory=-1):
    value = _os.fsdecode(value)
    if not _os.path.isabs(value) and directory not in (-1, None):
        if _sys.platform == "darwin":
            import fcntl
            base = _os.fsdecode(fcntl.fcntl(directory, 50, b"\0" * 1024).split(b"\0", 1)[0])
        elif _sys.platform.startswith("linux"):
            base = _os.readlink("/proc/self/fd/" + str(directory))
        else:
            raise PermissionError("CHAT_REFERENCE_PROTECTED: cannot resolve deletion dir_fd")
        value = _os.path.join(base, value)
    return _os.path.abspath(value)

def _command(command, cwd=None, env=None, executable=None):
    # Explicit child environments must preserve the hook. Silently dropping it
    # would authorize dynamic deletion in an unguarded Python grandchild.
    inherited = _os.environ if env is None else {_os.fsdecode(k): _os.fsdecode(v) for k, v in env.items()}
    if (inherited.get("BOX_AGENT_PYTHON_MUTATION_GUARD") != _json.dumps(_endpoint)
            or _guard_directory not in inherited.get("PYTHONPATH", "").split(_os.pathsep)):
        raise PermissionError("CHAT_REFERENCE_PROTECTED: preserve Python deletion guard in child environment")
    if isinstance(command, (list, tuple)):
        if executable:
            command = [executable, *command[1:]]
        command = _shlex.join([_os.fsdecode(arg) for arg in command])
    _check(kind="command", command=_os.fsdecode(command), cwd=_os.path.abspath(cwd or _os.getcwd()))

def _audit_command(event, args):
    if event == "subprocess.Popen":
        _command(args[1], args[2], args[3], args[0])
    elif event == "os.system":
        _command(args[0])
    elif event in ("os.exec", "os.posix_spawn"):
        _command(args[1], env=args[2], executable=args[0])
'''

_BOOTSTRAP = ("import json, os\n_endpoint = json.loads(os.environ[" + repr(_ENDPOINT)
              + "])\n_guard_directory = os.path.dirname(__file__)\n" + _CLIENT_CODE + r'''
def _audit(event, args):
    if event in ("os.remove", "os.rmdir"):
        _check(kind="mutation", path=_path(args[0], args[1]), operation="delete")
    elif event == "shutil.rmtree":
        # Python 3.10 emits only path; newer versions also include dir_fd.
        _check(kind="mutation", path=_path(args[0], args[1] if len(args) > 1 else None), operation="delete")
    elif event == "os.rename":
        # Replacing a published destination is an edit; only its source moves.
        _check(kind="mutation", path=_path(args[0], args[2]), operation="move")
    else:
        _audit_command(event, args)

_sys.addaudithook(_audit)

# Preserve an environment's existing customization after installing the hook.
import importlib.machinery as _machinery, importlib.util as _util
_own_directory = _os.path.dirname(__file__)
_spec = _machinery.PathFinder.find_spec("sitecustomize", [p for p in _sys.path if p != _own_directory])
if _spec and _spec.loader:
    _spec.loader.exec_module(_util.module_from_spec(_spec))
''')


def uses_python(command: str) -> bool:
    from .permissions import expand_inline_shell_path_variables

    command = expand_inline_shell_path_variables(command)
    return any(Path(item.executable.replace("\\", "/")).name.lower().startswith(("python", "pypy"))
               for item in inspect_shell_command(command).invocations)


def python_guard_error(command: str) -> str | None:
    """Fail closed for explicit startup options that disable sitecustomize."""
    from .permissions import expand_inline_shell_path_variables

    invocations = inspect_shell_command(expand_inline_shell_path_variables(command)).invocations
    keys = {_ENDPOINT, "PYTHONPATH"}
    if not uses_python(command):
        return None
    for invocation in invocations:
        name = Path(invocation.executable.replace("\\", "/")).name.lower()
        if name in {"unset", "export"} and any(word.split("=", 1)[0] in keys for word in invocation.arguments):
            return "CHAT_REFERENCE_PROTECTED: preserve the Python deletion guard environment"
        if not name.startswith(("python", "pypy")):
            continue
        arguments = iter(invocation.arguments)
        for argument in arguments:
            if not argument.startswith("-") or argument in {"-c", "-m", "--"}:
                break
            if argument in {"-W", "-X", "--check-hash-based-pycs", "--jit"}:
                next(arguments, None)
                continue
            if argument.startswith("-") and not argument.startswith("--") and set(argument[1:]) & set("ISE"):
                return "CHAT_REFERENCE_PROTECTED: Python -I/-S/-E disables deletion checks; use normal Python startup"
        prefix = invocation.prefix
        for index, word in enumerate(prefix):
            following = prefix[index + 1] if index + 1 < len(prefix) else ""
            if (word in {"-i", "--ignore-environment"}
                    or word in {"-u", "--unset"} and following in keys
                    or any(word.startswith(key + "=") or word in {"--unset=" + key, "-u" + key} for key in keys)):
                return "CHAT_REFERENCE_PROTECTED: preserve the Python deletion guard environment"
    return None


class PythonMutationGuard:
    """An invocation-scoped, thread-served policy bridge (never on ACP's loop)."""

    def __init__(self, environment: dict[str, str] | None) -> None:
        context = copy_context()
        token = secrets.token_hex(32)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.connection.settimeout(5)
                try:
                    request = json.loads(self.rfile.readline(_MAX_MESSAGE + 1))
                    if request.pop("token", None) != token:
                        raise ValueError("invalid guard token")
                    error = context.copy().run(check, request)
                except Exception:
                    error = "CHAT_REFERENCE_PROTECTED: Python deletion check failed; preserve files"
                try:
                    self.wfile.write((json.dumps({"error": error}) + "\n").encode())
                except OSError:
                    pass  # The owning tool may have cancelled/killed its child.

        def check(request):
            if request["kind"] == "mutation":
                if request["operation"] not in {"delete", "move"}:
                    raise ValueError("invalid operation")
                result = chat_mutation_error(Path(request["path"]), request["operation"])
            elif request["kind"] == "command":
                result = bash_chat_mutation_error(request["command"], Path(request["cwd"]), python_runtime=True)
            else:
                raise ValueError("invalid request")
            return result.error if result else None

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        self._directory = tempfile.TemporaryDirectory(prefix="box-agent-python-guard-")
        try:
            # Python normally prints sitecustomize errors and continues. A
            # broken guard must terminate startup instead of running unguarded.
            bootstrap = ("import os, sys\ntry:\n    exec(" + repr(_BOOTSTRAP)
                         + ")\nexcept BaseException as error:\n"
                         + "    sys.stderr.write('CHAT_REFERENCE_PROTECTED: Python guard startup failed: ' + str(error) + '\\n')\n"
                         + "    os._exit(126)\n")
            Path(self._directory.name, "sitecustomize.py").write_text(bootstrap, encoding="utf-8")
            self._server = Server(("127.0.0.1", 0), Handler)
            self.environment = dict(environment if environment is not None else os.environ)
            self.environment[_ENDPOINT] = json.dumps({"port": self._server.server_address[1], "token": token})
            self.environment["PYTHONPATH"] = self._directory.name + (
                os.pathsep + self.environment["PYTHONPATH"] if self.environment.get("PYTHONPATH") else ""
            )
            self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            self._thread.start()
        except BaseException:
            self._directory.cleanup()
            raise

    def kernel_setup_code(self) -> str:
        """Share command checks and guarded child startup with a Jupyter kernel."""
        environment = {key: self.environment[key] for key in (_ENDPOINT, "PYTHONPATH")}
        namespace = {"_endpoint": json.loads(environment[_ENDPOINT]),
                     "_guard_directory": self._directory.name}
        return f"""
_box_guard_sys._box_agent_command_guard = {namespace!r}
exec({_CLIENT_CODE!r}, _box_guard_sys._box_agent_command_guard)
_box_guard_sys._box_agent_command_env = {{key: _box_guard_os.environ.get(key) for key in {list(environment)!r}}}
_box_guard_os.environ[{_ENDPOINT!r}] = {environment[_ENDPOINT]!r}
_box_guard_os.environ["PYTHONPATH"] = {self._directory.name!r} + (_box_guard_os.pathsep + _box_guard_sys._box_agent_command_env["PYTHONPATH"] if _box_guard_sys._box_agent_command_env["PYTHONPATH"] else "")
"""

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._directory.cleanup()

    async def close_on_exit(self, process: asyncio.subprocess.Process) -> None:
        try:
            await process.wait()
        finally:
            await asyncio.to_thread(self.close)
