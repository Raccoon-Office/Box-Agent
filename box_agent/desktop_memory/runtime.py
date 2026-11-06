"""Nonblocking final-QA handoff using the turn's original immutable scope."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from copy import deepcopy
from typing import Any

from ..memory import MemoryRuntime, _MemoryTurn
from .backend import TurnScope
from .evidence import EvidenceCursor, _redact, no_evidence
from .experience import ExperienceRuntime
from .transport import MemoryProxyError


_log = logging.getLogger(__name__)
_ATTEMPT_CACHE_LIMIT = 5000


def _file_input_facts(context: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project accepted file metadata without sharing paths or file bodies."""
    files = context.get("files") if isinstance(context, dict) else None
    if not files:
        return None
    if not isinstance(files, list) or len(files) > 520:
        raise MemoryProxyError("INVALID_ARGUMENT")
    summaries = []
    for item in files:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not 0 < len(item["name"]) <= 1024
                or type(item.get("size")) is not int or not 0 <= item["size"] <= 9007199254740991):
            raise MemoryProxyError("INVALID_ARGUMENT")
        # A display name can itself be supplied as a path; only its basename
        # belongs to the metadata projection, never an absolute local prefix.
        name = item["name"].replace("\\", "/").rsplit("/", 1)[-1]
        if not name.strip() or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise MemoryProxyError("INVALID_ARGUMENT")
        summary = {"name": name, "size": item["size"]}
        if "mimeType" in item:
            mime = item["mimeType"]
            if not isinstance(mime, str) or len(mime) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in mime):
                raise MemoryProxyError("INVALID_ARGUMENT")
            if mime:
                summary["mimeType"] = mime
        summaries.append(summary)
    # Even omitted metadata cannot turn a credential-bearing input into a
    # newly learned QA. The whole handoff is skipped rather than partially sent.
    if _redact(summaries) != summaries:
        raise MemoryProxyError("MEMORY_INPUT_REDACTED")
    facts = {"files": summaries[:20], "omitted_files": max(0, len(summaries) - 20)}
    if len(json.dumps(facts, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
        raise MemoryProxyError("BUDGET_EXCEEDED")
    return facts


def _has_original_user_view(evidence: dict[str, Any], user: str) -> bool:
    if evidence.get("status") != "captured":
        return False
    for view in evidence.get("views", []):
        if view.get("source_ref", {}).get("event_type") == "user/message":
            try:
                if json.loads(view["text"]) == {"content": user}:
                    return True
            except (ValueError, TypeError, KeyError):
                pass
    return False


class DesktopMemoryRuntime(MemoryRuntime):
    requires_user_message = True

    def __init__(self, backend) -> None:
        super().__init__(backend)
        self._turn_scope: TurnScope | None = None
        self._scheduled: TurnScope | None = None
        self._attempts: OrderedDict[str, int] = OrderedDict()
        self._attempt_log = None
        self._attempts_complete = True
        self._evidence_cursor: EvidenceCursor | None = None
        self._evidence_session = None
        self._experience = ExperienceRuntime(backend.transport)
        self._close_tasks: set[asyncio.Task] = set()

    def user_turn_scope(self, turn: _MemoryTurn):
        from ..payload_privacy import private_payload_scope

        return private_payload_scope()

    @staticmethod
    def _attempt_events(log):
        for event in log.events_of_type("memory/desktop-attempt"):
            data = event.get("data", {})
            key, timestamp = data.get("turn_key"), data.get("timestamp")
            if (not isinstance(key, str) or len(key) != 64
                    or type(timestamp) is not int or timestamp < 0):
                raise ValueError("invalid_memory_attempt")
            yield key, timestamp

    def _remember_attempt(self, key: str, timestamp: int, *, bounded: bool) -> None:
        if key in self._attempts:
            self._attempts.move_to_end(key)
            return
        if bounded and len(self._attempts) >= _ATTEMPT_CACHE_LIMIT:
            self._attempts.popitem(last=False)
            self._attempts_complete = False
        self._attempts[key] = timestamp

    async def refresh(self, session: Any, turn: _MemoryTurn) -> None:
        self._turn_scope = None
        self._scheduled = None
        key = hashlib.sha256(json.dumps([turn.session_id, turn.turn_id], ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest()
        log = getattr(session.agent, "session_log", None)
        self._evidence_session = session
        try:
            from ..turn_hints import with_turn_hints
            goal = getattr(session.agent, "goal", None)
            wrapper = getattr(session.agent, "_apply_goal_context", None)
            logged = turn.logged_user_text
            wrapped = logged if logged is not None else with_turn_hints(turn.user)
            if getattr(goal, "status", None) == "active" and callable(wrapper):
                wrapped = wrapper(wrapped)
            current_message = turn.user_message
            input_facts = getattr(current_message, "input_facts", None)
            if input_facts is not None:
                from ..input_context import original_request

                # New input records persist originals; hints, Goal and connector
                # state exist only in the request projection, never in evidence.
                original = "\n".join(block["text"] for block in input_facts["blocks"]
                                     if block["type"] == "text")
                if current_message.content != original:
                    raise ValueError("current input does not match its persisted facts")
                turn.input_context = deepcopy(input_facts["context"])
                turn.user = original_request(original, turn.input_context)
                wrapped = original
            user_seq = (log.surface_message_sequence(session.agent.messages[1:], current_message)
                        if log is not None and current_message is not None else None)
            self._evidence_cursor = EvidenceCursor.begin(log, turn.user, wrapped_user=wrapped, user_seq=user_seq)
        except Exception:
            self._evidence_cursor = None
            self.backend.log_failure("evidence", "log_unavailable")
        self.backend.context_attempt_durable = True
        try:
            if log is not None and log is not self._attempt_log:
                self._attempts.clear()
                self._attempts_complete = True
                for attempt_key, timestamp in self._attempt_events(log):
                    # Retain the first occurrence even if a corrupt log contains duplicates.
                    if attempt_key not in self._attempts and len(self._attempts) < _ATTEMPT_CACHE_LIMIT:
                        self._attempts[attempt_key] = timestamp
                    elif attempt_key not in self._attempts:
                        self._attempts_complete = False
                self._attempt_log = log
            timestamp = self._attempts.get(key)
            if timestamp is None and log is not None and not self._attempts_complete:
                timestamp = next((value for attempt_key, value in self._attempt_events(log)
                                  if attempt_key == key), None)
            self.backend.context_requires_existing = timestamp is not None
            if timestamp is not None:
                turn.timestamp = timestamp
                self._remember_attempt(key, timestamp, bounded=log is not None)
            else:
                if log is not None:
                    log.append("memory/desktop-attempt", {"turn_key": key, "timestamp": turn.timestamp}, ignorable=True)
                    log.flush()
                self._remember_attempt(key, turn.timestamp, bounded=log is not None)
        except Exception as exc:
            self.backend.context_attempt_durable = False
            self.backend.log_failure("attempt", type(exc).__name__)
        await super().refresh(session, turn)
        self._turn_scope = self.backend.current_scope
        # An online cloud session can reconcile a prior unknown request even
        # while automatic learning is off. The native Host selects pending-only
        # work in that case; this does not qualify the current turn for learning.
        if (self._turn_scope and self._turn_scope.binding["source"] == "cloud"
                and self._turn_scope.binding["onlineAtStart"]
                and not self._turn_scope.binding.get("parentTurnId")):
            self._experience.schedule(turn.session_id, getattr(session.agent, "llm", None), getattr(session.agent, "token_limit", 0), self.tasks)

    def schedule(self, turn: _MemoryTurn) -> None:
        scope = self._turn_scope
        if scope is None or self._closed:
            return
        if len(self.tasks) >= self.backend.config.save_queue_limit:
            self.backend.log_failure("handoff", "queue_full")
            return
        self._scheduled = scope
        cursor = self._evidence_cursor
        if cursor is not None and getattr(self._evidence_session.agent, "session_log", None) is not cursor.log:
            cursor = None
        end = cursor.log.event_count if cursor else 0
        task = asyncio.create_task(self._finish(turn, scope, cursor, end, getattr(self._evidence_session.agent, "llm", None), getattr(self._evidence_session.agent, "token_limit", 0)), name="desktop-memory-handoff")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def end_turn(self, turn: _MemoryTurn) -> None:
        scope = self._turn_scope
        self.backend.end_turn()
        self._turn_scope = None
        self._evidence_cursor = None
        self._evidence_session = None
        if scope is not None and self._scheduled is not scope and not self._closed:
            # Cleanup has its own bounded allowance, so a full QA queue does
            # not by itself leave this turn open at the Host.
            if len(self._close_tasks) < self.backend.config.save_queue_limit:
                task = asyncio.create_task(self.backend.close_turn(scope), name="desktop-memory-close")
                self._close_tasks.add(task)
                self.tasks.add(task)
                task.add_done_callback(self._close_tasks.discard)
                task.add_done_callback(self.tasks.discard)
            else:
                self.backend.log_failure("turn_close", "queue_full")

    async def _finish(self, turn: _MemoryTurn, scope: TurnScope,
                      cursor: EvidenceCursor | None = None, end: int = 0, llm: Any = None, token_limit: int = 0) -> None:
        try:
            if not scope.learnable or turn.handle is None:
                return
            input_facts = _file_input_facts(turn.input_context)
            if not turn.user.strip() and input_facts is None:
                return
            result = await turn.handle.result(consume_events=False)
            if result.status.value != "completed" or not result.final_content.strip():
                return
            evidence = no_evidence(turn.session_id, turn.turn_id, scope.qa_event_id, "log_unavailable")
            if cursor is not None:
                try:
                    evidence = cursor.capture(session_id=turn.session_id, turn_id=turn.turn_id,
                                              event_id=scope.qa_event_id, user=turn.user,
                                              assistant=result.final_content, end=end)
                except Exception:
                    self.backend.log_failure("evidence", "log_unavailable")
            if not turn.user.strip() and not _has_original_user_view(evidence, turn.user):
                return
            async with self._save_slots:
                receipt = await self.backend.handoff(scope, turn.user, result.final_content, turn.timestamp,
                                                     evidence=evidence, input_facts=input_facts)
            _log.info("memory backend=desktop operation=handoff state=%s", receipt["state"])
            if receipt["state"] == "skipped_redacted":
                return
            self._experience.schedule(turn.session_id, llm, token_limit, self.tasks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.backend.log_failure("handoff", type(exc).__name__)
        finally:
            await self.backend.close_turn(scope)

    async def aclose(self) -> None:
        self._experience.close()
        try:
            await super().aclose()
        finally:
            await self.backend.aclose()
