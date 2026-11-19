"""Executing a downloaded Skill must leave its shared archive cache intact."""
from pathlib import Path
import sys

import httpx
import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.expert_packages import prepare_expert_meta
from box_agent.tools.bash_tool import BashTool
from box_agent.tools import jupyter_tool
from box_agent.tools.jupyter_tool import JupyterSandboxTool, SandboxEnvironment
from box_agent.tools.runtime import SkillRuntime, SkillRuntimeContext, managed_python_shell_command
from box_agent.tools.setup import add_workspace_tools
from box_agent.tools.skill_execution_env import build_skill_execution_env
from box_agent.tools.skill_scratch import cleanup_skill_scratch_dir
from tests.expert_package_helpers import archive_bytes, reference


@pytest.mark.asyncio
async def test_executed_python_skill_keeps_shared_expert_cache_reusable(tmp_path, monkeypatch, posix_shell):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    body = archive_bytes(extra=(
        ("skills/skill-0/scripts/main.py", b"import helper\nprint(helper.VALUE)\n"),
        ("skills/skill-0/scripts/helper.py", b"VALUE = 42\n"),
    ))
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(serve)
    selection = {"expert": reference(body)}
    first = await prepare_expert_meta(selection, transport=transport)
    package = Path(first["expert"]["packageSnapshot"]["directory"])
    script = package / "skills/skill-0/scripts/main.py"
    context = SkillRuntimeContext({"python": SkillRuntime(
        kind="python", status="available", provider="host", executable_path=sys.executable,
        env_vars={"BOX_AGENT_PYTHON": Path(sys.executable).as_posix()},
    )})
    for chat in ("first-chat", "second-chat"):
        workspace = tmp_path / chat
        workspace.mkdir()
        tool = BashTool(workspace_dir=str(workspace), allow_full_access=True, non_interactive=True,
                        runtime_env=build_skill_execution_env(context))
        command = f'{managed_python_shell_command(tool.shell_name)} "{script.as_posix()}"'
        result = await tool.execute(command, timeout=20)
        assert result.success, result.error
        assert "42" in result.content
        again = await prepare_expert_meta(selection, transport=transport)
        assert not list(package.rglob("__pycache__"))
        assert again["expert"]["packageSnapshot"]["directory"] == str(package)
    assert len(requests) == 1


def test_execute_code_environment_uses_the_same_bytecode_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    context = SkillRuntimeContext({})
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir="workspace"), tools=ToolsConfig(
        enable_file_tools=False, enable_bash=False, enable_sub_agent=False, enable_mcp=False,
    ))
    tools = []
    scratch = add_workspace_tools(tools, config, tmp_path / "workspace", sandbox_mode=True,
                                  skill_runtime_context=context, output=lambda _: None)
    try:
        kernel = next(tool for tool in tools if isinstance(tool, JupyterSandboxTool))
        assert kernel.runtime_env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert build_skill_execution_env(context)["PYTHONDONTWRITEBYTECODE"] == "1"
        # Keep main's lazy scratch setup: only validated execution may publish it.
        assert "BOX_AGENT_SCRATCH_DIR" not in kernel.runtime_env
        assert scratch.prepared is None
        assert not scratch.path.exists()
    finally:
        cleanup_skill_scratch_dir(scratch)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_python", [False, True])
async def test_execute_code_import_keeps_shared_expert_cache_reusable(tmp_path, monkeypatch, explicit_python):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.setattr(jupyter_tool, "SANDBOX_BASE_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(JupyterSandboxTool, "_sandbox_env", None)
    monkeypatch.setattr(JupyterSandboxTool, "_sandbox_env_key", None)

    async def ready(self, on_progress=None):
        # Exercise the installed interpreter/kernel without installing packages.
        pass

    monkeypatch.setattr(SandboxEnvironment, "_verify_packages", ready)
    body = archive_bytes(extra=(("skills/skill-0/scripts/helper.py", b"VALUE = 42\n"),))
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(serve)
    selection = {"expert": reference(body)}
    first = await prepare_expert_meta(selection, transport=transport)
    package = Path(first["expert"]["packageSnapshot"]["directory"])
    scripts = package / "skills/skill-0/scripts"
    tool = JupyterSandboxTool(
        workspace_dir=str(tmp_path / "workspace"), process_owner_id="expert-bytecode-regression",
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        result = await tool.execute(
            code=(f"import sys\nsys.path.insert(0, {str(scripts)!r})\n"
                  "import helper\nprint(helper.VALUE)\nassert sys.dont_write_bytecode"),
            session_id="cache-check", timeout=30,
            python_path=sys.executable if explicit_python else None,
        )
        assert result.success, result.error
        assert "42" in result.content
        again = await prepare_expert_meta(selection, transport=transport)
        assert again["expert"]["packageSnapshot"]["directory"] == str(package)
        assert not list(package.rglob("__pycache__"))
        assert len(requests) == 1
    finally:
        await tool.shutdown_owned_sessions()
