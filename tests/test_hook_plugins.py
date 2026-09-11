"""验证插件从公共运行入口到工具执行、结果发布和清理的完整链路。"""

import asyncio
from dataclasses import replace

import pytest

from box_agent.agent import Agent
from box_agent.agent_session import AgentSession
from box_agent.config import Config
from box_agent.events import ToolCallResult
from box_agent.hooks import BaseHook, HookConfigError
from box_agent.kernel.hook_types import BeforeToolDecision, ResultTextDecision
from box_agent.plugins.descriptors import PluginDescriptor, PluginScope
from box_agent.plugins.hooks import HookProviderPort, HookSpec
from box_agent.plugins.host import PluginScopeError
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.session_context import HostBindings, SessionOptions
from box_agent.tools.base import Tool, ToolResult


class Model:
    """首步调用工具，次步记录看到的历史并结束。"""

    model = "fixture"

    def __init__(self, arguments=None):
        self.arguments = arguments if arguments is not None else [{"text": "original"}]
        self.requests = 0
        self.history = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests += 1
        self.history = list(messages)
        if self.requests == 1:
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                ToolCall(id=f"call-{index}", type="function", function=FunctionCall(name="echo", arguments=args))
                for index, args in enumerate(self.arguments)
            ])
        else:
            yield StreamEvent(type="text", delta="完成")
            yield StreamEvent(type="finish", finish_reason="stop")


class Echo(Tool):
    name = "echo"
    description = "返回输入文本"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def __init__(self, parallel=False, success=True):
        self.parallel_safe = parallel
        self.calls = []
        self.success = success

    async def execute(self, text):
        self.calls.append(text)
        return ToolResult(success=self.success, content=text, error=None if self.success else "原始失败",
                          model_context="原始模型专用文本")


def plugin(specs, *, name="fixture.hooks", factory=None, disposer=None, dependencies=()):
    class Provider:
        def get_hooks(self):
            return tuple(specs)

    return PluginDescriptor(name, "1.0.0", (HookProviderPort,), factory or Provider,
                            dependencies=dependencies, disposer=disposer)


async def run(tool, specs, *, arguments=None, hooks=None, **kwargs):
    messages = [Message(role="user", content="执行")]
    model = Model(arguments)
    events = [event async for event in run_agent_loop(
        llm=model, tools={"echo": tool}, messages=messages, hooks=hooks,
        plugins=(plugin(specs),), max_steps=2, artifact_detection_enabled=False, **kwargs,
    )]
    return events, messages, model


@pytest.mark.parametrize("parallel", [False, True])
async def test_plugin_parameters_and_text_reach_tools_model_and_host_once(parallel):
    seen = []
    finished = []

    async def before(ctx):
        seen.append("before")
        return BeforeToolDecision.modify({"text": "changed"})

    async def after(ctx):
        seen.append("after")
        assert ctx.payload["content"] == "changed"
        return ResultTextDecision.replace("已脱敏")

    async def observer(ctx):
        finished.append(ctx)

    tool = Echo(parallel)
    events, messages, _ = await run(tool, [
        HookSpec("before", ("tool.before_execution",), "before_tool", before),
        HookSpec("after", ("tool.after_execution",), "result_text", after),
        HookSpec("finished", ("tool.finished",), "observer", observer),
    ])
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert tool.calls == ["changed"] and seen == ["before", "after"]
    assert result.success and result.content == "已脱敏"
    assert [message.content for message in messages if message.role == "tool"] == ["已脱敏"]
    assert len(finished) == 1 and finished[0].payload["executed"] is True


@pytest.mark.parametrize("parallel", [False, True])
async def test_denied_call_skips_execution_and_new_after_hooks_but_finishes(parallel):
    finished, after_calls, legacy_calls = [], [], []

    async def deny(ctx):
        return BeforeToolDecision.deny("策略拒绝", "POLICY_DENIED")

    async def after(ctx):
        after_calls.append(True)
        return ResultTextDecision.keep()

    async def observer(ctx):
        finished.append(ctx.payload)

    class Legacy(BaseHook):
        async def on_tool_result(self, **kwargs):
            legacy_calls.append(kwargs["success"])

    tool = Echo(parallel)
    events, messages, _ = await run(tool, [
        HookSpec("deny", ("tool.before_execution",), "before_tool", deny),
        HookSpec("after", ("tool.after_execution",), "result_text", after),
        HookSpec("finished", ("tool.finished",), "observer", observer),
    ], hooks=[Legacy()])
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert not tool.calls and not after_calls and legacy_calls == [False]
    assert not result.success and "POLICY_DENIED" in result.error
    assert result.raw_output["source"] == "plugin:fixture.hooks"
    assert len(finished) == 1 and finished[0]["executed"] is False
    assert len([message for message in messages if message.role == "tool"]) == 1


@pytest.mark.parametrize("parallel", [False, True])
async def test_modified_arguments_are_validated_before_execution(parallel):
    async def invalid(ctx):
        return BeforeToolDecision.modify({"text": 123})

    finished = []

    async def observe(ctx):
        finished.append(ctx.payload["executed"])

    tool = Echo(parallel)
    events, _, _ = await run(tool, [
        HookSpec("invalid", ("tool.before_execution",), "before_tool", invalid),
        HookSpec("finished", ("tool.finished",), "observer", observe),
    ])
    assert not tool.calls and finished == [False]
    assert "INVALID_TOOL_ARGUMENTS" in next(event.error for event in events if isinstance(event, ToolCallResult))


@pytest.mark.parametrize("granted", [False, True])
async def test_permission_retry_uses_final_arguments_without_repeating_hooks(granted):
    seen, finished = [], []

    class Gate(Echo):
        approved = False

        def approve_permission_request(self, request):
            self.approved = True

        async def execute(self, text):
            if not self.approved:
                return ToolResult(success=False, permission_request={"scope": "fixture", "command": text})
            return await super().execute(text)

    class Approval:
        async def negotiate(self, request):
            assert request["command"] == "final"
            return granted

    async def before(ctx):
        seen.append("before")
        return BeforeToolDecision.modify({"text": "final"})

    async def after(ctx):
        seen.append("after")
        return ResultTextDecision.keep()

    async def observer(ctx):
        finished.append(ctx.payload["executed"])

    tool = Gate()
    await run(tool, [
        HookSpec("before", ("tool.before_execution",), "before_tool", before),
        HookSpec("after", ("tool.after_execution",), "result_text", after),
        HookSpec("finished", ("tool.finished",), "observer", observer),
    ], permission_negotiator=Approval())
    assert seen == (["before", "after"] if granted else ["before"])
    assert tool.calls == (["final"] if granted else []) and finished == [granted]


@pytest.mark.parametrize("success", [True, False])
async def test_suppression_preserves_execution_status_and_replaces_model_text(success):
    async def suppress(ctx):
        return ResultTextDecision.suppress("隐藏文本")

    tool = Echo(success=success)
    events, messages, _ = await run(tool, [HookSpec("hide", ("tool.after_execution",), "result_text", suppress)])
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert result.success is success and tool.calls == ["original"]
    history = next(message.content for message in messages if message.role == "tool")
    assert "抑制" in result.content and "抑制" in history
    assert "原始" not in history and "original" not in history and "None" not in history


@pytest.mark.parametrize("managed", [False, True])
async def test_session_reuses_configuration_but_activates_fresh_run_plugins(tmp_path, managed):
    instances, runs, disposed = [], [], []

    class Provider:
        def __init__(self):
            instances.append(self)

        def get_hooks(self):
            async def observe(ctx):
                runs.append(ctx.run_id)
            return (HookSpec("start", ("run.started",), "observer", observe),)

    descriptor = plugin([], factory=Provider, disposer=lambda instance: disposed.append(instance))
    config = Config(llm={"model": "fixture"}, agent={}, tools={})
    if managed:
        session = await AgentSession.open(
            config=config,
            options=SessionOptions(workspace_dir=tmp_path, utility=True),
            host=HostBindings(llm_client=Model([]), tools=[], system_prompt="测试"),
            plugins=(descriptor,),
        )
    else:
        session = AgentSession.create(config=config, llm_client=Model([]), tools=[],
                                      system_prompt="测试", workspace_dir=tmp_path,
                                      plugins=(descriptor,), utility=True)
    try:
        for _ in range(2):
            _ = [event async for event in session.run_events()]
        assert len(instances) == len(disposed) == 2 and instances[0] is not instances[1]
        assert len(set(runs)) == 2
        if managed:
            assert not session.plugin_session.runtime.host._closed
    finally:
        await session.aclose()


async def test_invalid_registration_rolls_back_plugins_before_model_runs():
    disposed = []
    model = Model()

    async def observe(ctx):
        pass

    descriptor = plugin([
        HookSpec("duplicate", ("run.started",), "observer", observe),
        HookSpec("duplicate", ("run.started",), "observer", observe),
    ], disposer=lambda instance: disposed.append(instance))
    with pytest.raises(HookConfigError):
        _ = [event async for event in run_agent_loop(llm=model, tools={}, messages=[], plugins=(descriptor,))]
    assert model.requests == 0 and len(disposed) == 1


async def test_non_run_plugins_are_rejected_without_activation():
    created = []
    descriptor = replace(plugin([], factory=lambda: created.append(True)), scope=PluginScope.SESSION)
    with pytest.raises(PluginScopeError):
        _ = [event async for event in run_agent_loop(llm=Model(), tools={}, messages=[], plugins=(descriptor,))]
    assert not created


async def test_early_stream_close_releases_plugins_and_legacy_objects_remain_owned_by_caller():
    disposed, observed = [], []

    class Legacy(BaseHook):
        async def on_agent_start(self, **kwargs):
            observed.append("legacy")

        def close(self):
            pytest.fail("宿主传入的旧 Hook 不能被插件宿主销毁")

    async def observer(ctx):
        observed.append("plugin")

    descriptor = plugin([HookSpec("start", ("run.started",), "observer", observer)],
                        disposer=lambda instance: disposed.append(instance))
    events = run_agent_loop(llm=Model([]), tools={}, messages=[Message(role="user", content="hi")],
                            hooks=[Legacy()], plugins=(descriptor,))
    await anext(events)
    await events.aclose()
    assert len(disposed) == 1 and observed == ["legacy", "plugin"]


async def test_multiple_providers_follow_priority_and_keep_their_plugin_identity():
    seen = []

    async def first(ctx):
        seen.append("first")

    async def second(ctx):
        seen.append("second")

    descriptors = (
        plugin([HookSpec("same-local-id", ("run.started",), "observer", first, priority=10)], name="p.first"),
        plugin([HookSpec("same-local-id", ("run.started",), "observer", second, priority=-10)],
               name="p.second", dependencies=("p.first",)),
    )
    _ = [event async for event in run_agent_loop(llm=Model([]), tools={}, messages=[], plugins=descriptors)]
    assert seen == ["second", "first"]


async def test_explicit_replacement_overrides_model_context_even_when_visible_text_is_equal():
    async def replace_same(ctx):
        return ResultTextDecision.replace(ctx.payload["content"])

    _, messages, _ = await run(Echo(), [HookSpec("replace", ("tool.after_execution",), "result_text", replace_same)])
    assert [message.content for message in messages if message.role == "tool"] == ["original"]


async def test_duplicate_calls_finish_without_running_handlers_twice():
    seen, finished = [], []

    async def before(ctx):
        seen.append(ctx.tool_call_id)
        return BeforeToolDecision.allow()

    async def observer(ctx):
        finished.append((ctx.tool_call_id, ctx.payload["executed"]))

    tool = Echo(True)
    await run(tool, [
        HookSpec("before", ("tool.before_execution",), "before_tool", before),
        HookSpec("finished", ("tool.finished",), "observer", observer),
    ], arguments=[{"text": "same"}, {"text": "same"}])
    assert tool.calls == ["same"] and seen == ["call-0"]
    assert finished == [("call-0", True), ("call-1", False)]


async def test_run_cleanup_waits_for_timed_out_handler_before_disposing_plugin():
    release, cancelled, disposed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn(ctx):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        return BeforeToolDecision.allow()

    descriptor = plugin([HookSpec("wait", ("tool.before_execution",), "before_tool", stubborn, timeout_ms=10)],
                        disposer=lambda instance: disposed.set())

    async def consume():
        return [event async for event in run_agent_loop(llm=Model(), tools={"echo": Echo()},
                messages=[Message(role="user", content="hi")], plugins=(descriptor,), max_steps=2)]

    running = asyncio.create_task(consume())
    await asyncio.wait_for(cancelled.wait(), 1)
    await asyncio.sleep(0.02)
    assert not disposed.is_set() and not running.done()
    release.set()
    await asyncio.wait_for(running, 1)
    assert disposed.is_set()


async def test_managed_cancellation_drains_hooks_before_releasing_session(tmp_path, monkeypatch):
    import box_agent.composition as composition

    release, cancelled, cleanup_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []
    original_cleanup = composition._cleanup_hook_run

    async def track_cleanup(*args):
        cleanup_started.set()
        return await original_cleanup(*args)

    monkeypatch.setattr(composition, "_cleanup_hook_run", track_cleanup)

    async def stubborn(ctx):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        order.append("handler")
        return BeforeToolDecision.allow()

    descriptor = plugin(
        [HookSpec("wait", ("tool.before_execution",), "before_tool", stubborn, timeout_ms=10)],
        disposer=lambda instance: order.append("provider"),
    )
    session = await AgentSession.open(
        config=Config(llm={"model": "fixture"}, agent={"max_steps": 2, "enable_memory": False}, tools={}),
        options=SessionOptions(workspace_dir=tmp_path),
        host=HostBindings(llm_client=Model(), tools=[Echo()], system_prompt="system"),
        plugins=(descriptor,),
    )
    session.plugin_session.resources.cleanup.callback(order.append, "session")
    session.agent.add_user_message("hi")

    async def consume():
        return [event async for event in session.run_events()]

    running = asyncio.create_task(consume())
    closing = None
    try:
        await asyncio.wait_for(cancelled.wait(), 3)
        await asyncio.wait_for(cleanup_started.wait(), 3)
        running.cancel()
        closing = asyncio.create_task(session.aclose())
        await asyncio.sleep(0.02)
        assert not running.done() and not closing.done()
        assert order == [] and not session._closed
        release.set()
        results = await asyncio.wait_for(asyncio.gather(running, closing, return_exceptions=True), 3)
        assert results[1] is None
        assert order == ["handler", "provider", "session"]
        assert session._closed
    finally:
        release.set()
        await asyncio.gather(running, *([closing] if closing is not None else []), return_exceptions=True)
        await session.aclose()


async def test_managed_hook_plugins_cannot_replace_other_session_capabilities(tmp_path):
    from box_agent.kernel.ports import SummaryLLMPort

    disposed = []

    class Provider:
        def get_hooks(self):
            return ()

        async def generate(self, **kwargs):
            pytest.fail("a run plugin replaced the managed summary model")

    descriptor = PluginDescriptor(
        "fixture.override", "1.0.0", (HookProviderPort, SummaryLLMPort), Provider,
        disposer=disposed.append,
    )
    model = Model([])
    session = await AgentSession.open(
        config=Config(llm={"model": "fixture"}, agent={}, tools={}),
        options=SessionOptions(workspace_dir=tmp_path, utility=True),
        host=HostBindings(llm_client=model, tools=[], system_prompt="system"),
        plugins=(descriptor,),
    )
    try:
        with pytest.raises(ValueError, match="summary_llm"):
            _ = [event async for event in session.run_events()]
        assert model.requests == 0 and len(disposed) == 1
        assert not session.plugin_session.runtime.host._closed
    finally:
        await session.aclose()


@pytest.mark.parametrize("managed", [False, True])
async def test_run_owner_retries_repeatedly_interrupted_provider_cleanup(tmp_path, managed):
    attempts, order = [], []

    async def dispose(provider):
        attempts.append(provider)
        if len(attempts) <= 2:
            raise asyncio.CancelledError("provider cleanup interrupted")
        order.append("provider")

    descriptor = plugin([], disposer=dispose)
    session = None
    if managed:
        session = await AgentSession.open(
            config=Config(llm={"model": "fixture"}, agent={}, tools={}),
            options=SessionOptions(workspace_dir=tmp_path, utility=True),
            host=HostBindings(llm_client=Model([]), tools=[], system_prompt="system"),
            plugins=(descriptor,),
        )
        session.plugin_session.resources.cleanup.callback(order.append, "session")
        events = session.run_events()
    else:
        events = run_agent_loop(llm=Model([]), tools={}, messages=[], plugins=(descriptor,))
    try:
        with pytest.raises(asyncio.CancelledError, match="provider cleanup interrupted"):
            _ = [event async for event in events]
        assert len(attempts) == 3 and all(item is attempts[0] for item in attempts)
        assert order == ["provider"]
    finally:
        await events.aclose()
        if session is not None:
            await session.aclose()
    if managed:
        assert order == ["provider", "session"]


def test_agent_preserves_existing_positional_constructor_parameters(tmp_path):
    agent = Agent(Model([]), "sys", [], 2, str(tmp_path), 1000, None, True, enable_builtin_tools=False)
    assert agent.thinking_enabled is True and agent.default_run_options().plugins == ()


async def test_legacy_hooks_keep_the_callers_context_variables():
    from contextvars import ContextVar

    marker = ContextVar("legacy_hook_marker", default="before")

    class Legacy(BaseHook):
        async def on_agent_start(self, **kwargs):
            marker.set("after")

    class CheckingModel(Model):
        async def generate_stream(self, *args, **kwargs):
            assert marker.get() == "after"
            async for event in super().generate_stream(*args, **kwargs):
                yield event

    _ = [event async for event in run_agent_loop(llm=CheckingModel([]), tools={}, messages=[], hooks=[Legacy()])]
    assert marker.get() == "after"


@pytest.mark.parametrize("malformed", [(), ("text",), ("text", None, "extra"), 123,
                                     (None, None), (123, None), ("text", 123)])
@pytest.mark.parametrize("success", [False, True])
async def test_malformed_legacy_result_preserves_text_and_run_completion(malformed, success):
    class Legacy(BaseHook):
        async def on_tool_result(self, **kwargs):
            return malformed

    tool = Echo(success=success)
    events, messages, model = await run(tool, [], hooks=[Legacy()])
    assert tool.calls == ["original"] and model.requests == 2
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert result.success is success
    assert result.content == "original"
    assert result.error == (None if success else "原始失败")
    assert [message.content for message in messages if message.role == "tool"] == [
        "原始模型专用文本" if success else "Error: 原始失败"
    ]


@pytest.mark.parametrize("managed", [False, True])
async def test_late_handler_durability_error_still_releases_run_providers(tmp_path, managed):
    from box_agent.session_log import SessionLogDurabilityError

    disposed = []
    failure = SessionLogDurabilityError("handler cleanup write failed")

    async def handler(ctx):
        try:
            await asyncio.Event().wait()
        finally:
            raise failure

    descriptor = plugin(
        [HookSpec("late-durable", ("tool.before_execution",), "before_tool", handler, timeout_ms=50)],
        disposer=lambda instance: disposed.append(instance),
    )
    config = Config(llm={"model": "fixture"}, agent={"max_steps": 2}, tools={})
    if managed:
        session = await AgentSession.open(
            config=config, options=SessionOptions(workspace_dir=tmp_path),
            host=HostBindings(llm_client=Model(), tools=[Echo()], system_prompt="system"),
            plugins=(descriptor,),
        )
    else:
        session = AgentSession.create(config=config, llm_client=Model(), tools=[Echo()],
                                      system_prompt="system", workspace_dir=tmp_path,
                                      plugins=(descriptor,))
    try:
        session.agent.add_user_message("execute")
        with pytest.raises(SessionLogDurabilityError) as captured:
            _ = [event async for event in session.run_events()]
        assert captured.value is failure
        assert len(disposed) == 1
    finally:
        await session.aclose()


@pytest.mark.parametrize("managed", [False, True])
async def test_skill_reread_checks_text_after_hook_suppression(tmp_path, managed):
    from box_agent.tools.skill_loader import SkillLoader
    from box_agent.tools.skill_tool import GetSkillTool

    path = tmp_path / "skills" / "method" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: method\ndescription: Test method\n---\nPRIVATE_METHOD_BODY\n")
    loader = SkillLoader(path.parent.parent)
    loader.discover_skills()

    class ReaderModel:
        model = "fixture"
        calls = 0

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id=f"read-{self.calls}", type="function", function=FunctionCall(
                        name="get_skill", arguments={"skill_name": "method"},
                    )),
                ])
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")

    results = []

    async def suppress_first(ctx):
        results.append(dict(ctx.payload))
        return ResultTextDecision.replace("REDACTED") if len(results) == 1 else ResultTextDecision.keep()

    kwargs = dict(
        config=Config(llm={"model": "fixture"}, agent={}, tools={}),
        llm_client=ReaderModel(), system_prompt="system",
        tools=[GetSkillTool(loader)], workspace_dir=tmp_path,
        plugins=(plugin([HookSpec("redact", ("tool.after_execution",), "result_text", suppress_first)]),),
    )
    if managed:
        config = kwargs.pop("config")
        plugins = kwargs.pop("plugins")
        workspace = kwargs.pop("workspace_dir")
        session = await AgentSession.open(
            config=config, options=SessionOptions(workspace_dir=workspace),
            host=HostBindings(**kwargs), plugins=plugins,
        )
    else:
        session = AgentSession.create(**kwargs)
    try:
        session.agent.add_user_message("read the method twice")
        events = [event async for event in session.run_events()]
        tool_messages = [message for message in session.agent.messages if message.role == "tool"]
        assert tool_messages[0].content == "REDACTED"
        assert "PRIVATE_METHOD_BODY" in tool_messages[1].content
        outputs = [event for event in events if isinstance(event, ToolCallResult)]
        assert len(outputs) == 2 and all(event.success for event in outputs)
        assert len(session.agent.skill_runtime.read_facts) == 1
    finally:
        await session.aclose()
