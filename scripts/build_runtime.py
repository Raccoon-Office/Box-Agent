#!/usr/bin/env python3
"""Build a standalone box-agent-runtime artifact.

Usage:
    box-agent-build-runtime [--version 0.3.2] [--output dist/runtime]
    arch -x86_64 uv run box-agent-build-runtime --version 0.8.51 --arch x64

Produces:
    dist/runtime/box-agent-runtime-v{version}-{platform}-{arch}.tar.gz

Requires:
    pip install pyinstaller
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

# Make `python scripts/build_runtime.py` work from a source checkout even when
# the package has not been installed into the active environment.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from box_agent.tools.runtime import DEFAULT_NODE_VERSION, NodeRuntimeManager
from box_agent.tools.jupyter_tool import SANDBOX_DEFAULT_PACKAGES

# ── Win-only bundled tool versions ────────────────────────────
# bash + coreutils. PortableGit ships as a 7-Zip self-extracting .exe.
PORTABLE_GIT_VERSION = "2.46.0"
PORTABLE_GIT_TAG = "v2.46.0.windows.1"
PORTABLE_GIT_URL = (
    f"https://github.com/git-for-windows/git/releases/download/"
    f"{PORTABLE_GIT_TAG}/PortableGit-{PORTABLE_GIT_VERSION}-64-bit.7z.exe"
)
# Pinned SHA-256 of the upstream PortableGit self-extractor. Mismatch implies
# CDN tampering / MITM proxy / cache corruption; refuse to execute the .exe.
# Source: https://github.com/git-for-windows/git/releases/tag/v2.46.0.windows.1
PORTABLE_GIT_SHA256 = "dedae83f4d0851bcbf473c516701e2da6a5d7c574d694d5eceec46d1307132ea"

# Standalone CPython distribution with full stdlib + venv + ensurepip.
# install_only build is what we need.
PYTHON_STANDALONE_VERSION = "3.12.6"
PYTHON_STANDALONE_RELEASE = "20240909"
PYTHON_STANDALONE_URL = (
    f"https://github.com/indygreg/python-build-standalone/releases/download/"
    f"{PYTHON_STANDALONE_RELEASE}/cpython-{PYTHON_STANDALONE_VERSION}+"
    f"{PYTHON_STANDALONE_RELEASE}-x86_64-pc-windows-msvc-install_only.tar.gz"
)
# Pinned SHA-256 of the upstream python-build-standalone archive. Source:
# https://github.com/astral-sh/python-build-standalone/releases/download/
#   20240909/cpython-3.12.6+20240909-x86_64-pc-windows-msvc-install_only.tar.gz.sha256
PYTHON_STANDALONE_SHA256 = (
    "6280ce84c87ebaca2c4b42040bad48e7efbfd1b3f323579378ecf043e9fb023d"
)
BUILD_TOOLS_CACHE = Path.home() / ".cache" / "box-agent-build-tools"

# ── Platform detection ───────────────────────────────────────


ARCH_ALIASES = {
    "x86_64": "x64",
    "amd64": "x64",
    "x64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

SUPPORTED_TARGETS = {
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "win32-x64",
}

EXTERNAL_PYTHON_SANDBOX_HIDDEN_IMPORTS = {
    "ipykernel",
    "ipykernel.inprocess",
    "ipykernel.inprocess.manager",
    "ipykernel_launcher",
    "debugpy",
    "debugpy._vendored",
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
    "docx",
    "pypdf",
    "pdfplumber",
    "reportlab",
    "reportlab.pdfgen",
    "reportlab.lib",
    "pptx",
    "sklearn.preprocessing",
    "bs4",
    "lxml",
    "lxml.etree",
    "lxml.html",
    "chardet",
    "pip",
    "pip._internal",
    "pip._internal.cli",
    "pip._internal.cli.main",
}

EXTERNAL_PYTHON_SANDBOX_COLLECT_ARGS = {
    ("--collect-all", "ipykernel"),
    ("--collect-all", "debugpy"),
    ("--collect-all", "matplotlib"),
    ("--collect-submodules", "pandas"),
    ("--collect-submodules", "seaborn"),
    ("--collect-submodules", "openpyxl"),
    ("--collect-submodules", "sklearn"),
    ("--collect-submodules", "pip"),
}

EXTERNAL_PYTHON_SANDBOX_EXCLUDED_MODULES = {
    "ipykernel",
    "ipykernel.inprocess",
    "ipykernel_launcher",
    "debugpy",
    "pandas",
    "numpy",
    "matplotlib",
    "seaborn",
    "openpyxl",
    "xlrd",
    "sklearn",
    "docx",
    "pypdf",
    "pdfplumber",
    "reportlab",
    "pptx",
    "bs4",
    "lxml",
    "chardet",
    "pip",
}


def env_flag(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def pyinstaller_hidden_imports(*, external_python_sandbox: bool = False) -> list[str]:
    """Return PyInstaller hidden imports for the ACP runtime build."""
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
        "box_agent.mcp_servers",
        "box_agent.mcp_servers.web_extract",
        "box_agent.mcp_servers.web_extract_server",
        "box_agent.retry",
        "box_agent.schema",
        "box_agent.tools",
        "box_agent.tools.bash_tool",
        "box_agent.tools.file_tools",
        "box_agent.tools.jupyter_tool",
        "box_agent.tools.mcp_loader",
        "box_agent.tools.skill_tool",
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
        # Document processing (runtime extras)
        "docx",  # python-docx imports as 'docx'
        "pypdf",
        "pdfplumber",
        "reportlab",
        "reportlab.pdfgen",
        "reportlab.lib",
        "pptx",  # python-pptx imports as 'pptx'
        "sklearn.preprocessing",
        # HTML / XML / image / config / encoding (sandbox defaults)
        "bs4",  # beautifulsoup4 imports as 'bs4'
        "lxml",
        "lxml.etree",
        "lxml.html",
        "PIL",  # pillow imports as 'PIL'; required by the default image watermark
        "PIL.Image",  # (box_agent/tools/watermark.py) which runs in the ACP main
        # process — must stay bundled even in external-python-sandbox builds.
        "dateutil",  # python-dateutil imports as 'dateutil'
        "dateutil.parser",
        "chardet",
        # pip (used as library in frozen mode for runtime package installs)
        "pip",
        "pip._internal",
        "pip._internal.cli",
        "pip._internal.cli.main",
    ]
    if external_python_sandbox:
        return [
            imp for imp in hidden_imports if imp not in EXTERNAL_PYTHON_SANDBOX_HIDDEN_IMPORTS
        ]
    return hidden_imports


def pyinstaller_collect_args(*, external_python_sandbox: bool = False) -> list[str]:
    """Return flattened PyInstaller collect args for the ACP runtime build."""
    collect_groups = [
        ("--collect-all", "tiktoken"),
        ("--collect-all", "tiktoken_ext"),
        ("--collect-all", "jupyter_client"),
        ("--collect-all", "ipykernel"),
        ("--collect-all", "jupyter_core"),
        ("--collect-all", "debugpy"),
        ("--collect-all", "matplotlib"),
        ("--collect-submodules", "pandas"),
        ("--collect-submodules", "seaborn"),
        ("--collect-submodules", "openpyxl"),
        ("--collect-submodules", "sklearn"),
        ("--collect-submodules", "pip"),
    ]
    if external_python_sandbox:
        collect_groups = [
            group for group in collect_groups if group not in EXTERNAL_PYTHON_SANDBOX_COLLECT_ARGS
        ]
    return [item for group in collect_groups for item in group]


def pyinstaller_exclude_args(*, external_python_sandbox: bool = False) -> list[str]:
    """Return PyInstaller module exclusions for external-python sandbox builds."""
    if not external_python_sandbox:
        return []
    args: list[str] = []
    for module in sorted(EXTERNAL_PYTHON_SANDBOX_EXCLUDED_MODULES):
        args.extend(["--exclude-module", module])
    return args


def detect_platform() -> tuple[str, str]:
    """Return the running Python process platform in Electron naming convention."""
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

    arch = ARCH_ALIASES.get(machine, machine)

    return plat, arch


def parse_target(value: str | None) -> tuple[str, str]:
    """Parse and validate a target platform id such as ``darwin-x64``."""
    if not value:
        return detect_platform()

    target = value.strip().lower()
    if target in ARCH_ALIASES:
        plat, _arch = detect_platform()
        target = f"{plat}-{ARCH_ALIASES[target]}"

    if target not in SUPPORTED_TARGETS:
        supported = ", ".join(sorted(SUPPORTED_TARGETS))
        raise ValueError(f"Unsupported target: {value}. Supported targets: {supported}")
    plat, arch = target.rsplit("-", 1)
    return plat, arch


def require_supported_build_process(target_plat: str, target_arch: str) -> None:
    """Avoid producing metadata for a platform this process cannot build."""
    current_plat, current_arch = detect_platform()
    if target_plat == current_plat and (
        target_plat == "darwin" or target_arch == current_arch
    ):
        return

    target = f"{target_plat}-{target_arch}"
    current = f"{current_plat}-{current_arch}"
    hint = "Run the build on the matching target platform."
    raise RuntimeError(
        f"Refusing to build {target} from current process {current}. {hint}"
    )


def verify_entry_binary_arch(entry_bin: Path, *, plat: str, arch: str) -> None:
    """Verify the packaged launcher has the requested CPU architecture."""
    if plat not in {"darwin", "linux"}:
        return

    expected_tokens = {
        ("darwin", "x64"): ("x86_64",),
        ("darwin", "arm64"): ("arm64",),
        ("linux", "x64"): ("x86-64", "x86_64"),
        ("linux", "arm64"): ("aarch64", "arm64"),
    }.get((plat, arch))
    if expected_tokens is None:
        raise RuntimeError(f"Unsupported binary architecture check: {plat}-{arch}")
    try:
        result = subprocess.run(
            ["file", str(entry_bin)],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        if plat == "linux":
            raise RuntimeError("`file` command is required for Linux runtime builds")
        print(
            "Warning: `file` command not found; skipping binary arch verification",
            file=sys.stderr,
        )
        return

    output = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode != 0:
        raise RuntimeError(f"Failed to inspect packaged binary architecture: {output.strip()}")
    if not any(token in output for token in expected_tokens):
        raise RuntimeError(
            f"Packaged binary architecture mismatch for {entry_bin}: "
            f"expected one of {expected_tokens}, got {output.strip()}"
        )


def pyinstaller_target_arch_args(*, plat: str, arch: str) -> list[str]:
    """Return PyInstaller architecture args for macOS builds."""
    if plat != "darwin":
        return []
    target_arch = "x86_64" if arch == "x64" else arch
    return ["--target-arch", target_arch]


def bundled_stable_runtime_components(
    *,
    plat: str,
    arch: str,
    external_python_sandbox: bool = False,
) -> tuple[str, ...]:
    """Return stable runtimes bundled into the artifact for this build."""
    if external_python_sandbox:
        return ()
    if plat == "win32" and arch == "x64":
        return ("portable_git", "python", "node")
    return ()


def build_runtime_manifest(
    *,
    version: str,
    plat: str,
    arch: str,
    entry_path: str,
    external_python_sandbox: bool,
    bundled_components: tuple[str, ...],
) -> dict[str, object]:
    """Return the host-facing manifest for a packaged runtime."""
    return {
        "name": "box-agent",
        "version": version,
        "platform": plat,
        "arch": arch,
        "entry": entry_path,
        "mode": "standalone",
        "external_python_sandbox": external_python_sandbox,
        "bundled_stable_runtimes": list(bundled_components),
        "mcp_servers": {
            "box-agent-web-extract": {
                "entry": entry_path,
                "args": ["--web-extract-mcp"],
                "transport": "stdio",
            }
        },
    }


# ── Build ────────────────────────────────────────────────────


def resolve_officev3_dir(value: str | None = None) -> Path:
    """Resolve an officev3 checkout containing the runtime installer."""
    configured = value or os.environ.get("BOX_AGENT_OFFICEV3_DIR")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidates = [candidate.resolve()]
    else:
        candidates = [
            (PROJECT_ROOT.parents[1] / "frontend" / "officev3").resolve(),
            (PROJECT_ROOT.parent / "officev3").resolve(),
        ]

    for candidate in candidates:
        installer = candidate / "scripts" / "install-box-agent-runtime.js"
        if installer.is_file():
            return candidate

    rendered = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(
        "Unable to locate officev3 runtime installer. "
        f"Checked: {rendered}. Pass --install-officev3 /path/to/officev3 "
        "or set BOX_AGENT_OFFICEV3_DIR."
    )


def install_runtime_into_officev3(
    archive_path: Path,
    officev3_dir: Path,
    *,
    expected_version: str,
) -> Path:
    """Install a built runtime archive into officev3 build-resources."""
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise RuntimeError(f"Runtime archive not found: {archive_path}")

    installer = officev3_dir / "scripts" / "install-box-agent-runtime.js"
    if not installer.is_file():
        raise RuntimeError(f"officev3 runtime installer not found: {installer}")

    node_bin = shutil.which("node")
    if not node_bin:
        raise RuntimeError("Node.js is required to install the runtime into officev3")

    print(f"\nInstalling runtime into officev3: {officev3_dir}")
    result = subprocess.run(
        [node_bin, str(installer), str(archive_path)],
        cwd=str(officev3_dir),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"officev3 runtime installer failed with exit code {result.returncode}"
        )

    installed_dir = officev3_dir / "build-resources" / "box-agent-runtime"
    manifest_path = installed_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Installed officev3 runtime manifest is invalid: {manifest_path}: {exc}"
        ) from exc

    installed_version = str(manifest.get("version") or "")
    if installed_version != expected_version:
        raise RuntimeError(
            "Installed officev3 runtime version mismatch: "
            f"expected {expected_version}, got {installed_version or 'unknown'}"
        )

    return installed_dir


def build_runtime(
    version: str,
    output_dir: Path,
    *,
    target: str | None = None,
    external_python_sandbox: bool = False,
) -> Path:
    """Build the runtime artifact and return the archive path."""
    plat, arch = parse_target(target)
    require_supported_build_process(plat, arch)
    if plat in {"darwin", "linux"}:
        external_python_sandbox = True
    project_root = PROJECT_ROOT

    print(f"Building box-agent-runtime v{version} for {plat}-{arch}")
    print(f"Project root: {project_root}")

    # ── Step 0: Install runtime extras into current env ─────
    if external_python_sandbox:
        print("\nSkipping bundled Python sandbox extras (external python sandbox mode)...")
    else:
        print("\nInstalling runtime extras (data science packages)...")
    # Try uv first (used in dev), fall back to pip
    extras_cmd_pip = [sys.executable, "-m", "pip", "install", "--quiet", f"{project_root}[runtime]"]
    uv_bin = shutil.which("uv")
    if not external_python_sandbox:
        if uv_bin:
            extras_cmd_uv = [uv_bin, "pip", "install", "--quiet", f"{project_root}[runtime]"]
            result = subprocess.run(extras_cmd_uv, cwd=str(project_root), capture_output=True)
        else:
            result = subprocess.CompletedProcess(args=["uv"], returncode=1)
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

    hidden_imports = pyinstaller_hidden_imports(
        external_python_sandbox=external_python_sandbox
    )
    hidden_args = []
    for imp in hidden_imports:
        hidden_args.extend(["--hidden-import", imp])

    collect_args = pyinstaller_collect_args(
        external_python_sandbox=external_python_sandbox
    )
    exclude_args = pyinstaller_exclude_args(
        external_python_sandbox=external_python_sandbox
    )

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
        *collect_args,
        *exclude_args,
        *pyinstaller_target_arch_args(plat=plat, arch=arch),
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
        verify_entry_binary_arch(entry_bin, plat=plat, arch=arch)

    # VERSION file
    (runtime_dir / "VERSION").write_text(version + "\n", encoding="utf-8")

    # manifest.json
    entry_path = "bin/box-agent-acp"
    if plat == "win32":
        entry_path = "bin/box-agent-acp.exe"

    bundled_components = bundled_stable_runtime_components(
        plat=plat,
        arch=arch,
        external_python_sandbox=external_python_sandbox,
    )
    manifest = build_runtime_manifest(
        version=version,
        plat=plat,
        arch=arch,
        entry_path=entry_path,
        external_python_sandbox=external_python_sandbox,
        bundled_components=bundled_components,
    )
    (runtime_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    if bundled_components and plat == "darwin" and arch in {"arm64", "x64"}:
        _install_bundled_node_runtime(runtime_dir, plat=plat, arch=arch)
    elif bundled_components and plat == "win32" and arch == "x64":
        _install_bundled_win_runtimes(runtime_dir)
    elif external_python_sandbox:
        print("\nSkipping bundled stable runtimes (host-managed Node/Python/Git mode).")
    else:
        print(f"\nSkipping bundled Node runtime for unsupported platform: {plat}-{arch}")

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
    if plat == "linux":
        checksum_path = archive_path.with_name(f"{archive_path.name}.sha256")
        checksum_path.write_text(
            f"{_sha256_file(archive_path)}  {archive_path.name}\n",
            encoding="utf-8",
        )
        print(f"Checksum created: {checksum_path}")

    # ── Cleanup ──────────────────────────────────────────────
    shutil.rmtree(dist_dir, ignore_errors=True)
    shutil.rmtree(output_dir / "pyinstaller_build", ignore_errors=True)
    for f in output_dir.glob("*.spec"):
        f.unlink(missing_ok=True)

    return archive_path


def _install_bundled_node_runtime(runtime_dir: Path, *, plat: str, arch: str) -> None:
    """Install a relocatable macOS Node runtime into the runtime artifact."""
    node_root = runtime_dir / "runtimes" / "node"
    platform_id = f"{plat}-{arch}"
    print(f"\nInstalling bundled Node.js runtime {DEFAULT_NODE_VERSION} for {platform_id}...")
    manager = NodeRuntimeManager(root=node_root)
    manager.install_macos(version=DEFAULT_NODE_VERSION, platform_id=platform_id)
    shutil.rmtree(node_root / "downloads", ignore_errors=True)
    _relativize_node_manifest(node_root)
    print(f"Bundled Node runtime: {node_root}")


def _install_bundled_win_runtimes(runtime_dir: Path) -> None:
    """Bundle PortableGit (bash/coreutils) + standalone Python + Node into the Win runtime.

    box-agent on Windows can't rely on system bash/python/node — Mac users have
    them by default, Win users almost never. This drops self-contained copies
    into the runtime tar so the artifact is install-and-go.
    """
    print("\n[win] Installing bundled PortableGit + Python + Node runtimes...")
    BUILD_TOOLS_CACHE.mkdir(parents=True, exist_ok=True)

    _install_portable_git_win(runtime_dir)
    _install_portable_python_win(runtime_dir)

    node_root = runtime_dir / "runtimes" / "node"
    print(f"\n[win] Installing bundled Node.js runtime {DEFAULT_NODE_VERSION} for win-x64...")
    manager = NodeRuntimeManager(root=node_root)
    manager.install_win(version=DEFAULT_NODE_VERSION, platform_id="win-x64")
    shutil.rmtree(node_root / "downloads", ignore_errors=True)
    _relativize_node_manifest(node_root)
    print(f"[win] Bundled Node runtime: {node_root}")


def _install_portable_git_win(runtime_dir: Path) -> None:
    """Download and extract PortableGit 7-Zip SFX into ``runtime/PortableGit/``."""
    target = runtime_dir / "runtime" / "PortableGit"
    bash_exe = target / "usr" / "bin" / "bash.exe"
    if bash_exe.is_file():
        print(f"[win] PortableGit already present: {target}")
        return

    override = os.environ.get("BOX_AGENT_BUILD_PORTABLE_GIT")
    if override:
        archive = Path(override)
        if not archive.is_file():
            raise RuntimeError(f"BOX_AGENT_BUILD_PORTABLE_GIT not found: {archive}")
        print(f"[win] Using PortableGit override: {archive}")
    else:
        archive = BUILD_TOOLS_CACHE / f"PortableGit-{PORTABLE_GIT_VERSION}-64-bit.7z.exe"
        if not archive.is_file():
            print(f"[win] Downloading PortableGit -> {archive}")
            _download_to(PORTABLE_GIT_URL, archive)

    # PortableGit is a self-extracting .exe — about to be executed below.
    # Verify SHA-256 before invoking the binary, otherwise a tampered
    # download / poisoned cache entry would execute arbitrary code on the
    # build host.
    _verify_sha256(archive, PORTABLE_GIT_SHA256, label="PortableGit")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    # PortableGit-*.7z.exe is a 7-Zip self-extracting archive. The supported
    # extraction flags are: -y (assume yes), -o<dir> (output directory).
    print(f"[win] Extracting PortableGit -> {target}")
    result = subprocess.run(
        [str(archive), "-y", f"-o{target}"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"PortableGit extraction failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:500]}"
        )

    if not bash_exe.is_file():
        raise RuntimeError(f"PortableGit extracted but bash.exe missing: {bash_exe}")
    print(f"[win] PortableGit ready: {bash_exe}")


def _install_portable_python_win(runtime_dir: Path) -> None:
    """Download and extract python-build-standalone install_only into ``runtime/python/``."""
    target = runtime_dir / "runtime" / "python"
    python_exe = target / "python.exe"
    if python_exe.is_file():
        print(f"[win] Portable Python already present: {target}")
        return

    override = os.environ.get("BOX_AGENT_BUILD_PORTABLE_PYTHON")
    if override:
        archive = Path(override)
        if not archive.is_file():
            raise RuntimeError(f"BOX_AGENT_BUILD_PORTABLE_PYTHON not found: {archive}")
        print(f"[win] Using portable Python override: {archive}")
    else:
        archive = (
            BUILD_TOOLS_CACHE
            / f"cpython-{PYTHON_STANDALONE_VERSION}-{PYTHON_STANDALONE_RELEASE}-win-x64-install_only.tar.gz"
        )
        if not archive.is_file():
            print(f"[win] Downloading python-build-standalone -> {archive}")
            _download_to(PYTHON_STANDALONE_URL, archive)

    # The extracted interpreter is invoked later to pre-install sandbox
    # packages, so a tampered archive becomes code execution on the build
    # host. Verify the pinned SHA-256 before touching the contents.
    _verify_sha256(archive, PYTHON_STANDALONE_SHA256, label="python-build-standalone")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)

    # install_only tar.gz layout: ``python/python.exe + python/Lib/...``.
    print(f"[win] Extracting portable Python -> {target}")
    extract_root = target.parent / ".python-extract"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(extract_root)
        inner = extract_root / "python"
        if not inner.is_dir():
            raise RuntimeError(f"Portable Python archive missing 'python/' dir: {archive}")
        # Win: ``Path.rename`` (MoveFileEx) trips ``WinError 5`` when
        # Defender/Search Indexer briefly holds the freshly extracted
        # ``python.exe``. ``shutil.move`` falls back to copy+delete and is
        # more tolerant; retry once after a short pause as final safety net.
        try:
            shutil.move(str(inner), str(target))
        except PermissionError:
            import time
            time.sleep(2)
            shutil.move(str(inner), str(target))
    finally:
        if extract_root.exists():
            shutil.rmtree(extract_root, ignore_errors=True)

    if not python_exe.is_file():
        raise RuntimeError(f"Portable Python extracted but python.exe missing: {python_exe}")
    print(f"[win] Portable Python ready: {python_exe}")

    _install_sandbox_packages_win(python_exe)


# Pre-installed into the bundled Python's site-packages so that the frozen
# Win build's ``execute_code`` (InProcessKernelSession) can ``import pandas``
# etc. without spawning pip at first run. Mirrors
# Win build's ``execute_code`` (InProcessKernelSession) can ``import pandas``
# only if the underlying portable Python already has these wheels. Mirror
# ``box_agent.tools.jupyter_tool.SANDBOX_DEFAULT_PACKAGES`` + ``ipykernel``.
_SANDBOX_BUNDLED_PACKAGES = (*SANDBOX_DEFAULT_PACKAGES, "ipykernel")


def _install_sandbox_packages_win(python_exe: Path) -> None:
    """Pre-install sandbox data-science packages into the bundled portable Python."""
    print(f"\n[win] Pre-installing sandbox packages into bundled Python: {', '.join(_SANDBOX_BUNDLED_PACKAGES)}")
    print(f"[win] (this can take several minutes the first time)")
    # Strip parent-process venv markers so the bundled python doesn't try to
    # load pip/site-packages from the build venv (.venv-build / uv cpython)
    # — those copies are ABI-incompatible with python-build-standalone and
    # trip ``AttributeError: class must define a '_type_' attribute`` in
    # ctypes when imported.
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "__PYVENV_LAUNCHER__",
            "PYTHONNOUSERSITE",
            "PYTHONUSERBASE",
        }
    }
    clean_env["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run(
        [
            str(python_exe), "-I", "-m", "pip", "install",
            "--no-warn-script-location",
            "--disable-pip-version-check",
            *_SANDBOX_BUNDLED_PACKAGES,
        ],
        capture_output=True,
        env=clean_env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Pre-install of sandbox packages failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:1000]}"
        )
    print("[win] Sandbox packages ready in bundled Python.")


def _download_to(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with urllib.request.urlopen(url, timeout=300) as response, tmp.open("wb") as f:
            shutil.copyfileobj(response, f)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sha256(archive: Path, expected: str, *, label: str) -> None:
    """Abort the build if ``archive`` does not match the pinned SHA-256.

    Guards against CDN tampering / MITM proxies / corrupted cache entries.
    Matches the integrity check already done on the Node download path.
    """
    actual = _sha256_file(archive)
    if actual.lower() != expected.lower():
        raise RuntimeError(
            f"{label} SHA-256 mismatch for {archive}: "
            f"expected {expected}, got {actual}"
        )


def _relativize_node_manifest(node_root: Path) -> None:
    """Rewrite Node manifest paths relative to node_root for relocatable archives."""
    manifest_path = node_root / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    active = data.get("active")
    if not isinstance(active, dict):
        raise RuntimeError(f"Invalid Node manifest: {manifest_path}")
    for key in ("node", "npm", "npx", "node_modules"):
        value = active.get(key)
        if not isinstance(value, str):
            continue
        path = Path(value)
        if not path.is_absolute():
            continue
        active[key] = str(path.resolve().relative_to(node_root.resolve()))
    tmp_path = manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, manifest_path)


# ── CLI ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build box-agent standalone runtime")
    parser.add_argument(
        "--version",
        default=os.environ.get("BOX_AGENT_RUNTIME_VERSION"),
        help="Version string (default: BOX_AGENT_RUNTIME_VERSION or package version)",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("BOX_AGENT_RUNTIME_OUTPUT", "dist/runtime"),
        help="Output directory (default: BOX_AGENT_RUNTIME_OUTPUT or dist/runtime)",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("BOX_AGENT_RUNTIME_TARGET"),
        help="Target platform-arch, e.g. darwin-arm64 or darwin-x64. Arch-only values like x64 are allowed.",
    )
    parser.add_argument(
        "--arch",
        choices=sorted(set(ARCH_ALIASES.values())),
        default=None,
        help="Shortcut for --target <current-platform>-<arch>, e.g. --arch x64",
    )
    parser.add_argument(
        "--external-python-sandbox",
        action="store_true",
        default=env_flag("BOX_AGENT_EXTERNAL_PYTHON_SANDBOX"),
        help=(
            "Build ACP without bundled data-science/ipykernel/pip sandbox packages "
            "or stable Node/Python/Git runtimes. Requires the host to provide "
            "BOX_AGENT_SANDBOX_PYTHON and any stable tool runtimes it needs."
        ),
    )
    parser.add_argument(
        "--install-officev3",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help=(
            "After building, install the archive into officev3. Omit DIR to use "
            "BOX_AGENT_OFFICEV3_DIR or auto-detect the usual checkout layout."
        ),
    )
    args = parser.parse_args()

    if args.target and args.arch:
        print("Error: use either --target or --arch, not both", file=sys.stderr)
        sys.exit(1)

    version = args.version
    if not version:
        # Read from package
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from box_agent import __version__
        version = __version__

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    target = args.target
    if args.arch:
        plat, _arch = detect_platform()
        target = f"{plat}-{args.arch}"

    try:
        archive = build_runtime(
            version,
            output_dir,
            target=target,
            external_python_sandbox=args.external_python_sandbox,
        )
        installed_dir = None
        if args.install_officev3 is not None:
            officev3_dir = resolve_officev3_dir(args.install_officev3 or None)
            installed_dir = install_runtime_into_officev3(
                archive,
                officev3_dir,
                expected_version=version,
            )
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"\nDone! Artifact: {archive}")
    if installed_dir is not None:
        print(f"Done! Installed officev3 runtime: {installed_dir}")


if __name__ == "__main__":
    main()
