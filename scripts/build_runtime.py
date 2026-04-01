#!/usr/bin/env python3
"""Build a standalone box-agent-runtime artifact for the current platform.

Usage:
    python scripts/build_runtime.py [--version 0.3.2] [--output dist/runtime]

Produces:
    dist/runtime/box-agent-runtime-v{version}-{platform}-{arch}.tar.gz

Requires:
    pip install pyinstaller
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

# ── Platform detection ───────────────────────────────────────

def detect_platform() -> tuple[str, str]:
    """Return (platform, arch) in Electron naming convention."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        plat = "darwin"
    elif system == "linux":
        plat = "linux"
    elif system == "windows":
        plat = "win32"
    else:
        plat = system

    arch_map = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    arch = arch_map.get(machine, machine)

    return plat, arch


# ── Build ────────────────────────────────────────────────────

def build_runtime(version: str, output_dir: Path) -> Path:
    """Build the runtime artifact and return the archive path."""
    plat, arch = detect_platform()
    project_root = Path(__file__).resolve().parent.parent

    print(f"Building box-agent-runtime v{version} for {plat}-{arch}")
    print(f"Project root: {project_root}")

    # ── Step 0: Install runtime extras into current env ─────
    print("\nInstalling runtime extras (data science packages)...")
    # Try uv first (used in dev), fall back to pip
    extras_cmd_uv = ["uv", "pip", "install", "--quiet", f"{project_root}[runtime]"]
    extras_cmd_pip = [sys.executable, "-m", "pip", "install", "--quiet", f"{project_root}[runtime]"]
    result = subprocess.run(extras_cmd_uv, cwd=str(project_root), capture_output=True)
    if result.returncode != 0:
        result = subprocess.run(extras_cmd_pip, cwd=str(project_root))
    if result.returncode != 0:
        print("Warning: failed to install runtime extras", file=sys.stderr)

    # ── Step 1: PyInstaller ──────────────────────────────────
    dist_dir = output_dir / "pyinstaller_out"
    dist_dir.mkdir(parents=True, exist_ok=True)

    entry_point = project_root / "box_agent" / "acp" / "runtime_entry.py"

    # Collect data files: config/, skills/
    datas = [
        (str(project_root / "box_agent" / "config"), "box_agent/config"),
        (str(project_root / "box_agent" / "skills"), "box_agent/skills"),
    ]
    datas_args = []
    for src, dst in datas:
        if Path(src).exists():
            datas_args.extend(["--add-data", f"{src}{os.pathsep}{dst}"])

    # Hidden imports that PyInstaller misses
    hidden_imports = [
        "box_agent",
        "box_agent.acp",
        "box_agent.acp.debug_logger",
        "box_agent.agent",
        "box_agent.cli",
        "box_agent.config",
        "box_agent.core",
        "box_agent.events",
        "box_agent.llm",
        "box_agent.llm.anthropic_client",
        "box_agent.llm.openai_client",
        "box_agent.llm.llm_wrapper",
        "box_agent.logger",
        "box_agent.retry",
        "box_agent.schema",
        "box_agent.tools",
        "box_agent.tools.bash_tool",
        "box_agent.tools.file_tools",
        "box_agent.tools.jupyter_tool",
        "box_agent.tools.mcp_loader",
        "box_agent.tools.skill_tool",
        "box_agent.tools.web_search_tool",
        "box_agent.utils",
        # Third-party
        "tiktoken",
        "tiktoken_ext",
        "tiktoken_ext.openai_public",
        "httpx",
        "httpcore",
        "anthropic",
        "openai",
        "pydantic",
        "yaml",
        "mcp",
        "acp",
        "jupyter_client",
        "jupyter_client.provisioning",
        "jupyter_client.provisioning.local_provisioner",
        "ipykernel",
        "ipykernel.inprocess",
        "ipykernel.inprocess.manager",
        "ipykernel_launcher",
        "jupyter_core",
        # debugpy (ipykernel dependency, has _vendored/pydevd subtree)
        "debugpy",
        "debugpy._vendored",
        # Data science (runtime extras)
        "pandas",
        "numpy",
        "matplotlib",
        "matplotlib.backends",
        "matplotlib.backends.backend_agg",
        "seaborn",
        "openpyxl",
        "xlrd",
        "sklearn",
        "sklearn.cluster",
        "sklearn.linear_model",
        "sklearn.preprocessing",
        # pip (used as library in frozen mode for runtime package installs)
        "pip",
        "pip._internal",
        "pip._internal.cli",
        "pip._internal.cli.main",
    ]
    hidden_args = []
    for imp in hidden_imports:
        hidden_args.extend(["--hidden-import", imp])

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", "box-agent-acp",
        "--distpath", str(dist_dir),
        "--workpath", str(output_dir / "pyinstaller_build"),
        "--specpath", str(output_dir),
        *datas_args,
        *hidden_args,
        "--collect-all", "tiktoken",
        "--collect-all", "tiktoken_ext",
        "--collect-all", "jupyter_client",
        "--collect-all", "ipykernel",
        "--collect-all", "jupyter_core",
        "--collect-all", "debugpy",
        "--collect-all", "matplotlib",
        "--collect-submodules", "pandas",
        "--collect-submodules", "seaborn",
        "--collect-submodules", "openpyxl",
        "--collect-submodules", "sklearn",
        "--collect-submodules", "pip",
        str(entry_point),
    ]

    print(f"\nRunning PyInstaller...")
    result = subprocess.run(cmd, cwd=str(project_root))
    if result.returncode != 0:
        print("PyInstaller failed!", file=sys.stderr)
        sys.exit(1)

    pyinstaller_output = dist_dir / "box-agent-acp"
    if not pyinstaller_output.exists():
        print(f"Expected output not found: {pyinstaller_output}", file=sys.stderr)
        sys.exit(1)

    # ── Step 2: Assemble runtime directory ───────────────────
    runtime_name = f"box-agent-runtime-v{version}-{plat}-{arch}"
    runtime_dir = output_dir / "box-agent-runtime"
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True)

    # bin/ directory
    bin_dir = runtime_dir / "bin"
    bin_dir.mkdir()

    # Copy PyInstaller output into bin/
    # PyInstaller --onedir produces a directory with the executable + libs
    for item in pyinstaller_output.iterdir():
        dest = bin_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    # Ensure entry is executable
    entry_bin = bin_dir / "box-agent-acp"
    if plat == "win32":
        entry_bin = bin_dir / "box-agent-acp.exe"
    if entry_bin.exists():
        entry_bin.chmod(0o755)

    # VERSION file
    (runtime_dir / "VERSION").write_text(version + "\n", encoding="utf-8")

    # manifest.json
    entry_path = "bin/box-agent-acp"
    if plat == "win32":
        entry_path = "bin/box-agent-acp.exe"

    manifest = {
        "name": "box-agent",
        "version": version,
        "platform": plat,
        "arch": arch,
        "entry": entry_path,
        "mode": "standalone",
    }
    (runtime_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\nRuntime directory assembled: {runtime_dir}")
    print(f"  manifest.json: {json.dumps(manifest)}")

    # ── Step 3: Create archive ───────────────────────────────
    archive_name = f"{runtime_name}.tar.gz"
    archive_path = output_dir / archive_name

    print(f"\nCreating archive: {archive_path}")
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(runtime_dir, arcname="box-agent-runtime")

    # Archive size
    size_mb = archive_path.stat().st_size / (1024 * 1024)
    print(f"Archive created: {archive_path} ({size_mb:.1f} MB)")

    # ── Cleanup ──────────────────────────────────────────────
    shutil.rmtree(dist_dir, ignore_errors=True)
    shutil.rmtree(output_dir / "pyinstaller_build", ignore_errors=True)
    for f in output_dir.glob("*.spec"):
        f.unlink(missing_ok=True)

    return archive_path


# ── CLI ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build box-agent standalone runtime")
    parser.add_argument("--version", default=None, help="Version string (default: read from package)")
    parser.add_argument("--output", default="dist/runtime", help="Output directory")
    args = parser.parse_args()

    version = args.version
    if not version:
        # Read from package
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from box_agent import __version__
        version = __version__

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    archive = build_runtime(version, output_dir)
    print(f"\nDone! Artifact: {archive}")


if __name__ == "__main__":
    main()
