"""Offline public-Agent checks of the stable tool and Skill request prefix."""
import json

import pytest

from box_agent.agent import Agent
from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.llm.openai_client import OpenAIClient
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.session_log import SessionLog
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.skill_loader import SkillLoader, SkillSelector
from box_agent.tools.skill_tool import GetSkillTool


class MemorySearch(Tool):
    name = 'memory_search'
    description = 'Search test records.'
    parameters = {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}

    async def execute(self, query):
        return ToolResult(success=True, content=f'FOUND:{query}')


class ScriptedModel:
    model = 'offline-prefix'
    max_output_tokens = 512

    def __init__(self):
        self.requests = []
        self.log = None
        self.durable_runtime = []

    def estimate_text_tokens(self, text):
        return len(text.encode('utf-8')) // 3

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append(([message.model_copy(deep=True) for message in messages], list(tools or [])))
        self.durable_runtime.append([
            event["data"].get("content") for event in self.log.events
            if event["type"] == "user/message" and event["data"].get("source") == "runtime"
        ])
        n = len(self.requests)
        if n == 1:
            call = ToolCall(id='search-1', type='function', function=FunctionCall(
                name='tool_search', arguments={'tool_names': ['memory_search']}))
        elif n == 2:
            result = next(m for m in messages if m.role == 'tool' and m.tool_call_id == 'search-1')
            ref = json.loads(result.content)['activated'][0]['tool_ref']
            call = ToolCall(id='routed-1', type='function', function=FunctionCall(
                name='call_tool', arguments={'tool_ref': ref, 'arguments_json': json.dumps({'query': '中文'}, ensure_ascii=False)}))
        elif n == 3:
            call = ToolCall(id='skill-1', type='function', function=FunctionCall(
                name='get_skill', arguments={'skill_name': 'other'}))
        else:
            yield StreamEvent(type='text', delta=f'done-{n}')
            yield StreamEvent(type='finish', finish_reason='stop')
            return
        yield StreamEvent(type='finish', finish_reason='tool_use', tool_calls=[call])


def make_loader(tmp_path):
    for name in ('demo', 'other'):
        path = tmp_path / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\ndescription: {name} guide\n---\nBODY_{name}\n', encoding='utf-8')
    loader = SkillLoader(sources=[(tmp_path / 'skills', 'user')], skill_settings_path=tmp_path / 'settings.json')
    loader.discover_skills()
    return loader


def dump(messages):
    return [message.model_dump(mode='json') for message in messages]


def assert_batches_complete(messages):
    outstanding = set()
    for message in messages:
        if message.role == 'assistant' and message.tool_calls:
            assert not outstanding
            outstanding = {call.id for call in message.tool_calls}
        elif message.role == 'tool':
            assert message.tool_call_id in outstanding
            outstanding.remove(message.tool_call_id)
        elif outstanding:
            pytest.fail('non-tool message inserted inside an assistant tool batch')
    assert not outstanding


@pytest.mark.asyncio
async def test_stable_public_agent_keeps_wire_prefix_and_replays_skill_tail(tmp_path):
    loader = make_loader(tmp_path)
    selector = SkillSelector(loader)
    model = ScriptedModel()
    log = SessionLog.create(tmp_path / 'sessions', session_id='stable-prefix', cwd=tmp_path)
    try:
        model.log = log
        agent = Agent(llm_client=model, system_prompt='BASE', tools=[MemorySearch(), GetSkillTool(loader)],
                      skill_runtime=SkillRuntime(loader), session_log=log, workspace_dir=str(tmp_path),
                      deferred_mcp_loading_enabled=False, tool_interface_version=2, max_steps=8)
        agent.update_skill_catalog(selector, 'demo')
        agent.add_user_message('first task demo')
        _ = [event async for event in agent.run_events()]
        for index in range(8):
            if index == 0:
                agent.skill_runtime.select(['demo'])
                agent.update_skill_catalog(selector, 'other')
            if index == 2:
                (tmp_path / 'skills' / 'other' / 'SKILL.md').unlink()
                loader.discover_skills()
                agent.update_skill_catalog(selector, '')
            agent.add_user_message(f'continue {index}')
            _ = [event async for event in agent.run_events()]
        assert len(model.requests) >= 12
        first_messages, first_tools = model.requests[0]
        first_schemas = [tool.to_schema() for tool in first_tools]
        assert {'tool_search', 'call_tool', 'get_skill'} <= {schema['name'] for schema in first_schemas}
        assert 'memory_search' not in {schema['name'] for schema in first_schemas}
        for index, (messages, tools) in enumerate(model.requests):
            assert messages[0].content == first_messages[0].content
            assert [tool.to_schema() for tool in tools] == first_schemas
            assert_batches_complete(messages)
            runtime_contents = [m.content for m in messages if m.role == 'user' and m.source == 'runtime']
            assert all(content in model.durable_runtime[index] for content in runtime_contents)
            if index:
                previous = model.requests[index - 1][0]
                assert dump(messages[:len(previous)]) == dump(previous)
        all_messages = model.requests[-1][0]
        assert any(m.role == 'assistant' and m.tool_calls and m.tool_calls[0].function.name == 'call_tool' for m in all_messages)
        result = next(m for m in all_messages if m.role == 'tool' and m.tool_call_id == 'routed-1')
        assert result.name == 'memory_search' and result.content == 'FOUND:中文'
        assert any(m.role == 'tool' and m.tool_call_id == 'skill-1' and 'BODY_other' in m.content for m in all_messages)
        assert any(m.role == 'user' and m.source == 'runtime' and 'BODY_demo' in str(m.content) for m in all_messages)
        catalogs = [m.content for m in all_messages if m.role == 'user' and m.source == 'runtime'
                    and isinstance(m.content, str) and m.content.startswith('[Host Skill catalog:')]
        assert len(catalogs) >= 2
        assert any('other' in catalog for catalog in catalogs[:-1])
        assert '"name": "other"' not in catalogs[-1]
        replay = log.replay().messages
        assert [m.content for m in replay if m.role == 'user' and m.source == 'runtime'] == [
            m.content for m in agent.messages if m.role == 'user' and m.source == 'runtime']
        for provider in (OpenAIClient, AnthropicClient):
            converter = provider.__new__(provider)
            baseline_tools = converter._convert_tools(first_tools)
            baseline_system, _ = converter._convert_messages(first_messages)
            previous_wire = None
            for messages, tools in model.requests:
                system, wire = converter._convert_messages(messages)
                assert system == baseline_system
                assert converter._convert_tools(tools) == baseline_tools
                if previous_wire:
                    if provider is AnthropicClient and previous_wire[-1]['role'] == 'user':
                        assert wire[:len(previous_wire) - 1] == previous_wire[:-1]
                        old, new = previous_wire[-1], wire[len(previous_wire) - 1]
                        assert old['role'] == new['role']
                        if isinstance(old['content'], list):
                            assert new['content'][:len(old['content'])] == old['content']
                        else:
                            assert new['content'] == old['content']
                    else:
                        assert wire[:len(previous_wire)] == previous_wire
                previous_wire = wire
                expected_ids = {m.tool_call_id for m in messages if m.role == 'tool'}
                if provider is OpenAIClient:
                    calls = [call for item in wire for call in item.get('tool_calls', [])]
                    results = [item['tool_call_id'] for item in wire if item['role'] == 'tool']
                    assert {call['id'] for call in calls} == expected_ids == set(results)
                    if 'routed-1' in expected_ids:
                        assert next(call for call in calls if call['id'] == 'routed-1')['function']['name'] == 'call_tool'
                else:
                    blocks = [block for item in wire if isinstance(item['content'], list)
                              for block in item['content']]
                    calls = [block for block in blocks if block.get('type') == 'tool_use']
                    results = [block['tool_use_id'] for block in blocks if block.get('type') == 'tool_result']
                    assert {call['id'] for call in calls} == expected_ids == set(results)
                    if 'routed-1' in expected_ids:
                        assert next(call for call in calls if call['id'] == 'routed-1')['name'] == 'call_tool'
    finally:
        log.close()


@pytest.mark.asyncio
async def test_host_required_tool_bootstrap_routes_after_durable_descriptor(tmp_path):
    class BootstrapModel(ScriptedModel):
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(([m.model_copy(deep=True) for m in messages], list(tools or [])))
            if len(self.requests) == 1:
                descriptor = next(m for m in messages if m.role == 'user' and m.source == 'runtime'
                                  and m.name == 'tool_descriptions')
                assert any(event['type'] == 'user/message' and event['data'].get('content') == descriptor.content
                           for event in self.log.events)
                ref = json.loads(descriptor.content)['tool_ref']
                yield StreamEvent(type='finish', finish_reason='tool_use', tool_calls=[ToolCall(
                    id='bootstrap-call', type='function', function=FunctionCall(
                        name='call_tool', arguments={'tool_ref': ref, 'arguments_json': '{"query":"bootstrap"}'}))])
            else:
                yield StreamEvent(type='text', delta='done')
                yield StreamEvent(type='finish', finish_reason='stop')
    model = BootstrapModel()
    log = SessionLog.create(tmp_path / 'sessions', session_id='bootstrap', cwd=tmp_path)
    model.log = log
    try:
        agent = Agent(llm_client=model, system_prompt='BASE', tools=[MemorySearch()], session_log=log,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False,
                      tool_interface_version=2, max_steps=4)
        agent.local_tool_exposure.require_tools(['memory_search'])
        agent.add_user_message('run required capability')
        _ = [event async for event in agent.run_events()]
        assert len(model.requests) == 2
        result = next(m for m in model.requests[1][0] if m.tool_call_id == 'bootstrap-call')
        assert result.name == 'memory_search' and result.content == 'FOUND:bootstrap'
        assert [tool.to_schema() for tool in model.requests[0][1]] == [
            tool.to_schema() for tool in model.requests[1][1]]
        assert_batches_complete(model.requests[1][0])
    finally:
        log.close()


@pytest.mark.asyncio
async def test_reopened_session_rejects_old_ref_and_bootstraps_fresh_ref(tmp_path):
    executions = []

    class RecordingSearch(MemorySearch):
        async def execute(self, query):
            executions.append(query)
            return await super().execute(query)

    class FirstModel(ScriptedModel):
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(([m.model_copy(deep=True) for m in messages], list(tools or [])))
            yield StreamEvent(type='text', delta='first complete')
            yield StreamEvent(type='finish', finish_reason='stop')

    root = tmp_path / 'sessions'
    first_log = SessionLog.create(root, session_id='resume-v2', cwd=tmp_path)
    model = FirstModel()
    try:
        agent = Agent(llm_client=model, system_prompt='BASE', tools=[RecordingSearch()], session_log=first_log,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, tool_interface_version=2)
        agent.local_tool_exposure.require_tools(['memory_search'])
        agent.add_user_message('first')
        _ = [event async for event in agent.run_events()]
        old_descriptor = next(m for m in model.requests[0][0]
                              if m.role == 'user' and m.source == 'runtime' and m.name == 'tool_descriptions')
        old_ref = json.loads(old_descriptor.content)['tool_ref']
    finally:
        first_log.close()

    class ResumedModel(ScriptedModel):
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(([m.model_copy(deep=True) for m in messages], list(tools or [])))
            n = len(self.requests)
            descriptions = [m for m in messages if m.role == 'user' and m.source == 'runtime'
                            and m.name == 'tool_descriptions']
            current = json.loads(descriptions[-1].content)
            assert current['name'] == 'memory_search' and current['input_schema'] == MemorySearch.parameters
            assert current['tool_ref'] != old_ref
            if n == 1:
                ref, query = old_ref, 'must-not-run'
            elif n == 2:
                assert executions == []
                error = next(m for m in messages if m.role == 'tool' and m.tool_call_id == 'expired')
                assert 'search' in error.content.lower() or 'discover' in error.content.lower()
                ref, query = current['tool_ref'], 'fresh'
            else:
                yield StreamEvent(type='text', delta='complete')
                yield StreamEvent(type='finish', finish_reason='stop')
                return
            yield StreamEvent(type='finish', finish_reason='tool_use', tool_calls=[ToolCall(
                id='expired' if n == 1 else 'fresh', type='function', function=FunctionCall(
                    name='call_tool', arguments={'tool_ref': ref, 'arguments_json': json.dumps({'query': query})}))])

    reopened = SessionLog.open(root, session_id='resume-v2', cwd=tmp_path)
    provider = ResumedModel()
    try:
        resumed = Agent(llm_client=provider, system_prompt='BASE', tools=[RecordingSearch()], session_log=reopened,
                        workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, tool_interface_version=2,
                        max_steps=5)
        resumed.local_tool_exposure.require_tools(['memory_search'])
        resumed.add_user_message('resume')
        _ = [event async for event in resumed.run_events()]
        assert executions == ['fresh']
        assert len(provider.requests) == 3
        first_messages, first_tools = provider.requests[0]
        assert any(m.role == 'user' and m.source == 'runtime' and m.name == 'tool_context'
                   and 'expired' in str(m.content).lower() for m in first_messages)
        new_descriptor = [m for m in first_messages if m.role == 'user' and m.name == 'tool_descriptions'][-1]
        assert new_descriptor.content != old_descriptor.content
        assert any(m.role == 'user' and m.source == 'runtime' and m.content == new_descriptor.content
                   for m in reopened.replay().messages)
        for messages, tools in provider.requests:
            assert messages[0].content == first_messages[0].content
            assert [tool.to_schema() for tool in tools] == [tool.to_schema() for tool in first_tools]
            assert_batches_complete(messages)
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_compaction_redelivers_required_tool_description_at_request_tail(tmp_path):
    from dataclasses import replace
    from box_agent.schema import LLMResponse, Message, TokenUsage

    class Summary:
        def estimate_text_tokens(self, text):
            return len(text.encode('utf-8')) // 3

        async def generate(self, **kwargs):
            return LLMResponse(content='<summary>older turns</summary>', finish_reason='stop')

        async def generate_stream(self, **kwargs):
            yield StreamEvent(type='text', delta='<summary>older turns</summary>')
            yield StreamEvent(type='finish', finish_reason='stop')

    class Done(ScriptedModel):
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(([m.model_copy(deep=True) for m in messages], list(tools or [])))
            yield StreamEvent(type='text', delta='done')
            yield StreamEvent(type='finish', finish_reason='stop')

    log = SessionLog.create(tmp_path / 'sessions', session_id='compact-bootstrap', cwd=tmp_path)
    model = Done()
    try:
        agent = Agent(llm_client=model, system_prompt='BASE', tools=[MemorySearch()], session_log=log,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False,
                      tool_interface_version=2, max_steps=2, token_limit=8000)
        agent.local_tool_exposure.require_tools(['memory_search'])
        agent.add_user_message('first')
        _ = [event async for event in agent.run_events()]
        initial = model.requests[0][0]
        descriptor = next(m.content for m in initial if m.name == 'tool_descriptions')
        for index in range(24):
            agent.messages.append(Message(role='user' if index % 2 == 0 else 'assistant',
                                          content=f'old-{index}:' + 'x' * 200))
        agent.messages[-1].usage = TokenUsage(prompt_tokens=8000, total_tokens=8000)
        agent.add_user_message('latest')
        options = replace(agent.default_run_options(), summary_llm=Summary())
        run_events = [event async for event in agent.run_events(options=options)]
        assert len(model.requests) == 2, str(run_events)
        assert any(event['type'] == 'compaction/end' for event in log.events)
        final, tools = model.requests[-1]
        assert any('older turns' in str(m.content) for m in final)
        descriptions = [m for m in final if m.role == 'user' and m.source == 'runtime'
                        and m.name == 'tool_descriptions']
        assert len(descriptions) == 1 and descriptions[0].content == descriptor
        assert final[-1] == descriptions[0]
        assert any(m.name == 'tool_descriptions' and m.content == descriptor for m in log.replay().messages)
        assert final[0].content == initial[0].content
        assert [tool.to_schema() for tool in tools] == [tool.to_schema() for tool in model.requests[0][1]]
    finally:
        log.close()
