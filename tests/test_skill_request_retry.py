"""Restored Skill material survives unaccepted requests and partial log failures."""
from hashlib import sha256

import pytest

from box_agent.agent import Agent, AgentRunOptions
from box_agent.events import ErrorEvent
from box_agent.schema import StreamEvent
from tests.test_skill_entry_boundaries import CapturingProvider


class RejectOnce(CapturingProvider):
    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if len(self.requests) == 1:
            raise ConnectionError("offline connection rejected before output")
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


def agent_with_bodies(tmp_path, provider, bodies):
    agent = Agent(llm_client=provider, system_prompt="BASE", tools=[],
                  workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, max_steps=1)
    agent.restore_active_skill_instructions([
        (name, body, sha256(body.encode()).hexdigest(), order)
        for order, (name, body) in enumerate(bodies.items(), 1)
    ])
    agent.add_user_message("continue")
    return agent


@pytest.mark.asyncio
async def test_restored_body_survives_provider_failure_and_next_turn(tmp_path):
    provider = RejectOnce()
    body = "ORIGINAL_RESTORED_METHOD"
    agent = agent_with_bodies(tmp_path, provider, {"demo": body})
    options = AgentRunOptions(llm=provider, logger=None)
    events = [event async for event in agent.run_events(options=options)]
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert body in str(provider.requests[0])
    agent.add_user_message("retry")
    _ = [event async for event in agent.run_events(options=options)]
    assert len(provider.requests) == 2
    assert body in str(provider.requests[1])


@pytest.mark.asyncio
async def test_all_restored_bodies_survive_partial_ack_failure(tmp_path, monkeypatch):
    provider = CapturingProvider()
    bodies = {"first": "FIRST_RESTORED_BODY", "second": "SECOND_RESTORED_BODY"}
    agent = agent_with_bodies(tmp_path, provider, bodies)
    options = AgentRunOptions(llm=provider, logger=None)
    original = agent.skill_runtime.record_delivery
    failed = False

    def fail_second(snapshot, metadata, *, reason):
        nonlocal failed
        if snapshot.name == "second" and not failed:
            failed = True
            raise OSError("second acknowledgement failed")
        return original(snapshot, metadata, reason=reason)

    monkeypatch.setattr(agent.skill_runtime, "record_delivery", fail_second)
    with pytest.raises(OSError, match="second acknowledgement failed"):
        _ = [event async for event in agent.run_events(options=options)]
    assert provider.requests == []
    _ = [event async for event in agent.run_events(options=options)]
    assert len(provider.requests) == 1
    for body in bodies.values():
        assert body in str(provider.requests[0])


@pytest.mark.parametrize('first_response', ['empty', 'thinking', 'provider_stale', 'length', 'repetitive'])
async def test_unaccepted_response_keeps_restored_body_for_next_run(tmp_path, first_response):
    class RecoveringProvider(CapturingProvider):
        async def generate_stream(self, messages, **kwargs):
            self.requests.append([message.model_copy(deep=True) for message in messages])
            if len(self.requests) == 1:
                if first_response == 'repetitive':
                    for _ in range(20):
                        yield StreamEvent(type='text', delta='</parameter>\n')
                    return
                if first_response == 'thinking':
                    yield StreamEvent(type='thinking', delta='Still thinking')
                elif first_response in {'length', 'provider_stale'}:
                    yield StreamEvent(type='text', delta='Unfinished response')
                yield StreamEvent(type='finish', finish_reason=(
                    first_response if first_response in {'length', 'provider_stale'} else 'stop'))
            else:
                yield StreamEvent(type='text', delta='done')
                yield StreamEvent(type='finish', finish_reason='stop')

    provider = RecoveringProvider()
    body = 'RESTORED_BODY_AFTER_UNACCEPTED_RESPONSE'
    agent = agent_with_bodies(tmp_path, provider, {'demo': body})
    options = AgentRunOptions(llm=provider, logger=None)
    _ = [event async for event in agent.run_events(options=options)]
    agent.add_user_message('retry')
    _ = [event async for event in agent.run_events(options=options)]
    assert len(provider.requests) == 2
    assert body in str(provider.requests[-1])


def test_old_response_cannot_acknowledge_a_later_restore(tmp_path):
    from box_agent.context_input import DefaultContextEngine
    from box_agent.schema import Message
    from box_agent.skill_runtime import SkillRuntime
    from box_agent.tools.engine.contracts import PreparedTools

    runtime = SkillRuntime(None)
    body = 'RESTORED_METHOD'
    records = [{'name': 'demo', 'prompt': body, 'sha256': sha256(body.encode()).hexdigest(), 'loadOrder': 1}]
    runtime.restore_records(records)
    runtime.begin_turn()
    context = DefaultContextEngine()
    context.configure_run(skill_engine=runtime)
    history = [Message(role='system', content='BASE'), Message(role='user', content='continue')]
    tools = PreparedTools(definitions=(), targets={}, call_names={}, mcp_generations={})
    old = context.prepare_request(history, prepared_tools=tools, token_limit=10000)
    old.on_committed()
    runtime.restore_records(records)
    old.on_response()
    runtime.begin_turn()
    assert runtime.restoring_names == ('demo',)
    current = context.prepare_request(history, prepared_tools=tools, token_limit=10000)
    current.on_committed()
    current.on_response()
    current.on_response()
    runtime.begin_turn()
    assert runtime.restoring_names == ()
