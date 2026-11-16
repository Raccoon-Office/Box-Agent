"""Host protocol -> real SDK JSON, including disabled and auxiliary requests."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from acp.agent.connection import AgentSideConnection

from box_agent.acp import BoxACPAgent
from box_agent.acp.stdio_compat import StdioTransport
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.llm import SessionBoundLLM
from box_agent.llm.reasoning import RACCOON_THINKING_LEVELS
from box_agent.kernel.context_engine import _create_summary
from box_agent.schema import Message
from tests.test_reasoning_disabled_policy import wire_client
from tests.test_anthropic_image_request_preflight import _client as anthropic_wire

CAPABILITY = {"supported": True, "can_disable": True, "default_level": "off",
              "display_levels": [{"value": "level_1", "label": "Minimal"},
                                 {"value": "level_3", "label": "High"},
                                 {"value": "level_5", "label": "Max"}]}


@asynccontextmanager
async def protocol(tmp_path, llm):
    reader, writes = asyncio.StreamReader(), asyncio.Queue()
    class Writer:
        def write(self, data): writes.put_nowait(json.loads(data))
        async def drain(self): pass
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path)), tools=ToolsConfig())
    adapter = None
    def create(conn):
        nonlocal adapter
        adapter = BoxACPAgent(conn, config, llm, [], "system")
        return adapter
    conn = AgentSideConnection(create, StdioTransport(reader, Writer()))
    seq = 0
    async def request(meta, cwd=None, method="session/new", session_id=None):
        nonlocal seq
        seq += 1
        reader.feed_data((json.dumps({"jsonrpc": "2.0", "id": seq, "method": method,
            "params": ({"protocolVersion": 1, "clientCapabilities": {}} if method == "initialize" else
                       ({"sessionId": session_id, "prompt": [{"type": "text", "text": "hello"}]} if method == "session/prompt" else
                        {"cwd": str(cwd or tmp_path), "mcpServers": [], "_meta": meta}))}) + "\n").encode())
        while True:
            reply = await asyncio.wait_for(writes.get(), 10)
            if reply.get("id") == seq: return reply
    try:
        yield request, lambda reply: adapter._sessions[reply["result"]["sessionId"]]
    finally:
        await adapter.aclose()
        reader.feed_eof()
        await conn.close()


@pytest.mark.parametrize("meta", [
    {"reasoning_effort": bad} for bad in ("ultra", "", None, 1, True, [], {})
] + [{"raccoon_thinking_level": bad} for bad in ("max", "", None, 1, True, [], {})]
  + [{"reasoning_effort": "max", "raccoon_thinking_level": "level_5"}])
async def test_invalid_protocol_params_have_no_workspace_side_effect(tmp_path, meta):
    async with wire_client() as (base, calls), protocol(tmp_path, base) as (request, _):
        cwd = tmp_path / "must-not-exist"
        reply = await request(meta, cwd)
        assert reply["error"]["code"] == -32602
        assert not cwd.exists()
        assert calls == []


@pytest.mark.parametrize("level", [None, *RACCOON_THINKING_LEVELS])
@pytest.mark.parametrize("stream", [False, True])
async def test_unified_acp_to_top_level_wire(tmp_path, level, stream):
    async with wire_client(model="opaque-business-model-p") as (base, calls):
        base._client.raccoon_thinking_models = {base.model: CAPABILITY}
        async with protocol(tmp_path, base) as (request, state_for):
            reply = await request({} if level is None else {"raccoon_thinking_level": level})
            state = state_for(reply)
            messages = [Message(role="user", content="hello")]
            if stream:
                _ = [e async for e in state.session_llm.generate_stream(messages, thinking_enabled=state.thinking_enabled)]
            else:
                await state.session_llm.generate(messages, thinking_enabled=state.thinking_enabled)
            body = calls[-1]
            assert body["model"] == "opaque-business-model-p"
            assert body.get("raccoon_thinking_level") == level
            assert not ({"reasoning_effort", "thinking", "enable_thinking", "extra_body", "budget_tokens"} & body.keys())
            if level is None: assert "raccoon_thinking_level" not in body
            assert not hasattr(base._client, "raccoon_thinking_level")


@pytest.mark.parametrize("model,unified,expected", [
    ("gpt-4.1", False, None), ("gpt-6-astra", False, None),
    ("sn-sensenova-6-8-flash-lite", False, "none"),
    ("sn-glm-5-2", False, "low"), ("hosted-opaque", True, None),
])
async def test_acp_utility_and_real_context_summary_keep_minimum_policy(tmp_path, monkeypatch, model, unified, expected):
    monkeypatch.setenv("BOX_AGENT_SENSENOVA_MODEL_PREFIXES", "sn-glm-")
    def summary_response(payload):
        body = {"id": "summary", "created": 1, "model": model, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "finish_reason": "stop", "delta": {"content": "<summary>done</summary>"}}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n")
    async with wire_client(model=model, policy="low" if "glm" in model else None, respond=summary_response) as (base, calls):
        if unified: base._client.raccoon_thinking_models = {model: CAPABILITY}
        choice = {"raccoon_thinking_level": "level_5"} if unified else {"reasoning_effort": "max"}
        async with protocol(tmp_path, base) as (request, state_for):
            state = state_for(await request({**choice, "utility": True}))
            assert state.thinking_enabled is False
            _ = [e async for e in state.session_llm.generate_stream([Message(role="user", content="title")], thinking_enabled=state.thinking_enabled)]
            assert calls[-1].get("reasoning_effort") == expected
            assert "raccoon_thinking_level" not in calls[-1]
            main = state_for(await request(choice))
            assert await _create_summary(main.session_llm, [Message(role="user", content="history")], 1) == "done"
            assert calls[-1].get("reasoning_effort") == expected
            assert "raccoon_thinking_level" not in calls[-1]


@pytest.mark.parametrize("effort", [None, "medium", "high", "max"])
async def test_nonreasoning_native_default_and_explicit_preferences_are_omitted(tmp_path, effort):
    async with wire_client(model="gpt-4.1") as (base, calls), protocol(tmp_path, base) as (request, state_for):
        state = state_for(await request({} if effort is None else {"reasoning_effort": effort}))
        await state.session_llm.generate([Message(role="user", content="hello")], thinking_enabled=state.thinking_enabled)
        assert "reasoning_effort" not in calls[-1]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model", ["claude-opus-4-7", "claude-sonnet-4"])
@pytest.mark.parametrize("effort", [None, "fast", "medium", "high", "xhigh", "max"])
async def test_anthropic_model_specific_wire(model, effort, stream):
    captured = []
    client = await anthropic_wire(100_000, captured)
    client.model, client.reasoning_effort = model, effort
    client.max_output_tokens = 8192
    messages = [Message(role="user", content="hello")]
    try:
        if stream:
            _ = [event async for event in client.generate_stream(messages, thinking_enabled=effort is not None)]
        else:
            await client.generate(messages, thinking_enabled=effort is not None)
        body = captured[-1][1]
        if effort in (None, "fast"):
            assert "thinking" not in body
            assert "output_config" not in body
        elif model == "claude-opus-4-7":
            assert body["thinking"] == {"type": "adaptive"}
            assert body["output_config"] == {"effort": effort}
        else:
            assert body["thinking"]["type"] == "enabled"
            assert 1024 <= body["thinking"]["budget_tokens"] <= 6144
    finally:
        await client.client.close()

async def test_new_field_cannot_enable_unified_protocol_on_a_native_profile(tmp_path):
    async with wire_client(model="gpt-4.1") as (base, calls), protocol(tmp_path, base) as (request, _):
        cwd = tmp_path / "native-no-workspace"
        reply = await request({"raccoon_thinking_level": "level_5"}, cwd)
        assert reply["error"]["code"] == -32602
        assert not cwd.exists()
        assert calls == []


@pytest.mark.parametrize("auth_file", ["", "auth.json"])
async def test_only_native_host_profile_metadata_enables_unified_route(tmp_path, monkeypatch, auth_file):
    from box_agent.llm.model_profiles import client_for_model_profile
    registry = tmp_path / "profiles.json"
    registry.write_text(json.dumps({"version": 1, "profiles": {"revision": {
        "profileId": "profile", "profileRevision": "revision", "provider": "openai",
        "apiBase": "https://inference.example/v1", "apiKey": "test", "authFile": auth_file,
        "defaultModel": "opaque-p", "raccoonThinkingModels": {"opaque-p": CAPABILITY},
    }}}))
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))
    async with wire_client(model="opaque-p") as (fallback, calls):
        client = client_for_model_profile({"profileId": "profile", "profileRevision": "revision", "model": "opaque-p"}, fallback_client=fallback)
        try:
            bound = SessionBoundLLM(client, raccoon_thinking_level="level_4")
            body = bound._delegate._client._request_body_params([], None, max_tokens=8192, thinking_enabled=True, stream=False)
            if auth_file:
                assert body["extra_body"] == {"raccoon_thinking_level": "level_4"}
                assert "reasoning_effort" not in body
                assert bound.for_model("opaque-p")._delegate._client.raccoon_thinking_level == "level_4"
            else:
                assert "extra_body" not in body
            assert not hasattr(client._client, "raccoon_thinking_level")
        finally:
            await client.aclose()


@pytest.mark.parametrize("capability,expected", [
    ({**CAPABILITY, "can_disable": False, "default_level": "level_1"}, "level_1"),
    ({"supported": False, "can_disable": True, "default_level": "off", "display_levels": []}, None),
])
async def test_hosted_off_is_derived_without_mutating_session_preference(tmp_path, capability, expected):
    async with wire_client(model="opaque") as (base, calls), protocol(tmp_path, base) as (request, state_for):
        base._client.raccoon_thinking_models = {"opaque": capability}
        state = state_for(await request({"raccoon_thinking_level": "off"}))
        await state.session_llm.generate([Message(role="user", content="hello")], thinking_enabled=state.thinking_enabled)
        assert calls[-1].get("raccoon_thinking_level") == expected
        assert state.raccoon_thinking_level == "off"

async def test_native_main_fast_and_auxiliary_false_are_distinct(tmp_path):
    async with wire_client(model="gpt-6-astra") as (base, calls), protocol(tmp_path, base) as (request, state_for):
        state = state_for(await request({"reasoning_effort": "fast"}))
        messages = [Message(role="user", content="hello")]
        await state.session_llm.generate(messages, thinking_enabled=state.thinking_enabled)
        assert calls[-1]["reasoning_effort"] == "low"
        await state.session_llm.generate(messages, thinking_enabled=False)
        assert "reasoning_effort" not in calls[-1]


async def test_unified_400_is_not_retried_at_another_effort():
    from openai import BadRequestError
    def reject(_):
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "code": "params_unsupport_error", "param": None, "message": "unsupported"}})
    async with wire_client(model="opaque", respond=reject) as (base, calls):
        base._client.raccoon_thinking_models = {"opaque": CAPABILITY}
        session = SessionBoundLLM(base, raccoon_thinking_level="level_5")
        with pytest.raises(BadRequestError):
            await session.generate([Message(role="user", content="hello")], thinking_enabled=True)
        assert len(calls) == 1
        assert calls[0]["raccoon_thinking_level"] == "level_5"


@pytest.mark.parametrize("parent_cap,preference,parent_expected,child_expected", [
    ({**CAPABILITY, "can_disable": False, "default_level": "level_1"}, "off", "level_1", "off"),
    ({"supported": False, "can_disable": True, "default_level": "off", "display_levels": []},
     "level_5", None, "level_5"),
])
async def test_child_and_rebinding_derive_from_original_preference(
    tmp_path, parent_cap, preference, parent_expected, child_expected,
):
    async with wire_client(model="parent") as (base, calls), protocol(tmp_path, base) as (request, state_for):
        base._client.raccoon_thinking_models = {"parent": parent_cap, "child": CAPABILITY}
        state = state_for(await request({"raccoon_thinking_level": preference}))
        messages = [Message(role="user", content="hello")]
        await state.session_llm.generate(messages, thinking_enabled=state.thinking_enabled)
        assert calls[-1].get("raccoon_thinking_level") == parent_expected
        child = state.session_llm.for_model("child")
        await child.generate(messages, thinking_enabled=state.thinking_enabled)
        assert calls[-1]["raccoon_thinking_level"] == child_expected
        state.session_llm.bind(base.for_model("child"))
        await state.session_llm.generate(messages, thinking_enabled=state.thinking_enabled)
        assert calls[-1]["raccoon_thinking_level"] == child_expected
        assert state.raccoon_thinking_level == preference


async def test_initialize_advertises_unified_thinking_protocol(tmp_path):
    async with wire_client() as (base, _), protocol(tmp_path, base) as (request, _):
        reply = await request({}, method="initialize")
        assert reply["result"]["_meta"]["raccoon_thinking"] == {"protocol_version": 1}


@pytest.mark.parametrize("level", [None, "level_5"])
async def test_standard_execution_native_default_and_unified_wire(tmp_path, level):
    async with wire_client(model="gpt-5") as (base, calls), protocol(tmp_path, base) as (request, state_for):
        if level:
            base._client.raccoon_thinking_models = {base.model: CAPABILITY}
        meta = {"execution_profile": "standard"}
        meta.update({"raccoon_thinking_level": level} if level else {"reasoning_effort": "medium"})
        state = state_for(await request(meta))
        await state.session_llm.generate([Message(role="user", content="test")], thinking_enabled=state.thinking_enabled)
        assert state.execution_profile == "standard"
        body = calls[-1]
        assert body.get("reasoning_effort") == (None if level else "medium")
        assert body.get("raccoon_thinking_level") == level
        assert "extra_body" not in body


async def test_http_capability_conflict_reaches_acp_with_structured_code_without_retry(tmp_path):
    def reject(_):
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "code": "params_unsupport_error", "param": None, "message": "opaque failure"}})
    async with wire_client(model="opaque", respond=reject) as (base, calls), protocol(tmp_path, base) as (request, state_for):
        base._client.raccoon_thinking_models = {base.model: CAPABILITY}
        reply = await request({"raccoon_thinking_level": "level_5", "execution_profile": "standard"})
        state = state_for(reply)
        response = await request({}, method="session/prompt", session_id=reply["result"]["sessionId"])
        assert response["result"]["_meta"]["ok"] is False
        assert response["result"]["_meta"]["errorCode"] == "params_unsupport_error"
        assert len(calls) == 1
        assert calls[0]["raccoon_thinking_level"] == "level_5"
        assert state.raccoon_thinking_level == "level_5"

@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
async def test_unmanaged_catalog_keeps_existing_native_wire(stream, enabled):
    async with wire_client(model="sn-glm-5-3") as (base, calls):
        async def send():
            messages = [Message(role="user", content="hello")]
            if stream:
                _ = [event async for event in base.generate_stream(messages, thinking_enabled=enabled)]
            else:
                await base.generate(messages, thinking_enabled=enabled)
        await send()
        previous = calls[-1]
        base._client.raccoon_thinking_models = {base.model: {
            "managed": False, "supported": False, "can_disable": False, "display_levels": [],
        }}
        await send()
        assert calls[-1] == previous
        assert "raccoon_thinking_level" not in calls[-1]
