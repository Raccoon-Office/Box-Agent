"""Terminal-only rendering for the backward-compatible CLI Agent facade."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from box_agent.events import (
    AgentEvent,
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LogFileEvent,
    MemoryProposalEvent,
    PermissionRequestEvent,
    StepEnd,
    StepStart,
    StopReason,
    SubAgentEvent,
    SummarizationEvent,
    ThinkingEvent,
    ToolCallResult,
    ToolCallStart,
)
from box_agent.utils import calculate_display_width

if TYPE_CHECKING:
    from box_agent.agent import Agent


async def render_agent_events(agent: Agent, events: AsyncIterator[AgentEvent]) -> str:
    """Consume a turn with the terminal behavior shared by CLI and Agent.run."""

    final_content = ""
    agent.last_stop_reason = None
    try:
        async for event in events:
            agent._render_event(event)
            if isinstance(event, MemoryProposalEvent) and agent._proposal_negotiator is not None:
                try:
                    await agent._proposal_negotiator.negotiate(event)
                except Exception:
                    pass
            if isinstance(event, DoneEvent):
                final_content = event.final_content
                agent.last_stop_reason = event.stop_reason.value
    finally:
        close = getattr(events, "aclose", None)
        if callable(close):
            await close()
    return final_content


class Colors:
    """ANSI color definitions shared by CLI-facing renderers."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"


def _format_size(n: int) -> str:
    """Render a byte count as a short human label (``12.4KB``)."""

    if n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024  # type: ignore[assignment]
    return f"{n}B"


class CliRenderer:
    """Render structured Agent events to the existing terminal format."""

    def __init__(self) -> None:
        self.streaming_active = False
        self._step_content_rendered = False

    def render(self, event: AgentEvent) -> None:  # noqa: C901 — legacy flat mapping
        """Translate one ``AgentEvent`` into terminal output."""

        is_streaming = (
            isinstance(event, (ThinkingEvent, ContentEvent))
            and getattr(event, "_streaming", False)
        )
        if not is_streaming and self.streaming_active:
            print()
            self.streaming_active = False

        match event:
            case LogFileEvent(path=p):
                print(f"{Colors.DIM}📝 Log file: {p}{Colors.RESET}")

            case SummarizationEvent(estimated_tokens=est, api_tokens=api, token_limit=limit):
                print(
                    f"\n{Colors.BRIGHT_YELLOW}📊 Token usage - Local estimate: {est}, "
                    f"API reported: {api}, Limit: {limit}{Colors.RESET}"
                )
                if event.mode == "fallback":
                    print(
                        f"{Colors.BRIGHT_YELLOW}⚠️ Summary provider failed; "
                        "using a bounded deterministic history record."
                        f"{Colors.RESET}"
                    )
                elif event.mode == "blocked":
                    print(
                        f"{Colors.BRIGHT_RED}⛔ Context remains above the safe limit "
                        f"after compaction ({event.estimated_after} tokens).{Colors.RESET}"
                    )
                else:
                    print(
                        f"{Colors.BRIGHT_YELLOW}🔄 Message history compacted to "
                        f"approximately {event.estimated_after} tokens.{Colors.RESET}"
                    )

            case StepStart(step=s, max_steps=mx):
                self._step_content_rendered = False
                box_width = 58
                step_text = f"{Colors.BOLD}{Colors.BRIGHT_CYAN}💭 Step {s}/{mx}{Colors.RESET}"
                step_display_width = calculate_display_width(step_text)
                padding = max(0, box_width - 1 - step_display_width)
                print(f"\n{Colors.DIM}╭{'─' * box_width}╮{Colors.RESET}")
                print(f"{Colors.DIM}│{Colors.RESET} {step_text}{' ' * padding}{Colors.DIM}│{Colors.RESET}")
                print(f"{Colors.DIM}╰{'─' * box_width}╯{Colors.RESET}")

            case ThinkingEvent() if event._streaming:
                if event._header:
                    print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 Thinking:{Colors.RESET}")
                else:
                    print(f"{Colors.DIM}{event.content}{Colors.RESET}", end="", flush=True)
                    self.streaming_active = True

            case ThinkingEvent(content=text):
                print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 Thinking:{Colors.RESET}")
                print(f"{Colors.DIM}{text}{Colors.RESET}")

            case ContentEvent() if event._streaming:
                if event._header:
                    print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant:{Colors.RESET}")
                else:
                    print(f"{event.content}", end="", flush=True)
                    self.streaming_active = True
                    self._step_content_rendered |= bool(event.content.strip())

            case ContentEvent(content=text):
                self._step_content_rendered |= bool(text.strip())
                print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant:{Colors.RESET}")
                print(f"{text}")

            case ToolCallStart(tool_name=name, arguments=args, user_visible=user_visible):
                if not user_visible:
                    return
                print(
                    f"\n{Colors.BRIGHT_YELLOW}🔧 Tool Call:{Colors.RESET} "
                    f"{Colors.BOLD}{Colors.CYAN}{name}{Colors.RESET}"
                )
                print(f"{Colors.DIM}   Arguments:{Colors.RESET}")
                truncated = {}
                for key, value in args.items():
                    value_string = str(value)
                    truncated[key] = (
                        value_string[:200] + "..." if len(value_string) > 200 else value
                    )
                for line in json.dumps(truncated, indent=2, ensure_ascii=False).split("\n"):
                    print(f"   {Colors.DIM}{line}{Colors.RESET}")

            case ToolCallResult(
                success=ok,
                content=text,
                error=err,
                raw_output=raw_output,
                user_visible=user_visible,
            ):
                if not user_visible:
                    return
                if ok:
                    display = (
                        text[:300] + f"{Colors.DIM}...{Colors.RESET}"
                        if len(text) > 300
                        else text
                    )
                    print(f"{Colors.BRIGHT_GREEN}✓ Result:{Colors.RESET} {display}")
                    if raw_output and raw_output.get("type") == "memory_search":
                        self.render_memory_search(raw_output)
                else:
                    print(f"{Colors.BRIGHT_RED}✗ Error:{Colors.RESET} {Colors.RED}{err}{Colors.RESET}")

            case ArtifactEvent(kind=kind, filename=fname, rel_path=rel, size=sz):
                size_label = _format_size(sz)
                print(
                    f"{Colors.BRIGHT_CYAN}📎 {kind}{Colors.RESET} {fname} · "
                    f"{size_label} · {Colors.DIM}{rel}{Colors.RESET}"
                )

            case SubAgentEvent(task_preview=preview, event=inner, sub_agent_id=_, title=sub_title):
                raw_label = sub_title or preview
                label = raw_label[:40] + "..." if len(raw_label) > 40 else raw_label
                prefix = f"{Colors.DIM}  ┊ [{label}]{Colors.RESET}"
                match inner:
                    case StepStart(step=s, max_steps=mx):
                        print(f"{prefix}{Colors.DIM} Step {s}/{mx}{Colors.RESET}")
                    case ToolCallStart(tool_name=name, user_visible=True):
                        print(f"{prefix}{Colors.DIM} 🔧 {name}{Colors.RESET}")
                    case ToolCallResult(tool_name=name, success=ok, user_visible=True):
                        mark = "✓" if ok else "✗"
                        print(f"{prefix}{Colors.DIM} {mark} {name}{Colors.RESET}")
                    case ArtifactEvent(filename=fname):
                        print(f"{prefix}{Colors.DIM} 📎 {fname}{Colors.RESET}")
                    case ErrorEvent(message=msg):
                        print(f"{prefix}{Colors.DIM} ❌ {msg}{Colors.RESET}")

            case ErrorEvent(message=msg):
                print(f"\n{Colors.BRIGHT_RED}❌ Error:{Colors.RESET} {msg}")

            case PermissionRequestEvent(scope=scope, requested_scope=req_scope, path=path, reason=reason):
                print(f"\n{Colors.BRIGHT_YELLOW}🔒 Permission required: {scope} → {req_scope}{Colors.RESET}")
                if path:
                    print(f"   Path: {path}")
                print(f"   Reason: {reason}")

            case InjectedMessageEvent(content=text, user_visible=user_visible):
                if not user_visible:
                    return
                preview = text[:80] + "..." if len(text) > 80 else text
                print(
                    f"\n{Colors.DIM}💉 Injected:{Colors.RESET} "
                    f"{Colors.BRIGHT_WHITE}{preview}{Colors.RESET}"
                )

            case StepEnd(step=s, elapsed_seconds=el, total_elapsed_seconds=tot):
                print(
                    f"\n{Colors.DIM}⏱️  Step {s} completed in {el:.2f}s "
                    f"(total: {tot:.2f}s){Colors.RESET}"
                )

            case DoneEvent(stop_reason=reason, final_content=_):
                if reason == StopReason.CANCELLED:
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  Task cancelled by user.{Colors.RESET}")
                elif reason == StopReason.MAX_STEPS:
                    notice = (
                        "Step limit reached; see the output above for partial results."
                        if self._step_content_rendered else event.final_content
                    )
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {notice}{Colors.RESET}")
                self._step_content_rendered = False

            case _:
                pass

    def render_memory_search(self, raw_output: dict[str, Any]) -> None:
        """Render structured memory-search matches in the terminal."""

        matches = raw_output.get("matched_memories")
        if not isinstance(matches, list):
            return

        query = raw_output.get("query", "")
        if matches:
            print(f"{Colors.BRIGHT_CYAN}🧠 Matched memories:{Colors.RESET} {query}")
            for item in matches:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "")).strip()
                if text:
                    print(f"  {Colors.DIM}{text}{Colors.RESET}")
        else:
            print(f"{Colors.DIM}🧠 Matched memories: none for {query}{Colors.RESET}")


__all__ = ["CliRenderer", "Colors", "_format_size", "render_agent_events"]
