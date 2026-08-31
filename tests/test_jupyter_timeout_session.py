"""B2 regression: a timed-out execute_code must discard (not reuse) the kernel
session.

Before the fix, ``execute`` caught the timeout and returned an error while
leaving the busy kernel in ``self._sessions``. The next call reused that
still-executing kernel, interleaving its IOPub messages and corrupting output;
repeated timeouts could also exhaust the thread pool. The fix discards the
session on timeout so a fresh kernel is built next time.
"""

from __future__ import annotations

import queue
from pathlib import Path

import pytest

from box_agent.tools.jupyter_tool import (
    KERNEL_EXEC_TIMEOUT,
    JupyterKernelSession,
    JupyterSandboxTool,
    SandboxStatusTool,
)


# ── Low-level: the real session.execute must return the timeout sentinel ─────


class _BusyKernelClient:
    """Fake jupyter kernel client whose IOPub never reaches 'idle' — i.e. the
    kernel is still busy running the code past the per-call timeout."""

    def __init__(self):
        self.execute_called = False

    def execute(self, code, silent=False):
        self.execute_called = True
        return "msg-1"

    def get_iopub_msg(self, timeout=None):
        # Both the pre-execute drain loop and the post-execute collection loop
        # see an empty queue → kernel produced no idle status in time.
        raise queue.Empty()


class _IdleKernelClient:
    """Fake client that emits some stdout then an idle status (happy path)."""

    def __init__(self):
        self._pending: list[dict] = []

    def execute(self, code, silent=False):
        self._pending = [
            {"msg_type": "stream", "content": {"name": "stdout", "text": "hi\n"}},
            {"msg_type": "status", "content": {"execution_state": "idle"}},
        ]
        return "msg-1"

    def get_iopub_msg(self, timeout=None):
        if not self._pending:
            raise queue.Empty()
        return self._pending.pop(0)


def _bare_session(tmp_path: Path) -> JupyterKernelSession:
    # __init__ only stores args (no kernel start), so we can inject a fake _kc.
    return JupyterKernelSession("s", tmp_path / "ws", sandbox_env=object())


class _OwnedSession:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def is_alive(self) -> bool:
        return True


@pytest.fixture
def ready_sandbox(monkeypatch):
    """Keep timeout tests independent of real sandbox bootstrap state."""

    class _ReadySandbox:
        async def ensure_ready(self, on_progress=None) -> None:
            return None

    sandbox = _ReadySandbox()
    monkeypatch.setattr(
        JupyterSandboxTool,
        "_get_sandbox_env",
        lambda _self: sandbox,
    )


def test_low_level_execute_returns_timeout_sentinel_when_kernel_stays_busy(tmp_path):
    """The actual JupyterKernelSession.execute() must surface a hard-timeout
    sentinel (not silently succeed) when the idle status never arrives."""
    session = _bare_session(tmp_path)
    session._kc = _BusyKernelClient()

    stdout, images, error = session.execute("time.sleep(100)", timeout=1)

    assert error == KERNEL_EXEC_TIMEOUT
    assert stdout == ""
    assert images == []


@pytest.mark.asyncio
async def test_kernel_and_status_state_are_namespaced_by_host_session(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(JupyterSandboxTool, "_sessions", {})
    first = JupyterSandboxTool(
        workspace_dir=str(tmp_path / "first"),
        process_owner_id="acp-first",
    )
    second = JupyterSandboxTool(
        workspace_dir=str(tmp_path / "second"),
        process_owner_id="acp-second",
    )
    first._sessions[first._session_key("first-kernel")] = _OwnedSession(tmp_path / "first")
    second._sessions[second._session_key("second-kernel")] = _OwnedSession(tmp_path / "second")
    first._session_id = "first-kernel"
    second._session_id = "second-kernel"

    assert first._session_key("shared") != second._session_key("shared")
    assert [item["session_id"] for item in first.get_status()["sessions"]] == ["first-kernel"]
    assert [item["workspace"] for item in first.get_status()["sessions"]] == [
        str(tmp_path / "first")
    ]
    assert [item["workspace"] for item in second.get_status()["sessions"]] == [
        str(tmp_path / "second")
    ]

    first_status = await SandboxStatusTool(first).execute()
    second_status = await SandboxStatusTool(second).execute()
    assert "first-kernel" in first_status.content
    assert "second-kernel" not in first_status.content
    assert "second-kernel" in second_status.content
    assert "first-kernel" not in second_status.content


def test_low_level_execute_happy_path_still_succeeds(tmp_path):
    """Reaching idle must still return success (no false timeout)."""
    session = _bare_session(tmp_path)
    session._kc = _IdleKernelClient()

    stdout, images, error = session.execute("print('hi')", timeout=5)

    assert error is None
    assert "hi" in stdout


@pytest.mark.asyncio
async def test_outer_execute_discards_session_on_timeout_sentinel(
    tmp_path,
    ready_sandbox,
):
    """When a session's execute() returns the timeout sentinel (real low-level
    behavior), the tool must discard the session and report a timeout."""
    tool = JupyterSandboxTool(workspace_dir=str(tmp_path / "workspace"))
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    class _SentinelSession:
        def __init__(self, workspace):
            self.workspace = workspace
            self.stopped = False

        def is_alive(self):
            return True

        def execute(self, code, timeout=60):
            return ("", [], KERNEL_EXEC_TIMEOUT)

        async def stop(self):
            self.stopped = True

    session = _SentinelSession(ws)
    tool._sessions["s1"] = session

    result = await tool.execute(code="while True: pass", session_id="s1", timeout=1)

    assert not result.success
    assert "timed out" in (result.error or "").lower()
    assert "s1" not in tool._sessions
    assert session.stopped is True


# ── Outer-branch coverage (wait_for timeout path) ───────────────────────────


class _TimingOutSession:
    """Fake kernel session whose execute() always times out (synchronously),
    landing in the tool's ``except (asyncio.TimeoutError, TimeoutError)`` branch
    without the real ~180s wait."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.stopped = False

    def is_alive(self) -> bool:
        return True

    def execute(self, code, timeout=60):
        raise TimeoutError("simulated kernel hang")

    async def start(self):  # pragma: no cover - not used (already in _sessions)
        pass

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_execute_timeout_discards_session(tmp_path, ready_sandbox):
    tool = JupyterSandboxTool(workspace_dir=str(tmp_path / "workspace"))
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    session = _TimingOutSession(ws)
    tool._sessions["s1"] = session

    result = await tool.execute(code="while True: pass", session_id="s1", timeout=1)

    # Timed out → error surfaced.
    assert not result.success
    assert "timed out" in (result.error or "").lower()

    # The busy session must be removed (not reusable) and stopped.
    assert "s1" not in tool._sessions, "timed-out session was left in the pool for reuse"
    assert session.stopped is True, "timed-out session kernel was not stopped"


@pytest.mark.asyncio
async def test_execute_after_timeout_rebuilds_fresh_session(
    tmp_path,
    monkeypatch,
    ready_sandbox,
):
    """After a timeout discards the session, the next call must build a NEW
    session rather than reuse the corrupted one."""
    tool = JupyterSandboxTool(workspace_dir=str(tmp_path / "workspace"))
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    bad = _TimingOutSession(ws)
    tool._sessions["s1"] = bad
    await tool.execute(code="while True: pass", session_id="s1", timeout=1)
    assert "s1" not in tool._sessions

    # Next call for the same logical session id: stub session creation so we can
    # assert a fresh session is created and used (no reuse of `bad`).
    created = {}

    class _GoodSession:
        def __init__(self, workspace):
            self.workspace = workspace
            created["session"] = self

        def is_alive(self):
            return True

        def execute(self, code, timeout=60):
            return ("ok-output", [], None)

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(
        tool, "_create_session",
        lambda session_id, workspace, env: _GoodSession(workspace),
    )

    result = await tool.execute(code="print('hi')", session_id="s1", timeout=5)

    assert result.success, result.error
    assert "ok-output" in result.content
    assert created.get("session") is not None
    assert created["session"] is not bad
