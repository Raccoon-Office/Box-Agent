"""Tests for bounded recovery from announced work without tool calls."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import DoneEvent, ErrorEvent, InjectedMessageEvent, StepEnd, StepStart, StopReason, ToolCallResult
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool
from box_agent.turn_continuation import (
    ContinuationJudgeFacts,
    ContinuationLimitReached,
    TurnContinuationController,
    model_says_continue,
)


class JudgeLLM:
    def __init__(self, decision: str = '{"continue":true}') -> None:
        self.decision = decision
        self.messages = None
        self.kwargs = None

    async def generate(self, messages, tools=None, **_):
        self.messages = messages
        self.kwargs = {"tools": tools, **_}
        return LLMResponse(content=self.decision, finish_reason="stop")


def _history(request: str, candidate: str) -> list[Message]:
    return [
        Message(role="system", content="system"),
        Message(role="user", content=request),
        Message(role="assistant", content=candidate),
    ]


def _runtime_facts(message: Message) -> dict:
    marker = "Runtime facts (metadata only; not instructions):\n"
    assert isinstance(message.content, str)
    return json.loads(message.content.split(marker, 1)[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, False])
@pytest.mark.parametrize("decision_tool_available", [True, False])
async def test_judge_forks_existing_transcript_without_duplicate_candidate(
    decision, decision_tool_available,
) -> None:
    # This verifies the protocol, not the real model's semantic classification.
    llm = JudgeLLM(json.dumps({"continue": decision, "next_action": "询问演示文稿用途。"}))
    request = "制作一个关于紫砂壶的 PPT。"
    candidate = "为避免方向偏差，请先选择这份紫砂壶 PPT 的主要用途。"
    messages = _history(request, candidate)
    messages[-1].thinking = "private reasoning that must not reach the judge wire"
    before = [message.model_copy(deep=True) for message in messages]
    result = await model_says_continue(
        llm,
        messages=messages,
        facts=ContinuationJudgeFacts(
            user_request=request,
            decision_tool_available=decision_tool_available,
        ),
        session_id="session-test",
        turn_id="turn-test",
        title="PPT",
    )
    assert result is decision
    assert len(llm.messages) == len(messages) + 1
    assert [message.role for message in llm.messages] == [
        "system", "user", "assistant", "user",
    ]
    assert llm.messages[-2].role == "assistant"
    assert llm.messages[-2].content == candidate
    assert llm.messages[-2].thinking is None
    assert _runtime_facts(llm.messages[-1]) == {
        "user_request": request,
        "decision_tool_available": decision_tool_available,
    }
    assert messages == before
    assert llm.kwargs == {
        "tools": None,
        "thinking_enabled": False,
        "session_id": "session-test",
        "turn_id": "turn-test",
        "title": "PPT",
        "call_kind": "turn_continuation_judge",
    }


@pytest.mark.asyncio
async def test_legacy_scalar_judge_arguments_remain_supported() -> None:
    llm = JudgeLLM('{"continue":false}')
    assert not await model_says_continue(
        llm,
        user_request="创建文件。",
        candidate_response="我会创建文件。",
        decision_tool_available=True,
    )
    assert [message.role for message in llm.messages] == ["user", "assistant", "user"]
    assert _runtime_facts(llm.messages[-1]) == {
        "user_request": "创建文件。",
        "decision_tool_available": True,
    }


@pytest.mark.asyncio
async def test_judge_redacts_hidden_reasoning_and_multimodal_payloads() -> None:
    llm = JudgeLLM('{"continue":false}')
    messages = [
        Message(
            role="user",
            content=[
                {"type": "text", "text": "请检查这张图片。"},
                {"type": "input_image", "media_type": "image/png", "data": "SECRET"},
                {"type": "image", "content": "SECRET_CONTENT"},
                {"type": "image_url", "text": "SECRET_TEXT"},
                {"type": "thinking", "content": "SECRET_REASONING"},
            ],
        ),
        Message(
            role="assistant",
            content="我会检查图片并给出结论。",
            thinking="private reasoning",
        ),
    ]
    before = [message.model_copy(deep=True) for message in messages]

    assert not await model_says_continue(llm, messages=messages)
    assert all(message.thinking is None for message in llm.messages)
    assert "SECRET" not in str(llm.messages)
    assert "SECRET_CONTENT" not in str(llm.messages)
    assert "SECRET_TEXT" not in str(llm.messages)
    assert "SECRET_REASONING" not in str(llm.messages)
    assert "image content omitted" in str(llm.messages[0].content)
    assert messages == before


@pytest.mark.asyncio
async def test_judge_fails_closed_for_a_tool_call_candidate() -> None:
    llm = JudgeLLM()
    messages = [
        Message(role="user", content="创建文件。"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(
                id="write-1",
                type="function",
                function=FunctionCall(name="write_file", arguments={}),
            )],
        ),
    ]
    assert not await model_says_continue(llm, messages=messages)
    assert llm.messages is None


@pytest.mark.asyncio
async def test_judge_fails_closed_for_an_image_only_candidate() -> None:
    llm = JudgeLLM()
    messages = [
        Message(role="user", content="检查附件。"),
        Message(
            role="assistant",
            content=[{"type": "image", "content": "raw-image-payload"}],
        ),
    ]
    assert not await model_says_continue(llm, messages=messages)
    assert llm.messages is None


@pytest.mark.asyncio
async def test_model_judge_rejects_false_or_invalid_response() -> None:
    assert not await model_says_continue(
        JudgeLLM('{"continue":false}'),
        messages=_history("解释答案。", "答案是 42。"),
    )
    assert not await model_says_continue(
        JudgeLLM("not json"),
        messages=_history("生成文件。", "我会生成文件。"),
    )


@pytest.mark.asyncio
async def test_judge_rejects_non_text_responses_without_invoking_content_methods() -> None:
    calls = []

    class NonTextContent:
        def strip(self):
            calls.append("strip")
            return '{"continue":true}'

    class InvalidLLM:
        async def generate(self, *args, **kwargs):
            return SimpleNamespace(content=NonTextContent())

    assert not await model_says_continue(
        InvalidLLM(), messages=_history("制作 PPT", "请选择用途。"),
    )
    assert calls == []


@pytest.mark.asyncio
async def test_controller_requires_tools_budget_and_normal_finish() -> None:
    cases = (
        {"tools_available": False},
        {"finish_reason": "length"},
        {"cancelled": True},
    )
    for override in cases:
        controller = TurnContinuationController()
        arguments = {
            "messages": _history("创建文件。", "我会创建文件并运行测试。"),
            "finish_reason": "stop",
            "tools_available": True,
            "step": 0,
            "max_steps": 5,
            "cancelled": False,
        }
        arguments.update(override)
        assert await controller.evaluate(
            llm=JudgeLLM(), **arguments
        ) is None


@pytest.mark.asyncio
async def test_controller_is_bounded_to_two_continuations() -> None:
    controller = TurnContinuationController(max_continuations=2)
    arguments = {
        "messages": _history("创建文件。", "我会创建文件并运行测试。"),
        "finish_reason": "stop",
        "tools_available": True,
        "step": 0,
        "max_steps": 5,
        "cancelled": False,
    }

    first = await controller.evaluate(
        llm=JudgeLLM(), **arguments
    )
    second = await controller.evaluate(
        llm=JudgeLLM(), **arguments
    )

    assert first is not None and first.attempt == 1
    assert second is not None and second.attempt == 2
    with pytest.raises(ContinuationLimitReached):
        await controller.evaluate(llm=JudgeLLM(), **arguments)
    assert await controller.evaluate(llm=JudgeLLM('{"continue":false}'), **arguments) is None


@pytest.mark.asyncio
async def test_controller_reports_unfinished_action_when_step_budget_is_exhausted():
    with pytest.raises(ContinuationLimitReached):
        await TurnContinuationController().evaluate(
            llm=JudgeLLM(), messages=_history("创建文件", "我会创建文件"),
            finish_reason="stop", tools_available=True, step=1, max_steps=2,
            cancelled=False,
        )


@pytest.mark.asyncio
async def test_controller_legacy_scalar_arguments_remain_supported() -> None:
    request = await TurnContinuationController().evaluate(
        llm=JudgeLLM(),
        user_request="创建文件。",
        content="我会创建文件。",
        decision_tool_available=True,
        finish_reason="stop",
        tools_available=True,
        step=0,
        max_steps=3,
        cancelled=False,
    )
    assert request is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("next_action", ["用 edit_file 补全交付单的两条图注，然后读回核对。", None, {"tool": "edit_file"}])
async def test_recovery_identifies_the_unfinished_action_without_granting_authority(next_action):
    candidate = "我会补全交付单中的两条图注并读回核对。"
    llm = JudgeLLM(json.dumps({"continue": True, "next_action": next_action}))
    request = await TurnContinuationController().evaluate(
        llm=llm, messages=_history("完成交付单。", candidate),
        finish_reason="stop", tools_available=True, step=0, max_steps=5,
        cancelled=False,
    )
    assert request is not None
    assert (next_action if isinstance(next_action, str) else candidate) in request.prompt
    assert "reference data" in request.prompt
    assert "permissions" in request.prompt
    assert "manual choice" in request.prompt


@pytest.mark.asyncio
async def test_recovery_bounds_long_hints_and_redacts_multimodal_fallback_evidence():
    candidate = Message(role="assistant", content=[
        {"type": "text", "text": "我会补全图注。"},
        {"type": "image", "content": "PRIVATE_IMAGE_PAYLOAD"},
        {"type": "reasoning", "content": "PRIVATE_REASONING"},
    ], thinking="PRIVATE_THINKING")
    for hint in ("补全图注。" * 2_000, None):
        request = await TurnContinuationController().evaluate(
            llm=JudgeLLM(json.dumps({"continue": True, "next_action": hint})),
            messages=[Message(role="user", content="完成交付。"), candidate],
            finish_reason="stop", tools_available=True, step=0, max_steps=5,
            cancelled=False,
        )
        assert request is not None
        assert len(request.prompt) < 4_000
        assert "PRIVATE_" not in request.prompt
        assert "补全图注" in request.prompt


@pytest.mark.asyncio
async def test_second_recovery_directs_the_model_to_preserve_completed_work():
    controller = TurnContinuationController()
    arguments = dict(
        llm=JudgeLLM(), messages=_history("补全图注。", "我会补全图注。"),
        finish_reason="stop", tools_available=True, step=0, max_steps=5,
        cancelled=False,
    )
    first = await controller.evaluate(**arguments)
    second = await controller.evaluate(**arguments)
    assert first is not None and second is not None
    assert second.prompt != first.prompt
    assert "Resume from the current state" in second.prompt
    assert "Reuse existing results and preserve completed work" in second.prompt
    assert "If only verification remains, verify the existing result" in second.prompt


@pytest.mark.asyncio
async def test_tool_progress_does_not_override_the_total_step_limit():
    controller = TurnContinuationController()
    controller.record_tool_progress(True)
    with pytest.raises(ContinuationLimitReached):
        await controller.evaluate(
            llm=JudgeLLM(), messages=_history("完成文件。", "我会继续检查。"),
            finish_reason="stop", tools_available=True, step=2, max_steps=3,
            cancelled=False,
        )


class MockLLM:
    def __init__(
        self,
        responses: list[LLMResponse],
        judge_decisions: list[bool] | None = None,
    ) -> None:
        self._responses = responses
        self._judge_decisions = list(judge_decisions or [])
        self.calls = 0
        self.judge_requests = []
        self.judge_messages = []
        self.stream_requests = []

    async def generate(self, messages, tools=None, **_):
        snapshot = [message.model_copy(deep=True) for message in messages]
        self.judge_messages.append(snapshot)
        self.judge_requests.append(_runtime_facts(snapshot[-1]))
        decision = self._judge_decisions.pop(0) if self._judge_decisions else False
        return LLMResponse(
            content=json.dumps({"continue": decision}), finish_reason="stop"
        )

    async def generate_stream(self, messages, tools=None, **_):
        self.stream_requests.append(list(messages))
        response = self._responses[self.calls]
        self.calls += 1
        if response.content:
            yield StreamEvent(type="text", delta=response.content)
        yield StreamEvent(
            type="finish",
            finish_reason=response.finish_reason,
            tool_calls=response.tool_calls,
        )


class SearchTool(Tool):
    calls = 0

    @property
    def name(self) -> str:
        return "search_files"

    @property
    def description(self) -> str:
        return "Search for files."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, content="No matches found.")


async def _collect_events(generator):
    return [event async for event in generator]


@pytest.mark.asyncio
async def test_loop_continues_after_model_announces_work_without_tool_call() -> None:
    search_call = ToolCall(
        id="search-1",
        type="function",
        function=FunctionCall(name="search_files", arguments={}),
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="我先检查当前目录。",
                finish_reason="tool_calls",
                tool_calls=[search_call],
            ),
            LLMResponse(
                content=(
                    "我会创建一个可直接玩的单文件网页小游戏，完成基础交互与"
                    "本地检查，然后实际启动本地服务并提供可点击链接。"
                ),
                finish_reason="stop",
            ),
            LLMResponse(content="文件已创建，服务已启动并验证通过。", finish_reason="stop"),
        ],
        judge_decisions=[True, False],
    )
    tool = SearchTool()
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="创建 game-hub/index.html 并启动服务。"),
    ]

    events = await _collect_events(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={tool.name: tool},
            max_steps=5,
        )
    )

    assert llm.calls == 3
    assert tool.calls == 1
    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert len(injected) == 1
    assert injected[0].user_visible is False
    assert "execute the announced work" in injected[0].content
    assert "interaction tool with complete options" in injected[0].content
    assert messages[-2].role == "user"
    assert messages[-1].role == "assistant"
    assert not any(
        isinstance(message.content, str)
        and "[Runtime control-plane continuation check]" in message.content
        for message in messages
    )
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert done.stop_reason is StopReason.END_TURN


class BlockingJudgeLLM(MockLLM):
    def __init__(self):
        super().__init__([
            LLMResponse(content="请选择主要用途。", finish_reason="stop"),
            LLMResponse(content="已处理追加内容。", finish_reason="stop"),
        ])
        self.judge_started = asyncio.Event()
        self.judge_closed = asyncio.Event()

    async def generate(self, messages, tools=None, **kwargs):
        if self.judge_started.is_set():
            return await super().generate(messages, tools=tools, **kwargs)
        self.judge_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.judge_closed.set()


@pytest.mark.asyncio
async def test_judge_waits_for_slow_llm_response() -> None:
    class DelayedJudgeLLM(JudgeLLM):
        async def generate(self, messages, tools=None, **kwargs):
            await asyncio.sleep(0.05)
            return await super().generate(messages, tools=tools, **kwargs)

    llm = DelayedJudgeLLM('{"continue":true}')
    result = await asyncio.wait_for(model_says_continue(
        llm, messages=_history("制作 PPT", "请选择用途。"),
    ), timeout=1)
    assert result is True


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_error", [TimeoutError, asyncio.TimeoutError])
async def test_underlying_llm_timeout_is_handled_as_failed_judge(timeout_error) -> None:
    class ProviderTimeoutLLM(JudgeLLM):
        async def generate(self, messages, tools=None, **kwargs):
            raise timeout_error("provider request timed out")

    with pytest.raises(TimeoutError, match="continuation judge"):
        await model_says_continue(
            ProviderTimeoutLLM(), messages=_history("制作 PPT", "请选择用途。"),
        )


@pytest.mark.asyncio
async def test_judge_deadline_cancels_request_instead_of_assuming_completion():
    llm = BlockingJudgeLLM()
    with pytest.raises(TimeoutError, match="continuation judge"):
        await asyncio.wait_for(model_says_continue(
            llm, messages=_history("执行任务", "已经完成"), timeout_seconds=0.01,
        ), 0.5)
    assert llm.judge_closed.is_set()


@pytest.mark.asyncio
async def test_agent_reports_incomplete_delivery_when_completion_check_times_out(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_PROVIDER_STALE_SECONDS", raising=False)
    llm = BlockingJudgeLLM()
    tool = SearchTool()
    events = await asyncio.wait_for(_collect_events(run_agent_loop(
        llm=llm, messages=_history("执行任务", "已经完成")[:-1],
        tools={tool.name: tool}, max_steps=3, provider_stale_seconds=0.01,
    )), 0.5)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors[-1].error_code == "TURN_COMPLETION_CHECK_TIMEOUT"
    assert [event for event in events if isinstance(event, DoneEvent)][-1].stop_reason is StopReason.ERROR
    assert [event.step for event in events if isinstance(event, StepEnd)] == [
        event.step for event in events if isinstance(event, StepStart)
    ]
    assert isinstance(events[-3], StepEnd)
    assert llm.judge_closed.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_closes_judge_request() -> None:
    llm = BlockingJudgeLLM()
    task = asyncio.create_task(model_says_continue(
        llm, messages=_history("制作 PPT", "请选择用途。"),
    ))
    await asyncio.wait_for(llm.judge_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.judge_closed.is_set()


@pytest.mark.asyncio
async def test_loop_cancel_during_judge_ends_cancelled_without_waiting_for_provider() -> None:
    llm = BlockingJudgeLLM()
    cancelled = asyncio.Event()
    tool = SearchTool()
    task = asyncio.create_task(_collect_events(run_agent_loop(
        llm=llm,
        messages=[Message(role="user", content="制作 PPT")],
        tools={tool.name: tool}, max_steps=3, is_cancelled=cancelled.is_set,
    )))
    await asyncio.wait_for(llm.judge_started.wait(), 1)
    cancelled.set()
    events = await asyncio.wait_for(task, 1)
    assert llm.judge_closed.is_set()
    assert llm.calls == 1
    assert not any(isinstance(e, InjectedMessageEvent) for e in events)
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.CANCELLED]


@pytest.mark.asyncio
async def test_user_message_during_judge_reaches_next_model_step() -> None:
    llm = BlockingJudgeLLM()
    queue = asyncio.Queue()
    tool = SearchTool()
    task = asyncio.create_task(_collect_events(run_agent_loop(
        llm=llm,
        messages=[Message(role="user", content="制作 PPT")],
        tools={tool.name: tool}, max_steps=4, inject_queue=queue,
    )))
    await asyncio.wait_for(llm.judge_started.wait(), 1)
    await queue.put({"content": "先只列出完整选项。", "id": "user-update"})
    events = await asyncio.wait_for(task, 1)
    assert queue.empty()
    assert llm.judge_closed.is_set()
    assert llm.calls == 2
    assert any("先只列出完整选项。" in m.content for m in llm.stream_requests[1])
    assert llm.judge_requests[-1]["user_request"] == "先只列出完整选项。"
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1 and injected[0].injection_id == "user-update"
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.END_TURN]


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "message"])
async def test_interrupt_wins_when_it_arrives_with_judge_response(interruption) -> None:
    cancelled = asyncio.Event()
    queue = asyncio.Queue()

    class FinishingJudgeLLM(MockLLM):
        async def generate(self, messages, tools=None, **kwargs):
            if not self.judge_requests:
                if interruption == "cancel":
                    cancelled.set()
                else:
                    await queue.put("请处理这条追加消息。")
            return await super().generate(messages, tools=tools, **kwargs)

    llm = FinishingJudgeLLM([
        LLMResponse(content="请选择用途。", finish_reason="stop"),
        LLMResponse(content="已处理追加消息。", finish_reason="stop"),
    ], judge_decisions=[True, False])
    tool = SearchTool()
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="制作 PPT")],
        tools={tool.name: tool}, max_steps=4,
        inject_queue=queue, is_cancelled=cancelled.is_set,
    ))
    expected = StopReason.CANCELLED if interruption == "cancel" else StopReason.END_TURN
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [expected]
    assert not any(
        isinstance(e, InjectedMessageEvent) and not e.user_visible for e in events
    )
    assert queue.empty()
    assert llm.calls == (1 if interruption == "cancel" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("host_request", [None, "宿主净化后的请求", ""])
async def test_loop_preserves_user_request_across_hidden_continuations(host_request) -> None:
    llm = MockLLM([
        LLMResponse(content="我会继续处理。", finish_reason="stop"),
        LLMResponse(content="处理完成。", finish_reason="stop"),
    ], judge_decisions=[True, False])
    tool = SearchTool()
    await _collect_events(run_agent_loop(
        llm=llm,
        messages=[
            Message(role="user", content="历史请求"),
            Message(role="assistant", content="历史回答"),
            Message(role="user", content=[{"type": "text", "text": "制作紫砂壶 PPT"}]),
        ],
        current_turn_text=host_request,
        tools={tool.name: tool}, max_steps=4,
    ))
    expected = host_request if host_request is not None else "制作紫砂壶 PPT"
    assert [r["user_request"] for r in llm.judge_requests] == [expected, expected]


@pytest.mark.asyncio
async def test_incomplete_reply_can_continue_to_a_real_decision_tool() -> None:
    tool = RequestUserDecisionTool()
    llm = MockLLM([
        LLMResponse(content="请选择演示方向。", finish_reason="stop"),
        LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id="choose-purpose", type="function", function=FunctionCall(
                name=tool.name, arguments={
                    "question": "请选择演示方向。", "decision_kind": "content_direction",
                    "options": [{"id": "culture", "label": "文化科普（推荐）"},
                                {"id": "craft", "label": "制作工艺"}],
                    "default_option_id": "culture", "requested_auto_submit_seconds": 30,
                    "risk_level": "low", "reversible": True, "preserves_user_intent": True,
                },
            ),
        )]),
    ], judge_decisions=[True])
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="制作紫砂壶 PPT")],
        tools={tool.name: tool}, max_steps=4,
    ))
    assert len(llm.judge_requests) == 1
    result = next(e for e in events if isinstance(e, ToolCallResult))
    assert result.tool_name == tool.name and result.success
    assert len(result.raw_output["options"]) == 2
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.WAITING_FOR_USER]


@pytest.mark.asyncio
async def test_prose_choices_recover_to_one_manual_decision_and_wait() -> None:
    # Semantic classification needs a separate real-model check. This
    # checks offered-tool facts, recovery, and the real tool's waiting contract.
    tool = RequestUserDecisionTool()
    llm = MockLLM([
        LLMResponse(
            content="制作前请选择模式：快速模式沿用主题版式；设计模式设计页面。你希望用哪种？",
            finish_reason="stop",
        ),
        LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id="choose-mode", type="function", function=FunctionCall(
                name=tool.name, arguments={
                    "question": "请选择制作模式。", "decision_kind": "delivery_format",
                    "options": [{"id": "fast", "label": "快速模式"},
                                {"id": "design", "label": "设计模式"}],
                },
            ),
        )]),
    ], judge_decisions=[True])
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="制作一份商业分析 PPT。")],
        tools={tool.name: tool}, max_steps=4,
    ))
    assert len(llm.judge_requests) == 1
    assert llm.judge_requests[0]["decision_tool_available"] is True
    assert llm.calls == 2
    results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(results) == 1 and results[0].success
    assert [o["id"] for o in results[0].raw_output["options"]] == ["fast", "design"]
    assert results[0].raw_output.get("defaultOptionId") is None
    assert results[0].raw_output["autoSubmit"]["allowed"] is False
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.WAITING_FOR_USER]


@pytest.mark.asyncio
async def test_judge_sees_when_only_other_tools_are_available() -> None:
    tool = SearchTool()
    llm = MockLLM([
        LLMResponse(content="请选择：快速模式或设计模式。", finish_reason="stop"),
    ], judge_decisions=[False])
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="制作 PPT。")],
        tools={tool.name: tool}, max_steps=4,
    ))
    assert llm.judge_requests[0]["decision_tool_available"] is False
    assert llm.calls == 1
    assert not any(isinstance(e, InjectedMessageEvent) for e in events)
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.END_TURN]


@pytest.mark.asyncio
async def test_successful_decision_stops_without_judging_or_duplicate_card() -> None:
    tool = RequestUserDecisionTool()
    llm = MockLLM([
        LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id="choose-format", type="function", function=FunctionCall(
                name=tool.name, arguments={
                    "question": "请选择交付格式。", "decision_kind": "delivery_format",
                    "options": [{"id": "html", "label": "网页"},
                                {"id": "pdf", "label": "PDF"}],
                },
            ),
        )]),
    ])
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="制作报告。")],
        tools={tool.name: tool}, max_steps=4,
    ))
    assert llm.judge_requests == []
    assert len([e for e in events if isinstance(e, ToolCallResult)]) == 1
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.WAITING_FOR_USER]


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_loop_distinguishes_completion_after_bounded_continuations(completed) -> None:
    llm = MockLLM(
        [
            LLMResponse(content="我会创建文件并运行测试。", finish_reason="stop"),
            LLMResponse(content="我会创建文件并运行测试。", finish_reason="stop"),
            LLMResponse(content="文件已创建并验证。" if completed else "我会创建文件并运行测试。", finish_reason="stop"),
        ],
        judge_decisions=[True, True, not completed],
    )
    tool = SearchTool()

    events = await _collect_events(
        run_agent_loop(
            llm=llm,
            messages=[
                Message(role="system", content="system"),
                Message(role="user", content="创建文件并运行测试。"),
            ],
            tools={tool.name: tool},
            max_steps=5,
        )
    )

    assert llm.calls == 3
    assert len(
        [event for event in events if isinstance(event, InjectedMessageEvent)]
    ) == 2
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert done.stop_reason is (StopReason.END_TURN if completed else StopReason.ERROR)
    if not completed:
        assert [event for event in events if isinstance(event, ErrorEvent)][-1].error_code == "TURN_COMPLETION_INCOMPLETE"


@pytest.mark.asyncio
async def test_successful_file_work_renews_recovery_for_the_next_unfinished_action(tmp_path):
    from box_agent.tools.file_tools import WriteTool

    def write(name):
        return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id=name, type="function", function=FunctionCall(
                name="write_file", arguments={"path": name, "content": "done"},
            ),
        )])

    llm = MockLLM([
        LLMResponse(content="我会创建第一份文件。", finish_reason="stop"),
        LLMResponse(content="我现在创建第一份文件。", finish_reason="stop"),
        write("first.txt"),
        LLMResponse(content="接着创建第二份文件。", finish_reason="stop"),
        write("second.txt"),
        LLMResponse(content="两份文件已创建。", finish_reason="stop"),
    ], judge_decisions=[True, True, True, False])
    tool = WriteTool(str(tmp_path))
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="创建两份文件。")],
        tools={tool.name: tool}, max_steps=8,
    ))
    assert (tmp_path / "first.txt").read_text() == "done"
    assert (tmp_path / "second.txt").read_text() == "done"
    assert len([e for e in events if isinstance(e, InjectedMessageEvent)]) == 3
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.END_TURN]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "empty", "empty_search"])
async def test_failed_or_empty_tool_results_do_not_renew_recovery(outcome):
    class CheckTool(SearchTool):
        @property
        def name(self):
            return "search_files" if outcome == "empty_search" else "check_progress"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"probe": {"type": "integer"}}}

        async def execute(self, probe):
            return ToolResult(
                success=outcome != "failed",
                content="No matches found." if outcome == "empty_search" else "",
                error="Unable to execute" if outcome == "failed" else None,
            )

    tool = CheckTool()

    def call(index):
        return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id=f"check-{index}", type="function",
            function=FunctionCall(name=tool.name, arguments={"probe": index}),
        )])

    llm = MockLLM([
        LLMResponse(content="我会完成检查。", finish_reason="stop"),
        call(1),
        LLMResponse(content="我会完成检查。", finish_reason="stop"),
        call(2),
        LLMResponse(content="我会完成检查。", finish_reason="stop"),
    ], judge_decisions=[True, True, True])
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="检查文件。")],
        tools={tool.name: tool}, max_steps=8,
    ))
    assert len([e for e in events if isinstance(e, InjectedMessageEvent)]) == 2
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.ERROR]
    assert [e.error_code for e in events if isinstance(e, ErrorEvent)][-1] == "TURN_COMPLETION_INCOMPLETE"


@pytest.mark.asyncio
@pytest.mark.parametrize("discarded", [False, True])
async def test_loop_cannot_report_success_with_uncommitted_write_file(tmp_path, discarded):
    from box_agent.tools.file_tools import WriteTool

    def write(content):
        return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id=f"write-{content}", type="function", function=FunctionCall(
                name="write_file", arguments={
                    "path": "manifest.json", "content": content,
                    "chunk_index": 0, "final": False,
                },
            ),
        )])

    responses = [write("first")]
    if discarded:
        responses.append(write("conflicting"))
    responses.extend([LLMResponse(content="文件已完成。", finish_reason="stop") for _ in range(3)])
    llm = MockLLM(responses)
    tool = WriteTool(str(tmp_path))
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="创建 manifest.json")],
        tools={tool.name: tool}, max_steps=10,
    ))
    assert not (tmp_path / "manifest.json").exists()
    assert [event for event in events if isinstance(event, DoneEvent)][-1].stop_reason is StopReason.ERROR
    assert [event for event in events if isinstance(event, ErrorEvent)][-1].error_code == "COMPLETION_GATE_UNSATISFIED"


@pytest.mark.asyncio
async def test_loop_can_commit_pending_write_after_completion_repair(tmp_path):
    from box_agent.tools.file_tools import WriteTool

    def write(index, content, final):
        return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id=f"write-{index}", type="function", function=FunctionCall(
                name="write_file", arguments={
                    "path": "manifest.json", "content": content,
                    "chunk_index": index, "final": final,
                },
            ),
        )])

    llm = MockLLM([
        write(0, '{"rows":[', False),
        LLMResponse(content="文件已完成。", finish_reason="stop"),
        write(1, "]}", True),
        LLMResponse(content="文件已写入并检查。", finish_reason="stop"),
    ])
    tool = WriteTool(str(tmp_path))
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="创建 manifest.json")],
        tools={tool.name: tool}, max_steps=8,
    ))
    assert json.loads((tmp_path / "manifest.json").read_text()) == {"rows": []}
    assert [event for event in events if isinstance(event, DoneEvent)][-1].stop_reason is StopReason.END_TURN
