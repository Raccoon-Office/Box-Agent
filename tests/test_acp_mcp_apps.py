"""MCP Apps retain host-only data and run UI callbacks without model turns."""
import asyncio
from contextlib import ExitStack, asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import socket
import sys
import threading

import pytest
from acp import PROTOCOL_VERSION, RequestError
from mcp import types
from mcp.shared.exceptions import McpError

from box_agent.acp import BoxACPAgent
from box_agent.acp.mcp_apps import McpAppsACP, SNAPSHOT_EVENT, INTERACTION_EVENT
from box_agent.config import Config, AgentConfig, LLMConfig, ToolsConfig, MCPConfig
from box_agent.kernel.hook_types import BeforeToolDecision
from box_agent.plugins.descriptors import PluginDescriptor
from box_agent.plugins.hooks import HookProviderPort, HookSpec
from box_agent.schema import StreamEvent, ToolCall, FunctionCall
from box_agent.tools import mcp_loader
from box_agent.tools.base import Tool
from box_agent.tools.completion_checks import CompletionIssue
from box_agent.tools.mcp_apps import set_mcp_apps_host_capability
from box_agent.tools.mcp_sources import ResolvedMcpServer

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_apps_server.py"


class Connection:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **meta):
        self.updates.append(update.model_dump(mode="json", by_alias=True, exclude_none=True))


class FlightsLLM:
    model = "fixture-model"
    max_output_tokens = 2048

    def __init__(self):
        self.requests = []
        self.offered_descriptions = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append(deepcopy(messages))
        self.offered_descriptions.append({tool.name: tool.description for tool in tools or []})
        if len(self.requests) == 1:
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                ToolCall(id="flights-original", type="function", function=FunctionCall(
                    name="list_flights", arguments={"date": "2026-11-20"}))])
        else:
            yield StreamEvent(type="text", delta="Found flights.")
            yield StreamEvent(type="finish", finish_reason="stop")


class StableFlightsLLM(FlightsLLM):
    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append(deepcopy(messages))
        self.offered_descriptions.append({tool.name: tool.description for tool in tools or []})
        if len(self.requests) == 1:
            call = ToolCall(id="discover-flights", type="function", function=FunctionCall(
                name="tool_search", arguments={"tool_names": ["mcp__flight-fixture__list_flights"]}))
        elif len(self.requests) == 2:
            message = next(m for m in messages if m.role == 'tool' and m.tool_call_id == 'discover-flights')
            target = next(row for row in json.loads(message.content)['activated'] if row['name'] == 'mcp__flight-fixture__list_flights')
            call = ToolCall(id="flights-original", type="function", function=FunctionCall(
                name="call_tool", arguments={"tool_ref": target['tool_ref'],
                                             "arguments_json": json.dumps({"date": "2026-11-20"})}))
        else:
            yield StreamEvent(type="text", delta="Found flights.")
            yield StreamEvent(type="finish", finish_reason="stop")
            return
        yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[call])


@asynccontextmanager
async def environment(tmp_path, monkeypatch, *, interface_version=1):
    conn, llm = Connection(), (StableFlightsLLM() if interface_version == 2 else FlightsLLM())
    config = Config(llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(tmp_path), max_steps=4, tool_interface_version=interface_version),
        tools=ToolsConfig(enable_sub_agent=False, mcp=MCPConfig(deferred_loading_enabled=False)))
    host = BoxACPAgent(conn, config, llm, [], "system")
    response = await host.initialize(PROTOCOL_VERSION)
    assert "mcp_apps" not in (response.field_meta or {})
    record = tmp_path / "fixture-calls.jsonl"
    backend = mcp_loader.MCPServerConnection("flight-fixture", command=sys.executable,
        args=[str(FIXTURE)], env={"MCP_APPS_FIXTURE_LOG": str(record)}, connect_timeout=10)
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [backend])
    monkeypatch.setattr(mcp_loader, "_mcp_connection_epochs", {})
    catalog = mcp_loader.get_mcp_tool_catalog()
    catalog.clear()
    try:
        assert await backend.connect(), backend.last_error
        mcp_loader._replace_server_catalog(backend)
        host._base_tools = [tool for tool in backend.tools if "model" in tool.mcp_visibility]
        session = await host.new_session(str(tmp_path))
        session_id = session.session_id
        await host.prompt(session_id, [{"type": "text", "text": "Find flights"}], turn_id="initial-turn")
        yield host, conn, llm, backend, session_id, record
    finally:
        await host.aclose()
        await backend.disconnect()
        catalog.clear()
        set_mcp_apps_host_capability(False)


def request(session_id, **kwargs):
    return {"sessionId": session_id, "toolCallId": "flights-original", **kwargs}


def calls(record):
    return [json.loads(line) for line in record.read_text().splitlines()]


@pytest.mark.asyncio
@pytest.mark.parametrize("interface_version", [1, 2])
async def test_app_workflow_guidance_reaches_model_without_changing_card_result(
    tmp_path, monkeypatch, interface_version,
):
    from box_agent.tools.mcp_apps import MCP_APP_MODEL_GUIDANCE
    async with environment(tmp_path, monkeypatch, interface_version=interface_version) as (
        host, conn, llm, backend, session_id, record,
    ):
        if interface_version == 1:
            description = llm.offered_descriptions[0]['mcp__flight-fixture__list_flights']
        else:
            discovery = next(m for m in llm.requests[1] if m.tool_call_id == 'discover-flights')
            payload = json.loads(discovery.content)
            description = payload['activated'][0]['description']
            assert payload['mcp_server_instructions'] == [{
                'server_name': 'flight-fixture', 'source': 'mcp_server',
                'instructions': 'Flight fixture service instructions.',
            }]
        assert MCP_APP_MODEL_GUIDANCE in description
        guidance = ' '.join(description.split())
        assert '30-second interaction window from that invitation' in guidance
        assert 'Actually wait between checks using an available tool' in guidance
        assert 'Space checks at least 5 seconds apart' in guidance
        assert 'an immediate status query followed by a final reply is not sufficient' in guidance
        assert 'Do not repeat transient request statuses' in guidance
        assert 'do not repeat quoted prices, fees, credits, request parameters or prompt details' in guidance
        assert 'not a verified final result or failure, or an explicit user question' in guidance
        observation = next(m for m in llm.requests[-1] if m.role == 'tool' and m.tool_call_id == 'flights-original')
        assert observation.content == '125 flights available.'
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        assert snapshot['result']['content'] == [{'type': 'text', 'text': '125 flights available.'}]
        assert MCP_APP_MODEL_GUIDANCE not in snapshot['tool']['description']
        assert 'not-for-model-context' not in str(llm.requests)
        assert all('mcp__flight-fixture__refresh_flights' not in offered for offered in llm.offered_descriptions)
        assert [item['name'] for item in calls(record)] == ['list_flights']


@pytest.mark.asyncio
async def test_real_stdio_app_roundtrip_keeps_complete_data_out_of_model_and_refreshes_once(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        markers = [u['_meta']['mcpApp'] for u in conn.updates if u.get('_meta', {}).get('mcpApp')]
        assert markers == [{"version": 1, "resourceUri": "ui://flight-fixture/search.html", "title": "Flight search"}]
        snapshot = await host.ext_method("mcp/apps/snapshot", request(session_id))
        assert len(snapshot['result']['structuredContent']['flights']) == 125
        assert snapshot['arguments'] == {"date": "2026-11-20"}
        assert snapshot['result']['_meta']['fixtureViewOnly'] == "not-for-model-context"
        state = host._sessions[session_id]
        model_history = json.dumps([m.model_dump() for m in state.agent.messages])
        assert "not-for-model-context" not in model_history
        assert "Cathay Pacific" not in model_history
        assert "not-for-model-context" not in json.dumps(conn.updates)
        resource = await host.ext_method("mcp/apps/resource", request(session_id,
            bindingId=snapshot['bindingId'], uri=snapshot['resourceUri']))
        assert resource['contents'][0]['mimeType'] == 'text/html;profile=mcp-app'
        assert resource['contents'][0]['_meta']['ui']['csp']['connectDomains'] == []
        args = request(session_id, bindingId=snapshot['bindingId'], interactionId="refresh-1",
                       name="refresh_flights", arguments={"previous": 1})
        first = await host.ext_method("mcp/apps/call", args)
        second = await host.ext_method("mcp/apps/call", args)
        assert first == second
        assert first['structuredContent']['revision'] == 2
        assert [item['name'] for item in calls(record)] == ['list_flights', 'refresh_flights']
        assert len(llm.requests) == 2
        assert any(event['type'] == SNAPSHOT_EVENT and event['ignorable'] for event in state.agent.session_log.events)
        interactions = [e for e in state.agent.session_log.events if e['type'] == INTERACTION_EVENT]
        assert [e['data']['status'] for e in interactions] == ['started', 'completed']
        assert len([m for m in state.agent.messages if m.role == 'tool']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("active_turn", [False, True], ids=["idle", "active"])
@pytest.mark.parametrize("policy", ["hook-deny", "hook-modify", "capability"])
async def test_app_call_enforces_session_policy_before_remote_dispatch(
    tmp_path, monkeypatch, policy, active_turn,
):
    async with environment(tmp_path, monkeypatch) as (host, _, llm, _, session_id, record):
        agent = host._sessions[session_id].agent
        snapshot = await host.ext_method("mcp/apps/snapshot", request(session_id))
        hook_calls = []

        async def before(context):
            expected_session = (
                agent.active_tool_run_context.hook_context.session_id if active_turn else session_id
            )
            assert context.session_id == expected_session
            assert context.payload["tool_name"] == "mcp__flight-fixture__refresh_flights"
            hook_calls.append(context.tool_call_id)
            if policy == "hook-deny":
                return BeforeToolDecision.deny("Fixture policy denies refresh", "TEST_DENIED")
            return BeforeToolDecision.modify({"previous": 8})

        class Hooks:
            def get_hooks(self):
                return (HookSpec("app-policy", ("tool.before_execution",), "before_tool", before),)

        class Gate:
            async def check_invocation(self, name, arguments):
                assert name == "mcp__flight-fixture__refresh_flights"
                return CompletionIssue("TEST_GATE_DENIED", "Fixture capability denies refresh")

            async def check(self):
                return None

        class GateOwner(Tool):
            name = "fixture_guard"
            description = "Owns a fixture capability gate."
            parameters = {"type": "object", "properties": {}}

            async def execute(self):
                pytest.fail("Gate owner should not execute")

            async def create_completion_gate(self, context):
                return Gate()

            async def restore_completion_gate(self, context, state):
                assert state == {"fixture": True}
                return Gate()

        if policy == "capability":
            owner = agent.tools[GateOwner.name] = GateOwner()
            if not active_turn:
                agent.session_log.append("completion.paused", {
                    "version": 1, "checks": {owner.name: {"fixture": True}},
                }, ignorable=True)
                agent.session_log.flush()
        else:
            agent._plugins = (PluginDescriptor("fixture.app-policy", "1.0.0", (HookProviderPort,), Hooks),)

        entered, release = asyncio.Event(), asyncio.Event()
        prompt_task = None
        if active_turn:
            async def hold_model(messages, tools=None, **kwargs):
                entered.set()
                await release.wait()
                yield StreamEvent(type="text", delta="Done.")
                yield StreamEvent(type="finish", finish_reason="stop")

            monkeypatch.setattr(llm, "generate_stream", hold_model)
            prompt_task = asyncio.create_task(host.prompt(session_id, [{"type": "text", "text": "Wait"}]))
        try:
            if active_turn:
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert agent.active_tool_run_context is not None
            params = request(
                session_id, bindingId=snapshot["bindingId"], interactionId="app-policy-check",
                name="refresh_flights", arguments={"previous": 1},
            )
            if policy == "hook-deny":
                with pytest.raises(RequestError, match="MCP_APP_TOOL_DENIED"):
                    await host.ext_method("mcp/apps/call", params)
            else:
                result = await host.ext_method("mcp/apps/call", params)
                if policy == "capability":
                    assert result["isError"] and "TEST_GATE_DENIED" in result["content"][0]["text"]
                else:
                    assert result["structuredContent"]["revision"] == 2
                    assert calls(record)[-1]["arguments"] == {"previous": 8}
            assert len(calls(record)) == (2 if policy == "hook-modify" else 1)
            assert hook_calls == ([] if policy == "capability" else ["app-policy-check"])
        finally:
            release.set()
            if prompt_task is not None:
                await asyncio.wait_for(prompt_task, timeout=5)


@pytest.mark.asyncio
async def test_app_call_waits_for_current_turn_authorization_context(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, _, _, _, session_id, record):
        snapshot = await host.ext_method("mcp/apps/snapshot", request(session_id))
        reserved, proceed = asyncio.Event(), asyncio.Event()
        run_prompt = host._prompt_for_session
        params = request(session_id, bindingId=snapshot["bindingId"],
                         interactionId="during-preparation", name="refresh_flights",
                         arguments={"previous": 1})

        async def hold_preparation(*args, **kwargs):
            reserved.set()
            await proceed.wait()
            return await run_prompt(*args, **kwargs)

        monkeypatch.setattr(host, "_prompt_for_session", hold_preparation)
        task = asyncio.create_task(host.prompt(session_id, [{"type": "text", "text": "Wait"}]))
        try:
            await asyncio.wait_for(reserved.wait(), timeout=5)
            state = host._sessions[session_id]
            assert state._prompt_in_progress and state.agent.active_tool_run_context is None
            with pytest.raises(RequestError, match="MCP_APP_SESSION_BUSY"):
                await host.ext_method("mcp/apps/call", params)
            assert len(calls(record)) == 1
        finally:
            proceed.set()
            await asyncio.wait_for(task, timeout=5)
        assert (await host.ext_method("mcp/apps/call", params))["structuredContent"]["revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set", "clear"])
async def test_accepted_credential_change_revokes_app_binding_before_reconnect(
    tmp_path, monkeypatch, operation,
):
    async with environment(tmp_path, monkeypatch) as (host, _, _, backend, session_id, record):
        snapshot = await host.ext_method("mcp/apps/snapshot", request(session_id))
        ref = "fixture-credential"
        definition = ResolvedMcpServer(
            name=backend.name, config={"credentialRef": ref}, owner="connector", config_id="fixture",
            connector_id="fixture", connector_name="Fixture", source_path="<fixture>", fingerprint="fixture",
        )
        monkeypatch.setattr(mcp_loader, "_mcp_server_definitions", {backend.name: definition})
        monkeypatch.setattr(mcp_loader, "_mcp_runtime_credentials", {ref: {"X-Tenant": "old"}})
        monkeypatch.setattr(mcp_loader, "_mcp_runtime_credential_versions", {})
        credential = {"credentialRef": ref}
        if operation == "set":
            credential["headers"] = {"X-Tenant": "new"}
        result = await host.ext_method(f"mcp/credential/{operation}", credential)
        assert result == {"success": True, "affectedServers": [backend.name]}
        assert backend.session is not None
        for method, extra in (
            ("snapshot", {}),
            ("resource", {"bindingId": snapshot["bindingId"], "uri": snapshot["resourceUri"]}),
            ("call", {"bindingId": snapshot["bindingId"], "interactionId": "after-revoke",
                      "name": "refresh_flights", "arguments": {"previous": 1}}),
        ):
            expected = "APP_SERVER_UNAVAILABLE" if method == "snapshot" else "APP_SERVER_CHANGED"
            with pytest.raises(RequestError, match=expected):
                await host.ext_method(f"mcp/apps/{method}", request(session_id, **extra))
        assert len(calls(record)) == 1


@pytest.mark.asyncio
async def test_app_reply_is_withheld_after_binding_revocation_but_receipt_survives(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, _, _, backend, session_id, record):
        snapshot = await host.ext_method("mcp/apps/snapshot", request(session_id))
        result_received, send_reply = asyncio.Event(), asyncio.Event()
        remote_call = backend.session.call_tool

        async def hold_reply(*args, **kwargs):
            result = await remote_call(*args, **kwargs)
            result_received.set()
            await send_reply.wait()
            return result

        monkeypatch.setattr(backend.session, "call_tool", hold_reply)
        params = request(session_id, bindingId=snapshot["bindingId"], interactionId="revoked-before-reply",
                         name="refresh_flights", arguments={"previous": 1})
        pending = asyncio.create_task(host.ext_method("mcp/apps/call", params))
        try:
            await asyncio.wait_for(result_received.wait(), timeout=5)
            mcp_loader._begin_mcp_connection(backend.name)
            send_reply.set()
            with pytest.raises(RequestError, match="APP_SERVER_CHANGED"):
                await asyncio.wait_for(pending, timeout=5)
            log = host._sessions[session_id].agent.session_log
            assert [e["data"]["status"] for e in log.events_of_type(INTERACTION_EVENT)] == [
                "started", "completed",
            ]
            assert len(calls(record)) == 2
        finally:
            send_reply.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_history_recovers_snapshot_without_running_tools_or_accepting_old_binding(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        original = await host.ext_method('mcp/apps/snapshot', request(session_id))
        conn.updates.clear()
        await host.load_session(str(tmp_path), session_id)
        assert len(calls(record)) == 1
        assert len(llm.requests) == 2
        assert any(u.get('_meta', {}).get('mcpApp') for u in conn.updates)
        restored = await host.ext_method('mcp/apps/snapshot', request(session_id))
        assert restored['result'] == original['result']
        assert restored['bindingId'] != original['bindingId']
        with pytest.raises(RequestError, match='MCP_APP_BINDING_EXPIRED'):
            await host.ext_method('mcp/apps/resource', request(session_id,
                bindingId=original['bindingId'], uri=original['resourceUri']))
        host._sessions[session_id].agent.session_log.append('surface/reset', {})
        with pytest.raises(RequestError, match='MCP_APP_SNAPSHOT_UNAVAILABLE'):
            await host.ext_method('mcp/apps/snapshot', request(session_id))
        assert len(calls(record)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_code', [None, -32002, -32602])
async def test_history_restores_old_resource_or_current_ui_without_rewriting_snapshot(
    tmp_path, monkeypatch, missing_code,
):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        original = await host.ext_method('mcp/apps/snapshot', request(session_id))
        tool = next(t for t in backend.tools if t.remote_name == 'list_flights')
        current_uri = 'ui://new-flight-pages/v2.html'
        tool.mcp_ui_resource_uri = current_uri
        mcp_loader._replace_server_catalog(backend)
        with pytest.raises(RequestError, match='MCP_APP_BINDING_EXPIRED'):
            await host.ext_method('mcp/apps/resource', request(
                session_id, bindingId=original['bindingId'], uri=original['resourceUri']))
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        assert {k: v for k, v in snapshot.items() if k != 'bindingId'} == {
            k: v for k, v in original.items() if k != 'bindingId'}
        events = deepcopy(host._sessions[session_id].agent.session_log.events)
        seen = []
        async def read(uri):
            seen.append(uri)
            if missing_code is not None and uri == original['resourceUri']:
                raise McpError(types.ErrorData(code=missing_code, message='Resource not found', data={'uri': uri}))
            return types.ReadResourceResult.model_validate({
                'resolvedResourceUri': 'ui://untrusted/override.html',
                'contents': [{'uri': uri, 'mimeType': 'text/html;profile=mcp-app',
                              'text': '<p>Restored</p>', '_meta': {'ui': {'csp': {'connectDomains': []}}}}],
            })
        monkeypatch.setattr(backend.session, 'read_resource', read)
        result = await host.ext_method('mcp/apps/resource', request(
            session_id, bindingId=snapshot['bindingId'], uri=snapshot['resourceUri']))
        selected_uri = current_uri if missing_code is not None else original['resourceUri']
        assert seen == [original['resourceUri']] + ([current_uri] if missing_code is not None else [])
        assert result['resolvedResourceUri'] == selected_uri
        assert result['contents'][0]['uri'] == selected_uri
        assert host._sessions[session_id].agent.session_log.events == events
        assert (await host.ext_method('mcp/apps/snapshot', request(session_id))) == snapshot
        assert len(calls(record)) == 1
        assert len(llm.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['auth', 'timeout', 'invalid', 'empty', 'http401', 'http403', 'http404',
                                     'invalid_parameters', 'forbidden_uri', 'both_missing'])
async def test_history_resource_fallback_is_only_for_a_definite_missing_entry(tmp_path, monkeypatch, failure):
    import httpx
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        tool = next(t for t in backend.tools if t.remote_name == 'list_flights')
        tool.mcp_ui_resource_uri = 'ui://flight-fixture/v2.html'
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        seen = []
        async def read(uri):
            seen.append(uri)
            if failure == 'timeout':
                raise TimeoutError()
            if failure.startswith('http'):
                response = httpx.Response(int(failure[4:]), request=httpx.Request('POST', 'https://fixture.invalid/mcp'))
                raise httpx.HTTPStatusError('Endpoint missing', request=response.request, response=response)
            if failure == 'empty':
                return types.ReadResourceResult(contents=[])
            if failure == 'invalid':
                return types.ReadResourceResult(contents=[types.TextResourceContents(
                    uri=uri, mimeType='text/plain', text='Not an App')])
            if failure in {'invalid_parameters', 'forbidden_uri'}:
                raise McpError(types.ErrorData(
                    code=-32602,
                    message='Invalid resource parameters: locale is required' if failure == 'invalid_parameters' else 'URI forbidden',
                    data={'uri': uri},
                ))
            raise McpError(types.ErrorData(code=-32002 if failure == 'both_missing' else -32000,
                                          message='Resource not found' if failure == 'both_missing' else 'Unauthorized'))
        monkeypatch.setattr(backend.session, 'read_resource', read)
        expected = {'timeout': 'MCP_APP_REQUEST_TIMEOUT', 'invalid': 'APP_RESOURCE_INVALID',
                    'http401': 'APP_AUTH_REQUIRED', 'http403': 'APP_AUTH_REQUIRED',
                    'empty': 'APP_RESOURCE_INVALID', 'both_missing': 'APP_RESOURCE_NOT_FOUND'}.get(failure, 'MCP_APP_UNAVAILABLE')
        with pytest.raises(RequestError, match=expected):
            await host.ext_method('mcp/apps/resource', request(
                session_id, bindingId=snapshot['bindingId'], uri=snapshot['resourceUri']))
        assert seen == [snapshot['resourceUri']] + ([tool.mcp_ui_resource_uri] if failure == 'both_missing' else [])
        assert len(calls(record)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['connection', 'history', 'server'])
async def test_history_fallback_rechecks_binding_before_reading_new_page(tmp_path, monkeypatch, change):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        tool = next(t for t in backend.tools if t.remote_name == 'list_flights')
        tool.mcp_ui_resource_uri = 'ui://flight-fixture/v2.html'
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        seen = []
        async def read(uri):
            seen.append(uri)
            if change == 'connection':
                mcp_loader._replace_server_catalog(backend)
            elif change == 'server':
                backend.server_identity = 'other-server'
            else:
                host._sessions[session_id].agent.session_log.append('surface/reset', {})
            raise McpError(types.ErrorData(code=-32002, message='Resource not found'))
        monkeypatch.setattr(backend.session, 'read_resource', read)
        expected = {'connection': 'MCP_APP_BINDING_EXPIRED', 'history': 'MCP_APP_SNAPSHOT_UNAVAILABLE',
                    'server': 'MCP_APP_BINDING_EXPIRED'}[change]
        with pytest.raises(RequestError, match=expected):
            await host.ext_method('mcp/apps/resource', request(
                session_id, bindingId=snapshot['bindingId'], uri=snapshot['resourceUri']))
        assert seen == [snapshot['resourceUri']]
        assert len(calls(record)) == 1


@pytest.mark.asyncio
async def test_acp_enables_apps_without_host_capability_negotiation(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        assert backend.apps_enabled is True
        assert any(u.get('_meta', {}).get('mcpApp') for u in conn.updates)
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        assert snapshot['version'] == 1
        assert len(calls(record)) == 1


@pytest.mark.asyncio
async def test_app_calls_reject_cross_session_resources_visibility_and_changed_requests(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        binding = snapshot['bindingId']
        peer = await host.new_session(str(tmp_path))
        with pytest.raises(RequestError, match='MCP_APP_SNAPSHOT_UNAVAILABLE'):
            await host.ext_method('mcp/apps/snapshot', request(peer.session_id))
        with pytest.raises(RequestError, match='MCP_APP_UNAVAILABLE'):
            await host.ext_method('mcp/apps/resource', request(session_id, bindingId=binding, uri='ui://other/private'))
        with pytest.raises(RequestError, match='APP_TOOL_NOT_ALLOWED'):
            await host.ext_method('mcp/apps/call', request(session_id, bindingId=binding,
                interactionId='forbidden', name='model_only', arguments={}))
        invalid = await host.ext_method('mcp/apps/call', request(session_id, bindingId=binding,
            interactionId='bad-arguments', name='refresh_flights', arguments={'previous': 'not-an-integer'}))
        assert invalid['isError'] is True
        assert len(calls(record)) == 1
        valid = request(session_id, bindingId=binding, interactionId='refresh-1', name='refresh_flights', arguments={'previous': 1})
        await host.ext_method('mcp/apps/call', valid)
        with pytest.raises(RequestError, match='MCP_APP_INTERACTION_CONFLICT'):
            await host.ext_method('mcp/apps/call', {**valid, 'arguments': {'previous': 2}})
        mcp_loader._replace_server_catalog(backend)
        with pytest.raises(RequestError, match='MCP_APP_BINDING_EXPIRED'):
            await host.ext_method('mcp/apps/resource', request(session_id, bindingId=binding, uri=snapshot['resourceUri']))
        assert len(calls(record)) == 2


@pytest.mark.asyncio
async def test_cancel_only_the_named_app_interaction_without_cancelling_conversation(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        entered = asyncio.Event()
        async def waiting(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(mcp_loader, 'call_mcp_app_tool', waiting)
        params = request(session_id, bindingId=snapshot['bindingId'], interactionId='wait-1',
                         name='refresh_flights', arguments={'previous': 1})
        pending = asyncio.create_task(host.ext_method('mcp/apps/call', params))
        await asyncio.wait_for(entered.wait(), 2)
        assert await host.ext_method('mcp/apps/cancel', {'sessionId': session_id, 'interactionId': 'other'}) == {'cancelled': False}
        assert await host.ext_method('mcp/apps/cancel', {'sessionId': session_id, 'interactionId': 'wait-1'}) == {'cancelled': True}
        with pytest.raises(RequestError, match='MCP_APP_OUTCOME_UNKNOWN'):
            await pending
        assert not host._sessions[session_id].cancelled
        with pytest.raises(RequestError, match='MCP_APP_OUTCOME_UNKNOWN'):
            await host.ext_method('mcp/apps/call', params)
        assert len(calls(record)) == 1


@pytest.mark.asyncio
async def test_app_call_and_cancel_admitted_together_return_a_terminal_receipt(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, _, _, _, session_id, record):
        snapshot = await host.ext_method("mcp/apps/snapshot", request(session_id))
        params = request(session_id, bindingId=snapshot["bindingId"], interactionId="cancel-on-admission",
                         name="refresh_flights", arguments={"previous": 1})
        # Both requests are ready before the new interaction task first runs.
        call, cancel = await asyncio.gather(
            host.ext_method("mcp/apps/call", params),
            host.ext_method("mcp/apps/cancel", {"sessionId": session_id, "interactionId": params["interactionId"]}),
            return_exceptions=True,
        )
        assert cancel == {"cancelled": True}
        assert isinstance(call, RequestError) and call.data == {"code": "MCP_APP_OUTCOME_UNKNOWN"}
        await host.load_session(str(tmp_path), session_id)
        restored = await host.ext_method("mcp/apps/snapshot", request(session_id))
        with pytest.raises(RequestError, match="MCP_APP_OUTCOME_UNKNOWN"):
            await host.ext_method("mcp/apps/call", {**params, "bindingId": restored["bindingId"]})
        assert [item["name"] for item in calls(record)] == ["list_flights"]


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", ["queued", "flushing"])
async def test_app_call_replies_when_cancelled_during_its_initial_checkpoint(tmp_path, monkeypatch, checkpoint):
    from acp.agent.connection import AgentSideConnection
    from acp.client.connection import ClientSideConnection
    from box_agent import session_log
    from box_agent.acp.stdio_compat import StdioTransport

    async with environment(tmp_path, monkeypatch) as (host, conn, _, _, session_id, record):
        log = host._sessions[session_id].agent.session_log
        local, remote = socket.socketpair()
        client_reader, client_writer = await asyncio.open_connection(sock=local)
        agent_reader, agent_writer = await asyncio.open_connection(sock=remote)
        server = AgentSideConnection(host, StdioTransport(agent_reader, agent_writer))
        client = ClientSideConnection(conn, StdioTransport(client_reader, client_writer))
        try:
            snapshot = await client.ext_method("mcp/apps/snapshot", request(session_id))
            params = request(session_id, bindingId=snapshot["bindingId"], interactionId="cancel-at-start",
                             name="refresh_flights", arguments={"previous": 1})
            entered = asyncio.Event()
            with ExitStack() as gate:
                if checkpoint == "queued":
                    await log._write_lock.acquire()
                    gate.callback(log._write_lock.release)
                    acquire = log._write_lock.acquire

                    async def observe_waiter():
                        entered.set()
                        return await acquire()

                    monkeypatch.setattr(log._write_lock, "acquire", observe_waiter)
                else:
                    loop = asyncio.get_running_loop()
                    release = threading.Event()
                    gate.callback(release.set)
                    fsync = session_log._timed_fsync
                    log_fd = log._handle.fileno()

                    def hold_started(fd):
                        if fd == log_fd and log.events[-1]["data"].get("status") == "started":
                            loop.call_soon_threadsafe(entered.set)
                            assert release.wait(10)
                        fsync(fd)

                    monkeypatch.setattr(session_log, "_timed_fsync", hold_started)

                call = asyncio.create_task(client.ext_method("mcp/apps/call", params))
                await asyncio.wait_for(entered.wait(), 5)
                cancel = {"sessionId": session_id, "interactionId": params["interactionId"]}
                # A retried cancel must not interrupt the durable failure receipt.
                for _ in range(2):
                    assert await asyncio.wait_for(client.ext_method("mcp/apps/cancel", cancel), 5) == {"cancelled": True}

            with pytest.raises(RequestError, match="MCP_APP_OUTCOME_UNKNOWN") as error:
                await asyncio.wait_for(call, 3)
            assert error.value.data == {"code": "MCP_APP_OUTCOME_UNKNOWN"}
            receipts = [e["data"] for e in map(json.loads, log.path.read_bytes().splitlines())
                        if e.get("type") == INTERACTION_EVENT]
            assert [r["status"] for r in receipts] == (["failed"] if checkpoint == "queued" else ["started", "failed"])
            assert receipts[-1]["error"] == "MCP_APP_OUTCOME_UNKNOWN"
            with pytest.raises(RequestError, match="MCP_APP_OUTCOME_UNKNOWN"):
                await asyncio.wait_for(client.ext_method("mcp/apps/call", params), 3)
            assert [item["name"] for item in calls(record)] == ["list_flights"]
            fresh = await asyncio.wait_for(client.ext_method("mcp/apps/call", {**params, "interactionId": "fresh"}), 5)
            assert fresh["structuredContent"]["revision"] == 2
        finally:
            await client.close()
            await server.close()
            client_writer.close()
            agent_writer.close()
            await asyncio.gather(client_writer.wait_closed(), agent_writer.wait_closed())


@pytest.mark.asyncio
async def test_default_v2_call_tool_captures_original_call_id_and_full_input(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch, interface_version=2) as (host, conn, llm, backend, session_id, record):
        assert host._sessions[session_id].agent.tool_interface_version == 2
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        assert snapshot['arguments'] == {'date': '2026-11-20'}
        assert len(snapshot['result']['structuredContent']['flights']) == 125
        assert len(calls(record)) == 1
        assert 'not-for-model-context' not in str(llm.requests)
        assert any(u.get('toolCallId') == 'flights-original' and u.get('_meta', {}).get('mcpApp') for u in conn.updates)


@pytest.mark.asyncio
async def test_offline_agent_fixture_uses_real_acp_stdio_for_app_operations(tmp_path):
    state_dir = tmp_path / 'wire-state'
    process = await asyncio.create_subprocess_exec(sys.executable,
        str(FIXTURE.with_name('mcp_apps_agent.py')), '--state-dir', str(state_dir),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stderr = asyncio.create_task(process.stderr.read())
    updates = []
    next_id = 0

    async def rpc(method, params):
        nonlocal next_id
        next_id += 1
        request_id = next_id
        process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}) + '\n').encode())
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 15)
            assert line, 'ACP fixture exited before replying'
            message = json.loads(line)
            if message.get('id') == request_id:
                assert 'error' not in message, message
                return message['result']
            updates.append(message)

    try:
        initialized = await rpc('initialize', {'protocolVersion': PROTOCOL_VERSION, 'clientCapabilities': {}})
        assert initialized['agentInfo']['name'] == 'box-agent'
        for _ in range(100):
            status = await rpc('_mcp/status', {})
            if any(item['name'] == 'flight-fixture' and item['state'] == 'connected' for item in status['servers']):
                break
            await asyncio.sleep(.05)
        else:
            pytest.fail(f'fixture MCP failed to connect: {status}')
        session = await rpc('session/new', {'cwd': str(state_dir / 'workspace'), 'mcpServers': []})
        session_id = session['sessionId']
        await rpc('session/prompt', {'sessionId': session_id, 'prompt': [{'type': 'text', 'text': 'Find flights'}]})
        snapshot = await rpc('_mcp/apps/snapshot', request(session_id))
        assert len(snapshot['result']['structuredContent']['flights']) == 125
        resource = await rpc('_mcp/apps/resource', request(session_id,
            bindingId=snapshot['bindingId'], uri=snapshot['resourceUri']))
        assert resource['contents'][0]['mimeType'] == 'text/html;profile=mcp-app'
        refreshed = await rpc('_mcp/apps/call', request(session_id, bindingId=snapshot['bindingId'],
            interactionId='wire-refresh', name='refresh_flights', arguments={'previous': 1}))
        assert refreshed['structuredContent']['revision'] == 2
        assert any(item.get('params', {}).get('update', {}).get('_meta', {}).get('mcpApp') for item in updates)
        assert [item['name'] for item in calls(state_dir / 'calls.jsonl')] == ['list_flights', 'refresh_flights']
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        diagnostics = await stderr
        assert process.returncode == 0, diagnostics.decode(errors='replace')


@pytest.mark.asyncio
async def test_session_rebind_cancels_app_work_before_closing_its_log(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        entered = asyncio.Event()
        async def waiting(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(mcp_loader, 'call_mcp_app_tool', waiting)
        params = request(session_id, bindingId=snapshot['bindingId'], interactionId='rebind-wait',
                         name='refresh_flights', arguments={'previous': 1})
        task = asyncio.create_task(host.ext_method('mcp/apps/call', params))
        await asyncio.wait_for(entered.wait(), 2)
        await host.load_session(str(tmp_path), session_id)
        with pytest.raises(RequestError, match='MCP_APP_OUTCOME_UNKNOWN'):
            await task
        assert not host._mcp_apps._interactions
        restored = host._sessions[session_id].agent.session_log.events
        assert any(e['type'] == INTERACTION_EVENT and e['data'].get('error') == 'MCP_APP_OUTCOME_UNKNOWN' for e in restored)
        assert len(calls(record)) == 1


@pytest.mark.asyncio
async def test_rebind_revokes_old_state_before_waiting_for_app_cancellation(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        old_state = host._sessions[session_id]
        entered, cancelling, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        dispatched = []
        real_call = mcp_loader.call_mcp_app_tool

        async def waiting(*args, **kwargs):
            dispatched.append(args)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release_cleanup.wait()
                raise

        monkeypatch.setattr(mcp_loader, 'call_mcp_app_tool', waiting)
        params = request(session_id, bindingId=snapshot['bindingId'], interactionId='closing-first',
                         name='refresh_flights', arguments={'previous': 1})
        first = asyncio.create_task(host.ext_method('mcp/apps/call', params))
        await asyncio.wait_for(entered.wait(), 2)
        rebinding = asyncio.create_task(host.load_session(str(tmp_path), session_id))
        try:
            await asyncio.wait_for(cancelling.wait(), 2)
            assert host._sessions[session_id] is old_state
            assert old_state._mcp_apps_closing and not old_state._closed
            with pytest.raises(RequestError, match='MCP_APP_SESSION_UNAVAILABLE'):
                await host.ext_method('mcp/apps/call', {**params, 'interactionId': 'closing-second'})
            assert len(dispatched) == 1
        finally:
            release_cleanup.set()
            await rebinding
            with pytest.raises(RequestError, match='MCP_APP_OUTCOME_UNKNOWN'):
                await first
        replacement = host._sessions[session_id]
        assert replacement is not old_state and not replacement._mcp_apps_closing
        monkeypatch.setattr(mcp_loader, 'call_mcp_app_tool', real_call)
        refreshed_snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        result = await host.ext_method('mcp/apps/call', {**params,
            'bindingId': refreshed_snapshot['bindingId'], 'interactionId': 'replacement-refresh'})
        assert result['structuredContent']['revision'] == 2
        assert [item['name'] for item in calls(record)] == ['list_flights', 'refresh_flights']


@pytest.mark.asyncio
async def test_app_reads_same_server_data_without_model_turns_or_tool_execution(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        binding = snapshot['bindingId']
        count = len(llm.requests)
        data = await host.ext_method('mcp/apps/resource', request(session_id,
            bindingId=binding, uri='flight://details/1'))
        assert data['contents'][0]['mimeType'] == 'application/json'
        assert json.loads(data['contents'][0]['text']) == {"flight": 1, "airline": "ANA"}
        ticket = await host.ext_method('mcp/apps/resource', request(session_id,
            bindingId=binding, uri='flight://ticket/1'))
        assert ticket['contents'][0]['blob'] == 'Zml4dHVyZS10aWNrZXQ='
        assert len(llm.requests) == count
        assert len(calls(record)) == 1
        with pytest.raises(RequestError, match='MCP_APP_BINDING_EXPIRED'):
            await host.ext_method('mcp/apps/resource', request(session_id,
                bindingId='foreign-binding', uri='flight://details/1'))


@pytest.mark.asyncio
async def test_app_context_persistence_endpoints_are_removed_and_old_ignorable_events_stay_inert(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        log = host._sessions[session_id].agent.session_log
        # Previously written events remain readable, but never restore App state or become messages.
        log.append('mcp-app/model-context', {'version': 1, 'toolCallId': 'flights-original',
            'resourceUri': snapshot['resourceUri'], 'context': {'structuredContent': {'legacy': 'old-app-selection'}}},
            ignorable=True)
        log.flush()
        await host.load_session(str(tmp_path), session_id)
        new = await host.ext_method('mcp/apps/snapshot', request(session_id))
        assert new['result'] == snapshot['result']
        assert 'old-app-selection' not in json.dumps([m.model_dump() for m in host._sessions[session_id].agent.messages])
        for method in ['mcp/apps/context', 'mcp/apps/contexts']:
            with pytest.raises(RequestError, match='MCP_APP_METHOD_UNSUPPORTED'):
                await host.ext_method(method, request(session_id, bindingId=new['bindingId'],
                    context={'structuredContent': {'new': 'must-not-save'}}))
        events = host._sessions[session_id].agent.session_log.events
        assert len([event for event in events if event['type'] == 'mcp-app/model-context']) == 1
        assert 'must-not-save' not in json.dumps(events)
        assert len(llm.requests) == 2 and len(calls(record)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "unknown"])
async def test_history_edit_preserves_app_interaction_receipts_after_reload(tmp_path, monkeypatch, outcome):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        await host.prompt(session_id, [{'type': 'text', 'text': 'A later question'}], turn_id='later-turn')
        params = request(session_id, bindingId=snapshot['bindingId'], interactionId='same-write',
                         name='refresh_flights', arguments={'previous': 1})
        if outcome == 'unknown':
            original_call = mcp_loader.call_mcp_app_tool
            async def lose_response(*args, **kwargs):
                await original_call(*args, **kwargs)
                raise asyncio.CancelledError()
            with monkeypatch.context() as patch:
                patch.setattr(mcp_loader, 'call_mcp_app_tool', lose_response)
                with pytest.raises(RequestError, match='MCP_APP_OUTCOME_UNKNOWN'):
                    await host.ext_method('mcp/apps/call', params)
        else:
            first = await host.ext_method('mcp/apps/call', params)
            assert first['structuredContent']['revision'] == 2
        assert [item['name'] for item in calls(record)] == ['list_flights', 'refresh_flights']

        await host.prompt(session_id, [{'type': 'text', 'text': 'Edited later question'}],
            turn_id='edited-turn', history_edit={'target_turn_id': 'later-turn', 'expected_revision_id': 'initial'})
        log = host._sessions[session_id].agent.session_log
        assert not any(event['type'] == INTERACTION_EVENT for event in log._active_events())
        current = await host.ext_method('mcp/apps/snapshot', request(session_id))
        assert current['bindingId'] == snapshot['bindingId']

        # Verify both the live adapter and recovery from the persisted audit log.
        for reload in (False, True):
            if reload:
                await host.load_session(str(tmp_path), session_id)
                current = await host.ext_method('mcp/apps/snapshot', request(session_id))
                params = {**params, 'bindingId': current['bindingId']}
            if outcome == 'unknown':
                with pytest.raises(RequestError, match='MCP_APP_OUTCOME_UNKNOWN'):
                    await host.ext_method('mcp/apps/call', params)
            else:
                assert await host.ext_method('mcp/apps/call', params) == first
            with pytest.raises(RequestError, match='MCP_APP_INTERACTION_CONFLICT'):
                await host.ext_method('mcp/apps/call', {**params, 'arguments': {'previous': 2}})
            assert [item['name'] for item in calls(record)] == ['list_flights', 'refresh_flights']

        # Audit receipts must not revive a card whose original turn was removed.
        await host.prompt(session_id, [{'type': 'text', 'text': 'Replace original question'}],
            turn_id='replace-original', history_edit={'target_turn_id': 'initial-turn', 'expected_revision_id': 'edited-turn'})
        with pytest.raises(RequestError, match='MCP_APP_SNAPSHOT_UNAVAILABLE'):
            await host.ext_method('mcp/apps/call', params)
        assert len(calls(record)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["new", "legacy"])
async def test_saved_result_survives_restart_and_reauthorization_without_reexecuting(
    tmp_path, monkeypatch, legacy,
):
    async with environment(tmp_path, monkeypatch) as (host, _, _, backend, session_id, record):
        session_log = host._sessions[session_id].agent.session_log
        saved = deepcopy(next(iter(session_log.events_of_type(SNAPSHOT_EVENT)))["data"])
        assert not {"serverIdentity", "serverOrigin", "connectorId"}.intersection(saved)
        if legacy:
            saved["toolId"] = "mcp:flight-fixture/list_flights"  # Pre-namespace display alias.
            session_log.append(SNAPSHOT_EVENT, {**saved, "serverIdentity": "old-grant",
                "connectorId": "old-connector", "serverOrigin": "https://previous.example"}, ignorable=True)
            session_log.flush()
        old = await host.ext_method("mcp/apps/snapshot", request(session_id))
        await backend.disconnect()
        await host.load_session(str(tmp_path), session_id)
        with pytest.raises(RequestError, match="APP_SERVER_UNAVAILABLE"):
            await host.ext_method("mcp/apps/snapshot", request(session_id))
        replacement = mcp_loader.MCPServerConnection(backend.name, command=backend.command,
            args=backend.args, env=backend.env, headers={"Authorization": "Bearer renewed-fixture"},
            connect_timeout=10)
        monkeypatch.setattr(mcp_loader, "_mcp_connections", [replacement])
        try:
            assert await replacement.connect(), replacement.last_error
            mcp_loader._replace_server_catalog(replacement)
            current = await host.ext_method("mcp/apps/snapshot", request(session_id))
            assert current["bindingId"] != old["bindingId"]
            assert current["arguments"] == saved["arguments"]
            assert current["result"] == saved["result"]
            with pytest.raises(RequestError, match="BINDING_EXPIRED"):
                await host.ext_method("mcp/apps/resource", request(session_id,
                    bindingId=old["bindingId"], uri=old["resourceUri"]))
            resource = await host.ext_method("mcp/apps/resource", request(session_id,
                bindingId=current["bindingId"], uri=current["resourceUri"]))
            assert resource["contents"][0]["mimeType"] == "text/html;profile=mcp-app"
            assert [i["name"] for i in calls(record)] == ["list_flights"]
            log = host._sessions[session_id].agent.session_log
            log.append("surface/reset", {})
            with pytest.raises(RequestError, match="MCP_APP_SNAPSHOT_UNAVAILABLE"):
                await host.ext_method("mcp/apps/snapshot", request(session_id))
        finally:
            await replacement.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_page", [False, True])
async def test_history_loads_original_page_after_last_tool_is_removed(tmp_path, monkeypatch, missing_page):
    async with environment(tmp_path, monkeypatch) as (host, _, _, backend, session_id, record):
        backend.tools.clear()
        mcp_loader._replace_server_catalog(backend)
        snapshot = await host.ext_method("mcp/apps/snapshot", request(session_id))
        params = request(session_id, bindingId=snapshot["bindingId"], uri=snapshot["resourceUri"])
        if missing_page:
            async def missing(uri):
                raise McpError(types.ErrorData(code=-32002, message="Resource not found"))
            monkeypatch.setattr(backend.session, "read_resource", missing)
            with pytest.raises(RequestError, match="APP_RESOURCE_NOT_FOUND"):
                await host.ext_method("mcp/apps/resource", params)
        else:
            page = await host.ext_method("mcp/apps/resource", params)
            assert page["contents"][0]["uri"] == snapshot["resourceUri"]
            assert snapshot["result"]["structuredContent"]["revision"] == 1
        assert [item["name"] for item in calls(record)] == ["list_flights"]


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_history", [False, True])
async def test_history_open_waits_for_target_publication_and_rechecks_history(tmp_path, monkeypatch, edit_history):
    async with environment(tmp_path, monkeypatch) as (host, _, _, backend, session_id, record):
        release = asyncio.Event()
        async def publish():
            await release.wait()
            mcp_loader._mcp_connections.append(backend)
        monkeypatch.setattr(mcp_loader, "_mcp_connections", [])
        task = asyncio.create_task(publish())
        monkeypatch.setattr(mcp_loader, "_mcp_initial_connections", {backend.name: task})
        opening = asyncio.create_task(host.ext_method("mcp/apps/snapshot", request(session_id)))
        try:
            await asyncio.sleep(0)
            assert not opening.done()
            if edit_history:
                host._sessions[session_id].agent.session_log.append("surface/reset", {})
            release.set()
            if edit_history:
                with pytest.raises(RequestError, match="MCP_APP_SNAPSHOT_UNAVAILABLE"):
                    await opening
            else:
                assert (await opening)["result"]["structuredContent"]["revision"] == 1
        finally:
            release.set()
            await asyncio.gather(task, opening, return_exceptions=True)
        assert len(calls(record)) == 1


@pytest.mark.asyncio
async def test_history_open_renews_changed_hosted_login_without_a_prompt(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, _, llm, backend, session_id, record):
        first = await host.ext_method("mcp/apps/snapshot", request(session_id))
        login_file = tmp_path / "auth.json"
        def login(token):
            login_file.write_text(json.dumps({"access_token": token, "office_identity": "fixture"}))
        login("fixture-before")
        auth = mcp_loader.DynamicBearerAuth(auth_file=str(login_file))
        auth.app_scope = auth.current_app_scope()
        monkeypatch.setattr(backend, "auth", auth)
        login("fixture-after")
        renewals = []
        async def reconnect(name, *, expected_session):
            assert expected_session is backend.session
            renewals.append(name)
            auth.app_scope = auth.current_app_scope()
            mcp_loader._replace_server_catalog(backend)
            return {"success": True}
        monkeypatch.setattr(mcp_loader, "reconnect_mcp_server", reconnect)
        stale = request(session_id, bindingId=first["bindingId"], uri=first["resourceUri"])
        with pytest.raises(RequestError, match="APP_SERVER_CHANGED"):
            await host.ext_method("mcp/apps/resource", stale)
        assert not renewals
        current = await host.ext_method("mcp/apps/snapshot", request(session_id))
        assert current["bindingId"] != first["bindingId"] and current["result"] == first["result"]
        page = await host.ext_method("mcp/apps/resource", {**stale, "bindingId": current["bindingId"]})
        assert page["contents"][0]["uri"] == first["resourceUri"]
        assert renewals == [backend.name] and len(llm.requests) == 2 and len(calls(record)) == 1


@pytest.mark.asyncio
async def test_repeated_session_reload_never_revives_old_app_bindings(tmp_path, monkeypatch):
    import box_agent.acp.mcp_apps as acp_apps

    # Simulate address reuse deterministically, without depending on Python's allocator.
    monkeypatch.setattr(acp_apps, 'id', lambda value: 1, raising=False)
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        expired = []
        for _ in range(3):
            assert await host.ext_method('mcp/apps/snapshot', request(session_id)) == snapshot
            expired.append(snapshot['bindingId'])
            await host.load_session(str(tmp_path), session_id)
            snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
            for binding in expired:
                with pytest.raises(RequestError, match='MCP_APP_BINDING_EXPIRED'):
                    await host.ext_method('mcp/apps/resource', request(session_id,
                        bindingId=binding, uri=snapshot['resourceUri']))
                with pytest.raises(RequestError, match='MCP_APP_BINDING_EXPIRED'):
                    await host.ext_method('mcp/apps/call', request(session_id,
                        bindingId=binding, interactionId='stale-write', name='refresh_flights',
                        arguments={'previous': 1}))
            resource = await host.ext_method('mcp/apps/resource', request(session_id,
                bindingId=snapshot['bindingId'], uri=snapshot['resourceUri']))
            assert resource['contents'][0]['mimeType'] == 'text/html;profile=mcp-app'
            assert [item['name'] for item in calls(record)] == ['list_flights']
        result = await host.ext_method('mcp/apps/call', request(session_id,
            bindingId=snapshot['bindingId'], interactionId='current-write', name='refresh_flights',
            arguments={'previous': 1}))
        assert result['structuredContent']['revision'] == 2
        assert [item['name'] for item in calls(record)] == ['list_flights', 'refresh_flights']


@pytest.mark.asyncio
async def test_request_connector_context_preserves_session_and_existing_card_binding(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        original = await host.ext_method('mcp/apps/snapshot', request(session_id))
        state = host._sessions[session_id]
        nonce = state._mcp_apps_nonce
        controls = (state.thinking_enabled, state.execution_profile)
        messages = deepcopy(state.agent.messages)
        monkeypatch.setattr(backend, 'connector_id', 'travel')
        context = {'selected_connector_ids': ['travel'], 'connector_statuses': [
            {'id': 'travel', 'name': 'Travel', 'status': 'connected'}]}
        # Read-only restoration also works while the next turn is preparing.
        state._prompt_in_progress = True
        try:
            first = await host.ext_method('mcp/apps/snapshot', request(session_id, connectorContext=context))
            second_context = {**context, 'selected_connector_ids': ['travel', 'other']}
            second = await host.ext_method('mcp/apps/snapshot', request(session_id, connectorContext=second_context))
            assert first == second == original
            resource = await host.ext_method('mcp/apps/resource', request(session_id,
                connectorContext=second_context, bindingId=original['bindingId'], uri=original['resourceUri']))
            assert resource['contents'][0]['uri'] == original['resourceUri']
        finally:
            state._prompt_in_progress = False
        result = await host.ext_method('mcp/apps/call', request(session_id,
            connectorContext=context, bindingId=original['bindingId'], interactionId='context-refresh',
            name='refresh_flights', arguments={'previous': 1}))
        assert result['structuredContent']['revision'] == 2
        assert host._sessions[session_id] is state
        assert state._mcp_apps_nonce == nonce
        assert (state.thinking_enabled, state.execution_profile) == controls
        assert state.selected_connector_ids == set()
        assert state.connector_statuses is None
        assert state.agent.messages == messages
        assert len(llm.requests) == 2
        # A successful App request never grants the model or subsequent requests access.
        with pytest.raises(RequestError, match='APP_CONNECTOR_DISABLED'):
            await host.ext_method('mcp/apps/snapshot', request(session_id))
        state.selected_connector_ids.add('travel')
        for denied in [
            {**context, 'selected_connector_ids': []},
            {**context, 'connector_statuses': [{'id': 'travel', 'name': 'Travel', 'status': 'disconnected'}]},
            {**context, 'connector_status_unavailable': True},
            {'selected_connector_ids': ['travel']},
        ]:
            for method, fields in [
                ('snapshot', {}),
                ('resource', {'bindingId': original['bindingId'], 'uri': original['resourceUri']}),
                ('call', {'bindingId': original['bindingId'], 'interactionId': 'denied',
                          'name': 'refresh_flights', 'arguments': {'previous': 1}}),
            ]:
                with pytest.raises(RequestError, match='APP_CONNECTOR_DISABLED'):
                    await host.ext_method('mcp/apps/' + method, request(session_id, connectorContext=denied, **fields))
        assert [item['name'] for item in calls(record)] == ['list_flights', 'refresh_flights']
        # Existing hosts without the new field retain session authorization.
        assert await host.ext_method('mcp/apps/snapshot', request(session_id)) == original


@pytest.mark.asyncio
async def test_malformed_app_connector_context_never_falls_back_to_session_grants(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        monkeypatch.setattr(backend, 'connector_id', 'travel')
        host._sessions[session_id].selected_connector_ids.add('travel')
        for context in [None, [], {}, {'selected_connector_ids': 'travel'},
                        {'selected_connector_ids': ['travel'], 'connector_statuses': 'connected'},
                        {'selected_connector_ids': ['travel'], 'connector_status_unavailable': 'false'}]:
            with pytest.raises(RequestError, match='MCP_APP_INVALID_REQUEST'):
                await host.ext_method('mcp/apps/snapshot', request(session_id, connectorContext=context))
        assert len(calls(record)) == 1
