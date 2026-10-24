"""Validated semantic history supplied when a host recreates an ACP session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "officev3-session-continuation/v1"
MAX_MESSAGES = 12
MAX_MESSAGE_CHARS = 12_000
MAX_TOTAL_CHARS = 48_000


@dataclass(frozen=True, slots=True)
class ContinuationMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class SessionContinuation:
    product_session_id: str
    reason: str
    messages: tuple[ContinuationMessage, ...]
    truncated: bool
    source_task_id: str = ""
    target_task_id: str = ""


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
        remaining = MAX_TOTAL_CHARS - total_chars
        if remaining <= 0:
            truncated = True
            break
        limit = min(MAX_MESSAGE_CHARS, remaining)
        if len(content) > limit:
            content = content[:limit]
            truncated = True
        if messages and messages[-1].role == role:
            merged = f"{messages[-1].content}\n\n{content}"
            if len(merged) > MAX_MESSAGE_CHARS:
                merged = merged[:MAX_MESSAGE_CHARS]
                truncated = True
            total_chars -= len(messages[-1].content)
            messages[-1] = ContinuationMessage(role=role, content=merged)
            total_chars += len(merged)
        else:
            messages.append(ContinuationMessage(role=role, content=content))
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
    )


__all__ = [
    "ContinuationMessage",
    "MAX_MESSAGE_CHARS",
    "MAX_MESSAGES",
    "MAX_TOTAL_CHARS",
    "SCHEMA_VERSION",
    "SessionContinuation",
    "parse_session_continuation",
]
