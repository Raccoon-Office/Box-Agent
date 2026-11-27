import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from box_agent.tools import mcp_loader
from box_agent.acp import _request_org_scope
from box_agent.auth import scoped_request_org
from box_agent.tools.model_tool_context import set_model_tool_context, reset_model_tool_context


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment", ["public", "private", "third-party"])
async def test_shared_mcp_sender_uses_message_org_including_explicit_personal(monkeypatch, deployment):
    private = deployment == "private"
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true" if private else "false")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    url = {"public": "https://xiaohuanxiong.com/mcp", "private": "https://tenant.example/office/mcp",
           "third-party": "https://other.example/mcp"}[deployment]
    seen, messages = [], []
    queue = asyncio.Queue()

    async def handler(request):
        seen.append(request.headers.get("X-Org-Code"))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
        headers={"X-Org-Code": "configured-team"},
        event_hooks={"request": [mcp_loader._attach_mcp_session_header]}) as client:
        async def sender():
            for _ in range(7):
                payload, done = await queue.get()
                await client.post(url, json=payload)
                done.set_result(SimpleNamespace(content=[], isError=False))

        class Session:
            async def call_tool(self, name, arguments, **kwargs):
                messages.append(kwargs.get("meta", {}))
                done = asyncio.get_running_loop().create_future()
                await queue.put(({"method": "tools/call",
                    "params": {"name": name, "arguments": arguments, "_meta": kwargs.get("meta")}}, done))
                return await done

        worker = asyncio.create_task(sender())  # Created before any payer is bound.
        tool = mcp_loader.MCPTool(name="lookup", server_name="hosted", description="",
            parameters={}, session=Session(), server_url=url)

        async def invoke(org):
            with scoped_request_org(org):
                assert (await tool.execute()).success

        try:
            await asyncio.wait_for(asyncio.gather(invoke("team-a"), invoke("team-b")), 3)
            for meta in ({"org": None}, {}, None):
                with _request_org_scope(meta):
                    assert (await asyncio.wait_for(tool.execute(), 3)).success
            await asyncio.wait_for(invoke("team-b"), 3)
            await asyncio.wait_for(invoke(None), 3)
            await worker
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    expected = ["team-a", "team-b", None, None, None, "team-b", "configured-team"]
    assert seen == (expected if deployment != "third-party" else [None] * 7)
    if deployment != "third-party":
        assert all(meta["box_agent_org"] == "" for meta in messages[2:5])
        assert messages[5]["box_agent_org"] == "team-b"
        assert "box_agent_org" not in messages[6]
    else:
        assert all("box_agent_org" not in meta for meta in messages)


@pytest.mark.asyncio
async def test_explicit_personal_mcp_clears_configured_org_but_legacy_calls_preserve_it():
    for meta, expected in [({}, "configured-team"), ({"box_agent_org": ""}, None)]:
        request = httpx.Request("POST", "https://xiaohuanxiong.com/mcp",
            json={"method": "tools/call", "params": {"name": "lookup", "_meta": meta}},
            headers={"X-Org-Code": "configured-team"})
        await mcp_loader._attach_mcp_session_header(request)
        assert request.headers.get("X-Org-Code") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("method,payload", [
    ("GET", None),
    ("DELETE", None),
    ("POST", {"method": "initialize"}),
    ("POST", {"method": "notifications/cancelled", "params": {"requestId": 1}}),
])
async def test_trusted_mcp_non_tool_requests_preserve_configured_org(monkeypatch, private, method, payload):
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", str(private).lower())
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    url = "https://tenant.example/office/mcp" if private else "https://xiaohuanxiong.com/mcp"
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)
    factory = mcp_loader._mcp_http_client_options(url)["httpx_client_factory"]
    async with factory(headers={"x-OrG-CoDe": "configured-team"}) as client:
        await client.request(method, url, json=payload)

    assert len(requests) == 1
    assert requests[0].headers["X-Org-Code"] == "configured-team"
    if payload is not None:
        assert json.loads(requests[0].content) == payload


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
@pytest.mark.parametrize("org", ["team-one", ""])
@pytest.mark.parametrize("status,destination,session_id,forwarded_header", [
    (302, "https://other.example/mcp", "session-a", None),
    (307, "https://other.example/mcp", "session-a", None),
    (308, "https://other.example/mcp", "session-a", None),
    (307, "/canonical-mcp", "session-a", "session-a"),
    (308, "https://pre.xiaohuanxiong.com/mcp", "session-a", "session-a"),
    (307, "https://other.example/mcp", None, None),
    (308, "https://other.example/mcp", None, None),
    (307, "https://xiaohuanxiong.com.evil.example/mcp", "session-a", None),
])
async def test_mcp_redirect_preserves_tool_arguments_and_scopes_billing_metadata(
    monkeypatch, status, destination, session_id, forwarded_header, org,
):
    requests = []

    async def handler(request):
        wire_body = b"".join([chunk async for chunk in request.stream])
        assert wire_body == request.content
        if request.method == "POST":
            assert int(request.headers["Content-Length"]) == len(wire_body)
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={"location": destination})
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)
    meta = {"progressToken": "search", "box_agent_org": org}
    if session_id is not None:
        meta["box_agent_session_id"] = session_id
    payload = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "search", "arguments": {"query": "测试", "org": "tool-argument"},
                          "_meta": meta}}

    async with mcp_loader._session_mcp_http_client() as client:
        response = await client.post("https://xiaohuanxiong.com/mcp", json=payload)

    assert response.status_code == 200
    assert len(requests) == 2
    assert requests[0].headers.get("X-RACCOON-Session-ID") == session_id
    assert requests[0].headers.get("X-Org-Code") == (org or None)
    assert json.loads(requests[0].content) == payload
    forwarded = requests[1]
    assert forwarded.url == requests[0].url.join(destination)
    assert forwarded.headers.get("X-RACCOON-Session-ID") == forwarded_header
    trusted = forwarded.url.host in {"xiaohuanxiong.com", "pre.xiaohuanxiong.com"}
    assert forwarded.headers.get("X-Org-Code") == (
        (org or None) if status != 302 and trusted else None)
    if status == 302:
        assert forwarded.method == "GET"
        assert not forwarded.content
    else:
        assert forwarded.method == "POST"
        if not trusted:
            del meta["box_agent_org"]
        assert json.loads(forwarded.content) == payload


@pytest.mark.asyncio
async def test_untrusted_mcp_removes_internal_org_from_streamed_body_without_changing_tool_input():
    payload = {"method": "tools/call", "params": {
        "name": "lookup", "arguments": {"org": "tool-input"},
        "_meta": {"box_agent_org": "team-one", "progressToken": "keep"},
    }}

    async def body():
        yield json.dumps(payload).encode()

    request = httpx.Request("POST", "https://other.example/mcp", content=body(),
                            headers={"Content-Type": "application/json", "X-Org-Code": "team-one"})
    assert request.headers["Transfer-Encoding"] == "chunked"
    await mcp_loader._attach_mcp_session_header(request)

    wire_body = b"".join([chunk async for chunk in request.stream])
    assert wire_body == request.content
    assert int(request.headers["Content-Length"]) == len(wire_body)
    assert "Transfer-Encoding" not in request.headers
    assert "X-Org-Code" not in request.headers
    del payload["params"]["_meta"]["box_agent_org"]
    assert json.loads(wire_body) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
@pytest.mark.parametrize("private", [False, True])
async def test_trusted_mcp_transport_installs_org_header_hook(monkeypatch, transport, private):
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true" if private else "false")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    captured = {}

    @asynccontextmanager
    async def connect(url, headers=None, timeout=None, sse_read_timeout=None, auth=None,
                      httpx_client_factory=None):
        async with httpx_client_factory() as client:
            captured["hooks"] = client.event_hooks["request"]
        yield (None, None, None) if transport == "streamable_http" else (None, None)

    monkeypatch.setattr(mcp_loader, "sse_client" if transport == "sse" else "streamable_http_client", connect)
    connection = mcp_loader.MCPServerConnection(
        name="trusted", connection_type=transport,
        url="https://tenant.example/office/mcp" if private else "https://pre.xiaohuanxiong.com/mcp",
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
