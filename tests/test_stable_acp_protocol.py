"""Offline ACP acceptance of the default v2 routing and Skill context."""
import json
from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, ToolCall
from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL, SkillLoader
from box_agent.tools.skill_tool import GetSkillTool
from tests.acp_helpers import acp_params
from tests.test_acp import DummyConn


class PlanModel:
    model = 'offline-acp'
    max_output_tokens = 512

    def __init__(self):
        self.requests = []

    def estimate_text_tokens(self, text):
        return len(text.encode('utf-8')) // 3

    async def generate(self, messages, tools=None, **kwargs):
        return LLMResponse(content='general', finish_reason='stop')

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append(([m.model_copy(deep=True) for m in messages], list(tools or [])))
        if len(self.requests) == 1:
            call = ToolCall(id='discover-plan', type='function', function=FunctionCall(
                name='tool_search', arguments={'tool_names': ['plan_write']}))
        elif len(self.requests) == 2:
            search = next(m for m in messages if m.role == 'tool' and m.tool_call_id == 'discover-plan')
            target = next(row for row in json.loads(search.content)['activated'] if row['name'] == 'plan_write')
            arguments = {'action': 'set', 'title': 'Migration plan', 'objective': 'Verify migration',
                         'steps': [{'title': 'Check the result'}]}
            call = ToolCall(id='plan-v2', type='function', function=FunctionCall(
                name='call_tool', arguments={'tool_ref': target['tool_ref'],
                                             'arguments_json': json.dumps(arguments)}))
        else:
            yield StreamEvent(type='text', delta='done')
            yield StreamEvent(type='finish', finish_reason='stop')
            return
        yield StreamEvent(type='finish', finish_reason='tool_use', tool_calls=[call])


@pytest.mark.asyncio
async def test_default_acp_v2_search_routes_plan_and_persists_skill_catalog(tmp_path):
    folder = tmp_path / 'skills' / 'demo'
    folder.mkdir(parents=True)
    folder.joinpath('SKILL.md').write_text(
        '---\nname: demo\ndescription: demo workflow\nkeywords: [demo]\n---\nDEMO_METHOD_BODY\n', encoding='utf-8')
    loader = SkillLoader(folder.parent)
    loader.discover_skills()
    config = Config(llm=LLMConfig(api_key='test'),
                    agent=AgentConfig(max_steps=5, workspace_dir=str(tmp_path)),
                    tools=ToolsConfig(enable_sub_agent=False, enable_todo=False, enable_mcp=False))
    model, conn = PlanModel(), DummyConn()
    host = BoxACPAgent(conn, config, model, [GetSkillTool(loader)],
                       f'BASE\n{SKILL_SLOT_SENTINEL}', skill_loader=loader)
    try:
        session = await host.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path), field_meta={})))
        state = host._sessions[session.sessionId]
        assert state.agent.tool_interface_version == 2
        await host.prompt(**acp_params(SimpleNamespace(sessionId=session.sessionId,
            prompt=[{'text': 'demo workflow'}], field_meta={})))
        assert len(model.requests) >= 2
        first, tools = model.requests[0]
        schemas = [tool.to_schema() for tool in tools]
        assert 'call_tool' in {item['name'] for item in schemas}
        assert 'plan_write' not in {item['name'] for item in schemas}
        for messages, current_tools in model.requests:
            assert messages[0].content == first[0].content
            assert [tool.to_schema() for tool in current_tools] == schemas
        catalogs = [m for m in first if m.role == 'user' and m.source == 'runtime'
                    and isinstance(m.content, str) and m.content.startswith('[Host Skill catalog:')]
        assert catalogs and 'demo' in catalogs[-1].content and 'DEMO_METHOD_BODY' not in catalogs[-1].content
        assert any(m.role == 'user' and m.source == 'runtime' and m.content == catalogs[-1].content
                   for m in state.agent.session_log.replay().messages)
        history = state.agent.messages
        assert any(m.role == 'assistant' and m.tool_calls and m.tool_calls[0].function.name == 'call_tool'
                   for m in history)
        assert any(m.role == 'tool' and m.tool_call_id == 'plan-v2' and m.name == 'plan_write' for m in history)
        updates = [u.update.rawOutput for u in conn.updates
                   if isinstance(getattr(u.update, 'rawOutput', None), dict)]
        assert any(item.get('type') == 'plan_snapshot' for item in updates)
    finally:
        await host.aclose()
