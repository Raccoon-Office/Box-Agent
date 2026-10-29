"""The producer never blocks foreground work or invents new plans after uncertainty."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from box_agent.desktop_memory.experience import ExperienceRuntime
from box_agent.desktop_memory.transport import MemoryProxyError
from box_agent.payload_privacy import private_payloads
from box_agent.schema import LLMResponse


class Transport:
    def __init__(self, *, frozen=False):
        self.calls = []
        self.claimed = False
        self.prompt = 'Fixed native data\nBEGIN EVIDENCE VIEW\n{"content":"synthetic preference"}\nEND EVIDENCE VIEW'
        self.frozen = frozen
        self.unknown = False
        self.proposal = None

    async def request(self, operation, payload):
        assert operation == 'experience/job'
        self.calls.append(payload)
        action = payload['action']
        if action == 'claim':
            if self.claimed:
                return {'job': None}
            self.claimed = True
            return {'job': {'event_id': 'event', 'lease_token': 'lease', 'frozen': self.frozen}}
        assert payload['event_id'] == 'event' and payload['lease_token'] == 'lease'
        if action == 'model':
            return {'job': {'event_id': 'event', 'lease_token': 'lease', 'model_prompt': self.prompt,
                            'model_input_sha256': hashlib.sha256(self.prompt.encode()).hexdigest()}}
        if action == 'propose':
            self.proposal = payload['proposal']
            return {'job': {'state': 'PROPOSED'}}
        if action == 'advance':
            return {'state': 'SAVING_FACTS', 'outcome': 'unknown'} if self.unknown else {'state': 'COMMITTED'}
        if action == 'fail':
            return {'state': 'FAILED'}
        return {'job': {'state': 'PROPOSED'}}


class Model:
    def __init__(self, content='{"facts":[],"observations":[]}'):
        self.content = content
        self.calls = []
        self.gate = None
        self.entered = asyncio.Event()
        self.finish_reason = 'stop'

    async def generate(self, **kwargs):
        assert private_payloads()
        assert kwargs['tools'] is None and kwargs['thinking_enabled'] is False
        assert all(message.trace_redact_content for message in kwargs['messages'])
        self.calls.append(kwargs)
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        return LLMResponse(content=self.content, finish_reason=self.finish_reason)


async def finish(tasks):
    await asyncio.gather(*tuple(tasks), return_exceptions=True)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_producer_consumes_the_exact_native_prompt_without_tools_or_main_history():
    transport, model, tasks = Transport(), Model(), set()
    runtime = ExperienceRuntime(transport)
    runtime.schedule('delivery-session', model, 100000, tasks)
    await finish(tasks)
    assert len(model.calls) == 1
    assert model.calls[0]['messages'][1].content == transport.prompt
    assert transport.proposal == {'facts': [], 'observations': []}
    assert [item['action'] for item in transport.calls] == ['claim', 'model', 'propose', 'renew', 'advance', 'claim']
    assert not private_payloads()
    runtime.close()


@pytest.mark.asyncio
async def test_scheduling_is_nonblocking_and_cancellation_keeps_the_started_model_attempt_for_host_recovery():
    transport, model, tasks = Transport(), Model(), set()
    model.gate = asyncio.Event()
    runtime = ExperienceRuntime(transport)
    assert runtime.schedule('delivery', model, 100000, tasks) is None
    await asyncio.wait_for(model.entered.wait(), 1)
    assert not runtime.task.done()
    runtime.close()
    await finish(tasks)
    assert not any(call['action'] in {'propose', 'fail', 'advance'} for call in transport.calls)
    assert not private_payloads()


@pytest.mark.asyncio
async def test_all_session_producers_share_one_model_slot():
    first, second, tasks = Model(), Model(), set()
    first.gate = asyncio.Event()
    a, b = ExperienceRuntime(Transport()), ExperienceRuntime(Transport())
    a.schedule('first', first, 100000, tasks)
    await first.entered.wait()
    b.schedule('second', second, 100000, tasks)
    await asyncio.sleep(0)
    assert not second.entered.is_set()
    first.gate.set()
    await finish(tasks)
    assert second.entered.is_set()
    a.close(); b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('output', ['{"facts":[],"facts":[],"observations":[]}', '{"facts":NaN,"observations":[]}', '```json\n{}\n```', 'x' * (64 * 1024 + 1)])
async def test_invalid_or_oversized_output_uses_host_attempt_policy_without_persisting_a_proposal(output):
    transport, model, tasks = Transport(), Model(output), set()
    runtime = ExperienceRuntime(transport)
    runtime.schedule('delivery', model, 100000, tasks)
    await finish(tasks)
    assert transport.proposal is None
    failure = next(call for call in transport.calls if call['action'] == 'fail')
    assert failure['reason'] == 'invalid_model_output'
    assert len(model.calls) == 1
    runtime.close()


@pytest.mark.asyncio
async def test_unknown_stage_recovery_skips_model_generation_and_never_starts_a_new_stage():
    transport, model, tasks = Transport(frozen=True), Model(), set()
    transport.unknown = True
    runtime = ExperienceRuntime(transport)
    runtime.schedule('delivery', model, 100000, tasks)
    await finish(tasks)
    assert not model.calls
    assert [call['action'] for call in transport.calls] == ['claim', 'renew', 'advance']
    runtime.close()


@pytest.mark.asyncio
async def test_a_small_real_input_budget_rejects_before_any_model_request():
    transport, model, tasks = Transport(), Model(), set()
    runtime = ExperienceRuntime(transport)
    runtime.schedule('delivery', model, 1, tasks)
    await finish(tasks)
    assert not model.calls
    assert next(call for call in transport.calls if call['action'] == 'fail')['reason'] == 'model_context_too_large'
    runtime.close()


@pytest.mark.asyncio
async def test_uncertain_proposal_receipt_does_not_rerun_the_model_or_replace_the_candidate():
    transport, model, tasks = Transport(), Model(), set()
    actual = transport.request
    async def interrupted(operation, payload):
        if payload['action'] == 'propose':
            transport.calls.append(payload)
            raise MemoryProxyError('RESULT_UNKNOWN', outcome='unknown')
        if payload['action'] == 'fail':
            transport.calls.append(payload)
            raise MemoryProxyError('RECONCILIATION_REQUIRED', outcome='unknown')
        return await actual(operation, payload)
    transport.request = interrupted
    runtime = ExperienceRuntime(transport)
    runtime.schedule('delivery', model, 100000, tasks)
    await finish(tasks)
    assert len(model.calls) == 1
    assert not any(call['action'] == 'advance' for call in transport.calls)
    runtime.close()
