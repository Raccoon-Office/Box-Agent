"""Per-run mutable state owned by the stable agent kernel."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..loop_guards import (
    SEARCH_FILES_TOOL_NAME,
    delegated_tool_call_budget_message,
    search_files_empty_result_message,
    total_tool_call_budget_message,
    tool_call_budget_message,
)


@dataclass
class ToolBudgetState:
    """Track direct and delegated tool budgets for one agent-loop run."""

    tool_call_limits: Mapping[str, int]
    max_tool_calls: int | None
    max_delegated_tool_calls: int | None
    search_files_empty_result_limit: int
    logger: logging.Logger | None = None
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    tool_call_total: int = 0
    delegated_tool_call_total: int = 0
    delegated_budget_guidance_injected: bool = False
    tool_budget_wrapup_injected: set[str] = field(default_factory=set)
    search_files_consecutive_empty_results: int = 0

    def reserve(self, tool_name: str) -> tuple[bool, str | None]:
        """Reserve one direct tool call without changing counters on rejection."""
        if (
            tool_name == SEARCH_FILES_TOOL_NAME
            and self.search_files_consecutive_empty_results
            >= self.search_files_empty_result_limit
        ):
            return False, search_files_empty_result_message(
                self.search_files_empty_result_limit
            )
        if (
            tool_name == "sub_agent"
            and self.max_delegated_tool_calls is not None
            and self.delegated_tool_call_total >= self.max_delegated_tool_calls
        ):
            return False, delegated_tool_call_budget_message(
                self.max_delegated_tool_calls
            )
        if (
            self.max_tool_calls is not None
            and self.tool_call_total >= self.max_tool_calls
        ):
            return False, total_tool_call_budget_message(self.max_tool_calls)
        limit = self.tool_call_limits.get(tool_name)
        if limit is not None and self.tool_call_counts.get(tool_name, 0) >= limit:
            return False, tool_call_budget_message(tool_name, limit)
        if limit is not None:
            self.tool_call_counts[tool_name] = self.tool_call_counts.get(tool_name, 0) + 1
        self.tool_call_total += 1
        return True, None

    def record_delegated_tool_budget(
        self,
        tool_name: str,
        raw_output: Any,
    ) -> None:
        """Record valid positive child tool counts reported by a sub-agent."""
        if (
            self.max_delegated_tool_calls is None
            or tool_name != "sub_agent"
            or not isinstance(raw_output, dict)
            or raw_output.get("type") != "sub_agent_delegation"
        ):
            return
        nested_tool_calls = raw_output.get("tool_calls")
        if (
            isinstance(nested_tool_calls, bool)
            or not isinstance(nested_tool_calls, int)
            or nested_tool_calls <= 0
        ):
            return
        self.delegated_tool_call_total += nested_tool_calls
        if self.logger is not None:
            self.logger.info(
                "tool_budget/delegated_tool_calls count=%d total=%d limit=%d "
                "parent_total=%d",
                nested_tool_calls,
                self.delegated_tool_call_total,
                self.max_delegated_tool_calls,
                self.tool_call_total,
            )


@dataclass(slots=True)
class ToolExecutionState:
    """Mutable timing and cancellation state for one invocation or batch."""

    last_activity_at: float
    timed_out: bool = False
    cancellation_observed: bool = False
