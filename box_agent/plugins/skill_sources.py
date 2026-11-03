"""Cold-start resource plugin projection into the existing Skill catalog.

No plugin Python is imported here. A plan owns immutable Skill bodies and a
configuration snapshot; normal user/connector discovery remains independent.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..skill_dependencies import resolve_required_skills
from ..tools.skill_loader import RESERVED_BUILTIN_SKILL_NAMES, Skill, SkillLoader

log = logging.getLogger(__name__)
ACTIVATION_ENV = "BOX_AGENT_PLUGIN_ACTIVATION_ID"
_REFERENCE = re.compile(r"\{\{skill:([a-z0-9_]+)\}\}")


@dataclass(frozen=True)
class PluginSkillPlan:
    generation: str
    skills: tuple[Skill, ...] = ()
    replaced_names: frozenset[str] = frozenset()
    errors: tuple[dict[str, str], ...] = ()
    runtime_profiles: tuple[str, ...] = ()

    def loader_arguments(self) -> dict[str, Any]:
        return {"plugin_skills": self.skills,
                "replaced_skill_names": self.replaced_names}


def _skill_name(instance_id: str, contribution_id: str) -> str:
    digest = sha256(f"{instance_id}/{contribution_id}".encode()).hexdigest()[:12]
    return f"p__{instance_id[:40]}__{contribution_id[:40]}__{digest}"


def build_skill_plan(snapshot: dict[str, Any]) -> PluginSkillPlan:
    """Resolve explicitly declared Skill files; fail the candidate as a unit."""
    reader = SkillLoader(sources=[], skill_settings_path=Path(os.devnull))
    skills: list[Skill] = []
    occupied: set[str] = set()
    replaced: set[str] = set()
    runtime_profiles: set[str] = set()
    generation = str(snapshot.get("generation") or "")
    for instance_id, item in snapshot.get("instances", {}).items():
        manifest = item["manifest"]
        root = Path(item["package_root"]).resolve()
        contributions = manifest.get("contributes", {}).get("skills", [])
        from .manifest import validate_enabled_by
        validate_enabled_by(contributions)
        declarations = {entry["id"]: entry for entry in contributions}
        aliases = item.get("aliases", {})
        if set(aliases) & RESERVED_BUILTIN_SKILL_NAMES:
            raise ValueError("Plugin aliases cannot replace reserved system Skills")
        if replaced & set(aliases):
            raise ValueError("Duplicate plugin Skill alias")
        replaced.update(aliases)
        if set(aliases.values()) - declarations.keys():
            raise ValueError(f"Unknown Skill alias target in instance: {instance_id}")
        # Retain disabled aliases as tombstones. A disable must never reveal a
        # leftover builtin with the same public name.
        if not item.get("enabled", False):
            continue
        if profile := manifest.get("requires", {}).get("runtime_profile"):
            runtime_profiles.add(profile)
        names = {key: _skill_name(instance_id, key) for key in declarations}
        for alias, contribution_id in aliases.items():
            if contribution_id not in declarations:
                raise ValueError(f"Unknown Skill alias target: {instance_id}/{contribution_id}")
            if names[contribution_id] != _skill_name(instance_id, contribution_id):
                raise ValueError("Only one public alias per Skill is supported")
            names[contribution_id] = alias
        original_names: dict[str, str] = {}
        parsed: dict[str, Skill] = {}
        for key, declaration in declarations.items():
            path = (root / declaration["path"] / "SKILL.md").resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f"Missing or escaping Skill file: {instance_id}/{key}")
            skill = reader.load_skill(path, source="plugin")
            if skill is None or skill.broken:
                raise ValueError(f"Invalid Skill file: {instance_id}/{key}")
            if skill.name in original_names:
                raise ValueError(f"Duplicate Skill frontmatter name: {skill.name}")
            original_names[skill.name] = names[key]
            parsed[key] = skill
        exposed = item.get("expose")
        if isinstance(exposed, dict):
            exposed = exposed.get("skills", [])
        exposed = set(declarations if exposed is None else exposed)
        if exposed - declarations.keys():
            raise ValueError("Unknown exposed plugin Skill")
        for key, declaration in declarations.items():
            for target in declaration.get("enabled_by", []):
                if not parsed[target].user_visible:
                    raise ValueError(f"Skill enabled_by must reference a public entry: {instance_id}/{target}")
                if key in exposed and target not in exposed:
                    raise ValueError(f"Skill enabled_by entry is not exposed: {instance_id}/{target}")
        for key, skill in parsed.items():
            if key not in exposed:
                continue
            declaration = declarations[key]
            name = names[key]
            if name in occupied:
                raise ValueError(f"Duplicate Skill identity: {name}")
            occupied.add(name)
            skill.name, skill.owner_id = name, instance_id
            skill.plugin_package_root = root
            skill.plugin_enabled_by = tuple(declaration.get("enabled_by", []))
            skill.required_skills = [original_names.get(n, n) for n in skill.required_skills or []]
            skill.related_skills = [original_names.get(n, n) for n in skill.related_skills or []]

            def reference(match: re.Match[str]) -> str:
                target = match.group(1)
                if target not in declarations or target not in exposed:
                    raise ValueError(f"Unknown or hidden Skill reference: {instance_id}/{target}")
                return names[target]

            skill.content = _REFERENCE.sub(reference, skill.content)
            fields = declaration.get("config_keys", [])
            values = {field: copy.deepcopy(item.get("config", {})[field])
                      for field in fields if field in item.get("config", {})}
            identity = {"plugin_id": manifest["id"], "instance_id": instance_id,
                        "contribution_id": key, "package_version": manifest["version"],
                        "package_digest": item["package_digest"],
                        "config_revision": item["config_revision"], "generation": generation}
            skill.metadata = {**(skill.metadata or {}), "plugin": identity,
                              "requires_host_tools": declaration.get("requires_host_tools", [])}
            # JSON is data, never a shell/Python/Jinja template. Including the
            # identity in the body also makes the existing prompt hash reflect
            # a configuration revision for both explicit and tool reads.
            projection = json.dumps({**identity, "values": values,
                                     "skill_names": {k: names[k] for k in sorted(exposed)}},
                                    ensure_ascii=False, sort_keys=True)
            skill.content = ("## Plugin instance settings\n\n"
                             "Default preferences only. Current user instructions and already confirmed task choices take precedence. "
                             "These settings do not grant tools or permission.\n\n"
                             + projection + "\n\n" + skill.content)
            skills.append(skill)
    loader = SkillLoader(sources=[], plugin_skills=skills, replaced_skill_names=replaced,
                         skill_settings_path=Path(os.devnull))
    loader.discover_skills()
    for skill in skills:
        resolve_required_skills(loader, [skill.name])
        loader.verify_skill_source(skill)
        if not loader.get_skill(skill.name).to_prompt().strip():
            raise ValueError("Empty plugin Skill projection")
    return PluginSkillPlan(generation, tuple(skills), frozenset(replaced),
                           runtime_profiles=tuple(sorted(runtime_profiles)))


def plugin_settings_service(config: Any = None):
    from .settings_service import PluginSettingsService
    from .locations import plugin_location

    root, target_id, profile_id = plugin_location(config)
    return PluginSettingsService(root, target_id=target_id, profile_id=profile_id)


def load_plugin_skill_plan(config: Any) -> PluginSkillPlan:
    """Freeze this Config's plugin generation before assembling any sessions."""
    cached = getattr(config, "_plugin_skill_plan", None)
    if cached is not None:
        return cached
    settings = getattr(config, "plugin_platform", None)
    from .locations import plugin_location
    plugin_location(config)  # Register immutable roots, including in safe mode.
    if os.environ.get("BOX_AGENT_DISABLE_PLUGINS") == "1" or not getattr(settings, "enabled", True):
        plan = PluginSkillPlan("safe-mode")
        config._plugin_skill_plan = plan
        return plan
    service = plugin_settings_service(config)
    activation_id = os.environ.get(ACTIVATION_ENV) or None
    snapshot: dict[str, Any] = {}
    try:
        snapshot = service.get_snapshot(activation_id=activation_id)
        for item in snapshot.get("instances", {}).values():
            if item.get("enabled"):
                service.packages.load(item["package_digest"], verify=True)
        plan = build_skill_plan(snapshot)
    except Exception as error:
        if activation_id:
            _fail_candidate(service, activation_id, error)
            raise
        log.exception("plugin/load_failed: external Skills unavailable; core remains available")
        instances = snapshot.get("instances")
        names = frozenset(alias for item in (instances.values() if isinstance(instances, dict) else ())
                          if isinstance(item, dict) and isinstance(item.get("aliases"), dict)
                          for alias in item["aliases"] if isinstance(alias, str))
        plan = PluginSkillPlan(str(snapshot.get("generation") or ""), replaced_names=names,
                               errors=({"code": "PLUGIN_LOAD_FAILED", "message": str(error)},))
    config._plugin_skill_plan = plan
    return plan


def _fail_candidate(service: Any, activation_id: str, error: Exception) -> None:
    try:
        service.dispatch("profile.fail_apply", {"activation_id": activation_id,
                         "error": {"code": "PLUGIN_STARTUP_FAILED", "message": str(error)}})
    except Exception:
        log.exception("plugin/activation_failure_report_failed")


def confirm_plugin_startup(config: Any, loader: SkillLoader | None, *,
                           runtime_env: dict[str, str] | None = None,
                           available_tools: Any = None) -> PluginSkillPlan:
    """Called only after the real host catalog has passed its startup smoke."""
    plan = load_plugin_skill_plan(config)
    activation_id = os.environ.get(ACTIVATION_ENV)
    if plan.generation == "safe-mode":
        if activation_id:
            raise ValueError("Cannot activate plugins while safe mode is enabled")
        return plan
    service = plugin_settings_service(config)
    try:
        from .runtime_profiles import verify_runtime_profiles
        verify_runtime_profiles(config, plan.runtime_profiles, runtime_env=runtime_env)
        if available_tools is not None:
            from ..skill_runtime import SkillRuntime
            runtime = SkillRuntime(loader)
            runtime.available_tools_provider = lambda: available_tools
            runtime.check_host_tools(plan.skills)
        class InstalledCatalog:
            def get_skill(self, name, *, include_disabled=False):
                return loader.get_skill(name, include_disabled=True)

        for expected in plan.skills:
            actual = loader.get_skill(expected.name, include_disabled=True) if loader is not None else None
            if actual is None or actual.source != "plugin" or actual.to_prompt() != expected.to_prompt():
                raise ValueError(f"Plugin Skill startup smoke failed: {expected.name}")
            loader.verify_skill_source(actual)
            # User usage preferences must not make an installed generation fail
            # activation. Validate structural dependencies against the complete
            # frozen catalog; actual calls still use the enabled catalog.
            resolve_required_skills(InstalledCatalog(), [actual.name])
        if activation_id:
            service.dispatch("profile.confirm_apply", {"activation_id": activation_id})
    except Exception as error:
        if activation_id:
            _fail_candidate(service, activation_id, error)
            raise
        log.exception("plugin/startup_smoke_failed: continuing without external Skills")
        plan = PluginSkillPlan(plan.generation, replaced_names=plan.replaced_names,
                               errors=({"code": "PLUGIN_STARTUP_FAILED", "message": str(error)},))
        config._plugin_skill_plan = plan
        if loader is not None:
            loader.disable_plugin_skills()
    try:
        service.record_runtime_status(plan.generation, not bool(plan.errors), list(plan.errors))
    except Exception:
        # A diagnostic write failure must not take ordinary conversations down.
        log.exception("plugin/runtime_status_report_failed")
    return plan


def preflight_plugin_candidate(config: Any, activation_id: str) -> PluginSkillPlan:
    """Management verifies a candidate before the host stops accepting work."""
    from .runtime_profiles import verify_runtime_profiles

    service = plugin_settings_service(config)
    try:
        snapshot = service.get_snapshot(activation_id)
        for item in snapshot.get("instances", {}).values():
            if item.get("enabled"):
                service.packages.load(item["package_digest"], verify=True)
        plan = build_skill_plan(snapshot)
        verify_runtime_profiles(config, plan.runtime_profiles)
        return plan
    except Exception as error:
        _fail_candidate(service, activation_id, error)
        raise


def confirm_acp_plugin_startup(config: Any, loader: SkillLoader | None,
                               base_tools: list[Any], llm: Any) -> PluginSkillPlan:
    """Construct the actual default native catalog, without invoking any tool."""
    import tempfile
    from ..tools.setup import add_workspace_tools

    if not load_plugin_skill_plan(config).skills:
        return confirm_plugin_startup(config, loader, available_tools=base_tools)
    with tempfile.TemporaryDirectory(prefix="box-plugin-preflight-") as directory:
        tools = list(base_tools)
        add_workspace_tools(tools, config, Path(directory), sandbox_mode=True, allow_full_access=False,
                            non_interactive=True, output=lambda *_: None,
                            llm=llm, skill_loader=loader)
        bash = next((tool for tool in tools if tool.name == "bash"), None)
        return confirm_plugin_startup(config, loader, available_tools=tools,
                                      runtime_env=getattr(bash, "runtime_env", None))
