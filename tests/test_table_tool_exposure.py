"""validate_table is exposed on demand by the same detection that enforces it.

Ordinary sessions do not carry its schema. A turn with an explicit CSV/TSV
input receives the full description as a runtime message (the provider tool
array stays fixed) and the execution gate still applies to routed calls.
"""
import json

import pytest

from box_agent.agent import Agent
from box_agent.events import DoneEvent, ToolCallResult
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.tools.file.validate_table_tool import ValidateTableTool
from tests.test_table_execution_gate import Executor
from tests.test_table_validation import FIELDS, row, write_table


def _tool_names(tools):
    return {getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
            for tool in tools or ()}


def _validator_ref(messages):
    for message in messages:
        if getattr(message, "name", None) != "tool_descriptions" or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("name") == "validate_table":
            return payload["tool_ref"], payload
    return None, None


class RecordingLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self, script):
        self.script = script
        self.requests = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append((list(messages), _tool_names(tools)))
        step = self.script(list(messages), len(self.requests))
        if step is None:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")
            return
        name, arguments, call_id = step
        yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
            ToolCall(id=call_id, type="function", function=FunctionCall(name=name, arguments=arguments))])


def _agent(tmp_path, llm, executor=None):
    tools = [ValidateTableTool(str(tmp_path), allow_full_access=False)]
    if executor is not None:
        tools.append(executor)
    return Agent(llm_client=llm, system_prompt="system", tools=tools, workspace_dir=str(tmp_path),
                 deferred_mcp_loading_enabled=False, tool_interface_version=2, max_steps=6)


@pytest.fixture(autouse=True)
def _no_judge(monkeypatch):
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None
    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)


@pytest.mark.asyncio
async def test_ordinary_turn_carries_no_validator_schema(tmp_path):
    llm = RecordingLLM(lambda messages, step: None)
    agent = _agent(tmp_path, llm)
    agent.add_user_message("帮我写一段关于春天的短文")
    [event async for event in agent.run_events()]
    messages, tools = llm.requests[0]
    assert "validate_table" not in tools and "call_tool" in tools
    assert _validator_ref(messages) == (None, None)


@pytest.mark.asyncio
async def test_table_turn_delivers_validator_and_gate_still_orders_execution(tmp_path):
    path = tmp_path / "input.csv"
    write_table(path, [row(1), row(2)])
    executor = Executor("execute_code")

    def script(messages, step):
        ref, _ = _validator_ref(messages)
        if step == 1:
            return "execute_code", {}, "early"
        if step == 2:
            return "call_tool", {"tool_ref": ref, "arguments_json": json.dumps(
                {"path": str(path), "fields": FIELDS})}, "validate"
        if step == 3:
            return "execute_code", {}, "ready"
        return None

    llm = RecordingLLM(script)
    agent = _agent(tmp_path, llm, executor)
    agent.add_user_message(f"Process `{path}` completely")
    events = [event async for event in agent.run_events()]
    results = {e.tool_call_id: e for e in events if isinstance(e, ToolCallResult)}

    first_messages, first_tools = llm.requests[0]
    ref, description = _validator_ref(first_messages)
    assert ref is not None and "Validate EVERY original CSV/TSV record" in description["description"]
    assert all("validate_table" not in tools for _, tools in llm.requests)  # tool array stays fixed
    assert not results["early"].success
    assert "TABLE_VALIDATION_REQUIRED_BEFORE_EXECUTION" in results["early"].error
    assert "tool_search" in results["early"].error
    assert results["validate"].success and results["validate"].tool_name == "validate_table"
    assert results["ready"].success and executor.calls == 1
    assert next(e for e in events if isinstance(e, DoneEvent)).stop_reason.value == "end_turn"


@pytest.mark.asyncio
async def test_table_supplied_in_a_later_turn_delivers_validator_then(tmp_path):
    path = tmp_path / "later.csv"
    write_table(path, [row(1)])
    llm = RecordingLLM(lambda messages, step: None)
    agent = _agent(tmp_path, llm)
    agent.add_user_message("你好，等下给你一个表")
    [event async for event in agent.run_events()]
    assert _validator_ref(llm.requests[-1][0]) == (None, None)
    agent.add_user_message(f"表在这里：`{path}`，请汇总")
    [event async for event in agent.run_events()]
    assert _validator_ref(llm.requests[-1][0])[0] is not None
