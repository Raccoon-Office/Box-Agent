"""Integration test cases - Full agent demos."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from box_agent import LLMClient
from box_agent.agent import Agent
from box_agent.config import Config
from box_agent.tools import BashTool, EditTool, ReadTool, WriteTool
from box_agent.tools.mcp_loader import load_mcp_tools_async


@pytest.mark.asyncio
async def test_basic_agent_usage():
    """Test basic agent usage with file creation task.

    This is the integration test for basic agent functionality,
    converted from example.py.
    """
    print("\n" + "=" * 80)
    print("Integration Test: Basic Agent Usage")
    print("=" * 80)

    # Load configuration
    config_path = Path("box_agent/config/config.yaml")
    if not config_path.exists():
        pytest.skip("config.yaml not found")

    config = Config.from_yaml(config_path)

    # Check API key
    if not config.llm.api_key or config.llm.api_key == "YOUR_API_KEY_HERE":
        pytest.skip("API key not configured")

    # Use temporary workspace
    with tempfile.TemporaryDirectory() as workspace_dir:
        # Load system prompt (Agent will auto-inject workspace info)
        system_prompt_path = Path("box_agent/config/system_prompt.md")
        if system_prompt_path.exists():
            system_prompt = system_prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant."

        # Initialize LLM client
        llm_client = LLMClient(
            api_key=config.llm.api_key,
            api_base=config.llm.api_base,
            model=config.llm.model,
        )

        # Initialize basic tools
        tools = [
            ReadTool(workspace_dir=workspace_dir),
            WriteTool(workspace_dir=workspace_dir),
            EditTool(workspace_dir=workspace_dir),
            BashTool(),
        ]

        # Load MCP tools (optional) - with timeout protection
        try:
            # MCP tools are disabled by default to prevent test hangs
            # Enable specific MCP servers in mcp.json if needed
            mcp_tools = await load_mcp_tools_async(
                config_path="box_agent/config/mcp.json"
            )
            if mcp_tools:
                print(f"✓ Loaded {len(mcp_tools)} MCP tools")
                tools.extend(mcp_tools)
            else:
                print("⚠️  No MCP tools configured (mcp.json is empty)")
        except Exception as e:
            print(f"⚠️  MCP tools not loaded: {e}")

        # Create agent
        agent = Agent(
            llm_client=llm_client,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=config.agent.max_steps,
            workspace_dir=workspace_dir,
        )

        # Task: Create a Python file with hello world
        task = """
        Create a Python file named hello.py in the workspace that prints "Hello, Box Agent!".
        Then execute it to verify it works.
        """

        print(f"\nTask: {task}")
        print("\n" + "=" * 80 + "\n")

        agent.add_user_message(task)
        result = await agent.run()

        print("\n" + "=" * 80)
        print(f"Result: {result}")
        print("=" * 80)

        # Verify the file was created or task completed
        hello_file = Path(workspace_dir) / "hello.py"
        assert hello_file.exists() or "complete" in result.lower(), (
            "Agent should create the file or indicate completion"
        )

        print("\n✅ Basic agent usage test passed")


async def main():
    """Run all integration tests."""
    print("=" * 80)
    print("Running Integration Tests")
    print("=" * 80)
    print("\nNote: These tests require a valid API key in config.yaml")
    print("These tests will actually call the LLM API and may take some time.\n")

    try:
        await test_basic_agent_usage()
    except Exception as e:
        print(f"❌ Basic usage test failed: {e}")

    print("\n" + "=" * 80)
    print("Integration tests completed!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
