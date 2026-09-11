from __future__ import annotations

import pytest

from box_agent.agent_service import AgentService
from box_agent.session_log import SessionLog


def test_agent_service_uses_injected_factory_without_protocol_dependencies() -> None:
    captured: dict[str, object] = {}

    class CaptureAgent:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    service = AgentService(agent_factory=CaptureAgent)
    agent = service.create_agent(
        llm_client=object(),
        system_prompt="system",
        tools=[],
        max_steps=2,
        tool_limits=None,
        workspace_dir="workspace",
        token_limit=100,
        session_log=None,
    )

    assert isinstance(agent, CaptureAgent)
    assert captured["system_prompt"] == "system"
    assert captured["session_log"] is None


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_failed_agent_creation_releases_session_writer(tmp_path, failure):
    log = SessionLog.create(tmp_path, session_id="construction", cwd=tmp_path)

    def fail_factory(**kwargs):
        raise failure("construction failed")

    with pytest.raises(failure):
        AgentService(agent_factory=fail_factory).create_agent(
            llm_client=object(), system_prompt="system", tools=[], session_log=log,
            max_steps=2, tool_limits=None, workspace_dir=str(tmp_path), token_limit=100,
        )
    reopened = SessionLog.open(tmp_path, session_id="construction", cwd=tmp_path)
    reopened.close()
