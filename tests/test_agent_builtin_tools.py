"""Native capability discovery and construction share one registration source."""
from types import SimpleNamespace

import pytest

import box_agent.agent as agent_module
from box_agent.tools.base import Tool, ToolResult


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("enabled", [False, True])
def test_native_availability_matches_registered_tools(tmp_path, version, enabled):
    names = agent_module.resolve_agent_tool_availability(
        enable_builtin_tools=enabled, tool_interface_version=version,
    )
    agent = agent_module.Agent(
        SimpleNamespace(), "system", [], workspace_dir=str(tmp_path / "workspace"),
        enable_builtin_tools=enabled, tool_interface_version=version,
    )
    expected = {"tool_search", "goal_read", "goal_write"} if enabled else set()
    if enabled and version == 2:
        expected.add("call_tool")
    assert names == set(agent.tools) == expected
    assert all(name == tool.name for name, tool in agent.tools.items())
    assert (agent.mcp_tool_exposure is not None) == enabled


@pytest.mark.parametrize("version", [1, 2])
def test_native_preflight_does_not_construct_tools(monkeypatch, version):
    def unexpected(*args, **kwargs):
        raise AssertionError("Preflight must not construct native tools or MCP state")

    for name in (
        "ToolSearchTool", "_GoalReadTool", "_GoalWriteTool",
        "MCPToolExposureManager", "get_mcp_tool_catalog",
    ):
        monkeypatch.setattr(agent_module, name, unexpected)
    monkeypatch.setattr("box_agent.tools.tool_call_tool.CallTool", unexpected)
    names = agent_module.resolve_agent_tool_availability(tool_interface_version=version)
    assert {"tool_search", "goal_read", "goal_write"} <= names
    assert ("call_tool" in names) == (version == 2)


class ProbeTool(Tool):
    @property
    def name(self):
        return "native_registration_probe"

    @property
    def description(self):
        return "Verify a newly registered native capability."

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(success=True, content="native capability ran")


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("phase", [0, 1], ids=["discovery", "state"])
@pytest.mark.asyncio
async def test_one_native_definition_updates_preflight_and_construction(
    tmp_path, monkeypatch, version, phase,
):
    original = agent_module._agent_tool_registrations
    constructed = []

    def extended(**kwargs):
        registrations = original(**kwargs)
        if kwargs.get("enable_builtin_tools", True):
            def create(agent, catalog, allowed_connectors):
                # Keep the two existing initialization boundaries intact.
                assert hasattr(agent, "messages") == (phase == 1)
                constructed.append(agent)
                return ProbeTool()
            registrations[phase]["native_registration_probe"] = create
        return registrations

    monkeypatch.setattr(agent_module, "_agent_tool_registrations", extended)
    names = agent_module.resolve_agent_tool_availability(tool_interface_version=version)
    assert "native_registration_probe" in names
    assert not constructed

    agent = agent_module.Agent(
        SimpleNamespace(), "system", [], workspace_dir=str(tmp_path / "workspace"),
        tool_interface_version=version,
    )
    assert names == set(agent.tools)
    assert constructed == [agent]
    result = await agent.tools["native_registration_probe"].execute()
    assert result.success and result.content == "native capability ran"

    utility = agent_module.Agent(
        SimpleNamespace(), "system", [], workspace_dir=str(tmp_path / "utility"),
        enable_builtin_tools=False, tool_interface_version=version,
    )
    assert not utility.tools
    assert not agent_module.resolve_agent_tool_availability(
        enable_builtin_tools=False, tool_interface_version=version,
    )
    assert constructed == [agent]
