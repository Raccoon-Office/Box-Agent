from types import SimpleNamespace
import asyncio

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.schema import Message
from box_agent.session_assembly import finish_session
from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL, SkillLoader


def loader_at(tmp_path):
    path = tmp_path / 'skills' / 'demo' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: demo\ndescription: demo guide\n---\nbody\n', encoding='utf-8')
    loader = SkillLoader(sources=[(tmp_path / 'skills', 'user')], skill_settings_path=tmp_path / 'settings.json')
    loader.discover_skills()
    return loader


@pytest.mark.asyncio
@pytest.mark.parametrize('stable', [False, True])
async def test_session_assembly_uses_shared_skill_update_for_expert(tmp_path, stable):
    loader = loader_at(tmp_path)
    calls = []
    agent = SimpleNamespace(stable_prefix=stable, messages=[Message(role='system', content=f'BASE\n{SKILL_SLOT_SENTINEL}')], tools={})
    def set_prompt(prompt, **kwargs):
        agent.messages[0].content = prompt
    def update(selector, text, *, refresh):
        calls.append(text)
        if stable:
            selector.render_catalog(text, refresh=refresh)
        else:
            prompt = selector.update(text, refresh=refresh)
            if prompt is not None:
                set_prompt(prompt)
    agent.set_system_prompt = set_prompt
    agent.update_skill_catalog = update
    host = SimpleNamespace(tools=None, system_prompt=None, skill_catalog_filter=None)
    resources = SimpleNamespace(memory_manager=None, context=SimpleNamespace(host=host), skill_loader=loader,
                                state={'expert_context': SimpleNamespace(skill_query=lambda: 'demo')})
    session = SimpleNamespace(agent=agent, skill_selector=None)
    await finish_session(session, resources)
    assert calls == ['demo']
    assert session.skill_selector.matched_skill_names == ('demo',)
    assert SKILL_SLOT_SENTINEL not in agent.messages[0].content
    assert ('Available Skills' in agent.messages[0].content) is not stable


@pytest.mark.parametrize('status', ['ready', 'connected'])
def test_stable_mcp_update_instructs_routed_call(status):
    agent = BoxACPAgent.__new__(BoxACPAgent)
    session = SimpleNamespace(utility_session=False, turn_active=True, mcp_runtime_updates_requested=True,
                              agent=SimpleNamespace(stable_prefix=True), inject_queue=asyncio.Queue())
    agent._sessions = {'session': session}
    assert agent._inject_mcp_runtime_update(name='demo', state=status, tool_count=2, always_load_count=1) == 1
    update = session.inject_queue.get_nowait()
    assert update['source'] == 'runtime'
    assert 'tool_ref' in update['content'] and 'call_tool' in update['content']
    assert 'real name' not in update['content']
