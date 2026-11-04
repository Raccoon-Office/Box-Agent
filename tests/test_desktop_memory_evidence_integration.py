"""Regression at the real AgentService-to-evidence handoff boundary."""

import asyncio
import json

from box_agent.agent_service import AgentService
from box_agent.agent_session import AgentSession
from box_agent.api import RunRequest
from box_agent.schema import Message
from box_agent.session_context import HostBindings, SessionOptions
from box_agent.session_log import SessionLog
from box_agent.turn_hints import with_turn_hints
from tests.test_agent_session import session_config
from tests.test_desktop_memory_backend import backend, host  # noqa: F401 -- pytest fixture
from tests.test_session_plugins import RecordingLLM


async def test_repeated_user_message_does_not_handoff_unfinished_previous_tool_result(tmp_path, host):
    log = SessionLog.create(tmp_path / "canonical", session_id="actual-log", cwd=tmp_path)
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    process = backend(host)
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=RecordingLLM("new answer"), memory_manager=process, tools=[],
        system_prompt="BASE", session_log=log), options=SessionOptions(profile="acp", workspace_dir=tmp_path))
    try:
        user = "Repeat this task"
        session.agent.add_user_message(with_turn_hints(user))
        session.agent._persist_unlogged_messages(turn=1, step=None)
        log.append("tool/call", {"callId": "old", "name": "run_check", "arguments": {}})
        session.agent.messages.append(Message(role="tool", tool_call_id="old", content="OLD_TURN_RESULT"))
        session.agent._persist_unlogged_messages(turn=1, step=None, tool_result_metadata={
            "old": {"success": True, "content": "OLD_TURN_RESULT"},
        })
        start = log.event_count
        handle = await AgentService().start(RunRequest(
            run_id="new-turn", session_id="session-1", user_message=user), session=session)
        assert (await handle.result()).status.value == "completed"
        await asyncio.gather(*tuple(session.memory_runtime.tasks))
        evidence = next(payload["evidence"] for operation, payload in host.calls if operation == "qa/evidence")
        assert evidence["status"] == "captured"
        assert all(view["source_ref"]["event_seqs"][0] >= start for view in evidence["views"])
        assert "OLD_TURN_RESULT" not in json.dumps(evidence)
    finally:
        await session.aclose()
        await process.aclose()
        log.close()
