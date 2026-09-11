"""C1 characterization of the pre-Engine setup and Agent tool contract.

The fixed C1/C5 fixtures remain intact. Enumerated C5 changes, session-cwd
description changes and the connector search extension are applied before exact comparisons.
Network/runtime discovery is isolated; setup, tools, stores and Agent are real.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.agent import Agent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.memory import MemoryManager
from box_agent.tools import bash_tool, image_generation_tool, mcp_tool_catalog
from box_agent.tools import setup as tool_setup
from box_agent.tools import skill_loader as skill_loader_module
from box_agent.tools.base import build_tool_name_index
from box_agent.tools.mcp_loader import MCPTool
from box_agent.tools.runtime import SkillRuntimeContext


_ALWAYS_BASE = {"create_scheduled_task", "mcp_config"}
_SKILLS = {"get_skill", "list_skills"}
_LIST_SKILLS_SCHEMA = {
    "aliases": [],
    "schema": {
        "name": "list_skills",
        "description": (
            "List or search locally installed Skill names, descriptions and availability. "
            "Use an empty query to browse all available Skills, or an exact name to "
            "diagnose an unavailable Skill. This does not load instructions or access "
            "SkillHub. Follow next_offset for more results; if revision changes, restart "
            "from offset 0. Use get_skill to read a chosen Skill."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": ""},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "additionalProperties": False,
        },
    },
}
_ALWAYS_WORKSPACE = {
    "request_user_input", "request_user_decision", "report_execution_result",
    "obsidian_create_note", "obsidian_update_note", "obsidian_daily_note",
}
_FILES = {
    "read_file", "query_jsonl", "search_files", "write_file", "append_file", "edit_file",
}
_BASH = {"bash", "bash_output", "bash_kill"}
_TODO = {"todo_read", "todo_write"}
_PLAN = {"plan_read", "plan_write"}
_MEMORY = {"memory_read", "memory_write", "memory_search"}
_SANDBOX = {"execute_code", "sandbox_status"}
_GOALS = {"goal_read", "goal_write"}
_CHILD_DEFAULT_READS = {"query_jsonl", "read_file", "search_files"}
_FLAGS_OFF = {
    "enable_file_tools": False,
    "enable_bash": False,
    "enable_todo": False,
    "enable_plan": False,
    "enable_sub_agent": False,
    "enable_skills": False,
    "enable_mcp": False,
}
_SCHEMA_FIXTURE = Path(__file__).parent / "fixtures/tool_engine/c1_schemas.json"
_C5_SCHEMA_CHANGES = json.loads(
    (Path(__file__).parent / "fixtures/tool_engine/c5_schema_changes.json").read_text()
)
_CWD_SCHEMA_CHANGES = json.loads(
    (Path(__file__).parent / "fixtures/tool_engine/session_cwd_schema_changes.json").read_text()
)
_CONNECTOR_SEARCH_SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures/tool_engine/connector_search_schema.json").read_text(
        encoding="utf-8"
    )
)
_C5_DISCOVERABLE = {
    "append_file", "query_jsonl", "bash_output", "bash_kill", "sandbox_status",
    "mcp_config", "report_execution_result",
    "plan_read", "plan_write", "todo_read", "todo_write", "goal_read", "goal_write",
    "memory_read", "memory_search", "memory_write", "obsidian_create_note",
    "obsidian_update_note", "obsidian_daily_note", "search_skillhub", "install_skillhub_skill",
}


def _c5_names(names):
    return {_C5_SCHEMA_CHANGES["renames"].get(name, name) for name in names}


@pytest.fixture
def isolated_setup(tmp_path, monkeypatch):
    """Keep profile files and host/runtime discovery inside this test boundary."""
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    monkeypatch.setenv("BOX_AGENT_OBSIDIAN_CONFIG", str(profile / "obsidian.json"))
    monkeypatch.setattr(
        skill_loader_module, "SKILL_SETTINGS_PATH", profile / "skill-settings.json"
    )
    for key in (
        *image_generation_tool._ENDPOINT_ENV,
        *image_generation_tool._API_KEY_ENV,
        image_generation_tool._TIMEOUT_ENV,
        image_generation_tool._MAX_DIM_ENV,
    ):
        monkeypatch.delenv(key, raising=False)

    # Probe boundaries only: do not replace Tool constructors or setup branches.
    monkeypatch.setattr(bash_tool.platform, "system", lambda: "Linux")
    monkeypatch.setattr(bash_tool, "_resolve_login_shell", lambda: "/bin/bash")
    monkeypatch.setattr(
        tool_setup, "build_skill_runtime_context",
        lambda **kwargs: SkillRuntimeContext({}, sandbox_mode=kwargs["sandbox_mode"]),
    )
    monkeypatch.setattr(
        tool_setup, "build_skill_execution_env",
        lambda context: {"BOX_AGENT_HOME": str(profile)},
    )
    monkeypatch.setattr(
        tool_setup, "SandboxEnvironment",
        lambda: SimpleNamespace(venv_dir=profile / "sandbox-venv"),
    )

    catalog = mcp_tool_catalog.MCPToolCatalog()
    monkeypatch.setattr(mcp_tool_catalog, "_CATALOG", catalog)
    mcp_path = profile / "config/mcp.json"
    mcp_path.parent.mkdir()
    mcp_path.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setattr(
        tool_setup, "bootstrap_managed_mcp_config",
        lambda path: SimpleNamespace(path=mcp_path, warning=""),
    )
    # Timeout configuration is process-global; its behavior has dedicated tests.
    monkeypatch.setattr(tool_setup, "set_mcp_timeout_config", lambda **kwargs: None)
    remote = MCPTool(
        name="fixture_lookup", description="Look up a fixture record.",
        parameters={
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
        session=object(), server_name="fixture-server",
    )
    load_calls = []

    async def load_mcp(config_path, **kwargs):
        load_calls.append((config_path, kwargs))
        catalog.replace_server("fixture-server", [remote])
        catalog.mark_ready()
        return [remote]

    monkeypatch.setattr(tool_setup, "load_mcp_tools_async", load_mcp)
    return SimpleNamespace(
        profile=profile, catalog=catalog, remote=remote, load_calls=load_calls,
    )


def _config(env, flags=None, *, defaults=False):
    return Config(
        llm=LLMConfig(api_key="fixture-key", auth_file=str(env.profile / "auth.json")),
        agent=AgentConfig(
            workspace_dir=str(env.profile / "workspace"),
            memory_dir=str(env.profile / "memory"),
        ),
        tools=ToolsConfig(
            **({} if defaults else _FLAGS_OFF) | (flags or {}),
            skills_dir=str(env.profile / "skills"),
            mcp_config_path=str(env.profile / "config/mcp.json"),
        ),
    )


def _llm(mode):
    if mode == "none":
        return None
    capabilities = {} if mode == "unknown" else {"image_input": mode == "vision"}
    return SimpleNamespace(model="fixture-model", capabilities=capabilities)


async def _assemble(env, *, flags=None, defaults=False, llm_mode="none",
                    memory=False, sandbox=False, image_endpoint=False,
                    defer_skills=False, process_owner_id=None,
                    workspace_name="workspace"):
    config = _config(env, flags, defaults=defaults)
    if image_endpoint:
        config.image_generation.endpoint = "https://image.invalid/generate"
    llm = _llm(llm_mode)
    manager = MemoryManager(str(env.profile / "memory")) if memory else None
    tools, loader, mcp_task, skill_task = await tool_setup.initialize_base_tools(
        config, output=lambda *_: None, memory_manager=manager,
        llm=llm, defer_skills=defer_skills,
    )
    base_tools = list(tools)
    await tool_setup.await_skill_discovery(skill_task)
    loaded = await tool_setup.await_mcp_tools(mcp_task)
    tool_setup.merge_mcp_tools(tools, loaded)
    workspace = env.profile / workspace_name
    tool_setup.add_workspace_tools(
        tools, config, workspace,
        sandbox_mode=sandbox, allow_full_access=False, non_interactive=True,
        output=lambda *_: None, llm=llm, skill_loader=loader,
        process_owner_id=process_owner_id,
        env_context={"obsidian": {"enabled": False}},
    )
    return SimpleNamespace(
        config=config, tools=tools, base_tools=base_tools, loader=loader,
        mcp_task=mcp_task, skill_task=skill_task, manager=manager, llm=llm,
        workspace=workspace,
    )


def _agent(assembly, *, deferred=False):
    return Agent(
        llm_client=assembly.llm, system_prompt="Fixture system prompt.",
        tools=assembly.tools, workspace_dir=str(assembly.workspace),
        deferred_mcp_loading_enabled=deferred,
    )


def _normalized_schema(schema, profile):
    # Only the isolated workspace prefix changes between test runs. Preserve
    # descriptions, parameter types, bounds, enums, defaults and array ordering.
    serialized = json.dumps(schema, ensure_ascii=False)
    return json.loads(serialized.replace(str(profile), "<PROFILE>"))


def _assert_schema_contract(tools, profile, *, child_read_tools=()):
    expected = json.loads(_SCHEMA_FIXTURE.read_text(encoding="utf-8"))["tools"]
    # Preserve the old fixture and enumerate the Skill Engine's public additions.
    expected["list_skills"] = _LIST_SKILLS_SCHEMA
    expected["get_skill"]["schema"]["description"] = (
        "Read a Skill's method and resource paths. Follow next_offset with the returned revision "
        "when paged. Read required_skills before their steps; related_skills are optional. "
        "Skill guidance does not grant tools or permission. Use list_skills for names and availability. "
        "When using a Skill, follow its applicable workflow, required reference files and verification, "
        "consistent with the user request and permissions. If a required step is blocked, use an "
        "available permitted recovery or report it as incomplete; do not treat required steps as optional."
    )
    expected["get_skill"]["schema"]["input_schema"]["additionalProperties"] = False
    expected["get_skill"]["schema"]["input_schema"]["properties"].update({
        "offset": {"type": "integer", "minimum": 0, "description": "Zero-based line offset; omit to read the whole Skill when it fits."},
        "limit": {"type": "integer", "minimum": 1, "description": "Maximum lines for a bounded page."},
        "revision": {"type": "string", "description": "Version returned by a previous page; restart if it changed."},
    })
    index = build_tool_name_index(tools)
    expected_call_names = set()
    for tool in tools:
        baseline_name = next(
            (old for old, new in _C5_SCHEMA_CHANGES["renames"].items() if new == tool.name),
            tool.name,
        )
        entry = expected[baseline_name]
        entry["schema"]["name"] = tool.name
        if tool.name in _C5_SCHEMA_CHANGES["aliases"]:
            entry["aliases"] = _C5_SCHEMA_CHANGES["aliases"][tool.name]
        if tool.name in _C5_SCHEMA_CHANGES["descriptions"]:
            entry["schema"]["description"] = _C5_SCHEMA_CHANGES["descriptions"][tool.name]
        for change in _CWD_SCHEMA_CHANGES.get(tool.name, ()):
            target = entry["schema"]
            *parents, field = change["path"]
            for key in parents:
                target = target[key]
            assert target[field] == change["before"], (tool.name, change["path"])
            target[field] = change["after"]
        if tool.name == "tool_search":
            entry["schema"] = _CONNECTOR_SEARCH_SCHEMA
        if tool.name == "sub_agent":
            # This default is the one capability-dependent schema field. The
            # caller supplies the independently expected read set, never a set
            # inferred from the actual tool's live provider or parameter schema.
            entry["schema"]["input_schema"]["properties"]["required_tools"]["default"] = (
                sorted(child_read_tools)
            )
        assert list(tool.aliases) == entry["aliases"], tool.name
        assert _normalized_schema(tool.to_schema(), profile) == entry["schema"], tool.name
        schema = entry["schema"]
        assert _normalized_schema(tool.to_openai_schema(), profile) == {
            "type": "function",
            "function": {
                "name": schema["name"], "description": schema["description"],
                "parameters": schema["input_schema"],
            },
        }, tool.name
        for declared in (tool.name, *entry["aliases"]):
            for call_name in (declared, declared.replace("_", "-")):
                expected_call_names.add(call_name)
                assert index[call_name] is tool
    assert set(index) == expected_call_names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options, added",
    [
        pytest.param({}, set(), id="all-flags-disabled"),
        pytest.param({"flags": {"enable_file_tools": True}}, _FILES, id="files"),
        pytest.param({"flags": {"enable_bash": True}}, _BASH, id="bash"),
        pytest.param({"flags": {"enable_todo": True}}, _TODO, id="todo"),
        pytest.param({"flags": {"enable_plan": True}}, _PLAN, id="plan"),
        pytest.param({"memory": True}, _MEMORY, id="memory-manager"),
        pytest.param({"flags": {"enable_skills": True}}, _SKILLS, id="empty-skills"),
        pytest.param(
            {"flags": {"enable_skills": True}, "defer_skills": True},
            _SKILLS, id="deferred-empty-skills",
        ),
        pytest.param({"flags": {"enable_mcp": True}}, {"fixture_lookup"}, id="mcp"),
        pytest.param({"flags": {"enable_sub_agent": True}}, set(), id="subagent-no-llm"),
        pytest.param(
            {"flags": {"enable_sub_agent": True}, "llm_mode": "text"},
            {"sub_agent"}, id="subagent-text-llm",
        ),
        pytest.param({"llm_mode": "text"}, set(), id="text-only-llm"),
        pytest.param({"llm_mode": "vision"}, {"inspect_images"}, id="vision-llm"),
        pytest.param({"llm_mode": "unknown"}, {"inspect_images"}, id="unknown-vision-support"),
        pytest.param({"sandbox": True}, _SANDBOX, id="sandbox"),
        pytest.param({"image_endpoint": True}, {"generate_image"}, id="image-service"),
        pytest.param(
            {"defaults": True, "llm_mode": "text"},
            _FILES | _BASH | _TODO | _PLAN | _SKILLS | {"sub_agent", "fixture_lookup"},
            id="config-defaults",
        ),
        pytest.param(
            {"defaults": True, "llm_mode": "vision", "memory": True,
             "sandbox": True, "image_endpoint": True, "process_owner_id": "fixture-session"},
            _FILES | _BASH | _TODO | _PLAN | _MEMORY | _SANDBOX | _SKILLS
            | {"sub_agent", "fixture_lookup", "inspect_images", "generate_image"},
            id="all-capabilities-session-owned",
        ),
    ],
)
async def test_setup_capability_matrix_preserves_exact_tools_and_schemas(
    isolated_setup, options, added,
):
    assembly = await _assemble(isolated_setup, **options)
    expected = _c5_names(_ALWAYS_BASE | _ALWAYS_WORKSPACE | added)
    assert {tool.name for tool in assembly.tools} == expected
    assert (assembly.mcp_task is not None) == ("fixture_lookup" in added)
    assert len(isolated_setup.load_calls) == int("fixture_lookup" in added)
    assert (assembly.loader is not None) == ("get_skill" in added)
    assert (assembly.skill_task is not None) == options.get("defer_skills", False)
    agent = _agent(assembly)
    assert set(agent.tools) == expected | _GOALS | {"tool_search"}
    expected_direct = (expected - _C5_DISCOVERABLE) | {"tool_search"}
    if "read_file" in expected:
        expected_direct.add("query_jsonl")
    if options.get("process_owner_id") is not None:
        expected_direct.add("report_execution_result")
    assert agent.mcp_tool_exposure.prepare_tools(list(agent.tools.values())).offered_names == expected_direct
    # Agent must retain each actual setup object, including last-wins helpers.
    assembled_by_name = {tool.name: tool for tool in assembly.tools}
    assert all(agent.tools[name] is tool for name, tool in assembled_by_name.items())
    _assert_schema_contract(
        list(agent.tools.values()), isolated_setup.profile,
        child_read_tools=expected & _CHILD_DEFAULT_READS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True], ids=["eager", "deferred"])
async def test_utility_agent_keeps_goal_tools_and_session_local_state(isolated_setup, deferred):
    agents = [
        Agent(
            llm_client=_llm("text"), system_prompt="Utility.", tools=[],
            workspace_dir=str(isolated_setup.profile / f"utility-{index}"),
            deferred_mcp_loading_enabled=deferred,
        )
        for index in range(2)
    ]
    expected = _GOALS | {"tool_search"}
    for agent in agents:
        assert set(agent.tools) == expected
        assert agent.goal is None
        assert agent.mcp_tool_exposure.prepare_tools(list(agent.tools.values())).offered_names == {"tool_search"}
        assert agent.tools["goal_read"]._agent is agent
        assert agent.tools["goal_write"]._agent is agent
        _assert_schema_contract(list(agent.tools.values()), isolated_setup.profile)
    assert agents[0].activated_mcp_tools is not agents[1].activated_mcp_tools
    assert agents[0].tool_result_storage is not agents[1].tool_result_storage
    result = await agents[0].tools["goal_write"].execute(action="set", objective="Fixture goal")
    assert result.success
    assert agents[0].goal.objective == "Fixture goal"
    assert agents[1].goal is None
    assert agents[0].mcp_tool_exposure.prepare_tools(list(agents[0].tools.values())).offered_names == expected
    assert agents[1].mcp_tool_exposure.prepare_tools(list(agents[1].tools.values())).offered_names == {"tool_search"}


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_skills", [False, True], ids=["inline", "deferred"])
async def test_setup_preserves_shared_resources_and_session_owners(isolated_setup, defer_skills):
    skill_path = isolated_setup.profile / "skills/fixture-skill/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: fixture-skill\ndescription: A local fixture skill.\n---\nFixture instructions.\n",
        encoding="utf-8",
    )
    assembly = await _assemble(
        isolated_setup, defaults=True, llm_mode="vision", memory=True,
        sandbox=True, image_endpoint=True, defer_skills=defer_skills,
        process_owner_id="session-a",
    )
    agent = _agent(assembly)
    tools = agent.tools
    for name in _MEMORY:
        assert tools[name]._mgr is assembly.manager
    assert tools["memory_write"]._llm is assembly.llm
    assert tools["todo_write"]._store is tools["todo_read"]._store
    assert tools["plan_write"]._store is tools["plan_read"]._store
    assert tools["plan_write"]._store is not tools["todo_write"]._store
    assert tools["get_skill"].skill_loader is assembly.loader
    assert tools["list_skills"].skill_loader is assembly.loader
    assert set(assembly.loader.loaded_skills) == {"fixture-skill"}
    assert tools["sub_agent"]._resolve_skill_loader() is assembly.loader
    assert tools["sandbox_status"]._bound_sandbox_tool is tools["execute_code"]
    assert tools["execute_code"].process_owner_id == "session-a"
    for name in ("bash_output", "bash_kill"):
        base = next(tool for tool in assembly.base_tools if tool.name == name)
        assert base.process_owner_id is None
        assert tools[name] is not base
        assert tools[name].process_owner_id == tools["bash"].process_owner_id == "session-a"
        assert sum(tool.name == name for tool in assembly.tools) == 2

    other = await _assemble(
        isolated_setup, flags={"enable_bash": True, "enable_plan": True, "enable_todo": True},
        sandbox=True, process_owner_id="session-b", workspace_name="workspace-b",
    )
    other_tools = _agent(other).tools
    for name in ("plan_read", "todo_read"):
        assert other_tools[name]._store is not tools[name]._store
    assert other_tools["sandbox_status"]._bound_sandbox_tool is other_tools["execute_code"]
    assert other_tools["execute_code"] is not tools["execute_code"]
    assert other_tools["execute_code"].process_owner_id == "session-b"


@pytest.mark.asyncio
async def test_setup_agent_discovery_keeps_live_child_tools_and_session_activation(isolated_setup):
    assembly = await _assemble(
        isolated_setup, flags={"enable_mcp": True, "enable_sub_agent": True}, llm_mode="text",
    )
    agent = _agent(assembly, deferred=True)
    other = Agent(
        llm_client=assembly.llm, system_prompt="Second session.", tools=[],
        workspace_dir=str(isolated_setup.profile / "other"),
    )
    child = agent.tools["sub_agent"]
    assert "fixture_lookup" not in agent.tools
    assert "fixture_lookup" not in child._resolve_child_tools()
    assert "tool_search" not in child._resolve_child_tools()
    assert child._resolve_child_tools()["goal_read"] is agent.tools["goal_read"]

    result = await agent.tools["tool_search"].execute(query="fixture_lookup", top_k=1)
    assert result.success
    assert child._resolve_child_tools()["fixture_lookup"] is isolated_setup.remote
    assert "fixture_lookup" not in other._inherited_tools()
    assert "sub_agent" not in child._resolve_child_tools()
    assert "tool_search" not in child._resolve_child_tools()
    # Exposure borrows the Agent's activation store, which survives preparation.
    activated = agent.activated_mcp_tools
    first = agent.mcp_tool_exposure.prepare_tools(list(agent.tools.values()))
    second = agent.mcp_tool_exposure.prepare_tools(list(agent.tools.values()))
    assert agent.activated_mcp_tools is activated
    assert first.tools[-1] is second.tools[-1] is isolated_setup.remote
    _assert_schema_contract(first.tools, isolated_setup.profile)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_output_child", [False, True], ids=["empty-cwd", "ordinary-output-child"])
async def test_workspace_root_is_shared_by_file_shell_and_image_tools(isolated_setup, existing_output_child):
    if existing_output_child:
        (isolated_setup.profile / "workspace" / "output").mkdir(parents=True)
    assembly = await _assemble(
        isolated_setup, flags={"enable_file_tools": True, "enable_bash": True},
        llm_mode="vision", image_endpoint=True,
    )
    agent = _agent(assembly)
    tools = agent.tools
    assert "append_file" not in agent.mcp_tool_exposure.prepare_tools(list(tools.values())).offered_names
    expected_root = assembly.workspace
    assert Path(tools["bash"].workspace_dir) == expected_root
    assert Path(tools["bash"].scope_root_dir) == assembly.workspace
    assert tools["generate_image"].output_dir == expected_root
    for name in _FILES | {"inspect_images"}:
        assert Path(tools[name].workspace_dir) == assembly.workspace
        assert Path(tools[name].relative_root_dir) == expected_root
    for relative_path in ("report.txt", "output/report.txt"):
        result = await tools["write_file"].execute(path=relative_path, content="report")
        assert result.success
        assert (expected_root / relative_path).read_text(encoding="utf-8") == "report"
        assert tools["generate_image"]._resolve_output_path(relative_path) == expected_root / relative_path


@pytest.mark.asyncio
async def test_image_endpoint_environment_enables_registered_tool(isolated_setup, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_IMAGE_GENERATION_ENDPOINT", "https://image.invalid/env")
    assembly = await _assemble(isolated_setup)
    tools = _agent(assembly).tools
    assert set(tools) == _c5_names(_ALWAYS_BASE | _ALWAYS_WORKSPACE | _GOALS) | {"generate_image", "tool_search"}
    assert tools["generate_image"].endpoint == "https://image.invalid/env"
