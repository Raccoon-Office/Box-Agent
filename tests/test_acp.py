"""Integration tests for the MiniMax ACP adapter."""

from types import SimpleNamespace

import pytest

from box_agent.acp import MiniMaxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, LLMResponse, ToolCall
from box_agent.tools.base import Tool, ToolResult


class DummyConn:
    def __init__(self):
        self.updates = []

    async def sessionUpdate(self, payload):
        self.updates.append(payload)


class DummyLLM:
    def __init__(self):
        self.calls = 0

    async def generate(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                thinking="calling echo",
                tool_calls=[
                    ToolCall(
                        id="tool1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "ping"}),
                    )
                ],
                finish_reason="tool",
            )
        return LLMResponse(content="done", thinking=None, tool_calls=None, finish_reason="stop")


class EchoTool(Tool):
    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echo helper"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str):
        return ToolResult(success=True, content=f"tool:{text}")


@pytest.fixture
def acp_agent(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = MiniMaxACPAgent(conn, config, DummyLLM(), [EchoTool()], "system")
    return agent, conn


@pytest.mark.asyncio
async def test_acp_turn_executes_tool(acp_agent):
    agent, conn = acp_agent
    session = await agent.newSession(SimpleNamespace(cwd=None))
    prompt = SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "hello"}])
    response = await agent.prompt(prompt)
    assert response.stopReason == "end_turn"
    assert any("tool:ping" in str(update) for update in conn.updates)
    await agent.cancel(SimpleNamespace(sessionId=session.sessionId))
    assert agent._sessions[session.sessionId].cancelled


@pytest.mark.asyncio
async def test_acp_invalid_session(acp_agent):
    agent, _ = acp_agent
    prompt = SimpleNamespace(sessionId="missing", prompt=[{"text": "?"}])
    response = await agent.prompt(prompt)
    assert response.stopReason == "refusal"
