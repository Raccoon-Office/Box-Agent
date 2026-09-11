"""Safe input accounting and bounded Skill recovery at the real loop boundary."""

from dataclasses import replace
import asyncio
import json

import pytest

from box_agent.context_input import DefaultContextEngine
from box_agent.events import DoneEvent, ErrorEvent, StepStart, StopReason, SummarizationEvent
from box_agent.hooks import BaseHook
from box_agent.kernel.context_engine import _fallback_context_estimate, skill_reference_budget_chars
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall, TokenUsage
from box_agent.session_log import SessionLog
from box_agent.skill_context import SkillReferenceContext
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.engine.engine import DefaultToolEngine
from box_agent.tools.base import Tool
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "skills/demo/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: demo\ndescription: example\n---\nEXACT_METHOD_BODY\n" + "method line\n" * 100)
    loader = SkillLoader(sources=[(tmp_path / "skills", "user")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    return SkillRuntime(loader)


def test_safe_input_budget_does_not_reserve_model_output_twice():
    # Same observed input pressure as the Web failure, below its derived limit.
    history = [Message(role="assistant", content="previous response",
                       usage=TokenUsage(input_tokens=123183, output_tokens=0))]
    assert skill_reference_budget_chars(history, (), 180129, 64000) == 50000
    assert skill_reference_budget_chars(history, (), 180129, 64000) == skill_reference_budget_chars(history, (), 180129, 0)


def test_large_base64_image_uses_pixel_budget_and_keeps_provider_payload():
    image_data = "A" * 200_000
    image = {
        "type": "input_image",
        "media_type": "image/png",
        "data": image_data,
        "width": 1000,
        "height": 1000,
    }
    engine = DefaultContextEngine()
    request = engine.prepare_request(
        [Message(role="user", content="inspect this image")],
        prepared_tools=prepare_tools([]),
        token_limit=10_000,
        transient_message=Message(role="user", content=[image]),
    )
    assert request.blocked_reason is None
    assert request.request_only_input_tokens < 2_000
    assert request.messages[-1].content[0]["data"] == image_data


def test_host_and_paged_tool_share_the_safe_input_budget(runtime):
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    tool = GetSkillTool(runtime.loader)
    history = [Message(role="system", content="BASE"), Message(role="user", content="task"),
               Message(role="assistant", content="previous response", usage=TokenUsage(input_tokens=123183, output_tokens=0))]
    runtime.select(["demo"])
    projection = engine.prepare_request(history, prepared_tools=prepare_tools([tool]), token_limit=180129, output_tokens=64000)
    assert not projection.blocked_reason
    assert "EXACT_METHOD_BODY" in str(projection.messages)
    assert runtime.turn_deliveries == {}
    runtime.select([])
    engine.prepare_request(history, prepared_tools=prepare_tools([tool]), token_limit=180129, output_tokens=64000)
    page = engine.tool_reader("demo", limit=20)
    assert page.success and "EXACT_METHOD_BODY" in page.model_context
    assert page.raw_output["skill_reference"]["has_more"]


def test_declared_transient_cost_is_reserved_for_projection_and_batch_reads(runtime):
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    tool = GetSkillTool(runtime.loader)
    history = [Message(role="user", content="task")]
    transient = Message(role="user", content=[{"type": "text", "text": "transient"}])
    request = engine.prepare_request(history, prepared_tools=prepare_tools([tool]), token_limit=5000,
                                     transient_message=transient, transient_tokens=5000)
    assert request.blocked_reason
    assert not engine.tool_reader("demo").success
    assert runtime.turn_deliveries == {}


def test_final_request_checks_extra_material_even_without_selected_skills():
    engine = DefaultContextEngine()
    request = engine.prepare_request([Message(role="system", content="BASE"), Message(role="user", content="task")],
                                     prepared_tools=prepare_tools([]), token_limit=3000,
                                     extra_messages=(Message(role="user", content="extra " * 3000),))
    assert request.blocked_reason


def test_consecutive_skill_pages_charge_schemas_images_overlays_and_pending_envelopes(runtime):
    runtime.loader.get_skill("demo").skill_path.write_text(
        "---\nname: demo\ndescription: example\n---\n" + ('"\\方法 ' * 10 + '\n') * 3000)
    tool = GetSkillTool(runtime.loader)
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    history = [Message(role="user", content="task")]
    extra = Message(role="user", content="extra context " * 100)
    transient = Message(role="user", content=[{
        "type":"input_image", "media_type":"image/png", "data":"aGVsbG8=", "width":1, "height":1,
    }])
    engine.prepare_request(history, prepared_tools=prepare_tools([tool]), token_limit=7000, output_tokens=64000,
        extra_messages=(extra,), transient_message=transient, transient_tokens=2500)
    calls = [ToolCall(id=f"read-{index}", type="function", function=FunctionCall(
        name="get_skill", arguments={"skill_name":"demo", "offset":index * 20})) for index in range(3)]
    history.append(Message(role="assistant", content="", tool_calls=calls))
    followup = [{"type":"text", "text":"new transient " * 30}]
    engine.reserve_followup(followup)
    for index, call in enumerate(calls):
        page = engine.tool_reader("demo", offset=index * 20, limit=20 if index < 2 else None)
        assert page.success and not page.raw_output["skill_reference"].get("reused")
        history.append(Message(role="tool", name="get_skill", tool_call_id=call.id, content=page.model_context))
    assert page.raw_output["skill_reference"]["has_more"]
    actual = _fallback_context_estimate([*history, extra, transient, Message(role="user", content=followup)], {tool.name:tool})
    declared_reserve = 2500 - _fallback_context_estimate([transient], {})
    assert actual + declared_reserve + 1024 <= 7000


class Provider:
    max_output_tokens = 64000

    def __init__(self, *, summary_available=True):
        self.requests = []
        self.summary_calls = 0
        self.summary_available = summary_available

    async def generate(self, messages, **kwargs):
        if kwargs.get("call_kind") == "turn_continuation_judge":
            return LLMResponse(content='{"continue":false}', finish_reason="stop")
        assert kwargs.get("call_kind") == "context_summary"
        self.summary_calls += 1
        if not self.summary_available:
            raise RuntimeError("summary channel unavailable")
        return LLMResponse(content="<summary>Continue the current task.</summary>", finish_reason="stop")

    async def generate_stream(self, messages, **kwargs):
        self.requests.append(list(messages))
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


class StepHook(BaseHook):
    def __init__(self): self.steps = []
    async def on_step_start(self, *, step, max_steps): self.steps.append(step)


def crowded_history(runtime, *, prior_read=False):
    history = [Message(role="system", content="BASE"), Message(role="user", content="old task")]
    if prior_read:
        context = SkillReferenceContext(runtime)
        context.prepare_request([], budget_chars=50000)
        read = context.read("demo")
        history.extend([
            Message(role="assistant", content="", tool_calls=[ToolCall(id="old-read", type="function",
                function=FunctionCall(name="get_skill", arguments={"skill_name":"demo"}))]),
            Message(role="tool", name="get_skill", tool_call_id="old-read", content=read.model_context),
        ])
    history.append(Message(role="assistant", content="old execution " * 18000))
    history.extend(Message(role="assistant", content=f"recent result {index}") for index in range(6))
    history.append(Message(role="user", content="continue the current task"))
    runtime.begin_turn()
    return history


@pytest.mark.asyncio
async def test_loop_compacts_once_and_reprojects_without_trusting_old_read_facts(runtime, monkeypatch):
    history = crowded_history(runtime, prior_read=True)
    runtime.select(["demo"])
    tool = GetSkillTool(runtime.loader)
    limit = _fallback_context_estimate(history, {tool.name:tool}) + 100
    provider = Provider()
    hook = StepHook()
    projections = []
    failed_commits = []
    original = DefaultContextEngine.prepare_request

    def prepare(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        projections.append(result)
        if result.blocked_reason:
            return replace(result, on_committed=lambda: failed_commits.append(True))
        return result

    monkeypatch.setattr(DefaultContextEngine, "prepare_request", prepare)
    events = [event async for event in run_agent_loop(llm=provider, messages=history, tools={tool.name:tool},
        skill_engine=runtime, token_limit=limit, max_steps=1, hooks=[hook])]
    assert len(provider.requests) == 1
    assert projections[0].blocked_reason
    assert projections[-1].references and not projections[-1].blocked_reason
    assert not failed_commits
    assert len([event for event in events if isinstance(event, SummarizationEvent)]) == 1
    # Adding the summary instruction would exceed the safe input budget;
    # the bounded fallback must recover without sending that request.
    assert provider.summary_calls == 0
    assert len([event for event in events if isinstance(event, StepStart)]) == 1 and hook.steps == [1]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert "EXACT_METHOD_BODY" in str(provider.requests[0])
    assert not any(message.tool_call_id == "old-read" for message in provider.requests[0])
    assert _fallback_context_estimate(provider.requests[0], {tool.name:tool}) + 1024 <= limit
    assert runtime.turn_deliveries["demo"]["complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_available", [True, False])
async def test_loop_selected_body_recovers_with_summary_or_unavailable_summary_fallback(runtime, summary_available):
    runtime.loader.get_skill("demo").skill_path.write_text(
        "---\nname: demo\ndescription: example\n---\nEXACT_METHOD_BODY\n" + "method line\n" * 2500)
    runtime.select(["demo"])
    history = crowded_history(runtime)
    history[2].content = "old execution " * 4000
    provider = Provider(summary_available=summary_available)
    events = [event async for event in run_agent_loop(llm=provider, messages=history, tools={},
        skill_engine=runtime, token_limit=24000, max_steps=1)]
    assert provider.summary_calls == 1 and len(provider.requests) == 1
    compactions = [event for event in events if isinstance(event, SummarizationEvent)]
    assert len(compactions) == 1
    assert compactions[0].mode == ("summary" if summary_available else "fallback")
    assert "EXACT_METHOD_BODY" in str(provider.requests[0])
    assert _fallback_context_estimate(provider.requests[0], {}) + 1024 <= 24000
    assert runtime.turn_deliveries["demo"]["complete"]


@pytest.mark.asyncio
async def test_loop_final_input_recovery_includes_late_user_material_without_replaying_hook(runtime):
    runtime.select(["demo"])
    history = crowded_history(runtime)
    late_text = "LATEST_USER_MATERIAL\n" + "detail " * 4000
    limit = _fallback_context_estimate(history, {}) + 3000

    class LateHook(StepHook):
        async def on_step_start(self, **kwargs):
            await super().on_step_start(**kwargs)
            history.append(Message(role="user", content=late_text))

    hook = LateHook()
    provider = Provider()
    events = [event async for event in run_agent_loop(llm=provider, messages=history, tools={},
        skill_engine=runtime, token_limit=limit, max_steps=1, hooks=[hook])]
    assert len(provider.requests) == 1 and hook.steps == [1]
    assert len([event for event in events if isinstance(event, SummarizationEvent)]) == 1
    assert late_text in str(provider.requests[0]).replace("\\n", "\n")
    assert "EXACT_METHOD_BODY" in str(provider.requests[0])
    assert _fallback_context_estimate(provider.requests[0], {}) + 1024 <= limit


@pytest.mark.asyncio
async def test_loop_budget_recovery_preserves_summary_cancellation(runtime):
    runtime.loader.get_skill("demo").skill_path.write_text(
        "---\nname: demo\ndescription: example\n---\n" + "method line\n" * 2500)
    runtime.select(["demo"])
    history = crowded_history(runtime)
    history[2].content = "old execution " * 4000

    class CancelledSummary(Provider):
        async def generate(self, messages, **kwargs):
            assert kwargs.get("call_kind") == "context_summary"
            raise asyncio.CancelledError

    provider = CancelledSummary()
    with pytest.raises(asyncio.CancelledError):
        _ = [event async for event in run_agent_loop(llm=provider, messages=history, tools={},
            skill_engine=runtime, token_limit=24000, max_steps=1)]
    assert provider.requests == [] and runtime.turn_deliveries == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at,history_lines,skill_lines", [
    ("summary_return", 4000, 2500),
    ("summarization_event", 4000, 2500),
    ("step_hook", 1, 2500),
    ("step_hook", 4000, 2500),
    ("step_event", 4000, 2500),
    ("summary_return", 4000, 30000),
    ("summarization_event", 4000, 30000),
    ("summary_return", 18000, 30000),
    ("summarization_event", 18000, 30000),
])
async def test_loop_cancellation_before_request_commit_never_delivers(
    runtime, tmp_path, cancel_at, history_lines, skill_lines,
):
    runtime.loader.get_skill("demo").skill_path.write_text(
        "---\nname: demo\ndescription: example\n---\nEXACT_METHOD_BODY\n" + "method line\n" * skill_lines)
    runtime.select(["demo"])
    history = crowded_history(runtime)
    history[2].content = "old execution " * history_lines
    cancelled = False

    class CancellingProvider(Provider):
        async def generate(self, messages, **kwargs):
            nonlocal cancelled
            result = await super().generate(messages, **kwargs)
            if kwargs.get("call_kind") == "context_summary" and cancel_at == "summary_return":
                cancelled = True
            return result

    class CancellingHook(StepHook):
        def __init__(self):
            super().__init__()
            self.done = []

        async def on_step_start(self, **kwargs):
            nonlocal cancelled
            await super().on_step_start(**kwargs)
            if cancel_at == "step_hook":
                cancelled = True

        async def on_done(self, *, stop_reason, final_content):
            self.done.append(stop_reason)

    provider = CancellingProvider()
    hook = CancellingHook()
    log = SessionLog.create(tmp_path / "sessions", session_id="cancel-before-commit", cwd=tmp_path)
    events = []
    try:
        async for event in run_agent_loop(llm=provider, messages=history, tools={},
                skill_engine=runtime, token_limit=24000, max_steps=1, hooks=[hook],
                session_log=log, session_turn=1, is_cancelled=lambda: cancelled):
            events.append(event)
            if isinstance(event, SummarizationEvent) and cancel_at == "summarization_event":
                cancelled = True
            if isinstance(event, StepStart) and cancel_at == "step_event":
                cancelled = True
        assert provider.summary_calls == (0 if cancel_at in {"step_hook", "step_event"} else 1)
        assert len([event for event in events if isinstance(event, SummarizationEvent)]) == provider.summary_calls
        log.flush()
        records = [json.loads(line) for line in log.path.read_text().splitlines()]
        assert {
            "provider_requests": len(provider.requests),
            "delivered": tuple(runtime.turn_deliveries),
            "read_facts": len(runtime.read_facts),
            "persisted_skills": len(log.replay().skills),
            "request_committed": any(record.get("type") == "request/context" for record in records),
        } == {
            "provider_requests": 0, "delivered": (), "read_facts": 0,
            "persisted_skills": 0, "request_committed": False,
        }
        assert hook.steps == ([] if history_lines == 18000 or cancel_at == "step_event" else [1])
        assert hook.done == [StopReason.CANCELLED]
        assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [StopReason.CANCELLED]
        assert not any(isinstance(event, ErrorEvent) for event in events)
    finally:
        log.close()


@pytest.mark.asyncio
async def test_loop_compaction_counts_only_the_final_offered_schemas(monkeypatch):
    class HiddenTool(Tool):
        name = "hidden_tool"
        description = "hidden schema " * 10000
        parameters = {"type":"object", "properties":{}}
        async def execute(self, **kwargs): raise AssertionError("Tool was not offered")

    prepared = prepare_tools([])
    calls = []

    def prepare(self, **kwargs):
        calls.append(True)
        return prepared

    monkeypatch.setattr(DefaultToolEngine, "prepare_tools", prepare)
    provider = Provider()
    events = [event async for event in run_agent_loop(llm=provider,
        messages=[Message(role="system", content="BASE"), Message(role="user", content="task")],
        tools={"hidden_tool":HiddenTool()}, token_limit=4000, max_steps=1)]
    assert len(provider.requests) == 1
    assert calls == [True]
    assert not any(isinstance(event, SummarizationEvent) for event in events)


@pytest.mark.asyncio
async def test_loop_unrecoverable_selection_compacts_at_most_once_and_never_delivers(runtime):
    runtime.loader.get_skill("demo").skill_path.write_text("---\nname: demo\ndescription: example\n---\n" + "oversized method\n" * 30000)
    runtime.select(["demo"])
    provider = Provider()
    hook = StepHook()
    events = [event async for event in run_agent_loop(llm=provider,
        messages=[Message(role="system", content="BASE"), Message(role="user", content="task")],
        tools={}, skill_engine=runtime, token_limit=4000, max_steps=1, hooks=[hook])]
    assert provider.requests == []
    assert len([event for event in events if isinstance(event, SummarizationEvent)]) == 1
    assert provider.summary_calls <= 1 and hook.steps == [1]
    assert any(isinstance(event, DoneEvent) and event.stop_reason == StopReason.ERROR for event in events)
    assert runtime.turn_deliveries == {} and runtime.read_facts == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["compaction/summary", "compaction/flush", "request/context", "request/flush"])
async def test_loop_recovery_write_failure_propagates_without_skill_delivery(runtime, tmp_path, failure):
    history = crowded_history(runtime)
    runtime.select(["demo"])
    tool = GetSkillTool(runtime.loader)
    limit = _fallback_context_estimate(history, {tool.name:tool}) + 100
    provider = Provider()
    log = SessionLog.create(tmp_path / "sessions", session_id="budget-recovery", cwd=tmp_path)

    class FailingStore:
        pending_flush = False
        def __getattr__(self, name): return getattr(log, name)
        def append(self, kind, data, **kwargs):
            if kind == failure: raise OSError("recovery write failed")
            if ((failure == "compaction/flush" and kind == "compaction/end")
                    or (failure == "request/flush" and kind == "request/context")):
                self.pending_flush = True
            return log.append(kind, data, **kwargs)
        def flush(self):
            if self.pending_flush: raise OSError("recovery write failed")
            return log.flush()

    try:
        with pytest.raises(OSError, match="recovery write failed"):
            _ = [event async for event in run_agent_loop(llm=provider, messages=history, tools={tool.name:tool},
                skill_engine=runtime, token_limit=limit, max_steps=1, session_log=FailingStore(), session_turn=1)]
        assert provider.requests == []
        assert runtime.turn_deliveries == {} and runtime.read_facts == ()
        assert log.replay().skills == []
    finally:
        log.close()
