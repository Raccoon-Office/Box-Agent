"""Session-local model-context resource tracking.

Only complete read-file messages that still exist verbatim in model history
contribute coverage.  Receipts are diagnostic references, never coverage
sources.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .model_history import is_model_instruction_source_path
from .schema import Message


CONTEXT_RESOURCE_RAW_KEY = "context_resource"
MAX_RESOURCE_RECEIPT_CHARS = 300


class ResourceClass(str, Enum):
    """Retention class for content placed in model history."""

    INSTRUCTION_PINNED = "instruction_pinned"
    RECONSTRUCTABLE = "reconstructable"
    NON_RECONSTRUCTABLE = "non_reconstructable"


@dataclass(frozen=True, slots=True)
class ResourceDescriptor:
    """Stable identity and actual line range returned by ``read_file``."""

    resource_id: str
    content_version: str
    start_line: int
    end_line: int
    total_lines: int
    resource_class: ResourceClass

    @property
    def has_content(self) -> bool:
        return self.start_line > 0 and self.end_line >= self.start_line

    def as_raw_output(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "content_version": self.content_version,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "total_lines": self.total_lines,
            "resource_class": self.resource_class.value,
        }

    @classmethod
    def from_raw_output(cls, raw_output: object) -> ResourceDescriptor | None:
        if not isinstance(raw_output, dict):
            return None
        payload = raw_output.get(CONTEXT_RESOURCE_RAW_KEY)
        if not isinstance(payload, dict):
            return None
        try:
            resource_id = str(payload["resource_id"])
            content_version = str(payload["content_version"])
            start_line = int(payload["start_line"])
            end_line = int(payload["end_line"])
            total_lines = int(payload["total_lines"])
            resource_class = ResourceClass(str(payload["resource_class"]))
        except (KeyError, TypeError, ValueError):
            return None
        if not resource_id or len(content_version) != 64 or total_lines < 0:
            return None
        return cls(
            resource_id=resource_id,
            content_version=content_version,
            start_line=start_line,
            end_line=end_line,
            total_lines=total_lines,
            resource_class=resource_class,
        )


@dataclass(frozen=True, slots=True)
class ContextResourceSource:
    """One complete model-history message that can provide coverage."""

    tool_call_id: str
    descriptor: ResourceDescriptor
    model_content_hash: str


@dataclass(frozen=True, slots=True)
class HistoryTransformResult:
    """Metadata returned by a deterministic model-history transformation."""

    transformed_count: int = 0
    replaced_source_tool_call_ids: tuple[str, ...] = ()
    estimated_before: int | None = None
    estimated_after: int | None = None


def classify_read_resource(
    path: str,
    *,
    requested_path: str | None = None,
    builtin_skills_dir: Path | None = None,
) -> ResourceClass:
    """Classify a successfully read path conservatively."""
    if is_model_instruction_source_path(path, builtin_skills_dir=builtin_skills_dir) or is_model_instruction_source_path(
        requested_path
    ):
        return ResourceClass.INSTRUCTION_PINNED
    return ResourceClass.RECONSTRUCTABLE


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


class ContextResourceLedger:
    """Track complete read-file sources for one live model context."""

    def __init__(self) -> None:
        self.epoch = 0
        self._sources: dict[str, ContextResourceSource] = {}
        self._receipts: dict[str, tuple[str, ...]] = {}
        self._refreshed_versions: set[tuple[str, str]] = set()

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(self._sources)

    @property
    def receipt_ids(self) -> tuple[str, ...]:
        return tuple(self._receipts)

    def source(self, tool_call_id: str) -> ContextResourceSource | None:
        return self._sources.get(tool_call_id)

    def register_full_source(
        self,
        tool_call_id: str,
        descriptor: ResourceDescriptor,
        model_content: str,
    ) -> None:
        """Register content only after its complete body entered history."""
        if not tool_call_id or not descriptor.has_content:
            return
        self._receipts.pop(tool_call_id, None)
        self._sources[tool_call_id] = ContextResourceSource(
            tool_call_id=tool_call_id,
            descriptor=descriptor,
            model_content_hash=_content_hash(model_content),
        )

    def register_receipt(
        self,
        tool_call_id: str,
        source_tool_call_ids: Iterable[str],
    ) -> None:
        """Record a receipt reference without adding coverage."""
        sources = tuple(dict.fromkeys(source_tool_call_ids))
        if not tool_call_id or not sources:
            return
        self._sources.pop(tool_call_id, None)
        self._receipts[tool_call_id] = sources

    def invalidate_source_ids(self, tool_call_ids: Iterable[str]) -> tuple[str, ...]:
        invalidated: list[str] = []
        for tool_call_id in dict.fromkeys(tool_call_ids):
            if self._sources.pop(tool_call_id, None) is not None:
                invalidated.append(tool_call_id)
        return tuple(invalidated)

    def reconcile(self, messages: list[Message]) -> tuple[str, ...]:
        """Remove sources whose complete message no longer survives verbatim."""
        tool_messages = {
            message.tool_call_id: message
            for message in messages
            if message.role == "tool" and message.tool_call_id
        }
        stale_sources = []
        for tool_call_id, source in self._sources.items():
            message = tool_messages.get(tool_call_id)
            content = message.content if message is not None else None
            if (
                message is None
                or message.name != "read_file"
                or not isinstance(content, str)
                or _content_hash(content) != source.model_content_hash
            ):
                stale_sources.append(tool_call_id)
        invalidated = self.invalidate_source_ids(stale_sources)
        for receipt_id in tuple(self._receipts):
            if receipt_id not in tool_messages:
                self._receipts.pop(receipt_id, None)
        return invalidated

    def covering_source_ids(
        self,
        descriptor: ResourceDescriptor,
        messages: list[Message],
    ) -> tuple[str, ...]:
        """Return live sources whose interval union covers the descriptor."""
        self.reconcile(messages)
        if not descriptor.has_content:
            return ()
        candidates = sorted(
            (
                source
                for source in self._sources.values()
                if source.descriptor.resource_id == descriptor.resource_id
                and source.descriptor.content_version == descriptor.content_version
            ),
            key=lambda source: (
                source.descriptor.start_line,
                -source.descriptor.end_line,
                source.tool_call_id,
            ),
        )
        cursor = descriptor.start_line
        selected: list[str] = []
        for source in candidates:
            interval = source.descriptor
            if interval.end_line < cursor:
                continue
            if interval.start_line > cursor:
                break
            selected.append(source.tool_call_id)
            cursor = interval.end_line + 1
            if cursor > descriptor.end_line:
                return tuple(selected)
        return ()

    def claim_refresh_reload(self, descriptor: ResourceDescriptor) -> bool:
        """Allow one forced reload for an unchanged resource version.

        ``read_file(refresh=true)`` must still verify the file on disk, but
        repeatedly placing the same covered bytes back into model history can
        create unbounded read loops. The first refresh for a resource version
        remains a full reload; later covered refreshes use a receipt until the
        content version changes or the context epoch is rotated.
        """
        key = (descriptor.resource_id, descriptor.content_version)
        if key in self._refreshed_versions:
            return False
        self._refreshed_versions.add(key)
        return True

    def rotate_epoch(self) -> None:
        """Invalidate all coverage after a whole-history rewrite."""
        self.epoch += 1
        self._sources.clear()
        self._receipts.clear()
        self._refreshed_versions.clear()


def _safe_resource_label(resource_id: str, limit: int = 112) -> str:
    normalized = resource_id.replace("\r", "\\r").replace("\n", "\\n")
    if len(normalized) <= limit:
        return normalized
    return "..." + normalized[-(limit - 3) :]


def build_resource_receipt(
    descriptor: ResourceDescriptor,
    source_tool_call_ids: Iterable[str],
    *,
    refresh_unchanged: bool = False,
) -> str:
    """Build a bounded receipt that never claims to be a coverage source."""
    sources = ",".join(tuple(source_tool_call_ids)[:3])
    guidance = (
        "Refresh checked the file and its content version is unchanged. "
        "Reuse the sources above; reread only after the file changes."
        if refresh_unchanged
        else "Use refresh=true once if exact content must be reloaded."
    )
    text = (
        "[Resource already available in current model context]\n"
        f"Path: {_safe_resource_label(descriptor.resource_id)}\n"
        f"Version: {descriptor.content_version[:12]} Lines: "
        f"{descriptor.start_line}-{descriptor.end_line}\n"
        f"Sources: {sources}\n{guidance}"
    )
    return text[:MAX_RESOURCE_RECEIPT_CHARS]


def build_resource_shedding_placeholder(source: ContextResourceSource) -> str:
    """Describe reconstructable content removed under context pressure."""
    descriptor = source.descriptor
    return (
        "[Reconstructable read_file content shed from model history]\n"
        f"Path: {_safe_resource_label(descriptor.resource_id)}\n"
        f"Version: {descriptor.content_version[:12]} Lines: "
        f"{descriptor.start_line}-{descriptor.end_line}\n"
        "Call read_file again to restore exact content."
    )[:MAX_RESOURCE_RECEIPT_CHARS]
