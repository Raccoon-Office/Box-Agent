"""MCP names coexist without changing installed servers or legacy call history."""

import json
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.tools.base import Tool, ToolResult, build_tool_name_index
from box_agent.tools.engine.engine import DefaultToolEngine
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.mcp_loader import (
    MCPTool, _legacy_public_mcp_tool_name, _public_mcp_tool_name,
)
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from box_agent.tools.setup import merge_mcp_tools, register_mcp_tools, sync_mcp_tools
from box_agent.tools.tool_call_tool import CallTool


class Native(Tool):
    name = "generate_image"
    description = "Native image generation"
    parameters = {"type": "object", "properties": {}}
    _model_exposure_direct = True

    async def execute(self):
        return ToolResult(success=True, content="native")


def remote(server, raw="generate_image", *, connector_id=None, always_load=False):
    class Session:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return SimpleNamespace(content=[SimpleNamespace(text=server)], isError=False)

    name = _public_mcp_tool_name(server, raw, connector_id)
    old = _legacy_public_mcp_tool_name(server, raw, connector_id)
    return MCPTool(
        name=name, remote_name=raw, server_name=server, description="Generate an image",
        parameters={"type": "object", "properties": {}}, session=Session(),
        connector_id=connector_id, always_load=always_load,
        legacy_names=(old,) if old != name else (),
    )


def call(name, **arguments):
    return ToolCall(id="invoke", type="function", function=FunctionCall(name=name, arguments=arguments))


@pytest.mark.parametrize("connector_id", [None, "seko"])
def test_installed_mcp_uses_same_namespace_with_or_without_connector_metadata(connector_id):
    assert _public_mcp_tool_name("seko", "generate_image", connector_id) == "mcp__seko__generate_image"


@pytest.mark.parametrize("server,raw,expected", [
    ("playwright", "browser_click", "managed_browser_click"),
    ("browser-gateway", "browser_open_url", "user_browser_open_tab_and_read"),
    ("mcp-server-askecho-search-infinity", "web_search", "web_search"),
    ("box-agent-web-extract", "web_extract", "web_extract"),
])
def test_explicit_managed_public_contracts_are_preserved(server, raw, expected):
    assert _public_mcp_tool_name(server, raw) == expected
    assert _public_mcp_tool_name("third-party", raw).startswith("mcp__third-party__")


@pytest.mark.parametrize("left,right", [
    (("a__b", "c"), ("a", "b__c")),
    (("a_", "b"), ("a", "_b")),
    (("a.b", "c"), ("a/b", "c")),
    (("a" * 100, "b"), ("a" * 99 + "b", "b")),
])
def test_normalized_truncated_and_separator_names_do_not_collide(left, right):
    first, second = _public_mcp_tool_name(*left), _public_mcp_tool_name(*right)
    assert first != second
    assert max(len(first), len(second)) <= 64
    assert first == _public_mcp_tool_name(*left)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("deferred", [False, True])
async def test_native_and_two_same_named_mcp_tools_are_discoverable_and_callable(version, deferred):
    native, seko, other = Native(), remote("seko"), remote("other")
    catalog, active = MCPToolCatalog(), OrderedDict()
    for tool in (seko, other):
        catalog.replace_server(tool.server_name, [tool])
    manager = MCPToolExposureManager(catalog, active, deferred_mcp=deferred)
    search = ToolSearchTool(catalog, active, protected_names_provider=lambda: frozenset({native.name}))
    tools = {native.name: native, search.name: search}
    if not deferred:
        register_mcp_tools(tools, [seko, other])
    if version == 2:
        manager.enable_stable_prefix(search)
        tools["call_tool"] = CallTool()
    engine = DefaultToolEngine(tools=tools, tool_exposure=manager)
    before = engine.prepare_tools()
    result = await search.execute(tool_names=[seko.name, other.name])
    payload = json.loads(result.content)
    assert payload["conflicts"] == []
    assert {item["name"] for item in payload["activated"]} == {seko.name, other.name}
    if version == 2:
        manager.record_search_result("discover", result.content)
        manager.acknowledge_search_results([Message(
            role="tool", name="tool_search", tool_call_id="discover", content=result.content,
        )])
    prepared = engine.prepare_tools()
    assert prepared.targets[native.name] is native
    assert prepared.validate_call(native.name) is None
    assert (await prepared.targets[native.name].invoke({})).content == "native"
    for item in payload["activated"]:
        invocation = (call("call_tool", tool_ref=item["tool_ref"], arguments_json="{}")
                      if version == 2 else call(item["name"]))
        resolved, routed, error = prepared.resolve_call(invocation)
        assert error is None
        assert routed == (version == 2)
        targets = prepared.routed_targets if routed else prepared.targets
        target = targets[resolved.function.name]
        if not routed:
            assert prepared.validate_call(target.name) is None
        assert (await target.invoke(resolved.function.arguments)).content == target.server_name
        assert target._session.calls == [("generate_image", {})]
    if version == 2:
        assert [t.to_schema() for t in before.definitions] == [t.to_schema() for t in prepared.definitions]
        assert prepared.validate_call(seko.name) is not None  # no direct-call bypass


def test_eager_registration_and_disconnect_preserve_native_tools():
    native, seko, other = Native(), remote("seko"), remote("other")
    tools = [native]
    merge_mcp_tools(tools, [seko, other])
    assert tools == [native, seko, other]
    registry, fallbacks = {native.name: native}, {}
    sync_mcp_tools(registry, [seko, other], fallbacks)
    assert set(registry) == {native.name, seko.name, other.name}
    assert fallbacks == {}
    sync_mcp_tools(registry, [], fallbacks)
    assert registry == {native.name: native}


@pytest.mark.parametrize("raw", ["lookup", "law.search", "x" * 100])
def test_old_public_names_and_catalog_ids_resolve_without_changing_history(raw):
    tool = remote("installed", raw)
    catalog = MCPToolCatalog()
    catalog.replace_server(tool.server_name, [tool])
    old = tool.mcp_legacy_names[0]
    for name in (old, raw, f"mcp:installed/{old}", f"installed/{raw}"):
        hits, missing = catalog.lookup_exact([name])
        assert [entry.model_name for entry in hits] == [tool.name]
        assert missing == []
    prepared = prepare_tools([tool])
    original = call(old)
    assert prepared.canonicalize_calls([original])[0].function.name == tool.name
    assert original.function.name == old
    assert prepared.validate_call(old) is None
    assert [definition.name for definition in prepared.definitions] == [tool.name]


async def test_legacy_alias_cannot_activate_or_select_an_ambiguous_or_hidden_tool():
    first, second = remote("first", "lookup"), remote("second", "lookup")
    catalog, active = MCPToolCatalog(), OrderedDict()
    catalog.replace_server("first", [first])
    manager = MCPToolExposureManager(catalog, active)
    search = ToolSearchTool(catalog, active)
    assert prepare_tools([], tool_exposure=manager).validate_call("lookup")
    await search.execute(tool_names=["lookup"])
    assert prepare_tools([], tool_exposure=manager).call_names["lookup"] == first.name
    catalog.replace_server("second", [second])  # unactivated tools still cause ambiguity
    prepared = prepare_tools([], tool_exposure=manager)
    assert "lookup" not in prepared.call_names
    assert prepared.validate_call("lookup")
    assert catalog.lookup_exact(["lookup"]) == ([], ["lookup"])
    assert catalog.lookup_exact(["mcp:first/lookup"])[0][0].tool is first
    assert catalog.lookup_exact(["lookup"], server_name="second")[0][0].tool is second
    assert "lookup" not in prepare_tools([first], is_tool_visible=lambda _: False).call_names


def test_legacy_alias_never_shadows_native_names_or_native_aliases():
    native = Native()
    native.aliases = ("old_native",)
    for raw in (native.name, "old_native"):
        tool = remote("seko", raw)
        prepared = prepare_tools([native, tool])
        assert prepared.call_names[raw] == native.name
        assert prepared.call_names[tool.name] == tool.name
        hidden = prepare_tools([native, tool], is_tool_visible=lambda item: item is tool)
        assert raw not in hidden.call_names


def test_exact_names_disambiguate_case_and_punctuation():
    catalog = MCPToolCatalog()
    tools = [remote("demo", raw) for raw in ("find_item", "find-item", "Find_Item")]
    catalog.replace_server("demo", tools)
    for tool in tools:
        assert catalog.lookup_exact([tool.name])[0][0].tool is tool
        assert catalog.lookup_exact([tool.remote_name])[0][0].tool is tool
    prepared = prepare_tools(tools)
    assert set(prepared.targets) == {tool.name for tool in tools}
    assert "mcp--demo--find-item" not in prepared.call_names
    assert catalog.lookup_exact(["FIND ITEM"]) == ([], ["FIND ITEM"])


def test_server_name_punctuation_does_not_cause_implicit_alias_conflicts():
    first, second = remote("my_server"), remote("my-server")
    prepared = prepare_tools([first, second])
    assert set(prepared.targets) == {first.name, second.name}
    assert "mcp--my-server--generate-image" not in prepared.call_names


@pytest.mark.parametrize("reverse", [False, True])
def test_mcp_implicit_alias_cannot_shadow_native_explicit_alias(reverse):
    native, tool = Native(), remote("seko")
    native.aliases = (tool.name.replace("_", "-"),)
    tools = [native, tool]
    index = build_tool_name_index(reversed(tools) if reverse else tools)
    assert index[native.aliases[0]] is native
    assert index[tool.name] is tool


def test_old_hyphenated_call_name_remains_compatible_when_unique():
    tool = remote("installed", "old_lookup")
    assert prepare_tools([tool]).call_names["old-lookup"] == tool.name


def test_actual_duplicate_canonical_names_are_still_rejected():
    with pytest.raises(ValueError, match="conflicts"):
        build_tool_name_index([remote("seko"), remote("seko")])


async def test_legacy_alias_still_checks_connection_generation_and_connector_access():
    tool = remote("installed__server", "lookup", connector_id="installed")
    old = tool.mcp_legacy_names[0]
    allowed = {"installed"}
    catalog, active = MCPToolCatalog(), OrderedDict()
    catalog.replace_server(tool.server_name, [tool])
    manager = MCPToolExposureManager(catalog, active, allowed_connector_ids_provider=lambda: frozenset(allowed))
    search = ToolSearchTool(catalog, active)
    await search.execute(tool_names=[old])
    prepared = prepare_tools([], tool_exposure=manager)
    assert prepared.validate_call(old) is None
    allowed.clear()
    assert prepared.validate_call(old) is not None
    assert old not in prepare_tools([], tool_exposure=manager).call_names
    allowed.add("installed")
    catalog.replace_server(tool.server_name, [remote(tool.server_name, "lookup", connector_id="installed")])
    assert prepared.validate_call(old) is not None


@pytest.mark.parametrize("version", [1, 2])
async def test_old_session_resumes_with_new_mcp_names_without_rewriting_or_replaying_history(
    tmp_path, monkeypatch, version,
):
    from box_agent import agent as agent_module
    from box_agent.schema import StreamEvent
    from box_agent.session_log import SessionLog

    tool = remote("installed", "lookup")
    catalog = MCPToolCatalog()
    catalog.replace_server("installed", [tool])
    monkeypatch.setattr(agent_module, "get_mcp_tool_catalog", lambda: catalog)
    old_ref = "tool:v1:previous-process"
    old_call = (call("call_tool", tool_ref=old_ref, arguments_json="{}")
                if version == 2 else call("lookup"))
    old_call.id = "historical"
    history = [Message(role="user", content="previous task"),
               Message(role="assistant", content="", tool_calls=[old_call]),
               Message(role="tool", name=old_call.function.name, tool_call_id=old_call.id, content="old result")]
    root = tmp_path / "logs"
    log = SessionLog.create(root, session_id="upgrade", cwd=tmp_path)
    log.bind_tool_interface_version(version)
    log.append_unlogged_messages(history, turn=1, step=1)
    path = log.path
    log.close()
    original = path.read_bytes()

    class Model:
        model = "offline-upgrade"
        max_output_tokens = 512

        def __init__(self):
            self.requests = []

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append([m.model_copy(deep=True) for m in messages])
            step = len(self.requests)
            if version == 2 and step == 1:
                invocation = call("call_tool", tool_ref=old_ref, arguments_json="{}")
                invocation.id = "expired"
            elif step == (2 if version == 2 else 1):
                assert tool._session.calls == []
                if version == 2:
                    assert any(m.tool_call_id == "expired" and "expired" in str(m.content) for m in messages)
                invocation = call("tool_search", tool_names=["mcp:installed/lookup"])
                invocation.id = "discover"
            elif step == (3 if version == 2 else 2):
                result = next(m for m in messages if m.tool_call_id == "discover")
                item = json.loads(result.content)["activated"][0]
                assert item["name"] == tool.name
                invocation = (call("call_tool", tool_ref=item["tool_ref"], arguments_json="{}")
                              if version == 2 else call("lookup"))
                invocation.id = "fresh"
            else:
                yield StreamEvent(type="text", delta="complete")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[invocation])

    log = SessionLog.open(root, session_id="upgrade", cwd=tmp_path)
    model = Model()
    try:
        agent = agent_module.Agent(
            model, "system", [], session_log=log, workspace_dir=str(tmp_path),
            tool_interface_version=3 - version, max_steps=6,
        )
        assert agent.tool_interface_version == version
        assert tool._session.calls == []
        assert path.read_bytes() == original
        agent.add_user_message("continue")
        _ = [event async for event in agent.run_events()]
        assert tool._session.calls == [("lookup", {})]
        assert len(model.requests) == (4 if version == 2 else 3)
        notice = next(m for m in model.requests[0] if m.name == "tool_context")
        assert notice.source == "runtime" and "server-qualified" in notice.content
        replayed = log.replay().messages
        historical = next(m for m in replayed if m.tool_calls and m.tool_calls[0].id == "historical")
        assert historical.tool_calls[0] == old_call
        assert path.read_bytes().startswith(original)
    finally:
        log.close()
