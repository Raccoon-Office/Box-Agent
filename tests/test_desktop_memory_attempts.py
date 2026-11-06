"""Canonical Session Log protects turns that never reached a ready bootstrap."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from box_agent.desktop_memory.runtime import DesktopMemoryRuntime
from box_agent.memory import _MemoryTurn
from box_agent.session_log import SessionLog
from tests.test_desktop_memory_backend import host, backend  # noqa: F401


def session(log):
    agent = SimpleNamespace(system_prompt="base", session_log=log)
    agent.set_system_prompt = lambda value, **_: setattr(agent, "system_prompt", value)
    return SimpleNamespace(agent=agent, cancelled=False)


def test_attempt_event_scan_returns_detached_matches_only(tmp_path):
    log = SessionLog.create(tmp_path / "sessions", session_id="scan", cwd=tmp_path)
    try:
        log.append("memory/desktop-attempt", {"turn_key": "a" * 64, "timestamp": 1}, ignorable=True)
        log.append("other", {"content": "unrelated"}, ignorable=True)
        rows = list(log.events_of_type("memory/desktop-attempt"))
        assert len(rows) == 1
        rows[0]["data"]["timestamp"] = 2
        assert list(log.events_of_type("memory/desktop-attempt"))[0]["data"]["timestamp"] == 1
    finally:
        log.close()


@pytest.mark.asyncio
async def test_failed_first_context_records_attempt_before_request_and_restores_original_timestamp(host, tmp_path):
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="attempt", cwd=tmp_path)
    runtime = DesktopMemoryRuntime(backend(host))

    async def fail(operation, payload):
        if operation == "context":
            assert log.events[-1]["type"] == "memory/desktop-attempt"
            assert log.events[-1]["ignorable"] is True
            assert payload["require_existing"] is False
            raise OSError("synthetic bootstrap unavailable")

    host.before = fail
    try:
        await runtime.refresh(session(log), _MemoryTurn("synthetic question", "session-1", "original", 123))
        assert runtime._turn_scope is None
        assert "synthetic question" not in log.path.read_text()
    finally:
        await runtime.aclose()
        log.close()
    restored_log = SessionLog.open(root, session_id="attempt", cwd=tmp_path)
    restored = DesktopMemoryRuntime(backend(host))
    host.before = None
    try:
        turn = _MemoryTurn("synthetic question", "session-1", "original", 999)
        await restored.refresh(session(restored_log), turn)
        assert turn.timestamp == 123
        assert host.calls[-1][0] == "context"
        assert host.calls[-1][1]["require_existing"] is True
        assert sum(event["type"] == "memory/desktop-attempt" for event in restored_log.events) == 1
    finally:
        await restored.aclose()
        restored_log.close()


@pytest.mark.asyncio
async def test_log_flush_failure_never_sends_an_unprotected_context(host, tmp_path, monkeypatch):
    log = SessionLog.create(tmp_path / "sessions", session_id="failed-flush", cwd=tmp_path)
    runtime = DesktopMemoryRuntime(backend(host))
    try:
        monkeypatch.setattr(log, "flush", lambda: (_ for _ in ()).throw(OSError("synthetic")))
        target = session(log)
        await runtime.refresh(target, _MemoryTurn("question", "session", "turn", 123))
        assert not host.calls
        assert runtime._turn_scope is None
        assert target.memory_block is None
    finally:
        monkeypatch.undo()
        await runtime.aclose()
        log.close()


@pytest.mark.asyncio
async def test_attempt_lookup_failure_never_sends_an_unprotected_context(host, tmp_path, monkeypatch):
    log = SessionLog.create(tmp_path / "sessions", session_id="failed-scan", cwd=tmp_path)
    runtime = DesktopMemoryRuntime(backend(host))
    try:
        monkeypatch.setattr(log, "events_of_type", lambda *_: (_ for _ in ()).throw(OSError("synthetic")))
        target = session(log)
        await runtime.refresh(target, _MemoryTurn("question", "session-1", "turn", 123))
        assert not host.calls
        assert runtime._turn_scope is None
        assert target.memory_block is None
    finally:
        await runtime.aclose()
        log.close()


@pytest.mark.asyncio
async def test_long_session_keeps_new_turns_and_replays_evicted_attempts_after_restart(host, tmp_path):
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="long-session", cwd=tmp_path)
    for index in range(5001):
        key = hashlib.sha256(json.dumps(["session-1", f"turn-{index}"], ensure_ascii=False,
                                        separators=(",", ":")).encode()).hexdigest()
        log.append("memory/desktop-attempt", {"turn_key": key, "timestamp": index}, ignorable=True)
    log.flush()
    runtime = DesktopMemoryRuntime(backend(host))
    try:
        fresh = _MemoryTurn("question", "session-1", "new", 6001)
        await runtime.refresh(session(log), fresh)
        assert host.calls[-1][1]["require_existing"] is False
        assert len(runtime._attempts) <= 5000
        replay = _MemoryTurn("question", "session-1", "turn-0", 9999)
        await runtime.refresh(session(log), replay)
        assert replay.timestamp == 0
        assert host.calls[-1][1]["require_existing"] is True
    finally:
        await runtime.aclose()
        log.close()
    log = SessionLog.open(root, session_id="long-session", cwd=tmp_path)
    runtime = DesktopMemoryRuntime(backend(host))
    try:
        replay = _MemoryTurn("question", "session-1", "new", 9999)
        await runtime.refresh(session(log), replay)
        assert replay.timestamp == 6001
        assert host.calls[-1][1]["require_existing"] is True
        assert len(runtime._attempts) <= 5000
    finally:
        await runtime.aclose()
        log.close()
