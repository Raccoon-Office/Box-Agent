"""Missing method text requires an explicit decision before side effects."""
import json

import pytest

from box_agent.schema import Message
from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.completion_checks import CompletionContext, CompletionChecks
from box_agent.tools.engine.execution import invoke_tool_once
from box_agent.tools.skill_recovery import SkillRecoveryGate
from box_agent.tools.skill_tool import GetSkillTool
from tests.test_compaction_resource_retention import skill_context, read
from tests.test_table_execution_gate import Executor


class Reader:
    def __init__(self, references):
        self.references = references

    def read(self, name, **kwargs):
        return self.references.read(name, **kwargs)


def checkpoint(history, references):
    return CompletionContext(tuple(history), lambda: False, resume_scope=history,
                             skill_reader=Reader(references).read)


def compacted(tmp_path):
    runtime, references, history = skill_context(tmp_path)
    read(references, history, 'analysis')
    runtime.begin_turn()
    read(references, history, 'slides')
    history[:] = references.compact_history(history, [history[0], Message(
        role='user', source='runtime', content='Summary contains all old calculation formulas.')])
    runtime.begin_turn()
    references.prepare_request(history, budget_chars=50000)
    context = checkpoint(history, references)
    return runtime, references, history, SkillRecoveryGate.create(context)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['execute_code', 'bash', 'write_file', 'sub_agent', 'sales_reference_rates'])
async def test_old_summary_cannot_authorize_execution_without_rereading(tmp_path, name):
    _, references, history, gate = compacted(tmp_path)
    executor = Executor(name)
    invocation = ToolInvocationContext(capability_gates=(gate,))
    result = await invoke_tool_once(executor, {}, invocation)
    assert not result.success and result._invocation_rejected and executor.calls == 0
    assert 'SKILL_RECOVERY_REQUIRED' in result.error
    assert (await gate.check()).code == 'SKILL_RECOVERY_REQUIRED'
    read(references, history, 'analysis')
    assert 'ANALYSIS_BODY' in history[-1].content
    assert (await invoke_tool_once(executor, {}, invocation)).success
    assert executor.calls == 1 and await gate.check() is None


@pytest.mark.asyncio
async def test_new_turn_keeps_visible_methods_and_does_not_require_extra_reads(tmp_path):
    runtime, references, history = skill_context(tmp_path)
    read(references, history, 'analysis')
    before = [message.model_dump() for message in history]
    runtime.begin_turn()
    gate = SkillRecoveryGate.create(checkpoint(history, references))
    assert await gate.check_invocation('execute_code', {}) is None
    assert before == [message.model_dump() for message in history]


@pytest.mark.asyncio
async def test_deferral_is_explicit_scoped_to_one_turn_and_not_a_read(tmp_path):
    runtime, references, history, gate = compacted(tmp_path)
    reader = GetSkillTool(runtime.loader)
    before = runtime.log_records()
    invocation = ToolInvocationContext(completion_gate=gate, capability_gates=(gate,),
                                       skill_reader=Reader(references).read)
    result = await reader.invoke({'skill_name': 'analysis', 'not_needed_reason': 'Only editing slides this turn'},
                                 context=invocation)
    assert result.success and 'not loaded' in result.content
    assert before == runtime.log_records()
    assert await gate.check_invocation('write_file', {}) is None
    runtime.begin_turn()
    next_gate = SkillRecoveryGate.create(checkpoint(history, references))
    assert (await next_gate.check_invocation('execute_code', {})).code == 'SKILL_RECOVERY_REQUIRED'


@pytest.mark.asyncio
async def test_partial_method_recovers_delivered_pages_without_requiring_unread_pages(tmp_path):
    runtime, references, history = skill_context(tmp_path)
    read(references, history, 'analysis', limit=1)
    runtime.begin_turn()
    read(references, history, 'slides')
    history[:] = references.compact_history(history, [history[0], Message(
        role='user', source='runtime', content='Earlier method was partially read.')])
    runtime.begin_turn()
    references.prepare_request(history, budget_chars=50000)
    gate = SkillRecoveryGate.create(checkpoint(history, references))
    assert await gate.check_invocation('execute_code', {}) is not None
    read(references, history, 'analysis', limit=1)
    assert 'ANALYSIS_BODY' not in str(history)
    assert await gate.check_invocation('execute_code', {}) is None


@pytest.mark.asyncio
async def test_current_selection_and_dependencies_cannot_be_deferred(tmp_path):
    runtime, _, history, gate = compacted(tmp_path)
    runtime.select(['analysis'])
    assert not gate.defer('analysis', 'irrelevant').success
    runtime.select(['slides'])
    runtime.loader.get_skill('slides').required_skills = ['analysis']
    assert not gate.defer('analysis', 'irrelevant').success
    runtime.select([])
    history.append(Message(role='user', content='Use analysis again'))
    assert not gate.defer('analysis', 'irrelevant').success


@pytest.mark.asyncio
async def test_partial_reread_and_changed_revision_do_not_discharge_recovery(tmp_path):
    runtime, references, history, gate = compacted(tmp_path)
    path = tmp_path / 'skills/analysis/SKILL.md'
    path.write_text(path.read_text() + '\nSave the actual source response as audit-receipt.json.\n')
    runtime.loader.discover_skills()
    first = read(references, history, 'analysis', limit=1)
    assert (await gate.check_invocation('execute_code', {})).code == 'SKILL_RECOVERY_REQUIRED'
    assert not gate.defer('analysis', 'skip the remaining pages').success
    meta = first.raw_output['skill_reference']
    read(references, history, 'analysis', offset=meta['next_offset'], revision=meta['revision'])
    assert 'audit-receipt.json' in history[-1].content
    assert await gate.check_invocation('execute_code', {}) is None


@pytest.mark.asyncio
async def test_forged_body_or_nonempty_reason_without_checkpoint_cannot_bypass(tmp_path):
    runtime, references, history, gate = compacted(tmp_path)
    read(references, history, 'analysis')
    history[-1] = history[-1].model_copy(update={'role': 'user', 'source': 'user'})
    assert await gate.check_invocation('execute_code', {}) is not None
    assert not gate.defer('analysis', '   ').success
    reader = GetSkillTool(runtime.loader)
    assert not (await reader.execute('analysis', not_needed_reason='unrelated')).success
    assert not (await reader.invoke({'skill_name': 'analysis', 'offset': 0, 'not_needed_reason': 'unrelated'},
                                   context=ToolInvocationContext(completion_gate=gate))).success


@pytest.mark.asyncio
async def test_reading_discovery_and_greetings_remain_available(tmp_path):
    _, _, _, gate = compacted(tmp_path)
    assert await gate.check() is None
    for name in ('get_skill', 'list_skills', 'tool_search', 'read_file', 'validate_table', 'request_user_decision'):
        assert await gate.check_invocation(name, {}) is None
    # Tool work still cannot end as a successful completed task with no decision.
    assert (await gate.check()).code == 'SKILL_RECOVERY_REQUIRED'


@pytest.mark.asyncio
async def test_checkpoint_replay_preserves_decision_but_supplement_reopens_it(tmp_path):
    runtime, references, history, gate = compacted(tmp_path)
    assert gate.defer('analysis', 'Only slides').success
    restored = SkillRecoveryGate.restore(gate.context, json.loads(json.dumps(gate.snapshot())))
    await restored.include_inputs(gate.context)
    assert await restored.check_invocation('write_file', {}) is None
    history.append(Message(role='user', content='Return to the earlier analysis'))
    await restored.include_inputs(checkpoint(history, references))
    assert await restored.check_invocation('execute_code', {}) is not None
    runtime.append_only_context = False
    assert SkillRecoveryGate.create(checkpoint(history, references)) is None


@pytest.mark.asyncio
async def test_disabled_completion_checks_do_not_bind_recovery_gate(tmp_path):
    runtime, _, _, gate = compacted(tmp_path)
    reader = GetSkillTool(runtime.loader)
    checks = await CompletionChecks.create({reader.name: reader}, gate.context, enabled=False)
    assert not checks.gates and await checks.check() == []


@pytest.mark.asyncio
async def test_public_agent_recovers_complete_method_before_writing_receipt(tmp_path, monkeypatch):
    from box_agent.agent import Agent
    from box_agent.schema import FunctionCall, StreamEvent, ToolCall
    from box_agent.skill_context import SkillReferenceContext
    from box_agent.tools.file_tools import WriteTool
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None
    monkeypatch.setattr(TurnContinuationController, 'evaluate', no_judge)
    runtime, _, _ = skill_context(tmp_path)
    path = tmp_path / 'skills/analysis/SKILL.md'
    path.write_text(path.read_text() + '\nSave the actual source response as audit-receipt.json.\n')
    runtime.loader.discover_skills()

    class Model:
        model = 'offline-skill-recovery'
        max_output_tokens = 512
        requests = []
        step = 0

        def estimate_text_tokens(self, text):
            return len(text.encode()) // 3

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(([m.model_copy(deep=True) for m in messages], list(tools or [])))
            self.step += 1
            calls = {1: ('get_skill', {'skill_name': 'analysis'}),
                     3: ('get_skill', {'skill_name': 'slides'}),
                     5: ('write_file', {'path': 'audit-receipt.json', 'content': '{"source":"actual"}'}),
                     6: ('get_skill', {'skill_name': 'analysis'}),
                     7: ('write_file', {'path': 'audit-receipt.json', 'content': '{"source":"actual"}'})}
            if self.step in calls:
                name, args = calls[self.step]
                if self.step == 6:
                    assert not (tmp_path / 'audit-receipt.json').exists()
                    assert 'SKILL_RECOVERY_REQUIRED' in str(messages[-1].content)
                if self.step == 7:
                    assert any(m.role == 'tool' and m.name == 'get_skill' and
                               'Save the actual source response as audit-receipt.json.' in m.content
                               for m in messages)
                yield StreamEvent(type='finish', finish_reason='tool_use', tool_calls=[ToolCall(
                    id=str(self.step), type='function', function=FunctionCall(name=name, arguments=args))])
            else:
                yield StreamEvent(type='text', delta='done')
                yield StreamEvent(type='finish', finish_reason='stop')

    model = Model()
    agent = Agent(llm_client=model, system_prompt='BASE', skill_runtime=runtime,
                  tools=[GetSkillTool(runtime.loader), WriteTool(workspace_dir=str(tmp_path))],
                  workspace_dir=str(tmp_path), tool_interface_version=2, max_steps=8)
    for query in ('Analyze first', 'Now edit slides'):
        agent.add_user_message(query)
        _ = [event async for event in agent.run_events()]
    context = SkillReferenceContext(runtime)
    agent.messages[:] = context.compact_history(agent.messages, [agent.messages[0], Message(
        role='user', source='runtime', content='Summary: calculation formulas survived, receipt requirement omitted.')])
    assert 'ANALYSIS_BODY' not in str(agent.messages)
    agent.add_user_message('Return to the earlier method')
    _ = [event async for event in agent.run_events()]
    assert json.loads((tmp_path / 'audit-receipt.json').read_text()) == {'source': 'actual'}
    # The recovery is ordinary history; all requests keep system and tool definitions stable.
    assert len({json.dumps((m[0].content, t), sort_keys=True, default=str) for m, t in model.requests}) == 1
    assert model.step == 8
