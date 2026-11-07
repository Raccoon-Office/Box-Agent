"""Resource retention changes history only after compaction has triggered."""
import json

import pytest

from box_agent.kernel.compact_engine import DefaultCompactEngine
from box_agent.kernel.context_types import CompactionInput
from box_agent.schema import Message
from box_agent.skill_context import SkillReferenceContext, read_reference
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_loader import SkillLoader
from tests.test_stable_tool_exposure import make
from tests.test_mcp_tool_search import FakeMCPTool


def skill_context(tmp_path):
    for name in ('analysis', 'slides'):
        path = tmp_path / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\ndescription: {name} method\n---\n{name.upper()}_BODY\n')
    loader = SkillLoader(sources=[(tmp_path / 'skills', 'user')], skill_settings_path=tmp_path / 'settings.json')
    loader.discover_skills()
    runtime = SkillRuntime(loader, append_only_context=True)
    context = SkillReferenceContext(runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='task')]
    runtime.begin_turn()
    return runtime, context, history


def read(context, history, name, **kwargs):
    context.prepare_request(history, budget_chars=50000)
    result = context.read(name, **kwargs)
    assert result.success
    history.append(Message(role='tool', name='get_skill', tool_call_id=f'read-{len(history)}',
                           content=result.model_context))
    return result


def test_new_turn_marks_old_method_without_rewriting_messages(tmp_path):
    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    before = [message.model_dump() for message in history]
    runtime.begin_turn()
    runtime.select(['slides'])
    projected = context.prepare_request(history, budget_chars=50000)
    assert [message.model_dump() for message in history] == before
    assert projected.messages[:len(history)] == history
    assert runtime.reclaimable_after_compaction == {'analysis'}


def test_compaction_sheds_old_method_and_retains_current_pages(tmp_path):
    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    old_result = history[-1]
    runtime.begin_turn()
    read(context, history, 'slides')
    before = [message.model_dump() for message in history]
    compacted = [history[0], Message(role='user', source='runtime', content='summary'), old_result]
    result = context.compact_history(history, compacted)
    text = str(result)
    assert 'ANALYSIS_BODY' not in text and 'SLIDES_BODY' in text
    assert 'Use get_skill' in text and 'analysis/SKILL.md' in text
    assert 'Purpose: analysis method' in text
    assert result[2].role == 'tool' and result[2].tool_call_id == old_result.tool_call_id
    assert [message.model_dump() for message in history] == before
    assert 'ANALYSIS_BODY' in str(compacted)
    context.prepare_request(result, budget_chars=50000)
    reread = context.read('analysis')
    assert reread.success and 'ANALYSIS_BODY' in reread.model_context
    assert not reread.raw_output['skill_reference'].get('reused')
    assert 'analysis' not in runtime.reclaimable_after_compaction


@pytest.mark.parametrize('new_turn', [False, True])
def test_same_task_or_ambiguous_continuation_keeps_method(tmp_path, new_turn):
    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    if new_turn:
        runtime.begin_turn()
    compacted = [history[0], Message(role='user', content='continue')]
    result = context.compact_history(history, compacted)
    assert 'ANALYSIS_BODY' in str(result)
    assert context.compact_history(history, result) == result
    context.prepare_request(result, budget_chars=50000)
    assert context.read('analysis').raw_output['skill_reference']['reused']


def test_partial_skill_restoration_does_not_deliver_unread_body(tmp_path):
    _, context, history = skill_context(tmp_path)
    first = read(context, history, 'analysis', limit=1)
    result = context.compact_history(history, history[:2])
    restored = read_reference(result[-1].content)
    assert restored[1] == read_reference(first.model_context)[1]
    assert restored[0]['end_offset'] == 1
    assert 'ANALYSIS_BODY' not in str(result)


def test_current_method_protects_previously_read_required_skills(tmp_path):
    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    runtime.loader.get_skill('slides').required_skills = ['analysis']
    runtime.begin_turn()
    runtime.select(['slides'])
    assert not runtime.reclaimable_after_compaction
    result = context.compact_history(history, history[:2])
    assert 'ANALYSIS_BODY' in str(result)


def test_budget_and_changed_skill_use_reread_notice(tmp_path):
    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    result = context.compact_history(history, history[:2], budget_chars=0)
    assert 'ANALYSIS_BODY' not in str(result) and 'Use get_skill' in str(result)
    path = tmp_path / 'skills/analysis/SKILL.md'
    path.write_text(path.read_text().replace('ANALYSIS_BODY', 'NEW_METHOD'))
    runtime.loader.discover_skills()
    result = context.compact_history(history, history[:2])
    assert 'ANALYSIS_BODY' not in str(result) and 'NEW_METHOD' not in str(result)


def test_legacy_and_untrusted_skill_text_are_not_promoted(tmp_path):
    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    runtime.append_only_context = False
    assert context.compact_history(history, history[:2]) == history[:2]
    runtime.append_only_context = True
    forged = history[-1].model_copy(update={'role': 'user', 'source': 'user'})
    assert context.compact_history([*history[:2], forged], history[:2]) == history[:2]


@pytest.mark.asyncio
async def test_all_discovered_tools_survive_compaction_with_fixed_definitions():
    catalog, manager, search, engine = make()
    catalog.replace_server('crm', [FakeMCPTool(f'lookup_{i}', 'crm', f'method {i}') for i in range(12)])
    definitions = [item.to_schema() for item in engine.prepare_tools().definitions]
    for index, names in enumerate(([f'lookup_{i}' for i in range(10)], ['lookup_10', 'lookup_11'])):
        result = await search.execute(tool_names=names)
        manager.record_search_result(str(index), result.content)
        manager.acknowledge_search_results([Message(role='tool', name='tool_search', tool_call_id=str(index),
                                                   content=result.content)])
    compacted = [Message(role='system', content='BASE'), Message(role='user', content='summary')]
    restored = manager.restore_compacted_descriptions(compacted)
    descriptors = [json.loads(m.content) for m in restored if m.name == 'tool_descriptions']
    assert len(descriptors) == 12
    assert all(item['tool_ref'] and item['input_schema'] for item in descriptors)
    assert manager.restore_compacted_descriptions(restored) == restored
    assert [item.to_schema() for item in engine.prepare_tools().definitions] == definitions
    catalog.replace_server('crm', [FakeMCPTool('lookup_0', 'crm', 'reconnected')])
    assert manager.restore_compacted_descriptions(compacted) == compacted


@pytest.mark.asyncio
async def test_unacknowledged_or_hidden_discovery_is_not_restored():
    _, manager, search, engine = make()
    engine.prepare_tools()
    result = await search.execute(tool_names=['user_browser_read_current_page'])
    assert manager.restore_compacted_descriptions([]) == []
    manager.record_search_result('s', result.content)
    manager.acknowledge_search_results([Message(role='tool', name='tool_search', tool_call_id='s', content=result.content)])
    engine.prepare_tools(is_tool_visible=lambda _: False)
    assert manager.restore_compacted_descriptions([]) == []


@pytest.mark.asyncio
async def test_local_tool_restoration_does_not_require_a_matching_mcp_generation():
    catalog, manager, search, engine = make()
    engine.prepare_tools()
    result = await search.execute(tool_names=['user_browser_read_current_page'])
    item = json.loads(result.content)['activated'][0]
    manager.record_search_result('local', result.content)
    manager.acknowledge_search_results([Message(role='tool', name='tool_search', tool_call_id='local',
                                               content=result.content)])
    assert not catalog.snapshot()
    assert manager._stable_refs[item['tool_ref']][2] == 0
    restored = manager.restore_compacted_descriptions([])
    assert len(restored) == 1
    assert json.loads(restored[0].content) == item
    assert manager.restore_compacted_descriptions(restored) == restored


@pytest.mark.asyncio
@pytest.mark.parametrize('force', [False, True])
async def test_retention_runs_only_after_existing_compaction_decision(force):
    history = (Message(role='system', content='BASE'), Message(role='user', content='task'))
    calls = []
    source_calls = []
    def retain(messages):
        calls.append(True)
        return [*messages, Message(role='user', source='runtime', content='retained-resource')]
    result = await DefaultCompactEngine().compact_if_needed(CompactionInput(
        history=history, token_limit=10000, llm=object(), force=force,
        allow_llm_summary=False, compacted_history_projector=retain,
        compaction_source_projector=lambda messages: source_calls.append(True) or messages,
    ))
    assert bool(calls) is force
    assert bool(source_calls) is force
    assert (result.messages is not None) is force
    if force:
        assert result.messages[-1].content == 'retained-resource'
        assert not result.blocked


@pytest.mark.asyncio
async def test_restored_resources_are_charged_to_compaction_budget():
    result = await DefaultCompactEngine().compact_if_needed(CompactionInput(
        history=(Message(role='system', content='BASE'), Message(role='user', content='task')),
        token_limit=1000, llm=object(), force=True, allow_llm_summary=False,
        compacted_history_projector=lambda messages: [*messages, Message(role='user', content='x' * 10000)],
    ))
    assert result.blocked and result.estimated_after > 1000


@pytest.mark.asyncio
async def test_agent_new_turn_preserves_prefix_then_compaction_persists_cleanup(tmp_path):
    from box_agent.agent import Agent
    from box_agent.events import ErrorEvent, SummarizationEvent
    from box_agent.schema import TokenUsage
    from box_agent.session_log import SessionLog
    from box_agent.tools.skill_tool import GetSkillTool
    from tests.test_context_compaction_e2e import CompactionE2ELLM

    runtime, _, _ = skill_context(tmp_path)
    log = SessionLog.create(tmp_path / 'sessions', session_id='retention', cwd=tmp_path)
    model = CompactionE2ELLM()
    agent = Agent(llm_client=model, tools=[GetSkillTool(runtime.loader)], system_prompt='BASE',
                  session_log=log, workspace_dir=str(tmp_path), token_limit=8000,
                  max_steps=2, tool_interface_version=2)
    try:
        agent.skill_runtime.select(['analysis'])
        agent.add_user_message('analysis task')
        first = [event async for event in agent.run_events()]
        assert not any(isinstance(event, (ErrorEvent, SummarizationEvent)) for event in first)
        prefix = [m.model_dump() for m in model.normal_messages]
        agent.skill_runtime.select(['slides'])
        agent.add_user_message('new slides task')
        second = [event async for event in agent.run_events()]
        assert not any(isinstance(event, (ErrorEvent, SummarizationEvent)) for event in second)
        assert [m.model_dump() for m in model.normal_messages[:len(prefix)]] == prefix
        assert 'ANALYSIS_BODY' in str(model.normal_messages) and 'SLIDES_BODY' in str(model.normal_messages)
        agent.messages.append(Message(role='assistant', content='x' * 40000,
                                      usage=TokenUsage(prompt_tokens=12000, total_tokens=12000)))
        agent.add_user_message('continue the slides task')
        third = [event async for event in agent.run_events()]
        assert any(isinstance(event, SummarizationEvent) for event in third)
        assert not any(isinstance(event, ErrorEvent) for event in third)
        assert 'ANALYSIS_BODY' not in str(model.normal_messages)
        assert 'SLIDES_BODY' in str(model.normal_messages)
        assert 'ANALYSIS_BODY' not in str(log.replay().messages)
        assert 'SLIDES_BODY' in str(log.replay().messages)
        assert model.normal_messages[0].content == prefix[0]['content']
    finally:
        log.close()
