"""Session-local projection of host connector snapshots; never parse user XML."""

from __future__ import annotations

import logging
from typing import Any

from .schema import Message

_log = logging.getLogger(__name__)


def _remove_fragment(
    content: str | list[dict[str, Any]], fragment: str,
) -> tuple[str | list[dict[str, Any]], bool]:
    if isinstance(content, str):
        index = content.rfind(fragment)
        if index >= 0:
            return content[:index] + content[index + len(fragment):], True
        return content, False
    for index in range(len(content) - 1, -1, -1):
        block = content[index]
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            continue
        text, removed = _remove_fragment(block["text"], fragment)
        if removed:
            projected = list(content)
            projected[index] = {**block, "text": text}
            return projected, True
    return content, False


def project_connector_status(messages: list[Message]) -> list[Message]:
    """Keep the latest marked snapshot, copying only changed messages/blocks.

    Unmarked legacy text and user-authored examples are deliberately untouched.
    No latest-state cache is shared across runs, sessions, or child agents.
    """
    try:
        return _project_connector_status(messages)
    except Exception as exc:
        _log.warning("Connector status projection failed (%s); keeping original history",
                     type(exc).__name__)
        return messages


def _project_connector_status(messages: list[Message]) -> list[Message]:
    latest = next((index for index in range(len(messages) - 1, -1, -1)
                   if messages[index].role == "user" and messages[index].host_connector_status), None)
    projected = list(messages)
    for index, message in enumerate(messages):
        fragment = message.host_connector_status
        if message.role != "user" or not fragment:
            continue
        content, found = _remove_fragment(message.content, fragment)
        if index == latest:
            # Compaction may have bounded the original text. The persisted host
            # snapshot still owns current state and must not silently disappear.
            if found:
                continue
            content = (message.content + fragment if isinstance(message.content, str)
                       else [*message.content, {"type": "text", "text": fragment}])
            projected[index] = message.model_copy(update={"content": content})
        else:
            projected[index] = message.model_copy(update={
                "content": content, "host_connector_status": None,
            })
    return projected
