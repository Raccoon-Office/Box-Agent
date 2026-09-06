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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Final
from urllib.parse import urlsplit

from ..artifacts import (
    OUTPUT_SUBDIR,
    artifact_scan_root as _artifact_scan_root,
    avoid_collision,
    ensure_output_dir,
    make_artifact as _make_artifact,
    safe_output_name,
)
from ..cache_fingerprint import build_cache_fingerprint
from ..config import AgentConfig, ToolLimitsConfig
from ..context_resources import (
    ContextResourceLedger,
    ResourceDescriptor,
    build_resource_receipt,
)
from ..evidence import (
    extract_http_urls as _http_urls,
    normalize_search_url as _normalize_search_url,
)
from ..events import (
    AgentEvent,
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LLMOutputEvent,
    LLMActivityEvent,
    LogFileEvent,
    MemoryProposalEvent,
    MemoryPromotionCandidate,
    PermissionRequestEvent,
    PlanSnapshotEvent,
    ProgressEvent,
    StepEnd,
    StepStart,
    StopReason,
    SummarizationEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolCallResult,
    ToolCallStart,
    WebSearchEvent,
)
from .context_engine import (
    TRANSIENT_FOLLOWUP_CONTEXT_RATIO,
    TRANSIENT_IMAGE_DEFAULT_TOKENS,
    TRANSIENT_IMAGE_MAX_TOKENS,
    TRANSIENT_IMAGE_PIXEL_TOKEN_DIVISOR,
    CompactionOutcome,
    _LEGACY_SUMMARY_MARKER,
    _LOCAL_FALLBACK_CHAR_LIMIT,
    _RECENT_MESSAGE_CHAR_LIMIT,
    _RECENT_MESSAGE_LIMIT,
    _RUNTIME_STATE_MARKER,
    _RUNTIME_STATE_CHAR_LIMIT,
    _SUMMARY_MARKER,
    _SUMMARY_MESSAGE_PREFIX,
    _SUMMARY_MESSAGE_SUFFIX,
    _SUMMARY_OUTPUT_CHAR_LIMIT,
    _SUMMARY_REQUEST,
    _WORKFLOW_CHECKPOINT_MARKER,
    _bound_retained_messages,
    _bound_text_middle,
    _create_summary,
    _deterministic_history_fallback,
    _estimate_context_from_latest_response,
    _fallback_context_estimate,
    _is_compaction_metadata,
    _is_summary_marker,
    _maybe_summarize,
    _message_chars,
    _recent_message_groups,
    _restore_runtime_state,
    _select_recent_messages,
    _summary_message_text,
    _transient_followup_token_estimate,
    _validate_transient_followup_result,
)
from .permission_gateway import (
    MAX_TOOL_PERMISSION_RETRIES,
    _approve_tool_permission,
    _negotiate_tool_permission_chain,
    _permission_event_kwargs,
    _policy_decision_payload,
)
from .ports import KernelServices
from .stream_controller import (
    resolve_provider_stale_seconds as _kernel_resolve_provider_stale_seconds,
    stream_with_activity as _kernel_stream_with_activity,
)
from .state import ToolBudgetState
from .tool_engine import (
    ToolBatchCompleted,
    ToolEngine,
    ToolEngineActivity,
    ToolEngineProgress,
    ToolInvocationCompleted,
    ToolInvocationRequest,
)
from .tool_result_pipeline import (
    _prepare_browser_screenshot_output,
    _persist_browser_screenshot_output,
    _trace_safe_tool_raw_output,
    _ARTIFACT_REF_RE,
    _BROWSER_SNAPSHOT_OUTPUT_PATH_ERROR,
    _ContextResourceHistoryDecision,
    _IGNORE_DIRS,
    _INTERRUPTED_TOOL_STUB,
    _MAX_ARTIFACT_COMPONENT_BYTES,
    _MAX_ARTIFACT_REF_CHARS,
    _MODEL_HISTORY_FILE_MUTATION_TOOLS,
    _MODEL_HISTORY_PLACEHOLDER_ARGUMENTS,
    _MODEL_HISTORY_PLACEHOLDER_RECOVERY_REQUIRED,
    _ModelHistoryPlaceholderRecovery,
    _PLOT_DATA_RE,
    _SEARCH_QUERY_STOPWORDS,
    _SEARCH_QUERY_TERM_RE,
    _SITE_QUERY_RE,
    _SITE_QUERY_TOKEN_RE,
    _WEB_SEARCH_COMPACT_MAX_ITEMS,
    _WEB_SEARCH_RESULT_KEYS,
    _candidate_search_items,
    _cleanup_incomplete_messages,
    _context_resource_history_decision,
    _dedupe_web_search_content,
    _detect_artifacts,
    _detect_changed_files,
    _detect_new_files,
    _detect_regex_artifacts,
    _detect_tool_artifacts,
    _extract_web_search_payload,
    _first_present,
    _log_web_search_model_results,
    _model_history_placeholder_argument,
    _model_history_placeholder_recovery_error,
    _model_history_recovery_target,
    _normalize_search_title,
    _normalize_web_search_query,
    _persist_browser_snapshot_output,
    _prepare_browser_snapshot_output,
    _rank_web_search_items,
    _record_context_resource_history,
    _record_model_history_placeholder_recovery_result,
    _repeatable_framework_error,
    _requested_site_domain,
    _sanitize_dangling_tool_calls,
    _search_item_snippet,
    _search_item_title,
    _search_item_url,
    _search_result_list_found,
    _short_tool_text,
    _snapshot_workspace,
    _snapshot_workspace_signatures,
    _strip_plot_data,
    _tool_message_content_for_model,
    _url_matches_domain,
    _web_search_item_rank,
    _web_search_match_terms,
    _web_search_queries_are_near_duplicates,
    _web_search_query_terms,
    _web_search_result_key,
    _web_search_result_metadata,
    _with_filtered_search_items,
    _with_web_search_metadata,
    ToolResultPipelineInput,
    process_tool_result,
)
from ..logger import AgentLogger
from ..llm.debug_logging import reset_llm_debug_sink, set_llm_debug_sink
from ..model_history import is_model_history_placeholder
from ..session_trace import emit_session_trace
from ..session_log import SessionLogDurabilityError
from ..loop_guards import (
    EMPTY_ARGS_LIMIT,
    FINAL_SUMMARY_EXCLUDED_TOOLS,
    SEARCH_FILES_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    STREAM_REPEAT_MIN_CHUNKS,
    delegated_tool_call_budget_wrapup_text,
    format_injected_message,
    format_runtime_context_update,
    looks_like_truncated_output,
    near_limit_wrapup_text,
    no_progress_wrapup_text,
    repeated_stream_pattern,
    reply_is_substantial,
    search_files_empty_result_guidance,
    search_files_result_is_empty,
    total_tool_call_budget_wrapup_text,
    tool_call_budget_wrapup_text,
    truncation_continuation_text,
)

__all__ = ["run_agent_loop"]

_log = logging.getLogger("box_agent.core")
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
    build_tool_name_index,
)
from ..tools.argument_limits import RECOMMENDED_GENERATED_BODY_CHARS
from ..tools.browser_intent import BrowserToolIntentPolicy
from ..tools.skill_preload import build_active_skills_prompt
from ..tool_result_storage import ToolResultStorage
from ..turn_continuation import TurnContinuationController
from ..turn_policy import (
    text_is_short_acknowledgement,
    text_is_short_non_task_reply,
    text_requests_plan_start,
)

# Type alias — consumers supply a zero-arg callable that returns True
# when the execution should be cancelled.
CancelChecker = Callable[[], bool]
ActiveSkillActivator = Callable[[str, str], None]

_MODEL_HISTORY_PLACEHOLDER_REPAIR_LIMIT: Final[int] = 1
_MODEL_HISTORY_PLACEHOLDER_TOOL_ERROR = (
    "INTERNAL_MODEL_HISTORY_PLACEHOLDER: the requested tool argument is an internal "
    "history summary, not executable content. Regenerate the real argument. For static "
    "artifacts, use ordered write_file chunks instead of moving the body into execute_code."
)
_MODEL_HISTORY_PLACEHOLDER_REPAIR_GUIDANCE = (
    "An internal model-history placeholder was returned as a tool argument. Regenerate "
    "the missing real content now. Never copy text beginning with "
    "`[Full tool-call argument omitted from model history]`, `[Full file content omitted "
    "from model history]`, or `[Full tool output omitted from model history]` into any "
    "tool argument. For long static artifacts, continue write_file from the "
    "next_chunk_index returned by the last successful call for that path, or use "
    "chunk_index=0 only if no chunk has been accepted. Keep final=false until the "
    "last chunk; do not move the file body into execute_code."
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
) -> tuple[ToolCallResult | None, Message | None]:
    """Conservatively match v2 experience memory against the latest user prompt.

    Matches are injected as weak, one-turn context: the model is told these
    memories may be relevant and must ignore them when the user is starting a
    new task.  This avoids depending on the model deciding to call
    ``memory_search`` while keeping the memory signal non-authoritative.
    """
    latest_user = next((msg for msg in reversed(messages) if msg.role == "user"), None)
    if latest_user is None:
        return None, None

    user_text = (
        latest_user.content
        if isinstance(latest_user.content, str)
        else str(latest_user.content)
    )
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
        artifact_detection_enabled: If False, skip output-directory artifact
            snapshotting and detection for sessions that edit an existing
            project tree directly.
        truncation_continuation_enabled: If True (default), re-prompt the
            model once when a reply ends mid-sentence while the provider
            reported a normal finish, so the answer completes in the same
            message. See ``loop_guards.looks_like_truncated_output``.
        max_truncation_continuations: Per-turn cap on truncation
            continuations (loop guard against repeated false positives).
        artifact_root_dir: Optional explicit artifact directory supplied by a
            host session. Defaults to ``{workspace_dir}/output``.
        cache_fingerprint_context: Optional stable metadata to include with
            cache-sensitive request fingerprints, such as selected skill names.
        cache_fingerprint_sink: Optional callback that receives each fingerprint
            before the LLM request, for hosts that do not use ``AgentLogger``.
        current_turn_text: Optional host-sanitized latest user request used to
            gate tools that access the user's active browser tab. When omitted,
            the latest user message is used.
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
        Path.home() / ".box-agent" / "sessions"
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

    plan_write_succeeded = False
    # Suspected-truncation continuation (opt-in via
    # ``truncation_continuation_enabled``). Bounds how many times the loop
    # may re-prompt the model to finish a reply that ended mid-sentence
    # while the provider reported a normal finish.
    truncation_continuations = 0

    fallback_active_skill_prompts: dict[str, str] = {}

    def _activate_skill_result(
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> ToolResult:
        """Move a loaded skill from tool history into active system context."""
        tool = tools.get(tool_name)
        skill_name = arguments.get("skill_name")
        if (
            tool is None
            or not getattr(tool, "loads_active_skill_instructions", False)
            or not result.success
            or result.model_context is not None
            or not isinstance(skill_name, str)
            or not skill_name.strip()
            or not result.content.strip()
            or bool((result.raw_output or {}).get("broken"))
        ):
            return result

        normalized_name = skill_name.strip()
        if active_skill_activator is not None:
            active_skill_activator(normalized_name, result.content)
        elif messages and messages[0].role == "system":
            fallback_active_skill_prompts[normalized_name] = result.content
            system_content = (
                messages[0].content
                if isinstance(messages[0].content, str)
                else str(messages[0].content)
            )
            messages[0] = Message(
                role="system",
                content=build_active_skills_prompt(
                    system_content,
                    fallback_active_skill_prompts,
                ),
            )
        else:
            return result

        acknowledgement = (
            f"Skill '{normalized_name}' loaded into active system instructions. "
            "Follow those instructions for the active task."
        )
        return result.model_copy(update={"model_context": acknowledgement})

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

    # Per-turn guard for tools that can be repeatedly requested by the model
    # after it already has enough evidence. Once a budget is reached, later
    # calls are answered with synthetic tool errors so the protocol remains
    # valid while nudging the model to synthesize.
    tool_budget_state = ToolBudgetState(
        tool_call_limits=tool_call_limits,
        max_tool_calls=max_tool_calls,
        max_delegated_tool_calls=max_delegated_tool_calls,
        search_files_empty_result_limit=search_files_empty_result_limit,
        logger=_log,
    )
    visible_tool_call_total = 0
    final_summary_guidance_injected = False
    empty_final_answer_retry_injected = False
    web_search_seen_queries: set[str] = set()
    web_search_seen_result_keys: set[str] = set()
    verified_evidence_urls: set[str] = set()
    web_search_unique_results = 0
    web_search_duplicate_results = 0
    web_search_no_new_batches = 0
    search_files_empty_guidance_injected = False
    plan_start_emitted = False
    forced_plan_guidance_injected = False
    forced_plan_retry_injected = False
    plan_approval_approved = _plan_approval_is_approved(plan_approval)
    plan_approval_gate_completed = False
    plan_approval_request_id = "plan-" + hashlib.sha1(
        f"{run_start}:{_latest_user_text(messages)}".encode("utf-8", errors="ignore")
    ).hexdigest()[:10]
    model_history_placeholder_repairs = 0
    model_history_framework_error_counts: dict[str, int] = {}
    pending_model_history_recovery: _ModelHistoryPlaceholderRecovery | None = None

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
        if cancelled():
            if hook_mgr.hooks:
                await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            return

        step_start = perf_counter()
        web_search_step_seen = False
        web_search_step_executed = 0
        web_search_step_deferred = 0
        web_search_step_duplicate_queries = 0
        web_search_step_new_results = 0
        web_search_step_duplicate_results = 0
        web_search_step_structured_results = 0
        web_search_step_labels: list[str] = []
        model_history_placeholder_auto_repair_requested = False

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

        for tool_name, limit in tool_call_limits.items():
            if (
                tool_budget_state.tool_call_counts.get(tool_name, 0) >= limit
                and tool_name not in tool_budget_state.tool_budget_wrapup_injected
            ):
                tool_budget_state.tool_budget_wrapup_injected.add(tool_name)
                budget_text = tool_call_budget_wrapup_text(tool_name, limit)
                messages.append(
                    Message(role="user", content=format_injected_message(budget_text))
                )
                yield InjectedMessageEvent(content=budget_text, injection_id=None, user_visible=False)
        if (
            max_delegated_tool_calls is not None
            and tool_budget_state.delegated_tool_call_total >= max_delegated_tool_calls
            and not tool_budget_state.delegated_budget_guidance_injected
        ):
            tool_budget_state.delegated_budget_guidance_injected = True
            delegated_text = delegated_tool_call_budget_wrapup_text(
                max_delegated_tool_calls
            )
            messages.append(
                Message(role="user", content=format_injected_message(delegated_text))
            )
            yield InjectedMessageEvent(
                content=delegated_text,
                injection_id=None,
                user_visible=False,
            )
        if (
            tool_budget_state.search_files_consecutive_empty_results
            >= search_files_empty_result_limit
            and not search_files_empty_guidance_injected
        ):
            search_files_empty_guidance_injected = True
            guidance = search_files_empty_result_guidance(
                search_files_empty_result_limit
            )
            messages.append(
                Message(role="user", content=format_injected_message(guidance))
            )
            yield InjectedMessageEvent(
                content=guidance,
                injection_id=None,
                user_visible=False,
            )
        if (
            max_tool_calls is not None
            and tool_budget_state.tool_call_total >= max_tool_calls
            and "__total__" not in tool_budget_state.tool_budget_wrapup_injected
        ):
            tool_budget_state.tool_budget_wrapup_injected.add("__total__")
            budget_text = total_tool_call_budget_wrapup_text(max_tool_calls)
            messages.append(
                Message(role="user", content=format_injected_message(budget_text))
            )
            yield InjectedMessageEvent(
                content=budget_text,
                injection_id=None,
                user_visible=False,
            )

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
        transient_message = (
            Message(
                role="user",
                content=list(pending_transient_followup_blocks),
                trace_redact_content=True,
            )
            if pending_transient_followup_blocks
            else None
        )
        history_token_limit = max(
            1,
            token_limit - pending_transient_followup_tokens,
        )
        result = await _maybe_summarize(
            llm,
            messages,
            history_token_limit,
            api_total_tokens,
            False,
            session_id=session_id,
            turn_id=turn_id,
            title=title,
            api_prompt_tokens=api_prompt_tokens,
            tools=tools,
            summary_llm=summary_llm,
            allow_llm_summary=summary_failure_cooldown_steps == 0,
            session_log=session_log,
            session_turn=session_turn,
            session_step=step + 1,
        )
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
        new_msgs, _skip_next_token_check, est_before = result
        if new_msgs is not None:
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
            yield SummarizationEvent(
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
        if result.blocked:
            msg = (
                "Context remains above the safe input limit after bounded compaction "
                f"({result.estimated_after} estimated tokens; limit {token_limit}). "
                "Start a new session or reduce active instructions/tool output before retrying."
            )
            if hook_mgr.hooks:
                await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
            yield ErrorEvent(message=msg, is_fatal=True)
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
        if hook_mgr.hooks:
            await hook_mgr.fire_step_start(step=step + 1, max_steps=max_steps)

        # ── LLM call (streaming) ──────────────────────────────
        tool_list = list(tools.values())
        offered_mcp_generations: dict[str, int] = {}
        if tool_exposure_manager is not None:
            exposure = tool_exposure_manager.prepare_tools(tool_list)
            tool_list = exposure.tools
            offered_mcp_generations = exposure.mcp_generations
        # Apply intent filtering after catalog exposure so an activated MCP
        # browser tool cannot bypass the same visibility policy as a stable
        # core/fallback tool.
        tool_list = [
            tool
            for tool in tool_list
            if browser_intent_policy.is_tool_visible(tool.name)
        ]
        offered_tools_by_name = {tool.name: tool for tool in tool_list}
        offered_tools_by_call_name = build_tool_name_index(tool_list)
        offered_tool_names = frozenset(offered_tools_by_name)
        request_context_messages = [
            message
            for message in (auto_memory_context_message,)
            if message is not None
        ]
        request_messages = (
            [*messages, *request_context_messages]
            if request_context_messages
            else messages
        )
        provider_request_messages = (
            [*request_messages, transient_message]
            if transient_message is not None
            else request_messages
        )

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
                messages[1:],
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

        def _tool_target_identity(tool_name: str) -> tuple[str | None, str | None]:
            tool = offered_tools_by_name.get(tool_name)
            tool_id = getattr(tool, "mcp_tool_id", None)
            server_name = getattr(tool, "server_name", None)
            return (
                tool_id if isinstance(tool_id, str) and tool_id else None,
                server_name if isinstance(server_name, str) and server_name else None,
            )

        def _tool_offer_error(tool_name: str) -> str | None:
            if tool_exposure_manager is None:
                return None
            if tool_name not in offered_tool_names:
                return (
                    f"Tool '{tool_name}' was not offered in this model step. "
                    "Use tool_search and call an activated result on the next step."
                )
            return tool_exposure_manager.validate_call(
                tool_name,
                offered_mcp_generations.get(tool_name),
                offered_tools_by_name.get(tool_name),
            )

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
            request_only_input_tokens = (
                pending_transient_followup_tokens
                if transient_message is not None
                else 0
            )
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
                msg = (
                    "LLM stream aborted after repetitive output was detected. "
                    "Retry the turn; the repeated output was not saved to conversation history."
                )
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                    await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
                yield ErrorEvent(message=msg, is_fatal=True)
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
                msg = (
                    f"LLM stream interrupted: {exc.last_exception!s} "
                    f"(preserved partial content: {len(partial_text)} chars text, "
                    f"{len(partial_thinking)} chars thinking)"
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
                messages[1:],
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
        if cancelled():
            _cleanup_incomplete_messages(messages)
            if hook_mgr.hooks:
                await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            return

        # Resolve backwards-compatible aliases only against tools offered in
        # this model step. Keep the persisted assistant turn unchanged, while
        # all execution policy sees the canonical tool name.
        execution_tool_calls = []
        for tool_call in response.tool_calls:
            execution_call = tool_call.model_copy(deep=True)
            resolved_tool = offered_tools_by_call_name.get(
                execution_call.function.name
            )
            if resolved_tool is not None:
                execution_call.function.name = resolved_tool.name
            execution_tool_calls.append(execution_call)

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

        # Deduplicate identical calls emitted in the same assistant response.
        # Some providers occasionally repeat a mutation call byte-for-byte;
        # executing both can corrupt state or turn the second call into a
        # misleading conflict. Keep every original tool_call in model history,
        # but execute only the first occurrence and synthesize hidden replies
        # for its duplicates below so the protocol remains valid.
        unique_tool_calls = []
        duplicate_tool_calls = []
        first_tool_call_by_signature: dict[tuple[str, str], Any] = {}
        duplicate_source_by_id: dict[str, str] = {}
        for tc in execution_tool_calls:
            signature = (
                tc.function.name,
                json.dumps(
                    tc.function.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
            )
            first = first_tool_call_by_signature.get(signature)
            if first is None:
                first_tool_call_by_signature[signature] = tc
                unique_tool_calls.append(tc)
            else:
                duplicate_source_by_id[tc.id] = first.id
                duplicate_tool_calls.append(tc)

        if duplicate_tool_calls:
            _log.info(
                "tool/dedupe skipped=%d unique=%d",
                len(duplicate_tool_calls),
                len(unique_tool_calls),
            )

        # Preserve model order when a step mixes search with stateful tools.
        # Search-only batches may opt into bounded parallel execution without
        # declaring every MCP tool parallel-safe.
        parallel_web_search_batch = (
            web_search_concurrency > 1
            and bool(unique_tool_calls)
            and all(
                tc.function.name == WEB_SEARCH_TOOL_NAME
                for tc in unique_tool_calls
            )
        )

        # A successful interactive tool is a turn boundary. Preserve the
        # model's exact call order for the whole step so only calls before that
        # boundary can run; later siblings receive deterministic skip results.
        step_has_turn_ending_tool = any(
            getattr(
                offered_tools_by_name.get(tc.function.name),
                "ends_turn_on_success",
                False,
            )
            for tc in unique_tool_calls
        )

        # Split unique calls into regular (sequential) and parallel_safe groups.
        regular_calls = []
        parallel_calls = []
        for tc in unique_tool_calls:
            fn_name = tc.function.name
            if step_has_turn_ending_tool:
                regular_calls.append(tc)
            elif _model_history_placeholder_argument(fn_name, tc.function.arguments):
                # Placeholder repair is stateful and must be handled by the
                # sequential branch even if a future mutation tool is marked
                # parallel-safe.
                regular_calls.append(tc)
            elif fn_name in offered_tools_by_name and (
                (
                    fn_name == WEB_SEARCH_TOOL_NAME
                    and parallel_web_search_batch
                )
                or getattr(offered_tools_by_name[fn_name], "parallel_safe", False)
            ):
                parallel_calls.append(tc)
            else:
                regular_calls.append(tc)

        step_contains_plan_write = any(
            tc.function.name == "plan_write" for tc in [*regular_calls, *parallel_calls]
        )
        organic_plan_approval_gate_enabled = (
            pause_after_plan_write
            and not plan_approval_approved
            and not plan_approval_gate_enabled
            and has_plan_tool
            and step_contains_plan_write
        )
        plan_approval_gate_active = (
            plan_approval_gate_enabled or organic_plan_approval_gate_enabled
        )

        # Track whether this step produced any useful tool result, for the
        # no-progress circuit breaker. Set True in either execution branch.
        step_made_progress = False
        step_tool_success_by_id: dict[str, bool] = {}
        completed_turn_ending_tool: str | None = None

        def _record_search_files_result(tool_name: str, result: ToolResult) -> None:
            if tool_name != SEARCH_FILES_TOOL_NAME:
                return
            if search_files_result_is_empty(result):
                tool_budget_state.search_files_consecutive_empty_results += 1
            elif result.success:
                tool_budget_state.search_files_consecutive_empty_results = 0

        def _reserve_web_search_call(arguments: dict[str, Any]) -> tuple[bool, str | None]:
            nonlocal web_search_step_seen
            nonlocal web_search_step_executed
            nonlocal web_search_step_deferred
            nonlocal web_search_step_duplicate_queries

            web_search_step_seen = True
            query_key = _normalize_web_search_query(arguments)
            duplicate_query = next(
                (
                    seen_query
                    for seen_query in web_search_seen_queries
                    if _web_search_queries_are_near_duplicates(
                        query_key,
                        seen_query,
                    )
                ),
                None,
            )
            if duplicate_query is not None:
                web_search_step_duplicate_queries += 1
                return (
                    False,
                    "Duplicate web_search query skipped by runtime batching "
                    "(exact or near-duplicate). "
                    f"It substantially overlaps {duplicate_query!r}. Use the evidence already "
                    "returned and search a genuinely different evidence gap.",
                )
            if web_search_step_executed >= web_search_batch_size:
                web_search_step_deferred += 1
                return (
                    False,
                    f"web_search deferred by runtime batching (batch size {web_search_batch_size}). "
                    "Review the current batch results and re-issue only still-missing, non-duplicate queries.",
                )

            allowed_by_budget, budget_error = tool_budget_state.reserve(WEB_SEARCH_TOOL_NAME)
            if not allowed_by_budget:
                return False, budget_error
            if query_key:
                web_search_seen_queries.add(query_key)
            web_search_step_executed += 1
            return True, None

        tool_engine = ToolEngine(
            tools=offered_tools_by_name,
            is_cancelled=cancelled,
            activity_interval_seconds=(
                _runtime_defaults.tool_activity_interval_seconds
            ),
            event_poll_interval_seconds=(
                _runtime_defaults.tool_event_poll_interval_seconds
            ),
            cancel_grace_seconds=(
                _runtime_defaults.parallel_tool_cancel_grace_seconds
            ),
            max_parallel_tools=max_parallel_tools,
            batch_timeout_seconds=parallel_tool_timeout_seconds,
            web_search_concurrency=web_search_concurrency,
            web_search_tool_name=WEB_SEARCH_TOOL_NAME,
            passthrough_exceptions=(SessionLogDurabilityError,),
        )

        # 1. Sequential execution for regular tools (preserves ordering)
        for tc in regular_calls:
            tc_id = tc.id
            fn_name = tc.function.name
            fn_args = tc.function.arguments
            (
                browser_snapshot_target,
                browser_snapshot_path_error,
            ) = _prepare_browser_snapshot_output(
                fn_name,
                fn_args,
                workspace_dir,
                artifact_root_dir,
            )
            (
                browser_screenshot_target,
                browser_screenshot_path_error,
            ) = _prepare_browser_screenshot_output(
                fn_name,
                fn_args,
                workspace_dir,
                artifact_root_dir,
            )
            browser_snapshot_path_error = (
                browser_snapshot_path_error or browser_screenshot_path_error
            )
            placeholder_argument = _model_history_placeholder_argument(fn_name, fn_args)
            can_auto_repair_placeholder = (
                placeholder_argument is not None
                and model_history_placeholder_repairs
                < _MODEL_HISTORY_PLACEHOLDER_REPAIR_LIMIT
            )
            browser_intent_error = browser_intent_policy.tool_call_error(
                fn_name,
                fn_args,
            )
            placeholder_recovery_error = _model_history_placeholder_recovery_error(
                pending_model_history_recovery,
                fn_name,
                fn_args,
                workspace_dir,
                artifact_root_dir,
            )

            offered_error = _tool_offer_error(fn_name)

            if completed_turn_ending_tool is not None:
                allowed_to_execute = False
                internal_skip_error = (
                    f"Skipped because interactive tool '{completed_turn_ending_tool}' "
                    "already completed in this model step. Resume after the user responds."
                )
            elif offered_error is not None:
                allowed_to_execute = False
                internal_skip_error = offered_error
            elif browser_intent_error is not None:
                allowed_to_execute = False
                internal_skip_error = browser_intent_error
            elif placeholder_argument is not None:
                allowed_to_execute = False
                internal_skip_error = (
                    f"{_MODEL_HISTORY_PLACEHOLDER_TOOL_ERROR} "
                    f"Rejected argument: {fn_name}.{placeholder_argument}."
                )
                if can_auto_repair_placeholder:
                    model_history_placeholder_auto_repair_requested = True
                if pending_model_history_recovery is None:
                    pending_model_history_recovery = _ModelHistoryPlaceholderRecovery(
                        tool_name=fn_name,
                        argument_name=placeholder_argument,
                        target=_model_history_recovery_target(
                            fn_name,
                            fn_args,
                            workspace_dir,
                            artifact_root_dir,
                        ),
                        action=(
                            str(fn_args.get("action"))
                            if fn_name == "staged_file_write"
                            else None
                        ),
                    )
            elif placeholder_recovery_error is not None:
                allowed_to_execute = False
                internal_skip_error = placeholder_recovery_error
            elif browser_snapshot_path_error is not None:
                allowed_to_execute = False
                internal_skip_error = browser_snapshot_path_error
            elif plan_approval_gate_active and fn_name != "plan_write":
                allowed_to_execute = False
                internal_skip_error = _PLAN_APPROVAL_SKIP_MESSAGE
            elif fn_name == WEB_SEARCH_TOOL_NAME:
                allowed_to_execute, internal_skip_error = _reserve_web_search_call(fn_args)
            else:
                allowed_to_execute, internal_skip_error = tool_budget_state.reserve(fn_name)
            tool_user_visible = (
                placeholder_argument is not None and not can_auto_repair_placeholder
            ) or allowed_to_execute
            if tool_user_visible and fn_name not in FINAL_SUMMARY_EXCLUDED_TOOLS:
                visible_tool_call_total += 1

            tool_id, server_name = _tool_target_identity(fn_name)
            yield ToolCallStart(
                tool_call_id=tc_id,
                tool_name=fn_name,
                arguments=fn_args,
                user_visible=tool_user_visible,
                tool_id=tool_id,
                server_name=server_name,
            )

            # Hook: tool start (interceptor — may modify arguments)
            if hook_mgr.hooks and tool_user_visible and allowed_to_execute:
                fn_args = await hook_mgr.fire_tool_start(
                    tool_call_id=tc_id, tool_name=fn_name, arguments=fn_args,
                )
            if (
                session_log is not None
                and session_turn is not None
                and allowed_to_execute
                and fn_name in offered_tools_by_name
            ):
                session_log.append(
                    "tool/call",
                    {
                        "turn": session_turn,
                        "step": step + 1,
                        "callId": tc_id,
                        "name": fn_name,
                        "arguments": fn_args,
                    },
                )
                session_log.flush()
            tool_started_at = perf_counter()
            emit_session_trace(
                "tool.request",
                turn_id=turn_id,
                step=step + 1,
                tool_call_id=tc_id,
                data={
                    "tool_name": fn_name,
                    "tool_id": tool_id,
                    "server_name": server_name,
                    "arguments": fn_args,
                    "allowed_to_execute": allowed_to_execute,
                    "user_visible": tool_user_visible,
                },
            )

            # Snapshot workspace before tool execution for diff-based artifact detection
            pre_files: dict[Path, tuple[int, int]] = {}
            if artifact_detection_enabled and allowed_to_execute and tool_user_visible and workspace_dir:
                pre_files = _snapshot_workspace_signatures(
                    workspace_dir,
                    artifact_root_dir,
                )

            if not allowed_to_execute:
                result = ToolResult(success=False, content="", error=internal_skip_error or "")
            elif fn_name not in offered_tools_by_name:
                result = ToolResult(success=False, content="", error=f"Unknown tool: {fn_name}")
            elif (
                current_offer_error := _tool_offer_error(fn_name)
            ):
                result = ToolResult(success=False, content="", error=current_offer_error)
            else:
                completion: ToolInvocationCompleted | None = None
                async with aclosing(
                    tool_engine.invoke_serial(
                        ToolInvocationRequest(
                            call_id=tc_id,
                            tool_name=fn_name,
                            arguments=fn_args,
                        )
                    )
                ) as engine_events:
                    async for engine_record in engine_events:
                        if isinstance(engine_record, ToolEngineProgress):
                            yield engine_record.event
                        elif isinstance(engine_record, ToolEngineActivity):
                            yield LLMActivityEvent(
                                step=step + 1,
                                payload={
                                    "protocol": "agent_activity_v1",
                                    "phase": "tool_running",
                                    "tool_name": engine_record.tool_name,
                                },
                            )
                        elif isinstance(engine_record, ToolInvocationCompleted):
                            completion = engine_record
                if completion is None:
                    result = ToolResult(
                        success=False,
                        content="",
                        error="Tool execution interrupted — no result returned.",
                    )
                else:
                    result = completion.result

            if plan_approval_gate_active and fn_name == "plan_write" and result.success:
                result = result.model_copy(
                    update={
                        "raw_output": _attach_plan_approval_payload(
                            result.raw_output,
                            request_id=plan_approval_request_id,
                        )
                    }
                )
                plan_approval_gate_completed = True

            policy_decision: dict[str, Any] | None = None
            # Log tool result
            if logger:
                logger.log_tool_result(
                    tool_name=fn_name,
                    arguments=fn_args,
                    result_success=result.success,
                    result_content=result.content if result.success else None,
                    result_error=result.error if not result.success else None,
                    raw_output=_trace_safe_tool_raw_output(result.raw_output),
                    tool_id=tool_id,
                    server_name=server_name,
                )

            # ── Permission negotiation + retry ──────────────
            if not result.success and result.permission_request and permission_negotiator:
                def log_permission_retry(retry_result: ToolResult) -> None:
                    if logger:
                        logger.log_tool_result(
                            tool_name=fn_name,
                            arguments=fn_args,
                            result_success=retry_result.success,
                            result_content=(
                                retry_result.content if retry_result.success else None
                            ),
                            result_error=(
                                retry_result.error if not retry_result.success else None
                            ),
                            raw_output=_trace_safe_tool_raw_output(retry_result.raw_output),
                            tool_id=tool_id,
                            server_name=server_name,
                        )

                result, policy_decision = await _negotiate_tool_permission_chain(
                    result=result,
                    permission_negotiator=permission_negotiator,
                    tool_name=fn_name,
                    tool=offered_tools_by_name.get(fn_name),
                    arguments=fn_args,
                    retry_offer_error=lambda: (
                        f"Unknown tool: {fn_name}"
                        if fn_name not in offered_tools_by_name
                        else _tool_offer_error(fn_name)
                    ),
                    on_retry=log_permission_retry,
                )
            elif not result.success and result.permission_request:
                policy_decision = _policy_decision_payload(
                    tool_name=fn_name,
                    permission_request=result.permission_request,
                    decision="requested",
                )

            result = _persist_browser_snapshot_output(
                result,
                browser_snapshot_target,
            )
            result = _persist_browser_screenshot_output(
                result,
                browser_screenshot_target,
            )
            result = _activate_skill_result(fn_name, fn_args, result)
            result, transient_blocks, transient_estimate = (
                _validate_transient_followup_result(
                    result=result,
                    tool=offered_tools_by_name.get(fn_name),
                    llm=llm,
                    token_limit=token_limit,
                    pending_token_estimate=pending_transient_followup_tokens,
                )
            )
            if transient_blocks:
                pending_transient_followup_blocks.extend(transient_blocks)
                pending_transient_followup_tokens += transient_estimate
            pending_model_history_recovery = (
                _record_model_history_placeholder_recovery_result(
                    pending_model_history_recovery,
                    fn_name,
                    fn_args,
                    result,
                )
            )
            tool_budget_state.record_delegated_tool_budget(fn_name, result.raw_output)
            _record_search_files_result(fn_name, result)
            step_tool_success_by_id[tc_id] = result.success
            if result.success and fn_name == "plan_write":
                plan_write_succeeded = True
            if (
                allowed_to_execute
                and result.success
                and getattr(
                    offered_tools_by_name.get(fn_name),
                    "ends_turn_on_success",
                    False,
                )
            ):
                completed_turn_ending_tool = fn_name

            # Progress signal for the no-progress breaker: a successful tool
            # call with non-empty content counts as making progress.
            if (
                result.success
                and (result.content or "").strip()
                and not search_files_result_is_empty(result)
            ):
                step_made_progress = True

            # Hook: tool result (interceptor — may modify content/error)
            tc_content = result.content
            tc_error = result.error
            if hook_mgr.hooks and tool_user_visible:
                tc_content, tc_error = await hook_mgr.fire_tool_result(
                    tool_call_id=tc_id, tool_name=fn_name,
                    success=result.success, content=tc_content, error=tc_error,
                )

            outcome = process_tool_result(
                ToolResultPipelineInput(
                    messages=messages,
                    tool_call_id=tc_id,
                    tool_name=fn_name,
                    arguments=fn_args,
                    result=result,
                    visible_content=tc_content,
                    visible_error=tc_error,
                    result_storage=result_storage,
                    tool=tools.get(fn_name),
                    session_id=session_id,
                    resource_ledger=resource_ledger,
                    web_search_seen_result_keys=web_search_seen_result_keys,
                    framework_error_counts=model_history_framework_error_counts,
                    user_visible=tool_user_visible,
                    emit_legacy_permission_request=not permission_negotiator,
                    policy_decision=policy_decision,
                    tool_id=tool_id,
                    server_name=server_name,
                    turn_id=turn_id,
                    step=step + 1,
                    started_at=tool_started_at,
                )
            )
            web_search_step_new_results += outcome.web_search_new_results
            web_search_step_duplicate_results += outcome.web_search_duplicate_results
            web_search_unique_results += outcome.web_search_new_results
            web_search_duplicate_results += outcome.web_search_duplicate_results
            if outcome.web_search_inspected:
                web_search_step_structured_results += 1
            web_search_step_labels.extend(outcome.web_search_labels[:3])
            tc_content = outcome.visible_content
            tc_error = outcome.visible_error
            for event in outcome.events:
                yield event

            # Detect and yield structured artifacts (images, files) from tool output
            if artifact_detection_enabled and result.success and workspace_dir:
                post_files = _snapshot_workspace_signatures(
                    workspace_dir,
                    artifact_root_dir,
                )
                for artifact in _detect_tool_artifacts(
                    tc_id,
                    fn_name,
                    tc_content,
                    result.raw_output,
                    pre_files,
                    post_files,
                    workspace_dir,
                    artifact_root_dir,
                ):
                    yield artifact

            # Cancellation check after each tool
            if cancelled():
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                return

        # 2. Parallel execution for parallel_safe tools (e.g. generate_image, sub_agent)
        if parallel_calls:
            # Snapshot the workspace BEFORE any parallel tool runs. Per-tool
            # snapshots are impossible under concurrency, so the diff layer uses
            # one pre/post pair for the whole batch (see after the result loop).
            par_pre_files: dict[Path, tuple[int, int]] = {}
            if artifact_detection_enabled and workspace_dir:
                par_pre_files = _snapshot_workspace_signatures(
                    workspace_dir,
                    artifact_root_dir,
                )
            # Emit all ToolCallStart events and apply hook interceptors
            par_args_map: dict[str, dict[str, Any]] = {}  # tc.id → (possibly modified) args
            par_budget_errors: dict[str, str] = {}
            par_user_visible: dict[str, bool] = {}
            par_browser_snapshot_targets: dict[str, Path | None] = {}
            par_browser_screenshot_targets: dict[str, Path | None] = {}
            par_started_at: dict[str, float] = {}
            durable_parallel_calls = False
            for tc in parallel_calls:
                par_fn_args = tc.function.arguments
                (
                    browser_snapshot_target,
                    browser_snapshot_path_error,
                ) = _prepare_browser_snapshot_output(
                    tc.function.name,
                    par_fn_args,
                    workspace_dir,
                    artifact_root_dir,
                )
                par_browser_snapshot_targets[tc.id] = browser_snapshot_target
                (
                    browser_screenshot_target,
                    browser_screenshot_path_error,
                ) = _prepare_browser_screenshot_output(
                    tc.function.name,
                    par_fn_args,
                    workspace_dir,
                    artifact_root_dir,
                )
                par_browser_screenshot_targets[tc.id] = browser_screenshot_target
                browser_snapshot_path_error = (
                    browser_snapshot_path_error or browser_screenshot_path_error
                )
                browser_intent_error = browser_intent_policy.tool_call_error(
                    tc.function.name,
                    par_fn_args,
                )
                offered_error = _tool_offer_error(tc.function.name)
                placeholder_recovery_error = _model_history_placeholder_recovery_error(
                    pending_model_history_recovery,
                    tc.function.name,
                    par_fn_args,
                    workspace_dir,
                    artifact_root_dir,
                )
                if offered_error is not None:
                    allowed_to_execute = False
                    internal_skip_error = offered_error
                elif browser_intent_error is not None:
                    allowed_to_execute = False
                    internal_skip_error = browser_intent_error
                elif placeholder_recovery_error is not None:
                    allowed_to_execute = False
                    internal_skip_error = placeholder_recovery_error
                elif browser_snapshot_path_error is not None:
                    allowed_to_execute = False
                    internal_skip_error = browser_snapshot_path_error
                elif plan_approval_gate_active and tc.function.name != "plan_write":
                    allowed_to_execute = False
                    internal_skip_error = _PLAN_APPROVAL_SKIP_MESSAGE
                elif tc.function.name == WEB_SEARCH_TOOL_NAME:
                    allowed_to_execute, internal_skip_error = _reserve_web_search_call(par_fn_args)
                else:
                    allowed_to_execute, internal_skip_error = tool_budget_state.reserve(tc.function.name)
                par_user_visible[tc.id] = allowed_to_execute
                if allowed_to_execute and tc.function.name not in FINAL_SUMMARY_EXCLUDED_TOOLS:
                    visible_tool_call_total += 1
                tool_id, server_name = _tool_target_identity(tc.function.name)
                yield ToolCallStart(
                    tool_call_id=tc.id,
                    tool_name=tc.function.name,
                    arguments=par_fn_args,
                    user_visible=allowed_to_execute,
                    tool_id=tool_id,
                    server_name=server_name,
                )
                if hook_mgr.hooks and allowed_to_execute:
                    par_fn_args = await hook_mgr.fire_tool_start(
                        tool_call_id=tc.id, tool_name=tc.function.name, arguments=par_fn_args,
                    )
                par_args_map[tc.id] = par_fn_args
                par_started_at[tc.id] = perf_counter()
                if (
                    session_log is not None
                    and session_turn is not None
                    and allowed_to_execute
                    and tc.function.name in offered_tools_by_name
                ):
                    session_log.append(
                        "tool/call",
                        {
                            "turn": session_turn,
                            "step": step + 1,
                            "callId": tc.id,
                            "name": tc.function.name,
                            "arguments": par_fn_args,
                        },
                    )
                    durable_parallel_calls = True
                emit_session_trace(
                    "tool.request",
                    turn_id=turn_id,
                    step=step + 1,
                    tool_call_id=tc.id,
                    data={
                        "tool_name": tc.function.name,
                        "tool_id": tool_id,
                        "server_name": server_name,
                        "arguments": par_fn_args,
                        "allowed_to_execute": allowed_to_execute,
                        "user_visible": allowed_to_execute,
                        "parallel": True,
                    },
                )
                if not allowed_to_execute:
                    par_budget_errors[tc.id] = internal_skip_error or ""

            if durable_parallel_calls and session_log is not None:
                session_log.flush()

            # Offer, policy, and budget decisions stay in core. The engine only
            # schedules invocations or relays the immediate result prepared here.
            parallel_requests: list[ToolInvocationRequest] = []
            for tc in parallel_calls:
                fn_name = tc.function.name
                immediate_result: ToolResult | None = None
                if tc.id in par_budget_errors:
                    immediate_result = ToolResult(
                        success=False,
                        content="",
                        error=par_budget_errors[tc.id],
                    )
                elif fn_name not in offered_tools_by_name:
                    immediate_result = ToolResult(
                        success=False,
                        content="",
                        error=f"Unknown tool: {fn_name}",
                    )
                elif current_offer_error := _tool_offer_error(fn_name):
                    immediate_result = ToolResult(
                        success=False,
                        content="",
                        error=current_offer_error,
                    )
                parallel_requests.append(
                    ToolInvocationRequest(
                        call_id=tc.id,
                        tool_name=fn_name,
                        arguments=par_args_map[tc.id],
                        immediate_result=immediate_result,
                    )
                )

            batch_completion: ToolBatchCompleted | None = None
            async for engine_record in tool_engine.invoke_parallel(
                parallel_requests
            ):
                if isinstance(engine_record, ToolEngineProgress):
                    yield engine_record.event
                elif isinstance(engine_record, ToolEngineActivity):
                    yield LLMActivityEvent(
                        step=step + 1,
                        payload={
                            "protocol": "agent_activity_v1",
                            "phase": "tool_running",
                            "tool_name": engine_record.tool_name,
                        },
                    )
                elif isinstance(engine_record, ToolBatchCompleted):
                    batch_completion = engine_record

            if batch_completion is None:
                gathered = [
                    (
                        tc,
                        ToolResult(
                            success=False,
                            content="",
                            error=(
                                "Tool execution interrupted — no result returned."
                            ),
                        ),
                    )
                    for tc in parallel_calls
                ]
            else:
                gathered = [
                    (parallel_calls[outcome.index], outcome.result)
                    for outcome in batch_completion.outcomes
                ]

            # Accumulates absolute paths surfaced by the per-result regex layer
            # (and artifact raw_outputs), so the single post-batch diff pass
            # below doesn't re-emit them.
            par_already_emitted: set[str] = set()

            for tc, result in gathered:
                tc_id = tc.id
                fn_name = tc.function.name
                fn_args = par_args_map[tc_id]
                tool_id, server_name = _tool_target_identity(fn_name)
                tool_user_visible = par_user_visible.get(tc_id, True)
                policy_decision: dict[str, Any] | None = None

                if plan_approval_gate_active and fn_name == "plan_write" and result.success:
                    result = result.model_copy(
                        update={
                            "raw_output": _attach_plan_approval_payload(
                                result.raw_output,
                                request_id=plan_approval_request_id,
                            )
                        }
                    )
                    plan_approval_gate_completed = True

                if logger:
                    logger.log_tool_result(
                        tool_name=fn_name,
                        arguments=fn_args,
                        result_success=result.success,
                        result_content=result.content if result.success else None,
                        result_error=result.error if not result.success else None,
                        raw_output=_trace_safe_tool_raw_output(result.raw_output),
                        tool_id=tool_id,
                        server_name=server_name,
                    )

                # ── Permission negotiation + retry ──────────────
                if not result.success and result.permission_request and permission_negotiator:
                    def log_parallel_permission_retry(retry_result: ToolResult) -> None:
                        if logger:
                            logger.log_tool_result(
                                tool_name=fn_name,
                                arguments=fn_args,
                                result_success=retry_result.success,
                                result_content=(
                                    retry_result.content if retry_result.success else None
                                ),
                                result_error=(
                                    retry_result.error if not retry_result.success else None
                                ),
                                raw_output=_trace_safe_tool_raw_output(retry_result.raw_output),
                                tool_id=tool_id,
                                server_name=server_name,
                            )

                    result, policy_decision = await _negotiate_tool_permission_chain(
                        result=result,
                        permission_negotiator=permission_negotiator,
                        tool_name=fn_name,
                        tool=offered_tools_by_name.get(fn_name),
                        arguments=fn_args,
                        retry_offer_error=lambda: (
                            f"Unknown tool: {fn_name}"
                            if fn_name not in offered_tools_by_name
                            else _tool_offer_error(fn_name)
                        ),
                        on_retry=log_parallel_permission_retry,
                    )
                elif not result.success and result.permission_request:
                    policy_decision = _policy_decision_payload(
                        tool_name=fn_name,
                        permission_request=result.permission_request,
                        decision="requested",
                    )

                result = _persist_browser_snapshot_output(
                    result,
                    par_browser_snapshot_targets.get(tc_id),
                )
                result = _persist_browser_screenshot_output(
                    result,
                    par_browser_screenshot_targets.get(tc_id),
                )
                result, transient_blocks, transient_estimate = (
                    _validate_transient_followup_result(
                        result=result,
                        tool=offered_tools_by_name.get(fn_name),
                        llm=llm,
                        token_limit=token_limit,
                        pending_token_estimate=pending_transient_followup_tokens,
                    )
                )
                if transient_blocks:
                    pending_transient_followup_blocks.extend(transient_blocks)
                    pending_transient_followup_tokens += transient_estimate
                tool_budget_state.record_delegated_tool_budget(fn_name, result.raw_output)
                _record_search_files_result(fn_name, result)
                step_tool_success_by_id[tc_id] = result.success
                if result.success and fn_name == "plan_write":
                    plan_write_succeeded = True

                # Progress signal for the no-progress breaker.
                if (
                    result.success
                    and (result.content or "").strip()
                    and not search_files_result_is_empty(result)
                ):
                    step_made_progress = True

                # Hook: tool result (interceptor)
                par_content = result.content
                par_error = result.error
                if hook_mgr.hooks and tool_user_visible:
                    par_content, par_error = await hook_mgr.fire_tool_result(
                        tool_call_id=tc_id, tool_name=fn_name,
                        success=result.success, content=par_content, error=par_error,
                    )

                outcome = process_tool_result(
                    ToolResultPipelineInput(
                        messages=messages,
                        tool_call_id=tc_id,
                        tool_name=fn_name,
                        arguments=par_fn_args,
                        result=result,
                        visible_content=par_content,
                        visible_error=par_error,
                        result_storage=result_storage,
                        tool=tools.get(fn_name),
                        session_id=session_id,
                        resource_ledger=resource_ledger,
                        web_search_seen_result_keys=web_search_seen_result_keys,
                        framework_error_counts=model_history_framework_error_counts,
                        user_visible=tool_user_visible,
                        emit_legacy_permission_request=not permission_negotiator,
                        policy_decision=policy_decision,
                        tool_id=tool_id,
                        server_name=server_name,
                        turn_id=turn_id,
                        step=step + 1,
                        started_at=par_started_at[tc_id],
                        parallel=True,
                    )
                )
                web_search_step_new_results += outcome.web_search_new_results
                web_search_step_duplicate_results += (
                    outcome.web_search_duplicate_results
                )
                web_search_unique_results += outcome.web_search_new_results
                web_search_duplicate_results += outcome.web_search_duplicate_results
                if outcome.web_search_inspected:
                    web_search_step_structured_results += 1
                web_search_step_labels.extend(outcome.web_search_labels[:3])
                par_content = outcome.visible_content
                par_error = outcome.visible_error
                for event in outcome.events:
                    yield event

                # Artifact detection — layer 1 (regex) per result. The changed-file
                # layer runs once after the loop (single batch snapshot).
                if artifact_detection_enabled and result.success and tool_user_visible and workspace_dir:
                    regex_artifacts, regex_already = _detect_regex_artifacts(
                        tc_id, fn_name, par_content, result.raw_output,
                        workspace_dir, artifact_root_dir,
                    )
                    for artifact in regex_artifacts:
                        yield artifact
                    par_already_emitted |= regex_already

            # Artifact detection — layer 2 (diff), once for the whole batch.
            # Concurrency rules out per-tool snapshots, so new files are
            # attributed to the first parallel call's id.
            if artifact_detection_enabled and workspace_dir and parallel_calls:
                par_post_files = _snapshot_workspace_signatures(
                    workspace_dir,
                    artifact_root_dir,
                )
                for artifact in _detect_changed_files(
                    parallel_calls[0].id,
                    par_pre_files,
                    par_post_files,
                    par_already_emitted,
                    workspace_dir,
                ):
                    yield artifact

            # Cancellation check after all parallel results emitted — every
            # tool message is now appended, so the message list is in a
            # protocol-valid state for the next turn.
            if cancelled():
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                return

        # Reply to same-response duplicates without executing them. The source
        # result is already present in the immediately preceding tool messages,
        # so a compact reference is enough for the model and avoids duplicating
        # large tool output in history.
        for tc in duplicate_tool_calls:
            duplicate_started_at = perf_counter()
            source_id = duplicate_source_by_id[tc.id]
            source_succeeded = step_tool_success_by_id.get(source_id)
            if source_succeeded is True:
                duplicate_content = (
                    "Duplicate tool call skipped: identical call "
                    f"{source_id} already executed successfully in this response. "
                    "Reuse its result."
                )
                duplicate_error = None
            elif source_succeeded is False:
                duplicate_content = ""
                duplicate_error = (
                    "Duplicate tool call skipped: identical call "
                    f"{source_id} already failed in this response. "
                    "Fix that failure before retrying."
                )
            else:
                duplicate_content = ""
                duplicate_error = (
                    "Duplicate tool call skipped because its identical source "
                    f"call {source_id} did not produce a result."
                )

            tool_id, server_name = _tool_target_identity(tc.function.name)
            yield ToolCallStart(
                tool_call_id=tc.id,
                tool_name=tc.function.name,
                arguments=tc.function.arguments,
                user_visible=False,
                tool_id=tool_id,
                server_name=server_name,
            )
            emit_session_trace(
                "tool.request",
                turn_id=turn_id,
                step=step + 1,
                tool_call_id=tc.id,
                data={
                    "tool_name": tc.function.name,
                    "tool_id": tool_id,
                    "server_name": server_name,
                    "arguments": tc.function.arguments,
                    "allowed_to_execute": False,
                    "user_visible": False,
                    "duplicate_of": source_id,
                },
            )
            messages.append(
                Message(
                    role="tool",
                    content=duplicate_content or duplicate_error or "",
                    tool_call_id=tc.id,
                    name=tc.function.name,
                )
            )
            emit_session_trace(
                "tool.response",
                turn_id=turn_id,
                step=step + 1,
                tool_call_id=tc.id,
                data={
                    "tool_name": tc.function.name,
                    "tool_id": tool_id,
                    "server_name": server_name,
                    "success": source_succeeded is True,
                    "content": duplicate_content,
                    "error": duplicate_error,
                    "raw_output": None,
                    "model_content": duplicate_content or duplicate_error or "",
                    "policy_decision": None,
                    "user_visible": False,
                    "duplicate_of": source_id,
                    "duration_ms": max(
                        0,
                        int((perf_counter() - duplicate_started_at) * 1000),
                    ),
                },
            )
            yield ToolCallResult(
                tool_call_id=tc.id,
                tool_name=tc.function.name,
                success=source_succeeded is True,
                content=duplicate_content,
                error=duplicate_error,
                raw_output=None,
                user_visible=False,
                policy_decision=None,
                tool_id=tool_id,
                server_name=server_name,
            )

        if model_history_placeholder_auto_repair_requested:
            model_history_placeholder_repairs += 1
            messages.append(
                Message(
                    role="user",
                    content=format_injected_message(
                        _MODEL_HISTORY_PLACEHOLDER_REPAIR_GUIDANCE
                    ),
                )
            )
            yield InjectedMessageEvent(
                content=_MODEL_HISTORY_PLACEHOLDER_REPAIR_GUIDANCE,
                injection_id=None,
                user_visible=False,
            )

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

        if web_search_step_seen:
            if web_search_step_executed > 0 and web_search_step_structured_results > 0:
                if web_search_step_new_results == 0:
                    web_search_no_new_batches += 1
                else:
                    web_search_no_new_batches = 0

            total_web_search_calls = tool_budget_state.tool_call_counts.get(WEB_SEARCH_TOOL_NAME, 0)
            guidance_lines = [
                "Search batch controller update (internal; do not mention this controller to the user):",
                (
                    f"- Executed this batch: {web_search_step_executed}; "
                    f"total executed this turn: {total_web_search_calls}/{web_search_total_limit}; "
                    f"batch size: {web_search_batch_size}."
                ),
            ]
            if web_search_step_deferred:
                guidance_lines.append(f"- Deferred this batch: {web_search_step_deferred}.")
            if web_search_step_duplicate_queries:
                guidance_lines.append(f"- Duplicate queries skipped this batch: {web_search_step_duplicate_queries}.")
            if web_search_step_structured_results:
                guidance_lines.append(
                    f"- New structured results this batch: {web_search_step_new_results}; "
                    f"duplicate structured results this batch: {web_search_step_duplicate_results}; "
                    f"unique structured results this turn: {web_search_unique_results}; "
                    f"duplicates filtered this turn: {web_search_duplicate_results}."
                )
            if web_search_step_labels:
                examples = "; ".join(web_search_step_labels[:5])
                guidance_lines.append(f"- New result examples: {examples}.")
            if total_web_search_calls >= web_search_total_limit:
                guidance_lines.append(
                    "- The web_search total limit has been reached. Do not call web_search again; "
                    "synthesize the final answer from gathered evidence and briefly mark gaps."
                )
            elif web_search_no_new_batches >= 2:
                guidance_lines.append(
                    "- Two consecutive structured search batches added no new results. Stop searching unless "
                    "a critical first-party source is still missing."
                )
            else:
                guidance_lines.append(
                    f"- Before searching again, inspect the deduped evidence. If gaps remain, issue at most "
                    f"{web_search_batch_size} new, specific, non-duplicate web_search queries."
                )
            guidance_text = "\n".join(guidance_lines)
            messages.append(Message(role="user", content=format_injected_message(guidance_text)))
            yield InjectedMessageEvent(content=guidance_text, injection_id=None, user_visible=False)

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
