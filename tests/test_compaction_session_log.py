"""Verify pre-compaction session log discovery and read access."""

from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from box_agent.kernel.context_engine import _is_summary_marker, _maybe_summarize
from box_agent.runtime import invoke_tool_with_permissions
from box_agent.schema import Message
from box_agent.session_log import SessionLog
from box_agent.tools.file.jsonl_tool import JsonlQueryTool
from box_agent.tools.file.read_tool import ReadTool
from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine
from box_agent.user_paths import state_path


@pytest.fixture
def session_log(tmp_path, monkeypatch):
    home = tmp_path / "os-home"
    home.mkdir()
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log = SessionLog.create(state_path("sessions"), session_id="active-session", cwd=workspace)
    try:
        yield workspace, log
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_type", [ReadTool, JsonlQueryTool])
async def test_restricted_session_log_read_needs_no_permission_negotiation(session_log, reader_type):
    workspace, log = session_log
    policy = CapabilityPolicy(session_workspace_root=str(workspace))
    reader = reader_type(
        workspace_dir=str(workspace),
        allow_full_access=False,
        permission_engine=PermissionEngine(policy, workspace),
    )
    negotiator = SimpleNamespace(negotiate=AsyncMock(return_value=False))

    result, decision = await invoke_tool_with_permissions(
        reader, {"path": str(log.path)}, permission_negotiator=negotiator,
    )

    assert result.success, result.error
    assert "active-session" in result.content
    assert result.permission_request is None
    assert decision is None
    negotiator.negotiate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("log_kind", ["native", "windows", "none"])
async def test_summary_points_to_the_actual_log_with_available_reader(session_log, log_kind):
    _, own = session_log
    if log_kind == "native":
        log = own
        tools = {"query_jsonl": JsonlQueryTool()}
    elif log_kind == "windows":
        log = SimpleNamespace(path=PureWindowsPath(r"C:\Agent State\session id\session.jsonl"))
        tools = {"read_file": ReadTool()}
    else:
        log = None
        tools = None
    from tests.test_context_compaction_retention import _SummaryProvider

    result = await _maybe_summarize(
        _SummaryProvider(), [Message(role="system", content="system"), Message(role="user", content="last request")],
        10_000, 0, False, tools=tools, session_log=log, force=True,
    )

    summary = next(message.content for message in result.messages if _is_summary_marker(message))
    hint = "query the full session log at: "
    if log is None:
        assert hint not in summary
    else:
        assert hint + str(log.path) + " (JSONL;" in summary
        assert summary.index("pick up the last task") < summary.index(hint)
        assert "tool-results" not in summary
        if log_kind == "native":
            assert "prefer query_jsonl over reading the whole file" in summary
        else:
            assert "read_file" in summary and "pag" in summary
