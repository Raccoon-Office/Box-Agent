"""Compaction spends the input budget by degrading resources in order."""

import json
from contextlib import closing

import pytest

from box_agent.agent import Agent
from box_agent.events import DoneEvent, ErrorEvent, StopReason, SummarizationEvent
from box_agent.kernel import context_engine
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog
from box_agent.skill_context import SkillReferenceContext, visible_ranges
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_loader import SkillLoader


class _SummaryProvider:
    summary = ("Original summary beginning. " + "Evidence retained. " * 280 + "Original summary ending.")
    max_output_tokens = 4_000

    def __init__(self):
        self.summary_calls = 0

    def estimate_text_tokens(self, text):
        return (len(text.encode()) + 1) // 2

    async def generate_stream(self, messages, **kwargs):
        self.summary_calls += 1
        yield StreamEvent(type="text", delta=f"<summary>{self.summary}</summary>")
        yield StreamEvent(type="finish", finish_reason="stop")


class _RuntimeSection:
    def __init__(self, label):
        self.label = label

    def compaction_state(self):
        return self.label, f"{self.label} state " * 150


def _tool_round(index):
    calls = [
        ToolCall(id=f"result-{index}-{part}", type="function",
                 function=FunctionCall(name="read_file", arguments={"path": f"record-{index}-{part}.txt"}))
        for part in range(2)
    ]
    return [
        Message(role="assistant", content=f"Round {index} explanation", tool_calls=calls),
        *(Message(role="tool", name="read_file", tool_call_id=call.id,
                  content=f"{call.id} evidence " * 40) for call in calls),
    ]


def _messages_with_marker(messages, marker):
    return [message for message in messages if message.content.startswith(marker)]


def _tool_ids(messages):
    return [message.tool_call_id for message in messages if message.role == "tool"]


def _degradation(rounds=0, skills=0, users=0, runtime=()):
    return {
        "droppedRounds": rounds,
        "droppedSkillBodies": skills,
        "droppedEarlierUsers": users,
        "droppedRuntimeState": list(runtime),
    }


class _CompactionCase:
    skill_names = ("zebra", "albatross")  # Oldest first, independent of alphabetical order.
    hard_limit = 50_000

    def __init__(self, tmp_path, *, first_round_has_user=True):
        self.llm = _SummaryProvider()
        self.latest = Message(role="user", content="Current request: use these exact words.  ")
        self.earlier = [f"Historical user request {index}.\n Exact original  " + "x" * 250
                        for index in range(5)]
        self.history = [Message(role="system", content="System instructions."),
                        Message(role="user", content=self.earlier[0])]
        for name in self.skill_names:
            path = tmp_path / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {name}\ndescription: {name} method\n---\n"
                            + (f"{name.upper()}_BODY follows.\n" * 50), encoding="utf-8")
        loader = SkillLoader(sources=[(tmp_path / "skills", "user")], skill_settings_path=tmp_path / "settings.json")
        loader.discover_skills()
        self.references = SkillReferenceContext(SkillRuntime(loader, append_only_context=True))
        self.references.runtime.begin_turn()
        for name in self.skill_names:
            self.references.prepare_request(self.history, budget_chars=50_000)
            read = self.references.read(name)
            assert read.success
            call = ToolCall(id=f"read-{name}", type="function",
                            function=FunctionCall(name="get_skill", arguments={"skill_name": name}))
            self.history.extend([
                Message(role="assistant", content="", tool_calls=[call]),
                Message(role="tool", name="get_skill", tool_call_id=call.id, content=read.model_context),
            ])
        for index in range(5):
            if index == 4:
                self.history.append(self.latest)
            elif index or first_round_has_user:
                self.history.append(Message(role="user", content=self.earlier[index + 1]))
            self.history.extend(_tool_round(index))
        self.tools = {label: _RuntimeSection(label) for label in ("Todo", "Goal", "Plan")}
        # This charged overlay keeps all chosen limits above the initial recent window budget.
        self.descriptor = Message(role="user", source="runtime", name="tool_descriptions",
                                  content="Fixed tool description. " * 1_300)
        self.date = Message(role="user", source="runtime", name="current_date", content="2026-11-24")

    async def compact(self, *, limit=hard_limit, headroom=None):
        projected = []

        def project(history, *, max_skill_bodies=None):
            history = self.references.compact_history(
                self.history, history, count_text_tokens=self.llm.estimate_text_tokens,
                max_skill_bodies=max_skill_bodies,
            )
            candidate = [*history, self.descriptor, self.date]
            projected.append(candidate)
            return candidate

        result = await context_engine._maybe_summarize(
            self.llm, self.history, limit, 0, False, force=True, tools=self.tools,
            estimate_tools={}, summary_input_token_limit=100_000, request_headroom_tokens=headroom,
            compacted_history_projector=project, compaction_source_projector=self.references.compaction_source,
        )
        return result, projected


def _summary(messages):
    return next(message for message in messages if context_engine._is_summary_marker(message))


def _expected_summary(llm):
    return context_engine._SUMMARY_MESSAGE_PREFIX + llm.summary + context_engine._SUMMARY_MESSAGE_SUFFIX


@pytest.mark.asyncio
async def test_one_round_degradation_keeps_remaining_groups_and_resources(tmp_path):
    case = _CompactionCase(tmp_path, first_round_has_user=False)
    original = [message.model_dump() for message in case.history]
    full, _ = await case.compact()
    limit = full.estimated_after - 1
    outcome, projected = await case.compact(limit=limit)

    assert outcome.mode == "summary" and outcome.estimated_after <= limit
    assert outcome.degradation == _degradation(rounds=1)
    assert len(projected) == 2 and outcome.summary_calls == 1
    assert _tool_ids(outcome.messages) == _tool_ids(full.messages)[2:]
    assert [message.tool_calls for message in outcome.messages if message.tool_calls] == [
        message.tool_calls for message in full.messages if message.tool_calls
    ][1:]
    for marker in ("[Invoked Skills]", "[Post-Compaction Runtime State]"):
        assert _messages_with_marker(outcome.messages, marker) == _messages_with_marker(full.messages, marker)
    assert _summary(outcome.messages).content.startswith(_expected_summary(case.llm))
    latest_index = outcome.messages.index(case.latest)
    assert outcome.messages[latest_index + 1].tool_calls[0].id == "result-4-0"
    assert [message.model_dump() for message in case.history] == original


@pytest.mark.asyncio
async def test_exhausting_headroom_rebuilds_each_stage_before_accepting_hard_limit(tmp_path):
    case = _CompactionCase(tmp_path)
    outcome, projected = await case.compact(headroom=case.hard_limit - 1)

    assert outcome.mode == "summary" and 1 < outcome.estimated_after <= case.hard_limit
    assert outcome.degradation == _degradation(5, 2, 5, ("Todo", "Plan", "Goal"))
    assert case.llm.summary_calls == 1
    assert len(projected) == 16
    assert [_tool_ids(candidate) for candidate in projected[:6]] == [
        [f"result-{index}-{part}" for index in range(drop, 5) for part in range(2)]
        for drop in range(6)
    ]
    for candidate in projected:
        assert _summary(candidate).content.startswith(_expected_summary(case.llm))
        assert candidate.count(case.latest) + int(case.latest.content in _summary(candidate).content) == 1
        assert candidate[-2:] == [case.descriptor, case.date]
    for candidate in projected[:13]:
        assert _messages_with_marker(candidate, "[Post-Compaction Runtime State]") == _messages_with_marker(
            projected[0], "[Post-Compaction Runtime State]",
        )
    for candidate in projected[5:]:
        assert _summary(candidate).earlier_user_messages[-1] == case.latest.content
        assert not _tool_ids(candidate)
    for index, candidate in enumerate(projected[:6]):
        archive = _summary(candidate)
        assert archive.earlier_user_messages == (case.earlier + [case.latest.content])[:index + 1]
        assert all(text in archive.content for text in archive.earlier_user_messages)

    for candidate, dropped in zip(projected[5:8], range(3)):
        (skills,) = _messages_with_marker(candidate, "[Invoked Skills]")
        for index, name in enumerate(case.skill_names):
            snapshot = case.references.runtime.resolve_reference(name)
            assert f"### Skill: {name}\nPath: {snapshot.path}" in skills.content
            lines = snapshot.prompt.splitlines(keepends=True)
            ranges = visible_ranges(candidate, name=name, revision=snapshot.revision,
                                    lines=lines, source=snapshot.source, path=snapshot.path)
            if index < dropped:
                assert f"{name.upper()}_BODY" not in skills.content and not ranges
                assert "Use get_skill to read it when needed." in skills.content
            else:
                assert f"{name.upper()}_BODY" in skills.content and ranges == set(range(len(lines)))

    for dropped, candidate in enumerate(projected[7:13]):
        archive = _summary(candidate)
        assert archive.earlier_user_messages == [*case.earlier, case.latest.content]
        assert [text for text in case.earlier if text in archive.content] == case.earlier[dropped:]
        if dropped:
            assert f"[{dropped} earlier user messages are left out here; see the summary.]" in archive.content
    for candidate in projected[12:]:
        assert _summary(candidate).earlier_user_messages == [*case.earlier, case.latest.content]
        assert case.latest.content in _summary(candidate).content
    for candidate, expected in zip(projected[12:], [("Goal", "Plan", "Todo"), ("Goal", "Plan"), ("Goal",), ()]):
        sections = _messages_with_marker(candidate, "[Post-Compaction Runtime State]")
        labels = tuple(chunk.splitlines()[0] for chunk in sections[0].content.split("\n\n## ")[1:]) if sections else ()
        assert labels == expected

    # Verify that the first fitting candidate wins even when later candidates are smaller.
    single_skill_body = None
    for candidate_index, expected in [(6, _degradation(5, 1)), (8, _degradation(5, 2, 1)),
                                      (13, _degradation(5, 2, 5, ("Todo",)))]:
        limit = context_engine.request_input_tokens(projected[candidate_index], {}, llm=case.llm)
        accepted, attempts = await case.compact(limit=limit)
        assert not accepted.blocked and accepted.estimated_after == limit
        assert accepted.degradation == expected and len(attempts) == candidate_index + 1
        if candidate_index == 6:
            single_skill_body = accepted.messages
    case.references.prepare_request(single_skill_body, budget_chars=50_000)
    reread = case.references.read(case.skill_names[0])
    assert reread.success and not reread.raw_output["skill_reference"].get("reused")
    assert f"{case.skill_names[0].upper()}_BODY" in reread.model_context


@pytest.mark.asyncio
async def test_exhausted_hard_budget_preserves_summary_and_replays_all_user_originals(tmp_path):
    case = _CompactionCase(tmp_path)
    exhausted, _ = await case.compact(headroom=case.hard_limit - 1)
    limit = exhausted.estimated_after - 1
    blocked, _ = await case.compact(limit=limit)

    assert blocked.blocked and blocked.estimated_after > limit and blocked.summary_calls == 1
    assert blocked.degradation == exhausted.degradation
    assert _summary(blocked.messages).content.startswith(_expected_summary(case.llm))
    assert case.latest.content in _summary(blocked.messages).content

    root = tmp_path / "sessions"
    with closing(SessionLog.create(root, session_id="degraded-archive", cwd=tmp_path)) as log:
        log.replace_surface(exhausted.messages[1:], turn=1, step=1)
        log.flush()
    with closing(SessionLog.open(root, session_id="degraded-archive", cwd=tmp_path)) as log:
        replayed = log.replay().messages
    assert _summary(replayed).earlier_user_messages == [*case.earlier, case.latest.content]
    assert case.latest.content in _summary(replayed).content
    next_request = Message(role="user", content="Next real user request")
    resumed = await context_engine._maybe_summarize(
        case.llm, [case.history[0], *replayed, next_request], case.hard_limit, 0, False, force=True,
    )
    archive = _summary(resumed.messages)
    assert archive.earlier_user_messages == [*case.earlier, case.latest.content]
    assert [message for message in resumed.messages if context_engine._is_real_user_message(message)] == [
        next_request,
    ]


@pytest.mark.asyncio
async def test_fitting_history_does_not_trigger_compaction_to_gain_headroom(tmp_path):
    case = _CompactionCase(tmp_path)
    limit = context_engine.request_input_tokens(case.history, {}, llm=case.llm) + 1
    outcome = await context_engine._maybe_summarize(
        case.llm, case.history, limit, 0, False, request_headroom_tokens=limit - 1,
    )
    assert outcome.messages is None and case.llm.summary_calls == 0


class _ContextOverflow(Exception):
    status_code = 400
    code = "context_length_exceeded"


class _ReactiveProvider(_SummaryProvider):
    provider = "openai"
    model = "reactive-compaction-test"
    max_output_tokens = 1_000
    summary = "Original summary beginning. " + "Evidence retained. " * 145 + "Original summary ending."

    def __init__(self):
        super().__init__()
        self.main_requests = []

    async def generate_stream(self, messages, *, call_kind="", **kwargs):
        if call_kind == "context_summary":
            async for event in super().generate_stream(messages, **kwargs):
                yield event
            return
        self.main_requests.append(messages)
        if len(self.main_requests) == 1:
            raise _ContextOverflow("This model's maximum context length is 7200 tokens.")
        yield StreamEvent(type="text", delta="Completed after degradation.")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_reactive_degradation_retries_main_request_and_records_outcome(tmp_path):
    provider = _ReactiveProvider()
    with closing(SessionLog.create(tmp_path / "sessions", session_id="reactive-degradation", cwd=tmp_path)) as log:
        agent = Agent(llm_client=provider, system_prompt="System instructions.", tools=[], token_limit=12_000,
                      max_steps=1, workspace_dir=str(tmp_path), session_log=log,
                      deferred_mcp_loading_enabled=False)
        for index in range(5):
            agent.messages.extend([Message(role="user", content=f"Earlier prompt {index}"), *_tool_round(index)])
        agent.add_user_message("Continue the current task.")
        events = [event async for event in agent.run_events()]
        records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == StopReason.END_TURN
    (summary,) = [event for event in events if isinstance(event, SummarizationEvent)]
    assert summary.mode == "summary" and summary.degradation["droppedRounds"] > 0
    assert provider.summary_calls == 1 and len(provider.main_requests) == 2
    initial, resumed = provider.main_requests
    initial_ids, resumed_ids = _tool_ids(initial), _tool_ids(resumed)
    assert resumed_ids and resumed_ids == initial_ids[-len(resumed_ids):]
    assert len(initial_ids) - len(resumed_ids) >= 2 * summary.degradation["droppedRounds"]
    assert _summary(resumed).content.startswith(context_engine._SUMMARY_MESSAGE_PREFIX + provider.summary)
    assert any(message.content == "Continue the current task." for message in resumed)
    (record,) = [record["data"] for record in records if record["type"] == "compaction/summary"]
    assert record["degradation"] == summary.degradation
