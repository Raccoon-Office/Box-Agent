"""Memory Tool - Let agent read and write long-term memory.

Provides persistent cross-session memory that survives beyond individual
sessions (unlike SessionNoteTool which is workspace-scoped).
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolResult


class MemoryWriteTool(Tool):
    """Tool for writing entries to long-term memory (MEMORY.md)."""

    def __init__(self, memory_manager):
        from box_agent.memory import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Write to long-term memory that persists across sessions. "
            "Use this to save user preferences, important facts, project context, "
            "or anything that should be remembered in future sessions. "
            "The content is appended to existing memory. "
            "To replace all memory, set mode to 'overwrite'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The content to write to memory. Use markdown bullet points for structured notes, "
                        "e.g. '- User prefers Chinese responses\\n- Project uses React'"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "overwrite"],
                    "description": "Write mode: 'append' adds to existing memory (default), 'overwrite' replaces all memory.",
                },
            },
            "required": ["content"],
        }

    async def execute(self, content: str, mode: str = "append") -> ToolResult:
        try:
            if mode == "overwrite":
                self._mgr.write_manual_memory(content)
            else:
                existing = self._mgr.read_manual_memory()
                if existing:
                    new_content = f"{existing}\n{content}"
                else:
                    new_content = content
                self._mgr.write_manual_memory(new_content)

            return ToolResult(
                success=True,
                content=f"Memory updated ({mode}). Current memory:\n{self._mgr.read_manual_memory()}",
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to write memory: {e}")


class MemoryReadTool(Tool):
    """Tool for reading long-term memory."""

    def __init__(self, memory_manager):
        from box_agent.memory import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_read"

    @property
    def description(self) -> str:
        return (
            "Read long-term memory that persists across sessions. "
            "Returns all saved memory entries. Use this to check what has been remembered."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self) -> ToolResult:
        try:
            content = self._mgr.read_manual_memory()
            if not content:
                return ToolResult(success=True, content="No long-term memory saved yet.")
            return ToolResult(success=True, content=f"Long-term memory:\n{content}")
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to read memory: {e}")
