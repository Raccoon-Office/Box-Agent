"""Five-level host choices reach provider requests without cross-session leaks."""
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.llm import LLMClient, SessionBoundLLM
from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.llm.openai_client import _apply_thinking_params
from box_agent.llm.reasoning import parse_reasoning_effort
from box_agent.schema import LLMProvider
from tests.acp_helpers import acp_params

EFFORTS = ("fast", "medium", "high", "xhigh", "max")


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", EFFORTS)
async def test_acp_choice_reaches_sdk_http_body_without_mutating_default(tmp_path, effort):
    captured = []

    async def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "reply", "object": "chat.completion", "created": 0,
            "model": "sn-sensenova-6-8-flash-lite",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        })

    class Conn:
        async def session_update(self, *args, **kwargs):
            pass

    base = LLMClient(api_key="k", provider=LLMProvider.OPENAI, model="sn-sensenova-6-8-flash-lite", api_base="https://example.test/v1")
    await base._client.client.close()
    base._client.client = AsyncOpenAI(api_key="k", base_url="https://example.test/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    config = Config(llm=LLMConfig(api_key="k"), agent=AgentConfig(workspace_dir=str(tmp_path)), tools=ToolsConfig())
    adapter = BoxACPAgent(Conn(), config, base, [], "base")
    try:
        result = await adapter.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path), field_meta={"reasoning_effort": effort, "deep_think": effort == "fast"})))
        state = adapter._sessions[result.sessionId]
        assert state.reasoning_effort == effort
        assert state.agent.thinking_enabled == (effort != "fast")
        provider = state.session_llm._delegate._client
        await provider._make_api_request(api_messages=[{"role": "user", "content": "go"}], tools=None, thinking_enabled=state.agent.thinking_enabled)
        assert captured[-1]["reasoning_effort"] == ("none" if effort == "fast" else effort)
        # Streaming and image preflight use the same body builder.
        stream_body = provider._request_body_params([{"role": "user", "content": "go"}], None, max_tokens=64000, thinking_enabled=state.agent.thinking_enabled, stream=True)
        assert stream_body["reasoning_effort"] == captured[-1]["reasoning_effort"]
        assert not hasattr(base._client, "reasoning_effort")
        await provider._make_api_request(api_messages=[{"role": "user", "content": "utility"}], tools=None, thinking_enabled=False)
        assert captured[-1]["reasoning_effort"] == "none"
    finally:
        await base.aclose()


@pytest.mark.asyncio
async def test_rebinding_and_child_models_keep_effort_isolated():
    base = LLMClient(api_key="k", provider=LLMProvider.OPENAI, model="gpt-6-astra", api_base="https://example.test/v1")
    try:
        base._client._ephemeral_max_output_tokens = 99999
        medium = SessionBoundLLM(base, reasoning_effort="medium")
        assert medium._delegate._client._ephemeral_max_output_tokens is None
        assert base._client._ephemeral_max_output_tokens == 99999
        maximum = SessionBoundLLM(base, reasoning_effort="max")
        child = maximum.for_model("gpt-6.1-sol")
        maximum.bind(base.for_model("gpt-6.1-terra"))
        for client, effort in ((medium, "medium"), (maximum, "max"), (child, "max")):
            assert client._delegate._client.reasoning_effort == effort
        assert not hasattr(base._client, "reasoning_effort")
    finally:
        await base.aclose()


@pytest.mark.parametrize("bad", ["", "low", "ultra", 3, True, {}, []])
def test_unknown_effort_is_rejected(bad):
    with pytest.raises(ValueError, match="reasoning_effort"):
        parse_reasoning_effort(bad)


@pytest.mark.parametrize("effort,budget", [("fast", None), ("medium", 4000), ("high", 8000), ("xhigh", 16000), ("max", 32000)])
def test_anthropic_uses_native_budget(effort, budget):
    client = AnthropicClient.__new__(AnthropicClient)
    client.model = "claude-sonnet-4"
    client.max_output_tokens = 64000
    client.reasoning_effort = effort
    body = client._request_body_params(None, [], None, thinking_enabled=True)
    assert body.get("thinking", {}).get("budget_tokens") == budget


@pytest.mark.parametrize("model,effort,expected", [
    ("kimi-k3", "medium", "high"), ("kimi-k3", "xhigh", "max"),
    ("deepseek-v4-pro", "medium", "high"), ("deepseek-v4-pro", "xhigh", "high"),
    ("glm-5.3", "medium", "high"), ("glm-5.3", "xhigh", "max"),
    ("kimi-k3.1-pro", "fast", "low"), ("glm-5.2", "fast", "none"),
    ("gemini-2.5-pro", "xhigh", "high"), ("gpt-6-astra", "fast", "low"),
])
def test_known_dialects_keep_supported_values(model, effort, expected):
    body = {}
    _apply_thinking_params(body, model=model, thinking_enabled=effort != "fast", reasoning_effort=effort)
    assert body["reasoning_effort"] == expected
