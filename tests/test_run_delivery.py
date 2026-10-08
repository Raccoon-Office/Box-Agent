import asyncio
from types import SimpleNamespace

import pytest

from box_agent.agent_run import AgentRunHandle
from box_agent.events import ContentEvent, DoneEvent, StopReason
from box_agent.run_events import RunEventChannel


@pytest.mark.asyncio
async def test_channel_preserves_mixed_publisher_order():
    channel = RunEventChannel()
    await channel.publish("r", ContentEvent("one"))
    await channel.publish("r", ContentEvent("two"))
    channel.close()
    events = [await channel.get(), await channel.get()]
    assert [event.sequence for event in events] == [1, 2]
    assert [event.payload.content for event in events] == ["one", "two"]
    assert await channel.get() is channel.END


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_run_without_terminal_reports_failure(raises):
    async def events():
        yield ContentEvent("partial")
        if raises:
            raise ValueError("broken")

    handle = AgentRunHandle.for_run(
        state=SimpleNamespace(), run_id="r", events_factory=events,
    )
    if raises:
        with pytest.raises(ValueError, match="broken"):
            _ = [event async for event in handle.events()]
    else:
        _ = [event async for event in handle.events()]
    result = await handle.result()
    assert result.status == "failed"
    assert result.error["type"] == ("ValueError" if raises else "MissingTerminalEvent")


def test_delivery_modules_do_not_import_adapters_or_models():
    import ast
    from pathlib import Path

    for name in ("run_events.py", "run_result.py"):
        tree = ast.parse((Path(__file__).parents[1] / "box_agent" / name).read_text())
        for node in ast.walk(tree):
            imports = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                       else [item.name for item in node.names] if isinstance(node, ast.Import)
                       else [])
            assert not any(set(module.split(".")) & {"acp", "cli", "llm"} for module in imports)


@pytest.mark.parametrize("reason,kind", [
    ("end_turn", "normal"), ("max_steps", "budget_exhausted"),
    ("max_tokens", "budget_exhausted"), ("interrupted", "interrupted"),
    ("cancelled", "cancelled"), ("waiting_for_user", "waiting_for_user"),
    ("error", "failed"), ("vendor_reason", "unknown"),
])
def test_result_classifies_termination_without_changing_legacy_status(reason, kind):
    import json
    from box_agent.api import RunResult, RunStatus

    result = RunResult("r", RunStatus.COMPLETED, reason, "answer")
    assert result.status is RunStatus.COMPLETED
    assert result.stop_reason == reason
    assert result.termination_kind == kind
    assert json.loads(json.dumps(result.to_dict()))["termination_kind"] == kind


@pytest.mark.asyncio
async def test_result_only_drains_large_stream_and_rejects_late_subscription():
    async def events():
        for _ in range(5000):
            yield ContentEvent("piece")
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="finished")

    handle = AgentRunHandle.for_run(state=SimpleNamespace(), run_id="r", events_factory=events)
    first, second = await asyncio.gather(handle.result(), handle.result())
    assert first is second
    assert first.final_content == "finished"
    with pytest.raises(RuntimeError, match="result-only"):
        await anext(handle.events())
    await handle.aclose()


@pytest.mark.asyncio
async def test_cancelling_result_waiter_keeps_run_and_private_consumer_alive():
    gate = asyncio.Event()

    async def events():
        await gate.wait()
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="finished")

    handle = AgentRunHandle.for_run(state=SimpleNamespace(), run_id="r", events_factory=events)
    waiter = asyncio.create_task(handle.result())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.set()
    assert (await handle.result()).final_content == "finished"
    await handle.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("pending", [True, False])
async def test_result_only_routes_existing_and_future_permissions(approve, pending):
    from box_agent.api import ControlCommand
    from box_agent.run_control import PermissionBroker

    async def callback(request):
        if approve:
            await broker.respond(ControlCommand.permission_response(request["request_id"], approved=True))

    broker = PermissionBroker(run_id="r", on_request=callback)
    notified = asyncio.Event()
    broker.set_event_sink(lambda event: notified.set())
    if not pending:
        broker.use_result_only()
    task = asyncio.create_task(broker.negotiate({"scope": "safety", "request_id": "p"}))
    if pending:
        await notified.wait()
        broker.use_result_only()
    assert await asyncio.wait_for(task, 1) is approve
    await broker.aclose()


@pytest.mark.asyncio
async def test_close_wakes_empty_channel_consumer_without_queued_sentinel():
    channel = RunEventChannel()
    waiting = asyncio.create_task(channel.get())
    await asyncio.sleep(0)
    channel.close()
    assert await waiting is channel.END
    assert await channel.get() is channel.END


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_first", [False, True])
async def test_first_registered_consumer_wins_result_stream_race(stream_first):
    gate = asyncio.Event()

    async def events():
        await gate.wait()
        yield ContentEvent("visible")
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    handle = AgentRunHandle.for_run(state=SimpleNamespace(), run_id="race", events_factory=events)
    stream = handle.events()
    if stream_first:
        next_event = asyncio.create_task(anext(stream))
        result_waiter = asyncio.create_task(handle.result())
    else:
        result_waiter = asyncio.create_task(handle.result())
        next_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    gate.set()
    if stream_first:
        assert (await next_event).payload.content == "visible"
        with pytest.raises(RuntimeError, match="already have a consumer"):
            await anext(handle.events())
        assert len([event async for event in stream]) == 1
    else:
        with pytest.raises(RuntimeError, match="result-only"):
            await next_event
    result = await result_waiter
    assert result is await handle.result()
    await handle.aclose()
