"""Legacy PPT names survive a core-only upgrade until verified plugin takeover.

These offline tests install the real PPT resource package and call the actual
Skill reader. The host runtime profile deliberately has no external dependencies;
this proves catalog migration and recovery, not renderer/model readiness.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.config import PluginRuntimeProfile
from box_agent.plugins.skill_sources import (
    ACTIVATION_ENV,
    confirm_plugin_startup,
    load_plugin_skill_plan,
    plugin_settings_service,
)
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool
from tests.pptx_test_support import PPT_ALIASES, PPT_PLUGIN_ROOT


BUILTIN = Path(__file__).resolve().parents[1] / "box_agent/skills"


def _config():
    return SimpleNamespace(plugin_platform=SimpleNamespace(
        enabled=True, state_dir=None, target_id="local", profile_id="default",
        runtime_profiles={"ppt-host-v1": PluginRuntimeProfile()},
    ))


@pytest.fixture
def host(isolated_plugin_profile, monkeypatch):
    monkeypatch.delenv(ACTIVATION_ENV, raising=False)
    monkeypatch.delenv("BOX_AGENT_DISABLE_PLUGINS", raising=False)
    settings = isolated_plugin_profile / "config/skill-settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    service = plugin_settings_service(_config())
    yield SimpleNamespace(service=service, settings=settings)
    # Only this test's immutable installation is made removable for pytest.
    for directory, _, files in os.walk(service.packages.root):
        Path(directory).chmod(0o700)
        for name in files:
            (Path(directory) / name).chmod(0o600)


def _install(host, *, aliases=None):
    return host.service.install(
        "ppt", str(PPT_PLUGIN_ROOT), enabled=True,
        aliases=PPT_ALIASES if aliases is None else aliases, request_id="install",
    )


def _catalog(host):
    config = _config()
    plan = load_plugin_skill_plan(config)
    loader = SkillLoader(
        sources=[(BUILTIN, "builtin")], skill_settings_path=host.settings,
        **plan.loader_arguments(),
    )
    loader.discover_skills()
    return config, plan, loader


def _prepare(host, request_id):
    return host.service.prepare_apply(
        host.service.activation_status()["profile_revision"], request_id,
    )


def _activate(host, monkeypatch, request_id="activate"):
    candidate = _prepare(host, request_id)
    with monkeypatch.context() as activation:
        activation.setenv(ACTIVATION_ENV, candidate["activation_id"])
        config, plan, loader = _catalog(host)
        confirm_plugin_startup(config, loader, runtime_env={})
    assert host.service.activation_status()["activation"]["status"] == "active"
    return plan, loader


async def _assert_readable(loader, source, names=PPT_ALIASES):
    reader = GetSkillTool(loader)
    for name in names:
        skill = loader.get_skill(name)
        assert skill is not None, name
        assert skill.source == source, name
        result = await reader.execute(skill_name=name, limit=5)
        assert result.success, (name, result.error)
        reference = result.raw_output["skill_reference"]
        assert reference["name"] == name
        assert reference["source"] == source
        assert Path(reference["path"]) == skill.skill_path
        assert result.content


async def _assert_unavailable(loader, names=PPT_ALIASES):
    reader = GetSkillTool(loader)
    for name in names:
        assert loader.get_skill(name) is None, name
        result = await reader.execute(skill_name=name, limit=5)
        assert not result.success, name
    assert loader.get_skill("docx").source == "builtin"


@pytest.mark.asyncio
async def test_core_only_upgrade_keeps_all_legacy_ppt_names_callable(host):
    assert not host.service.store.path.exists()
    _, plan, loader = _catalog(host)
    assert plan.generation == "builtin" and not plan.skills
    await _assert_readable(loader, "builtin")
    assert host.service.get_snapshot()["instances"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["installed", "saved", "prepared"])
async def test_unconfirmed_ppt_install_does_not_remove_existing_workflow(host, phase):
    _install(host)
    if phase in {"saved", "prepared"}:
        view = host.service.get_config("ppt")
        host.service.save_config(
            "ppt", {"language": "en-US", "preferred_mode": "fast"},
            view["saved_revision"], "save", view["package_digest"], view["schema_digest"],
        )
    if phase == "prepared":
        _prepare(host, "prepare")
    _, plan, loader = _catalog(host)
    assert plan.generation == "builtin" and not plan.skills
    await _assert_readable(loader, "builtin")


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_plugin", [False, True])
@pytest.mark.parametrize("failure", ["catalog", "runtime-profile"])
async def test_failed_ppt_candidate_preserves_previous_callable_catalog(
    host, monkeypatch, previous_plugin, failure,
):
    from box_agent.plugins.manifest import PluginError

    _install(host)
    if previous_plugin:
        _activate(host, monkeypatch)
        view = host.service.get_config("ppt")
        host.service.save_config(
            "ppt", {"language": "en-US", "preferred_mode": "fast"},
            view["saved_revision"], "change", view["package_digest"], view["schema_digest"],
        )
    previous = host.service.get_snapshot()
    candidate = _prepare(host, "bad-start")
    with monkeypatch.context() as activation:
        activation.setenv(ACTIVATION_ENV, candidate["activation_id"])
        config, _, loader = _catalog(host)
        if failure == "catalog":
            loader = SkillLoader(sources=[])
            expected, message = ValueError, "startup smoke"
        else:
            config.plugin_platform.runtime_profiles = {}
            expected, message = PluginError, "runtime profile is not configured"
        with pytest.raises(expected, match=message):
            confirm_plugin_startup(config, loader, runtime_env={})
    assert host.service.get_snapshot() == previous
    assert host.service.activation_status()["activation"]["status"] == "failed"
    _, _, loader = _catalog(host)
    await _assert_readable(loader, "plugin" if previous_plugin else "builtin")


@pytest.mark.asyncio
async def test_verified_ppt_activation_atomically_replaces_all_legacy_names(host, monkeypatch):
    _install(host)
    plan, _ = _activate(host, monkeypatch)
    _, active, loader = _catalog(host)
    assert active.generation == plan.generation
    await _assert_readable(loader, "plugin")
    for name in PPT_ALIASES:
        skill = loader.get_skill(name)
        assert skill.metadata["plugin"]["instance_id"] == "ppt"
        assert skill.skill_path.is_relative_to(host.service.packages.root)
    assert len([skill for skill in loader.loaded_skills.values() if skill.source == "plugin"]) == 8


@pytest.mark.asyncio
async def test_first_ppt_activation_can_explicitly_roll_back_to_builtin_without_losing_saved_config(
    host, monkeypatch,
):
    _install(host)
    _activate(host, monkeypatch)
    view = host.service.get_config("ppt")
    host.service.save_config(
        "ppt", {"language": "en-US", "preferred_mode": "fast"},
        view["saved_revision"], "save-before-rollback", view["package_digest"], view["schema_digest"],
    )
    saved = host.service.get_config("ppt")
    previous = host.service.get_snapshot()
    candidate = host.service.rollback(saved["profile_revision"], "rollback-to-builtin")

    assert host.service.activation_status()["activation"]["status"] == "prepared"
    assert host.service.get_snapshot() == previous
    _, _, pending = _catalog(host)
    await _assert_readable(pending, "plugin")

    with monkeypatch.context() as activation:
        activation.setenv(ACTIVATION_ENV, candidate["activation_id"])
        config, plan, loader = _catalog(host)
        assert not plan.skills and not plan.replaced_names
        await _assert_readable(loader, "builtin")
        confirm_plugin_startup(config, loader, runtime_env={})

    assert host.service.activation_status()["activation"]["status"] == "active"
    assert host.service.get_snapshot()["instances"] == {}
    _, _, restored = _catalog(host)
    await _assert_readable(restored, "builtin")
    after = host.service.get_config("ppt")
    assert after["config"] == saved["config"]
    assert after["saved_revision"] == saved["saved_revision"]
    assert after["profile_revision"] == saved["profile_revision"]
    assert after["desired_enabled"] and not after["active_enabled"]


@pytest.mark.asyncio
async def test_disabling_migrated_ppt_keeps_legacy_names_unavailable(host, monkeypatch):
    _install(host)
    _activate(host, monkeypatch)
    view = host.service.get_config("ppt")
    host.service.set_enabled("ppt", False, view["saved_revision"], "disable")
    _, _, pending = _catalog(host)
    await _assert_readable(pending, "plugin")
    _activate(host, monkeypatch, "disable-apply")
    _, plan, disabled = _catalog(host)
    assert plan.replaced_names.issuperset(PPT_ALIASES)
    await _assert_unavailable(disabled)


@pytest.mark.asyncio
async def test_user_switch_never_reveals_legacy_ppt_after_migration(host, monkeypatch):
    _install(host)
    _activate(host, monkeypatch)
    before = host.service.store.path.read_bytes()
    host.settings.write_text(json.dumps({"disabledPluginSkills": [{
        "plugin_id": "com.box.ppt", "instance_id": "ppt", "contribution_id": "pptx",
    }]}))
    _, _, loader = _catalog(host)
    await _assert_unavailable(loader)
    assert loader.get_skill("pptx", include_disabled=True).source == "plugin"
    host.settings.write_text(json.dumps({"disabledPluginSkills": []}))
    _, _, enabled = _catalog(host)
    await _assert_readable(enabled, "plugin")
    assert host.service.store.path.read_bytes() == before


@pytest.mark.asyncio
async def test_damaged_active_ppt_package_does_not_revive_old_builtin(host, monkeypatch):
    _install(host)
    _, loader = _activate(host, monkeypatch)
    installed_skill = loader.get_skill("pptx").skill_path
    installed_skill.chmod(0o600)
    installed_skill.write_text(installed_skill.read_text() + "\nUnexpected mutation.\n")
    _, plan, damaged = _catalog(host)
    assert plan.errors and not plan.skills
    await _assert_unavailable(damaged)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["invalid-json", "invalid-aliases"])
async def test_unreadable_migration_state_cannot_silently_restore_legacy_ppt(host, monkeypatch, damage):
    _install(host)
    _activate(host, monkeypatch)
    if damage == "invalid-json":
        corrupt = "broken plugin state"
    else:
        state = json.loads(host.service.store.path.read_text())
        state["active"]["instances"]["ppt"]["aliases"] = None
        corrupt = json.dumps(state)
    host.service.store.path.write_text(corrupt)
    _, plan, loader = _catalog(host)
    assert plan.errors and not plan.skills
    await _assert_unavailable(loader)
    assert host.service.store.path.read_text() == corrupt


@pytest.mark.asyncio
async def test_explicit_safe_mode_restores_builtin_ppt_without_reading_corrupt_state(host, monkeypatch):
    _install(host)
    _activate(host, monkeypatch)
    host.service.store.path.write_text("broken plugin state")
    monkeypatch.setenv("BOX_AGENT_DISABLE_PLUGINS", "1")
    _, plan, loader = _catalog(host)
    assert plan.generation == "safe-mode" and not plan.errors
    await _assert_readable(loader, "builtin")
    assert host.service.store.path.read_text() == "broken plugin state"


@pytest.mark.parametrize("damage", [
    "missing", "invalid-json", "missing-compatibility", "unknown-name", "reserved-name",
])
def test_unknown_migration_ownership_requires_a_valid_host_compatibility_catalog(
    host, tmp_path, monkeypatch, damage,
):
    from box_agent.plugins import skill_sources
    from box_agent.plugins.manifest import PluginError

    # An unreadable state must not turn failure to read the host's migration
    # catalog into an empty denylist that silently enables old capabilities.
    host.service.store.root.mkdir(parents=True, exist_ok=True)
    host.service.store.path.write_text("broken plugin state")
    manifest_path = tmp_path / "host-manifest.json"
    if damage == "invalid-json":
        manifest_path.write_text("broken host manifest")
    elif damage != "missing":
        manifest = json.loads((BUILTIN / "_manifest.json").read_text())
        if damage == "missing-compatibility":
            manifest.pop("plugin_compatibility_names")
        else:
            manifest["plugin_compatibility_names"] = [
                "not-a-builtin" if damage == "unknown-name" else "roadmap",
            ]
        manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(skill_sources, "_BUILTIN_MANIFEST", manifest_path)

    with pytest.raises(PluginError) as caught:
        _catalog(host)

    assert caught.value.code == "builtin_manifest_invalid"
    assert host.service.store.path.read_text() == "broken plugin state"


@pytest.mark.asyncio
@pytest.mark.parametrize("aliases", [{}, {"pptx": "pptx"}])
async def test_partial_aliases_replace_only_explicitly_bound_legacy_names(host, monkeypatch, aliases):
    _install(host, aliases=aliases)
    _activate(host, monkeypatch)
    _, plan, loader = _catalog(host)
    assert plan.replaced_names == frozenset(aliases)
    await _assert_readable(loader, "plugin", aliases)
    await _assert_readable(loader, "builtin", set(PPT_ALIASES) - aliases.keys())
    assert len(plan.skills) == 8


@pytest.mark.asyncio
async def test_core_only_upgrade_preserves_existing_ppt_usage_preference(host):
    host.settings.write_text(json.dumps({"disabledSkillNames": ["pptx"]}))
    _, _, loader = _catalog(host)
    assert loader.get_skill("pptx") is None
    result = await GetSkillTool(loader).execute(skill_name="pptx")
    assert not result.success
    assert loader.get_skill("pptx", include_disabled=True).source == "builtin"
