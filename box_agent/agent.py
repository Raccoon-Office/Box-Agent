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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .core import run_agent_loop
from .events import (
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
    TokenUsageEvent,
    ToolCallResult,
    ToolCallStart,
)
from .llm import LLMClient
from .logger import AgentLogger
from .loop_guards import CompletionGate
from .schema import Message
from .tools.base import Tool, ToolResult
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


@dataclass
class GoalState:
    """Lightweight session goal tracked by the interactive CLI."""

    objective: str
    status: str
    created_at: str
    updated_at: str
    evidence: list[str] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    completed_by: str | None = None


def _clean_goal_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_goal_items(value: object) -> list[str]:
    if isinstance(value, str):
        item = value.strip()
        return [item] if item else []
    if isinstance(value, (list, tuple)):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _extend_goal_items(target: list[str], items: list[str]) -> None:
    for item in items:
        if item not in target:
            target.append(item)


def goal_payload(goal: GoalState | None) -> dict | None:
    if goal is None:
        return None
    return {
        "objective": goal.objective,
        "status": goal.status,
        "createdAt": goal.created_at,
        "updatedAt": goal.updated_at,
        "evidence": list(goal.evidence),
        "progress": list(goal.progress),
        "blockedReason": goal.blocked_reason,
        "completedBy": goal.completed_by,
    }


def goal_state_from_payload(payload: object) -> GoalState | None:
    """Build a GoalState from a persisted/host-provided payload."""
    if not isinstance(payload, dict):
        return None

    objective = _clean_goal_text(payload.get("objective"))
    if not objective:
        return None

    now = datetime.now().isoformat()
    status = _clean_goal_text(payload.get("status")) or "active"
    return GoalState(
        objective=objective,
        status=status,
        created_at=_clean_goal_text(payload.get("createdAt") or payload.get("created_at")) or now,
        updated_at=_clean_goal_text(payload.get("updatedAt") or payload.get("updated_at")) or now,
        evidence=_coerce_goal_items(payload.get("evidence")),
        progress=_coerce_goal_items(payload.get("progress")),
        blocked_reason=_clean_goal_text(payload.get("blockedReason") or payload.get("blocked_reason")) or None,
        completed_by=_clean_goal_text(payload.get("completedBy") or payload.get("completed_by")) or None,
    )


def _goal_snapshot(agent: "Agent", action: str | None = None) -> dict:
    payload = {
        "type": "goal_snapshot",
        "goal": goal_payload(agent.goal),
    }
    if action is not None:
        payload["action"] = action
    return payload


def should_continue_goal_autopilot(agent: "Agent", stop_reason: str | None) -> bool:
    """Return True when an automatic continuation may safely start."""
    goal = agent.goal
    if goal is None or goal.status != "active":
        return False
    return stop_reason == "end_turn"


def goal_autopilot_progress_signature(goal: GoalState | None) -> tuple | None:
    """Return goal fields that count as autopilot progress."""
    if goal is None:
        return None
    return (
        goal.objective,
        goal.status,
        tuple(goal.progress),
        tuple(goal.evidence),
        goal.blocked_reason,
        goal.completed_by,
    )


def goal_autopilot_prompt(goal: GoalState, continuation: int, max_continuations: int) -> str:
    """Build the internal prompt used to continue an active goal."""
    progress = "\n".join(f"- {item}" for item in goal.progress[-5:])
    evidence = "\n".join(f"- {item}" for item in goal.evidence[-5:])
    context_parts = []
    if progress:
        context_parts.append(f"Recent recorded progress:\n{progress}")
    if evidence:
        context_parts.append(f"Recent evidence:\n{evidence}")
    context = "\n\n".join(context_parts)
    context_block = f"\n\n{context}" if context else ""
    return (
        f"Goal autopilot continuation {continuation}/{max_continuations}.\n"
        "The previous turn ended while the durable goal is still active. Continue "
        "working from the current conversation and workspace state without waiting "
        "for another user instruction. Verify concrete state before claiming the "
        "goal is done. If the goal is satisfied, call `goal_write` with action "
        "`complete` and non-empty `evidence`. If an external dependency blocks "
        "progress, such as missing credentials, authorization, rate limits, a "
        "third-party service outage, or required user input, call `goal_write` "
        "with action `block` and a clear `blocked_reason`. If you make verified "
        "partial progress but the goal is still not done, call `goal_write` with "
        "action `progress` before ending. Avoid retrying the same failing external "
        "operation repeatedly without new evidence or a different approach."
        f"{context_block}"
    )


class _GoalReadTool(Tool):
    """Read the current durable session goal."""

    def __init__(self, agent: "Agent"):
        self._agent = agent

    @property
    def name(self) -> str:
        return "goal_read"

    @property
    def description(self) -> str:
        return (
            "Read the current durable session goal. Use this to check whether a goal "
            "is active, paused, complete, or unset before deciding whether to continue."
        )

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        goal = self._agent.goal
        if goal is None:
            return ToolResult(
                success=True,
                content="No current goal.",
                raw_output=_goal_snapshot(self._agent),
            )
        return ToolResult(
            success=True,
            content=f"Goal is {goal.status}: {goal.objective}",
            raw_output=_goal_snapshot(self._agent),
        )


class _GoalWriteTool(Tool):
    """Update the durable session goal."""

    def __init__(self, agent: "Agent"):
        self._agent = agent

    @property
    def name(self) -> str:
        return "goal_write"

    @property
    def description(self) -> str:
        return (
            "Update the durable session goal. Call action='complete' yourself when the "
            "active goal has been satisfied, and include evidence entries that name the "
            "files, tests, logs, command output, or artifacts proving completion; do not "
            "ask the user to run a slash command for completion. Use set/pause/resume/clear "
            "only when the user explicitly requests that lifecycle change. Use action='progress' "
            "to record verified progress, and action='block' with blocked_reason when external "
            "input is required."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "pause", "resume", "complete", "clear", "progress", "block"],
                    "description": "Goal lifecycle operation.",
                },
                "objective": {
                    "type": "string",
                    "description": "Goal objective. Required for action='set'.",
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Evidence for completion or progress, such as tests run, files changed, logs, or artifacts.",
                },
                "progress": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Verified progress updates to append to the goal snapshot.",
                },
                "blocked_reason": {
                    "type": "string",
                    "description": "Reason the goal is blocked. Required for action='block'.",
                },
                "completed_by": {
                    "type": "string",
                    "description": "Who or what completed the goal, for example 'model' or 'cli'.",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        objective: str | None = None,
        evidence: object = None,
        progress: object = None,
        blocked_reason: str | None = None,
        completed_by: str | None = None,
    ) -> ToolResult:
        action = (action or "").strip().lower()
        evidence_items = _coerce_goal_items(evidence)
        progress_items = _coerce_goal_items(progress)
        if action == "set":
            if not objective or not objective.strip():
                return ToolResult(success=False, error="'objective' is required for set.")
            goal = self._agent.set_goal(
                objective,
                evidence=evidence_items,
                progress=progress_items,
                blocked_reason=blocked_reason,
                completed_by=completed_by,
            )
            return ToolResult(
                success=True,
                content=f"Set goal: {goal.objective}",
                raw_output=_goal_snapshot(self._agent, action="set"),
            )

        if action == "pause":
            if self._agent.pause_goal() is None:
                return ToolResult(success=False, error="No goal to pause.")
            return ToolResult(
                success=True,
                content="Paused the current goal.",
                raw_output=_goal_snapshot(self._agent, action="pause"),
            )

        if action == "resume":
            if self._agent.resume_goal() is None:
                return ToolResult(success=False, error="No goal to resume.")
            return ToolResult(
                success=True,
                content="Resumed the current goal.",
                raw_output=_goal_snapshot(self._agent, action="resume"),
            )

        if action == "complete":
            if not evidence_items:
                return ToolResult(
                    success=False,
                    error="'evidence' is required for complete. Include files, tests, logs, command output, or artifacts.",
                )
            if self._agent.complete_goal(
                evidence=evidence_items,
                progress=progress_items,
                completed_by=completed_by or "model",
            ) is None:
                return ToolResult(success=False, error="No goal to complete.")
            return ToolResult(
                success=True,
                content="Marked the current goal complete.",
                raw_output=_goal_snapshot(self._agent, action="complete"),
            )

        if action == "progress":
            if not progress_items:
                return ToolResult(success=False, error="'progress' is required for progress.")
            if self._agent.update_goal_progress(progress_items, evidence=evidence_items) is None:
                return ToolResult(success=False, error="No goal to update.")
            return ToolResult(
                success=True,
                content="Updated goal progress.",
                raw_output=_goal_snapshot(self._agent, action="progress"),
            )

        if action == "block":
            reason = (blocked_reason or "").strip()
            if not reason:
                return ToolResult(success=False, error="'blocked_reason' is required for block.")
            if self._agent.block_goal(reason, evidence=evidence_items, progress=progress_items) is None:
                return ToolResult(success=False, error="No goal to block.")
            return ToolResult(
                success=True,
                content=f"Marked goal blocked: {reason}",
                raw_output=_goal_snapshot(self._agent, action="block"),
            )

        if action == "clear":
            self._agent.clear_goal()
            return ToolResult(
                success=True,
                content="Cleared the current goal.",
                raw_output=_goal_snapshot(self._agent, action="clear"),
            )

        return ToolResult(success=False, error=f"Unknown action: {action}")


def _format_size(n: int) -> str:
    """Render a byte count as a short human label (``12.4KB``)."""
    if n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024  # type: ignore[assignment]
    return f"{n}B"


class Agent:
    """Single agent with basic tools and MCP support."""

    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 200,
        workspace_dir: str = "./workspace",
        token_limit: int = 113400,
        hooks: list | None = None,
        thinking_enabled: bool = False,
        memory_promotion_enabled: bool = False,
        memory_promotion_hit_threshold: int = 5,
        memory_promotion_cooldown_days: int = 14,
        max_parallel_tools: int = 8,
        truncation_continuation_enabled: bool = True,
        max_truncation_continuations: int = 1,
    ):
        self.llm = llm_client
        self.tools = {tool.name: tool for tool in tools}
        self.max_steps = max_steps
        self.max_parallel_tools = max_parallel_tools
        self.truncation_continuation_enabled = truncation_continuation_enabled
        self.max_truncation_continuations = max_truncation_continuations
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        self.cancel_event: Optional[asyncio.Event] = None
        self.inject_queue: asyncio.Queue[str] = asyncio.Queue()
        self._permission_negotiator = None  # set by CLI/ACP when permission engine is active
        self._proposal_negotiator = None  # set by CLI/ACP to handle MemoryProposalEvent
        self._hooks = hooks
        self._memory_extractor = None  # set by CLI/ACP when memory extraction is enabled
        self.thinking_enabled = thinking_enabled
        self.memory_promotion_enabled = memory_promotion_enabled
        self.memory_promotion_hit_threshold = memory_promotion_hit_threshold
        self.memory_promotion_cooldown_days = memory_promotion_cooldown_days

        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        if "Current Workspace" not in system_prompt:
            workspace_info = (
                f"\n\n## Current Workspace\n"
                f"You are currently working in: `{self.workspace_dir.absolute()}`\n"
                f"All relative paths will be resolved relative to this directory."
            )
            system_prompt = system_prompt + workspace_info

        self.system_prompt = system_prompt
        for tool in self.tools.values():
            if hasattr(tool, "set_parent_system_prompt"):
                tool.set_parent_system_prompt(system_prompt)
            # Give sub-agents a live view of this agent's tool map so tools
            # registered after construction (e.g. MCP web_search, merged in by
            # register_mcp_tools which mutates self.tools in place) are still
            # inherited by child agents instead of a stale snapshot.
            if hasattr(tool, "set_tool_provider"):
                tool.set_tool_provider(lambda: self.tools)
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]
        self.logger = AgentLogger()
        self.api_total_tokens: int = 0
        self._streaming_active: bool = False  # Track if streaming output needs trailing newline
        self.last_stop_reason: str | None = None
        self.goal: GoalState | None = None
        self.tools["goal_read"] = _GoalReadTool(self)
        self.tools["goal_write"] = _GoalWriteTool(self)

    def add_user_message(self, content: str):
        """Add a user message to history."""
        if self.goal is not None and self.goal.status == "active":
            content = self._apply_goal_context(content)
        self.messages.append(Message(role="user", content=content))

    def set_goal(
        self,
        objective: str,
        *,
        evidence: object = None,
        progress: object = None,
        blocked_reason: str | None = None,
        completed_by: str | None = None,
    ) -> GoalState:
        """Set or replace the current session goal."""
        objective = objective.strip()
        if not objective:
            raise ValueError("Goal objective cannot be empty.")
        now = datetime.now().isoformat()
        self.goal = GoalState(
            objective=objective,
            status="active",
            created_at=now,
            updated_at=now,
            evidence=_coerce_goal_items(evidence),
            progress=_coerce_goal_items(progress),
            blocked_reason=(blocked_reason or "").strip() or None,
            completed_by=(completed_by or "").strip() or None,
        )
        return self.goal

    def pause_goal(self) -> GoalState | None:
        """Pause the current goal, if one exists."""
        if self.goal is None:
            return None
        self.goal.status = "paused"
        self.goal.updated_at = datetime.now().isoformat()
        return self.goal

    def resume_goal(self) -> GoalState | None:
        """Resume the current goal, if one exists."""
        if self.goal is None:
            return None
        self.goal.status = "active"
        self.goal.blocked_reason = None
        self.goal.updated_at = datetime.now().isoformat()
        return self.goal

    def complete_goal(
        self,
        *,
        evidence: object = None,
        progress: object = None,
        completed_by: str | None = None,
    ) -> GoalState | None:
        """Mark the current goal complete, if one exists."""
        if self.goal is None:
            return None
        self.goal.status = "complete"
        _extend_goal_items(self.goal.evidence, _coerce_goal_items(evidence))
        _extend_goal_items(self.goal.progress, _coerce_goal_items(progress))
        self.goal.blocked_reason = None
        completed_by = (completed_by or "").strip()
        if completed_by:
            self.goal.completed_by = completed_by
        self.goal.updated_at = datetime.now().isoformat()
        return self.goal

    def update_goal_progress(
        self,
        progress: object,
        *,
        evidence: object = None,
    ) -> GoalState | None:
        """Append progress/evidence to the current goal, if one exists."""
        if self.goal is None:
            return None
        _extend_goal_items(self.goal.progress, _coerce_goal_items(progress))
        _extend_goal_items(self.goal.evidence, _coerce_goal_items(evidence))
        self.goal.updated_at = datetime.now().isoformat()
        return self.goal

    def block_goal(
        self,
        blocked_reason: str,
        *,
        evidence: object = None,
        progress: object = None,
    ) -> GoalState | None:
        """Mark the current goal blocked with an explicit reason."""
        if self.goal is None:
            return None
        reason = blocked_reason.strip()
        if not reason:
            raise ValueError("Goal blocked_reason cannot be empty.")
        self.goal.status = "blocked"
        self.goal.blocked_reason = reason
        _extend_goal_items(self.goal.evidence, _coerce_goal_items(evidence))
        _extend_goal_items(self.goal.progress, _coerce_goal_items(progress))
        self.goal.updated_at = datetime.now().isoformat()
        return self.goal

    def clear_goal(self) -> GoalState | None:
        """Clear the current goal and return the removed state."""
        old_goal = self.goal
        self.goal = None
        return old_goal

    def restore_goal(self, payload: object) -> GoalState | None:
        """Restore goal state from a persisted or host-provided payload."""
        goal = goal_state_from_payload(payload)
        self.goal = goal
        return goal

    def _apply_goal_context(self, user_content: str) -> str:
        goal = self.goal
        if goal is None:
            return user_content
        return (
            "## Active Goal\n"
            f"Objective: {goal.objective}\n\n"
            "Work toward this durable goal across turns. Treat completion as evidence-based: "
            "verify the objective against concrete files, tests, logs, command output, or artifacts "
            "before saying it is done. Keep changes scoped to the goal and the user's latest message. "
            "If the goal is satisfied, call `goal_write` with action `complete` and non-empty "
            "`evidence` before your final answer, then state the evidence that proves completion. "
            "Use `goal_write` action `progress` for verified partial progress and action `block` "
            "with `blocked_reason` when external input is required. Do not ask the user to run "
            "a slash command for this.\n\n"
            "## Latest User Message\n"
            f"{user_content}"
        )

    def inject(self, content: str) -> None:
        """Inject a user message into the running agent loop.

        The message is queued and will be appended to the conversation
        at the next step boundary.  Safe to call from any thread.
        """
        self.inject_queue.put_nowait(content)

    def _check_cancelled(self) -> bool:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    # ── Event-stream API (new) ──────────────────────────────

    async def run_events(
        self,
        cancel_event: Optional[asyncio.Event] = None,
        *,
        force_plan_start: bool = False,
        require_plan_approval: bool = False,
        plan_approval: dict | None = None,
        pause_after_plan_write: bool = False,
        completion_gate: CompletionGate | None = None,
        artifact_detection_enabled: bool = True,
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
            permission_negotiator=self._permission_negotiator,
            hooks=self._hooks,
            memory_manager=getattr(self._memory_extractor, "_mgr", None),
            memory_extractor=self._memory_extractor,
            memory_promotion_enabled=self.memory_promotion_enabled,
            memory_promotion_hit_threshold=self.memory_promotion_hit_threshold,
            memory_promotion_cooldown_days=self.memory_promotion_cooldown_days,
            inject_queue=self.inject_queue,
            thinking_enabled=self.thinking_enabled,
            max_parallel_tools=self.max_parallel_tools,
            force_plan_start=force_plan_start,
            require_plan_approval=require_plan_approval,
            plan_approval=plan_approval,
            pause_after_plan_write=pause_after_plan_write,
            completion_gate=completion_gate,
            truncation_continuation_enabled=self.truncation_continuation_enabled,
            max_truncation_continuations=self.max_truncation_continuations,
            artifact_detection_enabled=artifact_detection_enabled,
        ):
            # Track token usage on Agent instance for backward compat
            if isinstance(event, TokenUsageEvent):
                self.api_total_tokens = event.total_tokens
            yield event

    # ── Backward-compatible run() ───────────────────────────

    async def run(
        self,
        cancel_event: Optional[asyncio.Event] = None,
        *,
        force_plan_start: bool = False,
        require_plan_approval: bool = False,
        plan_approval: dict | None = None,
        pause_after_plan_write: bool = False,
        completion_gate: CompletionGate | None = None,
        artifact_detection_enabled: bool = True,
    ) -> str:
        """Execute agent loop with terminal rendering.

        Signature and return value are unchanged from before the refactor.
        Internally it now consumes ``run_events()``.
        """
        final_content = ""
        self.last_stop_reason = None
        async for event in self.run_events(
            cancel_event,
            force_plan_start=force_plan_start,
            require_plan_approval=require_plan_approval,
            plan_approval=plan_approval,
            pause_after_plan_write=pause_after_plan_write,
            completion_gate=completion_gate,
            artifact_detection_enabled=artifact_detection_enabled,
        ):
            self._render_event(event)
            if isinstance(event, MemoryProposalEvent) and self._proposal_negotiator is not None:
                try:
                    await self._proposal_negotiator.negotiate(event)
                except Exception:
                    pass
            if isinstance(event, DoneEvent):
                final_content = event.final_content
                self.last_stop_reason = event.stop_reason.value
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

            case ToolCallStart(tool_name=name, arguments=args, user_visible=user_visible):
                if not user_visible:
                    return
                print(f"\n{Colors.BRIGHT_YELLOW}🔧 Tool Call:{Colors.RESET} {Colors.BOLD}{Colors.CYAN}{name}{Colors.RESET}")
                print(f"{Colors.DIM}   Arguments:{Colors.RESET}")
                truncated = {}
                for k, v in args.items():
                    vs = str(v)
                    truncated[k] = vs[:200] + "..." if len(vs) > 200 else v
                for line in json.dumps(truncated, indent=2, ensure_ascii=False).split("\n"):
                    print(f"   {Colors.DIM}{line}{Colors.RESET}")

            case ToolCallResult(success=ok, content=text, error=err, raw_output=raw_output, user_visible=user_visible):
                if not user_visible:
                    return
                if ok:
                    display = text[:300] + f"{Colors.DIM}...{Colors.RESET}" if len(text) > 300 else text
                    print(f"{Colors.BRIGHT_GREEN}✓ Result:{Colors.RESET} {display}")
                    if raw_output and raw_output.get("type") == "memory_search":
                        self._render_memory_search(raw_output)
                else:
                    print(f"{Colors.BRIGHT_RED}✗ Error:{Colors.RESET} {Colors.RED}{err}{Colors.RESET}")

            case ArtifactEvent(kind=kind, filename=fname, rel_path=rel, size=sz):
                size_label = _format_size(sz)
                print(f"{Colors.BRIGHT_CYAN}📎 {kind}{Colors.RESET} {fname} · {size_label} · {Colors.DIM}{rel}{Colors.RESET}")

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
                print(f"\n{Colors.DIM}💉 Injected:{Colors.RESET} {Colors.BRIGHT_WHITE}{preview}{Colors.RESET}")

            case StepEnd(step=s, elapsed_seconds=el, total_elapsed_seconds=tot):
                print(f"\n{Colors.DIM}⏱️  Step {s} completed in {el:.2f}s (total: {tot:.2f}s){Colors.RESET}")

            case DoneEvent(stop_reason=reason, final_content=_):
                if reason == StopReason.CANCELLED:
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  Task cancelled by user.{Colors.RESET}")
                elif reason == StopReason.MAX_STEPS:
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {event.final_content}{Colors.RESET}")

            case _:
                pass  # TokenUsageEvent etc. — no terminal output needed

    def _render_memory_search(self, raw_output: dict) -> None:
        """Render structured memory_search matches in the terminal."""
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

    def get_history(self) -> list[Message]:
        """Get message history."""
        return self.messages.copy()
