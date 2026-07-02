"""MCP tool loader with real MCP client integration and timeout handling."""

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
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
from mcp.client.streamable_http import streamablehttp_client

from box_agent.auth import request_auth_headers, resolve_auth_token, should_attach_auth_header

from .base import Tool, ToolResult


def _warn(msg: str) -> None:
    """Write diagnostic message to stderr (never stdout)."""
    sys.stderr.write(msg + "\n")

# Connection type aliases
ConnectionType = Literal["stdio", "sse", "http", "streamable_http"]


@dataclass
class MCPTimeoutConfig:
    """MCP timeout configuration."""

    connect_timeout: float = 10.0  # Connection timeout (seconds)
    execute_timeout: float = 60.0  # Tool execution timeout (seconds)
    sse_read_timeout: float = 120.0  # SSE read timeout (seconds)


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
        execute_timeout: float | None = None,
    ):
        self._name = name
        self._server_name = server_name
        self._description = description
        self._parameters = parameters
        self._session = session
        self._execute_timeout = execute_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs) -> ToolResult:
        """Execute MCP tool via the session with timeout protection."""
        timeout = self._execute_timeout or _default_timeout_config.execute_timeout

        try:
            # Wrap call_tool with timeout
            async with _timeout(timeout):
                result = await self._session.call_tool(self._name, arguments=kwargs)

            # MCP tool results are a list of content items
            content_parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    content_parts.append(item.text)
                else:
                    content_parts.append(str(item))

            content_str = "\n".join(content_parts)

            is_error = result.isError if hasattr(result, "isError") else False

            if is_error:
                err_msg = content_str.strip() or "Tool returned error"
                return ToolResult(success=False, content=content_str, error=err_msg)
            return ToolResult(success=True, content=content_str, error=None)

        except TimeoutError:
            return ToolResult(
                success=False,
                content="",
                error=f"MCP tool execution timed out after {timeout}s. The remote server may be slow or unresponsive.",
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"MCP tool execution failed: {str(e)}")


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
        # Lazy loading: skip connect at startup; load on query match
        lazy: bool = False,
        keywords: list[str] | None = None,
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
        # Lazy loading state
        self.lazy = lazy
        self.keywords = keywords or []
        # Connection state
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

    async def connect(self) -> bool:
        """Connect to the MCP server with timeout protection."""
        connect_timeout = self._get_connect_timeout()

        async def _close_exit_stack() -> None:
            if not self.exit_stack:
                return
            try:
                await self.exit_stack.aclose()
            except BaseException as cleanup_error:  # noqa: BLE001
                _warn(f"⚠ Failed to clean up MCP connection '{self.name}': {cleanup_error}")
            finally:
                self.exit_stack = None

        try:
            self.exit_stack = AsyncExitStack()

            # Wrap connection with timeout
            async with _timeout(connect_timeout):
                if self.connection_type == "stdio":
                    read_stream, write_stream = await self._connect_stdio()
                elif self.connection_type == "sse":
                    read_stream, write_stream = await self._connect_sse()
                else:  # http / streamable_http
                    read_stream, write_stream = await self._connect_streamable_http()

                # Enter client session context
                session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
                self.session = session

                # Initialize the session
                await session.initialize()

                # List available tools
                tools_list = await session.list_tools()

            # Wrap each tool with execute timeout
            execute_timeout = self._get_execute_timeout()
            for tool in tools_list.tools:
                parameters = tool.inputSchema if hasattr(tool, "inputSchema") else {}
                mcp_tool = MCPTool(
                    name=tool.name,
                    description=tool.description or "",
                    parameters=parameters,
                    session=session,
                    server_name=self.name,
                    execute_timeout=execute_timeout,
                )
                self.tools.append(mcp_tool)

            conn_info = self.url if self.url else self.command
            _warn(f"✓ Connected to MCP server '{self.name}' ({self.connection_type}: {conn_info}) - loaded {len(self.tools)} tools")
            for tool in self.tools:
                desc = tool.description[:60] if len(tool.description) > 60 else tool.description
                _warn(f"  - {tool.name}: {desc}...")
            return True

        except TimeoutError:
            _warn(f"✗ Connection to MCP server '{self.name}' timed out after {connect_timeout}s")
            await _close_exit_stack()
            return False

        except asyncio.CancelledError as e:
            _warn(f"✗ Connection to MCP server '{self.name}' was cancelled during initialization: {e}")
            await _close_exit_stack()
            return False

        except Exception as e:
            _warn(f"✗ Failed to connect to MCP server '{self.name}': {e}")
            await _close_exit_stack()
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
        """Connect via STDIO transport."""
        server_params = StdioServerParameters(command=self.command, args=self.args, env=self._build_stdio_env())
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

        # streamablehttp_client returns (read, write, get_session_id)
        read_stream, write_stream, _ = await self.exit_stack.enter_async_context(
            streamablehttp_client(
                url=self.url,
                headers=self.headers if self.headers else None,
                timeout=connect_timeout,
                sse_read_timeout=sse_read_timeout,
                auth=self.auth,
            )
        )
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
# Lazy MCP servers parsed at startup but NOT yet connected. Keyed by server
# name. ``ensure_lazy_mcp_loaded(query)`` connects matching servers on demand
# and pops them out of this dict.
_lazy_mcp_pending: dict[str, MCPServerConnection] = {}

# Built-in keyword presets for well-known heavy MCP servers. Used when an
# mcp.json entry omits the ``keywords`` field — the server name is looked up
# here and the preset is applied. This lets hosts ship a stock mcp.json (no
# keywords field) and still get smart lazy gating for the big-ticket servers.
# Keywords are bilingual on purpose so Chinese queries can match.
DEFAULT_MCP_KEYWORDS: dict[str, list[str]] = {
    "playwright": [
        "browser", "playwright", "chromium", "screenshot", "scrape", "crawl",
        "automation", "url", "html", "dom",
        "浏览器", "网页", "截图", "抓取", "爬虫", "自动化",
        "打开网址", "访问网页", "网址", "链接",
    ],
    "puppeteer": [
        "browser", "puppeteer", "chromium", "screenshot", "scrape", "automation",
        "浏览器", "网页", "截图", "抓取", "自动化",
    ],
}


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
    global _mcp_connections

    config_file = _resolve_mcp_config_path(config_path)

    if config_file is None:
        _warn(f"MCP config not found: {config_path}")
        return []

    try:
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
                    lazy=False,  # final lazy decision applied below
                    keywords=list(server_config.get("keywords", []) or []),
                )
            )

        # Resolve final lazy + keywords for each connection.
        #   1. If mcp.json sets explicit ``keywords``, use them as-is.
        #   2. Else fall back to ``DEFAULT_MCP_KEYWORDS`` by server name.
        #   3. Lazy default: explicit ``lazy`` wins; otherwise auto-lazy when
        #      the server ends up with non-empty keywords. Servers with no
        #      keywords stay eager so they don't get silently stranded.
        for conn, server_config in zip(connections, [
            config["mcpServers"][c.name] for c in connections
        ]):
            if not conn.keywords:
                conn.keywords = list(DEFAULT_MCP_KEYWORDS.get(conn.name.lower(), []))
            explicit_lazy = server_config.get("lazy")
            if explicit_lazy is None:
                conn.lazy = bool(conn.keywords)
            else:
                conn.lazy = bool(explicit_lazy)

        # Split eager vs lazy. Lazy servers are deferred to
        # ``ensure_lazy_mcp_loaded(query)`` — they keep their config but skip
        # the connect() round-trip.
        eager_connections: list[MCPServerConnection] = []
        for conn in connections:
            if conn.lazy:
                _lazy_mcp_pending[conn.name] = conn
                _warn(f"Deferred lazy MCP server: {conn.name} (keywords={conn.keywords})")
            else:
                eager_connections.append(conn)

        # Connect to all eager servers in parallel — one slow/broken server no
        # longer blocks the others. Each connection has its own timeout.
        results = await asyncio.gather(
            *(conn.connect() for conn in eager_connections),
            return_exceptions=True,
        )

        all_tools = []
        for conn, success in zip(eager_connections, results):
            if isinstance(success, BaseException):
                _warn(f"✗ MCP server '{conn.name}' raised during connect: {success}")
                continue
            if success:
                _mcp_connections.append(conn)
                all_tools.extend(conn.tools)

        _warn(f"Total MCP tools loaded: {len(all_tools)}")

        return all_tools

    except Exception as e:
        _warn(f"Error loading MCP config: {e}")
        import traceback

        traceback.print_exc()
        return []


async def cleanup_mcp_connections():
    """Clean up all MCP connections."""
    global _mcp_connections
    for connection in _mcp_connections:
        await connection.disconnect()
    _mcp_connections.clear()
    _lazy_mcp_pending.clear()


def get_pending_lazy_mcp_servers() -> dict[str, list[str]]:
    """Return ``{server_name: keywords}`` for lazy MCP servers not yet loaded.

    Useful for diagnostics / tests. The dict is a snapshot; mutating it has no
    effect on the underlying registry.
    """
    return {name: list(conn.keywords) for name, conn in _lazy_mcp_pending.items()}


async def ensure_lazy_mcp_loaded(query: str) -> list[Tool]:
    """Connect lazy MCP servers whose keywords overlap the cumulative query.

    Tokenizes ``query`` the same way ``SkillSelector`` does (English length>=2
    + Chinese 2-char sliding window) so callers can pass the cumulative query
    string straight through. A lazy server matches when ANY of its declared
    keywords token-overlaps the query. Matched servers are connected and
    moved from ``_lazy_mcp_pending`` into ``_mcp_connections``.

    Returns the list of newly-loaded MCPTool instances (empty if no match).
    Caller is responsible for merging them into the agent's tool list.
    """
    if not _lazy_mcp_pending or not query or not query.strip():
        return []

    # Reuse the skill loader's tokenizer for consistent semantics.
    from box_agent.tools.skill_loader import _tokenize

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    matched: list[MCPServerConnection] = []
    for name, conn in list(_lazy_mcp_pending.items()):
        if not conn.keywords:
            continue
        kw_tokens: set[str] = set()
        for kw in conn.keywords:
            kw_tokens |= _tokenize(kw)
        if kw_tokens & query_tokens:
            matched.append(conn)

    if not matched:
        return []

    results = await asyncio.gather(
        *(conn.connect() for conn in matched),
        return_exceptions=True,
    )

    new_tools: list[Tool] = []
    for conn, success in zip(matched, results):
        if isinstance(success, BaseException):
            # Connection raised — keep in _lazy_mcp_pending so a later session
            # (or the same session on a later turn) can retry. Without this,
            # a transient failure (or a title-gen session burning the slot)
            # would permanently blind the agent to this MCP server.
            _warn(f"✗ Lazy MCP server '{conn.name}' raised during connect: {success}")
            continue
        if not success:
            # Connect returned False — same reasoning: leave pending for retry.
            _warn(f"✗ Lazy MCP server '{conn.name}' failed to connect")
            continue
        # Success: move from pending to live registry.
        _lazy_mcp_pending.pop(conn.name, None)
        _mcp_connections.append(conn)
        new_tools.extend(conn.tools)
        _warn(f"✓ Lazy MCP server '{conn.name}' loaded ({len(conn.tools)} tools)")

    return new_tools
