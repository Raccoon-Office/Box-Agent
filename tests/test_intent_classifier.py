"""Unit tests for the ACP session-mode intent classifier."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from box_agent.acp.intent_classifier import classify_session_mode
from box_agent.schema import LLMResponse


class _FixedLLM:
    """Stub LLMClient returning a preconfigured content string."""

    def __init__(self, content: str):
        self._content = content
        self.call_count = 0

    async def generate(self, messages: list, tools: list | None = None) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(content=self._content, finish_reason="stop")


class _RaisingLLM:
    async def generate(self, messages: list, tools: list | None = None) -> LLMResponse:
        raise RuntimeError("boom")


class _SlowLLM:
    def __init__(self, delay: float):
        self._delay = delay

    async def generate(self, messages: list, tools: list | None = None) -> LLMResponse:
        await asyncio.sleep(self._delay)
        return LLMResponse(content="general", finish_reason="stop")


@pytest.mark.asyncio
async def test_classify_ppt_outline():
    llm = _FixedLLM("ppt_outline")
    mode = await classify_session_mode(llm, "帮我做一个讲解 AI 发展的 PPT 大纲")
    assert mode == "ppt_outline"
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_classify_data_analysis():
    llm = _FixedLLM("data_analysis")
    mode = await classify_session_mode(llm, "分析一下这个 Excel 表格的趋势")
    assert mode == "data_analysis"


@pytest.mark.asyncio
async def test_classify_ppt_plan_chat():
    llm = _FixedLLM("ppt_plan_chat")
    mode = await classify_session_mode(llm, "我想聊聊这个 PPT 应该怎么组织")
    assert mode == "ppt_plan_chat"


@pytest.mark.asyncio
async def test_classify_general_returns_none():
    llm = _FixedLLM("general")
    mode = await classify_session_mode(llm, "随便聊聊今天天气")
    assert mode is None


@pytest.mark.asyncio
async def test_classify_empty_user_text():
    llm = _FixedLLM("ppt_outline")
    mode = await classify_session_mode(llm, "")
    assert mode is None
    assert llm.call_count == 0  # Must short-circuit without hitting the LLM


@pytest.mark.asyncio
async def test_classify_handles_exception():
    mode = await classify_session_mode(_RaisingLLM(), "分析一下销售数据")
    assert mode is None  # Fallback to general on any exception


@pytest.mark.asyncio
async def test_classify_handles_invalid_output():
    llm = _FixedLLM("obviously-not-a-valid-label")
    mode = await classify_session_mode(llm, "some message")
    assert mode is None


@pytest.mark.asyncio
async def test_classify_tolerates_punctuated_output():
    llm = _FixedLLM("  `ppt_outline`.  ")
    mode = await classify_session_mode(llm, "outline this deck")
    assert mode == "ppt_outline"


@pytest.mark.asyncio
async def test_classify_echoed_sentence_with_label_first():
    llm = _FixedLLM("ppt_editor_standard_html — edit an HTML slide")
    mode = await classify_session_mode(llm, "change slide 3")
    assert mode == "ppt_editor_standard_html"


@pytest.mark.asyncio
async def test_classify_timeout_falls_back_to_none():
    # 0.5s LLM, 0.05s timeout → TimeoutError, fallback to None.
    mode = await classify_session_mode(_SlowLLM(delay=0.5), "hello", timeout=0.05)
    assert mode is None


@pytest.mark.asyncio
async def test_classify_passes_no_tools_to_llm():
    captured: dict[str, Any] = {}

    class _CapturingLLM:
        async def generate(self, messages, tools=None):
            captured["tools"] = tools
            captured["messages"] = messages
            return LLMResponse(content="general", finish_reason="stop")

    await classify_session_mode(_CapturingLLM(), "hello")
    assert captured["tools"] is None
    # Messages should be system + user only — no prior context.
    assert len(captured["messages"]) == 2
    assert captured["messages"][0].role == "system"
    assert captured["messages"][1].role == "user"
