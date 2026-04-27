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
import platform
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from box_agent import LLMClient, __version__
from box_agent.agent import Agent
from box_agent.config import Config
from box_agent.schema import LLMProvider
from box_agent.tools.base import Tool
from box_agent.tools.jupyter_tool import JupyterSandboxTool, SandboxStatusTool
from box_agent.tools.mcp_loader import cleanup_mcp_connections
from box_agent.tools.setup import add_workspace_tools, await_mcp_tools, initialize_base_tools
from box_agent.utils import calculate_display_width


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
    import yaml

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
  box-agent setup                        # Run first-time setup wizard
  box-agent config                       # Show current configuration
  box-agent config --edit                # Open config file in editor
  box-agent doctor                       # Check environment and connectivity
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
        "--task",
        "-t",
        type=str,
        default=None,
        help="Execute a task non-interactively and exit",
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

    # doctor subcommand
    subparsers.add_parser("doctor", help="Check environment and connectivity")

    # install-browser subcommand
    subparsers.add_parser(
        "install-browser",
        help="Install Chromium for Playwright MCP browser tools (~200MB)",
    )

    return parser.parse_args()


def cmd_setup():
    """Run the first-time setup wizard."""
    config_path = Config._ensure_user_config()
    print(f"{Colors.DIM}Config file: {config_path}{Colors.RESET}\n")
    run_setup_wizard(config_path)


def cmd_config(edit: bool = False):
    """Show current configuration or open it in an editor."""
    config_path = Config.find_config_file("config.yaml")
    if not config_path:
        print(f"{Colors.YELLOW}No config.yaml found. Run `box-agent setup` first.{Colors.RESET}")
        return

    print(f"{Colors.BOLD}Config file:{Colors.RESET} {config_path}\n")

    # Show key settings
    try:
        config = Config.from_yaml(config_path)
        masked_key = config.llm.api_key[:4] + "****" + config.llm.api_key[-4:] if len(config.llm.api_key) > 8 else "****"
        print(f"  api_base : {config.llm.api_base}")
        print(f"  provider : {config.llm.provider}")
        print(f"  model    : {config.llm.model}")
        print(f"  api_key  : {masked_key}")
    except Exception as e:
        print(f"{Colors.RED}  (could not parse config: {e}){Colors.RESET}")

    if edit:
        import os

        editor = os.environ.get("EDITOR")
        if not editor:
            editor = "open" if platform.system() == "Darwin" else "vi"
        print(f"\n{Colors.DIM}Opening with {editor}...{Colors.RESET}")
        subprocess.run([editor, str(config_path)])


async def cmd_doctor():
    """Check environment health: config, API, sandbox, MCP."""
    print(f"{Colors.BOLD}Box Agent Doctor{Colors.RESET}\n")

    # 1. Config
    config_path = Config.find_config_file("config.yaml")
    config = None
    if not config_path:
        print(f"  ❌ Config    — config.yaml not found (run `box-agent setup`)")
    else:
        try:
            config = Config.from_yaml(config_path)
            print(f"  ✅ Config    — {config_path}")
        except Exception as e:
            print(f"  ❌ Config    — parse error: {e}")

    # 2. API connectivity
    if config:
        try:
            from box_agent.retry import RetryConfig as DoctorRetryConfig
            from box_agent.schema import LLMProvider as LP, Message

            provider = LP.ANTHROPIC if config.llm.provider.lower() == "anthropic" else LP.OPENAI
            no_retry = DoctorRetryConfig(enabled=False, max_retries=0)
            client = LLMClient(
                api_key=config.llm.api_key,
                provider=provider,
                api_base=config.llm.api_base,
                model=config.llm.model,
                retry_config=no_retry,
            )
            from box_agent.schema import Message

            messages = [Message(role="user", content="hi")]
            response = await client.generate(messages)
            if response and response.content:
                print(f"  ✅ API       — {config.llm.api_base} ({config.llm.model})")
            else:
                print(f"  ❌ API       — empty response from {config.llm.api_base}")
        except Exception as e:
            print(f"  ❌ API       — {e}")
    else:
        print(f"  ⏭️  API       — skipped (no valid config)")

    # 3. Sandbox (Jupyter)
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
            print(f"  ✅ Sandbox   — jupyter_client OK, kernel spec found")
        else:
            print(f"  ⚠️  Sandbox   — jupyter_client OK, but kernel spec 'mini-agent-sandbox' not found")
    except ImportError:
        print(f"  ❌ Sandbox   — jupyter_client not installed")

    # 4. MCP
    mcp_path = Config.find_config_file("mcp.json")
    if mcp_path:
        print(f"  ✅ MCP       — {mcp_path}")
    else:
        print(f"  ⚠️  MCP       — mcp.json not found (optional)")

    # 5. Browser runtime (Playwright)
    _doctor_check_browser()

    print()


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


def _doctor_check_browser() -> None:
    """Check whether Node.js (npx) and Playwright Chromium are available."""
    npx = shutil.which("npx")
    if not npx:
        print(f"  ⚠️  Browser   — Node.js/npx not found (optional; needed for Playwright MCP)")
        return

    try:
        dry_run = subprocess.run(
            [npx, "-y", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_playwright_env(),
        )
    except subprocess.TimeoutExpired:
        print(f"  ⚠️  Browser   — playwright dry-run timed out")
        return

    combined = (dry_run.stdout or "") + (dry_run.stderr or "")
    browsers_path = _playwright_env()["PLAYWRIGHT_BROWSERS_PATH"]
    # Playwright prints "browser: chromium ... <install location>" and omits
    # the "Install location" section when already installed. Use "Download url"
    # presence as the "needs download" signal.
    if dry_run.returncode == 0 and "Download url" not in combined:
        print(f"  ✅ Browser   — Chromium installed ({browsers_path})")
    else:
        print(f"  ⚠️  Browser   — Chromium not installed in {browsers_path}")
        print(f"                 (run `box-agent install-browser`)")


async def cmd_install_browser() -> None:
    """Install Chromium for Playwright MCP, then enable the playwright entry in mcp.json."""
    print(f"{Colors.BOLD}Box Agent · Install Browser Runtime{Colors.RESET}\n")

    npx = shutil.which("npx")
    if not npx:
        print(f"{Colors.RED}❌ `npx` not found.{Colors.RESET}")
        print(f"{Colors.DIM}Install Node.js ≥ 18 first: https://nodejs.org/{Colors.RESET}")
        sys.exit(1)

    browsers_path = _default_browsers_path()
    browsers_path.mkdir(parents=True, exist_ok=True)

    print(f"{Colors.DIM}Target: {browsers_path}{Colors.RESET}")
    print(f"{Colors.DIM}Running: npx -y playwright install chromium (~200MB){Colors.RESET}\n")
    try:
        result = subprocess.run(
            [npx, "-y", "playwright", "install", "chromium"],
            check=False,
            env=_playwright_env(),
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
    """Flip `mcpServers.playwright.disabled` to false, adding the entry if missing."""
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


async def run_agent(workspace_dir: Path, task: str = None, sandbox_mode: bool = True):
    """Run Agent in interactive or non-interactive mode.

    Args:
        workspace_dir: Workspace directory path
        task: If provided, execute this task and exit (non-interactive mode)
        sandbox_mode: If True (default), enable Jupyter sandbox for Python code execution
    """
    session_start = datetime.now()

    # 1. Load configuration from package directory
    config_path = Config.get_default_config_path()

    if not config_path.exists():
        # This shouldn't happen after _ensure_user_config, but just in case
        print(f"{Colors.RED}❌ Configuration file not found: {config_path}{Colors.RESET}")
        return

    try:
        config = Config.from_yaml(config_path)
    except FileNotFoundError:
        print(f"{Colors.RED}❌ Error: Configuration file not found: {config_path}{Colors.RESET}")
        return
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
                    return
            else:
                return
        else:
            print(f"{Colors.RED}❌ Error: {e}{Colors.RESET}")
            print(f"{Colors.YELLOW}Please check: {config_path}{Colors.RESET}")
            return
    except Exception as e:
        print(f"{Colors.RED}❌ Error: Failed to load configuration file: {e}{Colors.RESET}")
        return

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
    )

    # Set retry callback
    if config.llm.retry.enabled:
        llm_client.retry_callback = on_retry
        print(f"{Colors.GREEN}✅ LLM retry mechanism enabled (max {config.llm.retry.max_retries} retries){Colors.RESET}")

    # 2.5 Verify API connectivity with a lightweight test call (no retry)
    print(f"{Colors.DIM}Verifying API connection...{Colors.RESET}", end=" ", flush=True)
    try:
        from box_agent.retry import RetryConfig as VerifyRetryConfig
        from box_agent.schema import Message as Msg
        # Use a temporary client with retry disabled to avoid long waits
        _verify_client = LLMClient(
            api_key=config.llm.api_key,
            provider=provider,
            api_base=config.llm.api_base,
            model=config.llm.model,
            retry_config=VerifyRetryConfig(enabled=False),
        )
        await _verify_client.generate(
            messages=[Msg(role="user", content="hi")],
        )
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
        # Offer to re-run setup wizard
        try:
            answer = input(f"{Colors.BRIGHT_CYAN}Would you like to reconfigure? [Y/n]: {Colors.RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
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
                    )
                    await _verify_client2.generate(messages=[Msg(role="user", content="hi")])
                    print(f"{Colors.GREEN}OK{Colors.RESET}")
                except Exception as e2:
                    print(f"{Colors.RED}FAILED{Colors.RESET}")
                    print(f"\n{Colors.RED}❌ API connection still failed: {e2}{Colors.RESET}")
                    print(f"{Colors.YELLOW}Please check your configuration: {config_path}{Colors.RESET}")
                    return
            else:
                return
        else:
            return

    # 3. Initialize memory manager (before base tools, so tools get the reference)
    memory_mgr = None
    if config.agent.enable_memory:
        from box_agent.memory import MemoryManager

        memory_mgr = MemoryManager(memory_dir=config.agent.memory_dir)

    # 3.4 One-time OpenClaw import
    if memory_mgr:
        try:
            await memory_mgr.import_openclaw(llm_client)
        except Exception:
            pass

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
    tools, skill_loader, mcp_task = await initialize_base_tools(config, memory_manager=memory_mgr, llm=llm_client)

    # 4. Add workspace-dependent tools
    non_interactive = task is not None
    allow_full_access = config.tools.allow_full_access

    # Build PermissionEngine + GrantStore for CLI (parity with ACP)
    perm_engine = None
    grant_store = None
    if not allow_full_access:
        from box_agent.tools.permissions import CapabilityPolicy, GrantStore, PermissionEngine
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

    add_workspace_tools(
        tools, config, workspace_dir,
        sandbox_mode=sandbox_mode,
        allow_full_access=allow_full_access,
        non_interactive=non_interactive,
        llm=llm_client,
        permission_engine=perm_engine,
    )

    if not allow_full_access:
        print(f"{Colors.YELLOW}🔒 Safety mode: tools restricted to workspace ({workspace_dir}){Colors.RESET}")
    if non_interactive:
        print(f"{Colors.YELLOW}🔒 Non-interactive mode: dangerous commands will be rejected{Colors.RESET}")

    # 5. Load System Prompt (with priority search)
    system_prompt_path = Config.find_config_file(config.agent.system_prompt_path)
    if system_prompt_path and system_prompt_path.exists():
        system_prompt = system_prompt_path.read_text(encoding="utf-8")
        print(f"{Colors.GREEN}✅ Loaded system prompt (from: {system_prompt_path}){Colors.RESET}")
    else:
        system_prompt = "You are Box-Agent, an intelligent assistant that can help users complete various tasks."
        print(f"{Colors.YELLOW}⚠️  System prompt not found, using default{Colors.RESET}")

    # 6. Inject Skills Metadata into System Prompt (Progressive Disclosure - Level 1)
    if skill_loader:
        skills_metadata = skill_loader.get_skills_metadata_prompt()
        if skills_metadata:
            # Replace placeholder with actual metadata
            system_prompt = system_prompt.replace("{SKILLS_METADATA}", skills_metadata)
            print(f"{Colors.GREEN}✅ Injected {len(skill_loader.loaded_skills)} skills metadata into system prompt{Colors.RESET}")
        else:
            # Remove placeholder if no skills
            system_prompt = system_prompt.replace("{SKILLS_METADATA}", "")
    else:
        # Remove placeholder if skills not enabled
        system_prompt = system_prompt.replace("{SKILLS_METADATA}", "")

    # 6.5 Inject Sandbox info if enabled
    if sandbox_mode:
        sandbox_info = """
## Sandbox Execution Mode (Enabled)

You have access to the `execute_code` tool which runs Python code in an isolated Jupyter kernel.

**When to use execute_code:**
- Data analysis and visualization (pandas, matplotlib, seaborn)
- Processing files (CSV, Excel, JSON, images)
- Document operations (Excel, Word, PDF, PowerPoint)
- Running Python scripts with persistent state
- Complex calculations requiring multiple steps

**Sandbox workspace:** Code runs in an isolated directory. Files saved are stored in the sandbox workspace.

**Best practices:**
- Break complex analysis into smaller code blocks
- Use print() to output intermediate results
- Clean up large data structures when done
- Check for errors after each step

**Available packages:** pandas, numpy, matplotlib, seaborn, scikit-learn, openpyxl, xlrd, python-docx, pypdf, pdfplumber, reportlab, python-pptx, and more via standard library.
"""
        system_prompt = system_prompt.replace("{SANDBOX_INFO}", sandbox_info)
        print(f"{Colors.GREEN}✅ Sandbox mode enabled with execute_code tool{Colors.RESET}")
    else:
        # Remove placeholder if sandbox not enabled
        system_prompt = system_prompt.replace("{SANDBOX_INFO}", "")

    # 6.6 Inject Memory context
    if memory_mgr:
        memory_block = memory_mgr.recall()
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
        workspace_dir=str(workspace_dir),
        hooks=hooks,
    )

    # Wire CLI permission negotiator (parity with ACP in-band negotiation)
    if grant_store is not None and not non_interactive:
        from box_agent.cli_permissions import CLIPermissionNegotiator
        agent._permission_negotiator = CLIPermissionNegotiator(grant_store)

    # Wire memory extractor
    if memory_extractor:
        agent._memory_extractor = memory_extractor

    # 8. Display welcome information
    if not task:
        print_banner()
        print_session_info(agent, workspace_dir, config.llm.model)

    # 8.5 Non-interactive mode: execute task and exit
    if task:
        print(f"\n{Colors.BRIGHT_BLUE}Agent{Colors.RESET} {Colors.DIM}›{Colors.RESET} {Colors.DIM}Executing task...{Colors.RESET}\n")
        # Block on MCP only when user is actually about to run
        for t in await await_mcp_tools(mcp_task):
            agent.tools.setdefault(t.name, t)
        agent.add_user_message(task)
        try:
            await agent.run()
        except Exception as e:
            print(f"\n{Colors.RED}❌ Error: {e}{Colors.RESET}")
        finally:
            print_stats(agent, session_start)

        # Cleanup MCP connections
        await _quiet_cleanup()
        return

    # 9. Setup prompt_toolkit session
    # Command completer
    command_completer = WordCompleter(
        ["/help", "/clear", "/clear_all", "/history", "/stats", "/sandbox_status", "/log", "/exit", "/quit", "/q"],
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
    while True:
        try:
            # Build prompt with optional sandbox session_id
            if sandbox_mode:
                # Try to get current session_id from sandbox tool
                sandbox_session_id = None
                for tool in tools:
                    if isinstance(tool, JupyterSandboxTool):
                        sandbox_session_id = tool._session_id
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
                    old_count = len(agent.messages)
                    agent.messages = [agent.messages[0]]  # Keep only system message
                    print(f"{Colors.GREEN}✅ Cleared {old_count - 1} messages, starting new session{Colors.RESET}\n")
                    if sandbox_mode:
                        print(f"{Colors.YELLOW}⚠️  Note: /clear does not clear sandbox state.{Colors.RESET}")
                        print(f"{Colors.DIM}   Use /clear_all to clear both messages and sandbox.{Colors.RESET}\n")
                    continue

                elif command == "/clear_all":
                    # Clear both message history AND sandbox kernel
                    old_count = len(agent.messages)
                    agent.messages = [agent.messages[0]]  # Keep only system message
                    if sandbox_mode:
                        await JupyterSandboxTool.shutdown_all()
                        print(f"{Colors.GREEN}✅ Cleared {old_count - 1} messages and shut down sandbox kernel{Colors.RESET}\n")
                    else:
                        print(f"{Colors.GREEN}✅ Cleared {old_count - 1} messages{Colors.RESET}\n")
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
                                result = await tool.execute()
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

                else:
                    print(f"{Colors.RED}❌ Unknown command: {user_input}{Colors.RESET}")
                    print(f"{Colors.DIM}Type /help to see available commands{Colors.RESET}\n")
                    continue

            # Normal conversation - exit check
            if user_input.lower() in ["exit", "quit", "q"]:
                print(f"\n{Colors.BRIGHT_YELLOW}👋 Goodbye! Thanks for using Box Agent{Colors.RESET}\n")
                print_stats(agent, session_start)
                break

            # Run Agent with cancellation + in-stream injection support
            # Ensure background-loaded MCP tools are registered (no-op after first call)
            for t in await await_mcp_tools(mcp_task):
                agent.tools.setdefault(t.name, t)
            mcp_task = None  # clear so we don't re-await the cached result each turn
            agent.add_user_message(user_input)

            # Create cancellation event
            cancel_event = asyncio.Event()
            agent.cancel_event = cancel_event

            # ── Terminal split: scroll region + fixed input line ──
            # Top area (rows 1..N-2): agent output scrolls here
            # Row N-1: separator
            # Row N: fixed input line (› prompt)
            term_rows = shutil.get_terminal_size().lines
            _ESC = "\033"
            # Set scroll region, draw separator + input line, move cursor back
            sys.stdout.write(
                f"{_ESC}[1;{term_rows - 2}r"       # scroll region: top to N-2
                f"{_ESC}[{term_rows - 1};1H"        # move to separator row
                f"{Colors.DIM}{'─' * 60}{Colors.RESET}"
                f"{_ESC}[{term_rows};1H{_ESC}[2K"   # move to input row, clear it
                f"{Colors.DIM}› {Colors.RESET}"      # draw input prompt
                f"{_ESC}[1;1H"                       # cursor back to scroll region top
            )
            sys.stdout.flush()

            print(
                f"\n{Colors.BRIGHT_BLUE}Agent{Colors.RESET} {Colors.DIM}›{Colors.RESET} "
                f"{Colors.DIM}Thinking... (Esc to cancel, type + Enter to inject){Colors.RESET}\n"
            )

            # ── Key listener thread ─────────────────────────
            esc_listener_stop = threading.Event()
            esc_cancelled = [False]
            pending_injections: list[str] = []

            def _refresh_input_line(buf: list[str]):
                """Redraw the fixed bottom input line without disturbing agent output."""
                text = "".join(buf)
                # save cursor → jump to input row → clear → draw → restore cursor
                sys.stdout.write(
                    f"{_ESC}[s"
                    f"{_ESC}[{term_rows};1H{_ESC}[2K"
                    f"{Colors.DIM}› {Colors.RESET}{text}"
                    f"{_ESC}[u"
                )
                sys.stdout.flush()

            def input_listener():
                input_buf: list[str] = []

                if platform.system() == "Windows":
                    try:
                        import msvcrt

                        while not esc_listener_stop.is_set():
                            if msvcrt.kbhit():
                                raw = msvcrt.getch()
                                if raw == b"\x1b" and not input_buf:
                                    print(f"\n{Colors.BRIGHT_YELLOW}⏹️  Esc pressed, cancelling...{Colors.RESET}")
                                    esc_cancelled[0] = True
                                    cancel_event.set()
                                    break
                                elif raw == b"\x1b":
                                    input_buf.clear()
                                    _refresh_input_line(input_buf)
                                elif raw in (b"\r", b"\n"):
                                    if input_buf:
                                        text = "".join(input_buf)
                                        input_buf.clear()
                                        _refresh_input_line(input_buf)
                                        pending_injections.append(text)
                                        print(f"{Colors.DIM}💉 Injected:{Colors.RESET} {Colors.BRIGHT_WHITE}{text}{Colors.RESET}")
                                elif raw == b"\x08":
                                    if input_buf:
                                        input_buf.pop()
                                        _refresh_input_line(input_buf)
                                else:
                                    try:
                                        ch = raw.decode("utf-8", errors="ignore")
                                        if ch and ch >= " ":
                                            input_buf.append(ch)
                                            _refresh_input_line(input_buf)
                                    except Exception:
                                        pass
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
                                if char == "\x1b" and not input_buf:
                                    print(f"\n{Colors.BRIGHT_YELLOW}⏹️  Esc pressed, cancelling...{Colors.RESET}")
                                    esc_cancelled[0] = True
                                    cancel_event.set()
                                    break
                                elif char == "\x1b":
                                    input_buf.clear()
                                    _refresh_input_line(input_buf)
                                elif char in ("\r", "\n"):
                                    if input_buf:
                                        text = "".join(input_buf)
                                        input_buf.clear()
                                        _refresh_input_line(input_buf)
                                        pending_injections.append(text)
                                        print(f"{Colors.DIM}💉 Injected:{Colors.RESET} {Colors.BRIGHT_WHITE}{text}{Colors.RESET}")
                                elif char == "\x7f":  # Backspace
                                    if input_buf:
                                        input_buf.pop()
                                        _refresh_input_line(input_buf)
                                elif char >= " ":
                                    input_buf.append(char)
                                    _refresh_input_line(input_buf)
                    finally:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass

            esc_thread = threading.Thread(target=input_listener, daemon=True)
            esc_thread.start()

            try:
                agent_task = asyncio.create_task(agent.run())

                while not agent_task.done():
                    if esc_cancelled[0]:
                        cancel_event.set()
                    while pending_injections:
                        agent.inject(pending_injections.pop(0))
                    await asyncio.sleep(0.1)

                _ = agent_task.result()

            except asyncio.CancelledError:
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  Agent execution cancelled{Colors.RESET}")
            finally:
                agent.cancel_event = None
                esc_listener_stop.set()
                esc_thread.join(timeout=0.2)
                # Restore full terminal: reset scroll region, clear bottom lines
                sys.stdout.write(
                    f"{_ESC}[r"                          # reset scroll region
                    f"{_ESC}[{term_rows - 1};1H{_ESC}[2K"  # clear separator
                    f"{_ESC}[{term_rows};1H{_ESC}[2K"      # clear input line
                    f"{_ESC}[{term_rows - 1};1H"            # position cursor
                )
                sys.stdout.flush()

            # Visual separation
            print(f"\n{Colors.DIM}{'─' * 60}{Colors.RESET}\n")

        except KeyboardInterrupt:
            print(f"\n\n{Colors.BRIGHT_YELLOW}👋 Interrupt signal detected, exiting...{Colors.RESET}\n")
            print_stats(agent, session_start)
            break

        except Exception as e:
            print(f"\n{Colors.RED}❌ Error: {e}{Colors.RESET}")
            print(f"{Colors.DIM}{'─' * 60}{Colors.RESET}\n")

    # 11. Cleanup MCP connections
    await _quiet_cleanup()


def main():
    """Main entry point for CLI"""
    # Parse command line arguments
    args = parse_args()

    # Handle log subcommand
    if args.command == "log":
        if args.filename:
            read_log_file(args.filename)
        else:
            show_log_directory(open_file_manager=True)
        return

    # Handle setup subcommand
    if args.command == "setup":
        cmd_setup()
        return

    # Handle config subcommand
    if args.command == "config":
        cmd_config(edit=args.edit)
        return

    # Handle doctor subcommand
    if args.command == "doctor":
        asyncio.run(cmd_doctor())
        return

    # Handle install-browser subcommand
    if args.command == "install-browser":
        asyncio.run(cmd_install_browser())
        return

    # Ensure user config exists; run setup wizard on first launch
    config_path = Config._ensure_user_config()
    try:
        config_check = Config.from_yaml(config_path)
        # If key is still the placeholder, treat as unconfigured
        if config_check.llm.api_key in ("YOUR_API_KEY_HERE", ""):
            print(f"{Colors.BRIGHT_CYAN}First-time setup detected. Let's configure Box Agent.{Colors.RESET}\n")
            run_setup_wizard(config_path)
    except Exception:
        # Config can't be parsed or key is missing — run wizard
        print(f"{Colors.BRIGHT_CYAN}First-time setup detected. Let's configure Box Agent.{Colors.RESET}\n")
        run_setup_wizard(config_path)

    # Determine workspace directory
    # Priority: CLI --workspace > current working directory
    if args.workspace:
        workspace_dir = Path(args.workspace).expanduser().absolute()
    else:
        # Use current working directory
        workspace_dir = Path.cwd()

    # Ensure workspace directory exists
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Run the agent (config always loaded from package directory)
    try:
        asyncio.run(run_agent(workspace_dir, task=args.task, sandbox_mode=not args.no_sandbox))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
