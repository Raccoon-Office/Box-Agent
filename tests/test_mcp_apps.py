"""MCP Apps stays isolated from model tools and result history."""

import asyncio
import json
from collections import OrderedDict
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import McpError

from box_agent.tools import mcp_apps, mcp_loader
from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool

URI = "ui://flights/search.html"
SECRET = "ui-only-private-result"


def descriptor(name="search", visibility=None):
    ui = {"resourceUri": URI}
    if visibility is not None:
        ui["visibility"] = visibility
    return {"name": name, "description": "Find flights", "inputSchema": {
        "type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"],
    }, "_meta": {"ui": ui}}


class Session:
    def __init__(self):
        self.calls = []
        self.payload = SECRET
        self.fail = None

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if self.fail:
            raise self.fail
        await asyncio.sleep(0)
        return types.CallToolResult(content=[types.TextContent(type="text", text="Found flights")],
                                    structuredContent={"date": arguments["date"]},
                                    _meta={"private": self.payload})

    async def read_resource(self, uri):
        return types.ReadResourceResult(contents=[types.TextResourceContents(
            uri=uri, mimeType=mcp_apps.UI_MIME_TYPE, text="<!doctype html><p>Flights</p>",
        )])

    async def list_resources(self):
        return types.ListResourcesResult(resources=[types.Resource(
            uri=URI, name="Flights", mimeType=mcp_apps.UI_MIME_TYPE,
            _meta={"ui": {"csp": {"connectDomains": ["https://flights.example"]}}},
        )])


def make_tool(session, name="search", visibility=None, **kwargs):
    info = descriptor(name, visibility)
    return mcp_loader.MCPTool(name=f"mcp__flights__{name}", description=info["description"],
                              parameters=info["inputSchema"], session=session,
                              server_name="flights", remote_name=name, descriptor=info,
                              server_identity="stable-flight-source", **kwargs)


@pytest.fixture
def app_connection(monkeypatch):
    monkeypatch.setattr(mcp_apps, "_host_enabled", True)
    session = Session()
    connection = mcp_loader.MCPServerConnection("flights", command="fake", connector_id="travel")
    connection.server_identity = "stable-flight-source"
    connection.session = session
    connection.tools = [make_tool(session, connector_id="travel"),
                        make_tool(session, "refresh", ["app"], connector_id="travel"),
                        make_tool(session, "model_only", ["model"], connector_id="travel")]
    for tool in connection.tools:
        tool._mcp_generation = 7
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [connection])
    return connection, session


def bound_arguments():
    return {"server_identity": "stable-flight-source", "generation": 7,
            "allowed_connector_ids": frozenset({"travel"})}


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_real_sdk_initialize_negotiates_only_for_ui_host(monkeypatch, enabled):
    monkeypatch.setattr(mcp_apps, "_host_enabled", enabled)
    server = Server("apps-test")
    observed = []

    @server.list_tools()
    async def list_tools():
        observed.append(server.request_context.session.client_params.capabilities.model_dump(
            by_alias=True, exclude_none=True))
        return [types.Tool(**descriptor())]

    @asynccontextmanager
    async def transport():
        request_send, request_receive = anyio.create_memory_object_stream(10)
        response_send, response_receive = anyio.create_memory_object_stream(10)
        async with anyio.create_task_group() as group:
            async with request_send, request_receive, response_send, response_receive:
                group.start_soon(server.run, request_receive, response_send,
                                 server.create_initialization_options())
                try:
                    yield response_receive, request_send
                finally:
                    group.cancel_scope.cancel()

    async def open_transport(connection):
        return await connection.exit_stack.enter_async_context(transport())

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "_connect_stdio", open_transport)
    connection = mcp_loader.MCPServerConnection("apps-test", command="unused", connect_timeout=2)
    assert await connection.connect()
    try:
        extensions = observed[0].get("extensions", {})
        assert (mcp_apps.UI_EXTENSION in extensions) is enabled
        if enabled:
            assert extensions[mcp_apps.UI_EXTENSION]["mimeTypes"] == [mcp_apps.UI_MIME_TYPE]
        assert connection.tools[0].mcp_ui_resource_uri == URI
        assert connection.tools[0].mcp_descriptor["_meta"] == descriptor()["_meta"]
    finally:
        await connection.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
async def test_app_only_hidden_from_search_exposure_inheritance_and_final_call(deferred):
    session = Session()
    tools = [make_tool(session, always_load=True), make_tool(session, "refresh", ["app"], always_load=True)]
    catalog = MCPToolCatalog()
    catalog.replace_server("flights", tools)
    active = OrderedDict()
    exposure = MCPToolExposureManager(catalog, active, deferred_mcp=deferred)
    search = ToolSearchTool(catalog, active)
    result = await search.execute(server_name="flights")
    assert [item["name"] for item in json.loads(result.content)["activated"]] == [tools[0].name]
    assert exposure.prepare_tools(tools).tools == [tools[0]]
    assert tools[1].name not in exposure.inherited_tools({tool.name: tool for tool in tools})
    assert exposure.validate_call(tools[1].name, None, tools[1])
    assert exposure.validate_call(tools[1].name, None)
    exposure.enable_stable_prefix(search)
    assert not exposure.is_discoverable(tools[1])
    assert not exposure.is_current_target(tools[1].name, tools[1])
    assert catalog.get(tools[1].mcp_tool_id) is not None


@pytest.mark.parametrize("visibility", [[], ["invalid"], "app", [None]])
def test_invalid_visibility_is_closed_for_model_and_app(visibility):
    tool = make_tool(Session(), visibility=visibility)
    assert not mcp_apps.mcp_tool_allows(tool, "model")
    assert not mcp_apps.mcp_tool_allows(tool, "app")


@pytest.mark.parametrize("legacy", [False, True])
def test_app_workflow_guidance_preserves_server_descriptor(monkeypatch, legacy):
    monkeypatch.setattr(mcp_apps, "_host_enabled", True)
    info = descriptor()
    if legacy:
        info["_meta"] = {"ui/resourceUri": URI}
    original = json.loads(json.dumps(info))
    tool = mcp_loader.MCPTool(
        name="mcp__flights__search", description=info["description"],
        parameters=info["inputSchema"], session=Session(), server_name="flights",
        remote_name="search", descriptor=info,
    )
    assert tool.description.startswith("Find flights\n\n")
    assert mcp_apps.MCP_APP_MODEL_GUIDANCE in tool.description
    assert tool.mcp_descriptor == original
    assert tool.parameters == original["inputSchema"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [False, True])
async def test_app_guidance_does_not_pollute_status_search_or_catalog_purposes(monkeypatch, stable):
    monkeypatch.setattr(mcp_apps, "_host_enabled", True)
    create_info = descriptor("create_canvas")
    create_info["description"] = "Create a canvas."
    create = mcp_loader.MCPTool(
        name="mcp__flights__create_canvas", description=create_info["description"],
        parameters=create_info["inputSchema"], session=Session(), server_name="flights",
        remote_name="create_canvas", descriptor=create_info,
    )
    status = mcp_loader.MCPTool(
        name="mcp__flights__get_task", description="Get task status.",
        parameters={"type": "object", "properties": {}}, session=Session(),
        server_name="flights", remote_name="get_task",
    )
    catalog = MCPToolCatalog()
    catalog.replace_server("flights", [create, status])
    active = OrderedDict()
    search = ToolSearchTool(catalog, active)
    manager = MCPToolExposureManager(catalog, active)
    if stable:
        manager.enable_stable_prefix(search)
    matches = catalog.search("read-only status query", server_name="flights", top_k=1)
    assert [entry.model_name for entry in matches] == [status.name]
    assert catalog.search("polling", server_name="flights") == []
    result = json.loads((await search.execute(tool_names=[create.name])).content)
    assert mcp_apps.MCP_APP_MODEL_GUIDANCE in result["activated"][0]["description"]
    assert result["server_catalogs"][0]["tools"] == [
        {"tool_name": create.name, "purpose": "Create a canvas."},
        {"tool_name": status.name, "purpose": "Get task status."},
    ]
    assert list(active) == [create.mcp_tool_id]


def test_app_guidance_is_delivered_and_restored_for_v2_always_load(monkeypatch):
    from box_agent.schema import Message

    monkeypatch.setattr(mcp_apps, "_host_enabled", True)
    tool = make_tool(Session(), always_load=True)
    catalog = MCPToolCatalog()
    catalog.replace_server("flights", [tool])
    active = OrderedDict()
    search = ToolSearchTool(catalog, active)
    manager = MCPToolExposureManager(catalog, active)
    manager.enable_stable_prefix(search)
    descriptions = manager.pending_bootstrap_descriptions(visible_history=[])
    assert len(descriptions) == 1
    delivered = json.loads(descriptions[0])
    assert mcp_apps.MCP_APP_MODEL_GUIDANCE in delivered["description"]
    assert catalog.get(tool.mcp_tool_id).description == "Find flights"
    manager.ack_bootstrap_descriptions(descriptions)
    history = [Message(role="user", source="runtime", name="tool_descriptions", content=descriptions[0])]
    assert manager.pending_bootstrap_descriptions(visible_history=history) == ()
    restored = manager.pending_bootstrap_descriptions(visible_history=[])
    assert restored == descriptions
    assert json.loads(restored[0])["tool_ref"] == delivered["tool_ref"]


@pytest.mark.parametrize("case", ["no-host", "ordinary", "invalid-ui", "app-only", "session-lease"])
def test_app_workflow_guidance_requires_renderable_model_visible_app(monkeypatch, case):
    monkeypatch.setattr(mcp_apps, "_host_enabled", case != "no-host")
    info = descriptor(visibility=["app"] if case == "app-only" else None)
    if case == "ordinary":
        info.pop("_meta")
    elif case == "invalid-ui":
        info["_meta"]["ui"]["resourceUri"] = "https://example.test/app"
    tool = mcp_loader.MCPTool(
        name="mcp__flights__search", description=info["description"],
        parameters=info["inputSchema"], session=Session(), server_name="flights",
        remote_name="search", descriptor=info,
        session_lease=(lambda: None) if case == "session-lease" else None,
    )
    assert tool.description == "Find flights"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_app_guidance_does_not_rewrite_results_or_expose_private_data(monkeypatch, is_error):
    monkeypatch.setattr(mcp_apps, "_host_enabled", True)
    session = Session()
    original = types.CallToolResult(
        content=[types.TextContent(type="text", text="Request rejected" if is_error else "Awaiting selection")],
        structuredContent={"status": "failed" if is_error else "pending"},
        isError=is_error, _meta={"private": SECRET},
    )
    async def call_tool(name, arguments, **kwargs):
        session.calls.append((name, arguments))
        return original
    session.call_tool = call_tool
    tool = make_tool(session)
    snapshots = []
    token = mcp_apps.set_mcp_app_capture(snapshots.append)
    try:
        result = await tool.invoke({"date": "today"}, context=ToolInvocationContext(parent_tool_call_id="call-1"))
    finally:
        mcp_apps.reset_mcp_app_capture(token)
    assert mcp_apps.MCP_APP_MODEL_GUIDANCE in tool.description
    assert result.success is not is_error
    assert result.content == original.content[0].text
    assert snapshots[0]["result"] == original.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert snapshots[0]["tool"]["description"] == "Find flights"
    assert SECRET not in result.model_dump_json()
    assert session.calls == [("search", {"date": "today"})]


@pytest.mark.asyncio
async def test_parallel_results_capture_private_data_without_model_raw_output():
    tool = make_tool(Session())
    snapshots = []
    token = mcp_apps.set_mcp_app_capture(snapshots.append)
    try:
        first, second = await asyncio.gather(
            tool.invoke({"date": "11-20"}, context=ToolInvocationContext(parent_tool_call_id="call-1")),
            tool.invoke({"date": "11-21"}, context=ToolInvocationContext(parent_tool_call_id="call-2")),
        )
    finally:
        mcp_apps.reset_mcp_app_capture(token)
    assert first.success and second.success
    assert first.raw_output is None and second.raw_output is None
    assert SECRET not in first.model_dump_json()
    assert {item["toolCallId"]: item["result"]["structuredContent"]["date"] for item in snapshots} == {
        "call-1": "11-20", "call-2": "11-21",
    }
    assert all(item["result"]["_meta"]["private"] == SECRET for item in snapshots)
    assert all(item["serverIdentity"] == "stable-flight-source" for item in snapshots)


@pytest.mark.asyncio
async def test_failed_optional_snapshot_does_not_turn_remote_success_into_retryable_failure():
    session = Session()
    session.payload = "x" * (mcp_apps.MAX_APP_PAYLOAD_BYTES + 1)
    tool = make_tool(session)
    snapshots = []
    token = mcp_apps.set_mcp_app_capture(snapshots.append)
    try:
        result = await tool.invoke({"date": "today"}, context=ToolInvocationContext(parent_tool_call_id="call-1"))
    finally:
        mcp_apps.reset_mcp_app_capture(token)
    assert result.success and result.content == "Found flights"
    assert result.raw_output is None and not snapshots
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_resource_read_reuses_connection_and_listing_security_metadata(app_connection):
    result = await mcp_loader.read_mcp_app_resource("flights", URI, **bound_arguments())
    assert result["contents"][0]["_meta"]["ui"]["csp"]["connectDomains"] == ["https://flights.example"]
    with pytest.raises(mcp_apps.McpAppError, match="APP_RESOURCE_NOT_ALLOWED"):
        await mcp_loader.read_mcp_app_resource("flights", "ui://other/private", **bound_arguments())


@pytest.mark.asyncio
async def test_saved_ui_entry_uses_original_tool_authority_after_resource_upgrade(app_connection):
    connection, session = app_connection
    for tool in connection.tools:
        tool.mcp_ui_resource_uri = 'ui://flights/v2.html'
    result = await mcp_loader.read_mcp_app_resource(
        'flights', URI, **bound_arguments(), ui_tool_name='search')
    assert result['contents'][0]['uri'] == URI
    assert result['contents'][0]['_meta']['ui']['csp']['connectDomains'] == ['https://flights.example']
    for name in ['missing', 'refresh']:
        with pytest.raises(mcp_apps.McpAppError, match='APP_RESOURCE_NOT_ALLOWED'):
            await mcp_loader.read_mcp_app_resource(
                'flights', URI, **bound_arguments(), ui_tool_name=name)
    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('code,message,data,missing', [
    (-32002, 'Resource not found', None, True),
    (-32602, 'Resource not found', {'uri': URI}, True),
    (-32602, f'Resource {URI} not found', None, True),
    (-32602, f'Unknown resource: {URI}', None, True),
    (-32602, 'Invalid arguments', None, False),
    (-32602, 'Invalid arguments', {'uri': URI}, False),
    (-32602, 'URI forbidden', {'uri': URI}, False),
    (-32602, 'Invalid resource parameters: locale is required', {'uri': URI, 'parameter': 'locale'}, False),
    (-32603, 'Internal error', None, False),
    (-32000, 'Unauthorized', None, False),
])
async def test_resource_not_found_classification_does_not_mask_other_errors(
    app_connection, code, message, data, missing,
):
    _, session = app_connection
    error = McpError(types.ErrorData(code=code, message=message, data=data))
    async def read(uri):
        raise error
    session.read_resource = read
    with pytest.raises(mcp_apps.McpAppError if missing else McpError) as caught:
        await mcp_loader.read_mcp_app_resource('flights', URI, **bound_arguments())
    if missing:
        assert caught.value.code == 'APP_RESOURCE_NOT_FOUND'
    else:
        assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['connector', 'generation', 'visibility'])
async def test_historical_resource_read_rechecks_authority_after_response(app_connection, change):
    connection, session = app_connection
    allowed = {'travel'}
    original = session.read_resource
    async def read(uri):
        response = await original(uri)
        if change == 'connector':
            allowed.clear()
        elif change == 'generation':
            connection.tools[0]._mcp_generation += 1
        else:
            connection.tools[0].mcp_visibility = frozenset({'app'})
        return response
    session.read_resource = read
    with pytest.raises(mcp_apps.McpAppError, match='APP_CONNECTOR_DISABLED|APP_BINDING_EXPIRED'):
        await mcp_loader.read_mcp_app_resource('flights', URI, **{
            **bound_arguments(), 'allowed_connector_ids': lambda: frozenset(allowed),
        }, ui_tool_name='search')


@pytest.mark.asyncio
async def test_app_only_call_returns_original_result_once_without_capturing_new_chat_view(app_connection):
    connection, session = app_connection
    snapshots = []
    token = mcp_apps.set_mcp_app_capture(snapshots.append)
    try:
        result = await mcp_loader.call_mcp_app_tool("flights", "refresh", {"date": "today"}, **bound_arguments())
    finally:
        mcp_apps.reset_mcp_app_capture(token)
    assert result["_meta"]["private"] == SECRET
    assert result["structuredContent"] == {"date": "today"}
    assert session.calls == [("refresh", {"date": "today"})]
    assert not snapshots


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [
    ({"server_identity": "other"}, "APP_SERVER_CHANGED"),
    ({"generation": 6}, "APP_BINDING_EXPIRED"),
    ({"allowed_connector_ids": frozenset()}, "APP_CONNECTOR_DISABLED"),
])
async def test_stale_or_unauthorized_app_binding_never_reaches_server(app_connection, change, code):
    _, session = app_connection
    args = {**bound_arguments(), **change}
    with pytest.raises(mcp_apps.McpAppError, match=code):
        await mcp_loader.call_mcp_app_tool("flights", "refresh", {"date": "today"}, **args)
    assert not session.calls


@pytest.mark.asyncio
async def test_app_call_checks_visibility_schema_and_existing_capability_gate(app_connection):
    _, session = app_connection
    with pytest.raises(mcp_apps.McpAppError, match="APP_TOOL_NOT_ALLOWED"):
        await mcp_loader.call_mcp_app_tool("flights", "model_only", {"date": "today"}, **bound_arguments())
    result = await mcp_loader.call_mcp_app_tool("flights", "refresh", {}, **bound_arguments())
    assert result["isError"]
    class Gate:
        async def check_invocation(self, name, arguments):
            return SimpleNamespace(code="DENIED", message="Not authorized")
    result = await mcp_loader.call_mcp_app_tool(
        "flights", "refresh", {"date": "today"}, **bound_arguments(),
        invocation_context=ToolInvocationContext(capability_gates=(Gate(),)),
    )
    assert result["isError"]
    assert not session.calls


@pytest.mark.asyncio
async def test_transport_failure_and_missing_ui_projection_do_not_replay_remote_call(app_connection):
    _, session = app_connection
    session.fail = TimeoutError()
    result = await mcp_loader.call_mcp_app_tool("flights", "refresh", {"date": "today"}, **bound_arguments())
    assert result["isError"] and len(session.calls) == 1
    session.fail = None
    session.payload = "x" * (mcp_apps.MAX_APP_PAYLOAD_BYTES + 1)
    with pytest.raises(mcp_apps.McpAppError, match="APP_RESULT_UNAVAILABLE"):
        await mcp_loader.call_mcp_app_tool("flights", "refresh", {"date": "today"}, **bound_arguments())
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_live_connector_revocation_during_guard_prevents_remote_dispatch(app_connection):
    _, session = app_connection
    allowed = {"travel"}
    class Revoke:
        async def check_invocation(self, name, arguments):
            allowed.clear()
            return None
    result = await mcp_loader.call_mcp_app_tool(
        "flights", "refresh", {"date": "today"},
        **{**bound_arguments(), "allowed_connector_ids": lambda: frozenset(allowed)},
        invocation_context=ToolInvocationContext(capability_gates=(Revoke(),)),
    )
    assert result["isError"] and not session.calls


@pytest.mark.asyncio
async def test_stable_v2_routed_call_captures_original_call_id_without_private_model_history(tmp_path, monkeypatch):
    from box_agent.agent import Agent
    from box_agent.schema import FunctionCall, StreamEvent, ToolCall
    from box_agent.session_log import SessionLog
    from box_agent.tools import mcp_tool_catalog

    monkeypatch.setattr(mcp_tool_catalog, "_CATALOG", MCPToolCatalog())
    class Model:
        model = "offline-apps"
        max_output_tokens = 256
        def __init__(self):
            self.requests = []
        def estimate_text_tokens(self, text):
            return len(text) // 3
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append([message.model_dump(mode="json") for message in messages])
            if len(self.requests) == 1:
                call = ToolCall(id="discover", type="function", function=FunctionCall(
                    name="tool_search", arguments={"tool_names": ["mcp__flights__search"]}))
            elif len(self.requests) == 2:
                discovery = next(message for message in messages if message.tool_call_id == "discover")
                ref = json.loads(discovery.content)["activated"][0]["tool_ref"]
                call = ToolCall(id="original-route-id", type="function", function=FunctionCall(
                    name="call_tool", arguments={"tool_ref": ref, "arguments_json": '{"date":"today"}'}))
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[call])

    model = Model()
    log = SessionLog.create(tmp_path / "sessions", session_id="apps-v2", cwd=tmp_path)
    snapshots = []
    token = mcp_apps.set_mcp_app_capture(snapshots.append)
    try:
        agent = Agent(llm_client=model, tools=[make_tool(Session())], system_prompt="BASE",
                      session_log=log, workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False,
                      tool_interface_version=2, max_steps=4)
        agent.add_user_message("Find flights")
        events = [event async for event in agent.run_events()]
        assert len(snapshots) == 1
        assert snapshots[0]["toolCallId"] == "original-route-id"
        assert snapshots[0]["arguments"] == {"date": "today"}
        assert snapshots[0]["result"]["_meta"]["private"] == SECRET
        assert SECRET not in json.dumps(model.requests)
        assert SECRET not in json.dumps(log.events)
        assert SECRET not in repr(events)
    finally:
        mcp_apps.reset_mcp_app_capture(token)
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("require_ui", [False, True])
async def test_resource_read_rechecks_connector_after_server_response(app_connection, require_ui):
    _, session = app_connection
    allowed = {"travel"}
    original = session.read_resource
    async def revoke(uri):
        value = await original(uri)
        allowed.clear()
        return value
    session.read_resource = revoke
    with pytest.raises(mcp_apps.McpAppError, match="APP_CONNECTOR_DISABLED"):
        await mcp_loader.read_mcp_app_resource(
            "flights", URI, **{**bound_arguments(), "allowed_connector_ids": lambda: frozenset(allowed)},
            require_ui=require_ui,
        )


@pytest.mark.parametrize("meta,expected", [
    ({"ui/resourceUri": URI}, URI),
    ({"ui": {"visibility": ["app"]}, "ui/resourceUri": URI}, URI),
    ({"ui": {"resourceUri": URI}, "ui/resourceUri": "ui://legacy/ignored"}, URI),
    ({"ui": {"resourceUri": "https://invalid.example"}, "ui/resourceUri": URI}, None),
    ({"ui": {"resourceUri": None}, "ui/resourceUri": URI}, None),
    ({"ui/resourceUri": "file:///private/data"}, None),
])
def test_ui_resource_metadata_accepts_legacy_without_overriding_modern_values(meta, expected):
    info = {**descriptor(), "_meta": meta}
    tool = mcp_loader.MCPTool(name="mcp__flights__search", description="Find flights",
        parameters=info["inputSchema"], session=Session(), server_name="flights",
        remote_name="search", descriptor=info, server_identity="stable-flight-source")
    assert tool.mcp_ui_resource_uri == expected


@pytest.mark.asyncio
async def test_app_data_resources_preserve_multi_content_blobs_and_metadata(app_connection):
    _, session = app_connection
    seen = []
    value = types.ReadResourceResult.model_validate({"_meta": {"revision": 3}, "contents": [
        {"uri": "flight://details/1", "mimeType": "application/json", "text": '{"flight":1}'},
        {"uri": "flight://ticket/1", "mimeType": "application/pdf", "blob": "dGlja2V0"},
    ]})
    async def read(uri):
        seen.append(uri)
        return value
    session.read_resource = read
    result = await mcp_loader.read_mcp_app_resource("flights", "flight://details/1",
        **bound_arguments(), require_ui=False)
    assert result == value.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert seen == ["flight://details/1"]
    with pytest.raises(mcp_apps.McpAppError, match="APP_RESOURCE_INVALID"):
        await mcp_loader.read_mcp_app_resource("flights", URI, **bound_arguments())
    with pytest.raises(mcp_apps.McpAppError, match="APP_BINDING_EXPIRED"):
        await mcp_loader.read_mcp_app_resource("flights", "flight://details/1",
            **{**bound_arguments(), "generation": 6}, require_ui=False)
    with pytest.raises(mcp_apps.McpAppError, match="APP_CONNECTOR_DISABLED"):
        await mcp_loader.read_mcp_app_resource("flights", "flight://details/1",
            **{**bound_arguments(), "allowed_connector_ids": frozenset()}, require_ui=False)
    assert seen == ["flight://details/1", URI]



@pytest.mark.asyncio
@pytest.mark.parametrize("change,expected_error", [
    ("connector", "APP_CONNECTOR_DISABLED"),
    ("generation", "APP_BINDING_EXPIRED"),
    ("visibility", "APP_TOOL_NOT_ALLOWED"),
    ("history", "APP_CALL_CANCELLED"),
    ("unchanged", None),
])
async def test_app_rechecks_authorization_after_waiting_for_a_concurrency_slot(app_connection, change, expected_error):
    connection, session = app_connection
    class HeldSemaphore(asyncio.Semaphore):
        def __init__(self):
            super().__init__(0)
            self.waiting = asyncio.Event()
        async def acquire(self):
            self.waiting.set()
            return await super().acquire()
    limiter = HeldSemaphore()
    tool = make_tool(session, "web_search", ["app"], connector_id="travel", concurrency_limiter=limiter)
    tool._mcp_generation = 7
    connection.tools.append(tool)
    allowed = {"travel"}
    retired = False
    pending = asyncio.create_task(mcp_loader.call_mcp_app_tool(
        "flights", "web_search", {"date": "today"},
        **{**bound_arguments(), "allowed_connector_ids": lambda: frozenset(allowed)},
        is_cancelled=lambda: retired,
    ))
    await asyncio.wait_for(limiter.waiting.wait(), 1)
    assert not session.calls
    if change == "connector":
        allowed.clear()
    elif change == "generation":
        tool._mcp_generation = 8
    elif change == "visibility":
        tool.mcp_visibility = frozenset({"model"})
    elif change == "history":
        retired = True
    limiter.release()
    result = await asyncio.wait_for(pending, 2)
    if expected_error:
        assert result["isError"] is True
        assert expected_error in result["content"][0]["text"]
        assert not session.calls
    else:
        assert not result.get("isError", False)
        assert len(session.calls) == 1
    assert not limiter.locked(), "failed admission releases the concurrency slot"


@pytest.mark.asyncio
async def test_hosted_search_rechecks_app_admission_after_reconnect_lock_wait(monkeypatch):
    class HeldLock(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.waiting = asyncio.Event()
        async def acquire(self):
            self.waiting.set()
            return await super().acquire()
    lock = HeldLock()
    await lock.acquire()
    lock.waiting.clear()
    session = Session()
    tool = make_tool(session)
    monkeypatch.setattr(mcp_loader, "_mcp_reconnect_locks", {"flights": lock})
    monkeypatch.setattr(tool, "_current_search_session", lambda: session)
    allowed = True
    async def call():
        with mcp_apps.bind_mcp_app_admission(lambda: None if allowed else "APP_CONNECTOR_DISABLED"):
            return await tool._call_hosted_search({"date": "today"}, {})
    pending = asyncio.create_task(call())
    await asyncio.wait_for(lock.waiting.wait(), 1)
    allowed = False
    lock.release()
    with pytest.raises(mcp_apps.McpAppError, match="APP_CONNECTOR_DISABLED"):
        await pending
    assert not session.calls
    # The App-only check cannot leak into an unrelated ordinary invocation.
    assert (await tool.execute(date="today")).success
    assert len(session.calls) == 1


@pytest.mark.parametrize("config,expected", [
    ({"connection_type": "streamable_http", "url": "https://flights.example/mcp"},
     "860bcdc38f9275ebbd0e8951fddfdab82ed9373662059e13cc1d74caf1b32e8d"),
    ({"command": "flight-server", "args": ["--mode", "test"]},
     "d98c74b5a3d942456729b1df68cb0e208019af2427fc7ac44638f0b7922f6f89"),
])
def test_app_server_identity_matches_original_saved_fingerprints(config, expected):
    connection = mcp_loader.MCPServerConnection("flights", **config)
    assert connection.server_identity == expected


@pytest.mark.parametrize("change", [
    {"name": "other-flights"}, {"connection_type": "sse"},
    {"command": "other-server"}, {"args": ["--other"]},
    {"url": "https://other.example/mcp"}, {"connector_id": "other-connector"},
])
def test_service_routing_changes_invalidate_app_server_identity(change):
    config = {"name": "flights", "command": "flight-server", "args": ["--mode", "test"]}
    original = mcp_loader.MCPServerConnection(**config)
    changed = mcp_loader.MCPServerConnection(**{**config, **change})
    assert changed.server_identity != original.server_identity


@pytest.mark.parametrize("change", ["env", "headers", "owner", "config_id", "source_path"])
def test_non_routing_config_changes_preserve_app_server_identity(tmp_path, monkeypatch, change):
    from dataclasses import replace
    from box_agent.tools.mcp_sources import ResolvedMcpServer

    monkeypatch.setattr(mcp_loader, '_mcp_auth_file', None)
    monkeypatch.setattr(mcp_loader, '_mcp_auth_token', None)
    monkeypatch.setattr(mcp_loader, '_mcp_credential_targets', {})
    config = ({'command': 'fixture-server', 'env': {'DB_PATH': 'A.sqlite'}} if change == 'env'
              else {'url': 'https://fixture.example/mcp', 'headers': {'X-Tenant': 'A'}})
    definition = ResolvedMcpServer(name='data', config=config, owner='user', config_id='custom-mcp:data',
        connector_id=None, connector_name=None, source_path=str(tmp_path / 'mcp.json'), fingerprint='unused')
    original = mcp_loader._build_connection(definition)
    if change == 'env':
        changed = replace(definition, config={**config, 'env': {'DB_PATH': 'B.sqlite'}})
    elif change == 'headers':
        changed = replace(definition, config={**config, 'headers': {'X-Tenant': 'B'}})
    elif change == 'owner':
        changed = replace(definition, owner='system', config_id='system:data')
    elif change == 'config_id':
        changed = replace(definition, config_id='custom-mcp:renamed')
    else:
        changed = replace(definition, source_path=str(tmp_path / 'another-mcp.json'))
    assert mcp_loader._build_connection(changed).server_identity == original.server_identity
    reordered = replace(definition, config=dict(reversed(list(config.items()))))
    assert mcp_loader._build_connection(reordered).server_identity == original.server_identity


def test_runtime_credentials_and_reference_preserve_app_server_identity(monkeypatch):
    from dataclasses import replace
    from box_agent.tools.mcp_sources import ResolvedMcpServer

    monkeypatch.setattr(mcp_loader, '_mcp_auth_file', None)
    monkeypatch.setattr(mcp_loader, '_mcp_auth_token', None)
    credentials = {'travel-token': {'Authorization': 'Bearer before-refresh'}}
    monkeypatch.setattr(mcp_loader, '_mcp_runtime_credentials', credentials)
    definition = ResolvedMcpServer(name='travel', owner='connector', config_id='connector:travel',
        connector_id='travel', connector_name='Travel', source_path='<runtime:connector>', fingerprint='old',
        config={'url': 'https://fixture.example/mcp', 'headers': {'X-Tenant': 'A'},
                'credentialRef': 'travel-token', '_connectorId': 'travel'})
    original = mcp_loader._build_connection(definition)
    credentials['travel-token'] = {'Authorization': 'Bearer after-refresh'}
    refreshed = mcp_loader._build_connection(replace(definition, fingerprint='refreshed'))
    assert refreshed.headers['Authorization'] != original.headers['Authorization']
    assert refreshed.server_identity == original.server_identity
    changed_reference = replace(definition, config={**definition.config, 'credentialRef': 'other-account'})
    assert mcp_loader._build_connection(changed_reference).server_identity == original.server_identity


@pytest.mark.asyncio
async def test_resource_listing_follows_cursor_to_csp_and_stops_at_target(app_connection):
    _, session = app_connection
    cursors = []
    ui = {'csp': {'resourceDomains': ['https://static.example'],
                  'connectDomains': ['https://api.example']}, 'prefersBorder': True}
    async def list_resources(*, params=None):
        cursor = params.cursor if params else None
        cursors.append(cursor)
        if cursor is None:
            return types.ListResourcesResult(resources=[], nextCursor='page-2')
        assert cursor == 'page-2'
        return types.ListResourcesResult(resources=[types.Resource(
            uri=URI, name='Flights', mimeType=mcp_apps.UI_MIME_TYPE, _meta={'ui': ui})],
            nextCursor='must-not-fetch')
    session.list_resources = list_resources
    result = await mcp_loader.read_mcp_app_resource('flights', URI, **bound_arguments())
    assert result['contents'][0]['_meta']['ui'] == ui
    assert cursors == [None, 'page-2']


@pytest.mark.asyncio
@pytest.mark.parametrize('repeat', [False, True])
async def test_missing_resource_metadata_stops_at_end_or_repeated_cursor(app_connection, repeat):
    _, session = app_connection
    cursors = []
    async def list_resources(*, params=None):
        cursor = params.cursor if params else None
        cursors.append(cursor)
        await asyncio.sleep(0)
        following = {None: 'page-2', 'page-2': 'page-3', 'page-3': 'page-2' if repeat else None}
        return types.ListResourcesResult(resources=[], nextCursor=following[cursor])
    session.list_resources = list_resources
    result = await asyncio.wait_for(mcp_loader.read_mcp_app_resource('flights', URI, **bound_arguments()), 1)
    assert '_meta' not in result['contents'][0]
    assert cursors == [None, 'page-2', 'page-3']


@pytest.mark.asyncio
async def test_paginated_resource_listing_obeys_existing_read_timeout(app_connection):
    connection, session = app_connection
    connection.execute_timeout = .02
    cursors = []
    async def list_resources(*, params=None):
        cursor = params.cursor if params else None
        cursors.append(cursor)
        if cursor is None:
            return types.ListResourcesResult(resources=[], nextCursor='page-2')
        await asyncio.Event().wait()
    session.list_resources = list_resources
    with pytest.raises(TimeoutError):
        await mcp_loader.read_mcp_app_resource('flights', URI, **bound_arguments())
    assert cursors == [None, 'page-2']


@pytest.mark.asyncio
async def test_content_ui_metadata_takes_precedence_without_listing(app_connection):
    _, session = app_connection
    ui = {'csp': {'resourceDomains': [], 'connectDomains': []}}
    original_read = session.read_resource
    async def read_resource(uri):
        response = await original_read(uri)
        response.contents[0].meta = {'ui': ui}
        return response
    async def list_resources(*args, **kwargs):
        pytest.fail('Content UI metadata must not be replaced by listing metadata')
    session.read_resource = read_resource
    session.list_resources = list_resources
    result = await mcp_loader.read_mcp_app_resource('flights', URI, **bound_arguments())
    assert result['contents'][0]['_meta']['ui'] == ui
