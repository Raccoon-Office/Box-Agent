from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import box_agent.agent as agent_module
import box_agent.composition as composition
from box_agent.agent import Agent
from box_agent.events import ContentEvent, DoneEvent, StopReason
from box_agent.hooks import BaseHook, HookManager
from box_agent.kernel.ports import KernelServices
from box_agent.schema import StreamEvent


class DeterministicLLM:
    async def generate_stream(self, **_kwargs):
        yield StreamEvent(type="text", delta="managed")
        yield StreamEvent(type="finish", finish_reason="stop")


def managed_services(*, llm, tools, hooks=None, **overrides) -> KernelServices:
    values = {
        "llm": llm,
        "summary_llm": None,
        "permission_gateway": None,
        "memory_lookup": None,
        "memory_extraction": None,
        "memory_promotion": None,
        "session_store": None,
        "hook_bus": HookManager(hooks),
        "tool_catalog": tools,
        "tool_exposure": None,
        "tool_result_store": None,
    }
    values.update(overrides)
    return KernelServices(**values)


@pytest.mark.asyncio
async def test_agent_forwards_managed_services_only_when_supplied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def fake_run_agent_loop(**kwargs):
        calls.append(kwargs)
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    monkeypatch.setattr(agent_module, "run_agent_loop", fake_run_agent_loop)
    llm = DeterministicLLM()
    agent = Agent(llm_client=llm, system_prompt="system", tools=[], workspace_dir=tmp_path)

    _ = [event async for event in agent.run_events()]
    services = managed_services(
        llm=llm,
        tools=agent.tools,
        tool_exposure=agent.mcp_tool_exposure,
        tool_result_store=agent.tool_result_storage,
    )
    options = replace(agent.default_run_options(), kernel_services=services)
    _ = [event async for event in agent.run_events(options=options)]

    assert "kernel_services" not in calls[0]
    assert calls[1]["kernel_services"] is services


@pytest.mark.asyncio
async def test_managed_services_run_real_event_stream_without_default_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_default_host(**_kwargs):
        pytest.fail("managed execution created a default PluginHost")

    monkeypatch.setattr(composition, "create_default_plugin_host", unexpected_default_host)
    llm = DeterministicLLM()
    agent = Agent(llm_client=llm, system_prompt="system", tools=[], workspace_dir=tmp_path)
    agent.add_user_message("hello")
    services = managed_services(
        llm=llm,
        tools=agent.tools,
        tool_exposure=agent.mcp_tool_exposure,
        tool_result_store=agent.tool_result_storage,
    )

    events = [
        event
        async for event in agent.run_events(
            options=replace(agent.default_run_options(), kernel_services=services)
        )
    ]

    assert any(isinstance(event, ContentEvent) and event.content == "managed" for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["llm", "tools"])
async def test_managed_services_reject_contradictory_required_capabilities_before_model(
    tmp_path: Path,
    mismatch: str,
) -> None:
    llm = DeterministicLLM()
    agent = Agent(llm_client=llm, system_prompt="system", tools=[], workspace_dir=tmp_path)
    agent.add_user_message("hello")
    services = managed_services(
        llm=object() if mismatch == "llm" else llm,
        tools={} if mismatch == "tools" else agent.tools,
    )

    with pytest.raises(ValueError, match=mismatch):
        _ = [
            event
            async for event in agent.run_events(
                options=replace(agent.default_run_options(), kernel_services=services)
            )
        ]


@pytest.mark.asyncio
async def test_managed_services_preserve_optional_capability_identity_and_run_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    observed = []

    class Legacy(BaseHook):
        async def on_agent_start(self, **kwargs):
            observed.append(self)

    class Kernel:
        def __init__(self, *, _services, **_kwargs):
            captured["services"] = _services

        async def run(self):
            await captured["services"].hook_bus.fire_agent_start(
                messages=[], tools={}, max_steps=1,
            )
            yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    monkeypatch.setattr(composition, "AgentLoopKernel", Kernel)
    llm = DeterministicLLM()
    agent = Agent(llm_client=llm, system_prompt="system", tools=[], workspace_dir=tmp_path)
    permission = object()
    memory = object()
    extractor = object()
    exposure = object()
    result_store = object()
    hooks = [Legacy()]
    services = managed_services(
        llm=llm,
        tools=agent.tools,
        hooks=hooks,
        permission_gateway=permission,
        memory_lookup=memory,
        memory_extraction=extractor,
        tool_exposure=exposure,
        tool_result_store=result_store,
    )
    options = replace(
        agent.default_run_options(),
        kernel_services=services,
        permission_negotiator=permission,
        memory_manager=memory,
        memory_extractor=extractor,
        hooks=hooks,
    )
    agent.mcp_tool_exposure = exposure
    agent.tool_result_storage = result_store

    _ = [event async for event in agent.run_events(options=options)]

    resolved = captured["services"]
    assert resolved.llm is services.llm
    assert resolved.summary_llm is services.summary_llm
    assert resolved.session_store is services.session_store
    assert resolved.tool_catalog is services.tool_catalog
    assert resolved.tool_engine is services.tool_engine
    assert resolved.permission_gateway is permission
    assert resolved.memory_lookup is memory
    assert resolved.memory_extraction is extractor
    assert resolved.tool_exposure is exposure
    assert resolved.tool_result_store is result_store
    assert observed == hooks and observed[0] is hooks[0]
    assert services.hook_bus.hooks[0] is hooks[0]
    assert resolved.hook_dispatch is resolved.hook_bus
    assert resolved.hook_context is resolved.hook_bus.context


@pytest.mark.asyncio
async def test_managed_execution_closes_inner_kernel_stream_on_early_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    class Kernel:
        def __init__(self, **_kwargs):
            pass

        async def run(self):
            nonlocal closed
            try:
                yield ContentEvent(content="first")
                yield ContentEvent(content="second")
            finally:
                closed = True

    monkeypatch.setattr(composition, "AgentLoopKernel", Kernel)
    monkeypatch.setattr(
        composition,
        "create_default_plugin_host",
        lambda **_kwargs: pytest.fail("managed execution created a default PluginHost"),
    )
    llm = DeterministicLLM()
    tools = {}
    stream = composition.run_agent_loop_with_default_services(
        run_arguments={
            "llm": llm,
            "summary_llm": None,
            "tools": tools,
            "hooks": [],
            "kernel_services": managed_services(llm=llm, tools=tools),
        },
        runtime_defaults=object(),
    )

    assert isinstance(await anext(stream), ContentEvent)
    await stream.aclose()

    assert closed is True


@pytest.mark.asyncio
async def test_managed_execution_preserves_kernel_error_when_stream_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_error = RuntimeError("kernel failed")
    cleanup_error = ValueError("stream cleanup failed")

    class Events:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise execution_error

        async def aclose(self):
            raise cleanup_error

    class Kernel:
        def __init__(self, **_kwargs):
            pass

        def run(self):
            return Events()

    monkeypatch.setattr(composition, "AgentLoopKernel", Kernel)
    monkeypatch.setattr(
        composition,
        "create_default_plugin_host",
        lambda **_kwargs: pytest.fail("managed execution created a default PluginHost"),
    )
    llm = DeterministicLLM()
    tools = {}
    stream = composition.run_agent_loop_with_default_services(
        run_arguments={
            "llm": llm,
            "summary_llm": None,
            "tools": tools,
            "hooks": [],
            "kernel_services": managed_services(llm=llm, tools=tools),
        },
        runtime_defaults=object(),
    )

    with pytest.raises(RuntimeError, match="kernel failed") as caught:
        await anext(stream)

    assert caught.value is execution_error
    assert caught.value.__cause__ is cleanup_error
