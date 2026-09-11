"""验证 HookBus 对调用方可见的顺序、决策、隔离与生命周期。"""

import asyncio
from dataclasses import replace

import pytest

from box_agent.hooks import HookBus, HookConfigError, HookStateError
from box_agent.kernel.hook_types import BeforeToolDecision, ResultText, ResultTextDecision
from box_agent.plugins.hooks import HookOwner, HookSpec
from box_agent.session_log import SessionLogDurabilityError


def register(bus, hook_id, handler, *, kind="before_tool", event=None, **kwargs):
    event = event or {"before_tool": "tool.before_execution", "result_text": "tool.after_execution",
                      "observer": "run.started"}[kind]
    return bus.register(HookSpec(hook_id, (event,), kind, handler, **kwargs),
                        HookOwner("fixture.plugin", "1.0.0", "plugin:fixture.plugin", bus.context.run_id))


def context(bus, event="tool.before_execution", call_id="call", **payload):
    return replace(bus.context, event=event, tool_call_id=call_id,
                   payload={"tool_name": "echo", "success": True, "executed": True, **payload})


async def allow(context):
    return BeforeToolDecision.allow()


async def test_hooks_match_updated_arguments_and_return_the_accumulated_value():
    bus = HookBus()
    seen = []

    async def normalize(ctx):
        seen.append("normalize")
        return BeforeToolDecision.modify({"path": "/safe/file", "nested": [1]})

    async def matcher(ctx):
        seen.append(ctx.payload["arguments"]["path"])
        with pytest.raises(TypeError):
            ctx.payload["arguments"]["path"] = "changed"
        with pytest.raises(AttributeError):
            ctx.payload["arguments"]["nested"].append(2)
        return ctx.payload["arguments"]["path"].startswith("/safe")

    async def keep(ctx):
        seen.append("keep")
        return BeforeToolDecision.allow()

    register(bus, "keep", keep, priority=10, matcher=matcher)
    register(bus, "normalize", normalize, priority=-10)
    bus.freeze()
    original = {"path": "file"}
    result = await bus.before_tool(context(bus, arguments={"path": "stale"}), original)
    assert result.action == "allow"
    assert result.arguments == {"path": "/safe/file", "nested": (1,)}
    assert original == {"path": "file"}
    assert seen == ["normalize", "/safe/file", "keep"]
    await bus.close()


async def test_deny_short_circuits_later_handlers_and_preserves_source():
    bus = HookBus()
    seen = []

    async def deny(ctx):
        return BeforeToolDecision.deny("路径不允许", "PATH_DENIED")

    async def later(ctx):
        seen.append(True)
        return BeforeToolDecision.allow()

    register(bus, "deny", deny)
    register(bus, "later", later)
    bus.freeze()
    result = await bus.before_tool(context(bus), {})
    assert result.action == "deny" and result.arguments is None
    assert (result.code, result.source, result.hook_id) == ("PATH_DENIED", "plugin:fixture.plugin", "deny")
    assert not seen
    await bus.close()


@pytest.mark.parametrize("kind", ["observer", "before_tool", "result_text"])
@pytest.mark.parametrize("failure", ["exception", "invalid", "timeout", "matcher"])
async def test_handler_failures_follow_the_declared_kind(kind, failure):
    bus = HookBus(observer_timeout_ms=20, interceptor_timeout_ms=20)
    seen = []

    async def broken(ctx):
        if failure == "exception":
            raise RuntimeError("失败")
        if failure == "timeout":
            await asyncio.Event().wait()
        return "无效返回值"

    async def bad_matcher(ctx):
        raise RuntimeError("匹配失败")

    async def later(ctx):
        seen.append(True)

    register(bus, "broken", broken, kind=kind, matcher=bad_matcher if failure == "matcher" else None)
    if kind == "observer":
        register(bus, "later", later, kind=kind)
    bus.freeze()
    if kind == "observer":
        await bus.observe(context(bus, "run.started"))
        assert seen == [True]
    elif kind == "before_tool":
        result = await bus.before_tool(context(bus), {})
        assert result.action == "deny" and result.code == "HOOK_EXECUTION_FAILED"
    else:
        result = await bus.after_tool(context(bus, "tool.after_execution"), ResultText("原始敏感文本"))
        assert result.suppressed and "原始敏感文本" not in result.text.content
    await bus.close()


async def test_text_replacements_accumulate_and_suppression_ends_the_chain():
    bus = HookBus()
    seen = []

    async def edit(ctx):
        return ResultTextDecision.replace("已处理", "仍然失败")

    async def keep(ctx):
        seen.append((ctx.payload["content"], ctx.payload["error"]))
        return ResultTextDecision.keep()

    register(bus, "edit", edit, kind="result_text")
    register(bus, "keep", keep, kind="result_text")
    bus.freeze()
    result = await bus.after_tool(context(bus, "tool.after_execution"), ResultText("旧文本"))
    assert result.text == ResultText("已处理", "仍然失败")
    assert seen == [("已处理", "仍然失败")]
    await bus.close()


async def test_registration_is_frozen_and_tokens_are_idempotent():
    bus = HookBus(run_id="run-a")
    token = register(bus, "first", allow)
    with pytest.raises(HookConfigError):
        register(bus, "first", allow)
    with pytest.raises(HookConfigError):
        bus.register(HookSpec("wrong", ("run.started",), "before_tool", allow),
                     HookOwner("p", "1.0.0", "plugin:p", "run-a"))
    bus.freeze()
    bus.freeze()
    with pytest.raises(HookStateError):
        register(bus, "second", allow)
    with pytest.raises(HookStateError):
        await token.dispose()
    with pytest.raises(HookStateError):
        await bus.before_tool(replace(context(bus), run_id="run-b"), {})
    await bus.close()
    await token.dispose()
    await bus.close()
    assert bus.state == "Closed" and bus.hooks == []
    with pytest.raises(HookStateError):
        await bus.before_tool(context(bus), {})


async def test_concurrent_calls_keep_their_own_arguments():
    bus = HookBus()
    barrier = asyncio.Event()
    entered = []

    async def handler(ctx):
        entered.append(ctx.tool_call_id)
        if len(entered) == 2:
            barrier.set()
        await barrier.wait()
        return BeforeToolDecision.modify({"value": ctx.payload["arguments"]["value"] + "!"})

    register(bus, "parallel", handler)
    bus.freeze()
    one, two = await asyncio.gather(
        bus.before_tool(context(bus, call_id="one"), {"value": "one"}),
        bus.before_tool(context(bus, call_id="two"), {"value": "two"}),
    )
    assert one.arguments == {"value": "one!"} and two.arguments == {"value": "two!"}
    await bus.close()


async def test_run_cancellation_interrupts_a_pending_handler_and_clears_tasks():
    cancelled = False
    entered, exited = asyncio.Event(), asyncio.Event()
    bus = HookBus(is_cancelled=lambda: cancelled)

    async def waiting(ctx):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    register(bus, "wait", waiting)
    bus.freeze()
    dispatch = asyncio.create_task(bus.before_tool(context(bus), {}))
    await entered.wait()
    cancelled = True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(dispatch, 1)
    await bus.close()
    assert exited.is_set() and not bus._tasks and not bus._dispatches


async def test_timeout_does_not_release_a_handler_that_ignores_cancellation():
    bus = HookBus(interceptor_timeout_ms=10)
    release = asyncio.Event()

    async def stubborn(ctx):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return BeforeToolDecision.allow()

    token = register(bus, "stubborn", stubborn)
    bus.freeze()
    result = await bus.before_tool(context(bus), {})
    assert result.action == "deny" and bus._tasks
    closing = asyncio.create_task(bus.close())
    await asyncio.sleep(0)
    assert bus.state == "Draining" and not closing.done() and not token._disposed
    release.set()
    await asyncio.wait_for(closing, 1)
    assert bus.state == "Closed" and not bus._tasks and token._disposed


async def test_matcher_and_handler_share_one_deadline_and_run_deadline_caps_it():
    import time

    bus = HookBus(interceptor_timeout_ms=200, run_deadline=time.monotonic() + 0.08)
    deadlines = []

    async def matcher(ctx):
        deadlines.append(ctx.deadline)
        await asyncio.sleep(0.01)
        return True

    async def handler(ctx):
        deadlines.append(ctx.deadline)
        await asyncio.sleep(0.2)
        return BeforeToolDecision.allow()

    register(bus, "bounded", handler, matcher=matcher)
    bus.freeze()
    result = await bus.before_tool(context(bus), {})
    assert result.action == "deny" and deadlines == [bus.context.deadline] * 2
    await bus.close()


async def test_session_durability_failure_propagates_out_of_the_bus():
    bus = HookBus()

    async def broken(ctx):
        raise SessionLogDurabilityError("持久化失败")

    register(bus, "durability", broken)
    bus.freeze()
    with pytest.raises(SessionLogDurabilityError):
        await bus.before_tool(context(bus), {})
    await bus.close()


async def test_chain_budget_is_not_reset_for_each_handler():
    bus = HookBus(interceptor_timeout_ms=200, chain_timeout_ms=30)
    seen = []

    async def first(ctx):
        seen.append("first")
        await asyncio.sleep(0.02)
        return BeforeToolDecision.allow()

    async def second(ctx):
        seen.append("second")
        await asyncio.sleep(0.05)
        return BeforeToolDecision.allow()

    register(bus, "first", first)
    register(bus, "second", second)
    bus.freeze()
    result = await bus.before_tool(context(bus), {})
    assert result.action == "deny" and result.hook_id == "second"
    assert seen == ["first", "second"]
    await bus.close()


async def test_disposed_token_cannot_remove_a_later_registration_with_the_same_hook_id():
    bus = HookBus()
    old = register(bus, "same", allow)
    await old.dispose()
    new = register(bus, "same", allow)
    await old.dispose()
    assert old.registration_id != new.registration_id and len(bus.hooks) == 1
    bus.freeze()
    assert (await bus.before_tool(context(bus), {})).action == "allow"
    await bus.close()


def test_decision_constructors_reject_contradictory_or_mutable_runtime_data():
    with pytest.raises(ValueError):
        BeforeToolDecision("deny", arguments={}, reason="deny", code="DENY")
    with pytest.raises(ValueError):
        BeforeToolDecision.modify({"service": object()})
    with pytest.raises(ValueError):
        ResultTextDecision("keep", text=ResultText("矛盾文本"))
    arguments = {"nested": [1]}
    decision = BeforeToolDecision.modify(arguments)
    arguments["nested"].append(2)
    assert decision.arguments["nested"] == (1,)


@pytest.mark.parametrize("finished_before_close", [False, True])
async def test_late_durability_failure_is_preserved_after_timeout_and_drain(finished_before_close):
    bus = HookBus(interceptor_timeout_ms=50)
    release, cleanup_started, cleanup_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = SessionLogDurabilityError("late durable write failed")
    if finished_before_close:
        release.set()

    async def handler(ctx):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            cleanup_finished.set()
            raise failure

    token = register(bus, "late-write", handler)
    bus.freeze()
    result = await bus.before_tool(context(bus), {})
    assert result.action == "deny"
    await asyncio.wait_for(cleanup_started.wait(), 1)
    if finished_before_close:
        await asyncio.wait_for(cleanup_finished.wait(), 1)
        await asyncio.sleep(0)
        assert not bus._tasks
    closing = asyncio.create_task(bus.close())
    if not finished_before_close:
        await asyncio.sleep(0)
        assert not closing.done() and not token._disposed
        release.set()
    with pytest.raises(SessionLogDurabilityError) as captured:
        await asyncio.wait_for(closing, 1)
    assert captured.value is failure
    assert cleanup_finished.is_set() and token._disposed
    assert bus.state == "Closed" and not bus._tasks and not bus._dispatches
    await bus.close()


async def test_observed_late_durability_failure_prevents_the_next_dispatch():
    bus = HookBus(interceptor_timeout_ms=50)
    finished = asyncio.Event()
    failure = SessionLogDurabilityError("late failure before next step")

    async def handler(ctx):
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
            raise failure

    register(bus, "late", handler)
    bus.freeze()
    assert (await bus.before_tool(context(bus), {})).action == "deny"
    await asyncio.wait_for(finished.wait(), 1)
    await asyncio.sleep(0)
    with pytest.raises(SessionLogDurabilityError) as captured:
        await bus.fire_step_start(step=2, max_steps=3)
    assert captured.value is failure
    await bus.close()
