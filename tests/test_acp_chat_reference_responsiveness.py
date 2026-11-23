"""Slow cleanup checks must not starve other sessions or ACP updates."""

from tests.acp_helpers import acp_params
from acp.schema import RequestPermissionRequest

import asyncio
import threading
import shlex
import sys
from contextlib import closing
from types import SimpleNamespace

import pytest
from acp import text_block, update_agent_message
from acp.schema import AllowedOutcome, RequestPermissionResponse

from box_agent import session_log as session_log_module
from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.session_log import SessionLog, default_session_root
from box_agent.tools import chat_references
from box_agent.tools.bash_tool import BashTool
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from tests.test_acp import DummyConn, DoneLLM


@pytest.mark.asyncio
async def test_acp_scratch_reference_and_concurrent_goal_survive_log_reopen(tmp_path, monkeypatch):
    class PreviewLLM(DoneLLM):
        async def generate_stream(self, messages, tools=None, **kwargs):
            if any("Show the scratch preview" in str(msg.content) for msg in messages if msg.role == "user"):
                yield StreamEvent(type="text", delta=f"![Preview]({preview.as_uri()})")
                yield StreamEvent(type="finish", finish_reason="stop")
            else:
                async for event in super().generate_stream(messages, tools, **kwargs):
                    yield event

    agent = BoxACPAgent(DummyConn(), Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            workspace_dir=str(tmp_path), max_steps=1, goal_autopilot_enabled=False,
            enable_memory=False, enable_memory_extraction=False, memory_maintainer_enabled=False,
        ),
        tools=ToolsConfig(enable_sub_agent=False),
    ), PreviewLLM(), [], "system")
    session_id = "scratch-preview-concurrent-goal"
    session = await agent.new_session(**acp_params(SimpleNamespace(
        cwd=str(tmp_path), field_meta={"session_mode": "general", "session_id": session_id},
    )))
    other = await agent.new_session(**acp_params(SimpleNamespace(
        cwd=str(tmp_path), field_meta={"session_mode": "general", "session_id": "independent-session"},
    )))
    scratch = await agent._sessions[session.sessionId].skill_scratch_dir.ensure()
    preview = scratch / "preview.png"
    preview.write_bytes(b"preview")
    disposable = scratch / "disposable.txt"
    disposable.touch()

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    scan_threads = set()
    writing_reference = asyncio.Event()
    release_reference = threading.Event()
    writing_goal = threading.Event()
    encode = session_log_module._encode_record
    scan = chat_references.local_dependencies

    def observe_scan(path, root):
        if path == preview:
            scan_threads.add(threading.get_ident())
        return scan(path, root)

    def hold_reference_write(record):
        event_type = record.get("type")
        if event_type == "chat/reference":
            # append() has assigned a sequence number. A competing writer at
            # this point could use the same number before the record is stored.
            loop.call_soon_threadsafe(writing_reference.set)
            assert threading.get_ident() != loop_thread, "Reference write ran on the ACP event loop"
            assert release_reference.wait(10)
        elif event_type == "goal/change":
            writing_goal.set()
        return encode(record)

    monkeypatch.setattr(chat_references, "local_dependencies", observe_scan)
    monkeypatch.setattr(session_log_module, "_encode_record", hold_reference_write)
    turn = asyncio.create_task(agent.prompt(**acp_params(SimpleNamespace(
        sessionId=session.sessionId, prompt=[{"text": "Show the scratch preview"}],
    ))))
    goal = None
    objective = "Keep this goal through a log reopen"
    try:
        await asyncio.wait_for(writing_reference.wait(), 5)
        assert scan_threads and loop_thread not in scan_threads
        goal = asyncio.create_task(agent.ext_method("goal", {
            "sessionId": session.sessionId, "action": "set", "objective": objective,
        }))
        response = await asyncio.wait_for(agent.prompt(**acp_params(SimpleNamespace(
            sessionId=other.sessionId, prompt=[{"text": "hello"}],
        ))), 5)
        assert response.stopReason == "end_turn"
        assert not turn.done()
        assert not writing_goal.is_set()
        release_reference.set()
        assert (await asyncio.wait_for(turn, 5)).stopReason == "end_turn"
        assert (await asyncio.wait_for(goal, 5))["ok"]
    finally:
        release_reference.set()
        await asyncio.gather(turn, *((goal,) if goal is not None else ()), return_exceptions=True)
        await agent.aclose()

    assert preview.read_bytes() == b"preview"
    assert not disposable.exists()
    with closing(SessionLog.open(default_session_root(), session_id=session_id, cwd=tmp_path)) as reopened:
        references = list(reopened.events_of_type("chat/reference"))
        goals = list(reopened.events_of_type("goal/change"))
        assert len(references) == len(goals) == 1
        assert references[0]["data"]["paths"] == [str(preview)]
        assert goals[0]["data"]["goal"]["objective"] == objective
        assert references[0]["seq"] < goals[0]["seq"]


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_bash", [False, True])
@pytest.mark.parametrize("python_script", [False, True], ids=["shell", "python-runtime"])
async def test_slow_cleanup_check_keeps_other_session_execution_and_updates_responsive(tmp_path, monkeypatch, direct_bash, python_script):
    import box_agent.tools.chat_references as references

    script = tmp_path / "cleanup.py"
    script.write_text("import os\nos.rmdir('cache')\n")
    command_text = shlex.join([sys.executable, str(script)]) if python_script else "rm -rf cache"
    entered, release = threading.Event(), threading.Event()

    def slow_scan(path, workspace=None):
        assert threading.current_thread() is not threading.main_thread(), "Cleanup scan blocked ACP event loop"
        entered.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr(references, "retained_chat_file_in", slow_scan)
    class CleanupConn(DummyConn):
        async def request_permission(self, session_id, tool_call, options, **meta):
            request = RequestPermissionRequest(session_id=session_id, tool_call=tool_call, options=options, field_meta=meta or None)
            return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", optionId="approve"))

    conn = CleanupConn()
    class CleanupLLM(DoneLLM):
        async def generate_stream(self, messages, tools=None, **kwargs):
            if any("cleanup cache" in str(message.content) for message in messages if message.role == "user") and not any(message.role == "tool" for message in messages):
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                    id="cleanup", type="function", function=FunctionCall(name="bash", arguments={"command": command_text}),
                )])
            else:
                async for event in super().generate_stream(messages, tools, **kwargs):
                    yield event

    agent = BoxACPAgent(conn, Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path), enable_memory=False, enable_memory_extraction=False, memory_maintainer_enabled=False),
        tools=ToolsConfig(enable_sub_agent=False, allow_full_access=True),
    ), CleanupLLM(), [], "system")
    session = await agent.new_session(**acp_params(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})))
    old_session = await agent.new_session(**acp_params(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})))
    (tmp_path / "cache").mkdir()
    tool = BashTool(workspace_dir=str(tmp_path), bypass_dangerous_command_approval=True)
    command = {"command": command_text}
    cleanup = asyncio.create_task(
        tool.execute(**command) if direct_bash else agent.prompt(**acp_params(SimpleNamespace(
            sessionId=old_session.sessionId, prompt=[{"text": "cleanup cache"}],
        )))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(agent._send(session.sessionId, update_agent_message(text_block("still responsive"))), 0.5)
        response = await asyncio.wait_for(agent.prompt(**acp_params(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{"text": "hello"}],
        ))), 1)
        assert response.stopReason == "end_turn"
        assert not cleanup.done()
        assert any(getattr(item.update, "sessionUpdate", None) == "agent_message_chunk" for item in conn.updates)
    finally:
        release.set()
        await asyncio.gather(cleanup, return_exceptions=True)
        await agent.aclose()
    result = await cleanup
    assert result.success if direct_bash else result.stopReason == "end_turn"
    assert not (tmp_path / "cache").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_bash", [False, True])
@pytest.mark.parametrize("python_script", [False, True], ids=["shell", "python-runtime"])
async def test_cancelled_reference_check_never_executes_pending_cleanup(tmp_path, monkeypatch, direct_bash, python_script):
    import box_agent.tools.chat_references as references
    from box_agent.tools.base import ToolInvocationContext
    from box_agent.tools.engine.execution import invoke_tool_once

    script = tmp_path / "cleanup.py"
    script.write_text("import os\nos.rmdir('cache')\n")
    command_text = shlex.join([sys.executable, str(script)]) if python_script else "rm -rf cache"
    entered, release = threading.Event(), threading.Event()
    def slow_scan(path, workspace=None):
        entered.set()
        assert release.wait(5)
        return None
    monkeypatch.setattr(references, "retained_chat_file_in", slow_scan)
    folder = tmp_path / "cache"
    folder.mkdir()
    tool = BashTool(workspace_dir=str(tmp_path), bypass_dangerous_command_approval=True)
    command = {"command": command_text}
    task = asyncio.create_task(tool.execute(**command) if direct_bash else invoke_tool_once(
        tool, command, ToolInvocationContext(chat_references=references.ChatReferences(tmp_path)),
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        if python_script:
            # Keep the runtime scan blocked until its subprocess has stopped;
            # one event-loop tick does not complete process termination.
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            # The pre-execution scan joins its worker before cancellation
            # completes, so it needs to be released before awaiting the task.
            await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert folder.is_dir()
