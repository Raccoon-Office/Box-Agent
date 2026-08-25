"""Public Agent facade.

The heavy lifting lives behind ``box_agent.runtime.run_agent_loop``.
This module keeps the public ``Agent`` API backward-compatible while
giving adapters one stable entry point for configuring and running a turn.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sys
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .events import (
    AgentEvent,
    ArtifactEvent,
    ContentEvent,
    ContextCheckpointEvent,
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
from .context_resources import ContextResourceLedger
from .config import AgentConfig, ToolLimitsConfig
from .llm import LLMClient
from .logger import AgentLogger
from .loop_guards import CompletionGate
from .runtime import run_agent_loop
from .schema import Message
from .tools.base import Tool, ToolResult, build_tool_name_index
from .tools.mcp_tool_catalog import get_mcp_tool_catalog
from .tools.mcp_tool_search import (
    ActivatedMCPTool,
    MCPToolExposureManager,
    ToolSearchTool,
)
from .tools.skill_preload import build_active_skills_prompt
from .tool_result_storage import ToolResultStorage
from .utils import calculate_display_width
from .workflow_policy import WorkflowPolicy
from .session_continuation import ContinuationMessage


_log = logging.getLogger(__name__)
_ACTIVE_SKILL_TOKEN_BUDGET = 32_000
_MAX_SOURCE_TEXT_ENV_CHARS = 120_000
_DEFAULT_AGENT_CONFIG = AgentConfig()


@dataclass(frozen=True, slots=True)
class AgentRunOptions:
    """Complete per-turn integration options for :meth:`Agent.run_events`.

    Obtain a correctly populated instance with
    :meth:`Agent.default_run_options`, then use ``dataclasses.replace`` for
    host-specific overrides.  Session configuration such as tools, context
    limits, and parallelism remains owned by the ``Agent`` instance.
    """

    llm: Any
    summary_llm: Any | None = None
    is_cancelled: Callable[[], bool] | None = None
    logger: AgentLogger | None = None
    permission_negotiator: Any | None = None
    hooks: list[Any] | None = None
    memory_manager: Any | None = None
    memory_extractor: Any | None = None
    memory_turn_id: str = ""
    inject_queue: asyncio.Queue[Any] | None = None
    session_id: str = ""
    turn_id: str = ""
    title: str = ""
    force_plan_start: bool = False
    require_plan_approval: bool = False
    plan_approval: dict[str, Any] | None = None
    plan_start_text: str | None = None
    pause_after_plan_write: bool = False
    max_tool_calls: int | None = None
    web_search_total_limit: int | None = None
    no_progress_limit: int | None = None
    completion_gate: CompletionGate | None = None
    artifact_detection_enabled: bool = True
    artifact_root_dir: str | Path | None = None
    cache_fingerprint_context: dict[str, Any] | None = None
    cache_fingerprint_sink: Callable[[dict[str, Any]], None] | None = None
    workflow_policy: WorkflowPolicy | None = None
    current_turn_text: str | None = None


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

    def compaction_state(self) -> tuple[str, str]:
        return "Goal", json.dumps(_goal_snapshot(self._agent), ensure_ascii=False)

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
        max_steps: int = _DEFAULT_AGENT_CONFIG.max_steps,
        workspace_dir: str = "./workspace",
        token_limit: int = 113400,
        hooks: list | None = None,
        thinking_enabled: bool = False,
        memory_promotion_enabled: bool = False,
        memory_promotion_hit_threshold: int = 5,
        memory_promotion_cooldown_days: int = 14,
        max_parallel_tools: int = 8,
        parallel_tool_timeout_seconds: float | None = 900.0,
        provider_stale_seconds: float | None = None,
        truncation_continuation_enabled: bool = True,
        max_truncation_continuations: int = 3,
        max_truncated_tool_call_retries: int = 3,
        truncated_tool_call_boost_cap: int = 32768,
        context_resource_dedup_enabled: bool = True,
        tool_limits: ToolLimitsConfig | None = None,
        deferred_mcp_loading_enabled: bool = True,
    ):
        self.llm = llm_client
        self.tools = {
            tool.name: tool
            for tool in tools
            if not deferred_mcp_loading_enabled
            or getattr(tool, "mcp_tool_id", None) is None
        }
        self.max_steps = max_steps
        self.tool_limits = tool_limits or ToolLimitsConfig()
        self.max_parallel_tools = max_parallel_tools
        self.parallel_tool_timeout_seconds = parallel_tool_timeout_seconds
        self.provider_stale_seconds = provider_stale_seconds
        self.truncation_continuation_enabled = truncation_continuation_enabled
        self.max_truncation_continuations = max_truncation_continuations
        self.max_truncated_tool_call_retries = max_truncated_tool_call_retries
        self.truncated_tool_call_boost_cap = truncated_tool_call_boost_cap
        self.context_resource_dedup_enabled = context_resource_dedup_enabled
        self.context_resource_ledger = ContextResourceLedger()
        self.activated_mcp_tools: OrderedDict[str, ActivatedMCPTool] = OrderedDict()
        self.mcp_tool_exposure: MCPToolExposureManager | None = None
        if deferred_mcp_loading_enabled:
            catalog = get_mcp_tool_catalog()
            self.mcp_tool_exposure = MCPToolExposureManager(
                catalog,
                self.activated_mcp_tools,
            )
            self.tools["tool_search"] = ToolSearchTool(
                catalog,
                self.activated_mcp_tools,
                protected_names_provider=lambda: frozenset(
                    build_tool_name_index(self.tools.values())
                ),
            )
        self.tool_result_storage = ToolResultStorage(
            Path.home() / ".box-agent" / "sessions"
        )
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        self.cancel_event: Optional[asyncio.Event] = None
        self.inject_queue: asyncio.Queue[Any] = asyncio.Queue()
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
                "This directory is the session workspace and default working root; "
                "it does not by itself define every path the runtime may allow. "
                "Relative tool paths resolve from each tool's active "
                "project/artifact root; in output mode, prefer the artifact-relative "
                "paths named by the active Skill or checkpoint instead of deriving "
                "absolute paths from this workspace."
            )
            system_prompt = system_prompt + workspace_info

        if self.mcp_tool_exposure is not None:
            system_prompt = (
                f"{system_prompt.rstrip()}\n\n## Deferred MCP tools\n"
                "Use `tool_search` when the visible tools do not cover the task. "
                "Every returned match is activated for this session; only those matches "
                "are added by their real tool name on the next step, while unreturned "
                "deferred tools remain hidden. Tools explicitly configured as alwaysLoad "
                "are already visible without search. Prefer a short capability or one exact "
                "tool name per search; do not combine several concrete tool names and task "
                "instructions into one query. Set `top_k` to the number of schemas "
                "the task actually needs; do not assume a small fixed cap. A successful "
                "`mcp_config` write only updates "
                "configuration; do not claim the server is connected until an internal "
                "MCP runtime update confirms registration. If that confirmation arrives "
                "during the turn, use `tool_search` to discover the newly registered "
                "capability instead of expecting all schemas to appear at once."
            )

        self.system_prompt = system_prompt
        self._active_skill_prompts: dict[str, str] = {}
        self._active_skill_hashes: dict[str, str] = {}
        self._active_skill_load_order: dict[str, int] = {}
        self._active_skill_sequence = 0
        self._bound_user_source_text = ""
        for tool in self.tools.values():
            if hasattr(tool, "set_parent_system_prompt"):
                tool.set_parent_system_prompt(system_prompt)
            # Give sub-agents a live view of the parent's currently visible real
            # tools. Deferred MCP discovery stays parent-owned; once activated,
            # a real MCP tool becomes inheritable without exposing tool_search.
            if hasattr(tool, "set_tool_provider"):
                tool.set_tool_provider(self._inherited_tools)
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]
        self.logger = AgentLogger()
        self.api_total_tokens: int = 0
        self.cache_fingerprint_context: dict[str, object] = {}
        self._streaming_active: bool = False  # Track if streaming output needs trailing newline
        self.last_stop_reason: str | None = None
        self.last_checkpoint: dict[str, Any] | None = None
        self.goal: GoalState | None = None
        self.tools["goal_read"] = _GoalReadTool(self)
        self.tools["goal_write"] = _GoalWriteTool(self)

    def _inherited_tools(self) -> dict[str, Tool]:
        if self.mcp_tool_exposure is None:
            return self.tools
        return self.mcp_tool_exposure.inherited_tools(self.tools)

    def set_system_prompt(self, system_prompt: str) -> None:
        """Update the live system prompt while preserving active skills."""
        rendered_prompt = build_active_skills_prompt(
            system_prompt,
            self._active_skill_prompts,
        )
        self.system_prompt = rendered_prompt
        if self.messages and self.messages[0].role == "system":
            self.messages[0] = Message(role="system", content=rendered_prompt)
        for tool in self.tools.values():
            if hasattr(tool, "set_parent_system_prompt"):
                tool.set_parent_system_prompt(rendered_prompt)

    def activate_skill_instructions(self, skill_name: str, skill_prompt: str) -> None:
        """Pin an on-demand skill in the managed system-prompt tail block."""
        normalized_name = skill_name.strip()
        if not normalized_name or not skill_prompt.strip():
            return
        prompt_hash = hashlib.sha256(skill_prompt.encode("utf-8")).hexdigest()
        if self._active_skill_hashes.get(normalized_name) == prompt_hash:
            return
        self._active_skill_prompts[normalized_name] = skill_prompt
        self._active_skill_hashes[normalized_name] = prompt_hash
        self._active_skill_sequence += 1
        self._active_skill_load_order[normalized_name] = self._active_skill_sequence
        self.set_system_prompt(self.system_prompt)
        diagnostics = self.active_skill_diagnostics()
        if diagnostics["budget_exceeded"]:
            _log.warning(
                "active skill prompt budget exceeded: names=%s estimated_tokens=%d budget=%d; "
                "instructions were preserved without silent truncation",
                diagnostics["names"],
                diagnostics["estimated_tokens"],
                diagnostics["token_budget"],
            )

    def deactivate_skill_instructions(self, skill_name: str) -> bool:
        """Explicitly remove one on-demand skill from the managed prompt tail."""
        normalized_name = skill_name.strip()
        if normalized_name not in self._active_skill_prompts:
            return False
        del self._active_skill_prompts[normalized_name]
        self._active_skill_hashes.pop(normalized_name, None)
        self._active_skill_load_order.pop(normalized_name, None)
        self.set_system_prompt(self.system_prompt)
        return True

    def clear_active_skill_instructions(self) -> None:
        """Clear on-demand skills at an explicit task/session boundary."""
        if not self._active_skill_prompts:
            return
        self._active_skill_prompts.clear()
        self._active_skill_hashes.clear()
        self._active_skill_load_order.clear()
        self.set_system_prompt(self.system_prompt)

    def active_skill_diagnostics(self) -> dict[str, object]:
        """Return metadata-only prompt budget diagnostics (never skill text)."""
        ordered_names = tuple(
            sorted(
                self._active_skill_prompts,
                key=lambda name: self._active_skill_load_order.get(name, 0),
            )
        )
        estimated_tokens = sum(
            max(1, len(self._active_skill_prompts[name]) // 4)
            for name in ordered_names
        )
        return {
            "names": ordered_names,
            "hashes": tuple(
                (name, self._active_skill_hashes[name]) for name in ordered_names
            ),
            "estimated_tokens": estimated_tokens,
            "token_budget": _ACTIVE_SKILL_TOKEN_BUDGET,
            "budget_exceeded": estimated_tokens > _ACTIVE_SKILL_TOKEN_BUDGET,
        }

    def add_user_message(self, content: str):
        """Add a user message to history."""
        if self.goal is not None and self.goal.status == "active":
            content = self._apply_goal_context(content)
        self.messages.append(Message(role="user", content=content))

    def bind_user_source_text(self, user_request: str) -> str:
        """Expose accumulated real user text to provenance-aware local tools.

        CLI and ACP call this only at real user-turn boundaries. Internal
        continuation prompts must not become source facts. Base64 preserves
        punctuation and newlines across subprocess environments; it is not a
        secrecy mechanism.
        """
        request = user_request.strip()
        if request:
            self._bound_user_source_text = (
                f"{self._bound_user_source_text.rstrip()}\n\n{request}"
                if self._bound_user_source_text.strip()
                else request
            )
            if len(self._bound_user_source_text) > _MAX_SOURCE_TEXT_ENV_CHARS:
                self._bound_user_source_text = self._bound_user_source_text[
                    -_MAX_SOURCE_TEXT_ENV_CHARS:
                ]
        encoded = base64.b64encode(
            self._bound_user_source_text.encode("utf-8")
        ).decode("ascii")
        bash_tool = self.tools.get("bash")
        if bash_tool is not None and hasattr(bash_tool, "update_runtime_env"):
            bash_tool.update_runtime_env({"BOX_AGENT_SOURCE_TEXT_B64": encoded})
        return self._bound_user_source_text

    def seed_continuation_messages(
        self, messages: tuple[ContinuationMessage, ...]
    ) -> int:
        """Seed semantic history into a fresh Agent without tool protocol state."""

        if len(self.messages) != 1 or self.messages[0].role != "system":
            return 0
        for message in messages:
            self.messages.append(Message(role=message.role, content=message.content))
        return len(messages)

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

    def set_permission_negotiator(self, negotiator: Any | None) -> None:
        """Configure the session-level permission negotiation adapter."""
        self._permission_negotiator = negotiator

    def set_memory_extractor(self, extractor: Any | None) -> None:
        """Configure the session-level memory extraction service."""
        self._memory_extractor = extractor

    def set_memory_proposal_negotiator(self, negotiator: Any | None) -> None:
        """Configure the terminal renderer's memory proposal handler."""
        self._proposal_negotiator = negotiator

    def clear_history(self) -> int:
        """Clear conversation turns while preserving the system message.

        Returns the number of removed messages.
        """
        removed = max(0, len(self.messages) - 1)
        del self.messages[1:]
        self.context_resource_ledger.rotate_epoch()
        return removed

    def _check_cancelled(self) -> bool:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    def default_run_options(self) -> AgentRunOptions:
        """Return a complete snapshot of the default integration options."""
        return AgentRunOptions(
            llm=self.llm,
            summary_llm=None,
            is_cancelled=self._check_cancelled,
            logger=self.logger,
            permission_negotiator=self._permission_negotiator,
            hooks=self._hooks,
            memory_manager=getattr(self._memory_extractor, "_mgr", None),
            memory_extractor=self._memory_extractor,
            inject_queue=self.inject_queue,
            cache_fingerprint_context=self.cache_fingerprint_context,
        )

    # ── Event-stream API (new) ──────────────────────────────

    async def run_events(
        self,
        cancel_event: Optional[asyncio.Event] = None,
        *,
        options: AgentRunOptions | None = None,
        force_plan_start: bool | None = None,
        require_plan_approval: bool | None = None,
        plan_approval: dict | None = None,
        pause_after_plan_write: bool | None = None,
        completion_gate: CompletionGate | None = None,
        artifact_detection_enabled: bool | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute the agent loop, yielding structured events.

        This is the preferred API for consumers that want fine-grained
        control over rendering (e.g. ACP, JSON-RPC, custom UIs).  Integrations
        with host-specific services should pass ``AgentRunOptions`` instead of
        calling the low-level core loop.
        """
        effective_options = options or self.default_run_options()

        if cancel_event is not None:
            self.cancel_event = cancel_event
            effective_options = replace(
                effective_options,
                is_cancelled=self._check_cancelled,
            )

        legacy_overrides: dict[str, Any] = {}
        if force_plan_start is not None:
            legacy_overrides["force_plan_start"] = force_plan_start
        if require_plan_approval is not None:
            legacy_overrides["require_plan_approval"] = require_plan_approval
        if plan_approval is not None:
            legacy_overrides["plan_approval"] = plan_approval
        if pause_after_plan_write is not None:
            legacy_overrides["pause_after_plan_write"] = pause_after_plan_write
        if completion_gate is not None:
            legacy_overrides["completion_gate"] = completion_gate
        if artifact_detection_enabled is not None:
            legacy_overrides["artifact_detection_enabled"] = artifact_detection_enabled
        if legacy_overrides:
            effective_options = replace(effective_options, **legacy_overrides)

        sub_agent_tool = self.tools.get("sub_agent")
        set_child_negotiator = getattr(
            sub_agent_tool,
            "set_permission_negotiator",
            None,
        )
        if callable(set_child_negotiator):
            set_child_negotiator(effective_options.permission_negotiator)

        events = run_agent_loop(
            llm=effective_options.llm,
            summary_llm=effective_options.summary_llm,
            messages=self.messages,
            tools=self.tools,
            max_steps=self.max_steps,
            tool_limits=self.tool_limits,
            max_tool_calls=effective_options.max_tool_calls,
            web_search_total_limit=effective_options.web_search_total_limit,
            token_limit=self.token_limit,
            is_cancelled=effective_options.is_cancelled,
            logger=effective_options.logger,
            workspace_dir=str(self.workspace_dir),
            permission_negotiator=effective_options.permission_negotiator,
            hooks=effective_options.hooks,
            memory_manager=effective_options.memory_manager,
            memory_extractor=effective_options.memory_extractor,
            memory_turn_id=effective_options.memory_turn_id,
            memory_promotion_enabled=self.memory_promotion_enabled,
            memory_promotion_hit_threshold=self.memory_promotion_hit_threshold,
            memory_promotion_cooldown_days=self.memory_promotion_cooldown_days,
            inject_queue=effective_options.inject_queue,
            thinking_enabled=self.thinking_enabled,
            session_id=effective_options.session_id,
            turn_id=effective_options.turn_id,
            title=effective_options.title,
            max_parallel_tools=self.max_parallel_tools,
            parallel_tool_timeout_seconds=self.parallel_tool_timeout_seconds,
            provider_stale_seconds=self.provider_stale_seconds,
            force_plan_start=effective_options.force_plan_start,
            require_plan_approval=effective_options.require_plan_approval,
            plan_approval=effective_options.plan_approval,
            plan_start_text=effective_options.plan_start_text,
            pause_after_plan_write=effective_options.pause_after_plan_write,
            no_progress_limit=effective_options.no_progress_limit,
            completion_gate=effective_options.completion_gate,
            truncation_continuation_enabled=self.truncation_continuation_enabled,
            max_truncation_continuations=self.max_truncation_continuations,
            max_truncated_tool_call_retries=self.max_truncated_tool_call_retries,
            truncated_tool_call_boost_cap=self.truncated_tool_call_boost_cap,
            artifact_detection_enabled=effective_options.artifact_detection_enabled,
            artifact_root_dir=effective_options.artifact_root_dir,
            cache_fingerprint_context=effective_options.cache_fingerprint_context,
            cache_fingerprint_sink=effective_options.cache_fingerprint_sink,
            active_skill_activator=self.activate_skill_instructions,
            workflow_policy=effective_options.workflow_policy,
            current_turn_text=effective_options.current_turn_text,
            context_resource_ledger=self.context_resource_ledger,
            context_resource_dedup_enabled=self.context_resource_dedup_enabled,
            tool_exposure_manager=self.mcp_tool_exposure,
            tool_result_storage=self.tool_result_storage,
        )
        async for event in events:
            # Track token usage on Agent instance for backward compat
            if isinstance(event, TokenUsageEvent):
                self.api_total_tokens = event.total_tokens
            if isinstance(event, DoneEvent):
                write_tool = self.tools.get("write_file")
                cleanup = getattr(write_tool, "cleanup_pending_writes", None)
                if callable(cleanup):
                    discarded = cleanup()
                    if discarded:
                        _log.info(
                            "write_file discarded incomplete transactions: %s",
                            discarded,
                        )
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
        current_turn_text: str | None = None,
    ) -> str:
        """Execute agent loop with terminal rendering.

        Signature and return value are unchanged from before the refactor.
        Internally it now consumes ``run_events()``.
        """
        final_content = ""
        self.last_stop_reason = None
        self.last_checkpoint = None
        options = self.default_run_options()
        if current_turn_text is not None:
            options = replace(options, current_turn_text=current_turn_text)
        async for event in self.run_events(
            cancel_event,
            options=options,
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
            elif isinstance(event, ContextCheckpointEvent):
                self.last_checkpoint = {
                    "checkpointId": event.checkpoint_id,
                    "workflowKind": event.workflow_kind,
                    "adapterId": event.adapter_id,
                    "schemaVersion": event.schema_version,
                    "workspaceIdentity": event.workspace_identity,
                    "path": event.path,
                    "stage": event.stage,
                    "artifactCount": event.artifact_count,
                    "artifactSetSha256": event.artifact_set_sha256,
                }
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

            case ContextCheckpointEvent(checkpoint_id=checkpoint_id, stage=stage):
                stage_text = f" · stage {stage}" if stage else ""
                print(
                    f"\n{Colors.BRIGHT_YELLOW}⏸️  Progress saved and task paused"
                    f"{stage_text} · checkpoint {checkpoint_id[:12]}.{Colors.RESET}"
                )

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
