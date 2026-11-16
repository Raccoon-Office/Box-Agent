import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from box_agent.tools import mcp_loader
from box_agent.tools.model_tool_context import set_model_tool_context, reset_model_tool_context


@pytest.mark.asyncio
@pytest.mark.parametrize("host,trusted", [
    ("xiaohuanxiong.com", True),
    ("pre.xiaohuanxiong.com", True),
    ("code-test.xiaohuanxiong.com", True),
    ("xiaohuanxiong.com.evil.com", False),
    ("evilxiaohuanxiong.com", False),
    ("10.158.136.99", False),
    ("third-party.example", False),
])
@pytest.mark.parametrize("server_name,tool_name", [
    ("ordinary-server", "any_tool"),
    ("box-agent-web-extract", "web_extract"),
])
async def test_shared_mcp_connection_sends_only_trusted_per_call_session_headers(
    host, trusted, server_name, tool_name,
):
    seen = []
    url = f"https://{host}/mcp"

    async def handler(request):
        seen.append(request.headers.get("X-RACCOON-Session-ID"))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [mcp_loader._attach_mcp_session_header]},
    ) as client:
        queue = asyncio.Queue()

        async def sender():
            for _ in range(3):
                payload, done = await queue.get()
                await client.post(url, json=payload)
                done.set_result(SimpleNamespace(content=[], isError=False))

        class Session:
            async def call_tool(self, name, arguments, **kwargs):
                assert ("meta" in kwargs) == (trusted and arguments["id"] != "")
                done = asyncio.get_running_loop().create_future()
                await queue.put(({
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments, "_meta": kwargs.get("meta")},
                }, done))
                return await done

        # Start the sender outside the callers' contexts, like the MCP SDK.
        worker = asyncio.create_task(sender())
        tool = mcp_loader.MCPTool(
            name=tool_name, server_name=server_name, description="", parameters={},
            session=Session(), server_url=url,
        )

        async def invoke(session_id):
            token = set_model_tool_context(model="m", max_output_tokens=100, session_id=session_id)
            try:
                assert (await tool.execute(id=session_id)).success
            finally:
                reset_model_tool_context(token)

        await asyncio.gather(invoke("session-a"), invoke("session-b"))
        await invoke("")
        await worker
    assert seen == (["session-a", "session-b", None] if trusted else [None, None, None])


@pytest.mark.asyncio
@pytest.mark.parametrize("status,destination,session_id,forwarded_header", [
    (302, "https://other.example/mcp", "session-a", None),
    (307, "https://other.example/mcp", "session-a", None),
    (308, "https://other.example/mcp", "session-a", None),
    (307, "/canonical-mcp", "session-a", "session-a"),
    (308, "https://pre.xiaohuanxiong.com/mcp", "session-a", "session-a"),
    (307, "https://other.example/mcp", None, None),
    (308, "https://other.example/mcp", None, None),
])
async def test_mcp_redirect_preserves_post_body_and_scopes_session_header(
    monkeypatch, status, destination, session_id, forwarded_header,
):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={"location": destination})
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)
    meta = {"progressToken": "search"}
    if session_id is not None:
        meta["box_agent_session_id"] = session_id
    payload = {"method": "tools/call", "params": {"name": "search", "_meta": meta}}

    async with mcp_loader._session_mcp_http_client() as client:
        response = await client.post("https://xiaohuanxiong.com/mcp", json=payload)

    assert response.status_code == 200
    assert len(requests) == 2
    assert requests[0].headers.get("X-RACCOON-Session-ID") == session_id
    forwarded = requests[1]
    assert forwarded.url == requests[0].url.join(destination)
    assert forwarded.headers.get("X-RACCOON-Session-ID") == forwarded_header
    if status == 302:
        assert forwarded.method == "GET"
        assert not forwarded.content
    else:
        assert forwarded.method == "POST"
        assert json.loads(forwarded.content) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
async def test_trusted_mcp_transport_installs_session_header_hook(monkeypatch, transport):
    captured = {}

    @asynccontextmanager
    async def connect(url, headers=None, timeout=None, sse_read_timeout=None, auth=None,
                      httpx_client_factory=None):
        async with httpx_client_factory() as client:
            captured["hooks"] = client.event_hooks["request"]
        yield (None, None, None) if transport == "streamable_http" else (None, None)

    monkeypatch.setattr(mcp_loader, "sse_client" if transport == "sse" else "streamable_http_client", connect)
    connection = mcp_loader.MCPServerConnection(
        name="trusted", connection_type=transport, url="https://pre.xiaohuanxiong.com/mcp",
    )
    async with AsyncExitStack() as stack:
        connection.exit_stack = stack
        await getattr(connection, "_connect_" + transport)()
    assert mcp_loader._attach_mcp_session_header in captured["hooks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {}, [], {"method": "initialize"},
    {"method": "tools/call", "params": {"_meta": {"box_agent_session_id": 42}}},
    {"method": "tools/call", "params": {"_meta": {"box_agent_session_id": "bad\r\nheader"}}},
])
async def test_unbound_or_invalid_messages_do_not_reuse_session_header(payload):
    request = httpx.Request("POST", "https://xiaohuanxiong.com/mcp", json=payload,
                            headers={"X-RACCOON-Session-ID": "stale"})
    await mcp_loader._attach_mcp_session_header(request)
    assert "X-RACCOON-Session-ID" not in request.headers
