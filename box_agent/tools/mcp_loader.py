"""MCP tool loader with real MCP client integration and timeout handling."""

import asyncio
import hashlib
import inspect
import json
import os
import re
import sys
import tempfile
import time
from contextlib import AsyncExitStack, asynccontextmanager
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
from mcp.client.stdio import stdio_client

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
from .model_tool_context import current_model_tool_context


_MODEL_TOOL_NAME_MAX_LENGTH = 64
_INVALID_MODEL_TOOL_NAME_CHARACTER = re.compile(r"[^a-zA-Z0-9_-]")


def _warn(msg: str) -> None:
    """Write diagnostic message to stderr (never stdout)."""
    sys.stderr.write(msg + "\n")


def _public_mcp_tool_name(server_name: str, remote_name: str) -> str:
    """Return a stable provider-safe name while preserving the MCP name separately."""
    mapped_name = public_browser_tool_name(server_name, remote_name)
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
ConnectionType = Literal["stdio", "sse", "http", "streamable_http"]

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


class MCPTool(Tool):
    """Wrapper for MCP tools with timeout handling."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        session: ClientSession,
        server_name: str = "",
        remote_name: str | None = None,
        fixed_arguments: dict[str, Any] | None = None,
        execute_timeout: float | None = None,
        always_load: bool = False,
        concurrency_limiter: asyncio.Semaphore | None = None,
    ):
        self._name = name
        self._remote_name = remote_name or name
        self._server_name = server_name
        self._fixed_arguments = dict(fixed_arguments or {})
        self._description = description
        self._parameters = parameters
        self._session = session
        self._execute_timeout = execute_timeout
        self._always_load = always_load
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
            if self._server_name == "playwright":
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
                result = await self._session.call_tool(
                    self._remote_name,
                    arguments=call_arguments,
                )

            # MCP tool results are a list of content items
            content_parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    content_parts.append(item.text)
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
            return ToolResult(success=True, content=content_str, error=None)

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
        self._web_search_concurrency_limiter: asyncio.Semaphore | None = None
        # Connection state
        self.last_error: str | None = None
        self.last_auth_status: int | None = None
        self.session: ClientSession | None = None
        self.exit_stack: AsyncExitStack | None = None
        self.tools: list[MCPTool] = []

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

        command_label = f" command={self.command!r}" if self.connection_type == "stdio" else ""
        _warn(
            f"[mcp] connect:start server={self.name!r} transport={self.connection_type} "
            f"timeout_s={connect_timeout}{command_label}"
        )

        async def _close_exit_stack() -> BaseException | None:
            if not self.exit_stack:
                return None
            cleanup_error: BaseException | None = None
            try:
                await self.exit_stack.aclose()
            except BaseException as error:  # noqa: BLE001
                cleanup_error = error
            finally:
                self.exit_stack = None
                self.session = None
            return cleanup_error

        def _warn_unexpected_cleanup_error(cleanup_error: BaseException | None) -> None:
            if cleanup_error is None or _is_mcp_authentication_error(cleanup_error):
                return
            _warn(f"⚠ Failed to clean up MCP connection '{self.name}': {cleanup_error}")

        try:
            self.exit_stack = AsyncExitStack()

            # Wrap connection with timeout
            async with _timeout(connect_timeout):
                if self.connection_type == "stdio":
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

            # Wrap each tool with execute timeout
            execute_timeout = self._get_execute_timeout()
            for tool in tools_list.tools:
                parameters = tool.inputSchema if hasattr(tool, "inputSchema") else {}
                public_name = _public_mcp_tool_name(self.name, tool.name)
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
                    always_load=self.always_load,
                    concurrency_limiter=self._concurrency_limiter_for_tool(tool.name),
                )
                self.tools.append(mcp_tool)

            conn_info = self.url if self.url else self.command
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
        connect_timeout = self._get_connect_timeout()
        sse_read_timeout = self._get_sse_read_timeout()

        parameters = inspect.signature(streamable_http_client).parameters
        if "http_client" in parameters and "headers" not in parameters:
            # MCP 2.x moved headers/auth/timeouts onto a caller-owned httpx2
            # client and changed the transport result from three values to two.
            if create_mcp_http_client is None:
                raise RuntimeError(
                    "MCP streamable HTTP requires create_mcp_http_client"
                )
            import httpx2

            transport_auth = self.auth
            if isinstance(self.auth, DynamicBearerAuth):
                dynamic_auth = self.auth

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
                headers=self.headers if self.headers else None,
                timeout=httpx2.Timeout(
                    connect=connect_timeout,
                    read=sse_read_timeout,
                    write=connect_timeout,
                    pool=connect_timeout,
                ),
                auth=transport_auth,
            )
            http_client = await self.exit_stack.enter_async_context(
                http_client_context
            )
            streams = await self.exit_stack.enter_async_context(
                streamable_http_client(url=self.url, http_client=http_client)
            )
        else:
            # MCP 1.x accepts transport configuration directly and returns
            # (read, write, get_session_id).
            streams = await self.exit_stack.enter_async_context(
                streamable_http_client(
                    url=self.url,
                    headers=self.headers if self.headers else None,
                    timeout=connect_timeout,
                    sse_read_timeout=sse_read_timeout,
                    auth=self.auth,
                )
            )
        read_stream, write_stream = streams[:2]
        return read_stream, write_stream

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


# Global connections registry
_mcp_connections: list[MCPServerConnection] = []
_mcp_reconnect_locks: dict[str, asyncio.Lock] = {}


@dataclass
class McpServerStatus:
    name: str
    state: str  # connecting | connected | failed | disabled
    transport: str = ""
    tool_count: int = 0
    tools: list = field(default_factory=list)
    error: str | None = None
    auth_status: int | None = None


_mcp_status: dict[str, McpServerStatus] = {}
_mcp_loading: bool = False
_mcp_config_path: str | None = None
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
) -> None:
    if auth_status is None and error:
        if "HTTP 401" in error or "Authentication failed" in error:
            auth_status = 401
        elif "HTTP 403" in error or "Authorization failed" in error:
            auth_status = 403
    _mcp_status[name] = McpServerStatus(
        name=name, state=state, transport=transport,
        tool_count=tool_count, tools=tools or [], error=error,
        auth_status=auth_status,
    )


def get_mcp_status() -> list[dict]:
    return [
        {
            "name": s.name,
            "state": s.state,
            "transport": s.transport,
            "toolCount": s.tool_count,
            "tools": s.tools,
            "error": s.error,
            "authStatus": s.auth_status,
        }
        for s in _mcp_status.values()
    ]


def _determine_connection_type(server_config: dict) -> ConnectionType:
    """Determine connection type from server config."""
    explicit_type = str(server_config.get("type") or server_config.get("transport") or "").lower()
    if explicit_type in ("stdio", "sse", "http", "streamable_http"):
        return explicit_type
    # Auto-detect: if url exists, default to streamable_http; otherwise stdio
    if server_config.get("url"):
        return "streamable_http"
    return "stdio"


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
        if config_file is not None:
            _mcp_config_path = str(config_file)

        if config_file is None:
            _warn(f"MCP config not found: {config_path}")
            return []

        with open(config_file, encoding="utf-8") as f:
            config = json.load(f)

        mcp_servers = config.get("mcpServers", {})

        if not mcp_servers:
            import sys as _sys
            _sys.stderr.write("No MCP servers configured\n")
            return []

        connections: list[MCPServerConnection] = []

        # Build connection objects for each enabled server
        for server_name, server_config in mcp_servers.items():
            if server_config.get("disabled", False):
                _warn(f"Skipping disabled server: {server_name}")
                _record_status(server_name, "disabled")
                continue

            conn_type = _determine_connection_type(server_config)
            url = server_config.get("url")
            command = server_config.get("command")

            # Validate config
            if conn_type == "stdio" and not command:
                _warn(f"No command specified for STDIO server: {server_name}")
                continue
            if conn_type in ("sse", "http", "streamable_http") and not url:
                _warn(f"No url specified for {conn_type.upper()} server: {server_name}")
                continue

            configured_headers = server_config.get("headers", {})
            auth = _dynamic_bearer_auth_for_url(
                url=url,
                headers=configured_headers,
                auth_file=auth_file,
                auth_token=auth_token,
            )
            connection_headers = (
                configured_headers
                if auth is not None
                else request_auth_headers(
                    auth_file=auth_file,
                    explicit_token=auth_token,
                    existing=configured_headers,
                    url=url,
                )
            )

            connections.append(
                MCPServerConnection(
                    name=server_name,
                    connection_type=conn_type,
                    command=command,
                    args=server_config.get("args", []),
                    env=server_config.get("env", {}),
                    url=url,
                    headers=connection_headers,
                    auth=auth,
                    # Per-server timeout overrides from mcp.json
                    connect_timeout=server_config.get("connect_timeout"),
                    execute_timeout=server_config.get("execute_timeout"),
                    sse_read_timeout=server_config.get("sse_read_timeout"),
                    always_load=bool(server_config.get("alwaysLoad", False)),
                )
            )

        # Connect to all servers in parallel — one slow/broken server no
        # longer blocks the others. Each connection has its own timeout.

        # Seed connecting state before gather so UI shows spinner during window
        for conn in connections:
            _record_status(conn.name, "connecting", transport=conn.url or conn.command or "")

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
                    transport=conn.url or conn.command or "",
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
                    transport=conn.url or conn.command or "",
                    tool_count=len(conn.tools),
                    tools=[t.name for t in conn.tools],
                )
            else:
                _record_status(
                    conn.name, "failed",
                    transport=conn.url or conn.command or "",
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
    global _mcp_connections
    for connection in _mcp_connections:
        await connection.disconnect()
    _mcp_connections.clear()
    _mcp_reconnect_locks.clear()
    get_mcp_tool_catalog().clear()


async def reconnect_mcp_server(name: str) -> dict:
    """Serialize hot reconnects for one server name."""
    lock = _mcp_reconnect_locks.setdefault(name, asyncio.Lock())
    async with lock:
        return await _reconnect_mcp_server_locked(name)


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
    """Re-read mcp.json and reconnect a single named MCP server in-place."""
    global _mcp_connections

    if not _mcp_config_path:
        return {"success": False, "error": "mcp.json path unknown; box-agent not yet initialized"}

    try:
        with open(_mcp_config_path, encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        return {"success": False, "error": f"Cannot read {_mcp_config_path}: {e}", "configPath": _mcp_config_path}

    server_config = config.get("mcpServers", {}).get(name)
    if not server_config:
        return {"success": False, "error": f"Server '{name}' not found in mcp.json", "configPath": _mcp_config_path}

    if server_config.get("disabled"):
        _record_status(name, "disabled")
        return {"success": False, "error": "Server is disabled", "configPath": _mcp_config_path}

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

        conn_type = _determine_connection_type(server_config)
        url = server_config.get("url")
        command = server_config.get("command")
        transport_label = url or command or ""

        # Mirror cold-start auth logic in load_mcp_tools_async(): compute a dynamic
        # bearer if applicable, otherwise fold static Authorization headers in via
        # request_auth_headers(). Without this a hot reconnect drops the token and
        # hosted MCP endpoints (e.g. mcp.xiaohuanxiong.com) return 401 forever.
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

        conn = MCPServerConnection(
            name=name,
            connection_type=conn_type,
            command=command,
            args=server_config.get("args", []),
            env=server_config.get("env", {}),
            url=url,
            headers=connection_headers,
            auth=auth,
            connect_timeout=server_config.get("connect_timeout"),
            execute_timeout=server_config.get("execute_timeout"),
            sse_read_timeout=server_config.get("sse_read_timeout"),
            always_load=bool(server_config.get("alwaysLoad", False)),
        )

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
            return {"success": False, "error": str(e), "configPath": _mcp_config_path}

        if success:
            _mcp_connections.append(conn)
            _replace_server_catalog(conn)
            _record_status(
                name, "connected",
                transport=transport_label,
                tool_count=len(conn.tools),
                tools=[t.name for t in conn.tools],
            )
            return {"success": True, "toolCount": len(conn.tools), "tools": [t.name for t in conn.tools], "configPath": _mcp_config_path}

        _record_status(
            name,
            "failed",
            transport=transport_label,
            error=conn.last_error,
            auth_status=conn.last_auth_status,
        )
        return {"success": False, "error": conn.last_error or "connect() returned False", "configPath": _mcp_config_path}
    finally:
        catalog.mark_server_ready(name)
