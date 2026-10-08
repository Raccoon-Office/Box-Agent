"""Public desktop adapter paths exercise bounded shared delivery."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import box_agent.acp as acp_module
import box_agent.composition as composition
from box_agent.agent_service import AgentService
from box_agent.api import RunDeliveryOptions
from box_agent.config import Config, LLMConfig, AgentConfig, ToolsConfig
from box_agent.events import ContentEvent, DoneEvent, StopReason
from box_agent.run_events import EventTooLargeError
from tests.test_acp import DoneLLM, DummyConn


async def make_adapter(tmp_path, monkeypatch, kernel, conn, **limits):
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    class SmallService(AgentService):
        async def start(self, *args, **kwargs):
            return await super().start(*args, **kwargs, delivery_options=RunDeliveryOptions(**limits))

    monkeypatch.setattr(acp_module, "AgentService", SmallService)
    monkeypatch.setattr(composition, "AgentLoopKernel", kernel)
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(tmp_path), enable_memory=False,
                          enable_memory_extraction=False, memory_maintainer_enabled=False),
        tools=ToolsConfig(enable_file_tools=False, enable_bash=False, enable_todo=False,
                          enable_plan=False, enable_sub_agent=False, enable_mcp=False,
                          enable_skills=False),
    )
    adapter = acp_module.BoxACPAgent(conn, config, DoneLLM(), [], "system")
    session = await adapter.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_mode": "general"}))
    request = SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "hello"}])
    return adapter, request


class BurstKernel:
    def __init__(self, **kwargs):
        self.args = kwargs

    async def run(self):
        for index in range(40):
            yield ContentEvent(content=f"piece-{index}")
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")


@pytest.mark.asyncio
async def test_slow_acp_send_preserves_all_events_with_small_buffer(tmp_path, monkeypatch):
    class SlowConn(DummyConn):
        async def sessionUpdate(self, payload):
            await asyncio.sleep(0)
            await super().sessionUpdate(payload)

    conn = SlowConn()
    adapter, request = await make_adapter(tmp_path, monkeypatch, BurstKernel, conn, max_events=2)
    try:
        response = await asyncio.wait_for(adapter.prompt(request), 5)
        assert response.stopReason == "end_turn"
        texts = [p.update.content.text for p in conn.updates
                 if getattr(p.update, "sessionUpdate", "") == "agent_message_chunk"]
        assert texts == [f"piece-{i}" for i in range(40)]
        assert (await adapter._sessions[request.sessionId]._run_handle.result()).status == "completed"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_acp_permission_reverse_request_settles_on_response_or_cancel(tmp_path, monkeypatch, cancel):
    from acp.schema import AllowedOutcome, RequestPermissionResponse

    asked, released, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    decisions = []

    class PermissionConn(DummyConn):
        async def requestPermission(self, request):
            assert request.sessionId
            asked.set()
            try:
                await released.wait()
                return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", optionId="approve"))
            finally:
                stopped.set()

    class PermissionKernel(BurstKernel):
        async def run(self):
            decision = await self.args["_services"].permission_gateway.negotiate({
                "scope": "safety", "requested_scope": "test", "reason": "test approval",
            })
            decisions.append(decision)
            yield DoneEvent(stop_reason=StopReason.CANCELLED if cancel else StopReason.END_TURN, final_content="")

    adapter, request = await make_adapter(tmp_path, monkeypatch, PermissionKernel, PermissionConn(), max_events=1)
    task = asyncio.create_task(adapter.prompt(request))
    try:
        await asyncio.wait_for(asked.wait(), 5)
        if cancel:
            await adapter.cancel(SimpleNamespace(sessionId=request.sessionId))
        else:
            released.set()
        response = await asyncio.wait_for(task, 5)
        assert decisions == [not cancel]
        assert stopped.is_set()
        assert response.stopReason == ("cancelled" if cancel else "end_turn")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.aclose()


@pytest.mark.asyncio
async def test_acp_oversized_delivery_raises_instead_of_normal_completion(tmp_path, monkeypatch):
    class LargeKernel(BurstKernel):
        async def run(self):
            yield ContentEvent(content="x" * 2000)
            yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="")

    adapter, request = await make_adapter(tmp_path, monkeypatch, LargeKernel, DummyConn(), max_bytes=512)
    try:
        with pytest.raises(EventTooLargeError):
            await asyncio.wait_for(adapter.prompt(request), 5)
        handle = adapter._sessions[request.sessionId]._run_handle
        assert (await handle.result()).error["code"] == "RUN_EVENT_TOO_LARGE"
        assert not handle.is_active
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_acp_cancel_wakes_producer_while_host_send_is_blocked(tmp_path, monkeypatch):
    sending, release = asyncio.Event(), asyncio.Event()

    class BlockedConn(DummyConn):
        async def sessionUpdate(self, payload):
            if getattr(payload.update, "sessionUpdate", "") == "agent_message_chunk":
                sending.set()
                await release.wait()
            await super().sessionUpdate(payload)

    adapter, request = await make_adapter(tmp_path, monkeypatch, BurstKernel, BlockedConn(), max_events=1)
    task = asyncio.create_task(adapter.prompt(request))
    try:
        await asyncio.wait_for(sending.wait(), 5)
        handle = adapter._sessions[request.sessionId]._run_handle
        await adapter.cancel(SimpleNamespace(sessionId=request.sessionId))
        result = await asyncio.wait_for(handle.result(), 1)
        assert result.status == "cancelled"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.aclose()


@pytest.mark.asyncio
async def test_acp_send_failure_does_not_return_normal_completion(tmp_path, monkeypatch):
    send_error = TimeoutError("simulated host send timeout")
    class FailedConn(DummyConn):
        async def sessionUpdate(self, payload):
            if getattr(payload.update, "sessionUpdate", "") == "agent_message_chunk":
                raise send_error
            await super().sessionUpdate(payload)

    adapter, request = await make_adapter(tmp_path, monkeypatch, BurstKernel, FailedConn(), max_events=1)
    try:
        with pytest.raises(TimeoutError) as caught:
            await asyncio.wait_for(adapter.prompt(request), 5)
        # Python 3.11 aliases asyncio.TimeoutError to the built-in class, so
        # _send wraps this error there; 3.10 propagates the original instance.
        if asyncio.TimeoutError is TimeoutError:
            assert caught.value.__cause__ is send_error
            assert str(caught.value).startswith("ACP session update timed out after ")
        else:
            assert caught.value is send_error
        assert not adapter._sessions[request.sessionId]._run_handle.is_active
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_acp_blocked_send_times_out_and_stops_run(tmp_path, monkeypatch):
    send_cancelled = asyncio.Event()

    class BlockedConn(DummyConn):
        async def sessionUpdate(self, payload):
            if getattr(payload.update, "sessionUpdate", "") == "agent_message_chunk":
                try:
                    await asyncio.Event().wait()
                finally:
                    send_cancelled.set()
            await super().sessionUpdate(payload)

    adapter, request = await make_adapter(tmp_path, monkeypatch, BurstKernel, BlockedConn(), max_events=1)
    monkeypatch.setattr(adapter, "_SESSION_UPDATE_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(TimeoutError, match="ACP session update timed out after 0.05s"):
            await asyncio.wait_for(adapter.prompt(request), 5)
        assert send_cancelled.is_set()
        assert not adapter._sessions[request.sessionId]._run_handle.is_active
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("during_close", [False, True])
async def test_acp_reports_post_done_cleanup_failure_and_can_run_again(
    tmp_path, monkeypatch, during_close,
):
    cleanup_started = asyncio.Event()
    fail_next = True

    class CleanupKernel(BurstKernel):
        async def run(self):
            nonlocal fail_next
            if not fail_next:
                yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="recovered")
                return
            fail_next = False
            try:
                yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="answer")
            finally:
                cleanup_started.set()
                try:
                    if during_close:
                        await asyncio.Event().wait()
                finally:
                    raise RuntimeError("post-Done cleanup failed")

    adapter, request = await make_adapter(tmp_path, monkeypatch, CleanupKernel, DummyConn())
    try:
        response = await asyncio.wait_for(adapter.prompt(request), 5)
        handle = adapter._sessions[request.sessionId]._run_handle
        assert cleanup_started.is_set()
        assert not handle.is_active
        assert (await handle.result()).status == "failed"
        assert response.stopReason == "end_turn"  # ACP has no generic error stop reason.
        assert response.field_meta["ok"] is False
        assert response.field_meta["completed"] is False
        assert response.field_meta["runStatus"] == "error"
        assert response.field_meta["lastStopReason"] == "error"
        assert response.field_meta["error"] == "post-Done cleanup failed"

        recovered = await asyncio.wait_for(adapter.prompt(request), 5)
        assert recovered.stopReason == "end_turn"
        assert recovered.field_meta["ok"] is True
        assert recovered.field_meta["completed"] is True
        assert recovered.field_meta["error"] is None
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,acp_reason", [
    (StopReason.END_TURN, "end_turn"),
    (StopReason.WAITING_FOR_USER, "end_turn"),
    (StopReason.MAX_STEPS, "max_turn_requests"),
])
async def test_acp_closes_pending_cleanup_and_preserves_terminal_reason(
    tmp_path, monkeypatch, reason, acp_reason,
):
    cleanup_cancelled = asyncio.Event()

    class ClosingKernel(BurstKernel):
        async def run(self):
            try:
                yield DoneEvent(stop_reason=reason, final_content="answer")
            finally:
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_cancelled.set()

    adapter, request = await make_adapter(tmp_path, monkeypatch, ClosingKernel, DummyConn())
    try:
        response = await asyncio.wait_for(adapter.prompt(request), 5)
        assert cleanup_cancelled.is_set()
        assert not adapter._sessions[request.sessionId]._run_handle.is_active
        assert response.stopReason == acp_reason
        assert response.field_meta["ok"] is True
        assert response.field_meta["lastStopReason"] == reason.value
        assert response.field_meta["completed"] is (reason != StopReason.WAITING_FOR_USER)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_acp_reports_missing_terminal_event_as_failure(tmp_path, monkeypatch):
    class MissingDoneKernel(BurstKernel):
        async def run(self):
            yield ContentEvent(content="partial answer")

    adapter, request = await make_adapter(tmp_path, monkeypatch, MissingDoneKernel, DummyConn())
    try:
        response = await asyncio.wait_for(adapter.prompt(request), 5)
        assert response.field_meta["ok"] is False
        assert response.field_meta["completed"] is False
        assert response.field_meta["runStatus"] == "error"
        assert response.field_meta["error"] == "run ended without a DoneEvent"
    finally:
        await adapter.aclose()
