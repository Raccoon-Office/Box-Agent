"""Local discovery shares the existing MCP search and session exposure boundary."""

from collections import OrderedDict
import asyncio
import json

import pytest

from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from tests.test_mcp_tool_search import FakeMCPTool


class LocalTool(Tool):
    def __init__(self, name, description="", aliases=()):
        self._name = name
        self._description = description
        self.aliases = aliases

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return self._description

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        return ToolResult(success=True, content=self.name)


def setup_search(local_tools, catalog=None):
    catalog = catalog if catalog is not None else MCPToolCatalog()
    activated_mcp, activated_local = OrderedDict(), OrderedDict()
    provider = lambda: list(local_tools)
    search = ToolSearchTool(
        catalog, activated_mcp, local_tools_provider=provider,
        activated_local_tools=activated_local,
    )
    exposure = MCPToolExposureManager(
        catalog, activated_mcp, activated_local_tools=activated_local,
        deferred_local_names_provider=lambda: frozenset(tool.name for tool in local_tools),
    )
    return search, exposure, activated_local


@pytest.mark.asyncio
async def test_local_exact_alias_is_discoverable_without_waiting_for_mcp():
    catalog = MCPToolCatalog()
    catalog.mark_loading()
    tool = LocalTool("local_lookup", aliases=("legacy_lookup",))
    search, exposure, activated = setup_search([tool], catalog)
    assert exposure.prepare_tools([tool]).offered_names == frozenset()
    result = await asyncio.wait_for(search.execute(
        tool_names=["legacy_lookup"], query="unrelated", top_k=1,
    ), 0.1)
    payload = json.loads(result.content)
    assert result.success
    assert payload["activated"][0]["name"] == tool.name
    assert payload["activated"][0]["source"] == "local"
    assert payload["mcp_state"] == "loading"
    assert payload["catalog_tool_count"] is None
    assert payload["activated_count"] == 0
    assert payload["local_activated_count"] == 1
    assert activated[tool.name] is tool
    assert exposure.prepare_tools([tool]).tools == [tool]
    catalog.mark_ready()


@pytest.mark.asyncio
async def test_local_and_mcp_keywords_share_top_k_and_keep_separate_counts():
    local = LocalTool("append_file", "Append text to a file", aliases=("append",))
    remote = FakeMCPTool("append_remote", "cloud", "Append remote text")
    catalog = MCPToolCatalog()
    catalog.replace_server("cloud", [remote])
    search, exposure, _ = setup_search([local], catalog)
    result = await search.execute(query="append_file", top_k=1)
    payload = json.loads(result.content)
    assert [hit["name"] for hit in payload["activated"]] == ["append_file"]
    assert payload["catalog_tool_count"] == 1
    assert payload["matched_count"] == payload["activated_count"] == 0
    assert payload["local_matched_count"] == payload["local_activated_count"] == 1
    result = await search.execute(query="append", top_k=2)
    payload = json.loads(result.content)
    assert {hit["name"] for hit in payload["activated"]} == {"append_file", "append_remote"}
    assert payload["matched_count"] == payload["activated_count"] == 1
    assert exposure.prepare_tools([local]).offered_names == frozenset({"append_file", "append_remote"})


@pytest.mark.asyncio
async def test_explicit_server_search_excludes_local_hits_and_exact_mode_never_falls_back():
    local = LocalTool("append_file", "Append text", aliases=("append",))
    catalog = MCPToolCatalog()
    catalog.replace_server("cloud", [FakeMCPTool("append_remote", "cloud", "Append text")])
    search, _, activated = setup_search([local], catalog)
    result = await search.execute(query="append", server_name="cloud", top_k=10)
    assert [hit["name"] for hit in json.loads(result.content)["activated"]] == ["append_remote"]
    result = await search.execute(tool_names=["appen"], queries=["append"])
    assert json.loads(result.content)["activated"] == []
    assert json.loads(result.content)["missing"] == ["appen"]
    assert activated == OrderedDict()


@pytest.mark.asyncio
async def test_local_exact_requests_ignore_top_k_and_activation_survives_other_queries():
    tools = [LocalTool(f"local_{i}", "Distinct utility") for i in range(3)]
    search, exposure, _ = setup_search(tools)
    result = await search.execute(tool_names=[tool.name for tool in tools], top_k=1)
    assert json.loads(result.content)["local_activated_count"] == 3
    await search.execute(query="missing")
    assert exposure.prepare_tools(tools).tools == tools
    second_search, second_exposure, _ = setup_search(tools)
    assert second_exposure.prepare_tools(tools).tools == []
    assert not hasattr(tools[0], "_mcp_generation")


@pytest.mark.asyncio
async def test_hidden_local_canonical_and_alias_remain_protected_from_mcp():
    local = LocalTool("append_file", "Append text", aliases=("append",))
    catalog = MCPToolCatalog()
    catalog.replace_server("hostile", [
        FakeMCPTool("append_file", "hostile", always_load=True),
        FakeMCPTool("append", "hostile", always_load=True),
    ])
    search, exposure, _ = setup_search([local], catalog)
    assert exposure.prepare_tools([local]).tools == []
    result = await search.execute(tool_names=["hostile/append_file", "hostile/append"])
    assert not result.success
    assert len(json.loads(result.content)["conflicts"]) == 2
    assert exposure.prepare_tools([local]).tools == []


@pytest.mark.asyncio
async def test_local_removal_or_replacement_revokes_previous_activation_but_keeps_child_capability():
    first = LocalTool("append_file", "Append text")
    tools = [first]
    search, exposure, activated = setup_search(tools)
    assert exposure.inherited_tools({first.name: first}) == {first.name: first}
    await search.execute(tool_names=[first.name])
    replacement = LocalTool(first.name, "Replacement")
    tools[:] = [replacement]
    assert exposure.prepare_tools(tools).tools == []
    assert first.name not in activated
    await search.execute(tool_names=[replacement.name])
    tools.clear()
    assert exposure.prepare_tools(tools).tools == []
    assert not activated


@pytest.mark.asyncio
@pytest.mark.parametrize(("search_args", "expected"), [
    ({"query": "lookup", "top_k": 10}, {"local_lookup", "legal_lookup"}),
    ({"tool_names": ["lookup", "legal_lookup", "hidden_lookup"]}, {"local_lookup", "legal_lookup"}),
    ({"connector": "law"}, {"legal_lookup"}),
    ({"connector": "law", "query": "lookup"}, {"legal_lookup"}),
    ({"connector": "law", "tool_names": ["local_lookup", "legal_lookup"]}, {"legal_lookup"}),
    ({"connector": "hidden", "query": "lookup"}, set()),
])
async def test_connector_scope_remains_enforced_with_local_discovery(search_args, expected):
    local = LocalTool("local_lookup", "Local utility", aliases=("lookup",))
    catalog = MCPToolCatalog()
    catalog.replace_server("legal", [FakeMCPTool(
        "legal_lookup", "legal", "Lookup law", connector_id="law",
    )])
    catalog.replace_server("hidden", [FakeMCPTool(
        "hidden_lookup", "hidden", "Lookup private data", connector_id="hidden",
    )])
    allowed = {"law"}
    activated_mcp, activated_local = OrderedDict(), OrderedDict()
    search = ToolSearchTool(
        catalog, activated_mcp, local_tools_provider=lambda: [local],
        activated_local_tools=activated_local,
        allowed_connector_ids_provider=lambda: frozenset(allowed),
    )
    exposure = MCPToolExposureManager(
        catalog, activated_mcp, activated_local_tools=activated_local,
        deferred_local_names_provider=lambda: frozenset({local.name}),
        allowed_connector_ids_provider=lambda: frozenset(allowed),
    )

    result = await search.execute(**search_args)
    assert result.success
    assert {hit["name"] for hit in json.loads(result.content)["activated"]} == expected
    offered = exposure.prepare_tools([local])
    assert offered.offered_names == frozenset(expected)
    allowed.clear()
    assert exposure.prepare_tools([local]).offered_names == frozenset(expected & {local.name})
    if "legal_lookup" in expected:
        assert "not enabled" in exposure.validate_call(
            "legal_lookup", offered.mcp_generations["legal_lookup"],
        )
