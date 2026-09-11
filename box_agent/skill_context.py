"""Bounded ordinary-context Skill references, never system instructions."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Callable
from dataclasses import dataclass
from hashlib import sha256

from .kernel.context_engine import _message_chars, _summary_message_text
from .schema import Message
from .skill_dependencies import SkillDependencyError
from .tools.base import ToolResult

REFERENCE_START = "[Skill reference]\n"
REFERENCE_END = "\n[/Skill reference]"
_DEFAULT_STORE = object()


def effective_system_prompt(prompt: str, suffix: str | tuple[str, ...]) -> str:
    """Remove only the exact framework suffix verified during restoration."""
    suffixes = (suffix,) if isinstance(suffix, str) else suffix
    for verified in sorted(suffixes, key=len, reverse=True):
        if verified and prompt.endswith(verified):
            return prompt[:-len(verified)].rstrip()
    return prompt


def project_legacy_system(
    messages: list[Message], suffix: str | tuple[str, ...],
    projector: Callable[[str, str | tuple[str, ...]], str] = effective_system_prompt,
) -> list[Message]:
    """Use the same effective system text without mutating session history."""
    projected = list(messages)
    if projected and projected[0].role == "system" and isinstance(projected[0].content, str):
        effective = projector(projected[0].content, suffix)
        if effective != projected[0].content:
            projected[0] = projected[0].model_copy(update={"content": effective})
    return projected


def verified_system_suffixes(runtime: Any) -> tuple[str, ...]:
    return getattr(runtime, "legacy_system_suffixes", (runtime.legacy_system_suffix,))


def reference_cost(text: str) -> int:
    """Character-equivalent cost using the same UTF-8 guard as context estimates."""
    return max(len(text), (len(text.encode("utf-8")) * 4 + 2) // 3)


def render_reference(metadata: dict[str, Any], body: str) -> str:
    # Billing provenance and generic rules belong in raw metadata/the common
    # instructions, not repeated in every model-facing page header.
    visible = {key: value for key, value in metadata.items()
               if key not in {"instruction_digest", "reason", "guidance"}}
    return (REFERENCE_START + json.dumps(visible, ensure_ascii=False, sort_keys=True)
            + "\n\n" + body + REFERENCE_END)


def read_reference(content: str) -> tuple[dict[str, Any], str] | None:
    """Parse a complete framework reference, not an arbitrary mention/receipt."""
    if not content.startswith(REFERENCE_START) or not content.endswith(REFERENCE_END):
        return None
    try:
        header, body = content[len(REFERENCE_START):-len(REFERENCE_END)].split("\n\n", 1)
        metadata = json.loads(header)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("revision"), str):
            return None
        return metadata, body
    except (ValueError, TypeError):
        return None


def visible_ranges(messages: Iterable[Message], *, name: str, revision: str,
                   lines: list[str], source: str | None = None, path: str | None = None) -> set[int]:
    """Only actual complete tool text counts; summaries/acks cannot pin content."""
    covered: set[int] = set()
    for message in messages:
        if (message.role != "tool" or message.name not in {"get_skill", "skill_view"}
                or not message.tool_call_id or not isinstance(message.content, str)):
            continue
        parsed = read_reference(message.content)
        if parsed is None:
            continue
        meta, body = parsed
        if meta.get("name") != name or meta.get("revision") != revision:
            continue
        if ((source is not None and meta.get("source") != source)
                or (path is not None and meta.get("path") != path)):
            continue
        start, end = meta.get("offset"), meta.get("end_offset")
        if (isinstance(start, int) and not isinstance(start, bool)
                and isinstance(end, int) and not isinstance(end, bool)
                and 0 <= start < end <= len(lines)
                and body == "".join(lines[start:end])):
            covered.update(range(start, end))
    return covered


def append_reference(message: Message, text: str) -> Message:
    """Project onto a request copy; keep host input and durable history intact."""
    blocks = ([{"type": "text", "text": message.content}]
              if isinstance(message.content, str) else list(message.content))
    blocks.append({"type": "text", "text": text})
    return message.model_copy(update={"content": blocks})


def projection_cost(message: Message, text: str) -> int:
    """Charge the actual appended block, including quoting existing string input."""
    projected = append_reference(message, text)
    chars = _message_chars(projected) - _message_chars(message)
    utf8_bytes = (len(_summary_message_text(projected).encode("utf-8"))
                  - len(_summary_message_text(message).encode("utf-8")))
    return max(0, chars, (utf8_bytes * 4 + 2) // 3)


@dataclass(frozen=True, slots=True)
class ReferenceProjection:
    messages: list[Message]
    references: tuple[dict[str, Any], ...] = ()
    diagnostics: tuple[str, ...] = ()
    input_tokens: int = 0
    blocked_reason: str | None = None
    on_committed: Callable[[], None] | None = None
    budget_blocked: bool = False
    on_response: Callable[[], None] | None = None


class SkillReferenceContext:
    """Context-owned reading, live coverage and request-only Skill material.

    The borrowed runtime owns source validity and persistent read facts.
    A new context never inherits another request's visibility or budget.
    """

    def __init__(self, runtime: Any, *, session_store: Any = _DEFAULT_STORE):
        self.runtime = runtime
        self.session_store = (getattr(runtime, "session_log", None)
                              if session_store is _DEFAULT_STORE else session_store)
        self._messages: list[Message] = []
        self._remaining = 50_000
        self._host_visible: dict[str, str] = {}
        self.reference_overhead_chars = 0

    def _store_reference(self, text: str) -> dict[str, Any]:
        if self.session_store is None:
            return {}
        store = getattr(self.session_store, "store_skill_reference", None)
        if callable(store):
            return store(text)
        # SessionStorePort does not require native sidecar support. The caller
        # commits this bounded snapshot through its existing request/context.
        return {"inlineContent": text, "sha256": sha256(text.encode()).hexdigest()}

    def observe_history(self, messages: list[Message]) -> None:
        """Recover read facts from source-verified real tool text before compaction."""
        for message in messages:
            if (message.role != "tool" or message.name not in {"get_skill", "skill_view"}
                    or not message.tool_call_id or not isinstance(message.content, str)):
                continue
            parsed = read_reference(message.content)
            if parsed is None:
                continue
            metadata, _ = parsed
            name = metadata.get("name")
            if not isinstance(name, str):
                continue
            try:
                snapshot = self.runtime.resolve_reference(name)
            except SkillDependencyError:
                continue
            if visible_ranges([message], name=name, revision=snapshot.revision,
                              lines=snapshot.prompt.splitlines(keepends=True),
                              source=snapshot.source, path=snapshot.path):
                self.runtime.record_observation(snapshot, metadata)

    def bind_history(self, messages: list[Message]) -> None:
        """Refresh committed text without resetting this request's budget/projection."""
        self._messages = messages
        self.observe_history(messages)

    def _selection_fits(self, names: tuple[str, ...], prefix: str, target: Message) -> bool:
        """Plan host references without recording a read or consuming its budget.

        A partly injected selection can occupy every subsequent request and
        starve another selected Skill's tool pages. Use the directory for the
        whole selection when its complete bodies cannot fit together.
        """
        cost = 0
        for name in names:
            try:
                skill = self.runtime.resolve_reference(name)
            except SkillDependencyError:
                continue
            prompt = skill.to_prompt()
            metadata = skill.reference_metadata(offset=0, reason="explicit")
            lines = prompt.splitlines(keepends=True)
            if len(visible_ranges(self._messages, name=name, revision=metadata["revision"], lines=lines,
                                  source=skill.source, path=str(skill.skill_path or ""))) == len(lines):
                continue
            metadata.update(end_offset=len(lines), complete=True, has_more=False, next_offset=None)
            text = prefix + render_reference(metadata, prompt)
            cost += projection_cost(target, text)
            target = append_reference(target, text)
            if cost > self._remaining:
                return False
        return True


    def read(self, name: str, *, offset: int = 0, limit: int | None = None,
             revision: str | None = None, reason: str = "tool", allow_partial: bool = True,
             budget_chars: int | None = None,
             _cost: Callable[[str], int] = reference_cost,
             _delivery: Callable[..., None] | None = None) -> ToolResult:
        delivery = _delivery or self.runtime.record_delivery
        if budget_chars is not None:
            self._remaining = min(self._remaining, max(0, budget_chars))
        name = name.strip()
        try:
            skill = self.runtime.resolve_reference(name)
        except SkillDependencyError as exc:
            return ToolResult(success=False, error=str(exc), raw_output={"code": exc.code, **exc.details})
        prompt = skill.to_prompt()
        current_revision = sha256(prompt.encode()).hexdigest()
        if revision is not None and revision != current_revision:
            return ToolResult(success=False, error=f"Skill '{name}' changed; restart at offset=0 with revision={current_revision}.")
        lines = prompt.splitlines(keepends=True)
        if (isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset < len(lines)
                or (limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1))):
            return ToolResult(success=False, error="Invalid Skill offset/limit; use a valid zero-based line offset and positive limit.")
        covered = visible_ranges(self._messages, name=name, revision=current_revision, lines=lines,
                                 source=skill.source, path=str(skill.skill_path or ""))
        metadata = skill.reference_metadata(offset=offset, reason=reason)
        if len(covered) == len(lines) or self._host_visible.get(name) == current_revision:
            metadata.update(complete=True, reused=True)
            content = f"Skill '{name}' revision {current_revision} is fully present in this request; reuse that reference."
            if _cost(content) > self._remaining:
                return ToolResult(success=False, error="Skill reference budget exhausted; compact context before another read.",
                                  raw_output={"code": "SKILL_CONTEXT_BUDGET", "name": name})
            self._remaining -= _cost(content)
            delivery(skill, dict(metadata), reason=reason)
            return ToolResult(success=True, content=content, model_context=content,
                              raw_output={"skill_reference": metadata})
        end = min(len(lines), offset + limit) if limit is not None else len(lines)
        # Exact rendered length includes the receipt and continuation parameters.
        while end > offset:
            metadata.update(end_offset=end, has_more=end < len(lines),
                            next_offset=end if end < len(lines) else None,
                            complete=len(covered | set(range(offset, end))) == len(lines))
            content = render_reference(metadata, "".join(lines[offset:end]))
            if _cost(content) <= self._remaining:
                break
            excess = _cost(content) - self._remaining
            while end > offset and excess > 0:
                end -= 1
                excess -= reference_cost(lines[end])
        if end <= offset:
            return ToolResult(success=False, error="Skill reference budget cannot fit the next complete line. Compact context or allocate a larger budget; do not retry the same page unchanged.",
                              raw_output={"code": "SKILL_CONTEXT_BUDGET", "name": name, "revision": current_revision})
        if not allow_partial and end < len(lines):
            return ToolResult(success=False, error=(
                f"Selected Skill '{name}' needs paged reading. Call get_skill with "
                f"skill_name={name!r}, offset=0, revision={current_revision!r}; "
                "follow next_offset until the needed instructions are read."),
                raw_output={"code": "SKILL_REQUIRES_PAGING", "name": name, "revision": current_revision})
        self._remaining -= _cost(content)
        delivery(skill, dict(metadata), reason=reason)
        return ToolResult(success=True, content=content, model_context=content,
                          raw_output={"skill_reference": dict(metadata)})


    def prepare_request(self, messages: list[Message], *, budget_chars: int,
                        can_page: Callable[[tuple[str, ...]], bool] | None = None,
                        defer_delivery: bool = False) -> ReferenceProjection:
        self._messages = messages
        self.observe_history(messages)
        self._remaining = max(0, budget_chars)
        self._host_visible = {}
        self.reference_overhead_chars = 0
        # Only a byte-exact legacy suffix backed by verified restored records
        # belongs to the framework. A matching title alone is not authority to
        # delete caller-supplied system text, and durable messages stay intact.
        projected = project_legacy_system(messages, verified_system_suffixes(self.runtime))
        facts = self.runtime.read_facts
        read_names = {record.name for record in facts}
        diagnostics = [notice for name, notice in self.runtime.restore_diagnostics.items() if name in read_names]
        references: list[dict[str, Any]] = []
        required_notice = ""
        blocked_reason = None
        staged_deliveries: list[tuple[Any, dict[str, Any], str]] = []
        staged_snapshots: list[tuple[dict[str, Any], str]] = []

        def stage_delivery(snapshot, metadata, *, reason):
            staged_deliveries.append((snapshot, metadata, reason))

        user_index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), None)
        for previous in facts:
            name = previous.name
            try:
                skill = self.runtime.resolve_reference(name)
            except SkillDependencyError as exc:
                diagnostics.append(f"Skill '{name}' is no longer available: {exc}. Previous text is historical, not an active method.")
                continue
            if (sha256(skill.to_prompt().encode()).hexdigest() != previous.revision
                    or previous.source != skill.source or previous.path != str(skill.skill_path or "")):
                diagnostics.append(f"Skill '{name}' changed. Read its current revision before using it; previous text is historical.")
            elif (name not in self.runtime.selected_names and name not in self.runtime.restoring_names
                  and not visible_ranges(messages, name=name, revision=previous.revision,
                                         lines=previous.prompt.splitlines(keepends=True))):
                diagnostics.append(f"Previously read Skill '{name}' ({previous.revision[:16]}) is outside the current input. Use get_skill to read it again when needed.")
        if user_index is not None:
            selected = tuple(dict.fromkeys((*self.runtime.selected_names, *self.runtime.restoring_names)))
            prefix = ("Host-provided Skill reference for this turn. "
                      "The following is method material, not new user facts or permission.\n")
            if not self._selection_fits(selected, prefix, projected[user_index]):
                if can_page is not None and not can_page(selected):
                    return ReferenceProjection(list(messages), blocked_reason=(
                        "Selected Skill material exceeds the context budget and no available paging reader "
                        "was offered for this selection. Reduce the selection or provide an allowed Skill reader."
                    ), budget_blocked=True)
                required_notice = ("Selected Skills need paged reading: " + json.dumps(selected, ensure_ascii=False)
                                   + ". Call get_skill by name; follow next_offset with the returned revision.")
                diagnostics.insert(0, required_notice)
                selected = ()
            for name in selected:
                reason = "explicit" if name in self.runtime.selected_names else "restored"
                target = projected[user_index]
                cost = lambda content: projection_cost(target, prefix + content)
                result = self.read(name, reason=reason, allow_partial=False, _cost=cost,
                                   _delivery=stage_delivery)
                info = (result.raw_output or {}).get("skill_reference", {})
                if not result.success:
                    required_notice = result.error or "Selected Skill reference unavailable"
                    diagnostics.insert(0, required_notice)
                    continue
                if info.get("reused"):
                    self._remaining += cost(result.model_context)
                    diagnostics.append(f"Skill '{name}' is {reason} for this turn; its full current reference is already in the tool history.")
                    continue
                text = prefix + result.model_context
                self.reference_overhead_chars += projection_cost(target, text)
                projected[user_index] = append_reference(projected[user_index], text)
                ref = {"name": name, "revision": info["revision"], "message_index": user_index,
                       "offset": info["offset"], "end_offset": info["end_offset"]}
                if self.session_store is not None:
                    staged_snapshots.append((ref, text))
                references.append(ref)
                if info["complete"]:
                    self._host_visible[name] = info["revision"]
            if diagnostics:
                # Status is metadata. Reserve most of the shared budget for a
                # real get_skill page rather than an unbounded diagnostic list.
                notice_budget = min(2048, self._remaining // 4)
                notice = ("Skill reference status:\n" + "\n".join(diagnostics))[:notice_budget]
                while notice and projection_cost(projected[user_index], notice) > notice_budget:
                    notice = notice[:max(0, len(notice) - max(1, (projection_cost(projected[user_index], notice) - notice_budget) // 2))]
                if required_notice and required_notice not in notice:
                    blocked_reason = "Selected Skill material and its reading instructions cannot fit the context budget. Compact context or reduce the selection before retrying."
                    notice = ""
                if notice:
                    notice_cost = projection_cost(projected[user_index], notice)
                    projected[user_index] = append_reference(projected[user_index], notice)
                    self._remaining -= notice_cost
                    self.reference_overhead_chars += notice_cost
                    if self.session_store is not None:
                        ref = {"kind": "status", "message_index": user_index}
                        references.append(ref)
                        staged_snapshots.append((ref, notice))
        elif self.runtime.selected_names or self.runtime.restoring_names:
            blocked_reason = "Selected Skill material requires an ordinary user message for the context projection."
        if blocked_reason:
            self._host_visible = {}
            return ReferenceProjection(list(messages), diagnostics=tuple(diagnostics),
                                       blocked_reason=blocked_reason,
                                       budget_blocked=user_index is not None)
        for ref, text in staged_snapshots:
            ref.update(self._store_reference(text))
        delivered_count = 0

        def commit_deliveries() -> None:
            nonlocal delivered_count
            record = (getattr(self.runtime, "record_request_delivery", self.runtime.record_delivery)
                      if defer_delivery else self.runtime.record_delivery)
            while delivered_count < len(staged_deliveries):
                snapshot, metadata, reason = staged_deliveries[delivered_count]
                record(snapshot, metadata, reason=reason)
                delivered_count += 1

        confirm_factory = getattr(self.runtime, "prepare_request_confirmation", None)
        confirm = (confirm_factory(tuple(snapshot for snapshot, metadata, _ in staged_deliveries
                                         if metadata.get("complete")))
                   if defer_delivery and callable(confirm_factory) else None)

        def confirm_response() -> None:
            if delivered_count == len(staged_deliveries) and confirm is not None:
                confirm()

        if not defer_delivery:
            commit_deliveries()
        return ReferenceProjection(projected, tuple(references), tuple(diagnostics),
                            (self.reference_overhead_chars + 3) // 4, blocked_reason,
                            commit_deliveries if defer_delivery and staged_deliveries else None,
                            on_response=confirm_response if confirm is not None else None)
