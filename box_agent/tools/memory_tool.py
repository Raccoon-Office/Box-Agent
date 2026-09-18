"""Memory tools — read, write, and search long-term memory.

Provides persistent cross-session memory that survives beyond individual
sessions.  Core memory (MEMORY.md) is always injected; topic-sharded context
memory is searchable on demand.
"""

from __future__ import annotations

import asyncio
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
                "topic": {
                    "type": "string",
                    "description": (
                        "Optional topic label that buckets context entries into "
                        "per-topic files (e.g. 'preferences', 'project-x'). "
                        "Only used when category='context'. Defaults to 'general'."
                    ),
                },
            },
            "required": ["content"],
        }

    async def execute(self, content: str, category: str = "core", mode: str = "append", topic: str = "general") -> ToolResult:
        try:
            if category == "context":
                if mode == "overwrite":
                    await asyncio.to_thread(
                        self._mgr.write_context,
                        content,
                        topic=topic,
                    )
                    strategy = "overwrite"
                elif self._llm is not None:
                    strategy = await self._mgr.update_context_with_llm(content, self._llm, topic=topic)
                else:
                    await asyncio.to_thread(
                        self._mgr.append_context,
                        content,
                        topic=topic,
                    )
                    strategy = "append_dedup"
                current = await asyncio.to_thread(self._mgr.read_context)
                label = "context"
            else:
                if mode == "overwrite":
                    await asyncio.to_thread(self._mgr.write_core, content)
                else:
                    await asyncio.to_thread(self._mgr.append_core, content)
                current = await asyncio.to_thread(self._mgr.read_core)
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
            core, context = await asyncio.gather(
                asyncio.to_thread(self._mgr.read_core),
                asyncio.to_thread(self._mgr.read_context),
            )
            if not core and not context:
                return ToolResult(success=True, content="No long-term memory saved yet.")

            parts: list[str] = []
            if core:
                parts.append(f"[Core Memory (MEMORY.md)]\n{core}")
            if context:
                parts.append(f"[Context Memory]\n{context}")
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
            "Search topic-sharded context/experience memory by keyword. Use this "
            "when the current request may depend on saved user preferences, prior "
            "decisions, repo/workflow conventions, previous pitfalls, specific "
            "paths or errors, or recurring task experience. Skip it for clearly "
            "one-off simple questions. Prefer 1-3 short durable search keys "
            "instead of the full user sentence: project/product names, repo/module "
            "paths, exact errors, workflows, artifact types, or prior decisions. "
            "For compound Chinese requests, split stable noun phrases into "
            "separate searches. Core memory is always available — use this for "
            "everything else."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search key or short phrase (case-insensitive). Avoid full "
                        "sentences with action words; use stable nouns such as "
                        "project names, paths, errors, workflows, or artifact types."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Optional context topic to search, e.g. 'preferences', "
                        "'project', 'feedback', or 'general'. If omitted, the "
                        "memory index routes the query to relevant topics first."
                    ),
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, topic: str | None = None) -> ToolResult:
        try:
            results = await asyncio.to_thread(
                self._mgr.search,
                query,
                topic=topic,
            )
            if not results:
                topic_suffix = f" in topic '{topic}'" if topic else ""
                return ToolResult(
                    success=True,
                    content=f"No matching memories found for '{query}'{topic_suffix}.",
                    raw_output={
                        "type": "memory_search",
                        "query": query,
                        **({"topic": topic} if topic else {}),
                        "matched_memories": [],
                    },
                )
            topic_suffix = f" in topic '{topic}'" if topic else ""
            return ToolResult(
                success=True,
                content=f"Found {len(results)} match(es) for '{query}'{topic_suffix}:\n" + "\n".join(results),
                raw_output={
                    "type": "memory_search",
                    "query": query,
                    **({"topic": topic} if topic else {}),
                    "matched_memories": [
                        {
                            "id": f"context:{index}",
                            "source": "context",
                            "category": "context",
                            "text": line,
                        }
                        for index, line in enumerate(results, start=1)
                    ],
                },
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to search memory: {e}")


# ── Correction memory tools (side path; does not extend memory_write) ──


def _reject_preference_or_secret(lesson: str, symptom: str = "") -> str | None:
    from box_agent.correction import classify_forbidden_content

    return classify_forbidden_content(f"{lesson}\n{symptom}")


class MemoryListCorrectionsTool(Tool):
    """List stored correction-memory entries."""

    def __init__(self, memory_manager):
        from box_agent.memory import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_list_corrections"

    @property
    def description(self) -> str:
        return (
            "List correction-memory entries (durable failure lessons). "
            "By default returns active corrections only. Does not touch MEMORY.md."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "superseded", "draft", "deleted"],
                    "description": "Optional status filter. Omit for active-only.",
                },
                "include_inactive": {
                    "type": "boolean",
                    "description": "If true and status is omitted, include non-active corrections.",
                },
            },
        }

    async def execute(
        self,
        status: str | None = None,
        include_inactive: bool = False,
    ) -> ToolResult:
        try:
            entries = await asyncio.to_thread(
                self._mgr.list_corrections,
                status=status,
                include_inactive=include_inactive,
            )
            if not entries:
                return ToolResult(success=True, content="还没有可复用的纠错记忆。")
            lines = []
            visible_lines = []
            for e in entries:
                label = {"active": "可用", "draft": "未验证", "superseded": "不再适用", "deleted": "已忘记"}.get(e.status, "未验证")
                lesson = e.content.splitlines()[0].removeprefix("- lesson: ") if e.content else ""
                visible_lines.append(f"- {lesson}（适用：{e.subject_name}；{label}）")
                lines.append(
                    f"- id={e.id} 状态={e.status} "
                    f"对象={e.subject_kind}:{e.subject_name}"
                    f"{('@' + e.subject_version) if e.subject_version else ''} "
                    f"指纹={e.error_fingerprint}\n  {e.content}"
                )
            return ToolResult(
                success=True,
                content=f"共找到 {len(entries)} 条纠错记忆：\n" + "\n".join(visible_lines),
                model_context=f"共找到 {len(entries)} 条纠错记忆：\n" + "\n".join(lines),
                raw_output={
                    "type": "memory_list_corrections",
                    "corrections": [
                        {
                            "id": e.id,
                            "status": e.status,
                            "subject_kind": e.subject_kind,
                            "subject_name": e.subject_name,
                            "subject_version": e.subject_version,
                            "error_fingerprint": e.error_fingerprint,
                            "content": e.content,
                        }
                        for e in entries
                    ],
                },
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"列出纠错记忆失败：{e}")


class MemoryWriteCorrectionTool(Tool):
    """Remember a concrete remedy only after validating runtime success evidence."""

    def __init__(self, memory_manager):
        from box_agent.memory import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_write_correction"

    @property
    def description(self) -> str:
        return (
            "Write a durable correction (failure lesson) to corrections memory. "
            "Requires verification_id from a real successful retry and a specific reusable remedy. "
            "Validated remedies become available automatically; never ask the user to manage draft states. "
            "Refuses preference and secret content. Does not write MEMORY.md."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "verification_id": {
                    "type": "string",
                    "description": "Runtime-issued evidence id from a matching successful retry after changed arguments or a recorded file repair; required before storing a remedy.",
                },
                "lesson": {
                    "type": "string",
                    "description": "Specific repair steps and their applicability; not a generic suggestion to retry.",
                },
                "symptom": {
                    "type": "string",
                    "description": "Short symptom summary (no long stacks).",
                },
                "error_fingerprint": {
                    "type": "string",
                    "description": "Normalized error fingerprint (or raw error to normalize).",
                },
                "subject_kind": {
                    "type": "string",
                    "enum": ["skill", "tool"],
                },
                "subject_name": {"type": "string"},
                "subject_version": {"type": "string"},
            },
            "required": ["lesson", "error_fingerprint", "subject_name", "verification_id"],
        }

    async def execute(
        self,
        lesson: str = "",
        symptom: str = "",
        error_fingerprint: str = "",
        subject_kind: str = "tool",
        subject_name: str = "",
        subject_version: str = "",
        verification_id: str = "",
    ) -> ToolResult:
        try:
            forbidden = _reject_preference_or_secret(lesson, symptom)
            if forbidden:
                return ToolResult(
                    success=False,
                    content="",
                    error="这条不算可复用纠错（偏好/敏感/一次性），没记下。",
                )

            from box_agent.correction import (
                CorrectionDraft,
                CorrectionReject,
                CorrectionSubject,
                normalize_error_fingerprint,
            )

            if not lesson.strip():
                return ToolResult(success=False, content="", error="缺少 lesson（纠错经验）。")
            if not subject_name.strip():
                return ToolResult(success=False, content="", error="缺少 subject_name（适用对象名称）。")
            if not error_fingerprint.strip():
                return ToolResult(success=False, content="", error="缺少 error_fingerprint（错误指纹）。")

            subject = CorrectionSubject(
                kind=subject_kind,  # type: ignore[arg-type]
                name=subject_name.strip(),
                version=subject_version or "",
            )
            subject, verification = self._mgr.correction_curator.verification_for(
                verification_id, subject, error_fingerprint,
            )
            draft = CorrectionDraft(
                lesson=lesson.strip(),
                symptom=(symptom or "").strip(),
                subject=subject,
                error_fingerprint=normalize_error_fingerprint(error_fingerprint),
                source="tool",
                verification=verification,
            )
            try:
                from box_agent.correction import CorrectionCurator

                CorrectionCurator().reject_if_forbidden(draft)
            except CorrectionReject:
                return ToolResult(
                    success=False,
                    content="",
                    error="这条不算可复用纠错（偏好/敏感/一次性），没记下。",
                )

            entry = await asyncio.to_thread(self._mgr.remember_verified_correction, draft)
            return ToolResult(
                success=True,
                content=f"已记住这个解决办法，下次遇到相同情况会提前提醒。\n解决办法：{lesson.strip()}",
                raw_output={"type": "memory_write_correction", "id": entry.id, "status": entry.status},
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"写入纠错记忆失败：{e}")


class MemorySupersedeCorrectionTool(Tool):
    """Invalidate/supersede a correction so default search skips it."""

    def __init__(self, memory_manager):
        from box_agent.memory import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_supersede_correction"

    @property
    def description(self) -> str:
        return (
            "Supersede (invalidate) a correction by id. Superseded corrections "
            "are hidden from default memory_search."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "Correction entry id to supersede.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason (e.g. 'fixed', 'obsolete').",
                },
            },
            "required": ["entry_id"],
        }

    async def execute(self, entry_id: str, reason: str = "fixed") -> ToolResult:
        try:
            entry = await asyncio.to_thread(
                self._mgr.supersede_correction,
                entry_id,
                reason=reason,
            )
            return ToolResult(
                success=True,
                content="这条解决办法已标记为不再适用。",
                model_context=f"Correction superseded: id={entry.id} status={entry.status}",
                raw_output={"type": "memory_supersede_correction", "id": entry.id, "status": entry.status},
            )
        except KeyError:
            return ToolResult(success=False, content="", error=f"未找到纠错记忆：{entry_id}")
        except Exception as e:
            return ToolResult(success=False, content="", error=f"作废纠错记忆失败：{e}")


class MemoryDeleteCorrectionTool(Tool):
    """Soft-delete a correction (status=deleted)."""

    def __init__(self, memory_manager):
        from box_agent.memory import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_delete_correction"

    @property
    def description(self) -> str:
        return (
            "Soft-delete a correction by id (status=deleted). Deleted corrections "
            "are hidden from default memory_search."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "Correction entry id to delete.",
                },
            },
            "required": ["entry_id"],
        }

    async def execute(self, entry_id: str) -> ToolResult:
        try:
            entry = await asyncio.to_thread(self._mgr.delete_correction, entry_id)
            return ToolResult(
                success=True,
                content="已忘掉这条解决办法。",
                model_context=f"Correction deleted: id={entry.id} status={entry.status}",
                raw_output={"type": "memory_delete_correction", "id": entry.id, "status": entry.status},
            )
        except KeyError:
            return ToolResult(success=False, content="", error=f"未找到纠错记忆：{entry_id}")
        except Exception as e:
            return ToolResult(success=False, content="", error=f"删除纠错记忆失败：{e}")
