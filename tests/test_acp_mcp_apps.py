"""MCP Apps retain host-only data and run UI callbacks without model turns."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest
from acp import PROTOCOL_VERSION, RequestError

from box_agent.acp import BoxACPAgent
from box_agent.acp.mcp_apps import McpAppsACP, SNAPSHOT_EVENT, INTERACTION_EVENT
from box_agent.config import Config, AgentConfig, LLMConfig, ToolsConfig, MCPConfig
from box_agent.schema import StreamEvent, ToolCall, FunctionCall
from box_agent.tools import mcp_loader
from box_agent.tools.mcp_apps import set_mcp_apps_host_capability

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
            description = json.loads(discovery.content)['activated'][0]['description']
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
@pytest.mark.parametrize("change", ["unchanged", "env", "headers"])
async def test_old_app_reopens_only_after_reconnect_to_same_backend(tmp_path, monkeypatch, change):
    async with environment(tmp_path, monkeypatch) as (host, conn, llm, backend, session_id, record):
        snapshot = await host.ext_method('mcp/apps/snapshot', request(session_id))
        env, headers = dict(backend.env), dict(backend.headers)
        if change == 'env':
            env['DB_PATH'] = str(tmp_path / 'other.sqlite')
        elif change == 'headers':
            headers['X-Tenant'] = 'other-tenant'
        await backend.disconnect()
        replacement = mcp_loader.MCPServerConnection(backend.name, command=backend.command,
            args=backend.args, env=env, headers=headers, connect_timeout=10)
        monkeypatch.setattr(mcp_loader, '_mcp_connections', [replacement])
        try:
            assert await replacement.connect(), replacement.last_error
            mcp_loader._replace_server_catalog(replacement)
            params = request(session_id, bindingId=snapshot['bindingId'], interactionId='after-reconnect',
                             name='refresh_flights', arguments={'previous': 1})
            if change == 'unchanged':
                with pytest.raises(RequestError, match='MCP_APP_BINDING_EXPIRED'):
                    await host.ext_method('mcp/apps/call', params)
                restored = await host.ext_method('mcp/apps/snapshot', request(session_id))
                assert restored['result'] == snapshot['result']
                await host.ext_method('mcp/apps/call', {**params, 'bindingId': restored['bindingId']})
                assert [item['name'] for item in calls(record)] == ['list_flights', 'refresh_flights']
            else:
                with pytest.raises(RequestError, match='APP_SERVER_CHANGED'):
                    await host.ext_method('mcp/apps/snapshot', request(session_id))
                with pytest.raises(RequestError, match='APP_SERVER_CHANGED'):
                    await host.ext_method('mcp/apps/call', params)
                assert [item['name'] for item in calls(record)] == ['list_flights']
        finally:
            await replacement.disconnect()


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
