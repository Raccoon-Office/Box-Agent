"""CLI-mode runtime wiring tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import box_agent.cli as cli
from box_agent.config import (
    AgentConfig,
    Config,
    LLMConfig,
    ToolLimitsConfig,
    ToolsConfig,
)
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.skill_loader import Skill, SkillLoader
from box_agent.tools.runtime import build_skill_runtime_context, build_skill_runtime_prompt
from box_agent.tools.setup import add_workspace_tools
from box_agent.tools.skill_tool import GetSkillTool
from box_agent.workspace_registry import WorkspaceRegistry


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _write_skill(
    skills_dir: Path,
    name: str,
    *,
    description: str,
    keywords: list[str],
    content: str,
    required_skills: list[str] | None = None,
) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    required = (
        f"required_skills: [{', '.join(required_skills)}]\n"
        if required_skills
        else ""
    )
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"keywords: [{', '.join(keywords)}]\n"
        f"{required}"
        "---\n"
        f"{content}\n",
        encoding="utf-8",
    )


def test_cli_node_execution_env_preserves_user_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8443")
    monkeypatch.setenv("npm_config_prefix", "/system/npm")
    monkeypatch.setenv("npm_config_cache", "/system/npm-cache")
    monkeypatch.setattr(
        cli,
        "build_skill_runtime_context",
        lambda **_kwargs: build_skill_runtime_context(
            sandbox_mode=False,
            node_runtime_root=tmp_path / "missing-node",
            office_node_runtime_root=tmp_path / "missing-office-node",
        ),
    )

    _npx, env = cli._cli_node_execution_env()

    assert env["HTTPS_PROXY"] == "http://proxy.example.test:8443"
    assert env["NPM_CONFIG_PREFIX"] == str(tmp_path / ".box-agent" / "skill-tools")
    assert "npm_config_prefix" not in env
    assert "npm_config_cache" not in env


def test_cli_presentation_gate_requires_a_skill_selected_provider(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "plain-text",
        description="Summarize and edit plain text",
        keywords=["text", "summary"],
        content="# PLAIN TEXT RULES",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()

    gate = cli._build_cli_skill_routed_completion_gate(
        "请制作一份 PPT",
        tmp_path / "workspace",
        skill_loader=skill_loader,
        tool_limits=ToolLimitsConfig(),
    )

    assert gate is None


def test_cli_presentation_gate_uses_builtin_skill_provider(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "pptx",
        description="Create editable PowerPoint presentation decks",
        keywords=["ppt", "pptx", "slides"],
        content="# PPTX RULES",
    )
    skill_loader = SkillLoader([(skills_dir, "builtin")])
    skill_loader.discover_skills()

    gate = cli._build_cli_skill_routed_completion_gate(
        "请制作一份 PPT",
        tmp_path / "workspace",
        skill_loader=skill_loader,
        tool_limits=ToolLimitsConfig(),
    )

    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"


def test_cli_skill_routing_handles_short_and_long_chinese_deck_prompts(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "pptx",
        description="Create editable PowerPoint presentation decks",
        keywords=["ppt", "pptx", "slides", "课件"],
        content="# PPTX RULES",
    )
    skill_loader = SkillLoader([(skills_dir, "builtin")])
    skill_loader.discover_skills()
    cases = (
        (
            "我是小学科学老师，请制作一份PPT给三年级讲太阳系，"
            "让八大行星能转起来并看清大小和远近。",
            "auto",
        ),
        (
            "帮我制作一份标题为《新能源汽车本土品牌市场动态分析》的"
            "数据解读型PPT。请主动搜索并引用最新的中国公开数据，"
            "所有图表均标注来源、统计时间和报告名称。",
            "auto",
        ),
    )

    for index, (prompt, expected_research_mode) in enumerate(cases):
        gate = cli._build_cli_skill_routed_completion_gate(
            prompt,
            tmp_path / f"workspace-{index}",
            skill_loader=skill_loader,
            tool_limits=ToolLimitsConfig(),
        )

        assert gate is not None
        assert gate.workflow_checkpoint_kind == "controlled_presentation"
        assert gate.workflow_options["research_mode"] == expected_research_mode


class _CaptureStreamLLM:
    instances: list["_CaptureStreamLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.system_prompts: list[str] = []
        self.message_snapshots: list[list[tuple[str, str]]] = []
        self.retry_callback = None
        self.instances.append(self)

    async def generate(self, *args, **kwargs):
        return LLMResponse(content="ok", finish_reason="stop")

    async def generate_stream(self, *, messages, **kwargs):
        self.message_snapshots.append([(message.role, message.content) for message in messages])
        self.system_prompts.append(messages[0].content)
        yield StreamEvent(type="text", delta="done.")
        yield StreamEvent(type="finish", finish_reason="stop")


class _PreloadedSkillThenGetSkillLLM(_CaptureStreamLLM):
    async def generate_stream(self, *, messages, **kwargs):
        self.message_snapshots.append([(message.role, message.content) for message in messages])
        self.system_prompts.append(messages[0].content)
        if len(self.message_snapshots) == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="preloaded-skill",
                        type="function",
                        function=FunctionCall(
                            name="get_skill", arguments={"skill_name": "pptx"}
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="done.")
        yield StreamEvent(type="finish", finish_reason="stop")


class _EmptyFinalAnswerLLM:
    def __init__(self, *args, **kwargs) -> None:
        self.calls = 0

    async def generate_stream(self, *, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="echo-1",
                        type="function",
                        function=FunctionCall(
                            name="echo",
                            arguments={"text": "evidence"},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="finish", finish_reason="stop")


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo input"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, text: str) -> ToolResult:
        return ToolResult(success=True, content=text)


class _EOFPromptSession:
    prompt_count = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def prompt_async(self, *args, **kwargs) -> str:
        type(self).prompt_count += 1
        raise EOFError


class _ExplicitSkillPromptSession:
    prompt_count = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def prompt_async(self, *args, **kwargs) -> str:
        type(self).prompt_count += 1
        if type(self).prompt_count == 1:
            return "请用 /report-skill 生成报告"
        raise EOFError


def test_cli_ctrl_d_exits_without_empty_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    system_prompt_path = tmp_path / "system_prompt.md"
    system_prompt_path.write_text(
        "base system\n\n{SKILLS_METADATA}\n\n{SANDBOX_INFO}",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
            system_prompt_path=str(system_prompt_path),
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )

    async def fake_initialize_base_tools(*args, **kwargs):
        return [], None, None, None

    monkeypatch.setattr(
        cli.Config,
        "get_default_config_path",
        staticmethod(lambda: config_path),
    )
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(
        cli.Config,
        "find_config_file",
        staticmethod(
            lambda name: Path(name) if name == str(system_prompt_path) else None
        ),
    )
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "PromptSession", _EOFPromptSession)
    _EOFPromptSession.prompt_count = 0

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            sandbox_mode=False,
            verify_api=False,
            completion_gate_enabled=False,
            goal_autopilot_enabled=False,
        )
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert _EOFPromptSession.prompt_count == 1
    assert "Goodbye! Thanks for using Box Agent" in output
    assert "❌ Error:" not in output


def test_interactive_cli_passes_explicit_skill_gate_to_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    system_prompt_path = tmp_path / "system_prompt.md"
    system_prompt_path.write_text(
        "base system\n\n{SKILLS_METADATA}\n\n{SANDBOX_INFO}",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    skill_path = tmp_path / "skills" / "report-skill" / "SKILL.md"
    skill_loader = SkillLoader(skill_path.parent.parent)
    skill_loader.loaded_skills["report-skill"] = Skill(
        name="report-skill",
        description="Generate an HTML report.",
        content="Generate the requested report.",
        source="user",
        skill_path=skill_path,
    )
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
            system_prompt_path=str(system_prompt_path),
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=True,
            allow_full_access=True,
        ),
    )
    run_options: list[dict[str, object]] = []

    async def fake_initialize_base_tools(*args, **kwargs):
        return [GetSkillTool(skill_loader)], skill_loader, None, None

    async def fake_run(self, *args, **kwargs):
        run_options.append(kwargs)
        return "done"

    monkeypatch.setattr(
        cli.Config,
        "get_default_config_path",
        staticmethod(lambda: config_path),
    )
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(
        cli.Config,
        "find_config_file",
        staticmethod(
            lambda name: Path(name) if name == str(system_prompt_path) else None
        ),
    )
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "PromptSession", _ExplicitSkillPromptSession)
    monkeypatch.setattr(cli.Agent, "run", fake_run)
    _ExplicitSkillPromptSession.prompt_count = 0

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            sandbox_mode=False,
            verify_api=False,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    assert len(run_options) == 1
    gate = run_options[0]["completion_gate"]
    assert gate.workflow_checkpoint_kind == "external_skill"
    assert gate.workflow_options["skill_name"] == "report-skill"


def test_cli_workspace_tools_receive_self_managed_node_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    node_root = tmp_path / ".box-agent" / "runtimes" / "node"
    node_bin = node_root / "versions" / "node-v22-test-darwin-arm64" / "bin"
    node = node_bin / "node"
    npm = node_bin / "npm"
    npx = node_bin / "npx"
    for path in (node, npm, npx):
        _make_executable(path)
    node_root.mkdir(parents=True, exist_ok=True)
    (node_root / "manifest.json").write_text(
        json.dumps(
            {
                "active": {
                    "version": "v22-test",
                    "node": str(node),
                    "npm": str(npm),
                    "npx": str(npx),
                }
            }
        ),
        encoding="utf-8",
    )

    runtime_context = build_skill_runtime_context(
        sandbox_mode=False,
        node_runtime_root=node_root,
    )
    tools = []
    add_workspace_tools(
        tools,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(workspace_dir=str(tmp_path / "workspace")),
            tools=ToolsConfig(enable_file_tools=False, enable_todo=False),
        ),
        tmp_path / "workspace",
        sandbox_mode=False,
        output=lambda _msg: None,
        skill_runtime_context=runtime_context,
    )

    bash_tool = next(tool for tool in tools if tool.name == "bash")
    assert bash_tool._subprocess_env["BOX_AGENT_NODE"] == str(node)
    assert bash_tool._subprocess_env["BOX_AGENT_NPM"] == str(npm)
    assert bash_tool._subprocess_env["BOX_AGENT_NPX"] == str(npx)
    skill_tools = tmp_path / ".box-agent" / "skill-tools"
    assert bash_tool._subprocess_env["NODE_PATH"].split(":") == [
        str(skill_tools / "lib" / "node_modules"),
        str(node_root / "sandbox" / "node_modules"),
    ]
    assert bash_tool._subprocess_env["NPM_CONFIG_CACHE"] == str(skill_tools / "npm-cache")
    assert bash_tool._subprocess_env["NPM_CONFIG_PREFIX"] == str(skill_tools)
    path_entries = bash_tool._subprocess_env["PATH"].split(":")
    assert path_entries[0] == str(skill_tools / "bin")
    assert path_entries.index(str(node_bin)) < path_entries.index("/usr/bin")

    prompt = build_skill_runtime_prompt(runtime_context)
    assert "- Node:" in prompt
    assert "标准 `node`/`npm`/`npx`" in prompt
    assert "$BOX_AGENT_NODE" in prompt


def test_cli_uses_saved_code_workspace_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    WorkspaceRegistry().set(workspace, "code")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    system_prompt_path = tmp_path / "system_prompt.md"
    system_prompt_path.write_text(
        "base system\n\n{SKILLS_METADATA}\n\n{SANDBOX_INFO}\n\n{FILE_DELIVERY_INFO}",
        encoding="utf-8",
    )
    code_prompt_path = tmp_path / "code_prompt.md"
    code_prompt_path.write_text(
        "## Software Engineering Mode (code_agent)\nCODE MODE MARKER",
        encoding="utf-8",
    )
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
            system_prompt_path=str(system_prompt_path),
            code_prompt_path=str(code_prompt_path),
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )
    workspace_tool_options: dict[str, object] = {}

    async def fake_initialize_base_tools(*args, **kwargs):
        return [], None, None, None

    def fake_add_workspace_tools(*args, **kwargs):
        workspace_tool_options.update(kwargs)

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(
        cli.Config,
        "find_config_file",
        staticmethod(
            lambda name: Path(name)
            if name in {str(system_prompt_path), str(code_prompt_path)}
            else None
        ),
    )
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", fake_add_workspace_tools)
    _CaptureStreamLLM.instances.clear()

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="fix the project",
            sandbox_mode=True,
            verify_api=False,
            completion_gate_enabled=False,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    assert workspace_tool_options["use_output_dir"] is False
    system_prompt = _CaptureStreamLLM.instances[0].system_prompts[0]
    assert "Project Workspace Mode" in system_prompt
    assert "Software Engineering Mode (code_agent)" in system_prompt
    assert "CODE MODE MARKER" in system_prompt
    assert "Do not create or use an `output/` folder" in system_prompt


def test_cli_task_preloads_pptx_even_when_filter_drops_it(tmp_path: Path, monkeypatch) -> None:
    skills_dir = tmp_path / "skills"
    prompt = "做一份 12 页新员工入职培训 PPT，1920×1080 可编辑"
    for index in range(16):
        _write_skill(
            skills_dir,
            f"lark-noise-{index}",
            description="做一份 新员工 入职 培训 可编辑 会议室 HR 友好 流程 清单",
            keywords=["做一份", "新员工", "入职", "培训", "可编辑", "会议室", "HR"],
            content=f"# Noise {index}",
        )
    _write_skill(
        skills_dir,
        "pptx",
        description="Create editable PowerPoint PPTX slide decks.",
        keywords=["ppt", "pptx", "powerpoint", "slide"],
        required_skills=["html-templates"],
        content="# PPTX FULL RULES\nUse the editable deck workflow.",
    )
    _write_skill(
        skills_dir,
        "html-templates",
        description="Select visual style constraints for HTML slide decks.",
        keywords=["html", "template", "visual"],
        content="# HTML TEMPLATE RULES\nSelect a Visual DNA profile.",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()
    assert "pptx" not in [skill.name for skill in skill_loader.filter_by_query(prompt)]

    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    system_prompt_path = tmp_path / "system_prompt.md"
    system_prompt_path.write_text(
        "base system\n\n{SKILLS_METADATA}\n\n{SANDBOX_INFO}",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
            system_prompt_path=str(system_prompt_path),
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=True,
            allow_full_access=True,
        ),
    )

    async def fake_initialize_base_tools(*args, **kwargs):
        return [GetSkillTool(skill_loader)], skill_loader, None, None

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(
        cli.Config,
        "find_config_file",
        staticmethod(lambda name: Path(name) if name == str(system_prompt_path) else None),
    )
    monkeypatch.setattr(cli, "LLMClient", _PreloadedSkillThenGetSkillLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    _CaptureStreamLLM.instances.clear()

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task=prompt,
            sandbox_mode=False,
            verify_api=False,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    first_system_prompt = _CaptureStreamLLM.instances[0].system_prompts[0]
    assert "## Auto-Loaded Skill Instructions" in first_system_prompt
    assert "# Skill: pptx" in first_system_prompt
    assert "# PPTX FULL RULES" in first_system_prompt
    assert "# Skill: html-templates" in first_system_prompt
    assert "# HTML TEMPLATE RULES" in first_system_prompt
    snapshots = _CaptureStreamLLM.instances[0].message_snapshots
    assert len(snapshots) == 2
    tool_messages = [content for role, content in snapshots[1] if role == "tool"]
    assert tool_messages == [
        "Skill 'pptx' is already preloaded in this session. "
        "Follow its system instructions directly."
    ]
    assert "# PPTX FULL RULES" not in tool_messages[0]


def test_cli_task_returns_failure_for_done_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=3,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )
    async def fake_initialize_base_tools(*args, **kwargs):
        return [_EchoTool()], None, None, None

    monkeypatch.setattr(
        cli.Config,
        "get_default_config_path",
        staticmethod(lambda: config_path),
    )
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", _EmptyFinalAnswerLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="Use echo and summarize the result",
            sandbox_mode=False,
            verify_api=False,
            json_summary=True,
            completion_gate_enabled=False,
            goal_autopilot_enabled=False,
        )
    )

    output = capsys.readouterr().out
    summary = json.loads(output[output.rfind("\n{") + 1 :])
    assert exit_code == 1
    assert summary["ok"] is False
    assert summary["error"]
    assert summary["goalAutopilot"]["lastStopReason"] == "error"


def test_cli_json_reports_checkpoint_pause_without_error_or_completion(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=3,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )

    async def fake_initialize_base_tools(*args, **kwargs):
        return [], None, None, None

    async def fake_run(self, *args, **kwargs):
        self.last_stop_reason = "checkpoint_paused"
        self.last_checkpoint = {
            "checkpointId": "checkpoint-1",
            "workflowKind": "controlled_presentation",
        }
        return "Progress saved."

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.Agent, "run", fake_run)

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="Create a deck",
            sandbox_mode=False,
            verify_api=False,
            json_summary=True,
            completion_gate_enabled=False,
            goal_autopilot_enabled=False,
        )
    )

    output = capsys.readouterr().out
    summary = json.loads(output[output.rfind("\n{") + 1 :])
    assert exit_code == 0
    assert summary["ok"] is True
    assert summary["error"] is None
    assert summary["runStatus"] == "paused"
    assert summary["completed"] is False
    assert summary["recoverable"] is True
    assert summary["checkpoint"]["checkpointId"] == "checkpoint-1"
