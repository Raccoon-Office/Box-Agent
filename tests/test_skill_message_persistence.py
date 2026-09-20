"""Explicit Skill activation becomes durable conversation history."""

import json

import pytest

from box_agent.agent import Agent
from box_agent.session_log import SessionLog
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_tool import GetSkillTool
from tests.test_skill_entry_boundaries import CapturingProvider, loader_at


def _skill_messages(messages):
    return [message for message in messages if "METHOD_BODY" in str(message.content)]


def _agent(tmp_path, provider, log, loader):
    return Agent(
        llm_client=provider,
        system_prompt="BASE",
        tools=[GetSkillTool(loader)],
        skill_runtime=SkillRuntime(loader),
        session_log=log,
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        max_steps=1,
    )


class DurableSkillProvider(CapturingProvider):
    def __init__(self, log):
        super().__init__()
        self.log = log
        self.durable_user_messages = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        events = [json.loads(line) for line in self.log.path.read_bytes().splitlines()[1:]]
        self.durable_user_messages.append([
            event["data"] for event in events if event["type"] == "user/message"
        ])
        async for event in super().generate_stream(messages, tools=tools, **kwargs):
            yield event


@pytest.mark.asyncio
async def test_selected_skill_is_durable_runtime_user_message_after_request(tmp_path):
    loader = loader_at(tmp_path / "skills")
    log = SessionLog.create(tmp_path / "sessions", session_id="slash-skill", cwd=tmp_path)
    provider = DurableSkillProvider(log)
    try:
        agent = _agent(tmp_path, provider, log, loader)
        agent.skill_runtime.select(["demo"])
        agent.add_user_message("Use the selected method")
        _ = [event async for event in agent.run_events()]

        assert len(provider.requests) == 1
        skill_messages = _skill_messages(agent.messages)
        assert len(skill_messages) == 1
        skill_message = skill_messages[0]
        assert (skill_message.role, skill_message.source) == ("user", "runtime")
        request_index = next(index for index, message in enumerate(agent.messages)
                             if message.content == "Use the selected method")
        assert agent.messages[request_index + 1] is skill_message
        assert _skill_messages(provider.requests[0])[0].content == skill_message.content

        durable_users = provider.durable_user_messages[0]
        assert durable_users[0]["content"] == "Use the selected method"
        assert durable_users[0]["source"] == "user"
        assert durable_users[1]["content"] == skill_message.content
        assert durable_users[1]["source"] == "runtime"
        replayed = log.replay().messages
        assert [(message.role, message.source, message.content) for message in replayed[:2]] == [
            ("user", "user", "Use the selected method"),
            ("user", "runtime", skill_message.content),
        ]

        agent.add_user_message("Continue")
        _ = [event async for event in agent.run_events()]
        assert len(_skill_messages(agent.messages)) == 1
        assert len(_skill_messages(provider.requests[-1])) == 1
        assert len(_skill_messages(log.replay().messages)) == 1
    finally:
        log.close()


@pytest.mark.asyncio
async def test_reopened_session_reuses_durable_skill_message_without_duplicate(tmp_path):
    loader = loader_at(tmp_path / "skills")
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="resume-slash-skill", cwd=tmp_path)
    try:
        agent = _agent(tmp_path, CapturingProvider(), log, loader)
        agent.skill_runtime.select(["demo"])
        agent.add_user_message("Use the selected method")
        _ = [event async for event in agent.run_events()]
        original = _skill_messages(agent.messages)
        assert len(original) == 1
        original_content = original[0].content
    finally:
        log.close()

    reopened = SessionLog.open(root, session_id="resume-slash-skill", cwd=tmp_path)
    provider = CapturingProvider()
    try:
        agent = _agent(tmp_path, provider, reopened, loader)
        restored = _skill_messages(agent.messages)
        assert len(restored) == 1
        assert (restored[0].role, restored[0].source, restored[0].content) == (
            "user", "runtime", original_content,
        )
        agent.add_user_message("Continue after restart")
        _ = [event async for event in agent.run_events()]
        assert len(_skill_messages(provider.requests[0])) == 1
        assert len(_skill_messages(reopened.replay().messages)) == 1
        assert _skill_messages(provider.requests[0])[0].content == original_content
    finally:
        reopened.close()
