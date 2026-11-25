"""Expert preflight matches the tools registered for the actual session."""
from types import SimpleNamespace

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.tools import setup as tool_setup
from box_agent.tools.runtime import SkillRuntime, SkillRuntimeContext
from box_agent.tools.setup import image_generation_tool_settings
from box_agent.tools.skill_execution_env import build_skill_execution_env
from box_agent.tools.skill_scratch import cleanup_skill_scratch_dir


@pytest.fixture(autouse=True)
def image_environment(monkeypatch):
    for name in (
        "BOX_AGENT_IMAGE_GENERATION_ENDPOINT", "BOX_AGENT_IMAGE_GEN_ENDPOINT",
        "RACCOON_PRIVATE_DEPLOYMENT", "RACCOON_PRIVATE_OFFICE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def config(endpoint="", *, managed=False):
    return SimpleNamespace(
        image_generation=SimpleNamespace(endpoint=endpoint),
        officev3=SimpleNamespace(use_default_image_generation_preset=managed),
    )


def test_unconfigured_image_tool_is_not_available_for_expert_preflight():
    assert image_generation_tool_settings(config()) == (
        False, None, "no image_generation.endpoint configured",
    )


def test_explicit_image_endpoint_is_available_without_creating_a_tool():
    endpoint = "https://images.example.test/v1/images/generations"
    settings = config(endpoint)
    available, actual, _ = image_generation_tool_settings(settings)
    assert available is True
    assert actual == endpoint
    assert settings.image_generation.endpoint == endpoint


@pytest.mark.parametrize("variable", ["BOX_AGENT_IMAGE_GENERATION_ENDPOINT", "BOX_AGENT_IMAGE_GEN_ENDPOINT"])
def test_environment_only_image_tool_keeps_native_constructor_fallback(monkeypatch, variable):
    monkeypatch.setenv(variable, "https://images.example.test/v1/images/generations")
    available, endpoint, _ = image_generation_tool_settings(config())
    assert available is True
    # Non-managed native registration passes None so the constructor resolves env.
    assert endpoint is None


@pytest.mark.parametrize("managed,expected_host", [
    (True, "code-test.xiaohuanxiong.com"),
    (False, "xiaohuanxiong.com"),
])
def test_hosted_alignment_applies_only_to_default_preset(managed, expected_host):
    original = "https://xiaohuanxiong.com/api/web/llm/v2/images/gen"
    llm = SimpleNamespace(api_base="https://code-test.xiaohuanxiong.com/api/web/llm/v2")
    available, endpoint, _ = image_generation_tool_settings(config(original, managed=managed), llm)
    assert available is True
    assert endpoint == f"https://{expected_host}/api/web/llm/v2/images/gen"


@pytest.mark.parametrize("in_config", [True, False])
def test_private_default_preset_uses_trusted_configured_service(monkeypatch, in_config):
    base = "https://tenant.example/office"
    endpoint = base + "/api/web/llm/v2/images/gen"
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", base)
    monkeypatch.setenv("BOX_AGENT_IMAGE_GENERATION_ENDPOINT", endpoint)
    llm = SimpleNamespace(api_base=base + "/api/web/llm/v2")
    available, actual, _ = image_generation_tool_settings(config(endpoint if in_config else "", managed=True), llm)
    assert available is True
    assert actual == endpoint


@pytest.mark.parametrize("in_config", [True, False])
@pytest.mark.parametrize("endpoint,private_base", [
    ("https://code-test.xiaohuanxiong.com/api/web/llm/v2/images/gen", "https://tenant.example/office"),
    ("https://other.example/custom", "https://tenant.example/office"),
    ("https://tenant.example/office/images/gen", "https://tenant.example/office/images/gen"),
])
def test_private_default_preset_rejects_endpoints_outside_service_boundary(monkeypatch, in_config, endpoint, private_base):
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", private_base)
    monkeypatch.setenv("BOX_AGENT_IMAGE_GENERATION_ENDPOINT", endpoint)
    llm = SimpleNamespace(api_base="https://tenant.example/office/api/web/llm/v2")
    available, actual, reason = image_generation_tool_settings(config(endpoint if in_config else "", managed=True), llm)
    assert available is False
    assert actual == ""
    assert reason == "private managed image endpoint is outside the service boundary"


def test_private_explicit_custom_image_endpoint_keeps_existing_registration(monkeypatch):
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    endpoint = "https://custom.example/v1/images/generations"
    llm = SimpleNamespace(api_base="https://tenant.example/office/api/web/llm/v2")
    available, actual, _ = image_generation_tool_settings(config(endpoint), llm)
    assert available is True
    assert actual == endpoint


@pytest.mark.parametrize("scenario", [
    pytest.param(
        {"absent": {"bash",
                    "execute_code",
                    "generate_image",
                    "inspect_images",
                    "plan_write",
                    "read_file",
                    "sub_agent",
                    "todo_write"}},
        id="optional-tools-disabled",
    ),
    pytest.param(
        {"tools": {"enable_bash": True},
         "present": {"bash"},
         "absent": {"bash_output", "bash_kill"}},
        id="bash-enabled",
    ),
    pytest.param(
        {"tools": {"enable_bash": True},
         "owner": "test-owner",
         "present": {"bash_output", "bash", "bash_kill"}},
        id="bash-owner-tools",
    ),
    pytest.param(
        {"tools": {"enable_file_tools": True},
         "present": {"read_file", "write_file", "search_files"}},
        id="file-tools-enabled",
    ),
    pytest.param(
        {"tools": {"enable_file_tools": True},
         "full_access": False,
         "present": {"read_file", "write_file"}},
        id="restricted-file-tools-remain-registered",
    ),
    pytest.param(
        {"tools": {"enable_todo": True}, "present": {"todo_write", "todo_read"}},
        id="todo-enabled",
    ),
    pytest.param(
        {"tools": {"enable_plan": True}, "present": {"plan_read", "plan_write"}},
        id="plan-enabled",
    ),
    pytest.param(
        {"sandbox": True, "present": {"sandbox_status", "execute_code"}},
        id="sandbox-enabled",
    ),
    pytest.param(
        {"tools": {"enable_sub_agent": True}, "absent": {"sub_agent"}},
        id="sub-agent-needs-model",
    ),
    pytest.param(
        {"tools": {"enable_sub_agent": True},
         "vision": False,
         "present": {"sub_agent"},
         "absent": {"inspect_images"}},
        id="text-model-sub-agent",
    ),
    pytest.param(
        {"vision": True, "present": {"inspect_images"}},
        id="vision-model",
    ),
    pytest.param(
        {"image": True, "present": {"generate_image"}},
        id="image-endpoint-configured",
    ),
    pytest.param(
        {"tools": {"enable_file_tools": True},
         "mode": "code_agent",
         "rg": True,
         "present": {"glob", "grep"}},
        id="code-agent-with-ripgrep",
    ),
    pytest.param(
        {"tools": {"enable_file_tools": True},
         "mode": "code_agent",
         "present": {"search_files"},
         "absent": {"glob", "grep"}},
        id="code-agent-without-ripgrep",
    ),
    pytest.param(
        {"tools": {"enable_file_tools": True}, "rg": True, "absent": {"glob", "grep"}},
        id="general-mode-with-ripgrep",
    ),
    pytest.param(
        {"mode": "code_agent", "rg": True, "absent": {"read_file", "glob", "grep"}},
        id="code-agent-files-disabled",
    ),
    pytest.param(
        {"tools": {"enable_bash": True,
                   "enable_file_tools": True,
                   "enable_todo": True,
                   "enable_plan": True,
                   "enable_sub_agent": True},
         "vision": True,
         "image": True,
         "sandbox": True,
         "mode": "code_agent",
         "rg": True,
         "owner": "test-owner",
         "present": {"bash",
                     "execute_code",
                     "generate_image",
                     "glob",
                     "grep",
                     "inspect_images",
                     "plan_write",
                     "read_file",
                     "sub_agent",
                     "todo_write"}},
        id="combined-session-capabilities",
    ),
])
def test_workspace_preflight_matches_real_registered_tools(tmp_path, monkeypatch, scenario):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    monkeypatch.delenv("BOX_AGENT_SKILL_TOOLS_ROOT", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    settings = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(enable_memory=False, enable_memory_extraction=False),
        tools=ToolsConfig(**{
            "enable_bash": False, "enable_file_tools": False,
            "enable_todo": False, "enable_plan": False,
            "enable_sub_agent": False, "enable_mcp": False,
            "enable_skills": False, **scenario.get("tools", {}),
        }),
    )
    settings.officev3.use_default_image_generation_preset = False
    settings.image_generation.endpoint = (
        "https://images.example.test/v1/images/generations" if scenario.get("image") else ""
    )
    llm = None
    if "vision" in scenario:
        llm = SimpleNamespace(
            model="test-model", api_base="https://models.example.test/v1",
            capabilities={"image_input": scenario["vision"]},
        )
    rg_path = str(tmp_path / "rg") if scenario.get("rg") else None
    monkeypatch.setattr(
        tool_setup, "resolve_ripgrep_executable",
        lambda runtime_env=None: (runtime_env or {}).get("BOX_AGENT_RG"),
    )
    sandbox_mode = scenario.get("sandbox", False)
    runtime_context = SkillRuntimeContext(
        runtimes={"node": SkillRuntime(
            kind="node", status="available", provider="host",
            env_vars={"BOX_AGENT_RG": rg_path},
        )} if rg_path else {},
        sandbox_mode=sandbox_mode,
    )
    runtime_env = build_skill_execution_env(runtime_context)
    common = {
        "sandbox_mode": sandbox_mode, "session_mode": scenario.get("mode"),
        "llm": llm, "process_owner_id": scenario.get("owner"),
    }
    predicted = tool_setup.resolve_workspace_tool_availability(
        settings, runtime_env=runtime_env, **common,
    )
    tools = []
    scratch = tool_setup.add_workspace_tools(
        tools, settings, tmp_path / "workspace", **common,
        skill_runtime_context=runtime_context,
        allow_full_access=scenario.get("full_access", True), output=lambda _: None,
    )
    try:
        actual = {tool.name for tool in tools}
        assert predicted == actual
        assert scenario.get("present", set()) <= actual
        assert not scenario.get("absent", set()).intersection(actual)
        assert {
            "publish_artifact", "request_user_input", "request_user_decision",
            "report_execution_result", "read_video",
        } <= actual
        # Registration is not execution: no Skill scratch or kernel was started.
        assert scratch.prepared is None
    finally:
        cleanup_skill_scratch_dir(scratch)


def test_workspace_preflight_does_not_construct_execution_resources(tmp_path, monkeypatch):
    settings = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(enable_memory=False, enable_memory_extraction=False),
        tools=ToolsConfig(enable_bash=True, enable_file_tools=True),
    )
    settings.officev3.use_default_image_generation_preset = False
    settings.image_generation.endpoint = ""
    monkeypatch.setattr(tool_setup, "resolve_ripgrep_executable", lambda runtime_env=None: None)

    def unexpected(*args, **kwargs):
        raise AssertionError("Preflight must not construct execution resources")

    for name in (
        "SessionSkillScratch", "BashTool", "ReadTool",
        "JupyterSandboxTool", "SandboxEnvironment", "TodoStore", "PlanStore",
        "RequestUserInputTool", "RequestUserDecisionTool", "ReportExecutionResultTool",
    ):
        monkeypatch.setattr(tool_setup, name, unexpected)
    from box_agent.tools import obsidian_tool
    monkeypatch.setattr(obsidian_tool, "ObsidianCliClient", unexpected)
    names = tool_setup.resolve_workspace_tool_availability(
        settings, sandbox_mode=True, runtime_env={},
    )
    assert {"bash", "read_file", "execute_code"} <= names
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_one_workspace_registration_is_used_by_preflight_and_construction(tmp_path, monkeypatch):
    from box_agent.tools.base import Tool, ToolResult

    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    settings = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(enable_memory=False, enable_memory_extraction=False),
        tools=ToolsConfig(enable_bash=False, enable_plan=False),
    )
    settings.image_generation.endpoint = ""
    registrations = tool_setup._workspace_tool_registrations
    constructed = []

    class ProbeTool(Tool):
        name = "new_workspace_probe"
        description = "Registration regression probe."
        parameters = {"type": "object", "properties": {}}

        def __init__(self):
            constructed.append(self)

        async def execute(self):
            return ToolResult(success=True, content="probe executed")

    def with_probe(*args, **kwargs):
        yield from registrations(*args, **kwargs)
        yield "new_workspace_probe", ProbeTool

    monkeypatch.setattr(tool_setup, "_workspace_tool_registrations", with_probe)
    assert "new_workspace_probe" in tool_setup.resolve_workspace_tool_availability(settings)
    assert not constructed
    tools = []
    scratch = tool_setup.add_workspace_tools(
        tools, settings, tmp_path / "workspace", output=lambda _: None,
        skill_runtime_context=SkillRuntimeContext(runtimes={}),
    )
    try:
        probe = next(tool for tool in tools if tool.name == "new_workspace_probe")
        assert constructed == [probe]
        result = await probe.execute()
        assert result.success and result.content == "probe executed"
    finally:
        cleanup_skill_scratch_dir(scratch)


def test_workspace_registration_preserves_shared_state_and_child_tool_order(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    settings = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(enable_memory=False, enable_memory_extraction=False),
        tools=ToolsConfig(enable_bash=False, enable_todo=True, enable_plan=True, enable_sub_agent=True),
    )
    settings.image_generation.endpoint = ""
    llm = SimpleNamespace(model="test-model", capabilities={"image_input": False})
    tools = []
    scratch = tool_setup.add_workspace_tools(
        tools, settings, tmp_path / "workspace", sandbox_mode=True, llm=llm,
        output=lambda _: None, skill_runtime_context=SkillRuntimeContext(runtimes={}, sandbox_mode=True),
    )
    try:
        actual = {tool.name: tool for tool in tools}
        assert actual["todo_write"]._store is actual["todo_read"]._store
        assert actual["plan_write"]._store is actual["plan_read"]._store
        assert actual["sandbox_status"]._bound_sandbox_tool is actual["execute_code"]
        assert tools[-1] is actual["sub_agent"]
        assert actual["sub_agent"]._child_tools_snapshot == {
            name: tool for name, tool in actual.items() if name != "sub_agent"
        }
        assert scratch.prepared is None
    finally:
        cleanup_skill_scratch_dir(scratch)


def test_obsidian_preflight_and_existing_factory_share_lazy_registrations(monkeypatch):
    from box_agent.tools import obsidian_tool

    constructed = []

    def create_client(env_context):
        constructed.append(env_context)
        return SimpleNamespace()

    monkeypatch.setattr(obsidian_tool, "ObsidianCliClient", create_client)
    context = object()
    declarations = list(obsidian_tool.obsidian_tool_registrations(context))
    assert not constructed
    tools = obsidian_tool.create_obsidian_tools(context)
    assert [name for name, _ in declarations] == [tool.name for tool in tools]
    assert constructed == [context] * len(tools)
