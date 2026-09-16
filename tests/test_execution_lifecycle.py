"""Deterministic generic lifecycle tests through the real kernel/ToolEngine."""

import asyncio
from dataclasses import replace

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import DoneEvent, LLMOutputEvent, StepEnd, StepStart, StopReason, ToolCallResult, ToolCallStart
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool
from box_agent.kernel.execution_lifecycle import ExecutionAction as Action


class ScriptedLLM:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []
        self.request_kinds = []

    async def generate_stream(self, messages, **kwargs):
        self.request_kinds.append(kwargs.get("call_kind", ""))
        self.requests.append([m.model_copy(deep=True) for m in messages])
        response = self.responses.pop(0)
        if response.content:
            yield StreamEvent(type="text", delta=response.content)
        yield StreamEvent(type="finish", finish_reason=response.finish_reason,
                          tool_calls=response.tool_calls)


class ReceiptTool(Tool):
    name = "receipt"
    description = "Write a local receipt."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}},
                  "required": ["value"]}

    def __init__(self, path):
        self.path = path

    async def execute(self, value):
        with self.path.open("a") as stream:
            stream.write(value + "\n")
        return ToolResult(success=True, content="Receipt stored.")


class Lifecycle:
    def __init__(self, steps=(), finishes=(), context=()):
        self.steps = list(steps)
        self.finishes = list(finishes)
        self.context = context
        self.begins = []
        self.results = []
        self.calls = []
        self.histories = []
        self.compactions = 0
        self.denied = None

    async def begin_run(self, **kwargs):
        self.begins.append(kwargs)

    async def before_step(self, messages):
        self.histories.append(list(messages))
        return self.steps.pop(0) if self.steps else None

    async def before_finish(self, content):
        return self.finishes.pop(0) if self.finishes else None

    def context_messages(self):
        return self.context

    def before_calls(self, calls):
        self.calls.append(tuple(calls))

    def tool_call_error(self, name, args):
        return self.denied

    def observe_tool_result(self, event):
        self.results.append(event)

    def after_compaction(self):
        self.compactions += 1


def call(name="receipt", value="one", id="runtime-one"):
    return ToolCall(type="function", id=id, function=FunctionCall(name=name, arguments={"value": value}))


async def run(llm, tools, lifecycle=None, **kwargs):
    messages = kwargs.pop("messages", [Message(role="system", content="Be useful."),
                                       Message(role="user", content="Complete my task.")])
    events = [event async for event in run_agent_loop(
        llm=llm, messages=messages, tools={tool.name: tool for tool in tools},
        run_lifecycle=lifecycle, artifact_detection_enabled=False,
        truncation_continuation_enabled=False, **kwargs)]
    return events, messages


def done(events):
    return [e for e in events if isinstance(e, DoneEvent)][-1]


@pytest.mark.asyncio
async def test_runtime_decision_uses_real_tool_protocol_and_waits_without_provider():
    decision = ToolCall(type="function", id="runtime-card", function=FunctionCall(
        name="request_user_decision", arguments={
            "question": "Which output?", "decision_kind": "delivery_format",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        }))
    life = Lifecycle(steps=[Action(tool_calls=(decision,))])
    llm = ScriptedLLM()
    events, messages = await run(llm, [RequestUserDecisionTool()], life, thinking_enabled=True)
    assert done(events).stop_reason == StopReason.WAITING_FOR_USER
    assert not llm.requests
    assert not any(isinstance(e, LLMOutputEvent) for e in events)
    assert len(life.begins) == 1 and life.begins[0]["thinking_enabled"] is True
    assert life.calls == [(decision,)]
    result = next(e for e in events if isinstance(e, ToolCallResult))
    assert result.success and result.raw_output
    assert result.origin == "runtime" and life.results == [result]
    assert next(e for e in events if isinstance(e, ToolCallStart)).origin == "runtime"
    assistant = next(m for m in messages if m.tool_calls)
    assert assistant.role == "assistant" and assistant.source == "runtime"
    assert any(m.role == "tool" and m.tool_call_id == decision.id for m in messages)


@pytest.mark.asyncio
async def test_before_finish_runs_tool_then_rechecks_instead_of_accepting_partial(tmp_path):
    tool = ReceiptTool(tmp_path / "receipt")
    life = Lifecycle(finishes=[Action(tool_calls=(call(),)), None])
    llm = ScriptedLLM([LLMResponse(finish_reason="stop", content="Partial result."), LLMResponse(finish_reason="stop", content="Delivered.")])
    events, _ = await run(llm, [tool], life, max_steps=4)
    assert tool.path.read_text() == "one\n"
    assert done(events).stop_reason == StopReason.END_TURN
    assert done(events).final_content == "Delivered."
    assert len(life.results) == 1


@pytest.mark.asyncio
async def test_runtime_calls_share_total_tool_budget_with_provider(tmp_path):
    tool = ReceiptTool(tmp_path / "receipt")
    life = Lifecycle(steps=[Action(tool_calls=(call(),))])
    llm = ScriptedLLM([LLMResponse(finish_reason="stop", content="", tool_calls=[call(value="two", id="model-two")]),
                       LLMResponse(finish_reason="stop", content="Budget handled.")])
    events, _ = await run(llm, [tool], life, max_steps=4, max_tool_calls=1)
    assert tool.path.read_text() == "one\n"
    results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(results) == 2 and not results[1].success
    assert results[0].origin == "runtime" and results[1].origin == "model"
    assert done(events).stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_runtime_batches_consume_step_budget(tmp_path):
    tool = ReceiptTool(tmp_path / "receipt")
    life = Lifecycle(steps=[Action(tool_calls=(call(),)), Action(tool_calls=(call(value="two"),))])
    llm = ScriptedLLM()
    events, _ = await run(llm, [tool], life, max_steps=1)
    assert tool.path.read_text() == "one\n"
    assert done(events).stop_reason == StopReason.MAX_STEPS and not llm.requests


@pytest.mark.asyncio
async def test_cancelled_run_never_enters_lifecycle_or_tool(tmp_path):
    tool = ReceiptTool(tmp_path / "receipt")
    life = Lifecycle(steps=[Action(tool_calls=(call(),))])
    events, _ = await run(ScriptedLLM(), [tool], life, is_cancelled=lambda: True)
    assert done(events).stop_reason == StopReason.CANCELLED
    assert not tool.path.exists() and not life.begins


@pytest.mark.asyncio
async def test_lifecycle_admission_applies_to_runtime_and_model_calls(tmp_path):
    tool = ReceiptTool(tmp_path / "receipt")
    life = Lifecycle(steps=[Action(tool_calls=(call(),))])
    life.denied = "Admission denied."
    llm = ScriptedLLM([LLMResponse(finish_reason="stop", content="", tool_calls=[call(id="model-one")]),
                       LLMResponse(finish_reason="stop", content="Cannot proceed.")])
    events, _ = await run(llm, [tool], life, max_steps=4)
    assert not tool.path.exists()
    assert len(life.calls) == 2 and len(life.results) == 2
    assert all(not e.success for e in life.results)
    assert done(events).stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_lifecycle_error_cannot_become_successful_final():
    life = Lifecycle(finishes=[Action(stop_reason=StopReason.ERROR, content="Receipt missing.")])
    events, _ = await run(ScriptedLLM([LLMResponse(finish_reason="stop", content="Partial.")]), [], life)
    assert done(events).stop_reason == StopReason.ERROR
    assert done(events).final_content == "Receipt missing."


@pytest.mark.asyncio
async def test_lifecycle_context_is_projected_but_not_persisted_in_history():
    context = Message(role="user", source="runtime", content="Current contract: durable receipt.")
    life = Lifecycle(context=(context,))
    llm = ScriptedLLM([LLMResponse(finish_reason="stop", content="Done.")])
    events, messages = await run(llm, [], life)
    assert done(events).stop_reason == StopReason.END_TURN
    assert any(m.content == context.content for m in llm.requests[0])
    assert not any(m.content == context.content for m in messages)


@pytest.mark.asyncio
async def test_no_lifecycle_preserves_ordinary_final():
    events, _ = await run(ScriptedLLM([LLMResponse(finish_reason="stop", content="Ordinary answer.")]), [])
    assert done(events).stop_reason == StopReason.END_TURN
    assert done(events).final_content == "Ordinary answer."


@pytest.mark.asyncio
async def test_new_user_during_lifecycle_preparation_discards_stale_action(tmp_path):
    queue = asyncio.Queue()
    tool = ReceiptTool(tmp_path / "receipt")

    class InjectingLifecycle(Lifecycle):
        async def before_step(self, messages):
            self.histories.append(list(messages))
            if len(self.histories) == 1:
                queue.put_nowait("Cancel the old task; answer my new question.")
                return Action(tool_calls=(call(),))
            assert any("new question" in str(m.content) for m in messages)
            return None

    life = InjectingLifecycle()
    llm = ScriptedLLM([LLMResponse(finish_reason="stop", content="New answer.")])
    events, _ = await run(llm, [tool], life, inject_queue=queue, max_steps=3)
    assert done(events).stop_reason == StopReason.END_TURN
    assert not tool.path.exists()
    assert len(life.begins) == 1


@pytest.mark.asyncio
async def test_cancel_during_lifecycle_preparation_prevents_tool(tmp_path):
    cancelled = False
    tool = ReceiptTool(tmp_path / "receipt")

    class CancellingLifecycle(Lifecycle):
        async def before_step(self, messages):
            nonlocal cancelled
            cancelled = True
            return Action(tool_calls=(call(),))

    events, _ = await run(ScriptedLLM(), [tool], CancellingLifecycle(),
                          is_cancelled=lambda: cancelled)
    assert done(events).stop_reason == StopReason.CANCELLED
    assert not tool.path.exists()


@pytest.mark.asyncio
async def test_runtime_result_is_committed_before_observation_with_durable_origin(tmp_path, monkeypatch):
    from box_agent.session_log import SessionLog
    from box_agent.tools.engine import engine, results

    log = SessionLog.create(tmp_path / "sessions", session_id="lifecycle", cwd=tmp_path)
    traces = []
    def capture(kind, **kwargs):
        traces.append((kind, kwargs))
    monkeypatch.setattr(engine, "emit_session_trace", capture)
    monkeypatch.setattr(results, "emit_session_trace", capture)
    tool = ReceiptTool(tmp_path / "receipt")

    class ObservingLifecycle(Lifecycle):
        def observe_tool_result(self, event):
            assert log.replay().messages[-1].role == "tool"
            super().observe_tool_result(event)

    life = ObservingLifecycle(steps=[Action(tool_calls=(call(),))])
    try:
        events, _ = await run(ScriptedLLM([LLMResponse(finish_reason="stop", content="Done.")]),
                              [tool], life, session_log=log, session_turn=1)
        assert done(events).stop_reason == StopReason.END_TURN
        assistant = next(e for e in log.events if e["type"] == "assistant/message"
                         and e["data"]["message"].get("tool_calls"))
        assert assistant["data"]["origin"] == "runtime"
        assert assistant["data"]["message"]["source"] == "runtime"
        assert next(e for e in log.events if e["type"] == "tool/call")["data"]["origin"] == "runtime"
        result = next(e for e in log.events if e["type"] == "tool/result")
        assert result["data"]["result"]["origin"] == "runtime"
        assert next(m for m in log.replay().messages if m.tool_calls).source == "runtime"
        for kind in ("tool.request", "tool.response"):
            assert next(kwargs for event, kwargs in traces if event == kind)["data"]["origin"] == "runtime"
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_runtime_tool_uses_existing_permission_negotiation(approve):
    from box_agent.tools.skillhub_install_tool import SkillHubInstallTool
    installed, requests = [], []
    async def installer(payload):
        installed.append(payload)
        return {"status": "installed", "skill": {"name": "fixture"}}
    candidate = {"id": "fixture-skill", "slug": "fixture", "name": "Fixture"}
    tool = SkillHubInstallTool(installer, candidate_provider=lambda _: candidate)
    class Negotiator:
        async def negotiate(self, request):
            requests.append(request)
            return approve
    invocation = ToolCall(type="function", id="runtime-install", function=FunctionCall(
        name=tool.name, arguments={"skill_id": "fixture-skill"}))
    life = Lifecycle(steps=[Action(tool_calls=(invocation,))])
    events, _ = await run(ScriptedLLM([LLMResponse(finish_reason="stop", content="Handled.")]),
                          [tool], life, permission_negotiator=Negotiator(), max_steps=3)
    assert len(requests) == 1
    assert len(installed) == int(approve)
    assert life.results[0].success is approve and life.results[0].origin == "runtime"
    assert done(events).stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_lifecycle_context_cannot_bypass_real_input_budget():
    life = Lifecycle(context=(Message(role="user", source="runtime", content="large " * 12000),))
    llm = ScriptedLLM()
    events, _ = await run(llm, [], life, token_limit=1024, max_steps=2)
    assert done(events).stop_reason == StopReason.ERROR
    assert all(kind == "context_summary" for kind in llm.request_kinds)


@pytest.mark.asyncio
async def test_real_compaction_notifies_lifecycle_and_refreshes_projected_context():
    from box_agent.events import SummarizationEvent

    class CompactingLLM(ScriptedLLM):
        async def generate_stream(self, messages, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                yield StreamEvent(type="text", delta="<summary>Earlier work.</summary>")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            async for event in super().generate_stream(messages, **kwargs):
                yield event

    class RestoringLifecycle(Lifecycle):
        def after_compaction(self):
            super().after_compaction()
            self.context = (Message(role="user", source="runtime", content="Re-read current method."),)

    life = RestoringLifecycle()
    llm = CompactingLLM([LLMResponse(finish_reason="stop", content="Resumed.")])
    history = [Message(role="system", content="System."), Message(role="user", content="Old."),
               Message(role="assistant", content="old detail " * 20000),
               Message(role="user", content="Current task.")]
    events, _ = await run(llm, [], life, token_limit=8000, messages=history)
    assert done(events).stop_reason == StopReason.END_TURN
    assert any(isinstance(event, SummarizationEvent) for event in events)
    assert life.compactions == 1
    assert any(m.content == "Re-read current method." for m in llm.requests[0])


@pytest.mark.asyncio
async def test_plan_approval_gate_takes_priority_over_runtime_actions(tmp_path):
    from box_agent.tools.plan_tool import PlanWriteTool, PlanStore
    tool = ReceiptTool(tmp_path / "receipt")
    life = Lifecycle(steps=[Action(tool_calls=(call(),))])
    llm = ScriptedLLM([LLMResponse(finish_reason="stop", content="Plan draft.")])
    events, _ = await run(llm, [tool, PlanWriteTool(PlanStore())], life,
                          require_plan_approval=True, max_steps=1)
    assert not life.histories and not tool.path.exists()
    assert done(events).stop_reason != StopReason.END_TURN


@pytest.mark.asyncio
async def test_compaction_rereads_current_method_before_next_provider_request(tmp_path):
    tool = ReceiptTool(tmp_path / "receipt")

    class RestoringLifecycle(Lifecycle):
        pending = False

        def after_compaction(self):
            super().after_compaction()
            self.pending = True

        async def before_step(self, messages):
            self.histories.append(list(messages))
            if self.pending:
                self.pending = False
                return Action(tool_calls=(call(value="method refreshed"),))
            return None

    class CompactingLLM(ScriptedLLM):
        async def generate_stream(self, messages, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                yield StreamEvent(type="text", delta="<summary>Earlier work.</summary>")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            assert tool.path.exists(), "provider must wait for the real method refresh"
            async for event in super().generate_stream(messages, **kwargs):
                yield event

    life = RestoringLifecycle()
    llm = CompactingLLM([LLMResponse(finish_reason="stop", content="Resumed after refresh.")])
    history = [Message(role="system", content="System."), Message(role="user", content="Old."),
               Message(role="assistant", content="old detail " * 20000),
               Message(role="user", content="Current task.")]
    events, _ = await run(llm, [tool], life, token_limit=8000, messages=history, max_steps=3)
    assert done(events).stop_reason == StopReason.END_TURN
    assert life.compactions == 1 and len(life.results) == 1
    assert tool.path.read_text() == "method refreshed\n"
    assert len(llm.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_plugin", [False, True])
async def test_managed_lifecycle_is_preserved_when_public_option_is_omitted(with_plugin):
    from box_agent.composition import compose_default_kernel_services
    from box_agent.hooks import HookManager
    from box_agent.kernel.execution_lifecycle import RunLifecycle
    from box_agent.kernel.ports import RunLifecyclePort
    from box_agent.plugins.descriptors import PluginDescriptor
    from box_agent.plugins.hooks import HookProviderPort

    class WaitingLifecycle(RunLifecycle):
        async def before_step(self, messages):
            return Action(stop_reason=StopReason.WAITING_FOR_USER, content="Awaiting user.")

    class Hooks:
        def get_hooks(self):
            return ()

    llm, tools, life = ScriptedLLM(), {}, WaitingLifecycle()
    assert isinstance(life, RunLifecyclePort)
    services = replace(compose_default_kernel_services({"llm": llm, "tools": tools, "run_lifecycle": life}),
                       hook_bus=HookManager([]), hook_dispatch=None, hook_context=None,
                       context_engine=None, compact_engine=None)
    plugins = (PluginDescriptor("test.hooks", "1.0.0", (HookProviderPort,), Hooks),) if with_plugin else ()
    events = [event async for event in run_agent_loop(
        llm=llm, messages=[Message(role="user", content="Task.")], tools=tools,
        kernel_services=services, plugins=plugins)]
    assert done(events).stop_reason == StopReason.WAITING_FOR_USER
    assert not llm.requests


@pytest.mark.asyncio
async def test_managed_lifecycle_rejects_conflicting_public_instance():
    from box_agent.composition import compose_default_kernel_services
    from box_agent.hooks import HookManager
    llm, tools = ScriptedLLM(), {}
    services = replace(compose_default_kernel_services({"llm": llm, "tools": tools, "run_lifecycle": Lifecycle()}),
                       hook_bus=HookManager([]), hook_dispatch=None, hook_context=None,
                       context_engine=None, compact_engine=None)
    with pytest.raises(ValueError, match="run_lifecycle"):
        _ = [event async for event in run_agent_loop(
            llm=llm, messages=[Message(role="user", content="Task.")], tools=tools,
            kernel_services=services, run_lifecycle=Lifecycle())]
    assert not llm.requests


@pytest.mark.asyncio
async def test_plugin_can_supply_lifecycle_without_public_instance():
    from box_agent.kernel.ports import RunLifecyclePort
    from box_agent.plugins.descriptors import PluginDescriptor
    life = Lifecycle(steps=[Action(stop_reason=StopReason.WAITING_FOR_USER, content="Awaiting user.")])
    descriptor = PluginDescriptor("test.lifecycle", "1.0.0", (RunLifecyclePort,), lambda: life)
    events, _ = await run(ScriptedLLM(), [], plugins=(descriptor,))
    assert done(events).stop_reason == StopReason.WAITING_FOR_USER
    assert len(life.begins) == 1


def test_kernel_accepts_lifecycle_only_through_resolved_services():
    from box_agent.composition import compose_default_kernel_services
    from box_agent.kernel.loop import AgentLoopKernel
    services = compose_default_kernel_services({"llm": ScriptedLLM(), "tools": {}})
    with pytest.raises(TypeError, match="only through.*_services"):
        AgentLoopKernel(_services=services, messages=[], run_lifecycle=Lifecycle())


@pytest.mark.asyncio
async def test_no_lifecycle_keeps_existing_checkpoint_count():
    class RunControl:
        calls = 0

        async def checkpoint(self):
            self.calls += 1
            return True

    control = RunControl()
    events, _ = await run(ScriptedLLM([LLMResponse(finish_reason="stop", content="Done.")]),
                          [], run_control=control)
    assert done(events).stop_reason == StopReason.END_TURN
    assert control.calls == 1


class CompactOnce:
    def __init__(self):
        self.calls = 0

    async def compact_if_needed(self, inputs):
        from box_agent.kernel.context_types import CompactionOutcome
        self.calls += 1
        if self.calls == 1:
            return CompactionOutcome(
                messages=[inputs.history[0], Message(role="user", source="runtime", content="Summary."),
                          inputs.history[-1]], estimated_before=100, estimated_after=20, mode="fallback")
        return CompactionOutcome(messages=None, estimated_before=20, estimated_after=20)


async def run_compacting_kernel(llm, lifecycle, *, tools=None, context=True, queue=None):
    from box_agent.composition import compose_default_kernel_services
    from box_agent.kernel.loop import AgentLoopKernel
    services = compose_default_kernel_services({"llm": llm, "tools": tools or {}, "run_lifecycle": lifecycle})
    services = replace(services, compact_engine=CompactOnce(),
                       context_engine=services.context_engine if context else None)
    history = [Message(role="system", content="System."), Message(role="user", content="Task.")]
    return [event async for event in AgentLoopKernel(
        _services=services, messages=history, max_steps=3, token_limit=8000,
        inject_queue=queue, artifact_detection_enabled=False).run()]


@pytest.mark.asyncio
async def test_legacy_context_compaction_refreshes_method_and_runtime_context_before_provider(tmp_path):
    tool = ReceiptTool(tmp_path / "receipt")

    class Restore(Lifecycle):
        pending = False
        def after_compaction(self):
            self.compactions += 1
            self.pending = True
            self.context = (Message(role="user", source="runtime", content="Fresh task context."),)

        async def before_step(self, messages):
            if self.pending:
                self.pending = False
                return Action(tool_calls=(call(),))
            return None

    class CheckingModel(ScriptedLLM):
        async def generate_stream(self, messages, **kwargs):
            assert tool.path.exists(), "legacy context must refresh the method before provider"
            assert any(m.content == "Fresh task context." for m in messages)
            async for event in super().generate_stream(messages, **kwargs):
                yield event

    lifecycle = Restore(context=(Message(role="user", source="runtime", content="Old task context."),))
    llm = CheckingModel([LLMResponse(finish_reason="stop", content="Done.")])
    events = await run_compacting_kernel(llm, lifecycle, tools={tool.name: tool}, context=False)
    assert done(events).stop_reason == StopReason.END_TURN
    assert tool.path.read_text() == "one\n" and lifecycle.compactions == 1


@pytest.mark.asyncio
async def test_before_finish_user_injection_closes_the_current_step():
    queue = asyncio.Queue()
    class Injecting(Lifecycle):
        sent = False
        async def before_finish(self, content):
            if not self.sent:
                self.sent = True
                queue.put_nowait("New user request.")
            return None
    llm = ScriptedLLM([LLMResponse(finish_reason="stop", content="Old answer."),
                       LLMResponse(finish_reason="stop", content="New answer.")])
    events, _ = await run(llm, [], Injecting(), inject_queue=queue)
    assert done(events).stop_reason == StopReason.END_TURN
    assert [e.step for e in events if isinstance(e, StepStart)] == [1, 2]
    assert [e.step for e in events if isinstance(e, StepEnd)] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [True, False])
async def test_compaction_reentry_user_injection_closes_the_current_step(context):
    queue = asyncio.Queue()
    class Injecting(Lifecycle):
        pending = False
        sent = False
        def after_compaction(self):
            self.pending = True

        async def before_step(self, messages):
            if self.pending:
                self.pending = False
                self.sent = True
                queue.put_nowait("New user request.")
            return None
    lifecycle = Injecting()
    llm = ScriptedLLM([LLMResponse(finish_reason="stop", content="New answer.")])
    events = await run_compacting_kernel(llm, lifecycle, context=context, queue=queue)
    assert lifecycle.sent and done(events).stop_reason == StopReason.END_TURN
    assert [e.step for e in events if isinstance(e, StepStart)] == [1, 2]
    assert [e.step for e in events if isinstance(e, StepEnd)] == [1, 2]


@pytest.mark.asyncio
async def test_legacy_context_reprojection_cannot_bypass_input_budget():
    class Inflate(Lifecycle):
        def after_compaction(self):
            self.context = (Message(role="user", source="runtime", content="Required " * 20000),)
    llm = ScriptedLLM([LLMResponse(finish_reason="stop", content="Must not run.")])
    events = await run_compacting_kernel(llm, Inflate(), context=False)
    assert done(events).stop_reason == StopReason.ERROR and not llm.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_action", [False, True])
@pytest.mark.parametrize("blocked_code", [None, "SKILL_METHOD_REVISION_CHANGED"])
async def test_nonbudget_context_rejection_preserves_reason_without_compacting(tmp_path, runtime_action, blocked_code):
    from box_agent.composition import compose_default_kernel_services
    from box_agent.context_input import DefaultContextEngine
    from box_agent.kernel.loop import AgentLoopKernel

    from types import SimpleNamespace
    from box_agent.events import ErrorEvent

    class UnavailableContext(DefaultContextEngine):
        def prepare_request(self, *args, **kwargs):
            return SimpleNamespace(blocked_reason="Required context is unavailable.",
                                   budget_blocked=False, blocked_code=blocked_code)

        prepare_tool_batch = prepare_request

    tool = ReceiptTool(tmp_path / "receipt")
    llm = ScriptedLLM()
    life = Lifecycle(steps=[Action(tool_calls=(call(),))] if runtime_action else [])
    compact = CompactOnce()
    services = compose_default_kernel_services({"llm": llm, "tools": {tool.name: tool}, "run_lifecycle": life})
    services = replace(services, compact_engine=compact, context_engine=UnavailableContext())
    events = [event async for event in AgentLoopKernel(
        _services=services, messages=[Message(role="system", content="System."),
                                     Message(role="user", content="Task.")], max_steps=1).run()]
    assert done(events).stop_reason == StopReason.ERROR and not tool.path.exists()
    assert compact.calls == 0 and not llm.requests
    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.error_code == (blocked_code or "CONTEXT_INPUT_NOT_READY")
    assert error.error_category == ("skill_dependency" if blocked_code else "context_readiness")
