"""Standalone config resources remain separate from writable user profiles."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent import cli, session_assembly
from box_agent.config import Config, PROMPT_TEMPLATE_FILES, prompt_override_drift


@pytest.fixture
def frozen_config_layout(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    package = runtime / "bin/_internal/box_agent"
    (package / "config").mkdir(parents=True)
    resources = runtime / "config"
    resources.mkdir()
    (runtime / "manifest.json").write_text('{"name":"box-agent","entry":"bin/box-agent-acp"}', encoding="utf-8")
    entry = runtime / "bin/box-agent-acp"
    profile = tmp_path / "profile"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(entry))
    monkeypatch.setattr(Config, "get_package_dir", staticmethod(lambda: package))
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(runtime=runtime, package=package, resources=resources, profile=profile)


@pytest.mark.parametrize("filename", PROMPT_TEMPLATE_FILES)
def test_frozen_prompt_resources_follow_the_runtime_and_keep_profile_precedence(frozen_config_layout, filename):
    layout = frozen_config_layout
    (layout.package / "config" / filename).write_text("stale bundled template", encoding="utf-8")
    target = layout.resources / filename
    target.write_text("external bundled template", encoding="utf-8")

    assert Config.get_bundled_config_dir() == layout.resources
    assert Config.find_config_file(filename) == target

    override = layout.profile / "config" / filename
    override.parent.mkdir(parents=True)
    override.write_text("user template", encoding="utf-8")
    assert Config.find_config_file(filename) == override
    assert target.read_text(encoding="utf-8") == "external bundled template"


@pytest.mark.parametrize("filename", ["config.yaml", "auth.json", "mcp.json", "user-mcp.json"])
def test_frozen_resources_do_not_supply_an_explicit_profiles_private_config(frozen_config_layout, filename):
    layout = frozen_config_layout
    (layout.resources / filename).write_text("must not be loaded", encoding="utf-8")
    assert Config.find_config_file(filename) is None
    assert not layout.profile.exists()


def test_frozen_config_bootstrap_copies_external_example_without_mutating_runtime(frozen_config_layout, monkeypatch, tmp_path):
    layout = frozen_config_layout
    monkeypatch.delenv("BOX_AGENT_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user-home"))
    example = layout.resources / "config-example.yaml"
    example.write_text("model: external-template\n", encoding="utf-8")
    (layout.package / "config/config-example.yaml").write_text("model: stale-template\n", encoding="utf-8")

    created = Config.get_default_config_path()

    assert created == tmp_path / "user-home/.box-agent/config/config.yaml"
    assert created.read_text(encoding="utf-8") == "model: external-template\n"
    created.write_text("model: user-choice\n", encoding="utf-8")
    assert Config._ensure_user_config() == created
    assert created.read_text(encoding="utf-8") == "model: user-choice\n"
    assert example.read_text(encoding="utf-8") == "model: external-template\n"


def test_missing_runtime_manifest_keeps_external_prompt_resources(frozen_config_layout):
    layout = frozen_config_layout
    (layout.runtime / "manifest.json").unlink()
    (layout.resources / "system_prompt.md").write_text("external prompt template", encoding="utf-8")
    bundled = layout.package / "config/system_prompt.md"
    bundled.write_text("Box-Agent package template", encoding="utf-8")

    assert Config.get_bundled_config_dir() == layout.resources
    assert Config.find_config_file("system_prompt.md") == layout.resources / "system_prompt.md"


def test_missing_external_prompt_does_not_silently_select_stale_internal_copy(frozen_config_layout):
    layout = frozen_config_layout
    (layout.package / "config/system_prompt.md").write_text("stale internal prompt", encoding="utf-8")
    assert Config.get_bundled_config_dir() == layout.resources
    assert Config.find_config_file("system_prompt.md") is None


def test_prompt_override_drift_uses_the_external_bundled_template(frozen_config_layout):
    layout = frozen_config_layout
    (layout.resources / "system_prompt.md").write_text("{CURRENT_SLOT}", encoding="utf-8")
    (layout.package / "config/system_prompt.md").write_text("{STALE_SLOT}", encoding="utf-8")
    override = layout.profile / "config/system_prompt.md"
    override.parent.mkdir(parents=True)
    override.write_text("{STALE_SLOT}", encoding="utf-8")

    drift = prompt_override_drift(override, "system_prompt.md")

    assert drift["missing"] == ["{CURRENT_SLOT}"]
    assert drift["unknown"] == ["{STALE_SLOT}"]
    assert prompt_override_drift(layout.resources / "system_prompt.md", "system_prompt.md") is None


def test_child_prompt_projection_reads_external_packaged_template(frozen_config_layout):
    layout = frozen_config_layout
    template = "Preamble\n{SKILLS_METADATA}\n## Working Guidelines\nKeep this guidance.\n<workflow>\nparent workflow\n</workflow>\nChild guidance.\n"
    (layout.resources / "system_prompt.md").write_text(template, encoding="utf-8")
    (layout.package / "config/system_prompt.md").write_text("unrelated stale template", encoding="utf-8")

    projected = session_assembly._child_prompt_template(template)

    assert projected.startswith("## Working Guidelines\n")
    assert "Keep this guidance." in projected and "Child guidance." in projected
    assert "Preamble" not in projected and "parent workflow" not in projected
    assert "{SKILLS_METADATA}" not in projected


def test_cli_mcp_initialization_uses_external_template_and_preserves_user_file(frozen_config_layout):
    layout = frozen_config_layout
    example = {"mcpServers": {"playwright": {"command": "external-browser", "args": [], "disabled": True}}}
    (layout.resources / "mcp-example.json").write_text(json.dumps(example), encoding="utf-8")
    (layout.package / "config/mcp-example.json").write_text('{"mcpServers": {}}', encoding="utf-8")

    target = cli._ensure_user_mcp_config()

    assert target == layout.profile / "config/mcp.json"
    assert json.loads(target.read_text(encoding="utf-8")) == example
    target.write_text('{"mcpServers": {"custom": {"command": "user-tool"}}}', encoding="utf-8")
    assert cli._ensure_user_mcp_config() == target
    assert "custom" in json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
    cli._enable_playwright_in_mcp(target)
    servers = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["custom"]["command"] == "user-tool"
    assert servers["playwright"]["command"] == "external-browser"
    assert servers["playwright"]["disabled"] is False
    assert servers["playwright"]["args"] == ["--timeout-navigation", "30000"]
    assert json.loads((layout.resources / "mcp-example.json").read_text(encoding="utf-8")) == example
