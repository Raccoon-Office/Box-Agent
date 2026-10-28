"""Nonblocking final-QA handoff using the turn's original immutable scope."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..memory import MemoryRuntime, _MemoryTurn
from .backend import TurnScope


_log = logging.getLogger(__name__)


class DesktopMemoryRuntime(MemoryRuntime):
    def __init__(self, backend) -> None:
        super().__init__(backend)
        self._turn_scope: TurnScope | None = None
        self._scheduled: TurnScope | None = None

    def user_turn_scope(self, turn: _MemoryTurn):
        from ..payload_privacy import private_payload_scope

        return private_payload_scope()

    async def refresh(self, session: Any, turn: _MemoryTurn) -> None:
        self._turn_scope = None
        self._scheduled = None
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
        task = asyncio.create_task(self._finish(turn, scope), name="desktop-memory-handoff")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def end_turn(self, turn: _MemoryTurn) -> None:
        scope = self._turn_scope
        self.backend.end_turn()
        self._turn_scope = None
        if scope is not None and self._scheduled is not scope and not self._closed:
            if len(self.tasks) < self.backend.config.save_queue_limit:
                task = asyncio.create_task(self.backend.close_turn(scope), name="desktop-memory-close")
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def _finish(self, turn: _MemoryTurn, scope: TurnScope) -> None:
        try:
            if not scope.learnable or not turn.user.strip() or turn.handle is None:
                return
            result = await turn.handle.result(consume_events=False)
            if result.status.value != "completed" or not result.final_content.strip():
                return
            async with self._save_slots:
                receipt = await self.backend.handoff(scope, turn.user, result.final_content, turn.timestamp)
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
