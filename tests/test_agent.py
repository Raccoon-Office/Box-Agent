"""Test cases for Agent."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from box_agent import LLMClient
from box_agent.agent import Agent
from box_agent.config import Config
from box_agent.tools import BashTool, EditTool, ReadTool, WriteTool

_CONFIG_PATH = Path("box_agent/config/config.yaml")
pytestmark = pytest.mark.skipif(
    not _CONFIG_PATH.exists(),
    reason="box_agent/config/config.yaml missing — integration tests skipped",
)


@pytest.mark.asyncio
async def test_agent_simple_task():
    """Test agent with a simple file creation task."""
    print("\n=== Testing Agent with Simple File Task ===")

    # Load config
    config_path = Path("box_agent/config/config.yaml")
    config = Config.from_yaml(config_path.resolve())

    # Create temp workspace
    with tempfile.TemporaryDirectory() as workspace_dir:
        print(f"Using workspace: {workspace_dir}")

        # Load system prompt (Agent will auto-inject workspace info)
        system_prompt_path = Path("box_agent/config/system_prompt.md")
        if system_prompt_path.exists():
            system_prompt = system_prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant that can use tools."

        # Initialize LLM client
        llm_client = LLMClient(
            api_key=config.llm.api_key,
            provider=config.llm.provider,
            api_base=config.llm.api_base,
            model=config.llm.model,
            auth_file=config.llm.auth_file,
            timeout=config.llm.timeout,
        )

        # Initialize tools
        tools = [
            ReadTool(workspace_dir=workspace_dir),
            WriteTool(workspace_dir=workspace_dir),
            EditTool(workspace_dir=workspace_dir),
            BashTool(workspace_dir=workspace_dir),
        ]

        # Create agent
        agent = Agent(
            llm_client=llm_client,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=10,  # Limit steps for testing
            workspace_dir=workspace_dir,
        )

        # Task: Create a simple text file
        task = "Create a file named 'test.txt' with the content 'Hello from Agent!'"
        print(f"\nTask: {task}\n")

        agent.add_user_message(task)

        result = await agent.run()

        print(f"\n{'=' * 80}")
        print(f"Agent Result: {result}")
        print("=" * 80)

        test_file = Path(workspace_dir) / "test.txt"
        assert test_file.exists(), "Agent should create test.txt"
        assert "Hello from Agent!" in test_file.read_text()


@pytest.mark.asyncio
async def test_agent_bash_task():
    """Test agent with a bash command task."""
    print("\n=== Testing Agent with Bash Task ===")

    # Load config
    config_path = Path("box_agent/config/config.yaml")
    config = Config.from_yaml(config_path.resolve())

    # Create temp workspace
    with tempfile.TemporaryDirectory() as workspace_dir:
        print(f"Using workspace: {workspace_dir}")

        # Load system prompt (Agent will auto-inject workspace info)
        system_prompt_path = Path("box_agent/config/system_prompt.md")
        if system_prompt_path.exists():
            system_prompt = system_prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant that can use tools."

        # Initialize LLM client
        llm_client = LLMClient(
            api_key=config.llm.api_key,
            provider=config.llm.provider,
            api_base=config.llm.api_base,
            model=config.llm.model,
            auth_file=config.llm.auth_file,
            timeout=config.llm.timeout,
        )

        # Initialize tools
        tools = [
            ReadTool(workspace_dir=workspace_dir),
            WriteTool(workspace_dir=workspace_dir),
            BashTool(workspace_dir=workspace_dir),
        ]

        # Create agent
        agent = Agent(
            llm_client=llm_client,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=10,
            workspace_dir=workspace_dir,
        )

        # Task: List files using bash
        task = "Use bash to list all files in the current directory and tell me what you find."
        print(f"\nTask: {task}\n")

        agent.add_user_message(task)

        result = await agent.run()

        print(f"\n{'=' * 80}")
        print(f"Agent Result: {result}")
        print("=" * 80)

        assert result


async def main():
    """Run all agent tests."""
    print("=" * 80)
    print("Running Agent Integration Tests")
    print("=" * 80)
    print("\nNote: These tests require a valid API key in config.yaml")
    print("These tests will actually call the LLM API and may take some time.\n")

    # Test simple file task
    result1 = await test_agent_simple_task()

    # Test bash task
    result2 = await test_agent_bash_task()

    print("\n" + "=" * 80)
    if result1 and result2:
        print("All Agent tests passed! ✅")
    else:
        print("Some Agent tests failed. Check the output above.")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
