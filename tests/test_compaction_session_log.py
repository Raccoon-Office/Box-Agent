"""Recovering pre-compaction context uses the active session's read-only log."""

import asyncio
import socket
from pathlib import PureWindowsPath
from shutil import which
from types import SimpleNamespace

import pytest
from acp.agent.connection import AgentSideConnection
from acp.connection import Connection

from box_agent.acp.stdio_compat import StdioTransport
from box_agent.kernel.context_engine import _is_summary_marker, _maybe_summarize
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog
from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.engine.execution import invoke_tool_once
from box_agent.tools.file.jsonl_tool import JsonlQueryTool
from box_agent.tools.file.read_tool import ReadTool
from box_agent.tools.file_tools import WriteTool
from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine
from box_agent.tools.ripgrep_tool import GlobTool, GrepTool
from tests.test_acp_history_edit import CaptureLLM, adapter


@pytest.fixture
def session_logs(tmp_path, monkeypatch):
    profile = tmp_path / "custom agent profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    own = SessionLog.create(profile / "sessions", session_id="active", cwd=workspace)
    other = SessionLog.create(profile / "sessions", session_id="other", cwd=workspace)
    try:
        yield workspace, own, other
    finally:
        own.close()
        other.close()


@pytest.mark.asyncio
async def test_session_tools_read_only_the_log_owned_by_the_current_invocation(session_logs):
    workspace, own, other = session_logs
    policy = CapabilityPolicy(session_workspace_root=str(workspace))
    engine = PermissionEngine(policy, workspace)
    settings = dict(workspace_dir=str(workspace), allow_full_access=False, permission_engine=engine)
    read, query, write = ReadTool(**settings), JsonlQueryTool(**settings), WriteTool(**settings)
    own_context = ToolInvocationContext(session_log=own)

    for tool in (read, query):
        result = await invoke_tool_once(tool, {"path": str(own.path)}, own_context)
        assert result.success, result.error
        assert result.permission_request is None
        assert "active" in result.content

    note = own.path.parent / "notes.txt"
    note.write_text("session-local notes", encoding="utf-8")
    result = await invoke_tool_once(read, {"path": str(note)}, own_context)
    assert result.success and "session-local notes" in result.content

    for log, context in ((other, own_context), (own, ToolInvocationContext(session_log=other)), (own, None)):
        result = await invoke_tool_once(read, {"path": str(log.path)}, context)
        assert not result.success
        assert "Access denied" in result.error

    result = await invoke_tool_once(write, {"path": str(note), "content": "changed"}, own_context)
    assert not result.success
    assert "Access denied" in result.error
    assert note.read_text(encoding="utf-8") == "session-local notes"


@pytest.mark.asyncio
async def test_session_log_read_exception_does_not_follow_links_outside_its_directory(session_logs):
    workspace, own, other = session_logs
    alias = own.path.parent / "alias.jsonl"
    try:
        alias.symlink_to(other.path)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    reader = ReadTool(
        workspace_dir=str(workspace), allow_full_access=False,
        permission_engine=PermissionEngine(CapabilityPolicy(), workspace),
    )
    result = await invoke_tool_once(reader, {"path": str(alias)}, ToolInvocationContext(session_log=own))
    assert not result.success
    assert "Access denied" in result.error


@pytest.mark.asyncio
async def test_searching_an_allowed_profile_returns_only_the_active_session_log(session_logs):
    executable = which("rg")
    if not executable:
        pytest.skip("ripgrep is unavailable")
    workspace, own, other = session_logs
    profile = own.path.parents[2]
    settings = dict(
        workspace_dir=str(workspace), allow_full_access=False, executable=executable,
        permission_engine=PermissionEngine(CapabilityPolicy(), workspace),
    )
    context = ToolInvocationContext(session_log=own)
    for tool, pattern in ((GlobTool(**settings), "**/session.jsonl"), (GrepTool(**settings), '"id"')):
        result = await invoke_tool_once(tool, {"path": str(profile), "pattern": pattern}, context)
        assert result.success, result.error
        assert own.path.parent.name in result.content
        assert other.path.parent.name not in result.content


@pytest.mark.asyncio
async def test_acp_denied_log_read_does_not_affect_another_sessions_own_read(tmp_path, monkeypatch):
    class ReadLogLLM(CaptureLLM):
        path = None

        async def generate_stream(self, messages, tools=None, **kwargs):
            if self.path is None:
                async for event in super().generate_stream(messages, tools, **kwargs):
                    yield event
                return
            assert any(tool.name == "query_jsonl" for tool in tools)
            path, self.path = self.path, None
            yield StreamEvent(
                type="finish", finish_reason="tool_calls", tool_calls=[ToolCall(
                    id="read-log", type="function",
                    function=FunctionCall(name="query_jsonl", arguments={"path": str(path), "limit": 1}),
                )],
            )

    async def on_message(method, params, notification):
        pass

    profile = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    llm = ReadLogLLM()
    host = adapter(workspace, llm)
    local, remote = socket.socketpair()
    client_reader, client_writer = await asyncio.open_connection(sock=local)
    agent_reader, agent_writer = await asyncio.open_connection(sock=remote)
    server = AgentSideConnection(host, StdioTransport(agent_reader, agent_writer))
    host._conn = server
    client = Connection(on_message, StdioTransport(client_reader, client_writer))

    async def request(method, params):
        return await asyncio.wait_for(client.send_request(method, params), timeout=8)

    try:
        await request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        sessions = []
        for name in ("first-session", "second-session"):
            response = await request("session/new", {
                "cwd": str(workspace), "mcpServers": [], "_meta": {"session_id": name},
            })
            sessions.append(response["sessionId"])
        target = host._sessions[sessions[1]].agent.session_log.path

        outcomes = []
        for identifier in sessions:
            llm.path = target
            response = await request("session/prompt", {
                "sessionId": identifier, "prompt": [{"type": "text", "text": "Read the session header."}],
            })
            assert response["stopReason"] == "end_turn"
            outcomes.append(next(m.content for m in llm.requests[-1] if m.tool_call_id == "read-log"))
        assert "Access denied" in outcomes[0]
        assert "second-session" in outcomes[1] and "Access denied" not in outcomes[1]
    finally:
        await host.aclose()
        await client.close()
        await server.close()
        client_writer.close()
        agent_writer.close()
        await asyncio.gather(client_writer.wait_closed(), agent_writer.wait_closed())


@pytest.mark.asyncio
@pytest.mark.parametrize("log_kind", ["native", "windows", "none"])
async def test_summary_points_to_the_actual_log_with_available_reader(session_logs, log_kind):
    _, own, _ = session_logs
    if log_kind == "native":
        log = own
        tools = {"query_jsonl": JsonlQueryTool()}
    elif log_kind == "windows":
        log = SimpleNamespace(path=PureWindowsPath(r"C:\Agent State\session id\session.jsonl"))
        tools = {"read_file": ReadTool()}
    else:
        log = None
        tools = None
    result = await _maybe_summarize(
        object(), [Message(role="system", content="system"), Message(role="user", content="last request")],
        10_000, 0, False, tools=tools, session_log=log, force=True, allow_llm_summary=False,
    )

    summary = next(message.content for message in result.messages if _is_summary_marker(message))
    hint = "query the full session log at: "
    if log is None:
        assert hint not in summary
    else:
        assert hint + str(log.path) + " (JSONL;" in summary
        assert summary.index(hint) < summary.index("pick up the last task")
        assert "tool-results" not in summary
        if log_kind == "native":
            assert "prefer query_jsonl over reading the whole file" in summary
        else:
            assert "read_file" in summary and "pag" in summary
