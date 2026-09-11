"""Public Session projection types.

The projection is the complete state reconstructed from a committed Session
Log. It is separate from provider context: a Context Engine may select,
summarize, or trim this state for one request without mutating it.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Sequence

from .schema import Message


@dataclass(frozen=True, slots=True, init=False)
class SessionProjection:
    """An isolated snapshot of one committed Session Log prefix.

    Public collections keep the historical list/dict shapes for consumers
    restoring mutable runtime state. Each access returns a defensive copy so
    mutating a restored Message or nested state cannot change this snapshot.
    """

    _messages: tuple[Message, ...]
    _goal: dict[str, Any] | None
    _plan: dict[str, Any] | None
    _todos: tuple[dict[str, Any], ...]
    _skills: tuple[dict[str, Any], ...]

    def __init__(
        self,
        messages: Sequence[Message],
        goal: dict[str, Any] | None,
        plan: dict[str, Any] | None,
        todos: Sequence[dict[str, Any]],
        skills: Sequence[dict[str, Any]],
    ) -> None:
        object.__setattr__(self, "_messages", deepcopy(tuple(messages)))
        object.__setattr__(self, "_goal", deepcopy(goal))
        object.__setattr__(self, "_plan", deepcopy(plan))
        object.__setattr__(self, "_todos", deepcopy(tuple(todos)))
        object.__setattr__(self, "_skills", deepcopy(tuple(skills)))

    @property
    def messages(self) -> list[Message]:
        return deepcopy(list(self._messages))

    @property
    def goal(self) -> dict[str, Any] | None:
        return deepcopy(self._goal)

    @property
    def plan(self) -> dict[str, Any] | None:
        return deepcopy(self._plan)

    @property
    def todos(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._todos))

    @property
    def skills(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._skills))


__all__ = ["SessionProjection"]
