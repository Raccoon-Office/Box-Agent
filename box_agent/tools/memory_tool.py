"""Memory tools — read, write, and search long-term memory.

Provides persistent cross-session memory that survives beyond individual
sessions.  Core memory (MEMORY.md) is always injected; context memory
(CONTEXT.md) is searchable on demand.
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolResult


class MemoryWriteTool(Tool):
    """Tool for writing entries to long-term memory."""

    def __init__(self, memory_manager, llm=None):
        from box_agent.memory import MemoryManager

        self._mgr: MemoryManager = memory_manager
        self._llm = llm

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Write to long-term memory that persists across sessions. "
            "Use category='core' ONLY when the user explicitly states personal info "
            "or preferences (e.g. 'my name is...', 'I prefer...'). Never write "
            "summaries or inferences to core. "
            "Use category='context' for project context, task patterns, and notes. "
            "Context writes are model-merged with existing context when possible."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The content to write. Use markdown bullet points, "
                        "e.g. '- User prefers Chinese responses\\n- Project uses React'"
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": ["core", "context"],
                    "description": (
                        "'core' for user identity/preferences (always recalled), "
                        "'context' for project info/task patterns (searchable). Default: 'core'."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "overwrite"],
                    "description": "Write mode: 'append' adds to existing (default), 'overwrite' replaces the category.",
                },
            },
            "required": ["content"],
        }

    async def execute(self, content: str, category: str = "core", mode: str = "append") -> ToolResult:
        try:
            if category == "context":
                if mode == "overwrite":
                    self._mgr.write_context(content)
                    strategy = "overwrite"
                elif self._llm is not None:
                    strategy = await self._mgr.update_context_with_llm(content, self._llm)
                else:
                    self._mgr.append_context(content)
                    strategy = "append_dedup"
                current = self._mgr.read_context()
                label = "context"
            else:
                if mode == "overwrite":
                    self._mgr.write_core(content)
                else:
                    self._mgr.append_core(content)
                current = self._mgr.read_core()
                label = "core"
                strategy = mode

            return ToolResult(
                success=True,
                content=f"Memory updated ({label}, {strategy}). Current {label} memory:\n{current}",
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to write memory: {e}")


class MemoryReadTool(Tool):
    """Tool for reading all long-term memory."""

    def __init__(self, memory_manager):
        from box_agent.memory import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_read"

    @property
    def description(self) -> str:
        return (
            "Read all long-term memory. Returns core memory (always recalled) "
            "and context memory (searchable). Use this to review what has been saved."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self) -> ToolResult:
        try:
            core = self._mgr.read_core()
            context = self._mgr.read_context()
            if not core and not context:
                return ToolResult(success=True, content="No long-term memory saved yet.")

            parts: list[str] = []
            if core:
                parts.append(f"[Core Memory (MEMORY.md)]\n{core}")
            if context:
                parts.append(f"[Context Memory (CONTEXT.md)]\n{context}")
            return ToolResult(success=True, content="\n\n".join(parts))
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to read memory: {e}")


class MemorySearchTool(Tool):
    """Tool for searching context memory by keyword."""

    def __init__(self, memory_manager):
        from box_agent.memory import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search context memory (CONTEXT.md) by keyword. Use this to find "
            "project context, task patterns, or historical notes. Core memory "
            "is always available — use this for everything else."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to search for (case-insensitive).",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str) -> ToolResult:
        try:
            results = self._mgr.search(query)
            if not results:
                return ToolResult(success=True, content=f"No matching memories found for '{query}'.")
            return ToolResult(
                success=True,
                content=f"Found {len(results)} match(es) for '{query}':\n" + "\n".join(results),
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to search memory: {e}")
