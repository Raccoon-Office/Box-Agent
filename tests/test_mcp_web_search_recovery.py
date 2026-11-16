"""Search session recovery through the real MCP SDK's HTTP transport."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from box_agent.tools import mcp_loader
from box_agent.tools.mcp_sources import ResolvedMcpServer
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog


@pytest.mark.parametrize("wait", [asyncio.create_task, asyncio.gather])
async def test_deadline_reports_timeout_through_nested_awaitables(wait):
    async def blocked():
        await asyncio.Future()

    with pytest.raises(TimeoutError):
        async with mcp_loader._timeout_compat(0):
            await wait(blocked())


async def test_caller_cancellation_during_deadline_cleanup_stays_cancelled():
    cleaning_up = asyncio.Event()

    async def operation():
        async with mcp_loader._timeout_compat(0):
            try:
                await asyncio.Future()
            finally:
                cleaning_up.set()
                await asyncio.Future()

    call = asyncio.create_task(operation())
    await asyncio.wait_for(cleaning_up.wait(), 2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call


async def test_deadline_during_caller_cancellation_cleanup_stays_cancelled():
    entered = asyncio.Event()

    async def operation():
        async with mcp_loader._timeout_compat(0.05):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.Future()

    call = asyncio.create_task(operation())
    await asyncio.wait_for(entered.wait(), 2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, 2)


@pytest.mark.parametrize("message", [None, "caller"])
async def test_caller_cancellation_in_deadline_callback_cycle_stays_cancelled(message):
    async def operation():
        task = asyncio.current_task()
        async with mcp_loader._timeout_compat(0):
            asyncio.get_running_loop().call_later(0, task.cancel, message)
            await asyncio.Future()

    call = asyncio.create_task(operation())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, 2)


async def test_outer_deadline_expires_across_nested_deadlines():
    with pytest.raises(TimeoutError, match="after 0s"):
        async with mcp_loader._timeout_compat(0):
            async with mcp_loader._timeout_compat(60):
                await asyncio.Future()


async def test_completed_deadline_restores_task_cancellation_and_disarms_timer():
    task = asyncio.current_task()
    original_cancel = task.cancel
    async with mcp_loader._timeout_compat(0):
        pass
    assert task.cancel == original_cancel
    await asyncio.sleep(0.01)


@pytest.fixture
async def search_server(monkeypatch):
    state = SimpleNamespace(
        initialized=0, expired=set(), calls=[], fail_initialize=False,
        reject_calls=False, tool_error=False, waiting=0, barrier=None, stall_initialize=None,
    )

    async def handle(request):
        if request.method != "POST":
            return httpx.Response(405)
        message = json.loads(request.content)
        method = message["method"]
        session_id = request.headers.get("mcp-session-id")
        if method == "initialize":
            assert session_id is None, "recovery must initialize without the expired ID"
            state.initialized += 1
            if state.stall_initialize is not None:
                state.stall_initialize.set()
                await asyncio.Event().wait()
            if state.fail_initialize:
                return httpx.Response(404)
            return httpx.Response(200, headers={"mcp-session-id": str(state.initialized)}, json={
                "jsonrpc": "2.0", "id": message["id"], "result": {
                    "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "search-test", "version": "1"},
                },
            })
        if method.startswith("notifications/"):
            return httpx.Response(202)
        if method == "tools/list":
            result = {"tools": [{"name": "web_search", "description": "Search", "inputSchema": {
                "type": "object", "properties": {"Query": {"type": "string"}},
            }}]}
        else:
            assert method == "tools/call"
            state.calls.append((session_id, message["params"], dict(request.headers)))
            if session_id in state.expired or state.reject_calls:
                if state.barrier is not None:
                    state.waiting += 1
                    if state.waiting == 5:
                        state.barrier.set()
                    await state.barrier.wait()
                return httpx.Response(404)
            result = {"content": [{"type": "text", "text": "search result"}], "isError": state.tool_error}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    def client_factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs)
        client.event_hooks["request"].append(mcp_loader._attach_mcp_session_header)
        return client

    monkeypatch.setattr(mcp_loader, "_mcp_http_client_options", lambda url: {"httpx_client_factory": client_factory})
    monkeypatch.setattr(mcp_loader, "_mcp_reconnect_locks", {})
    monkeypatch.setattr(mcp_loader, "_mcp_connection_epochs", {})
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [])
    monkeypatch.setattr(mcp_loader, "_mcp_status", {})
    monkeypatch.setattr(mcp_loader, "_mcp_config_path", None)
    monkeypatch.setattr(mcp_loader, "_cua_service", None)
    monkeypatch.setattr(mcp_loader, "_mcp_auth_file", "")
    monkeypatch.setattr(mcp_loader, "_mcp_auth_token", "")
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    name = mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME
    config = {"url": "https://xiaohuanxiong.com/api/web/mcp/web_search/v1/mcp", "type": "streamable_http"}
    definition = ResolvedMcpServer(name, config, "system", "search", None, None, "test.json", "test")
    monkeypatch.setattr(mcp_loader, "_mcp_server_definitions", {name: definition})
    conn = mcp_loader._build_connection(definition)
    assert await conn.connect()
    mcp_loader._mcp_connections.append(conn)
    mcp_loader._replace_server_catalog(conn)
    state.tool = conn.tools[0]
    state.catalog = catalog
    try:
        yield state
    finally:
        for current in list(mcp_loader._mcp_connections):
            await current.disconnect()


async def test_expired_search_session_recovers_and_old_tool_uses_replacement(search_server):
    state = search_server
    assert (await state.tool.execute(query="before")).success
    state.expired.add("1")
    result = await state.tool.invoke({"query": "after", "count": 3})
    assert result.success, result.error
    assert state.initialized == 2
    assert [(sid, params["arguments"]) for sid, params, _ in state.calls] == [
        ("1", {"Query": "before"}),
        ("1", {"Query": "after", "Count": 3}),
        ("2", {"Query": "after", "Count": 3}),
    ]
    assert (await state.tool.execute(query="old reference")).success
    assert state.calls[-1][0] == "2"
    assert state.initialized == 2
    assert mcp_loader.get_mcp_tools_for_server(state.tool.server_name)[0] is not state.tool


async def test_concurrent_expired_searches_share_one_reconnect(search_server):
    state = search_server
    state.expired.add("1")
    state.barrier = asyncio.Event()
    results = await asyncio.wait_for(asyncio.gather(*(
        state.tool.execute(query=f"query {index}") for index in range(5)
    )), 5)
    assert all(result.success for result in results), [result.error for result in results]
    assert state.initialized == 2
    assert len(state.calls) == 10
    replacement = mcp_loader.get_mcp_tools_for_server(state.tool.server_name)[0]
    assert replacement._concurrency_limiter is state.tool._concurrency_limiter


@pytest.mark.parametrize("failure", ["initialize", "call"])
async def test_failed_recovery_stops_after_one_attempt(search_server, failure):
    state = search_server
    state.expired.add("1")
    state.fail_initialize = failure == "initialize"
    state.reject_calls = failure == "call"
    result = await asyncio.wait_for(state.tool.execute(query="fails"), 5)
    assert not result.success
    assert "Session terminated" in result.error
    assert state.initialized == 2
    assert len(state.calls) == (1 if failure == "initialize" else 2)
    assert not state.catalog.loading


async def test_normal_search_tool_error_does_not_reconnect(search_server):
    state = search_server
    state.tool_error = True
    result = await state.tool.execute(query="fails")
    assert not result.success
    assert state.initialized == 1
    assert len(state.calls) == 1


async def test_removed_search_connection_is_not_resurrected(search_server):
    state = search_server
    await mcp_loader.disconnect_mcp_server(state.tool.server_name)
    result = await state.tool.execute(query="removed")
    assert not result.success
    assert state.initialized == 1
    assert state.calls == []


async def test_other_mcp_tools_are_not_replayed(search_server):
    state = search_server
    state.tool._remote_name = "write_record"
    state.expired.add("1")
    result = await state.tool.execute(value="one")
    assert not result.success
    assert "Session terminated" in result.error
    assert state.initialized == 1
    assert len(state.calls) == 1


async def test_recovery_keeps_per_call_session_metadata(search_server):
    from box_agent.tools.model_tool_context import reset_model_tool_context, set_model_tool_context

    state = search_server
    state.expired.add("1")
    token = set_model_tool_context(model="test", max_output_tokens=100, session_id="agent-session-a")
    try:
        result = await state.tool.execute(query="metadata")
    finally:
        reset_model_tool_context(token)
    assert result.success, result.error
    assert len(state.calls) == 2
    for _, params, headers in state.calls:
        assert params["_meta"]["box_agent_session_id"] == "agent-session-a"
        assert headers["x-raccoon-session-id"] == "agent-session-a"


async def test_cancelled_search_releases_recovery_waiters(search_server):
    state = search_server
    state.expired.add("1")
    state.barrier = asyncio.Event()
    call = asyncio.create_task(state.tool.execute(query="cancel"))
    while not state.calls:
        await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    connection = mcp_loader._mcp_connections[0]
    assert connection._active_search_calls == 0
    assert connection._search_calls_drained.is_set()
    state.barrier.set()
    result = await asyncio.wait_for(state.tool.execute(query="after cancellation"), 5)
    assert result.success, result.error


async def test_real_http_server_session_deletion_recovers(search_server):
    import sys
    import textwrap

    state = search_server
    await mcp_loader.disconnect_mcp_server(state.tool.server_name)
    server = mcp_loader.ManagedHttpServerProcess(
        name="search-test", command=sys.executable, env=None,
        args=["-c", textwrap.dedent('''
            import sys
            from mcp.server.fastmcp import Context, FastMCP

            args = sys.argv[1:]
            mcp = FastMCP("search-test", host=args[args.index("--host") + 1],
                          port=int(args[args.index("--port") + 1]), log_level="WARNING")

            @mcp.tool()
            async def web_search(Query: str, ctx: Context) -> str:
                return ctx.request_context.request.headers["mcp-session-id"]

            mcp.run(transport="streamable-http")
        ''')],
    )
    await server.start(30)
    try:
        definition = ResolvedMcpServer(
            state.tool.server_name, {"url": server.url, "type": "streamable_http"},
            "system", "search", None, None, "test.json", "test",
        )
        mcp_loader._mcp_server_definitions[state.tool.server_name] = definition
        # The fixture replaces only HTTP client creation. Use the actual client
        # for this end-to-end test against a real SDK server on localhost.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mcp_loader, "_mcp_http_client_options", lambda url: {})
            connected = await mcp_loader.reconnect_mcp_server(state.tool.server_name)
            assert connected["success"], connected
            tool = mcp_loader.get_mcp_tools_for_server(state.tool.server_name)[0]
            first = await tool.execute(query="before")
            assert first.success, first.error
            session_id = first.content
            async with httpx.AsyncClient() as client:
                response = await client.delete(server.url, headers={"mcp-session-id": session_id})
                response.raise_for_status()
            recovered = await tool.execute(query="after")
            assert recovered.success, recovered.error
            assert recovered.content != session_id
            assert (await tool.execute(query="again")).content == recovered.content
    finally:
        await mcp_loader.disconnect_mcp_server(state.tool.server_name)
        await server.stop()


async def test_cancelled_recovery_cleans_up_and_reports_failed_status(search_server):
    state = search_server
    state.expired.add("1")
    state.stall_initialize = asyncio.Event()
    call = asyncio.create_task(state.tool.execute(query="cancel reconnect"))
    await asyncio.wait_for(state.stall_initialize.wait(), 2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not mcp_loader._mcp_connections
    assert mcp_loader._mcp_status[state.tool.server_name].state == "failed"
    assert not state.catalog.loading


async def test_revocation_while_recovery_waits_does_not_reconnect(search_server):
    state = search_server
    connection = mcp_loader._mcp_connections[0]
    connection._search_calls_drained.clear()
    lock = mcp_loader._mcp_reconnect_locks.setdefault(state.tool.server_name, asyncio.Lock())
    recovery = asyncio.create_task(mcp_loader.reconnect_mcp_server(
        state.tool.server_name, expected_session=connection.session,
    ))
    while not lock.locked():
        await asyncio.sleep(0)
    await mcp_loader.disconnect_mcp_server(state.tool.server_name)
    connection._search_calls_drained.set()
    result = await asyncio.wait_for(recovery, 2)
    assert not result["success"]
    assert state.initialized == 1
    assert not mcp_loader._mcp_connections


async def test_recovery_obeys_original_execution_timeout(search_server):
    state = search_server
    state.expired.add("1")
    state.stall_initialize = asyncio.Event()
    state.tool._execute_timeout = 0.05
    result = await asyncio.wait_for(state.tool.execute(query="slow reconnect"), 2)
    assert not result.success
    assert "timed out" in result.error
    assert not mcp_loader._mcp_connections
    assert mcp_loader._mcp_status[state.tool.server_name].state == "failed"
    assert not state.catalog.loading
