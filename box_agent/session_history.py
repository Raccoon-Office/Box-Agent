"""Revision-aware navigation over a session's append-only event log."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import Agent


def prompt_fingerprint(prompt: Any) -> str:
    value = [block if isinstance(block, dict) else block.model_dump(mode="json")
             for block in prompt]
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def effective_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve rewinds without importing events from a discarded future."""
    active: list[dict[str, Any]] = []
    for event in events:
        if event["type"] == "history/revise":
            if not all(isinstance(event["data"].get(key), str) and event["data"][key]
                       for key in ("revision_id", "previous_revision_id", "turn_id")):
                raise ValueError("invalid history revision identity")
            cutoff = event["data"].get("restore_through_seq")
            if type(cutoff) is not int or cutoff < -1 or cutoff >= event["seq"]:
                raise ValueError("invalid history revision boundary")
            if cutoff == -1:
                active.clear()
            else:
                index = next((i for i, item in enumerate(active) if item["seq"] == cutoff), None)
                if index is None:
                    raise ValueError("history revision references discarded events")
                del active[index + 1:]
        active.append(event)
    return active


def current_revision(events: list[dict[str, Any]]) -> str:
    return next((event["data"]["revision_id"] for event in reversed(events)
                 if event["type"] == "history/revise"), "initial")


def revision_request(
    events: list[dict[str, Any]], *, target_turn_id: str, turn_id: str,
    expected_revision_id: str, fingerprint: str,
) -> dict[str, Any]:
    """Validate an edit against an exact host-message boundary, or its retry."""
    if not all(isinstance(value, str) and value.strip() and len(value) <= 512
               for value in (target_turn_id, turn_id, expected_revision_id)):
        raise ValueError("INVALID_HISTORY_REVISION")
    prior = next((event for event in events if event["type"] == "history/revise"
                  and event["data"].get("turn_id") == turn_id), None)
    if prior is not None:
        if (prior["data"].get("target_turn_id") != target_turn_id
                or prior["data"].get("previous_revision_id") != expected_revision_id
                or prior["data"].get("prompt_fingerprint") != fingerprint):
            raise ValueError("HISTORY_REVISION_IDEMPOTENCY_CONFLICT")
        if current_revision(events) != prior["data"]["revision_id"]:
            raise ValueError("HISTORY_REVISION_CONFLICT")
        return prior["data"]
    if current_revision(events) != expected_revision_id:
        raise ValueError("HISTORY_REVISION_CONFLICT")
    bindings = [event for event in effective_events(events)
                if event["type"] == "turn/binding"
                and event["data"].get("turn_id") == target_turn_id]
    if len(bindings) != 1:
        raise ValueError("HISTORY_ANCHOR_UNAVAILABLE")
    return {
        "revision_id": turn_id,
        "previous_revision_id": expected_revision_id,
        "restore_through_seq": bindings[0]["seq"] - 1,
        "target_turn_id": target_turn_id,
        "turn_id": turn_id,
        "prompt_fingerprint": fingerprint,
    }


async def revise_agent_history(
    agent: Agent, revision: dict[str, Any], *, cancelled: Callable[[], bool] | None = None,
) -> None:
    """Commit a checked prefix and restore the same live Agent from that prefix."""
    from .skill_runtime import SkillRuntime

    log = agent.session_log
    if log is None:
        raise ValueError("HISTORY_LOG_UNAVAILABLE")
    projection = log.replay(through_seq=revision["restore_through_seq"])
    pending: set[str] = set()
    for message in projection.messages:
        for call in message.tool_calls or []:
            pending.add(call.id)
        if message.role == "tool":
            if message.tool_call_id not in pending:
                raise ValueError("HISTORY_TOOL_PAIR_INVALID")
            pending.remove(message.tool_call_id)
    if pending:
        raise ValueError("HISTORY_TOOL_PAIR_INVALID")
    # Validate references before changing the durable or live session.
    candidate = SkillRuntime(agent.skill_runtime.loader, session_log=log)
    await candidate.arestore_records(projection.skills)
    if cancelled is not None and cancelled():
        raise ValueError("HISTORY_CANCELLED")
    existing = any(event["type"] == "history/revise"
                   and event["data"].get("turn_id") == revision["turn_id"]
                   for event in log.events)
    if not existing:
        log.append("history/revise", revision)
        log.flush()
    agent.skill_runtime.clear_references()
    agent.restore_session_projection(projection)
