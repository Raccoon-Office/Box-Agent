"""
Session integration tests - Testing multi-turn conversations and session management
"""

import base64
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from box_agent import LLMClient
from box_agent.agent import Agent
from box_agent.schema import LLMResponse, Message
from box_agent.tools.bash_tool import BashTool
from box_agent.tools.file_tools import ReadTool, WriteTool


@pytest.fixture
def mock_llm_client():
    """Create mock LLM client"""
    client = MagicMock(spec=LLMClient)
    return client


@pytest.fixture
def temp_workspace():
    """Create temporary workspace directory"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def test_multi_turn_conversation(mock_llm_client, temp_workspace):
    """Test multi-turn conversation and context sharing"""
    # Prepare test data
    system_prompt = "You are an intelligent assistant"
    tools = [
        ReadTool(workspace_dir=temp_workspace),
        WriteTool(workspace_dir=temp_workspace),
    ]

    # Create agent
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt=system_prompt,
        tools=tools,
        workspace_dir=temp_workspace,
    )

    # Verify initial state
    assert len(agent.messages) == 1  # Only system prompt
    assert agent.messages[0].role == "system"
    # Agent automatically adds workspace info to system prompt
    assert system_prompt in agent.messages[0].content
    assert "Current Workspace" in agent.messages[0].content
    assert "session workspace and default working root" in agent.messages[0].content
    assert (
        "does not by itself define every path the runtime may allow"
        in agent.messages[0].content
    )
    assert "filesystem safety boundary" not in agent.messages[0].content
    assert "Relative tool paths resolve from each tool's active" in agent.messages[0].content
    assert "All relative paths will be resolved relative to this directory" not in agent.messages[0].content

    # Add first user message
    agent.add_user_message("Hello")
    assert len(agent.messages) == 2
    assert agent.messages[1].role == "user"
    assert agent.messages[1].content == "Hello"

    # Add second user message
    agent.add_user_message("Help me create a file")
    assert len(agent.messages) == 3
    assert agent.messages[2].role == "user"

    # Verify all messages are retained in history
    user_messages = [m for m in agent.messages if m.role == "user"]
    assert len(user_messages) == 2
    assert user_messages[0].content == "Hello"
    assert user_messages[1].content == "Help me create a file"


def test_user_source_binding_accumulates_and_updates_bash_env(
    mock_llm_client,
    temp_workspace,
):
    bash = BashTool(workspace_dir=temp_workspace)
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[bash],
        workspace_dir=temp_workspace,
    )

    assert agent.bind_user_source_text("原始事实 A") == "原始事实 A"
    assert agent.bind_user_source_text("补充事实 B") == "原始事实 A\n\n补充事实 B"

    source_text = base64.b64decode(
        bash._subprocess_env["BOX_AGENT_SOURCE_TEXT_B64"]
    ).decode("utf-8")
    assert source_text == "原始事实 A\n\n补充事实 B"


def test_session_history_management(mock_llm_client, temp_workspace):
    """Test session history management"""
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt="System prompt",
        tools=[],
        workspace_dir=temp_workspace,
    )

    # Add multiple messages
    for i in range(5):
        agent.add_user_message(f"Message {i}")

    # Verify message count (1 system + 5 user)
    assert len(agent.messages) == 6

    # Clear history (keep system prompt)
    agent.messages = [agent.messages[0]]

    # Verify only system prompt remains after clearing
    assert len(agent.messages) == 1
    assert agent.messages[0].role == "system"


def test_active_goal_is_injected_into_user_turn(mock_llm_client, temp_workspace):
    """Test that an active goal is included with subsequent user messages."""
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[],
        workspace_dir=temp_workspace,
    )

    goal = agent.set_goal("Make the focused test suite pass")
    assert goal.status == "active"

    agent.add_user_message("Run the next check")

    assert len(agent.messages) == 2
    assert agent.messages[1].role == "user"
    assert "## Active Goal" in agent.messages[1].content
    assert "Make the focused test suite pass" in agent.messages[1].content
    assert "## Latest User Message" in agent.messages[1].content
    assert "goal_write" in agent.messages[1].content
    assert "/goal complete" not in agent.messages[1].content
    assert "Run the next check" in agent.messages[1].content


@pytest.mark.asyncio
async def test_goal_write_tool_marks_active_goal_complete(mock_llm_client, temp_workspace):
    """Test that the model-callable goal tool can complete an active goal."""
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[],
        workspace_dir=temp_workspace,
    )

    agent.set_goal("Finish without asking the user for a slash command")
    result = await agent.tools["goal_write"].execute(
        action="complete",
        evidence=["uv run pytest tests/test_session_integration.py -q passed"],
        completed_by="model",
    )

    assert result.success is True
    assert agent.goal is not None
    assert agent.goal.status == "complete"
    assert agent.goal.evidence == ["uv run pytest tests/test_session_integration.py -q passed"]
    assert agent.goal.completed_by == "model"
    assert result.raw_output is not None
    assert result.raw_output["type"] == "goal_snapshot"
    assert result.raw_output["action"] == "complete"
    assert result.raw_output["goal"]["status"] == "complete"
    assert result.raw_output["goal"]["evidence"] == [
        "uv run pytest tests/test_session_integration.py -q passed"
    ]


@pytest.mark.asyncio
async def test_goal_write_complete_requires_evidence(mock_llm_client, temp_workspace):
    """Model-driven goal completion must include concrete evidence."""
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[],
        workspace_dir=temp_workspace,
    )

    agent.set_goal("Finish only with evidence")
    result = await agent.tools["goal_write"].execute(action="complete")

    assert result.success is False
    assert "evidence" in (result.error or "")
    assert agent.goal is not None
    assert agent.goal.status == "active"


@pytest.mark.asyncio
async def test_goal_write_records_progress_and_blocked_reason(mock_llm_client, temp_workspace):
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[],
        workspace_dir=temp_workspace,
    )

    agent.set_goal("Coordinate external provider validation")
    progress = await agent.tools["goal_write"].execute(
        action="progress",
        progress=["Added CLI persistence"],
        evidence=["tests/test_cli_config.py updated"],
    )
    blocked = await agent.tools["goal_write"].execute(
        action="block",
        blocked_reason="Waiting for provider test credentials",
    )

    assert progress.success is True
    assert blocked.success is True
    assert agent.goal is not None
    assert agent.goal.status == "blocked"
    assert agent.goal.progress == ["Added CLI persistence"]
    assert agent.goal.evidence == ["tests/test_cli_config.py updated"]
    assert agent.goal.blocked_reason == "Waiting for provider test credentials"


def test_paused_goal_is_not_injected_into_user_turn(mock_llm_client, temp_workspace):
    """Test that pausing a goal stops prompt injection without deleting state."""
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[],
        workspace_dir=temp_workspace,
    )

    agent.set_goal("Keep investigating until verified")
    paused = agent.pause_goal()
    assert paused is not None
    assert paused.status == "paused"

    agent.add_user_message("Answer a side question")

    assert agent.goal is paused
    assert agent.messages[1].content == "Answer a side question"


def test_get_history(mock_llm_client, temp_workspace):
    """Test getting session history"""
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[],
        workspace_dir=temp_workspace,
    )

    # Add message
    agent.add_user_message("Test message")

    # Get history
    history = agent.get_history()

    # Verify history is a copy (doesn't affect original messages)
    assert len(history) == len(agent.messages)
    assert history is not agent.messages

    # Modifying copy should not affect original messages
    history.append(Message(role="user", content="New message"))
    assert len(agent.messages) == 2  # Original messages unchanged
    assert len(history) == 3  # Copy changed


def test_message_statistics(mock_llm_client, temp_workspace):
    """Test message statistics functionality"""
    agent = Agent(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[],
        workspace_dir=temp_workspace,
    )

    # Add different types of messages
    agent.add_user_message("User message 1")
    agent.messages.append(Message(role="assistant", content="Assistant response 1"))
    agent.add_user_message("User message 2")
    agent.messages.append(
        Message(
            role="tool", content="Tool result", tool_call_id="123", name="test_tool"
        )
    )

    # Count different types of messages
    user_msgs = sum(1 for m in agent.messages if m.role == "user")
    assistant_msgs = sum(1 for m in agent.messages if m.role == "assistant")
    tool_msgs = sum(1 for m in agent.messages if m.role == "tool")

    assert user_msgs == 2
    assert assistant_msgs == 1
    assert tool_msgs == 1
    assert len(agent.messages) == 5  # 1 system + 2 user + 1 assistant + 1 tool
