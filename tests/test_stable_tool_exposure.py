import json
from collections import OrderedDict

import pytest

from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine.engine import DefaultToolEngine
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from box_agent.tools.tool_call_tool import CallTool


class BrowserTool(Tool):
    name = "user_browser_read_current_page"
    description = "Read current browser page"
    parameters = {"type": "object", "properties": {}}
    async def execute(self):
        return ToolResult(success=True, content="ok")


class Remote(Tool):
    name = "remote_lookup"
    description = "Lookup"
    parameters = {"type": "object", "properties": {}}
    mcp_tool_id = "mcp:crm/remote_lookup"
    server_name = "crm"
    mcp_always_load = True
    @property
    def mcp_generation(self):
        return self._mcp_generation
    async def execute(self):
        return ToolResult(success=True, content="ok")


def make():
    catalog = MCPToolCatalog()
    active = OrderedDict()
    manager = MCPToolExposureManager(catalog, active)
    search = ToolSearchTool(catalog, active)
    manager.enable_stable_prefix(search)
    browser = BrowserTool()
    tools = {tool.name: tool for tool in [search, CallTool(), browser]}
    return catalog, manager, search, DefaultToolEngine(tools=tools, tool_exposure=manager)


@pytest.mark.asyncio
async def test_conditional_browser_not_direct_and_visibility_applies_to_search_and_ref():
    _, _, search, engine = make()
    allowed = {"value": False}
    visible = lambda name: name != BrowserTool.name or allowed["value"]
    before = engine.prepare_tools(is_tool_visible=visible)
    assert BrowserTool.name not in [definition.name for definition in before.definitions]
    assert not json.loads((await search.execute(tool_names=[BrowserTool.name])).content)["activated"]
    allowed["value"] = True
    engine.prepare_tools(is_tool_visible=visible)
    result = await search.execute(tool_names=[BrowserTool.name])
    ref = json.loads(result.content)["activated"][0]["tool_ref"]
    engine._tool_exposure.record_search_result("search", result.content)
    engine._tool_exposure.acknowledge_search_results([Message(role="tool", name="tool_search", tool_call_id="search", content=result.content)])
    prepared = engine.prepare_tools(is_tool_visible=visible)
    call = ToolCall(id="x", type="function", function=FunctionCall(name="call_tool", arguments={"tool_ref": ref, "arguments_json": "{}"}))
    assert prepared.resolve_call(call)[2] is None
    allowed["value"] = False
    assert prepared.resolve_call(call)[2]
    assert [item.to_schema() for item in before.definitions] == [item.to_schema() for item in prepared.definitions]


def test_always_load_ref_is_ineligible_until_description_acknowledged():
    catalog, manager, _, engine = make()
    tool = Remote()
    catalog.replace_server("crm", [tool])
    engine.prepare_tools()
    descriptions = manager.pending_bootstrap_descriptions()
    ref = json.loads(descriptions[0])["tool_ref"]
    call = ToolCall(id="x", type="function", function=FunctionCall(name="call_tool", arguments={"tool_ref": ref, "arguments_json": "{}"}))
    assert engine.prepare_tools().resolve_call(call)[2]
    manager.ack_bootstrap_descriptions(descriptions)
    assert manager.pending_bootstrap_descriptions() == ()
    assert engine.prepare_tools().resolve_call(call)[2] is None
    catalog.replace_server("crm", [Remote()])
    assert engine.prepare_tools().resolve_call(call)[2]
    assert len(manager.pending_bootstrap_descriptions()) == 1


@pytest.mark.asyncio
async def test_eager_mcp_tool_order_preserves_discovered_refs_until_replacement():
    from tests.test_mcp_tool_search import FakeMCPTool

    catalog, activated = MCPToolCatalog(), OrderedDict()
    manager = MCPToolExposureManager(catalog, activated, deferred_mcp=False)
    search = ToolSearchTool(catalog, activated)
    manager.enable_stable_prefix(search)
    tools = {tool.name: tool for tool in [
        search, CallTool(), FakeMCPTool("z_tool", "demo"), FakeMCPTool("a_tool", "demo"),
    ]}
    engine = DefaultToolEngine(tools=tools, tool_exposure=manager)
    before = engine.prepare_tools()
    result = await search.execute(tool_names=["z_tool"])
    ref = json.loads(result.content)["activated"][0]["tool_ref"]
    manager.record_search_result("search", result.content)
    manager.acknowledge_search_results([Message(
        role="tool", name="tool_search", tool_call_id="search", content=result.content,
    )])
    generation = catalog.get_by_model_name("z_tool").generation
    call = ToolCall(id="invoke", type="function", function=FunctionCall(
        name="call_tool", arguments={"tool_ref": ref, "arguments_json": "{}"},
    ))
    for _ in range(3):
        prepared = engine.prepare_tools()
        assert catalog.get_by_model_name("z_tool").generation == generation
        resolved, routed, error = prepared.resolve_call(call)
        assert routed and error is None and resolved.function.name == "z_tool"
        assert [t.to_schema() for t in prepared.definitions] == [t.to_schema() for t in before.definitions]
    tools["z_tool"] = FakeMCPTool("z_tool", "demo")
    assert engine.prepare_tools().resolve_call(call)[2] is not None
    assert catalog.get_by_model_name("z_tool").generation > generation


def test_host_required_local_tool_descriptions_follow_plan_and_goal_without_schema_changes():
    class Local(Tool):
        description = "Local capability"
        parameters = {"type": "object", "properties": {}}
        def __init__(self, name):
            self._name = name
        @property
        def name(self):
            return self._name
        async def execute(self):
            return ToolResult(success=True, content="ok")

    names = {"plan_write", "todo_write", "goal_write", "memory_search"}
    required = set()
    catalog = MCPToolCatalog()
    active = OrderedDict()
    manager = MCPToolExposureManager(catalog, active,
        deferred_local_names_provider=lambda: frozenset(names - required))
    search = ToolSearchTool(catalog, active)
    manager.enable_stable_prefix(search)
    tools = {tool.name: tool for tool in [search, CallTool(), *(Local(name) for name in sorted(names))]}
    engine = DefaultToolEngine(tools=tools, tool_exposure=manager)
    initial = engine.prepare_tools()
    baseline = [definition.to_schema() for definition in initial.definitions]
    assert manager.pending_bootstrap_descriptions() == ()
    required.update({"plan_write", "todo_write"})
    descriptions = manager.pending_bootstrap_descriptions()
    assert {json.loads(text)["name"] for text in descriptions} == {"plan_write", "todo_write"}
    plan = next(json.loads(text) for text in descriptions if json.loads(text)["name"] == "plan_write")
    request = ToolCall(id="plan", type="function", function=FunctionCall(name="call_tool", arguments={"tool_ref": plan["tool_ref"], "arguments_json": "{}"}))
    assert engine.prepare_tools().resolve_call(request)[2]
    manager.ack_bootstrap_descriptions(descriptions)
    current = engine.prepare_tools()
    assert current.resolve_call(request)[2] is None
    assert current.validate_call("plan_write") is not None
    assert [definition.to_schema() for definition in current.definitions] == baseline
    required.add("goal_write")
    assert [json.loads(text)["name"] for text in manager.pending_bootstrap_descriptions()] == ["goal_write"]
    assert [definition.to_schema() for definition in engine.prepare_tools().definitions] == baseline


def test_initial_common_and_explicit_tools_remain_direct_when_policy_changes():
    class Local(Tool):
        description = "Local capability"
        parameters = {"type": "object", "properties": {}}
        def __init__(self, name, explicit=False):
            self._name = name
            self._model_exposure_direct = explicit
        @property
        def name(self):
            return self._name
        async def execute(self):
            return ToolResult(success=True, content="ok")
    _, manager, search, engine = make()
    for tool in [Local("read_file"), Local("query_jsonl"), Local("bash_output"),
                 Local("bash_kill"), Local("memory_search", explicit=True), Local("plan_write")]:
        engine._tools[tool.name] = tool
    first = engine.prepare_tools()
    names = {definition.name for definition in first.definitions}
    assert {"read_file", "query_jsonl", "bash_output", "bash_kill", "memory_search"} <= names
    assert "plan_write" not in names
    engine._tools["plan_write"]._model_exposure_direct = True
    assert [definition.to_schema() for definition in engine.prepare_tools().definitions] == [definition.to_schema() for definition in first.definitions]


@pytest.mark.parametrize("stable", [False, True])
def test_child_inheritance_excludes_gateways_and_preserves_scoped_capabilities(stable):
    from tests.test_mcp_tool_search import FakeCoreTool, FakeMCPTool
    from box_agent.tools.sub_agent_capabilities import (
        CapabilityFailure, CapabilityResolver, parse_delegation_spec,
    )

    catalog = MCPToolCatalog()
    active = OrderedDict()
    allowed = {"allowed"}
    manager = MCPToolExposureManager(catalog, active,
        allowed_connector_ids_provider=lambda: frozenset(allowed))
    search = ToolSearchTool(catalog, active)
    if stable:
        manager.enable_stable_prefix(search)
    local = FakeCoreTool("read_file")
    active_remote = FakeMCPTool("active_remote", "active", connector_id="allowed")
    eager_remote = FakeMCPTool("eager_remote", "eager", always_load=True, connector_id="allowed")
    hidden_remote = FakeMCPTool("hidden_remote", "hidden", connector_id="allowed")
    denied_remote = FakeMCPTool("denied_remote", "denied", always_load=True, connector_id="denied")
    for server, tool in (("active", active_remote), ("eager", eager_remote),
                         ("hidden", hidden_remote), ("denied", denied_remote)):
        catalog.replace_server(server, [tool])
    manager.activate_server("active")
    tools = {tool.name: tool for tool in [local, search, CallTool()]}
    inherited = manager.inherited_tools(tools)
    assert inherited == {"read_file": local, "active_remote": active_remote, "eager_remote": eager_remote}
    assert {"tool_search", "call_tool", "hidden_remote", "denied_remote"}.isdisjoint(inherited)
    spec = parse_delegation_spec(task="read", required_tools=["read_file"])
    resolved = CapabilityResolver().resolve(spec, parent_tools=inherited)
    assert not isinstance(resolved, CapabilityFailure)
    assert resolved.tools == {"read_file": local}
    hidden_spec = parse_delegation_spec(task="read", required_tools=["hidden_remote"])
    assert isinstance(CapabilityResolver().resolve(hidden_spec, parent_tools=inherited), CapabilityFailure)


def test_scoped_child_freezes_definitions_and_rejects_reconnect_and_revocation():
    from tests.test_mcp_tool_search import FakeCoreTool, FakeMCPTool
    allowed = {"connected"}
    catalog = MCPToolCatalog()
    active = OrderedDict()
    parent = MCPToolExposureManager(catalog, active,
        allowed_connector_ids_provider=lambda: frozenset(allowed))
    search = ToolSearchTool(catalog, active)
    parent.enable_stable_prefix(search)
    local = FakeCoreTool("read_file")
    remote = FakeMCPTool("remote", "server", "first", connector_id="connected")
    hidden = FakeMCPTool("hidden", "hidden", "hidden", always_load=True)
    catalog.replace_server("server", [remote])
    catalog.replace_server("hidden", [hidden])
    parent.activate_server("server")
    tools = {tool.name: tool for tool in [search, CallTool(), local]}
    DefaultToolEngine(tools=tools, tool_exposure=parent).prepare_tools()
    child = parent.for_inherited_tools({"read_file": local, "remote": remote})
    child_tools = {"read_file": local, "remote": remote}
    engine = DefaultToolEngine(tools=child_tools, tool_exposure=child)
    before = engine.prepare_tools()
    assert {definition.name for definition in before.definitions} == {"read_file", "remote"}
    assert before.validate_call("remote") is None
    assert before.validate_call("hidden") is not None
    assert child.pending_bootstrap_descriptions() == ()
    assert set(child.inherited_tools({**child_tools, "hidden": hidden})) == {"read_file", "remote"}
    remote._description = "changed presentation"
    after = engine.prepare_tools()
    assert [item.to_schema() for item in before.definitions] == [item.to_schema() for item in after.definitions]
    allowed.clear()
    assert after.validate_call("remote") is not None
    allowed.add("connected")
    assert after.validate_call("remote") is None
    catalog.replace_server("server", [remote])
    assert after.validate_call("remote") is not None
    assert engine.prepare_tools().validate_call("remote") is not None


@pytest.mark.asyncio
async def test_scoped_child_request_schemas_stay_frozen_across_model_steps():
    from tests.test_mcp_tool_search import FakeCoreTool
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import StreamEvent
    _, parent, _, engine = make()
    local = FakeCoreTool("read_file")
    engine._tools[local.name] = local
    engine.prepare_tools()
    child = parent.for_inherited_tools({local.name: local})
    requests = []
    class LLM:
        async def generate_stream(self, messages, tools=None, **kwargs):
            requests.append([tool.to_schema() for tool in tools])
            if len(requests) == 1:
                local._description = "changed"
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id="read", type="function", function=FunctionCall(name="read_file", arguments={}))])
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")
    events = [event async for event in run_agent_loop(
        llm=LLM(), tools={local.name: local}, tool_exposure_manager=child,
        messages=[Message(role="user", content="read")], max_steps=2)]
    assert len(requests) == 2 and requests[0] == requests[1]


@pytest.mark.asyncio
async def test_child_mcp_permission_retry_fails_after_parent_revokes_connector():
    from tests.test_mcp_tool_search import FakeMCPTool
    from box_agent.events import ToolCallResult
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import StreamEvent
    allowed = {"connected"}
    catalog = MCPToolCatalog()
    active = OrderedDict()
    parent = MCPToolExposureManager(catalog, active,
        allowed_connector_ids_provider=lambda: frozenset(allowed))
    search = ToolSearchTool(catalog, active)
    parent.enable_stable_prefix(search)
    class Gated(FakeMCPTool):
        async def execute(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ToolResult(success=False, permission_request={"scope": "fixture"})
            return ToolResult(success=True, content="unexpected effect")
    remote = Gated("remote", "server", connector_id="connected")
    catalog.replace_server("server", [remote])
    parent.activate_server("server")
    DefaultToolEngine(tools={"tool_search": search, "call_tool": CallTool()}, tool_exposure=parent).prepare_tools()
    child = parent.for_inherited_tools({"remote": remote})
    class Approver:
        async def negotiate(self, request):
            allowed.clear()
            return True
    class LLM:
        requests = 0
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests += 1
            if self.requests == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                    id="remote", type="function", function=FunctionCall(name="remote", arguments={}))])
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")
    events = [event async for event in run_agent_loop(
        llm=LLM(), tools={"remote": remote}, tool_exposure_manager=child,
        messages=[Message(role="user", content="run")], permission_negotiator=Approver(), max_steps=2)]
    assert remote.calls == 1
    assert not next(event for event in events if isinstance(event, ToolCallResult)).success


def test_bootstrap_redelivers_after_compaction_and_bounds_aggregate():
    from tests.test_mcp_tool_search import FakeMCPTool
    catalog, manager, _, engine = make()
    tools = [FakeMCPTool(f"eager_{i}", "bulk", "x" * 12000, always_load=True) for i in range(6)]
    catalog.replace_server("bulk", tools)
    engine.prepare_tools()
    descriptions = manager.pending_bootstrap_descriptions(visible_history=[])
    assert sum(map(len, descriptions)) < 48_000
    assert any("omitted_count" in json.loads(text) for text in descriptions)
    initial = [text for text in descriptions if "tool_ref" in json.loads(text)]
    manager.ack_bootstrap_descriptions(descriptions)
    history = [Message(role="user", source="runtime", name="tool_descriptions", content=text) for text in descriptions]
    next_items = manager.pending_bootstrap_descriptions(visible_history=history)
    assert not set(initial) & set(next_items)
    restored = manager.pending_bootstrap_descriptions(visible_history=[])
    assert initial[0] in restored


@pytest.mark.asyncio
async def test_discovered_mcp_can_be_rediscovered_after_history_compaction_with_fixed_definitions():
    from tests.test_mcp_tool_search import FakeMCPTool
    catalog, manager, search, engine = make()
    original = [tool.to_schema() for tool in engine.prepare_tools().definitions]
    target = FakeMCPTool('lookup', 'crm', 'Look up a synthetic record')
    catalog.replace_server('crm', [target])

    async def discover(call_id):
        result = await search.execute(tool_names=['lookup'])
        manager.record_search_result(call_id, result.content)
        manager.acknowledge_search_results([Message(role='tool', name='tool_search',
            tool_call_id=call_id, content=result.content)])
        ref = json.loads(result.content)['activated'][0]['tool_ref']
        return ToolCall(id='execute', type='function', function=FunctionCall(
            name='call_tool', arguments={'tool_ref': ref, 'arguments_json': '{}'}))

    call = await discover('before')
    assert engine.prepare_tools().resolve_call(call)[2] is None
    # Removing conversation text neither disconnects MCP nor changes top-level schemas.
    assert manager.pending_bootstrap_descriptions(visible_history=[]) == ()
    repeated = await discover('after-compaction')
    assert repeated.function.arguments['tool_ref'] == call.function.arguments['tool_ref']
    assert engine.prepare_tools().resolve_call(repeated)[2] is None
    catalog.replace_server('crm', [FakeMCPTool('lookup', 'crm', 'New connection')])
    assert engine.prepare_tools().resolve_call(call)[2] is not None
    fresh = await discover('after-reconnection')
    assert fresh.function.arguments['tool_ref'] != call.function.arguments['tool_ref']
    assert engine.prepare_tools().resolve_call(fresh)[2] is None
    assert [tool.to_schema() for tool in engine.prepare_tools().definitions] == original


@pytest.mark.asyncio
async def test_search_bounds_whole_schemas_without_activating_omitted_tools():
    from tests.test_mcp_tool_search import FakeMCPTool
    catalog, manager, search, engine = make()
    giant = FakeMCPTool("giant", "bulk", "huge", parameters={"type": "object", "description": "x" * 50000})
    items = [FakeMCPTool(f"medium_{i}", "bulk", "x" * 12000) for i in range(5)]
    catalog.replace_server("bulk", [giant, *items])
    engine.prepare_tools()
    result = await search.execute(server_name="bulk")
    payload = json.loads(result.content)
    assert payload["oversized_schema_count"] == 1
    assert payload["omitted_for_size_count"] > 0
    assert len(result.content) < 64_000
    assert all(item["input_schema"] == next(tool.parameters for tool in items if tool.name == item["name"]) for item in payload["activated"])
    active_names = {value.model_name for value in manager._activated.values()}
    assert active_names == {item["name"] for item in payload["activated"]}
    assert "giant" not in active_names
    assert all(target is not giant for target, _, _ in manager._stable_refs.values())
    omitted = next(tool.name for tool in items if tool.name not in active_names)
    exact = json.loads((await search.execute(tool_names=[omitted])).content)
    assert [item["name"] for item in exact["activated"]] == [omitted]


@pytest.mark.asyncio
async def test_subagent_executes_scoped_fork_after_sandbox_replacement(tmp_path, monkeypatch):
    from box_agent.tools.jupyter_tool import JupyterSandboxTool
    from box_agent.tools.sub_agent_tool import SubAgentTool
    from box_agent.schema import StreamEvent
    observed = []
    parent_sandbox = JupyterSandboxTool(workspace_dir=str(tmp_path), process_owner_id="parent")
    async def execute(self, code, **kwargs):
        observed.append((self, code, self.process_owner_id, self._requires_subprocess))
        return ToolResult(success=True, content="child execution")
    monkeypatch.setattr(JupyterSandboxTool, "execute", execute)
    class LLM:
        requests = 0
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests += 1
            if self.requests == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                    id="python", type="function", function=FunctionCall(
                        name="execute_code", arguments={"code": "print(1)"}))])
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")
    parent_tools = {"execute_code": parent_sandbox}
    catalog, active = MCPToolCatalog(), OrderedDict()
    manager = MCPToolExposureManager(catalog, active)
    manager.enable_stable_prefix(ToolSearchTool(catalog, active))
    DefaultToolEngine(tools=parent_tools, tool_exposure=manager).prepare_tools()
    child = SubAgentTool(llm=LLM(), parent_tools=parent_tools, workspace_dir=str(tmp_path))
    child.set_tool_provider(lambda: manager.inherited_tools(parent_tools))
    child.set_tool_exposure_provider(lambda: manager)
    result = await child.execute(task="Run a calculation", required_tools=["execute_code"])
    assert result.success and len(observed) == 1
    tool, code, owner, subprocess_required = observed[0]
    assert tool is not parent_sandbox and code == "print(1)"
    assert owner.startswith("parent:subagent-") and subprocess_required
