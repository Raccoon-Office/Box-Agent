"""Connector and MCP server summaries aid discovery without broadening exposure."""

import json
from collections import OrderedDict

import pytest

from box_agent.schema import Message
from box_agent.tools.engine.engine import DefaultToolEngine
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from box_agent.tools.tool_call_tool import CallTool
from tests.test_mcp_tool_search import FakeMCPTool


def make_search(catalog, *, stable=True, allowed=None, protected=frozenset()):
    active = OrderedDict()
    search = ToolSearchTool(
        catalog, active, allowed_connector_ids_provider=allowed,
        protected_names_provider=lambda: protected,
    )
    manager = MCPToolExposureManager(catalog, active, allowed_connector_ids_provider=allowed)
    if stable:
        manager.enable_stable_prefix(search)
    return search, manager, active


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [False, True])
async def test_hit_lists_same_connector_across_servers_without_activating_siblings(stable):
    catalog = MCPToolCatalog()
    generate = FakeMCPTool("seko_generate", "images", "Generate image", connector_id="seko")
    confirm = FakeMCPTool("seko_confirm", "jobs", "  Confirm\n generation  ", connector_id="seko")
    status = FakeMCPTool("seko_status", "jobs", "查" * 200, connector_id="seko")
    catalog.replace_server("images", [generate])
    catalog.replace_server("jobs", [confirm, status])
    catalog.replace_server("notes", [FakeMCPTool("notes_list", "notes", connector_id="notes")])
    search, manager, active = make_search(catalog, stable=stable)

    result = await search.execute(query=generate.name, server_name="images", top_k=1)
    payload = json.loads(result.content)
    group, = payload["connector_catalogs"]
    assert result.success
    assert group["connector_id"] == "seko"
    assert group["tool_count"] == 3
    assert group["omitted_count"] == 0
    assert group["tools"] == [
        {"tool_name": "seko_generate", "purpose": "Generate image"},
        {"tool_name": "seko_confirm", "purpose": "Confirm generation"},
        {"tool_name": "seko_status", "purpose": "查" * 160},
    ]
    assert payload["catalog_tool_count"] == 1  # Search scope is still server-local.
    assert payload["activated_count"] == 1
    assert list(active) == [generate.mcp_tool_id]
    assert "notes_list" not in result.content
    if stable:
        assert payload["activated"][0]["input_schema"] == generate.parameters
        assert {tool.name for tool, _, _ in manager._stable_refs.values()} == {generate.name}
    else:
        assert [tool.name for tool in manager.prepare_tools([generate, confirm, status]).tools] == [generate.name]


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [False, True])
async def test_user_mcp_hit_lists_only_same_server_without_activating_siblings(stable):
    catalog = MCPToolCatalog()
    tools = [
        FakeMCPTool("mcp__seko__generate_image", "seko", "Generate image"),
        FakeMCPTool("mcp__seko__create_canvas", "seko", "  Create\n canvas  "),
        FakeMCPTool("mcp__seko__get_task", "seko", "Query generation"),
    ]
    catalog.replace_server("seko", tools)
    catalog.replace_server("other", [FakeMCPTool("other_status", "other")])
    search, manager, active = make_search(catalog, stable=stable)

    result = await search.execute(tool_names=[tools[0].name], server_name="seko")
    payload = json.loads(result.content)
    assert result.success
    assert "connector_catalogs" not in payload
    assert payload["server_catalogs"] == [{
        "server_name": "seko", "tool_count": 3, "omitted_count": 0,
        "tools": [
            {"tool_name": "mcp__seko__create_canvas", "purpose": "Create canvas"},
            {"tool_name": "mcp__seko__generate_image", "purpose": "Generate image"},
            {"tool_name": "mcp__seko__get_task", "purpose": "Query generation"},
        ],
    }]
    assert payload["server_catalogs_omitted_count"] == 0
    assert payload["activated_count"] == 1
    assert list(active) == [tools[0].mcp_tool_id]
    assert "other_status" not in result.content
    assert all(tool.calls == 0 for tool in tools)
    if stable:
        assert {tool.name for tool, _, _ in manager._stable_refs.values()} == {tools[0].name}
    else:
        assert manager.prepare_tools(tools).tools == [tools[0]]


@pytest.mark.asyncio
async def test_server_and_connector_with_same_identity_keep_separate_catalogs():
    catalog = MCPToolCatalog()
    catalog.replace_server("seko", [FakeMCPTool("user_generate", "seko")])
    catalog.replace_server("managed", [
        FakeMCPTool("connector_generate", "managed", connector_id="seko"),
        FakeMCPTool("connector_status", "managed", connector_id="seko"),
    ])
    search, _, _ = make_search(catalog)
    payload = json.loads((await search.execute(
        tool_names=["user_generate", "connector_generate"],
    )).content)
    assert payload["server_catalogs"][0]["tools"] == [
        {"tool_name": "user_generate", "purpose": ""},
    ]
    assert payload["connector_catalogs"][0]["tools"] == [
        {"tool_name": "connector_generate", "purpose": ""},
        {"tool_name": "connector_status", "purpose": ""},
    ]
    repeat = json.loads((await search.execute(
        tool_names=["user_generate", "connector_generate"],
    )).content)
    assert "server_catalogs" not in repeat
    assert "connector_catalogs" not in repeat
    catalog.replace_server("seko", [
        FakeMCPTool("user_generate", "seko"), FakeMCPTool("user_status", "seko"),
    ])
    refreshed = json.loads((await search.execute(
        tool_names=["user_generate", "connector_generate"],
    )).content)
    assert refreshed["server_catalogs"][0]["tool_count"] == 2
    assert "connector_catalogs" not in refreshed


@pytest.mark.asyncio
@pytest.mark.parametrize("connector_id", [None, "seko"])
async def test_catalog_excludes_app_only_hidden_conflicting_and_protected_tools(connector_id):
    catalog = MCPToolCatalog()
    tools = [FakeMCPTool(name, "seko", connector_id=connector_id) for name in [
        "generate", "app_only", "hidden", "duplicate", "bash", "tool_search", "call_tool",
    ]]
    tools[1].mcp_visibility = frozenset({"app"})
    catalog.replace_server("seko", tools)
    catalog.replace_server("other", [FakeMCPTool("duplicate", "other")])
    search, manager, _ = make_search(catalog, protected=frozenset({"bash"}))
    manager._stable_visibility = lambda name: name != "hidden"

    payload = json.loads((await search.execute(tool_names=["generate"])).content)
    catalog_field = "connector_catalogs" if connector_id else "server_catalogs"
    assert payload[catalog_field][0]["tools"] == [{"tool_name": "generate", "purpose": ""}]
    assert payload[catalog_field][0]["tool_count"] == 1


@pytest.mark.asyncio
async def test_connector_and_server_catalogs_share_budget_without_losing_omitted_group():
    catalog = MCPToolCatalog()
    for server, connector in [("managed", "connector"), ("custom", None)]:
        catalog.replace_server(server, [
            FakeMCPTool(f"{server}_{index:03d}", server, "x" * 160, connector_id=connector)
            for index in range(100)
        ])
    search, _, active = make_search(catalog)
    payload = json.loads((await search.execute(
        tool_names=["managed_000", "custom_000"],
    )).content)
    groups = payload["connector_catalogs"] + payload["server_catalogs"]
    assert len(json.dumps(groups, ensure_ascii=False)) <= 8_000
    assert payload["connector_catalogs"][0]["connector_id"] == "connector"
    assert payload["server_catalogs"] == []
    assert payload["server_catalogs_omitted_count"] == 1
    assert len(active) == 2
    later = json.loads((await search.execute(tool_names=["custom_000"])).content)
    assert later["server_catalogs"][0]["server_name"] == "custom"
    assert later["server_catalogs"][0]["omitted_count"] > 0


@pytest.mark.asyncio
async def test_catalog_excludes_unauthorized_hidden_conflicting_and_protected_tools():
    catalog = MCPToolCatalog()
    names = ["generate", "hidden", "duplicate", "bash", "tool_search", "call_tool"]
    catalog.replace_server("seko", [
        FakeMCPTool(name, "seko", connector_id="seko") for name in names
    ])
    catalog.replace_server("private", [
        FakeMCPTool(name, "private", connector_id="private")
        for name in ["duplicate", "secret"]
    ])
    allowed = {"seko"}
    search, manager, _ = make_search(
        catalog, allowed=lambda: frozenset(allowed), protected=frozenset({"bash"}),
    )
    manager._stable_visibility = lambda name: name != "hidden"

    payload = json.loads((await search.execute(tool_names=["generate", "secret"])).content)
    assert payload["connector_catalogs"][0]["tools"] == [{"tool_name": "generate", "purpose": ""}]
    assert payload["connector_catalogs"][0]["tool_count"] == 1
    allowed.clear()
    denied = json.loads((await search.execute(tool_names=["generate"])).content)
    assert denied["activated"] == []
    assert "connector_catalogs" not in denied


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [False, True])
@pytest.mark.parametrize("with_connector", [False, True])
async def test_unchanged_catalog_is_once_per_session_and_refresh_resends_it(stable, with_connector):
    catalog_field = "connector_catalogs" if with_connector else "server_catalogs"
    catalog = MCPToolCatalog()
    first = FakeMCPTool("generate", "seko", connector_id="seko" if with_connector else None)
    catalog.replace_server("seko", [first])
    search, _, _ = make_search(catalog, stable=stable)
    assert catalog_field in json.loads((await search.execute(tool_names=["generate"])).content)
    assert catalog_field not in json.loads((await search.execute(tool_names=["generate"])).content)

    fresh, _, _ = make_search(catalog, stable=stable)
    assert catalog_field in json.loads((await fresh.execute(tool_names=["generate"])).content)
    catalog.replace_server("seko", [
        FakeMCPTool("generate", "seko", connector_id="seko" if with_connector else None),
        FakeMCPTool("status", "seko", "Query status", connector_id="seko" if with_connector else None),
    ])
    updated = json.loads((await search.execute(tool_names=["generate"])).content)
    assert updated[catalog_field][0]["tool_count"] == 2
    catalog.replace_server("seko", [FakeMCPTool("generate", "seko", connector_id="seko" if with_connector else None)])
    removed = json.loads((await search.execute(tool_names=["generate"])).content)
    assert removed[catalog_field][0]["tool_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_connector", [False, True])
async def test_visibility_change_resends_current_catalog(with_connector):
    catalog_field = "connector_catalogs" if with_connector else "server_catalogs"
    catalog = MCPToolCatalog()
    catalog.replace_server("seko", [
        FakeMCPTool(name, "seko", connector_id="seko" if with_connector else None) for name in ["generate", "status"]
    ])
    search, manager, _ = make_search(catalog)
    hidden = set()
    manager._stable_visibility = lambda name: name not in hidden
    await search.execute(tool_names=["generate"])
    hidden.add("status")
    reduced = json.loads((await search.execute(tool_names=["generate"])).content)
    assert reduced[catalog_field][0]["tool_count"] == 1
    hidden.clear()
    expanded = json.loads((await search.execute(tool_names=["generate"])).content)
    assert expanded[catalog_field][0]["tool_count"] == 2


@pytest.mark.asyncio
async def test_missing_or_conflicting_hits_do_not_attach_catalogs():
    catalog = MCPToolCatalog()
    catalog.replace_server("one", [
        FakeMCPTool("duplicate", "one", connector_id="seko"),
    ])
    catalog.replace_server("two", [FakeMCPTool("duplicate", "two", connector_id="seko")])
    search, _, _ = make_search(catalog)
    for name in ["missing", "duplicate"]:
        payload = json.loads((await search.execute(tool_names=[name])).content)
        assert "connector_catalogs" not in payload
        assert "server_catalogs" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("with_connector", [False, True])
async def test_oversized_schema_does_not_trigger_or_suppress_catalog(with_connector):
    catalog_field = "connector_catalogs" if with_connector else "server_catalogs"
    catalog = MCPToolCatalog()
    catalog.replace_server("seko", [
        FakeMCPTool("giant", "seko", connector_id="seko" if with_connector else None,
                    parameters={"type": "object", "description": "x" * 50_000}),
        FakeMCPTool("small", "seko", connector_id="seko" if with_connector else None),
    ])
    search, _, active = make_search(catalog)
    oversized = json.loads((await search.execute(tool_names=["giant"])).content)
    assert oversized["oversized_schema_count"] == 1
    assert oversized["activated"] == []
    assert catalog_field not in oversized
    assert not active
    delivered = json.loads((await search.execute(tool_names=["small"])).content)
    assert delivered[catalog_field][0]["tool_count"] == 2
    assert list(active) == ["mcp:seko/small"]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_connector", [False, True])
async def test_catalog_budget_is_shared_across_groups_and_reports_omissions(with_connector):
    catalog_field = "connector_catalogs" if with_connector else "server_catalogs"
    catalog = MCPToolCatalog()
    for connector in ["a", "b", "c"]:
        catalog.replace_server(connector, [
            FakeMCPTool(f"{connector}_{index:03d}", connector, "x" * 160, connector_id=connector if with_connector else None)
            for index in range(100)
        ])
    search, _, active = make_search(catalog)
    payload = json.loads((await search.execute(tool_names=["a_000", "b_000", "c_000"])).content)
    groups = payload[catalog_field]
    assert len(json.dumps(groups, ensure_ascii=False)) <= 8_000
    assert sum(len(group["tools"]) for group in groups) < 300
    assert sum(group["omitted_count"] for group in groups) > 0
    assert payload[f"{catalog_field}_omitted_count"] > 0
    assert len(active) == 3
    # A group omitted for the shared budget is not permanently suppressed.
    later = json.loads((await search.execute(tool_names=["c_000"])).content)
    assert later[catalog_field][0]["connector_id" if with_connector else "server_name"] == "c"
    # An omitted sibling is still discoverable by its exact name.
    exact = json.loads((await search.execute(tool_names=["a_099"])).content)
    assert exact["activated"][0]["name"] == "a_099"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_connector", [False, True])
async def test_catalog_loss_after_compaction_allows_resend_without_changing_tool_definitions(with_connector):
    catalog_field = "connector_catalogs" if with_connector else "server_catalogs"
    catalog = MCPToolCatalog()
    catalog.replace_server("seko", [
        FakeMCPTool(name, "seko", connector_id="seko" if with_connector else None) for name in ["generate", "status"]
    ])
    search, manager, _ = make_search(catalog)
    engine = DefaultToolEngine(tools={"tool_search": search, "call_tool": CallTool()}, tool_exposure=manager)
    before = [definition.to_schema() for definition in engine.prepare_tools().definitions]
    result = await search.execute(tool_names=["generate"])
    message = Message(role="tool", name="tool_search", tool_call_id="search-1", content=result.content)
    manager.record_search_result("search-1", result.content)
    manager.acknowledge_search_results([message])
    repeat = json.loads((await search.execute(tool_names=["generate"])).content)
    assert catalog_field not in repeat

    compacted = manager.restore_compacted_descriptions([Message(role="user", content="Continue")])
    assert all(catalog_field not in (message.content or "") for message in compacted)
    manager.acknowledge_search_results(compacted)
    recovered = json.loads((await search.execute(tool_names=["generate"])).content)
    assert recovered[catalog_field][0]["tool_count"] == 2
    assert recovered["activated"][0]["tool_ref"] == json.loads(result.content)["activated"][0]["tool_ref"]
    assert [definition.to_schema() for definition in engine.prepare_tools().definitions] == before
    assert {tool.name for tool, _, _ in manager._stable_refs.values()} == {"generate"}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_connector", [False, True])
async def test_undelivered_catalog_does_not_suppress_later_search(with_connector):
    catalog_field = "connector_catalogs" if with_connector else "server_catalogs"
    catalog = MCPToolCatalog()
    catalog.replace_server("seko", [FakeMCPTool("generate", "seko", connector_id="seko" if with_connector else None)])
    search, manager, _ = make_search(catalog)
    result = await search.execute(tool_names=["generate"])
    manager.record_search_result("search-1", result.content)
    payload = json.loads(result.content)
    payload.pop(catalog_field)
    manager.acknowledge_search_results([
        Message(role="tool", name="tool_search", tool_call_id="search-1", content=json.dumps(payload)),
    ])
    assert catalog_field in json.loads((await search.execute(tool_names=["generate"])).content)
