from __future__ import annotations

import asyncio
import json

import pytest

from box_agent.agent_service import AgentService
from box_agent.agent import AgentRunOptions
from box_agent.agent_session import AgentSession
from box_agent.api import ControlCommand, RunRequest
from box_agent.events import ContentEvent, DoneEvent, StopReason, TokenUsageEvent


class _FakeAgent:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def add_user_message(self, content: str) -> None:
        self.messages.append(content)


class _FakeSession:
    def __init__(self) -> None:
        self.agent = _FakeAgent()
        self.cancelled = False
        self.inject_queue: asyncio.Queue[object] = asyncio.Queue()
        self.started = asyncio.Event()
        self.options = None

    def build_run_options(self, **overrides: object) -> dict[str, object]:
        self.options = overrides
        return overrides

    async def run_events(self, *, options=None):
        self.started.set()
        self.options = options
        yield ContentEvent(content="working")
        yield TokenUsageEvent(total_tokens=7)
        yield DoneEvent(
            stop_reason=StopReason.END_TURN,
            final_content="finished",
        )

    def request_cancel(self) -> None:
        self.cancelled = True


@pytest.mark.asyncio
async def test_agent_service_start_exposes_events_commands_and_result() -> None:
    session = _FakeSession()
    request = RunRequest(
        run_id="run-1",
        session_id="session-1",
        user_message="Do the work",
    )

    handle = await AgentService().start(request, session=session)
    await handle.send(ControlCommand.cancel())
    await handle.send(
        ControlCommand.inject_message("Keep going", injection_id="injection-1")
    )

    events = [event async for event in handle.events()]
    result = await handle.result()

    assert session.agent.messages == ["Do the work"]
    assert session.options["session_id"] == "session-1"
    assert session.options["turn_id"] == "run-1"
    assert session.options["current_turn_text"] == "Do the work"
    assert session.cancelled is True
    assert await session.inject_queue.get() == {
        "id": "injection-1",
        "content": "Keep going",
    }
    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.payload for event in events] == [
        ContentEvent(content="working"),
        TokenUsageEvent(total_tokens=7),
        DoneEvent(stop_reason=StopReason.END_TURN, final_content="finished"),
    ]
    assert result.run_id == "run-1"
    assert result.status == "completed"
    assert result.final_content == "finished"
    assert result.usage == {"total_tokens": 7}


def test_control_command_rejects_empty_injected_message() -> None:
    with pytest.raises(ValueError, match="message"):
        ControlCommand.inject_message(" ")


def test_control_command_builds_pause_resume_and_permission_response() -> None:
    assert ControlCommand.pause().to_dict() == {"kind": "pause", "request_id": None, "payload": {}}
    assert ControlCommand.resume().to_dict() == {"kind": "resume", "request_id": None, "payload": {}}
    assert ControlCommand.permission_response(
        "permission-1", approved=True,
    ).to_dict() == {
        "kind": "permission_response",
        "request_id": "permission-1",
        "payload": {"approved": True},
    }


@pytest.mark.asyncio
async def test_start_runs_without_an_event_consumer() -> None:
    session = _FakeSession()
    handle = await AgentService().start(
        RunRequest("run-2", "session-2", "Start immediately"),
        session=session,
    )

    await asyncio.wait_for(session.started.wait(), timeout=0.1)
    result = await handle.result()

    assert result.final_content == "finished"


def test_run_request_and_commands_are_json_serializable() -> None:
    request = RunRequest("run-3", "session-3", "Work", metadata={"origin": "sdk"})
    command = ControlCommand.inject_message("More work", injection_id="injection-3")

    assert json.loads(json.dumps(request.to_dict())) == {
        "run_id": "run-3",
        "session_id": "session-3",
        "user_message": "Work",
        "metadata": {"origin": "sdk"},
    }
    assert json.loads(json.dumps(command.to_dict())) == {
        "kind": "inject_message",
        "request_id": "injection-3",
        "payload": {"content": "More work"},
    }


@pytest.mark.asyncio
async def test_run_can_continue_existing_history_without_adding_a_message() -> None:
    session = _FakeSession()
    session.agent.add_user_message("Already staged by the host")

    handle = await AgentService().start(
        RunRequest("continued-run", "session-1", None), session=session,
    )
    await handle.result()

    assert session.agent.messages == ["Already staged by the host"]
    assert session._run_handle is handle


@pytest.mark.asyncio
async def test_closing_handle_settles_active_run_and_closes_its_stream() -> None:
    closed = asyncio.Event()

    class WaitingSession(_FakeSession):
        async def run_events(self, *, options=None):
            try:
                self.started.set()
                yield ContentEvent(content="working")
                await asyncio.Event().wait()
            finally:
                closed.set()

    session = WaitingSession()
    handle = await AgentService().start(
        RunRequest("close-run", "session-1", "Work"), session=session,
    )
    await session.started.wait()

    await handle.aclose()

    assert closed.is_set()
    assert not handle.is_active
    assert (await handle.result()).status == "cancelled"


class _SessionAgent(_FakeAgent):
    def default_run_options(self):
        return AgentRunOptions(llm=None)

    async def run_events(self, *, options):
        running = await options.run_control.checkpoint() if options.run_control else True
        reason = StopReason.CANCELLED if not running or options.is_cancelled() else StopReason.END_TURN
        yield DoneEvent(stop_reason=reason, final_content="finished")


@pytest.mark.asyncio
async def test_busy_start_preserves_history_and_current_handle():
    session = AgentSession(agent=_SessionAgent())
    service = AgentService()
    first = await service.start(RunRequest("first", "session", "accepted"), session=session)
    await first.send(ControlCommand.pause())
    try:
        # A second start must also be rejected before the runner's first step.
        for _ in range(2):
            with pytest.raises(RuntimeError, match="active run"):
                await service.start(RunRequest("second", "session", "rejected"), session=session)
            assert session.agent.messages == ["accepted"]
            assert session.run_handle is first
            await asyncio.sleep(0)
    finally:
        await first.aclose()


@pytest.mark.asyncio
async def test_cancelled_session_can_run_again_and_old_handle_cannot_control_it():
    session = AgentSession(agent=_SessionAgent())
    service = AgentService()
    first = await service.start(RunRequest("first", "session", "first"), session=session)
    await first.cancel()
    assert (await first.result()).status == "cancelled"
    second = await service.start(RunRequest("second", "session", "second"), session=session)
    for command in (ControlCommand.cancel(), ControlCommand.inject_message("stale")):
        with pytest.raises(RuntimeError, match="active run"):
            await first.send(command)
    assert session.inject_queue.empty()
    assert (await second.result()).status == "completed"


@pytest.mark.asyncio
async def test_closed_session_rejects_start_without_appending_message():
    session = AgentSession(agent=_SessionAgent())
    await session.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await AgentService().start(RunRequest("run", "session", "rejected"), session=session)
    assert session.agent.messages == []


@pytest.mark.asyncio
async def test_session_close_settles_a_run_before_its_first_step():
    session = AgentSession(agent=_SessionAgent())
    handle = await AgentService().start(RunRequest("run", "session", "work"), session=session)
    await session.aclose()
    assert not handle.is_active
    assert (await handle.result()).status == "cancelled"


@pytest.mark.asyncio
async def test_legacy_stream_cannot_steal_a_reserved_protocol_run():
    session = AgentSession(agent=_SessionAgent())
    handle = await AgentService().start(RunRequest("run", "session", "work"), session=session)
    try:
        with pytest.raises(RuntimeError, match="active run"):
            await anext(session.run_events())
        assert (await handle.result()).status == "completed"
    finally:
        await handle.aclose()


@pytest.mark.asyncio
async def test_terminal_run_rejects_commands_while_cleanup_is_pending():
    cleaning = asyncio.Event()
    finish_cleanup = asyncio.Event()

    class ClosingAgent(_SessionAgent):
        async def run_events(self, *, options):
            try:
                yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")
            finally:
                cleaning.set()
                await finish_cleanup.wait()

    session = AgentSession(agent=ClosingAgent())
    handle = await AgentService().start(RunRequest("run", "session", "work"), session=session)
    await cleaning.wait()
    try:
        with pytest.raises(RuntimeError, match="active run"):
            await handle.send(ControlCommand.inject_message("too late"))
        assert session.inject_queue.empty()
    finally:
        finish_cleanup.set()
        await handle.result()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["run", "store"])
async def test_foreign_permission_broker_rejects_start_without_changing_session(mismatch):
    from box_agent.run_control import PermissionBroker
    from box_agent.tools.permissions import GrantStore

    session = AgentSession(agent=_SessionAgent(), grant_store=GrantStore())
    original_handle = session.run_handle
    broker = PermissionBroker(
        run_id="different" if mismatch == "run" else "run",
        grant_store=GrantStore() if mismatch == "store" else session.grant_store,
        on_request=lambda _: None,
    )
    with pytest.raises(ValueError, match="different"):
        await AgentService().start(
            RunRequest("run", "session", "rejected"), session=session,
            options=session.build_run_options(permission_negotiator=broker),
        )
    assert session.agent.messages == []
    assert session.run_handle is original_handle


@pytest.mark.asyncio
async def test_start_preserves_cancellation_of_an_enclosing_host_turn():
    session = AgentSession(agent=_SessionAgent(), turn_active=True, cancelled=True)
    handle = await AgentService().start(RunRequest("run", "session"), session=session)
    assert (await handle.result()).status == "cancelled"
    assert session.cancelled
