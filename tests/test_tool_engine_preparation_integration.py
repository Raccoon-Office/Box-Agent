"""C2 compatibility across request preparation and real outer-run boundaries."""

import json
from collections import OrderedDict
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from box_agent import composition
from box_agent.cache_fingerprint import build_cache_fingerprint
from box_agent.core import run_agent_loop
from box_agent.events import ToolCallResult
from box_agent.kernel.ports import ToolCatalogPort, ToolEnginePort
from box_agent.plugins.defaults import DEFAULT_CAPABILITY_SCHEMA, default_plugin_descriptors
from box_agent.plugins.descriptors import PluginDescriptor
from box_agent.plugins.host import PluginHost
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tool_result_storage import ToolResultStorage
from box_agent.tools.base import Tool, ToolResult, build_tool_name_index
from box_agent.tools.engine import engine as engine_module
from box_agent.tools.engine.engine import DefaultToolEngine
from box_agent.tools.mcp_loader import MCPTool
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from tests.test_tool_engine_compatibility import (
    _C5_DISCOVERABLE, _c5_names,
    _agent,
    _assemble,
    _assert_schema_contract,
    _normalized_schema,
    _SCHEMA_FIXTURE,
    isolated_setup,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True], ids=["eager", "deferred"])
@pytest.mark.parametrize("all_capabilities", [False, True], ids=["defaults", "all-capabilities"])
async def test_preparation_preserves_legacy_order_schemas_and_aliases(
    isolated_setup, deferred, all_capabilities,
):
    assembly = await _assemble(
        isolated_setup, defaults=True, llm_mode="vision" if all_capabilities else "text",
        memory=all_capabilities, sandbox=all_capabilities,
        image_endpoint=all_capabilities,
    )
    agent = _agent(assembly, deferred=deferred)
    if deferred:
        result = await agent.tools["tool_search"].invoke({"tool_names": ["fixture_lookup"]})
        assert result.success

    # This is the pre-C2 request path: list the live registry, apply the existing
    # exposure, then filter and index names. It never invokes a business tool.
    legacy_tools = list(agent.tools.values())
    legacy_generations = {}
    if agent.mcp_tool_exposure is not None:
        exposure = agent.mcp_tool_exposure.prepare_tools(legacy_tools)
        legacy_tools = exposure.tools
        legacy_generations = exposure.mcp_generations
    visible = lambda name: name != "obsidian_daily_note"
    legacy_tools = [tool for tool in legacy_tools if visible(tool.name)]
    legacy_names = build_tool_name_index(legacy_tools)
    baseline = json.loads(_SCHEMA_FIXTURE.read_text(encoding="utf-8"))["tools"]
    expected_names = _c5_names(set(baseline)) - _C5_DISCOVERABLE - {"obsidian_daily_note"}
    expected_names.add("list_skills")
    expected_names.add("query_jsonl")  # The existing file configuration supplies JSONL directly.
    if not all_capabilities:
        expected_names -= {
            "inspect_images", "generate_image", "execute_code", "sandbox_status",
            "memory_read", "memory_write", "memory_search",
        }
    assert {tool.name for tool in legacy_tools} == expected_names

    # Schedule naming, aliases and every schema field retain the C1 contract.
    schedule = agent.tools["create_scheduled_task"]
    assert "create_scheduled_task" in expected_names
    assert schedule.aliases == ()
    expected_schedule = baseline["create_scheduled_task"]["schema"]
    schedule_schema = _normalized_schema(schedule.to_schema(), isolated_setup.profile)
    assert schedule_schema == expected_schedule
    schedule_openai = _normalized_schema(schedule.to_openai_schema(), isolated_setup.profile)
    assert schedule_openai == {
        "type": "function", "function": {
            "name": expected_schedule["name"], "description": expected_schedule["description"],
            "parameters": expected_schedule["input_schema"],
        },
    }
    _assert_schema_contract(
        [tool for tool in legacy_tools if tool is not schedule], isolated_setup.profile,
        child_read_tools={"query_jsonl", "read_file", "search_files"},
    )

    prepared = DefaultToolEngine(
        tools=agent.tools, tool_exposure=agent.mcp_tool_exposure,
        tool_result_store=agent.tool_result_storage,
    ).prepare_tools(is_tool_visible=visible)

    assert [tool.name for tool in prepared.definitions] == [tool.name for tool in legacy_tools]
    assert [tool.to_schema() for tool in prepared.definitions] == [
        tool.to_schema() for tool in legacy_tools
    ]
    assert [tool.to_openai_schema() for tool in prepared.definitions] == [
        tool.to_openai_schema() for tool in legacy_tools
    ]
    assert list(prepared.targets) == [tool.name for tool in legacy_tools]
    assert all(prepared.targets[tool.name] is tool for tool in legacy_tools)
    assert dict(prepared.call_names) == {name: tool.name for name, tool in legacy_names.items()}
    if deferred:
        assert dict(prepared.mcp_generations) == {
            name: generation for name, generation in legacy_generations.items()
            if name in prepared.targets
        }
    else:
        # C2 additionally fences eager MCP objects by their existing generation;
        # the old loop only retained generation metadata in deferred mode.
        assert dict(prepared.mcp_generations) == {"fixture_lookup": 1}
    assert build_cache_fingerprint(messages=[], tools=list(prepared.definitions)) == (
        build_cache_fingerprint(messages=[], tools=legacy_tools)
    )
    assert all(prepared.validate_call(name) is None for name in legacy_names)
    assert "obsidian_daily_note" not in prepared.targets


class RecordingTool(Tool):
    description = "Record a value in this test session."
    parameters = {
        "type": "object", "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def __init__(self, name):
        self._name = name
        self.aliases = (f"old_{name}",)
        self.values = []

    @property
    def name(self):
        return self._name

    async def execute(self, value):
        self.values.append(value)
        return ToolResult(success=True, content=f"Recorded {value}.")


class ScriptedLLM:
    def __init__(self, *calls):
        self.calls = calls
        self.offered_names = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        step = len(self.offered_names)
        self.offered_names.append([tool.name for tool in tools or []])
        if step < len(self.calls):
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[self.calls[step]])
        else:
            yield StreamEvent(type="text", delta="Done.")
            yield StreamEvent(type="finish", finish_reason="stop")


def _call(call_id, name, **arguments):
    return ToolCall(
        id=call_id, type="function", function=FunctionCall(name=name, arguments=arguments),
    )


def _messages():
    return [Message(role="system", content="Test tools."), Message(role="user", content="Proceed.")]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["connected", "disconnected", "generation_changed"])
async def test_eager_mcp_with_local_discovery_preserves_target_generation_and_execution(state):
    session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
        content=[SimpleNamespace(text="Connected result")], isError=False,
    )))
    if state == "disconnected":
        session.call_tool.side_effect = RuntimeError("Connection closed")
    remote = MCPTool(
        name="lookup", description="Look up a fixture.", parameters=RecordingTool.parameters,
        session=session, server_name="fixture-server",
    )
    remote._mcp_generation = 3
    # Eager mode intentionally has an empty deferred catalog. Its bound target
    # still carries generation metadata and owns connection failure handling.
    exposure = MCPToolExposureManager(MCPToolCatalog(), OrderedDict(), deferred_mcp=False)

    class MutatingLLM(ScriptedLLM):
        async def generate_stream(self, *args, **kwargs):
            if state == "generation_changed":
                remote._mcp_generation += 1
            async for event in super().generate_stream(*args, **kwargs):
                yield event

    events = [event async for event in run_agent_loop(
        llm=MutatingLLM(_call("eager-lookup", "lookup", value="first")),
        tools={remote.name: remote}, messages=_messages(), max_steps=2,
        tool_exposure_manager=exposure,
    )]
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert len(results) == 1
    if state == "generation_changed":
        assert not results[0].success
        assert "changed after it was offered" in results[0].error
        session.call_tool.assert_not_awaited()
    elif state == "disconnected":
        assert not results[0].success
        assert "MCP tool execution failed: Connection closed" in results[0].error
        session.call_tool.assert_awaited_once()
    else:
        assert results[0].success
        session.call_tool.assert_awaited_once_with("lookup", arguments={"value": "first"})


@pytest.mark.asyncio
@pytest.mark.parametrize("early_close", [False, True], ids=["normal-close", "early-close"])
async def test_outer_run_retains_activated_tools_and_borrowed_resources(
    tmp_path, monkeypatch, early_close,
):
    session = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(text="A retained result. " * 20)], isError=False,
        )),
        close=Mock(), aclose=AsyncMock(), connect=AsyncMock(), reconnect=AsyncMock(),
    )
    remote = MCPTool(
        name="lookup", description="Look up a fixture.", parameters=RecordingTool.parameters,
        session=session, server_name="fixture-server",
    )
    remote.aliases = ("old_lookup",)
    catalog = MCPToolCatalog()
    catalog.replace_server("fixture-server", [remote])
    catalog.mark_ready()
    activated = OrderedDict()
    exposure = MCPToolExposureManager(catalog, activated)
    search = ToolSearchTool(catalog, activated)
    registry = {search.name: search}
    storage = ToolResultStorage(tmp_path, default_result_limit=100, aggregate_budget=10_000)
    resource_closers = []
    for resource in (remote, search, exposure, catalog, storage):
        for method in ("close", "aclose", "dispose"):
            closer = AsyncMock() if method == "aclose" else Mock()
            monkeypatch.setattr(resource, method, closer, raising=False)
            resource_closers.append(closer)
    replace_server = Mock(wraps=catalog.replace_server)
    monkeypatch.setattr(catalog, "replace_server", replace_server)
    engines = []

    class RecordingEngine(DefaultToolEngine):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.prepared = []
            engines.append(self)

        def prepare_tools(self, **kwargs):
            prepared = super().prepare_tools(**kwargs)
            self.prepared.append(prepared)
            return prepared

    monkeypatch.setattr(engine_module, "DefaultToolEngine", RecordingEngine)
    history = _messages()
    first_llm = ScriptedLLM(
        _call("discover", "tool_search", tool_names=["lookup"]),
        _call("first-lookup", "old_lookup", value="first"),
    )
    first_events = run_agent_loop(
        llm=first_llm, tools=registry, messages=history, max_steps=4,
        tool_exposure_manager=exposure, tool_result_storage=storage,
    )
    first_results = []
    try:
        async for event in first_events:
            if isinstance(event, ToolCallResult):
                first_results.append(event)
                if early_close and event.tool_call_id == "first-lookup":
                    break
    finally:
        await first_events.aclose()

    assert [result.success for result in first_results] == [True, True]
    assert first_llm.offered_names[:2] == [["tool_search"], ["tool_search", "lookup"]]
    saved_result = next(message.content for message in history if message.tool_call_id == "first-lookup")
    assert saved_result.startswith("<persisted-output>")
    persisted_files = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert persisted_files

    history.append(Message(role="user", content="Look up the second record."))
    second_llm = ScriptedLLM(_call("second-lookup", "old_lookup", value="second"))
    second_events = [event async for event in run_agent_loop(
        llm=second_llm, tools=registry, messages=history, max_steps=3,
        tool_exposure_manager=exposure, tool_result_storage=storage,
    )]

    assert second_llm.offered_names[0] == ["tool_search", "lookup"]
    second_results = [event for event in second_events if isinstance(event, ToolCallResult)]
    assert len(second_results) == 1 and second_results[0].success
    assert [call.kwargs["arguments"] for call in session.call_tool.call_args_list] == [
        {"value": "first"}, {"value": "second"},
    ]
    assert len(engines) == 2
    assert all(engine._tools is registry for engine in engines)
    assert all(engine._tool_exposure is exposure for engine in engines)
    assert all(engine._tool_result_store is storage for engine in engines)
    assert [len(engine.prepared) for engine in engines] == [
        len(first_llm.offered_names), len(second_llm.offered_names),
    ]
    assert engines[0].prepared[1].targets["lookup"] is remote
    assert engines[1].prepared[0].targets["lookup"] is remote
    assert registry == {"tool_search": search}
    assert list(activated) == [remote.mcp_tool_id]
    assert catalog.get_by_model_name("lookup").tool is remote
    assert next(message.content for message in history if message.tool_call_id == "first-lookup") == saved_result
    assert all(path.read_bytes() == content for path, content in persisted_files.items())
    for closer in (*resource_closers, session.close, session.aclose, session.connect, session.reconnect):
        closer.assert_not_called()
    replace_server.assert_not_called()


@pytest.mark.asyncio
async def test_outer_composition_uses_explicit_tool_engine_plugin(monkeypatch):
    tool = RecordingTool("plugin_record")
    unrelated = RecordingTool("legacy_record")
    engine = DefaultToolEngine(tools={tool.name: tool})
    prepared = Mock(wraps=engine.prepare_tools)
    monkeypatch.setattr(engine, "prepare_tools", prepared)
    factory = Mock(return_value=engine)
    disposer = Mock()

    def build_host(**capabilities):
        descriptors = default_plugin_descriptors(**capabilities) + (
            PluginDescriptor(
                plugin_id="test.tool-engine", version="1.0.0",
                capabilities=(ToolEnginePort,), factory=factory, disposer=disposer,
            ),
        )
        return PluginHost(descriptors, schema=DEFAULT_CAPABILITY_SCHEMA)

    monkeypatch.setattr(composition, "create_default_plugin_host", build_host)
    llm = ScriptedLLM(_call("plugin-call", "old_plugin_record", value="plugin"))
    events = [event async for event in run_agent_loop(
        llm=llm, tools={unrelated.name: unrelated}, messages=_messages(), max_steps=3,
    )]

    assert llm.offered_names == [["plugin_record"], ["plugin_record"]]
    assert tool.values == ["plugin"]
    assert unrelated.values == []
    assert [event.success for event in events if isinstance(event, ToolCallResult)] == [True]
    assert prepared.call_count == 2
    factory.assert_called_once_with()
    disposer.assert_called_once_with(engine)


@pytest.mark.asyncio
async def test_default_engine_binds_replacement_catalog_after_plugin_activation(monkeypatch):
    original = RecordingTool("original_record")
    replacement = RecordingTool("replacement_record")
    replacement_registry = {replacement.name: replacement}
    factory = Mock(return_value=replacement_registry)

    def build_host(**capabilities):
        descriptors = tuple(
            replace(descriptor, factory=factory)
            if descriptor.capabilities == (ToolCatalogPort,) else descriptor
            for descriptor in default_plugin_descriptors(**capabilities)
        )
        return PluginHost(descriptors, schema=DEFAULT_CAPABILITY_SCHEMA)

    monkeypatch.setattr(composition, "create_default_plugin_host", build_host)
    llm = ScriptedLLM(_call("replacement-call", "old_replacement_record", value="replacement"))
    events = [event async for event in run_agent_loop(
        llm=llm, tools={original.name: original}, messages=_messages(), max_steps=3,
    )]

    assert llm.offered_names == [["replacement_record"], ["replacement_record"]]
    assert replacement.values == ["replacement"]
    assert original.values == []
    assert [event.success for event in events if isinstance(event, ToolCallResult)] == [True]
    factory.assert_called_once_with()
