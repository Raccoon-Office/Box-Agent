"""CLI-mode runtime wiring tests."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

import box_agent.cli as cli
import box_agent.composition as composition_module
import box_agent.runtime as runtime_module
from box_agent.agent import Agent
from box_agent.agent_session import AgentSession
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.events import DoneEvent, StopReason
from box_agent.kernel.ports import KernelServices
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.skill_loader import Skill, SkillLoader
from box_agent.tools.runtime import build_skill_runtime_context, build_skill_runtime_prompt
from box_agent.tools.setup import add_workspace_tools
from box_agent.tools.skill_tool import GetSkillTool
from box_agent.workspace_registry import WorkspaceRegistry
from tests.architecture_imports import forbidden_adapter_layer_imports
from box_agent.session_log import SessionLog


@pytest.fixture(autouse=True)
def isolated_cli_session_home(tmp_path, monkeypatch):
    """Persistent CLI test sessions must never use the developer's profile."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_cli_uses_session_event_api_without_kernel_or_plugin_imports() -> None:
    source_path = Path(cli.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    assert forbidden_adapter_layer_imports(
        source_path,
        inspected_module="box_agent.cli",
    ) == []
    assert cli.Agent is Agent
    assert cli.AgentSession is AgentSession
    session_run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "session"
        and node.func.attr == "run_events"
    ]
    assert len(session_run_calls) == 1
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "agent"
        and node.func.attr in {"run", "run_events"}
        for node in ast.walk(tree)
    )


def test_cli_public_path_reaches_plugin_composition_and_agent_loop_kernel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=1,
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
    bridge_calls: list[dict[str, object]] = []
    host_calls: list[dict[str, object]] = []
    kernel_calls: list[dict[str, object]] = []
    real_core_entrypoint = runtime_module._run_agent_loop
    real_create_host = composition_module.create_default_plugin_host

    def tracked_core_entrypoint(**kwargs):
        bridge_calls.append(kwargs)
        return real_core_entrypoint(**kwargs)

    def tracked_create_host(**capabilities):
        host_calls.append(capabilities)
        return real_create_host(**capabilities)

    class SentinelKernel:
        def __init__(self, *, _services, _runtime_defaults, **run_arguments):
            assert isinstance(_services, KernelServices)
            kernel_calls.append(
                {
                    "services": _services,
                    "run_arguments": run_arguments,
                }
            )

        async def run(self):
            yield DoneEvent(
                stop_reason=StopReason.END_TURN,
                final_content="kernel sentinel",
            )

    async def fake_initialize_base_tools(*_args, **_kwargs):
        return [], None, None, None

    monkeypatch.setattr(
        cli.Config,
        "get_default_config_path",
        staticmethod(lambda: config_path),
    )
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_module, "_run_agent_loop", tracked_core_entrypoint)
    monkeypatch.setattr(
        composition_module,
        "create_default_plugin_host",
        tracked_create_host,
    )
    monkeypatch.setattr(composition_module, "AgentLoopKernel", SentinelKernel)

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="exercise the public CLI path",
            sandbox_mode=False,
            verify_api=False,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    assert [len(bridge_calls), len(host_calls), len(kernel_calls)] == [1, 0, 1]
    services = kernel_calls[0]["services"]
    provided = bridge_calls[0]["kernel_services"]
    assert services.llm is provided.llm
    assert services.tool_catalog is provided.tool_catalog
    assert services.hook_dispatch is services.hook_bus
    assert services.hook_context is services.hook_bus.context
    assert services.llm is bridge_calls[0]["llm"]
    assert services.tool_catalog is bridge_calls[0]["tools"]
    assert kernel_calls[0]["run_arguments"]["messages"] is bridge_calls[0]["messages"]


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


def test_cli_resumes_messages_from_session_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    system_prompt_path = tmp_path / "system_prompt.md"
    system_prompt_path.write_text("base system", encoding="utf-8")
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
    _CaptureStreamLLM.instances.clear()

    for prompt in ("first request", "second request"):
        assert (
            asyncio.run(
                cli.run_agent(
                    workspace,
                    task=prompt,
                    session_id="cli-resume",
                    sandbox_mode=False,
                    verify_api=False,
                    goal_autopilot_enabled=False,
                )
            )
            == 0
        )

    restored = SessionLog.open(
        tmp_path / "home" / ".box-agent" / "sessions",
        session_id="cli-resume",
        cwd=workspace,
    )
    assert [
        (message.role, message.content) for message in restored.replay().messages
    ] == [
        ("user", "first request"),
        ("assistant", "done."),
        ("user", "second request"),
        ("assistant", "done."),
    ]
    restored.close()


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
            goal_autopilot_enabled=False,
        )
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert _EOFPromptSession.prompt_count == 1
    assert "Goodbye! Thanks for using Box Agent" in output
    assert "❌ Error:" not in output


def test_interactive_cli_delivers_explicit_skill_as_ordinary_reference(
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
    real_run = cli.Agent.run_events
    real_session_turn = cli._run_session_turn

    async def checked_session_turn(session, **overrides):
        assert session.explicitly_allowed_skill_names == {"report-skill"}
        for name in ("get_skill", "list_skills"):
            assert session.agent.tools[name].explicitly_allowed_skill_names is session.explicitly_allowed_skill_names
        return await real_session_turn(session, **overrides)

    async def fake_initialize_base_tools(*args, **kwargs):
        from box_agent.tools.skill_catalog_tool import ListSkillsTool
        return [GetSkillTool(skill_loader, blocked_skill_names={"report-skill"}),
                ListSkillsTool(skill_loader, blocked_skill_names={"report-skill"})], skill_loader, None, None

    async def fake_run(self, *args, **kwargs):
        catalog = await self.tools["list_skills"].execute(query="report-skill")
        assert catalog.raw_output["skills"][0]["available"]
        assert "report-skill" in self.tools["get_skill"].explicitly_allowed_skill_names
        run_options.append(
            {
                "kwargs": kwargs,
                "system_prompt": self.messages[0].content,
            }
        )
        async for event in real_run(self, *args, **kwargs):
            yield event

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
    monkeypatch.setattr(cli.Agent, "run_events", fake_run)
    monkeypatch.setattr(cli, "_run_session_turn", checked_session_turn)
    _ExplicitSkillPromptSession.prompt_count = 0
    _CaptureStreamLLM.instances.clear()

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
    assert "completion_gate" not in run_options[0]["kwargs"]
    assert "Generate the requested report." not in run_options[0]["system_prompt"]
    snapshot = _CaptureStreamLLM.instances[0].message_snapshots[0]
    assert any(role == "user" and "Generate the requested report." in str(content)
               for role, content in snapshot)
    assert all("Generate the requested report." not in str(content)
               for role, content in snapshot if role in ("system", "developer"))


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
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    assert "use_output_dir" not in workspace_tool_options
    assert workspace_tool_options["session_mode"] == "code_agent"
    system_prompt = _CaptureStreamLLM.instances[0].system_prompts[0]
    assert "Project Workspace Mode" in system_prompt
    assert "Software Engineering Mode (code_agent)" in system_prompt
    assert "CODE MODE MARKER" in system_prompt
    assert "Do not create or use an `output/` folder" in system_prompt


def test_cli_task_reads_pptx_on_demand_without_automatic_fulltext(tmp_path: Path, monkeypatch) -> None:
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
    assert "## Auto-Loaded Skill Instructions" not in first_system_prompt
    assert "# PPTX FULL RULES" not in first_system_prompt
    assert "# HTML TEMPLATE RULES" not in first_system_prompt
    snapshots = _CaptureStreamLLM.instances[0].message_snapshots
    assert len(snapshots) == 2
    tool_messages = [content for role, content in snapshots[1] if role == "tool"]
    assert len(tool_messages) == 1
    assert "# PPTX FULL RULES" in tool_messages[0]
    assert "# HTML TEMPLATE RULES" not in tool_messages[0]


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
            goal_autopilot_enabled=False,
        )
    )

    output = capsys.readouterr().out
    summary = json.loads(output[output.rfind("\n{") + 1 :])
    assert exit_code == 1
    assert summary["ok"] is False
    assert summary["error"]
    assert summary["goalAutopilot"]["lastStopReason"] == "error"


def test_cli_json_reports_waiting_for_user_without_completion(
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
        yield DoneEvent(
            stop_reason=StopReason.WAITING_FOR_USER,
            final_content="Waiting for user input.",
        )

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.Agent, "run_events", fake_run)

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="Create a deck",
            sandbox_mode=False,
            verify_api=False,
            json_summary=True,
            goal_autopilot_enabled=False,
        )
    )

    output = capsys.readouterr().out
    summary = json.loads(output[output.rfind("\n{") + 1 :])
    assert exit_code == 0
    assert summary["ok"] is True
    assert summary["error"] is None
    assert summary["runStatus"] == "waiting_for_user"
    assert summary["completed"] is False
    assert "recoverable" not in summary
    assert "checkpoint" not in summary


def _exercise_cli_source_binding(
    tmp_path, monkeypatch, *, task=None, inputs=(), on_run=None, autopilot=False,
):
    import base64

    from box_agent.tools.bash_tool import BashTool

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2, workspace_dir=str(workspace), enable_memory=False,
            enable_memory_extraction=False, memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False, enable_bash=False, enable_todo=False,
            enable_plan=False, enable_sub_agent=False, enable_mcp=False,
            enable_skills=False, allow_full_access=True,
        ),
    )
    bash = BashTool(
        workspace_dir=str(workspace), non_interactive=True,
        runtime_env={"BOX_AGENT_SOURCE_TEXT_B64": base64.b64encode(b"stale source").decode()},
    )
    captured = []
    prompts = iter(inputs)

    class InputSession:
        def __init__(self, *args, **kwargs):
            pass

        async def prompt_async(self, *args, **kwargs):
            try:
                return next(prompts)
            except StopIteration:
                raise EOFError from None

    async def base_tools(*args, **kwargs):
        return [bash], None, None, None

    async def run(self, *args, **kwargs):
        captured.append(base64.b64decode(
            bash._subprocess_env["BOX_AGENT_SOURCE_TEXT_B64"]
        ).decode("utf-8"))
        if on_run:
            await on_run(bash, workspace)
        yield DoneEvent(
            stop_reason=StopReason.END_TURN,
            final_content="assistant interpretation is not a user source",
        )

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli.Config, "find_config_file", staticmethod(lambda _name: None))
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "PromptSession", InputSession)
    monkeypatch.setattr(cli.Agent, "run_events", run)
    if autopilot:
        monkeypatch.setattr(
            cli, "should_continue_goal_autopilot",
            lambda _agent, _reason: len(captured) < 2,
        )
    exit_code = asyncio.run(cli.run_agent(
        workspace, task=task, sandbox_mode=False, verify_api=False,
        initial_goal="完成测试交付" if autopilot else None,
        goal_autopilot_enabled=autopilot,
    ))
    assert exit_code == 0
    return captured, bash


def test_cli_task_binds_verbatim_source_for_skill_subprocesses(tmp_path, monkeypatch):
    task = "生成 PPT，不需要生图。\n保留字符：$HOME、`literal`、引号'\"。"
    captured, _ = _exercise_cli_source_binding(tmp_path, monkeypatch, task=task)
    assert captured == [task]


def test_cli_source_excludes_synthetic_goal_continuations(tmp_path, monkeypatch):
    task = "使用已有图片制作 PPT，不需要生图。"
    captured, _ = _exercise_cli_source_binding(
        tmp_path, monkeypatch, task=task, autopilot=True,
    )
    assert captured == [task, task]


def test_cli_interactive_source_accumulates_and_clear_starts_fresh(tmp_path, monkeypatch):
    import base64

    captured, bash = _exercise_cli_source_binding(
        tmp_path, monkeypatch,
        inputs=("不要生图。", "背景 #FFFFFF", "/clear", "新的独立请求", "/clear_all"),
    )
    assert captured == ["不要生图。", "不要生图。\n\n背景 #FFFFFF", "新的独立请求"]
    assert base64.b64decode(bash._subprocess_env["BOX_AGENT_SOURCE_TEXT_B64"]) == b""


def test_cli_source_image_optout_reaches_real_pptx_scaffold(tmp_path, monkeypatch):
    import shlex
    import shutil

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the PPTX scaffold")
    script = Path(cli.__file__).parent / "skills/document-skills/pptx/scripts/inspect_deck_contract.js"
    manifests = []

    async def scaffold(bash, workspace):
        result = await bash.execute(command=shlex.join([
            node, str(script), "cover-hero-v1", "--title", "工作坊主视觉",
            "--out", str(workspace / "deck.json"),
        ]))
        assert result.success, result.error or result.content
        manifests.append(json.loads(
            (workspace / "assets/generated/manifest.json").read_text(encoding="utf-8")
        ))

    _exercise_cli_source_binding(
        tmp_path, monkeypatch, task="制作工作坊 PPT，不需要生图。", on_run=scaffold,
    )
    assert manifests[0]["generation_forbidden"] is True
    assert all(item["decision"] == "skip" for item in manifests[0]["image_plan"])


def _configure_persistent_cli_test(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n")
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(workspace), enable_memory=False, max_steps=1),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False),
    )
    async def base(*args, **kwargs):
        return [], None, None, None
    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", base)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    return workspace


@pytest.mark.parametrize("all_legacy_arguments", [False, True])
def test_legacy_cli_positional_arguments_keep_api_verification_disabled(
    tmp_path, monkeypatch, all_legacy_arguments,
):
    workspace = _configure_persistent_cli_test(tmp_path, monkeypatch)
    probes = []

    async def record_probe(client):
        probes.append(client)

    monkeypatch.setattr(cli, "_probe_llm_api", record_probe)
    arguments = [workspace, "task", None, False, False]
    options = {"session_id": "positional-contract"}
    if all_legacy_arguments:
        arguments.extend([False, False, False, False])
    else:
        options["goal_autopilot_enabled"] = False
    assert asyncio.run(cli.run_agent(*arguments, **options)) == 0
    assert probes == []
    restored = SessionLog.open(
        cli.default_session_root(), session_id="positional-contract", cwd=workspace,
    )
    try:
        assert restored.replay().messages[-1].content == "done."
    finally:
        restored.close()


def test_cli_failed_skill_restore_preserves_log_and_releases_writer_lock(tmp_path, monkeypatch):
    from box_agent.skill_dependencies import SkillDependencyError

    workspace = _configure_persistent_cli_test(tmp_path, monkeypatch)
    root = cli.default_session_root()
    log = SessionLog.create(root, session_id="missing-skill", cwd=workspace)
    log.append("skill/change", {"skills": [{"name": "missing", "sha256": "old", "loadOrder": 1}]})
    log.flush()
    path = log.path
    log.close()
    before = path.read_bytes()
    with pytest.raises(SkillDependencyError, match="No Skill source"):
        asyncio.run(cli.run_agent(workspace, task="continue", session_id="missing-skill",
                                  verify_api=False, sandbox_mode=False, goal_autopilot_enabled=False))
    reopened = SessionLog.open(root, session_id="missing-skill", cwd=workspace)
    reopened.close()
    assert path.read_bytes() == before


@pytest.mark.parametrize("persisted_goal", [None, "canonical goal"])
def test_resumed_cli_uses_log_goal_and_leaves_workspace_goal_unchanged(tmp_path, monkeypatch, persisted_goal):
    workspace = _configure_persistent_cli_test(tmp_path, monkeypatch)
    assert cli.cmd_goal(workspace, "set", ["workspace goal"]) == 0
    root = cli.default_session_root()
    if persisted_goal is not None:
        assert cli.cmd_goal(workspace, "set", [persisted_goal], session_id="goal-session") == 0
    else:
        SessionLog.create(root, session_id="goal-session", cwd=workspace).close()
    for prompt in ("first", "second"):
        assert asyncio.run(cli.run_agent(workspace, task=prompt, session_id="goal-session",
                                         verify_api=False, sandbox_mode=False, goal_autopilot_enabled=False)) == 0
    reopened = SessionLog.open(root, session_id="goal-session", cwd=workspace)
    try:
        goal = reopened.replay().goal
        assert (goal["objective"] if goal else None) == persisted_goal
        assert cli._load_goal_state(workspace).objective == "workspace goal"
    finally:
        reopened.close()
