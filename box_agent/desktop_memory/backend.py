"""Desktop-owned memory backend with immutable per-turn host authorization."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

from ..config import DesktopMemoryConfig, ExternalMemoryConfig
from ..memory import MemsenseMemoryBackend, MemoryEditError
from ..tools.base import check_tool_cancellation
from .transport import MemoryHostTransport, MemoryProxyError
from .visibility import CoreRead, DesktopInputGuard


def model_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True, slots=True, repr=False)
class TurnScope:
    binding: Mapping[str, Any]
    qa_event_id: str | None

    @property
    def learnable(self) -> bool:
        return (self.binding["source"] == "cloud" and self.binding["automaticAtStart"]
                and self.binding["onlineAtStart"] and not self.binding.get("parentTurnId")
                and self.qa_event_id is not None)


def _scope(data: dict[str, Any], epoch: str, session: str, turn: str) -> TurnScope:
    binding = data.get("binding")
    required = {"accountKey", "officeIdentity", "identityEpoch", "sessionId", "turnId", "source",
                "memoryGeneration", "automaticAtStart", "onlineAtStart", "automaticRevision"}
    if not isinstance(binding, dict) or not required <= binding.keys() or binding.keys() - required - {"subject", "parentTurnId"}:
        raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE")
    if (binding["identityEpoch"] != epoch or binding["sessionId"] != session or binding["turnId"] != turn
            or not isinstance(binding["source"], str) or binding["source"] not in {"legacy_read_only", "cloud", "unavailable"}
            or any(not _identifier(binding[key]) for key in ("accountKey", "officeIdentity", "sessionId", "turnId"))
            or any(type(binding[key]) is not bool for key in ("automaticAtStart", "onlineAtStart"))
            or any(type(binding[key]) is not int or not 0 <= binding[key] <= 9007199254740991
                   for key in ("memoryGeneration", "automaticRevision"))
            or any(not _identifier(binding[key]) for key in ("subject", "parentTurnId") if key in binding)
            or binding["source"] == "cloud" and (not binding.get("subject") or binding["memoryGeneration"] < 1)):
        raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE")
    event = data.get("qa_event_id")
    if event is not None and not _uuid(event):
        raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE")
    return TurnScope(MappingProxyType(dict(binding)), event)


_SECTIONS = (("user_items", "用户维护记忆", 6000), ("profile", "用户画像", 2000),
             ("automatic", "自动长期记忆", 3000), ("dates", "近期日期内容（UTC）", 2000),
             ("experience", "本轮相关经验", 3000))


def _format_sections(sections: Any, scope: TurnScope, *, max_chars: int = 15400, trim_priority: bool = False) -> str:
    if not isinstance(sections, dict) or sections.keys() - {item[0] for item in _SECTIONS}:
        raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE")
    if not scope.binding["automaticAtStart"] or scope.binding["source"] == "unavailable":
        return ""
    pieces = []
    section_keys = []
    for key, label, weight in _SECTIONS:
        view = sections.get(key)
        if view is None or key == "experience" and (scope.binding["source"] != "cloud" or not scope.binding["onlineAtStart"]):
            continue
        if (not isinstance(view, dict) or not isinstance(view.get("content"), str)
                or not isinstance(view.get("status", "ok"), str) or view.get("status", "ok") not in {"ok", "stale", "missing", "error"}
                or "revision" in view and (not isinstance(view["revision"], str) or len(view["revision"]) > 256)):
            raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE")
        text = view["content"]
        if not text or view.get("status") in {"missing", "error"}:
            continue
        revision = view.get("revision", "")
        header = "## " + label + (" [stale]" if view.get("status") == "stale" else "")
        if revision:
            header += " [" + revision.replace("\n", " ").replace("\r", " ") + "]"
        pieces.append((header + "\n", text, weight))
        section_keys.append(key)
    if not pieces:
        return ""
    available = max(0, max_chars - sum(len(header) for header, _, _ in pieces) - 2 * (len(pieces) - 1))
    allocations = [min(len(text), available * weight // 16000) for _, text, weight in pieces]
    if trim_priority:
        allocations = [min(len(text), weight) for _, text, weight in pieces]
        excess = max(0, sum(allocations) - available)
        for key in ("experience", "dates", "automatic", "profile", "user_items"):
            if key in section_keys:
                index = section_keys.index(key)
                removed = min(excess, allocations[index])
                allocations[index] -= removed
                excess -= removed
    remaining = available - sum(allocations)
    for index, (_, text, _) in enumerate(pieces):
        extra = min(remaining, len(text) - allocations[index])
        allocations[index] += extra
        remaining -= extra
    result = []
    suffix = "…[片段，需工具续读]"
    for (header, text, _), size in zip(pieces, allocations):
        if size == 0:
            continue
        if len(text) > size:
            text = text[:size - len(suffix)] + suffix if size >= len(suffix) else text[:size]
        result.append(header + text)
    return "\n\n".join(result)


class DesktopMemoryBackend(MemsenseMemoryBackend):
    """Reuse core-format helpers, never generic HTTP or a writable local manager."""

    backend_type = "desktop"

    def __init__(self, config: DesktopMemoryConfig) -> None:
        # Deliberately do not call ExternalMemoryBackend: it owns local corrections.
        self.desktop_config = config.model_copy(deep=True)
        self.config = ExternalMemoryConfig(context_max_chars=15400, max_retries=0)
        self.tenant_id = self.user_id = "desktop-host"
        self.corrections = None
        self.log_context: dict[str, str] = {}
        self.capabilities = frozenset({"recall", "read", "search", "save", "write", "edit"})
        self.transport = MemoryHostTransport(Path(config.bootstrap_file), config.identity_epoch, on_revoked=self.end_turn)
        self.model_input_guard = DesktopInputGuard(self._visibility, self._preload)
        self.current_scope: TurnScope | None = None
        self._sections: dict[str, Any] = {}
        self._serial = 0
        self._core_file_locks = {path: asyncio.Lock() for path in self.core_tool_paths}
        self._records: dict[str, tuple[CoreRead, TurnScope, dict[str, Any]]] = {}
        self._control_tail: asyncio.Task | None = None
        self._control_tasks: set[asyncio.Task] = set()
        self._closed = False

    def clone_for_session(self) -> DesktopMemoryBackend:
        return type(self)(self.desktop_config)

    def create_runtime(self):
        from .runtime import DesktopMemoryRuntime

        return DesktopMemoryRuntime(self)

    @property
    def correction_curator(self) -> None:
        return None

    def recall_corrections(self, *args: Any, **kwargs: Any) -> list:
        return []

    async def _post(self, *args: Any, **kwargs: Any) -> Any:
        raise MemoryProxyError("INVALID_OPERATION")

    def _binding(self, *, cloud: bool = False) -> TurnScope:
        scope = self.current_scope
        if self._closed or scope is None:
            raise MemoryProxyError("TURN_REVOKED")
        if not scope.binding["automaticAtStart"]:
            raise MemoryProxyError("MEMORY_DISABLED")
        if scope.binding["source"] == "unavailable":
            raise MemoryProxyError("MEMORY_NOT_READY")
        if cloud and scope.binding["source"] != "cloud":
            raise MemoryProxyError("MEMORY_READ_ONLY")
        if cloud and not scope.binding["onlineAtStart"]:
            raise MemoryProxyError("MEMORY_OFFLINE")
        return scope

    async def load_context(self, *, query: str, **context: Any) -> str:
        self.model_input_guard.reset()
        self._records.clear()
        self.current_scope = None
        self._sections = {}
        if not getattr(self, "context_attempt_durable", True):
            raise MemoryProxyError("MEMORY_ATTEMPT_NOT_DURABLE")
        self._serial += 1
        serial = self._serial
        session, turn = context.get("session_id"), context.get("turn_id")
        if not _identifier(session) or not _identifier(turn) or not isinstance(query, str):
            raise MemoryProxyError("INVALID_ARGUMENT")
        data = await self.transport.request("context", {"session_id": session, "turn_id": turn, "query": query,
                                                        "require_existing": getattr(self, "context_requires_existing", False)})
        if serial != self._serial or self._closed:
            raise MemoryProxyError("TURN_REVOKED")
        scope = _scope(data, self.desktop_config.identity_epoch, session, turn)
        content = _format_sections(data.get("sections"), scope)
        self.current_scope = scope
        self._sections = data["sections"]
        return content

    def _preload(self, limit: int) -> str:
        if self.current_scope is None or limit < 1:
            return ""
        text = _format_sections(self._sections, self.current_scope, max_chars=limit, trim_priority=True)
        for marker in ("--- MEMORY START ---", "--- MEMORY END ---", "{MEMORY_CONTEXT}",
                       "--- EXTERNAL MEMORY START ---", "--- EXTERNAL MEMORY END ---"):
            text = text.replace(marker, "[记忆边界]")
        return text

    def _visibility(self, read: CoreRead, active: bool, request_id: str) -> asyncio.Task | None:
        prior = self._records.get(read.read_id)
        if prior is None or self._closed:
            if active:
                raise MemoryProxyError("CORE_READ_REQUIRED")
            return None
        _, scope, _ = prior
        payload = {"binding": dict(scope.binding), "path": read.path, "read_id": read.read_id,
                   "content_sha256": read.content_sha256, "model_request_id": request_id}
        previous = self._control_tail

        async def send():
            if previous is not None:
                await asyncio.gather(asyncio.shield(previous), return_exceptions=True)
            result = await self.transport.request("visibility/commit" if active else "visibility/revoke", payload)
            if result.get("read_id") != read.read_id or result.get("state") != ("active" if active else "revoked"):
                raise MemoryProxyError("CORE_READ_REQUIRED")
            return result

        task = asyncio.create_task(send(), name="memory-visibility")
        self._control_tail = task
        self._control_tasks.add(task)

        def finished(done):
            self._control_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Failure is still raised by guard.require.

        task.add_done_callback(finished)
        return task

    @staticmethod
    def _record(data: dict[str, Any], path: str) -> dict[str, Any]:
        content = data.get("content")
        exists = data.get("exists")
        if (not isinstance(content, str) or len(content.encode("utf-8")) > 32 * 1024 * 1024
                or type(exists) is not bool or data.get("path") != "memory://" + path):
            raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE")
        revision = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest() if exists else "absent"
        if data.get("revision") != revision or not exists and content:
            raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE")
        return {"content": content, "exists": exists, "revision": revision}

    async def read_core_memory(self, path: str) -> dict[str, Any]:
        self._validate_core_tool_path(path)
        scope = self._binding(cloud=True)
        async with self._core_file_locks[path]:
            self.model_input_guard.forget(path)
            for key, prior in tuple(self._records.items()):
                if prior[0].path == path:
                    self._records.pop(key)
            data = await self.transport.request("core/read", {"binding": dict(scope.binding), "path": path})
            if self.current_scope is not scope:
                raise MemoryProxyError("TURN_REVOKED")
            record = self._record(data, path)
            if not _uuid(data.get("read_id")) or data["read_id"] in self._records:
                raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE")
            model = {"path": path, **record, "read_id": data["read_id"], **self._core_rule_fields(path)}
            read = CoreRead(path, data["read_id"], hashlib.sha256(record["content"].encode()).hexdigest(), model_json(model))
            self._records[read.read_id] = (read, scope, record)
            try:
                self.model_input_guard.admit(read)
            except BaseException:
                self._records.pop(read.read_id, None)
                raise
            return model

    async def _update_core_memory(self, path: str, operation: str, *, content: str = "", old_text: str = "",
                                  new_text: str = "") -> dict[str, Any]:
        self._validate_core_tool_path(path)
        scope = self._binding(cloud=True)
        async with self._core_file_locks[path]:
            read = await self.model_input_guard.require(path)
            prior = self._records.get(read.read_id)
            if prior is None or prior[1] is not scope or self.current_scope is not scope:
                raise MemoryProxyError("CORE_READ_REQUIRED")
            record = prior[2]
            if operation == "edit" and not record["exists"]:
                raise MemoryEditError("文件不存在，请用 memory_write 创建。")
            updated = (self._rewrite_core_content(record["content"], content) if operation == "write" else
                       self._edit_core_content(record["content"], self._ENTRY_OPEN_RE.sub("<mem>", old_text), new_text))
            if len(updated.encode("utf-8")) > 32 * 1024 * 1024:
                raise MemoryProxyError("BUDGET_EXCEEDED")
            check_tool_cancellation()
            self.model_input_guard.consume(path)
            try:
                data = await self.transport.request("core/mutate", {
                    "binding": dict(scope.binding), "path": path, "read_id": read.read_id,
                    "base_revision": record["revision"], "content": updated,
                })
                expected_hash = hashlib.sha256(updated.encode("utf-8")).hexdigest()
                if (data.get("path") != "memory://" + path or data.get("exists") is not True
                        or data.get("content_sha256") != expected_hash or data.get("revision") != "sha256:" + expected_hash):
                    raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE", outcome="unknown")
                if self.current_scope is not scope:
                    raise MemoryProxyError("TURN_REVOKED", outcome="unknown")
                return {"path": path, "operation": operation, "exists": True, "revision": data["revision"]}
            finally:
                self._visibility(read, False, "")
                self._records.pop(read.read_id, None)

    async def search_memory(self, query: str, *, limit: int = 6) -> list[Any]:
        scope = self._binding()
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 20:
            raise MemoryProxyError("INVALID_ARGUMENT")
        data = await self.transport.request("search", {"binding": dict(scope.binding), "query": query, "limit": limit})
        if self.current_scope is not scope or not isinstance(data.get("results"), list):
            raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE")
        return data["results"]

    async def managed_read(self, entry_ref: str, version: str, offset: int = 0, limit: int = 2000) -> dict[str, Any]:
        scope = self._binding()
        if (not _identifier(entry_ref) or not _identifier(version) or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 4000):
            raise MemoryProxyError("INVALID_ARGUMENT")
        data = await self.transport.request("managed/read", {"binding": dict(scope.binding), "entry_ref": entry_ref,
                                                             "version": version, "offset": offset, "limit": limit})
        if self.current_scope is not scope:
            raise MemoryProxyError("TURN_REVOKED")
        return data

    async def experience_search(self, query: str, limit: int = 6) -> dict[str, Any]:
        scope = self._binding(cloud=True)
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 20:
            raise MemoryProxyError("INVALID_ARGUMENT")
        data = await self.transport.request("experience/search", {"binding": dict(scope.binding), "query": query, "limit": limit})
        if self.current_scope is not scope:
            raise MemoryProxyError("TURN_REVOKED")
        return data

    async def handoff(self, scope: TurnScope, user: str, assistant: str, timestamp: int,
                      *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        if not scope.learnable:
            raise MemoryProxyError("MEMORY_READ_ONLY")
        if not user.strip() or not assistant.strip() or len((user + assistant).encode()) > 256 * 1024:
            raise MemoryProxyError("BUDGET_EXCEEDED")
        payload = {"binding": dict(scope.binding), "event_id": scope.qa_event_id,
                   "user": user, "assistant": assistant, "timestamp": timestamp}
        if evidence is not None:
            payload["evidence"] = evidence
        data = await self.transport.request("qa/evidence" if evidence is not None else "qa", payload)
        if (data.get("event_id") != scope.qa_event_id or data.get("state") not in {"accepted", "durable_unknown"}
                or data.get("state") == "accepted" and not _identifier(data.get("source_id"))):
            raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE", outcome="unknown")
        return data

    async def close_turn(self, scope: TurnScope) -> None:
        try:
            await self.transport.request("turn/close", {"binding": dict(scope.binding)})
        except Exception:
            self.log_failure("turn_close", "unconfirmed")

    def end_turn(self) -> None:
        self.model_input_guard.reset()
        self._records.clear()
        self.current_scope = None
        self._sections = {}
        self._serial += 1

    async def aclose(self) -> None:
        self.end_turn()
        self._closed = True
        tasks = tuple(self._control_tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=3)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.transport.close()
