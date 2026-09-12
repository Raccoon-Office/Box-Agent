"""Public Agent facade.

The heavy lifting lives behind ``box_agent.runtime.run_agent_loop``.
This module keeps the public ``Agent`` API backward-compatible while
giving adapters one stable entry point for configuring and running a turn.
"""

from __future__ import annotations

import asyncio
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
    ToolCallStart,
)
from .context_resources import ContextResourceLedger
from .config import AgentConfig, ToolLimitsConfig
from .llm import LLMClient
from .logger import AgentLogger
from .kernel.ports import KernelServices
from .runtime import run_agent_loop
from .schema import Message
from .session_log import SessionLog
from .tools.base import Tool, ToolResult, build_tool_name_index
from .tools.local_tool_exposure import LocalToolExposurePolicy
from .tools.mcp_tool_catalog import MCPToolCatalog, get_mcp_tool_catalog
from .tools.mcp_tool_search import (
    ActivatedMCPTool,
    MCPToolExposureManager,
    ToolSearchTool,
)
from .skill_runtime import SkillRuntime
from .skill_dependencies import SkillDependencyError
from .tool_result_storage import ToolResultStorage
from .cli_renderer import CliRenderer, Colors, _format_size, render_agent_events
from .session_continuation import ContinuationMessage

from box_agent.user_paths import state_path


_log = logging.getLogger(__name__)
_ACTIVE_SKILL_TOKEN_BUDGET = 32_000
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
    max_delegated_tool_calls: int | None = None
    web_search_total_limit: int | None = None
    no_progress_limit: int | None = None
    artifact_detection_enabled: bool = True
    artifact_root_dir: str | Path | None = None  # deprecated; accepted and ignored
    cache_fingerprint_context: dict[str, Any] | None = None
    cache_fingerprint_sink: Callable[[dict[str, Any]], None] | None = None
    current_turn_text: str | None = None
    kernel_services: KernelServices | None = None
    run_control: Any | None = None
    plugins: tuple[Any, ...] = ()


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
        allowed_connector_ids_provider: Callable[[], frozenset[str]] | None = None,
        tool_limits: ToolLimitsConfig | None = None,
        deferred_mcp_loading_enabled: bool = True,
        session_log: SessionLog | None = None,
        enable_builtin_tools: bool = True,
        plugins: tuple[Any, ...] = (),
        skill_runtime: SkillRuntime | None = None,
        session_id: str = "",
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
        self.activated_local_tools: OrderedDict[str, Tool] = OrderedDict()
        self.local_tool_exposure = LocalToolExposurePolicy(
            lambda: self.tools,
            goal_provider=lambda: getattr(self, "goal", None),
            active_skills_provider=lambda: self.skill_runtime.active_names,
        )
        self.mcp_tool_exposure: MCPToolExposureManager | None = None
        if enable_builtin_tools:
            # Eager MCP remains eager. Its local discovery uses an isolated empty
            # catalog so a process-global deferred server cannot leak into this mode.
            catalog = get_mcp_tool_catalog() if deferred_mcp_loading_enabled else MCPToolCatalog()
            self.mcp_tool_exposure = MCPToolExposureManager(
                catalog,
                self.activated_mcp_tools,
                activated_local_tools=self.activated_local_tools,
                deferred_local_names_provider=self.local_tool_exposure.deferred_names,
                deferred_mcp=deferred_mcp_loading_enabled,
                allowed_connector_ids_provider=allowed_connector_ids_provider,
            )
            self.tools["tool_search"] = ToolSearchTool(
                catalog,
                self.activated_mcp_tools,
                protected_names_provider=lambda: frozenset(
                    build_tool_name_index(
                        tool for tool in self.tools.values()
                        if getattr(tool, "mcp_tool_id", None) is None
                    )
                ),
                local_tools_provider=self.local_tool_exposure.candidate_tools,
                activated_local_tools=self.activated_local_tools,
                allowed_connector_ids_provider=allowed_connector_ids_provider,
            )
        self.tool_result_storage = ToolResultStorage(
            state_path('sessions')
        )
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        self.cancel_event: Optional[asyncio.Event] = None
        self.inject_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._permission_negotiator = None  # set by CLI/ACP when permission engine is active
        self._proposal_negotiator = None  # set by CLI/ACP to handle MemoryProposalEvent
        self._hooks = hooks
        self._plugins = tuple(plugins)
        self._memory_extractor = None  # set by CLI/ACP when memory extraction is enabled
        self.thinking_enabled = thinking_enabled
        self.memory_promotion_enabled = memory_promotion_enabled
        self.memory_promotion_hit_threshold = memory_promotion_hit_threshold
        self.memory_promotion_cooldown_days = memory_promotion_cooldown_days
        self._deprecated_artifact_root_warned = False

        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        caller_system_prompt = system_prompt
        if "Current Workspace" not in system_prompt:
            workspace_info = (
                f"\n\n## Current Workspace\n"
                f"You are currently working in: `{self.workspace_dir.absolute()}`\n"
                "This directory is the stable session cwd and default working root; "
                "it does not by itself define every path the runtime may allow. "
                "Relative tool paths resolve from this cwd. Model-created task "
                "subdirectories organize files but do not change the session cwd."
            )
            system_prompt = system_prompt + workspace_info

        if self.mcp_tool_exposure is not None:
            system_prompt = (
                f"{system_prompt.rstrip()}\n\n## Discoverable tools\n"
                "Use `tool_search` when the visible tools do not cover the task. "
                "Allowed local utilities include file append, diagnostics, plans, "
                "progress, goals, memory, scheduling and integrations; connected "
                "deferred MCP capabilities are searchable through the same entry. "
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
                "capability instead of expecting all schemas to appear at once. "
                "The latest <connector-status> in each user message is authoritative: "
                "only its connected entries may be searched or called. If an entry is "
                "disabled or disappears, do not call a previously activated tool from it."
            )

        # Only this host knows the exact rules it appended after caller input.
        self._system_prompt_tail = system_prompt[len(caller_system_prompt.rstrip()):]
        self.system_prompt = system_prompt
        from .plugins.defaults import _bind_skill_store, skill_loader_from_catalog
        loader = skill_loader_from_catalog(self.tools)
        self.skill_runtime = skill_runtime if skill_runtime is not None else SkillRuntime(
            loader, session_log=session_log
        )
        _bind_skill_store(self.skill_runtime, session_log)
        for tool in self.tools.values():
            if hasattr(tool, "set_parent_system_prompt"):
                tool.set_parent_system_prompt(system_prompt)
            # Give sub-agents the allowed local map and activated/eager MCP tools.
            # Parent schema visibility must not remove delegated local capability;
            # deferred MCP discovery still stays parent-owned.
            if hasattr(tool, "set_tool_provider"):
                tool.set_tool_provider(self._inherited_tools)
            if session_log is not None and hasattr(tool, "set_parent_session_log"):
                tool.set_parent_session_log(session_log)
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]
        self.logger = AgentLogger()
        self.api_total_tokens: int = 0
        self.cache_fingerprint_context: dict[str, object] = {}
        self._streaming_active: bool = False  # Track if streaming output needs trailing newline
        self.last_stop_reason: str | None = None
        self.goal: GoalState | None = None
        if enable_builtin_tools:
            self.tools["goal_read"] = _GoalReadTool(self)
            self.tools["goal_write"] = _GoalWriteTool(self)
        self.session_log = session_log
        self._pending_skill_restore: list[dict[str, Any]] = []
        self._skill_persistence_pending = False
        self.session_id = session_id.strip()
        if self.session_log is not None:
            projection = self.session_log.replay()
            self.messages.extend(projection.messages)
            self.restore_goal(projection.goal)
            plan_tool = self.tools.get("plan_write")
            configure_plan = getattr(plan_tool, "configure_session_persistence", None)
            if callable(configure_plan):
                configure_plan(self.session_log, projection.plan)
            todo_tool = self.tools.get("todo_write")
            configure_todos = getattr(todo_tool, "configure_session_persistence", None)
            if callable(configure_todos):
                configure_todos(self.session_log, projection.todos)
            self.restored_skills = projection.skills
            if self.restored_skills:
                try:
                    self.skill_runtime.restore_records(self.restored_skills)
                except SkillDependencyError as exc:
                    if exc.code != "SKILL_PROVIDER_UNAVAILABLE":
                        raise
                    # Legacy callers may supply current text through the
                    # tuple restore API after construction, before execution.
                    self._pending_skill_restore = self.restored_skills
        else:
            self.restored_skills = []
        self._sync_child_system_prompt()

    def _persist_goal(self) -> None:
        if self.session_log is None:
            return
        self.session_log.append("goal/change", {"goal": goal_payload(self.goal)})
        self.session_log.flush()

    def _next_session_turn(self) -> int:
        if self.session_log is None:
            raise RuntimeError("session log is not configured")
        turns = [
            event["data"].get("turn")
            for event in self.session_log.events
            if event["type"] == "turn/start"
        ]
        numeric_turns = [turn for turn in turns if isinstance(turn, int)]
        return max(numeric_turns, default=0) + 1

    def _persist_unlogged_messages(
        self,
        *,
        turn: int,
        step: int | None,
        tool_result_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if self.session_log is None:
            return
        self.session_log.append_unlogged_messages(
            self.messages[1:],
            turn=turn,
            step=step,
            tool_result_metadata=tool_result_metadata,
        )

    @staticmethod
    def _session_turn_reason(stop_reason: StopReason) -> dict[str, Any]:
        if stop_reason is StopReason.END_TURN:
            return {"kind": "completed"}
        if stop_reason is StopReason.CANCELLED:
            return {"kind": "aborted", "message": "cancelled"}
        if stop_reason is StopReason.ERROR:
            return {"kind": "error"}
        return {"kind": stop_reason.value}

    def _inherited_tools(self) -> dict[str, Tool]:
        if self.mcp_tool_exposure is None:
            return self.tools
        return self.mcp_tool_exposure.inherited_tools(self.tools)

    def set_system_prompt(self, system_prompt: str) -> None:
        """Replace system rules; Skill reference text is projected separately."""
        self.system_prompt = system_prompt
        if self.messages and self.messages[0].role == "system":
            self.messages[0] = Message(role="system", content=system_prompt)
        self._sync_child_system_prompt()

    def _project_system_prompt(self, prompt: str, suffixes: str | tuple[str, ...]) -> str:
        from .skill_context import effective_system_prompt

        tail = self._system_prompt_tail
        if tail and prompt.endswith(tail):
            caller_prompt = prompt[:-len(tail)]
            effective = effective_system_prompt(caller_prompt, suffixes)
            if effective != caller_prompt:
                return effective + tail
        return effective_system_prompt(prompt, suffixes)

    def _sync_child_system_prompt(self) -> None:
        system_prompt = self._project_system_prompt(self.system_prompt, self.skill_runtime.legacy_system_suffixes)
        for tool in self.tools.values():
            if hasattr(tool, "set_parent_system_prompt"):
                tool.set_parent_system_prompt(system_prompt)

    def activate_skill_instructions(self, skill_name: str, skill_prompt: str) -> None:
        """Deprecated host API: select ordinary reference material for this turn."""
        if skill_name.strip() and skill_prompt.strip():
            name = skill_name.strip()
            previous_sequence = self.skill_runtime.state.sequence
            replaces_pending = any(row["name"] == name for row in self._pending_skill_restore)
            order = max([previous_sequence, *(row["loadOrder"] for row in self._pending_skill_restore)]) + 1
            self.skill_runtime.register_reference(name, skill_prompt, order=order, persist=False)
            self._pending_skill_restore = [row for row in self._pending_skill_restore if row["name"] != name]
            if (replaces_pending or self.skill_runtime.state.sequence != previous_sequence
                    or self._skill_persistence_pending):
                self._persist_active_skills()

    def deactivate_skill_instructions(self, skill_name: str) -> bool:
        name = skill_name.strip()
        pending = any(row["name"] == name for row in self._pending_skill_restore)
        removed = self.skill_runtime.deactivate_reference(name) or pending
        self._pending_skill_restore = [row for row in self._pending_skill_restore if row["name"] != name]
        if removed or self._skill_persistence_pending:
            self._persist_active_skills()
        return removed

    def clear_active_skill_instructions(self) -> None:
        self.skill_runtime.clear_references()
        self._pending_skill_restore = []
        self._persist_active_skills()

    def _persist_active_skills(self) -> None:
        if self.session_log is not None:
            self._skill_persistence_pending = True
            records = {row["name"]: row for row in self._pending_skill_restore}
            records.update((row["name"], row) for row in self.skill_runtime.log_records())
            self.session_log.append("skill/change", {
                "skills": sorted(records.values(), key=lambda row: row["loadOrder"]),
            })
            self.session_log.flush()
            self._skill_persistence_pending = False

    def restore_active_skill_instructions(self, skills: list[tuple[str, str, str, int]]) -> None:
        """Restore current valid references; historical hashes remain provenance."""
        self.skill_runtime.restore_records([
            {"name": name, "prompt": prompt, "sha256": historical_hash, "loadOrder": order}
            for name, prompt, historical_hash, order in skills
        ])
        self._pending_skill_restore = []
        self._sync_child_system_prompt()

    def active_skill_diagnostics(self) -> dict[str, object]:
        """Return metadata for effective references without exposing their bodies."""
        valid = set(self.skill_runtime.active_names)
        records = [item for item in sorted(self.skill_runtime.state.reads.values(), key=lambda item: item.order)
                   if item.name in valid]
        estimated = sum(max(1, len(item.prompt) // 4) for item in records)
        return {"names": tuple(item.name for item in records),
                "hashes": tuple((item.name, item.revision) for item in records),
                "estimated_tokens": estimated, "token_budget": _ACTIVE_SKILL_TOKEN_BUDGET,
                "budget_exceeded": estimated > _ACTIVE_SKILL_TOKEN_BUDGET}

    def add_user_message(self, content: str):
        """Add a user message to history."""
        if self.goal is not None and self.goal.status == "active":
            content = self._apply_goal_context(content)
        self.messages.append(Message(role="user", content=content))

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
        self._persist_goal()
        return self.goal

    def pause_goal(self) -> GoalState | None:
        """Pause the current goal, if one exists."""
        if self.goal is None:
            return None
        self.goal.status = "paused"
        self.goal.updated_at = datetime.now().isoformat()
        self._persist_goal()
        return self.goal

    def resume_goal(self) -> GoalState | None:
        """Resume the current goal, if one exists."""
        if self.goal is None:
            return None
        self.goal.status = "active"
        self.goal.blocked_reason = None
        self.goal.updated_at = datetime.now().isoformat()
        self._persist_goal()
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
        self._persist_goal()
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
        self._persist_goal()
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
        self._persist_goal()
        return self.goal

    def clear_goal(self) -> GoalState | None:
        """Clear the current goal and return the removed state."""
        old_goal = self.goal
        self.goal = None
        self._persist_goal()
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
        if self.session_log is not None:
            # Commit the reset before changing live history. Use the existing
            # Store append/flush contract; custom stores need no new method.
            self.session_log.append("surface/reset", {"reason": "agent.clear_history"})
            self.session_log.flush()
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
            plugins=self._plugins,
            memory_manager=getattr(self._memory_extractor, "_mgr", None),
            memory_extractor=self._memory_extractor,
            inject_queue=self.inject_queue,
            session_id=self.session_id,
            max_tool_calls=self.tool_limits.general.max_tool_calls,
            max_delegated_tool_calls=(
                self.tool_limits.general.max_delegated_tool_calls
            ),
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
        artifact_detection_enabled: bool | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute the agent loop, yielding structured events.

        This is the preferred API for consumers that want fine-grained
        control over rendering (e.g. ACP, JSON-RPC, custom UIs).  Integrations
        with host-specific services should pass ``AgentRunOptions`` instead of
        calling the low-level core loop.
        """
        if self._skill_persistence_pending:
            self._persist_active_skills()
        if self._pending_skill_restore:
            self.skill_runtime.restore_records(self._pending_skill_restore)
            self._pending_skill_restore = []
        self._sync_child_system_prompt()
        self.skill_runtime.begin_turn()
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
        if artifact_detection_enabled is not None:
            legacy_overrides["artifact_detection_enabled"] = artifact_detection_enabled
        if legacy_overrides:
            effective_options = replace(effective_options, **legacy_overrides)

        if (effective_options.force_plan_start or effective_options.require_plan_approval
                or effective_options.pause_after_plan_write):
            self.local_tool_exposure.require_tools(("plan_read", "plan_write"))

        sub_agent_tool = self.tools.get("sub_agent")
        set_child_thinking = getattr(sub_agent_tool, "set_thinking_enabled", None)
        if callable(set_child_thinking):
            set_child_thinking(self.thinking_enabled)
        set_child_negotiator = getattr(
            sub_agent_tool,
            "set_permission_negotiator",
            None,
        )
        if callable(set_child_negotiator):
            set_child_negotiator(effective_options.permission_negotiator)

        session_turn: int | None = None
        session_step: int | None = None
        session_turn_open = False
        session_step_open = False
        if self.session_log is not None:
            session_turn = self._next_session_turn()
            self.session_log.append(
                "turn/start",
                {"turn": session_turn},
            )
            session_turn_open = True
            self._persist_unlogged_messages(turn=session_turn, step=None)

        if (
            effective_options.artifact_root_dir is not None
            and not self._deprecated_artifact_root_warned
        ):
            _log.warning(
                "artifact_root_dir is deprecated and ignored; the agent uses its workspace cwd"
            )
            self._deprecated_artifact_root_warned = True

        from .context_input import DefaultContextEngine

        run_arguments = dict(
            llm=effective_options.llm,
            summary_llm=effective_options.summary_llm,
            messages=self.messages,
            tools=self.tools,
            max_steps=self.max_steps,
            tool_limits=self.tool_limits,
            max_tool_calls=effective_options.max_tool_calls,
            max_delegated_tool_calls=effective_options.max_delegated_tool_calls,
            web_search_total_limit=effective_options.web_search_total_limit,
            token_limit=self.token_limit,
            is_cancelled=effective_options.is_cancelled,
            run_control=effective_options.run_control,
            logger=effective_options.logger,
            workspace_dir=str(self.workspace_dir),
            permission_negotiator=effective_options.permission_negotiator,
            hooks=effective_options.hooks,
            plugins=effective_options.plugins,
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
            truncation_continuation_enabled=self.truncation_continuation_enabled,
            max_truncation_continuations=self.max_truncation_continuations,
            max_truncated_tool_call_retries=self.max_truncated_tool_call_retries,
            truncated_tool_call_boost_cap=self.truncated_tool_call_boost_cap,
            artifact_detection_enabled=effective_options.artifact_detection_enabled,
            cache_fingerprint_context=effective_options.cache_fingerprint_context,
            cache_fingerprint_sink=effective_options.cache_fingerprint_sink,
            skill_engine=self.skill_runtime,
            context_engine=DefaultContextEngine(system_prompt_projector=self._project_system_prompt),
            current_turn_text=effective_options.current_turn_text,
            context_resource_ledger=self.context_resource_ledger,
            context_resource_dedup_enabled=self.context_resource_dedup_enabled,
            tool_exposure_manager=self.mcp_tool_exposure,
            tool_result_storage=self.tool_result_storage,
            session_log=self.session_log,
            session_turn=session_turn,
        )
        if effective_options.kernel_services is not None:
            run_arguments["kernel_services"] = effective_options.kernel_services
        events = run_agent_loop(**run_arguments)
        try:
            async for event in events:
                if self.session_log is not None and session_turn is not None:
                    if isinstance(event, (ContentEvent, ThinkingEvent)):
                        self.session_log.append(
                            "assistant/chunk",
                            {
                                "turn": session_turn,
                                "step": session_step,
                                "kind": (
                                    "thinking"
                                    if isinstance(event, ThinkingEvent)
                                    else "text"
                                ),
                                "content": event.content,
                            },
                        )
                    elif isinstance(event, StepStart):
                        session_step = event.step
                        self._persist_unlogged_messages(
                            turn=session_turn,
                            step=session_step,
                        )
                        self.session_log.append(
                            "step/start",
                            {"turn": session_turn, "step": session_step},
                        )
                        session_step_open = True
                    elif isinstance(event, StepEnd):
                        self._persist_unlogged_messages(
                            turn=session_turn,
                            step=event.step,
                        )
                        if session_step_open:
                            self.session_log.append(
                                "step/end",
                                {"turn": session_turn, "step": event.step},
                            )
                            self.session_log.flush()
                            session_step_open = False
                    elif isinstance(event, DoneEvent):
                        self._persist_unlogged_messages(
                            turn=session_turn,
                            step=session_step,
                        )
                        if session_step_open:
                            self.session_log.append(
                                "step/end",
                                {"turn": session_turn, "step": session_step},
                            )
                            session_step_open = False
                        self.session_log.append(
                            "turn/end",
                            {
                                "turn": session_turn,
                                "reason": self._session_turn_reason(event.stop_reason),
                            },
                        )
                        self.session_log.flush()
                        session_turn_open = False

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
        finally:
            try:
                close = getattr(events, "aclose", None)
                if callable(close):
                    await close()
            finally:
                if (
                    self.session_log is not None
                    and session_turn is not None
                    and session_turn_open
                    and not self.session_log.failed
                ):
                    self._persist_unlogged_messages(
                        turn=session_turn,
                        step=session_step,
                    )
                    if session_step_open:
                        self.session_log.append(
                            "step/end",
                            {"turn": session_turn, "step": session_step},
                        )
                    self.session_log.append(
                        "turn/end",
                        {"turn": session_turn, "reason": {"kind": "interrupted"}},
                    )
                    self.session_log.flush()

    # ── Backward-compatible run() ───────────────────────────

    async def run(
        self,
        cancel_event: Optional[asyncio.Event] = None,
        *,
        force_plan_start: bool = False,
        require_plan_approval: bool = False,
        plan_approval: dict | None = None,
        pause_after_plan_write: bool = False,
        artifact_detection_enabled: bool = True,
        current_turn_text: str | None = None,
    ) -> str:
        """Execute agent loop with terminal rendering.

        Signature and return value are unchanged from before the refactor.
        Internally it now consumes ``run_events()``.
        """
        options = self.default_run_options()
        if current_turn_text is not None:
            options = replace(options, current_turn_text=current_turn_text)
        events = self.run_events(
            cancel_event,
            options=options,
            force_plan_start=force_plan_start,
            require_plan_approval=require_plan_approval,
            plan_approval=plan_approval,
            pause_after_plan_write=pause_after_plan_write,
            artifact_detection_enabled=artifact_detection_enabled,
        )
        return await render_agent_events(self, events)

    # ── Terminal renderer ───────────────────────────────────

    def _render_event(self, event: AgentEvent) -> None:
        """Delegate terminal rendering while preserving the legacy method."""

        renderer = getattr(self, "_cli_renderer", None)
        if renderer is None:
            renderer = CliRenderer()
            self._cli_renderer = renderer
        renderer.streaming_active = getattr(
            self,
            "_streaming_active",
            renderer.streaming_active,
        )
        renderer.render(event)
        self._streaming_active = renderer.streaming_active

    def _render_memory_search(self, raw_output: dict) -> None:
        """Delegate structured memory rendering for compatibility callers."""

        renderer = getattr(self, "_cli_renderer", None)
        if renderer is None:
            renderer = CliRenderer()
            renderer.streaming_active = getattr(self, "_streaming_active", False)
            self._cli_renderer = renderer
        renderer.render_memory_search(raw_output)

    def get_history(self) -> list[Message]:
        """Get message history."""
        return self.messages.copy()
