"""Stable execution and composition boundary for framework consumers.

Application adapters should normally use :class:`box_agent.agent.Agent`.
Framework capabilities that need an isolated message/tool set (for example a
sub-agent) may use ``run_agent_loop`` from this module.  Keeping the import
here prevents integrations from depending on the implementation module
``box_agent.core`` directly.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from functools import wraps
from typing import Any

from .core import run_agent_loop as _run_agent_loop
from .events import AgentEvent
from .session_log import SessionLogDurabilityError
from .tools.base import Tool, ToolInvocationContext, ToolResult
from .tools.engine.execution import (
    PermissionChainCompleted,
    stream_tool_invocation,
    stream_tool_permission_chain,
)
from .tools.engine.scheduler import ToolEngineProgress, ToolInvocationCompleted
from .tools.model_tool_context import scoped_model_tool_context


@wraps(_run_agent_loop)
def run_agent_loop(**kwargs: Any) -> AsyncIterator[AgentEvent]:
    """Run the kernel with shared model-tool context composed in."""
    llm = kwargs.get("llm")
    events = _run_agent_loop(**kwargs)
    return scoped_model_tool_context(
        events,
        model=getattr(llm, "model", ""),
        max_output_tokens=getattr(llm, "max_output_tokens", 0),
        session_id=kwargs.get("session_id", ""),
    )


async def invoke_tool_with_permissions(
    tool: Tool,
    arguments: dict[str, Any],
    *,
    permission_negotiator: Any | None = None,
    invocation_context: ToolInvocationContext | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[ToolResult, dict[str, Any] | None]:
    """Invoke one Tool through the shared validation and permission policy.

    Adapters sometimes need a deterministic Tool call outside the agent loop,
    for example to inspect a host-supplied attachment before the next model
    turn.  Keep those calls on the same schema-validation, approval, retry, and
    repeated-request boundaries as ordinary model-selected tools.
    """

    def attempt_records(approval: dict[str, Any] | None = None, *, first_attempt: bool = False):
        return stream_tool_invocation(
            tool,
            arguments,
            invocation_context=invocation_context,
            is_cancelled=is_cancelled,
            # Preserve the standalone first-attempt exception payload below.
            passthrough_exceptions=(Exception,) if first_attempt else (),
            approved_permission_request=approval,
        )

    async def forward_progress(record: ToolEngineProgress) -> None:
        if invocation_context is not None and invocation_context.event_queue is not None:
            await invocation_context.event_queue.put(record.event)

    try:
        async with aclosing(attempt_records(first_attempt=True)) as records:
            async for record in records:
                if isinstance(record, ToolEngineProgress):
                    await forward_progress(record)
                elif isinstance(record, ToolInvocationCompleted):
                    result = record.result
    except SessionLogDurabilityError:
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc!s}"
        return (
            ToolResult(
                success=False,
                error=f"Tool execution failed: {detail}",
                raw_output={
                    "type": "tool_error",
                    "code": "TOOL_EXECUTION_FAILED",
                    "tool": tool.name,
                },
            ),
            None,
        )

    if (
        result.success
        or not result.permission_request
        or permission_negotiator is None
    ):
        return result, None

    async with aclosing(stream_tool_permission_chain(
        result=result,
        permission_negotiator=permission_negotiator,
        tool_name=tool.name,
        tool=tool,
        arguments=arguments,
        retry_offer_error=lambda: None,
        retry_records=attempt_records,
    )) as records:
        async for record in records:
            if isinstance(record, ToolEngineProgress):
                await forward_progress(record)
            elif isinstance(record, PermissionChainCompleted):
                return record.result, record.policy_decision
    raise RuntimeError("Tool invocation ended without a final result")


__all__ = [
    "invoke_tool_with_permissions",
    "run_agent_loop",
]
