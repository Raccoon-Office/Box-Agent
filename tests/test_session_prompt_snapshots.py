"""Golden snapshots of the assembled session system prompt per entry point.

``prepare_prompt`` is exercised for every adapter profile (CLI, ACP, Python
SDK) and session mode, so any change to what the model receives shows up as
a reviewable diff of ``tests/fixtures/session_prompts/*.md``.

Machine-specific inputs are pinned: the date is fixed, the workspace path is
replaced by ``<WORKSPACE>``, image generation is unconfigured, and the
runtime-discovery block (python/node paths) is a marker. Everything else is
the real prompt text.

Regenerate after an intentional change with::

    BOX_AGENT_UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest tests/test_session_prompt_snapshots.py
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import box_agent.session_assembly as session_assembly
import box_agent.tools.runtime as runtime_module
import box_agent.tools.setup as tools_setup
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.env_context import EnvContext
from box_agent.plugins.builtins import SessionResources
from box_agent.session_context import HostBindings, SessionContext, SessionOptions
from box_agent.tools.image_generation_tool import _API_KEY_ENV, _ENDPOINT_ENV
from box_agent.tools.permissions import CapabilityPolicy

SNAPSHOT_DIR = Path(__file__).parent / "fixtures" / "session_prompts"
UPDATE = os.environ.get("BOX_AGENT_UPDATE_PROMPT_SNAPSHOTS") == "1"
CONFIG_DIR = Path(__file__).resolve().parents[1] / "box_agent" / "config"
FIXED_DATE = dt.date(2026, 1, 2)
SKILL_RUNTIME_MARKER = "## Skill Runtime Context\n<skill runtime facts>"

# Tool names a default session registers (plus ripgrep-backed grep/glob and a
# configured image service). Prompts only mention tools the session has.
FULL_TOOLS = frozenset("""
    append_file bash bash_kill bash_output create_scheduled_task edit_file execute_code generate_image
    get_skill glob goal_read goal_write grep inspect_images list_skills mcp_config memory_read memory_search
    memory_write plan_read plan_write publish_artifact query_jsonl read_file report_execution_result
    request_user_decision request_user_input sandbox_status search_files sub_agent todo_read todo_write
    tool_search write_file
""".split())
# No image service, no ripgrep, planning/delegation and bash disabled by config.
MINIMAL_TOOLS = FULL_TOOLS - {
    "generate_image", "glob", "grep", "plan_read", "plan_write", "todo_read", "todo_write",
    "sub_agent", "bash", "bash_output", "bash_kill",
}

# name -> (profile, session_mode, utility, sandbox_mode, follow_up, tools)
CASES = {
    "acp_general": ("acp", None, False, True, False, FULL_TOOLS),
    "acp_general_follow_up": ("acp", None, False, True, True, FULL_TOOLS),
    "acp_code_agent": ("acp", "code_agent", False, True, False, FULL_TOOLS),
    "acp_data_analysis": ("acp", "data_analysis", False, True, False, FULL_TOOLS),
    "acp_utility": ("acp", None, True, True, False, frozenset()),
    "cli_general": ("cli", None, False, True, False, FULL_TOOLS),
    "cli_general_no_sandbox": ("cli", None, False, False, False, FULL_TOOLS - {"execute_code"}),
    "cli_code_agent": ("cli", "code_agent", False, True, False, FULL_TOOLS),
    "python_general": ("python", None, False, False, False, FULL_TOOLS - {"execute_code"}),
    "python_code_agent": ("python", "code_agent", False, False, False, FULL_TOOLS - {"execute_code"}),
    "acp_general_minimal_tools": ("acp", None, False, True, False, MINIMAL_TOOLS),
    "acp_code_agent_minimal_tools": ("acp", "code_agent", False, True, False, MINIMAL_TOOLS),
    "acp_general_with_policy": ("acp", None, False, True, False, FULL_TOOLS),
}
# Cases assembled with an officev3-style filesystem policy (the ACP main path).
POLICY_CASES = {"acp_general_with_policy"}
EXTRA_ALLOWED_DIRECTORY = "/Users/demo/Documents"


class _Memory:
    def read_core(self) -> str:
        return "name: snapshot user, prefers concise answers in Chinese"

    def recall(self) -> str:
        from box_agent.memory import MemoryManager

        return MemoryManager.build_memory_block("# Core Memory\n- 姓名：快照用户\n- 偏好：中文回复")


def _template() -> str:
    """The ACP server reads system_prompt.md once and fills the skills slot."""
    return (CONFIG_DIR / "system_prompt.md").read_text(encoding="utf-8").replace("{SKILLS_METADATA}", "")


@pytest.fixture
def pinned_environment(monkeypatch):
    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return FIXED_DATE

    monkeypatch.setattr(tools_setup, "date", FixedDate)
    for name in (*_ENDPOINT_ENV, *_API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime_module, "build_skill_runtime_prompt", lambda _ctx: SKILL_RUNTIME_MARKER)
    monkeypatch.setattr(session_assembly, "build_skill_runtime_prompt", lambda _ctx: SKILL_RUNTIME_MARKER)


async def _assemble(name: str, workspace: Path) -> str:
    profile, session_mode, utility, sandbox_mode, follow_up, tools = CASES[name]
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(
            workspace_dir=str(workspace),
            system_prompt_path=str(CONFIG_DIR / "system_prompt.md"),
            code_prompt_path=str(CONFIG_DIR / "code_prompt.md"),
            analysis_prompt_path=str(CONFIG_DIR / "analysis_prompt.md"),
        ),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False),
    )
    env_context = EnvContext.from_meta({
        "platform": "darwin",
        "cli": {"git": "/usr/bin/git"},
        "browser_tools": {"installed": True, "enabled": True, "available": True},
    })
    policy = None
    if name in POLICY_CASES:
        policy = CapabilityPolicy().with_filesystem_overrides(
            session_workspace_root=str(workspace),
            allowed_directories=[EXTRA_ALLOWED_DIRECTORY],
            filesystem_scope="session_workspace",
            replace_allowed_directories=True,
        )
    options = SessionOptions(
        profile=profile,
        effective_policy=policy,
        workspace_dir=workspace,
        utility=utility,
        sandbox_mode=sandbox_mode,
        session_mode=session_mode,
        follow_up_suggestions_enabled=follow_up,
        state={"env_context": env_context, "skill_runtime_context": object()},
    )
    host = HostBindings(system_prompt=_template() if profile == "acp" else None)
    resources = SessionResources(context=SessionContext(config=config, options=options, host=host))
    resources.memory_manager = _Memory()
    resources.tools = [SimpleNamespace(name=tool) for tool in sorted(tools)]
    await session_assembly.prepare_prompt(resources)
    return resources.system_prompt


def _normalize(prompt: str, workspace: Path) -> str:
    text = prompt.replace(str(workspace.resolve()), "<WORKSPACE>").replace(str(workspace), "<WORKSPACE>")
    return re.sub(r"[ \t]+\n", "\n", text).rstrip() + "\n"


@pytest.mark.parametrize("name", sorted(CASES))
async def test_session_prompt_matches_snapshot(name, tmp_path, pinned_environment):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    actual = _normalize(await _assemble(name, workspace), workspace)

    snapshot = SNAPSHOT_DIR / f"{name}.md"
    if UPDATE:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(actual, encoding="utf-8")
    assert snapshot.exists(), f"missing snapshot {snapshot}; regenerate with BOX_AGENT_UPDATE_PROMPT_SNAPSHOTS=1"
    assert actual == snapshot.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(CASES))
async def test_session_prompt_has_no_unrendered_placeholders(name, tmp_path, pinned_environment):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = await _assemble(name, workspace)

    assert not re.findall(r"\{[A-Z_]{3,}\}|\{\{\.[A-Za-z]+\}\}", prompt)


# Every tool name a session could have. A backticked mention of one of these is
# an instruction to use it, unless the clause is a prohibition ("不要用 X").
KNOWN_TOOL_NAMES = FULL_TOOLS | {"install_skillhub_skill", "search_skillhub", "web_extract", "web_search"}
_NEGATION = re.compile(r"不要|禁止|不得|勿|Do not|Do NOT|don't", re.IGNORECASE)


def _instructed_tools(prompt: str) -> dict[str, str]:
    """Map tool name -> first clause that tells the model to use it."""
    found: dict[str, str] = {}
    for clause in re.split(r"[。；\n]", prompt):
        if _NEGATION.search(clause):
            continue
        for name in re.findall(r"`([a-z_]+)", clause):
            if name in KNOWN_TOOL_NAMES:
                found.setdefault(name, clause.strip())
    return found


@pytest.mark.parametrize("name", sorted(name for name, case in CASES.items() if not case[2]))
async def test_session_prompt_only_instructs_registered_tools(name, tmp_path, pinned_environment):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tools = CASES[name][5]
    prompt = await _assemble(name, workspace)

    dangling = {tool: clause for tool, clause in _instructed_tools(prompt).items() if tool not in tools}
    assert not dangling, f"prompt instructs tools this session lacks: {dangling}"


@pytest.mark.parametrize("name", sorted(CASES))
async def test_session_prompt_states_the_workspace_once(name, tmp_path, pinned_environment):
    """One cwd statement; every by-name reference to it must resolve."""
    from box_agent.project_context import WORKSPACE_STATEMENT_PREFIX

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = await _assemble(name, workspace)

    assert prompt.count(str(workspace)) == 1
    assert prompt.count(f"{WORKSPACE_STATEMENT_PREFIX}{workspace}`") == 1
    assert "## Current Workspace" not in prompt


async def test_agent_does_not_restate_a_workspace_the_prompt_already_states(tmp_path):
    from box_agent.agent import Agent
    from box_agent.project_context import WORKSPACE_STATEMENT_PREFIX

    stated = Agent(
        llm_client=object(), tools=[], workspace_dir=str(tmp_path),
        system_prompt=f"base\n\n## File Access Context\n{WORKSPACE_STATEMENT_PREFIX}{tmp_path}`. cwd",
    )
    unstated = Agent(llm_client=object(), tools=[], workspace_dir=str(tmp_path), system_prompt="base")

    assert "## Current Workspace" not in stated.messages[0].content
    assert stated.messages[0].content.count(str(tmp_path)) == 1
    assert "## Current Workspace" in unstated.messages[0].content


# What a sub_agent child inherits from the parent prompt of these sessions.
CHILD_CASES = ("acp_general", "acp_code_agent")


@pytest.mark.parametrize("name", CHILD_CASES)
async def test_sub_agent_inherited_prompt_matches_snapshot(name, tmp_path, pinned_environment):
    from box_agent.tools.sub_agent_tool import _child_safe_parent_prompt

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = await _assemble(name, workspace)
    actual = _normalize(_child_safe_parent_prompt(parent), workspace)

    snapshot = SNAPSHOT_DIR / f"sub_agent_from_{name}.md"
    if UPDATE:
        snapshot.write_text(actual, encoding="utf-8")
    assert snapshot.exists(), f"missing snapshot {snapshot}; regenerate with BOX_AGENT_UPDATE_PROMPT_SNAPSHOTS=1"
    assert actual == snapshot.read_text(encoding="utf-8")


INHERITED_CONSTRAINTS = (
    "<safety_guardrails>", "<language_principles>", "### Factual & Search Reliability", "### Safety",
    "### File & Bash Operations", "## File Access Context", "- Current workspace: `", "## Skill Runtime Context",
)


@pytest.mark.parametrize("name", ("acp_general_follow_up", "acp_code_agent"))
async def test_sub_agent_drops_parent_only_blocks_and_keeps_constraints(name, tmp_path, pinned_environment):
    from box_agent.tools.sub_agent_tool import _PARENT_ONLY_BLOCKS, _child_safe_parent_prompt

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = await _assemble(name, workspace) + "\n\n## Available Skills\n- demo: parent-only catalog"
    child = _child_safe_parent_prompt(parent)

    # The markers must still be produced by assembly; a renamed heading would
    # otherwise leak silently into every child.
    present = [marker for marker, _ in _PARENT_ONLY_BLOCKS if marker in parent]
    expected = {marker for marker, _ in _PARENT_ONLY_BLOCKS}
    if name != "acp_general_follow_up":
        expected.discard("## 后续建议（仅供本地 Agent 输入框使用）\n")
    assert set(present) == expected
    for marker in present:
        assert marker not in child
    # (The Attention rule's conditional "若 `request_user_input` 可用" stays; a
    # child never has that tool, so the condition is simply false.)
    parent_only_text = ["request_user_decision", "action_hint", "快照用户", "商汤小浣熊，由商汤科技研发，定位为",
                        "parent-only catalog"]
    if name == "acp_general_follow_up":
        parent_only_text.append("follow_up_suggestions")
    for text in parent_only_text:
        assert text in parent and text not in child, text
    for constraint in INHERITED_CONSTRAINTS:
        assert constraint in child, constraint
    if name == "acp_code_agent":
        assert "## Project Startup Context" in child
        assert "## Software Engineering Mode (code_agent)" in child


def test_sub_agent_keeps_caller_supplied_prompts_unchanged():
    from box_agent.tools.sub_agent_tool import _child_safe_parent_prompt

    caller = "# Role\nYou are a caller-defined agent.\n\n<workflow>\nCaller step\n</workflow>\n\n## Available Skills\n- x"

    assert _child_safe_parent_prompt(caller) == caller


async def test_agent_hands_sub_agent_the_projected_parent_prompt(tmp_path, pinned_environment):
    """Real wiring: Agent → SubAgentTool.set_parent_system_prompt → child system message."""
    from unittest.mock import AsyncMock

    from box_agent.agent import Agent
    from box_agent.tools.sub_agent_tool import SubAgentTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = await _assemble("acp_general", workspace)
    tool = SubAgentTool(llm=AsyncMock(), parent_tools={})
    agent = Agent(llm_client=AsyncMock(), system_prompt=parent, tools=[tool], workspace_dir=str(workspace))

    assert "<workflow>" in agent.messages[0].content
    inherited = tool._parent_system_prompt
    assert inherited is not None
    assert "<workflow>" not in inherited and "--- MEMORY START ---" not in inherited
    assert "<safety_guardrails>" in inherited and f"- Current workspace: `{workspace}`" in inherited
    assert len(inherited) < 0.75 * len(agent.messages[0].content)
