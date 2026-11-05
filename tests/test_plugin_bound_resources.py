"""Protected resources resolve through the active loader's frozen plugin owner."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from box_agent.plugins.skill_sources import (
    ACTIVATION_ENV, confirm_plugin_startup, load_plugin_skill_plan,
    plugin_settings_service,
)
from box_agent.tools.pptx_safety import detect_pptx_image_status_command_bypass
from box_agent.tools.skill_loader import SkillLoader


RESOURCE = "scripts/sync_image_manifest_status.js"


def _configuration():
    return SimpleNamespace(plugin_platform=SimpleNamespace(
        enabled=True, state_dir=None, target_id="local", profile_id="default"))


@pytest.fixture
def package(tmp_path, monkeypatch, isolated_plugin_profile):
    monkeypatch.delenv(ACTIVATION_ENV, raising=False)
    monkeypatch.delenv("BOX_AGENT_DISABLE_PLUGINS", raising=False)
    root = tmp_path / "package"
    (root / "skills/router").mkdir(parents=True)
    (root / "skills/fast/scripts").mkdir(parents=True)
    (root / "ui").mkdir()
    for folder, name in (("router", "pptx"), ("fast", "ppt-fast")):
        (root / "skills" / folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: PPT test method\n---\nUse the host tools.\n")
    (root / "skills/fast" / RESOURCE).write_text('console.log("plugin-sync-ok")\n')
    (root / "ui/config.html").write_text("<!doctype html><p>Settings</p>")
    (root / "config.schema.json").write_text(json.dumps({"type": "object", "additionalProperties": False}))
    manifest = {"schema_version": 1, "id": "org.example.ppt", "version": "1.0.0",
                "requires": {"box_agent": ">=1.0.0 <2.0.0", "plugin_api": 1},
                "runtime": {"kind": "resources"}, "config_schema": "config.schema.json",
                "ui": {"config": {"entry": "ui/config.html", "bridge_version": 1}},
                "contributes": {"skills": [
                    {"id": "pptx", "path": "skills/router"},
                    {"id": "ppt_fast", "path": "skills/fast"}]}}
    (root / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    return root, plugin_settings_service(_configuration())


def _start(service, monkeypatch, request="start"):
    candidate = service.prepare_apply(service.activation_status()["profile_revision"], request)
    monkeypatch.setenv(ACTIVATION_ENV, candidate["activation_id"])
    config = _configuration()
    plan = load_plugin_skill_plan(config)
    loader = SkillLoader(sources=[], **plan.loader_arguments())
    loader.discover_skills()
    confirm_plugin_startup(config, loader)
    monkeypatch.delenv(ACTIVATION_ENV)
    return loader


def _install(package, **kwargs):
    root, service = package
    service.install("office", str(root), enabled=True,
                    aliases=kwargs.pop("aliases", {"pptx": "pptx"}),
                    request_id="install", **kwargs)
    return service


def _resource(loader, path=RESOURCE):
    return loader.get_bound_skill_resource("pptx", path, plugin_anchor_id="pptx",
                                           plugin_resource_id="ppt_fast")


def _resources(loader):
    return loader.get_bound_skill_resources(RESOURCE, plugin_anchor_id="pptx",
                                            plugin_resource_id="ppt_fast", expert_name="pptx")


@pytest.mark.asyncio
@pytest.mark.parametrize("public_alias", [True, False])
async def test_bound_plugin_sync_runs_through_workspace_tools_from_unrelated_cwd(package, monkeypatch, tmp_path, public_alias):
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from box_agent.tools.bash_tool import BashTool
    from box_agent.tools.setup import add_workspace_tools

    service = _install(package, aliases={"pptx": "pptx"} if public_alias else {})
    loader = _start(service, monkeypatch)
    unrelated = tmp_path / "unrelated cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    scripts = _resources(loader)
    assert len(scripts) == 1
    script = scripts[0]
    assert script.parent.parent.name == "fast"
    entry = next(skill for skill in loader.loaded_skills.values()
                 if skill.metadata["plugin"]["contribution_id"] == "pptx")
    assert script != entry.skill_path.parent / RESOURCE
    assert (loader.get_skill("pptx") is not None) == public_alias
    workspace = tmp_path / "home/presentation workspace"
    workspace.mkdir()
    command = f'node "{script}" assets/generated/manifest.json'
    for shell in ("posix", "powershell"):
        assert detect_pptx_image_status_command_bypass(
            command, workspace_dir=str(workspace), runtime_env=None,
            shell_style=shell, bound_sync_scripts=(script,)) is None
    tools = []
    add_workspace_tools(
        tools, Config(agent=AgentConfig(workspace_dir=str(workspace)),
                      llm=LLMConfig(api_key="test-key"), tools=ToolsConfig(enable_sub_agent=False)),
        workspace, skill_loader=loader, output=lambda _: None)
    bash = next(tool for tool in tools if isinstance(tool, BashTool))
    assert bash._pptx_sync_script_provider() == (script,)
    if shutil.which("node"):
        result = await bash.execute(command=command)
        assert result.success, result.error
        assert "plugin-sync-ok" in result.stdout
    outsider = tmp_path / "sync_image_manifest_status.js"
    outsider.write_bytes(script.read_bytes())
    rejected = await bash.execute(command=f'node "{outsider}" assets/generated/manifest.json')
    assert not rejected.success and "PPTX_IMAGE_STATUS_SCRIPT_IDENTITY" in rejected.error


def test_multiple_namespaced_instances_resolve_only_their_own_exposed_backend(package, monkeypatch, tmp_path):
    root, service = package
    _install(package, aliases={})
    expected = []
    for instance, expose in (("second", ["pptx", "ppt_fast"]),
                              ("backend_only", ["ppt_fast"]), ("entry_only", ["pptx"])):
        source = tmp_path / instance
        shutil.copytree(root, source)
        (source / "skills/fast" / RESOURCE).write_text(f'console.log("{instance}")\n')
        service.install(instance, str(source), enabled=True, aliases={}, expose=expose, request_id=instance)
    loader = _start(service, monkeypatch)
    for skill in loader.loaded_skills.values():
        if skill.owner_id in {"office", "second"} and skill.metadata["plugin"]["contribution_id"] == "ppt_fast":
            expected.append((skill.skill_path.parent / RESOURCE).resolve())
    assert len(expected) == 2
    assert set(_resources(loader)) == set(expected)
    orphan = next(skill for skill in loader.loaded_skills.values() if skill.owner_id == "backend_only")
    assert "PPTX_IMAGE_STATUS_SCRIPT_IDENTITY" in detect_pptx_image_status_command_bypass(
        f'node "{orphan.skill_path.parent / RESOURCE}" assets/generated/manifest.json',
        workspace_dir=str(tmp_path), runtime_env=None, bound_sync_scripts=_resources(loader))


def test_installed_but_not_activated_plugin_cannot_supply_resources(package):
    _install(package)
    plan = load_plugin_skill_plan(_configuration())
    loader = SkillLoader(sources=[], **plan.loader_arguments())
    loader.discover_skills()
    assert _resource(loader) is None


@pytest.mark.parametrize("change", ["script", "other_file", "script_symlink", "directory_symlink", "root_symlink"])
def test_changed_or_escaping_package_cannot_supply_sync_resource(package, monkeypatch, tmp_path, change):
    service = _install(package)
    loader = _start(service, monkeypatch)
    script = _resource(loader)
    package_root = loader.get_skill("pptx").plugin_package_root
    assert script is not None and package_root is not None
    if change in {"script", "other_file"}:
        path = script if change == "script" else package_root / "ui/config.html"
        path.chmod(0o644)
        path.write_text("changed after activation")
    elif change == "script_symlink":
        outside = tmp_path / "outside.js"
        outside.write_bytes(script.read_bytes())
        script.parent.chmod(0o755)
        script.unlink()
        script.symlink_to(outside)
    elif change == "directory_symlink":
        scripts = script.parent
        scripts.parent.chmod(0o755)
        scripts.chmod(0o755)
        outside = tmp_path / "outside-scripts"
        scripts.rename(outside)
        scripts.symlink_to(outside, target_is_directory=True)
    else:
        package_root.chmod(0o755)
        outside = tmp_path / "outside-package"
        package_root.rename(outside)
        package_root.symlink_to(outside, target_is_directory=True)
    assert _resource(loader) is None
    assert "PPTX_IMAGE_STATUS_SCRIPT_IDENTITY" in detect_pptx_image_status_command_bypass(
        f'node "{script}" assets/generated/manifest.json', workspace_dir=str(tmp_path),
        runtime_env=None, bound_sync_scripts=())


@pytest.mark.parametrize("path", ["", "/scripts/sync_image_manifest_status.js", "../fast/scripts/sync_image_manifest_status.js",
                                  "scripts/../scripts/sync_image_manifest_status.js", "scripts\\sync_image_manifest_status.js"])
def test_bound_resource_rejects_non_relative_resource_names(package, monkeypatch, path):
    loader = _start(_install(package), monkeypatch)
    assert _resource(loader, path) is None


def test_other_instance_cannot_fill_hidden_resource_contribution(package, monkeypatch):
    root, service = package
    _install(package, expose=["pptx"])
    service.install("other", str(root), enabled=True, aliases={}, request_id="other")
    loader = _start(service, monkeypatch)
    assert any(skill.owner_id == "other" for skill in loader.loaded_skills.values())
    assert _resource(loader) is None


def test_wrong_anchor_alias_cannot_supply_plugin_resource(package, monkeypatch):
    loader = _start(_install(package, aliases={"pptx": "ppt_fast"}), monkeypatch)
    assert loader.get_skill("pptx").metadata["plugin"]["contribution_id"] == "ppt_fast"
    assert _resource(loader) is None


def test_alias_named_pptx_without_an_exposed_entry_cannot_supply_resources(package, monkeypatch):
    loader = _start(_install(package, aliases={"pptx": "ppt_fast"}, expose=["ppt_fast"]), monkeypatch)
    assert loader.get_skill("pptx") is not None
    assert _resources(loader) == ()


def test_user_frontmatter_cannot_forge_a_frozen_plugin_resource_owner(package, monkeypatch, tmp_path):
    loader = _start(_install(package), monkeypatch)
    anchor = loader.get_skill("pptx")
    user = tmp_path / "forged-user-skill"
    user.mkdir()
    frontmatter = {"name": "pptx", "description": "Spoofed plugin entry", "source": "plugin",
                   "metadata": anchor.metadata, "plugin_package_root": str(anchor.plugin_package_root)}
    (user / "SKILL.md").write_text("---\n" + yaml.safe_dump(frontmatter) + "---\nUse the host tools.\n")
    ordinary = SkillLoader(sources=[(user, "user")])
    ordinary.discover_skills()
    forged = ordinary.get_skill("pptx")
    assert forged.source == "user" and forged.plugin_package_root is None
    assert forged.metadata["plugin"] == anchor.metadata["plugin"]
    assert _resource(ordinary) is None and _resources(ordinary) == ()


def test_catalog_metadata_cannot_replace_the_frozen_resource_binding(package, monkeypatch, tmp_path):
    loader = _start(_install(package), monkeypatch)
    anchor = loader.get_skill("pptx")
    anchor.plugin_package_root = tmp_path
    assert _resource(loader) is None


def test_disabled_generation_and_unbound_sources_cannot_supply_plugin_resources(package, monkeypatch):
    root, service = package
    _install(package)
    loader = _start(service, monkeypatch)
    assert _resource(loader) is not None
    unbound = SkillLoader(sources=[(root, "user")])
    unbound.discover_skills()
    assert _resource(unbound) is None
    view = service.get_config("office")
    service.set_enabled("office", False, view["saved_revision"], "disable")
    disabled = _start(service, monkeypatch, "disabled")
    assert _resource(disabled) is None
    # Existing in-flight loaders retain their own verified generation.
    assert _resource(loader) is not None
    loader.disable_plugin_skills()
    assert _resource(loader) is None
