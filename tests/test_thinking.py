"""Tests for session-level deep-think / extended thinking passthrough."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.llm.openai_client import OpenAIClient
from box_agent.schema import Message, StreamEvent


# ───────────────────────── Anthropic ─────────────────────────

@pytest.mark.asyncio
async def test_anthropic_request_has_thinking_when_enabled(monkeypatch):
    """AnthropicClient injects the ``thinking`` param when ``thinking_enabled=True``."""
    client = AnthropicClient(api_key="k", api_base="https://x.example", model="claude-3")

    captured: dict = {}

    async def fake_create(**params):
        captured.update(params)
        return SimpleNamespace(content=[], usage=None, stop_reason="stop")

    monkeypatch.setattr(client.client.messages, "create", fake_create)

    await client._make_api_request(
        system_message="hi",
        api_messages=[{"role": "user", "content": "go"}],
        tools=None,
        thinking_enabled=True,
    )

    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert captured["max_tokens"] > 8000  # budget must be strictly less than max_tokens


@pytest.mark.asyncio
async def test_anthropic_request_no_thinking_by_default(monkeypatch):
    client = AnthropicClient(api_key="k", api_base="https://x.example", model="claude-3")

    captured: dict = {}

    async def fake_create(**params):
        captured.update(params)
        return SimpleNamespace(content=[], usage=None, stop_reason="stop")

    monkeypatch.setattr(client.client.messages, "create", fake_create)

    await client._make_api_request(
        system_message=None,
        api_messages=[{"role": "user", "content": "go"}],
        tools=None,
    )

    assert "thinking" not in captured


# ───────────────────────── OpenAI ─────────────────────────

@pytest.mark.asyncio
async def test_openai_request_has_extra_body_when_enabled(monkeypatch):
    """OpenAIClient injects Qwen-style ``extra_body.enable_thinking`` when enabled."""
    client = OpenAIClient(api_key="k", api_base="https://x.example", model="qwen")

    captured: dict = {}

    async def fake_create(**params):
        captured.update(params)
        choice = SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=None, reasoning_details=None),
        )
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(client.client.chat.completions, "create", fake_create)

    await client._make_api_request(
        api_messages=[{"role": "user", "content": "go"}],
        tools=None,
        thinking_enabled=True,
    )

    assert captured["extra_body"] == {
        "enable_thinking": True,
        "thinking_budget": 8000,
    }
    assert captured["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_openai_request_no_extra_body_by_default(monkeypatch):
    """Default path sends no ``extra_body`` — especially no ``reasoning_split`` (deleted)."""
    client = OpenAIClient(api_key="k", api_base="https://x.example", model="qwen")

    captured: dict = {}

    async def fake_create(**params):
        captured.update(params)
        choice = SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=None, reasoning_details=None),
        )
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(client.client.chat.completions, "create", fake_create)

    await client._make_api_request(
        api_messages=[{"role": "user", "content": "go"}],
        tools=None,
    )

    assert "extra_body" not in captured, "extra_body must not be sent by default"
    assert "reasoning_effort" not in captured, "reasoning_effort must not be sent by default"


# ───────────────────────── Core plumbing ─────────────────────────

@pytest.mark.asyncio
async def test_run_agent_loop_forwards_thinking_flag():
    """``run_agent_loop(thinking_enabled=True)`` must thread the flag into ``generate_stream``."""
    from box_agent.core import run_agent_loop

    captured: dict = {}

    class _LLM:
        async def generate_stream(self, *, messages, tools, thinking_enabled=False):
            captured["thinking_enabled"] = thinking_enabled
            yield StreamEvent(type="text", delta="hi")
            yield StreamEvent(type="finish", finish_reason="stop")

        async def generate(self, messages, tools=None, *, thinking_enabled=False):
            return SimpleNamespace(content="", thinking=None, tool_calls=None)

    events = []
    async for ev in run_agent_loop(
        llm=_LLM(),
        messages=[Message(role="user", content="ping")],
        tools={},
        max_steps=1,
        thinking_enabled=True,
    ):
        events.append(ev)

    assert captured["thinking_enabled"] is True


# ───────────────────────── ACP wiring ─────────────────────────

@pytest.mark.asyncio
async def test_acp_new_session_reads_deep_think(tmp_path):
    class _Conn:
        async def sessionUpdate(self, payload):
            pass

    class _LLM:
        async def generate(self, *args, **kwargs):
            from box_agent.schema import LLMResponse
            return LLMResponse(content="general", finish_reason="stop")

    config = Config(
        llm=LLMConfig(api_key="k"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(_Conn(), config, _LLM(), [], "base")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={"session_mode": "general", "deep_think": True},
        )
    )
    state = agent._sessions[session.sessionId]
    assert state.thinking_enabled is True
    assert state.agent.thinking_enabled is True


@pytest.mark.asyncio
async def test_acp_new_session_default_no_deep_think(tmp_path):
    class _Conn:
        async def sessionUpdate(self, payload):
            pass

    class _LLM:
        pass

    config = Config(
        llm=LLMConfig(api_key="k"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(_Conn(), config, _LLM(), [], "base")

    session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]
    assert state.thinking_enabled is False
    assert state.agent.thinking_enabled is False


@pytest.mark.asyncio
async def test_acp_run_turn_forwards_thinking_to_core(tmp_path, monkeypatch):
    """Regression: ACP must pass ``agent.thinking_enabled`` into ``run_agent_loop``.

    A prior bug wired ``deep_think`` into ``Agent.thinking_enabled`` but the ACP
    turn-runner called ``run_agent_loop`` directly and dropped the flag — the
    HTTP body never carried ``thinking``. This test asserts the kwarg reaches
    core unchanged for the deep-think=True path.
    """
    from box_agent.events import DoneEvent, StopReason
    from box_agent import acp as acp_mod

    class _Conn:
        async def sessionUpdate(self, payload):
            pass

    class _LLM:
        async def generate(self, *args, **kwargs):
            from box_agent.schema import LLMResponse
            return LLMResponse(content="general", finish_reason="stop")

    config = Config(
        llm=LLMConfig(api_key="k"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(_Conn(), config, _LLM(), [], "base")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={"session_mode": "general", "deep_think": True},
        )
    )

    captured: dict = {}

    async def fake_loop(**kwargs):
        captured.update(kwargs)
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="ok")

    monkeypatch.setattr(acp_mod, "run_agent_loop", fake_loop)

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"type": "text", "text": "hi"}],
        )
    )

    assert captured.get("thinking_enabled") is True, (
        "ACP dropped thinking_enabled on the way to run_agent_loop"
    )
