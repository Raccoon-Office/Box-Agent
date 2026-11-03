"""User Skill usage is independent of installed plugin activation and aliases."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import yaml

from box_agent.plugins.skill_sources import (
    ACTIVATION_ENV, build_skill_plan, confirm_plugin_startup, load_plugin_skill_plan,
    plugin_settings_service,
)
from box_agent.tools.skill_catalog_tool import ListSkillsTool
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


def config():
    return SimpleNamespace(plugin_platform=SimpleNamespace(
        enabled=True, state_dir=None, target_id="local", profile_id="default"))


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(ACTIVATION_ENV, raising=False)
    monkeypatch.delenv("BOX_AGENT_DISABLE_PLUGINS", raising=False)
    root = tmp_path / "package"
    for name in ("entry", "other", "private", "shared"):
        directory = root / "skills" / name
        directory.mkdir(parents=True)
        hidden = "\nmetadata:\n  user_visible: false" if name in {"private", "shared"} else ""
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Example {name}{hidden}\n---\nDo {name}.\n")
    (root / "ui").mkdir()
    (root / "ui/config.html").write_text("<!doctype html><p>Config</p>")
    (root / "config.schema.json").write_text(json.dumps({"type": "object", "additionalProperties": False}))
    manifest = {"schema_version": 1, "id": "org.example.usage", "version": "1.0.0",
                "requires": {"box_agent": ">=1.0.0 <2.0.0", "plugin_api": 1},
                "runtime": {"kind": "resources"}, "config_schema": "config.schema.json",
                "ui": {"config": {"entry": "ui/config.html", "bridge_version": 1}},
                "contributes": {"skills": [
                    {"id": "entry", "path": "skills/entry"},
                    {"id": "other", "path": "skills/other"},
                    {"id": "private", "path": "skills/private", "enabled_by": ["entry"]},
                    {"id": "shared", "path": "skills/shared", "enabled_by": ["entry", "other"]},
                ]}}
    (root / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    service = plugin_settings_service(config())
    service.install("first", str(root), enabled=True, aliases={"demo": "entry"}, request_id="first")
    service.install("second", str(root), enabled=True, request_id="second")
    candidate = service.prepare_apply(service.activation_status()["profile_revision"], "initial")
    monkeypatch.setenv(ACTIVATION_ENV, candidate["activation_id"])
    cfg = config()
    plan = load_plugin_skill_plan(cfg)
    settings = tmp_path / "home/config/skill-settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    loader = SkillLoader(sources=[], skill_settings_path=settings, **plan.loader_arguments())
    loader.discover_skills()
    confirm_plugin_startup(cfg, loader)
    monkeypatch.delenv(ACTIVATION_ENV)
    return SimpleNamespace(root=root, service=service, plan=plan, loader=loader, settings=settings)


def identity(instance="first", contribution="entry", package="org.example.usage"):
    return {"plugin_id": package, "instance_id": instance, "contribution_id": contribution}


def names(fixture, instance="first"):
    return {skill.metadata["plugin"]["contribution_id"]: skill.name
            for skill in fixture.plan.skills if skill.owner_id == instance}


def preferences(fixture, rows, **extra):
    fixture.settings.write_text(json.dumps({"disabledPluginSkills": rows, **extra}))
    assert fixture.loader.maybe_reload()


@pytest.mark.asyncio
async def test_user_disable_keeps_public_metadata_but_blocks_entry_and_private_method(installed):
    f = installed
    n = names(f)
    preferences(f, [identity()])
    rows = {item["name"]: item for item in f.loader.list_skills_metadata(include_disabled=True)}
    assert rows["demo"]["user_visible"] and rows["demo"]["disabled"]
    assert rows["demo"]["user_enabled"] is False
    assert rows[n["private"]]["disabled"] and rows[n["private"]]["user_enabled"]
    assert rows[n["private"]]["unavailable_code"] == "SKILL_ENTRY_DISABLED"
    assert "demo" not in f.loader.list_skills()
    for name in ("demo", n["private"]):
        result = await GetSkillTool(f.loader).execute(skill_name=name)
        assert not result.success and "disabled" in result.error.lower()
    browse = await ListSkillsTool(f.loader).execute()
    assert "demo" not in {row["name"] for row in browse.raw_output["skills"]}
    exact = await ListSkillsTool(f.loader).execute(query=n["private"])
    assert exact.raw_output["skills"][0]["available"] is False


def test_shared_method_uses_or_and_instances_are_isolated(installed):
    f = installed
    first, second = names(f), names(f, "second")
    preferences(f, [identity()])
    assert f.loader.get_skill(first["shared"]) is not None
    preferences(f, [identity(), identity(contribution="other")])
    assert f.loader.get_skill(first["shared"]) is None
    assert all(f.loader.get_skill(name) is not None for name in second.values())
    preferences(f, [identity(contribution="other")])
    assert f.loader.get_skill("demo") is not None
    assert f.loader.get_skill(first["private"]) is not None
    assert f.loader.get_skill(first["shared"]) is not None


def test_usage_identity_survives_alias_change_and_cannot_match_another_package(installed):
    f = installed
    preferences(f, [identity(package="org.example.other")], disabledSkillNames=["demo"])
    assert f.loader.get_skill("demo") is not None
    preferences(f, [identity()])
    snapshot = f.service.get_snapshot()
    snapshot["instances"]["first"]["aliases"] = {"renamed": "entry"}
    plan = build_skill_plan(snapshot)
    renamed = SkillLoader(sources=[], skill_settings_path=f.settings, **plan.loader_arguments())
    renamed.discover_skills()
    assert renamed.get_skill("renamed") is None
    assert renamed.get_skill("renamed", include_disabled=True).user_enabled is False


def test_user_preferences_do_not_change_plugin_state_generation_or_frozen_bodies(installed):
    f = installed
    state_bytes = f.service.store.path.read_bytes()
    before = [skill.to_prompt() for skill in f.plan.skills]
    preferences(f, [identity()])
    preferences(f, [])
    assert f.service.store.path.read_bytes() == state_bytes
    assert f.service.get_snapshot()["generation"] == f.plan.generation
    assert before == [skill.to_prompt() for skill in f.plan.skills]
    assert all(not skill.disabled and skill.user_enabled for skill in f.plan.skills)


def test_restart_and_activation_smoke_accept_user_disabled_skills(installed, monkeypatch):
    f = installed
    preferences(f, [identity(), identity(instance="second")])
    candidate = f.service.prepare_apply(f.service.activation_status()["profile_revision"], "next")
    monkeypatch.setenv(ACTIVATION_ENV, candidate["activation_id"])
    cfg = config()
    plan = load_plugin_skill_plan(cfg)
    loader = SkillLoader(sources=[], skill_settings_path=f.settings, **plan.loader_arguments())
    loader.discover_skills()
    assert loader.get_skill("demo") is None
    confirmed = confirm_plugin_startup(cfg, loader)
    assert not confirmed.errors
    assert f.service.activation_status()["activation"]["status"] == "active"
    assert loader.get_skill("demo") is None


@pytest.mark.parametrize("change", ["missing", "self", "cycle", "hidden", "not_exposed"])
def test_invalid_usage_links_are_rejected_before_activation(installed, change):
    f = installed
    snapshot = f.service.get_snapshot()
    first = snapshot["instances"]["first"]
    declarations = first["manifest"]["contributes"]["skills"]
    by_id = {entry["id"]: entry for entry in declarations}
    if change == "missing":
        by_id["private"]["enabled_by"] = ["missing"]
    elif change == "self":
        by_id["entry"]["enabled_by"] = ["entry"]
    elif change == "cycle":
        by_id["entry"]["enabled_by"] = ["other"]
        by_id["other"]["enabled_by"] = ["entry"]
    elif change == "hidden":
        by_id["private"]["enabled_by"] = ["shared"]
    else:
        first["expose"] = ["private"]
    with pytest.raises(ValueError, match="enabled_by"):
        build_skill_plan(snapshot)


def test_optional_usage_links_preserve_older_package_behavior(installed):
    f = installed
    snapshot = f.service.get_snapshot()
    for item in snapshot["instances"].values():
        for entry in item["manifest"]["contributes"]["skills"]:
            entry.pop("enabled_by", None)
    plan = build_skill_plan(snapshot)
    assert all(not skill.plugin_enabled_by for skill in plan.skills)


def test_malformed_or_extra_identity_fields_do_not_disable_a_skill(installed):
    f = installed
    preferences(f, [{**identity(), "alias": "demo"}, {**identity(), "contribution_id": "../entry"},
                    {"name": "demo"}, None, "demo"])
    assert f.loader.get_skill("demo") is not None


@pytest.mark.asyncio
async def test_acp_listing_waits_for_discovery_and_can_refresh_disabled_entries(installed):
    from box_agent.acp import BoxACPAgent

    f = installed
    adapter = BoxACPAgent.__new__(BoxACPAgent)
    adapter._skill_loader = f.loader
    adapter._skills_loaded = False
    adapter._skill_task = asyncio.create_task(asyncio.to_thread(f.loader.discover_skills))
    f.settings.write_text(json.dumps({"disabledPluginSkills": [identity()]}))
    listing = await adapter.extMethod("list_skills", {"includeDisabled": True})
    rows = {row["name"]: row for row in listing["skills"]}
    assert adapter._skills_loaded
    assert rows["demo"]["disabled"] and rows["demo"]["user_enabled"] is False
    enabled_listing = await adapter.extMethod("list_skills", {})
    assert "demo" not in {row["name"] for row in enabled_listing["skills"]}
    f.settings.write_text(json.dumps({"disabledPluginSkills": []}))
    listing = await adapter.extMethod("list_skills", {"includeDisabled": True})
    row = next(row for row in listing["skills"] if row["name"] == "demo")
    assert not row["disabled"] and row["user_enabled"]
