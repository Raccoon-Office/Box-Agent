"""Direct and discoverable local tools read their original state owners."""

from types import SimpleNamespace
from pathlib import Path

import pytest

from box_agent.agent import Agent
from box_agent.tools.bash_tool import BackgroundShell, BackgroundShellManager, BashOutputTool, BashKillTool
from box_agent.tools.plan_tool import PlanStore, PlanReadTool, PlanWriteTool
from box_agent.tools.todo_tool import TodoStore, TodoReadTool, TodoWriteTool
from tests.test_local_tool_search import LocalTool


def visible(agent):
    return agent.mcp_tool_exposure.prepare_tools(list(agent.tools.values())).offered_names


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred_mcp", [False, True])
async def test_agent_without_mcp_can_discover_local_tools_without_losing_child_capabilities(tmp_path, deferred_mcp):
    append = LocalTool("append_file", "Append text")
    read = LocalTool("read_file", "Read text")
    agent = Agent(llm_client=object(), system_prompt="test", tools=[read, append],
                  workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=deferred_mcp)
    assert visible(agent) == frozenset({"read_file", "tool_search"})
    assert agent._inherited_tools()[append.name] is append
    result = await agent.tools["tool_search"].invoke({"tool_names": ["append_file"]})
    assert result.success
    assert "append_file" in visible(agent)
    assert "goal_read" not in visible(agent)


@pytest.mark.asyncio
async def test_plan_and_todo_state_make_both_tools_direct_and_preserve_exact_stores(tmp_path):
    plan, todo = PlanStore(), TodoStore()
    tools = [PlanReadTool(plan), PlanWriteTool(plan), TodoReadTool(todo), TodoWriteTool(todo)]
    agent = Agent(llm_client=object(), system_prompt="test", tools=tools, workspace_dir=str(tmp_path))
    assert visible(agent) == frozenset({"tool_search"})
    await agent.tools["plan_write"].invoke({"action": "set", "title": "Plan", "steps": [{"title": "Verify"}]})
    result = await agent.tools["todo_write"].invoke({"action": "set", "todos": [{"task": "Verify", "status": "in_progress"}]})
    assert result.success
    assert {"plan_read", "plan_write", "todo_read", "todo_write"} <= visible(agent)
    assert agent.tools["plan_read"]._store is plan
    assert agent.tools["todo_read"]._store is todo


@pytest.mark.parametrize("status", ["active", "paused", "blocked", "completed"])
def test_any_restored_goal_status_keeps_goal_management_direct(tmp_path, status):
    agent = Agent(llm_client=object(), system_prompt="test", tools=[], workspace_dir=str(tmp_path))
    assert "goal_read" not in visible(agent)
    agent.goal = SimpleNamespace(status=status)
    assert {"goal_read", "goal_write"} <= visible(agent)


def test_trusted_requirement_and_skill_hints_only_expose_allowed_real_tools(tmp_path):
    tools = [LocalTool("mcp_config"), LocalTool("plan_write"), LocalTool("plan_read")]
    agent = Agent(llm_client=object(), system_prompt="test", tools=tools, workspace_dir=str(tmp_path))
    assert visible(agent) == frozenset({"tool_search"})
    agent.local_tool_exposure.require_tools(["plan_write", "plan_read", "not_allowed"])
    agent.activate_skill_instructions("browser-use", "Original loaded guidance")
    assert visible(agent) == frozenset({"tool_search", "mcp_config", "plan_write", "plan_read"})
    assert "not_allowed" not in agent.tools


@pytest.mark.parametrize(("skill_name", "expected"), [
    ("obsidian", {"obsidian_create_note", "obsidian_update_note", "obsidian_daily_note"}),
    ("hyperframes-video", {"append_file"}),
    ("roadmap", set()),
])
def test_original_skill_tool_hints_follow_method_requirements_not_skill_name(tmp_path, skill_name, expected):
    available = {
        "append_file", "obsidian_create_note", "obsidian_update_note", "obsidian_daily_note",
        "plan_read", "plan_write", "todo_read", "todo_write",
    }
    agent = Agent(llm_client=object(), system_prompt="test",
                  tools=[LocalTool(name) for name in sorted(available)], workspace_dir=str(tmp_path))
    skill_path = Path(__file__).parents[1] / "box_agent" / "skills" / skill_name / "SKILL.md"
    original_method = skill_path.read_text(encoding="utf-8")
    assert all(name in original_method for name in expected)
    agent.activate_skill_instructions(skill_name, original_method)
    assert visible(agent) == frozenset({"tool_search", *expected})


def test_owned_completed_unread_shell_exposes_output_and_kill_without_other_owner_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(BackgroundShellManager, "_shells", {})
    shell = BackgroundShell("shell-1", "command", SimpleNamespace(), 0.0, owner_id="other")
    shell.status = "completed"
    shell.add_output("unread result")
    BackgroundShellManager.add(shell)
    agent = Agent(llm_client=object(), system_prompt="test", tools=[
        BashOutputTool(process_owner_id="mine"), BashKillTool(process_owner_id="mine"),
    ], workspace_dir=str(tmp_path))
    assert "bash_output" not in visible(agent)
    shell.owner_id = "mine"
    assert {"bash_output", "bash_kill"} <= visible(agent)
    BackgroundShellManager._remove(shell.bash_id)
    assert {"bash_output", "bash_kill"} <= visible(agent)


@pytest.mark.parametrize("heading", ["## Deferred MCP tools", "## Discoverable tools"])
def test_child_prompt_does_not_inherit_parent_discovery_instructions(heading):
    from box_agent.tools.sub_agent_tool import _child_safe_parent_prompt

    prompt = f"Parent constraints.\n\n{heading}\nUse `tool_search` to activate anything.\n\n## Safety\nKeep this boundary."
    child_prompt = _child_safe_parent_prompt(prompt)
    assert "Use `tool_search` to activate anything." not in child_prompt
    assert "Parent constraints." in child_prompt
    assert "Keep this boundary." in child_prompt
    assert "`tool_search` is not available here" in child_prompt
