"""Common single-attempt invocation and streaming permission continuation."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from ...session_log import SessionLogDurabilityError
from ..base import EventEmittingTool, Tool, ToolInvocationContext, ToolResult

if TYPE_CHECKING:
    from .scheduler import ToolEngineRecord


# Preserve the established log category while moving the implementation out of
# the legacy core compatibility facade.
_log = logging.getLogger("box_agent.core")

MAX_TOOL_PERMISSION_RETRIES: Final[int] = 4


@dataclass(frozen=True, slots=True)
class PermissionChainCompleted:
    """The final result and last decision for one permission continuation."""

    result: ToolResult
    policy_decision: dict[str, Any] | None


async def invoke_tool_once(
    tool: Tool,
    arguments: dict[str, Any],
    context: ToolInvocationContext | None = None,
) -> ToolResult:
    """Invoke the validated tool interface once, preserving legacy overrides."""
    if context is not None and (isinstance(tool, EventEmittingTool)
                               or getattr(tool, "uses_invocation_context", False)):
        return await tool.invoke(arguments, context=context)
    return await tool.invoke(arguments)


async def stream_tool_invocation(
    tool: Tool,
    arguments: dict[str, Any],
    *,
    invocation_context: ToolInvocationContext | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    passthrough_exceptions: tuple[type[BaseException], ...] = (),
    approved_permission_request: dict[str, Any] | None = None,
) -> AsyncIterator[ToolEngineRecord]:
    """Schedule one standalone attempt with an owned progress queue."""
    # Local import keeps the scheduler -> invoke_tool_once dependency acyclic.
    from .scheduler import ToolEngine, ToolInvocationRequest

    # Legacy standalone adapters can provide an invoke-only tool object.
    tool_name = getattr(tool, "name", type(tool).__name__)
    scheduler = ToolEngine(
        tools={tool_name: tool},
        is_cancelled=is_cancelled if is_cancelled is not None else lambda: False,
        activity_interval_seconds=15.0,
        event_poll_interval_seconds=0.1,
        cancel_grace_seconds=2.0,
        max_parallel_tools=1,
        batch_timeout_seconds=None,
        web_search_concurrency=1,
        web_search_tool_name="web_search",
        passthrough_exceptions=passthrough_exceptions,
    )
    request = ToolInvocationRequest(
        call_id=(
            invocation_context.parent_tool_call_id
            if invocation_context is not None else ""
        ),
        tool_name=tool_name,
        arguments=arguments,
        invocation_context=invocation_context,
        approved_permission_request=approved_permission_request,
    )
    async with aclosing(scheduler.invoke_serial(request)) as records:
        async for record in records:
            yield record


def _permission_event_kwargs(permission_request: dict[str, Any]) -> dict[str, Any]:
    """Normalize a tool permission_request dict for PermissionRequestEvent."""
    temporary_supported = permission_request.get("temporary_supported")
    persistent_supported = permission_request.get("persistent_supported")
    return {
        "scope": str(permission_request.get("scope") or ""),
        "requested_scope": str(permission_request.get("requested_scope") or ""),
        "reason": str(permission_request.get("reason") or ""),
        "path": str(permission_request.get("path") or ""),
        "temporary_supported": (
            True if temporary_supported is None else bool(temporary_supported)
        ),
        "persistent_supported": (
            True if persistent_supported is None else bool(persistent_supported)
        ),
        "persistent_label": str(permission_request.get("persistent_label") or ""),
        "command": str(permission_request.get("command") or ""),
        "risk": str(permission_request.get("risk") or ""),
    }


def _approve_tool_permission(tool: Tool, permission_request: dict[str, Any]) -> None:
    """Let a tool consume one-shot approval state before core retries it."""
    approver = getattr(tool, "approve_permission_request", None)
    if not callable(approver):
        return
    try:
        approver(permission_request)
    except SessionLogDurabilityError:
        raise
    except Exception as exc:
        _log.warning(
            "tool/permission_approval_hook_failed tool=%s error=%s",
            getattr(tool, "name", type(tool).__name__),
            exc,
        )


def _policy_decision_payload(
    *,
    tool_name: str,
    permission_request: dict[str, Any],
    decision: str,
    retry_count: int = 0,
    error: str = "",
) -> dict[str, Any]:
    """Build a host-facing policy decision payload for a permission request."""
    payload = {
        "type": "policy_decision",
        "tool_name": tool_name,
        "decision": decision,
        "retry_count": retry_count,
        **_permission_event_kwargs(permission_request),
    }
    if error:
        payload["error"] = error
    return payload


async def stream_tool_permission_chain(
    *,
    result: ToolResult,
    permission_negotiator: Any,
    tool_name: str,
    tool: Tool | None,
    arguments: dict[str, Any],
    retry_offer_error: Callable[[], str | None],
    retry_records: Callable[[dict[str, Any]], AsyncIterator[ToolEngineRecord]] | None = None,
    on_retry: Callable[[ToolResult], None] | None = None,
) -> AsyncIterator[ToolEngineRecord | PermissionChainCompleted]:
    """Negotiate distinct permission gates until the tool can execute.

    One invocation can legitimately cross more than one independent gate, for
    example a dangerous command that also targets an out-of-workspace path.
    Repeated identical requests are stopped rather than prompting forever when
    a tool or negotiator failed to apply an approved grant.
    """
    from .scheduler import ToolEngineActivity, ToolEngineProgress, ToolInvocationCompleted

    policy_decision: dict[str, Any] | None = None
    retry_count = 0
    seen_requests: set[str] = set()

    while (
        permission_negotiator is not None
        and not result.success
        and result.permission_request
    ):
        permission_request = result.permission_request
        request_key = json.dumps(
            permission_request,
            sort_keys=True,
            ensure_ascii=True,
            default=str,
        )
        if request_key in seen_requests:
            policy_decision = _policy_decision_payload(
                tool_name=tool_name,
                permission_request=permission_request,
                decision="error",
                retry_count=retry_count,
                error="Permission request repeated after approval",
            )
            _log.warning(
                "permission/repeated_after_approval tool=%s retry_count=%d",
                tool_name,
                retry_count,
            )
            break
        if retry_count >= MAX_TOOL_PERMISSION_RETRIES:
            policy_decision = _policy_decision_payload(
                tool_name=tool_name,
                permission_request=permission_request,
                decision="error",
                retry_count=retry_count,
                error="Permission retry limit reached",
            )
            _log.warning(
                "permission/retry_limit tool=%s retry_count=%d",
                tool_name,
                retry_count,
            )
            break

        seen_requests.add(request_key)
        policy_decision = _policy_decision_payload(
            tool_name=tool_name,
            permission_request=permission_request,
            decision="requested",
            retry_count=retry_count,
        )
        try:
            granted = await permission_negotiator.negotiate(permission_request)
        except SessionLogDurabilityError:
            raise
        except Exception as exc:
            policy_decision = _policy_decision_payload(
                tool_name=tool_name,
                permission_request=permission_request,
                decision="error",
                retry_count=retry_count,
                error=str(exc),
            )
            _log.warning(
                "permission/negotiator_error tool=%s error=%s",
                tool_name,
                exc,
            )
            break

        if not granted:
            policy_decision = _policy_decision_payload(
                tool_name=tool_name,
                permission_request=permission_request,
                decision="denied",
                retry_count=retry_count,
            )
            break

        retry_count += 1
        policy_decision = _policy_decision_payload(
            tool_name=tool_name,
            permission_request=permission_request,
            decision="approved",
            retry_count=retry_count,
        )
        offer_error = retry_offer_error()
        if offer_error is not None:
            result = ToolResult(success=False, content="", error=offer_error)
        elif tool is None:
            result = ToolResult(
                success=False,
                content="",
                error=f"Unknown tool: {tool_name}",
            )
        else:
            records = (
                retry_records(permission_request)
                if retry_records is not None
                else stream_tool_invocation(
                    tool, arguments, approved_permission_request=permission_request,
                )
            )
            async with aclosing(records):
                async for record in records:
                    if isinstance(record, (ToolEngineProgress, ToolEngineActivity)):
                        yield record
                    elif isinstance(record, ToolInvocationCompleted):
                        result = record.result
        if on_retry is not None:
            on_retry(result)

    yield PermissionChainCompleted(result=result, policy_decision=policy_decision)


async def _negotiate_tool_permission_chain(
    *,
    result: ToolResult,
    permission_negotiator: Any,
    tool_name: str,
    tool: Tool | None,
    arguments: dict[str, Any],
    retry_offer_error: Callable[[], str | None],
    on_retry: Callable[[ToolResult], None] | None = None,
) -> tuple[ToolResult, dict[str, Any] | None]:
    """Compatibility adapter for callers that only consume the final tuple."""
    async with aclosing(stream_tool_permission_chain(
        result=result,
        permission_negotiator=permission_negotiator,
        tool_name=tool_name,
        tool=tool,
        arguments=arguments,
        retry_offer_error=retry_offer_error,
        on_retry=on_retry,
    )) as records:
        async for record in records:
            if isinstance(record, PermissionChainCompleted):
                return record.result, record.policy_decision
    raise RuntimeError("Permission chain ended without a final result")
