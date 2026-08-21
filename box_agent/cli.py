"""
Box Agent - Interactive Runtime Example

Usage:
    box-agent [--workspace DIR] [--task TASK]

Examples:
    box-agent                              # Use current directory as workspace (interactive mode)
    box-agent --workspace /path/to/dir     # Use specific workspace directory (interactive mode)
    box-agent --task "create a file"       # Execute a task non-interactively
"""

import argparse
import asyncio
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
import yaml

from box_agent import LLMClient, __version__
from box_agent.artifacts import ensure_output_dir
from box_agent.agent import (
    Agent,
    GoalState,
    goal_autopilot_prompt,
    goal_autopilot_progress_signature,
    goal_payload,
    goal_state_from_payload,
    should_continue_goal_autopilot,
)
from box_agent.config import Config
from box_agent.completion import build_auto_completion_gate
from box_agent.events import StopReason
from box_agent.schema import LLMProvider, Message
from box_agent.tools.base import Tool
from box_agent.tools.jupyter_tool import JupyterSandboxTool, SandboxStatusTool
from box_agent.tools.mcp_loader import (
    cleanup_mcp_connections,
    get_all_mcp_tools,
    reconnect_auth_failed_mcp_servers_if_token_changed,
)
from box_agent.tools.skill_preload import (
    build_auto_loaded_skills_prompt,
    turn_preload_skill_names,
)
from box_agent.tools.setup import (
    add_workspace_tools,
    await_mcp_tools,
    build_file_delivery_prompt,
    build_image_generation_prompt,
    build_sandbox_info_prompt,
    initialize_base_tools,
    register_mcp_tools,
)
from box_agent.tools.runtime import (
    DEFAULT_NODE_VERSION,
    NodeRuntimeInstallError,
    NodeRuntimeManager,
    build_skill_runtime_context,
    build_skill_runtime_prompt,
    resolve_cli_shell_python,
)
from box_agent.tools.skill_execution_env import build_skill_execution_env
from box_agent.tools.skill_scratch import cleanup_skill_scratch_dir
from box_agent.utils import calculate_display_width
from box_agent.acp.project_context import build_project_startup_context_prompt
from box_agent.workspace_registry import WorkspaceRegistry, WorkspaceRegistryError
from box_agent.workflows import (
    CONTROLLED_PRESENTATION_WORKFLOW_KIND,
    build_external_skill_completion_gate,
    resolve_explicit_skill_invocation,
)


def run_setup_wizard(config_path: Path) -> bool:
    """Interactive first-run setup wizard.

    Prompts the user to choose a provider and enter API credentials,
    then writes the result to config_path.

    Returns:
        True if setup completed successfully, False if cancelled.
    """
    print(f"\n{Colors.BOLD}{Colors.BRIGHT_CYAN}╔{'═' * 48}╗{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BRIGHT_CYAN}║  🚀 Box Agent - First Run Setup               ║{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BRIGHT_CYAN}╚{'═' * 48}╝{Colors.RESET}")
    print()

    # Step 1: Choose provider
    print(f"{Colors.BRIGHT_YELLOW}[1/4] Choose LLM provider:{Colors.RESET}")
    print(f"  {Colors.BRIGHT_GREEN}1){Colors.RESET} Anthropic-compatible (Anthropic, etc.)")
    print(f"  {Colors.BRIGHT_GREEN}2){Colors.RESET} OpenAI-compatible (OpenAI, DeepSeek, SiliconFlow, etc.)")
    print()

    while True:
        try:
            choice = input(f"{Colors.BRIGHT_CYAN}Enter choice (1/2): {Colors.RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{Colors.YELLOW}Setup cancelled.{Colors.RESET}\n")
            return False
        if choice in ("1", "2"):
            break
        print(f"{Colors.RED}  Please enter 1 or 2{Colors.RESET}")

    provider = "anthropic" if choice == "1" else "openai"

    # Step 2: API Base URL with sensible defaults
    if provider == "anthropic":
        default_base = "https://api.anthropic.com"
        examples = (
            f"  {Colors.DIM}Anthropic : https://api.anthropic.com{Colors.RESET}"
        )
        default_model = "claude-sonnet-4-20250514"
    else:
        default_base = "https://api.openai.com/v1"
        examples = (
            f"  {Colors.DIM}OpenAI      : https://api.openai.com/v1{Colors.RESET}\n"
            f"  {Colors.DIM}DeepSeek    : https://api.deepseek.com{Colors.RESET}\n"
            f"  {Colors.DIM}SiliconFlow : https://api.siliconflow.cn/v1{Colors.RESET}"
        )
        default_model = "gpt-4o"

    print(f"\n{Colors.BRIGHT_YELLOW}[2/4] API Base URL:{Colors.RESET}")
    print(examples)
    print()
    try:
        api_base = input(f"{Colors.BRIGHT_CYAN}API Base URL [{default_base}]: {Colors.RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        print(f"\n{Colors.YELLOW}Setup cancelled.{Colors.RESET}\n")
        return False
    if not api_base:
        api_base = default_base

    # Step 3: Model
    print(f"\n{Colors.BRIGHT_YELLOW}[3/4] Model:{Colors.RESET}")
    try:
        model = input(f"{Colors.BRIGHT_CYAN}Model [{default_model}]: {Colors.RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        print(f"\n{Colors.YELLOW}Setup cancelled.{Colors.RESET}\n")
        return False
    if not model:
        model = default_model

    # Step 4: API Key
    print(f"\n{Colors.BRIGHT_YELLOW}[4/4] API Key:{Colors.RESET}")
    try:
        api_key = input(f"{Colors.BRIGHT_CYAN}API Key: {Colors.RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        print(f"\n{Colors.YELLOW}Setup cancelled.{Colors.RESET}\n")
        return False
    if not api_key:
        print(f"{Colors.RED}  API key cannot be empty.{Colors.RESET}\n")
        return False

    # Write config
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    data["api_key"] = api_key
    data["api_base"] = api_base
    data["provider"] = provider
    data["model"] = model

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"\n{Colors.GREEN}✅ Configuration saved to: {config_path}{Colors.RESET}")
    print(f"{Colors.DIM}   provider: {provider}, api_base: {api_base}{Colors.RESET}")
    print()
    return True


# ANSI color codes
class Colors:
    """Terminal color definitions"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Foreground colors
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # Bright colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

    # Background colors
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"


MAIN_LLM_KEYS = {
    "api_key",
    "api_base",
    "model",
    "provider",
    "auth_file",
    "context_window",
    "max_output_tokens",
    "timeout",
}

AGENT_KEYS = {
    "max_steps",
    "workspace_dir",
    "max_parallel_tools",
    "goal_autopilot_enabled",
    "goal_autopilot_max_turns",
    "goal_autopilot_max_seconds",
    "goal_autopilot_no_progress_turns",
    "system_prompt_path",
    "analysis_prompt_path",
    "code_prompt_path",
    "enable_memory",
    "memory_dir",
    "enable_memory_extraction",
    "memory_extraction_cooldown",
    "memory_extraction_step_interval",
    "memory_maintainer_enabled",
    "memory_maintainer_interval_hours",
    "memory_decay_days",
    "memory_archive_days",
    "memory_dedup_jaccard",
    "memory_compaction_enabled",
    "memory_context_max_entries",
    "memory_context_max_tokens",
    "memory_conflict_resolution_enabled",
    "memory_conflict_cluster_threshold",
    "memory_conflict_max_clusters_per_run",
    "memory_promotion_proposal_enabled",
    "memory_promotion_hit_threshold",
    "memory_promotion_cooldown_days",
}

SECRET_KEY_NAMES = {"api_key"}


def _mask_secret(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def _is_secret_path(parts: list[str]) -> bool:
    return bool(parts) and parts[-1] in SECRET_KEY_NAMES


def _normalize_config_path(key: str) -> list[str]:
    """Map user-facing dotted keys to the YAML layout consumed by Config."""
    parts = [part for part in key.strip().split(".") if part]
    if not parts:
        raise ValueError("Config key cannot be empty")

    if parts[0] == "llm" and len(parts) >= 2:
        if parts[1] == "retry":
            return ["retry", *parts[2:]]
        if parts[1] in MAIN_LLM_KEYS:
            return [parts[1], *parts[2:]]
    if parts[0] == "agent" and len(parts) >= 2 and parts[1] in AGENT_KEYS:
        return [parts[1], *parts[2:]]
    return parts


def _normalize_display_path(key: str) -> list[str]:
    """Map user-facing dotted keys to the expanded config summary layout."""
    parts = [part for part in key.strip().split(".") if part]
    if not parts:
        raise ValueError("Config key cannot be empty")

    if parts[0] in MAIN_LLM_KEYS:
        return ["llm", *parts]
    if parts[0] == "retry":
        return ["llm", *parts]
    if parts[0] in AGENT_KEYS:
        return ["agent", *parts]
    return parts


def _get_nested(data: dict[str, Any], parts: list[str]) -> Any:
    current: Any = data
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise KeyError(".".join(parts))
        current = current[part]
    return current


def _set_nested(data: dict[str, Any], parts: list[str], value: Any) -> None:
    current: dict[str, Any] = data
    for part in parts[:-1]:
        child = current.get(part)
        if child is None:
            child = {}
            current[part] = child
        if not isinstance(child, dict):
            raise ValueError(f"Cannot set {'.'.join(parts)} because {part} is not a mapping")
        current = child
    current[parts[-1]] = value


def _parse_config_value(raw: str) -> Any:
    if raw == "":
        return ""
    return yaml.safe_load(raw)


def _load_config_yaml(config_path: Path) -> dict[str, Any]:
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a YAML mapping")
    return data


def _config_summary(config: Config, config_path: Path, show_secrets: bool = False) -> dict[str, Any]:
    api_key = config.llm.api_key if show_secrets else _mask_secret(config.llm.api_key)
    lite_api_key = config.lite_llm.api_key if show_secrets else _mask_secret(config.lite_llm.api_key)
    image_api_key = config.image_generation.api_key if show_secrets else _mask_secret(config.image_generation.api_key)
    return {
        "config_file": str(config_path),
        "llm": {
            "provider": config.llm.provider,
            "api_base": config.llm.api_base,
            "model": config.llm.model,
            "api_key": api_key,
            "auth_file": config.llm.auth_file,
            "context_window": config.llm.context_window,
            "max_output_tokens": config.llm.max_output_tokens,
            "timeout": config.llm.timeout,
            "retry": {
                "enabled": config.llm.retry.enabled,
                "max_retries": config.llm.retry.max_retries,
                "initial_delay": config.llm.retry.initial_delay,
                "max_delay": config.llm.retry.max_delay,
                "exponential_base": config.llm.retry.exponential_base,
            },
        },
        "lite_llm": {
            "enabled": getattr(config.lite_llm, "_present", False),
            "provider": config.lite_llm.provider,
            "api_base": config.lite_llm.api_base,
            "model": config.lite_llm.model,
            "api_key": lite_api_key,
            "auth_file": config.lite_llm.auth_file,
            "max_output_tokens": config.lite_llm.max_output_tokens,
            "timeout": config.lite_llm.timeout,
        },
        "image_generation": {
            "configured": bool(config.image_generation.endpoint),
            "endpoint": config.image_generation.endpoint,
            "model": config.image_generation.model,
            "api_key": image_api_key,
            "auth_file": config.image_generation.auth_file,
            "timeout": config.image_generation.timeout,
        },
        "tool_limits": config.tool_limits.model_dump(),
        "agent": {
            "max_steps": config.agent.max_steps,
            "workspace_dir": config.agent.workspace_dir,
            "max_parallel_tools": config.agent.max_parallel_tools,
            "parallel_tool_timeout_seconds": config.agent.parallel_tool_timeout_seconds,
            "sub_agent_batch_synthesis_timeout_seconds": (
                config.agent.sub_agent_batch_synthesis_timeout_seconds
            ),
            "goal_autopilot_enabled": config.agent.goal_autopilot_enabled,
            "goal_autopilot_max_turns": config.agent.goal_autopilot_max_turns,
            "goal_autopilot_max_seconds": config.agent.goal_autopilot_max_seconds,
            "goal_autopilot_no_progress_turns": config.agent.goal_autopilot_no_progress_turns,
            "context_resource_dedup_enabled": (
                config.agent.context_resource_dedup_enabled
            ),
            "enable_memory": config.agent.enable_memory,
            "memory_dir": config.agent.memory_dir,
            "enable_memory_extraction": config.agent.enable_memory_extraction,
            "memory_maintainer_enabled": config.agent.memory_maintainer_enabled,
            "memory_promotion_proposal_enabled": config.agent.memory_promotion_proposal_enabled,
        },
        "tools": {
            "enable_file_tools": config.tools.enable_file_tools,
            "enable_bash": config.tools.enable_bash,
            "enable_todo": config.tools.enable_todo,
            "enable_plan": config.tools.enable_plan,
            "enable_sub_agent": config.tools.enable_sub_agent,
            "allow_full_access": config.tools.allow_full_access,
            "enable_skills": config.tools.enable_skills,
            "skills_dir": config.tools.skills_dir,
            "enable_mcp": config.tools.enable_mcp,
            "mcp_config_path": config.tools.mcp_config_path,
            "mcp": {
                "connect_timeout": config.tools.mcp.connect_timeout,
                "execute_timeout": config.tools.mcp.execute_timeout,
                "sse_read_timeout": config.tools.mcp.sse_read_timeout,
            },
        },
        "officev3": {
            "configured": getattr(config.officev3, "_present", False),
            "filesystem_scope": config.officev3.permissions.filesystem.scope,
            "allowed_directories": config.officev3.permissions.filesystem.allowed_directories,
            "session_workspace_root": config.officev3.paths.session_workspace_root,
            "openclaw_import": config.officev3.permissions.memory.openclaw_import,
        },
        "hooks": config.hooks.hooks,
    }


def _print_config_summary(summary: dict[str, Any]) -> None:
    print(f"{Colors.BOLD}Config file:{Colors.RESET} {summary['config_file']}\n")
    llm = summary["llm"]
    print(f"{Colors.BOLD}LLM{Colors.RESET}")
    print(f"  provider          : {llm['provider']}")
    print(f"  api_base          : {llm['api_base']}")
    print(f"  model             : {llm['model']}")
    print(f"  api_key           : {llm['api_key']}")
    print(f"  context_window    : {llm['context_window']}")
    print(f"  max_output_tokens : {llm['max_output_tokens']}")
    print(f"  timeout           : {llm['timeout']}")
    print(f"  retry.enabled     : {llm['retry']['enabled']}")

    agent = summary["agent"]
    print(f"\n{Colors.BOLD}Agent{Colors.RESET}")
    print(f"  workspace_dir     : {agent['workspace_dir']}")
    print(f"  max_steps         : {agent['max_steps']}")
    print(f"  max_parallel_tools: {agent['max_parallel_tools']}")
    print(f"  parallel_timeout  : {agent['parallel_tool_timeout_seconds']}s")
    print(f"  batch_synth_timeout: {agent['sub_agent_batch_synthesis_timeout_seconds']}s")
    print(f"  goal_autopilot    : {agent['goal_autopilot_enabled']} ({agent['goal_autopilot_max_turns']} turns, {agent['goal_autopilot_max_seconds']}s, no-progress {agent['goal_autopilot_no_progress_turns']})")
    print(f"  context_resources : {agent['context_resource_dedup_enabled']}")
    print(f"  enable_memory     : {agent['enable_memory']}")

    tool_limits = summary["tool_limits"]
    print(f"\n{Colors.BOLD}Tool limits{Colors.RESET}")
    print(
        "  general/search    : "
        f"summary>{tool_limits['general']['final_summary_after_calls']}, "
        f"web {tool_limits['web_search']['total_calls']}/"
        f"{tool_limits['web_search']['deep_research_total_calls']} "
        f"(batch {tool_limits['web_search']['batch_size']}, "
        f"concurrency {tool_limits['web_search']['concurrency']})"
    )
    print(
        "  workflows         : "
        f"skill {tool_limits['external_skill']['max_tool_calls']}+"
        f"{tool_limits['external_skill']['max_delegated_tool_calls']} delegated, "
        f"presentation {tool_limits['presentation']['max_tool_calls']}/"
        f"{tool_limits['presentation']['deep_research_max_tool_calls']}+"
        f"{tool_limits['presentation']['max_delegated_tool_calls']} delegated, "
        f"sub-agent {tool_limits['sub_agent']['general_max_tool_calls']}"
    )

    tools = summary["tools"]
    print(f"\n{Colors.BOLD}Tools{Colors.RESET}")
    print(f"  file_tools        : {tools['enable_file_tools']}")
    print(f"  bash              : {tools['enable_bash']}")
    print(f"  todo              : {tools['enable_todo']}")
    print(f"  plan              : {tools['enable_plan']}")
    print(f"  sub_agent         : {tools['enable_sub_agent']}")
    print(f"  skills            : {tools['enable_skills']} ({tools['skills_dir']})")
    print(f"  mcp               : {tools['enable_mcp']} ({tools['mcp_config_path']})")
    print(f"  allow_full_access : {tools['allow_full_access']}")

    lite = summary["lite_llm"]
    image = summary["image_generation"]
    print(f"\n{Colors.BOLD}Optional Services{Colors.RESET}")
    print(f"  lite_llm          : {lite['enabled']} ({lite['provider']} {lite['model']})")
    print(f"  image_generation  : {image['configured']} ({image['model']})")


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _config_exit_error(message: str, json_output: bool = False) -> int:
    if json_output:
        _json_print({"ok": False, "error": message})
    else:
        print(f"{Colors.RED}❌ {message}{Colors.RESET}")
    return 1


def get_log_directory() -> Path:
    """Get the log directory path."""
    return Path.home() / ".box-agent" / "log"


def show_log_directory(open_file_manager: bool = True) -> None:
    """Show log directory contents and optionally open file manager.

    Args:
        open_file_manager: Whether to open the system file manager
    """
    log_dir = get_log_directory()

    print(f"\n{Colors.BRIGHT_CYAN}📁 Log Directory: {log_dir}{Colors.RESET}")

    if not log_dir.exists() or not log_dir.is_dir():
        print(f"{Colors.RED}Log directory does not exist: {log_dir}{Colors.RESET}\n")
        return

    log_files = list(log_dir.glob("*.log"))

    if not log_files:
        print(f"{Colors.YELLOW}No log files found in directory.{Colors.RESET}\n")
        return

    # Sort by modification time (newest first)
    log_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    print(f"{Colors.DIM}{'─' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BRIGHT_YELLOW}Available Log Files (newest first):{Colors.RESET}")

    for i, log_file in enumerate(log_files[:10], 1):
        mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
        size = log_file.stat().st_size
        size_str = f"{size:,}" if size < 1024 else f"{size / 1024:.1f}K"
        print(f"  {Colors.GREEN}{i:2d}.{Colors.RESET} {Colors.BRIGHT_WHITE}{log_file.name}{Colors.RESET}")
        print(f"      {Colors.DIM}Modified: {mtime.strftime('%Y-%m-%d %H:%M:%S')}, Size: {size_str}{Colors.RESET}")

    if len(log_files) > 10:
        print(f"  {Colors.DIM}... and {len(log_files) - 10} more files{Colors.RESET}")

    print(f"{Colors.DIM}{'─' * 60}{Colors.RESET}")

    # Open file manager
    if open_file_manager:
        _open_directory_in_file_manager(log_dir)

    print()


def _open_directory_in_file_manager(directory: Path) -> None:
    """Open directory in system file manager (cross-platform)."""
    system = platform.system()

    try:
        if system == "Darwin":
            subprocess.run(["open", str(directory)], check=False)
        elif system == "Windows":
            subprocess.run(["explorer", str(directory)], check=False)
        elif system == "Linux":
            subprocess.run(["xdg-open", str(directory)], check=False)
    except FileNotFoundError:
        print(f"{Colors.YELLOW}Could not open file manager. Please navigate manually.{Colors.RESET}")
    except Exception as e:
        print(f"{Colors.YELLOW}Error opening file manager: {e}{Colors.RESET}")


def read_log_file(filename: str) -> None:
    """Read and display a specific log file.

    Args:
        filename: The log filename to read
    """
    log_dir = get_log_directory()
    log_file = log_dir / filename

    if not log_file.exists() or not log_file.is_file():
        print(f"\n{Colors.RED}❌ Log file not found: {log_file}{Colors.RESET}\n")
        return

    print(f"\n{Colors.BRIGHT_CYAN}📄 Reading: {log_file}{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * 80}{Colors.RESET}")

    try:
        with open(log_file, "r", encoding="utf-8") as f:
            content = f.read()
        print(content)
        print(f"{Colors.DIM}{'─' * 80}{Colors.RESET}")
        print(f"\n{Colors.GREEN}✅ End of file{Colors.RESET}\n")
    except Exception as e:
        print(f"\n{Colors.RED}❌ Error reading file: {e}{Colors.RESET}\n")


def print_banner():
    """Print welcome banner with proper alignment"""
    BOX_WIDTH = 58
    banner_text = f"{Colors.BOLD}🤖 Box Agent - Multi-turn Interactive Session{Colors.RESET}"
    banner_width = calculate_display_width(banner_text)

    # Center the text with proper padding
    total_padding = BOX_WIDTH - banner_width
    left_padding = total_padding // 2
    right_padding = total_padding - left_padding

    print()
    print(f"{Colors.BOLD}{Colors.BRIGHT_CYAN}╔{'═' * BOX_WIDTH}╗{Colors.RESET}")
    print(
        f"{Colors.BOLD}{Colors.BRIGHT_CYAN}║{Colors.RESET}{' ' * left_padding}{banner_text}{' ' * right_padding}{Colors.BOLD}{Colors.BRIGHT_CYAN}║{Colors.RESET}"
    )
    print(f"{Colors.BOLD}{Colors.BRIGHT_CYAN}╚{'═' * BOX_WIDTH}╝{Colors.RESET}")
    print()


def print_help():
    """Print help information"""
    help_text = f"""
{Colors.BOLD}{Colors.BRIGHT_YELLOW}Available Commands:{Colors.RESET}
  {Colors.BRIGHT_GREEN}/help{Colors.RESET}      - Show this help message
  {Colors.BRIGHT_GREEN}/clear{Colors.RESET}     - Clear session history (keep system prompt, sandbox intact)
  {Colors.BRIGHT_GREEN}/clear_all{Colors.RESET}  - Clear session history AND shutdown sandbox kernel
  {Colors.BRIGHT_GREEN}/history{Colors.RESET}   - Show current session message count
  {Colors.BRIGHT_GREEN}/stats{Colors.RESET}     - Show session statistics
  {Colors.BRIGHT_GREEN}/sandbox_status{Colors.RESET} - Show sandbox session status
  {Colors.BRIGHT_GREEN}/log{Colors.RESET}       - Show log directory and recent files
  {Colors.BRIGHT_GREEN}/log <file>{Colors.RESET} - Read a specific log file
  {Colors.BRIGHT_GREEN}/goal <objective>{Colors.RESET} - Set a durable session goal
  {Colors.BRIGHT_GREEN}/goal{Colors.RESET}      - Show current goal (also: set, pause, resume, progress, block, complete, clear)
  {Colors.BRIGHT_GREEN}/memory review{Colors.RESET} - 审阅可升级到核心记忆的候选条目
  {Colors.BRIGHT_GREEN}/exit{Colors.RESET}      - Exit program (also: exit, quit, q)

{Colors.BOLD}{Colors.BRIGHT_YELLOW}Keyboard Shortcuts:{Colors.RESET}
  {Colors.BRIGHT_CYAN}Esc{Colors.RESET}        - Cancel current agent execution
  {Colors.BRIGHT_CYAN}Ctrl+C{Colors.RESET}     - Exit program
  {Colors.BRIGHT_CYAN}Ctrl+U{Colors.RESET}     - Clear current input line
  {Colors.BRIGHT_CYAN}Ctrl+L{Colors.RESET}     - Clear screen
  {Colors.BRIGHT_CYAN}Ctrl+J{Colors.RESET}     - Insert newline (also Ctrl+Enter)
  {Colors.BRIGHT_CYAN}Tab{Colors.RESET}        - Auto-complete commands
  {Colors.BRIGHT_CYAN}↑/↓{Colors.RESET}        - Browse command history
  {Colors.BRIGHT_CYAN}→{Colors.RESET}          - Accept auto-suggestion

{Colors.BOLD}{Colors.BRIGHT_YELLOW}Usage:{Colors.RESET}
  - Enter your task directly, Agent will help you complete it
  - Agent remembers all conversation content in this session
  - Use {Colors.BRIGHT_GREEN}/clear{Colors.RESET} to start a new session (sandbox variables persist)
  - Use {Colors.BRIGHT_GREEN}/clear_all{Colors.RESET} to start completely fresh (kills sandbox kernel)
  - Press {Colors.BRIGHT_CYAN}Enter{Colors.RESET} to submit your message
  - Use {Colors.BRIGHT_CYAN}Ctrl+J{Colors.RESET} to insert line breaks within your message
"""
    print(help_text)


def print_session_info(agent: Agent, workspace_dir: Path, model: str):
    """Print session information with proper alignment"""
    BOX_WIDTH = 58

    def print_info_line(text: str):
        """Print a single info line with proper padding"""
        # Account for leading space
        text_width = calculate_display_width(text)
        padding = max(0, BOX_WIDTH - 1 - text_width)
        print(f"{Colors.DIM}│{Colors.RESET} {text}{' ' * padding}{Colors.DIM}│{Colors.RESET}")

    # Top border
    print(f"{Colors.DIM}┌{'─' * BOX_WIDTH}┐{Colors.RESET}")

    # Header (centered)
    header_text = f"{Colors.BRIGHT_CYAN}Session Info{Colors.RESET}"
    header_width = calculate_display_width(header_text)
    header_padding_total = BOX_WIDTH - 1 - header_width  # -1 for leading space
    header_padding_left = header_padding_total // 2
    header_padding_right = header_padding_total - header_padding_left
    print(f"{Colors.DIM}│{Colors.RESET} {' ' * header_padding_left}{header_text}{' ' * header_padding_right}{Colors.DIM}│{Colors.RESET}")

    # Divider
    print(f"{Colors.DIM}├{'─' * BOX_WIDTH}┤{Colors.RESET}")

    # Info lines
    print_info_line(f"Model: {model}")
    print_info_line(f"Workspace: {workspace_dir}")
    print_info_line(f"Message History: {len(agent.messages)} messages")
    print_info_line(f"Available Tools: {len(agent.tools)} tools")

    # Bottom border
    print(f"{Colors.DIM}└{'─' * BOX_WIDTH}┘{Colors.RESET}")
    print()
    print(f"{Colors.DIM}Type {Colors.BRIGHT_GREEN}/help{Colors.DIM} for help, {Colors.BRIGHT_GREEN}/exit{Colors.DIM} to quit{Colors.RESET}")
    print()


def print_stats(agent: Agent, session_start: datetime):
    """Print session statistics"""
    duration = datetime.now() - session_start
    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    # Count different types of messages
    user_msgs = sum(1 for m in agent.messages if m.role == "user")
    assistant_msgs = sum(1 for m in agent.messages if m.role == "assistant")
    tool_msgs = sum(1 for m in agent.messages if m.role == "tool")

    print(f"\n{Colors.BOLD}{Colors.BRIGHT_CYAN}Session Statistics:{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * 40}{Colors.RESET}")
    print(f"  Session Duration: {hours:02d}:{minutes:02d}:{seconds:02d}")
    print(f"  Total Messages: {len(agent.messages)}")
    print(f"    - User Messages: {Colors.BRIGHT_GREEN}{user_msgs}{Colors.RESET}")
    print(f"    - Assistant Replies: {Colors.BRIGHT_BLUE}{assistant_msgs}{Colors.RESET}")
    print(f"    - Tool Calls: {Colors.BRIGHT_YELLOW}{tool_msgs}{Colors.RESET}")
    print(f"  Available Tools: {len(agent.tools)}")
    if agent.api_total_tokens > 0:
        print(f"  API Tokens Used: {Colors.BRIGHT_MAGENTA}{agent.api_total_tokens:,}{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * 40}{Colors.RESET}\n")


def _session_stats(agent: Agent, session_start: datetime) -> dict[str, Any]:
    duration = datetime.now() - session_start
    return {
        "duration_seconds": int(duration.total_seconds()),
        "messages": {
            "total": len(agent.messages),
            "user": sum(1 for m in agent.messages if m.role == "user"),
            "assistant": sum(1 for m in agent.messages if m.role == "assistant"),
            "tool": sum(1 for m in agent.messages if m.role == "tool"),
        },
        "available_tools": len(agent.tools),
        "api_total_tokens": agent.api_total_tokens,
    }


def print_goal_status(agent: Agent) -> None:
    """Print the current session goal."""
    if agent.goal is None:
        print(f"\n{Colors.DIM}No active goal. Use /goal <objective> to set one.{Colors.RESET}\n")
        return

    status_color = {
        "active": Colors.BRIGHT_GREEN,
        "paused": Colors.YELLOW,
        "complete": Colors.BRIGHT_BLUE,
        "blocked": Colors.BRIGHT_YELLOW,
    }.get(agent.goal.status, Colors.BRIGHT_WHITE)
    print(f"\n{Colors.BOLD}{Colors.BRIGHT_CYAN}Current Goal:{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * 40}{Colors.RESET}")
    print(f"  Status   : {status_color}{agent.goal.status}{Colors.RESET}")
    print(f"  Objective: {agent.goal.objective}")
    print(f"  Created  : {agent.goal.created_at}")
    print(f"  Updated  : {agent.goal.updated_at}")
    if agent.goal.completed_by:
        print(f"  Completed: {agent.goal.completed_by}")
    if agent.goal.blocked_reason:
        print(f"  Blocked  : {agent.goal.blocked_reason}")
    if agent.goal.progress:
        print("  Progress :")
        for item in agent.goal.progress:
            print(f"    - {item}")
    if agent.goal.evidence:
        print("  Evidence :")
        for item in agent.goal.evidence:
            print(f"    - {item}")
    print(f"{Colors.DIM}{'─' * 40}{Colors.RESET}\n")


def _goal_store_path(workspace_dir: Path) -> Path:
    key = hashlib.sha256(str(workspace_dir.expanduser().absolute()).encode("utf-8")).hexdigest()[:24]
    return Path.home() / ".box-agent" / "goals" / f"{key}.json"


def _load_goal_state(workspace_dir: Path) -> GoalState | None:
    path = _goal_store_path(workspace_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    goal_data = data.get("goal") if isinstance(data, dict) else data
    return goal_state_from_payload(goal_data)


def _save_goal_state(workspace_dir: Path, goal: GoalState | None) -> None:
    path = _goal_store_path(workspace_dir)
    if goal is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "workspace": str(workspace_dir.expanduser().absolute()),
        "goal": goal_payload(goal),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _restore_cli_goal(agent: Agent, workspace_dir: Path) -> GoalState | None:
    goal = _load_goal_state(workspace_dir)
    if goal is None:
        return None
    agent.goal = goal
    return goal


def handle_goal_command(agent: Agent, command_line: str) -> None:
    """Handle /goal lifecycle commands for the interactive CLI."""
    parts = command_line.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if not arg or arg.lower() == "status":
        print_goal_status(agent)
        return

    action_parts = arg.split(maxsplit=1)
    subcommand = action_parts[0].lower()
    remainder = action_parts[1].strip() if len(action_parts) > 1 else ""

    if subcommand == "set":
        if not remainder:
            print(f"{Colors.RED}❌ Goal objective is required.{Colors.RESET}\n")
            return
        try:
            goal = agent.set_goal(remainder)
        except ValueError as exc:
            print(f"{Colors.RED}❌ {exc}{Colors.RESET}\n")
            return
        print(f"{Colors.GREEN}✅ Goal set:{Colors.RESET} {goal.objective}")
        print(f"{Colors.DIM}   Future turns will include this objective until paused, completed, blocked, or cleared.{Colors.RESET}\n")
        return

    if subcommand == "pause":
        if agent.pause_goal() is None:
            print(f"{Colors.YELLOW}⚠️  No goal to pause.{Colors.RESET}\n")
        else:
            print(f"{Colors.GREEN}✅ Goal paused. Use /goal resume to continue it.{Colors.RESET}\n")
        return

    if subcommand == "resume":
        if agent.resume_goal() is None:
            print(f"{Colors.YELLOW}⚠️  No goal to resume.{Colors.RESET}\n")
        else:
            print(f"{Colors.GREEN}✅ Goal resumed.{Colors.RESET}\n")
        return

    if subcommand == "progress":
        if not remainder:
            print(f"{Colors.RED}❌ Progress text is required.{Colors.RESET}\n")
            return
        if agent.update_goal_progress([remainder]) is None:
            print(f"{Colors.YELLOW}⚠️  No goal to update.{Colors.RESET}\n")
        else:
            print(f"{Colors.GREEN}✅ Goal progress recorded.{Colors.RESET}\n")
        return

    if subcommand == "block":
        if not remainder:
            print(f"{Colors.RED}❌ Blocked reason is required.{Colors.RESET}\n")
            return
        try:
            blocked = agent.block_goal(remainder)
        except ValueError as exc:
            print(f"{Colors.RED}❌ {exc}{Colors.RESET}\n")
            return
        if blocked is None:
            print(f"{Colors.YELLOW}⚠️  No goal to block.{Colors.RESET}\n")
        else:
            print(f"{Colors.YELLOW}⚠️  Goal blocked:{Colors.RESET} {remainder}\n")
        return

    if subcommand == "clear":
        if agent.clear_goal() is None:
            print(f"{Colors.YELLOW}⚠️  No goal to clear.{Colors.RESET}\n")
        else:
            print(f"{Colors.GREEN}✅ Goal cleared.{Colors.RESET}\n")
        return

    if subcommand == "complete":
        evidence = [remainder] if remainder else ["Completed via CLI /goal complete."]
        if agent.complete_goal(evidence=evidence, completed_by="cli") is None:
            print(f"{Colors.YELLOW}⚠️  No goal to complete.{Colors.RESET}\n")
        else:
            print(f"{Colors.GREEN}✅ Goal marked complete. Use /goal clear to remove it.{Colors.RESET}\n")
        return

    try:
        goal = agent.set_goal(arg)
    except ValueError as exc:
        print(f"{Colors.RED}❌ {exc}{Colors.RESET}\n")
        return
    print(f"{Colors.GREEN}✅ Goal set:{Colors.RESET} {goal.objective}")
    print(f"{Colors.DIM}   Future turns will include this objective until paused, completed, or cleared.{Colors.RESET}\n")


def cmd_goal(
    workspace_dir: Path,
    action: str = "status",
    text: list[str] | None = None,
    evidence: list[str] | None = None,
    progress: list[str] | None = None,
    json_output: bool = False,
) -> int:
    """Scriptable CLI goal management without starting the agent runtime."""
    action = (action or "status").strip().lower()
    text_value = " ".join(text or []).strip()
    evidence_items = [item.strip() for item in (evidence or []) if item.strip()]
    progress_items = [item.strip() for item in (progress or []) if item.strip()]
    goal = _load_goal_state(workspace_dir)
    now = datetime.now().isoformat()

    def emit(ok: bool = True, error: str | None = None) -> int:
        if json_output:
            payload = {"ok": ok, "goal": goal_payload(goal)}
            if error:
                payload["error"] = error
            _json_print(payload)
        else:
            if error:
                print(f"{Colors.RED}❌ {error}{Colors.RESET}")
            elif goal is None:
                print(f"{Colors.DIM}No goal for workspace: {workspace_dir}{Colors.RESET}")
            else:
                temp_agent = Agent.__new__(Agent)
                temp_agent.goal = goal
                print_goal_status(temp_agent)
        return 0 if ok else 1

    if action in ("status", "get"):
        return emit()

    if action == "set":
        if not text_value:
            return emit(False, "Goal objective is required.")
        goal = GoalState(
            objective=text_value,
            status="active",
            created_at=now,
            updated_at=now,
            evidence=evidence_items,
            progress=progress_items,
        )
        _save_goal_state(workspace_dir, goal)
        return emit()

    if action == "clear":
        goal = None
        _save_goal_state(workspace_dir, None)
        return emit()

    if goal is None:
        return emit(False, "No goal is set for this workspace.")

    if action == "pause":
        goal.status = "paused"
        goal.updated_at = now
    elif action == "resume":
        goal.status = "active"
        goal.blocked_reason = None
        goal.updated_at = now
    elif action == "complete":
        goal.status = "complete"
        goal.blocked_reason = None
        if text_value:
            evidence_items.append(text_value)
        if not evidence_items:
            evidence_items.append("Completed via `box-agent goal complete`.")
        for item in evidence_items:
            if item not in goal.evidence:
                goal.evidence.append(item)
        for item in progress_items:
            if item not in goal.progress:
                goal.progress.append(item)
        goal.completed_by = "cli"
        goal.updated_at = now
    elif action == "progress":
        if text_value:
            progress_items.append(text_value)
        if not progress_items:
            return emit(False, "Progress text is required.")
        for item in progress_items:
            if item not in goal.progress:
                goal.progress.append(item)
        for item in evidence_items:
            if item not in goal.evidence:
                goal.evidence.append(item)
        goal.updated_at = now
    elif action == "block":
        reason = text_value
        if not reason:
            return emit(False, "Blocked reason is required.")
        goal.status = "blocked"
        goal.blocked_reason = reason
        for item in evidence_items:
            if item not in goal.evidence:
                goal.evidence.append(item)
        for item in progress_items:
            if item not in goal.progress:
                goal.progress.append(item)
        goal.updated_at = now
    else:
        return emit(False, f"Unknown goal action: {action}")

    _save_goal_state(workspace_dir, goal)
    return emit()


def _workspace_from_args(args: argparse.Namespace) -> Path:
    if getattr(args, "workspace", None):
        return Path(args.workspace).expanduser().absolute()
    return Path.cwd()


def parse_args() -> argparse.Namespace:
    """Parse command line arguments

    Returns:
        Parsed arguments
    """
    parser = argparse.ArgumentParser(
        description="Box Agent - AI assistant with file tools and MCP support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  box-agent                              # Use current directory as workspace
  box-agent --workspace /path/to/dir     # Use specific workspace directory
  box-agent --task "create a report" --json
  box-agent --goal "Ship CLI parity" --task "finish the tests"
  box-agent goal status
  box-agent goal complete --evidence "uv run pytest tests/ -q passed"
  box-agent --task "create a PPT" --force-plan-start
  box-agent setup                        # Run first-time setup wizard
  box-agent config                       # Show current configuration
  box-agent config --get model           # Print one config value
  box-agent config --set max_steps 300   # Update one config value
  box-agent config --edit                # Open config file in editor
  box-agent doctor                       # Check environment and connectivity
  box-agent doctor --json                # Machine-readable health check
  box-agent log                          # Show log directory and recent files
  box-agent log agent_run_xxx.log        # Read a specific log file
        """,
    )
    parser.add_argument(
        "--workspace",
        "-w",
        type=str,
        default=None,
        help="Workspace directory (default: current directory)",
    )
    parser.add_argument(
        "--workspace-type",
        choices=("general", "code"),
        default=None,
        help="Persist the workspace type in ~/.box-agent/config/workspaces.json",
    )
    parser.add_argument(
        "--task",
        "-t",
        type=str,
        default=None,
        help="Execute a task non-interactively and exit",
    )
    parser.add_argument(
        "--goal",
        type=str,
        default=None,
        help="Set or replace the persistent workspace goal before starting interactive/--task mode",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON where supported (doctor/config; --task appends a summary)",
    )
    parser.add_argument(
        "--no-verify-api",
        action="store_true",
        help="Skip the startup API connectivity probe before running the agent",
    )
    parser.add_argument(
        "--deep-think",
        action="store_true",
        help="Enable model thinking mode for providers that support it",
    )
    parser.add_argument(
        "--force-plan-start",
        action="store_true",
        help="Force the next agent turn to publish a structured plan first",
    )
    parser.add_argument(
        "--no-completion-gate",
        action="store_true",
        help="Disable automatic deliverable-artifact completion checks in --task mode",
    )
    parser.add_argument(
        "--no-goal-autopilot",
        action="store_true",
        help="Disable automatic continuation for active durable goals in --task mode",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"box-agent {__version__}",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="Disable Jupyter sandbox mode (sandbox is enabled by default)",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # log subcommand
    log_parser = subparsers.add_parser("log", help="Show log directory or read log files")
    log_parser.add_argument(
        "filename",
        nargs="?",
        default=None,
        help="Log filename to read (optional, shows directory if omitted)",
    )

    # goal subcommand
    goal_parser = subparsers.add_parser("goal", help="Show or manage the persistent workspace goal")
    goal_parser.add_argument(
        "action",
        nargs="?",
        default="status",
        help="Goal action: status, set, pause, resume, complete, clear, progress, block",
    )
    goal_parser.add_argument(
        "text",
        nargs="*",
        default=[],
        help="Objective, evidence, progress text, or blocked reason depending on the action",
    )
    goal_parser.add_argument(
        "--evidence",
        action="append",
        default=[],
        help="Evidence entry to append (repeatable)",
    )
    goal_parser.add_argument(
        "--progress",
        action="append",
        default=[],
        help="Progress entry to append (repeatable)",
    )
    goal_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable goal status",
    )
    goal_parser.add_argument(
        "--workspace",
        "-w",
        type=str,
        default=None,
        help="Workspace directory for this goal command",
    )

    # setup subcommand
    subparsers.add_parser("setup", help="Run first-time setup wizard")

    # config subcommand
    config_parser = subparsers.add_parser("config", help="Show or edit configuration")
    config_parser.add_argument(
        "--edit",
        "-e",
        action="store_true",
        help="Open config file in editor",
    )
    config_parser.add_argument(
        "key",
        nargs="?",
        default=None,
        help="Config key to print, for example model or tools.enable_mcp",
    )
    config_parser.add_argument(
        "--get",
        dest="get_key",
        default=None,
        help="Print a single config value",
    )
    config_parser.add_argument(
        "--set",
        dest="set_pair",
        nargs=2,
        metavar=("KEY", "VALUE"),
        default=None,
        help="Update one config value; VALUE is parsed as YAML",
    )
    config_parser.add_argument(
        "--list",
        action="store_true",
        help="Show the expanded config summary",
    )
    config_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    config_parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="Show raw secret values instead of masked values",
    )

    # doctor subcommand
    doctor_parser = subparsers.add_parser("doctor", help="Check environment and connectivity")
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )

    # install-browser subcommand
    subparsers.add_parser(
        "install-browser",
        help="Install Chromium for Playwright MCP browser tools (~200MB)",
    )

    # install-node subcommand
    node_parser = subparsers.add_parser(
        "install-node",
        help="Install Box-Agent managed Node.js runtime for skills (macOS only)",
    )
    node_parser.add_argument(
        "--version",
        default=DEFAULT_NODE_VERSION,
        help=f"Node.js version to install (default: {DEFAULT_NODE_VERSION})",
    )

    return parser.parse_args()


def cmd_setup():
    """Run the first-time setup wizard."""
    config_path = Config._ensure_user_config()
    print(f"{Colors.DIM}Config file: {config_path}{Colors.RESET}\n")
    run_setup_wizard(config_path)


def cmd_config(
    edit: bool = False,
    get_key: str | None = None,
    set_pair: tuple[str, str] | None = None,
    list_all: bool = False,
    json_output: bool = False,
    show_secrets: bool = False,
) -> int:
    """Show, read, edit, or update configuration."""
    config_path = Config.find_config_file("config.yaml")
    if not config_path and set_pair is not None:
        config_path = Config._ensure_user_config()
    if not config_path:
        return _config_exit_error("No config.yaml found. Run `box-agent setup` first.", json_output)

    if edit:
        import os

        editor = os.environ.get("EDITOR")
        if not editor:
            editor = "open" if platform.system() == "Darwin" else "vi"
        if not json_output:
            print(f"{Colors.BOLD}Config file:{Colors.RESET} {config_path}")
            print(f"{Colors.DIM}Opening with {editor}...{Colors.RESET}")
        result = subprocess.run([editor, str(config_path)], check=False)
        return result.returncode

    if set_pair is not None:
        key, raw_value = set_pair
        old_text: str | None = None
        try:
            data = _load_config_yaml(config_path)
            yaml_parts = _normalize_config_path(key)
            value = _parse_config_value(raw_value)
            old_text = config_path.read_text(encoding="utf-8")
            _set_nested(data, yaml_parts, value)
            config_path.write_text(
                yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            Config.from_yaml(config_path)
        except Exception as e:
            if old_text is not None:
                config_path.write_text(old_text, encoding="utf-8")
            return _config_exit_error(f"Failed to update config: {e}", json_output)

        display_value = value
        if _is_secret_path(yaml_parts) and not show_secrets:
            display_value = _mask_secret(value)
        if json_output:
            _json_print({
                "ok": True,
                "config_file": str(config_path),
                "key": key,
                "value": display_value,
            })
        else:
            print(f"{Colors.GREEN}✅ Updated {key} = {display_value!r}{Colors.RESET}")
            print(f"{Colors.DIM}Config file: {config_path}{Colors.RESET}")
        return 0

    try:
        config = Config.from_yaml(config_path)
    except Exception as e:
        return _config_exit_error(f"Could not parse config: {e}", json_output)

    summary = _config_summary(config, config_path, show_secrets=show_secrets)
    if get_key:
        try:
            display_parts = _normalize_display_path(get_key)
            value = _get_nested(summary, display_parts)
        except (KeyError, ValueError):
            return _config_exit_error(f"Unknown config key: {get_key}", json_output)
        if _is_secret_path(display_parts) and not show_secrets:
            value = _mask_secret(value)
        if json_output:
            _json_print({"ok": True, "key": get_key, "value": value})
        else:
            print(value)
        return 0

    if json_output:
        _json_print({"ok": True, **summary})
    else:
        _print_config_summary(summary)
        if not list_all:
            print(f"\n{Colors.DIM}Use `box-agent config --list --json` for a machine-readable view.{Colors.RESET}")
            print(f"{Colors.DIM}Use `box-agent config --set KEY VALUE` to update a setting.{Colors.RESET}")
    return 0


def build_cli_env_context():
    """Build host-style environment facts for standalone CLI sessions."""
    from box_agent.acp.env_context import EnvContext
    from box_agent.tools.obsidian_tool import load_obsidian_config

    obsidian_config = load_obsidian_config()
    if not obsidian_config:
        return None
    return EnvContext.from_meta(
        {
            "platform": platform.system().lower(),
            "obsidian": obsidian_config,
        }
    )


def _resolve_obsidian_cli(cli_path: str | None) -> str | None:
    candidate = (cli_path or "").strip() or "obsidian"
    if "/" in candidate or "\\" in candidate:
        candidate = str(Path(candidate).expanduser())
    return shutil.which(candidate)


def _doctor_check(status: str, message: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "message": message}
    payload.update(details)
    return payload


def _doctor_status_prefix(status: str) -> str:
    return {
        "ok": "✅",
        "warning": "⚠️ ",
        "error": "❌",
        "skipped": "⏭️ ",
    }.get(status, "ℹ️ ")


def _doctor_line(name: str, result: dict[str, Any]) -> str:
    return f"  {_doctor_status_prefix(str(result.get('status')))} {name:<9} — {result.get('message', '')}"


def _doctor_obsidian_status() -> dict[str, Any]:
    from box_agent.tools.obsidian_tool import load_obsidian_config, obsidian_config_path

    config_path = obsidian_config_path()
    config = load_obsidian_config()
    if not config and not config_path.exists():
        return _doctor_check(
            "warning",
            "not configured (officev3 will create obsidian.json after Vault binding)",
            config_file=str(config_path),
        )
    if config.get("enabled") is False:
        return _doctor_check(
            "warning",
            "disabled (bind a Vault in officev3 Settings → Connect Data Sources)",
            config_file=str(config_path),
        )

    problems: list[str] = []
    vault_path = config.get("vault_path")
    vault: Path | None = None
    if isinstance(vault_path, str) and vault_path.strip():
        vault = Path(vault_path).expanduser()
        if not vault.exists() or not vault.is_dir():
            problems.append(f"Vault missing or not a directory: {vault}")
        elif not (vault / ".obsidian").is_dir():
            problems.append(f"Vault missing .obsidian: {vault}")
    else:
        problems.append("Vault not configured")

    cli_path = config.get("cli_path") if isinstance(config.get("cli_path"), str) else None
    resolved_cli = _resolve_obsidian_cli(cli_path)
    if not resolved_cli:
        problems.append(f"CLI not found: {(cli_path or 'obsidian').strip() or 'obsidian'}")

    if problems:
        return _doctor_check(
            "error",
            "; ".join(problems),
            config_file=str(config_path),
            vault_path=vault_path,
            cli_path=cli_path,
        )

    vault_label = config.get("vault_name") if isinstance(config.get("vault_name"), str) else None
    if not vault_label and vault is not None:
        vault_label = vault.name
    return _doctor_check(
        "ok",
        f"Vault: {vault_label or 'configured'}; CLI: {resolved_cli}",
        config_file=str(config_path),
        vault_path=str(vault) if vault else "",
        cli_path=resolved_cli,
    )


def _doctor_obsidian_status_line() -> str:
    return _doctor_line("Obsidian", _doctor_obsidian_status())


def _doctor_check_obsidian() -> None:
    print(_doctor_obsidian_status_line())


def _doctor_config_status() -> tuple[dict[str, Any], Config | None]:
    config_path = Config.find_config_file("config.yaml")
    config = None
    if not config_path:
        return _doctor_check("error", "config.yaml not found (run `box-agent setup`)"), None
    try:
        config = Config.from_yaml(config_path)
        return _doctor_check("ok", str(config_path), config_file=str(config_path)), config
    except Exception as e:
        return _doctor_check("error", f"parse error: {e}", config_file=str(config_path)), None


async def _probe_llm_api(client: LLMClient):
    """Run one tool-free connectivity probe classified as an internal helper call."""
    return await client.generate(
        messages=[Message(role="user", content="hi")],
        call_kind="utility",
    )


async def _doctor_api_status(config: Config | None) -> dict[str, Any]:
    if config is None:
        return _doctor_check("skipped", "skipped (no valid config)")
    try:
        from box_agent.retry import RetryConfig as DoctorRetryConfig
        from box_agent.schema import LLMProvider as LP

        provider = LP.ANTHROPIC if config.llm.provider.lower() == "anthropic" else LP.OPENAI
        no_retry = DoctorRetryConfig(enabled=False, max_retries=0)
        client = LLMClient(
            api_key=config.llm.api_key,
            provider=provider,
            api_base=config.llm.api_base,
            model=config.llm.model,
            retry_config=no_retry,
            max_output_tokens=config.llm.max_output_tokens,
            auth_file=config.llm.auth_file,
            timeout=config.llm.timeout,
        )
        response = await _probe_llm_api(client)
        if response and response.content:
            return _doctor_check(
                "ok",
                f"{config.llm.api_base} ({config.llm.model})",
                api_base=config.llm.api_base,
                provider=config.llm.provider,
                model=config.llm.model,
            )
        return _doctor_check("error", f"empty response from {config.llm.api_base}")
    except Exception as e:
        return _doctor_check("error", str(e))


def _doctor_sandbox_status() -> dict[str, Any]:
    try:
        import jupyter_client  # noqa: F401

        kernel_dir = Path.home() / "Library" / "Jupyter" / "kernels" / "mini-agent-sandbox"
        if not kernel_dir.exists():
            # Try the generic jupyter data path
            try:
                specs = jupyter_client.kernelspec.find_kernel_specs()
                kernel_dir = specs.get("mini-agent-sandbox")
            except Exception:
                kernel_dir = None
        if kernel_dir:
            return _doctor_check("ok", "jupyter_client OK, kernel spec found", kernel_dir=str(kernel_dir))
        return _doctor_check("warning", "jupyter_client OK, but kernel spec 'mini-agent-sandbox' not found")
    except ImportError:
        return _doctor_check("error", "jupyter_client not installed")


def _doctor_mcp_status() -> dict[str, Any]:
    _user_mcp = Path.home() / ".box-agent" / "config" / "mcp.json"
    mcp_path = _user_mcp if _user_mcp.exists() else Config.find_config_file("mcp.json")
    if mcp_path:
        return _doctor_check("ok", str(mcp_path), config_file=str(mcp_path))
    return _doctor_check("warning", "mcp.json not found (optional)")


async def cmd_doctor(json_output: bool = False) -> int:
    """Check environment health: config, API, sandbox, MCP."""
    checks: dict[str, dict[str, Any]] = {}
    if not json_output:
        print(f"{Colors.BOLD}Box Agent Doctor{Colors.RESET}\n")

    checks["config"], config = _doctor_config_status()
    if not json_output:
        print(_doctor_line("Config", checks["config"]))

    checks["api"] = await _doctor_api_status(config)
    if not json_output:
        print(_doctor_line("API", checks["api"]))

    checks["sandbox"] = _doctor_sandbox_status()
    if not json_output:
        print(_doctor_line("Sandbox", checks["sandbox"]))

    checks["mcp"] = _doctor_mcp_status()
    if not json_output:
        print(_doctor_line("MCP", checks["mcp"]))

    checks["browser"] = _doctor_browser_status()
    if not json_output:
        print(_doctor_line("Browser", checks["browser"]))

    # Obsidian diagnostics are read-only; never start Obsidian or run write/open commands.
    checks["obsidian"] = _doctor_obsidian_status()
    if not json_output:
        print(_doctor_line("Obsidian", checks["obsidian"]))

    ok = not any(check.get("status") == "error" for check in checks.values())
    if json_output:
        _json_print({"ok": ok, "checks": checks})
    else:
        print()
    return 0 if ok else 1


def _default_browsers_path() -> Path:
    """Default Chromium cache directory shared by CLI install and ACP runtime."""
    return Path.home() / ".box-agent" / "browsers"


def _playwright_env() -> dict[str, str]:
    """Environment dict with PLAYWRIGHT_BROWSERS_PATH pinned to our default,
    unless the caller already set it explicitly."""
    import os
    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_default_browsers_path()))
    return env


def _cli_node_execution_env() -> tuple[str | None, dict[str, str]]:
    """Return the managed npx path and shared Skill execution environment."""
    runtime_context = build_skill_runtime_context(sandbox_mode=False)
    env = _playwright_env()
    managed_env = build_skill_execution_env(runtime_context, base_env=env)
    env.pop("npm_config_prefix", None)
    env.pop("npm_config_cache", None)
    env.update(managed_env)
    npx = runtime_context.get("node").env_vars.get("BOX_AGENT_NPX")
    return npx or shutil.which("npx", path=env.get("PATH")), env


def _doctor_browser_status() -> dict[str, Any]:
    """Check whether Node.js (npx) and Playwright Chromium are available."""
    npx, execution_env = _cli_node_execution_env()
    browsers_path = execution_env["PLAYWRIGHT_BROWSERS_PATH"]
    if not npx:
        return _doctor_check(
            "warning",
            "Node.js/npx not found (optional; needed for Playwright MCP)",
            browsers_path=browsers_path,
        )

    try:
        dry_run = subprocess.run(
            [npx, "-y", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True,
            text=True,
            timeout=30,
            env=execution_env,
        )
    except subprocess.TimeoutExpired:
        return _doctor_check(
            "warning",
            "playwright dry-run timed out",
            npx=npx,
            browsers_path=browsers_path,
        )

    combined = (dry_run.stdout or "") + (dry_run.stderr or "")
    # Playwright prints "browser: chromium ... <install location>" and omits
    # the "Install location" section when already installed. Use "Download url"
    # presence as the "needs download" signal.
    if dry_run.returncode == 0 and "Download url" not in combined:
        return _doctor_check(
            "ok",
            f"Chromium installed ({browsers_path})",
            npx=npx,
            browsers_path=browsers_path,
        )
    return _doctor_check(
        "warning",
        f"Chromium not installed in {browsers_path} (run `box-agent install-browser`)",
        npx=npx,
        browsers_path=browsers_path,
        returncode=dry_run.returncode,
    )


def _doctor_check_browser() -> None:
    print(_doctor_line("Browser", _doctor_browser_status()))


async def cmd_install_browser() -> None:
    """Install Chromium for Playwright MCP, then enable the playwright entry in mcp.json."""
    print(f"{Colors.BOLD}Box Agent · Install Browser Runtime{Colors.RESET}\n")

    npx, execution_env = _cli_node_execution_env()
    if not npx:
        print(f"{Colors.RED}❌ `npx` not found.{Colors.RESET}")
        print(f"{Colors.DIM}Install Node.js ≥ 18 first: https://nodejs.org/{Colors.RESET}")
        sys.exit(1)

    browsers_path = Path(execution_env["PLAYWRIGHT_BROWSERS_PATH"])
    browsers_path.mkdir(parents=True, exist_ok=True)

    print(f"{Colors.DIM}Target: {browsers_path}{Colors.RESET}")
    print(f"{Colors.DIM}Running: npx -y playwright install chromium (~200MB){Colors.RESET}\n")
    try:
        result = subprocess.run(
            [npx, "-y", "playwright", "install", "chromium"],
            check=False,
            env=execution_env,
        )
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}⚠️  Interrupted.{Colors.RESET}")
        sys.exit(130)

    if result.returncode != 0:
        print(f"\n{Colors.RED}❌ Chromium install failed (exit {result.returncode}).{Colors.RESET}")
        sys.exit(result.returncode)

    print(f"\n{Colors.GREEN}✅ Chromium installed.{Colors.RESET}")

    # Enable the playwright server in the user's mcp.json
    mcp_path = _ensure_user_mcp_config()
    try:
        _enable_playwright_in_mcp(mcp_path)
        print(f"{Colors.GREEN}✅ Enabled `playwright` MCP server in:{Colors.RESET} {mcp_path}")
    except Exception as e:
        print(f"{Colors.YELLOW}⚠️  Could not auto-enable playwright in mcp.json: {e}{Colors.RESET}")
        print(f"{Colors.DIM}   Manually set `mcpServers.playwright.disabled = false` in {mcp_path}.{Colors.RESET}")

    print(f"\n{Colors.DIM}Restart box-agent to load the browser tools.{Colors.RESET}\n")


def cmd_install_node(version: str = DEFAULT_NODE_VERSION) -> None:
    """Install Box-Agent's self-managed Node runtime for this platform."""
    print(f"{Colors.BOLD}Box Agent · Install Node Runtime{Colors.RESET}\n")
    manager = NodeRuntimeManager()
    print(f"{Colors.DIM}Target: {manager.root}{Colors.RESET}")
    print(f"{Colors.DIM}Version: {version}{Colors.RESET}")
    print(f"{Colors.DIM}Source: official Node.js release archive + SHASUMS256.txt{Colors.RESET}\n")
    try:
        runtime = manager.install_for_current_platform(version=version)
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}⚠️  Interrupted.{Colors.RESET}")
        sys.exit(130)
    except NodeRuntimeInstallError as exc:
        print(f"{Colors.RED}❌ Node runtime install failed: {exc}{Colors.RESET}")
        sys.exit(1)

    print(f"{Colors.GREEN}✅ Node runtime installed.{Colors.RESET}")
    print(f"{Colors.DIM}node: {runtime.env_vars.get('BOX_AGENT_NODE')}{Colors.RESET}")
    print(f"{Colors.DIM}npm:  {runtime.env_vars.get('BOX_AGENT_NPM')}{Colors.RESET}")
    print(f"{Colors.DIM}npx:  {runtime.env_vars.get('BOX_AGENT_NPX')}{Colors.RESET}")
    print(f"\n{Colors.DIM}Restart box-agent or box-agent-acp sessions to pick up the runtime.{Colors.RESET}\n")


def _ensure_user_mcp_config() -> Path:
    """Return path to the user-writable mcp.json, copying the example if needed."""
    user_dir = Path.home() / ".box-agent" / "config"
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / "mcp.json"
    if target.exists():
        return target

    example = Config.get_package_dir() / "config" / "mcp-example.json"
    if example.exists():
        shutil.copy2(example, target)
    else:
        target.write_text('{\n    "mcpServers": {}\n}\n', encoding="utf-8")
    return target


def _enable_playwright_in_mcp(mcp_path: Path) -> None:
    """Enable Playwright and apply the managed browser timeout defaults."""
    import json

    data = json.loads(mcp_path.read_text(encoding="utf-8"))
    servers = data.setdefault("mcpServers", {})
    entry = servers.get("playwright")
    if entry is None:
        example_path = Config.get_package_dir() / "config" / "mcp-example.json"
        example = json.loads(example_path.read_text(encoding="utf-8"))
        entry = example.get("mcpServers", {}).get("playwright")
        if entry is None:
            raise RuntimeError("playwright entry missing from mcp-example.json")
        servers["playwright"] = entry

    # Migrate the previous managed default while preserving deliberate custom
    # execution timeouts. Keep the outer MCP deadline above Playwright's own
    # navigation deadline so the actionable browser error can be returned.
    if entry.get("execute_timeout") in (None, 60, 60.0):
        entry["execute_timeout"] = 45.0
    args = entry.get("args")
    if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
        next_args: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--timeout-navigation":
                index += 1
                if index < len(args) and not args[index].startswith("--"):
                    index += 1
                continue
            next_args.append(arg)
            index += 1
        entry["args"] = [*next_args, "--timeout-navigation", "30000"]
    entry["disabled"] = False
    mcp_path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


async def _quiet_cleanup():
    """Clean up MCP connections, suppressing noisy asyncgen teardown tracebacks."""
    # Silence the asyncgen finalization noise that anyio/mcp emits when
    # stdio_client's task group is torn down across tasks.  The handler is
    # intentionally NOT restored: asyncgen finalization happens during
    # asyncio.run() shutdown (after run_agent returns), so restoring the
    # handler here would still let the noise through.  Since this runs
    # right before process exit, swallowing late exceptions is safe.
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(lambda _loop, _ctx: None)
    try:
        await cleanup_mcp_connections()
    except Exception:
        pass
    # Shutdown Jupyter kernel sessions
    try:
        await JupyterSandboxTool.shutdown_all()
    except Exception:
        pass


async def run_agent(
    workspace_dir: Path,
    task: str | None = None,
    initial_goal: str | None = None,
    sandbox_mode: bool = True,
    verify_api: bool = True,
    json_summary: bool = False,
    deep_think: bool = False,
    force_plan_start: bool = False,
    completion_gate_enabled: bool = True,
    goal_autopilot_enabled: bool = True,
) -> int:
    """Run Agent in interactive or non-interactive mode.

    Args:
        workspace_dir: Workspace directory path
        task: If provided, execute this task and exit (non-interactive mode)
        initial_goal: Optional goal objective to set before the first turn
        sandbox_mode: If True (default), enable Jupyter sandbox for Python code execution
        verify_api: If True, probe API connectivity before starting the session
        json_summary: If True in non-interactive mode, append a JSON execution summary
        deep_think: If True, enable thinking mode for the run
        force_plan_start: If True, require the next turn to publish a plan first
        completion_gate_enabled: If True, guard deliverable tasks from ending before artifact creation
        goal_autopilot_enabled: If True, continue active goals in --task mode within configured budgets
    """
    session_start = datetime.now()

    # 1. Load configuration from package directory
    config_path = Config.get_default_config_path()

    if not config_path.exists():
        # This shouldn't happen after _ensure_user_config, but just in case
        print(f"{Colors.RED}❌ Configuration file not found: {config_path}{Colors.RESET}")
        return 1

    try:
        config = Config.from_yaml(config_path)
    except FileNotFoundError:
        print(f"{Colors.RED}❌ Error: Configuration file not found: {config_path}{Colors.RESET}")
        return 1
    except ValueError as e:
        error_msg = str(e)
        if "API Key" in error_msg or "api_key" in error_msg or "empty" in error_msg.lower():
            # First-run or unconfigured — launch interactive setup
            if run_setup_wizard(config_path):
                # Retry loading after setup
                try:
                    config = Config.from_yaml(config_path)
                except Exception as e2:
                    print(f"{Colors.RED}❌ Error: {e2}{Colors.RESET}")
                    return 1
            else:
                return 1
        else:
            print(f"{Colors.RED}❌ Error: {e}{Colors.RESET}")
            print(f"{Colors.YELLOW}Please check: {config_path}{Colors.RESET}")
            return 1
    except Exception as e:
        print(f"{Colors.RED}❌ Error: Failed to load configuration file: {e}{Colors.RESET}")
        return 1

    try:
        workspace_profile = WorkspaceRegistry().get(workspace_dir)
    except WorkspaceRegistryError as exc:
        workspace_profile = None
        print(f"{Colors.YELLOW}⚠️  Workspace config unavailable: {exc}{Colors.RESET}")
    code_workspace = bool(
        workspace_profile is not None and workspace_profile.task_type == "code"
    )
    if code_workspace:
        print(f"{Colors.GREEN}✅ Workspace type: code{Colors.RESET}")

    # 2. Initialize LLM client
    from box_agent.retry import RetryConfig as RetryConfigBase

    # Convert configuration format
    retry_config = RetryConfigBase(
        enabled=config.llm.retry.enabled,
        max_retries=config.llm.retry.max_retries,
        initial_delay=config.llm.retry.initial_delay,
        max_delay=config.llm.retry.max_delay,
        exponential_base=config.llm.retry.exponential_base,
        retryable_exceptions=(Exception,),
    )

    # Create retry callback function to display retry information in terminal
    def on_retry(exception: Exception, attempt: int):
        """Retry callback function to display retry information"""
        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  LLM call failed (attempt {attempt}): {str(exception)}{Colors.RESET}")
        next_delay = retry_config.calculate_delay(attempt - 1)
        print(f"{Colors.DIM}   Retrying in {next_delay:.1f}s (attempt {attempt + 1})...{Colors.RESET}")

    # Convert provider string to LLMProvider enum
    provider = LLMProvider.ANTHROPIC if config.llm.provider.lower() == "anthropic" else LLMProvider.OPENAI

    llm_client = LLMClient(
        api_key=config.llm.api_key,
        provider=provider,
        api_base=config.llm.api_base,
        model=config.llm.model,
        retry_config=retry_config if config.llm.retry.enabled else None,
        max_output_tokens=config.llm.max_output_tokens,
        auth_file=config.llm.auth_file,
        timeout=config.llm.timeout,
    )

    # Set retry callback
    if config.llm.retry.enabled:
        llm_client.retry_callback = on_retry
        print(f"{Colors.GREEN}✅ LLM retry mechanism enabled (max {config.llm.retry.max_retries} retries){Colors.RESET}")

    # 2.5 Verify API connectivity with a lightweight test call (no retry)
    if verify_api:
        print(f"{Colors.DIM}Verifying API connection...{Colors.RESET}", end=" ", flush=True)
        try:
            from box_agent.retry import RetryConfig as VerifyRetryConfig
            # Use a temporary client with retry disabled to avoid long waits
            _verify_client = LLMClient(
                api_key=config.llm.api_key,
                provider=provider,
                api_base=config.llm.api_base,
                model=config.llm.model,
                retry_config=VerifyRetryConfig(enabled=False),
                max_output_tokens=config.llm.max_output_tokens,
                auth_file=config.llm.auth_file,
                timeout=config.llm.timeout,
            )
            await _probe_llm_api(_verify_client)
            print(f"{Colors.GREEN}OK{Colors.RESET}")
        except Exception as e:
            err_str = str(e)
            print(f"{Colors.RED}FAILED{Colors.RESET}")
            print(f"\n{Colors.RED}❌ API connection failed: {err_str}{Colors.RESET}")
            print()
            print(f"{Colors.DIM}  api_key:    {config.llm.api_key[:8]}...{Colors.RESET}")
            print(f"{Colors.DIM}  api_base:   {config.llm.api_base}{Colors.RESET}")
            print(f"{Colors.DIM}  provider:   {config.llm.provider}{Colors.RESET}")
            print(f"{Colors.DIM}  model:      {config.llm.model}{Colors.RESET}")
            print()
            if task:
                print(f"{Colors.YELLOW}Use `--no-verify-api` to skip the startup probe when you expect the first LLM call to handle connectivity.{Colors.RESET}")
                return 1
            # Offer to re-run setup wizard
            try:
                answer = input(f"{Colors.BRIGHT_CYAN}Would you like to reconfigure? [Y/n]: {Colors.RESET}").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
            if answer in ("", "y", "yes"):
                if run_setup_wizard(config_path):
                    # Retry loading config and verifying
                    try:
                        config = Config.from_yaml(config_path)
                        provider = LLMProvider.ANTHROPIC if config.llm.provider.lower() == "anthropic" else LLMProvider.OPENAI
                        llm_client = LLMClient(
                            api_key=config.llm.api_key,
                            provider=provider,
                            api_base=config.llm.api_base,
                            model=config.llm.model,
                            retry_config=retry_config if config.llm.retry.enabled else None,
                            max_output_tokens=config.llm.max_output_tokens,
                            auth_file=config.llm.auth_file,
                            timeout=config.llm.timeout,
                        )
                        if config.llm.retry.enabled:
                            llm_client.retry_callback = on_retry
                        print(f"{Colors.DIM}Verifying API connection...{Colors.RESET}", end=" ", flush=True)
                        _verify_client2 = LLMClient(
                            api_key=config.llm.api_key,
                            provider=provider,
                            api_base=config.llm.api_base,
                            model=config.llm.model,
                            retry_config=VerifyRetryConfig(enabled=False),
                            max_output_tokens=config.llm.max_output_tokens,
                            auth_file=config.llm.auth_file,
                            timeout=config.llm.timeout,
                        )
                        await _probe_llm_api(_verify_client2)
                        print(f"{Colors.GREEN}OK{Colors.RESET}")
                    except Exception as e2:
                        print(f"{Colors.RED}FAILED{Colors.RESET}")
                        print(f"\n{Colors.RED}❌ API connection still failed: {e2}{Colors.RESET}")
                        print(f"{Colors.YELLOW}Please check your configuration: {config_path}{Colors.RESET}")
                        return 1
                else:
                    return 1
            else:
                return 1
    else:
        print(f"{Colors.DIM}Skipping startup API verification (--no-verify-api).{Colors.RESET}")

    # 3. Initialize memory manager (before base tools, so tools get the reference)
    memory_mgr = None
    if config.agent.enable_memory:
        from box_agent.memory import MemoryManager

        memory_mgr = MemoryManager(
            memory_dir=config.agent.memory_dir,
            dedup_jaccard_threshold=config.agent.memory_dedup_jaccard,
        )

    # 3.4 One-time OpenClaw import
    if memory_mgr:
        try:
            await memory_mgr.import_openclaw(llm_client)
        except Exception:
            pass

    # 3.4.1 Memory maintenance (decay / archive cleanup / dedup / compact).
    # Off the critical path — the compact phase can issue a slow LLM call on
    # large CONTEXT.md. Fire-and-forget, same pattern as the background MCP
    # loader, so the REPL is responsive even if maintenance takes a while.
    maintainer_task: asyncio.Task | None = None
    if memory_mgr and config.agent.memory_maintainer_enabled:
        from box_agent.memory_maintainer import MemoryMaintainer

        async def _run_maintainer() -> None:
            try:
                await MemoryMaintainer(memory_mgr, config.agent, llm=llm_client).run_if_due()
            except Exception:
                pass

        maintainer_task = asyncio.create_task(_run_maintainer(), name="memory-maintainer")

    # 3.5 Memory extractor (lifecycle-triggered auto memory)
    memory_extractor = None
    if memory_mgr and config.agent.enable_memory_extraction:
        from box_agent.memory import MemoryExtractor

        memory_extractor = MemoryExtractor(
            llm=llm_client,
            memory_manager=memory_mgr,
            cooldown=config.agent.memory_extraction_cooldown,
            step_interval=config.agent.memory_extraction_step_interval,
        )

    # 3.5 Initialize base tools (independent of workspace). MCP loads in the background.
    # CLI keeps skill discovery inline so users still see the "Loading Claude Skills..."
    # status line; the ACP path defers it (see run_acp_server).
    tools, skill_loader, mcp_task, _skill_task = await initialize_base_tools(
        config, memory_manager=memory_mgr, llm=llm_client
    )
    cli_preloaded_skill_hashes: dict[str, str] = {}
    if skill_loader:
        from box_agent.tools.skill_tool import GetSkillTool

        tools = [
            GetSkillTool(
                skill_loader,
                preloaded_skill_hashes=cli_preloaded_skill_hashes,
            )
            if isinstance(tool, GetSkillTool)
            else tool
            for tool in tools
        ]

    # 4. Add workspace-dependent tools
    non_interactive = task is not None
    allow_full_access = config.tools.allow_full_access

    # Build PermissionEngine + GrantStore for CLI (parity with ACP)
    perm_engine = None
    grant_store = None
    if not non_interactive:
        from box_agent.tools.permissions import GrantStore
        grant_store = GrantStore()
    if not allow_full_access:
        from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine
        if grant_store is None:
            from box_agent.tools.permissions import GrantStore
            grant_store = GrantStore()
        # Honor officev3.permissions.filesystem (scope + allowed_directories)
        # when the block is present — same code path the ACP server uses.
        # Otherwise fall back to a default session_workspace policy rooted at
        # the workspace directory.
        if getattr(config.officev3, "_present", False):
            policy = CapabilityPolicy.from_config(config)
            if not policy.session_workspace_root:
                policy = policy.model_copy(update={"session_workspace_root": str(workspace_dir)})
        else:
            policy = CapabilityPolicy(session_workspace_root=str(workspace_dir))
        perm_engine = PermissionEngine(policy, workspace_dir, grant_store=grant_store)

    cli_env_context = build_cli_env_context()
    skill_runtime_context = build_skill_runtime_context(
        sandbox_mode=sandbox_mode,
        env_context=cli_env_context,
        shell_python_path=resolve_cli_shell_python(),
    )

    skill_scratch_dir = add_workspace_tools(
        tools, config, workspace_dir,
        sandbox_mode=sandbox_mode,
        allow_full_access=allow_full_access,
        non_interactive=non_interactive,
        llm=llm_client,
        permission_engine=perm_engine,
        skill_runtime_context=skill_runtime_context,
        skill_loader=skill_loader,
        capability_state_provider=(
            lambda: "loading"
            if mcp_task is not None and not mcp_task.done()
            else "ready"
        ),
        use_output_dir=not code_workspace,
        env_context=cli_env_context,
    )

    if not allow_full_access:
        print(f"{Colors.YELLOW}🔒 Safety mode: tools restricted to workspace ({workspace_dir}){Colors.RESET}")
    if non_interactive:
        print(f"{Colors.YELLOW}🔒 Non-interactive mode: commands requiring safety approval will be rejected{Colors.RESET}")

    # 5. Load System Prompt (with priority search)
    system_prompt_path = Config.find_config_file(config.agent.system_prompt_path)
    if system_prompt_path and system_prompt_path.exists():
        system_prompt = system_prompt_path.read_text(encoding="utf-8")
        print(f"{Colors.GREEN}✅ Loaded system prompt (from: {system_prompt_path}){Colors.RESET}")
    else:
        system_prompt = "You are Box-Agent, an intelligent assistant that can help users complete various tasks."
        print(f"{Colors.YELLOW}⚠️  System prompt not found, using default{Colors.RESET}")

    # 6. Inject Skills Metadata into System Prompt (Progressive Disclosure - Level 1)
    # NOTE: actual skill list is injected per-turn via SkillSelector below
    # (keyword-filtered against the cumulative user query). Here we just
    # replace the placeholder with a sentinel that the selector can find.
    if skill_loader:
        from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL
        system_prompt = system_prompt.replace("{SKILLS_METADATA}", SKILL_SLOT_SENTINEL)
        print(
            f"{Colors.GREEN}✅ {len(skill_loader.loaded_skills)} skills available "
            f"(injected per-turn via keyword filter){Colors.RESET}"
        )
    else:
        # Remove placeholder if skills not enabled
        system_prompt = system_prompt.replace("{SKILLS_METADATA}", "")

    # 6.5 Inject Sandbox info if enabled
    if sandbox_mode:
        system_prompt = system_prompt.replace(
            "{SANDBOX_INFO}",
            build_sandbox_info_prompt(use_output_dir=not code_workspace),
        )
        print(f"{Colors.GREEN}✅ Sandbox mode enabled with execute_code tool{Colors.RESET}")
    else:
        # Remove placeholder if sandbox not enabled
        system_prompt = system_prompt.replace("{SANDBOX_INFO}", "")

    system_prompt = system_prompt.replace(
        "{FILE_DELIVERY_INFO}",
        build_file_delivery_prompt(use_output_dir=not code_workspace),
    )

    if code_workspace:
        project_mode_prompt = (
            "## Project Workspace Mode\n"
            "- This session is editing an existing code/project workspace.\n"
            "- Do not create or use an `output/` folder unless the user explicitly asks for one.\n"
            "- Treat file edits, generated source files, tests, and build results in the project tree as the deliverable."
        )
        system_prompt = (
            f"{system_prompt.rstrip()}\n\n{project_mode_prompt}\n\n"
            f"{build_project_startup_context_prompt(workspace_dir)}"
        )
        code_prompt_path = Config.find_config_file(config.agent.code_prompt_path)
        if code_prompt_path and code_prompt_path.exists():
            code_prompt = code_prompt_path.read_text(encoding="utf-8").strip()
            if code_prompt:
                system_prompt = f"{system_prompt.rstrip()}\n\n{code_prompt}"
                print(
                    f"{Colors.GREEN}✅ Loaded code workspace prompt "
                    f"(from: {code_prompt_path}){Colors.RESET}"
                )
        else:
            print(f"{Colors.YELLOW}⚠️  Code workspace prompt not found{Colors.RESET}")

    system_prompt = (
        f"{system_prompt.rstrip()}\n\n{build_image_generation_prompt(config)}"
    )

    system_prompt = f"{system_prompt.rstrip()}\n\n{build_skill_runtime_prompt(skill_runtime_context)}"

    if cli_env_context is not None:
        from box_agent.acp.env_context import build_env_context_prompt

        env_prompt = build_env_context_prompt(cli_env_context)
        if env_prompt:
            system_prompt = f"{system_prompt.rstrip()}\n\n{env_prompt}"
            print(f"{Colors.GREEN}✅ Loaded CLI environment context{Colors.RESET}")

    # 6.6 Inject Memory context
    if memory_mgr:
        memory_block = await asyncio.to_thread(memory_mgr.recall)
        if memory_block:
            system_prompt = f"{system_prompt.rstrip()}\n\n{memory_block}"
            print(f"{Colors.GREEN}✅ Loaded memory context{Colors.RESET}")

    # 7. Create Agent
    from box_agent.hooks import load_hooks
    hooks = load_hooks(config.hooks.hooks) if config.hooks.hooks else None
    agent = Agent(
        llm_client=llm_client,
        system_prompt=system_prompt,
        tools=tools,
        max_steps=config.agent.max_steps,
        tool_limits=config.tool_limits,
        workspace_dir=str(workspace_dir),
        token_limit=config.llm.context_token_limit,
        hooks=hooks,
        thinking_enabled=deep_think,
        max_parallel_tools=config.agent.max_parallel_tools,
        parallel_tool_timeout_seconds=config.agent.parallel_tool_timeout_seconds,
        memory_promotion_enabled=config.agent.memory_promotion_proposal_enabled,
        memory_promotion_hit_threshold=config.agent.memory_promotion_hit_threshold,
        memory_promotion_cooldown_days=config.agent.memory_promotion_cooldown_days,
        truncation_continuation_enabled=config.agent.retry_on_suspected_truncation,
        max_truncation_continuations=config.agent.max_truncation_continuations,
        max_truncated_tool_call_retries=config.agent.max_truncated_tool_call_retries,
        truncated_tool_call_boost_cap=config.agent.truncated_tool_call_boost_cap,
        context_resource_dedup_enabled=config.agent.context_resource_dedup_enabled,
        deferred_mcp_loading_enabled=(
            config.tools.enable_mcp
            and config.tools.mcp.deferred_loading_enabled
        ),
    )

    restored_goal = _restore_cli_goal(agent, workspace_dir)
    if initial_goal and initial_goal.strip():
        restored_goal = agent.set_goal(initial_goal)
        _save_goal_state(workspace_dir, agent.goal)

    # Wire CLI permission negotiator (parity with ACP in-band negotiation)
    if grant_store is not None and not non_interactive:
        from box_agent.cli_permissions import CLIPermissionNegotiator
        agent.set_permission_negotiator(CLIPermissionNegotiator(grant_store))

    # Wire memory extractor
    if memory_extractor:
        agent.set_memory_extractor(memory_extractor)

    # Wire memory promotion negotiator (interactive prompts).
    # Non-interactive `--task` mode skips it to avoid blocking on stdin.
    if memory_mgr and config.agent.memory_promotion_proposal_enabled and not task:
        from box_agent.cli_memory_proposal import CLIMemoryProposalNegotiator
        agent.set_memory_proposal_negotiator(CLIMemoryProposalNegotiator(memory_mgr))

    def _set_agent_system_prompt(system_prompt: str) -> None:
        agent.set_system_prompt(system_prompt)

    # 7.5 Skill selector: filter skill metadata per turn based on cumulative user query
    skill_selector = None
    if skill_loader:
        from box_agent.tools.skill_loader import SkillSelector, move_skill_slot_to_end

        relocated_prompt = move_skill_slot_to_end(agent.messages[0].content)
        if relocated_prompt != agent.messages[0].content:
            _set_agent_system_prompt(relocated_prompt)
        skill_selector = SkillSelector(skill_loader)
        skill_selector.bind(agent.messages[0].content)
    cli_preloaded_skill_names: list[str] = []

    def _sync_cli_cache_fingerprint_context() -> None:
        agent.cache_fingerprint_context["filtered_skill_names"] = (
            list(skill_selector.matched_skill_names) if skill_selector is not None else []
        )
        agent.cache_fingerprint_context["preloaded_skill_names"] = list(
            cli_preloaded_skill_names
        )

    def _apply_skill_filter(user_input: str) -> tuple[str, ...]:
        if skill_selector is None:
            _sync_cli_cache_fingerprint_context()
            return ()
        new_prompt = skill_selector.update(user_input)
        if new_prompt is not None:
            _set_agent_system_prompt(new_prompt)
        _sync_cli_cache_fingerprint_context()
        return skill_selector.matched_skill_names

    def _apply_cli_auto_loaded_skills(completion_gate, user_input: str) -> None:
        if skill_loader is None or skill_selector is None:
            _sync_cli_cache_fingerprint_context()
            return
        preload_names = turn_preload_skill_names(
            skill_selector.matched_skill_names,
            completion_gate,
            cli_env_context,
            user_input,
        )
        if not preload_names and not cli_preloaded_skill_names:
            _sync_cli_cache_fingerprint_context()
            return
        result = build_auto_loaded_skills_prompt(
            skill_loader,
            agent.system_prompt,
            preload_names,
        )
        previous_skill_names = set(cli_preloaded_skill_names)
        cli_preloaded_skill_names[:] = list(result.loaded_names)
        cli_preloaded_skill_hashes.clear()
        cli_preloaded_skill_hashes.update(result.loaded_skill_hashes)
        _sync_cli_cache_fingerprint_context()
        for missing_name in result.missing_names:
            print(f"{Colors.YELLOW}⚠️  Skill preload target not found: {missing_name}{Colors.RESET}")
        if result.changed:
            _set_agent_system_prompt(result.system_prompt)
        unloaded_skill_names = previous_skill_names - set(result.loaded_names)
        if unloaded_skill_names:
            print(
                f"{Colors.DIM}Auto-unloaded skills: "
                f"{', '.join(sorted(unloaded_skill_names))}{Colors.RESET}"
            )
        if result.loaded_names and result.changed:
            print(
                f"{Colors.DIM}Auto-loaded skills: "
                f"{', '.join(result.loaded_names)}{Colors.RESET}"
            )

    def _build_cli_completion_gate(user_input: str):
        if not completion_gate_enabled:
            return None
        explicit_skill = resolve_explicit_skill_invocation(skill_loader, user_input)
        if (
            explicit_skill is not None
            and explicit_skill.workflow != CONTROLLED_PRESENTATION_WORKFLOW_KIND
        ):
            return build_external_skill_completion_gate(
                user_text=user_input,
                workspace_dir=workspace_dir,
                skill=explicit_skill,
                tool_limits=config.tool_limits,
            )
        return build_auto_completion_gate(
            user_input,
            workspace_dir,
            confirmed_presentation=(
                explicit_skill is not None
                and explicit_skill.workflow
                == CONTROLLED_PRESENTATION_WORKFLOW_KIND
            ),
            tool_limits=config.tool_limits,
        )

    async def _refresh_mcp_after_auth_change() -> None:
        results = await reconnect_auth_failed_mcp_servers_if_token_changed()
        if not results:
            return
        if not config.tools.mcp.deferred_loading_enabled:
            register_mcp_tools(agent.tools, get_all_mcp_tools())
        for result in results:
            name = result["name"]
            if result.get("success"):
                print(
                    f"{Colors.GREEN}✅ Reconnected MCP server '{name}' "
                    f"after login token refresh{Colors.RESET}"
                )
            else:
                print(
                    f"{Colors.YELLOW}⚠️  MCP reconnect failed for '{name}': "
                    f"{result.get('error') or 'unknown error'}{Colors.RESET}"
                )

    # 8. Display welcome information
    if not task:
        print_banner()
        print_session_info(agent, workspace_dir, config.llm.model)
        if restored_goal is not None:
            print(f"{Colors.DIM}Loaded workspace goal: {restored_goal.status} — {restored_goal.objective}{Colors.RESET}\n")

    # 8.5 Non-interactive mode: execute task and exit
    if task:
        print(f"\n{Colors.BRIGHT_BLUE}Agent{Colors.RESET} {Colors.DIM}›{Colors.RESET} {Colors.DIM}Executing task...{Colors.RESET}\n")
        # Block on MCP only when user is actually about to run
        loaded_mcp_tools = await await_mcp_tools(mcp_task)
        if not config.tools.mcp.deferred_loading_enabled:
            register_mcp_tools(agent.tools, loaded_mcp_tools)
        await _refresh_mcp_after_auth_change()
        _apply_skill_filter(task)
        completion_gate = _build_cli_completion_gate(task)
        _apply_cli_auto_loaded_skills(completion_gate, task)
        agent.add_user_message(task)
        if completion_gate is not None:
            patterns = ", ".join(completion_gate.required_changed_artifact_globs)
            print(f"{Colors.DIM}Completion gate enabled for deliverable artifacts: {patterns}{Colors.RESET}")
        ok = True
        error: str | None = None
        auto_continuations = 0
        auto_budget_exhausted = False
        auto_no_progress_turns = 0
        auto_no_progress_exhausted = False
        final_content = ""
        auto_enabled = (
            goal_autopilot_enabled
            and config.agent.goal_autopilot_enabled
            and config.agent.goal_autopilot_max_turns > 0
        )
        auto_started = perf_counter()
        try:
            final_content = await agent.run(
                force_plan_start=force_plan_start,
                completion_gate=completion_gate,
                current_turn_text=task,
            )
            while auto_enabled and should_continue_goal_autopilot(agent, agent.last_stop_reason):
                elapsed = perf_counter() - auto_started
                if (
                    auto_continuations >= config.agent.goal_autopilot_max_turns
                    or elapsed >= config.agent.goal_autopilot_max_seconds
                ):
                    auto_budget_exhausted = True
                    break
                if agent.goal is None:
                    break
                auto_continuations += 1
                print(
                    f"\n{Colors.DIM}Goal autopilot continuing "
                    f"{auto_continuations}/{config.agent.goal_autopilot_max_turns}...{Colors.RESET}\n"
                )
                agent.add_user_message(
                    goal_autopilot_prompt(
                        agent.goal,
                        auto_continuations,
                        config.agent.goal_autopilot_max_turns,
                    )
                )
                before_signature = goal_autopilot_progress_signature(agent.goal)
                final_content = await agent.run(completion_gate=completion_gate)
                after_signature = goal_autopilot_progress_signature(agent.goal)
                if should_continue_goal_autopilot(agent, agent.last_stop_reason):
                    if after_signature == before_signature:
                        auto_no_progress_turns += 1
                    else:
                        auto_no_progress_turns = 0
                    if (
                        config.agent.goal_autopilot_no_progress_turns > 0
                        and auto_no_progress_turns >= config.agent.goal_autopilot_no_progress_turns
                    ):
                        auto_no_progress_exhausted = True
                        break
            if agent.last_stop_reason == StopReason.ERROR.value:
                ok = False
                error = final_content.strip() or "Agent execution failed."
        except Exception as e:
            ok = False
            error = str(e)
            print(f"\n{Colors.RED}❌ Error: {e}{Colors.RESET}")
        finally:
            if auto_budget_exhausted and agent.goal is not None and agent.goal.status == "active":
                print(
                    f"\n{Colors.YELLOW}⚠️  Goal autopilot stopped after "
                    f"{auto_continuations} continuation(s); goal remains active.{Colors.RESET}"
                )
            if auto_no_progress_exhausted and agent.goal is not None and agent.goal.status == "active":
                print(
                    f"\n{Colors.YELLOW}⚠️  Goal autopilot stopped after "
                    f"{auto_no_progress_turns} continuation(s) without recorded goal progress.{Colors.RESET}"
                )
            _save_goal_state(workspace_dir, agent.goal)
            if skill_scratch_dir is not None:
                try:
                    cleanup_skill_scratch_dir(skill_scratch_dir)
                except Exception as cleanup_error:
                    print(
                        f"{Colors.YELLOW}⚠️  Failed to clean Skill scratch: "
                        f"{cleanup_error}{Colors.RESET}",
                        file=sys.stderr,
                    )
            print_stats(agent, session_start)
            if json_summary:
                paused = agent.last_stop_reason == StopReason.CHECKPOINT_PAUSED.value
                _json_print({
                    "ok": ok,
                    "error": error,
                    "runStatus": "paused" if paused else ("completed" if ok else "error"),
                    "completed": ok and not paused,
                    "recoverable": paused,
                    "checkpoint": agent.last_checkpoint if paused else None,
                    "workspace": str(workspace_dir),
                    "task": task,
                    "goal": goal_payload(agent.goal),
                    "goalAutopilot": {
                        "enabled": auto_enabled,
                        "continuations": auto_continuations,
                        "budgetExhausted": auto_budget_exhausted,
                        "noProgressExhausted": auto_no_progress_exhausted,
                        "noProgressTurns": auto_no_progress_turns,
                        "lastStopReason": agent.last_stop_reason,
                    },
                    "stats": _session_stats(agent, session_start),
                })

        # Cleanup MCP connections
        await _quiet_cleanup()
        return 0 if ok else 1

    # 9. Setup prompt_toolkit session
    # Command completer
    command_completer = WordCompleter(
        [
            "/help",
            "/clear",
            "/clear_all",
            "/history",
            "/stats",
            "/sandbox_status",
            "/log",
            "/goal",
            "/goal pause",
            "/goal resume",
            "/goal clear",
            "/goal complete",
            "/memory",
            "/exit",
            "/quit",
            "/q",
        ],
        ignore_case=True,
        sentence=True,
    )

    # Custom style for prompt
    prompt_style = Style.from_dict(
        {
            "prompt": "#00ff00 bold",  # Green and bold
            "separator": "#666666",  # Gray
        }
    )

    # Custom key bindings
    kb = KeyBindings()

    @kb.add("c-u")  # Ctrl+U: Clear current line
    def _(event):
        """Clear the current input line"""
        event.current_buffer.reset()

    @kb.add("c-l")  # Ctrl+L: Clear screen (optional bonus)
    def _(event):
        """Clear the screen"""
        event.app.renderer.clear()

    @kb.add("c-j")  # Ctrl+J (对应 Ctrl+Enter)
    def _(event):
        """Insert a newline"""
        event.current_buffer.insert_text("\n")

    # Create prompt session with history and auto-suggest
    # Use FileHistory for persistent history across sessions (stored in user's home directory)
    history_file = Path.home() / ".box-agent" / ".history"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=command_completer,
        style=prompt_style,
        key_bindings=kb,
    )

    # 10. Interactive loop
    force_plan_next_turn = force_plan_start
    while True:
        try:
            # Build prompt with optional sandbox session_id
            if sandbox_mode:
                # Try to get current session_id from sandbox tool
                sandbox_session_id = None
                for tool in tools:
                    if isinstance(tool, JupyterSandboxTool):
                        sandbox_session_id = tool.session_id
                        break
                if sandbox_session_id:
                    prompt_parts = [
                        ("class:prompt", f"You [{sandbox_session_id}]"),
                        ("", " › "),
                    ]
                else:
                    prompt_parts = [
                        ("class:prompt", "You"),
                        ("", " › "),
                    ]
            else:
                prompt_parts = [
                    ("class:prompt", "You"),
                    ("", " › "),
                ]

            # Get user input using prompt_toolkit
            user_input = await session.prompt_async(
                prompt_parts,
                multiline=False,
                enable_history_search=True,
            )
            user_input = user_input.strip()

            if not user_input:
                continue

            # Handle commands
            if user_input.startswith("/"):
                command = user_input.lower()

                if command in ["/exit", "/quit", "/q"]:
                    print(f"\n{Colors.BRIGHT_YELLOW}👋 Goodbye! Thanks for using Box Agent{Colors.RESET}\n")
                    print_stats(agent, session_start)
                    break

                elif command == "/help":
                    print_help()
                    continue

                elif command == "/clear":
                    # Clear message history but keep system prompt
                    cleared_count = agent.clear_history()
                    print(f"{Colors.GREEN}✅ Cleared {cleared_count} messages, starting new session{Colors.RESET}\n")
                    if sandbox_mode:
                        print(f"{Colors.YELLOW}⚠️  Note: /clear does not clear sandbox state.{Colors.RESET}")
                        print(f"{Colors.DIM}   Use /clear_all to clear both messages and sandbox.{Colors.RESET}\n")
                    continue

                elif command == "/clear_all":
                    # Clear both message history AND sandbox kernel
                    cleared_count = agent.clear_history()
                    if sandbox_mode:
                        await JupyterSandboxTool.shutdown_all()
                        print(f"{Colors.GREEN}✅ Cleared {cleared_count} messages and shut down sandbox kernel{Colors.RESET}\n")
                    else:
                        print(f"{Colors.GREEN}✅ Cleared {cleared_count} messages{Colors.RESET}\n")
                    continue

                elif command == "/history":
                    print(f"\n{Colors.BRIGHT_CYAN}Current session message count: {len(agent.messages)}{Colors.RESET}\n")
                    continue

                elif command == "/stats":
                    print_stats(agent, session_start)
                    continue

                elif command == "/sandbox_status":
                    if sandbox_mode:
                        # Find the sandbox status tool and execute it
                        for tool in tools:
                            if isinstance(tool, SandboxStatusTool):
                                result = await tool.invoke({})
                                if result.success:
                                    print(f"\n{Colors.BRIGHT_CYAN}{result.content}{Colors.RESET}\n")
                                else:
                                    print(f"{Colors.RED}❌ {result.error}{Colors.RESET}\n")
                                break
                    else:
                        print(f"{Colors.YELLOW}⚠️  Sandbox mode not enabled{Colors.RESET}\n")
                    continue

                elif command == "/log" or command.startswith("/log "):
                    # Parse /log command
                    parts = user_input.split(maxsplit=1)
                    if len(parts) == 1:
                        # /log - show log directory
                        show_log_directory(open_file_manager=True)
                    else:
                        # /log <filename> - read specific log file
                        filename = parts[1].strip("\"'")
                        read_log_file(filename)
                    continue

                elif command == "/goal" or command.startswith("/goal "):
                    handle_goal_command(agent, user_input)
                    _save_goal_state(workspace_dir, agent.goal)
                    continue

                elif command == "/memory" or command.startswith("/memory "):
                    parts = user_input.split(maxsplit=1)
                    sub = parts[1].strip().lower() if len(parts) > 1 else ""
                    if sub == "review":
                        if not memory_mgr:
                            print(f"{Colors.YELLOW}⚠️  Memory disabled in config.{Colors.RESET}\n")
                        else:
                            from box_agent.cli_memory_proposal import CLIMemoryProposalNegotiator
                            from box_agent.events import MemoryProposalEvent, MemoryPromotionCandidate
                            entries = await asyncio.to_thread(
                                memory_mgr.list_promotion_candidates,
                                hit_threshold=config.agent.memory_promotion_hit_threshold,
                                cooldown_days=0,  # manual review bypasses cooldown
                            )
                            if not entries:
                                print(f"{Colors.DIM}🧠 暂无可升级到核心记忆的候选条目。{Colors.RESET}\n")
                            else:
                                await asyncio.to_thread(
                                    memory_mgr.mark_proposed,
                                    [e.id for e in entries],
                                )
                                evt = MemoryProposalEvent(
                                    candidates=tuple(
                                        MemoryPromotionCandidate(
                                            entry_id=e.id,
                                            content=e.content,
                                            hits=e.hits,
                                            confidence=e.confidence,
                                        ) for e in entries
                                    )
                                )
                                await CLIMemoryProposalNegotiator(memory_mgr).negotiate(evt)
                    else:
                        print(f"{Colors.DIM}用法: /memory review — 审阅可升级到核心记忆的候选条目{Colors.RESET}\n")
                    continue

                else:
                    print(f"{Colors.RED}❌ Unknown command: {user_input}{Colors.RESET}")
                    print(f"{Colors.DIM}Type /help to see available commands{Colors.RESET}\n")
                    continue

            # Normal conversation - exit check
            if user_input.lower() in ["exit", "quit", "q"]:
                print(f"\n{Colors.BRIGHT_YELLOW}👋 Goodbye! Thanks for using Box Agent{Colors.RESET}\n")
                print_stats(agent, session_start)
                break

            # Run Agent with Esc cancellation support
            # Finish background MCP discovery. Deferred mode leaves ordinary
            # MCP tools catalog-only; legacy eager mode registers them here.
            loaded_mcp_tools = await await_mcp_tools(mcp_task)
            if not config.tools.mcp.deferred_loading_enabled:
                register_mcp_tools(agent.tools, loaded_mcp_tools)
            await _refresh_mcp_after_auth_change()
            mcp_task = None  # clear so we don't re-await the cached result each turn

            print(
                f"\n{Colors.BRIGHT_BLUE}Agent{Colors.RESET} {Colors.DIM}›{Colors.RESET} "
                f"{Colors.DIM}Thinking... (Esc to cancel){Colors.RESET}\n"
            )
            _apply_skill_filter(user_input)
            preload_gate = _build_cli_completion_gate(user_input)
            _apply_cli_auto_loaded_skills(preload_gate, user_input)
            agent.add_user_message(user_input)

            # Create cancellation event
            cancel_event = asyncio.Event()
            agent.cancel_event = cancel_event

            esc_listener_stop = threading.Event()
            esc_cancelled = [False]

            def esc_key_listener():
                """Listen for Esc key in a separate thread."""
                if platform.system() == "Windows":
                    try:
                        import msvcrt

                        while not esc_listener_stop.is_set():
                            if msvcrt.kbhit():
                                char = msvcrt.getch()
                                if char == b"\x1b":  # Esc
                                    print(f"\n{Colors.BRIGHT_YELLOW}⏹️  Esc pressed, cancelling...{Colors.RESET}")
                                    esc_cancelled[0] = True
                                    cancel_event.set()
                                    break
                            esc_listener_stop.wait(0.05)
                    except Exception:
                        pass
                    return

                # Unix/macOS
                try:
                    import select
                    import termios
                    import tty

                    fd = sys.stdin.fileno()
                    old_settings = termios.tcgetattr(fd)

                    try:
                        tty.setcbreak(fd)
                        while not esc_listener_stop.is_set():
                            rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
                            if rlist:
                                char = sys.stdin.read(1)
                                if char == "\x1b":  # Esc
                                    print(f"\n{Colors.BRIGHT_YELLOW}⏹️  Esc pressed, cancelling...{Colors.RESET}")
                                    esc_cancelled[0] = True
                                    cancel_event.set()
                                    break
                    finally:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass

            esc_thread = threading.Thread(target=esc_key_listener, daemon=True)
            esc_thread.start()

            try:
                agent_task = asyncio.create_task(
                    agent.run(
                        force_plan_start=force_plan_next_turn,
                        completion_gate=preload_gate,
                        current_turn_text=user_input,
                    )
                )
                force_plan_next_turn = False

                while not agent_task.done():
                    if esc_cancelled[0]:
                        cancel_event.set()
                    await asyncio.sleep(0.1)

                _ = agent_task.result()

            except asyncio.CancelledError:
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  Agent execution cancelled{Colors.RESET}")
            finally:
                agent.cancel_event = None
                esc_listener_stop.set()
                esc_thread.join(timeout=0.2)
                _save_goal_state(workspace_dir, agent.goal)
                if skill_scratch_dir is not None:
                    try:
                        cleanup_skill_scratch_dir(skill_scratch_dir)
                    except Exception as cleanup_error:
                        print(
                            f"{Colors.YELLOW}⚠️  Failed to clean Skill scratch: "
                            f"{cleanup_error}{Colors.RESET}",
                            file=sys.stderr,
                        )

            # Visual separation
            print(f"\n{Colors.DIM}{'─' * 60}{Colors.RESET}\n")

        except EOFError:
            print(
                f"\n{Colors.BRIGHT_YELLOW}👋 Goodbye! Thanks for using Box Agent"
                f"{Colors.RESET}\n"
            )
            print_stats(agent, session_start)
            break

        except KeyboardInterrupt:
            print(f"\n\n{Colors.BRIGHT_YELLOW}👋 Interrupt signal detected, exiting...{Colors.RESET}\n")
            print_stats(agent, session_start)
            break

        except Exception as e:
            print(f"\n{Colors.RED}❌ Error: {e}{Colors.RESET}")
            print(f"{Colors.DIM}{'─' * 60}{Colors.RESET}\n")

    # 11. Cleanup MCP connections
    await _quiet_cleanup()
    return 0


def main() -> int:
    """Main entry point for CLI"""
    # Parse command line arguments
    args = parse_args()

    # Handle log subcommand
    if args.command == "log":
        if args.filename:
            read_log_file(args.filename)
        else:
            show_log_directory(open_file_manager=True)
        return 0

    # Handle setup subcommand
    if args.command == "setup":
        cmd_setup()
        return 0

    # Handle config subcommand
    if args.command == "config":
        get_key = args.get_key or args.key
        return cmd_config(
            edit=args.edit,
            get_key=get_key,
            set_pair=tuple(args.set_pair) if args.set_pair else None,
            list_all=args.list,
            json_output=args.json,
            show_secrets=args.show_secrets,
        )

    # Handle doctor subcommand
    if args.command == "doctor":
        return asyncio.run(cmd_doctor(json_output=args.json))

    # Handle install-browser subcommand
    if args.command == "install-browser":
        asyncio.run(cmd_install_browser())
        return 0

    # Handle install-node subcommand
    if args.command == "install-node":
        cmd_install_node(version=args.version)
        return 0

    workspace_dir = _workspace_from_args(args)
    if args.command == "goal":
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return cmd_goal(
            workspace_dir,
            action=args.action,
            text=args.text,
            evidence=args.evidence,
            progress=args.progress,
            json_output=args.json,
        )

    # Ensure user config exists; run setup wizard on first launch
    config_path = Config._ensure_user_config()
    try:
        config_check = Config.from_yaml(config_path)
        # If key is still the placeholder, treat as unconfigured
        if config_check.llm.api_key in ("YOUR_API_KEY_HERE", ""):
            print(f"{Colors.BRIGHT_CYAN}First-time setup detected. Let's configure Box Agent.{Colors.RESET}\n")
            if not run_setup_wizard(config_path):
                return 1
    except Exception:
        # Config can't be parsed or key is missing — run wizard
        print(f"{Colors.BRIGHT_CYAN}First-time setup detected. Let's configure Box Agent.{Colors.RESET}\n")
        if not run_setup_wizard(config_path):
            return 1

    # Ensure workspace directory exists
    workspace_dir.mkdir(parents=True, exist_ok=True)
    try:
        registry = WorkspaceRegistry()
        requested_workspace_type = getattr(args, "workspace_type", None)
        if requested_workspace_type:
            registry.set(workspace_dir, requested_workspace_type)
        workspace_profile = registry.get(workspace_dir)
    except WorkspaceRegistryError as exc:
        print(f"{Colors.RED}❌ Workspace config error: {exc}{Colors.RESET}")
        return 1
    if workspace_profile is None or workspace_profile.task_type != "code":
        # General tasks keep their canonical output directory. Code workspaces
        # edit the project tree directly and must not create it implicitly.
        ensure_output_dir(workspace_dir)

    # Run the agent (config always loaded from package directory)
    try:
        return asyncio.run(
            run_agent(
                workspace_dir,
                task=args.task,
                initial_goal=args.goal,
                sandbox_mode=not args.no_sandbox,
                verify_api=not args.no_verify_api,
                json_summary=args.json,
                deep_think=args.deep_think,
                force_plan_start=args.force_plan_start,
                completion_gate_enabled=not args.no_completion_gate,
                goal_autopilot_enabled=not args.no_goal_autopilot,
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
