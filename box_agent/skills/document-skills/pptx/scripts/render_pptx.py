#!/usr/bin/env python3
"""Render a PPTX for visual QA.

Preferred path on macOS, Linux, and Windows: LibreOffice converts PPTX to PDF,
then Poppler renders each slide as an image. If Poppler is unavailable, the
script falls back to a Node pdf.js renderer and may install its npm packages
inside the managed Office Raccoon Node environment. On macOS, if no PDF image
renderer is available, Quick Look can produce a lightweight preview thumbnail.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

NODE_RENDER_PACKAGES = ["pdfjs-dist", "@napi-rs/canvas"]
LIBREOFFICE_DOWNLOAD_URL = "https://www.libreoffice.org/download/download-libreoffice/"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def run_image_renderer(
    cmd: list[str], out_dir: Path, pattern: str,
) -> subprocess.CompletedProcess[str]:
    """Keep renderer outputs for QA without publishing each page to the host.

    External renderers can leave partial output on failure. Mark changed files
    in either case, while leaving unrelated/pre-existing images untouched.
    """
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in out_dir.glob(pattern)}
    try:
        return run(cmd)
    finally:
        for image in out_dir.glob(pattern):
            if not image.is_file():
                continue
            stat = image.stat()
            if before.get(image) == (stat.st_size, stat.st_mtime_ns):
                continue
            image.with_name(f".{image.name}.artifact.json").write_text(
                '{"type":"intermediate_asset"}\n', encoding="utf-8"
            )


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


def node_command() -> str | None:
    return os.environ.get("BOX_AGENT_NODE") or shutil.which("node")


def npm_command() -> str | None:
    return os.environ.get("BOX_AGENT_NPM") or shutil.which("npm")


def office_raccoon_prefix() -> Path:
    explicit = os.environ.get("BOX_AGENT_NODE_PREFIX") or os.environ.get("BOX_AGENT_RUNTIME_PREFIX")
    if explicit:
        return Path(explicit).expanduser()

    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "office-raccoon"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "office-raccoon"
        return home / "AppData" / "Roaming" / "office-raccoon"
    return home / ".local" / "share" / "office-raccoon"


def node_env(prefix: Path) -> dict[str, str]:
    env = os.environ.copy()
    node_modules = str(prefix / "node_modules")
    current = env.get("NODE_PATH")
    env["NODE_PATH"] = node_modules if not current else node_modules + os.pathsep + current
    return env


def node_package_available(package: str, prefix: Path) -> bool:
    node = node_command()
    if not node:
        return False
    result = subprocess.run(
        [node, "-e", f"require.resolve('{package}')"],
        capture_output=True,
        text=True,
        env=node_env(prefix),
    )
    return result.returncode == 0


def ensure_node_render_packages(prefix: Path) -> bool:
    node = node_command()
    npm = npm_command()
    if not node or not npm:
        return False

    missing = [package for package in NODE_RENDER_PACKAGES if not node_package_available(package, prefix)]
    if not missing:
        return True

    prefix.mkdir(parents=True, exist_ok=True)
    print(f"Installing Node PDF renderer packages into managed Office Raccoon prefix: {prefix}")
    install = run([npm, "install", "--prefix", str(prefix), *missing])
    if install.returncode != 0:
        print(install.stdout, end="")
        print(install.stderr, end="", file=sys.stderr)
        return False

    return all(node_package_available(package, prefix) for package in NODE_RENDER_PACKAGES)


def render_pdf_with_node(pdf_path: Path, out_dir: Path, image_format: str, dpi: int) -> bool:
    if image_format != "png":
        print("Node pdf.js fallback only supports PNG output.", file=sys.stderr)
        return False

    node = node_command()
    if not node:
        return False

    prefix = office_raccoon_prefix()
    if not ensure_node_render_packages(prefix):
        return False

    script = Path(__file__).with_name("pdfjs_render.js")
    result = subprocess.run(
        [
            node,
            str(script),
            str(pdf_path),
            "--out",
            str(out_dir),
            "--format",
            image_format,
            "--dpi",
            str(dpi),
        ],
        capture_output=True,
        text=True,
        env=node_env(prefix),
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Render PPTX slides for visual QA.")
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--out", type=Path, default=Path("rendered"))
    parser.add_argument("--out_dir", dest="out", type=Path)
    parser.add_argument("--format", choices=["png", "jpeg"], default="png")
    parser.add_argument("--dpi", type=int, default=160)
    args = parser.parse_args()

    if not args.pptx.is_file() or args.pptx.suffix.lower() != ".pptx":
        print(f"Error: not a .pptx file: {args.pptx}", file=sys.stderr)
        return 2

    soffice = find_binary(soffice_candidates())
    pdftoppm = find_binary(pdftoppm_candidates())
    args.out.mkdir(parents=True, exist_ok=True)

    if not soffice:
        qlmanage = shutil.which("qlmanage") if platform.system() == "Darwin" else None
        if qlmanage:
            result = run_image_renderer(
                [
                    qlmanage,
                    "-t",
                    "-s",
                    str(max(256, args.dpi * 10)),
                    "-o",
                    str(args.out),
                    str(args.pptx),
                ], args.out, f"{args.pptx.name}*.png",
            )
            if result.returncode == 0:
                previews = sorted(args.out.glob(f"{args.pptx.name}*.png"))
                print("Quick Look fallback: full per-slide rendering is unavailable.")
                print("Missing: LibreOffice 'soffice' or 'libreoffice'")
                print(f"Install LibreOffice for full PPTX rendering: {LIBREOFFICE_DOWNLOAD_URL}")
                print("Node pdf.js only handles PDF-to-PNG after LibreOffice or another tool creates a PDF.")
                print(f"Preview thumbnails: {len(previews)}")
                for path in previews:
                    print(path)
                return 0
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)

        print("Error: LibreOffice 'soffice' or 'libreoffice' is required for PPTX to PDF conversion.", file=sys.stderr)
        print(f"Install LibreOffice from: {LIBREOFFICE_DOWNLOAD_URL}", file=sys.stderr)
        print(
            "Node pdf.js can render PDF to PNG after a PDF exists, but it cannot convert PPTX to PDF.",
            file=sys.stderr,
        )
        if platform.system() == "Windows":
            print(
                "Note: PowerPoint export automation is intentionally not run by this script; ask the user before operating Microsoft PowerPoint.",
                file=sys.stderr,
            )
        return 1

    pdf_path = args.out / f"{args.pptx.stem}.pdf"

    convert = run(
        [
            soffice,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(args.out),
            str(args.pptx),
        ]
    )
    if convert.returncode != 0 or not pdf_path.exists():
        print(convert.stdout, end="")
        print(convert.stderr, end="", file=sys.stderr)
        print("Error: PPTX to PDF conversion failed.", file=sys.stderr)
        return 1

    if pdftoppm:
        prefix = args.out / "slide"
        image_cmd = [
            pdftoppm,
            f"-{args.format}",
            "-r",
            str(args.dpi),
            str(pdf_path),
            str(prefix),
        ]
        image = run_image_renderer(
            image_cmd, args.out, f"slide-*.{'png' if args.format == 'png' else 'jpg'}"
        )
        if image.returncode != 0:
            print(image.stdout, end="")
            print(image.stderr, end="", file=sys.stderr)
            print("Warning: Poppler PDF to image conversion failed; trying Node pdf.js fallback.", file=sys.stderr)
            if not render_pdf_with_node(pdf_path, args.out, args.format, args.dpi):
                print("Error: PDF to image conversion failed.", file=sys.stderr)
                return 1
    else:
        print("Poppler 'pdftoppm' not found; trying Node pdf.js fallback.")
        if not render_pdf_with_node(pdf_path, args.out, args.format, args.dpi):
            print(
                "Error: PDF to image conversion failed. Install Poppler or ensure Node/npm can install pdfjs-dist and @napi-rs/canvas into the Office Raccoon runtime.",
                file=sys.stderr,
            )
            return 1

    images = sorted(args.out.glob(f"slide-*.{args.format if args.format == 'png' else 'jpg'}"))
    print(f"PDF: {pdf_path}")
    print(f"Rendered slides: {len(images)}")
    for path in images:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
