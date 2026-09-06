"""Compatibility facade for the stable agent-loop kernel.

The implementation lives in :mod:`box_agent.kernel.loop`.  This module keeps
the historical import surface and exact run-loop signature while reading the
legacy timing constants when iteration begins.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Final

from .artifacts import (
    OUTPUT_SUBDIR,
    artifact_scan_root as _artifact_scan_root,
    avoid_collision,
    ensure_output_dir,
    make_artifact as _make_artifact,
    safe_output_name,
)
from .cache_fingerprint import build_cache_fingerprint
from .config import AgentConfig, ToolLimitsConfig
from .composition import run_agent_loop_with_default_services
from .context_resources import (
    ContextResourceLedger,
    ResourceDescriptor,
    build_resource_receipt,
)
from .evidence import (
    extract_http_urls as _http_urls,
    normalize_search_url as _normalize_search_url,
)
from .events import (
    AgentEvent,
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LLMActivityEvent,
    LLMOutputEvent,
    LogFileEvent,
    MemoryPromotionCandidate,
    MemoryProposalEvent,
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
from .hooks import HookManager
from .kernel.context_engine import (
    TRANSIENT_FOLLOWUP_CONTEXT_RATIO,
    TRANSIENT_IMAGE_DEFAULT_TOKENS,
    TRANSIENT_IMAGE_MAX_TOKENS,
    TRANSIENT_IMAGE_PIXEL_TOKEN_DIVISOR,
    CompactionOutcome,
    _LEGACY_SUMMARY_MARKER,
    _LOCAL_FALLBACK_CHAR_LIMIT,
    _RECENT_MESSAGE_CHAR_LIMIT,
    _RECENT_MESSAGE_LIMIT,
    _RUNTIME_STATE_CHAR_LIMIT,
    _RUNTIME_STATE_MARKER,
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
from .kernel.loop import (
    _SignedWebImageUrlStreamRewriter,
    _restore_signed_web_image_urls,
    _signed_web_image_url_map,
    ActiveSkillActivator,
    CancelChecker,
    FINAL_SUMMARY_TOOL_CALL_THRESHOLD,
    LLM_ACTIVITY_INTERVAL_SECONDS,
    LLM_PROVIDER_STALE_SECONDS,
    MAX_PROVIDER_STALE_RECOVERIES,
    PARALLEL_TOOL_CANCEL_GRACE_SECONDS,
    TOOL_ACTIVITY_INTERVAL_SECONDS,
    TOOL_EVENT_POLL_INTERVAL_SECONDS,
    _DEFAULT_AGENT_CONFIG,
    _EMPTY_FINAL_ANSWER_ERROR,
    _FORCED_PLAN_APPROVAL_GUIDANCE,
    _FORCED_PLAN_GUIDANCE,
    _FORCED_PLAN_RETRY_GUIDANCE,
    _MODEL_HISTORY_PLACEHOLDER_REPAIR_GUIDANCE,
    _MODEL_HISTORY_PLACEHOLDER_REPAIR_LIMIT,
    _MODEL_HISTORY_PLACEHOLDER_TOOL_ERROR,
    _OUTPUT_LENGTH_TOOL_RECOVERY,
    _OUTPUT_LENGTH_WRITE_FILE_RECOVERY,
    _PLAN_APPROVAL_DONE_CONTENT,
    _PLAN_APPROVAL_SKIP_MESSAGE,
    _PROVIDER_STALE_SECONDS_ENV,
    _WAITING_FOR_USER_DONE_CONTENT,
    _LoopRuntimeDefaults,
    _log,
    _attach_plan_approval_payload,
    _auto_match_memory_for_latest_prompt,
    _latest_user_text,
    _message_text,
    _plan_approval_is_approved,
    _plan_approval_payload,
    _plan_start_payload,
    _should_emit_plan_start,
    empty_final_answer_retry_text,
    final_summary_wrapup_text,
)
from .kernel.permission_gateway import (
    MAX_TOOL_PERMISSION_RETRIES,
    _approve_tool_permission,
    _negotiate_tool_permission_chain,
    _permission_event_kwargs,
    _policy_decision_payload,
)
from .kernel.state import ToolBudgetState
from .kernel.stream_controller import (
    resolve_provider_stale_seconds as _kernel_resolve_provider_stale_seconds,
    stream_with_activity as _kernel_stream_with_activity,
)
from .kernel.tool_engine import (
    ToolBatchCompleted,
    ToolEngine,
    ToolEngineActivity,
    ToolEngineProgress,
    ToolInvocationCompleted,
    ToolInvocationRequest,
)
from .kernel.tool_result_pipeline import (
    _WEB_SEARCH_IMAGE_LIST_KEYS,
    _WEB_SEARCH_IMAGE_URL_KEYS,
    _normalize_web_search_refs,
    _persist_browser_screenshot_output,
    _prepare_browser_screenshot_output,
    _search_item_image_details,
    _search_item_metadata,
    _search_item_reference_tag,
    _trace_safe_tool_raw_output,
    _web_search_http_url,
    _web_search_image_detail,
    ToolResultPipelineInput,
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
    process_tool_result,
)
from .logger import AgentLogger
from .llm.debug_logging import reset_llm_debug_sink, set_llm_debug_sink
from .loop_guards import (
    EMPTY_ARGS_LIMIT,
    FINAL_SUMMARY_EXCLUDED_TOOLS,
    SEARCH_FILES_TOOL_NAME,
    STREAM_REPEAT_MIN_CHUNKS,
    WEB_SEARCH_TOOL_NAME,
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
    tool_call_budget_wrapup_text,
    total_tool_call_budget_wrapup_text,
    truncation_continuation_text,
)
from .schema import LLMResponse, Message, StreamEvent
from .session_log import SessionLog, SessionLogDurabilityError
from .session_trace import emit_session_trace
from .tool_result_storage import ToolResultStorage
from .model_history import is_model_history_placeholder
from .tools.base import Tool, ToolResult, build_tool_name_index
from .tools.argument_limits import RECOMMENDED_GENERATED_BODY_CHARS
from .tools.browser_intent import BrowserToolIntentPolicy
from .tools.skill_preload import build_active_skills_prompt
from .turn_policy import (
    text_is_short_acknowledgement,
    text_is_short_non_task_reply,
    text_requests_plan_start,
)

__all__ = ["run_agent_loop"]


def _resolve_provider_stale_seconds(config_value: float | None = None) -> float:
    """Resolve staleness using the legacy defaults when iteration begins."""
    return _kernel_resolve_provider_stale_seconds(
        config_value,
        default_stale_seconds=LLM_PROVIDER_STALE_SECONDS,
        environment_variable_name=_PROVIDER_STALE_SECONDS_ENV,
    )


async def _stream_with_activity(
    stream: AsyncIterator[StreamEvent],
    *,
    stale_seconds: float | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream with activity using legacy defaults when iteration begins."""
    if stale_seconds is None:
        stale_seconds = LLM_PROVIDER_STALE_SECONDS
    async for event in _kernel_stream_with_activity(
        stream,
        stale_seconds=stale_seconds,
        activity_interval_seconds=LLM_ACTIVITY_INTERVAL_SECONDS,
    ):
        yield event


async def run_agent_loop(
    *,
    llm,
    summary_llm: Any | None = None,
    messages: list[Message],
    tools: dict[str, Tool],
    max_steps: int = _DEFAULT_AGENT_CONFIG.max_steps,
    tool_limits: ToolLimitsConfig | None = None,
    max_tool_calls: int | None = None,
    max_delegated_tool_calls: int | None = None,
    web_search_total_limit: int | None = None,
    token_limit: int = 113400,
    is_cancelled: CancelChecker | None = None,
    logger: AgentLogger | None = None,
    workspace_dir: str | None = None,
    permission_negotiator: Any | None = None,
    hooks: list | None = None,
    memory_manager: Any | None = None,
    memory_extractor: Any | None = None,
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
    tool_exposure_manager: Any | None = None,
    tool_result_storage: ToolResultStorage | None = None,
    session_log: SessionLog | None = None,
    session_turn: int | None = None,
) -> AsyncIterator[AgentEvent]:
    """Delegate one run while honoring monkeypatched core timing defaults."""
    run_arguments = dict(locals())
    runtime_defaults = _LoopRuntimeDefaults(
        parallel_tool_cancel_grace_seconds=PARALLEL_TOOL_CANCEL_GRACE_SECONDS,
        llm_activity_interval_seconds=LLM_ACTIVITY_INTERVAL_SECONDS,
        tool_activity_interval_seconds=TOOL_ACTIVITY_INTERVAL_SECONDS,
        tool_event_poll_interval_seconds=TOOL_EVENT_POLL_INTERVAL_SECONDS,
        llm_provider_stale_seconds=LLM_PROVIDER_STALE_SECONDS,
        max_provider_stale_recoveries=MAX_PROVIDER_STALE_RECOVERIES,
        provider_stale_seconds_environment_variable=_PROVIDER_STALE_SECONDS_ENV,
    )
    events = run_agent_loop_with_default_services(
        run_arguments=run_arguments,
        runtime_defaults=runtime_defaults,
    )
    try:
        async for event in events:
            yield event
    finally:
        await events.aclose()
