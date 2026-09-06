"""Permission negotiation for tool invocations in the stable agent kernel."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable
from typing import Any, Final

from ..session_log import SessionLogDurabilityError
from ..tools.base import Tool, ToolResult


# Preserve the established log category while moving the implementation out of
# the legacy core compatibility facade.
_log = logging.getLogger("box_agent.core")

MAX_TOOL_PERMISSION_RETRIES: Final[int] = 4


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
    """Negotiate distinct permission gates until the tool can execute.

    One invocation can legitimately cross more than one independent gate, for
    example a dangerous command that also targets an out-of-workspace path.
    Repeated identical requests are stopped rather than prompting forever when
    a tool or negotiator failed to apply an approved grant.
    """
    policy_decision: dict[str, Any] | None = None
    retry_count = 0
    seen_requests: set[str] = set()

    while not result.success and result.permission_request:
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
            _approve_tool_permission(tool, permission_request)
            try:
                result = await tool.invoke(arguments)
            except SessionLogDurabilityError:
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc!s}"
                trace = traceback.format_exc()
                result = ToolResult(
                    success=False,
                    content="",
                    error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
                )
        if on_retry is not None:
            on_retry(result)

    return result, policy_decision
