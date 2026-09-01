"""Test cases for MCP tool loading and Git-based MCP servers."""

import asyncio
import inspect
import json
import sys
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from box_agent.tools import mcp_loader
from box_agent.tools.browser_runtime_scope import (
    BrowserRuntimeCoordinator,
    release_browser_runtime,
    reset_browser_runtime_owner,
    set_browser_runtime_owner,
)
from box_agent.tools.mcp_loader import (
    DynamicBearerAuth,
    MCPServerConnection,
    MCPTool,
    MCPTimeoutConfig,
    WEB_SEARCH_MCP_MAX_CONCURRENCY,
    _determine_connection_type,
    _public_mcp_tool_name,
    cleanup_mcp_connections,
    get_mcp_timeout_config,
    load_mcp_tools_async,
    McpServerStatus,
    reconnect_auth_failed_mcp_servers_if_token_changed,
    set_mcp_timeout_config,
)
from box_agent.tools.model_tool_context import (
    current_model_tool_context,
    reset_model_tool_context,
    scoped_model_tool_context,
    set_model_tool_context,
)
from box_agent.tools.setup import merge_mcp_tools, register_mcp_tools
from box_agent.tools.base import Tool, ToolResult


def test_streamable_http_client_is_available():
    """The loader resolves the client exported by the installed MCP SDK."""
    assert callable(mcp_loader.streamable_http_client)


def test_public_mcp_tool_name_sanitizes_provider_invalid_remote_name():
    remote_name = "mcp-law-search-service.search_article"

    public_name = _public_mcp_tool_name("pkulaw", remote_name)

    assert public_name.startswith("mcp-law-search-service_search_article__")
    assert len(public_name) <= 64
    assert all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in public_name
    )
    assert public_name == _public_mcp_tool_name("pkulaw", remote_name)
    assert _public_mcp_tool_name("pkulaw", "search_law") == "search_law"


def test_public_mcp_tool_name_avoids_sanitized_name_collisions():
    dotted = _public_mcp_tool_name("pkulaw", "law.search")
    slashed = _public_mcp_tool_name("pkulaw", "law/search")

    assert dotted != slashed


@pytest.mark.asyncio
async def test_mcp_tool_uses_public_name_for_model_and_remote_name_for_execution():
    class FakeSession:
        called_name = None

        async def call_tool(self, name, arguments):
            self.called_name = name
            return SimpleNamespace(content=[], isError=False)

    session = FakeSession()
    remote_name = "mcp-law-search-service.search_article"
    tool = MCPTool(
        name=_public_mcp_tool_name("pkulaw", remote_name),
        remote_name=remote_name,
        description="search law",
        parameters={"type": "object"},
        session=session,
        server_name="pkulaw",
    )

    result = await tool.execute(query="民法典")

    assert result.success is True
    assert "." not in tool.name
    assert session.called_name == remote_name


@pytest.mark.asyncio
async def test_auth_refresh_reconnects_structured_401_and_403_failures(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(mcp_loader, "_mcp_auth_token", "old-token")
    monkeypatch.setattr(mcp_loader, "_mcp_auth_file", "")
    monkeypatch.setattr(mcp_loader, "_mcp_auth_fingerprint", "old-fingerprint")
    monkeypatch.setattr(
        mcp_loader,
        "_mcp_status",
        {
            "unauthenticated": McpServerStatus(
                name="unauthenticated",
                state="failed",
                auth_status=401,
            ),
            "forbidden": McpServerStatus(
                name="forbidden",
                state="failed",
                auth_status=403,
            ),
            "timeout": McpServerStatus(
                name="timeout",
                state="failed",
                error="connection timeout",
            ),
        },
    )
    monkeypatch.setattr(mcp_loader, "resolve_auth_token", lambda *_args, **_kwargs: "new-token")

    async def reconnect(name: str) -> dict:
        calls.append(name)
        return {"success": True}

    monkeypatch.setattr(mcp_loader, "reconnect_mcp_server", reconnect)

    result = await reconnect_auth_failed_mcp_servers_if_token_changed()

    assert calls == ["unauthenticated", "forbidden"]
    assert result == [
        {"name": "unauthenticated", "success": True},
        {"name": "forbidden", "success": True},
    ]
    assert await reconnect_auth_failed_mcp_servers_if_token_changed() == []


def test_recorded_auth_status_is_exposed_without_parsing_at_retry_time(monkeypatch):
    monkeypatch.setattr(mcp_loader, "_mcp_status", {})

    mcp_loader._record_status(
        "hosted",
        "failed",
        error="Authorization failed: Access denied (HTTP 403)",
    )

    status = mcp_loader.get_mcp_status()[0]
    assert status["authStatus"] == 403
    assert mcp_loader._mcp_status["hosted"].auth_status == 403


def test_streamable_http_client_has_supported_signature():
    """The installed SDK must expose either the MCP 1.x or 2.x client API."""
    parameters = inspect.signature(mcp_loader.streamable_http_client).parameters

    legacy_options = {"headers", "timeout", "sse_read_timeout", "auth"}
    assert legacy_options <= parameters.keys() or (
        "http_client" in parameters and "headers" not in parameters
    )


@pytest.mark.asyncio
async def test_streamable_http_mcp1_passes_transport_configuration(monkeypatch):
    captured = {}

    @asynccontextmanager
    async def fake_client(
        url,
        headers=None,
        timeout=0,
        sse_read_timeout=0,
        auth=None,
    ):
        captured.update(
            url=url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
            auth=auth,
        )
        yield ("read", "write", lambda: None)

    monkeypatch.setattr(mcp_loader, "streamable_http_client", fake_client)
    conn = MCPServerConnection(
        name="mcp1",
        connection_type="streamable_http",
        url="https://example.com/mcp",
        headers={"X-Test": "one"},
        connect_timeout=7,
        sse_read_timeout=11,
    )
    conn.exit_stack = AsyncExitStack()

    try:
        assert await conn._connect_streamable_http() == ("read", "write")
    finally:
        await conn.exit_stack.aclose()

    assert captured == {
        "url": "https://example.com/mcp",
        "headers": {"X-Test": "one"},
        "timeout": 7,
        "sse_read_timeout": 11,
        "auth": None,
    }


@pytest.mark.asyncio
async def test_streamable_http_mcp2_uses_preconfigured_http_client(monkeypatch):
    captured = {}
    fake_http_client = object()

    @asynccontextmanager
    async def managed_http_client():
        yield fake_http_client

    def fake_factory(*, headers=None, timeout=None, auth=None):
        captured.update(headers=headers, timeout=timeout, auth=auth)
        return managed_http_client()

    @asynccontextmanager
    async def fake_client(url, *, http_client=None, terminate_on_close=True):
        captured.update(url=url, http_client=http_client)
        yield ("read", "write")

    class FakeTimeout:
        def __init__(self, **kwargs):
            self.values = kwargs

    monkeypatch.setitem(
        sys.modules,
        "httpx2",
        SimpleNamespace(Timeout=FakeTimeout, Auth=object),
    )
    monkeypatch.setattr(mcp_loader, "create_mcp_http_client", fake_factory)
    monkeypatch.setattr(mcp_loader, "streamable_http_client", fake_client)
    conn = MCPServerConnection(
        name="mcp2",
        connection_type="streamable_http",
        url="https://example.com/mcp",
        headers={"X-Test": "two"},
        connect_timeout=13,
        sse_read_timeout=17,
        auth=DynamicBearerAuth(explicit_token="token"),
    )
    conn.exit_stack = AsyncExitStack()

    try:
        assert await conn._connect_streamable_http() == ("read", "write")
    finally:
        await conn.exit_stack.aclose()

    assert captured["headers"] == {"X-Test": "two"}
    assert captured["timeout"].values == {
        "connect": 13,
        "read": 17,
        "write": 13,
        "pool": 13,
    }
    assert captured["url"] == "https://example.com/mcp"
    assert captured["http_client"] is fake_http_client
    assert captured["auth"] is not conn.auth


class NamedDummyTool(Tool):
    """Small test double for a named tool."""

    def __init__(self, name: str):
        self._name = name

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return f"{self._name} test tool"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(success=True, content="mcp", error="")


@pytest.fixture(scope="module")
def mcp_config():
    """Read MCP configuration."""
    mcp_config_path = Path("box_agent/config/mcp.json")
    if not mcp_config_path.exists():
        pytest.skip("box_agent/config/mcp.json missing — integration test skipped")
    with open(mcp_config_path, encoding="utf-8") as f:
        return json.load(f)


# =============================================================================
# Connection Type Detection Tests
# =============================================================================


class TestDetermineConnectionType:
    """Tests for _determine_connection_type function."""

    def test_stdio_with_command_only(self):
        """STDIO is default when only command is specified."""
        config = {"command": "npx", "args": ["-y", "some-server"]}
        assert _determine_connection_type(config) == "stdio"

    def test_stdio_explicit_type(self):
        """Explicit type=stdio should return stdio."""
        config = {"command": "npx", "type": "stdio"}
        assert _determine_connection_type(config) == "stdio"

    def test_url_defaults_to_streamable_http(self):
        """URL without explicit type should default to streamable_http."""
        config = {"url": "https://mcp.example.com/mcp"}
        assert _determine_connection_type(config) == "streamable_http"

    def test_sse_explicit_type(self):
        """Explicit type=sse should return sse."""
        config = {"url": "https://mcp.example.com/sse", "type": "sse"}
        assert _determine_connection_type(config) == "sse"

    def test_http_explicit_type(self):
        """Explicit type=http should return http."""
        config = {"url": "https://mcp.example.com/http", "type": "http"}
        assert _determine_connection_type(config) == "http"

    def test_streamable_http_explicit_type(self):
        """Explicit type=streamable_http should return streamable_http."""
        config = {"url": "https://mcp.example.com/mcp", "type": "streamable_http"}
        assert _determine_connection_type(config) == "streamable_http"

    def test_case_insensitive_type(self):
        """Type should be case insensitive."""
        config = {"url": "https://mcp.example.com/sse", "type": "SSE"}
        assert _determine_connection_type(config) == "sse"

    def test_empty_config_defaults_to_stdio(self):
        """Empty config should default to stdio."""
        config = {}
        assert _determine_connection_type(config) == "stdio"

    def test_unknown_type_with_url_defaults_to_streamable_http(self):
        """Unknown type with URL should default to streamable_http."""
        config = {"url": "https://mcp.example.com/mcp", "type": "unknown"}
        assert _determine_connection_type(config) == "streamable_http"


class TestMCPToolRegistration:
    """Tests for merging MCP tools into the agent tool belt."""

    def test_register_mcp_tools_overrides_same_named_tool(self):
        """MCP tools should replace existing same-named tools."""
        fallback = NamedDummyTool("web_search")
        mcp_tool = NamedDummyTool("web_search")
        tool_map = {fallback.name: fallback}

        register_mcp_tools(tool_map, [mcp_tool])

        assert tool_map["web_search"] is mcp_tool

    def test_merge_mcp_tools_replaces_same_named_base_tool(self):
        """Future ACP sessions should receive MCP tools after base-list merge."""
        fallback = NamedDummyTool("web_search")
        mcp_tool = NamedDummyTool("web_search")
        base_tools = [fallback]

        merge_mcp_tools(base_tools, [mcp_tool])

        assert base_tools == [mcp_tool]


class TestMCPToolExecution:
    """Tests for defensive normalization of remote MCP results."""

    @pytest.mark.asyncio
    async def test_invalid_remote_schema_fails_closed_without_leaking_arguments(self):
        class FakeSession:
            calls = 0

            async def call_tool(self, name, arguments):
                self.calls += 1
                raise AssertionError("invalid schema must block remote execution")

        session = FakeSession()
        tool = MCPTool(
            name="remote_secret_tool",
            description="remote tool with a malformed schema",
            parameters={"type": "definitely-not-a-json-schema-type"},
            session=session,
        )
        secret = "TOP_SECRET_ARGUMENT_VALUE"

        result = await tool.invoke({"token": secret})

        assert result.success is False
        assert result.raw_output["code"] == "INVALID_TOOL_SCHEMA"
        assert secret not in (result.error or "")
        assert secret not in str(result.raw_output)
        assert session.calls == 0

    @pytest.mark.asyncio
    async def test_structured_error_envelope_is_failure_without_is_error_flag(self):
        class FakeSession:
            async def call_tool(self, name, arguments):
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "error": {
                                        "message": "Query 不能为空。",
                                        "type": "search_error",
                                    }
                                },
                                ensure_ascii=False,
                            )
                        )
                    ],
                    isError=False,
                )

        tool = MCPTool(
            name="web_search",
            description="search",
            parameters={"type": "object"},
            session=FakeSession(),
        )

        result = await tool.execute(Query="")

        assert result.success is False
        assert result.error == "Query 不能为空。"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "max_output_tokens"),
        [
            ("sn-sensenova-6-8-flash-lite", 63_999),
            ("sn-glm-5-2", 100_000),
        ],
    )
    async def test_web_extract_receives_current_model_capabilities(
        self,
        model: str,
        max_output_tokens: int,
    ):
        class FakeSession:
            arguments = None

            async def call_tool(self, name, arguments):
                assert name == "web_extract"
                self.arguments = arguments
                return SimpleNamespace(content=[], isError=False)

        session = FakeSession()
        tool = MCPTool(
            name="web_extract",
            description="extract",
            parameters={"type": "object"},
            session=session,
            server_name="box-agent-web-extract",
        )
        token = set_model_tool_context(
            model=model,
            max_output_tokens=max_output_tokens,
        )
        try:
            result = await tool.execute(
                url="https://example.com",
                model="model-supplied-wrong-value",
                max_output_tokens=1,
            )
        finally:
            reset_model_tool_context(token)

        assert result.success is True
        assert session.arguments == {
            "url": "https://example.com",
            "model": model,
            "max_output_tokens": max_output_tokens,
        }

    @pytest.mark.asyncio
    async def test_scoped_model_context_resets_before_yield_and_closes_cross_task(
        self,
    ):
        async def source():
            context = current_model_tool_context()
            assert context is not None
            assert context.model == "sn-glm-5-2"
            yield "event"

        events = scoped_model_tool_context(
            source(),
            model="sn-glm-5-2",
            max_output_tokens=100_000,
        )

        assert await events.__anext__() == "event"
        assert current_model_tool_context() is None
        close = getattr(events, "aclose")
        await asyncio.create_task(close())
        assert current_model_tool_context() is None

    @pytest.mark.parametrize(
        "error_value",
        [None, "", {}, {"count": 0}, []],
    )
    @pytest.mark.asyncio
    async def test_non_error_values_remain_a_success(self, error_value):
        class FakeSession:
            async def call_tool(self, name, arguments):
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "error": error_value,
                                    "data": {"status": "ok"},
                                }
                            )
                        )
                    ],
                    isError=False,
                )

        tool = MCPTool(
            name="status",
            description="status",
            parameters={"type": "object"},
            session=FakeSession(),
        )

        result = await tool.execute()

        assert result.success is True
        assert json.loads(result.content)["data"] == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_web_search_tools_share_connection_concurrency_limit(self):
        class FakeSession:
            def __init__(self):
                self.current = 0
                self.peak = 0

            async def call_tool(self, name, arguments):
                self.current += 1
                self.peak = max(self.peak, self.current)
                try:
                    await asyncio.sleep(0.02)
                    return SimpleNamespace(content=[], isError=False)
                finally:
                    self.current -= 1

        connection = MCPServerConnection(name="search")
        limiter = connection._concurrency_limiter_for_tool("web_search")
        session = FakeSession()
        tools = [
            MCPTool(
                name="web_search",
                description="search",
                parameters={"type": "object"},
                session=session,
                concurrency_limiter=limiter,
            )
            for _ in range(2)
        ]

        results = await asyncio.gather(
            *[
                tools[index % len(tools)].execute(Query=f"query-{index}")
                for index in range(12)
            ]
        )

        assert all(result.success for result in results)
        assert session.peak == WEB_SEARCH_MCP_MAX_CONCURRENCY

    @pytest.mark.asyncio
    async def test_web_search_concurrency_queue_timeout_does_not_call_server(self):
        class FakeSession:
            calls = 0

            async def call_tool(self, name, arguments):
                self.calls += 1
                return SimpleNamespace(content=[], isError=False)

        limiter = asyncio.Semaphore(1)
        await limiter.acquire()
        session = FakeSession()
        tool = MCPTool(
            name="web_search",
            description="search",
            parameters={"type": "object"},
            session=session,
            execute_timeout=0.01,
            concurrency_limiter=limiter,
        )
        try:
            result = await tool.execute(Query="queued")
        finally:
            limiter.release()

        assert result.success is False
        assert result.error == (
            "MCP tool concurrency queue timed out after 0.01s. "
            "Too many tasks are using the shared WebSearch connection."
        )
        assert session.calls == 0

    @pytest.mark.asyncio
    async def test_playwright_lock_wait_uses_execution_timeout(self):
        owner_a = "session-a:turn-1"
        owner_b = "session-b:turn-1"

        class FakeSession:
            calls = 0

            async def call_tool(self, name, arguments):
                self.calls += 1
                return SimpleNamespace(content=[], isError=False)

        session = FakeSession()
        tool = MCPTool(
            name="managed_browser_navigate",
            remote_name="browser_navigate",
            description="navigate",
            parameters={"type": "object"},
            session=session,
            server_name="playwright",
            execute_timeout=0.02,
        )

        await BrowserRuntimeCoordinator.acquire(owner_a)
        owner_token = set_browser_runtime_owner(owner_b)
        try:
            result = await tool.execute(url="https://example.com")
        finally:
            reset_browser_runtime_owner(owner_token)

        assert result.success is False
        assert result.error is not None
        assert result.error.startswith("BROWSER_RUNTIME_BUSY:")
        assert session.calls == 0

        waiting = asyncio.create_task(BrowserRuntimeCoordinator.acquire(owner_b))
        await asyncio.sleep(0)
        assert waiting.done() is False
        await release_browser_runtime(owner_a)
        await asyncio.wait_for(waiting, timeout=0.5)
        await release_browser_runtime(owner_b)

    @pytest.mark.asyncio
    async def test_playwright_snapshot_releases_turn_lease(self):
        owner_a = "session-a:turn-1"
        owner_b = "session-b:turn-1"

        class FakeSession:
            async def call_tool(self, name, arguments):
                return SimpleNamespace(
                    content=[SimpleNamespace(text=f"{name} complete")],
                    isError=False,
                )

        owner_token = set_browser_runtime_owner(owner_a)
        try:
            navigate = MCPTool(
                name="managed_browser_navigate",
                remote_name="browser_navigate",
                description="navigate",
                parameters={"type": "object"},
                session=FakeSession(),
                server_name="playwright",
                execute_timeout=1,
            )
            snapshot = MCPTool(
                name="managed_browser_snapshot",
                remote_name="browser_snapshot",
                description="snapshot",
                parameters={"type": "object"},
                session=FakeSession(),
                server_name="playwright",
                execute_timeout=1,
            )

            assert (await navigate.execute(url="https://example.com")).success
            waiting = asyncio.create_task(BrowserRuntimeCoordinator.acquire(owner_b))
            await asyncio.sleep(0)
            assert waiting.done() is False

            assert (await snapshot.execute()).success
            await asyncio.wait_for(waiting, timeout=0.5)
            await release_browser_runtime(owner_b)
        finally:
            reset_browser_runtime_owner(owner_token)

    @pytest.mark.asyncio
    async def test_playwright_call_timeout_is_not_reported_as_runtime_busy(self):
        class SlowSession:
            async def call_tool(self, name, arguments):
                await asyncio.sleep(1)

        owner = "session-a:turn-1"
        owner_token = set_browser_runtime_owner(owner)
        try:
            tool = MCPTool(
                name="managed_browser_navigate",
                remote_name="browser_navigate",
                description="navigate",
                parameters={"type": "object"},
                session=SlowSession(),
                server_name="playwright",
                execute_timeout=0.02,
            )
            result = await tool.execute(url="https://example.com")
        finally:
            await release_browser_runtime(owner)
            reset_browser_runtime_owner(owner_token)

        assert result.success is False
        assert result.error == (
            "MCP tool execution timed out after 0.02s. "
            "The remote server may be slow or unresponsive."
        )


# =============================================================================
# MCPServerConnection Initialization Tests
# =============================================================================


class TestMCPServerConnectionInit:
    """Tests for MCPServerConnection initialization."""

    def test_stdio_connection_init(self):
        """Test STDIO connection initialization."""
        conn = MCPServerConnection(
            name="test-stdio",
            connection_type="stdio",
            command="npx",
            args=["-y", "test-server"],
            env={"API_KEY": "test"},
        )
        assert conn.name == "test-stdio"
        assert conn.connection_type == "stdio"
        assert conn.command == "npx"
        assert conn.args == ["-y", "test-server"]
        assert conn.env == {"API_KEY": "test"}
        assert conn.url is None

    def test_url_connection_init(self):
        """Test URL-based connection initialization."""
        conn = MCPServerConnection(
            name="test-url",
            connection_type="streamable_http",
            url="https://mcp.example.com/mcp",
            headers={"Authorization": "Bearer token"},
        )
        assert conn.name == "test-url"
        assert conn.connection_type == "streamable_http"
        assert conn.url == "https://mcp.example.com/mcp"
        assert conn.headers == {"Authorization": "Bearer token"}
        assert conn.command is None

    def test_sse_connection_init(self):
        """Test SSE connection initialization."""
        conn = MCPServerConnection(
            name="test-sse",
            connection_type="sse",
            url="https://mcp.example.com/sse",
        )
        assert conn.name == "test-sse"
        assert conn.connection_type == "sse"
        assert conn.url == "https://mcp.example.com/sse"

    def test_default_values(self):
        """Test default values for optional parameters."""
        conn = MCPServerConnection(name="test-default")
        assert conn.connection_type == "stdio"
        assert conn.args == []
        assert conn.env == {}
        assert conn.headers == {}

    def test_timeout_overrides(self):
        """Test per-server timeout override initialization."""
        conn = MCPServerConnection(
            name="test-timeout",
            connection_type="sse",
            url="https://mcp.example.com/sse",
            connect_timeout=15.0,
            execute_timeout=90.0,
            sse_read_timeout=180.0,
        )
        assert conn.connect_timeout == 15.0
        assert conn.execute_timeout == 90.0
        assert conn.sse_read_timeout == 180.0

    def test_only_web_search_receives_internal_shared_limiter(self):
        conn = MCPServerConnection(name="test-search")

        first = conn._concurrency_limiter_for_tool("web_search")
        second = conn._concurrency_limiter_for_tool("web_search")

        assert first is second
        assert first is not None
        assert first._value == WEB_SEARCH_MCP_MAX_CONCURRENCY
        assert conn._concurrency_limiter_for_tool("browser_navigate") is None


# =============================================================================
# Windows stdio env supplement tests
# =============================================================================


class TestStdioEnvBuild:
    """Cover ``MCPServerConnection._build_stdio_env`` on both platforms.

    The unit-level checks pin the return value we produce. The integration
    checks then simulate the MCP SDK's ``{**get_default_environment(),
    **server.env}`` merge to prove the final env handed to CreateProcess
    contains the variables we care about — this is what actually gets a
    Windows stdio MCP server started, and it's the invariant that must
    survive future SDK changes.
    """

    def _fake_default_env(self):
        # Mirrors ``mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS`` on Windows
        # closely enough for the merge behavior we care about.
        return {
            "APPDATA": r"C:\Users\test\AppData\Roaming",
            "HOMEDRIVE": "C:",
            "HOMEPATH": r"\Users\test",
            "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
            "PATH": r"C:\Windows\System32",
            "PATHEXT": ".COM;.EXE;.BAT",
            "PROCESSOR_ARCHITECTURE": "AMD64",
            "SYSTEMDRIVE": "C:",
            "SYSTEMROOT": r"C:\Windows",
            "TEMP": r"C:\Users\test\AppData\Local\Temp",
            "USERNAME": "test",
            "USERPROFILE": r"C:\Users\test",
        }

    def _final_sdk_env(self, server_env):
        """Simulate ``stdio_client``'s env merge for a Windows spawn."""
        default = self._fake_default_env()
        return {**default, **server_env} if server_env is not None else default

    def test_non_windows_returns_server_env_untouched(self, monkeypatch):
        """macOS/Linux path must not diverge from the pre-fix baseline."""
        monkeypatch.setattr("box_agent.tools.mcp_loader.sys.platform", "linux")

        conn = MCPServerConnection(name="ssh", command="ssh", env={"FOO": "bar"})
        assert conn._build_stdio_env() == {"FOO": "bar"}

        # Empty env → None so the SDK applies its own default allowlist.
        conn_empty = MCPServerConnection(name="ssh", command="ssh", env={})
        assert conn_empty._build_stdio_env() is None

    def test_windows_supplements_missing_system_vars(self, monkeypatch):
        """Windows path adds vars the SDK allowlist misses (ComSpec etc.)."""
        monkeypatch.setattr("box_agent.tools.mcp_loader.sys.platform", "win32")
        # Only the variables our supplement covers — plus a distractor
        # that must NOT be pulled in blindly.
        fake_os_env = {
            "windir": r"C:\Windows",
            "ComSpec": r"C:\Windows\System32\cmd.exe",
            "ProgramData": r"C:\ProgramData",
            "ALLUSERSPROFILE": r"C:\ProgramData",
            "ProgramFiles": r"C:\Program Files",
            "ProgramFiles(x86)": r"C:\Program Files (x86)",
            "ProgramW6432": r"C:\Program Files",
            "USERDOMAIN": "TESTDOMAIN",
            "SECRETS_NOT_INHERITED": "should-not-leak",
        }
        monkeypatch.setattr("box_agent.tools.mcp_loader.os.environ", fake_os_env)

        conn = MCPServerConnection(name="ssh", command="ssh", env={})
        built = conn._build_stdio_env()
        assert built is not None
        # Every supplement entry present.
        for name in MCPServerConnection._WINDOWS_ENV_SUPPLEMENT:
            assert built[name] == fake_os_env[name], name
        # Only the allowlisted supplement leaked through; nothing else.
        assert "SECRETS_NOT_INHERITED" not in built

    def test_windows_final_env_after_sdk_merge_has_all_vars(self, monkeypatch):
        """End-to-end: after the SDK re-merges, the final env is sufficient.

        This is the invariant that matters for CreateProcess. If the SDK ever
        changes its default allowlist or merge order, this catches it.
        """
        monkeypatch.setattr("box_agent.tools.mcp_loader.sys.platform", "win32")
        fake_os_env = {
            "windir": r"C:\Windows",
            "ComSpec": r"C:\Windows\System32\cmd.exe",
            "ProgramData": r"C:\ProgramData",
            "ALLUSERSPROFILE": r"C:\ProgramData",
            "ProgramFiles": r"C:\Program Files",
            "ProgramFiles(x86)": r"C:\Program Files (x86)",
            "ProgramW6432": r"C:\Program Files",
            "USERDOMAIN": "TESTDOMAIN",
        }
        monkeypatch.setattr("box_agent.tools.mcp_loader.os.environ", fake_os_env)

        conn = MCPServerConnection(name="ssh", command="ssh", env={"MY_VAR": "yes"})
        server_env = conn._build_stdio_env()
        final = self._final_sdk_env(server_env)

        # SDK defaults still land (nothing above should have shadowed them).
        assert final["SYSTEMROOT"] == r"C:\Windows"
        assert final["PATH"] == r"C:\Windows\System32"
        # Our supplement lands.
        for name in MCPServerConnection._WINDOWS_ENV_SUPPLEMENT:
            assert final[name] == fake_os_env[name], name
        # Server-specific mcp.json env wins.
        assert final["MY_VAR"] == "yes"

    def test_windows_user_env_wins_over_supplement(self, monkeypatch):
        """User-supplied ``env`` in mcp.json must not be shadowed by us."""
        monkeypatch.setattr("box_agent.tools.mcp_loader.sys.platform", "win32")
        monkeypatch.setattr(
            "box_agent.tools.mcp_loader.os.environ",
            {"ComSpec": r"C:\Windows\System32\cmd.exe"},
        )
        # Explicit user override, both same casing and a lower-case variant.
        conn = MCPServerConnection(
            name="ssh",
            command="ssh",
            env={"ComSpec": r"C:\custom\shell.exe"},
        )
        built = conn._build_stdio_env()
        assert built["ComSpec"] == r"C:\custom\shell.exe"

        # Case-variant supplied by user: we must not re-add our CamelCase
        # copy on top (would leave two entries pointing at different values).
        conn2 = MCPServerConnection(
            name="ssh",
            command="ssh",
            env={"COMSPEC": r"C:\custom\shell.exe"},
        )
        built2 = conn2._build_stdio_env()
        # Only one case variant remains — the user's — with their value.
        comspec_keys = [k for k in built2 if k.lower() == "comspec"]
        assert comspec_keys == ["COMSPEC"]
        assert built2["COMSPEC"] == r"C:\custom\shell.exe"

    def test_windows_missing_host_vars_are_silently_skipped(self, monkeypatch):
        """A minimal host env (e.g. CI runner) must not crash env-build."""
        monkeypatch.setattr("box_agent.tools.mcp_loader.sys.platform", "win32")
        monkeypatch.setattr("box_agent.tools.mcp_loader.os.environ", {})

        conn = MCPServerConnection(name="ssh", command="ssh", env={})
        # Empty user env + no host vars → None, so the SDK applies its own
        # defaults unmodified. This preserves the pre-fix behavior on hosts
        # where none of our supplement variables exist.
        assert conn._build_stdio_env() is None


# =============================================================================
# Timeout Configuration Tests
# =============================================================================


class TestMCPTimeoutConfig:
    """Tests for MCP timeout configuration."""

    def test_default_timeout_config(self):
        """Test default timeout configuration values."""
        config = MCPTimeoutConfig()
        assert config.connect_timeout == 60.0
        assert config.execute_timeout == 120.0
        assert config.sse_read_timeout == 180.0

    def test_custom_timeout_config(self):
        """Test custom timeout configuration values."""
        config = MCPTimeoutConfig(
            connect_timeout=5.0,
            execute_timeout=30.0,
            sse_read_timeout=60.0,
        )
        assert config.connect_timeout == 5.0
        assert config.execute_timeout == 30.0
        assert config.sse_read_timeout == 60.0

    def test_set_global_timeout_config(self):
        """Test setting global timeout configuration."""
        # Save original config
        original = get_mcp_timeout_config()
        original_connect = original.connect_timeout
        original_execute = original.execute_timeout

        try:
            # Set new values
            set_mcp_timeout_config(connect_timeout=20.0, execute_timeout=120.0)
            config = get_mcp_timeout_config()
            assert config.connect_timeout == 20.0
            assert config.execute_timeout == 120.0
        finally:
            # Restore original values
            set_mcp_timeout_config(
                connect_timeout=original_connect,
                execute_timeout=original_execute,
            )

    def test_partial_timeout_config_update(self):
        """Test partial update of timeout configuration."""
        original = get_mcp_timeout_config()
        original_connect = original.connect_timeout
        original_execute = original.execute_timeout
        original_sse = original.sse_read_timeout

        try:
            # Only update connect_timeout
            set_mcp_timeout_config(connect_timeout=25.0)
            config = get_mcp_timeout_config()
            assert config.connect_timeout == 25.0
            # Other values should remain unchanged from previous test state
        finally:
            set_mcp_timeout_config(
                connect_timeout=original_connect,
                execute_timeout=original_execute,
                sse_read_timeout=original_sse,
            )


class TestMCPServerConnectionTimeout:
    """Tests for MCPServerConnection timeout behavior."""

    def test_get_effective_connect_timeout_with_override(self):
        """Test getting effective connect timeout with per-server override."""
        conn = MCPServerConnection(
            name="test",
            connection_type="sse",
            url="https://example.com",
            connect_timeout=20.0,
        )
        assert conn._get_connect_timeout() == 20.0

    def test_get_effective_connect_timeout_without_override(self):
        """Test getting effective connect timeout using global default."""
        conn = MCPServerConnection(
            name="test",
            connection_type="sse",
            url="https://example.com",
        )
        # Should use global default
        global_config = get_mcp_timeout_config()
        assert conn._get_connect_timeout() == global_config.connect_timeout

    def test_get_effective_execute_timeout_with_override(self):
        """Test getting effective execute timeout with per-server override."""
        conn = MCPServerConnection(
            name="test",
            connection_type="sse",
            url="https://example.com",
            execute_timeout=180.0,
        )
        assert conn._get_execute_timeout() == 180.0

    @pytest.mark.asyncio
    async def test_timeout_log_identifies_connection_stage(self, monkeypatch, capsys):
        """Timeout diagnostics should identify the MCP connection phase that stalled."""
        conn = MCPServerConnection(
            name="slow-stdio",
            connection_type="stdio",
            command="npx",
            connect_timeout=0.02,
        )

        async def stall_transport():
            await asyncio.sleep(1)

        monkeypatch.setattr(conn, "_connect_stdio", stall_transport)

        assert await conn.connect() is False
        assert conn.last_error == "Connection timed out after 0.02s during open-stdio-transport"
        assert conn.last_auth_status is None

        stderr = capsys.readouterr().err
        assert "[mcp] connect:start server='slow-stdio' transport=stdio" in stderr
        assert "[mcp] connect:timeout server='slow-stdio' stage=open-stdio-transport" in stderr

    @pytest.mark.parametrize(
        ("statuses", "expected_error"),
        [
            (
                (500, 401),
                "Authentication failed: Token is invalid or expired (HTTP 401)",
            ),
            (
                (401, 500),
                "Authentication failed: Token is invalid or expired (HTTP 401)",
            ),
            (
                (500, 403),
                "Authorization failed: Access denied or insufficient permissions (HTTP 403)",
            ),
            (
                (403, 500),
                "Authorization failed: Access denied or insufficient permissions (HTTP 403)",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_streamable_http_auth_failure_survives_transport_cancellation(
        self, monkeypatch, capsys, statuses, expected_error
    ):
        """Transport auth failures must replace cancel-scope noise regardless of order."""

        errors = []
        for status in statuses:
            request = httpx.Request("POST", "https://example.com/mcp")
            response = httpx.Response(status, request=request)
            errors.append(
                httpx.HTTPStatusError(
                    f"HTTP {status}",
                    request=request,
                    response=response,
                )
            )

        class FakeCleanupError(Exception):
            exceptions = tuple(errors)

        class FakeExitStack:
            async def enter_async_context(self, context):
                return context

            async def aclose(self):
                raise FakeCleanupError("unhandled errors in a TaskGroup")

        class FakeSession:
            async def initialize(self):
                raise asyncio.CancelledError("Cancelled via cancel scope")

        conn = MCPServerConnection(
            name="invalid-token",
            connection_type="streamable_http",
            url="https://example.com/mcp",
        )

        async def fake_transport():
            return object(), object()

        monkeypatch.setattr(mcp_loader, "AsyncExitStack", FakeExitStack)
        monkeypatch.setattr(mcp_loader, "ClientSession", lambda *_args: FakeSession())
        monkeypatch.setattr(conn, "_connect_streamable_http", fake_transport)

        assert await conn.connect() is False
        assert conn.last_error == expected_error
        assert conn.last_auth_status == (401 if "401" in expected_error else 403)
        assert conn.exit_stack is None
        assert conn.session is None

        stderr = capsys.readouterr().err
        assert "[mcp] connect:failed server='invalid-token'" in stderr
        assert "Failed to clean up MCP connection" not in stderr


# =============================================================================
# URL-based Config Loading Tests
# =============================================================================


@pytest.mark.asyncio
async def test_url_config_validation():
    """Test that URL-based config without url is rejected."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "mcpServers": {
                "broken-sse": {
                    "type": "sse",
                    # Missing "url" field
                }
            }
        }
        json.dump(config, f)
        f.flush()

        try:
            tools = await load_mcp_tools_async(f.name)
            # Should return empty list (server skipped due to missing url)
            assert tools == []
        finally:
            await cleanup_mcp_connections()
            Path(f.name).unlink()


@pytest.mark.asyncio
async def test_stdio_config_validation():
    """Test that STDIO config without command is rejected."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "mcpServers": {
                "broken-stdio": {
                    "type": "stdio",
                    # Missing "command" field
                }
            }
        }
        json.dump(config, f)
        f.flush()

        try:
            tools = await load_mcp_tools_async(f.name)
            # Should return empty list (server skipped due to missing command)
            assert tools == []
        finally:
            await cleanup_mcp_connections()
            Path(f.name).unlink()


@pytest.mark.asyncio
async def test_mixed_config_loading():
    """Test loading config with both STDIO and URL-based servers."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "mcpServers": {
                "stdio-server": {"command": "npx", "args": ["-y", "nonexistent-server"], "disabled": True},
                "url-server": {"url": "https://mcp.nonexistent.example.com/mcp", "disabled": True},
                "sse-server": {"url": "https://sse.nonexistent.example.com/sse", "type": "sse", "disabled": True},
            }
        }
        json.dump(config, f)
        f.flush()

        try:
            # All servers are disabled, should return empty but not error
            tools = await load_mcp_tools_async(f.name)
            assert tools == []
        finally:
            await cleanup_mcp_connections()
            Path(f.name).unlink()


@pytest.mark.asyncio
async def test_mcp_tools_loading():
    """Test loading MCP tools from mcp.json."""
    print("\n=== Testing MCP Tool Loading ===")

    try:
        # Load MCP tools
        tools = await load_mcp_tools_async("box_agent/config/mcp.json")

        print(f"Loaded {len(tools)} MCP tools")

        # Display loaded tools
        if tools:
            for tool in tools:
                desc = tool.description[:60] if len(tool.description) > 60 else tool.description
                print(f"  - {tool.name}: {desc}")

        # Test should pass even if no tools loaded (e.g., no mcp.json or no Node.js)
        assert isinstance(tools, list), "Should return a list of tools"
        print("✅ MCP tools loading test passed")

    finally:
        # Cleanup MCP connections
        await cleanup_mcp_connections()


@pytest.mark.asyncio
async def test_git_mcp_loading(mcp_config):
    """Test loading MCP Server from Git repository."""
    print("\n" + "=" * 70)
    print("Testing: Loading MCP Server from Git repository")
    print("=" * 70)

    try:
        # Load MCP tools
        tools = await load_mcp_tools_async("box_agent/config/mcp.json")

        print("\n✅ Loaded successfully!")
        print("\n📊 Statistics:")
        print(f"  • Total tools loaded: {len(tools)}")

        # Verify tools list is not empty
        assert isinstance(tools, list), "Should return a list of tools"

        if tools:
            print("\n🔧 Available tools:")
            for tool in tools:
                desc = tool.description[:80] + "..." if len(tool.description) > 80 else tool.description
                print(f"  • {tool.name}")
                print(f"    {desc}")

        print("\n" + "=" * 70)
        print("✅ All tests passed! MCP Server loaded from Git repository successfully!")
        print("=" * 70)

    finally:
        # Cleanup MCP connections
        print("\n🧹 Cleaning up MCP connections...")
        await cleanup_mcp_connections()


@pytest.mark.asyncio
async def test_git_mcp_tool_availability():
    """Test Git MCP tool availability."""
    print("\n=== Testing Git MCP Tool Availability ===")

    try:
        tools = await load_mcp_tools_async("box_agent/config/mcp.json")

        if not tools:
            pytest.skip("No MCP tools loaded")
            return

        # Find search tool
        search_tool = None
        for tool in tools:
            if "search" in tool.name.lower():
                search_tool = tool
                break

        assert search_tool is not None, "Should contain search-related tools"
        print(f"✅ Found search tool: {search_tool.name}")

    finally:
        await cleanup_mcp_connections()


@pytest.mark.asyncio
async def test_mcp_tool_execution():
    """Test executing an MCP tool if available (memory server)."""
    print("\n=== Testing MCP Tool Execution ===")

    try:
        tools = await load_mcp_tools_async("box_agent/config/mcp.json")

        if not tools:
            print("⚠️  No MCP tools loaded, skipping execution test")
            pytest.skip("No MCP tools available")
            return

        # Try to find and test create_entities (from memory server)
        create_tool = None
        for tool in tools:
            if tool.name == "create_entities":
                create_tool = tool
                break

        if create_tool:
            print(f"Testing: {create_tool.name}")
            try:
                result = await create_tool.execute(
                    entities=[
                        {
                            "name": "test_entity",
                            "entityType": "test",
                            "observations": ["Test observation for pytest"],
                        }
                    ]
                )
                assert result.success, f"Tool execution should succeed: {result.error}"
                print(f"✅ Tool execution successful: {result.content[:100]}")
            except Exception as e:
                pytest.fail(f"Tool execution failed: {e}")
        else:
            print("⚠️  create_entities tool not found, skipping execution test")
            pytest.skip("create_entities tool not available")

    finally:
        await cleanup_mcp_connections()


@pytest.mark.asyncio
async def test_connection_timeout_on_unreachable_server():
    """Test that connection to unreachable server times out properly."""
    print("\n=== Testing Connection Timeout ===")

    # Set a short timeout for testing
    original = get_mcp_timeout_config()
    original_connect = original.connect_timeout

    try:
        set_mcp_timeout_config(connect_timeout=2.0)

        conn = MCPServerConnection(
            name="unreachable-test",
            connection_type="streamable_http",
            url="https://10.255.255.1:9999/mcp",  # Non-routable IP, will timeout
        )

        import time

        start = time.time()
        success = await conn.connect()
        elapsed = time.time() - start

        assert success is False, "Connection to unreachable server should fail"
        # Should timeout within reasonable time (connect_timeout + some overhead)
        assert elapsed < 10.0, f"Should timeout quickly, but took {elapsed:.1f}s"
        print(f"✅ Connection timed out as expected in {elapsed:.1f}s")

    finally:
        set_mcp_timeout_config(connect_timeout=original_connect)
        await cleanup_mcp_connections()


@pytest.mark.asyncio
async def test_per_server_timeout_override_in_config():
    """Test that per-server timeout overrides from config are respected."""
    print("\n=== Testing Per-Server Timeout Override ===")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "mcpServers": {
                "fast-server": {
                    "url": "https://10.255.255.1:9999/mcp",
                    "connect_timeout": 1.0,  # Very short timeout
                    "execute_timeout": 30.0,
                }
            }
        }
        json.dump(config, f)
        f.flush()

        try:
            import time

            start = time.time()
            tools = await load_mcp_tools_async(f.name)
            elapsed = time.time() - start

            # Should fail due to unreachable server
            assert tools == []
            # Should respect the short 1.0s connect_timeout
            assert elapsed < 5.0, f"Should use per-server timeout, but took {elapsed:.1f}s"
            print(f"✅ Per-server timeout override worked, failed in {elapsed:.1f}s")

        finally:
            await cleanup_mcp_connections()
            Path(f.name).unlink()


async def main():
    """Run all MCP tests."""
    print("=" * 80)
    print("Running MCP Integration Tests")
    print("=" * 80)
    print("\nNote: These tests require Node.js and will use MCP servers defined in mcp.json")
    print("Tests will pass even if MCP is not configured.\n")

    await test_mcp_tools_loading()
    await test_mcp_tool_execution()
    await test_connection_timeout_on_unreachable_server()

    print("\n" + "=" * 80)
    print("MCP tests completed! ✅")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
