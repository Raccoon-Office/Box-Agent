"""Integration tests for the Box ACP adapter."""

from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, ToolCall
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

    async def generate_stream(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type="thinking", delta="calling echo")
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="tool1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "ping"}),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")


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
    agent = BoxACPAgent(conn, config, DummyLLM(), [EchoTool()], "system")
    return agent, conn


@pytest.mark.asyncio
async def test_acp_turn_executes_tool(acp_agent):
    agent, conn = acp_agent
    # Explicit session_mode skips auto-classification so DummyLLM's first
    # response is consumed by the main agent loop as designed.
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    prompt = SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "hello"}])
    response = await agent.prompt(prompt)
    assert response.stopReason == "end_turn"
    assert any("tool:ping" in str(update) for update in conn.updates)
    await agent.cancel(SimpleNamespace(sessionId=session.sessionId))
    assert agent._sessions[session.sessionId].cancelled


@pytest.mark.asyncio
async def test_acp_invalid_session(acp_agent):
    """Auto-creates session when sessionId is not found (compatibility)."""
    agent, _ = acp_agent
    # Provide an explicit mode via the auto-created session by monkeypatching
    # the default newSession call path — not available here, so we instead
    # ensure the DummyLLM is resilient: the classifier's first response is
    # the tool-call one, which parses to no mode → general. The main loop
    # then sees a fresh LLM (second) call returning "done", so there's no
    # tool invocation to assert on. We only check stopReason.
    prompt = SimpleNamespace(sessionId="missing", prompt=[{"text": "?"}])
    response = await agent.prompt(prompt)
    assert response.stopReason == "end_turn"
