"""Trusted capability-owned checks before a run reports successful completion."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
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
    resume_scope: Any = None

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
    """Per-task gates; only trusted checkpoints may survive an interactive pause."""

    def __init__(self, context: CompletionContext):
        self.context = context
        self.gates: dict[str, CompletionCheck] = {}
        self._tool_gates: dict[int, CompletionCheck] = {}
        self.failures: list[CompletionIssue] = []
        self.repairs = 0
        self.suspended = False
        self._tools: Mapping = {}

    @staticmethod
    def _pending_state(tools: Mapping, context: CompletionContext) -> dict | None:
        if context.session_log is not None:
            for event in reversed(getattr(context.session_log, "events", ())):
                if event["type"] == "completion.closed":
                    return None
                if event["type"] == "completion.paused":
                    return deepcopy(event["data"])
        if context.resume_scope is not None:
            for tool in tools.values():
                pending = getattr(tool, "_pending_completion_states", {}).get(id(context.resume_scope))
                if pending is not None and pending[0] is context.resume_scope:
                    return deepcopy(pending[1])
        return None

    @classmethod
    async def create(cls, tools: Mapping, context: CompletionContext) -> CompletionChecks:
        checks = cls(context)
        checks._tools = tools
        pending = cls._pending_state(tools, context)
        if pending is not None and pending.get("version") != 1:
            checks.failures.append(CompletionIssue("COMPLETION_RESTORE_FAILED", "Unsupported completion checkpoint", False))
        elif pending is not None:
            restorers = {tool.name: tool for tool in tools.values()
                         if callable(getattr(tool, "restore_completion_gate", None))}
            checks.repairs = pending.get("repairs", 0)
            checks.failures.extend(CompletionIssue(**issue) for issue in pending.get("failures", []))
            for name, state in pending.get("checks", {}).items():
                try:
                    tool = restorers[name]
                    gate = await tool.restore_completion_gate(context, state)
                    checks.gates[name] = gate
                    checks._tool_gates[id(tool)] = gate
                except Exception as exc:
                    checks.failures.append(CompletionIssue(
                        "COMPLETION_RESTORE_FAILED", f"{name}: {exc}", False,
                    ))
        await checks.include_inputs(tools, context)
        return checks

    def suspend(self) -> None:
        """Save trusted capability state at an interactive continuation boundary."""
        self.suspended = True
        if not self.gates and not self.failures:
            return
        states = {}
        failures = list(self.failures)
        for name, gate in self.gates.items():
            try:
                states[name] = gate.snapshot()
            except Exception as exc:
                failures.append(CompletionIssue("COMPLETION_RESTORE_FAILED", f"{name}: {exc}", False))
        state = {"version": 1, "checks": states, "repairs": self.repairs,
                 "failures": [vars(issue) for issue in failures]}
        self.context.record("completion.paused", state)
        if self.context.resume_scope is not None:
            pending = (self.context.resume_scope, deepcopy(state))
            for tool in self._tools.values():
                states = getattr(tool, "_pending_completion_states", None)
                if states is None:
                    states = tool._pending_completion_states = {}
                states[id(self.context.resume_scope)] = pending

    def close(self) -> None:
        """A terminal run ends the pending task, without leaking it into a new one."""
        if self._pending_state(self._tools, self.context) is not None:
            self.context.record("completion.closed", {})
        for tool in self._tools.values():
            states = getattr(tool, "_pending_completion_states", {})
            pending = states.get(id(self.context.resume_scope))
            if pending is not None and pending[0] is self.context.resume_scope:
                states.pop(id(self.context.resume_scope))

    async def include_inputs(self, tools: Mapping, context: CompletionContext) -> None:
        """Extend capability gates after a user supplement, preserving receipts."""
        self._tools = tools
        seen: set[int] = set()
        for registered_name, tool in tools.items():
            if id(tool) in seen:
                continue
            seen.add(id(tool))
            factory = getattr(tool, "create_completion_gate", None)
            if factory is None:
                continue
            try:
                gate = self._tool_gates.get(id(tool))
                if gate is None:
                    gate = await factory(context)
                    if gate is not None:
                        self.gates[tool.name] = gate
                        self._tool_gates[id(tool)] = gate
                else:
                    include_inputs = getattr(gate, "include_inputs", None)
                    if include_inputs is not None:
                        await include_inputs(context)
            except Exception as exc:
                self.failures.append(CompletionIssue(
                    "COMPLETION_CHECK_FAILED", f"{registered_name}: {exc}", False,
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
