"""Plugin activation, strict model gates and task-safe adapter lifetime."""

import asyncio
from types import SimpleNamespace

import pytest

from box_agent.plugins.cua.config import CuaConfig
from box_agent.plugins.cua.wiring import build_cua_bindings
from box_agent.tools.mcp_result_hooks import current_mcp_result_adapters


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_CUA_VISION", raising=False)


def test_enabled_binding_preserves_llm():
    llm = object()
    binding = build_cua_bindings(llm=llm, config=CuaConfig())
    assert binding.active
    assert binding.llm is llm


@pytest.mark.parametrize("value", ["0", "false", "no", " FALSE "])
def test_environment_kill_switch_disables_vision(value, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_CUA_VISION", value)
    assert not build_cua_bindings(llm=object(), config=CuaConfig()).active


@pytest.mark.parametrize("capability, expected", [(True, True), (False, False), (None, False)])
def test_only_confirmed_image_models_can_inject(capability, expected):
    llm = SimpleNamespace(capabilities={"image_input": capability})
    binding = build_cua_bindings(llm=llm, config=CuaConfig(server_name="desktop"))
    assert binding.allows_transient_followup(
        server_name="desktop", remote_name="screenshot",
    ) is expected
    assert not binding.allows_transient_followup(
        server_name="other", remote_name="screenshot",
    )


def test_capability_change_is_checked_before_each_tool_result():
    llm = SimpleNamespace(capabilities={"image_input": True})
    binding = build_cua_bindings(llm=llm, config=CuaConfig())
    args = dict(server_name="computer-use", remote_name="screenshot")
    assert binding.allows_transient_followup(**args)
    llm.capabilities["image_input"] = False
    assert not binding.allows_transient_followup(**args)


async def test_stream_binds_adapter_per_pull_and_close_across_tasks():
    binding = build_cua_bindings(llm=object(), config=CuaConfig())
    closed = []

    async def events():
        try:
            for index in range(3):
                assert current_mcp_result_adapters() == (binding,)
                yield index
        finally:
            assert current_mcp_result_adapters() == (binding,)
            closed.append(True)

    bound = binding.bind_run(events())
    assert await anext(bound) == 0
    assert current_mcp_result_adapters() == ()
    assert await asyncio.create_task(anext(bound)) == 1
    assert current_mcp_result_adapters() == ()
    await asyncio.create_task(bound.aclose())
    assert closed == [True]
    assert current_mcp_result_adapters() == ()


async def test_stream_cancellation_restores_callers_adapter_scope():
    binding = build_cua_bindings(llm=object(), config=CuaConfig())

    async def events():
        assert current_mcp_result_adapters() == (binding,)
        raise asyncio.CancelledError()
        yield  # make an async generator

    with pytest.raises(asyncio.CancelledError):
        await anext(binding.bind_run(events()))
    assert current_mcp_result_adapters() == ()


async def test_unified_activation_installs_and_restores_plugin_binding(tmp_path):
    from box_agent.agent_session import AgentSession
    from box_agent.events import DoneEvent, StopReason
    from box_agent.session_context import HostBindings, SessionOptions
    from tests.test_agent_session import session_config
    from tests.test_session_plugins import RecordingLLM

    config = session_config(tmp_path)
    config.plugins["cua"] = {"server_name": "my-desktop"}
    llm = RecordingLLM()
    session = await AgentSession.open(
        config=config,
        host=HostBindings(llm_client=llm, system_prompt="system", tools=[]),
        options=SessionOptions(workspace_dir=tmp_path),
    )

    async def fake_run_events(*, options):
        assert options.llm is options.kernel_services.llm is llm
        adapters = current_mcp_result_adapters()
        assert len(adapters) == 1
        assert adapters[0].config.server_name == "my-desktop"
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    session.agent.run_events = fake_run_events
    try:
        events = [e async for e in session.run_events(options=session.build_run_options(logger=None))]
        assert len(events) == 1
        assert current_mcp_result_adapters() == ()
        assert session.agent.run_events is fake_run_events
    finally:
        await session.aclose()


def test_disposal_removes_temporary_instance_method():
    class Agent:
        async def run_events(self):
            yield "done"

    agent = Agent()
    binding = build_cua_bindings(llm=object(), config=CuaConfig())
    binding.install_agent(agent)
    assert "run_events" in vars(agent)
    binding.close()
    binding.close()
    assert "run_events" not in vars(agent)
