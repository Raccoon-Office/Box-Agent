"""Require an explicit method decision when previously delivered text is missing."""

from __future__ import annotations

from ..skill_dependencies import SkillDependencyError, resolve_required_skills
from ..skill_context import visible_ranges
from .base import ToolResult
from .completion_checks import CompletionContext, CompletionIssue


class SkillRecoveryGate:
    """Per-turn recovery, owned by get_skill rather than the execution kernel.

    A summary is not a read receipt. Unrelated methods can be deferred for this
    turn; the next turn must decide again if their instructions are still absent.
    """

    _PREPARATION_TOOLS = frozenset({
        "get_skill", "skill_view", "list_skills", "tool_search", "read_file", "read",
        "list_directory", "search_files", "search_content", "validate_table", "plan_write",
        "request_user_decision",
    })

    def __init__(self, context: CompletionContext, references):
        self.context = context
        self.references = references
        self.deferred: dict[str, dict] = {}
        self.work_attempted = False
        self.recovering: set[str] = set()
        self.missing()

    @classmethod
    def create(cls, context: CompletionContext):
        owner = getattr(context.skill_reader, "__self__", None)
        references = getattr(owner, "references", None)
        if references is None or not references.runtime.append_only_context:
            return None
        return cls(context, references)

    async def include_inputs(self, context: CompletionContext) -> None:
        # A user supplement may bring a deferred method back into scope.
        if context is not self.context:
            self.deferred.clear()
        self.context = context

    @staticmethod
    def _identity(record):
        return {"revision": record.revision, "source": record.source, "path": record.path}

    def missing(self):
        messages = (self.context.resume_scope if isinstance(self.context.resume_scope, list)
                    else self.context.messages)
        missing = {}
        for record in self.references.runtime.read_facts:
            expected = {line for start, end in record.delivered_ranges for line in range(start, end)}
            if record.delivered_complete:
                expected = set(range(len(record.prompt.splitlines(keepends=True))))
            if record.name in self.recovering:
                expected = set(range(len(record.prompt.splitlines(keepends=True))))
            if not expected:
                continue
            covered = visible_ranges(messages, name=record.name, revision=record.revision,
                                     lines=record.prompt.splitlines(keepends=True),
                                     source=record.source, path=record.path)
            if not expected <= covered:
                missing[record.name] = record
                if record.delivered_complete:
                    self.recovering.add(record.name)
            else:
                self.recovering.discard(record.name)
        return missing

    def _required(self):
        runtime = self.references.runtime
        required = set(runtime.selected_names) | set(runtime.restoring_names) | set(runtime.turn_deliveries)
        messages = (self.context.resume_scope if isinstance(self.context.resume_scope, list)
                    else self.context.messages)
        user_input = next((str(message.content) for message in reversed(messages)
                           if message.role == "user" and message.source != "runtime"), "")
        required.update(record.name for record in runtime.read_facts if record.name in user_input)
        if runtime.loader is not None and required:
            names = [name for name in required if name not in runtime.state.reads
                     or runtime.state.reads[name].source != "caller"]
            try:
                required.update(skill.name for skill in resolve_required_skills(runtime.loader, names))
            except SkillDependencyError:
                required.update(record.name for record in runtime.read_facts)
        return required

    def _pending(self):
        required = self._required()
        return {name: record for name, record in self.missing().items()
                if name in required or self.deferred.get(name, {}).get("identity") != self._identity(record)}

    def defer(self, name: str, reason: str) -> ToolResult:
        name, reason = name.strip(), reason.strip()
        if not reason or name not in self.missing():
            return ToolResult(success=False, error="Only a missing previously read Skill can be deferred, with a nonempty reason.")
        if name in self._required():
            return ToolResult(success=False, error="The current turn selected or read this Skill; read its missing instructions instead.")
        record = self.missing()[name]
        self.deferred[name] = {"identity": self._identity(record), "reason": reason}
        self.context.record("skill.recovery_deferred", {"name": name, **self.deferred[name]})
        return ToolResult(success=True, content=(
            f"Skill '{name}' deferred for this turn only: {reason}. "
            "Its instructions were not loaded. If the task returns to this method, call get_skill without not_needed_reason."
        ))

    def _issue(self):
        pending = self._pending()
        if not pending:
            return None
        names = ", ".join(sorted(pending))
        return CompletionIssue(
            "SKILL_RECOVERY_REQUIRED",
            f"Previously read Skill instructions are missing from the current context: {names}. "
            "Before executing or completing work, call get_skill for each relevant method and follow its pages. "
            "Do not reconstruct a method or its deliverables from the summary. For an unrelated method only, "
            "call get_skill with skill_name and not_needed_reason explaining why this turn does not use it. "
            "After rereading, check all required outputs, including audit receipts and sidecar files.",
        )

    async def check_invocation(self, name: str, arguments: dict) -> CompletionIssue | None:
        if name in {"get_skill", "skill_view", "list_skills", "tool_search"}:
            return None
        self.work_attempted = True
        if name in self._PREPARATION_TOOLS:
            return None
        issue = self._issue()
        if issue is not None:
            self.context.record("skill.execution_blocked", {"tool": name, "code": issue.code,
                                                          "skills": sorted(self._pending())})
        return issue

    async def check(self) -> CompletionIssue | None:
        # A greeting or answer without tool work need not reload old methods.
        return self._issue() if self.work_attempted else None

    def snapshot(self) -> dict:
        return {"version": 1, "deferred": self.deferred, "work_attempted": self.work_attempted,
                "recovering": sorted(self.recovering)}

    @classmethod
    def restore(cls, context: CompletionContext, state: dict):
        gate = cls.create(context)
        if (gate is None or state.get("version") != 1 or not isinstance(state.get("deferred"), dict)
                or not isinstance(state.get("work_attempted"), bool)
                or not isinstance(state.get("recovering"), list)
                or not all(isinstance(name, str) for name in state["recovering"])):
            raise ValueError("Invalid Skill recovery checkpoint")
        for name, item in state["deferred"].items():
            if (not isinstance(name, str) or not isinstance(item, dict)
                    or not isinstance(item.get("identity"), dict)
                    or not all(isinstance(item["identity"].get(key), str) for key in ("revision", "source", "path"))
                    or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
                raise ValueError("Invalid Skill recovery decision")
        gate.deferred = {name: dict(item) for name, item in state["deferred"].items()}
        gate.work_attempted = state["work_attempted"]
        gate.recovering.update(state["recovering"])
        return gate
