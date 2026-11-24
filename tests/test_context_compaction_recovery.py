"""Summary recovery and pre-output provider overflow stay within bounded retries."""

import asyncio
import json

import pytest

from box_agent.agent import Agent
from box_agent.events import ContentEvent, DoneEvent, ErrorEvent, StepStart, StopReason, SummarizationEvent
from box_agent.kernel import context_engine
from box_agent.kernel.context_types import ContextCompactionState
from box_agent.schema import FunctionCall, Message, StreamEvent, TokenUsage, ToolCall
from box_agent.session_log import SessionLog
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine.contracts import ToolDefinitionView


class _ContextOverflow(Exception):
    status_code = 400
    code = "context_length_exceeded"


class _AuthenticationError(Exception):
    status_code = 401


class _PermissionError(Exception):
    status_code = 403


class _Summary:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.requests = []
        self.request_options = []

    async def generate_stream(self, messages, **kwargs):
        self.requests.append(messages)
        self.request_options.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else ("<summary>Retained evidence.</summary>", "stop")
        if isinstance(outcome, Exception):
            raise outcome
        content, finish, *tool_calls = outcome
        yield StreamEvent(type="text", delta=content)
        yield StreamEvent(type="finish", finish_reason=finish, tool_calls=tool_calls or None)


def _history(rounds=9, size=600):
    history = [Message(role="system", content="system")]
    for index in range(rounds):
        call = ToolCall(id=f"read-{index}", type="function",
                        function=FunctionCall(name="read", arguments={"page": index}))
        history.extend([
            Message(role="user", content=f"Original request {index}"),
            Message(role="assistant", content="", tool_calls=[call]),
            Message(role="tool", tool_call_id=call.id, name="read", content="证据" * size),
        ])
    history.append(Message(role="user", content="Current task"))
    return history


@pytest.mark.asyncio
async def test_summary_local_preflight_trims_whole_rounds_and_notes_unseen_history():
    provider = _Summary()
    history = _history()
    outcome = await context_engine._maybe_summarize(
        provider, history, 20_000, 0, False, force=True, summary_input_token_limit=9_500,
    )
    assert outcome.mode == "summary"
    assert outcome.summary_calls == len(provider.requests) == 1
    assert 1 <= outcome.summary_retries <= 3 and outcome.head_truncated
    request = provider.requests[0]
    assert request[0] is history[0] and request[-2] is history[-1]
    assert request[1].content == context_engine._SUMMARY_TRUNCATION_MARKER
    list(context_engine.tool_message_groups(request))
    assert "Original request 0" not in [message.content for message in request]
    assert "Original request 0" in outcome.messages[1].earlier_user_messages
    summary = next(message for message in outcome.messages if context_engine._is_summary_marker(message))
    assert context_engine._SUMMARY_TRUNCATION_NOTE in summary.content


@pytest.mark.asyncio
@pytest.mark.parametrize("all_fail", [False, True])
async def test_summary_provider_overflow_is_retried_at_most_three_times(all_fail):
    provider = _Summary([_ContextOverflow("Prompt is too long")] * (5 if all_fail else 3))
    history = _history(rounds=16, size=40)
    outcome = await context_engine._maybe_summarize(provider, history, 20_000, 0, False, force=True)
    assert outcome.summary_retries == (4 if all_fail else 3)
    assert outcome.head_truncated
    assert outcome.summary_calls == len(provider.requests) == (5 if all_fail else 4)
    assert outcome.mode == ("fallback" if all_fail else "summary")
    assert outcome.summary_path == ("deterministic" if all_fail else "shared")
    shared = provider.requests[:4]
    assert all(len(previous) > len(nxt) for previous, nxt in zip(shared, shared[1:]))
    assert all(request[0] is history[0] and request[-2] is history[-1] for request in shared)
    assert outcome.messages[1].earlier_user_messages[0] == "Original request 0"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [("<summary>partial", "length"), ("wrong envelope", "stop")])
async def test_bad_summary_output_gets_one_strict_retry(outcome):
    provider = _Summary([outcome] * 3)
    result = await context_engine._maybe_summarize(provider, _history(size=10), 20_000, 0, False, force=True)
    assert result.mode == "fallback"
    assert result.summary_path == "deterministic"
    assert result.summary_retries == 2
    assert result.summary_calls == len(provider.requests) == 3
    assert "4,000 characters" in provider.requests[1][-1].content
    assert not result.head_truncated


def test_summary_instruction_distinguishes_user_turns_and_preserves_security_rules():
    instruction = context_engine._SUMMARY_REQUEST
    assert instruction.startswith("CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.")
    assert "preserve the user's security-relevant instructions and constraints verbatim" in instruction.lower()
    assert "Only actual user-role messages count" in instruction
    assert "'user: ...' or 'Human: ...'" in instruction
    assert "Do not list this request or any earlier summary request as a user message" in instruction
    assert "message still counts as a user message" in instruction
    assert "Quote the recent conversation verbatim" in instruction
    assert instruction.endswith("REMINDER: Do NOT call any tools. Respond with plain text only — a <summary> block. "
                                "Tool calls will be rejected and you will fail the task.")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_response", [
    ("KITE-214", "stop"),
    ("<summary>Tool response.</summary>", "stop", ToolCall(
        id="read-attempt", type="function", function=FunctionCall(name="read", arguments={}),
    )),
])
async def test_summary_retries_instead_of_accepting_latest_instruction_or_tool_call(first_response):
    provider = _Summary([first_response, ("<summary>Retained the latest request.</summary>", "stop")])
    history = _history(size=10)
    history[-1] = Message(role="user", content="最终回复只写 KITE-214")
    result = await context_engine._maybe_summarize(provider, history, 20_000, 0, False, force=True)
    assert result.mode == "summary"
    assert result.summary_calls == 2
    assert all(request[-2] is history[-1] for request in provider.requests)
    retry = provider.requests[1][-1].content
    assert "For this reply only" in retry and "including the newest one" in retry
    assert "Do not answer that message or carry out the work it asks for" in retry
    assert "on how to summarize still apply" in retry and "The user did not write it" in retry
    summary = next(message for message in result.messages if context_engine._is_summary_marker(message))
    assert "Retained the latest request." in summary.content


@pytest.mark.asyncio
@pytest.mark.parametrize("error, count", [
    (_AuthenticationError("Authentication failed"), 1),
    (_PermissionError("Forbidden"), 1),
    (_PermissionError("This model is not supported by your account"), 1),
    (ConnectionError("connection reset"), 3),
])
async def test_summary_only_retries_bare_transport_failures(error, count):
    provider = _Summary([error] * count)
    result = await context_engine._maybe_summarize(provider, _history(size=10), 20_000, 0, False, force=True)
    assert result.mode == "fallback"
    assert result.summary_calls == len(provider.requests) == count
    assert result.summary_retries == count - 1


@pytest.mark.asyncio
async def test_summary_with_sdk_retries_goes_directly_to_independent_request():
    provider = _Summary([ConnectionError("connection reset")])
    provider.retry_config = object()
    result = await context_engine._maybe_summarize(provider, _history(size=10), 20_000, 0, False, force=True)
    assert result.summary_path == "independent"
    assert result.summary_calls == len(provider.requests) == 2
    assert result.summary_retries == 1


@pytest.mark.asyncio
async def test_summary_caps_calls_and_keeps_the_resolved_model_for_every_layer():
    class ReadTool(Tool):
        name = "read_report"
        description = "Read the report."
        parameters = {"type": "object"}

        async def execute(self):
            pytest.fail("Compaction never executes tools")

    capped = _Summary([
        *[_ContextOverflow("Prompt is too long")] * 3,
        ("KITE-214", "stop"), ConnectionError("connection reset"),
    ])
    capped.model = "fixed-summary-model"

    class ResolvedSummary(_Summary):
        model = capped.model
        max_output_tokens = 20_000
        clones = 0

        def for_model(self, model, *, max_output_tokens):
            assert model == self.model and max_output_tokens == 8_192
            self.clones += 1
            return capped

    main = _Summary()
    resolved = ResolvedSummary()
    tools = (ToolDefinitionView.from_tool(ReadTool()),)
    history = _history(rounds=16, size=40)
    result = await context_engine._maybe_summarize(
        main, history, 20_000, 0, False, force=True, summary_llm=resolved, summary_tools=tools,
    )
    assert resolved.clones == 1 and main.requests == resolved.requests == []
    assert result.mode == "summary" and result.summary_path == "independent"
    assert result.summary_calls == len(capped.requests) == 6
    assert result.summary_retries == 5 and result.head_truncated
    assert all(request[0] is history[0] for request in capped.requests[:-1])
    assert all(options["tools"] == list(tools) for options in capped.request_options[:-1])
    independent = capped.requests[-1]
    assert [message.content for message in independent if message.role == "system"] == [
        "You are a helpful AI assistant tasked with summarizing conversations.",
    ]
    assert independent[1:-1] == capped.requests[-2][1:-1]
    assert independent[-1].content.endswith(context_engine._SUMMARY_FORMAT_RETRY)
    assert capped.request_options[-1]["tools"] is None


class _ReactiveProvider:
    model = "reactive-model"
    provider = "openai"
    max_output_tokens = 1_000

    def __init__(self, rejected, *, partial=False, tool_call=None):
        self.rejected = rejected
        self.partial = partial
        self.tool_call = tool_call
        self.summary_provider = _Summary()
        self.main_requests = []

    async def generate_stream(self, messages, *, call_kind="", **kwargs):
        if call_kind == "context_summary":
            async for chunk in self.summary_provider.generate_stream(messages):
                yield chunk
            return
        self.main_requests.append(messages)
        if self.tool_call is not None and len(self.main_requests) == 1:
            yield StreamEvent(type="finish", finish_reason="tool_calls", tool_calls=[self.tool_call])
            return
        if self.rejected:
            self.rejected -= 1
            if self.partial:
                yield StreamEvent(type="text", delta="Partial model output")
            raise _ContextOverflow(
                "This model's maximum context length is 12000 tokens. However, your messages resulted in 13000 tokens."
            )
        yield StreamEvent(type="text", delta="Completed answer.")
        yield StreamEvent(type="finish", finish_reason="stop",
                          usage=TokenUsage(prompt_tokens=1500, completion_tokens=10, total_tokens=1510))


def _agent(tmp_path, provider, *, tools=(), **kwargs):
    return Agent(llm_client=provider, system_prompt="System prompt.", tools=list(tools),
                 workspace_dir=str(tmp_path), token_limit=12_000,
                 deferred_mcp_loading_enabled=False, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected, partial, calls", [(2, False, 3), (3, False, 3), (1, True, 1)])
async def test_reactive_reprojects_in_same_step_and_never_retries_output(tmp_path, rejected, partial, calls):
    log = SessionLog.create(tmp_path / "sessions", session_id="reactive", cwd=tmp_path)
    provider = _ReactiveProvider(rejected, partial=partial)
    agent = _agent(tmp_path, provider, max_steps=1, session_log=log)
    try:
        agent.messages.extend(_history(rounds=9, size=35)[1:])
        events = [event async for event in agent.run_events()]
        assert len(provider.main_requests) == calls
        assert sum(isinstance(event, StepStart) for event in events) == 1
        stop = next(event for event in events if isinstance(event, DoneEvent))
        traced = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        accepted = [entry for entry in traced if entry.get("type") == "assistant/message"
                    and "modelRound" in entry["data"]]
        compactions = [entry for entry in traced if entry.get("type") == "compaction/summary"]
        if not partial:
            assert len(compactions) == 2
            assert len(provider.summary_provider.requests) == 2
            assert all("summaryRetries" in entry["data"] and "headTruncated" in entry["data"]
                       for entry in compactions)
            assert all(entry["data"]["summaryPath"] == "shared" for entry in compactions)
            assert all(event.summary_path == "shared" for event in events if isinstance(event, SummarizationEvent))
            assert all("Current task" in [m.content for m in request]
                       for request in provider.main_requests)
            assert len(provider.main_requests[1]) < len(provider.main_requests[0])
        else:
            assert not compactions
            assert any(isinstance(event, ContentEvent) and event.content == "Partial model output" for event in events)
        if rejected < 3 and not partial:
            assert stop.stop_reason == StopReason.END_TURN
            assert len(accepted) == 1
            assert accepted[0]["data"]["message"]["content"] == "Completed answer."
        else:
            assert stop.stop_reason == StopReason.ERROR
            error = next(event for event in events if isinstance(event, ErrorEvent) and event.is_fatal)
            assert error.error_code == "CONTEXT_INPUT_BUDGET_EXCEEDED"
            assert error.error_details["reactiveCompactionAttempts"] == (0 if partial else 2)
            assert len(accepted) == int(partial)
    finally:
        log.close()


@pytest.mark.asyncio
async def test_reactive_retries_do_not_execute_the_previous_tool_again(tmp_path):
    class CountedTool(Tool):
        name = "counted"
        description = "Count invocations."
        parameters = {"type": "object", "properties": {}}
        calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            return ToolResult(success=True, content="Recorded result.")

    call = ToolCall(id="once", type="function", function=FunctionCall(name="counted", arguments={}))
    provider = _ReactiveProvider(1, tool_call=call)
    tool = CountedTool()
    agent = _agent(tmp_path, provider, tools=[tool], max_steps=2)
    agent.messages.extend(_history(rounds=7, size=40)[1:])
    events = [event async for event in agent.run_events()]
    assert tool.calls == 1
    assert len(provider.main_requests) == 3
    assert [event.step for event in events if isinstance(event, StepStart)] == [1, 2]
    assert [message.tool_call_id for message in agent.messages if message.role == "tool"].count("once") == 1


@pytest.mark.asyncio
async def test_cancelling_a_reactive_summary_keeps_the_uncompacted_surface(tmp_path):
    cancelled = asyncio.Event()

    class CancellingSummary(_Summary):
        async def generate_stream(self, messages, **kwargs):
            async for event in super().generate_stream(messages, **kwargs):
                if event.type == "finish":
                    cancelled.set()
                yield event

    provider = _ReactiveProvider(1)
    provider.summary_provider = CancellingSummary()
    log = SessionLog.create(tmp_path / "sessions", session_id="cancel-reactive", cwd=tmp_path)
    agent = _agent(tmp_path, provider, max_steps=1, session_log=log)
    agent.cancel_event = cancelled
    original = _history(size=40)[1:]
    agent.messages.extend(original)
    try:
        events = [event async for event in agent.run_events()]
        assert len(provider.main_requests) == 1
        assert any(isinstance(event, DoneEvent) and event.stop_reason == StopReason.CANCELLED for event in events)
        assert not any(isinstance(event, SummarizationEvent) for event in events)
        assert log.replay().messages == original
    finally:
        log.close()


def test_same_step_compactions_do_not_count_as_rapid_refills():
    state = ContextCompactionState(step=1)
    for _ in range(3):
        state.record("summary", applied=True)
    assert state.rapid_refills == 0
    state.step += 2
    assert state.refilled_rapidly and state.rapid_refills < 2
    state.record("summary", applied=True)
    assert state.rapid_refills == 1
    assert not state.refilled_rapidly


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_fails", [False, True])
async def test_compaction_circuit_breakers_span_turns(tmp_path, summary_fails):
    provider = _ReactiveProvider(0)
    if summary_fails:
        provider.summary_provider.outcomes = [_AuthenticationError("Authentication failed")] * 3
    agent = _agent(tmp_path, provider, max_steps=1)
    outcomes = []
    for turn in range(4):
        if summary_fails and turn:
            # Give each failed compaction more than three steps to refill.
            for step in range(3):
                agent.add_user_message(f"Small request {turn}-{step}")
                _ = [event async for event in agent.run_events()]
        agent.messages.extend([
            Message(role="assistant", content="材料" * 10_000),
            Message(role="user", content=f"Continue turn {turn}"),
        ])
        outcomes.append([event async for event in agent.run_events()])

    assert len(provider.summary_provider.requests) == 3
    if summary_fails:
        assert all(any(isinstance(event, SummarizationEvent) and event.mode == "fallback"
                       for event in events) for events in outcomes)
        assert not any(isinstance(event, ErrorEvent) and event.is_fatal for event in outcomes[-1])
    else:
        error = next(event for event in outcomes[-1] if isinstance(event, ErrorEvent) and event.is_fatal)
        assert error.error_code == "CONTEXT_COMPACTION_THRASHING"
        assert "分块读取" in error.message
        assert len(provider.main_requests) == 3
