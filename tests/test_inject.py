"""Tests for in-stream message injection (inject_queue)."""

import asyncio

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import (
    ContentEvent,
    DoneEvent,
    InjectedMessageEvent,
    StepEnd,
    StepStart,
    StopReason,
    ToolCallResult,
    ToolCallStart,
)
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


# ── Helpers ─────────────────────────────────────────────────────


class MockLLM:
    """Deterministic LLM that yields pre-configured responses in order."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._idx = 0

    async def generate_stream(self, messages, tools=None, **_):
        resp = self._responses[self._idx]
        self._idx += 1
        if resp.thinking:
            yield StreamEvent(type="thinking", delta=resp.thinking)
        if resp.content:
            yield StreamEvent(type="text", delta=resp.content)
        yield StreamEvent(
            type="finish",
            finish_reason=resp.finish_reason,
            usage=resp.usage,
            tool_calls=resp.tool_calls,
        )


class EchoTool(Tool):
    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echoes text back"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str = ""):
        return ToolResult(success=True, content=f"echo:{text}")


async def collect(gen) -> list:
    return [ev async for ev in gen]


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_at_step_boundary():
    """Injected message is drained at step boundary as guidance for the active task."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    # Step 1: tool call, step 2: final response
    llm = MockLLM([
        LLMResponse(
            content="calling tool",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={"text": "a"}))],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    # Pre-load injection so it's ready when step 2 starts
    await queue.put("extra context here")

    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={"echo": EchoTool()}, max_steps=5, inject_queue=queue)
    )

    # InjectedMessageEvent should appear in events
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert injected[0].content == "extra context here"
    assert injected[0].user_visible is True

    # The injected message should be in the message history
    user_msgs = [m for m in msgs if m.role == "user"]
    injected_msg = next(m for m in user_msgs if "extra context" in m.content)
    assert injected_msg.content != "extra context here"
    assert "mid-turn guidance" in injected_msg.content
    assert "not as a new standalone task" in injected_msg.content
    assert "then continue the original task" in injected_msg.content
    assert injected_msg.source == "user"
    assert "The user sent" in injected_msg.content


@pytest.mark.asyncio
async def test_hidden_runtime_injection_updates_context_without_user_message():
    """Internal runtime state reaches the model but is not rendered as user input."""
    queue: asyncio.Queue[dict] = asyncio.Queue()
    msgs = _msgs()
    await queue.put(
        {
            "id": "mcp-runtime-1",
            "content": "MCP server is connected",
            "user_visible": False,
            "source": "runtime",
        }
    )
    llm = MockLLM([LLMResponse(content="ready", finish_reason="stop")])

    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={}, max_steps=5, inject_queue=queue)
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert injected[0].injection_id == "mcp-runtime-1"
    assert injected[0].user_visible is False
    runtime_message = next(
        message for message in msgs if "MCP server is connected" in message.content
    )
    assert "authoritative runtime context" in runtime_message.content
    assert "Mid-turn user message" not in runtime_message.content
    assert runtime_message.source == "runtime"


@pytest.mark.asyncio
@pytest.mark.parametrize("max_steps,reserve,expected_step", [
    (12, 10, 3), (300, 10, 291), (10, 10, None), (6, 10, None),
    (12, 0, None), (12, 1, 12),
])
async def test_budget_feedback_reaches_model_as_runtime_at_existing_threshold(
    max_steps, reserve, expected_step,
):
    from box_agent.config import ToolLimitsConfig
    from box_agent.kernel.loop import _latest_user_text

    calls = []
    final_step = expected_step or 2

    class CapturingLLM:
        async def generate_stream(self, messages, **kwargs):
            calls.append([message.model_copy(deep=True) for message in messages])
            if len(calls) == final_step:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")
            else:
                yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                    ToolCall(id=f"echo-{len(calls)}", type="function", function=FunctionCall(
                        name="echo", arguments={"text": "progress"},
                    )),
                ])

    msgs = _msgs()
    events = await collect(run_agent_loop(
        llm=CapturingLLM(), messages=msgs, tools={"echo": EchoTool()}, max_steps=max_steps,
        tool_limits=ToolLimitsConfig(general={
            "wrapup_remaining_steps": reserve, "final_summary_after_calls": 512,
        }),
    ))
    nudges = [event for event in events if isinstance(event, InjectedMessageEvent)
              and "步数预算提醒" in event.content]
    assert _latest_user_text(msgs) == "hi"
    if expected_step is None:
        assert not nudges
        return
    assert len(nudges) == 1
    assert nudges[0].user_visible is False
    assert not any("步数预算提醒" in str(message.content)
                   for request in calls[:-1] for message in request)
    feedback = next(message for message in calls[-1]
                    if "步数预算提醒" in str(message.content))
    assert feedback.role == "user"
    assert feedback.source == "runtime"
    assert "Runtime state update:" in feedback.content
    assert "The user sent" not in feedback.content
    assert feedback.content.endswith(nudges[0].content)
    assert f"含当前步还可用 {max_steps - expected_step + 1} 步" in feedback.content
    assert "继续执行尚缺的必要操作，包括保存产物、读回校验和交付" in feedback.content
    assert "在现有权限和工具预算内" in feedback.content
    assert "请求后等待，不猜测输入或擅自继续" in feedback.content
    assert "不将未完成的任务声明为完成" in feedback.content
    assert "停止调用任何工具" not in feedback.content


@pytest.mark.asyncio
@pytest.mark.parametrize("max_steps,reserve,near_step", [
    (12, 10, 3), (300, 10, 291), (6, 10, None),
    (12, 0, None), (12, 1, 12), (1, 10, None),
])
@pytest.mark.parametrize("answer_on_last_step", [True, False])
async def test_final_step_handoff_is_independent_and_does_not_extend_budget(
    max_steps, reserve, near_step, answer_on_last_step,
):
    from box_agent.config import ToolLimitsConfig

    requests = []

    class CapturingLLM:
        async def generate_stream(self, messages, **kwargs):
            requests.append([message.model_copy(deep=True) for message in messages])
            if len(requests) == max_steps and answer_on_last_step:
                yield StreamEvent(type="text", delta="Current status: incomplete")
                yield StreamEvent(type="finish", finish_reason="stop")
            else:
                yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                    ToolCall(id=f"echo-{len(requests)}", type="function",
                             function=FunctionCall(name="echo", arguments={"text": "progress"})),
                ])

    events = await collect(run_agent_loop(
        llm=CapturingLLM(), messages=_msgs(), tools={"echo": EchoTool()},
        max_steps=max_steps, tool_limits=ToolLimitsConfig(general={
            "wrapup_remaining_steps": reserve, "final_summary_after_calls": 512,
        }),
    ))
    assert len(requests) == max_steps
    for marker, expected_step in (("步数预算提醒", near_step), ("最后一步交付提醒", max_steps)):
        injections = [event for event in events if isinstance(event, InjectedMessageEvent)
                      and marker in event.content]
        assert len(injections) == (0 if expected_step is None else 1)
        observed_steps = [index + 1 for index, request in enumerate(requests)
                          if any(marker in str(message.content) for message in request)]
        assert observed_steps == ([] if expected_step is None else list(range(expected_step, max_steps + 1)))
        assert all(not event.user_visible for event in injections)
    feedback = requests[-1][-1]
    assert "最后一步交付提醒" in feedback.content
    assert feedback.source == "runtime"
    assert "停止调用任何工具" in feedback.content
    assert "尚未完成或未验证的部分" in feedback.content
    assert "不将未完成的任务声明为完成" in feedback.content
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason is (StopReason.END_TURN if answer_on_last_step else StopReason.MAX_STEPS)


@pytest.mark.asyncio
async def test_no_progress_reminder_does_not_suppress_final_step_handoff():
    class FailingTool(EchoTool):
        async def execute(self, text: str = ""):
            return ToolResult(success=False, content="", error="unavailable")

    llm = MockLLM([
        LLMResponse(content="", tool_calls=[ToolCall(
            id=f"failed-{index}", type="function",
            function=FunctionCall(name="echo", arguments={"text": "try"}),
        )], finish_reason="tool") for index in range(2)
    ] + [LLMResponse(content="Incomplete: tool unavailable", finish_reason="stop")])
    events = await collect(run_agent_loop(
        llm=llm, messages=_msgs(), tools={"echo": FailingTool()},
        max_steps=3, no_progress_limit=1,
    ))
    reminders = [event.content for event in events if isinstance(event, InjectedMessageEvent)]
    assert len(reminders) == 2
    assert "没有取得有效进展" in reminders[0]
    assert "最后一步交付提醒" in reminders[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for_user", [False, True])
async def test_budget_reminder_allows_required_action_and_respects_waiting(wait_for_user):
    from box_agent.config import ToolLimitsConfig

    class RequiredAction(EchoTool):
        ends_turn_on_success = wait_for_user

    class Preparation(EchoTool):
        @property
        def name(self):
            return "prepare"

    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(
            id=f"action-{index}", type="function",
            function=FunctionCall(name="echo", arguments={"text": "required action"}),
        )], finish_reason="tool")
        for index in range(2)
    ]
    # The first action is preparation; the second happens after the reminder.
    responses[0].tool_calls[0].function.name = "prepare"
    responses.append(LLMResponse(content="delivered", finish_reason="stop"))
    llm = MockLLM(responses)
    events = await collect(run_agent_loop(
        llm=llm, messages=_msgs(),
        tools={"prepare": Preparation(), "echo": RequiredAction()}, max_steps=12,
        tool_limits=ToolLimitsConfig(general={"wrapup_remaining_steps": 11}),
    ))
    reminder_index = next(i for i, event in enumerate(events)
                          if isinstance(event, InjectedMessageEvent)
                          and "步数预算提醒" in event.content)
    action_index = next(i for i, event in enumerate(events)
                        if isinstance(event, ToolCallResult) and event.tool_name == "echo")
    assert reminder_index < action_index
    assert events[action_index].success
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason is (StopReason.WAITING_FOR_USER if wait_for_user else StopReason.END_TURN)
    assert llm._idx == (2 if wait_for_user else 3)
    assert not any(isinstance(event, InjectedMessageEvent)
                   and "最后一步交付提醒" in event.content for event in events)


@pytest.mark.asyncio
async def test_mixed_queue_preserves_order_and_latest_user_request_before_model_call():
    from box_agent.kernel.loop import _latest_user_text

    queue = asyncio.Queue()
    queue.put_nowait({"id": "user-1", "content": "Make it ten pages"})
    queue.put_nowait({"id": "runtime-1", "content": "Tool catalog ready",
                      "source": "runtime", "user_visible": False})
    captured = []

    class CapturingLLM(MockLLM):
        async def generate_stream(self, messages, **kwargs):
            captured.extend(message.model_copy(deep=True) for message in messages)
            async for event in super().generate_stream(messages, **kwargs):
                yield event

    events = await collect(run_agent_loop(
        llm=CapturingLLM([LLMResponse(content="done", finish_reason="stop")]),
        messages=_msgs(), tools={}, max_steps=5, inject_queue=queue,
    ))
    user_message, runtime_message = captured[-2:]
    assert user_message.source == "user"
    assert runtime_message.source == "runtime"
    assert user_message.content.endswith("Make it ten pages")
    assert runtime_message.content.endswith("Tool catalog ready")
    assert _latest_user_text(captured) == user_message.content
    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert [(event.injection_id, event.user_visible) for event in injected] == [
        ("user-1", True), ("runtime-1", False),
    ]
    step = next(index for index, event in enumerate(events) if isinstance(event, StepStart))
    assert all(events.index(event) < step for event in injected)
    assert queue.empty()


@pytest.mark.asyncio
async def test_no_tool_calls_continues_with_injection():
    """When LLM wants to stop but inject queue has content, loop continues."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    class InjectDuringStreamLLM:
        """LLM that injects a message into the queue during its first streaming response."""

        def __init__(self, responses, inject_queue):
            self._responses = list(responses)
            self._idx = 0
            self._queue = inject_queue

        async def generate_stream(self, messages, tools=None, **_):
            resp = self._responses[self._idx]
            self._idx += 1
            if resp.content:
                yield StreamEvent(type="text", delta=resp.content)
            # Inject after first response streaming (simulates user typing during LLM output)
            if self._idx == 1:
                await self._queue.put("follow-up question")
            yield StreamEvent(
                type="finish",
                finish_reason=resp.finish_reason,
                usage=resp.usage,
                tool_calls=resp.tool_calls,
            )

    llm = InjectDuringStreamLLM(
        [
            LLMResponse(content="first reply", finish_reason="stop"),
            LLMResponse(content="after injection", finish_reason="stop"),
        ],
        queue,
    )

    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={}, max_steps=5, inject_queue=queue)
    )

    # With streaming, check DoneEvent for final content
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].final_content == "after injection"

    # Should have had 2 steps (first ended with continue, second is final)
    steps = [e for e in events if isinstance(e, StepStart)]
    assert len(steps) == 2


@pytest.mark.asyncio
async def test_empty_queue_no_effect():
    """Empty inject queue does not change behavior (backward compat)."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={}, max_steps=5, inject_queue=queue)
    )

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN
    assert done[0].final_content == "hello"

    # No injection events
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 0


@pytest.mark.asyncio
async def test_none_queue_no_effect():
    """inject_queue=None (default) works the same as before."""
    msgs = _msgs()
    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={}, max_steps=5, inject_queue=None)
    )

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_multiple_injections_drain_in_order():
    """Multiple queued messages are drained FIFO at step boundary."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    llm = MockLLM([
        LLMResponse(
            content="calling tool",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={"text": "x"}))],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    # Queue multiple messages
    await queue.put("first injection")
    await queue.put("second injection")
    await queue.put("third injection")

    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={"echo": EchoTool()}, max_steps=5, inject_queue=queue)
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 3
    assert injected[0].content == "first injection"
    assert injected[1].content == "second injection"
    assert injected[2].content == "third injection"


@pytest.mark.asyncio
async def test_inject_during_tool_execution():
    """Injection queued during tool execution is picked up at next step boundary."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    class SlowEchoTool(Tool):
        @property
        def name(self):
            return "echo"

        @property
        def description(self):
            return "Slow echo"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"text": {"type": "string"}}}

        async def execute(self, text: str = ""):
            # Simulate injection arriving during tool execution
            await queue.put("injected during tool")
            return ToolResult(success=True, content=f"echo:{text}")

    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={"text": "go"}))],
            finish_reason="tool",
        ),
        LLMResponse(content="final", finish_reason="stop"),
    ])

    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={"echo": SlowEchoTool()}, max_steps=5, inject_queue=queue)
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert injected[0].content == "injected during tool"
