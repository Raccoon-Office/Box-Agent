"""Shared tool-result preparation and its caller-owned commit boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from ...context_resources import ContextResourceLedger
from ...events import AgentEvent, PermissionRequestEvent, ToolCallResult, WebSearchEvent
from ...schema import Message
from ...session_trace import emit_session_trace
from ...tool_result_storage import ToolResultStorage
from ..base import Tool, ToolResult
from ..browser_result_adapter import _trace_safe_tool_raw_output
from ..file_result_adapter import (
    _context_resource_history_decision,
    _record_context_resource_history,
    _repeatable_framework_error,
    _tool_message_content_for_model,
)
from ..web_search_runtime import (
    _dedupe_web_search_content,
    _extract_web_search_payload,
    _log_web_search_model_results,
)
from .execution import _permission_event_kwargs


@dataclass(frozen=True, slots=True)
class ToolResultPipelineInput:
    """Explicit inputs for one completed tool call's shared post-processing."""

    messages: list[Message]
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: ToolResult
    visible_content: str
    visible_error: str | None
    result_storage: ToolResultStorage
    tool: Tool | None = None
    session_id: str = ""
    resource_ledger: ContextResourceLedger | None = None
    web_search_seen_result_keys: set[str] | None = None
    framework_error_counts: dict[str, int] | None = None
    user_visible: bool = True
    emit_legacy_permission_request: bool = False
    policy_decision: dict[str, Any] | None = None
    tool_id: str | None = None
    server_name: str | None = None
    turn_id: str = ""
    step: int = 0
    started_at: float | None = None
    parallel: bool = False
    commit_result: Callable[[Message, ToolCallResult, int], None] | None = None
    hook_text_modified: bool = False


@dataclass(frozen=True, slots=True)
class ToolResultPipelineOutcome:
    """Events and explicit counter deltas produced for one completed tool call."""

    events: tuple[AgentEvent, ...]
    tool_message: Message
    model_content: str
    visible_content: str
    visible_error: str | None
    web_search_new_results: int = 0
    web_search_duplicate_results: int = 0
    web_search_labels: tuple[str, ...] = ()
    web_search_inspected: bool = False


def process_tool_result(
    pipeline_input: ToolResultPipelineInput,
) -> ToolResultPipelineOutcome:
    """Commit one stored tool reply before exposing its result-derived events."""

    result = pipeline_input.result
    visible_content = pipeline_input.visible_content
    visible_error = pipeline_input.visible_error
    new_count = 0
    duplicate_count = 0
    new_labels: list[str] = []
    inspected = False

    if result.success and pipeline_input.tool_name == "web_search":
        (
            visible_content,
            new_count,
            duplicate_count,
            new_labels,
            inspected,
        ) = _dedupe_web_search_content(
            visible_content,
            (
                pipeline_input.web_search_seen_result_keys
                if pipeline_input.web_search_seen_result_keys is not None
                else set()
            ),
            pipeline_input.arguments,
        )

    resource_decision = _context_resource_history_decision(
        tool_name=pipeline_input.tool_name,
        arguments=pipeline_input.arguments,
        result=result,
        messages=pipeline_input.messages,
        ledger=pipeline_input.resource_ledger,
    )
    model_content = _tool_message_content_for_model(
        tool_name=pipeline_input.tool_name,
        arguments=pipeline_input.arguments,
        result=result,
        visible_content=visible_content,
        visible_error=visible_error,
        resource_receipt=resource_decision.receipt,
    )
    if pipeline_input.hook_text_modified:
        # 已处理的可见文本进入历史，避免原 model_context 或资源回执覆盖修改。
        model_content = visible_content if result.success else f"Error: {visible_error or visible_content}"
    repeated = _repeatable_framework_error(
        tool_name=pipeline_input.tool_name,
        result=result,
        visible_error=visible_error,
    )
    if repeated is not None and pipeline_input.framework_error_counts is not None and not pipeline_input.hook_text_modified:
        signature, label = repeated
        count = pipeline_input.framework_error_counts.get(signature, 0) + 1
        pipeline_input.framework_error_counts[signature] = count
        if count > 1:
            model_content = (
                f"Error: REPEATED_FRAMEWORK_FAILURE: {label} occurrence {count}. "
                "The first matching tool result contains the full diagnostic and repair "
                "guidance. Do not retry the unchanged call."
            )
    if result.success and pipeline_input.tool_name == "web_search":
        _log_web_search_model_results(
            pipeline_input.arguments,
            visible_content,
            model_content,
        )

    tool_message = Message(
        role="tool",
        content=model_content,
        tool_call_id=pipeline_input.tool_call_id,
        name=pipeline_input.tool_name,
    )
    tool_message = pipeline_input.result_storage.process_message(
        tool_message,
        tool=pipeline_input.tool,
        session_id=pipeline_input.session_id,
        persistence_content=result.persistence_content,
        content_already_processed=(
            result.success
            and result.model_context is not None
            and model_content == result.model_context
        ),
    )
    model_content = tool_message.content
    result_event = ToolCallResult(
        tool_call_id=pipeline_input.tool_call_id,
        tool_name=pipeline_input.tool_name,
        success=result.success,
        content=visible_content,
        error=visible_error,
        raw_output=_trace_safe_tool_raw_output(result.raw_output),
        user_visible=pipeline_input.user_visible,
        policy_decision=pipeline_input.policy_decision,
        tool_id=pipeline_input.tool_id,
        server_name=pipeline_input.server_name,
    )
    if pipeline_input.commit_result is None:
        pipeline_input.messages.append(tool_message)
    else:
        pipeline_input.commit_result(tool_message, result_event, pipeline_input.step)
    _record_context_resource_history(
        tool_call_id=pipeline_input.tool_call_id,
        decision=resource_decision,
        result=result,
        visible_content=visible_content,
        model_content=model_content,
        ledger=pipeline_input.resource_ledger,
    )

    trace_data: dict[str, Any] = {
        "tool_name": pipeline_input.tool_name,
        "tool_id": pipeline_input.tool_id,
        "server_name": pipeline_input.server_name,
        "success": result.success,
        "content": visible_content,
        "error": visible_error,
        "raw_output": result.raw_output,
        "model_content": model_content,
        "policy_decision": pipeline_input.policy_decision,
        "user_visible": pipeline_input.user_visible,
    }
    if pipeline_input.parallel:
        trace_data["parallel"] = True
    trace_data["duration_ms"] = (
        max(0, int((perf_counter() - pipeline_input.started_at) * 1000))
        if pipeline_input.started_at is not None
        else 0
    )
    emit_session_trace(
        "tool.response",
        turn_id=pipeline_input.turn_id,
        step=pipeline_input.step,
        tool_call_id=pipeline_input.tool_call_id,
        data=trace_data,
    )

    events: list[AgentEvent] = [result_event]
    if result.success and pipeline_input.user_visible:
        web_search_payload = _extract_web_search_payload(
            pipeline_input.tool_name,
            visible_content,
        )
        if web_search_payload is not None:
            events.append(
                WebSearchEvent(
                    tool_call_id=pipeline_input.tool_call_id,
                    payload=web_search_payload,
                )
            )
    if (
        not result.success
        and result.permission_request
        and pipeline_input.emit_legacy_permission_request
    ):
        events.append(
            PermissionRequestEvent(
                tool_call_id=pipeline_input.tool_call_id,
                **_permission_event_kwargs(result.permission_request),
            )
        )

    return ToolResultPipelineOutcome(
        events=tuple(events),
        tool_message=tool_message,
        model_content=model_content,
        visible_content=visible_content,
        visible_error=visible_error,
        web_search_new_results=new_count,
        web_search_duplicate_results=duplicate_count,
        web_search_labels=tuple(new_labels),
        web_search_inspected=inspected,
    )
