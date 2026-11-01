import pytest

from box_agent.tools.completion_checks import CompletionChecks, CompletionContext, CompletionIssue
from box_agent.tools.base import Tool


class GatedTool(Tool):
    name = "gated"

    async def create_completion_gate(self, context):
        class Gate:
            passed = False

            async def check(self):
                return None if self.passed else CompletionIssue("MISSING", "Run the check")
        return Gate()


@pytest.mark.asyncio
async def test_completion_proof_is_private_to_each_run_and_aliases_share_one_gate():
    tool = GatedTool()
    context = CompletionContext((), lambda: False)
    first = await CompletionChecks.create({"gated": tool, "alias": tool}, context)
    second = await CompletionChecks.create({"gated": tool}, context)
    assert len(first.gates) == 1
    first.gates["gated"].passed = True
    # Supplements can arrive through an alias without replacing the receipt or
    # asking ordinary tools to implement completion-specific properties.
    await first.include_inputs({"alias": tool, "ordinary": Tool(), "gated": tool}, context)
    assert await first.check() == []
    assert (await second.check())[0].code == "MISSING"


@pytest.mark.asyncio
async def test_ordinary_tool_has_no_completion_requirement():
    checks = await CompletionChecks.create({"ordinary": Tool()}, CompletionContext((), lambda: False))
    assert checks.gates == {} and await checks.check() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["creation", "check"])
async def test_failed_completion_check_is_fatal_not_silent_success(stage):
    class BrokenTool(GatedTool):
        async def create_completion_gate(self, context):
            if stage == "creation":
                raise ValueError("failed factory")
            return self

        async def check(self):
            raise ValueError("failed check")
    checks = await CompletionChecks.create({"gated": BrokenTool()}, CompletionContext((), lambda: False))
    issues = await checks.check()
    assert len(issues) == 1 and not issues[0].repairable
    assert issues[0].code == "COMPLETION_CHECK_FAILED"
