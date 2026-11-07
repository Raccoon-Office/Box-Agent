"""An already-triggered compaction may shorten its summary to avoid a false block."""
import pytest

from box_agent.kernel.context_engine import (
    _maybe_summarize, _bound_text_middle, _SUMMARY_MESSAGE_PREFIX,
    _SUMMARY_MESSAGE_SUFFIX, request_input_tokens,
)
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall


class SummaryModel:
    summary = ('Current task and exact artifact paths. ' * 200).strip()
    calls = 0

    def estimate_text_tokens(self, text):
        return (len(text.encode()) + 1) // 2

    async def generate_stream(self, messages, **kwargs):
        self.calls += 1
        yield StreamEvent(type='text', delta=f'<summary>{self.summary}</summary>')
        yield StreamEvent(type='finish', finish_reason='stop')


def fixture():
    llm = SummaryModel()
    system = Message(role='system', content='BASE ' * 1000)
    user = Message(role='user', content='Finish the report and preserve the existing artifacts.')
    call = Message(role='assistant', content='', tool_calls=[ToolCall(
        id='current-call', type='function', function=FunctionCall(name='execute_code', arguments={'code': 'write_json()'}))])
    result = Message(role='tool', name='execute_code', tool_call_id='current-call', content='JSON written; MD pending.')
    descriptor = Message(role='user', source='runtime', name='tool_descriptions', content='EXACT_DESCRIPTOR ' * 150)
    history = [system, Message(role='user', content='old input'),
               Message(role='assistant', content='old result ' * 5000), user, call, result]

    def project(messages):
        return [*messages, descriptor]

    def estimate(summary_limit):
        summary = Message(role='user', source='runtime', content=(
            _SUMMARY_MESSAGE_PREFIX + _bound_text_middle(llm.summary, summary_limit, label='summary')
            + _SUMMARY_MESSAGE_SUFFIX))
        return request_input_tokens(project([system, summary, user, call, result]), {}, llm=llm)

    return llm, history, project, estimate, (system, user, call, result, descriptor)


@pytest.mark.asyncio
async def test_compaction_recovers_small_budget_overflow_without_another_model_call():
    llm, history, project, estimate, protected = fixture()
    limit = estimate(2000) - 77
    result = await _maybe_summarize(llm, history, limit, 0, False, force=True,
                                  summary_input_token_limit=100000, compacted_history_projector=project)
    assert result.mode == 'summary' and result.estimated_after <= limit
    assert llm.calls == result.summary_calls == 1
    for message in protected:
        assert message in result.messages
    assert result.messages[-2].tool_call_id == 'current-call'
    assert result.messages[-3].tool_calls[0].function.arguments == {'code': 'write_json()'}


@pytest.mark.asyncio
async def test_fitting_compaction_keeps_the_existing_summary_length():
    llm, history, project, estimate, _ = fixture()
    result = await _maybe_summarize(llm, history, estimate(2000) + 1, 0, False, force=True,
                                  summary_input_token_limit=100000, compacted_history_projector=project)
    assert result.mode == 'summary'
    assert result.messages[1].content == (
        _SUMMARY_MESSAGE_PREFIX + _bound_text_middle(llm.summary, 2000, label='summary') + _SUMMARY_MESSAGE_SUFFIX)


@pytest.mark.asyncio
async def test_oversized_protected_history_still_blocks_without_truncating_inputs_or_calls():
    llm, history, project, estimate, protected = fixture()
    result = await _maybe_summarize(llm, history, estimate(512) - 1, 0, False, force=True,
                                  summary_input_token_limit=100000, compacted_history_projector=project)
    assert result.blocked and llm.calls == 1
    assert all(message in result.messages for message in protected)


@pytest.mark.asyncio
async def test_summary_fallback_does_not_add_a_compaction_trigger():
    llm, history, project, _, _ = fixture()
    limit = request_input_tokens(history, {}, llm=llm) + 1
    result = await _maybe_summarize(llm, history, limit, 0, False, compacted_history_projector=project)
    assert result.messages is None and llm.calls == 0
