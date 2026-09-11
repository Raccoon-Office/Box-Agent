from __future__ import annotations

import asyncio

import pytest

from box_agent.api import ControlCommand
from box_agent.core import run_agent_loop
from box_agent.events import DoneEvent, StopReason
from box_agent.run_control import PermissionBroker, RunControl
from box_agent.schema import Message, StreamEvent


@pytest.mark.asyncio
async def test_pause_waits_at_checkpoint_and_resume_releases_same_run() -> None:
    control = RunControl()
    control.request_pause()

    checkpoint = asyncio.create_task(control.checkpoint())
    await asyncio.sleep(0)

    assert control.state == "paused"
    assert not checkpoint.done()

    control.request_resume()

    assert await checkpoint is True
    assert control.state == "running"


@pytest.mark.asyncio
async def test_cancel_releases_a_paused_checkpoint() -> None:
    control = RunControl()
    control.request_pause()
    checkpoint = asyncio.create_task(control.checkpoint())
    await asyncio.sleep(0)

    control.request_cancel()

    assert await checkpoint is False
    assert control.cancelled is True


@pytest.mark.asyncio
async def test_kernel_does_not_start_until_a_paused_run_is_resumed() -> None:
    class Model:
        calls = 0

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    control = RunControl()
    control.request_pause()
    model = Model()

    async def collect():
        return [event async for event in run_agent_loop(
            llm=model,
            messages=[Message(role="user", content="go")],
            tools={},
            max_steps=1,
            run_control=control,
        )]

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    assert control.state == "paused"
    assert model.calls == 0

    control.request_resume()
    events = await asyncio.wait_for(task, timeout=1)

    assert model.calls == 1
    assert any(
        isinstance(event, DoneEvent) and event.stop_reason == StopReason.END_TURN
        for event in events
    )


@pytest.mark.asyncio
async def test_permission_broker_matches_response_by_request_id() -> None:
    requests: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    broker = PermissionBroker(
        run_id="run-1",
        on_request=requests.put,
    )
    pending = asyncio.create_task(
        broker.negotiate({"scope": "filesystem", "requested_scope": "workspace"})
    )
    request = await asyncio.wait_for(requests.get(), timeout=0.1)

    assert isinstance(request["request_id"], str)
    assert await broker.respond(
        ControlCommand(
            "permission_response",
            request_id=str(request["request_id"]),
            payload={"option_id": "approve"},
        )
    ) is True
    assert await pending is True


@pytest.mark.asyncio
async def test_permission_broker_cancel_releases_waiters() -> None:
    broker = PermissionBroker(run_id="run-2", on_request=lambda _request: None)
    pending = asyncio.create_task(broker.negotiate({"scope": "safety"}))
    await asyncio.sleep(0)

    broker.cancel()

    assert await pending is False
