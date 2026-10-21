"""Exercise MCP lifecycle ownership with the real SDK and in-memory transport."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession, types
from mcp.server.lowlevel import Server

from box_agent.tools import mcp_loader
from box_agent.tools.mcp_sources import ResolvedMcpServer
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog


@pytest.fixture
def sdk_connection(monkeypatch):
    server = Server("lifecycle-test")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name="echo", description="Echo", inputSchema={"type": "object"})]

    @server.call_tool()
    async def call_tool(name, arguments):
        return [types.TextContent(type="text", text="ok")]

    state = SimpleNamespace(scopes=[], owners=[], exits=[], transports=0, stall=None,
                            closing=None, release_close=None, close_error=False)

    class ObservedSession(ClientSession):
        async def __aenter__(self):
            result = await super().__aenter__()
            state.scopes.append(self._task_group.cancel_scope)
            state.owners.append(asyncio.current_task())
            return result

        async def initialize(self):
            if state.stall is not None:
                state.stall.set()
                await asyncio.Event().wait()
            return await super().initialize()

        async def __aexit__(self, *args):
            state.exits.append(asyncio.current_task())
            return await super().__aexit__(*args)

    @asynccontextmanager
    async def transport():
        request_send, request_receive = anyio.create_memory_object_stream(10)
        response_send, response_receive = anyio.create_memory_object_stream(10)
        async with anyio.create_task_group() as group:
            async with request_send, request_receive, response_send, response_receive:
                state.transports += 1
                group.start_soon(server.run, request_receive, response_send,
                                 server.create_initialization_options())
                try:
                    yield response_receive, request_send
                finally:
                    if state.closing is not None:
                        state.closing.set()
                        await state.release_close.wait()
                    state.transports -= 1
                    group.cancel_scope.cancel()
        if state.close_error:
            raise RuntimeError("transport cleanup failed")

    async def open_transport(conn):
        return await conn.exit_stack.enter_async_context(transport())

    monkeypatch.setattr(mcp_loader, "ClientSession", ObservedSession)
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "_connect_stdio", open_transport)
    conn = mcp_loader.MCPServerConnection("lifecycle-test", command="unused", connect_timeout=1)
    return conn, state


async def test_repeated_cross_task_disconnect_closes_real_sdk_scopes(sdk_connection, monkeypatch):
    conn, state = sdk_connection
    from anyio._backends._asyncio import CancelScope

    callbacks = []
    original = CancelScope._deliver_cancellation

    def count(scope, origin):
        callbacks.append(scope)
        return original(scope, origin)

    monkeypatch.setattr(CancelScope, "_deliver_cancellation", count)
    for _ in range(5):
        assert (await asyncio.gather(conn.connect())) == [True]
        try:
            assert len(conn.tools) == 1
            result = await conn.session.call_tool("echo", {})
            assert result.content[0].text == "ok"
        finally:
            await asyncio.gather(conn.disconnect(), conn.disconnect())
        assert state.transports == 0
        assert not any(scope._active for scope in state.scopes)
        assert all(task.done() for task in state.owners)
        assert state.owners == state.exits
        assert conn.session is None
        assert conn.exit_stack is None
        # Give any mistakenly retained cancellation callback several turns.
        await asyncio.sleep(0)
        before = len(callbacks)
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(callbacks) == before


async def test_cancelled_startup_waits_for_sdk_cleanup(sdk_connection):
    conn, state = sdk_connection
    state.stall = asyncio.Event()
    startup = asyncio.create_task(conn.connect())
    await asyncio.wait_for(state.stall.wait(), 2)
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup
    assert state.transports == 0
    assert state.owners == state.exits
    assert not any(scope._active for scope in state.scopes)
    state.stall = None
    assert await conn.connect()
    await conn.disconnect()


async def test_startup_timeout_closes_sdk_and_can_retry(sdk_connection):
    conn, state = sdk_connection
    conn.connect_timeout = 0.02
    state.stall = asyncio.Event()
    assert await conn.connect() is False
    assert "timed out" in conn.last_error
    assert state.transports == 0
    assert not any(scope._active for scope in state.scopes)
    state.stall = None
    conn.connect_timeout = 1
    assert await conn.connect()
    await conn.disconnect()


async def test_cancelled_disconnect_does_not_interrupt_sdk_cleanup(sdk_connection):
    conn, state = sdk_connection
    state.closing, state.release_close = asyncio.Event(), asyncio.Event()
    assert await conn.connect()
    close = asyncio.create_task(conn.disconnect())
    await asyncio.wait_for(state.closing.wait(), 2)
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert state.transports == 1
    state.release_close.set()
    await conn.disconnect()
    assert state.transports == 0
    assert state.owners == state.exits


async def test_close_timeout_retains_owner_until_cleanup_finishes(sdk_connection, monkeypatch):
    conn, state = sdk_connection
    state.closing, state.release_close = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(mcp_loader, "_MCP_CONNECTION_CLOSE_TIMEOUT", 0.02)
    assert await conn.connect()
    try:
        with pytest.raises(TimeoutError, match="cleanup"):
            await conn.disconnect()
        with pytest.raises(TimeoutError, match="cleanup"):
            await conn.connect()
        assert len(state.owners) == 1
        assert state.transports == 1
    finally:
        state.release_close.set()
        await conn.disconnect()
    assert state.transports == 0


async def test_cleanup_failure_is_reported_not_treated_as_success(sdk_connection, capsys):
    conn, state = sdk_connection
    assert await conn.connect()
    state.close_error = True
    with pytest.raises(RuntimeError, match="Failed to close"):
        await conn.disconnect()
    assert "transport cleanup failed" in capsys.readouterr().err
    assert state.transports == 0


@pytest.mark.parametrize("cancel_via", ["asyncio", "anyio"])
async def test_unexpected_owner_exit_cleans_up_and_marks_connection_failed(
    sdk_connection, monkeypatch, cancel_via,
):
    conn, state = sdk_connection
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [conn])
    monkeypatch.setattr(mcp_loader, "_mcp_status", {})
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    assert await conn.connect()
    mcp_loader._replace_server_catalog(conn)
    assert catalog.snapshot()
    # Simulate the SDK's background task group cancelling its owning task.
    owner = state.owners[0]
    if cancel_via == "anyio":
        state.scopes[0].cancel()
    else:
        owner.cancel()
    await asyncio.wait_for(asyncio.shield(owner), 2)
    assert state.transports == 0
    assert not any(scope._active for scope in state.scopes)
    assert mcp_loader._mcp_status[conn.name].state == "failed"
    assert not catalog.snapshot()
    assert mcp_loader.get_all_mcp_tools() == []
    await conn.disconnect()


@pytest.mark.parametrize("operation", ["disconnect", "reconnect", "cleanup"])
async def test_cleanup_failure_is_not_hidden_by_connection_registry(monkeypatch, operation):
    async def fail_close():
        raise TimeoutError("cleanup still running")

    closed = []

    async def other_close():
        closed.append("other")

    conn = SimpleNamespace(name="blocked", tools=[SimpleNamespace(name="old_tool")], disconnect=fail_close)
    other = SimpleNamespace(name="other", tools=[], disconnect=other_close)
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [conn, other])
    monkeypatch.setattr(mcp_loader, "_mcp_status", {})
    monkeypatch.setattr(mcp_loader, "_mcp_config_path", None)
    monkeypatch.setattr(mcp_loader, "_mcp_reconnect_locks", {})
    monkeypatch.setattr(mcp_loader, "_cua_service", None)
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    monkeypatch.setattr(mcp_loader, "_mcp_server_definitions", {
        "blocked": ResolvedMcpServer("blocked", {"command": "unused"}, "user",
                                     "test", None, None, "unused.json", "test"),
    })
    monkeypatch.setattr(mcp_loader, "_build_connection",
                        lambda *_: pytest.fail("must finish old cleanup before reconnecting"))

    if operation == "cleanup":
        with pytest.raises(RuntimeError, match="blocked"):
            await mcp_loader.cleanup_mcp_connections()
        assert closed == ["other"]
        assert mcp_loader._mcp_connections == [conn]
    else:
        method = (mcp_loader.disconnect_mcp_server if operation == "disconnect"
                  else mcp_loader.reconnect_mcp_server)
        result = await method("blocked")
        assert result["success"] is False
        assert result["error"] == "cleanup still running"
        assert conn in mcp_loader._mcp_connections
    assert mcp_loader._mcp_status["blocked"].state == "failed"
    assert mcp_loader.get_all_mcp_tools() == []


async def test_cancelled_global_cleanup_keeps_unfinished_connections_for_retry(monkeypatch):
    async def interrupted():
        raise asyncio.CancelledError()

    conn = SimpleNamespace(name="interrupted", disconnect=interrupted)
    pending = SimpleNamespace(name="pending")
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [conn, pending])
    monkeypatch.setattr(mcp_loader, "_cua_service", None)
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    with pytest.raises(asyncio.CancelledError):
        await mcp_loader.cleanup_mcp_connections()
    assert mcp_loader._mcp_connections == [conn, pending]


@pytest.mark.parametrize("operation", ["startup", "reconnect"])
async def test_connection_lost_before_publication_is_not_registered(
    sdk_connection, monkeypatch, tmp_path, operation,
):
    conn, state = sdk_connection
    catalog = MCPToolCatalog()
    definition = ResolvedMcpServer(conn.name, {"command": "unused"}, "user",
                                   "test", None, None, "unused.json", "test")
    for name, value in {
        "_mcp_connections": [], "_mcp_status": {}, "_mcp_config_path": None,
        "_mcp_reconnect_locks": {}, "_cua_service": None, "_mcp_loading": False,
        "_mcp_auth_file": "", "_mcp_auth_token": "", "_mcp_auth_fingerprint": "",
        "_mcp_server_definitions": {conn.name: definition},
    }.items():
        monkeypatch.setattr(mcp_loader, name, value)
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    monkeypatch.setattr(mcp_loader, "_resolve_registered_sources", lambda _: {conn.name: definition})
    monkeypatch.setattr(mcp_loader, "_build_connection", lambda _: conn)
    connect = conn.connect

    async def connect_then_lose_transport():
        result = await connect()
        owner = state.owners[-1]
        owner.cancel()
        await owner
        return result

    monkeypatch.setattr(conn, "connect", connect_then_lose_transport)
    if operation == "startup":
        config = tmp_path / "mcp.json"
        config.write_text("{}", encoding="utf-8")
        assert await mcp_loader.load_mcp_tools_async(str(config)) == []
    else:
        assert (await mcp_loader.reconnect_mcp_server(conn.name))["success"] is False
    assert mcp_loader._mcp_connections == []
    assert mcp_loader._mcp_status[conn.name].state == "failed"
    assert not catalog.snapshot()
    await conn.disconnect()
