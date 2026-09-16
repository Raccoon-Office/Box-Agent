"""Task-scoped method identities and user inputs; no execution or budgets."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .schema import Message
from .skill_dependencies import SkillDependencyError
from .skill_state import SkillReferenceSnapshot


@dataclass(frozen=True, slots=True)
class SkillMethod:
    name: str
    source: str
    path: str
    revision: str
    ranges: tuple[tuple[int, int], ...]

    def record(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "path": self.path,
                "revision": self.revision, "ranges": [list(pair) for pair in self.ranges]}


@dataclass
class SkillTaskState:
    """A task's adopted methods, distinct from historical read receipts."""

    task_id: str = ""
    methods: dict[str, SkillMethod] = field(default_factory=dict)
    user_inputs: list[Any] = field(default_factory=list)
    user_input_ids: list[str] = field(default_factory=list)
    # Candidate user facts alone do not opt legacy reads into method semantics.
    methods_initialized: bool = False

    def record(self) -> dict[str, Any]:
        return {"schema": 1, "task_id": self.task_id, "methods_initialized": self.methods_initialized,
                "methods": [method.record() for method in self.methods.values()],
                "user_inputs": deepcopy(self.user_inputs), "user_input_ids": list(self.user_input_ids)}

    @classmethod
    def restore(cls, value: Any) -> SkillTaskState:
        if value is None:
            return cls()
        try:
            if not isinstance(value, dict) or value.get("schema") != 1:
                raise ValueError("unknown task schema")
            task_id, methods, inputs = value["task_id"], value["methods"], value["user_inputs"]
            if not isinstance(task_id, str) or not isinstance(methods, list) or not isinstance(inputs, list):
                raise ValueError("invalid task fields")
            parsed: dict[str, SkillMethod] = {}
            for item in methods:
                fields = [item[key] for key in ("name", "source", "path", "revision")]
                if any(not isinstance(part, str) for part in fields) or not fields[0] or not fields[3]:
                    raise ValueError("invalid method identity")
                ranges = item["ranges"]
                if not isinstance(ranges, list) or not ranges:
                    raise ValueError("missing method ranges")
                for pair in ranges:
                    if (not isinstance(pair, list) or len(pair) != 2
                            or any(type(number) is not int for number in pair)
                            or not 0 <= pair[0] < pair[1]):
                        raise ValueError("invalid method ranges")
                if fields[0] in parsed:
                    raise ValueError("duplicate method")
                parsed[fields[0]] = SkillMethod(*fields, tuple(tuple(pair) for pair in ranges))
            if parsed and not task_id:
                raise ValueError("missing task identity")
            # Schema-1 records predating this marker already treated an empty
            # method list as explicit reference/release semantics.
            initialized = value.get("methods_initialized", True)
            if type(initialized) is not bool or (not initialized and (task_id or parsed)):
                raise ValueError("invalid method initialization")
            for content in inputs:
                Message(role="user", source="user", content=content)
            input_ids = value.get("user_input_ids", [f"legacy-task:{task_id}:{i}" for i in range(len(inputs))])
            if (not isinstance(input_ids, list) or len(input_ids) != len(inputs)
                    or any(not isinstance(item, str) or not item for item in input_ids)
                    or len(set(input_ids)) != len(input_ids)):
                raise ValueError("invalid user input identities")
            return cls(task_id, parsed, deepcopy(inputs), list(input_ids), initialized)
        except (KeyError, TypeError, ValueError) as exc:
            raise SkillDependencyError("SKILL_TASK_INVALID", f"Cannot restore adopted Skill task: {exc}") from exc

    def adopt(self, snapshot: SkillReferenceSnapshot, ranges: tuple[tuple[int, int], ...], *,
              replace: tuple[str, ...] = (), new_task: bool = False, latest_input: Any = None,
              latest_input_id: str | None = None) -> None:
        self.methods_initialized = True
        if new_task or not self.task_id:
            self.task_id = uuid4().hex
            self.methods = {}
            self.user_inputs = [] if latest_input is None else [deepcopy(latest_input)]
            self.user_input_ids = [] if latest_input is None else [latest_input_id or uuid4().hex]
        previous = self.methods.get(snapshot.name)
        if previous and (previous.source, previous.path, previous.revision) == (
                snapshot.source, snapshot.path, snapshot.revision):
            ranges = tuple(sorted(set((*previous.ranges, *ranges))))
        for name in replace:
            self.methods.pop(name, None)
        self.methods[snapshot.name] = SkillMethod(
            snapshot.name, snapshot.source, snapshot.path, snapshot.revision, ranges)

    def release(self, name: str) -> None:
        self.methods_initialized = True
        self.methods.pop(name, None)
        if not self.methods:
            self.task_id = ""
            self.user_inputs = []
            self.user_input_ids = []
