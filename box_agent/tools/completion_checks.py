"""Trusted capability-owned checks before a run reports successful completion."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..schema import Message
from ..session_trace import emit_session_trace


@dataclass(frozen=True)
class CompletionContext:
    messages: tuple[Message, ...]
    is_cancelled: Callable[[], bool]
    session_log: Any = None
    turn_id: str = ""

    def record(self, kind: str, data: dict) -> None:
        payload = {**data, "turn_id": self.turn_id}
        if self.session_log is not None:
            self.session_log.append(kind, payload, ignorable=True)
            self.session_log.flush()
        emit_session_trace(kind, turn_id=self.turn_id, data=payload)


@dataclass(frozen=True)
class CompletionIssue:
    code: str
    message: str
    repairable: bool = True


class CompletionCheck(Protocol):
    async def check(self) -> CompletionIssue | None: ...


class CompletionChecks:
    """Fresh per-run gates; neither transcript text nor public tool JSON is proof."""

    def __init__(self, context: CompletionContext):
        self.context = context
        self.gates: dict[str, CompletionCheck] = {}
        self.failures: list[CompletionIssue] = []
        self.repairs = 0

    @classmethod
    async def create(cls, tools: Mapping, context: CompletionContext) -> CompletionChecks:
        checks = cls(context)
        await checks.include_inputs(tools, context)
        return checks

    async def include_inputs(self, tools: Mapping, context: CompletionContext) -> None:
        """Extend capability gates after a user supplement, preserving receipts."""
        seen: set[int] = set()
        for tool in tools.values():
            if id(tool) in seen:
                continue
            seen.add(id(tool))
            factory = getattr(tool, "create_completion_gate", None)
            if factory is None:
                continue
            try:
                gate = self.gates.get(tool.name)
                if gate is None:
                    gate = await factory(context)
                    if gate is not None:
                        self.gates[tool.name] = gate
                else:
                    include_inputs = getattr(gate, "include_inputs", None)
                    if include_inputs is not None:
                        await include_inputs(context)
            except Exception as exc:
                self.failures.append(CompletionIssue(
                    "COMPLETION_CHECK_FAILED", f"{tool.name}: {exc}", False,
                ))

    async def check(self) -> list[CompletionIssue]:
        issues = list(self.failures)
        for name, gate in self.gates.items():
            try:
                issue = await gate.check()
            except Exception as exc:
                issue = CompletionIssue("COMPLETION_CHECK_FAILED", f"{name}: {exc}", False)
            if issue is not None:
                issues.append(issue)
        if self.gates or issues:
            self.context.record("completion.check", {
                "passed": not issues,
                "checks": list(self.gates),
                "issues": [{"code": i.code, "message": i.message} for i in issues],
            })
        return issues
