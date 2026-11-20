"""REPL-only discovery does not revoke the internal Browser backend."""
import json
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from box_agent.plugins.repl import RunBinding
from box_agent.plugins.repl.runtime import ReplRuntime, Realm
from box_agent.plugins.repl.tools import create_tools
from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.tools.engine.engine import DefaultToolEngine
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from box_agent.tools.tool_call_tool import CallTool
from tests.test_local_tool_search import LocalTool
from tests.test_mcp_tool_search import FakeMCPTool


def setup(tmp_path, *, stable=True, deferred=True):
    catalog, active = MCPToolCatalog(), OrderedDict()
    manager = MCPToolExposureManager(catalog, active, deferred_mcp=deferred)
    gui = [FakeMCPTool(name, server, "click GUI", always_load=True) for name, server in [
        ("managed_browser_click", "playwright"), ("user_browser_click", "browser-gateway"),
        ("mcp__computer-use__click", "computer-use"), ("custom_click", "desktop-custom"),
    ]]
    other = FakeMCPTool("crm_lookup", "crm", "lookup", always_load=True)
    for tool in [*gui, other]:
        catalog.replace_server(tool.server_name, [tool])
    read = FakeMCPTool("user_browser_read_current_page", "browser-gateway", "read current browser body", always_load=True)
    catalog.replace_server("browser-gateway", [gui[1], read])
    gui.append(read)
    ready = LocalTool("ensure_cua_ready")
    runtime = ReplRuntime(node="unused", workspace=tmp_path, cua_service=SimpleNamespace(name="desktop-custom"))
    search = ToolSearchTool(catalog, active)
    tools = {tool.name: tool for tool in [search, CallTool(), ready, *create_tools(runtime)]}
    if not deferred:
        tools.update({t.name: t for t in [*gui, other]})
    search._local_tools_provider = lambda: tools.values()
    if stable:
        manager.enable_stable_prefix(search)
    manager._live_candidates = lambda: tools
    agent = SimpleNamespace(tools=tools, messages=[], mcp_tool_exposure=manager)
    engine = DefaultToolEngine(tools=tools, tool_exposure=manager)
    config = SimpleNamespace(plugins={"repl": {"trusted_local_code": True},
                                      "cua": {"server_name": "desktop-custom"}})
    binding = RunBinding(SimpleNamespace(session=SimpleNamespace(config=config), options=SimpleNamespace(current_turn_text="当前页面")))
    return catalog, manager, search, gui, runtime, agent, engine, binding


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [True, False])
@pytest.mark.parametrize("deferred", [True, False])
async def test_repl_hides_legacy_exact_alias_keyword_server_and_eager_tools(tmp_path, stable, deferred):
    catalog, manager, search, gui, runtime, agent, engine, binding = setup(tmp_path, stable=stable, deferred=deferred)
    gui[0].aliases = ("old_browser_click",)
    binding.install_agent(agent)
    prepared = engine.prepare_tools()
    blocked = {t.name for t in gui} | {"ensure_cua_ready"}
    assert not blocked & {d.name for d in prepared.definitions}
    for query in [dict(tool_names=list(blocked) + ["old_browser_click"]), dict(query="click GUI"),
                  dict(server_name="playwright"), dict(server_name="browser-gateway"),
                  dict(server_name="computer-use"), dict(server_name="desktop-custom")]:
        result = json.loads((await search.execute(**query)).content)
        assert not blocked & {t["name"] for t in result["activated"]}
    result = json.loads((await search.execute(tool_names=["cua_repl_js", "browser_repl_js", "crm_lookup"])).content)
    assert {t["name"] for t in result["activated"]} == {"cua_repl_js", "browser_repl_js", "crm_lookup"}
    assert not blocked & {json.loads(x)["name"] for x in manager.pending_bootstrap_descriptions()}
    # A late MCP reconnect must not restore model visibility.
    replacement = FakeMCPTool("managed_browser_click", "playwright", always_load=True)
    catalog.replace_server("playwright", [replacement])
    assert not json.loads((await search.execute(tool_names=[replacement.name])).content)["activated"]
    assert manager.is_current_target(replacement.name, replacement)
    assert manager.validate_call(replacement.name, replacement.mcp_generation, replacement)
    binding.close()
    assert manager.validate_call(replacement.name, replacement.mcp_generation, replacement)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, -1])
async def test_previous_refs_and_direct_readiness_are_rejected_after_repl_binding(tmp_path, index):
    _, manager, search, gui, _, agent, engine, binding = setup(tmp_path)
    engine.prepare_tools()
    result = await search.execute(tool_names=[gui[index].name])
    ref = json.loads(result.content)["activated"][0]["tool_ref"]
    manager.record_search_result("s", result.content)
    manager.acknowledge_search_results([Message(role="tool", name="tool_search", tool_call_id="s", content=result.content)])
    before = engine.prepare_tools()
    call = ToolCall(id="x", type="function", function=FunctionCall(name="call_tool", arguments={"tool_ref": ref, "arguments_json": "{}"}))
    assert before.resolve_call(call)[2] is None
    assert before.validate_call("ensure_cua_ready") is None
    binding.install_agent(agent)
    assert before.resolve_call(call)[2] is not None  # even an already prepared request
    assert before.validate_call("ensure_cua_ready") is not None
    assert engine.prepare_tools().resolve_call(call)[2] is not None
    assert manager.restore_compacted_descriptions([]) == []
    assert gui[index].calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [True, False])
async def test_hidden_browser_is_callable_internally_with_existing_policy(tmp_path, deferred):
    import threading
    import time
    from box_agent.tools.base import ToolInvocationContext
    _, _, _, gui, runtime, agent, engine, binding = setup(tmp_path, deferred=deferred)
    binding.install_agent(agent)
    engine.prepare_tools()
    realm = Realm(cell={"cancelled": threading.Event(), "context": ToolInvocationContext(), "receipts": []})
    result, _ = await runtime._invoke(realm, gui[0].name, {}, time.monotonic() + 10, None)
    assert result.success and gui[0].calls == 1
    result, _ = await runtime._invoke(realm, gui[1].name, {}, time.monotonic() + 10, None)
    assert result.success and gui[1].calls == 1
    runtime.turn_text = "写一首诗"
    with pytest.raises(ValueError):
        await runtime._invoke(realm, gui[1].name, {}, time.monotonic() + 10, None)
    assert gui[1].calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("gui_index", [0, 1, 2, 3])
async def test_standard_session_rejects_historical_gui_refs_and_hides_backends(tmp_path, gui_index):
    _, manager, search, gui, _, agent, engine, binding = setup(tmp_path)
    binding.run.session.config.plugins.pop("repl")
    for name in list(agent.tools):
        if "_repl_" in name:
            del agent.tools[name]
    engine.prepare_tools()
    old = await search.execute(tool_names=[gui[gui_index].name])
    ref = json.loads(old.content)["activated"][0]["tool_ref"]
    manager.record_search_result("old", old.content)
    manager.acknowledge_search_results([Message(role="tool", name="tool_search", tool_call_id="old", content=old.content)])
    request = ToolCall(id="x", type="function", function=FunctionCall(
        name="call_tool", arguments={"tool_ref": ref, "arguments_json": "{}"}))
    prepared = engine.prepare_tools()
    assert prepared.resolve_call(request)[2] is None
    binding.install_agent(agent)
    assert prepared.resolve_call(request)[2] is not None
    assert engine.prepare_tools().resolve_call(request)[2] is not None
    assert manager.restore_compacted_descriptions([]) == []
    result = await search.execute(tool_names=[t.name for t in gui] + ["browser_repl_js"])
    assert json.loads(result.content)["activated"] == []

    from box_agent.core import run_agent_loop
    from box_agent.events import ToolCallResult
    from tests.test_tool_engine_preparation_integration import ScriptedLLM

    events = [event async for event in run_agent_loop(
        llm=ScriptedLLM(request), tools=agent.tools,
        messages=[Message(role="user", content="继续操作当前页面")],
        tool_exposure_manager=manager, max_steps=2,
    )]
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert len(results) == 1 and not results[0].success
    assert "tool_ref" in results[0].error
    assert gui[gui_index].calls == 0
