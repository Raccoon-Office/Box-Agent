import asyncio
import errno
import socket
import threading
from copy import deepcopy
from pathlib import Path

import pytest
from acp import RequestError

from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.session_log import SessionLog, SessionLogDurabilityError, SessionLogInUseError
from tests.acp_helpers import acp_params
from tests.test_acp_history_edit import CaptureLLM, adapter, request


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", ["turn/binding", "user/message", "assistant/message", "turn/end"])
async def test_another_session_can_finish_while_a_checkpoint_is_on_disk(tmp_path, monkeypatch, checkpoint):
    from acp.agent.connection import AgentSideConnection
    from acp.connection import Connection
    from box_agent import session_log
    from box_agent.acp.stdio_compat import StdioTransport

    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    flush_threads = []
    updates = []

    async def on_message(method, params, notification):
        if method == "session/update":
            updates.append(params)

    host = adapter(workspace, CaptureLLM())
    local, remote = socket.socketpair()
    client_reader, client_writer = await asyncio.open_connection(sock=local)
    agent_reader, agent_writer = await asyncio.open_connection(sock=remote)
    server = AgentSideConnection(host, StdioTransport(agent_reader, agent_writer))
    host._conn = server
    client = Connection(on_message, StdioTransport(client_reader, client_writer))

    async def prompt(session, turn):
        return await client.send_request("session/prompt", {
            "sessionId": session, "prompt": [{"type": "text", "text": turn}], "_meta": {"turn_id": turn},
        })

    first = None
    try:
        await client.send_request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        sessions = []
        for name in ("slow", "independent"):
            response = await client.send_request("session/new", {
                "cwd": str(workspace), "mcpServers": [], "_meta": {"session_id": name},
            })
            sessions.append(response["sessionId"])
        slow, independent = sessions
        log = host._sessions[slow].agent.session_log
        slow_fd = log._handle.fileno()
        fsync = session_log._timed_fsync

        def slow_fsync(fd):
            if fd == slow_fd and not flush_threads and log.events[-1]["type"] == checkpoint:
                flush_threads.append(threading.get_ident())
                loop.call_soon_threadsafe(entered.set)
                if flush_threads[0] != loop_thread:
                    assert release.wait(10), "independent session did not finish during fsync"
            fsync(fd)

        monkeypatch.setattr(session_log, "_timed_fsync", slow_fsync)
        first = asyncio.create_task(prompt(slow, "slow-turn"))
        await asyncio.wait_for(entered.wait(), 8)
        assert flush_threads != [loop_thread], "session fsync ran on the ACP event loop"
        if checkpoint == "user/message":
            # A retry must not receive a durable acceptance receipt while the
            # input is only in the writer's in-memory history.
            assert not log.has_committed_input("slow-turn")
            with pytest.raises(RequestError) as busy:
                await asyncio.wait_for(prompt(slow, "slow-turn"), 3)
            assert busy.value.data["code"] == "SESSION_BUSY"
        second = await asyncio.wait_for(prompt(independent, "second-turn"), 8)
        assert second["stopReason"] == "end_turn"
        assert any(item["sessionId"] == independent and
                   item["update"]["sessionUpdate"] == "agent_message_chunk" for item in updates)
        assert not first.done()
        release.set()
        assert (await asyncio.wait_for(first, 8))["stopReason"] == "end_turn"
        assert log.has_committed_input("slow-turn")
        assert [event["seq"] for event in log.events] == list(range(len(log.events)))
        assert log.replay().messages[-1].content == "generated-answer"
    finally:
        release.set()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
        await host.aclose()
        await client.close()
        await server.close()
        client_writer.close()
        agent_writer.close()
        await asyncio.gather(client_writer.wait_closed(), agent_writer.wait_closed())


@pytest.mark.asyncio
async def test_recovered_id_created_by_older_agent_resumes_from_legacy_directory(tmp_path, monkeypatch):
    import box_agent.acp as acp

    session_id = "desktop-recovery-90eb2590-5c6f-473c-9178-5caf9f6bd821"
    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    old = adapter(workspace, CaptureLLM())
    try:
        with monkeypatch.context() as legacy:
            legacy.setattr(acp, "_session_storage_root", lambda _: profile / "sessions")
            created = await old.new_session(str(workspace), session_id=session_id)
            await old.prompt(**acp_params(request(created, "old runtime context", "previous")))
            path = old._sessions[created.sessionId].agent.session_log.path
    finally:
        await old.aclose()

    llm = CaptureLLM()
    current = adapter(workspace, llm)
    try:
        await current.resume_session(str(workspace), created.sessionId)
        assert current._sessions[created.sessionId].agent.session_log.path == path
        await current.prompt(**acp_params(request(created, "new message", "next")))
        assert len(llm.requests) == 1
        assert "old runtime context" in repr(llm.requests[0])
        assert not (profile / "recovered-sessions").exists()
    finally:
        await current.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sharing", "lock_access", "log_read", "log_write"])
async def test_resume_storage_failure_allows_text_in_a_separate_session(tmp_path, monkeypatch, failure):
    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    source = adapter(workspace, CaptureLLM())
    try:
        original = await source.new_session(cwd=str(workspace), session_id="original")
        await source.prompt(**acp_params(request(original, "prior request", "previous-turn")))
        original_path = source._sessions[original.sessionId].agent.session_log.path
        original_bytes = original_path.read_bytes()
        task_record = workspace / ".box-agent/task-registry/tasks/original.json"
        task_bytes = task_record.read_bytes()
    finally:
        await source.aclose()

    llm = CaptureLLM()
    host = adapter(workspace, llm)
    open_projection = SessionLog.open_with_projection
    flush = SessionLog.flush

    def open_log(root, **options):
        if options["session_id"] == "original":
            if failure in {"sharing", "lock_access"}:
                error = PermissionError(errno.EACCES, "open failed", str(original_path.with_name(".writer.lock")))
                error.winerror = 32 if failure == "sharing" else 5
                if failure == "sharing":
                    raise SessionLogInUseError("busy") from error
                raise error
            if failure == "log_read":
                raise PermissionError(errno.EACCES, "read failed", str(original_path))
        return open_projection(root, **options)

    def flush_log(log):
        if log.path == original_path and failure == "log_write":
            raise SessionLogDurabilityError("fsync failed") from OSError(errno.ENOSPC, "disk full")
        return flush(log)

    monkeypatch.setattr(SessionLog, "open_with_projection", staticmethod(open_log))
    monkeypatch.setattr(SessionLog, "flush", flush_log)
    try:
        with pytest.raises(RequestError) as caught:
            await host.resume_session(cwd=str(workspace), session_id=original.sessionId)
        assert caught.value.code == -32012
        data = caught.value.data
        if failure == "sharing":
            assert data == {"code": "SESSION_BUSY", "resource": "writer_lock", "winerror": 32}
        else:
            assert data["code"] == "SESSION_STORAGE_UNAVAILABLE"
            assert data["operation"] == ("restore" if failure == "log_write" else "open")
        if failure == "lock_access":
            assert (data["reason"], data["resource"], data["winerror"]) == ("access_denied", "writer_lock", 5)
        assert llm.requests == []

        unchanged_since_failure = original_path.read_bytes()
        assert unchanged_since_failure.startswith(original_bytes)
        replacement_id = "desktop-recovery-1e2ec0dc-4603-4516-b385-273ada194b7d"
        replacement = await host.new_session(cwd=str(workspace), session_id=replacement_id)
        prompt = request(replacement, "new request", "next-turn")
        prompt.field_meta["session_continuation"] = {
            "schema_version": "officev3-session-continuation/v1",
            "product_session_id": replacement_id,
            "messages": [{"role": "user", "content": "prior request"}],
        }
        response = await host.prompt(**acp_params(prompt))
        assert response.stopReason == "end_turn"
        assert len(llm.requests) == 1
        assert "prior request" in repr(llm.requests[0])
        assert "new request" in repr(llm.requests[0])
        assert "artifacts and outcomes before repeating earlier actions" in repr(llm.requests[0])
        assert original_path.read_bytes() == unchanged_since_failure
        assert task_record.read_bytes() == task_bytes
        assert task_record.with_name(f"{replacement_id}.json").exists()
        path = host._sessions[replacement.sessionId].agent.session_log.path
        assert path.is_relative_to(profile / "recovered-sessions")
        await host.resume_session(cwd=str(workspace), session_id=replacement.sessionId)
        assert host._sessions[replacement.sessionId].agent.session_log.path == path
        assert len(llm.requests) == 1
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_prompt_write_failure_has_a_terminal_storage_result_without_replaying(tmp_path, monkeypatch):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.new_session(cwd=str(tmp_path), session_id="original")
        log = host._sessions[session.sessionId].agent.session_log
        append = log.append

        def write(event_type, *args, **kwargs):
            if event_type == "turn/end":
                log._failed = True
                raise SessionLogDurabilityError("write failed") from OSError(errno.ENOSPC, "disk full")
            return append(event_type, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(log, "append", write)
            with pytest.raises(RequestError) as caught:
                await host.prompt(**acp_params(request(session, "user request", "current")))
        assert caught.value.data["code"] == "SESSION_STORAGE_UNAVAILABLE"
        assert caught.value.data["operation"] == "prompt"
        assert len(llm.requests) == 1
        assert not host._sessions[session.sessionId]._prompt_in_progress
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_denied_scratch_keeps_tools_running_and_acp_serving(tmp_path, monkeypatch):
    from acp.agent.connection import AgentSideConnection
    from acp.connection import Connection
    from box_agent.acp.stdio_compat import StdioTransport
    from box_agent.tools import skill_scratch

    class ScratchLLM(CaptureLLM):
        request_scratch = False

        async def generate_stream(self, messages, tools=None, **kwargs):
            if self.request_scratch:
                self.request_scratch = False
                self.requests.append(deepcopy(messages))
                yield StreamEvent(type="finish", finish_reason="tool_calls", tool_calls=[
                    ToolCall(
                        id="scratch-tool", type="function",
                        function=FunctionCall(name="bash", arguments={"command": "echo scratch-$((1+1))"}),
                    ),
                ])
            else:
                async for event in super().generate_stream(messages, tools, **kwargs):
                    yield event

    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    attempts = []

    def deny_scratch(root, relative_path):
        attempts.append(root / relative_path)
        if threading.get_ident() != loop_thread:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10), "scratch preparation was not released"
        fail_at(root / relative_path)

    monkeypatch.setattr(skill_scratch, "_prepare_real_directory_chain", deny_scratch)
    llm = ScratchLLM()
    host = adapter(workspace, llm)
    local, remote = socket.socketpair()
    client_reader, client_writer = await asyncio.open_connection(sock=local)
    agent_reader, agent_writer = await asyncio.open_connection(sock=remote)
    server = AgentSideConnection(host, StdioTransport(agent_reader, agent_writer))
    host._conn = server

    async def on_message(method, params, notification):
        pass

    client = Connection(on_message, StdioTransport(client_reader, client_writer))

    async def new_session(name):
        result = await client.send_request("session/new", {
            "cwd": str(workspace), "mcpServers": [], "_meta": {"session_id": name},
        })
        return result["sessionId"]

    async def prompt(session_id, turn_id):
        response = await client.send_request("session/prompt", {
            "sessionId": session_id, "prompt": [{"type": "text", "text": turn_id}],
            "_meta": {"turn_id": turn_id},
        })
        assert response["stopReason"] == "end_turn"

    failed_tool_turn = None
    try:
        await client.send_request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        first = await asyncio.wait_for(new_session("first"), 5)
        await asyncio.wait_for(prompt(first, "plain-text"), 5)
        assert not attempts
        assert not (workspace / ".box-agent-scratch").exists()

        llm.request_scratch = True
        failed_tool_turn = asyncio.create_task(prompt(first, "try-scratch"))
        await asyncio.wait_for(started.wait(), 5)
        second = await asyncio.wait_for(new_session("second"), 5)
        await asyncio.wait_for(prompt(second, "independent-text"), 5)
        assert not failed_tool_turn.done()

        release.set()
        await asyncio.wait_for(failed_tool_turn, 5)
        # Non-fatal: the command still ran and the model is not told about scratch.
        assert "scratch-2" in repr(llm.requests[-1])
        assert "SKILL_SCRATCH_UNAVAILABLE" not in repr(llm.requests[-1])
        await asyncio.wait_for(prompt(first, "text-after-the-error"), 5)
        assert "text-after-the-error" in repr(llm.requests[-1])
        assert len(attempts) == 1
    finally:
        release.set()
        if failed_tool_turn is not None:
            await asyncio.gather(failed_tool_turn, return_exceptions=True)
        await host.aclose()
        await client.close()
        await server.close()
        client_writer.close()
        agent_writer.close()
        await asyncio.gather(client_writer.wait_closed(), agent_writer.wait_closed())


def fail_at(path: Path):
    raise PermissionError(errno.EACCES, "workspace denied", str(path))
