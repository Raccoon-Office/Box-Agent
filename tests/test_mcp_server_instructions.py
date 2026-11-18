"""MCP usage notes follow the tools exposed to each model request."""

import json
from collections import OrderedDict

import pytest

from box_agent.agent import Agent
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from tests.test_mcp_tool_search import FakeMCPTool


def mcp_tool(name, server, instructions="", **options):
    tool = FakeMCPTool(name, server, **options)
    tool.mcp_server_instructions = instructions
    return tool


def note(server, instructions):
    return {"server_name": server, "source": "mcp_server", "instructions": instructions}


@pytest.mark.parametrize("interface_version", [1, 2])
async def test_model_receives_eager_and_discovered_instructions_once(tmp_path, monkeypatch, interface_version):
    catalog = MCPToolCatalog()
    eager = mcp_tool("open_view", "builtin", "Open a view before reading it.", always_load=True)
    catalog.replace_server("builtin", [
        eager,
        mcp_tool("read_view", "builtin", eager.mcp_server_instructions),
    ])
    user_tool = mcp_tool("list_issues", "user", "Look up the project ID first.")
    catalog.replace_server("user", [user_tool])
    monkeypatch.setattr("box_agent.agent.get_mcp_tool_catalog", lambda: catalog)

    class Model:
        model = "offline-mcp-test"
        max_output_tokens = 512

        def __init__(self):
            self.requests = []

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(([message.model_copy(deep=True) for message in messages], list(tools or [])))
            step = len(self.requests)
            if step == 1 and interface_version == 2:
                descriptors = [
                    json.loads(message.content) for message in messages
                    if message.name == "tool_descriptions"
                ]
                ref = next(item["tool_ref"] for item in descriptors if item.get("name") == eager.name)
                function = FunctionCall(name="call_tool", arguments={"tool_ref": ref, "arguments_json": "{}"})
            elif step == 1:
                function = FunctionCall(name=eager.name, arguments={})
            elif step in (2, 3):
                function = FunctionCall(name="tool_search", arguments={"tool_names": [user_tool.name, "read_view"]})
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                ToolCall(id=f"call-{step}", type="function", function=function),
            ])

    model = Model()
    agent = Agent(
        llm_client=model, system_prompt="test", tools=[], workspace_dir=str(tmp_path),
        tool_interface_version=interface_version, max_steps=5,
    )
    agent.add_user_message("Open the view and look up the issues.")
    _ = [event async for event in agent.run_events()]

    assert len(model.requests) == 4
    assert eager.calls == 1
    first_messages, first_tools = model.requests[0]
    assert (eager.name in {tool.name for tool in first_tools}) == (interface_version == 1)
    first_notes = [
        json.loads(message.content)["mcp_server_instructions"]
        for message in first_messages
        if message.name == "tool_descriptions" and "mcp_server_instructions" in message.content
    ]
    assert first_notes == [[note("builtin", eager.mcp_server_instructions)]]
    assert all(user_tool.mcp_server_instructions not in message.content for message in first_messages)
    final_messages, _ = model.requests[-1]
    eager_notes = [
        message for message in final_messages
        if message.name == "tool_descriptions" and "mcp_server_instructions" in message.content
    ]
    assert len(eager_notes) == 1 and eager_notes[0].source == "runtime"
    search_results = {
        message.tool_call_id: json.loads(message.content) for message in final_messages
        if message.role == "tool" and message.name == "tool_search"
    }
    assert search_results["call-2"]["mcp_server_instructions"] == [note("user", user_tool.mcp_server_instructions)]
    assert "mcp_server_instructions" not in search_results["call-3"]
    assert all("mcp_server_instructions" not in message.content for message in final_messages if message.role == "system")


async def test_search_exposes_only_matched_allowed_servers_and_refreshes_after_compaction():
    catalog = MCPToolCatalog()
    catalog.replace_server("user", [
        mcp_tool("find", "user", "Find the object first."),
        mcp_tool("read", "user", "Find the object first."),
    ])
    catalog.replace_server("private", [
        mcp_tool("secret", "private", "Private instructions.", connector_id="private"),
    ])
    catalog.replace_server("empty", [mcp_tool("echo", "empty")])
    active = OrderedDict()
    search = ToolSearchTool(catalog, active, allowed_connector_ids_provider=frozenset)
    manager = MCPToolExposureManager(catalog, active, allowed_connector_ids_provider=frozenset)
    manager.enable_stable_prefix(search)
    result = await search.execute(tool_names=["missing", "find", "read", "secret", "echo"])
    payload = json.loads(result.content)
    assert {entry["name"] for entry in payload["activated"]} == {"find", "read", "echo"}
    assert payload["mcp_server_instructions"] == [note("user", "Find the object first.")]

    manager.record_search_result("search-1", result.content)
    manager.acknowledge_search_results([
        Message(role="tool", name="tool_search", tool_call_id="search-1", content=result.content),
    ])
    repeat = json.loads((await search.execute(tool_names=["read"])).content)
    assert "mcp_server_instructions" not in repeat

    compacted = manager.restore_compacted_descriptions([Message(role="user", content="Continue")])
    assert not any("mcp_server_instructions" in message.content for message in compacted)
    manager.acknowledge_search_results(compacted)
    rediscovered = json.loads((await search.execute(tool_names=["read"])).content)
    assert rediscovered["mcp_server_instructions"] == [note("user", "Find the object first.")]

    catalog.replace_server("user", [mcp_tool("read", "user", "Use the new workflow.")])
    replaced = json.loads((await search.execute(tool_names=["read"])).content)
    assert replaced["mcp_server_instructions"] == [note("user", "Use the new workflow.")]
    catalog.replace_server("user", [mcp_tool("read", "user")])
    no_instructions = json.loads((await search.execute(tool_names=["read"])).content)
    assert no_instructions["activated_count"] == 1
    assert "mcp_server_instructions" not in no_instructions


async def test_search_instruction_budget_counts_json_escaping_without_displacing_tools():
    catalog = MCPToolCatalog()
    for server in ("a", "b", "c"):
        catalog.replace_server(server, [mcp_tool(f"{server}_find", server, '"' * 950)])
    search = ToolSearchTool(catalog, OrderedDict())

    payload = json.loads((await search.execute(tool_names=["a_find", "b_find", "c_find"])).content)
    assert payload["activated_count"] == 3
    notes = payload["mcp_server_instructions"]
    assert [item["server_name"] for item in notes] == ["a", "b"]
    assert len(json.dumps({"mcp_server_instructions": notes}, ensure_ascii=False)) <= 4_096
    followup = json.loads((await search.execute(tool_names=["c_find"])).content)
    assert followup["mcp_server_instructions"] == [note("c", '"' * 950)]


async def test_optional_instructions_preserve_inline_legacy_search_result():
    catalog = MCPToolCatalog()
    catalog.replace_server("user", [
        mcp_tool("find", "user", "a" * 2_000, description="x" * 18_500),
    ])
    search = ToolSearchTool(catalog, OrderedDict())

    result = await search.execute(tool_names=["find"])
    payload = json.loads(result.content)
    assert result.success and len(result.content) <= 20_000
    assert payload["activated"][0]["name"] == "find"
    assert "mcp_server_instructions" not in payload


def test_eager_instructions_require_a_visible_callable_tool():
    catalog = MCPToolCatalog()
    catalog.replace_server("oversized", [
        mcp_tool("large", "oversized", "Oversized instructions.", always_load=True,
                 parameters={"type": "object", "description": "x" * 50_000}),
    ])
    catalog.replace_server("hidden", [
        mcp_tool("hidden", "hidden", "Hidden instructions.", always_load=True, connector_id="hidden"),
    ])
    active = OrderedDict()
    manager = MCPToolExposureManager(catalog, active, allowed_connector_ids_provider=frozenset)
    manager.enable_stable_prefix(ToolSearchTool(catalog, active))
    descriptions = [json.loads(text) for text in manager.pending_bootstrap_descriptions()]
    assert len(descriptions) == 1 and descriptions[0]["name"] == "large"
    assert "tool_ref" not in descriptions[0] and "mcp_server_instructions" not in descriptions[0]
