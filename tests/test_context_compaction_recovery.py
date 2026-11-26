"""Summary recovery and pre-output provider overflow stay within bounded retries."""

import asyncio
import json
from contextlib import closing

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
    assert all(message in request for message in history if message.role == "user")
    assert history[2] not in request and history[3] not in request
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
    if all_fail:
        assert outcome.messages[-1].runtime_kind == "compaction_notice"
    else:
        assert outcome.messages[1].earlier_user_messages[0] == "Original request 0"


@pytest.mark.asyncio
@pytest.mark.parametrize(("content", "finish"), [
    ("<summary>First detail.\nSecond detail.</summary>", "length"),
    ("Preamble.\n<summary>First detail.\nSecond detail.</summary>\nUnfinished postscript", "max_tokens"),
])
async def test_output_limit_keeps_one_complete_summary_without_retry(content, finish):
    provider = _Summary([(content, finish)])
    result = await context_engine._maybe_summarize(provider, _history(size=10), 20_000, 0, False, force=True)
    assert result.mode == "summary"
    assert result.summary_calls == len(provider.requests) == 1
    assert result.summary_retries == 0
    summary = next(message for message in result.messages if context_engine._is_summary_marker(message))
    assert summary.content.startswith(
        context_engine._SUMMARY_MESSAGE_PREFIX
        + "First detail.\nSecond detail."
        + context_engine._SUMMARY_MESSAGE_SUFFIX
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    ("<summary>partial", "length"),
    ("partial</summary>", "max_tokens"),
    ("<summary>  </summary>", "length"),
    ("<summary>first</summary><summary>second</summary>", "max_tokens"),
    ("<summary>first</summary><summary>partial", "length"),
    ("wrong envelope", "stop"),
])
async def test_bad_summary_output_retries_by_failure_type_then_uses_independent_request(outcome):
    provider = _Summary([outcome] * 5)
    provider.max_output_tokens = 2_048
    result = await context_engine._maybe_summarize(provider, _history(size=10), 20_000, 0, False, force=True)
    assert result.mode == "fallback" and result.summary_path == "deterministic"
    assert result.summary_retries == 4 and result.summary_calls == len(provider.requests) == 5
    first, *retries, independent = provider.requests
    assert first[-1].content == context_engine._summary_request(2_048)
    for retry in retries:
        assert retry[:-1] == first[:-1]
        if outcome[0] in ("<summary>partial", "partial</summary>"):
            assert "max_tokens limit of 2048" in retry[-1].content and "within 2048 tokens" in retry[-1].content
        else:
            assert retry[-1].content.endswith(context_engine._SUMMARY_FORMAT_RETRY)
    assert independent[-1].content.endswith(context_engine._SUMMARY_FORMAT_RETRY)
    assert all(options["max_output_tokens"] == 2_048 for options in provider.request_options)
    assert independent[0].content == context_engine._INDEPENDENT_SUMMARY_SYSTEM
    assert not result.head_truncated


def test_summary_instruction_distinguishes_user_turns_and_preserves_security_rules():
    instruction = context_engine._summary_request(2_048)
    assert instruction.startswith("Create a detailed continuation summary of the conversation above")
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
    ("<summary>Tool response.</summary>", "length", ToolCall(
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
@pytest.mark.parametrize("status", [400, 401, 403, 429, 503, None])
async def test_summary_service_failure_ends_turn_with_main_request_error_and_preserves_history(tmp_path, status):
    from box_agent.llm.error_messages import client_error_details
    from box_agent.runtime import run_agent_loop

    exc = ConnectionError("connection reset") if status is None else RuntimeError(f"HTTP {status}")
    exc.status_code = status
    exc.code = "provider_error"
    exc.request_id = "summary-request-id"
    class Provider(_Summary):
        async def generate(self, *args, **kwargs):
            pytest.fail("Summary requests must stream")

    provider = Provider([exc] * 2)
    provider.retry_config = object()
    provider.model = "summary-route"
    provider.context_window = 100_000
    history = _history(size=800)
    result = await context_engine._maybe_summarize(provider, history, 5_000, 0, False)
    assert result.mode == "error" and result.messages is None and result.error_code == exc.code
    assert result.summary_calls == 1 and result.summary_retries == 0 and result.summary_path == "shared"
    assert result.provider_error.error_details == client_error_details(exc, provider)
    original = list(history)
    log = SessionLog.create(tmp_path / "sessions", session_id="provider-error", cwd=tmp_path)
    try:
        events = [e async for e in run_agent_loop(
            llm=_Summary(), summary_llm=provider, messages=history, token_limit=5_000, tools={},
            session_log=log, session_turn=1, max_steps=1,
        )]
        (error,) = [e for e in events if isinstance(e, ErrorEvent)]
        assert error.error_details == result.provider_error.error_details and error.exception is exc
        assert error.error_category == result.provider_error.error_category and error.error_code == exc.code
        assert events[-1].stop_reason == StopReason.ERROR
        assert len(provider.requests) == 2
        assert history == original and log.replay().messages == original[1:]
        log_text = log.path.read_text(encoding="utf-8")
        assert '"compaction/start"' in log_text and '"compaction/end"' not in log_text
    finally:
        log.close()


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
        ("KITE-214", "stop"),
    ])
    capped.model = "fixed-summary-model"
    capped.max_output_tokens = 20_000

    class ResolvedSummary(_Summary):
        model = capped.model
        max_output_tokens = 20_000
        clones = 0

        def for_model(self, model, *, max_output_tokens):
            assert model == self.model and max_output_tokens == self.max_output_tokens
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
    assert result.summary_calls == len(capped.requests) == 5
    assert result.summary_retries == 4 and result.head_truncated
    assert all(request[0] is history[0] for request in capped.requests[:-1])
    assert all(options["tools"] == list(tools) for options in capped.request_options[:-1])
    assert resolved.max_output_tokens == 20_000
    assert all(options["max_output_tokens"] == resolved.max_output_tokens for options in capped.request_options)
    assert all("within 20000 tokens, including the closing </summary> tag" in request[-1].content
               for request in capped.requests)
    independent = capped.requests[-1]
    assert [message.content for message in independent if message.role == "system"] == [
        "You are a helpful AI assistant tasked with summarizing conversations.",
    ]
    assert independent[1:-1] == capped.requests[-2][1:-1]
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


def _agent(tmp_path, provider, *, tools=(), token_limit=12_000, **kwargs):
    return Agent(llm_client=provider, system_prompt="System prompt.", tools=list(tools),
                 workspace_dir=str(tmp_path), token_limit=token_limit,
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
async def test_fallback_keeps_the_latest_complete_group_and_omits_large_older_output():
    history = _history(rounds=3, size=20)
    history[-5].content = "large result" * 20_000
    result = await context_engine._maybe_summarize(
        _Summary([("bad format", "stop")] * 5), history, 30_000, 0, False, force=True,
    )
    assert result.mode == "fallback"
    assert result.messages[:-1] == [history[0], *history[-4:]]
    assert result.messages[-1].runtime_kind == "compaction_notice"
    assert not any(context_engine._is_summary_marker(m) for m in result.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_fails", [False, True])
async def test_compaction_circuit_breakers_span_turns(tmp_path, summary_fails):
    provider = _ReactiveProvider(0)
    if summary_fails:
        provider.summary_provider.outcomes = [("malformed", "stop")] * 15
    agent = _agent(tmp_path, provider, token_limit=20_000, max_steps=1)
    outcomes = []
    for turn in range(4):
        if summary_fails and turn:
            # Give each failed compaction more than three steps to refill.
            for step in range(3):
                agent.add_user_message(f"Small request {turn}-{step}")
                _ = [event async for event in agent.run_events()]
        agent.messages.extend([
            Message(role="assistant", content="材料" * 24_000),
            Message(role="user", content=f"Continue turn {turn}"),
        ])
        outcomes.append([event async for event in agent.run_events()])

    assert len(provider.summary_provider.requests) == sum(
        e.summary_calls for events in outcomes for e in events if isinstance(e, SummarizationEvent)
    )
    if summary_fails:
        assert all(any(isinstance(event, SummarizationEvent) and event.mode == "fallback"
                       for event in events) for events in outcomes)
        assert not any(isinstance(event, ErrorEvent) and event.is_fatal for event in outcomes[-1])
    else:
        error = next(event for event in outcomes[-1] if isinstance(event, ErrorEvent) and event.is_fatal)
        assert error.error_code == "CONTEXT_COMPACTION_THRASHING"
        assert "分块读取" in error.message
        assert len(provider.main_requests) == 3


@pytest.mark.parametrize("ratio", [1.0, 1.4])
async def test_each_successful_request_gets_a_new_reactive_budget_in_provider_units(ratio):
    from box_agent.context_input import DefaultContextEngine
    from box_agent.runtime import run_agent_loop

    class Echo(Tool):
        name = "echo"
        description = "Echo a receipt."
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ToolResult(success=True, content="receipt")

    class Provider(_Summary):
        main_calls = 0
        max_output_tokens = 1_000

        async def generate_stream(self, messages, *, call_kind="", **kwargs):
            if call_kind == "context_summary":
                async for chunk in super().generate_stream(messages, **kwargs):
                    yield chunk
                return
            self.main_calls += 1
            if self.main_calls % 2:
                raise _ContextOverflow("maximum input length is 12000 tokens")
            if self.main_calls < 6:
                call = ToolCall(id=f"echo-{self.main_calls}", type="function",
                                function=FunctionCall(name="echo", arguments={"value": self.main_calls}))
                yield StreamEvent(type="finish", finish_reason="tool_calls", tool_calls=[call])
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")

    class Context(DefaultContextEngine):
        def prepare_request(self, messages, **kwargs):
            limits.append(kwargs["token_limit"])
            return super().prepare_request(messages, **kwargs)

    llm = Provider()
    limits = []
    context = Context(llm=llm)
    context.token_calibration.record(llm, int(ratio * 100), 100)
    events = [event async for event in run_agent_loop(
        llm=llm, messages=_history(rounds=7, size=40), tools={"echo": Echo()},
        max_steps=3, token_limit=20_000, context_engine=context,
    )]
    assert events[-1].stop_reason == StopReason.END_TURN, events[-1].final_content
    assert llm.main_calls == 6 and len(llm.requests) == 3
    assert limits.count(12_000 - 1_000 - context_engine.REQUEST_INPUT_HEADROOM_TOKENS) == 3


async def test_third_summary_request_still_contains_the_fact_from_the_first_summary():
    fact = "KITE-3519"
    history = _history(size=15)
    for cycle in range(3):
        provider = _Summary([*([_ContextOverflow("prompt too long")] if cycle else []),
                             (f"<summary>Preserve {fact}</summary>", "stop")])
        result = await context_engine._maybe_summarize(provider, history, 20_000, 0, False, force=True)
        assert result.mode == "summary"
        for request in provider.requests:
            assert any(fact in str(message.content) for message in request) == bool(cycle)
        history = [*result.messages, *_history(rounds=7, size=10)[1:]]


async def _fallback(history, *, log=None, limit=50_000, llm=None):
    provider = llm or _Summary()
    provider.outcomes = [("malformed", "stop")] * 5
    return await context_engine._maybe_summarize(
        provider, history, limit, 0, False, force=True, session_log=log,
    )


async def test_fallback_summary_anchor_is_byte_identical_and_repeated_notices_keep_omitted_seqs(tmp_path):
    system = Message(role="system", content="system")
    log = SessionLog.create(tmp_path, session_id="notice", cwd=tmp_path)
    try:
        summary = Message(role="user", source="runtime", content=context_engine._SUMMARY_MESSAGE_PREFIX + "KITE-92")
        dropped = Message(role="user", content="obsolete")
        work = _history(rounds=1, size=10)[2:4]
        history = [system, dropped, summary, *work]
        log.append_unlogged_messages(history[1:], turn=1, step=1)
        log.flush()
        seq = log.surface_message_sequence(history[1:], dropped)
        first = await _fallback(history, log=log)
        assert first.mode == "fallback" and first.messages[:-1] == [system, summary, *work]
        assert first.messages[1] is summary
        notice = first.messages[-1]
        assert notice.runtime_kind == "compaction_notice" and notice.source == "runtime"
        assert f"seq ranges: {seq}." in notice.content and str(log.path) in notice.content
        assert "Only if continuing the current task requires" in notice.content
        assert notice.model_content().startswith("[Host runtime reference source=runtime kind=compaction_notice]")
        log.replace_surface(first.messages[1:], turn=1, step=2)
        log.flush()
        request = Message(role="user", content="newest task")
        tail = Message(role="assistant", content="work after the latest request")
        second_history = [system, *log.replay().messages, request, tail]
        log.append_unlogged_messages(second_history[1:], turn=2, step=1)
        log.flush()
        second = await _fallback(second_history, log=log)
        assert second.messages[:-1] == [system, request, tail]
        assert second.messages[-1].content.count("could not generate a summary") == 1
        assert second.messages[-1].content.startswith(notice.content.split("seq ranges:")[0])
        assert str(seq) in second.messages[-1].content.split("seq ranges: ")[1].split(".")[0].split(", ")
        assert not context_engine._is_real_user_message(second.messages[-1])
    finally:
        log.close()


@pytest.mark.parametrize("over", [0, 1])
async def test_fallback_takes_a_contiguous_suffix_at_exactly_12000_local_tokens(over):
    class Counter(_Summary):
        estimate_text_tokens = staticmethod(len)

    llm = Counter()
    anchor = Message(role="user", content="task")
    old = Message(role="assistant", content="skip")
    call = _history(rounds=1, size=0)[2:4]
    call[1].content = "x" * (12_000 - context_engine._fallback_context_estimate(call, None, llm=llm) + over)
    history = [Message(role="system", content="system"), anchor, old, *call]
    outcome = await _fallback(history, llm=llm)
    assert outcome.messages[:-1] == ([history[0], anchor] if over else [history[0], anchor, *call])
    assert "session.jsonl" not in outcome.messages[-1].content and "query_jsonl" not in outcome.messages[-1].content


async def test_fallback_drops_interactions_then_older_archived_users_and_preserves_latest():
    latest = "KEEP\n" + "汉" * 3_000
    history = [Message(role="system", content="system"), Message(role="user", content="old-" * 1200),
               Message(role="user", content=latest), *(Message(role="assistant", content=f"step {i}") for i in range(7))]
    compacted = await context_engine._maybe_summarize(_Summary(), history, 50_000, 0, False, force=True)
    full = await _fallback(compacted.messages, limit=1)
    assert full.blocked
    assert full.degradation["droppedRounds"] == 5 and full.degradation["droppedEarlierUsers"] == 1
    assert "old-old-" not in full.messages[1].content
    assert "Retained evidence." in full.messages[1].content and latest in full.messages[1].content
    assert full.messages[-1].runtime_kind == "compaction_notice"


async def test_fallback_without_omissions_records_latest_anchor_with_no_notice():
    history = _history(rounds=1, size=10)
    outcome = await _fallback(history)
    assert outcome.mode == "fallback" and outcome.messages == history


async def test_fallback_reports_omission_without_directing_an_unbounded_log_search(tmp_path):
    from contextlib import closing

    history = _history(rounds=4, size=10)
    with closing(SessionLog.create(tmp_path, session_id="unmapped", cwd=tmp_path)) as log:
        outcome = await _fallback(history, log=log)
        notice = outcome.messages[-1]
        assert notice.runtime_kind == "compaction_notice" and "omitted from context" in notice.content
        assert str(log.path) not in notice.content and "query_jsonl" not in notice.content


async def test_previous_anchor_and_its_groups_share_one_local_fallback_allowance():
    class Counter(_Summary):
        estimate_text_tokens = staticmethod(len)

    llm = Counter()
    previous = Message(role="user", content="previous" * 500)
    last = Message(role="user", content="new")
    history = [Message(role="system", content="system"), previous, Message(role="assistant", content="A" * 5_000),
               Message(role="assistant", content="B" * 6_000), last]
    result = await _fallback(history, llm=llm)
    assert result.messages[:-1] == [history[0], previous, history[3], last]
    assert context_engine._fallback_context_estimate(result.messages[1:-2], {}, llm=llm) <= 12_000
    tight = await _fallback(history, llm=llm, limit=2_000)
    assert tight.messages[:-1] == [history[0], last] and tight.mode == "fallback"


async def test_fallback_logs_the_notice_or_latest_anchor_before_reopening(tmp_path):
    from box_agent.runtime import run_agent_loop

    class Provider(_Summary):
        context_window = 80_000
        max_output_tokens = 1_000

        async def generate_stream(self, messages, *, call_kind="", **kwargs):
            if call_kind == "context_summary":
                yield StreamEvent(type="text", delta="bad format")
            else:
                yield StreamEvent(type="text", delta="resumed")
            yield StreamEvent(type="finish", finish_reason="stop")

    for omit in (False, True):
        sid = f"fallback-{omit}"
        log = SessionLog.create(tmp_path, session_id=sid, cwd=tmp_path)
        history = _history(rounds=2 if omit else 1, size=10)
        history[-3].usage = TokenUsage(total_tokens=15_000)
        events = [e async for e in run_agent_loop(
            llm=Provider(), tools={}, messages=history, token_limit=10_000, session_log=log, session_turn=1, max_steps=1,
        )]
        assert events[-1].stop_reason == StopReason.END_TURN
        log.close()
        with closing(SessionLog.open(tmp_path, session_id=sid, cwd=tmp_path)) as reopened:
            (record,) = [e["data"] for e in reopened.events if e["type"] == "compaction/summary"]
            assert (record["mode"], record["summaryPath"]) == ("fallback", "deterministic")
            assert (record["message"].get("runtime_kind") == "compaction_notice") == omit
            if not omit:
                assert record["message"]["content"] == "Current task"
            assert reopened.replay().messages == history[1:]
