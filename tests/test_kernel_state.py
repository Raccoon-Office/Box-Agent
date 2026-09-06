"""Direct behavior checks for per-run kernel tool budget state."""

from __future__ import annotations

import pytest

from box_agent.events import InjectedMessageEvent, ToolCallResult
from box_agent.kernel.state import ToolBudgetState
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


def _state(**overrides: object) -> ToolBudgetState:
    options: dict[str, object] = {
        "tool_call_limits": {"web_search": 2},
        "max_tool_calls": 5,
        "max_delegated_tool_calls": 4,
        "search_files_empty_result_limit": 3,
    }
    options.update(overrides)
    return ToolBudgetState(**options)


def test_reserve_increments_direct_total_and_matching_tool_count() -> None:
    state = _state()

    assert state.reserve("web_search") == (True, None)
    assert state.tool_call_total == 1
    assert state.tool_call_counts == {"web_search": 1}


@pytest.mark.parametrize(
    ("state", "tool_name", "expected_error"),
    [
        (
            _state(tool_call_limits={"web_search": 1}),
            "web_search",
            "Tool call budget reached for web_search (1 calls this turn). "
            "Do not call web_search again; continue the current deliverable and "
            "final response from the evidence and tool results already collected. "
            "If anything is missing, briefly mark it as a gap instead of searching again.",
        ),
        (
            _state(max_tool_calls=1),
            "read_file",
            "Total tool call budget reached (1 calls this task). "
            "Do not call any more tools; synthesize the final answer from the "
            "evidence and tool results already collected.",
        ),
    ],
)
def test_rejected_reservation_preserves_direct_counters(
    state: ToolBudgetState,
    tool_name: str,
    expected_error: str,
) -> None:
    assert state.reserve(tool_name) == (True, None)

    assert state.reserve(tool_name) == (False, expected_error)
    assert state.tool_call_total == 1
    assert state.tool_call_counts == ({"web_search": 1} if tool_name == "web_search" else {})


def test_record_delegated_budget_counts_only_positive_non_bool_integers() -> None:
    state = _state()

    state.record_delegated_tool_budget(
        "sub_agent",
        {"type": "sub_agent_delegation", "tool_calls": 3},
    )
    for invalid_count in (True, False, 0, -1, 1.5, "2", None):
        state.record_delegated_tool_budget(
            "sub_agent",
            {"type": "sub_agent_delegation", "tool_calls": invalid_count},
        )
    state.record_delegated_tool_budget(
        "read_file",
        {"type": "sub_agent_delegation", "tool_calls": 9},
    )
    state.record_delegated_tool_budget(
        "sub_agent",
        {"type": "another_result", "tool_calls": 9},
    )

    assert state.delegated_tool_call_total == 3


class _DelegatingTool(Tool):
    calls = 0

    @property
    def name(self) -> str:
        return "sub_agent"

    @property
    def description(self) -> str:
        return "Return a delegated-tool count for the budget boundary test."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, label: str = "") -> ToolResult:
        self.calls += 1
        return ToolResult(
            success=True,
            content="child result",
            raw_output={"type": "sub_agent_delegation", "tool_calls": 2},
        )


class _BudgetBoundaryLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **_kwargs):
        raise AssertionError("this fixture must not need context summarization")

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls <= 2:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id=f"sub-agent-{self.calls}",
                        type="function",
                        function=FunctionCall(
                            name="sub_agent",
                            arguments={"label": str(self.calls)},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="final answer")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_delegated_limit_rejection_keeps_error_and_wrapup_at_loop_boundary() -> None:
    tool = _DelegatingTool()
    events = [
        event
        async for event in run_agent_loop(
            llm=_BudgetBoundaryLLM(),
            messages=[Message(role="user", content="delegate twice")],
            tools={"sub_agent": tool},
            max_steps=4,
            max_delegated_tool_calls=2,
        )
    ]

    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "sub-agent-2"
    )
    assert tool.calls == 1
    assert blocked.success is False
    assert blocked.error == (
        "Delegated tool call budget reached (2 child calls this task). "
        "Do not start another sub_agent. Continue with the parent tools to merge, "
        "verify, finalize, and deliver the artifacts already produced."
    )
    assert any(
        isinstance(event, InjectedMessageEvent)
        and "子 Agent 内部工具预算已达到上限（2 次）" in event.content
        for event in events
    )
