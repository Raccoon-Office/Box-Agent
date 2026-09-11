"""Skill delivery and recovery through the shared public Session entrypoint."""

from hashlib import sha256

import pytest

from box_agent.agent_session import AgentSession
from box_agent.events import DoneEvent, StopReason
from box_agent.session_log import SessionLog
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_tool import GetSkillTool
from tests.test_agent_session import session_config
from tests.test_context_request_commit import BODY, RecoverableStore
from tests.test_skill_entry_boundaries import CapturingProvider, loader_at


@pytest.mark.asyncio
@pytest.mark.parametrize("with_reader", [False, True])
async def test_session_uses_supplied_runtime_for_ordinary_selected_material(tmp_path, with_reader):
    loader = loader_at(tmp_path / "skills")
    runtime = SkillRuntime(loader)
    runtime.select(["demo"])
    provider = CapturingProvider()
    log = SessionLog.create(tmp_path / "sessions", session_id="selected", cwd=tmp_path)
    try:
        session = AgentSession.create(
            config=session_config(tmp_path), llm_client=provider, system_prompt="BASE",
            tools=[GetSkillTool(loader)] if with_reader else [],
            skill_runtime=runtime, skill_loader=loader, session_log=log,
        )
        session.agent.add_user_message("Use the assigned method")
        events = [event async for event in session.run_events(
            options=session.build_run_options(logger=None),
        )]
        assert session.agent.skill_runtime is runtime
        assert runtime.loader is session.skill_loader
        assert runtime.session_log is log
        assert len(provider.requests) == 1
        assert any(message.role == "user" and "METHOD_BODY" in str(message.content)
                   for message in provider.requests[0])
        assert all("METHOD_BODY" not in str(message.content)
                   for message in provider.requests[0] if message.role in {"system", "developer"})
        assert [message.content for message in session.agent.messages if message.role == "user"] == [
            "Use the assigned method",
        ]
        assert runtime.turn_deliveries["demo"]["complete"]
        assert log.replay().skills[0]["name"] == "demo"
        assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [StopReason.END_TURN]
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["context_append", "context_flush"])
async def test_session_retry_preserves_restored_material_after_uncommitted_request(tmp_path, fault):
    log = SessionLog.create(tmp_path / "sessions", session_id="restore", cwd=tmp_path)
    log.append("skill/change", {"skills": [{
        "name": "demo", "sha256": sha256(BODY.encode()).hexdigest(), "loadOrder": 1,
    }]})
    log.flush()
    provider = CapturingProvider()
    runtime = SkillRuntime(None)
    try:
        session = AgentSession.create(
            config=session_config(tmp_path), llm_client=provider, system_prompt="BASE", tools=[],
            skill_runtime=runtime, session_log=RecoverableStore(log, fault),
        )
        session.agent.restore_active_skill_instructions([
            ("demo", BODY, sha256(BODY.encode()).hexdigest(), 1),
        ])
        session.agent.add_user_message("Continue")
        with pytest.raises(OSError, match="recoverable store failure"):
            _ = [event async for event in session.run_events()]
        assert provider.requests == []
        assert not session.turn_active
        assert runtime.turn_deliveries == {}

        session.agent.add_user_message("Retry")
        _ = [event async for event in session.run_events()]
        assert len(provider.requests) == 1
        assert BODY in str(provider.requests[0])
        session.agent.clear_active_skill_instructions()
        session.agent.add_user_message("Continue without that method")
        _ = [event async for event in session.run_events()]
        assert BODY not in str(provider.requests[-1])
        assert log.replay().skills == []
    finally:
        log.close()
