"""Optional domain-owned execution lifecycle; the kernel owns all tool execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..events import StopReason, ToolCallResult
from ..schema import Message, ToolCall

if TYPE_CHECKING:
    from .ports import LLMPort


@dataclass(frozen=True, slots=True)
class ExecutionAction:
    """One bounded runtime tool batch or an explicit conversation boundary.

    Tool calls take precedence over a requested boundary. Their actual result
    must be observed before the lifecycle decides whether the task can finish.
    An empty action vetoes finalization and consumes the current step.
    """

    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason | None = None
    content: str = ""


class RunLifecycle:
    """No-op base for session/domain policies implementing RunLifecyclePort."""

    async def begin_run(self, *, messages: list[Message], current_turn_text: str | None,
                        llm: LLMPort, thinking_enabled: bool,
                        is_cancelled: Callable[[], bool]) -> None:
        pass

    async def before_step(self, messages: list[Message]) -> ExecutionAction | None:
        return None

    async def before_finish(self, content: str) -> ExecutionAction | None:
        return None

    def context_messages(self) -> tuple[Message, ...]:
        return ()

    def before_calls(self, calls: tuple[ToolCall, ...]) -> None:
        pass

    def tool_call_error(self, name: str, args: dict) -> str | None:
        return None

    def observe_tool_result(self, event: ToolCallResult) -> None:
        pass

    def after_compaction(self) -> None:
        pass
