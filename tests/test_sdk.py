from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from box_agent.api import ControlCommand, RunRequest
from box_agent.events import ContentEvent, DoneEvent, StopReason
from box_agent.run_control import PermissionBroker
from box_agent.sdk import AgentClient


class _Agent:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def add_user_message(self, content: str) -> None:
        self.messages.append(content)


@dataclass
class _Options:
    run_control: object | None = None
    permission_negotiator: object | None = None


class _Session:
    def __init__(self) -> None:
        self.agent = _Agent()
        self.inject_queue: asyncio.Queue[object] = asyncio.Queue()
        self.cancelled = False

    def build_run_options(self, **overrides: object) -> _Options:
        return _Options()

    async def run_events(self, *, options=None):
        if options.run_control is not None:
            await options.run_control.checkpoint()
        yield ContentEvent(content="answer")
        yield DoneEvent(
            stop_reason=StopReason.END_TURN,
            final_content="answer",
        )


@pytest.mark.asyncio
async def test_sdk_client_starts_and_completes_a_run() -> None:
    session = _Session()
    client = AgentClient(session)

    result = await client.run(
        RunRequest("sdk-run", "sdk-session", "Explain this"),
    )

    assert session.agent.messages == ["Explain this"]
    assert result.run_id == "sdk-run"
    assert result.status == "completed"
    assert result.final_content == "answer"


@pytest.mark.asyncio
async def test_sdk_client_exposes_handle_for_streaming_and_control() -> None:
    session = _Session()
    handle = await AgentClient(session).start(
        RunRequest("sdk-stream", "sdk-session", "Stream this"),
    )
    await handle.send(ControlCommand.cancel())

    events = [event async for event in handle.events()]

    assert [type(event.payload) for event in events] == [ContentEvent, DoneEvent]
    assert session.cancelled is True
    assert (await handle.result()).run_id == "sdk-stream"


@pytest.mark.asyncio
async def test_sdk_handle_pauses_and_resumes_before_the_next_kernel_action() -> None:
    session = _Session()
    handle = await AgentClient(session).start(
        RunRequest("sdk-pause", "sdk-session", "Pause this"),
    )

    await handle.send(ControlCommand("pause"))
    stream = handle.events()
    next_event = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)

    assert handle.control_state == "paused"
    assert not next_event.done()

    await handle.send(ControlCommand("resume"))
    assert (await next_event).payload.content == "answer"
    await stream.aclose()


@pytest.mark.asyncio
async def test_sdk_handle_routes_permission_response_to_pending_request() -> None:
    session = _Session()

    async def run_events(*, options=None):
        assert isinstance(options.permission_negotiator, PermissionBroker)
        approved = await options.permission_negotiator.negotiate({
            "scope": "filesystem",
            "requested_scope": "workspace",
        })
        yield DoneEvent(
            stop_reason=StopReason.END_TURN,
            final_content="approved" if approved else "denied",
        )

    session.run_events = run_events
    broker = PermissionBroker(run_id="sdk-permission", on_request=lambda _request: None)
    options = _Options(permission_negotiator=broker)
    handle = await AgentClient(session).start(
        RunRequest("sdk-permission", "sdk-session", "Check permission"),
        options=options,
    )

    stream = handle.events()
    permission = await stream.__anext__()
    request_id = permission.payload.request_id
    assert request_id
    await handle.send(ControlCommand(
        "permission_response",
        request_id=request_id,
        payload={"approved": True},
    ))
    done = await stream.__anext__()
    assert done.payload.final_content == "approved"
    await stream.aclose()
