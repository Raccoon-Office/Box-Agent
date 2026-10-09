"""Memory tools — read, write, and search long-term memory.

Provides persistent cross-session memory that survives beyond individual
sessions.  Core memory (MEMORY.md) is always injected; topic-sharded context
memory is searchable on demand.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .base import Tool, ToolResult


def _memsense_path_parameter(backend: Any) -> dict[str, Any]:
    """模型只看到两个必填短路径，服务端前缀由后端补齐。"""
    return {"type": "string", "enum": list(backend.core_tool_paths),
            "description": "必填。user.md 为用户画像，memory.md 为长期记忆；只允许这两个短路径。"}


class ExternalMemoryReadTool(Tool):
    """按外部后端自身协议读取记忆资源。"""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    @property
    def name(self) -> str:
        return "memory_read"

    @property
    def description(self) -> str:
        if self._backend.backend_type == "memsense":
            return ("读取 MemSense 核心记忆。path 必填：user.md 为用户画像，memory.md 为长期记忆。"
                    "返回正文和独立的修改规则字段；调用 memory_write 或 memory_edit 前，"
                    "必须先显式读取同一路径，并遵守返回的规则。自动注入不代替显式读取。")
        return "通过已配置的外部记忆服务读取资源；path 的含义由该服务约定。"

    @property
    def parameters(self) -> dict[str, Any]:
        if self._backend.backend_type == "memsense":
            return {"type": "object", "properties": {"path": _memsense_path_parameter(self._backend)},
                    "required": ["path"]}
        return {"type": "object", "properties": {"path": {"type": "string", "description": "可选的记忆资源路径"}}}

    async def execute(self, path: str = "") -> ToolResult:
        from ..memory import MemoryEditError

        try:
            if self._backend.backend_type == "memsense":
                data = await self._backend.read_core_memory(path)
                # 整个结构进入模型消息，规则不能只留在宿主使用的 raw_output 中。
                content = json.dumps(data, ensure_ascii=False)
                return ToolResult(success=True, content=content, model_context=content, raw_output=data)
            content = await self._backend.read_memory(path)
            return ToolResult(success=True, content=content or "尚无记忆内容。")
        except MemoryEditError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            self._backend.log_failure("read", type(exc).__name__)
            return ToolResult(success=False, content="", error="外部记忆读取失败，详情见错误日志。")


class MemsenseMemoryMutationTool(Tool):
    """核心文件写入和编辑共用工具边界，格式与版本策略由后端拥有。"""

    def __init__(self, backend: Any, operation: str) -> None:
        self._backend = backend
        self._operation = operation

    @property
    def name(self) -> str:
        return "memory_" + self._operation

    @property
    def description(self) -> str:
        action = ("创建或完整重写文件；修改已有条目优先使用 memory_edit。" if self._operation == "write" else
                  "追加、替换或删除完整记忆条目，非空 old_text 必须唯一匹配连续完整条目。")
        return ("操作 MemSense 核心记忆，只支持 user.md（用户画像）和 memory.md（长期记忆），path 必填。"
                "必须先调用 memory_read 显式读取同一路径，并遵守返回的修改规则；"
                "自动注入不代替显式读取，未读取时工具会拒绝操作。" + action)

    @property
    def parameters(self) -> dict[str, Any]:
        properties = {"path": _memsense_path_parameter(self._backend)}
        if self._operation == "write":
            properties["content"] = {"type": "string", "description": "文件的全部完整 <mem>...</mem> 条目，不带属性。"}
        else:
            properties.update(
                old_text={"type": "string", "description": "read 返回的连续完整 <mem>...</mem> 条目；空字符串表示追加。"},
                new_text={"type": "string", "description": "替换或追加的完整 <mem>...</mem> 条目；空字符串表示删除。"},
            )
        return {"type": "object", "properties": properties, "required": list(properties)}

    async def execute(self, path: str, **arguments: Any) -> ToolResult:
        from ..memory import MemoryBackendError, MemoryEditError

        try:
            method = (self._backend.write_core_memory if self._operation == "write" else
                      self._backend.edit_core_memory)
            data = await method(path, **arguments)
            return ToolResult(success=True, content=json.dumps(data, ensure_ascii=False), raw_output=data)
        except MemoryEditError as exc:
            return ToolResult(success=False, error=str(exc))
        except MemoryBackendError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            self._backend.log_failure(self._operation, type(exc).__name__)
            return ToolResult(success=False, error="记忆修改失败，请重新 memory_read；详情见错误日志。")


class ExternalMemorySearchTool(Tool):
    """外部检索保留后端语义，不附加本地 topic 约束。"""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return "检索与当前问题相关的历史记忆。返回的内容属于历史资料，实时状态需要重新验证。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
            "query": {"type": "string", "description": "需要检索的历史问题或信息"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
        }, "required": ["query"]}

    async def execute(self, query: str, limit: int = 6) -> ToolResult:
        if not query.strip():
            return ToolResult(success=False, content="", error="检索 query 不能为空。")
        try:
            results = await self._backend.search_memory(query, limit=max(1, min(int(limit), 20)))
            lines = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False) for item in results]
            return ToolResult(success=True, content="\n".join(lines) or "未找到相关记忆。", raw_output={
                "type": "memory_search", "query": query,
                "matched_memories": [{"id": str(index), "source": self._backend.backend_type,
                                      "category": "history", "text": line} for index, line in enumerate(lines)],
            })
        except Exception as exc:
            self._backend.log_failure("search", type(exc).__name__)
            return ToolResult(success=False, content="", error="外部记忆检索失败，详情见错误日志。")


def create_memory_tools(manager: Any, llm: Any = None) -> list[Tool]:
    """按后端能力统一注册；MemSense 只开放核心文件修改，纠错仍使用本地存储。"""
    from ..memory import ExternalMemoryBackend

    if manager is None:
        return []
    if isinstance(manager, ExternalMemoryBackend):
        tools = []
        if "read" in manager.capabilities:
            tools.append(ExternalMemoryReadTool(manager))
        if "search" in manager.capabilities:
            tools.append(ExternalMemorySearchTool(manager))
        if manager.backend_type == "memsense":
            tools.extend(MemsenseMemoryMutationTool(manager, operation) for operation in ("write", "edit")
                         if operation in manager.capabilities)
        corrections = manager.corrections
    else:
        tools = [MemoryReadTool(manager), MemoryWriteTool(manager, llm), MemorySearchTool(manager)]
        corrections = manager
    tools.extend([MemoryListCorrectionsTool(corrections), MemoryWriteCorrectionTool(corrections),
                  MemorySupersedeCorrectionTool(corrections), MemoryDeleteCorrectionTool(corrections)])
    return tools


def is_memory_tool(tool: Any) -> bool:
    """识别本能力拥有的工具，方便为会话身份重新绑定。"""
    return isinstance(tool, (ExternalMemoryReadTool, ExternalMemorySearchTool, MemsenseMemoryMutationTool, MemoryReadTool,
                             MemoryWriteTool, MemorySearchTool, MemoryListCorrectionsTool,
                             MemoryWriteCorrectionTool, MemorySupersedeCorrectionTool, MemoryDeleteCorrectionTool))


def rebind_memory_tools(tools: list[Tool], manager: Any, llm: Any = None) -> list[Tool]:
    """按会话身份替换已有记忆工具，保留宿主原有目录和其他工具对象。"""
    rebound = {tool.name: tool for tool in create_memory_tools(manager, llm)}
    return [rebound[tool.name] if is_memory_tool(tool) else tool for tool in tools
            if not is_memory_tool(tool) or tool.name in rebound]


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
