"""Session Skill sources, selection and durable delivery facts."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping, MutableSequence
from typing import Any
from hashlib import sha256
from types import SimpleNamespace

from .skill_dependencies import SkillDependencyError, resolve_required_skills
from .skill_state import SkillReferenceSnapshot, SkillRead, SkillSessionState
from .skill_restore import invalid_restore, recover_available_records, validate_restore_records
from .tools.base import ToolResult

from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import (
    AutoLoadedSkillsPrompt,
    build_auto_loaded_skills_prompt,
)


class SkillRuntime:
    """Borrow the loader; own source validity, selection and delivery facts."""

    def __init__(self, loader: SkillLoader | None, *, session_log: Any = None,
                 allow_partial_restore: bool = False):
        self.loader = loader
        self.session_log = session_log
        self._allow_partial_restore = allow_partial_restore
        self.state = SkillSessionState()
        self.turn_deliveries: dict[str, dict[str, Any]] = {}
        self._restore_pending: tuple[str, ...] = ()
        self._restoring: tuple[str, ...] = ()
        self._restore_diagnostics: dict[str, str] = {}
        self._legacy_system_suffix = ""
        self._legacy_system_suffixes: tuple[str, ...] = ()
        self._restore_generation = 0

    def begin_turn(self) -> None:
        self.turn_deliveries.clear()
        # A rejected request has not delivered its restored methods. Retain
        # them across retries until a complete read or explicit removal.
        self._restoring = self._restore_pending

    def select(self, names: list[str] | tuple[str, ...]) -> None:
        self.state.selected = tuple(dict.fromkeys(name.strip() for name in names if name.strip()))

    def deactivate_reference(self, name: str) -> bool:
        """Withdraw an active or pending host reference without editing history."""
        removed = name in (*self.state.reads, *self.state.selected,
                           *self._restore_pending, *self._restoring)
        self.state.reads.pop(name, None)
        self.select([item for item in self.state.selected if item != name])
        self._restore_pending = tuple(item for item in self._restore_pending if item != name)
        self._restoring = tuple(item for item in self._restoring if item != name)
        self._restore_diagnostics.pop(name, None)
        self.turn_deliveries.pop(name, None)
        return removed

    def clear_references(self) -> None:
        self.state = SkillSessionState()
        self._restore_pending = self._restoring = ()
        self._restore_diagnostics.clear()
        self.turn_deliveries.clear()
        # Keep the verified legacy suffix as evidence for request-only
        # stripping; forgetting it could expose old author text as system.

    @property
    def active_names(self) -> tuple[str, ...]:
        if self.loader is not None:
            self.loader.maybe_reload()
        names = []
        for name in dict.fromkeys((*self.state.reads, *self.state.selected)):
            try:
                skill = self._resolve(name)
            except SkillDependencyError:
                continue
            previous = self.state.reads.get(name)
            if previous is not None and (sha256(skill.to_prompt().encode()).hexdigest() != previous.revision
                                         or previous.source != skill.source or previous.path != str(skill.skill_path or "")):
                continue
            names.append(name)
        return tuple(names)

    def _resolve(self, name: str) -> Any:
        record = self.state.reads.get(name)
        if record is not None and record.source == "caller":
            return SimpleNamespace(name=name, source="caller", skill_path=record.path,
                                   required_skills=[], related_skills=[], broken=False,
                                   to_prompt=lambda: record.prompt)
        if self.loader is None:
            raise SkillDependencyError("SKILL_PROVIDER_UNAVAILABLE", "No Skill source is configured.")
        resolve_required_skills(self.loader, [name])
        return self.loader.get_skill(name)

    def register_reference(self, name: str, prompt: str, *, expected_hash: str | None = None,
                           order: int | None = None, persist: bool = True) -> None:
        """Compatibility for an explicit host-provided reference, never system text."""
        revision = sha256(prompt.encode()).hexdigest()
        previous = self.state.reads.get(name)
        self.select([*self.state.selected, name])
        if previous is not None and previous.revision == revision and previous.prompt == prompt:
            return
        self._restore_diagnostics.pop(name, None)
        if expected_hash is not None and revision != expected_hash:
            self._restore_diagnostics[name] = self._restore_notice(
                name, {"sha256": expected_hash}, revision, "caller", "",
            )
        self.state.sequence = max(self.state.sequence + 1, order or 0)
        self.state.reads[name] = SkillRead(name, "caller", "", revision, prompt,
                                         order or self.state.sequence, "explicit")
        if persist and self.session_log is not None:
            self.session_log.append("skill/change", {"skills": self.log_records()})
            self.session_log.flush()

    def restore_records(self, records: list[dict[str, Any]]) -> None:
        """Resolve current valid references atomically, without rewriting history.

        Historical hashes describe the original read. An upgrade may change
        the effective source or body; that relationship is ordinary metadata,
        not a reason to prevent the session from continuing.
        """
        recovered = False
        if self._allow_partial_restore:
            available = recover_available_records(records, self.loader)
            recovered = available != records
            records = available
        validate_restore_records(records)
        if self.loader is not None:
            self.loader.maybe_reload()
        restored: dict[str, SkillRead] = {}
        diagnostics: dict[str, str] = {}
        historical_bodies_verified = not recovered
        for record in sorted(records, key=lambda row: row["loadOrder"]):
            name = record["name"]
            if self.loader is not None:
                # A prior caller reference must not bypass current source and
                # required-dependency validity during session restoration.
                skill = resolve_required_skills(self.loader, [name])[-1]
                prompt, source, path = skill.to_prompt(), skill.source, str(skill.skill_path or "")
            elif isinstance(record.get("prompt"), str):
                # The old public tuple API supplies current host text when no
                # Loader exists. It is never a historical snapshot assertion.
                prompt, source, path = record["prompt"], "caller", ""
            else:
                raise SkillDependencyError("SKILL_PROVIDER_UNAVAILABLE", "No Skill source is configured.")
            revision = sha256(prompt.encode()).hexdigest()
            same_body = revision == record["sha256"]
            if same_body and any(end > len(prompt.splitlines())
                                 for _, end in record.get("deliveredRanges", ())):
                raise invalid_restore("deliveredRanges exceeds the verified source length")
            historical_bodies_verified = historical_bodies_verified and same_body
            changed = (not same_body
                       or ("source" in record and record["source"] != source)
                       or ("path" in record and record["path"] != path))
            if changed:
                diagnostics[name] = self._restore_notice(name, record, revision, source, path)
            restored[name] = SkillRead(
                name, source, path, revision, prompt, record["loadOrder"], "restored",
                () if changed else tuple(tuple(pair) for pair in record.get("deliveredRanges", ())),
                False if changed else record.get("deliveredComplete", True),
            )
        from .tools.skill_preload import build_active_skills_prompt
        suffix = (build_active_skills_prompt("", {name: item.prompt for name, item in restored.items()})
                  if historical_bodies_verified else "")
        self.state = SkillSessionState(reads=restored,
                                       sequence=max((item.order for item in restored.values()), default=0))
        self._restore_diagnostics = diagnostics
        self._restore_pending = tuple(restored)
        self._restoring = ()
        self._restore_generation += 1
        if suffix:
            self._legacy_system_suffix = suffix
            self._legacy_system_suffixes = tuple(dict.fromkeys((*self._legacy_system_suffixes, suffix)))

    @staticmethod
    def _restore_notice(name: str, historical: dict[str, Any], revision: str, source: str, path: str) -> str:
        return (f"Skill '{name}' used the current valid reference at session restoration: "
                f"historical sha256={historical['sha256']}; current sha256={revision}. "
                f"Historical source={historical.get('source', 'unspecified')} path={historical.get('path', 'unspecified')}; "
                f"current source={source} path={path}. Historical logs remain unchanged; "
                "the restored reference does not prove the historical source or version.")


    def _remember(self, skill: Any, prompt: str, reason: str, metadata: dict[str, Any]) -> None:
        revision = sha256(prompt.encode()).hexdigest()
        old = self.state.reads.get(skill.name)
        ranges = set(old.delivered_ranges) if (old is not None and old.revision == revision
                  and old.source == skill.source and old.path == str(skill.skill_path or "")) else set()
        ranges.add((metadata["offset"], metadata["end_offset"]))
        coverage = {line for start, end in ranges for line in range(start, end)}
        self.state.sequence += 1
        self.state.reads[skill.name] = SkillRead(
            skill.name, skill.source, str(skill.skill_path or ""), revision,
            prompt, self.state.sequence, reason, tuple(sorted(ranges)),
            len(coverage) == metadata["total_lines"],
        )
        if self.session_log is not None:
            self.session_log.append("skill/change", {"skills": self.log_records()})
            self.session_log.flush()

    def log_records(self) -> list[dict[str, Any]]:
        return [read.log_record() for read in sorted(self.state.reads.values(), key=lambda r: r.order)]

    @property
    def read_facts(self) -> tuple[SkillRead, ...]:
        return tuple(self.state.reads.values())

    @property
    def selected_names(self) -> tuple[str, ...]:
        return self.state.selected

    @property
    def restoring_names(self) -> tuple[str, ...]:
        return self._restoring

    @property
    def restore_diagnostics(self) -> dict[str, str]:
        return dict(self._restore_diagnostics)

    @property
    def legacy_system_suffix(self) -> str:
        return self._legacy_system_suffix

    @property
    def legacy_system_suffixes(self) -> tuple[str, ...]:
        """Keep previously verified history boundaries across later restores."""
        return self._legacy_system_suffixes

    def resolve_reference(self, name: str) -> SkillReferenceSnapshot:
        """Validate the effective source and return data without recording a read."""
        import json

        if self.loader is not None:
            self.loader.maybe_reload()
        skill = self._resolve(name.strip())
        prompt = skill.to_prompt()
        digest = getattr(skill, "instruction_digest", None)
        if digest and skill.skill_path:
            try:
                if sha256(skill.skill_path.read_bytes()).hexdigest() != digest:
                    raise SkillDependencyError("SKILL_SOURCE_CHANGED", "Skill source changed during reading. Refresh and retry from offset=0.")
            except OSError as exc:
                raise SkillDependencyError("SKILL_SOURCE_UNAVAILABLE", f"Skill source became unreadable: {exc}") from exc
        metadata = self._metadata(skill, prompt, offset=0, reason="tool")
        return SkillReferenceSnapshot(skill.name, skill.source, str(skill.skill_path or ""),
                                      metadata["revision"], prompt, json.dumps(metadata, ensure_ascii=False))

    def record_delivery(self, snapshot: SkillReferenceSnapshot, metadata: dict[str, Any], *, reason: str) -> None:
        """Record returned material; this says nothing about current visibility."""
        if reason != "restored" and not metadata.get("reused"):
            self._remember(snapshot, snapshot.prompt, reason, metadata)
        self.turn_deliveries[snapshot.name] = dict(metadata)
        if metadata.get("complete"):
            self._restore_pending = tuple(name for name in self._restore_pending if name != snapshot.name)

    def record_request_delivery(self, snapshot: SkillReferenceSnapshot, metadata: dict[str, Any], *, reason: str) -> None:
        """Record a logged request while retaining retryable restoration work."""
        pending = self._restore_pending
        try:
            self.record_delivery(snapshot, metadata, reason=reason)
        finally:
            # A partial batch or failed provider call must not consume a
            # subset of the methods needed by the next retry.
            self._restore_pending = pending

    def prepare_request_confirmation(self, snapshots: tuple[SkillReferenceSnapshot, ...]) -> Callable[[], None]:
        """Confirm only the restored revisions actually used by this response."""
        generation = self._restore_generation

        def confirm() -> None:
            if generation != self._restore_generation:
                return
            confirmed = set()
            for snapshot in snapshots:
                current = self.state.reads.get(snapshot.name)
                if (current is not None and current.revision == snapshot.revision
                        and current.source == snapshot.source and current.path == snapshot.path):
                    confirmed.add(snapshot.name)
            self._restore_pending = tuple(name for name in self._restore_pending if name not in confirmed)

        return confirm

    def record_observation(self, snapshot: SkillReferenceSnapshot, metadata: dict[str, Any]) -> None:
        """Recover validated historical delivery facts without rewriting logs."""
        old = self.state.reads.get(snapshot.name)
        ranges = set(old.delivered_ranges) if (old and old.revision == snapshot.revision
                  and old.source == snapshot.source and old.path == snapshot.path) else set()
        pair = (metadata["offset"], metadata["end_offset"])
        if pair in ranges:
            return
        ranges.add(pair)
        self.state.sequence += 1
        self.state.reads[snapshot.name] = SkillRead(
            snapshot.name, snapshot.source, snapshot.path, snapshot.revision, snapshot.prompt,
            self.state.sequence, "history", tuple(sorted(ranges)),
            len({line for start, end in ranges for line in range(start, end)}) == len(snapshot.prompt.splitlines()),
        )

    def read(self, name: str, **kwargs: Any) -> ToolResult:
        """Standalone compatibility read; request state belongs to its Context."""
        from .skill_context import SkillReferenceContext

        return SkillReferenceContext(self).read(name, **kwargs)

    def _metadata(self, skill: Any, prompt: str, *, offset: int, reason: str) -> dict[str, Any]:
        metadata = {"name": skill.name, "source": skill.source, "path": str(skill.skill_path or ""),
                "revision": sha256(prompt.encode()).hexdigest(), "offset": offset, "end_offset": offset,
                "instruction_digest": getattr(skill, "instruction_digest", None),
                "skill_version": str((getattr(skill, "metadata", None) or {}).get("version", "")).strip(),
                "total_lines": len(prompt.splitlines()), "complete": False, "has_more": False,
                "required_skills": list(skill.required_skills or []),
                "related_skills": list(skill.related_skills or []), "reason": reason,
                "guidance": "Method reference only; required_skills must be read before their steps. This does not grant tools or permission."}
        dependencies = []
        for name in dict.fromkeys([*metadata["required_skills"], *metadata["related_skills"]]):
            related = self.loader.get_skill(name) if self.loader is not None else None
            description = str(getattr(related, "description", ""))
            dependencies.append({"name": name, "required": name in metadata["required_skills"],
                                 "description": description[:160] + ("… (list_skills for details)" if len(description) > 160 else ""),
                                 "source_available": related is not None and not related.broken})
        if dependencies:
            metadata["dependencies"] = dependencies
        return metadata



def prepare_auto_loaded_skills(
    skill_loader: SkillLoader,
    system_prompt: str,
    skill_names: list[str] | tuple[str, ...],
    *,
    include_disabled: bool = False,
    preloaded_skill_names: MutableSequence[str],
    preloaded_skill_hashes: MutableMapping[str, str],
    preloaded_skill_attributions: MutableMapping[str, Any] | None = None,
    prompt_builder: Callable[..., AutoLoadedSkillsPrompt] = build_auto_loaded_skills_prompt,
) -> tuple[AutoLoadedSkillsPrompt, set[str]]:
    """Legacy pure helper retained for external callers and historical tests.

    Default Agent, CLI, ACP and Tool Engine paths use SkillRuntime instead.
    This helper is not a supported way to inject Skill bodies into a run.
    """

    result = prompt_builder(
        skill_loader,
        system_prompt,
        skill_names,
        include_disabled=include_disabled,
    )
    unloaded_skill_names = apply_auto_loaded_skill_state(
        result,
        preloaded_skill_names=preloaded_skill_names,
        preloaded_skill_hashes=preloaded_skill_hashes,
        preloaded_skill_attributions=preloaded_skill_attributions,
    )
    return result, unloaded_skill_names


def apply_auto_loaded_skill_state(
    result: AutoLoadedSkillsPrompt,
    *,
    preloaded_skill_names: MutableSequence[str],
    preloaded_skill_hashes: MutableMapping[str, str],
    preloaded_skill_attributions: MutableMapping[str, Any] | None = None,
) -> set[str]:
    """Apply one preload result while preserving collection identities.

    Adapters retain warning, logging, prompt replacement, and host metadata
    decisions.  This helper only performs the shared state transition and
    returns names that were removed so each host can render them as before.
    """

    previous_skill_names = set(preloaded_skill_names)
    preloaded_skill_names[:] = result.loaded_names
    preloaded_skill_hashes.clear()
    preloaded_skill_hashes.update(result.loaded_skill_hashes)
    if preloaded_skill_attributions is not None:
        preloaded_skill_attributions.clear()
        preloaded_skill_attributions.update(
            {
                attribution.skill_name: attribution
                for attribution in result.loaded_attributions
            }
        )
    return previous_skill_names - set(result.loaded_names)


__all__ = ["SkillRuntime", "apply_auto_loaded_skill_state", "prepare_auto_loaded_skills"]
