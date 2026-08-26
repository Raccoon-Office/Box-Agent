"""Tests for lightweight LLM prompt service + ACP ``llm/prompt`` extension."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from box_agent.llm.lightweight import (
    LightweightContentFiltered,
    LightweightInvalidArgs,
    LightweightPromptError,
    LightweightTimeout,
    run_lightweight_prompt,
)
from box_agent.schema import LLMResponse, StreamEvent
from box_agent.schema.schema import TokenUsage


class _FakeLLM:
    """Minimal LLMClient stub for lightweight calls."""

    provider = "anthropic"
    model = "claude-sonnet-test"

    def __init__(
        self,
        content: str = "ok",
        *,
        delay: float = 0.0,
        usage: TokenUsage | None = None,
        raises: Exception | None = None,
    ):
        self._content = content
        self._delay = delay
        self._usage = usage
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages,
        tools=None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "thinking_enabled": thinking_enabled,
                "session_id": session_id,
                "turn_id": turn_id,
                "title": title,
                "call_kind": call_kind,
            }
        )
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return LLMResponse(
            content=self._content,
            finish_reason="stop",
            usage=self._usage,
        )

    async def generate_stream(
        self,
        messages,
        tools=None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "thinking_enabled": thinking_enabled,
                "session_id": session_id,
                "turn_id": turn_id,
                "title": title,
                "call_kind": call_kind,
            }
        )
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        if self._content:
            yield StreamEvent(type="text", delta=self._content)
        yield StreamEvent(type="finish", finish_reason="stop", usage=self._usage)


@pytest.mark.asyncio
async def test_lightweight_returns_text_and_usage():
    llm = _FakeLLM(
        content="summary text",
        usage=TokenUsage(prompt_tokens=42, completion_tokens=7, total_tokens=49),
    )
    result = await run_lightweight_prompt(llm, "summarize this", system_prompt="you are terse")

    assert result.text == "summary text"
    assert result.input_tokens == 42
    assert result.output_tokens == 7
    assert result.finish_reason == "stop"
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_lightweight_passes_no_tools_and_no_thinking():
    llm = _FakeLLM()
    await run_lightweight_prompt(llm, "hello")

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["tools"] is None
    assert call["thinking_enabled"] is False
    assert call["call_kind"] == "utility"
    # No system message when none supplied.
    assert [m.role for m in call["messages"]] == ["user"]


@pytest.mark.asyncio
async def test_lightweight_threads_session_id():
    llm = _FakeLLM()
    await run_lightweight_prompt(llm, "hello", session_id="office-session-1")

    assert llm.calls[0]["session_id"] == "office-session-1"


@pytest.mark.asyncio
async def test_lightweight_includes_system_when_provided():
    llm = _FakeLLM()
    await run_lightweight_prompt(llm, "hello", system_prompt="be brief")

    assert [m.role for m in llm.calls[0]["messages"]] == ["system", "user"]


@pytest.mark.asyncio
async def test_lightweight_ignores_blank_system_prompt():
    llm = _FakeLLM()
    await run_lightweight_prompt(llm, "hello", system_prompt="   ")

    assert [m.role for m in llm.calls[0]["messages"]] == ["user"]


@pytest.mark.asyncio
async def test_lightweight_rejects_empty_prompt():
    llm = _FakeLLM()
    with pytest.raises(LightweightInvalidArgs):
        await run_lightweight_prompt(llm, "   ")
    assert llm.calls == []


@pytest.mark.asyncio
async def test_lightweight_rejects_non_positive_timeout():
    llm = _FakeLLM()
    with pytest.raises(LightweightInvalidArgs):
        await run_lightweight_prompt(llm, "hi", timeout=0)


@pytest.mark.asyncio
async def test_lightweight_timeout_raises_structured_error():
    llm = _FakeLLM(delay=0.2)
    with pytest.raises(LightweightTimeout) as ei:
        await run_lightweight_prompt(llm, "hi", timeout=0.01)
    assert ei.value.code == "timeout"


@pytest.mark.asyncio
async def test_lightweight_wraps_llm_exception():
    llm = _FakeLLM(raises=RuntimeError("provider blew up"))
    with pytest.raises(LightweightPromptError) as ei:
        await run_lightweight_prompt(llm, "hi")
    assert ei.value.code == "lightweight_failed"
    assert "provider blew up" in str(ei.value)


@pytest.mark.asyncio
async def test_lightweight_does_not_leak_raw_json_blob():
    # A generic provider error must be humanized, not dumped raw.
    blob = "Error code: 500 - {'error': {'message': 'kaboom', 'type': 'server_error'}}"
    llm = _FakeLLM(raises=RuntimeError(blob))
    with pytest.raises(LightweightPromptError) as ei:
        await run_lightweight_prompt(llm, "hi")
    # server_error is humanized to a friendly sentence; no raw dict leaks.
    assert "{" not in str(ei.value)
    assert ei.value.code == "lightweight_failed"


@pytest.mark.asyncio
async def test_lightweight_content_filter_distinct_error():
    blob = (
        "Error code: 400 - {'error': {'code': 'content_filter', "
        "'message': 'Inappropriate input/output rejected', "
        "'type': 'invalid_request_error'}}"
    )
    llm = _FakeLLM(raises=RuntimeError(blob))
    with pytest.raises(LightweightContentFiltered) as ei:
        await run_lightweight_prompt(llm, "习明泽 怎么了")
    assert ei.value.code == "content_filter"
    # Message is a neutral technical note — NOT the chat refusal copy, and not
    # suitable as a title value.
    assert "换个问题" not in str(ei.value)
    assert "{" not in str(ei.value)


@pytest.mark.asyncio
async def test_lightweight_content_filter_is_subclass():
    # Existing `except LightweightPromptError` callers still catch it.
    assert issubclass(LightweightContentFiltered, LightweightPromptError)


@pytest.mark.asyncio
async def test_lightweight_cancellation_propagates():
    llm = _FakeLLM(delay=10)

    task = asyncio.create_task(run_lightweight_prompt(llm, "hi", timeout=30))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# ACP extMethod integration
# ---------------------------------------------------------------------------


class _StubAgent:
    """Bare BoxACPAgent surface for exercising extMethod without ACP wiring."""

    def __init__(self, llm: _FakeLLM):
        self._llm = llm
        self._lite_llm = llm

    # Bind the real implementation as if it were a method on this stub.
    from box_agent.acp import BoxACPAgent

    extMethod = BoxACPAgent.extMethod
    _llm_prompt = BoxACPAgent._llm_prompt
    _llm_for_binding = BoxACPAgent._llm_for_binding
    _utility_llm_for_meta = BoxACPAgent._utility_llm_for_meta


@pytest.mark.asyncio
async def test_extmethod_llm_prompt_success():
    llm = _FakeLLM(
        content="title here",
        usage=TokenUsage(prompt_tokens=12, completion_tokens=3, total_tokens=15),
    )
    agent = _StubAgent(llm)

    resp = await agent.extMethod(
        "llm/prompt",
        {
            "prompt": "give me a title",
            "systemPrompt": "be terse",
            "timeoutMs": 5000,
            "_meta": {"purpose": "local_agent_title"},
            "workspaceLabel": "local-agent-title",
        },
    )

    assert resp["text"] == "title here"
    assert llm.calls[0]["call_kind"] == "title_generate"
    assert resp["finishReason"] == "stop"
    assert resp["usage"] == {"inputTokens": 12, "outputTokens": 3}
    assert "durationMs" in resp
    call = llm.calls[0]
    assert call["session_id"].startswith("local-agent-utility-")
    assert call["turn_id"].startswith(f'{call["session_id"]}-turn-')
    assert call["title"] == "local_agent_title"


@pytest.mark.asyncio
async def test_extmethod_title_prompt_caps_output_at_eight_thousand_tokens(monkeypatch):
    llm = _FakeLLM(content="short title")
    agent = _StubAgent(llm)
    captured: dict[str, Any] = {}

    def fake_resolve_model_client(client, **kwargs):
        captured.update(kwargs)
        return client, {"mode": "test"}

    monkeypatch.setattr(
        "box_agent.acp.resolve_model_client",
        fake_resolve_model_client,
    )

    resp = await agent.extMethod(
        "llm/prompt",
        {
            "prompt": "give me a title",
            "_meta": {"purpose": "title"},
        },
    )

    assert resp["text"] == "short title"
    assert captured["max_output_tokens_cap"] == 8_000


@pytest.mark.asyncio
async def test_extmethod_llm_prompt_empty_returns_error():
    agent = _StubAgent(_FakeLLM())
    resp = await agent.extMethod("llm/prompt", {"prompt": ""})

    assert "error" in resp
    assert resp["error"]["code"] == "invalid_args"


@pytest.mark.asyncio
async def test_extmethod_llm_prompt_bad_timeout_returns_error():
    agent = _StubAgent(_FakeLLM())
    resp = await agent.extMethod(
        "llm/prompt", {"prompt": "hi", "timeoutMs": "not-a-number"}
    )

    assert resp["error"]["code"] == "invalid_args"


@pytest.mark.asyncio
async def test_extmethod_llm_prompt_timeout_returns_error():
    agent = _StubAgent(_FakeLLM(delay=0.2))
    resp = await agent.extMethod("llm/prompt", {"prompt": "hi", "timeoutMs": 10})

    assert resp["error"]["code"] == "timeout"


@pytest.mark.asyncio
async def test_extmethod_llm_prompt_content_filter_returns_code():
    """A moderation refusal surfaces a stable `content_filter` code so the host
    can fall back to a neutral default (e.g. a generic title)."""
    blob = "Error code: 400 - {'error': {'code': 'content_filter', 'message': 'x'}}"
    agent = _StubAgent(_FakeLLM(raises=RuntimeError(blob)))
    resp = await agent.extMethod(
        "llm/prompt",
        {"prompt": "习明泽 怎么了", "_meta": {"purpose": "local_agent_title"}},
    )

    assert resp["error"]["code"] == "content_filter"
    # Never the chat refusal copy, never a raw blob.
    assert "换个问题" not in resp["error"]["message"]
    assert "{" not in resp["error"]["message"]


@pytest.mark.asyncio
async def test_extmethod_llm_prompt_does_not_create_session():
    """The endpoint must never touch BoxACPAgent._sessions."""
    llm = _FakeLLM(content="ok")
    agent = _StubAgent(llm)
    # _sessions isn't even attached to the stub — if the impl reached for it,
    # we'd get an AttributeError.
    resp = await agent.extMethod("llm/prompt", {"prompt": "hi"})
    assert resp.get("text") == "ok"
    assert not hasattr(agent, "_sessions")
