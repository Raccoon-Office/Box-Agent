"""Append-only trusted connector updates and post-compaction state recovery."""

from __future__ import annotations

import logging

from .schema import Message

_log = logging.getLogger(__name__)
_UPDATE_NOTICE = (
    "[Host connector status update: this complete snapshot supersedes all earlier "
    "host connector snapshots. If unavailable, do not reuse earlier connection states.]"
)


def _snapshot_key(snapshot: str) -> tuple[str, ...]:
    # Only inspect the separately marked host fragment, never user-authored XML.
    return tuple(sorted(line.strip() for line in snapshot.splitlines()
                        if line.strip() and line.strip() not in {
                            "<connector-status>", "</connector-status>",
                        }))


def _append_snapshot(content: str, snapshot: str, *, update: bool = True) -> str:
    notice = _UPDATE_NOTICE + "\n" if update else ""
    return f"{content.rstrip()}\n\n{notice}{snapshot.strip()}"


def append_connector_status(messages: list[Message], content: str, snapshot: str | None) -> str:
    """Append the first/changed snapshot to a NEW message; leave history untouched."""
    if not snapshot:
        return content
    try:
        previous = next((message.host_connector_status for message in reversed(messages)
                         if message.role == "user" and message.host_connector_status), None)
        if previous is not None and _snapshot_key(previous) == _snapshot_key(snapshot):
            return content
    except Exception as exc:
        _log.warning("Connector status comparison failed (%s); appending current snapshot",
                     type(exc).__name__)
        return _append_snapshot(content, snapshot)
    return _append_snapshot(content, snapshot, update=previous is not None)


def _contains_snapshot(message: Message) -> bool:
    fragment = message.host_connector_status.strip()
    if isinstance(message.content, str):
        return fragment in message.content
    return any(block.get("type") == "text" and isinstance(block.get("text"), str)
               and fragment in block["text"] for block in message.content)


def restore_connector_status(messages: list[Message]) -> list[Message]:
    """Restore state dropped by compaction, never delete historical updates.

    Unchanged host turns retain internal state metadata. Restore missing snapshots
    at each retained state run's first message. Earlier recovered runs must remain
    stable even when a later turn changes state; no cross-request cache is needed.
    """
    try:
        marked = [(index, message) for index, message in enumerate(messages)
                  if message.role == "user" and message.host_connector_status]
        if not marked:
            return messages
        runs: list[list[tuple[int, Message]]] = []
        previous_key = None
        for index, message in marked:
            key = _snapshot_key(message.host_connector_status)
            if not runs or key != previous_key:
                runs.append([])
            runs[-1].append((index, message))
            previous_key = key
        projected = messages
        for run in runs:
            if any(_contains_snapshot(message) for _, message in run):
                continue
            anchor, message = run[0]
            content = (_append_snapshot(message.content, message.host_connector_status)
                       if isinstance(message.content, str) else [
                           *message.content,
                           {"type": "text", "text": _append_snapshot("", message.host_connector_status)},
                       ])
            if projected is messages:
                projected = list(messages)
            projected[anchor] = message.model_copy(update={"content": content})
        return projected
    except Exception as exc:
        _log.warning("Connector status recovery failed (%s); keeping original history",
                     type(exc).__name__)
        return messages
