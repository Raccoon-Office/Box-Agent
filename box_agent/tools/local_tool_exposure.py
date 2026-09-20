"""Deterministic local schema exposure over caller-owned capabilities and state."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .base import Tool, build_tool_name_index
from .bash_tool import BackgroundShellManager


DISCOVERABLE_LOCAL_NAMES = frozenset({
    "append_file", "query_jsonl", "bash_output", "bash_kill", "sandbox_status",
    "mcp_config", "report_execution_result",
    "plan_read", "plan_write", "todo_read", "todo_write", "goal_read", "goal_write",
    "memory_read", "memory_search", "memory_write",
    "memory_list_corrections", "memory_write_correction", "memory_supersede_correction", "memory_delete_correction",
    "obsidian_create_note", "obsidian_update_note", "obsidian_daily_note",
    "search_skillhub", "install_skillhub_skill",
})

# These audited method hints change schema visibility only. They never register
# tools, enable configurations, or grant permission to resources or side effects.
SKILL_TOOL_HINTS: Mapping[str, frozenset[str]] = {
    "browser-use": frozenset({"mcp_config"}),
    "mcp-config": frozenset({"mcp_config"}),
    "memory-guide": frozenset({"memory_read", "memory_search", "memory_write", "memory_list_corrections", "memory_write_correction", "memory_supersede_correction", "memory_delete_correction"}),
    "ppt-fast": frozenset({"append_file", "query_jsonl", "report_execution_result"}),
    "docx": frozenset({"append_file", "report_execution_result"}),
    "pdf": frozenset({"append_file", "report_execution_result"}),
    "xlsx": frozenset({"query_jsonl", "report_execution_result"}),
    "hyperframes-video": frozenset({"append_file"}),
    "obsidian": frozenset({"obsidian_create_note", "obsidian_update_note", "obsidian_daily_note"}),
}


class LocalToolExposurePolicy:
    """Read existing owners and stably append needed local tool definitions."""

    def __init__(
        self,
        tools_provider: Callable[[], Mapping[str, Tool]],
        *,
        goal_provider: Callable[[], Any],
        active_skills_provider: Callable[[], Iterable[str]],
    ) -> None:
        self._tools_provider = tools_provider
        self._goal_provider = goal_provider
        self._active_skills_provider = active_skills_provider
        self._required_names: set[str] = set()

    def require_tools(self, names: Iterable[str]) -> None:
        """Make host/method-required tools direct within the current allowed map."""
        index = build_tool_name_index(self._tools_provider().values())
        self._required_names.update(index[name].name for name in names if name in index)

    def candidate_tools(self) -> tuple[Tool, ...]:
        return tuple(
            tool for tool in self._tools_provider().values()
            if tool.name in DISCOVERABLE_LOCAL_NAMES
            and getattr(tool, "mcp_tool_id", None) is None
        )

    def deferred_names(self) -> frozenset[str]:
        tools = self._tools_provider()
        direct = {
            name for name, tool in tools.items()
            if getattr(tool, "_model_exposure_direct", False)
        }
        # JSONL is part of the file/data surface whenever ordinary file access
        # was already enabled by setup; a standalone diagnostic remains findable.
        if "read_file" in tools:
            direct.add("query_jsonl")
        for read_name, write_name, method in (
            ("plan_read", "plan_write", "get"),
            ("todo_read", "todo_write", "list"),
        ):
            for name in (read_name, write_name):
                store = getattr(tools.get(name), "_store", None)
                if store is not None and getattr(store, method)():
                    direct.update((read_name, write_name))
                    break
        if self._goal_provider() is not None:
            direct.update(("goal_read", "goal_write"))
        for name in ("bash_output", "bash_kill"):
            tool = tools.get(name)
            if tool is not None and BackgroundShellManager.get_available_ids(
                getattr(tool, "process_owner_id", None),
            ):
                # Completed handles can still contain unread output. Once the
                # session needs these schemas, retain them for stable follow-up.
                direct.update(("bash_output", "bash_kill"))
        for skill_name in self._active_skills_provider():
            direct.update(SKILL_TOOL_HINTS.get(skill_name, ()))
        self._required_names.update(direct & tools.keys())
        return frozenset(
            tool.name for tool in self.candidate_tools()
            if tool.name not in self._required_names
        )
