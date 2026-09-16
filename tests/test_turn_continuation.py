"""Tests for bounded recovery from announced work without tool calls."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import box_agent.turn_continuation as continuation_module
from box_agent.core import run_agent_loop
from box_agent.events import (
    DoneEvent, ErrorEvent, InjectedMessageEvent, StepEnd, StepStart, StopReason, ToolCallResult,
)
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool
from box_agent.turn_continuation import (
    TurnContinuationController, TurnContinuationError, model_says_continue,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, False])
@pytest.mark.parametrize("decision_tool_available", [True, False])
async def test_judge_passes_candidate_and_request_without_exposing_tools(
    decision, decision_tool_available,
) -> None:
    # This verifies the protocol, not the real model's semantic classification.
    llm = JudgeLLM(json.dumps({"continue": decision}))
    request = "制作一个关于紫砂壶的 PPT。"
    candidate = "为避免方向偏差，请先选择这份紫砂壶 PPT 的主要用途。"
    result = await model_says_continue(
        llm,
        user_request=request,
        candidate_response=candidate,
        decision_tool_available=decision_tool_available,
        session_id="session-test",
        turn_id="turn-test",
        title="PPT",
    )
    assert result is decision
    assert json.loads(llm.messages[-1].content) == {
        "user_request": request,
        "candidate_response": candidate,
        "decision_tool_available": decision_tool_available,
    }
    assert llm.kwargs == {
        "tools": None,
        "thinking_enabled": False,
        "session_id": "session-test",
        "turn_id": "turn-test",
        "title": "PPT",
        "call_kind": "turn_continuation_judge",
    }


@pytest.mark.asyncio
async def test_model_judge_accepts_explicit_false() -> None:
    assert not await model_says_continue(
        JudgeLLM('{"continue":false}'),
        user_request="解释答案。",
        candidate_response="答案是 42。",
    )

@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["not json", "{}", "[]", '{"continue":"false"}', '{"continue":0}'])
async def test_invalid_judge_response_is_not_a_completed_verdict(decision) -> None:
    with pytest.raises(TurnContinuationError, match="completion check failed"):
        await model_says_continue(
            JudgeLLM(decision), user_request="生成文件。", candidate_response="我会生成文件。",
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

    with pytest.raises(TurnContinuationError, match="completion check failed"):
        await model_says_continue(
            InvalidLLM(), user_request="制作 PPT", candidate_response="请选择用途。",
        )
    assert calls == []


@pytest.mark.asyncio
async def test_controller_requires_tools_and_normal_finish() -> None:
    cases = (
        {"tools_available": False},
        {"finish_reason": "length"},
        {"cancelled": True},
    )
    for override in cases:
        controller = TurnContinuationController()
        arguments = {
            "content": "我会创建文件并运行测试。",
            "finish_reason": "stop",
            "tools_available": True,
            "step": 0,
            "max_steps": 5,
            "cancelled": False,
        }
        arguments.update(override)
        assert await controller.evaluate(
            llm=JudgeLLM(), user_request="创建文件。", **arguments
        ) is None


@pytest.mark.asyncio
async def test_controller_is_bounded_to_two_continuations() -> None:
    controller = TurnContinuationController(max_continuations=2)
    arguments = {
        "content": "我会创建文件并运行测试。",
        "finish_reason": "stop",
        "tools_available": True,
        "step": 0,
        "max_steps": 5,
        "cancelled": False,
    }

    first = await controller.evaluate(
        llm=JudgeLLM(), user_request="创建文件。", **arguments
    )
    second = await controller.evaluate(
        llm=JudgeLLM(), user_request="创建文件。", **arguments
    )

    assert first is not None and first.attempt == 1
    assert second is not None and second.attempt == 2
    with pytest.raises(TurnContinuationError, match="continuation limit"):
        await controller.evaluate(llm=JudgeLLM(), user_request="创建文件。", **arguments)
    # A fresh, valid negative verdict may still finish after two continuations.
    assert await controller.evaluate(
        llm=JudgeLLM('{"continue":false}'), user_request="创建文件。", **arguments
    ) is None


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
        self.stream_requests = []

    async def generate(self, messages, tools=None, **_):
        self.judge_requests.append(json.loads(messages[-1].content))
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
async def test_judge_timeout_cancels_provider_request(monkeypatch) -> None:
    monkeypatch.setattr(continuation_module, "JUDGE_TIMEOUT_SECONDS", 0.02)
    llm = BlockingJudgeLLM()
    with pytest.raises(TurnContinuationError, match="completion check failed"):
        await asyncio.wait_for(model_says_continue(
            llm, user_request="制作 PPT", candidate_response="请选择用途。",
        ), timeout=1)
    assert llm.judge_closed.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_closes_judge_request() -> None:
    llm = BlockingJudgeLLM()
    task = asyncio.create_task(model_says_continue(
        llm, user_request="制作 PPT", candidate_response="请选择用途。",
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
async def test_loop_continuation_is_bounded() -> None:
    llm = MockLLM(
        [
            LLMResponse(content="我会创建文件并运行测试。", finish_reason="stop"),
            LLMResponse(content="我会创建文件并运行测试。", finish_reason="stop"),
            LLMResponse(content="我会创建文件并运行测试。", finish_reason="stop"),
        ],
        judge_decisions=[True, True, True],
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
    assert done.stop_reason is StopReason.ERROR
    assert len(llm.judge_requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["service", "configuration", "invalid_json", "timeout"])
@pytest.mark.parametrize("max_steps", [1, 4])
async def test_loop_judge_failure_never_becomes_normal_completion(monkeypatch, failure, max_steps) -> None:
    monkeypatch.setattr(continuation_module, "JUDGE_TIMEOUT_SECONDS", 0.02)
    closed = asyncio.Event()

    class FailedJudgeLLM(MockLLM):
        async def generate(self, messages, **kwargs):
            self.judge_requests.append(messages)
            try:
                if failure == "service":
                    raise ConnectionError("private provider detail")
                if failure == "configuration":
                    raise ValueError("private configuration detail")
                if failure == "invalid_json":
                    return LLMResponse(content="invalid", finish_reason="stop")
                await asyncio.Event().wait()
            finally:
                closed.set()

    llm = FailedJudgeLLM([LLMResponse(content="Task completed.", finish_reason="stop")])
    messages = [Message(role="user", content="Create a report.")]
    events = await asyncio.wait_for(_collect_events(run_agent_loop(
        llm=llm, messages=messages, tools={"search_files": SearchTool()}, max_steps=max_steps,
        truncation_continuation_enabled=False,
    )), 1)
    assert llm.calls == len(llm.judge_requests) == 1
    assert closed.is_set()
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [StopReason.ERROR]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1 and errors[0].is_fatal
    assert "private" not in errors[0].message
    assert [event.step for event in events if isinstance(event, StepStart)] == [1]
    assert [event.step for event in events if isinstance(event, StepEnd)] == [1]
    assert any(message.role == "assistant" and message.content == "Task completed." for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_steps", [1, 4])
@pytest.mark.parametrize("interruption", ["cancel", "message"])
async def test_interruption_wins_over_judge_failure_even_at_step_limit(max_steps, interruption) -> None:
    cancelled, queue = asyncio.Event(), asyncio.Queue()

    class InterruptedJudgeLLM(MockLLM):
        async def generate(self, messages, **kwargs):
            if not self.judge_requests:
                self.judge_requests.append(messages)
                if interruption == "cancel":
                    cancelled.set()
                else:
                    queue.put_nowait("New request.")
                raise ConnectionError("failed together with interruption")
            return await super().generate(messages, **kwargs)

    llm = InterruptedJudgeLLM([
        LLMResponse(content="Old answer.", finish_reason="stop"),
        LLMResponse(content="New answer.", finish_reason="stop"),
    ])
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="Old request.")],
        tools={"search_files": SearchTool()}, max_steps=max_steps,
        is_cancelled=cancelled.is_set, inject_queue=queue,
    ))
    expected = (StopReason.CANCELLED if interruption == "cancel" else
                StopReason.MAX_STEPS if max_steps == 1 else StopReason.END_TURN)
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [expected]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert [event.step for event in events if isinstance(event, StepStart)] == [
        event.step for event in events if isinstance(event, StepEnd)
    ]
    if interruption == "message" and max_steps > 1:
        assert any("New request." in message.content for message in llm.stream_requests[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, False])
async def test_last_step_requires_a_valid_completed_verdict(decision) -> None:
    llm = MockLLM([LLMResponse(content="Candidate answer.", finish_reason="stop")],
                  judge_decisions=[decision])
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="Do the task.")],
        tools={"search_files": SearchTool()}, max_steps=1,
    ))
    assert len(llm.judge_requests) == 1
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [
        StopReason.ERROR if decision else StopReason.END_TURN
    ]


@pytest.mark.asyncio
async def test_stream_only_llm_remains_compatible_without_optional_judge() -> None:
    llm = MockLLM([LLMResponse(content="Completed answer.", finish_reason="stop")])
    llm.generate = None
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="Do the task.")],
        tools={"search_files": SearchTool()}, max_steps=1,
    ))
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [StopReason.END_TURN]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_verdict_after_two_continuations_is_still_checked(failed) -> None:
    class FinalJudgeLLM(MockLLM):
        async def generate(self, messages, **kwargs):
            if failed and len(self.judge_requests) == 2:
                self.judge_requests.append(messages)
                raise ConnectionError("third verdict failed")
            return await super().generate(messages, **kwargs)

    llm = FinalJudgeLLM([
        LLMResponse(content="I will act now.", finish_reason="stop"),
        LLMResponse(content="I will act now.", finish_reason="stop"),
        LLMResponse(content="Final result.", finish_reason="stop"),
    ], judge_decisions=[True, True, False])
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="Do the task.")],
        tools={"search_files": SearchTool()}, max_steps=5,
    ))
    assert llm.calls == len(llm.judge_requests) == 3
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [
        StopReason.ERROR if failed else StopReason.END_TURN
    ]
