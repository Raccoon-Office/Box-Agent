"""External resource assembly and version-matched Windows payload updates."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
import sys
import tarfile

import pytest

from scripts import build_runtime, build_win_runtime, verify_linux_runtime


def source_tree(root: Path) -> Path:
    config = root / "box_agent/config"
    config.mkdir(parents=True)
    for name in build_runtime._RUNTIME_CONFIG_FILES:
        (config / name).write_text("bundled " + name)
    (config / "config.yaml").write_text("private configuration")
    (config / "auth.json").write_text("private authentication")
    skills = root / "box_agent/skills"
    skills.mkdir()
    (skills / "_manifest.json").write_text('{"skills": []}')
    for relative in (
        "example/SKILL.md", "example/references/input.txt",
        "example/scripts/helper.py", "example/lib/renderer.cjs",
        "example/node_modules/dependency/private.txt",
        "example/__pycache__/helper.pyc", "example/helper.pyo", ".DS_Store",
    ):
        path = skills / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture " + relative)
    return root


def payload(root: Path, version: str) -> Path:
    for relative in ("bin/box-agent-acp.exe", "skills/_manifest.json", "config/config-example.yaml"):
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(version)
    build_win_runtime._write_manifest(root, version)
    return root


def files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_resource_copy_preserves_skill_assets_without_local_dependencies_or_credentials(tmp_path):
    project = source_tree(tmp_path / "source")
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    build_runtime.copy_runtime_resources(project, runtime)

    copied = files(runtime)
    assert "skills/example/lib/renderer.cjs" in copied
    assert "skills/example/scripts/helper.py" in copied
    assert "skills/example/references/input.txt" in copied
    assert not any("node_modules" in path or "__pycache__" in path for path in copied)
    assert not any(path.endswith((".pyc", ".pyo", ".DS_Store")) for path in copied)
    assert set(p.name for p in (runtime / "config").iterdir()) == build_runtime._RUNTIME_CONFIG_FILES
    assert copied["config/config-example.yaml"] == b"bundled config-example.yaml"
    assert copied["config/memory_block_default.md"] == b"bundled memory_block_default.md"
    assert (project / "box_agent/skills/example/node_modules/dependency/private.txt").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="source links require developer-mode privileges")
def test_resource_copy_refuses_a_link_outside_the_source_tree(tmp_path):
    project = source_tree(tmp_path / "source")
    foreign = tmp_path / "foreign"
    foreign.write_text("must not be packaged")
    (project / "box_agent/skills/external.txt").symlink_to(foreign)
    with pytest.raises(RuntimeError, match="contains a link"):
        build_runtime.copy_runtime_resources(project, tmp_path / "runtime")
    assert foreign.read_text() == "must not be packaged"


def test_linux_assembly_keeps_resources_beside_bin_and_mcp_inside_the_frozen_package(tmp_path, monkeypatch):
    project = source_tree(tmp_path / "source")
    captured = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        output = Path(command[command.index("--distpath") + 1]) / "box-agent-acp"
        output.mkdir(parents=True)
        (output / "box-agent-acp").write_bytes(b"frozen program")
        native = output / "_internal/box_agent/mcp_servers/native/panel_keyboard.swift"
        native.parent.mkdir(parents=True)
        native.write_text("native source")
        return CompletedProcess(command, 0)

    monkeypatch.setattr(build_runtime, "PROJECT_ROOT", project)
    monkeypatch.setattr(build_runtime, "require_supported_build_process", lambda *args: None)
    monkeypatch.setattr(build_runtime, "verify_entry_binary_arch", lambda *args, **kwargs: None)
    monkeypatch.setattr(build_runtime.subprocess, "run", fake_run)
    archive = build_runtime.build_runtime("test-version", tmp_path / "output", target="linux-x64")

    assert "--add-data" not in captured
    pairs = list(zip(captured, captured[1:]))
    assert ("--collect-data", "box_agent.mcp_servers") in pairs
    runtime = archive.parent / "box-agent-runtime"
    assert (runtime / "bin/box-agent-acp").read_bytes() == b"frozen program"
    assert verify_linux_runtime._verify_external_resources(runtime) == runtime / "config/config-example.yaml"
    with tarfile.open(archive) as handle:
        names = handle.getnames()
    assert "box-agent-runtime/skills/example/scripts/helper.py" in names
    assert "box-agent-runtime/config/system_prompt.md" in names
    assert "box-agent-runtime/bin/_internal/box_agent/mcp_servers/native/panel_keyboard.swift" in names
    assert not any("node_modules" in name or "__pycache__" in name for name in names)
    assert not any("bin/_internal/box_agent/skills" in name or "bin/_internal/box_agent/config" in name for name in names)


@pytest.mark.parametrize("missing", ["config/config-example.yaml", "skills/_manifest.json"])
def test_linux_verifier_rejects_missing_external_resources(tmp_path, missing):
    runtime = payload(tmp_path / "runtime", "fixture")
    (runtime / missing).unlink()
    with pytest.raises(RuntimeError, match="missing"):
        verify_linux_runtime._verify_external_resources(runtime)


@pytest.mark.parametrize("name", ["config", "skills"])
def test_linux_verifier_rejects_duplicate_internal_resources(tmp_path, name):
    runtime = payload(tmp_path / "runtime", "fixture")
    (runtime / "bin/_internal/box_agent" / name).mkdir(parents=True)
    with pytest.raises(RuntimeError, match="duplicate internal"):
        verify_linux_runtime._verify_external_resources(runtime)


@pytest.mark.parametrize("exe_only", [True, False])
def test_windows_install_rejects_missing_resources_before_touching_destination(tmp_path, exe_only):
    source = payload(tmp_path / "source", "new")
    target = payload(tmp_path / "target", "old")
    before = files(target)
    (source / "skills/_manifest.json").unlink()
    with pytest.raises(ValueError, match="Incomplete runtime payload"):
        build_win_runtime._install_to(source, target, exe_only)
    assert files(target) == before


@pytest.mark.parametrize("old_layout", ["internal", "external"])
def test_windows_exe_only_replaces_bin_and_resources_together_and_keeps_stable_tools(tmp_path, old_layout):
    source = payload(tmp_path / "source", "new")
    target = payload(tmp_path / "target", "old")
    tool = target / "runtime/python/python.exe"
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b"stable Python")
    (target / "skills/stale.md").write_text("old-only Skill")
    if old_layout == "internal":
        internal = target / "bin/_internal/box_agent"
        internal.mkdir(parents=True)
        for name in ("skills", "config"):
            (target / name).rename(internal / name)

    build_win_runtime._install_to(source, target, True)

    assert (target / "bin/box-agent-acp.exe").read_text() == "new"
    assert (target / "skills/_manifest.json").read_text() == "new"
    assert (target / "config/config-example.yaml").read_text() == "new"
    assert not (target / "skills/stale.md").exists()
    assert not (target / "bin/_internal/box_agent/skills").exists()
    assert json.loads((target / "manifest.json").read_text())["version"] == "new"
    assert tool.read_bytes() == b"stable Python"
    assert not list(tmp_path.glob(".box-agent-payload-*"))


def test_windows_exe_only_publish_failure_restores_old_bin_resources_and_version(tmp_path, monkeypatch):
    source = payload(tmp_path / "source", "new")
    target = payload(tmp_path / "target", "old")
    before = files(target)
    rename = Path.rename

    def fail_config(self, destination):
        if self.name == "config" and self.parent.name == "new":
            raise OSError("simulated config publication failure")
        return rename(self, destination)

    monkeypatch.setattr(Path, "rename", fail_config)
    with pytest.raises(OSError, match="publication failure"):
        build_win_runtime._replace_exe_only_payload(source, target)
    assert files(target) == before
    assert not list(tmp_path.glob(".box-agent-payload-*"))


@pytest.mark.parametrize("interruption", ["after_backup", "before_publish", "after_publish"])
def test_windows_exe_only_interruption_restores_previous_payload(tmp_path, monkeypatch, interruption):
    source = payload(tmp_path / "source", "new")
    target = payload(tmp_path / "target", "old")
    before = files(target)
    rename = Path.rename

    def interrupt_config(self, destination):
        if self == target / "config" and interruption == "after_backup":
            rename(self, destination)
            raise KeyboardInterrupt("interrupted after backing up config")
        if self.name == "config" and self.parent.name == "new" and interruption != "after_backup":
            if interruption == "after_publish":
                rename(self, destination)
            raise KeyboardInterrupt("interrupted while publishing config")
        return rename(self, destination)

    monkeypatch.setattr(Path, "rename", interrupt_config)
    with pytest.raises(KeyboardInterrupt, match="interrupted"):
        build_win_runtime._install_to(source, target, True)
    assert files(target) == before
    assert not list(tmp_path.glob(".box-agent-payload-*"))


def test_windows_exe_only_build_failure_keeps_the_published_payload(tmp_path, monkeypatch):
    runtime = payload(tmp_path / "output/box-agent-runtime", "old")
    stable = runtime / "runtimes/node/marker.txt"
    stable.parent.mkdir(parents=True)
    stable.write_text("stable Node")
    before = files(runtime)

    def failed_build(bin_dir, **kwargs):
        bin_dir.mkdir(parents=True)
        (bin_dir / "box-agent-acp.exe").write_bytes(b"partial build")
        raise RuntimeError("simulated build failure")

    monkeypatch.setattr(build_win_runtime, "_ensure_win", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_install_runtime_extras", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_load_build_dependencies", lambda: None)
    monkeypatch.setattr(build_win_runtime, "BUILD_TOOLS_CACHE", tmp_path / "cache", raising=False)
    monkeypatch.setattr(build_win_runtime, "_run_pyinstaller", failed_build)
    monkeypatch.setattr(build_win_runtime.sys, "argv", [
        "build_win_runtime.py", "--exe-only", "--no-tar", "--output", str(runtime.parent),
    ])
    with pytest.raises(RuntimeError, match="build failure"):
        build_win_runtime.main()
    assert files(runtime) == before
    assert not list(runtime.parent.glob(".box-agent-build-*"))


@pytest.mark.parametrize("rollback_failure", ["restore", "remove", "interrupt"])
def test_windows_exe_only_incomplete_rollback_retains_old_backup_and_stable_tools(
    tmp_path, monkeypatch, rollback_failure,
):
    source = payload(tmp_path / "source", "new")
    target = payload(tmp_path / "target", "old")
    stable_tools = ("runtime/python/python.exe", "runtimes/node/marker.txt")
    for relative in stable_tools:
        tool = target / relative
        tool.parent.mkdir(parents=True)
        tool.write_text("stable tool")
    rename = Path.rename
    remove = build_win_runtime._rmtree_with_retry

    def fail_publication_or_restore(self, destination):
        if self.name == "config" and self.parent.name == "new":
            raise OSError("simulated config publication failure")
        if self.name == "skills" and self.parent.name == "old":
            if rollback_failure == "restore":
                raise PermissionError("simulated old Skills restore failure")
            if rollback_failure == "interrupt":
                raise KeyboardInterrupt("interrupted while restoring old Skills")
        return rename(self, destination)

    def fail_rollback_removal(path, **kwargs):
        if rollback_failure == "remove" and path == target / "skills":
            raise PermissionError("simulated new Skills removal failure")
        return remove(path, **kwargs)

    monkeypatch.setattr(Path, "rename", fail_publication_or_restore)
    monkeypatch.setattr(build_win_runtime, "_rmtree_with_retry", fail_rollback_removal)
    with pytest.raises(RuntimeError, match="backup retained at") as exc_info:
        build_win_runtime._replace_exe_only_payload(source, target)

    backups = list(tmp_path.glob(".box-agent-payload-*/old"))
    assert len(backups) == 1
    backup = backups[0]
    assert str(backup) in str(exc_info.value)
    assert "simulated config publication failure" in str(exc_info.value)
    expected_error = KeyboardInterrupt if rollback_failure == "interrupt" else PermissionError
    assert isinstance(exc_info.value.__cause__, expected_error)
    assert "publication failure" in str(exc_info.value.__cause__.__context__)
    assert (backup / "bin/box-agent-acp.exe").read_text() == "old"
    assert (backup / "skills/_manifest.json").read_text() == "old"
    if rollback_failure == "remove":
        assert (backup / "config/config-example.yaml").read_text() == "old"
    for relative in stable_tools:
        assert (target / relative).read_text() == "stable tool"


def test_windows_exe_only_staging_failure_keeps_old_payload_and_cleans_temporary_files(
    tmp_path, monkeypatch,
):
    source = payload(tmp_path / "source", "new")
    target = payload(tmp_path / "target", "old")
    before = files(target)
    copytree = build_win_runtime.shutil.copytree

    def failed_copy(source_path, destination, *args, **kwargs):
        if source_path == source / "skills":
            raise OSError("simulated staging failure")
        return copytree(source_path, destination, *args, **kwargs)

    monkeypatch.setattr(build_win_runtime.shutil, "copytree", failed_copy)
    with pytest.raises(OSError, match="staging failure"):
        build_win_runtime._replace_exe_only_payload(source, target)
    assert files(target) == before
    assert not list(tmp_path.glob(".box-agent-payload-*"))
