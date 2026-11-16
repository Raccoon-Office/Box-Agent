"""Connector summaries aid discovery without broadening tool exposure."""

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
async def test_unchanged_catalog_is_once_per_session_and_refresh_resends_it(stable):
    catalog = MCPToolCatalog()
    first = FakeMCPTool("generate", "seko", connector_id="seko")
    catalog.replace_server("seko", [first])
    search, _, _ = make_search(catalog, stable=stable)
    assert "connector_catalogs" in json.loads((await search.execute(tool_names=["generate"])).content)
    assert "connector_catalogs" not in json.loads((await search.execute(tool_names=["generate"])).content)

    fresh, _, _ = make_search(catalog, stable=stable)
    assert "connector_catalogs" in json.loads((await fresh.execute(tool_names=["generate"])).content)
    catalog.replace_server("seko", [
        FakeMCPTool("generate", "seko", connector_id="seko"),
        FakeMCPTool("status", "seko", "Query status", connector_id="seko"),
    ])
    updated = json.loads((await search.execute(tool_names=["generate"])).content)
    assert updated["connector_catalogs"][0]["tool_count"] == 2
    catalog.replace_server("seko", [FakeMCPTool("generate", "seko", connector_id="seko")])
    removed = json.loads((await search.execute(tool_names=["generate"])).content)
    assert removed["connector_catalogs"][0]["tool_count"] == 1


@pytest.mark.asyncio
async def test_visibility_change_resends_current_catalog():
    catalog = MCPToolCatalog()
    catalog.replace_server("seko", [
        FakeMCPTool(name, "seko", connector_id="seko") for name in ["generate", "status"]
    ])
    search, manager, _ = make_search(catalog)
    hidden = set()
    manager._stable_visibility = lambda name: name not in hidden
    await search.execute(tool_names=["generate"])
    hidden.add("status")
    reduced = json.loads((await search.execute(tool_names=["generate"])).content)
    assert reduced["connector_catalogs"][0]["tool_count"] == 1
    hidden.clear()
    expanded = json.loads((await search.execute(tool_names=["generate"])).content)
    assert expanded["connector_catalogs"][0]["tool_count"] == 2


@pytest.mark.asyncio
async def test_missing_conflicting_or_unowned_hits_do_not_attach_connector_catalogs():
    catalog = MCPToolCatalog()
    catalog.replace_server("one", [
        FakeMCPTool("unowned", "one"),
        FakeMCPTool("duplicate", "one", connector_id="seko"),
    ])
    catalog.replace_server("two", [FakeMCPTool("duplicate", "two", connector_id="seko")])
    search, _, _ = make_search(catalog)
    for name in ["missing", "duplicate", "unowned"]:
        payload = json.loads((await search.execute(tool_names=[name])).content)
        assert "connector_catalogs" not in payload


@pytest.mark.asyncio
async def test_oversized_schema_does_not_trigger_or_suppress_connector_catalog():
    catalog = MCPToolCatalog()
    catalog.replace_server("seko", [
        FakeMCPTool("giant", "seko", connector_id="seko",
                    parameters={"type": "object", "description": "x" * 50_000}),
        FakeMCPTool("small", "seko", connector_id="seko"),
    ])
    search, _, active = make_search(catalog)
    oversized = json.loads((await search.execute(tool_names=["giant"])).content)
    assert oversized["oversized_schema_count"] == 1
    assert oversized["activated"] == []
    assert "connector_catalogs" not in oversized
    assert not active
    delivered = json.loads((await search.execute(tool_names=["small"])).content)
    assert delivered["connector_catalogs"][0]["tool_count"] == 2
    assert list(active) == ["mcp:seko/small"]


@pytest.mark.asyncio
async def test_catalog_budget_is_shared_across_connectors_and_reports_omissions():
    catalog = MCPToolCatalog()
    for connector in ["a", "b", "c"]:
        catalog.replace_server(connector, [
            FakeMCPTool(f"{connector}_{index:03d}", connector, "x" * 160, connector_id=connector)
            for index in range(100)
        ])
    search, _, active = make_search(catalog)
    payload = json.loads((await search.execute(tool_names=["a_000", "b_000", "c_000"])).content)
    groups = payload["connector_catalogs"]
    assert len(json.dumps(groups, ensure_ascii=False)) <= 8_000
    assert sum(len(group["tools"]) for group in groups) < 300
    assert sum(group["omitted_count"] for group in groups) > 0
    assert payload["connector_catalogs_omitted_count"] > 0
    assert len(active) == 3
    # A group omitted for the shared budget is not permanently suppressed.
    later = json.loads((await search.execute(tool_names=["c_000"])).content)
    assert later["connector_catalogs"][0]["connector_id"] == "c"
    # An omitted sibling is still discoverable by its exact name.
    exact = json.loads((await search.execute(tool_names=["a_099"])).content)
    assert exact["activated"][0]["name"] == "a_099"


@pytest.mark.asyncio
async def test_catalog_loss_after_compaction_allows_resend_without_changing_tool_definitions():
    catalog = MCPToolCatalog()
    catalog.replace_server("seko", [
        FakeMCPTool(name, "seko", connector_id="seko") for name in ["generate", "status"]
    ])
    search, manager, _ = make_search(catalog)
    engine = DefaultToolEngine(tools={"tool_search": search, "call_tool": CallTool()}, tool_exposure=manager)
    before = [definition.to_schema() for definition in engine.prepare_tools().definitions]
    result = await search.execute(tool_names=["generate"])
    message = Message(role="tool", name="tool_search", tool_call_id="search-1", content=result.content)
    manager.record_search_result("search-1", result.content)
    manager.acknowledge_search_results([message])
    repeat = json.loads((await search.execute(tool_names=["generate"])).content)
    assert "connector_catalogs" not in repeat

    compacted = manager.restore_compacted_descriptions([Message(role="user", content="Continue")])
    assert all("connector_catalogs" not in (message.content or "") for message in compacted)
    manager.acknowledge_search_results(compacted)
    recovered = json.loads((await search.execute(tool_names=["generate"])).content)
    assert recovered["connector_catalogs"][0]["tool_count"] == 2
    assert recovered["activated"][0]["tool_ref"] == json.loads(result.content)["activated"][0]["tool_ref"]
    assert [definition.to_schema() for definition in engine.prepare_tools().definitions] == before
    assert {tool.name for tool, _, _ in manager._stable_refs.values()} == {"generate"}


@pytest.mark.asyncio
async def test_undelivered_catalog_does_not_suppress_later_search():
    catalog = MCPToolCatalog()
    catalog.replace_server("seko", [FakeMCPTool("generate", "seko", connector_id="seko")])
    search, manager, _ = make_search(catalog)
    result = await search.execute(tool_names=["generate"])
    manager.record_search_result("search-1", result.content)
    payload = json.loads(result.content)
    payload.pop("connector_catalogs")
    manager.acknowledge_search_results([
        Message(role="tool", name="tool_search", tool_call_id="search-1", content=json.dumps(payload)),
    ])
    assert "connector_catalogs" in json.loads((await search.execute(tool_names=["generate"])).content)
