"""Author-facing diagnostics use real loaders while isolating all installed state."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from box_agent.plugins.package_store import package_digest
from box_agent.plugins.settings_service import PluginSettingsService


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/check_resource_plugin.py"
SPEC = importlib.util.spec_from_file_location("check_resource_plugin", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "package"
    shutil.copytree(REPO / "examples/resource-skill-plugin", root)
    (root / "config.schema.json").write_text(json.dumps({
        "type": "object", "additionalProperties": False,
        "properties": {"tone": {"type": "string", "enum": ["brief", "detailed"], "default": "brief"}},
        "required": ["tone"],
    }))
    manifest = yaml.safe_load((root / "plugin.yaml").read_text())
    manifest["contributes"]["skills"][0]["config_keys"] = ["tone"]
    (root / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    with (root / "skills/brief/SKILL.md").open("a") as stream:
        stream.write("\nReference the same instance: {{skill:brief}}.\n")
    return root


def test_check_saves_in_temporary_store_and_never_confirms_or_changes_source(package, tmp_path, monkeypatch):
    real_profile = tmp_path / "existing-profile"
    real_profile.mkdir()
    sentinel = real_profile / "state.json"
    sentinel.write_text("existing data must not be read or changed")
    monkeypatch.setenv("BOX_AGENT_HOME", str(real_profile))
    monkeypatch.setenv("BOX_AGENT_PLUGIN_STATE_DIR", str(real_profile))
    monkeypatch.setenv("BOX_AGENT_PLUGIN_ACTIVATION_ID", "do-not-use")
    monkeypatch.setenv("BOX_AGENT_DISABLE_PLUGINS", "1")
    before = package_digest(package)
    roots = []
    original_init = PluginSettingsService.__init__

    def observe_store(self, root, *args, **kwargs):
        roots.append(Path(root))
        original_init(self, root, *args, **kwargs)

    def forbid_confirmation(*args, **kwargs):
        pytest.fail("Authoring checks must not confirm a runtime activation")

    monkeypatch.setattr(PluginSettingsService, "__init__", observe_store)
    monkeypatch.setattr(PluginSettingsService, "confirm_apply", forbid_confirmation)
    result = checker.check_package(package, config={"tone": "detailed"}, aliases={"team-brief": "brief"})
    assert result["success"], result
    assert result["scope"] == "authoring_static_preflight"
    assert result["configuration"] == {"keys": ["tone"], "saved_revision": 2}
    assert result["temporary_store_removed"]
    assert not result["runtime_verified"] and not result["activation_confirmed"]
    assert len(result["skills"]) == 2
    assert {skill["instance_id"] for skill in result["skills"]} == {"authoring_check", "authoring_peer"}
    assert len({skill["model_name"] for skill in result["skills"]}) == 2
    assert result["skills"][0]["model_name"] == "team-brief"
    assert all(skill["config_keys"] == ["tone"] for skill in result["skills"])
    assert all(skill["projection_verified"] and skill["reference_binding_verified"]
               and skill["reference_count"] == 1 for skill in result["skills"])
    assert roots and all(not root.parent.exists() for root in roots)
    assert sentinel.read_text() == "existing data must not be read or changed"
    assert list(real_profile.iterdir()) == [sentinel]
    assert package_digest(package) == before
    assert "detailed" not in json.dumps(result)


@pytest.mark.parametrize("mutation,stage,code", [
    ("invalid_config", "install_package", "invalid_config"),
    ("unsupported_tool", "install_package", "unsupported_feature"),
    ("missing_skill_reference", "skill_plan_and_same_instance_references", "authoring_check_failed"),
    ("invalid_enabled_by", "install_package", "invalid_manifest"),
])
def test_failures_report_the_authoring_stage(package, mutation, stage, code):
    manifest_path = package / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    config = None
    if mutation == "invalid_config":
        config = {"tone": "unknown"}
    elif mutation == "unsupported_tool":
        manifest["contributes"]["tools"] = []
    elif mutation == "invalid_enabled_by":
        manifest["contributes"]["skills"][0]["enabled_by"] = ["missing_entry"]
    else:
        with (package / "skills/brief/SKILL.md").open("a") as stream:
            stream.write("\n{{skill:missing_entry}}\n")
    manifest_path.write_text(yaml.safe_dump(manifest))
    result = checker.check_package(package, config=config)
    assert not result["success"]
    assert result["error"]["stage"] == stage
    assert result["error"]["code"] == code
    assert not result["activation_confirmed"]
    assert result["temporary_store_removed"]
    if mutation == "invalid_config":
        assert "unknown" not in json.dumps(result)


def test_declared_runtime_and_tools_are_reported_without_execution(package, monkeypatch):
    path = package / "plugin.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["requires"]["runtime_profile"] = "business-runtime-v1"
    manifest["contributes"]["skills"][0]["requires_host_tools"] = ["write_file"]
    path.write_text(yaml.safe_dump(manifest))

    def forbid_process(*args, **kwargs):
        pytest.fail("Static authoring checks must not run a dependency or business subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    result = checker.check_package(package)
    assert result["success"], result
    assert result["required_runtime_profiles"] == ["business-runtime-v1"]
    assert result["required_host_tools"] == ["write_file"]
    assert result["runtime_verified"] is False
    assert result["ui"]["javascript_executed"] is False


def test_cli_returns_json_and_nonzero_for_invalid_configuration(package, tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"tone":"bad"}')
    result = subprocess.run([sys.executable, str(SCRIPT), str(package), "--config", str(invalid), "--json"],
                            cwd=tmp_path, text=True, capture_output=True, timeout=30,
                            env={**os.environ, "PYTHONPATH": str(REPO)})
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["error"]["code"] == "invalid_config"
    result = subprocess.run([sys.executable, str(SCRIPT), str(package), "--json"], cwd=tmp_path,
                            text=True, capture_output=True, timeout=30,
                            env={**os.environ, "PYTHONPATH": str(REPO)})
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["success"] is True


def test_host_version_override_only_changes_manifest_comparison(package):
    from box_agent import __version__

    result = checker.check_package(package, host_version="2.0.0")
    assert not result["success"]
    assert result["host"]["reported_version"] == __version__
    assert result["host"]["manifest_check_version"] == "2.0.0"
    assert result["error"]["code"] == "incompatible_host"


@pytest.mark.parametrize("content", ["null", "[]"])
def test_cli_rejects_non_object_config_instead_of_silently_using_defaults(package, tmp_path, content):
    config = tmp_path / "config.json"
    config.write_text(content)
    result = subprocess.run([sys.executable, str(SCRIPT), str(package), "--config", str(config), "--json"],
                            cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "invalid_config_file"


@pytest.mark.parametrize("mutation", ["values", "skill_names", "body_reference"])
def test_projection_checks_detect_wrong_defaults_and_cross_instance_binding(package, monkeypatch, mutation):
    from box_agent.plugins import skill_sources

    original_build = skill_sources.build_skill_plan

    def broken_projection(snapshot):
        plan = original_build(snapshot)
        first, peer = plan.skills
        sections = first.content.split("\n\n", 3)
        projection = json.loads(sections[2])
        if mutation == "values":
            projection["values"]["tone"] = "injected-wrong-value"
        elif mutation == "skill_names":
            projection["skill_names"]["brief"] = peer.name
        else:
            sections[3] = sections[3].replace(first.name, peer.name)
        sections[2] = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        first.content = "\n\n".join(sections)
        return plan

    monkeypatch.setattr(skill_sources, "build_skill_plan", broken_projection)
    result = checker.check_package(package)
    assert not result["success"]
    assert result["error"]["stage"] == "skill_projection_and_reference_binding"
    assert result["temporary_store_removed"]
    assert "injected-wrong-value" not in json.dumps(result)
