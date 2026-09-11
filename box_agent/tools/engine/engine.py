"""Run-scoped orchestration over caller-owned tool capabilities."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from time import perf_counter
from typing import Any

from ...events import AgentEvent, LLMActivityEvent, ToolCallResult, ToolCallStart
from ...kernel.hook_types import HookContext, ResultText, copy_data
from ...loop_guards import (
    FINAL_SUMMARY_EXCLUDED_TOOLS, delegated_tool_call_budget_wrapup_text,
    search_files_empty_result_guidance, search_files_result_is_empty,
    tool_call_budget_wrapup_text, total_tool_call_budget_wrapup_text,
)
from ...schema import Message, ToolCall
from ...session_log import SessionLogDurabilityError
from ...session_trace import emit_session_trace
from ..base import Tool, ToolResult, ToolInvocationContext
from ..browser_result_adapter import (
    _prepare_browser_snapshot_output, _prepare_browser_screenshot_output,
    _persist_browser_snapshot_output, _persist_browser_screenshot_output,
    _trace_safe_tool_raw_output,
)
from ..file_result_adapter import (
    _ModelHistoryPlaceholderRecovery, _model_history_placeholder_argument,
    _MODEL_HISTORY_PLACEHOLDER_REPAIR_LIMIT, _MODEL_HISTORY_PLACEHOLDER_TOOL_ERROR,
    _MODEL_HISTORY_PLACEHOLDER_REPAIR_GUIDANCE,
    _model_history_placeholder_recovery_error, _model_history_recovery_target,
    _record_model_history_placeholder_recovery_result,
)
from ..web_search_policy import SearchBatch, WebSearchPolicy
from .artifact_results import (
    _snapshot_workspace_signatures, _detect_tool_artifacts,
    _detect_regex_artifacts, _detect_changed_files,
)
from .budget import ToolBudgetState
from .call_contracts import (
    ToolCallRecord, ToolExecutionOptions, ToolRunContext, ToolStepControl, ToolStepSummary,
)
from .contracts import PreparedTools
from .execution import PermissionChainCompleted, _policy_decision_payload, stream_tool_permission_chain
from .preparation import prepare_tools
from .results import ToolResultPipelineInput, process_tool_result
from .scheduler import (
    ToolEngine, ToolEngineActivity, ToolEngineProgress, ToolInvocationRequest,
    ToolInvocationCompleted, ToolBatchCompleted,
)


_log = logging.getLogger("box_agent.core")


class DefaultToolEngine:
    """Keep request definitions bound to the original session's real tools.

    The catalog, MCP exposure and result store are borrowed. Creating or
    finishing a run never recreates or disposes of those session resources.
    """

    def __init__(
        self,
        *,
        tools: Mapping[str, Tool],
        tool_exposure: Any = None,
        tool_result_store: Any = None,
    ) -> None:
        self._tools = tools
        self._tool_exposure = tool_exposure
        self._tool_result_store = tool_result_store
        self._context: ToolRunContext | None = None
        self._active_calls: AsyncIterator | None = None

    def prepare_tools(
        self,
        *,
        is_tool_visible: Callable[[str], bool] | None = None,
    ) -> PreparedTools:
        """Prepare the model's definitions without copying tool instances."""
        return prepare_tools(
            list(self._tools.values()),
            tool_exposure=self._tool_exposure,
            is_tool_visible=(
                (lambda tool: is_tool_visible(tool.name))
                if is_tool_visible is not None
                else None
            ),
        )

    def configure_run(self, context: ToolRunContext, options: ToolExecutionOptions) -> None:
        """Bind run services once; keep every session-owned object borrowed."""
        if context.artifact_root_dir is not None:
            _log.warning(
                "artifact_root_dir is deprecated and ignored; artifact discovery uses workspace_dir"
            )
        self._context, self._options = context, options
        self._budget = ToolBudgetState(
            tool_call_limits=options.tool_call_limits,
            max_tool_calls=options.max_tool_calls,
            max_delegated_tool_calls=options.max_delegated_tool_calls,
            search_files_empty_result_limit=options.search_files_empty_result_limit,
            logger=_log,
        )
        self._search = WebSearchPolicy(options.web_search_batch_size, options.tool_call_limits.get("web_search", 0))
        self._recovery: _ModelHistoryPlaceholderRecovery | None = None
        self._placeholder_repairs = 0
        self._framework_errors: dict[str, int] = {}
        self._empty_search_guidance = False
        self._execution_targets: dict[str, Tool] = {}
        self._scheduler = ToolEngine(
            tools=self._execution_targets, is_cancelled=context.is_cancelled,
            activity_interval_seconds=options.activity_interval_seconds,
            event_poll_interval_seconds=options.event_poll_interval_seconds,
            cancel_grace_seconds=options.cancel_grace_seconds,
            max_parallel_tools=options.max_parallel_tools,
            batch_timeout_seconds=options.batch_timeout_seconds,
            web_search_concurrency=options.web_search_concurrency,
            web_search_tool_name="web_search",
            passthrough_exceptions=(SessionLogDurabilityError,),
        )

    async def aclose(self) -> None:
        if self._active_calls is not None:
            await self._active_calls.aclose()
            self._active_calls = None

    def budget_guidance(self) -> list[str]:
        """Return original one-shot budget notices at the next request boundary."""
        budget, options = self._budget, self._options
        guidance = []
        for name, limit in options.tool_call_limits.items():
            if budget.tool_call_counts.get(name, 0) >= limit and name not in budget.tool_budget_wrapup_injected:
                budget.tool_budget_wrapup_injected.add(name)
                guidance.append(tool_call_budget_wrapup_text(name, limit))
        if (options.max_delegated_tool_calls is not None
                and budget.delegated_tool_call_total >= options.max_delegated_tool_calls
                and not budget.delegated_budget_guidance_injected):
            budget.delegated_budget_guidance_injected = True
            guidance.append(delegated_tool_call_budget_wrapup_text(options.max_delegated_tool_calls))
        if (budget.search_files_consecutive_empty_results >= options.search_files_empty_result_limit
                and not self._empty_search_guidance):
            self._empty_search_guidance = True
            guidance.append(search_files_empty_result_guidance(options.search_files_empty_result_limit))
        if (options.max_tool_calls is not None and budget.tool_call_total >= options.max_tool_calls
                and "__total__" not in budget.tool_budget_wrapup_injected):
            budget.tool_budget_wrapup_injected.add("__total__")
            guidance.append(total_tool_call_budget_wrapup_text(options.max_tool_calls))
        return guidance

    def _event(self, record: ToolEngineProgress | ToolEngineActivity, step: int) -> AgentEvent:
        if isinstance(record, ToolEngineProgress):
            return record.event
        return LLMActivityEvent(step=step, payload={
            "protocol": "agent_activity_v1", "phase": "tool_running", "tool_name": record.tool_name,
        })

    def _prepare_paths(self, call: ToolCallRecord) -> str | None:
        context = self._context
        if "filename" in call.arguments:
            if call.name == "managed_browser_snapshot":
                call.snapshot_target = None
            elif call.name == "managed_browser_take_screenshot":
                call.screenshot_target = None
        snapshot, snapshot_error = _prepare_browser_snapshot_output(
            call.name, call.arguments, context.workspace_dir,
        )
        screenshot, screenshot_error = _prepare_browser_screenshot_output(
            call.name, call.arguments, context.workspace_dir,
        )
        # The adapters consume a managed filename. Preserve that saved target
        # when rechecking unchanged arguments after a Hook, replace it if a Hook
        # explicitly supplies a new managed target.
        if snapshot is not None:
            call.snapshot_target = snapshot
        if screenshot is not None:
            call.screenshot_target = screenshot
        return snapshot_error or screenshot_error

    def _argument_error(self, call: ToolCallRecord, summary: ToolStepSummary) -> str | None:
        context = self._context
        argument = _model_history_placeholder_argument(call.name, call.arguments)
        if argument is not None:
            if self._placeholder_repairs < _MODEL_HISTORY_PLACEHOLDER_REPAIR_LIMIT:
                summary.repair_guidance = _MODEL_HISTORY_PLACEHOLDER_REPAIR_GUIDANCE
            if self._recovery is None:
                self._recovery = _ModelHistoryPlaceholderRecovery(
                    tool_name=call.name, argument_name=argument,
                    target=_model_history_recovery_target(
                        call.name, call.arguments, context.workspace_dir,
                    ),
                    action=str(call.arguments.get("action")) if call.name == "staged_file_write" else None,
                )
            return f"{_MODEL_HISTORY_PLACEHOLDER_TOOL_ERROR} Rejected argument: {call.name}.{argument}."
        return _model_history_placeholder_recovery_error(
            self._recovery, call.name, call.arguments, context.workspace_dir,
        )

    async def _start_call(self, call: ToolCallRecord, prepared: PreparedTools,
                          control: ToolStepControl, summary: ToolStepSummary,
                          search: SearchBatch) -> AsyncIterator[AgentEvent]:
        context = self._context
        path_error = self._prepare_paths(call)
        intent_error = context.policy_error(call.name, call.arguments)
        offer_error = prepared.admission_error(call.name)
        if offer_error and intent_error:
            offer_error = f"{intent_error}\n{offer_error}"
        placeholder = _model_history_placeholder_argument(call.name, call.arguments)
        if summary.completed_turn_ending_tool is not None:
            call.rejection = (
                f"Skipped because interactive tool '{summary.completed_turn_ending_tool}' "
                "already completed in this model step. Resume after the user responds."
            )
        elif offer_error or intent_error:
            call.rejection = offer_error or intent_error
        elif argument_error := self._argument_error(call, summary):
            call.rejection = argument_error
        elif path_error:
            call.rejection = path_error
        elif control.allowed_names is not None and call.name not in control.allowed_names:
            call.rejection = control.blocked_reason
        elif call.name == "web_search":
            call.allowed, call.rejection = self._search.reserve(call.arguments, search, self._budget)
        else:
            call.allowed, call.rejection = self._budget.reserve(call.name)
        call.user_visible = call.allowed or (
            placeholder is not None and self._placeholder_repairs >= _MODEL_HISTORY_PLACEHOLDER_REPAIR_LIMIT
        )
        if call.user_visible and call.name not in FINAL_SUMMARY_EXCLUDED_TOOLS:
            summary.visible_calls += 1
        yield ToolCallStart(
            tool_call_id=call.call_id, tool_name=call.name,
            arguments=deepcopy(call.arguments), user_visible=call.user_visible,
            tool_id=call.tool_id, server_name=call.server_name,
        )
        if call.user_visible and call.allowed:
            if context.hook_dispatch is not None:
                resolution = await context.hook_dispatch.before_tool(
                    self._hook_context(call, control.step, "tool.before_execution"), call.arguments,
                )
                if resolution.action == "deny":
                    call.allowed = False
                    call.rejection = f"{resolution.code}: {resolution.reason}"
                    call.hook_rejection = {
                        "code": resolution.code, "reason": resolution.reason,
                        "hook_id": resolution.hook_id, "source": resolution.source,
                    }
                else:
                    call.arguments = copy_data(resolution.arguments)
            elif context.hooks.hooks:
                call.arguments = deepcopy(await context.hooks.fire_tool_start(
                    tool_call_id=call.call_id, tool_name=call.name, arguments=call.arguments,
                ))
            if call.allowed:
                final_path_error = self._prepare_paths(call)
                final_error = (prepared.admission_error(call.name)
                               or context.policy_error(call.name, call.arguments)
                               or self._argument_error(call, summary) or final_path_error)
                if final_error:
                    call.allowed, call.rejection = False, final_error
        if call.allowed and call.target is not None:
            context.record_call(call, control.step)
        call.started_at = perf_counter()
        trace = {
            "tool_name": call.name, "tool_id": call.tool_id, "server_name": call.server_name,
            "arguments": call.arguments, "allowed_to_execute": call.allowed,
            "user_visible": call.user_visible,
        }
        if call.parallel:
            trace["parallel"] = True
        emit_session_trace("tool.request", turn_id=context.turn_id, step=control.step,
                           tool_call_id=call.call_id, data=trace)
        if (not call.parallel and self._options.artifact_detection_enabled
                and call.allowed and call.user_visible and context.workspace_dir):
            call.before_files = _snapshot_workspace_signatures(context.workspace_dir)

    def _hook_context(self, call: ToolCallRecord, step: int, event: str, **payload: Any) -> HookContext:
        """由装配层的运行身份创建本次工具调用快照。"""
        base = self._context.hook_context
        if base is None:
            raise RuntimeError("HookDispatchPort 缺少装配层提供的 Run 身份")
        return replace(
            base, event=event, step=step, tool_call_id=call.call_id,
            payload={"tool_name": call.name, "tool_call_id": call.call_id,
                     "arguments": call.arguments, **payload},
        )

    def _request(self, call: ToolCallRecord, prepared: PreparedTools) -> ToolInvocationRequest:
        error = call.rejection if not call.allowed else prepared.validate_call(call.name)
        if call.allowed and call.target is None:
            error = f"Unknown tool: {call.name}"
        result = ToolResult(
            success=False, content="", error=error or "", raw_output=call.hook_rejection,
        ) if not call.allowed or error else None
        return ToolInvocationRequest(
            call.call_id, call.name, call.arguments, immediate_result=result,
            on_invoke=lambda: setattr(call, "invoked", True),
            invocation_context=ToolInvocationContext(parent_tool_call_id=call.call_id, skill_reader=self._context.skill_reader),
        )

    def _log_result(self, call: ToolCallRecord, result: ToolResult) -> None:
        if self._context.logger:
            self._context.logger.log_tool_result(
                tool_name=call.name, arguments=call.arguments, result_success=result.success,
                result_content=result.content if result.success else None,
                result_error=result.error if not result.success else None,
                raw_output=_trace_safe_tool_raw_output(result.raw_output),
                tool_id=call.tool_id, server_name=call.server_name,
            )

    async def _finish_call(self, call: ToolCallRecord, result: ToolResult,
                           prepared: PreparedTools, control: ToolStepControl,
                           summary: ToolStepSummary, search: SearchBatch,
                           emitted_paths: set) -> AsyncIterator[AgentEvent]:
        context = self._context
        self._log_result(call, result)
        if not result.success and result.permission_request:
            if context.permission_negotiator:
                async with aclosing(stream_tool_permission_chain(
                    result=result, permission_negotiator=context.permission_negotiator,
                    tool_name=call.name, tool=call.target, arguments=call.arguments,
                    retry_offer_error=lambda: prepared.validate_call(call.name),
                    retry_records=lambda approval: self._scheduler.invoke_serial(
                        ToolInvocationRequest(
                            call.call_id, call.name, call.arguments,
                            approved_permission_request=approval,
                            on_invoke=lambda: setattr(call, "invoked", True),
                        )
                    ),
                    on_retry=lambda retry: self._log_result(call, retry),
                )) as records:
                    async for record in records:
                        if isinstance(record, PermissionChainCompleted):
                            result, call.policy_decision = record.result, record.policy_decision
                        else:
                            yield self._event(record, control.step)
            else:
                call.policy_decision = _policy_decision_payload(
                    tool_name=call.name, permission_request=result.permission_request, decision="requested",
                )
        # Keep the actual execution outcome apart from adaptations and Hook
        # display changes. Large result payloads are referenced, not recopied.
        call.execution_result = result
        call.executed = bool(
            call.invoked and not result.permission_request
            and (result.raw_output or {}).get("code") not in {"INVALID_TOOL_SCHEMA", "INVALID_TOOL_ARGUMENTS"}
        )
        if control.result_transform is not None:
            result = control.result_transform(call.name, result)
        result = _persist_browser_snapshot_output(result, call.snapshot_target)
        result = _persist_browser_screenshot_output(result, call.screenshot_target)
        # Skill text remains an ordinary tool result. The reader has already
        # applied its shared request budget; no system activation is performed.
        result, blocks, tokens = context.validate_followup(
            result, call.target, control.pending_followup_tokens + summary.transient_tokens,
        )
        if blocks:
            summary.transient_blocks.extend(blocks)
            summary.transient_tokens += tokens
        self._recovery = _record_model_history_placeholder_recovery_result(
            self._recovery, call.name, call.arguments, result,
        )
        self._budget.record_delegated_tool_budget(call.name, result.raw_output)
        if call.name == "search_files":
            if search_files_result_is_empty(result):
                self._budget.search_files_consecutive_empty_results += 1
            elif result.success:
                self._budget.search_files_consecutive_empty_results = 0
        if result.success:
            summary.successful_tools.add(call.name)
            if (result.content or "").strip() and not search_files_result_is_empty(result):
                summary.made_progress = True
            if call.allowed and getattr(call.target, "ends_turn_on_success", False):
                summary.completed_turn_ending_tool = call.name
        content, error = result.content, result.error
        text_resolution = None
        if context.hook_dispatch is not None:
            text_resolution = await context.hook_dispatch.after_tool(
                self._hook_context(
                    call, control.step, "tool.after_execution", executed=call.executed,
                    success=result.success, user_visible=call.user_visible,
                ), ResultText(content, error),
            )
            content, error = text_resolution.text.content, text_resolution.text.error
        elif context.hooks.hooks and call.user_visible:
            content, error = await context.hooks.fire_tool_result(
                tool_call_id=call.call_id, tool_name=call.name, success=result.success,
                content=content, error=error,
            )
        hook_text_modified = bool(text_resolution and text_resolution.modified)
        outcome = process_tool_result(ToolResultPipelineInput(
            messages=context.messages, tool_call_id=call.call_id, tool_name=call.name,
            arguments=call.arguments, result=result, visible_content=content, visible_error=error,
            result_storage=context.result_storage, tool=call.target, session_id=context.session_id,
            resource_ledger=context.resource_ledger, web_search_seen_result_keys=self._search.seen_result_keys,
            framework_error_counts=self._framework_errors, user_visible=call.user_visible,
            emit_legacy_permission_request=not context.permission_negotiator,
            policy_decision=call.policy_decision, tool_id=call.tool_id, server_name=call.server_name,
            turn_id=context.turn_id, step=control.step, started_at=call.started_at,
            parallel=call.parallel, commit_result=context.commit_result,
            hook_text_modified=hook_text_modified,
        ))
        self._search.record_result(outcome, search)
        if context.hook_dispatch is not None and not context.is_cancelled():
            await context.hook_dispatch.observe(self._hook_context(
                call, control.step, "tool.finished", executed=call.executed,
                success=result.success, user_visible=call.user_visible,
                content=outcome.visible_content, error=outcome.visible_error,
                suppressed=bool(text_resolution and text_resolution.suppressed),
                hook_source=text_resolution.source if text_resolution else None,
                hook_rejection=call.hook_rejection,
            ))
        for event in outcome.events:
            yield event
        if self._options.artifact_detection_enabled and result.success and context.workspace_dir:
            if call.parallel:
                if call.user_visible:
                    artifacts, paths = _detect_regex_artifacts(
                        call.call_id, call.name, outcome.visible_content, result.raw_output,
                        context.workspace_dir,
                    )
                    emitted_paths.update(paths)
                    for artifact in artifacts:
                        yield artifact
            else:
                after = _snapshot_workspace_signatures(context.workspace_dir)
                for artifact in _detect_tool_artifacts(
                    call.call_id, call.name, outcome.visible_content, result.raw_output,
                    call.before_files, after, context.workspace_dir,
                ):
                    yield artifact

    async def execute_calls(self, prepared: PreparedTools, calls: list[ToolCall],
                            control: ToolStepControl) -> AsyncIterator[AgentEvent | ToolStepSummary]:
        if self._context is None:
            raise RuntimeError("Tool engine run has not been configured")
        if self._active_calls is not None:
            raise RuntimeError("A tool step is already running")
        async with aclosing(self._execute_calls(prepared, calls, control)) as records:
            self._active_calls = records
            try:
                async for record in records:
                    yield record
            finally:
                self._active_calls = None

    async def _execute_calls(self, prepared: PreparedTools, calls: list[ToolCall],
                             control: ToolStepControl) -> AsyncIterator[AgentEvent | ToolStepSummary]:
        context = self._context
        self._execution_targets.clear()
        self._execution_targets.update(prepared.targets)
        summary, search = ToolStepSummary(), SearchBatch()
        unique, duplicates, first_by_signature = [], [], {}
        for original, normalized in zip(calls, prepared.canonicalize_calls(calls)):
            name, arguments = normalized.function.name, normalized.function.arguments
            signature = name, json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            tool_id, server_name = prepared.target_identity(name)
            call = ToolCallRecord(normalized.id, original.function.name, name, arguments,
                                  prepared.targets.get(name), tool_id, server_name)
            if signature in first_by_signature:
                duplicates.append((call, first_by_signature[signature]))
            else:
                first_by_signature[signature] = call
                unique.append(call)
        if duplicates:
            _log.info("tool/dedupe skipped=%d unique=%d", len(duplicates), len(unique))
        turn_boundary = any(getattr(call.target, "ends_turn_on_success", False) for call in unique)
        search_batch = self._options.web_search_concurrency > 1 and all(call.name == "web_search" for call in unique)
        serial, parallel = [], []
        for call in unique:
            call.parallel = not turn_boundary and not _model_history_placeholder_argument(call.name, call.arguments) and (
                call.target is not None and (
                    (call.name == "web_search" and search_batch) or getattr(call.target, "parallel_safe", False)
                )
            )
            (parallel if call.parallel else serial).append(call)
        outcomes: dict[str, bool] = {}
        for call in serial:
            async for event in self._start_call(call, prepared, control, summary, search):
                yield event
            result = ToolResult(success=False, content="", error="Tool execution interrupted — no result returned.")
            async with aclosing(self._scheduler.invoke_serial(self._request(call, prepared))) as records:
                async for record in records:
                    if isinstance(record, ToolInvocationCompleted):
                        result = record.result
                    else:
                        yield self._event(record, control.step)
            async with aclosing(self._finish_call(call, result, prepared, control, summary, search, set())) as events:
                async for event in events:
                    if isinstance(event, ToolCallResult):
                        outcomes[call.call_id] = event.success
                    yield event
            if context.is_cancelled():
                return
        if parallel:
            before = (_snapshot_workspace_signatures(context.workspace_dir)
                      if self._options.artifact_detection_enabled and context.workspace_dir else None)
            for call in parallel:
                async for event in self._start_call(call, prepared, control, summary, search):
                    yield event
            context.flush_calls()
            results = {}
            async with aclosing(self._scheduler.invoke_parallel([self._request(call, prepared) for call in parallel])) as records:
                async for record in records:
                    if isinstance(record, ToolBatchCompleted):
                        results = {outcome.call_id: outcome.result for outcome in record.outcomes}
                    else:
                        yield self._event(record, control.step)
            emitted = set()
            # The first parallel batch finishes before the permission UI opens;
            # follow-up permission chains retain input order and no batch timer.
            for call in parallel:
                result = results.get(call.call_id, ToolResult(
                    success=False, content="", error="Tool execution interrupted — no result returned.",
                ))
                async with aclosing(self._finish_call(call, result, prepared, control, summary, search, emitted)) as events:
                    async for event in events:
                        if isinstance(event, ToolCallResult):
                            outcomes[call.call_id] = event.success
                        yield event
            if self._options.artifact_detection_enabled and context.workspace_dir:
                after = _snapshot_workspace_signatures(context.workspace_dir)
                for artifact in _detect_changed_files(parallel[0].call_id, before, after, emitted, context.workspace_dir):
                    yield artifact
            if context.is_cancelled():
                return
        for duplicate, source in duplicates:
            duplicate_events = self._finish_duplicate(duplicate, source, outcomes.get(source.call_id), control.step)
            if context.hook_dispatch is not None and not context.is_cancelled():
                final = duplicate_events[-1]
                await context.hook_dispatch.observe(self._hook_context(
                    duplicate, control.step, "tool.finished", executed=False,
                    success=final.success, content=final.content, error=final.error,
                    duplicate_of=source.call_id, user_visible=False,
                ))
            for event in duplicate_events:
                yield event
        if summary.repair_guidance:
            self._placeholder_repairs += 1
        summary.search_guidance = self._search.guidance(search, self._budget.tool_call_counts.get("web_search", 0))
        yield summary

    def _finish_duplicate(self, call: ToolCallRecord, source: ToolCallRecord,
                          succeeded: bool | None, step: int) -> list[AgentEvent]:
        if succeeded is True:
            content = f"Duplicate tool call skipped: identical call {source.call_id} already executed successfully in this response. Reuse its result."
            error = None
        elif succeeded is False:
            content = ""
            error = f"Duplicate tool call skipped: identical call {source.call_id} already failed in this response. Fix that failure before retrying."
        else:
            content = ""
            error = f"Duplicate tool call skipped because its identical source call {source.call_id} did not produce a result."
        context = self._context
        trace = {
            "tool_name": call.name, "tool_id": call.tool_id, "server_name": call.server_name,
            "arguments": call.arguments, "allowed_to_execute": False, "user_visible": False,
            "duplicate_of": source.call_id,
        }
        emit_session_trace("tool.request", turn_id=context.turn_id, step=step, tool_call_id=call.call_id, data=trace)
        start = ToolCallStart(tool_call_id=call.call_id, tool_name=call.name, arguments=call.arguments,
                              user_visible=False, tool_id=call.tool_id, server_name=call.server_name)
        event = ToolCallResult(tool_call_id=call.call_id, tool_name=call.name, success=succeeded is True,
                               content=content, error=error, user_visible=False,
                               tool_id=call.tool_id, server_name=call.server_name)
        context.commit_result(Message(role="tool", content=content or error or "",
                                      tool_call_id=call.call_id, name=call.name), event, step)
        emit_session_trace("tool.response", turn_id=context.turn_id, step=step, tool_call_id=call.call_id, data={
            "tool_name": call.name, "tool_id": call.tool_id, "server_name": call.server_name,
            "success": succeeded is True, "content": content, "error": error, "raw_output": None,
            "model_content": content or error or "", "policy_decision": None, "user_visible": False,
            "duplicate_of": source.call_id, "duration_ms": 0,
        })
        return [start, event]
