"""Auxiliary requests use session thinking and the selected provider's policy."""

import json
from types import SimpleNamespace

import httpx
import pytest

from box_agent.kernel.compact_engine import DefaultCompactEngine
from box_agent.kernel.context_types import CompactionInput
from box_agent.llm.lightweight import run_lightweight_prompt
from box_agent.llm.llm_wrapper import SessionBoundLLM
from box_agent.schema import Message
from tests.test_lightweight_prompt import _StubAgent
from tests.test_reasoning_disabled_policy import wire_client


LIMITED_MODEL = "SenseNova-Flash-Lite-20260727-v39-fp8-step4k-dpov2-mtp"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled,expected", [(False, "low"), (True, "high")])
async def test_known_sensenova_model_uses_supported_reasoning_effort(stream, enabled, expected):
    async with wire_client(model=LIMITED_MODEL) as (client, requests):
        if stream:
            _ = [event async for event in client.generate_stream(
                [Message(role="user", content="hello")], thinking_enabled=enabled)]
        else:
            await client.generate([Message(role="user", content="hello")], thinking_enabled=enabled)
    assert [request["reasoning_effort"] for request in requests] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_known_sensenova_model_rejects_explicit_none_before_http(stream):
    async with wire_client(model=LIMITED_MODEL, policy="none") as (client, requests):
        with pytest.raises(ValueError, match="reasoning_effort_when_disabled.*none.*low"):
            if stream:
                _ = [event async for event in client.generate_stream(
                    [Message(role="user", content="hello")])]
            else:
                await client.generate([Message(role="user", content="hello")])
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [
    "SenseNova-Flash-Lite-other", LIMITED_MODEL + "-other", "gemini-2.0-flash", "gpt-4.1",
])
async def test_known_model_default_does_not_change_unrelated_models(model):
    async with wire_client(model=model) as (client, requests):
        await client.generate([Message(role="user", content="hello")])
    expected = None if model == "gpt-4.1" else "none"
    assert requests[0].get("reasoning_effort") == expected


def summary_response(payload):
    chunk = {
        "id": "summary-response", "created": 1, "model": payload["model"],
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "finish_reason": "stop", "delta": {
            "content": "<summary>Keep the user's request.</summary>",
        }}],
    }
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
        text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["shared", "routed", "independent"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_compaction_inherits_session_thinking_and_preserves_selected_endpoint_policy(binding, enabled):
    async with wire_client(policy="low", respond=summary_response) as (main, main_requests):
        async with wire_client(model="SenseNova-Summary", respond=summary_response) as (other, other_requests):
            summary = None
            if binding == "routed":
                summary = SessionBoundLLM(main.for_model("SenseNova-Routed-Summary"))
            elif binding == "independent":
                summary = other
            outcome = await DefaultCompactEngine().compact_if_needed(CompactionInput(
                history=(Message(role="system", content="BASE"), Message(role="user", content="request")),
                token_limit=10000, llm=main, summary_llm=summary, thinking_enabled=enabled,
                force=True, estimate_tools={}, summary_input_token_limit=10000,
            ))
            assert outcome.mode == "summary", outcome.error
            assert outcome.summary_calls == 1
            requests = other_requests if binding == "independent" else main_requests
            expected = "high" if enabled else "none" if binding == "independent" else "low"
            assert [request["reasoning_effort"] for request in requests] == [expected]
            assert not (main_requests if binding == "independent" else other_requests)
            assert main.reasoning_effort_when_disabled == "low"
            assert other.reasoning_effort_when_disabled is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_public_agent_passes_session_thinking_into_compaction(tmp_path, enabled):
    from box_agent.agent import Agent
    from tests.test_context_compaction_e2e import CompactionE2ELLM

    class RecordingLLM(CompactionE2ELLM):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(kwargs)
            async for event in super().generate_stream(messages, tools, **kwargs):
                yield event

    llm = RecordingLLM()
    agent = Agent(llm_client=llm, system_prompt="Be concise", tools=[], max_steps=2,
        workspace_dir=str(tmp_path), token_limit=10000, thinking_enabled=enabled,
        deferred_mcp_loading_enabled=False)
    agent.messages.extend([
        Message(role="user", content="old request"),
        Message(role="assistant", content="x" * 50000),
        Message(role="user", content="latest request"),
    ])
    _ = [event async for event in agent.run_events()]
    summaries = [call for call in llm.requests if call.get("call_kind") == "context_summary"]
    assert len(summaries) == 1
    assert summaries[0]["thinking_enabled"] is enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_lightweight_prompt_can_inherit_session_thinking(enabled):
    async with wire_client(model=LIMITED_MODEL) as (client, requests):
        await run_lightweight_prompt(client, "Summarize", thinking_enabled=enabled)
    assert requests[0]["reasoning_effort"] == ("high" if enabled else "low")


@pytest.mark.asyncio
async def test_acp_session_utility_uses_current_thinking_without_copying_main_provider_policy():
    async with wire_client(policy="low") as (main, main_requests):
        async with wire_client(model="SenseNova-Utility") as (utility, requests):
            agent = _StubAgent(main)
            session = SimpleNamespace(
                upstream_session_id="office-session", session_llm=SessionBoundLLM(utility),
                agent=SimpleNamespace(thinking_enabled=True),
            )
            agent._sessions = {"acp-session": session}
            for enabled in (True, False):
                session.agent.thinking_enabled = enabled
                response = await agent.extMethod("llm/prompt", {
                    "prompt": "Summarize", "_meta": {"session_id": "office-session"},
                })
                assert "text" in response, response
            assert [request["reasoning_effort"] for request in requests] == ["high", "none"]
            assert main_requests == []
            assert utility.reasoning_effort_when_disabled is None


@pytest.mark.asyncio
async def test_acp_follow_up_suggestions_inherit_effective_session_thinking(tmp_path, monkeypatch):
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from tests.test_follow_up_suggestions import _DedicatedFollowUpLLM, _RecordingConn

    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "state"))
    llm = _DedicatedFollowUpLLM()
    llm.release_suggestions.set()
    agent = BoxACPAgent(_RecordingConn(), Config(
        llm=LLMConfig(api_key="test"), agent=AgentConfig(max_steps=2),
        tools=ToolsConfig(),
    ), llm, [], "system")
    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={
        "follow_up_suggestions": True, "deep_think": True, "session_id": "office-session",
    }))
    response = await agent.prompt(SimpleNamespace(
        sessionId=session.sessionId, prompt=[{"text": "Give a short answer"}], field_meta={},
    ))
    assert response.stopReason == "end_turn"
    task = agent._sessions[session.sessionId].follow_up_suggestions_task
    assert task is not None
    await task
    assert llm.generate_calls[-1]["thinking_enabled"] is True
