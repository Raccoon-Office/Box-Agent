"""Large history restoration must preserve state without blocking ACP control."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp import text_block, update_agent_message

import box_agent.acp as acp_module
from box_agent.acp import BoxACPAgent, RequestError
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog, SessionLogInUseError, TOOL_OUTCOME_UNKNOWN
from box_agent.tools.skill_tool import create_skill_tools


class Connection:
    def __init__(self):
        self.updates = []

    async def sessionUpdate(self, payload):
        self.updates.append(payload)


class DoneLLM:
    async def generate_stream(self, *args, **kwargs):
        yield StreamEvent(type="text", delta="answer")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_module, "state_path", lambda name: tmp_path / "profile" / name)
    tools, loader = create_skill_tools(sources=[])
    return BoxACPAgent(Connection(), Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(tmp_path), max_steps=1),
        tools=ToolsConfig(enable_sub_agent=False, enable_mcp=False),
    ), DoneLLM(), tools, "system", skill_loader=loader)


def request(tmp_path, session_id):
    return SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": session_id})


def stored_session(tmp_path, *, pending_tool=False):
    log = SessionLog.create(tmp_path / "profile" / "sessions", session_id="restore", cwd=tmp_path)
    log.append_unlogged_messages([Message(role="user", content="retained question")], turn=1, step=1)
    if pending_tool:
        log.append("turn/start", {"turn": 1})
        log.append("step/start", {"turn": 1, "step": 1})
        log.append_unlogged_messages([
            Message(role="user", content="retained question"),
            Message(role="assistant", content="", tool_calls=[ToolCall(
                id="call", type="function", function=FunctionCall(name="read_file", arguments={}),
            )]),
        ], turn=1, step=1)
        log.append("tool/call", {"turn": 1, "step": 1, "callId": "call"})
    path = log.path
    log.close()
    return path


@pytest.mark.parametrize("damage", [None, "json", "message"])
def test_restore_and_recovery_backup_use_bounded_reads(tmp_path, monkeypatch, damage):
    path = stored_session(tmp_path)
    if damage == "json":
        with path.open("ab") as handle:
            handle.write(b'{broken}\n{"partial":')
    elif damage == "message":
        log = SessionLog.open(tmp_path / "profile" / "sessions", session_id="restore", cwd=tmp_path)
        log.append("user/message", {"role": "invalid"}, surface_op="append")
        log.close()
    original_bytes = path.read_bytes()
    original_open = Path.open

    class BoundedReader:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            assert 0 <= size <= 1024 * 1024, "restore read the whole log into memory"
            return self.handle.read(size)

        def __iter__(self):
            return iter(self.handle)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

    def guarded_open(candidate, mode="r", *args, **kwargs):
        handle = original_open(candidate, mode, *args, **kwargs)
        return BoundedReader(handle) if candidate == path and mode in {"rb", "r+b"} else handle

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded_open)
        log, projection = SessionLog.open_with_projection(
            tmp_path / "profile" / "sessions", session_id="restore", cwd=tmp_path, recover=True,
        )
    try:
        if damage:
            assert log.recovery_source.read_bytes() == original_bytes
            assert not projection.has_messages
        else:
            assert projection.messages[0].content == "retained question"
            assert path.read_bytes() == original_bytes
    finally:
        log.close()


def test_revision_queries_preserve_snapshot_isolation_across_reopen(tmp_path, monkeypatch):
    root = tmp_path / "logs"
    log = SessionLog.create(root, session_id="revision", cwd=tmp_path)
    log.append("turn/binding", {"turn_id": "old"})
    snapshot = log.events
    snapshot[0]["data"]["turn_id"] = "mutated"
    assert log.has_turn_binding("old")
    assert not log.has_turn_binding("mutated")
    log.append("history/revise", {
        "revision_id": "edited", "previous_revision_id": "initial",
        "turn_id": "edited", "restore_through_seq": -1,
    })
    log.close()
    log = SessionLog.open(root, session_id="revision", cwd=tmp_path, recover=True)
    try:
        def forbid_snapshot(_):
            raise AssertionError("metadata lookup copied the history")
        monkeypatch.setattr(SessionLog, "events", property(forbid_snapshot))
        assert log.current_revision() == "edited"
        assert log.has_turn_binding("old")
    finally:
        log.close()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_streamed_restore_truncates_only_uncommitted_tail_bytes(tmp_path, newline):
    path = stored_session(tmp_path)
    committed = path.read_bytes().replace(b"\n", newline)
    path.write_bytes(committed + b'{"type":"user/message","data":{"content":"\xe4')
    log, projection = SessionLog.open_with_projection(
        tmp_path / "profile" / "sessions", session_id="restore", cwd=tmp_path, recover=True,
    )
    try:
        assert projection.messages[0].content == "retained question"
        assert path.read_bytes() == committed
        assert log.recovery_source is None
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_tool", [False, True])
async def test_acp_reuses_validated_history_and_refreshes_crash_repair(
    tmp_path, monkeypatch, host, pending_tool,
):
    stored_session(tmp_path, pending_tool=pending_tool)
    original = SessionLog.replay
    replay_threads = []

    def replay(log, **kwargs):
        replay_threads.append(threading.current_thread())
        return original(log, **kwargs)

    def forbid_snapshot(_):
        raise AssertionError("session response copied the history")

    monkeypatch.setattr(SessionLog, "replay", replay)
    monkeypatch.setattr(SessionLog, "events", property(forbid_snapshot))
    try:
        response = await host.newSession(request(tmp_path, "restore"))
        state = host._sessions[response.sessionId]
        assert response.field_meta["history_revision_id"] == "initial"
        assert state.continuation_applied
        assert state.agent.messages[1].content == "retained question"
        assert len(replay_threads) == (2 if pending_tool else 1)
        assert all(thread is not threading.main_thread() for thread in replay_threads)
        if pending_tool:
            assert state.agent.messages[-1].role == "tool"
            assert "outcome is unknown" in state.agent.messages[-1].content
            assert any(event["data"].get("error", {}).get("code") == TOOL_OUTCOME_UNKNOWN
                       for event in state.agent.session_log._events)
        else:
            async def finish_turn(*args, **kwargs):
                return "end_turn"

            # Exercise the RPC's binding and response metadata independently
            # of the tool engine's separate history consumers.
            monkeypatch.setattr(host, "_run_turn", finish_turn)
            result = await host.prompt(SimpleNamespace(
                sessionId=response.sessionId, prompt=[{"text": "follow-up"}],
                field_meta={"turn_id": "next"},
            ))
            assert result.field_meta["history_revision_id"] == "initial"
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["open", "repair"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_slow_restore_keeps_control_responsive_and_owns_lock_until_worker_finishes(
    tmp_path, monkeypatch, host, phase, cancel,
):
    stored_session(tmp_path)
    control = await host.newSession(request(tmp_path, "control"))
    entered, release = threading.Event(), threading.Event()
    original = (SessionLog.open_with_projection if phase == "open"
                else SessionLog.prepare_resume_projection)

    def slow(*args, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5), "test did not release restore worker"
        return result

    if phase == "open":
        monkeypatch.setattr(SessionLog, "open_with_projection", staticmethod(slow))
    else:
        monkeypatch.setattr(SessionLog, "prepare_resume_projection", slow)
    pending = asyncio.create_task(host.newSession(request(tmp_path, "restore")))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(host._send(control.sessionId, update_agent_message(text_block("control"))), 0.5)
        await asyncio.wait_for(host.cancel(SimpleNamespace(sessionId=control.sessionId)), 0.5)
        assert host._conn.updates
        if cancel:
            pending.cancel()
            await asyncio.sleep(0)
            # Shutdown can cancel the owned creation task a second time.
            closing = asyncio.create_task(host.aclose())
            await asyncio.sleep(0)
            assert not pending.done()
            assert not closing.done()
            with pytest.raises(RequestError):
                await host.newSession(request(tmp_path, "restore"))
            with pytest.raises(SessionLogInUseError):
                SessionLog.open(tmp_path / "profile" / "sessions", session_id="restore", cwd=tmp_path)
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(closing, 2)
            assert "restore" not in host._session_bindings_in_progress
            reopened = SessionLog.open(tmp_path / "profile" / "sessions", session_id="restore", cwd=tmp_path)
            reopened.close()
        else:
            assert (await asyncio.wait_for(pending, 2)).sessionId
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await host.aclose()


@pytest.mark.asyncio
async def test_cancelled_failed_restore_releases_writer_for_retry(tmp_path, monkeypatch, host):
    stored_session(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = SessionLog.replay

    def fail_replay(log, **kwargs):
        entered.set()
        assert release.wait(5)
        raise OSError("restore read failed")

    monkeypatch.setattr(SessionLog, "replay", fail_replay)
    pending = asyncio.create_task(host.newSession(request(tmp_path, "restore")))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        monkeypatch.setattr(SessionLog, "replay", original)
        assert (await host.newSession(request(tmp_path, "restore"))).sessionId
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await host.aclose()
