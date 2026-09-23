"""Orchestrate two existing macOS runtime builds without modifying host checkouts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO

ARCHES = ("arm64", "x64")
MACH_O_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}
PYTHON_PROBE = """
import json, platform, sys
sys.path.insert(0, sys.argv[1])
import PyInstaller
from scripts import build_runtime
print(json.dumps({
    "platform": platform.system(), "machine": platform.machine(),
    "python": platform.python_version(), "pyinstaller": PyInstaller.__version__,
    "prefix": sys.prefix, "source": str(build_runtime.PROJECT_ROOT),
}))
"""


def normalize_version(version: str) -> str:
    value = version.removeprefix("v")
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("--mac-all requires an explicit stable --version X.Y.Z")
    return value


def build_environment(python: Path, cache_dir: Path | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in {
        "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "__PYVENV_LAUNCHER__",
        "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYINSTALLER_CONFIG_DIR",
        "BOX_AGENT_RUNTIME_VERSION", "BOX_AGENT_RUNTIME_OUTPUT", "BOX_AGENT_RUNTIME_TARGET",
    }}
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    if cache_dir is not None:
        env["PYINSTALLER_CONFIG_DIR"] = str(cache_dir)
    return env


def inspect_python(python: Path, arch: str, project_root: Path) -> dict[str, str]:
    if not python.is_file():
        raise ValueError(
            f"Missing {arch} Python: {python}. "
            "Prepare a separate venv with project dependencies and PyInstaller."
        )
    try:
        result = subprocess.run(
            [str(python), "-c", PYTHON_PROBE, str(project_root)],
            cwd=project_root, env=build_environment(python),
            check=True, capture_output=True, text=True, timeout=90,
        )
        info = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot run {arch} Python/build imports: {python}. "
            "Check dependencies and Rosetta."
        ) from exc
    expected_machine = "x86_64" if arch == "x64" else "arm64"
    if info.get("platform") != "Darwin" or info.get("machine") != expected_machine:
        raise ValueError(
            f"Python architecture mismatch: {python}; expected Darwin/{expected_machine}, "
            f"got {info.get('platform')}/{info.get('machine')}"
        )
    if Path(info["source"]).resolve() != project_root.resolve():
        raise ValueError(f"Python imported build code from another checkout: {python}")
    return info


def artifact_names(version: str) -> list[str]:
    archives = [f"box-agent-runtime-v{version}-darwin-{arch}.tar.gz" for arch in ARCHES]
    return [name for archive in archives for name in (archive, archive + ".sha256")] + [
        f"box-agent-runtime-v{version}-mac.json"
    ]


def assert_new_output(output_dir: Path, version: str) -> None:
    for name in artifact_names(version):
        if os.path.lexists(output_dir / name):
            raise ValueError(
                f"Refusing to overwrite existing release: {output_dir / name}. "
                "Choose a new version or an empty output directory."
            )


def source_file_allowed(relative: Path) -> bool:
    if relative.parts[0] not in {
        "box_agent", "scripts", "pyproject.toml", "uv.lock", "README.md",
        "MANIFEST.in", "LICENSE",
    }:
        return False
    return not any(
        part.startswith(".env") or part.startswith(".venv") or
        part in {".git", "__pycache__", "node_modules", "config.yaml", "mcp.json"}
        for part in relative.parts
    ) and relative.suffix not in {".pyc", ".pyo"}


def stream_sha256(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def file_sha256(file: Path) -> str:
    with file.open("rb") as stream:
        return stream_sha256(stream)


def snapshot_source(project_root: Path, destination: Path) -> str:
    """Copy current tracked/new source once, excluding local configs and build outputs."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=project_root, capture_output=True, text=True, check=True,
    )
    digest = hashlib.sha256()
    for name in sorted(set(result.stdout.split("\0")) - {""}):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not source_file_allowed(relative):
            continue
        source = project_root / relative
        if source.is_symlink():
            raise ValueError(f"Source must be a regular file, not a symlink: {relative}")
        if not source.exists():
            continue  # Honor tracked deletions in the current worktree.
        if not source.is_file():
            raise ValueError(
                f"Source must be a regular file (initialize/vendor submodules first): {relative}"
            )
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        digest.update(name.encode() + b"\0" + file_sha256(target).encode() + b"\0")
    for required in (
        "scripts/build_runtime.py", "box_agent/acp/runtime_entry.py", "pyproject.toml",
    ):
        if not (destination / required).is_file():
            raise ValueError(f"Source snapshot is incomplete: {required}")
    return digest.hexdigest()


def validate_runtime(work_dir: Path, version: str, arch: str) -> dict[str, object]:
    runtime = work_dir / "box-agent-runtime"
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
    expected = {"version": version, "platform": "darwin", "arch": arch,
                "entry": "bin/box-agent-acp", "external_python_sandbox": True,
                "bundled_stable_runtimes": []}
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError(f"Runtime manifest does not match {version}/darwin-{arch}/ACP-only")
    if (runtime / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError("Runtime VERSION mismatch")
    if any((runtime / name).exists() for name in ("runtime", "runtimes")):
        raise ValueError("ACP-only macOS runtime must not bundle stable Python/Node runtimes")
    native_files = []
    for file in runtime.rglob("*"):
        if file.is_symlink() or not file.is_file():
            continue
        with file.open("rb") as stream:
            is_native = stream.read(4) in MACH_O_MAGICS
        if is_native:
            try:
                subprocess.run(
                    ["xcrun", "lipo", str(file), "-verify_arch",
                     "x86_64" if arch == "x64" else "arm64"],
                    check=True, capture_output=True, text=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"Native dependency does not support {arch}: {file}") from exc
            native_files.append(file)
    if runtime / "bin/box-agent-acp" not in native_files:
        raise ValueError("Missing native ACP launcher")
    archive = work_dir / f"box-agent-runtime-v{version}-darwin-{arch}.tar.gz"
    # Compare archived metadata and native payloads, not just the staged directory.
    with tarfile.open(archive, "r:gz") as bundle:
        for file in [runtime / "manifest.json", runtime / "VERSION", *native_files]:
            name = "box-agent-runtime/" + file.relative_to(runtime).as_posix()
            member = bundle.getmember(name)
            if not member.isfile():
                raise ValueError(f"Expected regular archived file: {name}")
            with bundle.extractfile(member) as stream:
                if stream_sha256(stream) != file_sha256(file):
                    raise ValueError(f"Archive content differs from validated runtime: {name}")
    return {"file": archive.name, "target": f"darwin-{arch}", "sha256": file_sha256(archive),
            "size": archive.stat().st_size, "nativeFilesVerified": len(native_files)}


def publish_local_files(staged: list[Path], output_dir: Path) -> None:
    """Link already-validated files without ever replacing an existing artifact."""
    created = []
    try:
        for source in staged:
            target = output_dir / source.name
            os.link(source, target)
            created.append(target)
    except BaseException:
        for target in reversed(created):
            target.unlink()
        raise


def build_macos_runtimes(
    project_root: Path, version: str, output_dir: Path, *,
    arm_python: str | None = None, intel_python: str | None = None, dry_run: bool = False,
) -> list[Path]:
    version = normalize_version(version)
    if platform.system() != "Darwin":
        raise ValueError("--mac-all requires macOS with runnable ARM and Intel Python environments")
    project_root = project_root.resolve()
    output_dir = output_dir.resolve()
    assert_new_output(output_dir, version)
    # Do not resolve Python symlinks: doing so would bypass pyvenv.cfg and lose its dependencies.
    pythons = {
        "arm64": Path(arm_python or project_root / ".venv/bin/python").expanduser().absolute(),
        "x64": Path(intel_python or project_root / ".venv-x64/bin/python").expanduser().absolute(),
    }
    environments = {arch: inspect_python(pythons[arch], arch, project_root) for arch in ARCHES}
    prefixes = {Path(info["prefix"]).resolve() for info in environments.values()}
    if len(prefixes) != 2:
        raise ValueError("ARM and Intel must use separate Python environments")
    python_versions = {info["python"].rsplit(".", 1)[0] for info in environments.values()}
    builder_versions = {info["pyinstaller"] for info in environments.values()}
    if len(python_versions) != 1 or len(builder_versions) != 1:
        raise ValueError(
            "Use the same Python major/minor and PyInstaller version in both environments"
        )
    print(json.dumps({"version": version, "output": str(output_dir), "dryRun": dry_run,
                      "pythons": {arch: str(python) for arch, python in pythons.items()},
                      "environments": environments, "artifacts": artifact_names(version),
                      "installsHostRuntime": False, "uploads": False}, indent=2), flush=True)
    if dry_run:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = output_dir / f".mac-all-v{version}.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ValueError(
            f"Another build (or interrupted build) owns {lock}; inspect it before retrying"
        ) from exc
    try:
        assert_new_output(output_dir, version)
        stage = Path(tempfile.mkdtemp(prefix=f".mac-all-v{version}-", dir=output_dir))
        print(f"Isolated build workspace (retained for diagnostics): {stage}", flush=True)
        source = stage / "source"
        source_hash = snapshot_source(project_root, source)
        records, staged = [], []
        for arch in ARCHES:
            work_dir = stage / arch
            work_dir.mkdir()
            subprocess.run(
                [str(pythons[arch]), str(source / "scripts/build_runtime.py"),
                 "--version", version, "--target", f"darwin-{arch}",
                 "--output", str(work_dir), "--external-python-sandbox"],
                cwd=source,
                env=build_environment(pythons[arch], work_dir / "pyinstaller-cache"),
                check=True,
            )
            record = validate_runtime(work_dir, version, arch)
            records.append(record)
            archive = work_dir / str(record["file"])
            checksum = archive.with_name(archive.name + ".sha256")
            checksum.write_text(f"{record['sha256']}  {archive.name}\n", encoding="utf-8")
            staged.extend((archive, checksum))
        report = stage / f"box-agent-runtime-v{version}-mac.json"
        report.write_text(
            json.dumps({
                "version": version, "sourceSnapshotSha256": source_hash,
                "environments": environments, "artifacts": records,
            }, indent=2) + "\n", encoding="utf-8",
        )
        publish_local_files([*staged, report], output_dir)
    finally:
        lock.rmdir()
    archives = [output_dir / str(record["file"]) for record in records]
    for archive in archives:
        print(f"Done! Artifact: {archive}")
    print(f"Done! Dual-architecture report: {output_dir / report.name}")
    return archives
