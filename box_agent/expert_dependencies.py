"""Preflight the concrete Skill and tool selections supplied by expert clients.

The desktop editor saves its selected Skills as default/required Skills. Older
drafts can also repeat them in optionalSkills; that field is not a request to
silently drop a selected dependency. This module does not install resources or
change the host's profile.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
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


def _names(value: Any) -> list[str]:
    values = value.splitlines() if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(item.strip() for item in values if isinstance(item, str) and item.strip()))


def _profiles(meta: Mapping[str, Any]):
    expert = meta.get("expert")
    if isinstance(expert, dict) and isinstance(expert.get("name"), str) and expert["name"].strip():
        yield expert, expert.get("packageSnapshot"), True
    team = meta.get("expert_team") or meta.get("expertTeam")
    if not isinstance(team, dict) or not isinstance(team.get("name"), str) or not team["name"].strip():
        return
    members = team.get("members")
    members = members if isinstance(members, list) else []
    for member in [team.get("leader"), *members]:
        if isinstance(member, dict) and isinstance(member.get("name"), str) and member["name"].strip():
            yield member, member.get("packageSnapshot", team.get("packageSnapshot")), False


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
    for profile, snapshot, has_required in _profiles(raw_meta):
        names = _names(profile.get("defaultSkills") or profile.get("default_skills"))
        if has_required:
            names = list(dict.fromkeys([*_names(profile.get("requiredSkills") or profile.get("required_skills")), *names]))
        declared = profile.get("skillBindings")
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
                elif selected.broken:
                    failure = "broken", "The selected Skill cannot be loaded."
                elif source == "BUILTIN" and selected.source != "builtin":
                    failure = "conflict", "A same-name Skill from another source cannot satisfy the builtin binding."
                elif source == "BUNDLED":
                    failure = _bundled_binding(name, binding, snapshot, expert_context, skill_loader, selected)
                elif source == "MARKET" and not is_installed_market_skill(selected, binding.get("skillId")):
                    failure = "unavailable", "The selected Skill has no matching market installation identity."
                if failure is None and selected is not None:
                    try:
                        for dependency in resolve_required_skills(skill_loader, [name]):
                            if skill_access_filter is not None and not skill_access_filter(dependency):
                                failure = "unavailable", f"Required Skill '{dependency.name}' is not enabled for this conversation."
                                break
                            skill_loader.verify_skill_source(dependency)
                    except SkillDependencyError as exc:
                        failure = "unavailable", str(exc)
            if failure is not None:
                state, reason = failure
                item = {
                    "expertId": profile.get("id") or profile["name"],
                    "expertName": profile["name"], "name": name,
                    "source": source, "required": True, "state": state, "reason": reason,
                }
                if isinstance(binding.get("skillId"), str):
                    item["skillId"] = binding["skillId"]
                failures.append(item)
    if failures:
        raise ExpertDependencyError(failures)
