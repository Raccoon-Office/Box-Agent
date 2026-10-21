"""Slow cleanup checks must not starve other sessions or ACP updates."""

import asyncio
import threading
import shlex
import sys
from types import SimpleNamespace

import pytest
from acp import text_block, update_agent_message
from acp.schema import AllowedOutcome, RequestPermissionResponse

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.tools.bash_tool import BashTool
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from tests.test_acp import DummyConn, DoneLLM


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_bash", [False, True])
@pytest.mark.parametrize("python_script", [False, True], ids=["shell", "python-runtime"])
async def test_slow_cleanup_check_keeps_other_session_execution_and_updates_responsive(tmp_path, monkeypatch, direct_bash, python_script):
    import box_agent.tools.chat_references as references

    script = tmp_path / "cleanup.py"
    script.write_text("import os\nos.rmdir('cache')\n")
    command_text = shlex.join([sys.executable, str(script)]) if python_script else "rm -rf cache"
    entered, release = threading.Event(), threading.Event()

    def slow_scan(path):
        assert threading.current_thread() is not threading.main_thread(), "Cleanup scan blocked ACP event loop"
        entered.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr(references, "retained_chat_file_in", slow_scan)
    class CleanupConn(DummyConn):
        async def requestPermission(self, request):
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
    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    old_session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    (tmp_path / "cache").mkdir()
    tool = BashTool(workspace_dir=str(tmp_path), bypass_dangerous_command_approval=True)
    command = {"command": command_text}
    cleanup = asyncio.create_task(
        tool.execute(**command) if direct_bash else agent.prompt(SimpleNamespace(
            sessionId=old_session.sessionId, prompt=[{"text": "cleanup cache"}],
        ))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(agent._send(session.sessionId, update_agent_message(text_block("still responsive"))), 0.5)
        response = await asyncio.wait_for(agent.prompt(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{"text": "hello"}],
        )), 1)
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
    def slow_scan(path):
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
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert folder.is_dir()
