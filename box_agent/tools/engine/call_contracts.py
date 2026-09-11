"""Explicit run inputs, call ownership, and model-step outcomes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...schema import Message
from ..base import Tool, ToolResult

if TYPE_CHECKING:
    from ...context_resources import ContextResourceLedger
    from ...events import ToolCallResult
    from ...kernel.ports import HookBusPort, HookDispatchPort
    from ...kernel.hook_types import HookContext
    from ...logger import AgentLogger
    from ...tool_result_storage import ToolResultStorage


@dataclass(frozen=True, slots=True)
class ToolExecutionOptions:
    tool_call_limits: dict[str, int]
    max_tool_calls: int | None
    max_delegated_tool_calls: int | None
    search_files_empty_result_limit: int
    web_search_batch_size: int
    web_search_concurrency: int
    max_parallel_tools: int
    batch_timeout_seconds: float | None
    activity_interval_seconds: float
    event_poll_interval_seconds: float
    cancel_grace_seconds: float
    artifact_detection_enabled: bool = True


@dataclass(frozen=True, slots=True)
class ToolRunContext:
    """Borrowed services and narrow kernel callbacks for one outer run."""

    messages: list[Message]
    hooks: HookBusPort
    result_storage: ToolResultStorage
    is_cancelled: Callable[[], bool]
    record_call: Callable[[ToolCallRecord, int], None]
    flush_calls: Callable[[], None]
    commit_result: Callable[[Message, ToolCallResult, int], None]
    validate_followup: Callable[[ToolResult, Tool | None, int], tuple[ToolResult, list[dict[str, Any]] | None, int]]
    policy_error: Callable[[str, dict[str, Any]], str | None]
    workspace_dir: str | None = None
    # Deprecated compatibility input; artifact discovery always uses workspace_dir.
    artifact_root_dir: str | Path | None = None
    session_id: str = ""
    turn_id: str = ""
    permission_negotiator: Any = None
    logger: AgentLogger | None = None
    resource_ledger: ContextResourceLedger | None = None
    activate_skill: Callable[[str, str], None] | None = None
    hook_dispatch: HookDispatchPort | None = None
    hook_context: HookContext | None = None
    skill_reader: Callable[..., ToolResult] | None = None


@dataclass(frozen=True, slots=True)
class ToolStepControl:
    """Conversation decisions supplied by the kernel, not made by tools."""

    step: int
    allowed_names: frozenset[str] | None = None
    blocked_reason: str = ""
    result_transform: Callable[[str, ToolResult], ToolResult] | None = None
    pending_followup_tokens: int = 0


@dataclass(slots=True)
class ToolCallRecord:
    """One logical call, including its final arguments and original outcome."""

    call_id: str
    requested_name: str
    name: str
    arguments: dict[str, Any]
    target: Tool | None
    tool_id: str | None = None
    server_name: str | None = None
    allowed: bool = False
    user_visible: bool = False
    rejection: str | None = None
    started_at: float = 0.0
    snapshot_target: Path | None = None
    screenshot_target: Path | None = None
    before_files: dict[Path, tuple[int, int]] | None = None
    execution_result: ToolResult | None = None
    policy_decision: dict[str, Any] | None = None
    parallel: bool = False
    invoked: bool = False
    executed: bool = False
    hook_rejection: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolStepSummary:
    """Internal summary; never forwarded as a host event."""

    made_progress: bool = False
    visible_calls: int = 0
    completed_turn_ending_tool: str | None = None
    successful_tools: set[str] = field(default_factory=set)
    transient_blocks: list[dict[str, Any]] = field(default_factory=list)
    transient_tokens: int = 0
    repair_guidance: str | None = None
    search_guidance: str | None = None
