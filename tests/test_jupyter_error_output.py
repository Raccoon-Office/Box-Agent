"""Python diagnostics printed before a failure must reach the next model call."""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from box_agent.tools.file_result_adapter import _tool_message_content_for_model
from box_agent.tools.jupyter_tool import (
    InProcessKernelSession,
    JupyterKernelSession,
    JupyterSandboxTool,
)


class _DiagnosticKernelClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.pending = []

    def execute(self, code, silent=False):
        self.pending = [
            {"msg_type": "stream", "content": {"name": "stdout", "text": "buffer length: 0\n"}},
            {"msg_type": "stream", "content": {"name": "stderr", "text": "warning: empty input\n"}},
        ]
        if self.outcome == "error":
            self.pending.append({
                "msg_type": "error",
                "content": {"ename": "ValueError", "evalue": "missing record"},
            })
        if self.outcome != "timeout":
            self.pending.append({"msg_type": "status", "content": {"execution_state": "idle"}})
        return "msg-1"

    def get_iopub_msg(self, timeout=None):
        if not self.pending:
            raise queue.Empty()
        return self.pending.pop(0)

    def stop_channels(self):
        pass


@pytest.mark.parametrize("session_type", [JupyterKernelSession, InProcessKernelSession])
@pytest.mark.parametrize("outcome", ["success", "error", "timeout"])
async def test_execute_code_retains_diagnostics_in_model_history(
    tmp_path, monkeypatch, session_type, outcome,
):
    class ReadySandbox:
        _explicit_python = False

        async def ensure_ready(self, on_progress=None):
            pass

    env = ReadySandbox()
    workspace = tmp_path / "workspace"
    session = (
        session_type("s1", workspace, env)
        if session_type is JupyterKernelSession
        else session_type("s1", workspace)
    )
    session._kc = _DiagnosticKernelClient(outcome)
    session._km = SimpleNamespace(is_alive=lambda: True, shutdown_kernel=lambda **kwargs: None)
    monkeypatch.setattr(JupyterSandboxTool, "_sessions", {"s1": session})
    tool = JupyterSandboxTool(workspace_dir=str(workspace))
    monkeypatch.setattr(tool, "_get_sandbox_env", lambda: env)

    result = await tool.execute("print('buffer length:', 0)", session_id="s1", timeout=1)

    assert result.success is (outcome == "success")
    assert "buffer length: 0" in result.content
    assert "warning: empty input" in result.content
    model_content = _tool_message_content_for_model(
        tool_name="execute_code", arguments={}, result=result,
        visible_content=result.content, visible_error=result.error,
    )
    assert "buffer length: 0" in model_content
    assert "warning: empty input" in model_content
    if outcome == "error":
        assert "ValueError: missing record" in model_content
        assert "s1" in tool._sessions
    elif outcome == "timeout":
        assert "timed out" in model_content
        assert "s1" not in tool._sessions
