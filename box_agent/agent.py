"""Core Agent implementation.

The heavy lifting now lives in ``box_agent.core.run_agent_loop``.
This module keeps the public ``Agent`` API backward-compatible while
delegating to the shared execution core.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Optional

from .core import run_agent_loop
from .events import (
    AgentEvent,
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    LogFileEvent,
    StepEnd,
    StepStart,
    StopReason,
    SummarizationEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolCallResult,
    ToolCallStart,
)
from .llm import LLMClient
from .logger import AgentLogger
from .schema import Message
from .tools.base import Tool
from .utils import calculate_display_width


# ANSI color codes
class Colors:
    """Terminal color definitions"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Foreground colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    # Bright colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


class Agent:
    """Single agent with basic tools and MCP support."""

    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 50,
        workspace_dir: str = "./workspace",
        token_limit: int = 80000,
    ):
        self.llm = llm_client
        self.tools = {tool.name: tool for tool in tools}
        self.max_steps = max_steps
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        self.cancel_event: Optional[asyncio.Event] = None

        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        if "Current Workspace" not in system_prompt:
            workspace_info = (
                f"\n\n## Current Workspace\n"
                f"You are currently working in: `{self.workspace_dir.absolute()}`\n"
                f"All relative paths will be resolved relative to this directory."
            )
            system_prompt = system_prompt + workspace_info

        self.system_prompt = system_prompt
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]
        self.logger = AgentLogger()
        self.api_total_tokens: int = 0
        self._streaming_active: bool = False  # Track if streaming output needs trailing newline

    def add_user_message(self, content: str):
        """Add a user message to history."""
        self.messages.append(Message(role="user", content=content))

    def _check_cancelled(self) -> bool:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    # ── Event-stream API (new) ──────────────────────────────

    async def run_events(
        self, cancel_event: Optional[asyncio.Event] = None
    ) -> AsyncIterator[AgentEvent]:
        """Execute the agent loop, yielding structured events.

        This is the preferred API for consumers that want fine-grained
        control over rendering (e.g. ACP, JSON-RPC, custom UIs).
        """
        if cancel_event is not None:
            self.cancel_event = cancel_event

        async for event in run_agent_loop(
            llm=self.llm,
            messages=self.messages,
            tools=self.tools,
            max_steps=self.max_steps,
            token_limit=self.token_limit,
            is_cancelled=self._check_cancelled,
            logger=self.logger,
            workspace_dir=str(self.workspace_dir),
        ):
            # Track token usage on Agent instance for backward compat
            if isinstance(event, TokenUsageEvent):
                self.api_total_tokens = event.total_tokens
            yield event

    # ── Backward-compatible run() ───────────────────────────

    async def run(self, cancel_event: Optional[asyncio.Event] = None) -> str:
        """Execute agent loop with terminal rendering.

        Signature and return value are unchanged from before the refactor.
        Internally it now consumes ``run_events()``.
        """
        final_content = ""
        async for event in self.run_events(cancel_event):
            self._render_event(event)
            if isinstance(event, DoneEvent):
                final_content = event.final_content
        return final_content

    # ── Terminal renderer ───────────────────────────────────

    def _render_event(self, event: AgentEvent) -> None:  # noqa: C901 — intentionally flat
        """Translate an ``AgentEvent`` into terminal output."""

        # End streaming line before non-streaming events
        is_streaming = (
            isinstance(event, (ThinkingEvent, ContentEvent))
            and getattr(event, "_streaming", False)
        )
        if not is_streaming and self._streaming_active:
            print()  # newline to end the streaming line
            self._streaming_active = False

        match event:
            case LogFileEvent(path=p):
                print(f"{Colors.DIM}📝 Log file: {p}{Colors.RESET}")

            case SummarizationEvent(estimated_tokens=est, api_tokens=api, token_limit=limit):
                print(
                    f"\n{Colors.BRIGHT_YELLOW}📊 Token usage - Local estimate: {est}, "
                    f"API reported: {api}, Limit: {limit}{Colors.RESET}"
                )
                print(f"{Colors.BRIGHT_YELLOW}🔄 Triggering message history summarization...{Colors.RESET}")

            case StepStart(step=s, max_steps=mx):
                BOX_WIDTH = 58
                step_text = f"{Colors.BOLD}{Colors.BRIGHT_CYAN}💭 Step {s}/{mx}{Colors.RESET}"
                step_display_width = calculate_display_width(step_text)
                padding = max(0, BOX_WIDTH - 1 - step_display_width)
                print(f"\n{Colors.DIM}╭{'─' * BOX_WIDTH}╮{Colors.RESET}")
                print(f"{Colors.DIM}│{Colors.RESET} {step_text}{' ' * padding}{Colors.DIM}│{Colors.RESET}")
                print(f"{Colors.DIM}╰{'─' * BOX_WIDTH}╯{Colors.RESET}")

            case ThinkingEvent() if event._streaming:
                if event._header:
                    print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 Thinking:{Colors.RESET}")
                else:
                    print(f"{Colors.DIM}{event.content}{Colors.RESET}", end="", flush=True)
                    self._streaming_active = True

            case ThinkingEvent(content=text):
                print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 Thinking:{Colors.RESET}")
                print(f"{Colors.DIM}{text}{Colors.RESET}")

            case ContentEvent() if event._streaming:
                if event._header:
                    print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant:{Colors.RESET}")
                else:
                    print(f"{event.content}", end="", flush=True)
                    self._streaming_active = True

            case ContentEvent(content=text):
                print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant:{Colors.RESET}")
                print(f"{text}")

            case ToolCallStart(tool_name=name, arguments=args):
                print(f"\n{Colors.BRIGHT_YELLOW}🔧 Tool Call:{Colors.RESET} {Colors.BOLD}{Colors.CYAN}{name}{Colors.RESET}")
                print(f"{Colors.DIM}   Arguments:{Colors.RESET}")
                truncated = {}
                for k, v in args.items():
                    vs = str(v)
                    truncated[k] = vs[:200] + "..." if len(vs) > 200 else v
                for line in json.dumps(truncated, indent=2, ensure_ascii=False).split("\n"):
                    print(f"   {Colors.DIM}{line}{Colors.RESET}")

            case ToolCallResult(success=ok, content=text, error=err):
                if ok:
                    display = text[:300] + f"{Colors.DIM}...{Colors.RESET}" if len(text) > 300 else text
                    print(f"{Colors.BRIGHT_GREEN}✓ Result:{Colors.RESET} {display}")
                else:
                    print(f"{Colors.BRIGHT_RED}✗ Error:{Colors.RESET} {Colors.RED}{err}{Colors.RESET}")

            case ArtifactEvent(artifact_type=atype, filename=fname, path=fpath):
                print(f"{Colors.BRIGHT_CYAN}📎 Artifact ({atype}):{Colors.RESET} {fname} → {fpath}")

            case ErrorEvent(message=msg):
                print(f"\n{Colors.BRIGHT_RED}❌ Error:{Colors.RESET} {msg}")

            case StepEnd(step=s, elapsed_seconds=el, total_elapsed_seconds=tot):
                print(f"\n{Colors.DIM}⏱️  Step {s} completed in {el:.2f}s (total: {tot:.2f}s){Colors.RESET}")

            case DoneEvent(stop_reason=reason, final_content=_):
                if reason == StopReason.CANCELLED:
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  Task cancelled by user.{Colors.RESET}")
                elif reason == StopReason.MAX_STEPS:
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {event.final_content}{Colors.RESET}")

            case _:
                pass  # TokenUsageEvent etc. — no terminal output needed

    def get_history(self) -> list[Message]:
        """Get message history."""
        return self.messages.copy()
