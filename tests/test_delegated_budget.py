"""Hard quotas govern actual child execution, independently of result reports."""
import asyncio

import pytest

from box_agent.tools.base import Tool, ToolInvocationContext, ToolResult
from box_agent.tools.delegated_budget import DelegatedBudget, bind_budgets, current_budgets
from box_agent.tools.engine.execution import invoke_tool_once
from box_agent.tools.engine.budget import ToolBudgetState
from box_agent.tools.sub_agent_tool import SubAgentTool


class CountingTool(Tool):
    name = "count"
    description = "Count executions"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = 0

    async def execute(self):
        self.calls += 1
        await asyncio.sleep(0)
        return ToolResult(success=True)


@pytest.mark.asyncio
async def test_parallel_builtin_children_share_one_hard_quota(tmp_path, monkeypatch):
    leaf = CountingTool()
    child = SubAgentTool(llm=object(), parent_tools={leaf.name: leaf}, workspace_dir=str(tmp_path))
    quota = DelegatedBudget(3)

    async def run_child(**kwargs):
        results = await asyncio.gather(*(invoke_tool_once(leaf, {}) for _ in range(3)))
        return ToolResult(success=True, raw_output={"executed": sum(r.success for r in results)})

    monkeypatch.setattr(child, "_run_general_loop", run_child)
    results = await asyncio.gather(*(
        invoke_tool_once(child, {"task": "Count", "required_tools": []},
                         ToolInvocationContext(child_budgets=(quota,)))
        for _ in range(2)
    ))
    assert all(r.success for r in results), results
    assert sum(r.raw_output["executed"] for r in results) == 3
    assert leaf.calls == quota.used == 3
    assert current_budgets() == ()


@pytest.mark.asyncio
async def test_nested_scopes_must_satisfy_every_ancestor():
    outer, inner = DelegatedBudget(2), DelegatedBudget(1)
    tool = CountingTool()
    with bind_budgets((outer, inner, outer)):
        assert (await invoke_tool_once(tool, {})).success
        assert not (await invoke_tool_once(tool, {})).success
    with bind_budgets((outer,)):
        assert (await invoke_tool_once(tool, {})).success
        assert not (await invoke_tool_once(tool, {})).success
    assert (outer.used, inner.used, tool.calls) == (2, 1, 2)


@pytest.mark.asyncio
async def test_permission_attempt_releases_quota_and_retry_charges_once():
    class PermissionTool(CountingTool):
        approved = False

        def approve_permission_request(self, request):
            self.approved = True

        async def execute(self):
            if not self.approved:
                return ToolResult(success=False, permission_request={"scope": "test"})
            return await super().execute()

    tool, quota, context = PermissionTool(), DelegatedBudget(1), ToolInvocationContext()
    with bind_budgets((quota,)):
        result = await invoke_tool_once(tool, {}, context)
        assert result.permission_request and quota.used == 0
        assert (await invoke_tool_once(tool, {}, context,
                                      approved_permission_request=result.permission_request)).success
        assert quota.used == tool.calls == 1
        tool.approved = False
        denied = await invoke_tool_once(tool, {}, approved_permission_request={"scope": "test"})
        assert not denied.success and not tool.approved


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_entered_failed_or_cancelled_execution_keeps_charge(failure):
    class BrokenTool(CountingTool):
        async def execute(self):
            raise failure()

    quota = DelegatedBudget(1)
    with bind_budgets((quota,)):
        with pytest.raises(failure):
            await invoke_tool_once(BrokenTool(), {})
    assert quota.used == 1
    assert current_budgets() == ()


@pytest.mark.asyncio
async def test_invalid_arguments_and_unsupported_delegation_do_not_execute():
    quota, tool = DelegatedBudget(2), CountingTool()
    tool.parameters = {"type": "object", "required": ["value"]}
    with bind_budgets((quota,)):
        assert not (await invoke_tool_once(tool, {})).success
        tool.name = "sub_agent"
        rejected = await invoke_tool_once(tool, {"value": 1})
        assert "DELEGATED_BUDGET_UNSUPPORTED" in rejected.error
    assert quota.used == tool.calls == 0


@pytest.mark.asyncio
async def test_unconfigured_delegation_retains_legacy_behavior():
    tool = CountingTool()
    tool.name = "sub_agent"
    assert (await invoke_tool_once(tool, {})).success


def test_duplicate_result_reports_are_statistics_only():
    budget = ToolBudgetState({}, None, None, 3)
    report = {"type": "sub_agent_delegation", "tool_calls": 100}
    budget.record_delegated_tool_budget("sub_agent", report, "child")
    budget.record_delegated_tool_budget("sub_agent", report, "child")
    assert budget.delegated_tool_call_total == 100
    assert budget.reserve("sub_agent")[0]


@pytest.mark.asyncio
async def test_kernel_propagates_parent_quota_to_builtin_child(tmp_path, monkeypatch):
    from box_agent.core import run_agent_loop
    from box_agent.schema import Message, StreamEvent, ToolCall, FunctionCall

    class Model:
        requests = 0

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests += 1
            if self.requests == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id=f"child-{i}", type="function", function=FunctionCall(
                        name="sub_agent", arguments={"task": f"Count {i}", "required_tools": []},
                    )) for i in range(2)
                ])
            else:
                yield StreamEvent(type="finish", finish_reason="stop")

    leaf, model = CountingTool(), Model()
    child = SubAgentTool(llm=model, parent_tools={}, workspace_dir=str(tmp_path))

    async def run_child(**kwargs):
        for _ in range(5):
            await invoke_tool_once(leaf, {})
        return ToolResult(success=True)

    monkeypatch.setattr(child, "_run_general_loop", run_child)
    _ = [event async for event in run_agent_loop(
        llm=model, tools={"sub_agent": child},
        messages=[Message(role="user", content="Count twice")],
        max_steps=3, max_delegated_tool_calls=3, workspace_dir=str(tmp_path),
    )]
    assert leaf.calls == 3
