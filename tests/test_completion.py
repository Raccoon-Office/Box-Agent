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
async def test_uncommitted_write_requirement_survives_pause_and_does_not_leak_to_new_task(tmp_path):
    from box_agent.tools.base import ToolInvocationContext
    from box_agent.tools.file_tools import WriteTool

    tool, scope = WriteTool(str(tmp_path)), []
    context = CompletionContext((), lambda: False, resume_scope=scope)
    original = await CompletionChecks.create({tool.name: tool}, context)
    await tool.invoke(
        {"path": "result.json", "content": '{"rows":[', "final": False},
        context=ToolInvocationContext(completion_gate=original.gates[tool.name]),
    )
    original.suspend()
    tool.cleanup_pending_writes()

    unrelated = await CompletionChecks.create(
        {tool.name: tool}, CompletionContext((), lambda: False, resume_scope=[]),
    )
    assert await unrelated.check() == []
    resumed = await CompletionChecks.create({tool.name: tool}, context)
    issue = (await resumed.check())[0]
    assert issue.code == "WRITE_FILE_UNCOMMITTED"
    assert "chunk_index=0" in issue.message
    await tool.invoke(
        {"path": "result.json", "content": '{"rows":[]}'},
        context=ToolInvocationContext(completion_gate=resumed.gates[tool.name]),
    )
    assert await resumed.check() == []
    resumed.close()
    assert await (await CompletionChecks.create({tool.name: tool}, context)).check() == []


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


class ResumableTool(GatedTool):
    async def create_completion_gate(self, context):
        gate = await super().create_completion_gate(context)
        gate.snapshot = lambda: {"passed": gate.passed}
        return gate

    async def restore_completion_gate(self, context, state):
        gate = await self.create_completion_gate(context)
        gate.passed = state["passed"]
        return gate


@pytest.mark.asyncio
async def test_in_memory_checkpoint_is_scoped_and_ends_with_the_task():
    tool, scope = ResumableTool(), []
    context = CompletionContext((), lambda: False, resume_scope=scope)
    original = await CompletionChecks.create({"gated": tool}, context)
    original.gates["gated"].passed = True
    original.suspend()
    other_context = CompletionContext((), lambda: False, resume_scope=[])
    unrelated = await CompletionChecks.create({"gated": tool}, other_context)
    assert (await unrelated.check())[0].code == "MISSING"
    unrelated.gates["gated"].passed = True
    unrelated.suspend()
    resumed = await CompletionChecks.create({"alias": tool}, context)
    assert await resumed.check() == []
    resumed.close()
    fresh = await CompletionChecks.create({"gated": tool}, context)
    assert (await fresh.check())[0].code == "MISSING"
    other_resumed = await CompletionChecks.create({"gated": tool}, other_context)
    assert await other_resumed.check() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_tool", "unknown_version"])
async def test_unrestorable_checkpoint_blocks_completion(tmp_path, failure):
    from box_agent.session_log import SessionLog
    log = SessionLog.create(tmp_path / "logs", session_id="check", cwd=tmp_path)
    context = CompletionContext((), lambda: False, log)
    try:
        tool = ResumableTool()
        checks = await CompletionChecks.create({"gated": tool}, context)
        checks.suspend()
        if failure == "unknown_version":
            context.record("completion.paused", {"version": 999, "checks": {}})
        restored = await CompletionChecks.create({}, context)
        assert (await restored.check())[0].code == "COMPLETION_RESTORE_FAILED"
    finally:
        log.close()
