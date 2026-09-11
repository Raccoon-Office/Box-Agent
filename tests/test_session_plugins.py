"""Managed sessions preserve model inputs and own scoped plugin resources."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from box_agent.agent_session import AgentSession
from box_agent.schema import StreamEvent
from tests.test_agent_session import session_config


class RecordingLLM:
    def __init__(self, answer="done"):
        self.answer = answer
        self.requests = []
        self.closed = 0

    async def generate_stream(self, **kwargs):
        self.requests.append(kwargs)
        yield StreamEvent(type="text", delta=self.answer)
        yield StreamEvent(type="finish", finish_reason="stop")

    async def aclose(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


@pytest.mark.asyncio
async def test_managed_session_reuses_resources_and_honors_next_run_model(tmp_path, monkeypatch):
    assert hasattr(AgentSession, "open"), "managed session entrypoint is missing"
    from box_agent.plugins.runtime import PluginRuntime
    from box_agent.session_context import HostBindings, SessionOptions
    import box_agent.composition as composition

    def unexpected_default_host(**kwargs):
        pytest.fail("managed run assembled a second default PluginHost")

    monkeypatch.setattr(composition, "create_default_plugin_host", unexpected_default_host)
    config = session_config(tmp_path)
    first, second = RecordingLLM("first"), RecordingLLM("second")
    runtime = PluginRuntime()
    session = await AgentSession.open(
        config=config, runtime=runtime,
        host=HostBindings(llm_client=first, system_prompt="original prompt", tools=[]),
        options=SessionOptions(workspace_dir=tmp_path),
    )
    assert session.config is session.plugin_session.context.config is config
    session.agent.add_user_message("one")
    _ = [event async for event in session.run_events(
        options=session.build_run_options(logger=None),
    )]
    session.agent.add_user_message("two")
    _ = [event async for event in session.run_events(
        options=replace(session.build_run_options(logger=None), llm=second),
    )]
    assert len(first.requests) == len(second.requests) == 1
    assert "original prompt" in first.requests[0]["messages"][0].content
    await session.aclose()
    await session.aclose()
    await runtime.aclose()
    assert first.closed == second.closed == 0  # explicitly borrowed clients
    with pytest.raises(RuntimeError, match="closed"):
        _ = [event async for event in session.run_events()]


@pytest.mark.asyncio
async def test_runtime_isolates_sessions_and_releases_only_the_owned_scope(tmp_path):
    assert hasattr(AgentSession, "open"), "managed session entrypoint is missing"
    from box_agent.plugins.descriptors import PluginDescriptor, PluginScope
    from box_agent.plugins.runtime import PluginRuntime
    from box_agent.plugins.registries import CapabilityBinding, CapabilityPolicy
    from box_agent.session_context import HostBindings, SessionOptions

    class SessionPort:
        pass

    class RunPort:
        pass

    created, disposed, contexts = [], [], []

    def create_session(factory_context):
        resource = SessionPort()
        contexts.append(factory_context.context)
        created.append(resource)
        return resource

    def create_run(factory_context):
        resource = RunPort()
        created.append(resource)
        return resource

    runtime = PluginRuntime(
        plugins=(
            PluginDescriptor("test.session", "1.0.0", (SessionPort,),
                             scope=PluginScope.SESSION, context_factory=create_session,
                             disposer=disposed.append),
            PluginDescriptor("test.run", "1.0.0", (RunPort,),
                             scope=PluginScope.RUN, context_factory=create_run,
                             dependencies=("test.session",), disposer=disposed.append),
        ),
        bindings=(CapabilityBinding(SessionPort, CapabilityPolicy.REQUIRED_SINGLE),
                  CapabilityBinding(RunPort, CapabilityPolicy.REQUIRED_SINGLE)),
    )
    sessions = []
    for name in ("one", "two"):
        sessions.append(await AgentSession.open(
            config=session_config(tmp_path / name), runtime=runtime,
            host=HostBindings(llm_client=RecordingLLM(), system_prompt="system", tools=[]),
            options=SessionOptions(workspace_dir=tmp_path / name),
        ))
    assert len(created) == 2
    assert contexts[0].config is sessions[0].config
    assert contexts[1].config is sessions[1].config
    for _ in range(2):
        sessions[0].agent.add_user_message("hello")
        _events = [event async for event in sessions[0].run_events(
            options=sessions[0].build_run_options(logger=None),
        )]
    assert len(created) == 4
    assert disposed == created[2:]
    await sessions[0].aclose()
    assert disposed == [created[2], created[3], created[0]]
    sessions[1].agent.add_user_message("still live")
    _events = [event async for event in sessions[1].run_events(
        options=sessions[1].build_run_options(logger=None),
    )]
    await runtime.aclose()
    assert disposed.count(created[1]) == 1


@pytest.mark.asyncio
async def test_open_failure_rolls_back_prepared_plugin_resources(tmp_path):
    assert hasattr(AgentSession, "open"), "managed session entrypoint is missing"
    from box_agent.plugins.descriptors import PluginDescriptor, PluginScope
    from box_agent.plugins.runtime import PluginRuntime
    from box_agent.plugins.registries import CapabilityBinding, CapabilityPolicy
    from box_agent.session_context import HostBindings

    class Resource:
        pass

    closed = []
    runtime = PluginRuntime(
        plugins=(PluginDescriptor("test.owned", "1.0.0", (Resource,), factory=Resource,
                                  scope=PluginScope.SESSION, disposer=closed.append),),
        bindings=(CapabilityBinding(Resource, CapabilityPolicy.REQUIRED_SINGLE),),
    )

    def failing_agent(**kwargs):
        raise ValueError("constructor failed")

    with pytest.raises(ValueError, match="constructor failed"):
        await AgentSession.open(
            config=session_config(tmp_path), runtime=runtime, agent_factory=failing_agent,
            host=HostBindings(llm_client=RecordingLLM(), system_prompt="system", tools=[]),
        )
    assert len(closed) == 1
    await runtime.aclose()
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_open_builds_tools_prompt_and_skill_state_from_config(tmp_path):
    from box_agent.session_context import HostBindings, SessionOptions

    config = session_config(tmp_path)
    config.agent.enable_memory = False
    template = tmp_path / "system.md"
    template.write_text("Configured prompt\n{SKILLS_METADATA}\n{SANDBOX_INFO}\n{FILE_DELIVERY_INFO}",
                        encoding="utf-8")
    config.agent.system_prompt_path = str(template)
    config.tools.enable_file_tools = True
    session = await AgentSession.open(
        config=config,
        host=HostBindings(llm_client=RecordingLLM(), output=lambda _: None),
        options=SessionOptions(workspace_dir=tmp_path),
    )
    try:
        assert "Configured prompt" in session.agent.messages[0].content
        assert "{SKILLS_METADATA}" not in session.agent.messages[0].content
        assert "read_file" in session.agent.tools
        assert "bash" not in session.agent.tools
        assert session.skill_loader is None
    finally:
        await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_effort", [None, "none", "low"])
async def test_session_closes_transport_of_client_created_from_config(tmp_path, disabled_effort):
    from box_agent.session_context import HostBindings

    config = session_config(tmp_path)
    config.llm.reasoning_effort_when_disabled = disabled_effort
    session = await AgentSession.open(
        config=config,
        host=HostBindings(tools=[], system_prompt="system"),
    )
    transport = session.agent.llm._client.client
    try:
        assert not transport.is_closed()
        assert session.agent.llm.reasoning_effort_when_disabled == disabled_effort
    finally:
        await session.aclose()
    assert transport.is_closed()
