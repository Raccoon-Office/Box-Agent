#!/usr/bin/env python3
"""Check dependencies used by the pptx skill.

The checks are diagnostic. Missing npm/pip packages may be installed only into
the managed Office Raccoon runtime; system packages and global dependencies
still require explicit user approval.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


PYTHON_MODULES = ["pptx", "PIL", "lxml"]
ADVANCED_PYTHON_MODULES = ["defusedxml"]
BINARIES = ["pdftoppm", "node", "npm"]
FALLBACK_BINARIES_BY_SYSTEM = {
    "Darwin": ["qlmanage"],
    "Linux": [],
    "Windows": ["powershell"],
}
NODE_PACKAGES = ["pptxgenjs", "pdfjs-dist", "@napi-rs/canvas"]
OPTIONAL_NODE_PACKAGES: dict[str, str] = {
    "pngjs": "source-vs-render image comparison QA only",
    "playwright": "browser host for CLI HTML editable PPTX export",
}
LIBREOFFICE_DOWNLOAD_URL = "https://www.libreoffice.org/download/download-libreoffice/"
def node_command() -> str:
    return os.environ.get("BOX_AGENT_NODE") or shutil.which("node") or "node"


def managed_node_prefix() -> Path:
    if os.environ.get("BOX_AGENT_NODE_PREFIX"):
        return Path(os.environ["BOX_AGENT_NODE_PREFIX"])
    if os.environ.get("BOX_AGENT_RUNTIME_PREFIX"):
        return Path(os.environ["BOX_AGENT_RUNTIME_PREFIX"])
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "office-raccoon"
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "office-raccoon"
    return Path.home() / ".config" / "office-raccoon"


def managed_node_modules() -> Path:
    return managed_node_prefix() / "node_modules"


def playwright_install_cmd() -> str:
    return "reinstall or repair Office Raccoon's managed runtime"


def playwright_chromium_cmd() -> str:
    return "Office Raccoon Settings -> Plugins -> Web automation (Playwright) -> Download Chromium and enable"


def playwright_browsers_path() -> Path:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return Path(configured)
    return Path.home() / ".box-agent" / "browsers"


def usable_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def configured_playwright_executable() -> Path | None:
    for name in [
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
        "BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
        "PLAYWRIGHT_EXECUTABLE_PATH",
    ]:
        value = os.environ.get(name)
        if value and usable_file(Path(value)):
            return Path(value)
    return None


def browser_revision(path: Path) -> int:
    try:
        return int(path.name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def executable_candidates(browser_dir: Path, headless: bool) -> list[Path]:
    system = platform.system()
    if headless:
        if system == "Darwin":
            return [
                browser_dir / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell",
                browser_dir / "chrome-headless-shell-mac" / "chrome-headless-shell",
            ]
        if system == "Windows":
            return [
                browser_dir / "chrome-headless-shell-win64" / "chrome-headless-shell.exe",
                browser_dir / "chrome-headless-shell-win" / "chrome-headless-shell.exe",
            ]
        return [
            browser_dir / "chrome-headless-shell-linux64" / "chrome-headless-shell",
            browser_dir / "chrome-headless-shell-linux" / "chrome-headless-shell",
        ]

    if system == "Darwin":
        return [
            browser_dir
            / "chrome-mac-arm64"
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing",
            browser_dir
            / "chrome-mac"
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing",
            browser_dir / "chrome-mac-arm64" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
            browser_dir / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
        ]
    if system == "Windows":
        return [
            browser_dir / "chrome-win64" / "chrome.exe",
            browser_dir / "chrome-win" / "chrome.exe",
        ]
    return [
        browser_dir / "chrome-linux64" / "chrome",
        browser_dir / "chrome-linux" / "chrome",
    ]


def managed_playwright_executable() -> Path | None:
    root = playwright_browsers_path()
    if not root.exists():
        return None
    for prefix, headless in [
        ("chromium_headless_shell-", True),
        ("chromium-", False),
    ]:
        dirs = sorted(
            [entry for entry in root.iterdir() if entry.is_dir() and entry.name.startswith(prefix)],
            key=browser_revision,
            reverse=True,
        )
        for browser_dir in dirs:
            for candidate in executable_candidates(browser_dir, headless):
                if usable_file(candidate):
                    return candidate
    return None


def playwright_registry_executable() -> tuple[Path | None, bool]:
    node_path = os.environ.get("NODE_PATH", "")
    managed = str(managed_node_modules())
    merged_node_path = managed if not node_path else os.pathsep.join([managed, node_path])
    result = subprocess.run(
        [
            node_command(),
            "-e",
            (
                "process.env.NODE_PATH = "
                + repr(merged_node_path)
                + "; require('module').Module._initPaths(); "
                "const {chromium}=require(process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH || 'playwright'); "
                "process.stdout.write(chromium.executablePath());"
            ),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, False
    expected = Path(result.stdout.strip())
    return (expected if usable_file(expected) else None), True


def find_binary(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        path = shutil.which(candidate)
        if path:
            return path
    return None


def render_runtime_candidates(binary_name: str) -> list[str]:
    root = os.environ.get("BOX_AGENT_RENDER_RUNTIME")
    if not root:
        return []

    binary = binary_name + (".exe" if platform.system() == "Windows" else "")
    base = Path(root)
    return [
        str(base / "bin" / binary),
        str(base / "poppler" / "bin" / binary),
        str(base / "poppler" / "Library" / "bin" / binary),
    ]


def soffice_candidates() -> list[str]:
    candidates = [
        os.environ.get("BOX_AGENT_SOFFICE", ""),
        *render_runtime_candidates("soffice"),
        "soffice",
        "libreoffice",
    ]
    system = platform.system()
    if system == "Darwin":
        candidates.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        root = os.environ.get("BOX_AGENT_RENDER_RUNTIME")
        if root:
            candidates.append(str(Path(root) / "LibreOffice.app" / "Contents" / "MacOS" / "soffice"))
    elif system == "Windows":
        for root in [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]:
            if root:
                candidates.append(str(Path(root) / "LibreOffice" / "program" / "soffice.exe"))
    return candidates


def pdftoppm_candidates() -> list[str]:
    return [
        os.environ.get("BOX_AGENT_PDFTOPPM", ""),
        *render_runtime_candidates("pdftoppm"),
        "pdftoppm",
        "pdftoppm.exe",
    ]


def has_node_package(package: str) -> bool:
    node_path = os.environ.get("NODE_PATH", "")
    managed = str(managed_node_modules())
    merged_node_path = managed if not node_path else os.pathsep.join([managed, node_path])
    result = subprocess.run(
        [
            node_command(),
            "-e",
            (
                "process.env.NODE_PATH = "
                + repr(merged_node_path)
                + "; require('module').Module._initPaths(); "
                + f"require.resolve('{package}')"
            ),
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def has_playwright_chromium() -> bool:
    if configured_playwright_executable():
        return True
    executable, registry_available = playwright_registry_executable()
    if registry_available:
        return executable is not None
    return managed_playwright_executable() is not None


def main() -> int:
    missing: list[str] = []

    print("Python modules:")
    for module in PYTHON_MODULES:
        ok = importlib.util.find_spec(module) is not None
        print(f"  {'ok  ' if ok else 'miss'} {module}")
        if not ok:
            missing.append(f"python module: {module}")

    print("\nAdvanced Python modules:")
    for module in ADVANCED_PYTHON_MODULES:
        ok = importlib.util.find_spec(module) is not None
        print(f"  {'ok  ' if ok else 'warn'} {module} (advanced OOXML helpers only)")

    print("\nRender binaries:")
    soffice_path = find_binary(soffice_candidates())
    print(f"  {'ok  ' if soffice_path else 'miss'} soffice/libreoffice{(' -> ' + soffice_path) if soffice_path else ''}")
    if not soffice_path:
        missing.append("binary: soffice or libreoffice")
        print(f"       install LibreOffice: {LIBREOFFICE_DOWNLOAD_URL}")

    pdftoppm_path = find_binary(pdftoppm_candidates())
    print(f"  {'ok  ' if pdftoppm_path else 'warn'} pdftoppm{(' -> ' + pdftoppm_path) if pdftoppm_path else ''}")

    for binary in ["node", "npm"]:
        path = shutil.which(binary)
        print(f"  {'ok  ' if path else 'miss'} {binary}{' -> ' + path if path else ''}")
        if not path:
            missing.append(f"binary: {binary}")

    print("\nFallback binaries:")
    for binary in FALLBACK_BINARIES_BY_SYSTEM.get(platform.system(), []):
        path = shutil.which(binary)
        print(f"  {'ok  ' if path else 'warn'} {binary}{' -> ' + path if path else ''}")

    print("\nNode packages:")
    if os.environ.get("BOX_AGENT_NODE") or shutil.which("node"):
        for package in NODE_PACKAGES:
            ok = has_node_package(package)
            print(f"  {'ok  ' if ok else 'miss'} {package}")
            if not ok:
                missing.append(f"node package: {package}")
        for package, description in OPTIONAL_NODE_PACKAGES.items():
            ok = has_node_package(package)
            print(f"  {'ok  ' if ok else 'warn'} {package} ({description})")
            if package == "pngjs" and not ok:
                print("       image comparison QA is blocked without pngjs; PPTX generation/export can continue")
        if has_node_package("playwright"):
            browser_ok = has_playwright_chromium()
            print(f"  {'ok  ' if browser_ok else 'warn'} playwright chromium browser")
            if not browser_ok:
                print("       CLI editable PPTX export is blocked until Chromium is installed; ask the user to choose HTML delivery or native PptxGenJS PPTX")
                print(f"       download Chromium: {playwright_chromium_cmd()}")
        else:
            print("       CLI editable PPTX export is blocked without a browser host; ask the user to choose HTML delivery or native PptxGenJS PPTX")
            print(f"       install Playwright: {playwright_install_cmd()}")
            print(f"       download Chromium: {playwright_chromium_cmd()}")
    else:
        print("  skip node package checks because node is missing")

    if missing:
        print("\nMissing dependencies for the full workflow:")
        for item in missing:
            print(f"  - {item}")
        if not pdftoppm_path:
            print("\nNote: pdftoppm is optional when Node pdf.js rendering packages are available.")
        return 1

    print("\nAll checked dependencies are available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
