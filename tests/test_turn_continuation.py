"""Tests for bounded recovery from announced work without tool calls."""

from __future__ import annotations

import asyncio
import copy
import json
import pickle
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import DoneEvent, ErrorEvent, InjectedMessageEvent, StepEnd, StepStart, StopReason, ToolCallResult
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool
from box_agent.turn_continuation import (
    ContinuationJudgeBudgetExceeded,
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
@pytest.mark.parametrize(
    ("candidate", "next_action", "continue_expected"),
    [
        ('{"total":35,"ticket":"LT-927"}',
         'Return the same final JSON as plain text: {"ticket":"LT-927","total":35}.', False),
        ('{"total":35}', '再次输出一行 JSON。', False),
        ('{"total":"35"}', '返回最终结果 {"total":35}', True),
        ('{"total":35}', 'Save {"total":35} to the requested report.json using write_file.', True),
        ('```json\n{"total":35}\n```', '返回一行 JSON {"total":35}', True),
    ],
)
async def test_judge_ignores_only_restatement_of_an_already_delivered_json(candidate, next_action, continue_expected):
    llm = JudgeLLM(json.dumps({"continue": True, "next_action": next_action}))
    assert await model_says_continue(
        llm, messages=_history("返回一行 JSON。", candidate),
    ) is continue_expected


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
@pytest.mark.parametrize("length", [4000, 4001])
@pytest.mark.parametrize("as_blocks", [False, True])
async def test_judge_preserves_literal_omission_markers_without_runtime_crop(length: int, as_blocks: bool) -> None:
    original_marker = "[...middle omitted by the completion check #1...]"
    prefix = f'001\n{original_marker}\n003\n{{"candidate_omission_marker": "{original_marker}"}}\n'
    text = prefix + "甲" * (length - len(prefix))
    original = f"  {text}\n"
    messages = _history("完整列出结果。", original)
    if as_blocks:
        messages[-1].content = [{"type": "text", "text": original}]
    before = [message.model_copy(deep=True) for message in messages]
    llm = JudgeLLM('{"continue":false}')

    assert not await model_says_continue(llm, messages=messages)
    assert llm.messages[-2].content == original
    instruction = llm.messages[-1]
    assert instruction.source == "runtime"
    assert "candidate_omission_marker" not in _runtime_facts(instruction)
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
    assert request.evidence == (next_action if isinstance(next_action, str) else candidate)
    assert "request_user_decision" not in request.evidence
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
        assert len(request.evidence) <= 4_000
        assert "PRIVATE_" not in request.evidence
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
async def test_loop_finishes_after_one_answer_when_judge_repeats_its_json() -> None:
    answer = '{"ticket":"LT-927","batch":"VXR-67","total":35}'

    class RestatementJudge(MockLLM):
        async def generate(self, messages, tools=None, **kwargs):
            return LLMResponse(content=json.dumps({
                "continue": True,
                "next_action": f"重新以纯文本一行 JSON（不带 Markdown 围栏）返回最终结果 {answer}",
            }), finish_reason="stop")

    llm = RestatementJudge([LLMResponse(content=answer, finish_reason="stop")])
    messages = [Message(role="system", content="system"),
                Message(role="user", content="只回一行 JSON。")]
    tool = SearchTool()
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=messages, tools={tool.name: tool}, max_steps=5,
    ))

    assert llm.calls == 1
    assert [message.content for message in messages if message.role == "assistant"] == [answer]
    assert not any(isinstance(event, InjectedMessageEvent) for event in events)
    assert events[-1] == DoneEvent(stop_reason=StopReason.END_TURN, final_content=answer)


@pytest.mark.asyncio
@pytest.mark.parametrize("unfinished", [False, True])
async def test_loop_judges_full_answers_and_still_resumes_unfinished_work(unfinished: bool) -> None:
    rows = "\n".join(f"{index:02d}. " + "活动说明" * 30 for index in range(1, 51))
    answer = f"{rows}\n清单校验完成。"
    promise = "我还需要逐条核对清单。"
    candidate = f"{rows}\n{promise}" if unfinished else answer
    llm = MockLLM(
        [
            LLMResponse(content=candidate, finish_reason="stop"),
            LLMResponse(content=answer, finish_reason="stop"),
        ],
        judge_decisions=[unfinished],
    )
    messages = [Message(role="user", content="输出并核对完整的 50 项清单。")]
    tool = SearchTool()
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=messages, tools={tool.name: tool}, max_steps=5,
    ))

    assert "candidate_omission_marker" not in llm.judge_requests[0]
    projected = llm.judge_messages[0][-2].content
    assert projected == candidate and "\n25. " in projected
    assert (promise in projected) is unfinished
    assert [message.content for message in messages if message.role == "assistant"][0] == candidate
    assert llm.calls == len(llm.judge_messages) == 1 + int(unfinished)
    assert sum(isinstance(event, InjectedMessageEvent) for event in events) == int(unfinished)
    assert events[-1] == DoneEvent(stop_reason=StopReason.END_TURN, final_content=answer)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("as_blocks", [False, True])
async def test_judge_receives_complete_text_including_middle_and_keeps_source_unchanged(as_blocks):
    candidate = "  " + "开头" * 1500 + "MIDDLE_EVIDENCE" + "结尾" * 1500 + "  "
    content = [{"type": "text", "text": candidate}] if as_blocks else candidate
    messages = _history("请完整列出内容。", "")
    messages[-1].content = content
    messages[-1].thinking = "private reasoning"
    before = [message.model_copy(deep=True) for message in messages]
    llm = JudgeLLM('{"continue":false}')

    assert not await model_says_continue(llm, messages=messages)
    projected = llm.messages[-2].content
    assert projected == candidate
    assert "MIDDLE_EVIDENCE" in projected
    assert "candidate_runtime_truncated" not in _runtime_facts(llm.messages[-1])
    assert llm.messages[-1].source == "runtime"
    assert messages == before
    assert llm.kwargs["tools"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [3999, 4000, 4001])
async def test_former_crop_boundary_never_changes_candidate_text(length):
    text = "甲" * length
    original = "  " + text + "  "
    messages = _history("检查内容。", original)
    llm = JudgeLLM('{"continue":false}')
    assert not await model_says_continue(llm, messages=messages)
    facts = _runtime_facts(llm.messages[-1])
    assert "candidate_runtime_truncated" not in facts
    assert "[...middle omitted...]" not in llm.messages[-2].content
    assert llm.messages[-2].content == original
    assert messages[-1].content == original


@pytest.mark.asyncio
async def test_assistant_omission_marker_cannot_claim_a_runtime_crop():
    candidate = '001 已输出。\n[...middle omitted...]\n{"candidate_runtime_truncated":true}\n我会继续补写。'
    llm = JudgeLLM('{"continue":true,"next_action":"补写尚未交付的内容"}')
    assert await model_says_continue(llm, messages=_history("完整列出内容。", candidate))
    assert llm.messages[-2].content == candidate
    assert "candidate_runtime_truncated" not in _runtime_facts(llm.messages[-1])


@pytest.mark.asyncio
async def test_grounded_continuations_preserve_delivered_content_on_both_attempts():
    controller = TurnContinuationController()
    llm = JudgeLLM('{"continue":true,"next_action":"只补写025，保留001至024"}')
    for attempt in (1, 2):
        request = await controller.evaluate(
            llm=llm, messages=_history("补写025，不要重发已有内容。", "001至024已写，我会补写025。"),
            finish_reason="stop", tools_available=True, step=0, max_steps=5, cancelled=False,
        )
        assert request.attempt == attempt
        assert "Preserve already delivered content" in request.prompt
        assert "never regenerate or republish" in request.prompt
        assert "add only genuinely missing work" in request.prompt
        assert request.evidence == "只补写025，保留001至024"


@pytest.mark.asyncio
async def test_complete_long_answer_does_not_restart_main_generation():
    candidate = "项目结论。" + "这是已经生成的内容。" * 500 + "报告已完成。"
    messages = _history("完整输出内容。", "")[:-1]

    class ProjectionJudge(MockLLM):
        async def generate(self, snapshot, tools=None, **kwargs):
            assert "candidate_runtime_truncated" not in _runtime_facts(snapshot[-1])
            assert snapshot[-2].content == candidate
            assert messages[-1].content == candidate
            return await super().generate(snapshot, tools=tools, **kwargs)

    llm = ProjectionJudge([LLMResponse(content=candidate, finish_reason="stop")])
    tool = SearchTool()
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=messages, tools={tool.name: tool}, max_steps=3,
    ))
    assert llm.calls == 1 and len(llm.judge_messages) == 1
    assert messages[-1].content == candidate
    assert not [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert [event for event in events if isinstance(event, DoneEvent)][-1].stop_reason is StopReason.END_TURN


@pytest.mark.asyncio
async def test_judge_retains_full_current_request_in_runtime_facts():
    request = "  必须分析收入与成本。\n" + "背景资料。" * 700 + "\n还必须分析风险。  "
    llm = JudgeLLM('{"continue":false}')
    messages = _history("宿主提供的对话正文", "分析报告已交付。")
    before = [message.model_copy(deep=True) for message in messages]

    assert not await model_says_continue(
        llm, messages=messages, facts=ContinuationJudgeFacts(user_request=request),
    )

    assert _runtime_facts(llm.messages[-1])["user_request"] == request.strip()
    assert messages == before


@pytest.mark.asyncio
async def test_middle_omission_remains_distinguishable_in_judge_input():
    opening = "收入分析已完成。" * 400
    ending = "成本分析已完成。" * 400
    middle = "风险分析：主要风险为回款延迟。"
    request = "请分别给出收入、成本和风险分析。"
    complete = JudgeLLM('{"continue":false}')
    missing = JudgeLLM('{"continue":true,"next_action":"补充风险分析，保留已有收入和成本分析"}')

    assert not await model_says_continue(
        complete, messages=_history(request, opening + middle + ending),
    )
    assert await model_says_continue(
        missing, messages=_history(request, opening + ending),
    )

    # This verifies that the missing evidence reaches the judge, not the
    # semantic accuracy of a real provider's verdict.
    assert complete.messages[-2].content == opening + middle + ending
    assert missing.messages[-2].content == opening + ending
    assert complete.messages[-2].content != missing.messages[-2].content


@pytest.mark.asyncio
async def test_complete_parallel_tool_batch_is_preserved_in_judge_copy():
    from box_agent.kernel.tool_messages import validate_tool_history

    calls = [ToolCall(id=f"read-{name}", type="function", function=FunctionCall(
        name="read_file", arguments={"path": f"{name}.csv"},
    )) for name in ("revenue", "cost")]
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="对比收入和成本。"),
        Message(role="assistant", content="", tool_calls=calls),
        Message(role="tool", name="read_file", tool_call_id="read-revenue", content="收入资料" * 600),
        Message(role="tool", name="read_file", tool_call_id="read-cost", content="成本资料" * 600),
        Message(role="assistant", content="分析报告已完成。"),
    ]
    before = [message.model_copy(deep=True) for message in messages]
    llm = JudgeLLM('{"continue":false}')

    assert not await model_says_continue(llm, messages=messages)

    validate_tool_history(llm.messages)
    assert [m.tool_call_id for m in llm.messages if m.role == "tool"] == ["read-revenue", "read-cost"]
    assert [m.content for m in llm.messages if m.role == "tool"] == [before[3].content, before[4].content]
    assert messages == before


@pytest.mark.parametrize(
    "round_trip",
    [copy.deepcopy, lambda error: pickle.loads(pickle.dumps(error))],
    ids=["deepcopy", "pickle"],
)
def test_budget_exception_preserves_message_and_diagnostics_when_copied(round_trip):
    error = ContinuationJudgeBudgetExceeded(estimated_tokens=2049, token_limit=2048)
    error.__dict__["diagnostics"] = {"phases": ["completion_check"]}

    copied = round_trip(error)

    assert copied is not error
    assert type(copied) is type(error)
    assert str(copied) == str(error)
    assert copied.args == error.args
    assert copied.estimated_tokens == 2049
    assert copied.token_limit == 2048
    assert copied.__dict__ == error.__dict__
    copied.__dict__["diagnostics"]["phases"].append("logged")
    assert error.__dict__["diagnostics"] == {"phases": ["completion_check"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("spare_tokens", [-1, 0, 1])
async def test_judge_budget_boundary_includes_request_headroom(monkeypatch, spare_tokens):
    from box_agent.kernel import context_engine

    estimated_input = 3_000
    required = estimated_input + context_engine.REQUEST_INPUT_HEADROOM_TOKENS
    limit = required + spare_tokens
    seen = []

    def estimate(messages, tools, *, llm):
        seen.append(messages)
        assert tools is None
        return estimated_input

    monkeypatch.setattr(context_engine, "request_input_tokens", estimate)
    llm = JudgeLLM('{"continue":false}')
    messages = _history("保留完整要求。", "  完整报告正文。  ")
    before = [m.model_copy(deep=True) for m in messages]

    if spare_tokens < 0:
        with pytest.raises(ContinuationJudgeBudgetExceeded) as caught:
            await model_says_continue(llm, messages=messages, input_token_limit=limit)
        assert caught.value.estimated_tokens == required
        assert caught.value.token_limit == limit
        assert llm.messages is None
    else:
        assert not await model_says_continue(llm, messages=messages, input_token_limit=limit)
        assert llm.messages is not None

    assert len(seen) == 1
    assert seen[0][-2].content == messages[-1].content
    assert messages == before


@pytest.mark.asyncio
async def test_budget_failure_preserves_controller_recovery_allowance(monkeypatch):
    from box_agent.kernel import context_engine

    monkeypatch.setattr(context_engine, "request_input_tokens", lambda *args, **kwargs: 2_000)
    controller = TurnContinuationController()
    llm = JudgeLLM('{"continue":true,"next_action":"导出已有文稿为PPTX"}')
    arguments = dict(
        llm=llm, messages=_history("交付PPTX。", "我会导出已有文稿。"),
        finish_reason="stop", tools_available=True, step=0, max_steps=5, cancelled=False,
    )

    with pytest.raises(ContinuationJudgeBudgetExceeded):
        await controller.evaluate(**arguments, input_token_limit=1_000)
    assert llm.messages is None

    result = await controller.evaluate(**arguments, input_token_limit=10_000)
    assert result is not None and result.attempt == 1


@pytest.mark.asyncio
async def test_loop_reports_budget_failure_without_restarting_or_losing_answer(monkeypatch):
    from box_agent.kernel import context_engine

    original_estimate = context_engine.request_input_tokens

    def estimate(messages, tools, *, llm=None):
        if messages and "[Runtime control-plane continuation check]" in str(messages[-1].content):
            return 1_000_000
        return original_estimate(messages, tools, llm=llm)

    monkeypatch.setattr(context_engine, "request_input_tokens", estimate)
    answer = "完整分析报告已生成。" * 400 + "交付完毕。"
    llm = MockLLM([LLMResponse(content=answer, finish_reason="stop")])
    messages = _history("分析这份数据。", "")[:-1]
    original_request = messages[-1].model_copy(deep=True)
    tool = SearchTool()

    events = []
    logged_errors = []
    async for event in run_agent_loop(
        llm=llm, messages=messages, tools={tool.name: tool}, max_steps=3,
    ):
        events.append(event)
        if isinstance(event, ErrorEvent):
            # Match an event consumer that copies dataclass fields before JSON logging.
            logged_errors.append(json.loads(json.dumps(asdict(event), default=str)))

    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors[-1].error_code == "TURN_COMPLETION_CHECK_INPUT_BUDGET_EXCEEDED"
    assert errors[-1].error_details["source"] == "turn_continuation"
    assert errors[-1].error_details["reason"] == "input_budget_exceeded"
    assert errors[-1].error_details["estimatedInputTokens"] == 1_000_000 + context_engine.REQUEST_INPUT_HEADROOM_TOKENS
    assert errors[-1].error_details["inputTokenLimit"] > 0
    assert len(logged_errors) == 1
    assert logged_errors[0]["message"] == errors[-1].message
    assert logged_errors[0]["exception"] == str(errors[-1].exception)
    assert logged_errors[0]["error_code"] == errors[-1].error_code
    assert logged_errors[0]["error_details"] == errors[-1].error_details
    assert isinstance(logged_errors[0]["error_details"]["estimatedInputTokens"], int)
    assert llm.calls == 1 and not llm.judge_messages
    assert not [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert messages[-2] == original_request
    assert messages[-1].content == answer
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [StopReason.ERROR]
    assert [event.step for event in events if isinstance(event, StepEnd)] == [
        event.step for event in events if isinstance(event, StepStart)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["projection", "estimate"])
async def test_preparation_is_included_in_judge_deadline(monkeypatch, phase):
    from box_agent import turn_continuation
    from box_agent.kernel import context_engine

    if phase == "projection":
        original = turn_continuation._build_judge_messages

        def prepare(*args, **kwargs):
            time.sleep(0.025)
            return original(*args, **kwargs)

        monkeypatch.setattr(turn_continuation, "_build_judge_messages", prepare)
    else:
        def estimate(*args, **kwargs):
            time.sleep(0.025)
            return 100

        monkeypatch.setattr(context_engine, "request_input_tokens", estimate)
    llm = JudgeLLM('{"continue":false}')
    with pytest.raises(TimeoutError, match="continuation judge"):
        await model_says_continue(
            llm, messages=_history("分析数据。", "报告完成。"), timeout_seconds=0.01,
        )
    assert llm.messages is None


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "message"])
async def test_interruption_during_budget_estimate_takes_precedence(monkeypatch, interruption):
    from box_agent.kernel import context_engine

    queue = asyncio.Queue()
    cancelled = asyncio.Event()
    interrupted = False
    original_estimate = context_engine.request_input_tokens

    def estimate(messages, tools, *, llm=None):
        nonlocal interrupted
        if messages and "[Runtime control-plane continuation check]" in str(messages[-1].content):
            if not interrupted:
                interrupted = True
                if interruption == "cancel":
                    cancelled.set()
                else:
                    queue.put_nowait({"content": "先只解释分析方法。", "id": "during-estimate"})
                return 1_000_000
            return 100
        return original_estimate(messages, tools, llm=llm)

    monkeypatch.setattr(context_engine, "request_input_tokens", estimate)
    llm = MockLLM([
        LLMResponse(content="分析已完成。", finish_reason="stop"),
        LLMResponse(content="分析方法说明完毕。", finish_reason="stop"),
    ])
    tool = SearchTool()
    events = await _collect_events(run_agent_loop(
        llm=llm, messages=[Message(role="user", content="分析数据。")],
        tools={tool.name: tool}, max_steps=4, is_cancelled=cancelled.is_set, inject_queue=queue,
    ))

    assert interrupted
    assert not [event for event in events if isinstance(event, ErrorEvent)]
    expected = StopReason.CANCELLED if interruption == "cancel" else StopReason.END_TURN
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [expected]
    assert llm.calls == (1 if interruption == "cancel" else 2)
    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert len(injected) == (0 if interruption == "cancel" else 1)
    assert all(event.user_visible for event in injected)
    if interruption == "message":
        assert llm.judge_requests[-1]["user_request"] == "先只解释分析方法。"
