"""Offline recovery regressions from the integrated6c1bbc0 r7 handoff."""

import asyncio

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import DoneEvent, ErrorEvent, ProgressEvent, StopReason
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


class WriteOnce(Tool):
    name = 'write_once'
    description = 'Commit one action'
    parameters = {'type': 'object', 'properties': {}}

    def __init__(self):
        self.calls = 0

    async def execute(self):
        self.calls += 1
        return ToolResult(success=True, content='committed-result')


class RepeatingProvider:
    def __init__(self, channel, *, repeat_forever=False, cancel=None):
        self.channel = channel
        self.repeat_forever = repeat_forever
        self.cancel = cancel
        self.requests = []
        self.closed = 0

    async def generate_stream(self, messages, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        try:
            if len(self.requests) == 1:
                yield StreamEvent(type='finish', finish_reason='tool_use', tool_calls=[
                    ToolCall(id='write', type='function', function=FunctionCall(name='write_once', arguments={}))])
            elif len(self.requests) == 2 or self.repeat_forever:
                for _ in range(20):
                    yield StreamEvent(type=self.channel, delta='</parameter>\n')
            else:
                yield StreamEvent(type='text', delta='Completed and verified.')
                yield StreamEvent(type='finish', finish_reason='stop')
        finally:
            self.closed += 1
            if self.cancel and len(self.requests) == 2:
                self.cancel.set()


@pytest.mark.parametrize('channel', ['text', 'thinking'])
@pytest.mark.parametrize('repeat_forever', [False, True])
async def test_repeat_recovery_preserves_committed_actions_and_retries_once(channel, repeat_forever):
    provider = RepeatingProvider(channel, repeat_forever=repeat_forever)
    tool = WriteOnce()
    messages = [Message(role='system', content='BASE'), Message(role='user', content='Do the work')]
    events = [event async for event in run_agent_loop(llm=provider, tools={tool.name: tool},
                                                     messages=messages, max_steps=5)]
    assert tool.calls == 1
    assert len(provider.requests) == provider.closed == 3
    assert any(message.role == 'tool' and message.content == 'committed-result'
               for message in provider.requests[-1])
    assert '</parameter>' not in str(messages)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    if repeat_forever:
        assert errors[-1].error_code == 'LLM_REPETITIVE_OUTPUT'
        assert errors[-1].error_details['recoveryAttempts'] == 1
        assert done.stop_reason == StopReason.ERROR
    else:
        assert errors == []
        assert done.stop_reason == StopReason.END_TURN


async def test_cancel_after_repetitive_stream_closes_does_not_retry():
    cancel = asyncio.Event()
    provider = RepeatingProvider('text', cancel=cancel)
    tool = WriteOnce()
    events = [event async for event in run_agent_loop(llm=provider, tools={tool.name: tool},
        messages=[Message(role='system', content='BASE'), Message(role='user', content='Do work')],
        max_steps=5, is_cancelled=cancel.is_set)]
    assert len(provider.requests) == 2
    assert tool.calls == 1
    assert [event for event in events if isinstance(event, DoneEvent)][-1].stop_reason == StopReason.CANCELLED


async def test_history_budget_error_reports_actual_limit():
    class Summary:
        async def generate(self, **kwargs):
            return LLMResponse(content='<summary>Earlier task.</summary>', finish_reason='stop')

        async def generate_stream(self, **kwargs):
            raise AssertionError('Oversized input must not reach provider')
            yield

    # Drive the real history-compaction boundary with a large system prompt.
    events = [event async for event in run_agent_loop(llm=Summary(), tools={},
        messages=[Message(role='system', content='large rules ' * 1000),
                  Message(role='user', content='Continue')], token_limit=2000, max_steps=1)]
    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.error_code == 'CONTEXT_INPUT_BUDGET_EXCEEDED'
    assert error.error_details['historyLimitTokens'] == 2000
    assert error.error_details['totalLimitTokens'] == 2000
    assert error.error_details['estimatedHistoryTokens'] > 2000
    assert 'history limit 2000' in error.message


class ScriptedProvider:
    capabilities = {'image_input': True}

    def __init__(self, scripts):
        self.scripts = scripts
        self.requests = []

    async def generate(self, **kwargs):
        return LLMResponse(content='<summary>Earlier task.</summary>', finish_reason='stop')

    async def generate_stream(self, messages, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        for event in self.scripts[len(self.requests) - 1]:
            yield event


class AttachImage(WriteOnce):
    name = 'attach_image'
    transient_followup_allowed = True

    def __init__(self, size=1):
        super().__init__()
        self.size = size

    async def execute(self):
        self.calls += 1
        return ToolResult(success=True, content='image receipt', transient_followup_content=[
            {'type': 'input_image', 'media_type': 'image/png', 'data': 'YQ==',
             'width': self.size, 'height': self.size}])


def tool_script(name):
    return [StreamEvent(type='finish', finish_reason='tool_use', tool_calls=[
        ToolCall(id=name, type='function', function=FunctionCall(name=name, arguments={}))])]


def repeated_script():
    return [StreamEvent(type='text', delta='</parameter>\n') for _ in range(20)]


def done_script():
    return [StreamEvent(type='text', delta='done'), StreamEvent(type='finish', finish_reason='stop')]


def request_history():
    return [Message(role='system', content='BASE'), Message(role='user', content='Do work')]


def has_image(messages):
    return any(isinstance(message.content, list) and any(
        isinstance(block, dict) and block.get('type') == 'input_image' for block in message.content)
        for message in messages)


async def test_image_is_retried_until_valid_response_then_consumed():
    provider = ScriptedProvider([tool_script('attach_image'), repeated_script(),
                                 tool_script('write_once'), done_script()])
    image, write, history = AttachImage(), WriteOnce(), request_history()
    events = [event async for event in run_agent_loop(llm=provider, messages=history,
        tools={image.name: image, write.name: write}, max_steps=6, token_limit=10000)]
    assert image.calls == write.calls == 1
    assert [has_image(request) for request in provider.requests] == [False, True, True, False]
    assert not has_image(history)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [StopReason.END_TURN]


async def test_successful_tool_does_not_reset_repetitive_stream_recovery_limit():
    provider = ScriptedProvider([repeated_script(), tool_script('write_once'), repeated_script(), done_script()])
    write = WriteOnce()
    events = [event async for event in run_agent_loop(llm=provider, messages=request_history(),
        tools={write.name: write}, max_steps=8)]
    assert len(provider.requests) == 3 and write.calls == 1
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [StopReason.ERROR]
    assert [event.error_details['recoveryAttempts'] for event in events if isinstance(event, ErrorEvent)] == [1]


async def test_repetitive_stream_on_last_step_does_not_make_extra_request():
    provider = ScriptedProvider([repeated_script(), done_script()])
    events = [event async for event in run_agent_loop(llm=provider, messages=request_history(),
                                                     tools={}, max_steps=1)]
    assert len(provider.requests) == 1
    assert [event.error_details['recoveryAttempts'] for event in events if isinstance(event, ErrorEvent)] == [0]


async def test_cancel_at_recovery_progress_stops_before_next_request():
    provider = ScriptedProvider([repeated_script(), done_script()])
    cancel, events = asyncio.Event(), []
    async for event in run_agent_loop(llm=provider, messages=request_history(), tools={},
                                      max_steps=4, is_cancelled=cancel.is_set):
        events.append(event)
        if isinstance(event, ProgressEvent):
            cancel.set()
    assert len(provider.requests) == 1
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [StopReason.CANCELLED]


async def test_history_budget_error_subtracts_nonzero_image_overlay():
    provider = ScriptedProvider([tool_script('attach_image'), done_script()])
    image = AttachImage(size=1000)
    history = [Message(role='system', content='x' * 18000), Message(role='user', content='Do work')]
    events = [event async for event in run_agent_loop(llm=provider, messages=history,
        tools={image.name: image}, max_steps=3, token_limit=6400)]
    assert len(provider.requests) == image.calls == 1
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    details = errors[0].error_details
    assert details['transientInputTokens'] > 1500
    assert details['historyLimitTokens'] == (details['totalLimitTokens']
        - details['transientInputTokens'] - details['extraInputTokens'])
    assert str(details['historyLimitTokens']) in errors[0].message
