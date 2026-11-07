"""Session-level protocol pinning and runtime-message compatibility."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.agent import Agent
from box_agent.agent_session import AgentSession
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import Message
from box_agent.session_log import SessionLog, SessionLogIncompatible
from box_agent.tools.browser_intent import has_recent_successful_browser_context
from box_agent.tools.file_tools import WriteTool


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


def test_new_host_session_uses_stable_tools_and_public_agent_keeps_legacy_default(tmp_path):
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path)),
                    tools=ToolsConfig())
    session = AgentSession.create(config=config, llm_client=SimpleNamespace(),
                                  system_prompt="system", tools=[])
    assert session.agent.tool_interface_version == 2
    assert "call_tool" in session.agent.tools
    legacy = Agent(SimpleNamespace(), "system", [], workspace_dir=str(tmp_path))
    assert legacy.tool_interface_version == 1
    assert "call_tool" not in legacy.tools


@pytest.mark.parametrize("version", [1, 2])
def test_session_keeps_pinned_tool_protocol_after_config_change_and_reopen(tmp_path, version):
    log = SessionLog.create(tmp_path / "logs", session_id="session", cwd=tmp_path)
    try:
        assert log.bind_tool_interface_version(version) == version
        assert log.bind_tool_interface_version(3 - version) == version
    finally:
        log.close()
    log = SessionLog.open(tmp_path / "logs", session_id="session", cwd=tmp_path)
    try:
        agent = Agent(SimpleNamespace(), "system", [], workspace_dir=str(tmp_path),
                      session_log=log, tool_interface_version=3 - version)
        assert agent.tool_interface_version == version
        assert len([event for event in log.events if event["type"] == "tools/contract"]) == (1 if version == 2 else 0)
    finally:
        log.close()


def test_unmarked_historical_session_stays_legacy_even_when_empty(tmp_path):
    log = SessionLog.create(tmp_path / "logs", session_id="session", cwd=tmp_path)
    log.close()
    log = SessionLog.open(tmp_path / "logs", session_id="session", cwd=tmp_path)
    try:
        assert log.bind_tool_interface_version(2) == 1
        assert not log.events
    finally:
        log.close()


def test_unknown_tool_protocol_is_rejected_without_recovery(tmp_path):
    log = SessionLog.create(tmp_path / "logs", session_id="session", cwd=tmp_path)
    log.append("tools/contract", {"version": 999})
    log.flush()
    log.close()
    with pytest.raises(SessionLogIncompatible, match="tool interface"):
        SessionLog.open(tmp_path / "logs", session_id="session", cwd=tmp_path, recover=True)


def test_runtime_catalog_does_not_change_browser_previous_turn(tmp_path):
    messages = [Message(role="user", content="Look at the current browser tab"),
                Message(role="user", source="runtime", content="Skill catalog"),
                Message(role="tool", name="browser_get_tabs", tool_call_id="tabs", content="Success"),
                Message(role="assistant", content="Here is the current tab."),
                Message(role="user", content="Continue"),
                Message(role="user", source="runtime", content="New catalog")]
    # Use a real gated browser-context name from the policy.
    from box_agent.tools.browser_intent import _BROWSER_CONTEXT_TOOLS
    messages[2].name = sorted(_BROWSER_CONTEXT_TOOLS)[0]
    assert has_recent_successful_browser_context(messages)


def test_write_tool_root_changes_do_not_mutate_its_schema(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))
    before = tool.to_schema()
    tool.relative_root_dir = tmp_path / "other"
    assert tool.to_schema() == before
