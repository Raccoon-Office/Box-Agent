"""Real ACP frames -> provider SDK HTTP requests, without external billing."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
from acp import RequestError
from acp.agent.connection import AgentSideConnection
from acp.connection import Connection

from box_agent.acp import BoxACPAgent
from box_agent.acp.stdio_compat import StdioTransport
from box_agent.auth import current_request_org
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.llm import LLMClient
from box_agent.retry import RetryConfig
from box_agent.schema import LLMProvider, Message


def completion(request, provider):
    body = json.loads(request.content)
    content = "<summary>ok</summary>" if request.headers.get("X-RACCOON-Call-Kind") == "context_summary" else "ok"
    if provider == LLMProvider.ANTHROPIC:
        message = {"id": "msg_test", "type": "message", "role": "assistant",
                   "model": "test", "content": [], "stop_reason": None,
                   "usage": {"input_tokens": 2, "output_tokens": 1}}
        if not body.get("stream"):
            return httpx.Response(200, json={**message, "stop_reason": "end_turn",
                "content": [{"type": "text", "text": content}]})
        events = [
            {"type": "message_start", "message": message},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": content}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        text = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    else:
        choice = {"index": 0, "finish_reason": "stop"}
        reply = {"id": "test", "created": 1, "model": "test", "choices": [choice]}
        if not body.get("stream"):
            choice["message"] = {"role": "assistant", "content": content}
            return httpx.Response(200, json={**reply, "object": "chat.completion"})
        choice["delta"] = {"content": content}
        text = f"data: {json.dumps({**reply, 'object': 'chat.completion.chunk'})}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)


@asynccontextmanager
async def wire_agent(tmp_path, provider=LLMProvider.OPENAI, respond=None,
                     api_base="https://code-test.xiaohuanxiong.com/v1"):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"access_token": "login-token", "office_identity": "legacy-team"}))
    requests = []

    async def handle(request):
        requests.append(request)
        return await respond(request) if respond else completion(request, provider)

    llm = LLMClient(api_key="box-agent-auth-json", provider=provider,
        api_base=api_base, model="test",
        auth_file=str(auth_file), retry_config=RetryConfig(enabled=False))
    await llm._client.client.close()
    llm._client.client = llm._client.client.with_options(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), max_retries=0)
    config = Config(
        llm=LLMConfig(api_key="box-agent-auth-json", auth_file=str(auth_file)),
        agent=AgentConfig(workspace_dir=str(tmp_path), max_steps=2,
            enable_memory=False, enable_memory_extraction=False, goal_autopilot_enabled=False),
        tools=ToolsConfig(enable_file_tools=False, enable_bash=False, enable_mcp=False,
            enable_sub_agent=False, enable_skills=False, enable_plan=False, enable_todo=False),
    )
    host = BoxACPAgent(None, config, llm, [], "system")
    local, remote = socket.socketpair()
    client_reader, client_writer = await asyncio.open_connection(sock=local)
    agent_reader, agent_writer = await asyncio.open_connection(sock=remote)
    server = AgentSideConnection(host, StdioTransport(agent_reader, agent_writer), use_unstable_protocol=True)
    host._conn = server

    async def notification(method, params, is_notification):
        if method == "session/request_permission":
            return {"outcome": {"outcome": "selected", "optionId": "approve"}}

    client = Connection(notification, StdioTransport(client_reader, client_writer))

    async def rpc(method, params, *, notify=False):
        if notify:
            return await client.send_notification(method, params)
        return await asyncio.wait_for(client.send_request(method, params), 10)

    try:
        await rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        yield rpc, host, requests, auth_file
    finally:
        await host.aclose()
        await client.close()
        await server.close()
        client_writer.close()
        agent_writer.close()
        await asyncio.gather(client_writer.wait_closed(), agent_writer.wait_closed())
        await llm.aclose()


async def new(rpc, workspace, **meta):
    reply = await rpc("session/new", {"cwd": str(workspace), "mcpServers": [], "_meta": meta})
    return reply["sessionId"]


async def prompt(rpc, session_id, **meta):
    return await rpc("session/prompt", {"sessionId": session_id,
        "prompt": [{"type": "text", "text": "hello"}], "_meta": meta})


@pytest.mark.asyncio
async def test_private_acp_uses_request_org_and_preserves_login_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")

    async with wire_agent(tmp_path,
                          api_base="https://tenant.example/office/v1") as (rpc, _, requests, _):
        with pytest.raises(RequestError) as error:
            await new(rpc, tmp_path, org={"invalid": "org"})
        assert error.value.code == -32602
        assert error.value.data["code"] == "INVALID_ORG"
        assert not requests
        session = await new(rpc, tmp_path, org="creation-only")
        for meta in ({"org": "request-team"}, {"org": None}, {}, None, {"org": "next-team"}):
            start = len(requests)
            params = {"sessionId": session, "prompt": [{"type": "text", "text": "hello"}]}
            utility = {"prompt": "write a title"}
            if meta is not None:
                params["_meta"] = utility["_meta"] = meta
            assert (await rpc("session/prompt", params))["stopReason"] == "end_turn"
            assert (await rpc("_llm/prompt", utility))["text"] == "ok"
            expected = meta.get("org") if meta is not None else None
            assert requests[start:]
            assert all(r.headers.get("X-Org-Code") == expected for r in requests[start:])
        start = len(requests)
        with pytest.raises(RequestError):
            await prompt(rpc, session, org=123)
        assert len(requests) == start
        assert (await prompt(rpc, session, org="after-rejection"))["stopReason"] == "end_turn"
        assert all(r.headers.get("X-Org-Code") == "after-rejection" for r in requests[start:])
        assert all(r.headers["Authorization"] == "Bearer login-token" for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(LLMProvider))
async def test_existing_session_uses_request_org_and_defaults_to_personal(tmp_path, provider):
    async with wire_agent(tmp_path, provider) as (rpc, host, requests, auth_file):
        before = auth_file.read_bytes()
        session = await new(rpc, tmp_path, org="creation-only")
        for meta, expected in [({"org": "team-a"}, "team-a"), ({"org": "team-b"}, "team-b"),
                               ({"org": None}, None), ({}, None), (None, None)]:
            start = len(requests)
            params = {"sessionId": session, "prompt": [{"type": "text", "text": "hello"}]}
            if meta is not None:
                params["_meta"] = meta
            reply = await rpc("session/prompt", params)
            assert reply["stopReason"] == "end_turn"
            assert requests[start:]
            assert all(r.headers.get("X-Org-Code") == expected for r in requests[start:])
            assert all(r.headers["Authorization"] == "Bearer login-token" for r in requests[start:])
        assert auth_file.read_bytes() == before
        assert len(host._sessions) == 1
        assert current_request_org() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(LLMProvider))
@pytest.mark.parametrize("meta", [{"org": "utility-team"}, {"org": None}, {}, None])
async def test_independent_utility_request_uses_org_on_nonstreaming_wire(tmp_path, provider, meta):
    async with wire_agent(tmp_path, provider) as (rpc, _, requests, _):
        params = {"prompt": "write a title", "purpose": "title_generate"}
        if meta is not None:
            params["_meta"] = meta
        reply = await rpc("_llm/prompt", params)
        assert reply["text"] == "ok"
        org = meta.get("org") if meta is not None else None
        assert requests and all(r.headers.get("X-Org-Code") == org for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["session/load", "session/resume"])
async def test_restored_session_uses_next_prompt_org_without_storing_restore_org(tmp_path, method):
    async with wire_agent(tmp_path) as (rpc, _, requests, _):
        session = await new(rpc, tmp_path, session_id="restored", org="team-a")
        await prompt(rpc, session, org="team-a")
        count = len(requests)
        await rpc(method, {"sessionId": session, "cwd": str(tmp_path), "mcpServers": [],
                           "_meta": {"org": "team-b"}})
        assert len(requests) == count
        await prompt(rpc, session, org=None)
        assert all("X-Org-Code" not in r.headers for r in requests[count:])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [False, 123, {}, [], "", "  ", "team\r\ninjected", "团队"])
async def test_invalid_org_rejects_only_its_request_before_side_effects(tmp_path, bad):
    async with wire_agent(tmp_path) as (rpc, host, requests, _):
        missing = tmp_path / "must-not-be-created"
        with pytest.raises(RequestError) as error:
            await new(rpc, missing, org=bad)
        assert error.value.code == -32602
        assert error.value.data["code"] == "INVALID_ORG"
        assert not missing.exists() and not requests
        first = await new(rpc, tmp_path, session_id="first")
        peer = await new(rpc, tmp_path, session_id="peer")
        log = host._sessions[first].agent.session_log
        before = log.path.read_bytes()
        with pytest.raises(RequestError):
            await prompt(rpc, first, org=bad, turn_id="rejected")
        assert log.path.read_bytes() == before
        assert not requests
        assert (await prompt(rpc, peer, org=None))["stopReason"] == "end_turn"
        assert all("X-Org-Code" not in r.headers for r in requests)


@pytest.mark.asyncio
async def test_stalled_org_request_does_not_change_concurrent_personal_request(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        if request.headers.get("X-Org-Code") == "slow-team":
            entered.set()
            await release.wait()
        return completion(request, LLMProvider.OPENAI)

    async with wire_agent(tmp_path, respond=respond) as (rpc, _, requests, _):
        first = await new(rpc, tmp_path, session_id="slow")
        peer = await new(rpc, tmp_path, session_id="peer")
        slow = asyncio.create_task(prompt(rpc, first, org="slow-team"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert (await prompt(rpc, peer, org=None))["stopReason"] == "end_turn"
            assert not slow.done()
        finally:
            release.set()
            await slow
        assert {r.headers.get("X-Org-Code") for r in requests
                if r.headers.get("X-RACCOON-Session-ID") == "slow"} == {"slow-team"}
        assert {r.headers.get("X-Org-Code") for r in requests
                if r.headers.get("X-RACCOON-Session-ID") == "peer"} == {None}


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [{"org": "all-calls"}, {"org": None}, {}])
async def test_request_org_covers_preparation_summary_and_inherited_child_tasks(tmp_path, monkeypatch, meta):
    from box_agent.kernel.context_engine import _create_summary

    async with wire_agent(tmp_path) as (rpc, host, requests, _):
        original = host._prompt_for_session

        async def prepare(params, session_id, state, *args):
            messages = [Message(role="user", content="history")]
            await _create_summary(state.summary_llm, messages, 1)
            child = state.session_llm.for_model("child-model")
            await asyncio.create_task(child.generate(messages))
            return await original(params, session_id, state, *args)

        monkeypatch.setattr(host, "_prompt_for_session", prepare)
        session = await new(rpc, tmp_path)
        await prompt(rpc, session, **meta)
        assert len(requests) >= 3
        assert all(r.headers.get("X-Org-Code") == meta.get("org") for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [{"org": "creation-team"}, {"org": None}, {}])
async def test_implicit_session_creation_keeps_prompt_billing_identity(tmp_path, monkeypatch, meta):
    async with wire_agent(tmp_path) as (rpc, host, requests, _):
        original = host._new_session_with_binding

        async def create(params, **kwargs):
            await host._llm.generate([Message(role="user", content="prepare")])
            return await original(params, **kwargs)

        monkeypatch.setattr(host, "_new_session_with_binding", create)
        assert (await prompt(rpc, "missing-session", **meta))["stopReason"] == "end_turn"
        assert len(requests) >= 2
        assert all(r.headers.get("X-Org-Code") == meta.get("org") for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [{"org": "retry-team"}, {"org": None}, {}])
async def test_hosted_auth_retry_keeps_request_billing_identity(tmp_path, monkeypatch, meta):
    rejected = False

    async def respond(request):
        nonlocal rejected
        if not rejected:
            rejected = True
            return httpx.Response(401, json={"error": {"message": "unauthorized"}})
        return completion(request, LLMProvider.OPENAI)

    async with wire_agent(tmp_path, respond=respond) as (rpc, _, requests, auth_file):
        async def refresh(*args, **kwargs):
            auth_file.write_text(json.dumps({"access_token": "refreshed-token", "office_identity": "other-default"}))
            return "refreshed-token"
        monkeypatch.setattr("box_agent.llm.base.refresh_hosted_auth_token_if_needed", refresh)
        result = await rpc("_llm/prompt", {"prompt": "hello", "_meta": meta})
        assert result["text"] == "ok"
        org = meta.get("org")
        assert [r.headers.get("X-Org-Code") for r in requests] == [org, org]
        assert [r.headers["Authorization"] for r in requests] == ["Bearer login-token", "Bearer refreshed-token"]


@pytest.mark.asyncio
async def test_organization_rejection_never_retries_as_personal(tmp_path):
    async def reject(request):
        return httpx.Response(403, json={"error": {"message": "organization quota denied"}})

    async with wire_agent(tmp_path, respond=reject) as (rpc, _, requests, _):
        result = await rpc("_llm/prompt", {"prompt": "hello", "_meta": {"org": "denied-team"}})
        assert "error" in result
        assert requests and all(r.headers.get("X-Org-Code") == "denied-team" for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("cancel_meta", [None, {}, {"org": None}, {"org": ""}, {"org": 123}])
async def test_cancellation_ignores_org_and_next_request_chooses_its_identity(
    tmp_path, monkeypatch, cancel_meta, private,
):
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", str(private).lower())
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    api_base = "https://tenant.example/office/v1" if private else "https://code-test.xiaohuanxiong.com/v1"
    entered = asyncio.Event()

    async def respond(request):
        if request.headers.get("X-Org-Code") == "cancelled-team":
            entered.set()
            await asyncio.Future()
        return completion(request, LLMProvider.OPENAI)

    async with wire_agent(tmp_path, respond=respond, api_base=api_base) as (rpc, _, requests, _):
        failed = await rpc("_llm/prompt", {"prompt": "", "_meta": {"org": "failed-team"}})
        assert failed["error"]["code"] == "invalid_args"
        session = await new(rpc, tmp_path)
        task = asyncio.create_task(prompt(rpc, session, org="cancelled-team"))
        await asyncio.wait_for(entered.wait(), 3)
        cancel_params = {"sessionId": session}
        if cancel_meta is not None:
            cancel_params["_meta"] = cancel_meta
        await rpc("session/cancel", cancel_params, notify=True)
        assert (await task)["stopReason"] == "cancelled"
        start = len(requests)
        assert (await prompt(rpc, session, org="next-team"))["stopReason"] == "end_turn"
        assert all(r.headers.get("X-Org-Code") == "next-team" for r in requests[start:])
        start = len(requests)
        assert (await prompt(rpc, session))["stopReason"] == "end_turn"
        assert all("X-Org-Code" not in r.headers for r in requests[start:])
        await rpc("_llm/prompt", {"prompt": "personal"})
        assert "X-Org-Code" not in requests[-1].headers
        assert current_request_org() is None
