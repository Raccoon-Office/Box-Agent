"""Validated semantic history supplied when a host recreates an ACP session."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import logging
import os
from pathlib import Path
import re
import shutil
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session_log import SessionLog


SCHEMA_VERSION = "officev3-session-continuation/v1"
MAX_MESSAGES = 12
MAX_MESSAGE_CHARS = 12_000
MAX_TOTAL_CHARS = 48_000
_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContinuationMessage:
    role: str
    content: str
    image_refs: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SessionContinuation:
    product_session_id: str
    reason: str
    messages: tuple[ContinuationMessage, ...]
    truncated: bool
    source_task_id: str = ""
    target_task_id: str = ""
    source_session_id: str = ""


@dataclass(frozen=True, slots=True)
class ImportedContinuation:
    """Verified branch inputs and their original inference-round baseline."""

    messages: tuple[ContinuationMessage, ...]
    model_round: int


def _parse_image_refs(value: Any, role: str) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or (value and role != "user"):
        raise ValueError("INVALID_CONTINUATION_IMAGE_REFERENCES")
    references = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or "data" in item:
            raise ValueError("INVALID_CONTINUATION_IMAGE_REFERENCES")
        image_id = item.get("imageId")
        if not isinstance(image_id, str) or not image_id or image_id in seen:
            raise ValueError("INVALID_CONTINUATION_IMAGE_REFERENCES")
        reference: dict[str, Any] = {"imageId": image_id}
        for key in ("contentRef", "sha256", "media_type", "messageSeq"):
            if key in item:
                reference[key] = item[key]
        digest = reference.get("sha256")
        path = reference.get("contentRef")
        sequence = reference.get("messageSeq")
        if ((digest is not None and (not isinstance(digest, str)
                                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None))
                or (path is not None and (not isinstance(path, str)
                                         or re.fullmatch(r"images/[0-9a-f]{64}\.(png|jpg)", path) is None))
                or ("media_type" in reference and reference["media_type"] not in {"image/png", "image/jpeg"})
                or (sequence is not None and (type(sequence) is not int or sequence < 0))):
            raise ValueError("INVALID_CONTINUATION_IMAGE_REFERENCES")
        seen.add(image_id)
        references.append(reference)
    return tuple(references)


def _source_session_id(value: dict[str, Any], messages: list[ContinuationMessage]) -> str:
    source = value.get("source_session_id", "")
    if not isinstance(source, str):
        raise ValueError("INVALID_CONTINUATION_IMAGE_SOURCE")
    source = source.strip()
    if any(message.image_refs for message in messages) and not source:
        raise ValueError("CONTINUATION_IMAGE_SOURCE_REQUIRED")
    return source


def parse_session_continuation(value: Any) -> SessionContinuation | None:
    """Return one bounded v1 continuation snapshot or ``None``."""

    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    product_session_id = value.get("product_session_id")
    if not isinstance(product_session_id, str) or not product_session_id.strip():
        return None

    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list):
        return None

    messages: list[ContinuationMessage] = []
    total_chars = 0
    truncated = bool(value.get("truncated"))
    for raw in raw_messages[:MAX_MESSAGES]:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        content = raw.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        image_refs = _parse_image_refs(raw.get("image_refs"), role)
        remaining = MAX_TOTAL_CHARS - total_chars
        if remaining <= 0:
            truncated = True
            break
        limit = min(MAX_MESSAGE_CHARS, remaining)
        if len(content) > limit:
            content = content[:limit]
            truncated = True
        if messages and messages[-1].role == role and not image_refs and not messages[-1].image_refs:
            merged = f"{messages[-1].content}\n\n{content}"
            if len(merged) > MAX_MESSAGE_CHARS:
                merged = merged[:MAX_MESSAGE_CHARS]
                truncated = True
            total_chars -= len(messages[-1].content)
            messages[-1] = ContinuationMessage(role=role, content=merged)
            total_chars += len(merged)
        else:
            messages.append(ContinuationMessage(role=role, content=content, image_refs=image_refs))
            total_chars += len(content)

    if len(raw_messages) > MAX_MESSAGES:
        truncated = True
    if not messages:
        return None

    def _text(name: str) -> str:
        raw = value.get(name)
        return raw.strip() if isinstance(raw, str) else ""

    return SessionContinuation(
        product_session_id=product_session_id.strip(),
        reason=_text("reason")[:64],
        messages=tuple(messages),
        truncated=truncated,
        source_task_id=_text("source_task_id")[:128],
        target_task_id=_text("target_task_id")[:128],
        source_session_id=_source_session_id(value, messages),
    )


def import_continuation_images(
    continuation: SessionContinuation,
    *,
    source_log: "SessionLog",
    destination_log: "SessionLog",
) -> ImportedContinuation:
    """Copy verified attachment bytes without reviving evicted occurrences.

    The caller supplies SessionLogs already authorized for this workspace. Host
    metadata is only a lookup key: source ownership, content, lifecycle state,
    dimensions and hashes come from the source log. No host path is opened.
    This does not append messages; the caller commits the returned references
    with the corresponding messages and restores ``model_round`` when forking.
    """
    from .image_store import SessionImageStore
    from .image_context import ImageContext
    from .session_history import effective_events

    if continuation.source_session_id != source_log.header.get("id"):
        raise ValueError("CONTINUATION_IMAGE_SOURCE_MISMATCH")
    source_log.assert_workspace(destination_log.header["cwd"])
    projection = source_log.replay()
    catalog = {item["imageId"]: item for item in ImageContext(source_log).images}
    events = {event["seq"]: event for event in effective_events(source_log.events)}
    catalog_order = {image_id: index for index, image_id in enumerate(catalog)}
    # The semantic protocol has no tool messages. A tool image therefore stays
    # attached to the original user task, identified through its assistant call
    # and tool result in the trusted source log, never through host-supplied text
    # paths or a newly chosen user message.
    latest_user: int | None = None
    call_owners: dict[tuple[Any, str], int] = {}
    user_owners: dict[int, int] = {}
    for event in events.values():
        event_type, data = event["type"], event["data"]
        if event_type == "surface/reset":
            latest_user = None
            call_owners.clear()
        elif event_type == "user/message" and data.get("source", "user") == "user":
            latest_user = event["seq"]
            user_owners[event["seq"]] = event["seq"]
        elif event_type == "assistant/message" and latest_user is not None:
            for call in data.get("message", {}).get("tool_calls") or ():
                if isinstance(call, dict) and isinstance(call.get("id"), str):
                    call_owners[(data.get("turn"), call["id"])] = latest_user
        elif event_type == "tool/result":
            call_id = data.get("message", {}).get("tool_call_id")
            owner_seq = call_owners.get((data.get("turn"), call_id))
            if owner_seq is not None:
                user_owners[event["seq"]] = owner_seq
    source_store = SessionImageStore(source_log.path.parent / "images")
    target_store = SessionImageStore(destination_log.path.parent / "images")
    staged: list[tuple[ContinuationMessage, list[tuple[dict[str, Any], dict[str, Any] | None]]]] = []
    seen: set[str] = set()
    for message in continuation.messages:
        attachments = []
        for requested in message.image_refs:
            image_id = requested.get("imageId")
            ref = catalog.get(image_id)
            if ref is None or image_id in seen:
                raise ValueError("CONTINUATION_IMAGE_NOT_OWNED")
            for key in ("contentRef", "sha256", "media_type", "messageSeq"):
                if key in requested and requested[key] != ref.get(key):
                    raise ValueError("CONTINUATION_IMAGE_REFERENCE_MISMATCH")
            owner = events.get(ref.get("messageSeq"))
            user_owner_seq = user_owners.get(ref.get("messageSeq"))
            user_owner = events.get(user_owner_seq)
            owner_data = user_owner.get("data", {}) if user_owner else {}
            original_content = owner_data.get("content")
            content_matches = isinstance(original_content, str) and (
                original_content.strip() == message.content.strip()
                or (continuation.truncated and continuation.reason != "history_edit"
                    and original_content.strip().startswith(message.content.strip()))
            )
            if (message.role != "user" or not owner or not user_owner
                    or owner_data.get("source", "user") != "user"
                    or not content_matches):
                raise ValueError("CONTINUATION_IMAGE_MESSAGE_MISMATCH")
            copied = deepcopy(ref)
            copied["sourceUserMessageSeq"] = user_owner_seq
            copied["sourceImageOrder"] = catalog_order[image_id]
            if owner["type"] == "tool/result":
                tool_message = owner["data"]["message"]
                copied.setdefault("sourceToolSessionId", source_log.header["id"])
                copied.setdefault("sourceToolResultSeq", owner["seq"])
                copied["toolCallId"] = tool_message["tool_call_id"]
                if tool_message.get("name"):
                    copied["toolName"] = tool_message["name"]
            try:
                block = source_store.load_block(ref)
            except ValueError:
                # An unavailable attachment must not destroy the text branch
                # or resurrect merely because its file is restored later.
                block = None
                copied.update(active=False, eligible=False, evicted=True,
                              evictionReason="missing_or_corrupt_file")
            attachments.append((copied, block))
            seen.add(image_id)
        attachments.sort(key=lambda pair: pair[0]["sourceImageOrder"])
        staged.append((message, attachments))

    imported = []
    for message, attachments in staged:
        references = []
        for ref, block in attachments:
            stored = target_store.persist_block(block) if block is not None else None
            if block is not None and stored is None:
                ref.update(active=False, eligible=False, evicted=True,
                           evictionReason="persistence_failed")
            # Preserve lifecycle and origin; a later explicit user submission
            # creates a new occurrence in the normal ingress path instead.
            if stored is not None:
                ref.update(stored)
            ref["sourceSessionId"] = source_log.header["id"]
            ref["sourceMessageSeq"] = ref.pop("messageSeq")
            if not ref.get("active", True) or ref.get("evicted", False):
                ref["eligible"] = False
            references.append(ref)
        imported.append(ContinuationMessage(message.role, message.content, tuple(references)))
    return ImportedContinuation(tuple(imported), projection.model_round)


def export_session_bundle(source_log: "SessionLog", destination_directory: str | Path) -> Path:
    """Copy the exact log and its image/Skill snapshots into a new directory.

    Read raw events rather than the active projection, so reset, compaction and
    eviction never remove archived files from an export. Publish the JSONL last;
    no existing directory is overwritten. Missing/corrupt files keep their log
    references and are handled as unavailable by the destination runtime.
    """
    from .image_store import SessionImageStore

    source_log.flush()
    original = source_log.path.read_bytes()
    events = source_log.events
    destination = Path(destination_directory)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    source_store = SessionImageStore(source_log.path.parent / "images")
    target_store = SessionImageStore(destination / "images")
    copied: set[str] = set()
    copied_skills: set[str] = set()
    try:
        for event in events:
            data = event["data"]
            for ref in data.get("skillReferences") or ():
                if not isinstance(ref, dict) or "contentRef" not in ref:
                    continue
                if ref["contentRef"] in copied_skills:
                    continue
                # Reuse the SessionLog path/hash/UTF-8 validation. Only an
                # explicitly referenced immutable snapshot is copied; runtime
                # temporary files and unrelated session files stay behind.
                snapshot = source_log.read_skill_reference(ref).encode("utf-8")
                target = destination / ref["contentRef"]
                target.parent.mkdir(mode=0o700, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(snapshot)
                    handle.flush()
                    os.fsync(handle.fileno())
                copied_skills.add(ref["contentRef"])
            references = list(data.get("images") or ())
            result = data.get("result")
            raw = result.get("rawOutput") if isinstance(result, dict) else None
            if isinstance(raw, dict):
                references.extend(raw.get("mcp_image_references") or ())
            for ref in references:
                if not isinstance(ref, dict) or ref.get("contentRef") in copied:
                    continue
                try:
                    block = source_store.load_block(ref)
                except ValueError:
                    _log.warning("Export retained an unavailable image reference: %s", ref.get("imageId", "legacy"))
                    continue
                if target_store.persist_block(block) is None:
                    raise OSError("SESSION_IMAGE_EXPORT_FAILED")
                copied.add(ref["contentRef"])
        path = destination / "session.jsonl"
        pending = destination / ".session.jsonl.tmp"
        with pending.open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        pending.rename(path)
        if os.name != "nt":
            directories = ((destination / "skill-references",) if copied_skills else ()) + (destination, destination.parent)
            for directory in directories:
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return path


__all__ = [
    "ContinuationMessage",
    "ImportedContinuation",
    "MAX_MESSAGE_CHARS",
    "MAX_MESSAGES",
    "MAX_TOTAL_CHARS",
    "SCHEMA_VERSION",
    "SessionContinuation",
    "export_session_bundle",
    "import_continuation_images",
    "parse_session_continuation",
]
