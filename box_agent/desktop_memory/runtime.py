"""Nonblocking final-QA handoff using the turn's original immutable scope."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from ..memory import MemoryRuntime, _MemoryTurn
from .backend import TurnScope
from .evidence import EvidenceCursor, no_evidence


_log = logging.getLogger(__name__)


class DesktopMemoryRuntime(MemoryRuntime):
    def __init__(self, backend) -> None:
        super().__init__(backend)
        self._turn_scope: TurnScope | None = None
        self._scheduled: TurnScope | None = None
        self._attempts: dict[str, int] = {}
        self._attempt_log = None
        self._evidence_cursor: EvidenceCursor | None = None
        self._evidence_session = None

    def user_turn_scope(self, turn: _MemoryTurn):
        from ..payload_privacy import private_payload_scope

        return private_payload_scope()

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
            wrapped = wrapper(with_turn_hints(turn.user)) if getattr(goal, "status", None) == "active" and callable(wrapper) else None
            self._evidence_cursor = EvidenceCursor.begin(log, turn.user, wrapped_user=wrapped)
        except Exception:
            self._evidence_cursor = None
            self.backend.log_failure("evidence", "log_unavailable")
        self.backend.context_attempt_durable = True
        try:
            if log is not None and log is not self._attempt_log:
                self._attempts = {}
                for event in log.events:
                    if event.get("type") != "memory/desktop-attempt":
                        continue
                    data = event.get("data", {})
                    timestamp = data.get("timestamp")
                    if (not isinstance(data.get("turn_key"), str) or len(data["turn_key"]) != 64
                            or type(timestamp) is not int or timestamp < 0):
                        raise ValueError("invalid_memory_attempt")
                    self._attempts.setdefault(data["turn_key"], timestamp)
                self._attempt_log = log
            self.backend.context_requires_existing = key in self._attempts
            if key in self._attempts:
                turn.timestamp = self._attempts[key]
            else:
                if len(self._attempts) >= 5000:
                    raise ValueError("memory_attempt_budget")
                if log is not None:
                    log.append("memory/desktop-attempt", {"turn_key": key, "timestamp": turn.timestamp}, ignorable=True)
                    log.flush()
                self._attempts[key] = turn.timestamp
        except Exception as exc:
            self.backend.context_attempt_durable = False
            self.backend.log_failure("attempt", type(exc).__name__)
        await super().refresh(session, turn)
        self._turn_scope = self.backend.current_scope

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
        task = asyncio.create_task(self._finish(turn, scope, cursor, end), name="desktop-memory-handoff")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def end_turn(self, turn: _MemoryTurn) -> None:
        scope = self._turn_scope
        self.backend.end_turn()
        self._turn_scope = None
        self._evidence_cursor = None
        self._evidence_session = None
        if scope is not None and self._scheduled is not scope and not self._closed:
            if len(self.tasks) < self.backend.config.save_queue_limit:
                task = asyncio.create_task(self.backend.close_turn(scope), name="desktop-memory-close")
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def _finish(self, turn: _MemoryTurn, scope: TurnScope,
                      cursor: EvidenceCursor | None = None, end: int = 0) -> None:
        try:
            if not scope.learnable or not turn.user.strip() or turn.handle is None:
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
            async with self._save_slots:
                receipt = await self.backend.handoff(scope, turn.user, result.final_content, turn.timestamp,
                                                     evidence=evidence)
            _log.info("memory backend=desktop operation=handoff state=%s", receipt["state"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.backend.log_failure("handoff", type(exc).__name__)
        finally:
            await self.backend.close_turn(scope)

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            await self.backend.aclose()
