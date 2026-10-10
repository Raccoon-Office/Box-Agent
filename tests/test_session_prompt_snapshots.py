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
}


class _Memory:
    def read_core(self) -> str:
        return "name: snapshot user, prefers concise answers in Chinese"

    def recall(self) -> str:
        return "## Memory\n<memory block>"


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
    options = SessionOptions(
        profile=profile,
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
