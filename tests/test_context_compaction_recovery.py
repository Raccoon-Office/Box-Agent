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
        attempts = [e for e in log.events if e["type"].startswith("compaction/")]
        assert [e["type"] for e in attempts] == ["compaction/start", "compaction/end"]
        assert all(e["data"]["format"] == 2 for e in attempts)
        assert attempts[-1]["data"]["stage"] == "summary" and attempts[-1]["data"]["error"]
        assert not any("surfaceOp" in e for e in log.events[attempts[0]["seq"] + 1:])
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
        # Leave enough removable history for two progressively smaller targets.
        agent.messages.extend(_history(rounds=9, size=100)[1:])
        events = [event async for event in agent.run_events()]
        assert len(provider.main_requests) == calls
        assert sum(isinstance(event, StepStart) for event in events) == 1
        stop = next(event for event in events if isinstance(event, DoneEvent))
        traced = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        accepted = [entry for entry in traced if entry.get("type") == "assistant/message"
                    and "modelRound" in entry["data"]]
        compactions = [entry for entry in traced if entry.get("type") == "compaction/end"]
        if not partial:
            assert len(compactions) == 2
            assert len(provider.summary_provider.requests) == 2
            assert all("summaryRetries" in entry["data"] and "headTruncated" in entry["data"]
                       for entry in compactions)
            assert all(entry["data"]["summaryPath"] == "shared" for entry in compactions)
            assert all(event.summary_path == "shared" for event in events if isinstance(event, SummarizationEvent))
            assert all(any(m.content == "Current task" or "Current task" in (m.earlier_user_messages or ())
                           for m in request) for request in provider.main_requests)
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
        state.record()
    assert state.rapid_refills == 0
    state.step += 2
    assert state.refilled_rapidly and state.rapid_refills < 2
    state.record()
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
    assert result.messages[1].compaction_node == "fallback_head"
    assert result.messages[2:-1] == history[-4:]
    assert result.messages[-1].runtime_kind == "compaction_notice"
    assert not any(context_engine._is_summary_marker(m) for m in result.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_fails", [False, True])
async def test_fourth_compaction_uses_summary_after_failures_and_stops_only_for_rapid_refills(tmp_path, summary_fails):
    provider = _ReactiveProvider(0)
    if summary_fails:
        provider.summary_provider.outcomes = [("malformed", "stop")] * 12
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
        assert [e.mode for events in outcomes for e in events if isinstance(e, SummarizationEvent)] == [
            "fallback", "fallback", "fallback", "summary",
        ]
        assert len(provider.summary_provider.requests) == 13
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


async def _fallback(history, *, log=None, limit=65_536, llm=None):
    provider = llm or _Summary()
    provider.outcomes = [("malformed", "stop")] * 5
    return await context_engine._maybe_summarize(
        provider, history, limit, 0, False, force=True, session_log=log,
    )


async def test_fallback_embeds_legacy_summary_and_repeated_notices_keep_structured_omissions(tmp_path):
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
        assert first.mode == "fallback" and first.messages[2:-1] == work
        assert first.plan.cut_seq == work[0]._surface_seq
        assert first.plan.head.content.endswith(summary.content)
        assert first.plan.head.omitted_ranges == [(seq, seq)]
        notice = first.messages[-1]
        assert notice.runtime_kind == "compaction_notice" and notice.source == "runtime"
        assert f"seq ranges: {seq}." in notice.content and str(log.path) in notice.content
        assert "Only if continuing the current task requires" in notice.content
        assert notice.model_content().startswith("[Host runtime reference source=runtime kind=compaction_notice]")
        # Even a notice rendered in another language must carry the same omissions forward.
        notice.content = "Omitted session log seq ranges: 999999."
        first.plan.head.content = "Localized fallback head"
        log.commit_compaction(first.plan, turn=1, step=2)
        log.flush()
        request = Message(role="user", content="newest task")
        tail = Message(role="assistant", content="work after the latest request")
        second_history = [system, *log.replay().messages, request, tail]
        log.append_unlogged_messages(second_history[1:], turn=2, step=1)
        log.flush()
        second = await _fallback(second_history, log=log)
        assert second.messages[2:-1] == [request, tail]
        assert second.plan.cut_seq == request._surface_seq
        assert second.messages[-1].content.count("could not generate a summary") == 1
        assert any(lo <= seq <= hi for lo, hi in second.plan.head.omitted_ranges)
        assert second.messages[-1].omitted_ranges == second.plan.head.omitted_ranges
        assert "999999" not in second.messages[-1].content
        assert not context_engine._is_real_user_message(second.messages[-1])
    finally:
        log.close()


async def test_fallback_cannot_retain_rounds_before_previous_compaction_appends(tmp_path):
    system = Message(role="system", content="system")
    primary = Message(role="user", content="Continue")
    original = Message(role="user", content="Read diagram", input_facts={
        "context": {"files": [{"name": "diagram.png", "path": "/ws/diagram.png", "mimeType": "image/png"}]},
    })
    old_round = _history(rounds=1, size=10)[2:4]
    state = Message(role="user", source="runtime", compaction_node="appended", content="previous state")
    with closing(SessionLog.create(tmp_path, session_id="fallback-cut", cwd=tmp_path)) as log:
        log.append_unlogged_messages([original, *old_round, state, primary], turn=1, step=1)
        history = [system, *log.replay().messages]
        result = await _fallback(history, log=log, llm=_FallbackCounter())
        assert result.plan.cut_seq == primary._surface_seq
        assert result.plan.head.content.endswith(
            "Read diagram\nAttachments: image diagram.png → /ws/diagram.png (re-open with inspect_images if needed)"
        )
        assert result.plan.head.omitted_ranges == [(old_round[0]._surface_seq, state._surface_seq)]
        assert result.messages[2:-1] == [primary]
        log.commit_compaction(result.plan, turn=1, step=2)
        log.flush()
        assert log.replay().messages[1]._surface_seq == primary._surface_seq


async def test_fallback_reuses_structured_summary_and_user_after_both_cross_the_cut(tmp_path):
    system = Message(role="system", content="system")
    user = Message(role="user", content="Latest original")
    state = Message(role="user", source="runtime", compaction_node="appended", content="previous state")
    with closing(SessionLog.create(tmp_path, session_id="mixed-anchor", cwd=tmp_path)) as log:
        hint = context_engine._session_log_hint(log, None)
        summary = context_engine._summary_with_users("BODY", ["hidden", "visible"], [], True, hint)
        # The renderer must use the stored body and visibility, even if an old
        # client changed how it presented that information.
        summary.content = "stale translated rendering"
        log.append_unlogged_messages([summary, user, state], turn=1, step=1)
        history = [system, *log.replay().messages]
        first = await _fallback(history, log=log, llm=_FallbackCounter())
        assert first.plan.head.fallback_user_count == 1
        assert first.plan.head.earlier_user_messages == ["hidden", "visible", user.content]
        assert first.plan.head.content.endswith(user.content)
        assert "BODY" in first.plan.head.content and "2. visible" in first.plan.head.content
        assert "hidden" not in first.plan.head.content and "stale" not in first.plan.head.content
        log.commit_compaction(first.plan, turn=1, step=2)
        log.flush()
        second = await _fallback([system, *log.replay().messages], log=log, llm=_FallbackCounter())
        assert second.plan.head.content == first.plan.head.content
        assert second.plan.head.omitted_ranges == first.plan.head.omitted_ranges


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
    assert outcome.plan.head.earlier_user_messages == [anchor.content]
    assert outcome.messages[2:-1] == ([] if over else call)
    assert outcome.plan.cut_index == (len(history) if over else 3)
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


async def test_fallback_embeds_a_leading_anchor_to_make_a_nonempty_replace_without_a_notice():
    history = _history(rounds=1, size=10)
    outcome = await _fallback(history)
    assert outcome.mode == "fallback"
    assert outcome.plan.cut_index == 2 and outcome.plan.head.earlier_user_messages == [history[1].content]
    assert outcome.messages[2:] == history[2:] and not outcome.plan.appends


async def test_fallback_reports_omission_without_directing_an_unbounded_log_search(tmp_path):
    from contextlib import closing

    history = _history(rounds=4, size=10)
    with closing(SessionLog.create(tmp_path, session_id="unmapped", cwd=tmp_path)) as log:
        outcome = await _fallback(history, log=log)
        notice = outcome.messages[-1]
        assert notice.runtime_kind == "compaction_notice" and "omitted from context" in notice.content
        assert str(log.path) not in notice.content and "query_jsonl" not in notice.content


class _FallbackCounter(_Summary):
    context_window = 65_536
    estimate_text_tokens = staticmethod(len)


@pytest.mark.parametrize("previous_is_summary", [True, False], ids=["summary", "user"])
async def test_fallback_keeps_a_large_previous_anchor_outside_the_interaction_allowance(tmp_path, previous_is_summary):
    llm = _FallbackCounter()
    if previous_is_summary:
        previous = context_engine._summary_with_users("S" * 6_000, ["U" * 9_000], [], True, "")
    else:
        previous = Message(role="user", content="P" * 13_000)
    system = Message(role="system", content="system")
    primary = Message(role="user", content="继续")
    work_history = _history(rounds=3, size=0)
    old, middle, recent = (work_history[start:start + 2] for start in (2, 5, 8))
    for group, size in zip((old, middle, recent), (2_000, 5_000, 6_000)):
        group[-1].content = "X" * size
    history = [system, previous, *old, *middle, *recent, primary]
    assert context_engine._fallback_context_estimate([previous], None, llm=llm) > 12_000
    assert context_engine._fallback_context_estimate([*middle, *recent], None, llm=llm) <= 12_000
    with closing(SessionLog.create(tmp_path, session_id="previous-anchor", cwd=tmp_path)) as log:
        log.append_unlogged_messages(history[1:], turn=1, step=1)
        log.flush()
        first, last = (log.surface_message_sequence(history[1:], message) for message in old)
        result = await _fallback(history, llm=llm, log=log)
        assert result.mode == "fallback" and result.degradation is None
        assert result.messages[2:-1] == [*middle, *recent, primary]
        assert previous.content in result.plan.head.content
        assert result.plan.cut_seq == middle[0]._surface_seq
        notice = result.messages[-1]
        assert notice.runtime_kind == "compaction_notice"
        assert f"seq ranges: {first}-{last}." in notice.content and str(log.path) in notice.content


def _expected_fallback_head(embedded_text):
    content = "Earlier conversation context was omitted."
    if embedded_text is not None:
        content += "\n\n" + embedded_text
    return Message(role="user", source="runtime", content=content)


async def test_fallback_drops_previous_summary_users_in_order_before_dropping_its_body(tmp_path):
    llm = _FallbackCounter()
    system = Message(role="system", content="system")
    primary = Message(role="user", content="继续")
    older = Message(role="assistant", content="A" * 4_000)
    newer = Message(role="assistant", content="B" * 6_000)
    texts = ["early original " * 200, "latest original " * 250]
    body = "summary fact " * 450
    with closing(SessionLog.create(tmp_path, session_id="previous-summary", cwd=tmp_path)) as log:
        hint = context_engine._session_log_hint(log, None)
        entries = context_engine._earlier_user_entries(texts[:-1], len, 10_000)
        previous = context_engine._summary_with_users(body, texts, entries, True, hint)
        latest_only = context_engine._summary_with_users(body, texts, [], True, hint)
        body_only = context_engine._summary_with_users(body, [], [], False, hint)
        history = [system, previous, older, newer, primary]
        log.append_unlogged_messages(history[1:], turn=1, step=1)
        log.flush()
        anchor_seq, old_seq, new_seq, _ = log.surface_sequences(history[1:])
        cases = [
            ([previous, newer], (old_seq, old_seq), 1, 0),
            ([previous], (old_seq, new_seq), 2, 0),
            ([latest_only], (anchor_seq, new_seq), 2, 1),
            ([body_only], (anchor_seq, new_seq), 2, 2),
            ([], (anchor_seq, new_seq), 2, 2),
        ]
        for retained, omitted, dropped_rounds, dropped_users in cases:
            notice = context_engine._omission_notice([omitted], log.path)
            expected_head = _expected_fallback_head(retained[0].content if retained else None)
            expected = [system, expected_head, *retained[1:], primary, notice]
            limit = context_engine._fallback_context_estimate(expected, None, llm=llm)
            result = await _fallback(history, llm=llm, log=log, limit=limit)
            assert result.mode == "fallback"
            assert [message.content for message in result.messages] == [message.content for message in expected]
            assert result.messages[-2] is primary
            assert result.degradation == {
                "droppedRounds": dropped_rounds, "droppedEarlierUsers": dropped_users,
                "droppedSkillBodies": 0, "droppedRuntimeState": [],
            }
            if retained:
                assert result.messages[1].earlier_user_messages == texts


async def test_fallback_drops_previous_real_user_as_a_whole_after_interactions():
    llm = _FallbackCounter()
    system = Message(role="system", content="system")
    previous = Message(role="user", content="P" * 13_000)
    work = Message(role="assistant", content="W" * 6_000)
    primary = Message(role="user", content="继续")
    history = [system, previous, work, primary]
    notice = context_engine._omission_notice([], None)
    for kept, dropped_users in (([previous], 0), ([], 1)):
        expected_head = _expected_fallback_head(kept[0].content if kept else None)
        expected = [system, expected_head, primary, notice]
        limit = context_engine._fallback_context_estimate(expected, None, llm=llm)
        result = await _fallback(history, llm=llm, limit=limit)
        assert result.mode == "fallback"
        assert [m.content for m in result.messages] == [m.content for m in expected]
        assert result.messages[-2] is primary
        assert result.degradation == {
            "droppedRounds": 1, "droppedEarlierUsers": dropped_users,
            "droppedSkillBodies": 0, "droppedRuntimeState": [],
        }


async def test_fallback_commits_one_prefix_replace_and_only_appends_a_notice_on_omission(tmp_path):
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
            events = reopened.events
            start = next(i for i, event in enumerate(events) if event["type"] == "compaction/start")
            stop = next(i for i, event in enumerate(events) if event["type"] == "compaction/end")
            changes = events[start + 1:stop]
            assert [event["type"] for event in changes] == ["user/message"] * (1 + int(omit))
            assert changes[0]["data"]["compaction_node"] == "fallback_head"
            assert changes[0]["surfaceOp"]["op"] == "replace"
            if omit:
                assert changes[-1]["data"]["runtime_kind"] == "compaction_notice"
                assert changes[-1]["surfaceOp"] == "append"
            assert events[stop]["data"]["mode"] == "fallback"
            assert events[stop]["data"]["summaryPath"] == "deterministic"
            assert reopened.replay().messages == history[1:]


async def test_second_kernel_fallback_covers_previous_head_and_appends_and_keeps_one_original_suffix(tmp_path):
    from box_agent.runtime import run_agent_loop

    class Provider(_Summary):
        context_window = 128_000
        max_output_tokens = 2_048

        async def generate_stream(self, messages, *, call_kind="", **kwargs):
            if call_kind == "context_summary":
                yield StreamEvent(type="text", delta="malformed")
                yield StreamEvent(type="finish", finish_reason="stop")
            else:
                yield StreamEvent(type="text", delta="reply")
                yield StreamEvent(type="finish", finish_reason="stop", usage=TokenUsage(total_tokens=100_000))

    history = _history(rounds=2, size=10)[:-1]
    old_call, old_result = history[-2:]
    old_call.usage = TokenUsage(total_tokens=100_000)
    with closing(SessionLog.create(tmp_path, session_id="twice", cwd=tmp_path)) as log:
        provider = Provider()
        for turn in (1, 2):
            if turn == 2:
                history.append(Message(role="user", content="Continue"))
            events = [e async for e in run_agent_loop(
                llm=provider, messages=history, tools={}, session_log=log,
                session_turn=turn, token_limit=65_536, max_steps=1,
            )]
            assert events[-1].stop_reason == StopReason.END_TURN
            assert [e.mode for e in events if isinstance(e, SummarizationEvent)] == ["fallback"]

        first, second = [e for e in log.events if e["type"] == "compaction/end"]
        first_surface = log.replay(through_seq=first["seq"]).messages
        assert [m._surface_seq for m in first_surface[-3:-1]] == [old_call._surface_seq, old_result._surface_seq]
        assert first_surface[-1].compaction_node == "appended"
        start2, = [e for e in log.events[first["seq"] + 1:second["seq"]] if e["type"] == "compaction/start"]
        before = log.replay(through_seq=start2["seq"]).messages
        after = log.replay(through_seq=second["seq"]).messages
        edits = [e for e in log.events[start2["seq"] + 1:second["seq"]] if "surfaceOp" in e]
        head, notice = edits
        assert head["surfaceOp"] == {"op": "replace", "start": first_surface[0]._surface_seq,
                                     "end": first_surface[-1]._surface_seq}
        assert head["sourceEventSeqs"] == [m._surface_seq for m in first_surface]
        assert head["data"]["earlier_user_messages"] == ["Original request 1"]
        assert notice["surfaceOp"] == "append" and notice["data"]["runtime_kind"] == "compaction_notice"
        tail = before[len(first_surface):]
        assert [m.content for m in tail] == ["reply", "Continue"]
        assert all(m.compaction_node is None for m in tail)
        assert second["data"]["cutSeq"] == tail[0]._surface_seq
        assert [m._surface_seq for m in after[1:-1]] == [m._surface_seq for m in tail]
        omitted = after[-1].omitted_ranges
        assert any(lo <= old_call._surface_seq <= old_result._surface_seq <= hi for lo, hi in omitted)


async def test_step_after_fallback_can_run_a_new_summary_in_the_same_turn():
    from box_agent.runtime import run_agent_loop

    class Provider(_Summary):
        context_window = 80_000
        max_output_tokens = 1_000
        main_calls = 0

        async def generate_stream(self, messages, *, call_kind="", **kwargs):
            if call_kind == "context_summary":
                async for event in super().generate_stream(messages, **kwargs):
                    yield event
                return
            self.main_calls += 1
            if self.main_calls == 1:
                call = ToolCall(id="read-count", type="function", function=FunctionCall(name="count", arguments={"n": 1}))
                yield StreamEvent(type="finish", finish_reason="tool_calls", tool_calls=[call],
                                  usage=TokenUsage(prompt_tokens=18_000, total_tokens=18_003, completion_tokens=3))
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")

    class Count(Tool):
        name = "count"
        description = "Count."
        parameters = {"type": "object"}

        async def execute(self, n):
            return ToolResult(success=True, content=str(n))

    provider = Provider([("bad", "stop")] * 5)
    events = [e async for e in run_agent_loop(llm=provider, messages=_history(size=900), tools={"count": Count()},
                                              max_steps=2, token_limit=10_000)]
    assert [e.mode for e in events if isinstance(e, SummarizationEvent)] == ["fallback", "summary"]
    assert events[-1].stop_reason == StopReason.END_TURN and len(provider.requests) == 6
