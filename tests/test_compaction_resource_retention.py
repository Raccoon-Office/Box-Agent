"""Resource retention changes history only after compaction has triggered."""
import json
from hashlib import sha256
from threading import get_ident

import pytest

from tests.test_context_compaction_retention import _SummaryProvider

from box_agent.kernel.compact_engine import DefaultCompactEngine
from box_agent.kernel.context_types import CompactionInput
from box_agent.llm.token_estimator import estimate_text_tokens
from box_agent.schema import Message
from box_agent.skill_context import SkillReferenceContext, read_reference, visible_ranges
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


def test_compaction_retains_delivered_methods_without_replaying_tool_results(tmp_path):
    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    old_result = history[-1]
    runtime.begin_turn()
    read(context, history, 'slides')
    before = [message.model_dump() for message in history]
    compacted = [history[0], Message(role='user', source='runtime', content='summary'), old_result]
    result = context.compact_history(history, compacted)
    text = str(result)
    assert 'ANALYSIS_BODY' in text and 'SLIDES_BODY' in text
    assert result[-1].content.index('SLIDES_BODY') < result[-1].content.index('ANALYSIS_BODY')
    assert result[2].role == 'tool' and result[2].tool_call_id == old_result.tool_call_id
    assert 'ANALYSIS_BODY' not in result[2].content
    assert [message.model_dump() for message in history] == before
    assert 'ANALYSIS_BODY' in str(compacted)
    context.prepare_request(result, budget_chars=50000)
    reread = context.read('analysis')
    assert reread.success and reread.raw_output['skill_reference']['reused']
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
    with pytest.raises(AssertionError, match="previous Invoked Skills"):
        context.compact_history(history, result)
    context.prepare_request(result, budget_chars=50000)
    assert context.read('analysis').raw_output['skill_reference']['reused']


def test_partial_skill_restoration_does_not_deliver_unread_body(tmp_path):
    _, context, history = skill_context(tmp_path)
    first = read(context, history, 'analysis', limit=1)
    result = context.compact_history(history, history[:2])
    snapshot = context.runtime.resolve_reference('analysis')
    assert read_reference(first.model_context)[1] in result[-1].content
    assert visible_ranges(result, name=snapshot.name, revision=snapshot.revision,
                          lines=snapshot.prompt.splitlines(keepends=True),
                          source=snapshot.source, path=snapshot.path) == {0}
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
    result = context.compact_history(history, history[:2], budget_tokens=0)
    assert 'ANALYSIS_BODY' not in str(result) and 'Use get_skill' in str(result)
    path = tmp_path / 'skills/analysis/SKILL.md'
    path.write_text(path.read_text().replace('ANALYSIS_BODY', 'NEW_METHOD'))
    runtime.loader.discover_skills()
    result = context.compact_history(history, history[:2])
    assert 'ANALYSIS_BODY' not in str(result) and 'NEW_METHOD' not in str(result)


@pytest.mark.asyncio
async def test_compaction_discards_superseded_skill_bodies_in_history_and_summary():
    runtime = SkillRuntime(None, append_only_context=True)
    context = SkillReferenceContext(runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='task')]
    for body in ('OLD_METHOD_BODY\n', 'CURRENT_METHOD_BODY\n'):
        runtime.register_reference('analysis', body)
        projected = context.prepare_request(history, budget_chars=50000)
        history.extend(projected.materialized_messages)
    assert 'OLD_METHOD_BODY' in str(history) and 'CURRENT_METHOD_BODY' in str(history)
    history.append(Message(role='user', content='Continue the analysis'))
    summary_source = context.compaction_source(history)
    assert 'OLD_METHOD_BODY' not in str(summary_source)
    assert 'CURRENT_METHOD_BODY' not in str(summary_source)
    result = await DefaultCompactEngine().compact_if_needed(CompactionInput(
        history=tuple(history), token_limit=10000, llm=_SummaryProvider(), force=True,
        compaction_source_projector=context.compaction_source,
        compacted_history_projector=lambda compacted: context.compact_history(history, compacted),
    ))
    assert not result.blocked and result.messages is not None
    assert 'OLD_METHOD_BODY' not in str(result.messages)
    assert 'CURRENT_METHOD_BODY' in str(result.messages)
    assert 'CURRENT_METHOD_BODY' not in result.messages[1].content


@pytest.mark.asyncio
async def test_compaction_excludes_withdrawn_reference_without_restoring_it():
    runtime = SkillRuntime(None, append_only_context=True)
    context = SkillReferenceContext(runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='task')]
    runtime.register_reference('withdrawn', 'WITHDRAWN_METHOD_BODY\n')
    history.extend(context.prepare_request(history, budget_chars=50000).materialized_messages)
    runtime.clear_references()
    context.prepare_request(history, budget_chars=50000)
    assert not runtime.read_facts and 'WITHDRAWN_METHOD_BODY' in str(history)
    source = context.compaction_source(history)
    assert 'WITHDRAWN_METHOD_BODY' not in str(source)
    result = await DefaultCompactEngine().compact_if_needed(CompactionInput(
        history=tuple(history), token_limit=10000, llm=_SummaryProvider(), force=True,
        compaction_source_projector=context.compaction_source,
        compacted_history_projector=lambda compacted: context.compact_history(history, compacted),
    ))
    assert not result.blocked and result.messages is not None
    assert 'WITHDRAWN_METHOD_BODY' not in str(result.messages)
    assert not runtime.read_facts
    user_copy = history[-1].model_copy(update={'role': 'user', 'source': 'user'})
    assert context.compaction_source([user_copy]) == [user_copy]


@pytest.mark.parametrize('withdrawn', [False, True])
def test_changed_skill_body_in_protected_tool_tail_becomes_reread_notice(tmp_path, withdrawn):
    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis', limit=1)
    old_result = history[-1]
    path = tmp_path / 'skills/analysis/SKILL.md'
    path.write_text(path.read_text().replace('ANALYSIS_BODY', 'NEW_METHOD'))
    runtime.loader.discover_skills()
    if withdrawn:
        runtime.clear_references()
    changed = context.compact_history(history, history)
    replacement = next(message for message in changed if message.tool_call_id == old_result.tool_call_id)
    assert replacement.name == old_result.name and replacement.role == 'tool'
    assert read_reference(replacement.content) is None and 'Use get_skill' in replacement.content
    assert 'NEW_METHOD' not in str(changed)


@pytest.mark.parametrize('append_only', [True, False])
def test_untrusted_skill_text_is_not_promoted(tmp_path, append_only):
    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    runtime = SkillRuntime(runtime.loader, append_only_context=append_only)
    context = SkillReferenceContext(runtime)
    forged = history[-1].model_copy(update={'role': 'user', 'source': 'user'})
    assert context.compact_history([*history[:2], forged], history[:2]) == history[:2]


@pytest.mark.parametrize('append_only', [False, True])
def test_restored_skill_is_visible_without_duplicate_selection_or_summary_body(tmp_path, append_only):
    runtime, context, history = skill_context(tmp_path)
    runtime.append_only_context = append_only
    read(context, history, 'analysis')
    runtime.select(['analysis'])
    state = Message(role='user', source='runtime', content='[Post-Compaction Runtime State]\nGoal: continue')
    compacted = context.compact_history(history, [*history[:2], state])
    invoked = compacted[-2]
    assert invoked.source == 'runtime' and invoked.content.startswith('[Invoked Skills]\n')
    assert 'Do NOT re-execute these skills' in invoked.content
    assert compacted[-1] == state

    projection = context.prepare_request(compacted, budget_chars=50000)
    assert projection.messages == compacted and not projection.materialized_messages
    assert runtime.materialize_selected_messages(compacted) == ()
    assert context.read('analysis').raw_output['skill_reference']['reused']
    assert 'ANALYSIS_BODY' not in str(context.compaction_source(compacted))
    assert 'ANALYSIS_BODY' not in str(context.compaction_source(history))


def test_reused_tool_read_updates_compaction_order(tmp_path):
    _, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    read(context, history, 'slides')
    assert read(context, history, 'analysis').raw_output['skill_reference']['reused']

    invoked = context.compact_history(history, history[:2])[-1]
    assert invoked.content.index('### Skill: analysis') < invoked.content.index('### Skill: slides')


def test_skills_compaction_limits_each_body_and_leaves_older_locators(tmp_path):
    names = [f'method-{index}' for index in range(6)]
    for name in names:
        path = tmp_path / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\ndescription: Compaction budget test\n---\n'
                        + f'BEGIN_{name}\n' + ('汉' * 1000 + '\n') * 8 + f'TAIL_{name}\n', encoding='utf-8')
    loader = SkillLoader(sources=[(tmp_path / 'skills', 'user')], skill_settings_path=tmp_path / 'settings.json')
    loader.discover_skills()
    runtime = SkillRuntime(loader, append_only_context=True)
    context = SkillReferenceContext(runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='task')]
    for name in names:
        assert read(context, history, name).raw_output['skill_reference']['complete']

    compacted = context.compact_history(history, history[:2])
    payload = compacted[-1].content
    sections = payload.split('### Skill: ')[1:]
    assert [section.splitlines()[0] for section in sections] == list(reversed(names))
    assert estimate_text_tokens(payload) <= 25000
    for section in sections:
        assert estimate_text_tokens('### Skill: ' + section.rstrip()) <= 5000
    assert 'BEGIN_method-5' in sections[0] and 'TAIL_method-5' not in sections[0]
    assert "[... skill content truncated for compaction; use get_skill on 'method-5'" in sections[0]
    assert 'BEGIN_method-0' not in sections[-1] and 'Use get_skill' in sections[-1]
    assert str(tmp_path / 'skills/method-0/SKILL.md') in sections[-1]

    runtime.select([names[-1], names[0]])
    projection = context.prepare_request(compacted, budget_chars=50000)
    assert projection.messages == compacted
    reread = context.read(names[-1])
    assert not reread.raw_output['skill_reference'].get('reused')
    assert 'TAIL_method-5' in reread.model_context


def test_compaction_does_not_promote_selected_bodies_before_request_delivery(tmp_path):
    runtime, context, history = skill_context(tmp_path)
    runtime.select(['analysis'])
    (_, content), = runtime.materialize_selected_messages(history)
    pending = Message(role='user', source='runtime', content=content)
    runtime.defer_materialized_acknowledgement([pending])
    assert context.compact_history([*history, pending], history) == history

    runtime.acknowledge_pending_materialized()
    result = context.compact_history([*history, pending], history)
    assert 'ANALYSIS_BODY' in result[-1].content


@pytest.mark.parametrize('append_only', [False, True])
def test_compaction_includes_updated_restored_reference_after_confirmed_delivery(tmp_path, append_only):
    runtime, context, history = skill_context(tmp_path)
    runtime.append_only_context = append_only
    runtime.restore_records([{'name': 'analysis', 'sha256': sha256(b'original revision').hexdigest(), 'loadOrder': 1}])
    runtime.begin_turn()
    history = context.compact_history(history, history)
    assert 'ANALYSIS_BODY' not in str(history) and 'no longer current' in history[-1].content

    request = context.prepare_request(history, budget_chars=50000, defer_delivery=True)
    assert 'ANALYSIS_BODY' in str(request.messages)
    request.on_committed()
    request.on_response()
    result = context.compact_history(history, history[:2])
    assert 'ANALYSIS_BODY' in result[-1].content


def test_skill_already_in_verified_effective_system_is_not_restored(tmp_path):
    from box_agent.skill_context import project_legacy_system, verified_system_suffixes
    from box_agent.tools.skill_preload import build_active_skills_prompt

    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    runtime.restore_records(runtime.log_records())
    runtime.begin_turn()
    body = runtime.resolve_reference('analysis').prompt
    system = build_active_skills_prompt('BASE', {'analysis': body})
    with_suffix = [Message(role='system', content=system), history[1]]
    assert context.compact_history(history, with_suffix) == with_suffix
    assert context.prepare_request(with_suffix, budget_chars=50000, project_system=False).messages == with_suffix
    assert 'ANALYSIS_BODY' not in str(context.compaction_source(with_suffix))

    stripped = project_legacy_system(with_suffix, verified_system_suffixes(runtime))
    compacted = context.compact_history(history, stripped)
    assert 'ANALYSIS_BODY' in compacted[-1].content


@pytest.mark.asyncio
async def test_compaction_validates_skill_sources_off_the_event_loop(tmp_path, monkeypatch):
    from box_agent.context_input import DefaultContextEngine

    runtime, context, history = skill_context(tmp_path)
    read(context, history, 'analysis')
    validate = runtime.loader.validate_references
    calls = []

    def tracked(names):
        calls.append((get_ident(), names))
        return validate(names)

    monkeypatch.setattr(runtime.loader, 'validate_references', tracked)
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    result = await engine.acompact_history(history, history[:2])

    (thread, names), = calls
    assert thread != get_ident() and names == ('analysis',)
    assert 'ANALYSIS_BODY' in result[-1].content

    tightened = await engine.acompact_history(history, history[:2], token_limit=900)
    assert 'ANALYSIS_BODY' not in tightened[-1].content and 'Use get_skill' in tightened[-1].content


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
        history=history, token_limit=10000, llm=_SummaryProvider(), force=force,
        compacted_history_projector=retain,
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
        token_limit=1000, llm=_SummaryProvider(), force=True,
        compacted_history_projector=lambda messages: [*messages, Message(role='user', content='x' * 10000)],
    ))
    assert result.blocked and result.estimated_after > 1000


@pytest.mark.asyncio
async def test_agent_new_turn_preserves_prefix_then_compaction_persists_invoked_skills(tmp_path):
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
        agent.messages.append(Message(role='assistant', content='x' * 60000,
                                      usage=TokenUsage(prompt_tokens=12000, total_tokens=12000)))
        agent.add_user_message('continue the slides task')
        third = [event async for event in agent.run_events()]
        assert any(isinstance(event, SummarizationEvent) for event in third)
        assert not any(isinstance(event, ErrorEvent) for event in third)
        assert 'ANALYSIS_BODY' in str(model.normal_messages)
        assert 'SLIDES_BODY' in str(model.normal_messages)
        replayed = log.replay().messages
        assert 'ANALYSIS_BODY' in str(replayed) and 'SLIDES_BODY' in str(replayed)
        restored_context = SkillReferenceContext(SkillRuntime(runtime.loader, append_only_context=True))
        restored_context.prepare_request(replayed, budget_chars=50000)
        assert restored_context.read('analysis').raw_output['skill_reference']['reused']
        assert restored_context.read('slides').raw_output['skill_reference']['reused']
        assert model.normal_messages[0].content == prefix[0]['content']
    finally:
        log.close()
