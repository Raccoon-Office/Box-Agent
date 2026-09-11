"""PR #117 boundary regressions using public hosts and real log replay."""

from dataclasses import replace
from hashlib import sha256

import pytest

from box_agent.agent import Agent
from box_agent.agent_session import AgentSession
from box_agent.config import Config
from box_agent.events import ErrorEvent
from box_agent.schema import LLMResponse, Message
from box_agent.session_log import SessionLog
from box_agent.skill_dependencies import SkillDependencyError
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_preload import build_active_skills_prompt
from box_agent.tools.skill_tool import GetSkillTool
from box_agent.tools.sub_agent_tool import SubAgentTool
from tests.test_skill_entry_boundaries import CapturingProvider, loader_at


class SummaryProvider(CapturingProvider):
    async def generate(self, *args, **kwargs):
        return LLMResponse(content='<summary>Continue without the removed Skill.</summary>',
                           finish_reason='stop')


@pytest.mark.parametrize('operation', ['clear', 'deactivate'])
@pytest.mark.parametrize('child_case', [False, True])
async def test_removed_legacy_skill_is_absent_before_budget_and_child_request(tmp_path, operation, child_case):
    provider = SummaryProvider()
    child = SubAgentTool(llm=provider, parent_tools={}, workspace_dir=str(tmp_path))
    agent = Agent(llm_client=provider, system_prompt='BASE', tools=[child] if child_case else [],
                  workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False,
                  max_steps=1, token_limit=5000)
    body = 'REMOVED_LEGACY_BODY' if child_case else 'LEGACY_LINE\n' * 3000
    agent.restore_active_skill_instructions([('demo', body, sha256(body.encode()).hexdigest(), 1)])
    old_system = build_active_skills_prompt('BASE', {'demo': body})
    agent.set_system_prompt(old_system)
    if operation == 'clear':
        agent.clear_active_skill_instructions()
    else:
        assert agent.deactivate_skill_instructions('demo')
    if child_case:
        result = await child.execute(task='Work without assigned skills', required_tools=[])
        assert result.success
    else:
        agent.add_user_message('Continue without removed Skill')
        events = [event async for event in agent.run_events()]
        assert not [event.message for event in events if isinstance(event, ErrorEvent)]
    assert provider.requests
    assert body not in str(provider.requests)
    assert agent.messages[0].content == old_system


@pytest.mark.parametrize('child_case', [False, True])
async def test_constructor_legacy_cleanup_preserves_host_rules_and_raw_history(tmp_path, child_case):
    provider = SummaryProvider()
    body = 'CONSTRUCTOR_LEGACY_BODY\n' * 3000
    original = build_active_skills_prompt('BASE', {'demo': body})
    child = SubAgentTool(llm=provider, parent_tools={}, workspace_dir=str(tmp_path))
    agent = Agent(llm_client=provider, system_prompt=original, tools=[child] if child_case else [],
                  workspace_dir=str(tmp_path), max_steps=1, token_limit=5000)
    host_system = agent.messages[0]
    agent.restore_active_skill_instructions([('demo', body, sha256(body.encode()).hexdigest(), 1)])
    agent.clear_active_skill_instructions()
    if child_case:
        result = await child.execute(task='Continue with no assigned Skill', required_tools=[])
        assert result.success
    else:
        agent.add_user_message('Continue without the removed Skill')
        events = [event async for event in agent.run_events()]
        assert not [event.message for event in events if isinstance(event, ErrorEvent)]
    assert provider.requests
    sent = str(provider.requests)
    assert 'CONSTRUCTOR_LEGACY_BODY' not in sent
    assert 'Current Workspace' in sent
    if not child_case:
        assert 'Discoverable tools' in sent
    assert agent.messages[0] is host_system
    assert body in host_system.content


class Store:
    def __init__(self):
        self.rows = []

    def append(self, event, data, **kwargs):
        self.rows.append(event)
        return {}

    def append_unlogged_messages(self, messages, **kwargs):
        return []

    def replace_surface(self, messages, **kwargs):
        return []

    def flush(self):
        pass


@pytest.mark.parametrize('matching', [False, True])
async def test_store_plugin_replacement_requires_matching_skill_persistence(tmp_path, monkeypatch, matching):
    import box_agent.composition as composition
    from box_agent.core import run_agent_loop
    from box_agent.kernel.ports import SessionStorePort
    from box_agent.plugins.defaults import DEFAULT_CAPABILITY_SCHEMA
    from box_agent.plugins.host import PluginHost

    original, replacement = Store(), Store()
    runtime = SkillRuntime(None, session_log=replacement if matching else original)
    runtime.register_reference('demo', 'Read and verify.', persist=False)
    original_factory = composition.create_default_plugin_host
    hosts = []

    def replaced_host(**capabilities):
        source = original_factory(**capabilities)
        hosts.append(source)
        host = PluginHost(tuple(
            replace(descriptor, factory=lambda: replacement)
            if descriptor.capabilities == (SessionStorePort,) else descriptor
            for descriptor in source.discover()), schema=DEFAULT_CAPABILITY_SCHEMA)
        hosts.append(host)
        return host

    monkeypatch.setattr(composition, 'create_default_plugin_host', replaced_host)
    provider = CapturingProvider()

    async def run():
        return [event async for event in run_agent_loop(
            llm=provider, tools={}, messages=[Message(role='system', content='BASE'),
                                            Message(role='user', content='Read')],
            skill_engine=runtime, session_log=original, session_turn=1, max_steps=1)]

    try:
        if matching:
            await run()
            assert 'skill/change' in replacement.rows
            assert 'request/context' in replacement.rows
        else:
            with pytest.raises(ValueError, match='Skill.*Store'):
                await run()
            assert provider.requests == []
            assert replacement.rows == []
        assert original.rows == []
        assert runtime.session_log is (replacement if matching else original)
        assert hosts[-1]._closed
    finally:
        for host in hosts:
            await host.close()


BAD_FIELDS = [
    {'loadOrder': '1'}, {'loadOrder': True}, {'loadOrder': -1}, {'loadOrder': 0},
    {'deliveredRanges': [[0]]}, {'deliveredRanges': [[0, '1']]},
    {'deliveredRanges': [[False, 1]]}, {'deliveredRanges': [[-1, 2]]},
    {'deliveredRanges': [[2, 1]]}, {'deliveredRanges': [[0, 10000000]]},
    {'deliveredRanges': None}, {'deliveredComplete': 'true'},
    {'name': []}, {'sha256': None}, {'source': 1},
]


@pytest.mark.parametrize('bad_fields', BAD_FIELDS)
def test_invalid_skill_log_rejected_before_restore_replaces_state(tmp_path, bad_fields):
    loader = loader_at(tmp_path / 'skills')
    runtime = SkillRuntime(loader)
    runtime.register_reference('keep', 'Existing state', persist=False)
    before_state = runtime.state
    prompt = loader.get_skill('demo').to_prompt()
    record = {'name': 'demo', 'sha256': sha256(prompt.encode()).hexdigest(), 'loadOrder': 1,
              'deliveredRanges': [[0, 1]], 'deliveredComplete': False, **bad_fields}
    log = SessionLog.create(tmp_path / 'sessions', session_id='bad-skill', cwd=tmp_path)
    log.append('skill/change', {'skills': [record]})
    log.flush()
    log.close()
    log = SessionLog.open(tmp_path / 'sessions', session_id='bad-skill', cwd=tmp_path)
    before_log = log.path.read_bytes()
    try:
        with pytest.raises(SkillDependencyError, match='Invalid Skill restore'):
            Agent(llm_client=CapturingProvider(), system_prompt='BASE',
                  tools=[GetSkillTool(loader)], skill_runtime=runtime, session_log=log,
                  workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False)
        assert runtime.state is before_state
        assert log.path.read_bytes() == before_log
    finally:
        log.close()


def test_valid_legacy_skill_log_can_be_restored_and_read(tmp_path):
    loader = loader_at(tmp_path / 'skills')
    runtime = SkillRuntime(loader)
    runtime.restore_records([{'name': 'demo', 'loadOrder': 1,
                             'sha256': sha256(loader.get_skill('demo').to_prompt().encode()).hexdigest()}])
    assert runtime.read('demo').success


@pytest.mark.parametrize('existing_store', [False, True])
def test_sync_composition_enforces_the_same_skill_store_binding(tmp_path, existing_store):
    from box_agent.composition import compose_default_kernel_services

    loader = loader_at(tmp_path / 'skills')
    old, new = Store(), Store()
    runtime = SkillRuntime(loader, session_log=old if existing_store else None)
    arguments = dict(llm=CapturingProvider(), tools={'get_skill': GetSkillTool(loader)},
                     skill_engine=runtime, session_log=new)
    if existing_store:
        with pytest.raises(ValueError, match='Skill.*Store'):
            compose_default_kernel_services(arguments)
        assert runtime.session_log is old
    else:
        services = compose_default_kernel_services(arguments)
        assert services.skill_engine.read('demo').success
        assert 'skill/change' in new.rows
        auto = compose_default_kernel_services({key: value for key, value in arguments.items()
                                               if key != 'skill_engine'})
        assert auto.skill_engine.session_log is new


def _public_agent(entrypoint, **kwargs):
    if entrypoint == 'agent':
        return Agent(deferred_mcp_loading_enabled=False, **kwargs)
    return AgentSession.create(
        config=Config(llm={'model': 'fixture'}, agent={}, tools={'enable_mcp': False}),
        **kwargs,
    ).agent


@pytest.mark.parametrize('entrypoint', ['agent', 'session'])
@pytest.mark.parametrize('omit_store', [False, True])
def test_public_host_rejects_transferring_bound_skill_facts(tmp_path, entrypoint, omit_store):
    old = SessionLog.create(tmp_path / 'sessions', session_id='old', cwd=tmp_path)
    new = SessionLog.create(tmp_path / 'sessions', session_id='new', cwd=tmp_path)
    runtime = SkillRuntime(None, session_log=old)
    runtime.register_reference('old-method', 'Existing caller method')
    original_state = runtime.state
    before_old, before_new = old.path.read_bytes(), new.path.read_bytes()
    try:
        with pytest.raises(ValueError, match='Skill.*Store'):
            _public_agent(
                entrypoint, llm_client=CapturingProvider(), system_prompt='BASE', tools=[],
                workspace_dir=str(tmp_path), skill_runtime=runtime,
                session_log=None if omit_store else new,
            )
        assert runtime.session_log is old
        assert runtime.state is original_state
        assert old.path.read_bytes() == before_old
        assert new.path.read_bytes() == before_new
    finally:
        old.close()
        new.close()


@pytest.mark.parametrize('entrypoint', ['agent', 'session'])
@pytest.mark.parametrize('already_bound', [False, True])
@pytest.mark.parametrize('falsey', [False, True])
def test_public_host_preserves_compatible_skill_runtime(tmp_path, entrypoint, already_bound, falsey):
    class Runtime(SkillRuntime):
        def __bool__(self):
            return not falsey

    log = SessionLog.create(tmp_path / 'sessions', session_id='matching', cwd=tmp_path)
    runtime = Runtime(None, session_log=log if already_bound else None)
    runtime.register_reference('known', 'Known caller method', persist=False)
    try:
        agent = _public_agent(
            entrypoint, llm_client=CapturingProvider(), system_prompt='BASE', tools=[],
            workspace_dir=str(tmp_path), skill_runtime=runtime, session_log=log,
        )
        assert agent.skill_runtime is runtime
        assert runtime.session_log is log
        runtime.register_reference('new-method', 'New caller method')
        assert {record['name'] for record in log.replay().skills} == {'known', 'new-method'}
    finally:
        log.close()


@pytest.mark.parametrize('second_restore', ['empty', 'changed'])
def test_repeated_restore_preserves_verified_suffix_evidence(second_restore):
    from box_agent.context_input import DefaultContextEngine

    runtime = SkillRuntime(None)
    old_body = 'OLD_VERIFIED_BODY'
    row = {'name': 'demo', 'prompt': old_body, 'sha256': sha256(old_body.encode()).hexdigest(), 'loadOrder': 1}
    runtime.restore_records([row])
    history = [Message(role='system', content=build_active_skills_prompt('BASE', {'demo': old_body}))]
    runtime.restore_records([] if second_restore == 'empty' else [{**row, 'prompt': 'NEW_BODY'}])
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    assert engine.project_history(history)[0].content == 'BASE'
    assert old_body in history[0].content


@pytest.mark.parametrize("damage", ["missing-hash", "missing-order", "unavailable", "bad-coverage"])
def test_partial_restore_preserves_facts_without_asserting_unverified_legacy_body(tmp_path, damage):
    from copy import deepcopy

    loader = loader_at(tmp_path / "skills")
    body = loader.get_skill("demo").to_prompt()
    valid = {"name": "demo", "sha256": sha256(body.encode()).hexdigest(), "loadOrder": 1}
    records = [dict(valid)]
    if damage == "missing-hash":
        del records[0]["sha256"]
    elif damage == "missing-order":
        del records[0]["loadOrder"]
    elif damage == "unavailable":
        records.append({"name": "removed", "sha256": "old", "loadOrder": 2})
    else:
        records[0]["deliveredRanges"] = [[0, 9999]]
    before = deepcopy(records)
    store = Store()
    runtime = SkillRuntime(loader, session_log=store, allow_partial_restore=True)
    runtime.restore_records(records)
    assert runtime.active_names == (() if damage == "bad-coverage" else ("demo",))
    assert runtime.legacy_system_suffixes == ()
    assert store.rows == []
    assert records == before
    with pytest.raises(SkillDependencyError):
        SkillRuntime(loader).restore_records(records)


@pytest.mark.asyncio
async def test_delegation_honors_parent_skill_reader_access_filter(tmp_path):
    loader = loader_at(tmp_path / "skills")
    provider = CapturingProvider()
    reader = GetSkillTool(loader, skill_access_filter=lambda skill: False)
    child = SubAgentTool(llm=provider, parent_tools={"get_skill": reader}, workspace_dir=str(tmp_path))
    child.set_skill_provider(lambda: loader)
    result = await child.execute(task="Use the method", skills=["demo"], required_tools=[])
    assert not result.success
    assert "not enabled for this conversation" in result.error
    assert provider.requests == []
