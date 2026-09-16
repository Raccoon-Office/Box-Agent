"""Runtime batches share real Context/ToolEngine budget and receipt contracts."""

from dataclasses import replace

import pytest

from box_agent.context_input import DefaultContextEngine
from box_agent.events import DoneEvent, StopReason, SummarizationEvent
from box_agent.kernel.context_engine import _fallback_context_estimate
from box_agent.kernel.execution_lifecycle import ExecutionAction, RunLifecycle
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "skills"
    for name in ("analysis-method", "reference-guide"):
        path = root / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: A generic analysis method\n---\n"
                        f"EXACT_{name}_BODY\n" + "method line\n" * 100)
    loader = SkillLoader(sources=[(root, "user")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    return SkillRuntime(loader)


def read_call(name="analysis-method", *, id="read", **arguments):
    return ToolCall(id=id, type="function", function=FunctionCall(
        name="get_skill", arguments={"skill_name": name, **arguments}))


class Provider:
    max_output_tokens = 64000

    def __init__(self):
        self.requests = []
        self.summary_calls = 0

    async def generate(self, messages, **kwargs):
        if kwargs.get("call_kind") == "turn_continuation_judge":
            return LLMResponse(content='{"continue":false}', finish_reason="stop")
        assert kwargs.get("call_kind") == "context_summary"
        self.summary_calls += 1
        return LLMResponse(content="<summary>Continue the analysis.</summary>", finish_reason="stop")

    async def generate_stream(self, messages, **kwargs):
        if kwargs.get("call_kind") == "context_summary":
            response = await self.generate(messages, **kwargs)
            yield StreamEvent(type="text", delta=response.content)
        else:
            self.requests.append([message.model_copy(deep=True) for message in messages])
            yield StreamEvent(type="text", delta="Analysis complete.")
        yield StreamEvent(type="finish", finish_reason="stop")


class Lifecycle(RunLifecycle):
    def __init__(self, *, steps=(), finishes=(), context=()):
        self.steps = list(steps)
        self.finishes = list(finishes)
        self.context = context
        self.results = []
        self.compactions = 0

    async def before_step(self, messages):
        return self.steps.pop(0) if self.steps else None

    async def before_finish(self, content):
        return self.finishes.pop(0) if self.finishes else None

    def context_messages(self):
        return self.context

    def observe_tool_result(self, event):
        self.results.append(event)

    def after_compaction(self):
        self.compactions += 1


async def run(runtime, lifecycle, *, history=None, provider=None, tools=(), **kwargs):
    provider = provider or Provider()
    skill_tool = GetSkillTool(runtime.loader)
    tool_map = {tool.name: tool for tool in (skill_tool, *tools)}
    history = history if history is not None else [Message(role="system", content="Be useful."),
                                                   Message(role="user", content="Complete the analysis.")]
    events = [event async for event in run_agent_loop(
        llm=provider, messages=history, tools=tool_map, skill_engine=runtime,
        run_lifecycle=lifecycle, artifact_detection_enabled=False,
        truncation_continuation_enabled=False, **kwargs)]
    return events, provider, history, tool_map


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_runtime_skill_read_after_real_summary_gets_fresh_budget_and_exact_body(runtime, selected):
    if selected:
        runtime.select(["analysis-method"])

    class Restore(Lifecycle):
        async def before_step(self, messages):
            if self.compactions and not self.results:
                return ExecutionAction(tool_calls=(read_call(),))
            return None

    life = Restore()
    history = [Message(role="system", content="Be useful."), Message(role="user", content="Old task."),
               Message(role="assistant", content="old execution " * 18000)]
    history.extend(Message(role="assistant", content=f"recent result {index}") for index in range(6))
    history.append(Message(role="user", content="Continue the analysis."))
    events, provider, _, tools = await run(runtime, life, history=history, token_limit=24000, max_steps=3)
    assert len(life.results) == 1 and life.results[0].success
    assert life.results[0].origin == "runtime"
    assert not life.results[0].raw_output["skill_reference"].get("reused")
    assert provider.summary_calls == 1 and len(provider.requests) == 1
    assert len([event for event in events if isinstance(event, SummarizationEvent)]) == 1
    assert "EXACT_analysis-method_BODY" in str(provider.requests[0])
    assert _fallback_context_estimate(provider.requests[0], tools) + 1024 <= 24000


@pytest.mark.asyncio
async def test_initial_runtime_read_uses_current_small_context_limit(runtime):
    runtime.loader.get_skill("analysis-method").skill_path.write_text(
        "---\nname: analysis-method\ndescription: analysis\n---\n" + "long method line\n" * 2000)
    life = Lifecycle(steps=[ExecutionAction(tool_calls=(read_call(),))])
    _, provider, history, tools = await run(runtime, life, token_limit=4000, max_steps=1)
    assert life.results[0].success
    assert life.results[0].raw_output["skill_reference"]["has_more"]
    assert _fallback_context_estimate(history, tools) + 1024 <= 4000
    assert provider.requests == []


@pytest.mark.asyncio
async def test_before_finish_batch_cannot_reuse_request_only_selected_body(runtime):
    runtime.select(["analysis-method"])
    life = Lifecycle(finishes=[ExecutionAction(tool_calls=(read_call(),))])
    _, provider, _, _ = await run(runtime, life, max_steps=3)
    assert len(provider.requests) == 2 and life.results[0].success
    assert not life.results[0].raw_output["skill_reference"].get("reused")
    assert "EXACT_analysis-method_BODY" in life.results[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("same_batch", [False, True])
async def test_runtime_batches_reset_once_while_pages_in_one_batch_share_allowance(runtime, same_batch):
    for name in ("analysis-method", "reference-guide"):
        runtime.loader.get_skill(name).skill_path.write_text(
            f"---\nname: {name}\ndescription: analysis\n---\n" + "line of method\n" * 2100)
    calls = (read_call(id="first"), read_call("reference-guide", id="second"))
    steps = ([ExecutionAction(tool_calls=calls)] if same_batch else
             [ExecutionAction(tool_calls=(call,)) for call in calls])
    life = Lifecycle(steps=steps)
    _, provider, _, _ = await run(runtime, life, token_limit=100000, max_steps=len(steps))
    assert len(life.results) == 2 and all(result.success for result in life.results)
    assert life.results[1].raw_output["skill_reference"]["has_more"] is same_batch
    assert not provider.requests


@pytest.mark.asyncio
async def test_runtime_read_reports_true_budget_exhaustion_without_delivering_selected_projection(runtime, monkeypatch):
    runtime.select(["analysis-method"])
    commits = []
    original = DefaultContextEngine.prepare_request

    def prepare(self, *args, **kwargs):
        projection = original(self, *args, **kwargs)
        return replace(projection, on_committed=lambda: commits.append(True))

    monkeypatch.setattr(DefaultContextEngine, "prepare_request", prepare)
    life = Lifecycle(steps=[ExecutionAction(tool_calls=(read_call(),))])
    events, provider, _, _ = await run(runtime, life, token_limit=2000, max_steps=1)
    assert len(life.results) == 1 and not life.results[0].success
    assert life.results[0].raw_output["code"] == "SKILL_CONTEXT_BUDGET"
    assert not commits and not runtime.turn_deliveries and not provider.requests
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == StopReason.MAX_STEPS


@pytest.mark.asyncio
@pytest.mark.parametrize("reducible", [False, True])
async def test_initial_runtime_batch_compacts_once_or_blocks_before_tool_execution(runtime, reducible):
    life = Lifecycle(steps=[ExecutionAction(tool_calls=(read_call(),))], context=(
        () if reducible else (Message(role="user", source="runtime", content="Required overlay " * 20000),)))
    history = [Message(role="system", content="Be useful."), Message(role="user", content="Old task."),
               Message(role="assistant", content="old execution " * 18000)]
    history.extend(Message(role="assistant", content=f"recent result {index}") for index in range(6))
    history.append(Message(role="user", content="Continue the analysis."))
    events, provider, _, _ = await run(runtime, life, history=history, token_limit=24000, max_steps=1)
    if reducible:
        assert len(life.results) == 1 and life.results[0].success
        assert provider.summary_calls == 1
        assert len([event for event in events if isinstance(event, SummarizationEvent)]) == 1
    else:
        assert not life.results
        assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == StopReason.ERROR
    assert not provider.requests


class ImageTool(Tool):
    name = "inspect_analysis_image"
    description = "Inspect an image for the analysis."
    parameters = {"type": "object", "properties": {}}
    transient_followup_allowed = True
    blocks = [{"type": "input_image", "media_type": "image/png", "data": "A" * 200000,
               "width": 64, "height": 64}]

    async def execute(self):
        return ToolResult(success=True, content="Image inspected.", transient_followup_content=self.blocks)


class SchemaTool(Tool):
    name = "analysis_receipt"
    description = "Read analysis receipt. " * 150
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}

    async def execute(self, value=""):
        return ToolResult(success=True, content="ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("same_batch", [False, True])
async def test_runtime_pages_reserve_live_schemas_overlays_images_and_all_pending_replies(runtime, same_batch):
    runtime.loader.get_skill("analysis-method").skill_path.write_text(
        "---\nname: analysis-method\ndescription: analysis\n---\n" + ('"\\方法 ' * 10 + '\n') * 3000)
    image = ToolCall(id="image", type="function", function=FunctionCall(name=ImageTool.name, arguments={}))
    tails = tuple(ToolCall(id=f"pending-receipt-{index}-" + "x" * 100, type="function",
                          function=FunctionCall(name=SchemaTool.name, arguments={"value": str(index)}))
                  for index in range(12))
    reads = (read_call(), *tails)
    steps = ([ExecutionAction(tool_calls=(image, *reads))] if same_batch else
             [ExecutionAction(tool_calls=(image,)), ExecutionAction(tool_calls=reads)])
    overlay = Message(role="user", source="runtime", content="Required task context. " * 100)
    life = Lifecycle(steps=steps, context=(overlay,))
    _, provider, history, tools = await run(runtime, life, tools=(ImageTool(), SchemaTool()),
                                           token_limit=9000, max_steps=len(steps))
    assert all(result.success for result in life.results)
    page = next(result for result in life.results if result.tool_name == "get_skill")
    assert page.raw_output["skill_reference"]["has_more"]
    transient = Message(role="user", source="runtime", content=ImageTool.blocks)
    assert _fallback_context_estimate([*history, overlay, transient], tools) + 1024 <= 9000
    assert not provider.requests


@pytest.mark.asyncio
async def test_runtime_batch_preserves_declared_transient_reserve_and_drops_unsent_references(runtime):
    from box_agent.tools.engine.preparation import prepare_tools

    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    tool = GetSkillTool(runtime.loader)
    prepared = prepare_tools([tool])
    history = [Message(role="user", content="analysis")]
    runtime.select(["analysis-method"])
    unsent = engine.prepare_request(history, prepared_tools=prepared, token_limit=20000)
    assert unsent.references
    image = Message(role="user", source="runtime", content=ImageTool.blocks)
    projection = engine.prepare_tool_batch(history, prepared_tools=prepared, token_limit=5000,
                                           transient_message=image, transient_tokens=5000)
    assert projection.blocked_reason
    assert not projection.references and projection.on_committed is None and projection.on_response is None
    read = engine.tool_reader("analysis-method")
    assert not read.success and read.raw_output["code"] == "SKILL_CONTEXT_BUDGET"
    assert runtime.turn_deliveries == {}


@pytest.mark.asyncio
async def test_cancellation_during_runtime_batch_summary_prevents_tool_and_provider(runtime):
    cancelled = False

    class CancellingProvider(Provider):
        async def generate(self, messages, **kwargs):
            nonlocal cancelled
            result = await super().generate(messages, **kwargs)
            if kwargs.get("call_kind") == "context_summary":
                cancelled = True
            return result

    life = Lifecycle(steps=[ExecutionAction(tool_calls=(read_call(),))])
    history = [Message(role="system", content="Be useful."), Message(role="user", content="Old task."),
               Message(role="assistant", content="old execution " * 18000)]
    history.extend(Message(role="assistant", content=f"recent result {index}") for index in range(6))
    history.append(Message(role="user", content="Continue the analysis."))
    events, provider, _, _ = await run(runtime, life, history=history, token_limit=24000, max_steps=1,
                                       provider=CancellingProvider(), is_cancelled=lambda: cancelled)
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == StopReason.CANCELLED
    assert not life.results and not provider.requests and not runtime.turn_deliveries


def history_with_old_usage(*, body="Previous response."):
    from box_agent.schema import TokenUsage

    return [Message(role="system", content="Be useful."), Message(role="user", content="Prior analysis."),
            Message(role="assistant", content=body, usage=TokenUsage(input_tokens=200, output_tokens=10)),
            Message(role="user", content="Continue the analysis.")]


@pytest.mark.asyncio
async def test_runtime_batch_rejects_new_huge_schema_despite_small_prior_api_usage(runtime):
    class HugeSchemaTool(SchemaTool):
        description = "Newly offered schema details. " * 2600
        executions = 0

        async def execute(self, value=""):
            self.executions += 1
            return await super().execute(value)

    tool = HugeSchemaTool()
    call = ToolCall(id="runtime-receipt", type="function", function=FunctionCall(
        name=tool.name, arguments={"value": "must not execute"}))
    life = Lifecycle(steps=[ExecutionAction(tool_calls=(call,))])
    events, provider, history, tools = await run(runtime, life, history=history_with_old_usage(),
                                                tools=(tool,), token_limit=5000, max_steps=1)
    assert _fallback_context_estimate(history, tools) > 5000
    assert tool.executions == 0 and not life.results
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == StopReason.ERROR
    assert provider.requests == []


def test_runtime_reader_rechecks_full_input_after_nonreference_results_with_old_api_usage(runtime):
    from box_agent.tools.engine.preparation import prepare_tools

    runtime.loader.get_skill("analysis-method").skill_path.write_text(
        "---\nname: analysis-method\ndescription: analysis\n---\n" + "method line\n" * 4000)
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    tool = GetSkillTool(runtime.loader)
    tools = {tool.name: tool}
    history = history_with_old_usage(body="earlier detail " * 1100)
    projection = engine.prepare_tool_batch(history, prepared_tools=prepare_tools([tool]), token_limit=10000)
    assert not projection.blocked_reason
    history.extend([
        Message(role="assistant", content="", tool_calls=[ToolCall(id="fresh", type="function",
            function=FunctionCall(name="analysis_receipt", arguments={"value": "new result"})), read_call()]),
        Message(role="tool", name="analysis_receipt", tool_call_id="fresh", content="fresh detail " * 300),
    ])
    assert _fallback_context_estimate(history, tools) + 1024 < 10000
    read = engine.tool_reader("analysis-method", usage="reference")
    assert read.success
    history.append(Message(role="tool", name="get_skill", tool_call_id="read", content=read.model_context))
    assert _fallback_context_estimate(history, tools) + 1024 <= 10000


def test_provider_preparation_retains_usage_authority_after_a_runtime_batch(runtime):
    from box_agent.tools.engine.preparation import prepare_tools

    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    tool = GetSkillTool(runtime.loader)
    tools = {tool.name: tool}
    history = history_with_old_usage(body="earlier detail " * 5000)
    prepared = prepare_tools([tool])
    batch = engine.prepare_tool_batch(history, prepared_tools=prepared, token_limit=5000)
    assert batch.budget_blocked
    request = engine.prepare_request(history, prepared_tools=prepared, token_limit=5000)
    assert not request.blocked_reason
    read = engine.tool_reader("analysis-method", usage="reference")
    assert read.success
    assert _fallback_context_estimate(history, tools) > 5000
