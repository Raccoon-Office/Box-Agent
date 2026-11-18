#!/usr/bin/env python3
"""Windows-only BoxAgent runtime builder; ACP-only by default.

Stages:
    bin/              ← PyInstaller-frozen BoxAgent (changes when source changes)
    skills/, config/  ← version-matched external resource data
    runtime/          ← PortableGit + Python (only --bundled-python-sandbox)
    runtimes/         ← Node (only --bundled-python-sandbox)

`--exe-only` rebuilds **bin/, skills/ and config/**, leaving runtime/ and runtimes/ untouched.
The selected profile must match the existing output and --install-to manifests;
switch profiles with a clean build (without --exe-only).
Use this when you change BoxAgent Python source but don't touch bash/node/python.

Usage:
    # Slim ACP build; analysis kernel and stable runtimes are provided by the host
    python scripts/build_win_runtime.py --version 0.8.40

    # Legacy full bundle (PyInstaller + bash + python + node + tar.gz)
    python scripts/build_win_runtime.py --version 0.8.40 --bundled-python-sandbox

    # Rebuild only the BoxAgent exe; keep existing runtime/ and runtimes/
    python scripts/build_win_runtime.py --version 0.8.40 --exe-only

    # Drop new bin/ straight onto the dev install (no tar)
    python scripts/build_win_runtime.py --exe-only --no-tar \\
        --install-to D:\\qilin2\\officev3\\build-resources\\box-agent-runtime

Output (default):
    dist/runtime/box-agent-runtime/                 (assembled tree)
    dist/runtime/box-agent-runtime-v{ver}-win32-x64.tar.gz
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _read_version_from_package() -> str:
    """Pull version from box_agent/__init__.py or pyproject — fallback to 'dev'."""
    init_file = PROJECT_ROOT / "box_agent" / "__init__.py"
    if init_file.is_file():
        for line in init_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("__version__"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "dev"


def _ensure_win() -> None:
    if sys.platform != "win32":
        print(f"This script only runs on Windows (got: {sys.platform})", file=sys.stderr)
        sys.exit(2)


def _install_runtime_extras() -> None:
    """Install build extras into the interpreter running this script."""
    requirement = f"{PROJECT_ROOT}[runtime]"
    pip_cmd = [
        sys.executable, "-m", "pip", "install", "--quiet", requirement,
    ]
    uv_bin = shutil.which("uv")
    if uv_bin:
        uv_cmd = [
            uv_bin,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--quiet",
            requirement,
        ]
        result = subprocess.run(uv_cmd, cwd=str(PROJECT_ROOT))
        if result.returncode == 0:
            return
        print(
            "uv failed to install runtime extras; retrying with pip...",
            file=sys.stderr,
        )

    result = subprocess.run(pip_cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to install runtime extras into "
            f"{sys.executable} (exit code {result.returncode})"
        )


def _load_build_dependencies() -> None:
    """Load project helpers only after their dependencies are installed."""
    global BUILD_TOOLS_CACHE
    global PYTHON_STANDALONE_VERSION
    global DEFAULT_NODE_VERSION
    global NodeRuntimeManager
    global _install_portable_git_win
    global _install_portable_python_win
    global _install_sandbox_packages_win
    global _relativize_node_manifest

    from scripts import build_runtime
    from box_agent.tools.runtime import (
        DEFAULT_NODE_VERSION as node_version,
        NodeRuntimeManager as node_runtime_manager,
    )

    BUILD_TOOLS_CACHE = build_runtime.BUILD_TOOLS_CACHE
    PYTHON_STANDALONE_VERSION = build_runtime.PYTHON_STANDALONE_VERSION
    DEFAULT_NODE_VERSION = node_version
    NodeRuntimeManager = node_runtime_manager
    _install_portable_git_win = build_runtime._install_portable_git_win
    _install_portable_python_win = build_runtime._install_portable_python_win
    _install_sandbox_packages_win = build_runtime._install_sandbox_packages_win
    _relativize_node_manifest = build_runtime._relativize_node_manifest


def _rmtree_with_retry(
    path: Path,
    *,
    attempts: int = 5,
    delay_seconds: float = 0.2,
) -> None:
    """Remove a tree, retrying transient Windows directory errors."""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay_seconds * (attempt + 1))


def _windows_pyinstaller_hidden_imports(*, external_python_sandbox: bool = True) -> list[str]:
    """Return the shared runtime hidden imports for the Windows build."""
    from scripts import build_runtime

    return build_runtime.pyinstaller_hidden_imports(
        external_python_sandbox=external_python_sandbox,
    )


def _windows_pyinstaller_collect_args(*, external_python_sandbox: bool = True) -> list[str]:
    """Return the shared runtime collect args for the Windows build."""
    from scripts import build_runtime

    return build_runtime.pyinstaller_collect_args(
        external_python_sandbox=external_python_sandbox,
    )


def _run_pyinstaller(bin_dir: Path, *, external_python_sandbox: bool = True) -> None:
    """Run PyInstaller and copy the output into ``bin_dir``."""
    from scripts import build_runtime

    work_dir = bin_dir.parent.parent / "pyinstaller_work"
    dist_dir = bin_dir.parent.parent / "pyinstaller_out"
    spec_dir = bin_dir.parent.parent
    if work_dir.exists():
        _rmtree_with_retry(work_dir)
    if dist_dir.exists():
        _rmtree_with_retry(dist_dir)

    entry_point = PROJECT_ROOT / "box_agent" / "acp" / "runtime_entry.py"

    hidden_imports = _windows_pyinstaller_hidden_imports(
        external_python_sandbox=external_python_sandbox
    )
    hidden_args: list[str] = []
    for imp in hidden_imports:
        hidden_args.extend(["--hidden-import", imp])
    collect_args = _windows_pyinstaller_collect_args(
        external_python_sandbox=external_python_sandbox
    )
    exclude_args = build_runtime.pyinstaller_exclude_args(
        external_python_sandbox=external_python_sandbox
    )

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "box-agent-acp",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        "--specpath", str(spec_dir),
        *hidden_args, *collect_args, *exclude_args,
        str(entry_point),
    ]

    print("\n[win] Running PyInstaller...")
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print("PyInstaller failed!", file=sys.stderr)
        sys.exit(1)

    pyinstaller_output = dist_dir / "box-agent-acp"
    if not pyinstaller_output.exists():
        print(f"Expected output not found: {pyinstaller_output}", file=sys.stderr)
        sys.exit(1)

    if bin_dir.exists():
        _rmtree_with_retry(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    for item in pyinstaller_output.iterdir():
        dest = bin_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    entry_bin = bin_dir / "box-agent-acp.exe"
    if entry_bin.exists():
        entry_bin.chmod(0o755)

    shutil.rmtree(dist_dir, ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    for spec in spec_dir.glob("*.spec"):
        spec.unlink(missing_ok=True)

    print(f"[win] BoxAgent exe -> {bin_dir}")


def _install_node_win(runtime_dir: Path) -> None:
    node_root = runtime_dir / "runtimes" / "node"
    if (node_root / "versions").exists() and any((node_root / "versions").iterdir()):
        print(f"[win] Node runtime already present: {node_root}")
        return
    print(f"\n[win] Installing bundled Node.js {DEFAULT_NODE_VERSION} for win-x64...")
    manager = NodeRuntimeManager(root=node_root)
    manager.install_win(version=DEFAULT_NODE_VERSION, platform_id="win-x64")
    shutil.rmtree(node_root / "downloads", ignore_errors=True)
    _relativize_node_manifest(node_root)
    print(f"[win] Node runtime ready: {node_root}")


def _write_manifest(
    runtime_dir: Path, version: str, *, external_python_sandbox: bool = True
) -> None:
    from scripts import build_runtime

    manifest = build_runtime.build_runtime_manifest(
        version=version,
        plat="win32",
        arch="x64",
        entry_path="bin/box-agent-acp.exe",
        external_python_sandbox=external_python_sandbox,
        bundled_components=build_runtime.bundled_stable_runtime_components(
            plat="win32",
            arch="x64",
            external_python_sandbox=external_python_sandbox,
        ),
    )
    (runtime_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (runtime_dir / "VERSION").write_text(version + "\n", encoding="utf-8")


def _validate_exe_only_profile(runtime_dir: Path, *, external_python_sandbox: bool) -> None:
    """Reject an in-place rebuild that would mislabel retained runtime files."""
    rebuild_hint = "Run a clean build without --exe-only to create or switch profiles."
    try:
        manifest = json.loads((runtime_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read runtime manifest at {runtime_dir}. {rebuild_hint}") from exc
    existing = manifest.get("external_python_sandbox") if isinstance(manifest, dict) else None
    if not isinstance(existing, bool):
        raise ValueError(f"Cannot determine runtime profile at {runtime_dir}. {rebuild_hint}")
    if existing != external_python_sandbox:
        existing_name = "slim" if existing else "full"
        requested_name = "slim" if external_python_sandbox else "full"
        raise ValueError(
            f"--exe-only profile mismatch at {runtime_dir}: existing {existing_name}, "
            f"requested {requested_name}. {rebuild_hint}"
        )


def _create_tar(output_dir: Path, runtime_dir: Path, version: str) -> Path:
    archive_name = f"box-agent-runtime-v{version}-win32-x64.tar.gz"
    archive_path = output_dir / archive_name
    print(f"\n[win] Creating archive: {archive_path}")
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(runtime_dir, arcname="box-agent-runtime")
    size_mb = archive_path.stat().st_size / (1024 * 1024)
    print(f"[win] Archive created: {archive_path} ({size_mb:.1f} MB)")
    return archive_path


def _validate_external_payload(runtime_dir: Path) -> None:
    for relative in (
        "bin/box-agent-acp.exe", "skills/_manifest.json",
        "config/config-example.yaml", "manifest.json", "VERSION",
    ):
        if not (runtime_dir / relative).is_file():
            raise ValueError(f"Incomplete runtime payload: missing {runtime_dir / relative}")


def _replace_exe_only_payload(runtime_dir: Path, target: Path) -> None:
    """Replace the versioned executable and resources together; retain stable tools."""
    _validate_external_payload(runtime_dir)
    names = ("bin", "skills", "config", "manifest.json", "VERSION")
    target.parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=".box-agent-payload-", dir=target.parent))
    preserve_backup = False
    try:
        staging, backup = folder / "new", folder / "old"
        staging.mkdir()
        backup.mkdir()
        for name in names:
            source = runtime_dir / name
            if source.is_dir():
                shutil.copytree(source, staging / name)
            else:
                shutil.copy2(source, staging / name)
        target.mkdir(parents=True, exist_ok=True)
        preserve_backup = True
        try:
            for name in names:
                destination = target / name
                if destination.is_symlink():
                    raise ValueError(f"Refusing to replace redirected runtime payload: {destination}")
                if destination.exists():
                    destination.rename(backup / name)
                (staging / name).rename(destination)
        except BaseException as publish_error:
            try:
                # An interrupt can arrive after a rename completes but before
                # Python resumes. The staging and backup paths record the result.
                for name in reversed(names):
                    if (staging / name).exists():
                        continue
                    destination = target / name
                    if destination.is_dir():
                        _rmtree_with_retry(destination)
                    else:
                        destination.unlink()
                for name in reversed(names):
                    previous = backup / name
                    if previous.exists():
                        previous.rename(target / name)
            except BaseException as rollback_error:
                raise RuntimeError(
                    f"Runtime payload publication failed ({publish_error!r}); "
                    f"rollback failed ({rollback_error!r}); "
                    f"old payload backup retained at {backup}"
                ) from rollback_error
            preserve_backup = False
            raise
        else:
            preserve_backup = False
    finally:
        if not preserve_backup:
            _rmtree_with_retry(folder)


def _install_to(runtime_dir: Path, target: Path, exe_only: bool) -> None:
    """Copy assembled runtime tree to an existing officev3 install location."""
    _validate_external_payload(runtime_dir)
    target = target.resolve()
    if exe_only:
        _replace_exe_only_payload(runtime_dir, target)
        print(f"[win] Installed bin/, skills/ and config/ -> {target}")
    else:
        if target.exists():
            _rmtree_with_retry(target)
        shutil.copytree(runtime_dir, target)
        print(f"[win] Installed full runtime tree -> {target}")


def main() -> None:
    _ensure_win()

    parser = argparse.ArgumentParser(description="Windows-only BoxAgent runtime builder")
    parser.add_argument("--version", default=None,
                        help="Version string (default: read from box_agent/__init__.py)")
    parser.add_argument("--output", default="dist/runtime",
                        help="Output directory (default: dist/runtime)")
    parser.add_argument("--exe-only", action="store_true",
                        help="Rebuild bin/, skills/ and config/, keeping the same slim/full profile "
                             "and existing runtime/ and runtimes/ in output and --install-to.")
    parser.add_argument("--no-tar", action="store_true",
                        help="Skip the tar.gz archive step (faster dev iteration).")
    parser.add_argument("--bundled-python-sandbox", action="store_true",
                        help="Legacy standalone bundle including Python analysis dependencies. "
                             "By default Windows builds ACP only and uses the host's managed tools.")
    parser.add_argument("--install-to", default=None,
                        help="After build, copy artifacts to this path "
                             "(e.g. officev3 build-resources/box-agent-runtime).")
    args = parser.parse_args()
    external_python_sandbox = not args.bundled_python_sandbox

    version = args.version or _read_version_from_package()
    output_dir = Path(args.output).resolve()
    runtime_dir = output_dir / "box-agent-runtime"
    if args.exe_only:
        try:
            _validate_exe_only_profile(runtime_dir, external_python_sandbox=external_python_sandbox)
            if args.install_to:
                _validate_exe_only_profile(
                    Path(args.install_to).resolve(), external_python_sandbox=external_python_sandbox
                )
        except ValueError as exc:
            parser.error(str(exc))
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\n[win] Installing runtime extras...")
    _install_runtime_extras()
    _load_build_dependencies()
    BUILD_TOOLS_CACHE.mkdir(parents=True, exist_ok=True)

    print(f"Building box-agent-runtime v{version} for win32-x64")
    print(f"Output: {runtime_dir}")
    print(f"Mode: {'exe-only' if args.exe_only else 'full'}")

    if args.exe_only:
        # Prepare the whole versioned payload before replacing existing files.
        from scripts.build_runtime import copy_runtime_resources
        with tempfile.TemporaryDirectory(prefix=".box-agent-build-", dir=output_dir) as folder:
            staging = Path(folder)
            _run_pyinstaller(staging / "bin", external_python_sandbox=external_python_sandbox)
            copy_runtime_resources(PROJECT_ROOT, staging)
            _write_manifest(staging, version, external_python_sandbox=external_python_sandbox)
            _replace_exe_only_payload(staging, runtime_dir)
    else:
        # Full clean build
        if runtime_dir.exists():
            _rmtree_with_retry(runtime_dir)
        runtime_dir.mkdir(parents=True)
        bin_dir = runtime_dir / "bin"
        bin_dir.mkdir()
        _run_pyinstaller(bin_dir, external_python_sandbox=external_python_sandbox)
        from scripts.build_runtime import copy_runtime_resources
        copy_runtime_resources(PROJECT_ROOT, runtime_dir)
        _write_manifest(runtime_dir, version, external_python_sandbox=external_python_sandbox)
        # bash + python + sandbox packages + node
        if not external_python_sandbox:
            _install_portable_git_win(runtime_dir)
            _install_portable_python_win(runtime_dir)
            python_exe = runtime_dir / "runtime" / "python" / "python.exe"
            if python_exe.is_file():
                _install_sandbox_packages_win(python_exe)
            _install_node_win(runtime_dir)

    _validate_external_payload(runtime_dir)

    print(f"\n[win] Runtime tree assembled: {runtime_dir}")

    if not args.no_tar:
        _create_tar(output_dir, runtime_dir, version)
    else:
        print("[win] --no-tar set, skipping archive step")

    if args.install_to:
        _install_to(runtime_dir, Path(args.install_to), args.exe_only)


if __name__ == "__main__":
    main()
