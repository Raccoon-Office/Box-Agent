"""Preflight the concrete Skill and tool selections supplied by expert clients.

The desktop editor saves its selected Skills as default/required Skills. Older
drafts can also repeat them in optionalSkills; that field is not a request to
silently drop a selected dependency. This module does not install resources or
change the host's profile.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .experts import ExpertSessionContext
from .skill_dependencies import SkillDependencyError, resolve_required_skills
from .tools.skill_loader import Skill, SkillLoader
from .tools.skillhub_install_tool import is_installed_market_skill


class ExpertDependencyError(ValueError):
    code = "EXPERT_DEPENDENCIES_NOT_READY"

    def __init__(self, dependencies: list[dict[str, Any]]) -> None:
        super().__init__("Expert dependencies are not ready.")
        self.dependencies = dependencies


def _profiles(meta: Mapping[str, Any], context: ExpertSessionContext):
    """Pair assembled profiles with host resource bindings, without reselecting Skills."""
    if context.expert is not None:
        raw = meta.get("expert")
        raw = raw if isinstance(raw, dict) else {}
        yield context.expert, raw, raw.get("packageSnapshot")
    if context.team is None:
        return
    team = meta.get("expert_team") or meta.get("expertTeam")
    team = team if isinstance(team, dict) else {}
    if context.team.leader is not None:
        raw = team.get("leader")
        raw = raw if isinstance(raw, dict) else {}
        yield context.team.leader, raw, raw.get("packageSnapshot", team.get("packageSnapshot"))
    # The context already owns normalization and member limits. Match only the
    # surviving raw objects in source order to retain each member's resource identity.
    raw_members = (
        member for member in team.get("members", [])
        if isinstance(member, dict) and isinstance(member.get("name"), str) and member["name"].strip()
    )
    for profile, raw in zip(context.team.members, raw_members):
        yield profile, raw, raw.get("packageSnapshot", team.get("packageSnapshot"))


def expert_market_skill_bindings(
    raw_meta: Mapping[str, Any], context: ExpertSessionContext, *, required_only: bool = False,
) -> dict[str, frozenset[str]]:
    """Keep selected market identities on the session loader, including optional Skills."""
    identities: dict[str, set[str]] = {}
    for profile, raw, _ in _profiles(raw_meta, context):
        names = {*getattr(profile, "required_skills", ()), *profile.default_skills}
        if not required_only:
            names.update(getattr(profile, "optional_skills", ()))
        declared = raw.get("skillBindings")
        for binding in declared if isinstance(declared, list) else ():
            if (not isinstance(binding, dict) or binding.get("source") != "MARKET"
                    or not isinstance(binding.get("slug"), str) or binding["slug"] not in names):
                continue
            skill_id = binding.get("skillId")
            # An invalid optional identity is unavailable, never a same-name fallback.
            identities.setdefault(binding["slug"], set()).add(skill_id if isinstance(skill_id, str) else "")
    return {name: frozenset(values) for name, values in identities.items()}


def resolve_optional_market_conflicts(
    raw_meta: Mapping[str, Any], context: ExpertSessionContext,
) -> ExpertSessionContext:
    """Omit optional bindings that conflict with one required market identity.

    Readers address one session-wide Skill by name. Keep the required identity
    and report the omitted optional contract rather than substituting its body.
    Required/required and optional/optional conflicts remain fail-closed.
    """
    required = expert_market_skill_bindings(raw_meta, context, required_only=True)
    replacements = {}
    unavailable = list(context.unavailable_optional_skills)
    for profile, raw, _ in _profiles(raw_meta, context):
        optional = set(profile.optional_skills) - {*getattr(profile, "required_skills", ()), *profile.default_skills}
        conflicts = {}
        declared = raw.get("skillBindings")
        for binding in declared if isinstance(declared, list) else ():
            if not isinstance(binding, dict) or binding.get("source") != "MARKET":
                continue
            name = binding.get("slug")
            if not isinstance(name, str) or name not in optional:
                continue
            identities = required.get(name, ())
            required_id = next(iter(identities)) if len(identities) == 1 else None
            if required_id and binding.get("skillId") != required_id:
                conflicts[name] = {
                    "expertId": profile.id, "expertName": profile.name, "name": name,
                    "skillId": binding["skillId"] if isinstance(binding.get("skillId"), str) else "",
                    "requiredSkillId": required_id,
                    "reason": (
                        "Optional binding conflicts with the required market identity. "
                        "Do not substitute the same-name Skill for this optional binding."
                    ),
                }
        if conflicts:
            replacements[id(profile)] = replace(
                profile, optional_skills=[name for name in profile.optional_skills if name not in conflicts],
            )
            unavailable.extend(conflicts.values())
    if not replacements:
        return context
    team = context.team
    if team is not None:
        team = replace(team, leader=replacements.get(id(team.leader), team.leader),
                       members=[replacements.get(id(member), member) for member in team.members])
    return replace(context, expert=replacements.get(id(context.expert), context.expert), team=team,
                   unavailable_optional_skills=unavailable)


def _tool_names(tools: Mapping[str, Any] | Iterable[Any]) -> set[str]:
    if isinstance(tools, Mapping):
        return {name for name, tool in tools.items() if isinstance(name, str) and tool is not None and tool is not False}
    return {tool if isinstance(tool, str) else tool.name for tool in tools
            if isinstance(tool, str) or isinstance(getattr(tool, "name", None), str)}


def _bundled_binding(
    name: str, binding: Mapping[str, Any], snapshot: Any,
    context: ExpertSessionContext, loader: SkillLoader, selected: Skill,
) -> tuple[str, str] | None:
    """Check declared package identity without inventing per-member routing.

    Official teams can contain several declared copies of the same Skill name.
    Each binding must be intact, while the loader retains its existing source
    precedence among those copies.
    """
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("directory"), str):
        return "missing", "The bound expert package is not prepared."
    skills = snapshot.get("skills")
    if not isinstance(skills, list) or not isinstance(binding.get("key"), str) or not binding["key"]:
        return "conflict", "The package Skill binding has no valid key."
    matches = [item for item in skills if isinstance(item, dict)
               and item.get("key") == binding["key"] and item.get("slug") == name]
    if len(matches) != 1 or not isinstance(matches[0].get("directory"), str):
        return "conflict", "The Skill key and name do not identify one prepared package resource."
    try:
        root = Path(snapshot["directory"])
        directory = Path(matches[0]["directory"])
        if not root.is_absolute() or not directory.is_absolute():
            return "conflict", "The prepared package requires absolute resource paths."
        root = root.resolve(strict=True)
        directory = directory.resolve(strict=True)
        path = directory / "SKILL.md"
        if (not directory.is_relative_to(root) or not path.resolve(strict=True).is_relative_to(root)
                or context.skill_bindings_by_path.get(path) != name
                or name not in context.skill_bindings_by_directory.get(directory, ())):
            return "conflict", "The Skill is outside its explicit package binding."
        expected = loader.load_skill(path, source="expert")
        if expected is None or expected.broken or expected.name != name:
            return "broken", "The bound package Skill cannot be loaded."
        if (selected.source != "expert" or selected.skill_path is None
                or context.skill_bindings_by_path.get(selected.skill_path) != name):
            return "conflict", "The selected Skill is not a bound resource of this expert session."
    except (OSError, RuntimeError, ValueError):
        return "missing", "The bound package Skill is unavailable."
    return None


def validate_expert_dependencies(
    raw_meta: Mapping[str, Any], expert_context: ExpertSessionContext | None,
    skill_loader: SkillLoader | None, tools: Mapping[str, Any] | Iterable[Any],
    *, skill_access_filter: Callable[[Skill], bool] | None = None,
) -> None:
    """Reject unavailable client-selected dependencies before session commit.

    Only default/required selections and their explicit bindings are checked;
    optionalSkills alone does not expand the required set. Tools are the actual
    session tools, not a list of everything the runtime could theoretically add.
    Market bindings reuse the host installation marker checked by the existing
    SkillHub install tool. Skill bodies and same-name matches are not receipts.
    """
    if expert_context is None:
        return
    failures: list[dict[str, Any]] = []
    available_tools = _tool_names(tools)
    for profile, raw, snapshot in _profiles(raw_meta, expert_context):
        names = dict.fromkeys([*getattr(profile, "required_skills", ()), *profile.default_skills])
        declared = raw.get("skillBindings")
        declared = declared if isinstance(declared, list) else []
        for name in names:
            bindings = [binding for binding in declared if isinstance(binding, dict) and binding.get("slug") == name]
            # Identical duplicate declarations are harmless, conflicting ones
            # cannot be resolved merely by choosing the first matching name.
            invalid = any(value is not None and not isinstance(value, str)
                          for binding in bindings for value in (binding.get("source"), binding.get("skillId"), binding.get("key")))
            distinct = set() if invalid else {
                (binding.get("source"), binding.get("skillId"), binding.get("key")) for binding in bindings
            }
            binding = bindings[0] if bindings else {}
            source = binding.get("source")
            failure: tuple[str, str] | None = None
            dependency_error: SkillDependencyError | None = None
            dependency_name: str | None = None
            if invalid:
                failure = "conflict", "The selected dependency has an invalid binding identity."
            elif len(distinct) > 1:
                failure = "conflict", "The selected dependency has conflicting bindings."
            elif source == "TOOL":
                if name not in available_tools:
                    failure = "unavailable", "The required tool is not enabled in this session."
            elif source not in {None, "BUILTIN", "BUNDLED", "MARKET"}:
                failure = "unavailable", "The selected dependency source is not supported."
            else:
                selected = skill_loader.get_skill(name) if skill_loader is not None else None
                if selected is None:
                    disabled = skill_loader.get_skill(name, include_disabled=True) if skill_loader is not None else None
                    failure = ("disabled", "The selected Skill is not enabled for this session.") if disabled is not None else (
                        "missing", "The selected Skill is not available in the cloud runtime.")
                elif source == "MARKET" and not is_installed_market_skill(selected, binding.get("skillId")):
                    failure = "unavailable", "The selected Skill has no matching market installation identity."
                elif selected.broken:
                    failure = "broken", "The selected Skill cannot be loaded."
                elif source == "BUILTIN" and selected.source != "builtin":
                    failure = "conflict", "A same-name Skill from another source cannot satisfy the builtin binding."
                elif source == "BUNDLED":
                    failure = _bundled_binding(name, binding, snapshot, expert_context, skill_loader, selected)
                if failure is None and selected is not None:
                    try:
                        for dependency in resolve_required_skills(skill_loader, [name]):
                            # A binding omitted from optionalSkills may still be
                            # required transitively by this profile, not another's.
                            if dependency.name != name and any(
                                not isinstance(candidate.get("skillId"), str)
                                or not is_installed_market_skill(dependency, candidate["skillId"])
                                for candidate in declared
                                if isinstance(candidate, dict)
                                and candidate.get("source") == "MARKET"
                                and candidate.get("slug") == dependency.name
                            ):
                                failure = "unavailable", f"Required Skill '{dependency.name}' does not match this expert's market installation identity."
                                dependency_name = dependency.name
                                break
                            if skill_access_filter is not None and not skill_access_filter(dependency):
                                failure = "unavailable", f"Required Skill '{dependency.name}' is not enabled for this conversation."
                                dependency_name = dependency.name
                                break
                            skill_loader.verify_skill_source(dependency)
                    except SkillDependencyError as exc:
                        state = {"SKILL_NOT_FOUND": "missing", "SKILL_DISABLED": "disabled",
                                 "SKILL_BROKEN": "broken"}.get(exc.code, "unavailable")
                        failure = state, str(exc)
                        dependency_error = exc
                        dependency_name = exc.details.get("skill")
            if failure is not None:
                state, reason = failure
                item = {
                    "expertId": profile.id,
                    "expertName": profile.name, "name": name,
                    "source": source, "required": True, "state": state, "reason": reason,
                }
                if isinstance(binding.get("skillId"), str):
                    item["skillId"] = binding["skillId"]
                if isinstance(dependency_name, str) and dependency_name:
                    item["dependencyName"] = dependency_name
                if dependency_error is not None:
                    item["dependencyCode"] = dependency_error.code
                failures.append(item)
    if failures:
        raise ExpertDependencyError(failures)
