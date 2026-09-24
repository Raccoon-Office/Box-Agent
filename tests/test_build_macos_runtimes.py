"""Dual-architecture orchestration tests; no PyInstaller, installs or uploads."""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import build_macos_runtimes as dual
from scripts import build_runtime


def write_runtime(work_dir: Path, version: str, arch: str) -> Path:
    runtime = work_dir / "box-agent-runtime"
    (runtime / "bin/_internal").mkdir(parents=True)
    (runtime / "bin/box-agent-acp").write_bytes(b"\xcf\xfa\xed\xfe" + arch.encode())
    (runtime / "bin/_internal/library.dylib").write_bytes(b"\xcf\xfa\xed\xfe" + b"dependency")
    (runtime / "VERSION").write_text(version + "\n")
    (runtime / "manifest.json").write_text(json.dumps({
        "version": version, "platform": "darwin", "arch": arch, "entry": "bin/box-agent-acp",
        "external_python_sandbox": True, "bundled_stable_runtimes": [],
    }))
    archive = work_dir / f"box-agent-runtime-v{version}-darwin-{arch}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(runtime, arcname="box-agent-runtime")
    return archive


def python_info(root: Path, arch: str) -> dict[str, str]:
    return {"platform": "Darwin", "machine": "arm64" if arch == "arm64" else "x86_64",
            "python": "3.12.13", "pyinstaller": "6.19.0",
            "source": str(root), "prefix": str(root / arch)}


@pytest.mark.parametrize("version", ["../0.9.13", "0.9.13/next", "0.09.13", "0.9.13-beta.1", "latest", ""])
def test_rejects_unsafe_or_nonstable_version(version: str) -> None:
    with pytest.raises(ValueError, match="version"):
        dual.normalize_version(version)


def test_accepts_leading_v_without_changing_package_version() -> None:
    assert dual.normalize_version("v0.9.13") == "0.9.13"


def test_build_environment_does_not_inherit_host_python_or_pyinstaller_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("PYTHONPATH", "VIRTUAL_ENV", "PYINSTALLER_CONFIG_DIR", "BOX_AGENT_RUNTIME_TARGET"):
        monkeypatch.setenv(key, "host-value")
    env = dual.build_environment(Path("/intel/bin/python"), Path("/isolated/cache"))
    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env
    assert "BOX_AGENT_RUNTIME_TARGET" not in env
    assert env["PYINSTALLER_CONFIG_DIR"] == "/isolated/cache"
    assert env["PATH"].startswith("/intel/bin" + os.pathsep)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_preflight_rejects_wrong_python_architecture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    python = tmp_path / "python"
    python.touch()
    monkeypatch.setattr(dual.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args=args[0], returncode=0, stdout=json.dumps(python_info(tmp_path, "arm64")),
    ))
    with pytest.raises(ValueError, match="architecture mismatch"):
        dual.inspect_python(python, "x64", tmp_path)


def test_preflight_rejects_missing_python(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Missing x64 Python"):
        dual.inspect_python(tmp_path / "missing", "x64", tmp_path)


def test_snapshot_uses_current_source_but_excludes_config_and_generated_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    names = ["scripts/build_runtime.py", "box_agent/acp/runtime_entry.py", "pyproject.toml",
             "box_agent/config/config.yaml", "box_agent/config/mcp.json", "box_agent/config/.env",
             "box_agent/__pycache__/old.pyc", "dist/old.tar.gz", "workspace/task.py", "box_agent/deleted.py"]
    for name in names:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("original")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    (root / "box_agent/acp/runtime_entry.py").write_text("current edit")
    (root / "box_agent/new.py").write_text("new file")
    (root / "box_agent/deleted.py").unlink()
    output = tmp_path / "snapshot"
    source_hash = dual.snapshot_source(root, output)
    assert len(source_hash) == 64
    assert (output / "box_agent/acp/runtime_entry.py").read_text() == "current edit"
    assert (output / "box_agent/new.py").is_file()
    assert not (output / "box_agent/deleted.py").exists()
    for name in names[3:]:
        assert not (output / name).exists()
    assert (root / "box_agent/config/config.yaml").read_text() == "original"


@pytest.mark.parametrize("arch", dual.ARCHES)
def test_validates_archive_metadata_and_every_native_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arch: str,
) -> None:
    archive = write_runtime(tmp_path, "0.9.13", arch)
    calls = []
    monkeypatch.setattr(dual.subprocess, "run", lambda args, **kwargs: calls.append(args))
    record = dual.validate_runtime(tmp_path, "0.9.13", arch)
    assert record["sha256"] == dual.file_sha256(archive)
    assert record["nativeFilesVerified"] == 2
    assert len(calls) == 2
    assert all(args[-1] == ("arm64" if arch == "arm64" else "x86_64") for args in calls)


def test_snapshot_rejects_dangling_symlinks_instead_of_silently_omitting_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "box_agent/link.py"
    source.parent.mkdir()
    source.symlink_to("missing.py")
    monkeypatch.setattr(dual.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args=args[0], returncode=0, stdout="box_agent/link.py\0",
    ))
    with pytest.raises(ValueError, match="symlink"):
        dual.snapshot_source(tmp_path, tmp_path / "snapshot")


def test_rejects_manifest_mislabeled_as_other_architecture(tmp_path: Path) -> None:
    write_runtime(tmp_path, "0.9.13", "arm64")
    with pytest.raises(ValueError, match="manifest"):
        dual.validate_runtime(tmp_path, "0.9.13", "x64")


def test_rejects_archived_binary_different_from_validated_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_runtime(tmp_path, "0.9.13", "arm64")
    monkeypatch.setattr(dual.subprocess, "run", lambda *args, **kwargs: None)
    (tmp_path / "box-agent-runtime/bin/box-agent-acp").write_bytes(b"\xcf\xfa\xed\xfechanged")
    with pytest.raises(ValueError, match="Archive content differs"):
        dual.validate_runtime(tmp_path, "0.9.13", "arm64")


def test_rejects_native_dependency_architecture_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_runtime(tmp_path, "0.9.13", "x64")
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(dual.subprocess, "run", fail)
    with pytest.raises(ValueError, match="does not support x64"):
        dual.validate_runtime(tmp_path, "0.9.13", "x64")


def setup_orchestration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    output = root / "dist/runtime"
    monkeypatch.setattr(dual.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dual, "inspect_python", lambda python, arch, project_root: python_info(root, arch))
    def snapshot(project_root, destination):
        destination.mkdir()
        (destination / "source.txt").write_text("one shared snapshot")
        return "source-sha256"
    monkeypatch.setattr(dual, "snapshot_source", snapshot)
    return root, output


def test_dry_run_preflights_both_environments_without_building_or_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, output = setup_orchestration(tmp_path, monkeypatch)
    def unexpected(*args, **kwargs):
        pytest.fail("dry-run must not snapshot or build")
    monkeypatch.setattr(dual, "snapshot_source", unexpected)
    monkeypatch.setattr(dual.subprocess, "run", unexpected)
    assert dual.build_macos_runtimes(root, "0.9.13", output, dry_run=True) == []
    assert not output.exists()


@pytest.mark.parametrize("field, value, message", [
    ("python", "3.11.9", "same Python"),
    ("pyinstaller", "6.18.0", "same Python"),
    ("prefix", "shared", "separate Python"),
])
def test_preflight_rejects_inconsistent_or_shared_build_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str, message: str,
) -> None:
    root, output = setup_orchestration(tmp_path, monkeypatch)
    def inspect(python, arch, project_root):
        info = python_info(root, arch)
        if field == "prefix" or arch == "x64":
            info[field] = value
        return info
    monkeypatch.setattr(dual, "inspect_python", inspect)
    with pytest.raises(ValueError, match=message):
        dual.build_macos_runtimes(root, "0.9.13", output, dry_run=True)
    assert not output.exists()


def test_dual_mode_refuses_nonmac_host_before_building(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dual.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="requires macOS"):
        dual.build_macos_runtimes(tmp_path, "0.9.13", tmp_path / "output")


def test_dual_build_isolates_caches_and_promotes_only_after_both_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, output = setup_orchestration(tmp_path, monkeypatch)
    calls = []
    def run(args, **kwargs):
        if args[0] == "xcrun":
            return
        assert not list(output.glob("*.tar.gz"))
        arch = args[args.index("--target") + 1].removeprefix("darwin-")
        work = Path(args[args.index("--output") + 1])
        assert kwargs["cwd"].joinpath("source.txt").read_text() == "one shared snapshot"
        assert "--install-officev3" not in args and "--install-lab" not in args
        calls.append((args, kwargs))
        write_runtime(work, "0.9.13", arch)
    monkeypatch.setattr(dual.subprocess, "run", run)
    archives = dual.build_macos_runtimes(root, "v0.9.13", output)
    assert [file.name for file in archives] == [
        f"box-agent-runtime-v0.9.13-darwin-{arch}.tar.gz" for arch in dual.ARCHES
    ]
    assert all(file.is_file() for file in archives)
    assert calls[0][1]["cwd"] == calls[1][1]["cwd"]
    assert calls[0][1]["env"]["PYINSTALLER_CONFIG_DIR"] != calls[1][1]["env"]["PYINSTALLER_CONFIG_DIR"]
    assert calls[0][0][0].endswith(".venv/bin/python")
    assert calls[1][0][0].endswith(".venv-x64/bin/python")
    report = json.loads((output / "box-agent-runtime-v0.9.13-mac.json").read_text())
    assert report["sourceSnapshotSha256"] == "source-sha256"
    assert len(report["artifacts"]) == 2
    assert not (output / ".mac-all-v0.9.13.lock").exists()


def test_intel_build_failure_leaves_no_half_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, output = setup_orchestration(tmp_path, monkeypatch)
    def run(args, **kwargs):
        if args[0] == "xcrun":
            return
        arch = args[args.index("--target") + 1].removeprefix("darwin-")
        if arch == "x64":
            raise subprocess.CalledProcessError(1, args)
        write_runtime(Path(args[args.index("--output") + 1]), "0.9.13", arch)
    monkeypatch.setattr(dual.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        dual.build_macos_runtimes(root, "0.9.13", output)
    assert not list(output.glob("*.tar.gz"))
    assert not list(output.glob("*.sha256"))
    assert not list(output.glob("*.json"))
    assert not (output / ".mac-all-v0.9.13.lock").exists()


def test_existing_release_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, output = setup_orchestration(tmp_path, monkeypatch)
    output.mkdir(parents=True)
    old = output / "box-agent-runtime-v0.9.13-darwin-arm64.tar.gz"
    old.write_bytes(b"keep old release")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        dual.build_macos_runtimes(root, "0.9.13", output)
    assert old.read_bytes() == b"keep old release"


def test_active_release_lock_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, output = setup_orchestration(tmp_path, monkeypatch)
    lock = output / ".mac-all-v0.9.13.lock"
    lock.mkdir(parents=True)
    with pytest.raises(ValueError, match="owns"):
        dual.build_macos_runtimes(root, "0.9.13", output)
    assert lock.is_dir()


def test_promotion_collision_rolls_back_only_our_new_links(tmp_path: Path) -> None:
    staged = tmp_path / "stage"
    staged.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    files = [staged / name for name in ("arm.tar.gz", "intel.tar.gz")]
    for file in files:
        file.write_bytes(b"new")
    (output / "intel.tar.gz").write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        dual.publish_local_files(files, output)
    assert not (output / "arm.tar.gz").exists()
    assert (output / "intel.tar.gz").read_bytes() == b"existing"


def test_keyboard_interrupt_during_promotion_rolls_back_our_new_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "stage"
    staged.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    files = [staged / "arm.tar.gz", staged / "intel.tar.gz"]
    for file in files:
        file.write_bytes(b"new")
    original_link = dual.os.link
    def interrupt(source, target):
        if source == files[1]:
            raise KeyboardInterrupt()
        original_link(source, target)
    monkeypatch.setattr(dual.os, "link", interrupt)
    with pytest.raises(KeyboardInterrupt):
        dual.publish_local_files(files, output)
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("extra", [
    ["--arch", "x64"], ["--target", "darwin-arm64"],
    ["--install-officev3"], ["--install-lab"],
])
def test_cli_rejects_dual_build_with_single_arch_or_host_install(extra, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["build_runtime", "--mac-all", "--version", "0.9.13", *extra])
    with pytest.raises(SystemExit) as exc:
        build_runtime.main()
    assert exc.value.code == 2


def test_cli_requires_explicit_dual_release_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOX_AGENT_RUNTIME_VERSION", raising=False)
    monkeypatch.setattr("sys.argv", ["build_runtime", "--mac-all"])
    with pytest.raises(SystemExit) as exc:
        build_runtime.main()
    assert exc.value.code == 2


def test_cli_dispatches_dual_dry_run_without_creating_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "not-created"
    calls = []
    monkeypatch.setattr(dual, "build_macos_runtimes", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr("sys.argv", [
        "build_runtime", "--mac-all", "--version", "0.9.13",
        "--output", str(output), "--dry-run",
    ])
    build_runtime.main()
    assert calls[0][0][1:] == ("0.9.13", output)
    assert calls[0][1]["dry_run"] is True
    assert not output.exists()


def test_cli_keeps_existing_single_target_build_and_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "single"
    archive = output / "runtime.tar.gz"
    calls = []
    monkeypatch.setattr(
        build_runtime, "build_runtime",
        lambda *args, **kwargs: calls.append((args, kwargs)) or archive,
    )
    monkeypatch.setattr(build_runtime, "resolve_officev3_dir", lambda _: tmp_path / "officev3")
    monkeypatch.setattr(
        build_runtime, "install_runtime_into_officev3",
        lambda *args, **kwargs: calls.append((args, kwargs)) or tmp_path,
    )
    monkeypatch.setattr("sys.argv", [
        "build_runtime", "--version", "0.9.13", "--target", "darwin-arm64",
        "--output", str(output), "--install-officev3",
    ])
    build_runtime.main()
    assert calls[0][1]["target"] == "darwin-arm64"
    assert calls[1][0][0] == archive


def test_cli_rejects_dry_run_for_legacy_single_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["build_runtime", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        build_runtime.main()
    assert exc.value.code == 2
