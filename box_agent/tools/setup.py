"""Tool initialization helpers shared by CLI and ACP entry-points.

Extracted from ``cli.py`` so that ``box_agent.acp`` can assemble the
tool belt without pulling in ``prompt_toolkit`` and the rest of the
interactive-CLI surface.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from box_agent.config import Config
from box_agent.tools.base import Tool
from box_agent.tools.bash_tool import BashKillTool, BashOutputTool, BashTool
from box_agent.tools.file_tools import EditTool, ReadTool, WriteTool
from box_agent.tools.jupyter_tool import JupyterSandboxTool, SandboxEnvironment, SandboxStatusTool
from box_agent.tools.mcp_loader import load_mcp_tools_async, set_mcp_timeout_config
from box_agent.tools.memory_tool import MemoryReadTool, MemoryWriteTool
from box_agent.tools.skill_tool import create_skill_tools
from box_agent.tools.sub_agent_tool import SubAgentTool
from box_agent.tools.todo_tool import TodoReadTool, TodoStore, TodoWriteTool
from box_agent.tools.web_search_tool import WebSearchTool


# Minimal color constants used in status messages.
# The full ``Colors`` class lives in ``cli.py``; we only need a small subset.
class Colors:
    """Terminal color subset for tool-setup status messages."""

    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BRIGHT_CYAN = "\033[96m"
    DIM = "\033[2m"
    RESET = "\033[0m"


async def initialize_base_tools(config: Config, output=None, memory_manager=None):
    """Initialize base tools (independent of workspace)

    These tools are loaded from package configuration and don't depend on workspace.
    Note: File tools are now workspace-dependent and initialized in add_workspace_tools()

    Args:
        config: Configuration object
        output: Callable for status messages (default: print). Pass a stderr
                writer when stdout must stay clean (e.g. ACP mode).
        memory_manager: Optional MemoryManager instance for memory tools.

    Returns:
        Tuple of (list of tools, skill loader if skills enabled)
    """
    _out = output or print

    tools = []
    skill_loader = None

    # 0. Memory tools (cross-session, workspace-independent)
    if memory_manager is not None:
        tools.append(MemoryReadTool(memory_manager))
        tools.append(MemoryWriteTool(memory_manager))
        _out(f"{Colors.GREEN}✅ Loaded memory tools (memory_read, memory_write){Colors.RESET}")

    # 1. Bash auxiliary tools (output monitoring and kill)
    # Note: BashTool itself is created in add_workspace_tools() with workspace_dir as cwd
    if config.tools.enable_bash:
        bash_output_tool = BashOutputTool()
        tools.append(bash_output_tool)
        _out(f"{Colors.GREEN}✅ Loaded Bash Output tool{Colors.RESET}")

        bash_kill_tool = BashKillTool()
        tools.append(bash_kill_tool)
        _out(f"{Colors.GREEN}✅ Loaded Bash Kill tool{Colors.RESET}")

    # 2. Web search tool (fallback when no MCP search service)
    if config.tools.enable_web_search:
        tools.append(WebSearchTool())
        _out(f"{Colors.GREEN}✅ Loaded Web Search tool (web_search){Colors.RESET}")

    # 3. Claude Skills (loaded from package directory)
    if config.tools.enable_skills:
        _out(f"{Colors.BRIGHT_CYAN}Loading Claude Skills...{Colors.RESET}")
        try:
            # Resolve skills directory with priority search
            # Expand ~ to user home directory for portability
            skills_path = Path(config.tools.skills_dir).expanduser()
            if skills_path.is_absolute():
                skills_dir = str(skills_path)
            else:
                # Search in priority order:
                # 1. Current directory (dev mode: ./skills or ./box_agent/skills)
                # 2. Package directory (installed: site-packages/box_agent/skills)
                search_paths = [
                    skills_path,  # ./skills for backward compatibility
                    Path("box_agent") / skills_path,  # ./box_agent/skills
                    Config.get_package_dir() / skills_path,  # site-packages/box_agent/skills
                ]

                # Find first existing path
                skills_dir = str(skills_path)  # default
                for path in search_paths:
                    if path.exists():
                        skills_dir = str(path.resolve())
                        break

            skill_tools, skill_loader = create_skill_tools(skills_dir)
            if skill_tools:
                tools.extend(skill_tools)
                _out(f"{Colors.GREEN}✅ Loaded Skill tool (get_skill){Colors.RESET}")
            else:
                _out(f"{Colors.YELLOW}⚠️  No available Skills found{Colors.RESET}")
        except Exception as e:
            _out(f"{Colors.YELLOW}⚠️  Failed to load Skills: {e}{Colors.RESET}")

    # 4. MCP tools (loaded with priority search)
    if config.tools.enable_mcp:
        _out(f"{Colors.BRIGHT_CYAN}Loading MCP tools...{Colors.RESET}")
        try:
            # Apply MCP timeout configuration from config.yaml
            mcp_config = config.tools.mcp
            set_mcp_timeout_config(
                connect_timeout=mcp_config.connect_timeout,
                execute_timeout=mcp_config.execute_timeout,
                sse_read_timeout=mcp_config.sse_read_timeout,
            )
            _out(
                f"{Colors.DIM}  MCP timeouts: connect={mcp_config.connect_timeout}s, "
                f"execute={mcp_config.execute_timeout}s, sse_read={mcp_config.sse_read_timeout}s{Colors.RESET}"
            )

            # Use priority search for mcp.json
            mcp_config_path = Config.find_config_file(config.tools.mcp_config_path)
            if mcp_config_path:
                mcp_tools = await load_mcp_tools_async(str(mcp_config_path))
                if mcp_tools:
                    tools.extend(mcp_tools)
                    _out(f"{Colors.GREEN}✅ Loaded {len(mcp_tools)} MCP tools (from: {mcp_config_path}){Colors.RESET}")
                else:
                    _out(f"{Colors.YELLOW}⚠️  No available MCP tools found{Colors.RESET}")
            else:
                _out(f"{Colors.YELLOW}⚠️  MCP config file not found: {config.tools.mcp_config_path}{Colors.RESET}")
        except Exception as e:
            _out(f"{Colors.YELLOW}⚠️  Failed to load MCP tools: {e}{Colors.RESET}")

    _out("")  # Empty line separator
    return tools, skill_loader


def add_workspace_tools(tools: List[Tool], config: Config, workspace_dir: Path, sandbox_mode: bool = False,
                        allow_full_access: bool = True, non_interactive: bool = False, output=None,
                        llm=None):
    """Add workspace-dependent tools

    These tools need to know the workspace directory.

    Args:
        tools: Existing tools list to add to
        config: Configuration object
        workspace_dir: Workspace directory path
        sandbox_mode: If True, enable Jupyter sandbox mode
        allow_full_access: If True, tools can access full system; if False, restricted to workspace
        non_interactive: If True, dangerous commands are rejected without prompting
        output: Callable for status messages (default: print)
        llm: LLM client instance (needed for sub_agent tool)
    """
    _out = output or print
    # Ensure workspace directory exists
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Bash tool - needs workspace as cwd for command execution
    if config.tools.enable_bash:
        sandbox_venv_path = None
        if sandbox_mode and not getattr(sys, "frozen", False):
            sandbox_venv_path = str(SandboxEnvironment().venv_dir)
        bash_tool = BashTool(
            workspace_dir=str(workspace_dir),
            allow_full_access=allow_full_access,
            non_interactive=non_interactive,
            sandbox_venv_path=sandbox_venv_path,
        )
        tools.append(bash_tool)
        _out(f"{Colors.GREEN}✅ Loaded Bash tool (cwd: {workspace_dir}){Colors.RESET}")

    # File tools - need workspace to resolve relative paths
    if config.tools.enable_file_tools:
        tools.extend(
            [
                ReadTool(workspace_dir=str(workspace_dir), allow_full_access=allow_full_access),
                WriteTool(workspace_dir=str(workspace_dir), allow_full_access=allow_full_access),
                EditTool(workspace_dir=str(workspace_dir), allow_full_access=allow_full_access),
            ]
        )
        _out(f"{Colors.GREEN}✅ Loaded file operation tools (workspace: {workspace_dir}){Colors.RESET}")

    # Todo tool - task tracking for multi-step workflows
    if config.tools.enable_todo:
        store = TodoStore()
        tools.append(TodoWriteTool(store))
        tools.append(TodoReadTool(store))
        _out(f"{Colors.GREEN}✅ Loaded todo tools (todo_write, todo_read){Colors.RESET}")

    # Jupyter sandbox tool - Python code execution environment
    if sandbox_mode:
        sandbox_tool = JupyterSandboxTool(workspace_dir=str(workspace_dir))
        tools.append(sandbox_tool)
        # Also add sandbox status tool
        status_tool = SandboxStatusTool()
        SandboxStatusTool.set_sandbox_tool(sandbox_tool)
        tools.append(status_tool)
        _out(f"{Colors.GREEN}✅ Loaded Jupyter sandbox tool (execute_code){Colors.RESET}")
        _out(f"{Colors.GREEN}✅ Loaded sandbox status tool{Colors.RESET}")

    # Sub-agent tool — must be registered last so it can reference all other tools
    if config.tools.enable_sub_agent and llm is not None:
        parent_tools = {t.name: t for t in tools}
        sub_agent_tool = SubAgentTool(
            llm=llm,
            parent_tools=parent_tools,
            workspace_dir=str(workspace_dir),
        )
        tools.append(sub_agent_tool)
        _out(f"{Colors.GREEN}✅ Loaded sub-agent tool (sub_agent){Colors.RESET}")
