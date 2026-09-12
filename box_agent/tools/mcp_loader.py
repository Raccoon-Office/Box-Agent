"""MCP tool loader with real MCP client integration and timeout handling."""

import asyncio
import hashlib
import inspect
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx


# Python 3.10 compatibility: asyncio.timeout was added in 3.11
if sys.version_info >= (3, 11):
    _timeout = asyncio.timeout
else:

    @asynccontextmanager
    async def _timeout(delay: float):  # type: ignore[misc]
        """Minimal asyncio.timeout shim for Python 3.10."""
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        handle = loop.call_later(delay, task.cancel)  # type: ignore[union-attr]
        try:
            yield
        except asyncio.CancelledError:
            raise TimeoutError(f"Timed out after {delay}s")
        finally:
            handle.cancel()

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment, stdio_client

try:
    from mcp.client.streamable_http import (
        create_mcp_http_client,
        streamable_http_client,
    )
except ImportError:
    # MCP <= 1.19 exports the same client without the separator.
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

    create_mcp_http_client = None

from box_agent.auth import request_auth_headers, resolve_auth_token, should_attach_auth_header
from box_agent.config import MCPConfig

from .base import Tool, ToolResult
from .browser_runtime_scope import (
    acquire_browser_runtime_for_current_turn,
    release_browser_runtime_for_current_turn,
)
from .browser_tool_names import (
    browser_tool_fixed_arguments,
    public_browser_tool_description,
    public_browser_tool_name,
    public_browser_tool_parameters,
    public_browser_tool_text,
)
from .mcp_tool_catalog import get_mcp_tool_catalog
from .mcp_sources import (
    McpConfigSource,
    ResolvedMcpServer,
    configured_mcp_sources,
    resolve_mcp_sources,
)
from .model_tool_context import current_model_tool_context
from .playwright_session_pool import (
    BrowserSessionLimitError,
    PlaywrightSessionPool,
    close_all_tabs,
)

# Async context manager factory yielding the ClientSession to use for one call.
SessionLease = Callable[[], AbstractAsyncContextManager[Any]]


_MODEL_TOOL_NAME_MAX_LENGTH = 64
_INVALID_MODEL_TOOL_NAME_CHARACTER = re.compile(r"[^a-zA-Z0-9_-]")


def _warn(msg: str) -> None:
    """Write diagnostic message to stderr (never stdout)."""
    sys.stderr.write(msg + "\n")


def _public_mcp_tool_name(
    server_name: str,
    remote_name: str,
    connector_id: str | None = None,
) -> str:
    """Return a stable provider-safe name while preserving the MCP name separately."""
    mapped_name = public_browser_tool_name(server_name, remote_name)
    if connector_id and mapped_name == remote_name:
        mapped_name = f"mcp__{server_name}__{remote_name}"
    if (
        len(mapped_name) <= _MODEL_TOOL_NAME_MAX_LENGTH
        and not _INVALID_MODEL_TOOL_NAME_CHARACTER.search(mapped_name)
    ):
        return mapped_name

    digest = hashlib.sha256(
        f"{server_name}\0{remote_name}".encode("utf-8")
    ).hexdigest()[:12]
    safe_stem = _INVALID_MODEL_TOOL_NAME_CHARACTER.sub("_", mapped_name)
    safe_stem = safe_stem[: _MODEL_TOOL_NAME_MAX_LENGTH - len(digest) - 2]
    return f"{safe_stem}__{digest}"


def _mcp_tool_always_load(server_name: str, tool: Any, server_default: bool) -> bool:
    """Use the owning server's deferred-loading policy."""
    return server_default


def _replace_server_catalog(connection: "MCPServerConnection") -> None:
    catalog = get_mcp_tool_catalog()
    catalog.replace_server(connection.name, connection.tools)
    conflicts = sorted(
        {
            entry.model_name
            for entry in catalog.snapshot()
            if entry.name_conflict
        }
    )
    if conflicts:
        _warn(
            "MCP tool name conflict detected; deferred loading will hide all "
            f"conflicting targets: {', '.join(conflicts)}"
        )


def _structured_mcp_error_message(content: str) -> str | None:
    """Recover error envelopes from servers that forget MCP ``isError=true``."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or "error" not in payload:
        return None
    error = payload["error"]
    if not error:
        return None
    if isinstance(error, str):
        return error.strip() or None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        if any(error.get(key) not in (None, "", False) for key in ("code", "type")):
            return "Tool returned error"
    return None


def _walk_exception_tree(error: BaseException | None):
    """Yield an exception and its nested group/cause/context exceptions once."""
    if error is None:
        return

    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        yield current

        nested = getattr(current, "exceptions", ())
        if isinstance(nested, tuple):
            pending.extend(reversed(nested))

        cause = getattr(current, "__cause__", None)
        if isinstance(cause, BaseException):
            pending.append(cause)
        context = getattr(current, "__context__", None)
        if isinstance(context, BaseException):
            pending.append(context)


def _http_statuses_from_exception(error: BaseException | None) -> set[int]:
    statuses: set[int] = set()
    for current in _walk_exception_tree(error):
        status = getattr(current, "status_code", None)
        if status is None:
            status = getattr(getattr(current, "response", None), "status_code", None)
        try:
            normalized = int(status)
        except (TypeError, ValueError):
            continue
        if 100 <= normalized <= 599:
            statuses.add(normalized)
    return statuses


def _mcp_auth_status(*errors: BaseException | None) -> int | None:
    statuses = {
        status
        for error in errors
        for status in _http_statuses_from_exception(error)
    }
    for preferred in (401, 403):
        if preferred in statuses:
            return preferred
    return None


def _is_mcp_authentication_error(error: BaseException | None) -> bool:
    return _mcp_auth_status(error) is not None


def _mcp_connection_error_message(
    primary_error: BaseException,
    cleanup_error: BaseException | None = None,
) -> str:
    """Prefer the transport's real HTTP failure over cancellation side effects."""
    auth_status = _mcp_auth_status(primary_error, cleanup_error)
    if auth_status == 401:
        return "Authentication failed: Token is invalid or expired (HTTP 401)"
    if auth_status == 403:
        return "Authorization failed: Access denied or insufficient permissions (HTTP 403)"

    # Streamable HTTP may cancel ``session.initialize()`` and only expose the
    # actionable transport error while its task group is being closed.
    if isinstance(primary_error, asyncio.CancelledError) and cleanup_error is not None:
        for current in _walk_exception_tree(cleanup_error):
            if getattr(current, "exceptions", ()):
                continue
            message = str(current).strip()
            if message and not isinstance(current, asyncio.CancelledError):
                return message

    return str(primary_error).strip() or type(primary_error).__name__


# Connection type aliases
# ``stdio_http`` is internal: a stdio-style command that box-agent spawns in
# HTTP mode and then dials once per agent session (Playwright multiplexing).
ConnectionType = Literal["stdio", "sse", "http", "streamable_http", "stdio_http"]

# The hosted WebSearch service documents five as its stable concurrency. Keep
# this internal: per-turn fan-out may be tuned, but concurrent Agent sessions
# must not multiply load beyond the shared MCP connection's capacity.
WEB_SEARCH_MCP_MAX_CONCURRENCY = 5
_DEFAULT_MCP_CONFIG = MCPConfig()


@dataclass
class MCPTimeoutConfig:
    """MCP timeout configuration."""

    connect_timeout: float = _DEFAULT_MCP_CONFIG.connect_timeout
    execute_timeout: float = _DEFAULT_MCP_CONFIG.execute_timeout
    sse_read_timeout: float = _DEFAULT_MCP_CONFIG.sse_read_timeout


# Global default timeout config
_default_timeout_config = MCPTimeoutConfig()


class DynamicBearerAuth(httpx.Auth):
    """Attach the latest hosted login token to every HTTP request.

    URL-based MCP transports keep a persistent HTTP client alive across tool
    calls. Reading auth.json in this auth hook keeps MCP behavior aligned with
    LLM clients, which refresh login auth before each API request.
    """

    requires_request_body = False
    requires_response_body = False

    def __init__(self, auth_file: str = "", explicit_token: str = ""):
        self.auth_file = auth_file
        self.explicit_token = explicit_token

    def sync_auth_flow(self, request: httpx.Request):
        yield self._with_current_auth(request)

    async def async_auth_flow(self, request: httpx.Request):
        yield self._with_current_auth(request)

    def _with_current_auth(self, request: httpx.Request) -> httpx.Request:
        if "authorization" in request.headers:
            return request

        token = resolve_auth_token(self.explicit_token, self.auth_file)
        if not token or not should_attach_auth_header(str(request.url)):
            return request

        request.headers["Authorization"] = f"Bearer {token}"
        return request


def _has_authorization_header(headers: dict[str, str] | None) -> bool:
    return any(key.lower() == "authorization" for key in (headers or {}))


def _dynamic_bearer_auth_for_url(
    url: str | None,
    headers: dict[str, str],
    auth_file: str = "",
    auth_token: str = "",
) -> DynamicBearerAuth | None:
    if not url or _has_authorization_header(headers) or not should_attach_auth_header(url):
        return None
    return DynamicBearerAuth(auth_file=auth_file, explicit_token=auth_token)


def set_mcp_timeout_config(
    connect_timeout: float | None = None,
    execute_timeout: float | None = None,
    sse_read_timeout: float | None = None,
) -> None:
    """Set global MCP timeout configuration.

    Args:
        connect_timeout: Connection timeout in seconds
        execute_timeout: Tool execution timeout in seconds
        sse_read_timeout: SSE read timeout in seconds
    """
    global _default_timeout_config
    if connect_timeout is not None:
        _default_timeout_config.connect_timeout = connect_timeout
    if execute_timeout is not None:
        _default_timeout_config.execute_timeout = execute_timeout
    if sse_read_timeout is not None:
        _default_timeout_config.sse_read_timeout = sse_read_timeout


def get_mcp_timeout_config() -> MCPTimeoutConfig:
    """Get current MCP timeout configuration."""
    return _default_timeout_config


# --------------------------------------------------------------------------
# Managed browser isolation (one Playwright BrowserContext per agent session
# and per sub_agent run)
# --------------------------------------------------------------------------

PLAYWRIGHT_SERVER_NAME = "playwright"
_MULTIPLEX_LOOPBACK_HOST = "127.0.0.1"
_MULTIPLEX_READY_POLL_INTERVAL = 0.1
_MULTIPLEX_STOP_GRACE_SECONDS = 3.0


@dataclass
class PlaywrightIsolationConfig:
    """Runtime knobs for per-session Playwright contexts (from ``tools.mcp``)."""

    enabled: bool = _DEFAULT_MCP_CONFIG.playwright_per_session_context
    max_clients: int = _DEFAULT_MCP_CONFIG.playwright_max_session_clients
    idle_timeout: float = _DEFAULT_MCP_CONFIG.playwright_session_idle_timeout


_playwright_isolation_config = PlaywrightIsolationConfig()


def set_playwright_isolation_config(
    enabled: bool | None = None,
    max_clients: int | None = None,
    idle_timeout: float | None = None,
) -> None:
    if enabled is not None:
        _playwright_isolation_config.enabled = enabled
    if max_clients is not None:
        _playwright_isolation_config.max_clients = max(1, int(max_clients))
    if idle_timeout is not None:
        _playwright_isolation_config.idle_timeout = max(0.0, float(idle_timeout))


def get_playwright_isolation_config() -> PlaywrightIsolationConfig:
    return _playwright_isolation_config


def playwright_multiplex_blocker(
    server_name: str,
    server_config: dict,
    isolation: PlaywrightIsolationConfig | None = None,
) -> str | None:
    """Return why ``server_config`` cannot be multiplexed, or ``None`` when it can.

    Multiplexing means: spawn ``@playwright/mcp`` once in HTTP mode and open
    one MCP client per agent session. Upstream only hands each client its own
    ``BrowserContext`` when ``--isolated`` is set and
    ``--shared-browser-context`` is not; a persistent profile
    (``--user-data-dir`` / no ``--isolated``) is launched per client instead,
    which would fork Chromium per session and fight over the profile lock.
    """
    isolation = isolation or _playwright_isolation_config
    if server_name != PLAYWRIGHT_SERVER_NAME:
        return "not the playwright server"
    if not isolation.enabled:
        return "tools.mcp.playwright_per_session_context is false"
    if _determine_connection_type(server_config) != "stdio":
        return "playwright entry is URL-based; box-agent does not own the process"
    if not server_config.get("command"):
        return "playwright entry has no command"
    args = [str(a) for a in server_config.get("args", []) or []]
    flags = {arg.split("=", 1)[0] for arg in args}
    if "--isolated" not in args:
        return "--isolated missing (persistent profile cannot be shared per session)"
    if "--shared-browser-context" in flags:
        return "--shared-browser-context forces one context for every client"
    if "--user-data-dir" in flags:
        return "--user-data-dir pins a persistent profile"
    for flag in ("--port", "--host", "--allowed-hosts"):
        if flag in flags:
            return f"{flag} is already set in mcp.json; leave HTTP mode to the user"
    for flag in ("--cdp-endpoint", "--remote-endpoint", "--extension", "--config"):
        if flag in flags:
            return f"{flag} supplies browser ownership outside the isolated managed context"
    env = server_config.get("env") or {}
    for setting in (
        "SHARED_BROWSER_CONTEXT", "USER_DATA_DIR", "CDP_ENDPOINT", "REMOTE_ENDPOINT",
        "EXTENSION", "CONFIG", "PORT", "HOST", "ALLOWED_HOSTS",
    ):
        key = f"PLAYWRIGHT_MCP_{setting}"
        if env.get(key):
            return f"{key} overrides the managed browser configuration"
    return None


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((_MULTIPLEX_LOOPBACK_HOST, 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Orphan protection for owned HTTP MCP servers.
#
# A stdio MCP child dies with its pipe when box-agent exits. An HTTP child does
# not notice its parent going away, so we (1) kill live children at interpreter
# exit, (2) persist {owner_pid, port} records and, on the next start, ask any
# server whose owner is gone to shut itself down via upstream's
# ``GET /killkillkill`` endpoint. Records owned by a still-running process (e.g.
# a CLI session alongside the ACP server) are left alone.
# ---------------------------------------------------------------------------

_live_server_processes: "set[ManagedHttpServerProcess]" = set()
_atexit_registered = False


def _managed_server_state_path() -> Path:
    override = os.environ.get("BOX_AGENT_RUN_STATE_DIR")
    base = Path(override) if override else Path.home() / ".box-agent" / "run"
    return base / "managed-mcp-servers.json"


def _read_server_records() -> list[dict]:
    path = _managed_server_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _write_server_records(records: list[dict]) -> None:
    path = _managed_server_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(records, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as error:
        _warn(f"[mcp] could not persist managed server record: {error}")


def _record_server(process: "ManagedHttpServerProcess") -> None:
    records = [
        r for r in _read_server_records()
        if not (r.get("owner_pid") == os.getpid() and r.get("port") == process.port)
    ]
    records.append(
        {
            "name": process.name,
            "owner_pid": os.getpid(),
            "pid": process.pid,
            "port": process.port,
            "started_at": time.time(),
        }
    )
    _write_server_records(records)


def _forget_server(process: "ManagedHttpServerProcess") -> None:
    records = [
        r for r in _read_server_records()
        if not (r.get("owner_pid") == os.getpid() and r.get("port") == process.port)
    ]
    _write_server_records(records)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        synchronize = 0x00100000
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            # WAIT_TIMEOUT (0x102) → still running; WAIT_OBJECT_0 → exited.
            return kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _ask_server_to_exit(port: int, timeout: float = 2.0) -> bool:
    """Use upstream's ``/killkillkill`` endpoint; returns True when it answered."""
    url = f"http://{_MULTIPLEX_LOOPBACK_HOST}:{port}/killkillkill"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
    except Exception:  # noqa: BLE001
        return False
    return response.status_code == 200


async def reap_stale_managed_servers() -> list[dict]:
    """Shut down HTTP MCP servers whose owning box-agent process is gone."""
    records = _read_server_records()
    if not records:
        return []
    keep: list[dict] = []
    reaped: list[dict] = []
    for record in records:
        owner = int(record.get("owner_pid") or 0)
        port = int(record.get("port") or 0)
        if owner == os.getpid() or (owner and _pid_alive(owner)):
            keep.append(record)
            continue
        if port and await _ask_server_to_exit(port):
            reaped.append(record)
            _warn(
                f"[mcp] reaped orphaned {record.get('name', 'mcp')} server "
                f"port={port} (owner pid {owner} is gone)"
            )
        # Either reaped or nothing listening any more: drop the record.
    if len(keep) != len(records):
        _write_server_records(keep)
    return reaped


def _kill_live_servers_at_exit() -> None:
    for process in list(_live_server_processes):
        child = process.process
        if child is None or child.returncode is not None:
            continue
        try:
            child.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            _forget_server(process)
        except Exception:  # noqa: BLE001
            pass


def _track_server(process: "ManagedHttpServerProcess") -> None:
    global _atexit_registered
    _live_server_processes.add(process)
    _record_server(process)
    if not _atexit_registered:
        import atexit

        atexit.register(_kill_live_servers_at_exit)
        _atexit_registered = True


def _untrack_server(process: "ManagedHttpServerProcess") -> None:
    _live_server_processes.discard(process)
    _forget_server(process)


class ManagedHttpServerProcess:
    """Spawn a stdio-style MCP command in ``--port`` HTTP mode and own its lifetime.

    Used for ``@playwright/mcp``: the configured ``command``/``args`` from
    mcp.json are launched with ``--port P --host 127.0.0.1 --allowed-hosts
    127.0.0.1:P`` appended, then box-agent dials ``http://127.0.0.1:P/mcp``
    once per agent session. stderr of the child is forwarded to our stderr
    (stdout stays pure ACP protocol); stdout of the child is discarded.
    """

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str] | None,
        *,
        cwd: str | None = None,
        port: int | None = None,
    ):
        self.name = name
        self.command = command
        self.base_args = list(args)
        self.env = env
        self.cwd = cwd or tempfile.gettempdir()
        self.port = port
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._listening = asyncio.Event()

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("server process has no port yet")
        return f"http://{_MULTIPLEX_LOOPBACK_HOST}:{self.port}/mcp"

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def launch_args(self) -> list[str]:
        if self.port is None:
            raise RuntimeError("server process has no port yet")
        return [
            *self.base_args,
            "--port",
            str(self.port),
            "--host",
            _MULTIPLEX_LOOPBACK_HOST,
            "--allowed-hosts",
            f"{_MULTIPLEX_LOOPBACK_HOST}:{self.port}",
        ]

    def _spawn_env(self) -> dict[str, str]:
        # Mirror mcp.client.stdio: SDK default allowlist, overridden by the
        # server's own env from mcp.json (plus the Windows supplement).
        return {**get_default_environment(), **(self.env or {})}

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        prefix = f"[{self.name}] "
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                if "Listening on" in text:
                    self._listening.set()
                _warn(prefix + text)
        except (asyncio.CancelledError, ValueError):
            pass
        except Exception as error:  # noqa: BLE001
            _warn(f"{prefix}stderr drain stopped: {error}")

    async def _port_open(self) -> bool:
        try:
            _reader, writer = await asyncio.open_connection(
                _MULTIPLEX_LOOPBACK_HOST, self.port
            )
        except OSError:
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True

    async def start(self, ready_timeout: float) -> None:
        """Spawn the child and wait until its HTTP port accepts connections."""
        if self.port is None:
            self.port = _pick_free_loopback_port()
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = await asyncio.create_subprocess_exec(
            self.command,
            *self.launch_args(),
            env=self._spawn_env(),
            cwd=self.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"mcp-{self.name}-stderr"
        )
        _track_server(self)
        deadline = time.monotonic() + ready_timeout
        while True:
            if self.process.returncode is not None:
                _untrack_server(self)
                raise RuntimeError(
                    f"{self.name} MCP server exited with code {self.process.returncode} "
                    "before listening"
                )
            if await self._port_open():
                return
            if time.monotonic() >= deadline:
                await self.stop()
                raise TimeoutError(
                    f"{self.name} MCP server did not listen on port {self.port} "
                    f"within {ready_timeout}s"
                )
            await asyncio.sleep(_MULTIPLEX_READY_POLL_INTERVAL)

    async def stop(self) -> None:
        process = self.process
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), _MULTIPLEX_STOP_GRACE_SECONDS)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), _MULTIPLEX_STOP_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    _warn(f"[{self.name}] MCP server pid={process.pid} did not exit after kill")
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stderr_task = None
        _untrack_server(self)


class MCPTool(Tool):
    """Wrapper for MCP tools with timeout handling."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        session: ClientSession | None,
        server_name: str = "",
        remote_name: str | None = None,
        fixed_arguments: dict[str, Any] | None = None,
        execute_timeout: float | None = None,
        always_load: bool = False,
        connector_id: str | None = None,
        connector_name: str | None = None,
        concurrency_limiter: asyncio.Semaphore | None = None,
        session_lease: SessionLease | None = None,
    ):
        """
        ``session`` is the fixed ClientSession shared by every caller (classic
        one-connection-per-server mode). ``session_lease`` instead resolves a
        session per call — an async context manager factory such as
        ``PlaywrightSessionPool.lease`` — so each agent session can be routed to
        its own MCP client. Exactly one of the two must be provided.
        """
        if session is None and session_lease is None:
            raise ValueError("MCPTool requires either session or session_lease")
        self._name = name
        self._remote_name = remote_name or name
        self._server_name = server_name
        self._fixed_arguments = dict(fixed_arguments or {})
        self._session_lease = session_lease
        self._description = description
        self._parameters = parameters
        self._session = session
        self._execute_timeout = execute_timeout
        self._always_load = always_load
        self._connector_id = connector_id
        self._connector_name = connector_name
        self._concurrency_limiter = concurrency_limiter
        self._mcp_generation = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def remote_name(self) -> str:
        """Name used when calling the backing MCP server."""
        return self._remote_name

    @property
    def mcp_tool_id(self) -> str:
        return f"mcp:{self._server_name}/{self._name}"

    @property
    def mcp_connector_id(self) -> str | None:
        return self._connector_id

    @property
    def mcp_connector_name(self) -> str | None:
        return self._connector_name

    @property
    def mcp_generation(self) -> int:
        return self._mcp_generation

    @property
    def mcp_always_load(self) -> bool:
        return self._always_load

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs) -> ToolResult:
        """Execute MCP tool via the session with timeout protection."""
        timeout = self._execute_timeout or _default_timeout_config.execute_timeout
        is_web_extract = (
            self._server_name == "box-agent-web-extract"
            and self._remote_name == "web_extract"
        )
        browser_runtime_acquired = False
        release_browser_runtime_after_call = False
        concurrency_slot_acquired = False
        waiting_for_concurrency_slot = self._concurrency_limiter is not None

        try:
            # Legacy shared-browser mode only: one Playwright stdio session for
            # every agent session, so distinct turns must take turns. With a
            # session lease each agent session owns its BrowserContext and no
            # cross-session serialization is needed.
            if self._server_name == "playwright" and self._session_lease is None:
                try:
                    async with _timeout(min(timeout, 5.0)):
                        await acquire_browser_runtime_for_current_turn()
                        browser_runtime_acquired = True
                except TimeoutError:
                    return ToolResult(
                        success=False,
                        content="",
                        error=(
                            "BROWSER_RUNTIME_BUSY: Playwright is currently used by "
                            "another local-agent turn. Retry later or use a non-browser "
                            "fallback."
                        ),
                    )

            async with _timeout(timeout):
                if self._concurrency_limiter is not None:
                    await self._concurrency_limiter.acquire()
                    concurrency_slot_acquired = True
                    waiting_for_concurrency_slot = False
                call_arguments = {**kwargs, **self._fixed_arguments}
                if is_web_extract:
                    model_context = current_model_tool_context()
                    if model_context is not None:
                        call_arguments["model"] = model_context.model
                        call_arguments["max_output_tokens"] = (
                            model_context.max_output_tokens
                        )
                if self._session_lease is not None:
                    async with self._session_lease() as leased_session:
                        if (
                            self._server_name == PLAYWRIGHT_SERVER_NAME
                            and self._remote_name == "browser_close"
                        ):
                            # Upstream keeps this client's BrowserContext (and
                            # its windows) alive while other clients share the
                            # browser; close our tabs first so "close" is real.
                            try:
                                await close_all_tabs(leased_session)
                            except Exception as tab_error:  # noqa: BLE001
                                _warn(f"[browser] tab cleanup before close skipped: {tab_error}")
                        result = await leased_session.call_tool(
                            self._remote_name,
                            arguments=call_arguments,
                        )
                else:
                    result = await self._session.call_tool(
                        self._remote_name,
                        arguments=call_arguments,
                    )

            # MCP tool results are a list of content items
            content_parts = []
            inline_images: list[dict[str, str]] = []
            for item in result.content:
                if hasattr(item, "text"):
                    content_parts.append(item.text)
                elif hasattr(item, "data") and hasattr(item, "mimeType"):
                    data = item.data
                    mime_type = item.mimeType
                    if isinstance(data, str) and isinstance(mime_type, str):
                        inline_images.append({"data": data, "mime_type": mime_type})
                else:
                    content_parts.append(str(item))

            content_str = "\n".join(content_parts)

            is_error = result.isError if hasattr(result, "isError") else False

            structured_error = _structured_mcp_error_message(content_str)
            if is_error or structured_error:
                release_browser_runtime_after_call = self._server_name == "playwright"
                public_content = public_browser_tool_text(
                    self._server_name,
                    content_str,
                )
                err_msg = public_browser_tool_text(
                    self._server_name,
                    structured_error or content_str.strip() or "Tool returned error",
                )
                return ToolResult(
                    success=False,
                    content=public_content,
                    error=err_msg,
                )
            release_browser_runtime_after_call = (
                self._server_name == "playwright"
                and self._remote_name == "browser_snapshot"
            )
            return ToolResult(
                success=True,
                content=content_str,
                error=None,
                raw_output=(
                    {"mcp_inline_images": inline_images}
                    if inline_images
                    else None
                ),
            )

        except TimeoutError:
            release_browser_runtime_after_call = browser_runtime_acquired
            if waiting_for_concurrency_slot:
                error = (
                    f"MCP tool concurrency queue timed out after {timeout}s. "
                    "Too many tasks are using the shared WebSearch connection."
                )
            else:
                error = (
                    f"MCP tool execution timed out after {timeout}s. "
                    "The remote server may be slow or unresponsive."
                )
            return ToolResult(
                success=False,
                content="",
                error=error,
            )
        except BrowserSessionLimitError as e:
            return ToolResult(success=False, content="", error=str(e))
        except Exception as e:
            release_browser_runtime_after_call = browser_runtime_acquired
            error = public_browser_tool_text(self._server_name, str(e))
            return ToolResult(
                success=False,
                content="",
                error=f"MCP tool execution failed: {error}",
            )
        finally:
            if concurrency_slot_acquired and self._concurrency_limiter is not None:
                self._concurrency_limiter.release()
            if browser_runtime_acquired and release_browser_runtime_after_call:
                await release_browser_runtime_for_current_turn()


async def _open_streamable_http(
    exit_stack: AsyncExitStack,
    *,
    url: str | None,
    headers: dict[str, str] | None,
    auth: httpx.Auth | None,
    connect_timeout: float,
    sse_read_timeout: float,
):
    """Open a Streamable HTTP transport on ``exit_stack`` and return (read, write).

    Shared by the per-server discovery connection and the per-session
    Playwright clients so both speak the same MCP 1.x / 2.x dialect.
    """
    parameters = inspect.signature(streamable_http_client).parameters
    if "http_client" in parameters and "headers" not in parameters:
        # MCP 2.x moved headers/auth/timeouts onto a caller-owned httpx2
        # client and changed the transport result from three values to two.
        if create_mcp_http_client is None:
            raise RuntimeError(
                "MCP streamable HTTP requires create_mcp_http_client"
            )
        import httpx2

        transport_auth = auth
        if isinstance(auth, DynamicBearerAuth):
            dynamic_auth = auth

            class Httpx2DynamicBearerAuth(httpx2.Auth):
                """MCP 2.x auth hook retaining per-request token refresh."""

                async def async_auth_flow(self, request):
                    if "authorization" not in request.headers:
                        token = resolve_auth_token(
                            dynamic_auth.explicit_token,
                            dynamic_auth.auth_file,
                        )
                        if token and should_attach_auth_header(str(request.url)):
                            request.headers["Authorization"] = f"Bearer {token}"
                    yield request

            transport_auth = Httpx2DynamicBearerAuth()

        http_client_context = create_mcp_http_client(
            headers=headers if headers else None,
            timeout=httpx2.Timeout(
                connect=connect_timeout,
                read=sse_read_timeout,
                write=connect_timeout,
                pool=connect_timeout,
            ),
            auth=transport_auth,
        )
        http_client = await exit_stack.enter_async_context(http_client_context)
        streams = await exit_stack.enter_async_context(
            streamable_http_client(url=url, http_client=http_client)
        )
    else:
        # MCP 1.x accepts transport configuration directly and returns
        # (read, write, get_session_id).
        streams = await exit_stack.enter_async_context(
            streamable_http_client(
                url=url,
                headers=headers if headers else None,
                timeout=connect_timeout,
                sse_read_timeout=sse_read_timeout,
                auth=auth,
            )
        )
    read_stream, write_stream = streams[:2]
    return read_stream, write_stream


class MCPServerConnection:
    """Manages connection to a single MCP server (STDIO or URL-based) with timeout handling."""

    def __init__(
        self,
        name: str,
        connection_type: ConnectionType = "stdio",
        # STDIO params
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        # URL-based params
        url: str | None = None,
        headers: dict[str, str] | None = None,
        auth: httpx.Auth | None = None,
        # Timeout overrides (per-server)
        connect_timeout: float | None = None,
        execute_timeout: float | None = None,
        sse_read_timeout: float | None = None,
        always_load: bool = False,
        connector_id: str | None = None,
        connector_name: str | None = None,
    ):
        self.name = name
        self.connection_type = connection_type
        # STDIO
        self.command = command
        self.args = args or []
        self.env = env or {}
        # URL-based
        self.url = url
        self.headers = headers or {}
        self.auth = auth
        # Timeout settings (per-server overrides)
        self.connect_timeout = connect_timeout
        self.execute_timeout = execute_timeout
        self.sse_read_timeout = sse_read_timeout
        self.always_load = always_load
        self.connector_id = connector_id
        self.connector_name = connector_name
        self._web_search_concurrency_limiter: asyncio.Semaphore | None = None
        # Connection state
        self.last_error: str | None = None
        self.last_auth_status: int | None = None
        self.session: ClientSession | None = None
        self.exit_stack: AsyncExitStack | None = None
        self.tools: list[MCPTool] = []
        # ``stdio_http`` only: the server process we own and the per-session
        # client pool that hands every agent session its own BrowserContext.
        self.server_process: ManagedHttpServerProcess | None = None
        self.session_pool: PlaywrightSessionPool | None = None

    @property
    def transport_label(self) -> str:
        """Human-readable transport for status reporting (command for stdio-style)."""
        if self.connection_type in ("stdio", "stdio_http"):
            return self.command or ""
        return self.url or self.command or ""

    def _get_connect_timeout(self) -> float:
        """Get effective connect timeout."""
        return self.connect_timeout or _default_timeout_config.connect_timeout

    def _get_sse_read_timeout(self) -> float:
        """Get effective SSE read timeout."""
        return self.sse_read_timeout or _default_timeout_config.sse_read_timeout

    def _get_execute_timeout(self) -> float:
        """Get effective execute timeout."""
        return self.execute_timeout or _default_timeout_config.execute_timeout

    def _concurrency_limiter_for_tool(
        self,
        tool_name: str,
    ) -> asyncio.Semaphore | None:
        if tool_name != "web_search":
            return None
        if self._web_search_concurrency_limiter is None:
            self._web_search_concurrency_limiter = asyncio.Semaphore(
                WEB_SEARCH_MCP_MAX_CONCURRENCY
            )
        return self._web_search_concurrency_limiter

    async def connect(self) -> bool:
        """Connect to the MCP server with timeout protection."""
        self.last_auth_status = None
        connect_timeout = self._get_connect_timeout()
        started_at = time.monotonic()
        stage = "open-transport"

        def elapsed_ms() -> int:
            return round((time.monotonic() - started_at) * 1000)

        command_label = (
            f" command={self.command!r}"
            if self.connection_type in ("stdio", "stdio_http")
            else ""
        )
        _warn(
            f"[mcp] connect:start server={self.name!r} transport={self.connection_type} "
            f"timeout_s={connect_timeout}{command_label}"
        )

        async def _close_exit_stack() -> BaseException | None:
            cleanup_error: BaseException | None = None
            if self.exit_stack:
                try:
                    await self.exit_stack.aclose()
                except BaseException as error:  # noqa: BLE001
                    cleanup_error = error
                finally:
                    self.exit_stack = None
                    self.session = None
            # A failed multiplexed connect must not leave an orphan HTTP server.
            await self._shutdown_multiplexed_server()
            return cleanup_error

        def _warn_unexpected_cleanup_error(cleanup_error: BaseException | None) -> None:
            if cleanup_error is None or _is_mcp_authentication_error(cleanup_error):
                return
            _warn(f"⚠ Failed to clean up MCP connection '{self.name}': {cleanup_error}")

        try:
            self.exit_stack = AsyncExitStack()
            session_lease: SessionLease | None = None

            # Wrap connection with timeout
            async with _timeout(connect_timeout):
                if self.connection_type == "stdio_http":
                    stage = "spawn-http-server"
                    await self._start_multiplexed_server(connect_timeout)
                    stage = "open-http-transport"
                    read_stream, write_stream = await self._connect_streamable_http()
                elif self.connection_type == "stdio":
                    stage = "open-stdio-transport"
                    read_stream, write_stream = await self._connect_stdio()
                elif self.connection_type == "sse":
                    stage = "open-sse-transport"
                    read_stream, write_stream = await self._connect_sse()
                else:  # http / streamable_http
                    stage = "open-http-transport"
                    read_stream, write_stream = await self._connect_streamable_http()

                _warn(
                    f"[mcp] connect:transport-open server={self.name!r} "
                    f"stage={stage} elapsed_ms={elapsed_ms()}"
                )

                # Enter client session context
                session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
                self.session = session

                # Initialize the session
                stage = "initialize-session"
                await session.initialize()
                _warn(
                    f"[mcp] connect:initialized server={self.name!r} "
                    f"elapsed_ms={elapsed_ms()}"
                )

                # List available tools
                stage = "list-tools"
                tools_list = await session.list_tools()
                _warn(
                    f"[mcp] connect:tools-listed server={self.name!r} "
                    f"elapsed_ms={elapsed_ms()} tool_count={len(tools_list.tools)}"
                )

            if self.connection_type == "stdio_http":
                # The connection above was only a discovery client (no tool call
                # → upstream never launched a browser for it). Drop it so the
                # only live clients are the per-session ones from the pool.
                stage = "close-discovery-client"
                await self._close_discovery_client()
                self.session_pool = self._build_session_pool()
                session_lease = self.session_pool.lease
                session = None

            # Wrap each tool with execute timeout
            execute_timeout = self._get_execute_timeout()
            for tool in tools_list.tools:
                parameters = tool.inputSchema if hasattr(tool, "inputSchema") else {}
                public_name = _public_mcp_tool_name(
                    self.name,
                    tool.name,
                    self.connector_id,
                )
                fixed_arguments = browser_tool_fixed_arguments(self.name, parameters)
                parameters = public_browser_tool_parameters(parameters, fixed_arguments)
                mcp_tool = MCPTool(
                    name=public_name,
                    remote_name=tool.name,
                    description=public_browser_tool_description(
                        self.name,
                        tool.description or "",
                    ),
                    parameters=parameters,
                    session=session,
                    server_name=self.name,
                    fixed_arguments=fixed_arguments,
                    execute_timeout=execute_timeout,
                    always_load=_mcp_tool_always_load(self.name, tool, self.always_load),
                    connector_id=self.connector_id,
                    connector_name=self.connector_name,
                    concurrency_limiter=self._concurrency_limiter_for_tool(tool.name),
                    session_lease=session_lease,
                )
                self.tools.append(mcp_tool)

            conn_info = self.url if self.url else self.command
            if self.connection_type == "stdio_http" and self.server_process is not None:
                conn_info = (
                    f"{self.command} → {self.server_process.url} "
                    f"pid={self.server_process.pid} per-session contexts, "
                    f"max {self.session_pool.max_clients}"
                )
            _warn(f"✓ Connected to MCP server '{self.name}' ({self.connection_type}: {conn_info}) - loaded {len(self.tools)} tools")
            for tool in self.tools:
                desc = tool.description[:60] if len(tool.description) > 60 else tool.description
                _warn(f"  - {tool.name}: {desc}...")
            self.last_error = None
            return True

        except TimeoutError as e:
            self.last_error = f"Connection timed out after {connect_timeout}s during {stage}"
            cleanup_error = await _close_exit_stack()
            self.last_auth_status = _mcp_auth_status(e, cleanup_error)
            auth_failure = self.last_auth_status is not None
            if auth_failure:
                self.last_error = _mcp_connection_error_message(e, cleanup_error)
                _warn(
                    f"[mcp] connect:failed server={self.name!r} stage={stage} "
                    f"elapsed_ms={elapsed_ms()} error={self.last_error}"
                )
            else:
                _warn(
                    f"[mcp] connect:timeout server={self.name!r} stage={stage} "
                    f"timeout_s={connect_timeout} elapsed_ms={elapsed_ms()} error={self.last_error}"
                )
            _warn_unexpected_cleanup_error(cleanup_error)
            return False

        except asyncio.CancelledError as e:
            cleanup_error = await _close_exit_stack()
            self.last_auth_status = _mcp_auth_status(e, cleanup_error)
            self.last_error = _mcp_connection_error_message(e, cleanup_error)
            if self.last_auth_status is not None:
                _warn(
                    f"[mcp] connect:failed server={self.name!r} stage={stage} "
                    f"elapsed_ms={elapsed_ms()} error={self.last_error}"
                )
            else:
                _warn(
                    f"[mcp] connect:cancelled server={self.name!r} stage={stage} "
                    f"elapsed_ms={elapsed_ms()} error={self.last_error}"
                )
            _warn_unexpected_cleanup_error(cleanup_error)
            return False

        except Exception as e:
            cleanup_error = await _close_exit_stack()
            self.last_auth_status = _mcp_auth_status(e, cleanup_error)
            self.last_error = _mcp_connection_error_message(e, cleanup_error)
            _warn(
                f"[mcp] connect:failed server={self.name!r} stage={stage} "
                f"elapsed_ms={elapsed_ms()} error={self.last_error}"
            )
            _warn_unexpected_cleanup_error(cleanup_error)
            import traceback

            traceback.print_exc()
            return False

    # Windows system variables that non-trivial CLIs (ssh.exe, git.exe, etc.)
    # rely on during early process init but that the MCP SDK's default env
    # allowlist (``mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS``) does not
    # include. Supplementing these fixed sshmcp startup on Windows; the SDK's
    # allowlist covers PATH / APPDATA / SYSTEMROOT / ... but omits e.g.
    # ``ComSpec`` and ``ProgramData``, which ssh's DLL init path resolves.
    #
    # Casing note: Windows itself treats env-var names case-insensitively, so
    # the exact spelling below is only a readability convention — we do NOT
    # depend on any casing invariant surviving into CreateProcess. An earlier
    # revision of this code tried to enforce single-casing on both sides of
    # the SDK's merge; that was found to be unnecessary and has been removed.
    _WINDOWS_ENV_SUPPLEMENT = (
        "windir",
        "ComSpec",
        "ProgramData",
        "ALLUSERSPROFILE",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramW6432",
        "USERDOMAIN",
    )

    def _build_stdio_env(self) -> dict[str, str] | None:
        """Build the ``env`` argument passed to ``StdioServerParameters``.

        Windows-only fix. On macOS and Linux the behavior is unchanged: we
        return the server's own ``env`` (or ``None`` so the SDK applies its
        default inherited-env allowlist).

        On Windows the MCP SDK's ``get_default_environment()`` allowlist is
        conservative and omits several variables that non-trivial CLIs read
        at startup — see ``_WINDOWS_ENV_SUPPLEMENT`` above. Without them,
        ssh.exe (as launched by sshmcp) exits before emitting any output and
        the MCP loader only sees the stdout pipe close. We supplement those
        variables from the host process's own environment; ``os.environ``
        lookups on Windows are case-insensitive, so we don't chase casing.

        Important interaction with the SDK: ``stdio_client()`` re-merges the
        env we return as ``{**get_default_environment(), **server.env}``
        (see ``mcp/client/stdio/__init__.py``). That means:

          1. Server-specific ``env`` entries from ``mcp.json`` win over both
             the SDK defaults and our supplement (Python dict merge order).
          2. Supplemented variables end up in the final env because the SDK
             defaults don't declare them, so nothing overrides them.

        The regression test in ``tests/test_mcp.py`` exercises the SDK's
        merge and asserts the final env shape, so future SDK changes to
        that merge order are caught in CI.
        """
        if sys.platform != "win32":
            return self.env if self.env else None

        # Start from the server's env from mcp.json — its keys will remain
        # ours to override once the SDK re-merges with its defaults.
        env: dict[str, str] = dict(self.env or {})

        for name in self._WINDOWS_ENV_SUPPLEMENT:
            value = os.environ.get(name)
            if value is None:
                continue
            # Respect a case-variant already supplied via mcp.json; users
            # who set ``PROGRAMDATA=...`` explicitly should not be shadowed
            # by our CamelCase copy.
            if any(k.lower() == name.lower() for k in env):
                continue
            env[name] = value

        return env or None

    async def _connect_stdio(self):
        """Connect via STDIO transport.

        Force a writable cwd (system temp) so child processes (e.g.
        @playwright/mcp) can create working directories without hitting
        permission errors on protected paths like ``C:\\Program Files\\``.
        """
        server_params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self._build_stdio_env(),
            cwd=tempfile.gettempdir(),
        )
        return await self.exit_stack.enter_async_context(stdio_client(server_params))

    async def _connect_sse(self):
        """Connect via SSE transport with timeout parameters."""
        connect_timeout = self._get_connect_timeout()
        sse_read_timeout = self._get_sse_read_timeout()

        return await self.exit_stack.enter_async_context(
            sse_client(
                url=self.url,
                headers=self.headers if self.headers else None,
                timeout=connect_timeout,
                sse_read_timeout=sse_read_timeout,
                auth=self.auth,
            )
        )

    async def _connect_streamable_http(self):
        """Connect via Streamable HTTP transport with timeout parameters."""
        return await _open_streamable_http(
            self.exit_stack,
            url=self.url,
            headers=self.headers,
            auth=self.auth,
            connect_timeout=self._get_connect_timeout(),
            sse_read_timeout=self._get_sse_read_timeout(),
        )

    # ------------------------------------------------------------------
    # stdio_http (Playwright multiplexing) helpers
    # ------------------------------------------------------------------
    async def _start_multiplexed_server(self, ready_timeout: float) -> None:
        """Spawn the configured command in HTTP mode and point ``self.url`` at it."""
        try:
            await reap_stale_managed_servers()
        except Exception as error:  # noqa: BLE001
            _warn(f"[mcp] stale server reap skipped: {error}")
        self.server_process = ManagedHttpServerProcess(
            name=self.name,
            command=self.command or "",
            args=self.args,
            env=self._build_stdio_env(),
        )
        await self.server_process.start(ready_timeout)
        self.url = self.server_process.url
        _warn(
            f"[mcp] {self.name}: spawned HTTP MCP server pid={self.server_process.pid} "
            f"url={self.url}"
        )

    async def _close_discovery_client(self) -> None:
        if self.exit_stack is None:
            return
        try:
            await self.exit_stack.aclose()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.exit_stack = None
            self.session = None

    def _build_session_pool(self) -> PlaywrightSessionPool:
        isolation = get_playwright_isolation_config()
        connect_timeout = self._get_connect_timeout()

        async def _session_factory(exit_stack: AsyncExitStack) -> ClientSession:
            async with _timeout(connect_timeout):
                read_stream, write_stream = await _open_streamable_http(
                    exit_stack,
                    url=self.url or "",
                    headers=self.headers,
                    auth=self.auth,
                    connect_timeout=connect_timeout,
                    sse_read_timeout=self._get_sse_read_timeout(),
                )
                session = await exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()
            return session

        return PlaywrightSessionPool(
            _session_factory,
            max_clients=isolation.max_clients,
            idle_timeout=isolation.idle_timeout,
        )

    async def _shutdown_multiplexed_server(self) -> None:
        pool, self.session_pool = self.session_pool, None
        process, self.server_process = self.server_process, None
        try:
            if pool is not None:
                try:
                    await pool.close_all(final=True)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if process is not None:
                await process.stop()

    async def disconnect(self):
        """Properly disconnect from the MCP server."""
        if self.exit_stack:
            try:
                await self.exit_stack.aclose()
            except Exception:
                # anyio cancel scope may raise RuntimeError or ExceptionGroup
                # when stdio_client's task group is closed from a different
                # task context during shutdown.
                pass
            finally:
                self.exit_stack = None
                self.session = None
        await self._shutdown_multiplexed_server()


# Global connections registry
_mcp_connections: list[MCPServerConnection] = []
_mcp_reconnect_locks: dict[str, asyncio.Lock] = {}
_mcp_source_reconcile_lock = asyncio.Lock()


@dataclass
class McpServerStatus:
    name: str
    state: str  # connecting | connected | failed | disabled
    owner: str = "user"
    config_id: str = ""
    connector_id: str | None = None
    connector_name: str | None = None
    source_path: str = ""
    transport: str = ""
    tool_count: int = 0
    tools: list = field(default_factory=list)
    error: str | None = None
    auth_status: int | None = None


_mcp_status: dict[str, McpServerStatus] = {}
_mcp_loading: bool = False
_mcp_config_path: str | None = None
_mcp_sources: tuple[McpConfigSource, ...] = ()
_mcp_server_definitions: dict[str, ResolvedMcpServer] = {}
_mcp_source_overrides: dict[str, dict[str, dict]] = {}
_mcp_runtime_credentials: dict[str, dict[str, str]] = {}
_mcp_runtime_credential_versions: dict[str, int] = {}
# Auth inputs from the last load_mcp_tools_async() call — reused by
# reconnect_mcp_server() so a single-server hot reconnect gets the same
# DynamicBearer / Authorization headers the cold-start path would build.
_mcp_auth_file: str = ""
_mcp_auth_token: str = ""
_mcp_auth_fingerprint: str = ""


def _current_mcp_auth_fingerprint() -> str:
    """Return a non-reversible marker for the currently resolved login token."""
    token = resolve_auth_token(_mcp_auth_token, _mcp_auth_file)
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_mcp_loading() -> bool:
    return _mcp_loading or get_mcp_tool_catalog().loading


def get_mcp_config_path() -> str | None:
    return _mcp_config_path


def get_mcp_config_paths() -> dict[str, str]:
    return {source.owner: str(source.path) for source in _mcp_sources}


def get_mcp_tools_for_server(name: str) -> list:
    """Return the Tool objects currently held by a connected server."""
    conn = next((c for c in _mcp_connections if c.name == name), None)
    return list(conn.tools) if conn else []


def get_all_mcp_tools() -> list:
    """Return current MCP tools in connection registration order."""
    return [tool for connection in _mcp_connections for tool in connection.tools]


async def disconnect_mcp_server(name: str) -> dict:
    """Disconnect a named MCP server and return its removed tool names."""
    global _mcp_connections
    conn = next((c for c in _mcp_connections if c.name == name), None)
    removed_tools: list[str] = []
    if conn:
        removed_tools = [t.name for t in conn.tools]
        try:
            await conn.disconnect()
        except Exception:
            pass
        _mcp_connections = [c for c in _mcp_connections if c.name != name]
    get_mcp_tool_catalog().remove_server(name)
    _record_status(name, "disabled")
    return {"success": True, "removedTools": removed_tools}


def _record_status(
    name: str,
    state: str,
    transport: str = "",
    tool_count: int = 0,
    tools: list | None = None,
    error: str | None = None,
    auth_status: int | None = None,
    definition: ResolvedMcpServer | None = None,
) -> None:
    if auth_status is None and error:
        if "HTTP 401" in error or "Authentication failed" in error:
            auth_status = 401
        elif "HTTP 403" in error or "Authorization failed" in error:
            auth_status = 403
    definition = definition or _mcp_server_definitions.get(name)
    _mcp_status[name] = McpServerStatus(
        name=name,
        state=state,
        owner=definition.owner if definition else "user",
        config_id=definition.config_id if definition else f"custom-mcp:{name}",
        connector_id=definition.connector_id if definition else None,
        connector_name=definition.connector_name if definition else None,
        source_path=definition.source_path if definition else (_mcp_config_path or ""),
        transport=transport,
        tool_count=tool_count, tools=tools or [], error=error,
        auth_status=auth_status,
    )


def get_playwright_session_pool() -> PlaywrightSessionPool | None:
    """Return the live per-session Playwright client pool, if multiplexing is active."""
    conn = next((c for c in _mcp_connections if c.name == PLAYWRIGHT_SERVER_NAME), None)
    return conn.session_pool if conn is not None else None


def get_browser_isolation_status() -> dict | None:
    """Describe the managed-browser isolation mode for ``_mcp/status`` consumers."""
    conn = next((c for c in _mcp_connections if c.name == PLAYWRIGHT_SERVER_NAME), None)
    if conn is None:
        return None
    if conn.session_pool is not None:
        snapshot = conn.session_pool.snapshot()
        if conn.server_process is not None:
            snapshot["pid"] = conn.server_process.pid
            snapshot["url"] = conn.server_process.url
        return snapshot
    return {"mode": "shared_context"}


async def close_browser_session(session_key: str) -> bool:
    """Close the managed BrowserContext owned by ``session_key`` (no-op if none)."""
    pool = get_playwright_session_pool()
    if pool is None:
        return False
    return await pool.close(session_key)


def get_mcp_status() -> list[dict]:
    browser = get_browser_isolation_status()
    statuses = []
    for s in _mcp_status.values():
        entry = {
            "name": s.name,
            "owner": s.owner,
            "configId": s.config_id,
            "connectorId": s.connector_id,
            "connectorName": s.connector_name,
            "sourcePath": s.source_path,
            "state": s.state,
            "transport": s.transport,
            "toolCount": s.tool_count,
            "tools": s.tools,
            "error": s.error,
            "authStatus": s.auth_status,
        }
        if s.name == PLAYWRIGHT_SERVER_NAME and browser is not None:
            entry["browser"] = browser
        statuses.append(entry)
    return statuses


def _determine_connection_type(server_config: dict) -> ConnectionType:
    """Determine connection type from server config."""
    explicit_type = str(server_config.get("type") or server_config.get("transport") or "").lower()
    if explicit_type in ("stdio", "sse", "http", "streamable_http"):
        return explicit_type
    # Auto-detect: if url exists, default to streamable_http; otherwise stdio
    if server_config.get("url"):
        return "streamable_http"
    return "stdio"


def _effective_connection_type(server_name: str, server_config: dict) -> ConnectionType:
    """Connection type after applying Playwright per-session multiplexing."""
    conn_type = _determine_connection_type(server_config)
    if server_name == PLAYWRIGHT_SERVER_NAME and conn_type == "stdio":
        blocker = playwright_multiplex_blocker(server_name, server_config)
        if blocker is None:
            return "stdio_http"
        _warn(
            "[mcp] playwright: per-session browser contexts disabled "
            f"({blocker}); falling back to one shared stdio session"
        )
    return conn_type


def set_mcp_runtime_credential(credential_ref: str, headers: dict[str, str]) -> list[str]:
    """Store connector credentials in memory and return affected server names.

    Credential material is deliberately excluded from MCP config files and status
    payloads. The desktop host must restore it after every Box-Agent restart.
    """

    normalized_ref = credential_ref.strip()
    if not normalized_ref:
        raise ValueError("credentialRef is required")
    normalized_headers = {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).strip() and str(value).strip()
    }
    if not normalized_headers:
        raise ValueError("credential headers are required")
    _mcp_runtime_credentials[normalized_ref] = normalized_headers
    _mcp_runtime_credential_versions[normalized_ref] = (
        _mcp_runtime_credential_versions.get(normalized_ref, 0) + 1
    )
    return [
        name
        for name, definition in _mcp_server_definitions.items()
        if definition.owner == "connector" and definition.config.get("credentialRef") == normalized_ref
    ]


def get_mcp_connector_server_names() -> dict[str, frozenset[str]]:
    """Return every configured MCP server name expected for each connector."""
    names_by_connector: dict[str, set[str]] = {}
    for definition in _mcp_server_definitions.values():
        if definition.owner != "connector" or not definition.connector_id:
            continue
        names_by_connector.setdefault(definition.connector_id, set()).add(definition.name)
    return {
        connector_id: frozenset(server_names)
        for connector_id, server_names in names_by_connector.items()
    }


def clear_mcp_runtime_credential(credential_ref: str) -> list[str]:
    normalized_ref = credential_ref.strip()
    _mcp_runtime_credentials.pop(normalized_ref, None)
    _mcp_runtime_credential_versions[normalized_ref] = (
        _mcp_runtime_credential_versions.get(normalized_ref, 0) + 1
    )
    return [
        name
        for name, definition in _mcp_server_definitions.items()
        if definition.owner == "connector" and definition.config.get("credentialRef") == normalized_ref
    ]


def _materialize_server_config(definition: ResolvedMcpServer) -> dict:
    server_config = dict(definition.config)
    credential_ref = server_config.pop("credentialRef", None)
    if credential_ref is not None and definition.owner != "connector":
        raise ValueError("credentialRef requires a connector-owned source")
    server_config.pop("_connectorId", None)
    server_config.pop("_connectorName", None)
    if isinstance(credential_ref, str):
        credential_headers = _mcp_runtime_credentials.get(credential_ref)
        if credential_headers:
            server_config["headers"] = {
                **dict(server_config.get("headers") or {}),
                **credential_headers,
            }
    return server_config


def _waiting_for_credential(definition: ResolvedMcpServer) -> bool:
    credential_ref = definition.config.get("credentialRef")
    return isinstance(credential_ref, str) and not _mcp_runtime_credentials.get(credential_ref)


def _build_connection(definition: ResolvedMcpServer) -> "MCPServerConnection":
    server_config = _materialize_server_config(definition)
    conn_type = _effective_connection_type(definition.name, server_config)
    url = server_config.get("url")
    configured_headers = server_config.get("headers", {})
    auth = _dynamic_bearer_auth_for_url(
        url=url,
        headers=configured_headers,
        auth_file=_mcp_auth_file,
        auth_token=_mcp_auth_token,
    )
    connection_headers = (
        configured_headers
        if auth is not None
        else request_auth_headers(
            auth_file=_mcp_auth_file,
            explicit_token=_mcp_auth_token,
            existing=configured_headers,
            url=url,
        )
    )
    return MCPServerConnection(
        name=definition.name,
        connection_type=conn_type,
        command=server_config.get("command"),
        args=server_config.get("args", []),
        env=server_config.get("env", {}),
        url=url,
        headers=connection_headers,
        auth=auth,
        connect_timeout=server_config.get("connect_timeout"),
        execute_timeout=server_config.get("execute_timeout"),
        sse_read_timeout=server_config.get("sse_read_timeout"),
        always_load=bool(server_config.get("alwaysLoad", False)),
        connector_id=definition.connector_id,
        connector_name=definition.connector_name,
    )


def _resolve_registered_sources(config_path: str) -> dict[str, ResolvedMcpServer]:
    global _mcp_sources
    _mcp_sources = configured_mcp_sources(config_path)
    if "connector" in _mcp_source_overrides and not any(
        source.owner == "connector" for source in _mcp_sources
    ):
        connector_source = McpConfigSource("connector", Path("<runtime:connector>"))
        sources = list(_mcp_sources)
        user_index = next(
            (index for index, source in enumerate(sources) if source.owner == "user"),
            len(sources),
        )
        sources.insert(user_index, connector_source)
        _mcp_sources = tuple(sources)
    raw_reserved_names = os.environ.get("BOX_AGENT_RESERVED_MCP_SERVER_NAMES", "")
    reserved_names: set[str] = set()
    if raw_reserved_names:
        try:
            parsed_reserved_names = json.loads(raw_reserved_names)
            if isinstance(parsed_reserved_names, list):
                reserved_names = {
                    name.strip()
                    for name in parsed_reserved_names
                    if isinstance(name, str) and name.strip()
                }
        except json.JSONDecodeError:
            _warn("Ignoring invalid BOX_AGENT_RESERVED_MCP_SERVER_NAMES JSON")
    resolved = resolve_mcp_sources(
        _mcp_sources,
        _mcp_runtime_credential_versions,
        reserved_names,
        _mcp_source_overrides,
    )
    for conflict in resolved.conflicts:
        _warn(f"Skipping conflicting MCP server: {conflict}")
    return resolved.servers


def _resolve_mcp_config_path(config_path: str) -> Path | None:
    """
    Resolve MCP config path with fallback logic.

    Priority:
    1. If the specified path exists, use it
    2. If mcp.json doesn't exist, try mcp-example.json in the same directory
    3. Return None if no config found

    Args:
        config_path: User-specified config path

    Returns:
        Resolved Path object or None if not found
    """
    config_file = Path(config_path)

    # If specified path exists, use it directly
    if config_file.exists():
        return config_file

    # Fallback: if looking for mcp.json, try mcp-example.json
    if config_file.name == "mcp.json":
        example_file = config_file.parent / "mcp-example.json"
        if example_file.exists():
            _warn(f"mcp.json not found, using template: {example_file}")
            return example_file

    return None


async def load_mcp_tools_async(
    config_path: str = "mcp.json",
    auth_token: str = "",
    auth_file: str = "",
) -> list[Tool]:
    """
    Load MCP tools from config file.

    This function:
    1. Reads the MCP config file (with fallback to mcp-example.json)
    2. Connects to each server (STDIO or URL-based)
    3. Fetches tool definitions
    4. Wraps them as Tool objects

    Supported config formats:
    - STDIO: {"command": "...", "args": [...], "env": {...}}
    - URL-based: {"url": "https://...", "type": "sse|http|streamable_http", "headers": {...}}

    Per-server timeout overrides (optional):
    - "connect_timeout": float - Connection timeout in seconds
    - "execute_timeout": float - Tool execution timeout in seconds
    - "sse_read_timeout": float - SSE read timeout in seconds

    Note:
    - If mcp.json is not found, will automatically fallback to mcp-example.json
    - User-specific mcp.json should be created by copying mcp-example.json

    Args:
        config_path: Path to MCP configuration file (default: "mcp.json")
        auth_token: Optional in-memory product login token.
        auth_file: Optional auth.json path read before connecting URL-based
            MCP servers that do not define their own Authorization.

    Returns:
        List of Tool objects representing MCP tools
    """
    global _mcp_connections, _mcp_status, _mcp_loading, _mcp_config_path
    global _mcp_server_definitions
    global _mcp_auth_file, _mcp_auth_token, _mcp_auth_fingerprint
    _mcp_loading = True
    catalog = get_mcp_tool_catalog()
    catalog.mark_loading()
    # Remember the auth inputs so reconnect_mcp_server() can rebuild the same
    # dynamic bearer / Authorization headers it would have used on cold start.
    _mcp_auth_file = auth_file
    _mcp_auth_token = auth_token
    _mcp_auth_fingerprint = _current_mcp_auth_fingerprint()
    try:
        config_file = _resolve_mcp_config_path(config_path)
        if config_file is None and not any(
            os.environ.get(name, "").strip()
            for name in (
                "BOX_AGENT_USER_MCP_CONFIG_PATH",
                "BOX_AGENT_SYSTEM_MCP_CONFIG_PATH",
                "BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH",
            )
        ):
            _warn(f"MCP config not found: {config_path}")
            return []

        effective_path = str(config_file or Path(config_path).expanduser())
        _mcp_config_path = effective_path
        _mcp_server_definitions = _resolve_registered_sources(effective_path)

        if not _mcp_server_definitions:
            import sys as _sys
            _sys.stderr.write("No MCP servers configured\n")
            return []

        connections: list[MCPServerConnection] = []

        # Build connection objects for each enabled server
        for server_name, definition in _mcp_server_definitions.items():
            server_config = _materialize_server_config(definition)
            if server_config.get("disabled", False):
                _warn(f"Skipping disabled server: {server_name}")
                _record_status(server_name, "disabled", definition=definition)
                continue

            if _waiting_for_credential(definition):
                _record_status(
                    server_name, "connecting",
                    error="Waiting for host credential", definition=definition,
                )
                continue

            conn_type = _effective_connection_type(server_name, server_config)
            url = server_config.get("url")
            command = server_config.get("command")

            # Validate config
            if conn_type in ("stdio", "stdio_http") and not command:
                _warn(f"No command specified for STDIO server: {server_name}")
                continue
            if conn_type in ("sse", "http", "streamable_http") and not url:
                _warn(f"No url specified for {conn_type.upper()} server: {server_name}")
                continue

            connections.append(_build_connection(definition))

        # Connect to all servers in parallel — one slow/broken server no
        # longer blocks the others. Each connection has its own timeout.

        # Seed connecting state before gather so UI shows spinner during window
        for conn in connections:
            _record_status(conn.name, "connecting", transport=conn.transport_label)

        results = await asyncio.gather(
            *(conn.connect() for conn in connections),
            return_exceptions=True,
        )

        all_tools = []
        for conn, success in zip(connections, results):
            if isinstance(success, BaseException):
                _warn(f"✗ MCP server '{conn.name}' raised during connect: {success}")
                _record_status(
                    conn.name, "failed",
                    transport=conn.transport_label,
                    error=str(success),
                    auth_status=_mcp_auth_status(success),
                )
                continue
            if success:
                _mcp_connections.append(conn)
                _replace_server_catalog(conn)
                all_tools.extend(conn.tools)
                _record_status(
                    conn.name, "connected",
                    transport=conn.transport_label,
                    tool_count=len(conn.tools),
                    tools=[t.name for t in conn.tools],
                )
            else:
                _record_status(
                    conn.name, "failed",
                    transport=conn.transport_label,
                    error=conn.last_error or "connect() returned False",
                    auth_status=conn.last_auth_status,
                )

        _warn(f"Total MCP tools loaded: {len(all_tools)}")

        return all_tools

    except Exception as e:
        _warn(f"Error loading MCP config: {e}")
        import traceback

        traceback.print_exc()
        return []

    finally:
        _mcp_loading = False
        catalog.mark_ready()


async def cleanup_mcp_connections():
    """Clean up all MCP connections."""
    global _mcp_connections, _mcp_server_definitions, _mcp_sources
    for connection in _mcp_connections:
        await connection.disconnect()
    _mcp_connections.clear()
    _mcp_reconnect_locks.clear()
    _mcp_server_definitions = {}
    _mcp_sources = ()
    _mcp_source_overrides.clear()
    _mcp_status.clear()
    get_mcp_tool_catalog().clear()


async def reconnect_mcp_server(name: str) -> dict:
    """Serialize hot reconnects for one server name."""
    global _mcp_server_definitions
    lock = _mcp_reconnect_locks.setdefault(name, asyncio.Lock())
    async with lock:
        if _mcp_config_path:
            try:
                # Reading another server's new credential version does not mean
                # its existing connection has used it. Update this server only.
                current = _resolve_registered_sources(_mcp_config_path)
                if name in current:
                    _mcp_server_definitions[name] = current[name]
                else:
                    _mcp_server_definitions.pop(name, None)
            except Exception as error:
                return {"success": False, "error": str(error)}
        return await _reconnect_mcp_server_locked(name)


async def _mcp_startup_ready() -> bool:
    """Keep source updates behind the cold-discovery publication boundary."""
    catalog = get_mcp_tool_catalog()
    if _mcp_loading or catalog.initial_loading:
        return await catalog.wait_until_ready(
            timeout=max(5.0, get_mcp_timeout_config().connect_timeout)
        )
    return True


async def reconcile_mcp_sources(source: str | None = None) -> dict:
    """Apply source-file changes without restarting unrelated MCP servers."""

    async with _mcp_source_reconcile_lock:
        if not await _mcp_startup_ready():
            return {"success": False, "error": "MCP startup is still loading; retry the source update after readiness."}
        return await _reconcile_mcp_sources_locked(source)


async def replace_mcp_source(
    source: str, config: dict, connector_ids: list[str] | None = None,
) -> dict:
    """Replace a host-managed MCP source in memory and reconcile its connections."""

    if source != "connector":
        return {"success": False, "error": f"Unsupported runtime MCP source: {source}"}
    if not isinstance(config, dict):
        return {"success": False, "error": "config must be an object"}
    raw_servers = config.get("mcpServers", {})
    if not isinstance(raw_servers, dict):
        return {"success": False, "error": "mcpServers must be an object"}
    servers: dict[str, dict] = {}
    for name, server_config in raw_servers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(server_config, dict):
            return {"success": False, "error": "Invalid MCP server entry"}
        servers[name] = dict(server_config)

    if connector_ids is not None and (
        not isinstance(connector_ids, list) or not connector_ids
        or any(not isinstance(item, str) or not item.strip() for item in connector_ids)
    ):
        return {"success": False, "error": "connectorIds must be a non-empty list for the connector source"}

    async with _mcp_source_reconcile_lock:
        if not await _mcp_startup_ready():
            return {"success": False, "error": "MCP startup is still loading; retry the source update after readiness."}
        previous = _mcp_source_overrides.get(source)
        if connector_ids is not None:
            targets = {item.strip().lower() for item in connector_ids}
            connector_ids = sorted(targets)
            # A connector update is a scoped replacement, including removals.
            # Never publish another connector's half-restored configuration.
            existing = previous
            if existing is None:
                existing = {
                    name: definition.config
                    for name, definition in _mcp_server_definitions.items()
                    if definition.owner == source
                }
            retained = {
                name: value for name, value in existing.items()
                if str(value.get("_connectorId", "")).strip().lower() not in targets
            }
            incoming = {
                name: value for name, value in servers.items()
                if str(value.get("_connectorId", "")).strip().lower() in targets
            }
            conflicts = sorted(retained.keys() & incoming.keys())
            if conflicts:
                return {"success": False, "error": (
                    "MCP server names belong to connectors outside connectorIds: "
                    + ", ".join(conflicts)
                )}
            servers = {**retained, **incoming}
        _mcp_source_overrides[source] = servers
        if connector_ids is not None:
            result = await _reconcile_mcp_sources_locked(source, connector_ids)
        else:
            result = await _reconcile_mcp_sources_locked(source)
        if result.get("error") and not result.get("results"):
            if previous is None:
                _mcp_source_overrides.pop(source, None)
            else:
                _mcp_source_overrides[source] = previous
        return result


async def _reconcile_mcp_sources_locked(
    source: str | None = None, connector_ids: list[str] | None = None,
) -> dict:
    global _mcp_server_definitions
    if source is not None and source not in {"system", "connector", "user"}:
        return {"success": False, "error": f"Unknown MCP source: {source}"}
    if not _mcp_config_path:
        return {"success": False, "error": "MCP source paths are not initialized"}

    previous = dict(_mcp_server_definitions)
    try:
        current = _resolve_registered_sources(_mcp_config_path)
    except Exception as error:
        return {"success": False, "error": str(error)}

    async def retire(name: str, before: ResolvedMcpServer | None,
                     after: ResolvedMcpServer | None) -> dict | None:
        # An in-flight reconnect must finish publishing before revocation can
        # close it. Return only after that same server's critical section ends.
        lock = _mcp_reconnect_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if after is None:
                result = await disconnect_mcp_server(name)
                if before is not None:
                    _record_status(name, "disabled", definition=before)
                _mcp_server_definitions.pop(name, None)
                return {"name": name, "action": "removed", **result}
            _mcp_server_definitions[name] = after
            if before is None or not before.config.get("disabled", False) or any(
                connection.name == name for connection in _mcp_connections
            ):
                result = await disconnect_mcp_server(name)
                _record_status(name, "disabled", definition=after)
                return {"name": name, "action": "disabled", **result}
            _record_status(name, "disabled", definition=after)
            return None

    async def reconnect(name: str, action: str) -> dict:
        result = await reconnect_mcp_server(name)
        return {"name": name, "action": action, **result}

    def in_scope(definition: ResolvedMcpServer | None) -> bool:
        return definition is not None and (
            source is None or definition.owner == source
        ) and (
            connector_ids is None or definition.connector_id in connector_ids
        )

    operations = []
    all_names = sorted(set(previous) | set(current))
    for name in all_names:
        before = previous.get(name)
        after = current.get(name)
        before_in_scope, after_in_scope = in_scope(before), in_scope(after)
        if not before_in_scope and not after_in_scope:
            continue
        if not after_in_scope:
            # Revoke the old owner without applying a newly revealed source
            # outside this update. Its own reconciliation may activate it later.
            operations.append(retire(name, before, None))
            continue
        if after.config.get("disabled", False):
            operations.append(retire(name, before, after))
            continue
        if before is not None and before.fingerprint == after.fingerprint:
            status = _mcp_status.get(name)
            # An unchanged configuration is not proof of a successful connection.
            # Retry failures, but leave healthy or still-loading servers alone.
            if status is None or status.state != "failed":
                continue
        operations.append(reconnect(name, "added" if before is None else "modified"))

    # Reconnects and revocations share per-server locks, while unrelated names
    # start independently even if one removal waits for a slow connection.
    results = [result for result in await asyncio.gather(*operations) if result is not None]

    return {
        "success": all(result.get("success", False) for result in results),
        "source": source,
        "results": results,
        "configPaths": get_mcp_config_paths(),
    }


async def reconnect_auth_failed_mcp_servers_if_token_changed() -> list[dict]:
    """Reconnect auth-failed MCP servers after the product login token rotates.

    Hosted MCP discovery happens once during CLI startup. If that initial
    connection receives a 401, there is no persistent HTTP client on which the
    dynamic bearer hook can observe a later desktop-login refresh. This helper
    notices the auth-file change and retries only servers whose last failure was
    an authentication failure. Connected servers do not need reconnecting:
    ``DynamicBearerAuth`` already reads the latest token for every request.
    """
    global _mcp_auth_fingerprint

    current_fingerprint = _current_mcp_auth_fingerprint()
    if not current_fingerprint or current_fingerprint == _mcp_auth_fingerprint:
        return []
    _mcp_auth_fingerprint = current_fingerprint

    failed_names = [
        status.name
        for status in _mcp_status.values()
        if status.state == "failed"
        and status.auth_status in {401, 403}
    ]
    results: list[dict] = []
    for name in failed_names:
        result = await reconnect_mcp_server(name)
        results.append({"name": name, **result})
    return results


async def _reconnect_mcp_server_locked(name: str) -> dict:
    """Reconnect one server from the resolved multi-source registry."""
    global _mcp_connections

    definition = _mcp_server_definitions.get(name)
    if definition is None:
        return {"success": False, "error": f"Server '{name}' not found in MCP sources"}
    server_config = _materialize_server_config(definition)

    if server_config.get("disabled"):
        _record_status(name, "disabled", definition=definition)
        return {"success": False, "error": "Server is disabled", "configPath": definition.source_path}

    catalog = get_mcp_tool_catalog()
    catalog.mark_server_loading(name)
    try:
        # Drop any existing connection for this server. Searches wait on the
        # per-server refresh marker until the replacement snapshot is stable.
        old_conn = next((c for c in _mcp_connections if c.name == name), None)
        if old_conn:
            try:
                await old_conn.disconnect()
            except Exception:
                pass
            _mcp_connections = [c for c in _mcp_connections if c.name != name]
            catalog.remove_server(name)

        url = server_config.get("url")
        command = server_config.get("command")
        transport_label = url or command or ""
        if _waiting_for_credential(definition):
            _record_status(
                name, "connecting", transport=transport_label,
                error="Waiting for host credential", definition=definition,
            )
            return {
                "success": False, "waitingForCredential": True,
                "error": "Waiting for host credential", "configPath": definition.source_path,
            }
        conn = _build_connection(definition)

        _record_status(name, "connecting", transport=transport_label)
        try:
            success = await conn.connect()
        except Exception as e:
            _record_status(
                name,
                "failed",
                transport=transport_label,
                error=str(e),
                auth_status=_mcp_auth_status(e),
            )
            return {"success": False, "error": str(e), "configPath": definition.source_path}

        if success:
            _mcp_connections.append(conn)
            _replace_server_catalog(conn)
            _record_status(
                name, "connected",
                transport=transport_label,
                tool_count=len(conn.tools),
                tools=[t.name for t in conn.tools],
            )
            return {"success": True, "toolCount": len(conn.tools), "tools": [t.name for t in conn.tools], "configPath": definition.source_path}

        _record_status(
            name,
            "failed",
            transport=transport_label,
            error=conn.last_error,
            auth_status=conn.last_auth_status,
        )
        return {"success": False, "error": conn.last_error or "connect() returned False", "configPath": definition.source_path}
    finally:
        catalog.mark_server_ready(name)
