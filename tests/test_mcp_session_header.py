import asyncio
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
async def test_redirect_to_third_party_removes_session_header():
    seen = []

    def handler(request):
        seen.append(request.headers.get("X-RACCOON-Session-ID"))
        if request.url.host == "xiaohuanxiong.com":
            return httpx.Response(302, headers={"location": "https://other.example/mcp"})
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True,
        event_hooks={"request": [mcp_loader._attach_mcp_session_header]},
    ) as client:
        await client.post("https://xiaohuanxiong.com/mcp", json={
            "method": "tools/call", "params": {"_meta": {"box_agent_session_id": "session-a"}},
        })
    assert seen == ["session-a", None]


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
