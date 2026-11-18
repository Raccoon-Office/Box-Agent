"""Host catalog discovery stays separate from execution and model context."""

import json
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.acp import _connector_skill_is_available
from box_agent.schema import Message
from box_agent.session_log import SessionLog
from box_agent.tools.connector_discovery import (
    ConnectorDiscovery, PROMPT, RESULT_BUDGET, bind_discovery_mode, parse_snapshot,
)
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from tests.test_mcp_tool_search import FakeMCPTool


def entry(connector_id="tmeet", **changes):
    return {"id": connector_id, "name": "腾讯会议", "description": "管理会议、录制和纪要。",
            "kind": "cli", "auth_mode": "oauth", "skills": ["tmeet-skill"],
            "configured": False, "enabled": False, "status": "disconnected",
            "execution_ready": False, **changes}


def snapshot(entries=None, *, revision=1, available=True):
    return {"schema_version": 1, "catalog_revision": "catalog-v1", "state_revision": revision,
            "available": available, "entries": [entry()] if entries is None else entries}


def search_fixture(entries=None, *, selected=None, live=None, catalog=None):
    selected = set() if selected is None else selected
    live = set() if live is None else live
    discovery = ConnectorDiscovery(lambda: selected)
    discovery.update(snapshot(entries))
    allowed = lambda: frozenset(connector_id for connector_id in live if discovery.permitted(connector_id))
    active = OrderedDict()
    catalog = catalog or MCPToolCatalog()
    search = ToolSearchTool(catalog, active, allowed_connector_ids_provider=allowed,
                            connector_discovery=discovery, readiness_timeout=0)
    manager = MCPToolExposureManager(catalog, active, allowed_connector_ids_provider=allowed)
    manager.enable_stable_prefix(search)
    return discovery, search, manager, active


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 2}, {"state_revision": True},
    {"state_revision": -1}, {"state_revision": 2**53}, {"available": "yes"},
    {"entries": [entry()] * 257}, {"entries": [entry(), entry()]},
    {"entries": [entry(description="x" * 1025)]}, {"entries": [entry(name="bad\nname")]},
    {"entries": [entry(enabled=1)]}, {"entries": [entry(kind="custom")]},
    {"entries": [entry(skills=["x"] * 17)]}, {"entries": [entry(keywords=["x" * 65])]},
    {"entries": [entry(token="secret")]}, {"catalog_revision": "x" * 129},
])
def test_discovery_rejects_unversioned_unbounded_or_secret_fields(change):
    with pytest.raises(ValueError):
        parse_snapshot({**snapshot(), **change})


def test_discovery_snapshot_is_detached_and_revision_failures_invalidate_readiness():
    selected = {"tmeet"}
    value = snapshot([entry(configured=True, enabled=True, status="connected", execution_ready=True)])
    discovery = ConnectorDiscovery(lambda: selected)
    discovery.update(value)
    value["entries"][0]["name"] = "changed outside"
    assert discovery.entries["tmeet"]["name"] == "腾讯会议"
    assert discovery.permitted("tmeet")
    for bad in [snapshot(revision=0), snapshot(revision=1), {"schema_version": 1}]:
        with pytest.raises(ValueError):
            discovery.update(bad)
        assert not discovery.permitted("tmeet")
    discovery.update({**snapshot([entry(configured=True, enabled=True, status="connected",
                                       execution_ready=True)]), "state_revision": 2})
    assert discovery.permitted("tmeet")
    discovery.update(None)
    assert discovery.action(discovery.entries["tmeet"], frozenset()) == "recheck"
    assert not discovery.permitted("tmeet")


@pytest.mark.parametrize("changes,selected,live,expected", [
    ({"status": "unknown"}, True, True, "recheck"),
    ({"reason_code": "unsupported"}, True, True, "recheck"),
    *[({"status": state}, True, True, "wait") for state in
      ["configuring", "authorizing", "connecting", "cancelling", "disconnecting"]],
    ({"status": "unauthorized", "enabled": False}, True, True, "reauthorize"),
    ({"configured": False, "enabled": False}, True, True, "connect"),
    ({"enabled": False, "status": "error"}, True, True, "enable"),
    ({"status": "disconnected"}, True, True, "retry"),
    ({"status": "error"}, True, True, "retry"),
    ({"execution_ready": False}, True, True, "retry"),
    ({"execution_ready": False, "reason_code": "EXECUTION_UNAVAILABLE"}, True, True, "retry"),
    ({}, False, True, "select"),
    ({}, True, False, "retry"),
    ({}, True, True, None),
    ({"kind": "cli"}, True, False, None),
])
def test_discovery_actions_keep_state_dimensions_and_precedence(changes, selected, live, expected):
    item = entry(kind="mcp", configured=True, enabled=True, status="connected", execution_ready=True)
    item.update(changes)
    discovery, _, _, _ = search_fixture([item], selected={"tmeet"} if selected else set())
    assert discovery.action(item, frozenset({"tmeet"}) if live else frozenset()) == expected


@pytest.mark.asyncio
async def test_unconnected_discovery_returns_candidates_without_activating_or_granting_tools():
    catalog = MCPToolCatalog()
    tool = FakeMCPTool("read_minutes", "tmeet-server", "读取会议纪要", connector_id="tmeet")
    catalog.replace_server("tmeet-server", [tool])
    discovery, search, manager, active = search_fixture(catalog=catalog)
    result = await search.execute(query="会议纪要")
    payload = json.loads(result.content)
    assert result.success and result.raw_output is None
    assert payload["connector_candidates"][0]["action"] == "connect"
    assert payload["catalog_tool_count"] == payload["matched_count"] == payload["activated_count"] == 0
    assert payload["activated"] == [] and not active and not manager._stable_refs
    assert manager.validate_call(tool.name, None, tool) is not None
    explicit = await search.execute(connector="tmeet")
    assert explicit.success
    assert explicit.raw_output == {"type": "connector_action_required", "version": 1,
                                   "connector_id": "tmeet", "action": "connect", "state_revision": 1}
    assert not discovery.permitted("tmeet")
    assert "tool_ref" not in explicit.content and "input_schema" not in explicit.content


@pytest.mark.asyncio
async def test_exact_tool_server_and_ambiguous_scopes_never_request_connection():
    _, search, _, _ = search_fixture([entry(), entry("other", name="腾讯会议国际版")])
    for args in [{"connector": "腾讯"}, {"connector": "missing"},
                 {"query": "会议", "server_name": "missing"}, {"tool_names": ["tmeet"]}]:
        result = await search.execute(**args)
        assert result.raw_output is None
        assert "connector_candidates" not in json.loads(result.content)
    mixed = await search.execute(connector="tmeet", query="会议")
    assert mixed.raw_output is None
    assert json.loads(mixed.content)["connector_candidates"][0]["id"] == "tmeet"


@pytest.mark.asyncio
async def test_candidate_discovery_does_not_wait_for_mcp_loading():
    catalog = MCPToolCatalog()
    catalog.mark_loading()
    _, search, _, _ = search_fixture(catalog=catalog)
    result = await search.execute(connector="tmeet")
    assert result.success and result.raw_output["action"] == "connect"
    assert json.loads(result.content)["catalog_tool_count"] is None


@pytest.mark.asyncio
async def test_connected_tool_counts_top_k_and_revocation_keep_existing_contract():
    catalog = MCPToolCatalog()
    tools = [FakeMCPTool(name, "server", "会议", connector_id="tmeet") for name in ("read", "list")]
    catalog.replace_server("server", tools)
    item = entry(kind="mcp", configured=True, enabled=True, status="connected", execution_ready=True)
    discovery, search, manager, active = search_fixture([item], selected={"tmeet"}, live={"tmeet"}, catalog=catalog)
    result = await search.execute(query="会议", top_k=1)
    payload = json.loads(result.content)
    assert result.raw_output is None
    assert payload["catalog_tool_count"] == 2
    assert payload["matched_count"] == payload["activated_count"] == 1
    assert "connector_candidates" not in payload
    assert len(active) == 1
    discovery.update(snapshot([{**item, "enabled": False}], revision=2))
    assert manager.validate_call(tools[0].name, None, tools[0]) is not None
    assert (await search.execute(connector="tmeet")).raw_output["action"] == "enable"


@pytest.mark.asyncio
async def test_only_currently_visible_committed_candidate_descriptions_are_deduplicated():
    discovery, search, manager, _ = search_fixture()
    first = await search.execute(query="会议")
    assert "purpose" in json.loads((await search.execute(query="会议")).content)["connector_candidates"][0]
    manager.record_search_result("first", first.content)
    message = Message(role="tool", name="tool_search", tool_call_id="first", content=first.content)
    manager.acknowledge_search_results([message])
    assert "purpose" not in json.loads((await search.execute(query="会议")).content)["connector_candidates"][0]
    discovery.update(snapshot([entry(status="authorizing")], revision=2))
    candidate = json.loads((await search.execute(query="会议")).content)["connector_candidates"][0]
    assert candidate["action"] == "wait" and candidate["state_revision"] == 2
    manager.acknowledge_search_results([])
    assert "purpose" in json.loads((await search.execute(query="会议")).content)["connector_candidates"][0]
    manager.acknowledge_search_results([message.model_copy(update={"tool_call_id": "forged"})])
    assert "purpose" in json.loads((await search.execute(query="会议")).content)["connector_candidates"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [10, 41, 200])
@pytest.mark.parametrize("character", ["会", '"', "\\"])
async def test_candidate_count_and_serialized_budget_do_not_scale_with_catalog(count, character):
    entries = [entry(f"connector-{index}", name=character * 128, description=character * 1024,
                     configured=True, enabled=True, status="connected", execution_ready=True,
                     keywords=["会议"], skills=[character * 128] * 16) for index in range(count)]
    _, search, _, _ = search_fixture(entries, selected={row["id"] for row in entries})
    result = await search.execute(query="会议")
    payload = json.loads(result.content)
    assert len(payload["connector_candidates"]) == 3
    extra = {key: payload[key] for key in ("connector_candidates", "notice")}
    assert len(json.dumps(extra, ensure_ascii=False)) <= RESULT_BUDGET
    assert all(len(row.get("purpose", "")) <= 160 for row in payload["connector_candidates"])
    assert len(PROMPT) <= 600
    assert "After requesting setup, finish independent work, end the turn and wait" in PROMPT
    assert "do not repeatedly search alternatives" in PROMPT


def test_cli_skill_requires_current_selection_readiness_and_its_named_entry():
    selected = {"tmeet"}
    item = entry(configured=True, enabled=True, status="connected", execution_ready=True)
    discovery, _, _, _ = search_fixture([item], selected=selected)
    skill = SimpleNamespace(source="connector", owner_id="tmeet", name="tmeet-skill", disabled=False)
    assert _connector_skill_is_available(skill, selected, discovery)
    assert not _connector_skill_is_available(SimpleNamespace(**{**vars(skill), "name": "unowned"}), selected, discovery)
    selected.clear()
    assert not _connector_skill_is_available(skill, selected, discovery)


def test_new_v2_discovery_mode_is_bound_and_legacy_logs_cannot_upgrade(tmp_path):
    for name, interface, requested in [("enabled", 2, True), ("old-host", 2, False), ("v1", 1, True)]:
        log = SessionLog.create(tmp_path, session_id=name, cwd=tmp_path)
        try:
            log.bind_tool_interface_version(interface)
            expected = interface == 2 and requested
            assert bind_discovery_mode(log, requested=requested, new_session=True, tool_version=interface) == expected
        finally:
            log.close()
        log = SessionLog.open(tmp_path, session_id=name, cwd=tmp_path)
        try:
            assert bind_discovery_mode(log, requested=True, new_session=False, tool_version=interface) == expected
        finally:
            log.close()


# Fixture produced by Lab's real connectorDiscoverySnapshot from its bundled manifest.
CATALOG_FIXTURE = Path(__file__).parent / "fixtures" / "connector_discovery_catalog.json"


@pytest.mark.parametrize("query,expected", [
    ("整理腾讯会议的会议纪要", {"tmeet"}),
    ("查找会议录制", {"tmeet"}),
    ("meeting minutes", {"tmeet"}),
    ("会議の録画", {"tmeet"}),
    ("读取金山云文档", {"kdocs"}),
    ("cloud documents", {"kdocs"}),
    ("WPS クラウド文書", {"kdocs"}),
    ("检索法律法规与案例", {"pkulaw"}),
    ("legal cases", {"pkulaw"}),
    ("判例検索", {"pkulaw"}),
    ("查询地点的天气和路线", {"tencent-map"}),
    ("weather directions", {"tencent-map"}),
    ("天気と経路", {"tencent-map"}),
    ("读取代码仓库的Pull Request", {"github"}),
    ("プルリクエスト", {"github"}),
    ("project issue tracking", {"linear"}),
    ("課題管理", {"linear"}),
    ("读取Notion工作区页面", {"notion"}),
    ("Notion ワークスペース", {"notion"}),
    ("提取发票中的关键信息", {"laiye-adp"}),
    ("invoice extraction", {"laiye-adp"}),
    ("請求書の文書解析", {"laiye-adp"}),
    ("用可灵生成视频", {"klingai-cn"}),
    ("動画生成", {"klingai-cn"}),
    ("创建思维导图", {"xmind", "xmind-cn"}),
    ("マインドマップ", {"xmind", "xmind-cn"}),
])
def test_native_catalog_recalls_multilingual_business_intents(query, expected):
    discovery = ConnectorDiscovery(lambda: set())
    discovery.update(json.loads(CATALOG_FIXTURE.read_text()))
    assert expected & {item["id"] for item in discovery.search([query])}


@pytest.mark.asyncio
async def test_final_request_budget_matrix_covers_visible_history_and_compacted_projection(record_property):
    from box_agent.context_input import DefaultContextEngine
    from box_agent.kernel.context_engine import request_input_tokens
    from box_agent.llm import LLMClient
    from box_agent.schema import FunctionCall, ToolCall
    from box_agent.tools.engine.preparation import prepare_tools
    from box_agent.tools.tool_call_tool import CallTool

    # Explicitly use the runtime's local heuristic; these are not provider-reported tokens.
    counter = SimpleNamespace(estimate_text_tokens=lambda text: LLMClient.estimate_text_tokens(None, text))
    measurements = []
    for count in (10, 41, 200):
        common = [entry(f"match-{index}", name=f"Matching {index}",
                        description="Relevant capability", keywords=["sharedcap", *(["uniquecap"] if index == 0 else [])])
                  for index in range(3)]
        fillers = [entry(f"unsearched-{index}", name=f"PRIVATE NAME {index}",
                         description="PRIVATE DESCRIPTION") for index in range(count - 3)]
        discovery, search, manager, _ = search_fixture([*common, *fillers])
        prepared_tools = prepare_tools([search, CallTool()], tool_exposure=manager)
        definitions = [tool.to_openai_schema() for tool in prepared_tools.definitions]
        history = [Message(role="system", content=PROMPT), Message(role="user", content="Proceed")]

        def measure(phase, extra_chars=0):
            projected = DefaultContextEngine(llm=counter).prepare_request(
                history, prepared_tools=prepared_tools, token_limit=100_000,
            )
            assert projected.blocked_reason is None
            manager.acknowledge_search_results(projected.messages)
            request = {"messages": [message.model_dump(exclude_none=True) for message in projected.messages],
                       "tools": definitions}
            serialized = json.dumps(request, ensure_ascii=False)
            assert "PRIVATE NAME" not in serialized and "PRIVATE DESCRIPTION" not in serialized
            measurements.append({"catalog_count": count, "phase": phase, "request_chars": len(serialized),
                                 "fixed_tools_chars": len(json.dumps(definitions, ensure_ascii=False)),
                                 "candidate_extra_chars": extra_chars,
                                 "estimated_input_tokens": request_input_tokens(projected.messages, prepared_tools.definitions, llm=counter)})

        async def search_phase(phase, query):
            result = await search.execute(query=query)
            payload = json.loads(result.content)
            rows = payload["connector_candidates"]
            extra = {key: payload[key] for key in ("connector_candidates", "notice")}
            size = len(json.dumps(extra, ensure_ascii=False))
            assert size <= RESULT_BUDGET and len(rows) <= 3
            manager.record_search_result(phase, result.content)
            history.extend([
                Message(role="assistant", content="", tool_calls=[ToolCall(
                    id=phase, type="function", function=FunctionCall(name="tool_search", arguments={"query": query}),
                )]),
                Message(role="tool", name="tool_search", tool_call_id=phase, content=result.content),
            ])
            measure(phase, size)
            return rows

        measure("unsearched")
        assert len(await search_phase("one", "uniquecap")) == 1
        assert len(await search_phase("three", "sharedcap")) == 3
        assert all("purpose" not in row for row in await search_phase("repeated", "sharedcap"))
        discovery.update(snapshot([{**row, "status": "authorizing"} for row in [*common, *fillers]], revision=2))
        changed = await search_phase("state-changed", "sharedcap")
        assert all(row["action"] == "wait" and row["state_revision"] == 2 and "purpose" not in row for row in changed)
        # Model a committed compaction projection without earlier search tool messages.
        history[:] = [history[0], Message(role="user", content="Compacted task summary. Continue.")]
        measure("compacted")
        restored = await search_phase("rediscovered", "sharedcap")
        assert all("purpose" in row and row["state_revision"] == 2 for row in restored)

    for phase in {row["phase"] for row in measurements}:
        same_phase = [row for row in measurements if row["phase"] == phase]
        assert len({row["request_chars"] for row in same_phase}) == 1, same_phase
        assert len({row["estimated_input_tokens"] for row in same_phase}) == 1, same_phase
    assert len({row["fixed_tools_chars"] for row in measurements}) == 1
    record_property("token_source", "LLMClient.estimate_text_tokens: ceil(UTF-8 bytes / 2), runtime heuristic, not provider usage")
    record_property("connector_budget_matrix", json.dumps(measurements))
