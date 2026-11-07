"""Original table validation must precede execution, including restored gates."""
import json

import pytest

from box_agent.schema import LLMResponse, Message
from box_agent.events import ToolCallResult
from box_agent.runtime import run_agent_loop
from box_agent.tools.base import Tool, ToolInvocationContext, ToolResult
from box_agent.tools.engine.execution import invoke_tool_once
from box_agent.tools.file.validate_table_tool import TableCompletionGate
from tests.test_validate_table_tool import setup_gate, ScriptedLLM, scripted_call
from tests.test_table_validation import FIELDS, row, write_table


class Executor(Tool):
    description = "Test execution with an observable side effect."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name="execute_code"):
        self._name = name
        self.calls = 0

    @property
    def name(self):
        return self._name

    async def execute(self):
        self.calls += 1
        return ToolResult(success=True, content="executed")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["execute_code", "bash", "sub_agent"])
async def test_execution_has_no_side_effect_until_all_inputs_are_validated(tmp_path, name):
    validator, gate, path = await setup_gate(tmp_path)
    other = tmp_path / "other.csv"
    write_table(other, [row(3)])
    gate.sources[other] = None
    executor = Executor(name)
    context = ToolInvocationContext(capability_gates=(gate,))
    denied = await invoke_tool_once(executor, {}, context)
    assert not denied.success and denied._invocation_rejected and executor.calls == 0
    assert "TABLE_VALIDATION_REQUIRED_BEFORE_EXECUTION" in denied.error
    for source in (path, other):
        validated = await validator.invoke({"path": str(source), "fields": FIELDS},
                                          context=ToolInvocationContext(completion_gate=gate))
        assert validated.success
        result = await invoke_tool_once(executor, {}, context)
        assert result.success is (source == other)
    assert executor.calls == 1
    write_table(path, [row(9)])
    denied = await invoke_tool_once(executor, {}, context)
    assert not denied.success and "TABLE_SOURCE_UNVERIFIED" in denied.error
    assert executor.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["read_file", "get_skill", "list_skills", "tool_search", "sales_reference_rates"])
async def test_reading_and_capability_discovery_are_available_before_validation(tmp_path, name):
    _, gate, _ = await setup_gate(tmp_path)
    executor = Executor(name)
    assert (await invoke_tool_once(executor, {}, ToolInvocationContext(capability_gates=(gate,)))).success
    assert executor.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose,mode,allowed", [
    ("computation", "input", False), ("diagnosis", "input", True),
    ("computation", "edit_target", True),
])
async def test_invalid_originals_allow_diagnosis_or_declared_repair_only(tmp_path, purpose, mode, allowed):
    validator, gate, path = await setup_gate(tmp_path, rows=[row(1, amount="NaN")])
    await validator.invoke({"path": str(path), "fields": FIELDS, "purpose": purpose, "source_mode": mode},
                           context=ToolInvocationContext(completion_gate=gate))
    # Checkpoint round trip uses the same trusted receipts, not public result JSON.
    restored = TableCompletionGate.restore(validator, gate.context, json.loads(json.dumps(gate.snapshot())))
    executor = Executor()
    result = await invoke_tool_once(executor, {}, ToolInvocationContext(capability_gates=(restored,)))
    assert result.success is allowed and executor.calls == int(allowed)


@pytest.mark.asyncio
async def test_missing_or_disabled_gate_preserves_execution_and_gate_errors_fail_closed():
    executor = Executor()
    assert (await invoke_tool_once(executor, {})).success
    class Broken:
        async def check_invocation(self, name, arguments):
            raise ValueError("unavailable evidence")
    result = await invoke_tool_once(executor, {}, ToolInvocationContext(capability_gates=(Broken(),)))
    assert not result.success and "CAPABILITY_CHECK_FAILED" in result.error
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_real_engine_blocks_then_retries_after_validation(tmp_path, monkeypatch):
    from box_agent.turn_continuation import TurnContinuationController
    async def no_judge(*args, **kwargs):
        return None
    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    validator, _, path = await setup_gate(tmp_path)
    executor = Executor()
    responses = [scripted_call(executor.name, {}, "early"),
                 scripted_call(validator.name, {"path": str(path), "fields": FIELDS}, "validate"),
                 scripted_call(executor.name, {}, "ready"),
                 LLMResponse(content="done", finish_reason="stop")]
    events = [e async for e in run_agent_loop(
        llm=ScriptedLLM(responses), messages=[Message(role="user", content=f"Process {path}")],
        tools={validator.name: validator, executor.name: executor}, max_steps=5, workspace_dir=str(tmp_path),
    )]
    results = {e.tool_call_id: e for e in events if isinstance(e, ToolCallResult)}
    assert not results["early"].success
    assert results["validate"].success and results["ready"].success
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_discovered_executor_cannot_bypass_gate_through_call_tool(tmp_path, monkeypatch):
    from collections import OrderedDict
    from box_agent.schema import FunctionCall, StreamEvent, ToolCall
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
    from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
    from box_agent.tools.tool_call_tool import CallTool
    from box_agent.turn_continuation import TurnContinuationController
    from box_agent.tools import local_tool_exposure
    monkeypatch.setattr(local_tool_exposure, "DISCOVERABLE_LOCAL_NAMES",
                        local_tool_exposure.DISCOVERABLE_LOCAL_NAMES | {"sub_agent"})
    async def no_judge(*args, **kwargs):
        return None
    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    validator, _, path = await setup_gate(tmp_path)
    executor = Executor("sub_agent")
    catalog, active = MCPToolCatalog(), OrderedDict()
    search = ToolSearchTool(catalog, active)
    exposure = MCPToolExposureManager(catalog, active)
    exposure.enable_stable_prefix(search)
    tools = {t.name: t for t in [validator, executor, search, CallTool()]}
    class LLM:
        step = 0
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.step += 1
            if self.step == 1:
                name, arguments = "tool_search", {"tool_names": [executor.name]}
            elif self.step == 3:
                name, arguments = "validate_table", {"path": str(path), "fields": FIELDS}
            elif self.step in (2, 4):
                result = next(m for m in messages if m.tool_call_id == "1" and m.role == "tool")
                ref = json.loads(result.content)["activated"][0]["tool_ref"]
                name, arguments = "call_tool", {"tool_ref": ref, "arguments_json": "{}"}
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                ToolCall(id=str(self.step), type="function", function=FunctionCall(name=name, arguments=arguments))])
    events = [e async for e in run_agent_loop(
        llm=LLM(), messages=[Message(role="user", content=f"Process {path}")],
        tools=tools, max_steps=5, workspace_dir=str(tmp_path), tool_exposure_manager=exposure,
    )]
    results = {e.tool_call_id: e for e in events if isinstance(e, ToolCallResult)}
    assert results["2"].tool_name == "sub_agent" and not results["2"].success
    assert "TABLE_VALIDATION_REQUIRED_BEFORE_EXECUTION" in results["2"].error
    assert results["4"].success and executor.calls == 1
