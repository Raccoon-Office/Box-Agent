"""Stable agent-loop orchestration kernel.

This module contains the **single source of truth** for the agent loop.
It yields structured ``AgentEvent`` objects via an ``AsyncGenerator``.
CLI, ACP, and any future consumer all drive the same generator.

No ``print()`` or ``input()`` calls live here — all I/O is delegated
to the consumer through the event stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Final
from urllib.parse import urlsplit

from ..cache_fingerprint import build_cache_fingerprint
from ..config import AgentConfig, ToolLimitsConfig
from ..context_resources import (
    ContextResourceLedger,
)
from ..evidence import (
    extract_http_urls as _http_urls,
)
from ..events import (
    AgentEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LLMOutputEvent,
    LLMActivityEvent,
    LogFileEvent,
    MemoryProposalEvent,
    MemoryPromotionCandidate,
    PlanSnapshotEvent,
    ProgressEvent,
    StepEnd,
    StepStart,
    StopReason,
    SummarizationEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolCallResult,
)
from .context_engine import (
    REQUEST_INPUT_HEADROOM_TOKENS,
    _fallback_context_estimate,
    _is_compaction_metadata,
    _validate_transient_followup_result,
)
from .ports import KernelServices
from .tool_messages import ToolMessageCommitter, session_log_messages
from ..tools.engine.call_contracts import (
    ToolExecutionOptions, ToolRunContext, ToolStepControl, ToolStepSummary,
)
from .stream_controller import (
    StreamInterruptionRecovery,
    RepetitiveStreamRecovery,
    resolve_provider_stale_seconds as _kernel_resolve_provider_stale_seconds,
    stream_with_activity as _kernel_stream_with_activity,
)
from .tool_messages import (
    _cleanup_incomplete_messages,
    _sanitize_dangling_tool_calls,
)
from ..logger import AgentLogger
from ..llm.debug_logging import reset_llm_debug_sink, set_llm_debug_sink
from ..loop_guards import (
    EMPTY_ARGS_LIMIT,
    WEB_SEARCH_TOOL_NAME,
    STREAM_REPEAT_MIN_CHUNKS,
    format_injected_message,
    format_runtime_context_update,
    looks_like_truncated_output,
    near_limit_wrapup_text,
    no_progress_wrapup_text,
    repeated_stream_pattern,
    reply_is_substantial,
    truncation_continuation_text,
)

from box_agent.user_paths import state_path

__all__ = ["run_agent_loop"]

_log = logging.getLogger("box_agent.core")
_WARNED_DEPRECATED_ARTIFACT_ROOT_SESSIONS: set[str] = set()
_DEFAULT_AGENT_CONFIG = AgentConfig()
PARALLEL_TOOL_CANCEL_GRACE_SECONDS: Final[float] = 2.0
LLM_ACTIVITY_INTERVAL_SECONDS: Final[float] = 15.0
TOOL_ACTIVITY_INTERVAL_SECONDS: Final[float] = 15.0
TOOL_EVENT_POLL_INTERVAL_SECONDS: Final[float] = 0.1
# Slow deep-thinking models can legitimately spend several minutes before the
# provider emits another SSE chunk. Keep a bounded recovery cutoff without
# treating a three-minute reasoning pause as a stale stream.
LLM_PROVIDER_STALE_SECONDS: Final[float] = (
    _DEFAULT_AGENT_CONFIG.provider_stale_seconds
)
MAX_PROVIDER_STALE_RECOVERIES: Final[int] = 3
_PROVIDER_STALE_SECONDS_ENV: Final[str] = "BOX_AGENT_PROVIDER_STALE_SECONDS"


@dataclass(frozen=True, slots=True)
class _LoopRuntimeDefaults:
    """Immutable process defaults injected into one kernel run."""

    parallel_tool_cancel_grace_seconds: float = PARALLEL_TOOL_CANCEL_GRACE_SECONDS
    llm_activity_interval_seconds: float = LLM_ACTIVITY_INTERVAL_SECONDS
    tool_activity_interval_seconds: float = TOOL_ACTIVITY_INTERVAL_SECONDS
    tool_event_poll_interval_seconds: float = TOOL_EVENT_POLL_INTERVAL_SECONDS
    llm_provider_stale_seconds: float = LLM_PROVIDER_STALE_SECONDS
    max_provider_stale_recoveries: int = MAX_PROVIDER_STALE_RECOVERIES
    provider_stale_seconds_environment_variable: str = _PROVIDER_STALE_SECONDS_ENV


_DEFAULT_LOOP_RUNTIME_DEFAULTS = _LoopRuntimeDefaults()


def _resolve_provider_stale_seconds(
    config_value: float | None = None,
    *,
    _runtime_defaults: _LoopRuntimeDefaults = _DEFAULT_LOOP_RUNTIME_DEFAULTS,
) -> float:
    """Effective provider-stale cutoff.

    Precedence: the ``BOX_AGENT_PROVIDER_STALE_SECONDS`` env var (an operational
    escape hatch) wins, then the configured ``agent.provider_stale_seconds``,
    then the historical ``LLM_PROVIDER_STALE_SECONDS`` default. Non-positive,
    non-finite (``inf``/``nan``), or unparseable values are ignored so a bad
    override cannot silently disable the guard.
    """
    return _kernel_resolve_provider_stale_seconds(
        config_value,
        default_stale_seconds=_runtime_defaults.llm_provider_stale_seconds,
        environment_variable_name=(
            _runtime_defaults.provider_stale_seconds_environment_variable
        ),
    )


async def _stream_with_activity(
    stream: AsyncIterator[StreamEvent],
    *,
    stale_seconds: float | None = None,
    _runtime_defaults: _LoopRuntimeDefaults = _DEFAULT_LOOP_RUNTIME_DEFAULTS,
) -> AsyncIterator[StreamEvent]:
    """Add bounded host heartbeats and stop a provider stream that is stale."""
    # Read the module default at call time (not def time) so monkeypatching
    # ``LLM_PROVIDER_STALE_SECONDS`` still takes effect and callers can pass an
    # explicit per-turn value.
    if stale_seconds is None:
        stale_seconds = _runtime_defaults.llm_provider_stale_seconds
    async for event in _kernel_stream_with_activity(
        stream,
        stale_seconds=stale_seconds,
        activity_interval_seconds=_runtime_defaults.llm_activity_interval_seconds,
    ):
        yield event
from ..schema import LLMResponse, Message, StreamEvent
from ..tools.base import (
    Tool,
    ToolResult,
)
from ..tools.argument_limits import RECOMMENDED_GENERATED_BODY_CHARS
from ..tools.browser_intent import BrowserToolIntentPolicy
from ..tool_result_storage import ToolResultStorage
from ..turn_continuation import TurnContinuationController
from ..turn_policy import (
    text_is_short_non_task_reply,
    text_requests_plan_start,
)

# Type alias — consumers supply a zero-arg callable that returns True
# when the execution should be cancelled.
CancelChecker = Callable[[], bool]
ActiveSkillActivator = Callable[[str, str], None]

# Compatibility constants remain importable here; file recovery owns them.
from ..tools.file_result_adapter import (
    _MODEL_HISTORY_PLACEHOLDER_REPAIR_LIMIT,
    _MODEL_HISTORY_PLACEHOLDER_TOOL_ERROR,
    _MODEL_HISTORY_PLACEHOLDER_REPAIR_GUIDANCE,
)

_OUTPUT_LENGTH_TOOL_RECOVERY = (
    "The previous response ended because it reached the maximum output length. "
    "None of its tool calls were executed, and no tool side effects occurred. "
    "Retry and complete the original task. Do not assume that any tool call from "
    "that response took effect."
)
_OUTPUT_LENGTH_WRITE_FILE_RECOVERY = (
    "The previous response ended because it reached the maximum output length. "
    "None of the tool calls in that response were executed, so that response made "
    "no file-system changes. Previously accepted chunks, if any, are still pending. "
    "Retry and complete the original task without emitting the entire large file in "
    "one write_file call. For each path, continue with the next_chunk_index returned "
    "by its last successful write_file result; use chunk_index=0 only when no chunk "
    "has been accepted for that path. Keep final=false until the last chunk, then set "
    "final=true."
)


_FORCED_PLAN_GUIDANCE = (
    "Host UI requires a structured execution plan for this turn. "
    "Before giving the substantive answer, call `plan_write` with action `set` "
    "to publish the task objective, scope, steps, verification, risks, and assumptions. "
    "Keep the plan concise and relevant to the user's latest request."
)

_FORCED_PLAN_RETRY_GUIDANCE = (
    "The host is still waiting for the structured plan card. "
    "Call `plan_write` with action `set` now before continuing the answer."
)

_FORCED_PLAN_APPROVAL_GUIDANCE = (
    "Host UI requires an explicit user approval before execution. "
    "Call `plan_write` with action `set` to publish the task objective, scope, "
    "steps, verification, risks, and assumptions. Do not call execution tools "
    "such as file, bash, code, or sub-agent tools in this turn. After publishing "
    "the plan, stop and wait for the host to approve it. Do not publish a new "
    "plan when the latest user message is only a greeting, acknowledgement, "
    "thanks, or approval such as ok, continue, confirmed, 好的, 收到, or 继续 "
    "without a concrete task."
)

_PLAN_APPROVAL_SKIP_MESSAGE = (
    "Execution is paused until the user approves the published plan. "
    "Do not retry this tool yet; publish or revise the plan first."
)

_PLAN_APPROVAL_DONE_CONTENT = "计划已生成，等待用户确认后再执行。"
_WAITING_FOR_USER_DONE_CONTENT = "Waiting for the user's response."

FINAL_SUMMARY_TOOL_CALL_THRESHOLD: Final[int] = (
    ToolLimitsConfig().general.final_summary_after_calls
)


def final_summary_wrapup_text(
    tool_call_count: int,
    threshold: int = FINAL_SUMMARY_TOOL_CALL_THRESHOLD,
) -> str:
    return (
        "This turn has used many visible tool calls "
        f"({tool_call_count}, threshold {threshold}). "
        "Stop calling tools now unless a single, clearly required verification step is impossible to skip. "
        "If a deliverable is still incomplete, state the concrete gap and next action instead of continuing tool work. "
        "The final user-visible response must be a concise conclusion, "
        "not a process log: state the result, list created/changed files or concrete outputs when relevant, "
        "mention only important caveats, and give the next action if one is needed. "
        "Do not enumerate every tool call."
    )


def empty_final_answer_retry_text(tool_call_count: int) -> str:
    return (
        "The previous natural end produced no visible final answer after using "
        f"{tool_call_count} visible tool call(s). "
        "Answer the user now with a concise final conclusion. Do not call tools unless the task is impossible "
        "to summarize without one."
    )


_EMPTY_FINAL_ANSWER_ERROR = "工具已执行完成，但模型未生成最终答复，请重试。"




def _message_text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            value = block.get("text") or block.get("content")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _latest_user_text(messages: list[Message]) -> str:
    for msg in reversed(messages):
        if msg.role == "user" and not _is_compaction_metadata(msg):
            return _message_text(msg.content)
    return ""


def _should_emit_plan_start(
    messages: list[Message],
    tools: dict[str, Tool],
    *,
    plan_start_text: str | None = None,
) -> bool:
    if "plan_write" not in tools:
        return False
    candidate = _latest_user_text(messages) if plan_start_text is None else plan_start_text
    return text_requests_plan_start(candidate)


def _plan_approval_is_approved(plan_approval: dict[str, Any] | None) -> bool:
    if not isinstance(plan_approval, dict):
        return False
    decision = str(plan_approval.get("decision") or "").strip().lower()
    return decision in {
        "approve",
        "approved",
        "accept",
        "accepted",
        "confirm",
        "confirmed",
        "execute",
        "proceed",
        "yes",
    }


def _plan_approval_payload(
    *,
    request_id: str,
    state: str,
    plan_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "required": True,
        "state": state,
        "request_id": request_id,
    }
    if plan_id:
        payload["plan_id"] = plan_id
    return payload


def _attach_plan_approval_payload(
    raw_output: dict[str, Any] | None,
    *,
    request_id: str,
    state: str = "pending",
) -> dict[str, Any]:
    output = dict(raw_output or {})
    if output.get("type") != "plan_snapshot":
        output = {
            "type": "plan_snapshot",
            "version": 1,
            "action": "set",
            "plan": None,
            "summary": {
                "steps": 0,
                "verification": 0,
                "risks": 0,
                "assumptions": 0,
            },
        }

    plan = output.get("plan")
    plan_id: str | None = None
    if isinstance(plan, dict):
        plan = dict(plan)
        plan["status"] = "draft" if state == "pending" else str(plan.get("status") or "active")
        output["plan"] = plan
        raw_plan_id = plan.get("id")
        if raw_plan_id is not None:
            plan_id = str(raw_plan_id)

    output["approval"] = _plan_approval_payload(
        request_id=request_id,
        state=state,
        plan_id=plan_id,
    )
    return output


def _plan_start_payload(approval: dict[str, Any] | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    plan = {
        "id": "pending",
        "title": "正在制定执行方案",
        "objective": "根据当前请求梳理目标、范围、步骤、验证方式和风险。",
        "scope": "",
        "status": "draft",
        "steps": [],
        "verification": [],
        "risks": [],
        "assumptions": [],
        "created_at": now,
        "updated_at": now,
    }
    payload = {
        "type": "plan_snapshot",
        "version": 1,
        "action": "start",
        "plan": plan,
        "summary": {
            "steps": 0,
            "verification": 0,
            "risks": 0,
            "assumptions": 0,
        },
    }
    if approval is not None:
        payload["approval"] = approval
    return payload




async def _auto_match_memory_for_latest_prompt(
    messages: list[Message],
    memory_manager: Any,
    *,
    current_turn_text: str | None = None,
) -> tuple[ToolCallResult | None, Message | None]:
    """Conservatively match v2 experience memory against the latest user prompt.

    Prefer the host-sanitized current request over wrapped message history.
    Matches are injected as weak, one-turn context: the model is told these
    memories may be relevant and must ignore them when the user is starting a
    new task.  This avoids depending on the model deciding to call
    ``memory_search`` while keeping the memory signal non-authoritative.
    """
    if current_turn_text is not None:
        user_text = current_turn_text
    else:
        latest_user = next((msg for msg in reversed(messages) if msg.role == "user"), None)
        if latest_user is None:
            return None, None

        user_text = (
            latest_user.content
            if isinstance(latest_user.content, str)
            else str(latest_user.content)
        )
    if not user_text.strip():
        return None, None
    try:
        matches = await asyncio.to_thread(
            memory_manager.auto_match_context,
            user_text,
        )
    except Exception:
        return None, None

    if not matches:
        return None, None

    memory_lines = "\n".join(item["text"] for item in matches)
    memory_context = Message(
        role="user",
        content=format_runtime_context_update(
            "## Possibly relevant memory\n"
            "The following memories were automatically matched from prior context. "
            "Use them only if they are clearly relevant to the user's current request. "
            "If the user is starting a new task or the memories do not fit, ignore "
            "them and do not assume continuity.\n\n"
            f"{memory_lines}"
        ),
    )

    raw_output = {
        "type": "memory_search",
        "trigger": "auto",
        "query": user_text,
        "matched_memories": matches,
    }
    return (
        ToolCallResult(
            tool_call_id="memory-auto-match",
            tool_name="memory_search",
            success=True,
            content=f"Auto-matched {len(matches)} possible context memor{'y' if len(matches) == 1 else 'ies'}.",
            raw_output=raw_output,
        ),
        memory_context,
    )




# ── Cleanup helper ──────────────────────────────────────────────




# ── Main loop ───────────────────────────────────────────────────


def _signed_web_image_url_map(messages: list[Message]) -> dict[str, str]:
    """Map unsigned VolcSearch image paths to exact signed tool-result URLs."""
    signed_urls: dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested)
            return
        if isinstance(value, list):
            for nested in value:
                visit(nested)
            return
        if not isinstance(value, str) or "?" not in value:
            return
        try:
            parts = urlsplit(value)
        except ValueError:
            return
        hostname = (parts.hostname or "").casefold()
        if (
            parts.scheme.casefold() != "https"
            or not hostname.endswith("volcsearch-sign.byteimg.com")
            or "x-expires=" not in parts.query
            or "x-signature=" not in parts.query
        ):
            return
        unsigned = value.split("?", 1)[0]
        signed_urls[unsigned] = value

    for message in messages:
        if message.role != "tool" or message.name != WEB_SEARCH_TOOL_NAME:
            continue
        content = _message_text(message.content).strip()
        if not content:
            continue
        if content.startswith("[OK]"):
            content = content[4:].strip()
        try:
            visit(json.loads(content))
        except json.JSONDecodeError:
            continue
    return signed_urls

def _restore_signed_web_image_urls(
    content: str,
    signed_urls: dict[str, str],
) -> str:
    """Restore a stripped signed image URL only from exact web_search evidence."""
    restored = content
    for unsigned, signed in sorted(
        signed_urls.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        for candidate in (unsigned, unsigned.replace("~", r"\~")):
            restored = re.sub(
                rf"{re.escape(candidate)}(?!\?)",
                lambda _match, replacement=signed: replacement,
                restored,
            )
    return restored

class _SignedWebImageUrlStreamRewriter:
    """Repair signed image URLs without waiting for the full model response."""

    def __init__(self, signed_urls: dict[str, str]):
        self._signed_urls = dict(signed_urls)
        self._prefixes = tuple(
            dict.fromkeys(
                candidate
                for unsigned in self._signed_urls
                for candidate in (unsigned, unsigned.replace("~", r"\~"))
            )
        )
        self._buffer = ""

    def _pending_suffix_length(self) -> int:
        maximum = 0
        for candidate in self._prefixes:
            limit = min(len(candidate), len(self._buffer))
            for length in range(limit, maximum, -1):
                if self._buffer.endswith(candidate[:length]):
                    maximum = length
                    break
        return maximum

    def feed(self, delta: str) -> str:
        if not self._signed_urls:
            return delta
        self._buffer += delta
        pending_length = self._pending_suffix_length()
        if pending_length:
            ready = self._buffer[:-pending_length]
            self._buffer = self._buffer[-pending_length:]
        else:
            ready = self._buffer
            self._buffer = ""
        return _restore_signed_web_image_urls(ready, self._signed_urls)

    def flush(self) -> str:
        ready = _restore_signed_web_image_urls(self._buffer, self._signed_urls)
        self._buffer = ""
        return ready


async def _run_agent_loop_impl(
    *,
    _runtime_defaults: _LoopRuntimeDefaults,
    _services: KernelServices,
    messages: list[Message],
    max_steps: int = _DEFAULT_AGENT_CONFIG.max_steps,
    tool_limits: ToolLimitsConfig | None = None,
    max_tool_calls: int | None = None,
    max_delegated_tool_calls: int | None = None,
    web_search_total_limit: int | None = None,
    token_limit: int = 113400,
    is_cancelled: CancelChecker | None = None,
    logger: AgentLogger | None = None,
    workspace_dir: str | None = None,
    memory_turn_id: str = "",
    memory_promotion_enabled: bool = False,
    memory_promotion_hit_threshold: int = 5,
    memory_promotion_cooldown_days: int = 14,
    inject_queue: asyncio.Queue[Any] | None = None,
    thinking_enabled: bool = False,
    session_id: str = "",
    turn_id: str = "",
    title: str = "",
    call_kind: str = "",
    force_plan_start: bool = False,
    require_plan_approval: bool = False,
    plan_approval: dict[str, Any] | None = None,
    plan_start_text: str | None = None,
    pause_after_plan_write: bool = False,
    no_progress_limit: int | None = None,
    max_parallel_tools: int = 8,
    parallel_tool_timeout_seconds: float | None = 900.0,
    provider_stale_seconds: float | None = None,
    truncation_continuation_enabled: bool = True,
    max_truncation_continuations: int = 3,
    max_truncated_tool_call_retries: int = 3,
    truncated_tool_call_boost_cap: int = 32768,
    artifact_detection_enabled: bool = True,
    artifact_root_dir: str | Path | None = None,
    cache_fingerprint_context: dict[str, Any] | None = None,
    cache_fingerprint_sink: Callable[[dict[str, Any]], None] | None = None,
    active_skill_activator: ActiveSkillActivator | None = None,
    current_turn_text: str | None = None,
    context_resource_ledger: ContextResourceLedger | None = None,
    context_resource_dedup_enabled: bool = True,
    session_turn: int | None = None,
    run_control: Any | None = None,
) -> AsyncIterator[AgentEvent]:
    """Execute the agent loop, yielding structured events.

    This is the single source of truth for the agent execution loop.
    It does **not** print anything to stdout.  Consumers (CLI, ACP,
    JSON-RPC) decide how to render each event.

    Args:
        _services: Immutable resolved capability implementations.
        messages: Message history (mutated in-place).
        max_steps: Maximum LLM call iterations.
        tool_limits: Typed limits for search, wrap-up, and delegated runs.
        max_tool_calls: Optional hard cap across all tool executions in this loop.
        max_delegated_tool_calls: Optional aggregate cap for tool calls reported by
            successful delegated sub-agent runs.
        web_search_total_limit: Optional per-turn web search override.
        token_limit: Token threshold for triggering summarization.
        is_cancelled: Optional callable — return ``True`` to stop.
        logger: Optional ``AgentLogger`` for file-based logging.
        workspace_dir: Workspace directory for artifact detection.
        memory_turn_id: Optional caller-owned turn id to stamp on
            lifecycle-triggered memory extraction entries.
        inject_queue: Optional queue for in-stream message injection.
            When present, queued user messages are drained at each
            step boundary and appended to the conversation before
            the next LLM call.
        require_plan_approval: If True, the loop must publish a plan and
            stop before executing non-plan tools unless ``plan_approval``
            carries an approved decision.
        plan_approval: Host-supplied decision metadata for a previously
            published plan.
        plan_start_text: Optional host-sanitized latest user request for
            plan-start detection. When omitted, the latest user message is used.
        pause_after_plan_write: If True, an organic ``plan_write`` call also
            becomes an approval boundary: the plan is published with pending
            approval and the turn ends before sibling or later tools execute.
        parallel_tool_timeout_seconds: Wall-clock cap for one batch of
            parallel_safe tool calls. When exceeded, completed results are kept
            and unfinished calls receive synthetic timeout failures so the
            parent turn can continue.
        artifact_detection_enabled: If False, skip cwd-rooted artifact
            snapshotting and detection.
        truncation_continuation_enabled: If True (default), re-prompt the
            model once when a reply ends mid-sentence while the provider
            reported a normal finish, so the answer completes in the same
            message. See ``loop_guards.looks_like_truncated_output``.
        max_truncation_continuations: Per-turn cap on truncation
            continuations (loop guard against repeated false positives).
        artifact_root_dir: Deprecated compatibility input. It is ignored;
            artifact discovery always scans ``workspace_dir``.
        cache_fingerprint_context: Optional stable metadata to include with
            cache-sensitive request fingerprints, such as selected skill names.
        cache_fingerprint_sink: Optional callback that receives each fingerprint
            before the LLM request, for hosts that do not use ``AgentLogger``.
        current_turn_text: Optional host-sanitized latest user request used for
            automatic memory matching and gating tools that access the user's
            active browser tab. When omitted, the latest user message is used.
        context_resource_ledger: Optional caller-owned ledger. Agent sessions
            pass a persistent instance; direct and child loops get a local one.
        context_resource_dedup_enabled: Disable the first-batch resource-history
            optimization without changing visible tool execution.
    """
    llm = _services.llm
    summary_llm = _services.summary_llm
    permission_negotiator = _services.permission_gateway
    memory_lookup = _services.memory_lookup
    memory_extractor = _services.memory_extraction
    memory_promotion = _services.memory_promotion
    session_log = _services.session_store
    hook_mgr = _services.hook_bus
    tools = _services.tool_catalog
    tool_exposure_manager = _services.tool_exposure
    tool_result_storage = _services.tool_result_store
    compact_engine = _services.compact_engine
    if compact_engine is None:
        from .compact_engine import DefaultCompactEngine

        compact_engine = DefaultCompactEngine()
    context_engine = _services.context_engine
    if context_engine is not None:
        context_engine.bind_history(messages)

    if artifact_root_dir is not None:
        warning_key = session_id or workspace_dir or "<anonymous>"
        if warning_key not in _WARNED_DEPRECATED_ARTIFACT_ROOT_SESSIONS:
            _WARNED_DEPRECATED_ARTIFACT_ROOT_SESSIONS.add(warning_key)
            _log.warning(
                "artifact_root_dir is deprecated and ignored; artifact discovery uses workspace_dir"
            )

    cancelled = is_cancelled or (lambda: False)
    # Capture before memory, repair and continuation messages can change history.
    continuation_user_request = (
        current_turn_text if current_turn_text is not None else _latest_user_text(messages)
    )
    effective_tool_limits = tool_limits or ToolLimitsConfig()
    web_search_batch_size = effective_tool_limits.web_search.batch_size
    web_search_concurrency = effective_tool_limits.web_search.concurrency
    search_files_empty_result_limit = (
        effective_tool_limits.search_files.consecutive_empty_limit
    )
    wrapup_remaining_steps = effective_tool_limits.general.wrapup_remaining_steps
    final_summary_after_calls = (
        effective_tool_limits.general.final_summary_after_calls
    )
    resource_ledger = (
        context_resource_ledger or ContextResourceLedger()
        if context_resource_dedup_enabled
        else None
    )
    result_storage = tool_result_storage or ToolResultStorage(
        state_path('sessions')
    )
    result_storage.set_context_token_limit(token_limit)
    result_storage.initialize_history(messages)
    tool_call_limits = {
        WEB_SEARCH_TOOL_NAME: effective_tool_limits.web_search.total_calls,
    }
    if web_search_total_limit is not None:
        tool_call_limits[WEB_SEARCH_TOOL_NAME] = max(
            0,
            web_search_total_limit,
        )
    web_search_total_limit = tool_call_limits[WEB_SEARCH_TOOL_NAME]

    if logger:
        logger.start_new_run()
        log_path = logger.get_log_file_path()
        if log_path:
            yield LogFileEvent(path=str(log_path))

    if hook_mgr.hooks:
        await hook_mgr.fire_agent_start(messages=messages, tools=tools, max_steps=max_steps)

    auto_memory_context_message: Message | None = None
    if memory_lookup:
        injected, auto_memory_context_message = await _auto_match_memory_for_latest_prompt(
            messages,
            memory_lookup,
            current_turn_text=current_turn_text,
        )
        if injected is not None:
            yield injected

    browser_intent_policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text=current_turn_text,
        messages=messages,
    )

    api_total_tokens = 0
    api_prompt_tokens = 0
    summary_failure_cooldown_steps = 0
    run_start = perf_counter()

    # Defensive: heal any dangling assistant.tool_calls from a prior interrupted
    # turn (process crash, SIGKILL) before the first LLM request, so the
    # protocol-state precondition holds.
    healed = _sanitize_dangling_tool_calls(messages)
    if healed:
        _log.warning(
            "Healed %d dangling assistant tool_call(s) on loop entry — "
            "synthesized interrupted-stub tool responses.",
            healed,
        )
    if resource_ledger is not None:
        invalidated = resource_ledger.reconcile(messages)
        if invalidated:
            _log.info(
                "context_resource/ledger_reconciled invalidated=%s epoch=%d",
                ",".join(invalidated),
                resource_ledger.epoch,
            )

    async def _build_proposal_event() -> MemoryProposalEvent | None:
        """Read promotion candidates from memory and bump their last_proposed."""
        if not (memory_promotion_enabled and memory_promotion):
            return None
        try:
            entries = await asyncio.to_thread(
                memory_promotion.list_promotion_candidates,
                hit_threshold=memory_promotion_hit_threshold,
                cooldown_days=memory_promotion_cooldown_days,
            )
        except Exception:
            return None
        if not entries:
            return None
        try:
            await asyncio.to_thread(
                memory_promotion.mark_proposed,
                [e.id for e in entries],
            )
        except Exception:
            pass
        return MemoryProposalEvent(
            candidates=tuple(
                MemoryPromotionCandidate(
                    entry_id=e.id,
                    content=e.content,
                    hits=e.hits,
                    confidence=e.confidence,
                )
                for e in entries
            )
        )

    async def _build_proposal_event_with_plan() -> MemoryProposalEvent | None:
        """Same as ``_build_proposal_event`` but also asks the LLM to draft a
        single core rewrite consuming the hot candidates.  On any planner
        failure, falls back to the legacy per-candidate proposal (plan=None).
        """
        event = await _build_proposal_event()
        if event is None:
            return None
        wanted = {c.entry_id for c in event.candidates}
        try:
            context_entries = await asyncio.to_thread(
                memory_promotion.read_all_context_entries,
            )
            entries = [
                e for e in context_entries if e.id in wanted
            ]
        except Exception as exc:
            _log.warning(
                "proposal_with_plan: failed to read context entries, falling back to legacy event: %s",
                exc,
            )
            return event
        if not entries:
            _log.warning(
                "proposal_with_plan: no entries match candidate ids %s, falling back to legacy event",
                sorted(wanted),
            )
            return event
        try:
            plan = await memory_promotion.plan_promotion(entries, llm)
        except Exception as exc:
            _log.warning(
                "proposal_with_plan: plan_promotion raised, falling back to legacy event: %s",
                exc,
            )
            return event
        if plan is None:
            _log.warning(
                "proposal_with_plan: plan_promotion returned None (see prior warnings), falling back to legacy event for %d candidates",
                len(entries),
            )
            return event
        return MemoryProposalEvent(candidates=event.candidates, plan=plan)

    # Loop-guard state: detect when the model emits the same tool_call
    # signature with empty arguments two turns in a row. With a healthy LLM
    # this should never happen — it's the fingerprint of a relay/provider
    # bug or a model stuck after seeing "missing required argument" errors,
    # and continuing burns max_steps without progress.
    empty_args_signature: tuple[str, ...] | None = None
    empty_args_repeats = 0

    # Near-limit wrap-up: when only the configured trailing steps are left, inject a
    # one-shot instruction telling the model to stop gathering more material
    # (tool calls / searches) and synthesize a final answer from what it
    # already has, instead of burning the last steps and exiting with a
    # "couldn't be completed" failure.
    wrapup_injected = False

    # No-progress circuit breaker (opt-in via ``no_progress_limit``). Counts
    # consecutive steps in which no tool call returned a success with usable
    # (non-empty) content. After the limit is hit, inject the same wrap-up
    # synthesis nudge instead of letting a stuck agent flail to max_steps —
    # the failure mode seen when a sub-agent has no web_search and retries raw
    # curl scraping dozens of times. Disabled (None) for the top-level agent to
    # preserve existing behavior.
    no_progress_steps = 0
    turn_continuation = TurnContinuationController()
    stream_recovery = StreamInterruptionRecovery()
    repetitive_recovery = RepetitiveStreamRecovery()

    plan_write_succeeded = False
    # Suspected-truncation continuation (opt-in via
    # ``truncation_continuation_enabled``). Bounds how many times the loop
    # may re-prompt the model to finish a reply that ended mid-sentence
    # while the provider reported a normal finish.
    truncation_continuations = 0

    # Truncated tool-call retry counter. When the provider (or a relay) clips
    # a tool_call's argument stream mid-JSON, retry the same turn with the
    # SAME message state — no broken assistant turn is appended — and boost
    # the per-request max_tokens on genuine output-cap truncations. Only
    # after exhausting the retries do we surface a user-visible error.
    truncated_tool_call_retries = 0
    oversized_tool_argument_retries = 0
    provider_stale_retries = 0
    provider_stale_recoveries = 0
    # Resolve once per turn: env override > configured value > module default.
    effective_provider_stale_seconds = _resolve_provider_stale_seconds(
        provider_stale_seconds,
        _runtime_defaults=_runtime_defaults,
    )
    pending_transient_followup_blocks: list[dict[str, Any]] = []
    pending_transient_followup_tokens = 0
    request_overlay_tokens = 0
    request_context_messages: list[Message] = []
    tool_list: list[Any] = []

    # Per-turn guard for tools that can be repeatedly requested by the model
    # after it already has enough evidence. Once a budget is reached, later
    # calls are answered with synthetic tool errors so the protocol remains
    # valid while nudging the model to synthesize.
    tool_engine = _services.tool_engine
    assert tool_engine is not None
    tool_messages = ToolMessageCommitter(messages, session_log, session_turn)

    def validate_followup(result, tool, pending):
        accepted, blocks, tokens = _validate_transient_followup_result(
            result=result, tool=tool, llm=llm, token_limit=token_limit,
            pending_token_estimate=pending,
        )
        if blocks and context_engine is not None:
            context_engine.reserve_followup(blocks)
        return accepted, blocks, tokens

    tool_engine.configure_run(
        ToolRunContext(
            messages=messages, hooks=hook_mgr, result_storage=result_storage,
            hook_dispatch=_services.hook_dispatch, hook_context=_services.hook_context,
            is_cancelled=cancelled, record_call=tool_messages.record_call,
            flush_calls=tool_messages.flush_calls,
            commit_result=tool_messages.commit_result,
            validate_followup=validate_followup,
            policy_error=browser_intent_policy.tool_call_error,
            workspace_dir=workspace_dir,
            session_id=session_id, turn_id=turn_id,
            permission_negotiator=permission_negotiator, logger=logger,
            resource_ledger=resource_ledger, activate_skill=active_skill_activator,
            skill_reader=context_engine.tool_reader if context_engine is not None else None,
        ),
        ToolExecutionOptions(
            tool_call_limits=tool_call_limits, max_tool_calls=max_tool_calls,
            max_delegated_tool_calls=max_delegated_tool_calls,
            search_files_empty_result_limit=search_files_empty_result_limit,
            web_search_batch_size=web_search_batch_size,
            web_search_concurrency=web_search_concurrency,
            max_parallel_tools=max_parallel_tools,
            batch_timeout_seconds=parallel_tool_timeout_seconds,
            activity_interval_seconds=_runtime_defaults.tool_activity_interval_seconds,
            event_poll_interval_seconds=_runtime_defaults.tool_event_poll_interval_seconds,
            cancel_grace_seconds=_runtime_defaults.parallel_tool_cancel_grace_seconds,
            artifact_detection_enabled=artifact_detection_enabled,
        ),
    )
    visible_tool_call_total = 0
    final_summary_guidance_injected = False
    empty_final_answer_retry_injected = False
    verified_evidence_urls: set[str] = set()
    plan_start_emitted = False
    forced_plan_guidance_injected = False
    forced_plan_retry_injected = False
    plan_approval_approved = _plan_approval_is_approved(plan_approval)
    plan_approval_gate_completed = False
    plan_approval_request_id = "plan-" + hashlib.sha1(
        f"{run_start}:{_latest_user_text(messages)}".encode("utf-8", errors="ignore")
    ).hexdigest()[:10]

    async def cancellation_done_event() -> DoneEvent:
        if hook_mgr.hooks:
            await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
        return DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")

    async def run_checkpoint() -> bool:
        """Honor cooperative pause/resume at a run-safe boundary."""
        if run_control is not None and not await run_control.checkpoint():
            return False
        return not cancelled()

    async def compact_context(history_token_limit, *, force=False, estimate_tools=None):
        """Apply one compaction through the same durable surface/event path."""
        nonlocal summary_failure_cooldown_steps
        # Context may project effective rules before estimation and summary.
        # Older custom contexts retain the identity projection.
        project_history = getattr(context_engine, "project_history", None)
        effective_messages = project_history(messages) if callable(project_history) else messages
        from .context_types import CompactionInput

        def before_summary(estimated: int) -> None:
            if session_log is not None and session_turn is not None:
                session_log.append_unlogged_messages(
                    effective_messages[1:], turn=session_turn, step=step + 1,
                )
                session_log.append("compaction/start", {
                    "turn": session_turn, "step": step + 1,
                    "estimatedBefore": estimated, "tokenLimit": history_token_limit,
                })
                session_log.flush()

        result = await compact_engine.compact_if_needed(CompactionInput(
            history=tuple(effective_messages), token_limit=history_token_limit,
            llm=llm, api_total_tokens=api_total_tokens,
            session_id=session_id, turn_id=turn_id, title=title,
            api_prompt_tokens=api_prompt_tokens, tools=tools,
            summary_llm=summary_llm,
            allow_llm_summary=summary_failure_cooldown_steps == 0,
            before_summary=before_summary,
            force=force, estimate_tools=estimate_tools,
            summary_input_token_limit=token_limit if force else None,
        ))
        if result.mode == "fallback" and result.summary_calls > 0 and result.error:
            summary_failure_cooldown_steps = (
                max_steps
                if result.error_type
                in {
                    "BadRequestError",
                    "AuthenticationError",
                    "PermissionDeniedError",
                }
                else 3
            )
        elif summary_failure_cooldown_steps > 0:
            summary_failure_cooldown_steps -= 1
        event = None
        new_msgs, _skip_next_token_check, est_before = result
        if new_msgs is not None:
            # The compacted surface retains the host's original system record;
            # effective rules are projected again for every request.
            new_msgs[0] = messages[0]
            # Snapshot messages before compression, then extract in background
            if memory_extractor:
                _snapshot = list(messages)
                asyncio.create_task(
                    memory_extractor.maybe_extract(
                        _snapshot,
                        "pre_summarize",
                        turn_id=memory_turn_id,
                    )
                )
            if session_log is not None and session_turn is not None:
                session_log.append(
                    "compaction/summary",
                    {
                        "turn": session_turn,
                        "step": step + 1,
                        "mode": result.mode,
                        "message": new_msgs[1].model_dump(
                            mode="json",
                            exclude_none=True,
                        ),
                        "estimatedBefore": est_before,
                        "estimatedAfter": result.estimated_after,
                        "error": result.error,
                    },
                )
                session_log.replace_surface(
                    new_msgs[1:],
                    turn=session_turn,
                    step=step + 1,
                )
                session_log.append(
                    "compaction/end",
                    {
                        "turn": session_turn,
                        "step": step + 1,
                        "mode": result.mode,
                        "error": result.error,
                    },
                )
                session_log.flush()
            messages.clear()
            messages.extend(new_msgs)
            if resource_ledger is not None:
                resource_ledger.rotate_epoch()
                _log.info(
                    "context_resource/epoch_rotated transform=summary epoch=%d",
                    resource_ledger.epoch,
                )
            event = SummarizationEvent(
                estimated_tokens=est_before,
                api_tokens=api_prompt_tokens,
                token_limit=token_limit,
                estimated_after=result.estimated_after,
                mode=result.mode,
                summary_calls=result.summary_calls,
                micro_compacted=0,
                error=result.error,
                error_type=result.error_type,
                trigger_source=result.trigger_source,
            )
        return result, event

    for step in range(max_steps):
        if resource_ledger is not None:
            invalidated = resource_ledger.reconcile(messages)
            if invalidated:
                _log.info(
                    "context_resource/ledger_reconciled invalidated=%s epoch=%d",
                    ",".join(invalidated),
                    resource_ledger.epoch,
                )
        for message in messages:
            if message.role == "user":
                verified_evidence_urls.update(_http_urls(message.content))

        # ── Cancellation check (top of step) ────────────────
        # No cleanup needed here — messages are consistent at step boundaries.
        if not await run_checkpoint():
            yield await cancellation_done_event()
            return

        step_start = perf_counter()
        # ── Drain inject queue (in-stream injection) ───────
        if inject_queue:
            while not inject_queue.empty():
                injected_item = inject_queue.get_nowait()
                injection_id = None
                user_visible = True
                injection_source = "user"
                if isinstance(injected_item, dict):
                    injected_text = str(injected_item.get("content") or "")
                    raw_injection_id = injected_item.get("id")
                    if isinstance(raw_injection_id, str):
                        injection_id = raw_injection_id
                    raw_user_visible = injected_item.get("user_visible")
                    if isinstance(raw_user_visible, bool):
                        user_visible = raw_user_visible
                    raw_source = injected_item.get("source")
                    if raw_source == "runtime":
                        injection_source = "runtime"
                else:
                    injected_text = str(injected_item)
                if not injected_text:
                    continue
                if injection_source == "user":
                    continuation_user_request = injected_text
                formatted_injection = (
                    format_runtime_context_update(injected_text)
                    if injection_source == "runtime"
                    else format_injected_message(injected_text)
                )
                messages.append(
                    Message(role="user", content=formatted_injection)
                )
                yield InjectedMessageEvent(
                    content=injected_text,
                    injection_id=injection_id,
                    user_visible=user_visible,
                )

        has_plan_tool = "plan_write" in tools
        latest_user_text = _latest_user_text(messages)
        latest_user_is_short_non_task = text_is_short_non_task_reply(latest_user_text)
        plan_approval_gate_enabled = (
            require_plan_approval
            and not plan_approval_approved
            and has_plan_tool
            and not latest_user_is_short_non_task
        )
        force_plan_for_turn = (force_plan_start or plan_approval_gate_enabled) and has_plan_tool
        if force_plan_for_turn and not forced_plan_guidance_injected:
            forced_plan_guidance_injected = True
            guidance = (
                _FORCED_PLAN_APPROVAL_GUIDANCE
                if plan_approval_gate_enabled
                else _FORCED_PLAN_GUIDANCE
            )
            messages.append(
                Message(role="user", content=format_injected_message(guidance))
            )
            yield InjectedMessageEvent(
                content=guidance,
                injection_id=None,
                user_visible=False,
            )

        if not plan_start_emitted and (
            force_plan_for_turn
            or _should_emit_plan_start(messages, tools, plan_start_text=plan_start_text)
        ):
            plan_start_emitted = True
            approval = (
                _plan_approval_payload(
                    request_id=plan_approval_request_id,
                    state="drafting",
                    plan_id="pending",
                )
                if plan_approval_gate_enabled
                else None
            )
            yield PlanSnapshotEvent(payload=_plan_start_payload(approval))

        for guidance in tool_engine.budget_guidance():
            messages.append(Message(role="user", content=format_injected_message(guidance)))
            yield InjectedMessageEvent(content=guidance, injection_id=None, user_visible=False)

        # ── Fresh tool-result aggregate budget (Layer 1) ───
        # This runs immediately before the next LLM request. Decisions are
        # frozen by tool_use_id so later turns keep the same cache prefix.
        budget_outcome = result_storage.enforce_fresh_budget(
            messages,
            tools=tools,
            session_id=session_id,
        )
        if budget_outcome.persisted_count:
            _log.info(
                "tool_result_budget persisted=%d fresh=%d before=%d after=%d limit=%d",
                budget_outcome.persisted_count,
                budget_outcome.fresh_count,
                budget_outcome.original_chars,
                budget_outcome.remaining_chars,
                result_storage.aggregate_budget,
            )
        # ── Usage-driven context summarization (Layer 2) ───
        prepared_tools = _services.tool_engine.prepare_tools(
            is_tool_visible=browser_intent_policy.is_tool_visible,
        )
        tool_list = list(prepared_tools.definitions)
        offered_tools_by_name = prepared_tools.targets
        budget_tools_by_name = {tool.name: tool for tool in tool_list}
        request_context_messages = [
            message
            for message in (auto_memory_context_message,)
            if message is not None
        ]
        transient_message = (
            Message(
                role="user",
                content=list(pending_transient_followup_blocks),
                trace_redact_content=True,
            )
            if pending_transient_followup_blocks
            else None
        )
        extra_tokens = (_fallback_context_estimate(request_context_messages, {})
                        if request_context_messages else 0)
        transient_tokens = (max(pending_transient_followup_tokens,
            _fallback_context_estimate([transient_message], {})) if transient_message is not None else 0)
        history_token_limit = max(
            1,
            token_limit - extra_tokens - transient_tokens,
        )
        if cancelled():
            yield await cancellation_done_event()
            return
        result, summarization_event = await compact_context(
            history_token_limit, estimate_tools=budget_tools_by_name,
        )
        context_compacted = result.messages is not None
        if summarization_event is not None:
            yield summarization_event
        if cancelled():
            yield await cancellation_done_event()
            return
        if result.blocked:
            budget_details = {
                "stage": "history_compaction", "totalLimitTokens": token_limit,
                "historyLimitTokens": history_token_limit,
                "estimatedHistoryTokens": result.estimated_after,
                "extraInputTokens": extra_tokens, "transientInputTokens": transient_tokens,
                "skillReferenceTokens": 0,
            }
            msg = (
                "Context remains above the safe input limit after bounded compaction "
                f"({result.estimated_after} estimated history tokens; history limit {history_token_limit}; "
                f"total input limit {token_limit}; extra input {extra_tokens}; transient input {transient_tokens}; "
                "Skill references not yet projected). "
                "Start a new session or reduce active instructions/tool output before retrying."
            )
            if hook_mgr.hooks:
                await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
            yield ErrorEvent(message=msg, is_fatal=True,
                             error_code="CONTEXT_INPUT_BUDGET_EXCEEDED", error_category="context_length",
                             error_details=budget_details)
            yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
            return

        # ── Near-limit wrap-up nudge (one-shot) ─────────────
        # Reserve the final few steps for synthesis: stop further
        # research and force a self-contained answer from gathered
        # material before the step budget is exhausted.
        if (
            not wrapup_injected
            and max_steps > wrapup_remaining_steps
            and step >= max_steps - wrapup_remaining_steps
        ):
            wrapup_injected = True
            wrapup_text = near_limit_wrapup_text(step, max_steps)
            messages.append(
                Message(role="user", content=format_injected_message(wrapup_text))
            )
            yield InjectedMessageEvent(content=wrapup_text, injection_id=None, user_visible=False)

        # ── No-progress circuit breaker (one-shot) ──────────
        # The agent has gone no_progress_limit consecutive steps without a
        # single useful tool result. Stop the flailing and force a synthesis
        # from whatever was gathered, rather than burning the rest of the
        # step budget on the same failing approach.
        if (
            not wrapup_injected
            and no_progress_limit
            and no_progress_steps >= no_progress_limit
        ):
            wrapup_injected = True
            stall_text = no_progress_wrapup_text(no_progress_steps)
            messages.append(
                Message(role="user", content=format_injected_message(stall_text))
            )
            yield InjectedMessageEvent(content=stall_text, injection_id=None, user_visible=False)

        # ── Step start ──────────────────────────────────────
        yield StepStart(step=step + 1, max_steps=max_steps)
        if cancelled():
            yield await cancellation_done_event()
            return
        if hook_mgr.hooks:
            await hook_mgr.fire_step_start(step=step + 1, max_steps=max_steps)
        if cancelled():
            yield await cancellation_done_event()
            return

        # ── LLM call (streaming) ──────────────────────────────
        request_overlay_tokens = pending_transient_followup_tokens if transient_message is not None else 0
        skill_references = ()
        on_request_committed = None
        on_response_received = None
        if context_engine is not None:
            output_budget = getattr(llm, "max_output_tokens", 0)
            projection = context_engine.prepare_request(
                messages, prepared_tools=prepared_tools, token_limit=token_limit,
                output_tokens=output_budget if isinstance(output_budget, int) else 0,
                extra_messages=tuple(request_context_messages),
                transient_message=transient_message,
                transient_tokens=pending_transient_followup_tokens,
            )
            recovery_blocked = False
            if (projection.blocked_reason and getattr(projection, "budget_blocked", False)
                    and not context_compacted):
                # The final request includes schemas, overlays and guidance
                # added after the regular history check. Retry this projection
                # once, without replaying step hooks, tools or delivery commits.
                result, summarization_event = await compact_context(
                    max(1, token_limit - extra_tokens - transient_tokens - REQUEST_INPUT_HEADROOM_TOKENS),
                    force=True, estimate_tools=budget_tools_by_name,
                )
                if summarization_event is not None:
                    yield summarization_event
                if cancelled():
                    yield await cancellation_done_event()
                    return
                recovery_blocked = result.blocked
                # Rebinding is mandatory: persistent read facts do not prove
                # the corresponding tool text survived compaction.
                projection = context_engine.prepare_request(
                    messages, prepared_tools=prepared_tools, token_limit=token_limit,
                    output_tokens=output_budget if isinstance(output_budget, int) else 0,
                    extra_messages=tuple(request_context_messages),
                    transient_message=transient_message,
                    transient_tokens=pending_transient_followup_tokens,
                )
            if projection.blocked_reason or recovery_blocked:
                msg = projection.blocked_reason or "Context remains above the safe input limit after bounded compaction."
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                    await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
                yield ErrorEvent(message=msg, is_fatal=True)
                yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
                return
            request_messages = projection.context_messages
            provider_request_messages = projection.messages
            skill_references = projection.references
            request_overlay_tokens = projection.request_only_input_tokens
            on_request_committed = getattr(projection, "on_committed", None)
            on_response_received = getattr(projection, "on_response", None)
        else:
            # Legacy manually constructed service bundles may omit Context.
            request_messages = [*messages, *request_context_messages]
            provider_request_messages = ([*request_messages, transient_message]
                                         if transient_message is not None else request_messages)

        # Hooks and compaction can yield control after the step-start check.
        # Honour cancellation before committing Skill delivery or any request.
        if cancelled():
            yield await cancellation_done_event()
            return

        if session_log is not None and session_turn is not None:
            request_provider = getattr(llm, "provider", None)
            if not isinstance(request_provider, str):
                request_provider = None
            request_model = getattr(llm, "model", None)
            if not isinstance(request_model, str):
                request_model = None
            request_max_output = getattr(llm, "max_output_tokens", None)
            if not isinstance(request_max_output, int):
                request_max_output = None
            session_log.append_unlogged_messages(
                session_log_messages(messages),
                turn=session_turn,
                step=step + 1,
            )
            session_log.append(
                "request/header",
                {
                    "turn": session_turn,
                    "step": step + 1,
                    "header": {
                        "config": {
                            "provider": request_provider,
                            "model": request_model,
                            "maxOutputTokens": request_max_output,
                        },
                        "system": messages[0].content,
                        "tools": [tool.to_schema() for tool in tool_list],
                    },
                },
            )
            session_log.append(
                "request/context",
                {
                    "turn": session_turn,
                    "step": step + 1,
                    "provider": request_provider,
                    "model": request_model,
                    "tokenLimit": token_limit,
                    "skillReferences": list(skill_references),
                    **(
                        {
                            "autoMemoryContext": {
                                "sha256": hashlib.sha256(
                                    str(auto_memory_context_message.content).encode("utf-8")
                                ).hexdigest(),
                                "chars": len(str(auto_memory_context_message.content)),
                            }
                        }
                        if auto_memory_context_message is not None
                        else {}
                    ),
                },
            )
            session_log.flush()

        cache_fingerprint = build_cache_fingerprint(
            messages=request_messages,
            tools=tool_list,
            context=cache_fingerprint_context,
        )
        if cache_fingerprint_sink is not None:
            try:
                cache_fingerprint_sink(cache_fingerprint)
            except Exception:
                _log.debug("cache fingerprint sink failed", exc_info=True)
        if logger:
            logger.log_request(
                messages=request_messages,
                tools=tool_list,
                cache_fingerprint=cache_fingerprint,
            )

        # Request diagnostics can fail too. Keep restored material pending until
        # all request logging succeeds, immediately before the provider call.
        if cancelled():
            yield await cancellation_done_event()
            return
        if callable(on_request_committed):
            on_request_committed()

        llm_debug_sink_token = (
            set_llm_debug_sink(logger.log_llm_debug_record)
            if logger
            else None
        )
        try:
            # Stream thinking and visible text deltas as soon as the provider
            # yields them. Structured progress surfaces such as plan/todo are
            # emitted as separate events, so visible text does not need a
            # leading buffer to protect host UI ordering.
            text_content = ""
            thinking_content = ""
            finish_event: StreamEvent | None = None
            thinking_header_yielded = False
            stream_repeat_pattern: str | None = None
            text_chunk_count = 0
            thinking_chunk_count = 0
            signed_image_url_rewriter = _SignedWebImageUrlStreamRewriter(
                _signed_web_image_url_map(messages)
            )

            stream_kwargs = {
                "messages": provider_request_messages,
                "tools": tool_list,
                "thinking_enabled": thinking_enabled,
                "session_id": session_id,
                "turn_id": turn_id,
                "title": title,
            }
            if call_kind:
                stream_kwargs["call_kind"] = call_kind
            request_only_input_tokens = request_overlay_tokens
            llm_stream = llm.generate_stream(**stream_kwargs)
            async for chunk in _stream_with_activity(
                llm_stream,
                stale_seconds=effective_provider_stale_seconds,
                _runtime_defaults=_runtime_defaults,
            ):
                if cancelled():
                    break
                if chunk.type == "thinking":
                    thinking_chunk_count += 1
                    candidate = thinking_content + (chunk.delta or "")
                    stream_repeat_pattern = (
                        repeated_stream_pattern(candidate)
                        if thinking_chunk_count >= STREAM_REPEAT_MIN_CHUNKS
                        else None
                    )
                    if stream_repeat_pattern is not None:
                        break
                    if not thinking_header_yielded:
                        yield ThinkingEvent(content="", _streaming=True, _header=True)
                        thinking_header_yielded = True
                    thinking_content = candidate
                    yield ThinkingEvent(content=chunk.delta or "", _streaming=True)
                elif chunk.type == "text":
                    text_chunk_count += 1
                    visible_delta = signed_image_url_rewriter.feed(chunk.delta or "")
                    candidate = text_content + visible_delta
                    stream_repeat_pattern = (
                        repeated_stream_pattern(candidate)
                        if text_chunk_count >= STREAM_REPEAT_MIN_CHUNKS
                        else None
                    )
                    if stream_repeat_pattern is not None:
                        break
                    text_content = candidate
                    if visible_delta:
                        yield ContentEvent(content=visible_delta, _streaming=True)
                elif chunk.type == "activity" and chunk.activity:
                    yield LLMActivityEvent(step=step + 1, payload=dict(chunk.activity))
                elif chunk.type == "finish":
                    finish_event = chunk

            if stream_repeat_pattern is None and not cancelled():
                final_visible_delta = signed_image_url_rewriter.flush()
                if final_visible_delta:
                    text_content += final_visible_delta
                    yield ContentEvent(content=final_visible_delta, _streaming=True)

            if stream_repeat_pattern is not None:
                closer = getattr(llm_stream, "aclose", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception:
                        _log.debug("failed to close repetitive LLM stream", exc_info=True)
                _cleanup_incomplete_messages(messages)
                _log.warning(
                    "repetitive_llm_stream_aborted: pattern=%r text_len=%d thinking_len=%d",
                    stream_repeat_pattern,
                    len(text_content),
                    len(thinking_content),
                )
                if cancelled():
                    yield await cancellation_done_event()
                    return
                recovery_text = repetitive_recovery.request(step=step, max_steps=max_steps)
                if recovery_text is not None:
                    messages.append(Message(role="user", content=recovery_text))
                    yield InjectedMessageEvent(content=recovery_text, injection_id=None, user_visible=False)
                    yield ProgressEvent(step=step + 1, content="模型输出异常重复，正在重新生成（1/1）。")
                    elapsed = perf_counter() - step_start
                    total = perf_counter() - run_start
                    if hook_mgr.hooks:
                        await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                    yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                    continue
                msg = (
                    "LLM stream aborted after repetitive output was detected. "
                    "Bounded recovery is exhausted or no steps remain. "
                    "The repeated output was not saved to conversation history."
                )
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                    await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
                yield ErrorEvent(message=msg, is_fatal=True, error_code="LLM_REPETITIVE_OUTPUT",
                                 error_category="invalid_response", error_details={
                                     "recoveryAttempts": repetitive_recovery.attempts,
                                     "maxRecoveryAttempts": 1, "retryable": False,
                                 })
                yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
                return

            if cancelled():
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                return

            if finish_event is None:
                msg = "LLM stream ended without a finish event"
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                    await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
                yield ErrorEvent(message=msg, is_fatal=True)
                yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
                return

            # Build LLMResponse equivalent from streamed data
            response = LLMResponse(
                content=text_content,
                thinking=thinking_content if thinking_content else None,
                tool_calls=finish_event.tool_calls,
                finish_reason=finish_event.finish_reason or "stop",
                usage=finish_event.usage,
                provider_response_id=finish_event.provider_response_id,
                truncated_tool_calls=finish_event.truncated_tool_calls,
                raw_finish_reason=finish_event.raw_finish_reason,
                stream_dropped_mid_tool=finish_event.stream_dropped_mid_tool,
                oversized_tool_calls=finish_event.oversized_tool_calls,
            )
            provider_request_id = finish_event.provider_request_id
            yield LLMOutputEvent(
                step=step + 1,
                content=response.content,
                thinking=response.thinking,
                tool_calls=(
                    [tc.model_dump() for tc in response.tool_calls]
                    if response.tool_calls
                    else None
                ),
                finish_reason=response.finish_reason,
                usage=(
                    response.usage.model_dump(
                        include={
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                        }
                    )
                    if response.usage
                    else None
                ),
                provider_request_id=provider_request_id,
            )

        except Exception as exc:
            from ..llm.error_messages import structured_llm_error
            from ..retry import StreamInterrupted

            provider_request_id = None
            if isinstance(exc, StreamInterrupted):
                partial_text = exc.partial_text or ""
                partial_thinking = exc.partial_thinking or ""
                if partial_text or partial_thinking:
                    messages.append(
                        Message(
                            role="assistant",
                            content=partial_text,
                            thinking=partial_thinking or None,
                            tool_calls=None,
                        )
                    )
                if cancelled():
                    if hook_mgr.hooks:
                        await hook_mgr.fire_done(
                            stop_reason=StopReason.CANCELLED,
                            final_content="Task cancelled by user.",
                        )
                    yield DoneEvent(
                        stop_reason=StopReason.CANCELLED,
                        final_content="Task cancelled by user.",
                    )
                    return
                recovery_text = stream_recovery.request(step=step, max_steps=max_steps)
                if recovery_text is not None:
                    messages.append(Message(role="user", content=recovery_text))
                    yield InjectedMessageEvent(
                        content=recovery_text, injection_id=None, user_visible=False,
                    )
                    yield ProgressEvent(
                        step=step + 1, content="模型连接中断，正在自动恢复（1/1）。",
                    )
                    _log.warning(
                        "stream_interruption/recovery attempt=1/1 session_id=%s error=%s",
                        session_id, exc.last_exception,
                    )
                    elapsed = perf_counter() - step_start
                    total = perf_counter() - run_start
                    if hook_mgr.hooks:
                        await hook_mgr.fire_step_end(
                            step=step + 1,
                            elapsed_seconds=elapsed,
                            total_elapsed_seconds=total,
                        )
                    yield StepEnd(
                        step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total,
                    )
                    continue
                msg = (
                    "模型连接中断，任务尚未完成。已保留当前进度，请稍后发送“继续”重试。"
                )
                _log.warning(
                    "stream_interruption/stopped attempts=%d session_id=%s error=%s",
                    stream_recovery.attempts, session_id, exc.last_exception,
                )
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=False, exception=exc)
                    await hook_mgr.fire_done(stop_reason=StopReason.INTERRUPTED, final_content=partial_text)
                yield ErrorEvent(message=msg, is_fatal=False, exception=exc)
                yield DoneEvent(stop_reason=StopReason.INTERRUPTED, final_content=partial_text)
                return
            # structured_llm_error unwraps RetryExhaustedError to inspect the
            # underlying provider error while preserving a stable host contract.
            try:
                error_provider = getattr(llm, "provider", "")
            except Exception:
                error_provider = ""
            try:
                error_model = getattr(llm, "model", "")
            except Exception:
                error_model = ""
            error_details = structured_llm_error(
                exc,
                provider=error_provider,
                model=error_model,
            )
            msg = str(error_details["message"])
            if error_details["category"] == "content_filter":
                # Model refusal (e.g. content moderation): present as a normal
                # assistant reply — the turn ended cleanly, it's not a crash.
                # No "Error:" prefix, no red banner; persisted to history.
                messages.append(Message(role="assistant", content=msg, tool_calls=None))
                if hook_mgr.hooks:
                    await hook_mgr.fire_done(stop_reason=StopReason.END_TURN, final_content=msg)
                yield ContentEvent(content=msg)
                yield DoneEvent(stop_reason=StopReason.END_TURN, final_content=msg)
                return
            if hook_mgr.hooks:
                await hook_mgr.fire_error(message=msg, is_fatal=True, exception=exc)
                await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
            yield ErrorEvent(
                message=msg,
                is_fatal=True,
                exception=exc,
                error_code=error_details["code"],
                error_category=str(error_details["category"]),
                error_details=error_details,
            )
            yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
            return
        finally:
            if llm_debug_sink_token is not None:
                reset_llm_debug_sink(llm_debug_sink_token)

        # ── Token tracking ──────────────────────────────────
        if response.usage:
            api_total_tokens = response.usage.total_tokens
            api_prompt_tokens = response.usage.prompt_tokens
            yield TokenUsageEvent(total_tokens=api_total_tokens)

        # ── Hook: LLM response ─────────────────────────────
        if hook_mgr.hooks:
            await hook_mgr.fire_llm_response(response=response)

        # ── Log response ────────────────────────────────────
        if logger:
            logger.log_response(
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
                finish_reason=response.finish_reason,
                usage=response.usage,
                provider_request_id=provider_request_id,
            )

        # ── Suspected-truncation diagnostic (always on) ─────
        # A normal finish ("stop"/"end_turn"/None) with no tool calls but a
        # body that ends mid-thought means the provider likely clipped the
        # turn without admitting it (vs the honest "length" path below).
        # Logged unconditionally — independent of the continuation feature —
        # so the frequency is visible in box-agent-stderr.log for triage.
        if (
            not response.tool_calls
            and response.finish_reason in (None, "stop", "end_turn")
            and response.content
            and reply_is_substantial(
                len(response.content),
                response.usage.completion_tokens if response.usage else None,
            )
            and looks_like_truncated_output(response.content)
        ):
            _tail = response.content.rstrip()[-40:]
            _log.warning(
                "suspected_truncation: finish_reason=%r completion_tokens=%s "
                "content_len=%d request_id=%s tail=%r",
                response.finish_reason,
                response.usage.completion_tokens if response.usage else None,
                len(response.content),
                provider_request_id,
                _tail,
            )

        # ── Build assistant turn (append AFTER truncation handling) ─
        # The assistant message that carries a broken tool_call must NOT be
        # persisted when we plan to retry — feeding a half-baked tool_call
        # back to the model just teaches it to keep producing them. Build the
        # message here, then append only in the branches that keep it.
        if (callable(on_response_received)
                and response.finish_reason not in {"provider_stale", "length", "max_tokens"}
                and not (response.truncated_tool_calls or response.stream_dropped_mid_tool or response.oversized_tool_calls)
                and ((response.content or "").strip() or response.tool_calls)):
            on_response_received()

        assistant_msg = Message(
            role="assistant",
            content=response.content,
            thinking=response.thinking,
            usage=response.usage,
            request_only_input_tokens=request_only_input_tokens,
            tool_calls=(
                [tool_call.model_copy(deep=True) for tool_call in response.tool_calls]
                if response.tool_calls
                else None
            ),
        )

        # Raw image blocks are a one-shot request overlay. Retain them only
        # when an empty provider-stale response will be retried verbatim.
        if (
            response.finish_reason != "provider_stale"
            or (response.content or "").strip()
            or (response.thinking or "").strip()
        ):
            pending_transient_followup_blocks.clear()
            pending_transient_followup_tokens = 0

        if response.finish_reason == "provider_stale":
            has_partial_content = bool(response.content.strip())
            if has_partial_content:
                provider_stale_retries = 0
            can_retry_stale = (
                provider_stale_recoveries
                < _runtime_defaults.max_provider_stale_recoveries
            )
            if can_retry_stale:
                provider_stale_recoveries += 1
                if not has_partial_content:
                    provider_stale_retries += 1
                if has_partial_content:
                    messages.append(assistant_msg)
                    recovery_text = (
                        "模型服务在上一轮已经输出部分内容、但尚未完成动作时长时间没有返回"
                        "新数据。请从未完成的动作继续，不要重复已经输出的说明，也不要把"
                        "说明误当成任务完成。若要生成长文件，请使用 write_file 的"
                        "chunk_index/final 分块协议，每块建议不超过 "
                        f"{RECOMMENDED_GENERATED_BODY_CHARS:,} 字符；bash 只传短命令。"
                    )
                    messages.append(
                        Message(
                            role="user",
                            content=format_injected_message(recovery_text),
                        )
                    )
                    yield InjectedMessageEvent(
                        content=recovery_text,
                        injection_id=None,
                        user_visible=False,
                    )
                _log.warning(
                    "provider stale recovery %d/%d after %.0fs without new chunks "
                    "consecutive_empty=%d partial_content_len=%d",
                    provider_stale_recoveries,
                    _runtime_defaults.max_provider_stale_recoveries,
                    effective_provider_stale_seconds,
                    provider_stale_retries,
                    len(response.content),
                )
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                continue
            msg = "模型服务长时间没有返回数据，已停止本轮任务。"
            _cleanup_incomplete_messages(messages)
            if hook_mgr.hooks:
                await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
            yield ErrorEvent(message=msg, is_fatal=True)
            yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
            return

        # ── Deterministic streamed tool-argument budget violation ─────
        # This is locally detected before JSON parsing or tool execution.
        # Retrying with a larger completion budget repeats the failure, so
        # provide one explicit authoring-protocol repair and then stop.
        if response.finish_reason == "tool_argument_limit":
            details = response.oversized_tool_calls or []
            rendered = ", ".join(
                f"{item.get('name') or '?'}={item.get('arguments_len', 0)}/"
                f"{item.get('limit', 0)} chars"
                for item in details
            ) or "unknown tool"
            if response.content.strip():
                messages.append(assistant_msg)
            if oversized_tool_argument_retries < 1:
                oversized_tool_argument_retries += 1
                repair_text = (
                    "上一轮工具参数在流式生成阶段超过安全预算，工具没有执行。"
                    f"超限信息：{rendered}。不要重新生成相同的大参数，也不要提高 token "
                    "预算。bash 只执行短命令；长文本文件请使用 write_file 的有序分块。"
                    "同一路径已有成功分块时，从最近一次结果返回的 next_chunk_index 继续；"
                    "只有尚无已接受分块时才使用 chunk_index=0。每块建议不超过 "
                    f"{RECOMMENDED_GENERATED_BODY_CHARS:,} 字符，最后一块设置 final=true，"
                    "然后校验文件。"
                )
                messages.append(
                    Message(role="user", content=format_injected_message(repair_text))
                )
                yield InjectedMessageEvent(
                    content=repair_text,
                    injection_id=None,
                    user_visible=False,
                )
                _log.warning(
                    "tool argument limit repair %d/1: %s request_id=%s",
                    oversized_tool_argument_retries,
                    rendered,
                    provider_request_id,
                )
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                continue

            msg = "工具参数连续超出安全预算，已停止本轮；请改为分块写入后重试。"
            _log.error("tool argument limit repair exhausted: %s", rendered)
            _cleanup_incomplete_messages(messages)
            if hook_mgr.hooks:
                await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
            yield ErrorEvent(message=msg, is_fatal=True)
            yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
            return

        # ── Output truncated by provider token limit ────────
        # The finish reason, not best-effort JSON parseability, determines
        # whether a response is complete enough to execute. A parseable tool
        # call that arrived with length/max_tokens is still only a discarded
        # attempt. Both parseable and broken attempts receive the same hidden
        # user recovery instruction and no tool result, because no ToolCall
        # was admitted into executable conversation history.
        if response.finish_reason in ("length", "max_tokens"):
            stream_dropped = getattr(response, "stream_dropped_mid_tool", False)
            has_broken_tool_call = bool(response.truncated_tool_calls)
            visible_text = (response.content or "").strip()
            parsed_tool_names = {
                tool_call.function.name
                for tool_call in (response.tool_calls or [])
                if tool_call.function.name
            }
            truncated_tool_names = {
                str(item.get("name"))
                for item in (response.truncated_tool_calls or [])
                if item.get("name")
            }
            tool_names = parsed_tool_names | truncated_tool_names
            has_tool_attempt = bool(
                response.tool_calls or response.truncated_tool_calls
            )

            if has_tool_attempt:
                if visible_text:
                    messages.append(
                        Message(
                            role="assistant",
                            content=response.content,
                            thinking=response.thinking,
                        )
                    )
                if truncated_tool_call_retries < max_truncated_tool_call_retries:
                    truncated_tool_call_retries += 1
                    repair_text = (
                        _OUTPUT_LENGTH_WRITE_FILE_RECOVERY
                        if "write_file" in tool_names
                        else _OUTPUT_LENGTH_TOOL_RECOVERY
                    )
                    messages.append(
                        Message(
                            role="user",
                            content=format_injected_message(repair_text),
                        )
                    )
                    yield InjectedMessageEvent(
                        content=repair_text,
                        injection_id=None,
                        user_visible=False,
                    )
                    _log.warning(
                        "discarded output-length tool attempt %d/%d: tools=%s "
                        "parseable=%s broken=%s stream_dropped=%s request_id=%s",
                        truncated_tool_call_retries,
                        max_truncated_tool_call_retries,
                        sorted(tool_names),
                        bool(response.tool_calls),
                        has_broken_tool_call,
                        stream_dropped,
                        provider_request_id,
                    )
                    elapsed = perf_counter() - step_start
                    total = perf_counter() - run_start
                    if hook_mgr.hooks:
                        await hook_mgr.fire_step_end(
                            step=step + 1,
                            elapsed_seconds=elapsed,
                            total_elapsed_seconds=total,
                        )
                    yield StepEnd(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                    continue

                msg = "工具调用因输出长度限制未执行；分块重试仍未完成。"
                _log.error(
                    "output-length tool recovery exhausted: tools=%s request_id=%s",
                    sorted(tool_names),
                    provider_request_id,
                )
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                    await hook_mgr.fire_done(
                        stop_reason=StopReason.MAX_TOKENS,
                        final_content=msg,
                    )
                yield ErrorEvent(message=msg, is_fatal=True)
                yield DoneEvent(stop_reason=StopReason.MAX_TOKENS, final_content=msg)
                return

            # No tool attempt: preserve the existing text continuation and
            # empty-response retry behavior.
            if visible_text and truncation_continuations < max_truncation_continuations:
                messages.append(assistant_msg)
                truncation_continuations += 1
                tail = response.content.rstrip()[-40:]
                cont_text = truncation_continuation_text(tail)
                messages.append(Message(role="user", content=cont_text))
                yield InjectedMessageEvent(
                    content=cont_text, injection_id=None, user_visible=False,
                )
                _log.warning(
                    "length-with-visible-text continuation %d/%d: "
                    "has_broken_tool_call=%s stream_dropped=%s "
                    "completion_tokens=%s request_id=%s",
                    truncation_continuations,
                    max_truncation_continuations,
                    has_broken_tool_call,
                    stream_dropped,
                    response.usage.completion_tokens if response.usage else None,
                    provider_request_id,
                )
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                continue

            if (
                not visible_text
                and truncated_tool_call_retries < max_truncated_tool_call_retries
            ):
                truncated_tool_call_retries += 1
                requested_max = getattr(llm, "max_output_tokens", None) or 4096
                boost = requested_max * (truncated_tool_call_retries + 1)
                boost_cap = max(truncated_tool_call_boost_cap, requested_max)
                boosted = min(boost, boost_cap)
                if not stream_dropped and hasattr(llm, "set_ephemeral_max_output_tokens"):
                    llm.set_ephemeral_max_output_tokens(boosted)
                _log.warning(
                    "truncation retry %d/%d: stream_dropped=%s has_broken_tool_call=%s "
                    "boosted_max_tokens=%s completion_tokens=%s request_id=%s",
                    truncated_tool_call_retries,
                    max_truncated_tool_call_retries,
                    stream_dropped,
                    has_broken_tool_call,
                    None if stream_dropped else boosted,
                    response.usage.completion_tokens if response.usage else None,
                    provider_request_id,
                )
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                continue

            # Retries / continuations exhausted — persist plain text and
            # surface the error.
            messages.append(assistant_msg)
            usage = response.usage
            diag_parts: list[str] = []
            if usage is not None:
                diag_parts.append(f"completion_tokens={usage.completion_tokens}")
                diag_parts.append(f"total_tokens={usage.total_tokens}")
            requested_max = getattr(llm, "max_output_tokens", None)
            if requested_max is not None:
                diag_parts.append(f"requested_max_tokens={requested_max}")
            if provider_request_id:
                diag_parts.append(f"request_id={provider_request_id}")
            if response.truncated_tool_calls:
                rendered = ", ".join(
                    f"{tc.get('name') or '?'}(args≈{tc.get('arguments_len', 0)} chars)"
                    for tc in response.truncated_tool_calls
                )
                diag_parts.append(f"truncated_tool_calls=[{rendered}]")
            diag_parts.append(f"retries={truncated_tool_call_retries}")
            diag_parts.append(f"continuations={truncation_continuations}")
            # User-facing message: keep it short and honest — the real cause
            # is rarely "hit max_tokens" (much more often a relay dropped the
            # stream or the model emitted broken JSON), and the long English
            # diagnostic that used to be inlined here got string-concatenated
            # onto the partial reply by hosts that append GENERATE chunks
            # (officev3 does). The full diagnostic still goes to stderr so
            # operators can triage.
            msg = "输出被截断，请重试。"
            _log.error(
                "truncation retries exhausted: %s",
                "; ".join(diag_parts),
            )
            _cleanup_incomplete_messages(messages)
            if hook_mgr.hooks:
                await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                await hook_mgr.fire_done(stop_reason=StopReason.MAX_TOKENS, final_content=msg)
            yield ErrorEvent(message=msg, is_fatal=True)
            yield DoneEvent(stop_reason=StopReason.MAX_TOKENS, final_content=msg)
            return

        # ── Append assistant message (non-truncated path) ───
        messages.append(assistant_msg)
        if session_log is not None and session_turn is not None:
            session_log.append_unlogged_messages(
                session_log_messages(messages),
                turn=session_turn,
                step=step + 1,
            )

        # Reset the retry counter now that a clean turn landed — a future
        # truncation on a later step should get its own fresh budget.
        truncated_tool_call_retries = 0
        oversized_tool_argument_retries = 0
        provider_stale_retries = 0
        provider_stale_recoveries = 0

        # ── No tool calls → done (or continue if injected) ──
        if not response.tool_calls:
            if (
                force_plan_for_turn
                and not plan_write_succeeded
                and not forced_plan_retry_injected
            ):
                forced_plan_retry_injected = True
                messages.append(
                    Message(
                        role="user",
                        content=format_injected_message(_FORCED_PLAN_RETRY_GUIDANCE),
                    )
                )
                yield InjectedMessageEvent(
                    content=_FORCED_PLAN_RETRY_GUIDANCE,
                    injection_id=None,
                    user_visible=False,
                )
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                continue

            # Check inject queue — if messages are pending, continue
            # the loop so the LLM sees them on the next iteration.
            if inject_queue and not inject_queue.empty():
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                continue

            # ── Suspected-truncation continuation (opt-in) ──
            # The provider reported a normal finish with no tool calls, but
            # the body ends mid-thought. Re-prompt once (bounded) to finish
            # the reply in the *same* message: the truncated assistant text
            # is already appended above, and we do NOT emit a DoneEvent, so
            # the continuation streams into the same prompt turn. Skipped for
            # short replies (legitimately end without punctuation).
            if (
                truncation_continuation_enabled
                and truncation_continuations < max_truncation_continuations
                and response.finish_reason in (None, "stop", "end_turn")
                and response.content.strip()
                and reply_is_substantial(
                    len(response.content),
                    response.usage.completion_tokens if response.usage else None,
                )
                and looks_like_truncated_output(response.content)
            ):
                truncation_continuations += 1
                tail = response.content.rstrip()[-40:]
                cont_text = truncation_continuation_text(tail)
                messages.append(Message(role="user", content=cont_text))
                yield InjectedMessageEvent(content=cont_text, injection_id=None, user_visible=False)
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                continue

            if visible_tool_call_total > 0 and not response.content.strip():
                if (
                    not empty_final_answer_retry_injected
                    and step + 1 < max_steps
                ):
                    empty_final_answer_retry_injected = True
                    retry_text = empty_final_answer_retry_text(visible_tool_call_total)
                    messages.append(
                        Message(role="user", content=format_injected_message(retry_text))
                    )
                    yield InjectedMessageEvent(
                        content=retry_text,
                        injection_id=None,
                        user_visible=False,
                    )
                    elapsed = perf_counter() - step_start
                    total = perf_counter() - run_start
                    if hook_mgr.hooks:
                        await hook_mgr.fire_step_end(
                            step=step + 1,
                            elapsed_seconds=elapsed,
                            total_elapsed_seconds=total,
                        )
                    yield StepEnd(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                    continue

                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                _log.error(
                    "empty final answer after bounded retry: visible_tool_calls=%d request_id=%s",
                    visible_tool_call_total,
                    provider_request_id,
                )
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                    await hook_mgr.fire_error(
                        message=_EMPTY_FINAL_ANSWER_ERROR,
                        is_fatal=True,
                        exception=None,
                    )
                    await hook_mgr.fire_done(
                        stop_reason=StopReason.ERROR,
                        final_content=_EMPTY_FINAL_ANSWER_ERROR,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                yield ErrorEvent(message=_EMPTY_FINAL_ANSWER_ERROR, is_fatal=True)
                yield DoneEvent(
                    stop_reason=StopReason.ERROR,
                    final_content=_EMPTY_FINAL_ANSWER_ERROR,
                )
                return

            continuation = await turn_continuation.evaluate(
                llm=llm,
                user_request=continuation_user_request,
                content=response.content,
                finish_reason=response.finish_reason,
                tools_available=bool(tool_list),
                step=step,
                max_steps=max_steps,
                cancelled=cancelled(),
                session_id=session_id,
                turn_id=turn_id,
                title=title,
                should_interrupt=lambda: cancelled() or (
                    inject_queue is not None and not inject_queue.empty()
                ),
            )
            # Re-enter the existing cancellation/queue handlers before accepting
            # a verdict made while the user was cancelling or changing the task.
            if cancelled() or (inject_queue is not None and not inject_queue.empty()):
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                continue
            if continuation is not None:
                messages.append(Message(role="user", content=continuation.prompt))
                yield InjectedMessageEvent(
                    content=continuation.prompt,
                    injection_id=None,
                    user_visible=False,
                )
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                continue

            elapsed = perf_counter() - step_start
            total = perf_counter() - run_start
            if hook_mgr.hooks:
                await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                await hook_mgr.fire_done(stop_reason=StopReason.END_TURN, final_content=response.content)
            # Extract memory at agent loop end (background)
            if memory_extractor:
                asyncio.create_task(
                    memory_extractor.maybe_extract(
                        messages,
                        "loop_end",
                        turn_id=memory_turn_id,
                    )
                )
            yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
            proposal = await _build_proposal_event_with_plan()
            if proposal is not None:
                yield proposal
            yield DoneEvent(stop_reason=StopReason.END_TURN, final_content=response.content)
            return

        # ── Cancellation check (before tools) ──────────────
        if not await run_checkpoint():
            _cleanup_incomplete_messages(messages)
            if hook_mgr.hooks:
                await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            return

        # Resolve backwards-compatible aliases only against tools offered in
        # this model step. Keep the persisted assistant turn unchanged, while
        # all execution policy sees the canonical tool name.
        execution_tool_calls = prepared_tools.canonicalize_calls(response.tool_calls)

        # ── Execute tool calls ──────────────────────────────
        # Loop-guard: bail out if the model emits the same all-empty-args
        # tool_call set as the previous turn. This is the signature of an
        # upstream protocol bug (e.g. relay truncation) where empty args
        # come back, error responses get fed back, and the model just
        # repeats — without this check the loop runs to max_steps.
        all_empty = all(not tc.function.arguments for tc in execution_tool_calls)
        if all_empty:
            sig = tuple(sorted(tc.function.name for tc in execution_tool_calls))
            if sig == empty_args_signature:
                empty_args_repeats += 1
            else:
                empty_args_signature = sig
                empty_args_repeats = 1
            if empty_args_repeats >= EMPTY_ARGS_LIMIT:
                msg = (
                    f"Aborting: model emitted empty-arguments tool_calls "
                    f"{empty_args_repeats}x in a row ({list(sig)}). "
                    "This usually indicates an upstream relay bug or model "
                    "loop. See logs for the raw stream."
                )
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                    await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
                yield ErrorEvent(message=msg, is_fatal=True)
                yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
                return
        else:
            empty_args_signature = None
            empty_args_repeats = 0

        # The kernel decides the conversation boundary. The engine owns all
        # per-call preparation, scheduling, permission continuation and results.
        step_contains_plan_write = any(tc.function.name == "plan_write" for tc in execution_tool_calls)
        organic_plan_approval_gate_enabled = (
            pause_after_plan_write and not plan_approval_approved
            and not plan_approval_gate_enabled and has_plan_tool and step_contains_plan_write
        )
        plan_approval_gate_active = plan_approval_gate_enabled or organic_plan_approval_gate_enabled

        def decorate_control_result(name: str, result: ToolResult) -> ToolResult:
            if plan_approval_gate_active and name == "plan_write" and result.success:
                return result.model_copy(update={"raw_output": _attach_plan_approval_payload(
                    result.raw_output, request_id=plan_approval_request_id,
                )})
            return result

        control = ToolStepControl(
            step=step + 1,
            allowed_names=frozenset({"plan_write"}) if plan_approval_gate_active else None,
            blocked_reason=_PLAN_APPROVAL_SKIP_MESSAGE,
            result_transform=decorate_control_result,
            pending_followup_tokens=pending_transient_followup_tokens,
        )
        tool_summary: ToolStepSummary | None = None
        async with aclosing(tool_engine.execute_calls(prepared_tools, response.tool_calls, control)) as records:
            async for record in records:
                if isinstance(record, ToolStepSummary):
                    tool_summary = record
                else:
                    yield record
        if cancelled():
            _cleanup_incomplete_messages(messages)
            if hook_mgr.hooks:
                await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            return
        if tool_summary is None:
            raise RuntimeError("Tool engine ended without a step summary")
        step_made_progress = tool_summary.made_progress
        visible_tool_call_total += tool_summary.visible_calls
        completed_turn_ending_tool = tool_summary.completed_turn_ending_tool
        plan_write_succeeded = plan_write_succeeded or "plan_write" in tool_summary.successful_tools
        plan_approval_gate_completed = plan_approval_gate_completed or (
            plan_approval_gate_active and "plan_write" in tool_summary.successful_tools
        )
        pending_transient_followup_blocks.extend(tool_summary.transient_blocks)
        pending_transient_followup_tokens += tool_summary.transient_tokens
        if tool_summary.repair_guidance:
            messages.append(Message(role="user", content=format_injected_message(tool_summary.repair_guidance)))
            yield InjectedMessageEvent(content=tool_summary.repair_guidance, injection_id=None, user_visible=False)

        if completed_turn_ending_tool is not None:
            elapsed = perf_counter() - step_start
            total = perf_counter() - run_start
            if hook_mgr.hooks:
                await hook_mgr.fire_step_end(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                await hook_mgr.fire_done(
                    stop_reason=StopReason.WAITING_FOR_USER,
                    final_content=_WAITING_FOR_USER_DONE_CONTENT,
                )
            yield StepEnd(
                step=step + 1,
                elapsed_seconds=elapsed,
                total_elapsed_seconds=total,
            )
            yield DoneEvent(
                stop_reason=StopReason.WAITING_FOR_USER,
                final_content=_WAITING_FOR_USER_DONE_CONTENT,
            )
            return

        if plan_approval_gate_completed:
            elapsed = perf_counter() - step_start
            total = perf_counter() - run_start
            if hook_mgr.hooks:
                await hook_mgr.fire_step_end(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                await hook_mgr.fire_done(
                    stop_reason=StopReason.END_TURN,
                    final_content=_PLAN_APPROVAL_DONE_CONTENT,
                )
            yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
            yield DoneEvent(
                stop_reason=StopReason.END_TURN,
                final_content=_PLAN_APPROVAL_DONE_CONTENT,
            )
            return

        if tool_summary.search_guidance:
            messages.append(Message(role="user", content=format_injected_message(tool_summary.search_guidance)))
            yield InjectedMessageEvent(content=tool_summary.search_guidance, injection_id=None, user_visible=False)

        if (
            visible_tool_call_total > final_summary_after_calls
            and not final_summary_guidance_injected
        ):
            final_summary_guidance_injected = True
            summary_text = final_summary_wrapup_text(
                visible_tool_call_total,
                final_summary_after_calls,
            )
            messages.append(Message(role="user", content=format_injected_message(summary_text)))
            yield InjectedMessageEvent(content=summary_text, injection_id=None, user_visible=False)

        # ── Step end ────────────────────────────────────────
        # Update the no-progress counter (only steps that ran tools reach
        # here — the no-tool-call path returns earlier with END_TURN).
        if no_progress_limit:
            if step_made_progress:
                no_progress_steps = 0
            else:
                no_progress_steps += 1

        elapsed = perf_counter() - step_start
        total = perf_counter() - run_start
        yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
        if hook_mgr.hooks:
            await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)

        # ── Periodic memory extraction (background) ──────────
        if memory_extractor:
            asyncio.create_task(
                memory_extractor.maybe_extract(
                    messages,
                    "step_interval",
                    turn_id=memory_turn_id,
                )
            )

    # ── Max steps exhausted ─────────────────────────────────
    msg = f"Task couldn't be completed after {max_steps} steps."
    if memory_extractor:
        asyncio.create_task(
            memory_extractor.maybe_extract(
                messages,
                "loop_end",
                turn_id=memory_turn_id,
            )
        )
    if hook_mgr.hooks:
        await hook_mgr.fire_done(stop_reason=StopReason.MAX_STEPS, final_content=msg)
    proposal = await _build_proposal_event_with_plan()
    if proposal is not None:
        yield proposal
    yield DoneEvent(stop_reason=StopReason.MAX_STEPS, final_content=msg)


_SERVICE_OWNED_RUN_ARGUMENTS = frozenset(
    {
        "llm",
        "summary_llm",
        "tools",
        "permission_negotiator",
        "hooks",
        "plugins",
        "memory_manager",
        "memory_extractor",
        "session_log",
        "tool_exposure_manager",
        "tool_result_storage",
    }
)


class AgentLoopKernel:
    """One configured execution of the stable agent-loop state machine."""

    def __init__(
        self,
        *,
        _services: KernelServices,
        _runtime_defaults: _LoopRuntimeDefaults = _DEFAULT_LOOP_RUNTIME_DEFAULTS,
        **run_arguments: Any,
    ) -> None:
        service_arguments = _SERVICE_OWNED_RUN_ARGUMENTS.intersection(run_arguments)
        if service_arguments:
            names = ", ".join(sorted(service_arguments))
            raise TypeError(
                "AgentLoopKernel accepts capability implementations only through "
                f"_services; received: {names}"
            )
        self._runtime_defaults = _runtime_defaults
        self._run_arguments = dict(run_arguments)
        if _services.tool_engine is None:
            # Legacy callers may still construct the original service bundle.
            # Resolve the default once at the run boundary, never per step.
            from ..tools.engine.engine import DefaultToolEngine

            _services = replace(
                _services,
                tool_engine=DefaultToolEngine(
                    tools=_services.tool_catalog,
                    tool_exposure=_services.tool_exposure,
                    tool_result_store=_services.tool_result_store,
                ),
            )
        self._services = _services

    async def run(self) -> AsyncIterator[AgentEvent]:
        """Run this kernel instance and yield its existing event stream."""
        events = _run_agent_loop_impl(
            _runtime_defaults=self._runtime_defaults,
            _services=self._services,
            **self._run_arguments,
        )
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()
            if self._services.tool_engine is not None:
                await self._services.tool_engine.aclose()


async def run_agent_loop(
    *,
    _services: KernelServices,
    messages: list[Message],
    max_steps: int = _DEFAULT_AGENT_CONFIG.max_steps,
    tool_limits: ToolLimitsConfig | None = None,
    max_tool_calls: int | None = None,
    max_delegated_tool_calls: int | None = None,
    web_search_total_limit: int | None = None,
    token_limit: int = 113400,
    is_cancelled: CancelChecker | None = None,
    logger: AgentLogger | None = None,
    workspace_dir: str | None = None,
    memory_turn_id: str = "",
    memory_promotion_enabled: bool = False,
    memory_promotion_hit_threshold: int = 5,
    memory_promotion_cooldown_days: int = 14,
    inject_queue: asyncio.Queue[Any] | None = None,
    thinking_enabled: bool = False,
    session_id: str = "",
    turn_id: str = "",
    title: str = "",
    call_kind: str = "",
    force_plan_start: bool = False,
    require_plan_approval: bool = False,
    plan_approval: dict[str, Any] | None = None,
    plan_start_text: str | None = None,
    pause_after_plan_write: bool = False,
    no_progress_limit: int | None = None,
    max_parallel_tools: int = 8,
    parallel_tool_timeout_seconds: float | None = 900.0,
    provider_stale_seconds: float | None = None,
    truncation_continuation_enabled: bool = True,
    max_truncation_continuations: int = 3,
    max_truncated_tool_call_retries: int = 3,
    truncated_tool_call_boost_cap: int = 32768,
    artifact_detection_enabled: bool = True,
    artifact_root_dir: str | Path | None = None,
    cache_fingerprint_context: dict[str, Any] | None = None,
    cache_fingerprint_sink: Callable[[dict[str, Any]], None] | None = None,
    active_skill_activator: ActiveSkillActivator | None = None,
    current_turn_text: str | None = None,
    context_resource_ledger: ContextResourceLedger | None = None,
    context_resource_dedup_enabled: bool = True,
    session_turn: int | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run with capabilities resolved by the outer composition layer."""
    run_arguments = dict(locals())
    services = run_arguments.pop("_services")

    kernel = AgentLoopKernel(
        _services=services,
        **run_arguments,
    )
    events = kernel.run()
    try:
        async for event in events:
            yield event
    finally:
        await events.aclose()
