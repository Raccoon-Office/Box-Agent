"""Standalone resources move beside bin without relocating Python modules."""

import sys
from pathlib import Path

import pytest

from box_agent.resource_paths import bundled_resource_dir


def frozen_runtime(tmp_path, monkeypatch):
    root = tmp_path / "release with spaces"
    entry = root / "bin" / "box-agent-acp"
    entry.parent.mkdir(parents=True)
    entry.write_text("fixture")
    (root / "manifest.json").write_text('{"name":"box-agent","entry":"bin/box-agent-acp"}')
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(entry))
    return root


@pytest.mark.parametrize("name", ["skills", "config"])
def test_source_and_wheel_keep_package_resources(tmp_path, monkeypatch, name):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    package = tmp_path / "site-packages" / "box_agent"
    assert bundled_resource_dir(name, package_dir=package) == package / name


@pytest.mark.parametrize("name", ["skills", "config"])
def test_frozen_release_uses_resources_beside_bin(tmp_path, monkeypatch, name):
    root = frozen_runtime(tmp_path, monkeypatch)
    (root / name).mkdir()
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    monkeypatch.chdir(tmp_path)
    assert bundled_resource_dir(name, package_dir=root / "bin/_internal/box_agent") == root.resolve() / name


def test_missing_release_resource_does_not_fall_back_to_internal_copy(tmp_path, monkeypatch):
    root = frozen_runtime(tmp_path, monkeypatch)
    package = root / "bin/_internal/box_agent"
    (package / "skills").mkdir(parents=True)
    assert bundled_resource_dir("skills", package_dir=package) == root.resolve() / "skills"
    assert not bundled_resource_dir("skills", package_dir=package).exists()


def test_frozen_executable_outside_bin_reports_invalid_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "other-app"))
    with pytest.raises(ValueError, match="Invalid runtime layout.*inside bin/"):
        bundled_resource_dir("config", package_dir=tmp_path / "package")


@pytest.mark.parametrize("name", ["../config", "", "mcp_servers", "/skills"])
def test_resource_names_cannot_escape_the_release(name):
    with pytest.raises(ValueError, match="Unknown bundled resource"):
        bundled_resource_dir(name)


def test_release_resource_cannot_redirect_to_a_foreign_directory(tmp_path, monkeypatch):
    root = frozen_runtime(tmp_path, monkeypatch)
    foreign = tmp_path / "unrelated-data"
    foreign.mkdir()
    try:
        (root / "skills").symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this platform")
    with pytest.raises(ValueError, match="must not be redirected"):
        bundled_resource_dir("skills")


@pytest.mark.parametrize("manifest", [None, '{"name":"other-app"}', '[]', 'not-json'])
@pytest.mark.parametrize("name", ["skills", "config"])
def test_release_resource_location_is_independent_of_manifest(tmp_path, monkeypatch, manifest, name):
    root = frozen_runtime(tmp_path, monkeypatch)
    if manifest is None:
        (root / "manifest.json").unlink()
    else:
        (root / "manifest.json").write_text(manifest)
    package = root / "bin/_internal/box_agent"
    (package / name).mkdir(parents=True)
    (root / name).mkdir()
    assert bundled_resource_dir(name, package_dir=package) == root.resolve() / name


def test_skill_navigation_scans_its_relocated_resource_tree(tmp_path, monkeypatch, capsys):
    import json
    import runpy
    import shutil

    skills = tmp_path / "release/skills"
    script = skills / "skill-navigation-assistant-sl/scripts/scan_skills.py"
    script.parent.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "box_agent/skills/skill-navigation-assistant-sl/scripts/scan_skills.py"
    shutil.copyfile(source, script)
    example = skills / "relocated-example"
    example.mkdir()
    (example / "SKILL.md").write_text("---\nname: relocated-example\ndescription: relocated resource\n---\n# Example\n")
    user_skills = tmp_path / "home/.box-agent/skills"
    user_skill = user_skills / "user-example"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("---\nname: user-example\ndescription: user skill\n---\n# User\n")
    legacy = tmp_path / "release/bin/_internal/box_agent/skills"
    stale = legacy / "stale-example"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("---\nname: stale-example\ndescription: stale skill\n---\n# Stale\n")

    # Model an installed application with both layouts, without reading the host.
    app = Path("/Applications/office-raccoon.app/Contents/Resources/box-agent-runtime")
    original_resolve = Path.resolve
    installed_roots = {app / "skills": skills, app / "bin/_internal/box_agent/skills": legacy}

    def resolve_fixture(path, strict=False):
        return original_resolve(installed_roots.get(path, path), strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_fixture)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(sys, "argv", [str(script), "--json"])
    namespace = runpy.run_path(str(script))
    namespace["main"]()
    result = json.loads(capsys.readouterr().out)
    selected = [item for item in result if item["slug"] == "relocated-example"]
    assert len(selected) == 1
    assert selected[0]["source"] == str(skills)
    assert any(item["slug"] == "user-example" for item in result)
    assert not any(item["source"] == str(legacy) for item in result)
