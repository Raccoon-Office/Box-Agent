from box_agent.schema import Message
from box_agent.skill_context import SkillReferenceContext, read_reference, visible_ranges
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_loader import SkillLoader, SkillSelector


def make_loader(tmp_path):
    path = tmp_path / 'skills' / 'demo' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: demo\ndescription: demo guide\n---\nMETHOD_BODY\n', encoding='utf-8')
    loader = SkillLoader(sources=[(tmp_path / 'skills', 'user')], skill_settings_path=tmp_path / 'settings.json')
    loader.discover_skills()
    return loader, path


def test_catalog_is_trusted_tail_deduplicated_and_reports_removal(tmp_path):
    loader, path = make_loader(tmp_path)
    runtime = SkillRuntime(loader, append_only_context=True)
    selector = SkillSelector(loader)
    messages = [Message(role='system', content='BASE'), Message(role='user', content='demo')]
    first = runtime.catalog_message(selector, messages, 'demo', refresh=False)
    assert first and first.source == 'runtime' and 'demo' in first.content
    messages.append(first)
    assert runtime.catalog_message(selector, messages, '', refresh=False) is None
    path.unlink()
    loader.discover_skills()
    changed = runtime.catalog_message(selector, messages, '', refresh=False)
    assert changed and 'No Skill recommendations' in changed.content
    assert messages[0].content == 'BASE'


def test_selected_reference_is_bounded_durable_tail_and_deduplicated(tmp_path):
    loader, _ = make_loader(tmp_path)
    runtime = SkillRuntime(loader, append_only_context=True)
    runtime.select(['demo'])
    context = SkillReferenceContext(runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='demo')]
    before = [m.model_dump() for m in history]
    projection = context.prepare_request(history, budget_chars=50000, defer_delivery=True)
    assert [m.model_dump() for m in history] == before
    assert projection.messages[:2] == history
    assert len(projection.materialized_messages) == 1
    message = projection.materialized_messages[0]
    metadata, body = read_reference(message.content)
    assert metadata['kind'] == 'runtime_skill_instructions' and 'METHOD_BODY' in body
    snapshot = runtime.resolve_reference('demo')
    assert visible_ranges([message], name='demo', revision=snapshot.revision,
                          lines=snapshot.prompt.splitlines(keepends=True),
                          source=snapshot.source, path=snapshot.path)
    assert projection.on_committed
    projection.on_committed()
    history.append(message)
    again = context.prepare_request(history, budget_chars=50000, defer_delivery=True)
    assert not again.materialized_messages
    assert again.messages == history


def test_selection_over_budget_materializes_paging_notice(tmp_path):
    loader, _ = make_loader(tmp_path)
    runtime = SkillRuntime(loader, append_only_context=True)
    runtime.register_reference('large', 'x' * 10000, persist=False)
    context = SkillReferenceContext(runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='large')]
    result = context.prepare_request(history, budget_chars=1600, can_page=lambda names: True)
    assert not result.blocked_reason
    assert any('paged reading' in m.content for m in result.materialized_messages)
    assert all('x' * 100 not in m.content for m in result.materialized_messages)


def test_restored_tail_can_be_confirmed_after_failed_response(tmp_path):
    loader, _ = make_loader(tmp_path)
    original = SkillRuntime(loader)
    original.read('demo')
    records = original.log_records()
    runtime = SkillRuntime(loader, append_only_context=True)
    runtime.restore_records(records)
    runtime.begin_turn()
    context = SkillReferenceContext(runtime)
    messages = [Message(role='system', content='BASE'), Message(role='user', content='continue')]
    first = context.prepare_request(messages, budget_chars=50000, defer_delivery=True)
    assert first.materialized_messages and first.on_committed and first.on_response
    first.on_committed()
    messages.extend(first.materialized_messages)
    assert runtime.restoring_names == ('demo',)
    retry = context.prepare_request(messages, budget_chars=50000, defer_delivery=True)
    assert not retry.materialized_messages
    assert retry.on_committed and retry.on_response
    retry.on_committed()
    retry.on_response()
    runtime.begin_turn()
    assert not runtime.restoring_names


def test_bounded_no_reader_fallback_is_not_repeated(tmp_path):
    loader, _ = make_loader(tmp_path)
    runtime = SkillRuntime(loader, append_only_context=True)
    runtime.register_reference('large', 'x' * 10000, persist=False)
    context = SkillReferenceContext(runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='large')]
    first = context.prepare_request(history, budget_chars=1600)
    assert first.materialized_messages
    history.extend(first.materialized_messages)
    second = context.prepare_request(history, budget_chars=1600)
    assert not second.materialized_messages


def test_selected_skill_reappears_at_tail_after_compaction_without_changing_system(tmp_path):
    loader, _ = make_loader(tmp_path)
    runtime = SkillRuntime(loader, append_only_context=True)
    runtime.select(['demo'])
    context = SkillReferenceContext(runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='task')]
    initial = context.prepare_request(history, budget_chars=50000, defer_delivery=True)
    initial.on_committed()
    compacted = [history[0], Message(role='user', content='summary and current task')]
    before = [m.model_dump() for m in compacted]
    restored = context.prepare_request(compacted, budget_chars=50000, defer_delivery=True)
    assert [m.model_dump() for m in compacted] == before
    assert restored.messages[:2] == compacted
    assert len(restored.materialized_messages) == 1
    assert restored.materialized_messages[0].source == 'runtime'
    assert 'METHOD_BODY' in restored.materialized_messages[0].content
    assert restored.messages[0].content == 'BASE'
    compacted.extend(restored.materialized_messages)
    restored.on_committed()
    assert not context.prepare_request(compacted, budget_chars=50000).materialized_messages


def test_ordinary_skill_compaction_keeps_locator_and_requires_body_reread(tmp_path):
    loader, path = make_loader(tmp_path)
    runtime = SkillRuntime(loader, append_only_context=True)
    context = SkillReferenceContext(runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='task')]
    context.prepare_request(history, budget_chars=50000)
    first = context.read('demo')
    history.append(Message(role='tool', name='get_skill', tool_call_id='read', content=first.model_context))
    context.prepare_request(history, budget_chars=50000)
    compacted = [history[0], Message(role='user', content='continue from summary')]
    restored = context.prepare_request(compacted, budget_chars=50000)
    text = str(restored.materialized_messages)
    assert 'Use get_skill' in text and str(path.parent) in text
    assert 'METHOD_BODY' not in text
    assert restored.messages[0].content == 'BASE'
    reread = context.read('demo')
    assert reread.success and 'METHOD_BODY' in reread.model_context
    assert not reread.raw_output['skill_reference'].get('reused')
