"""Build the single-skill ZIP accepted by the macOS client from a staged runtime."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile


# The test entry can coexist with any built-in or user-installed PPT skill.
NAMES = {
    "sn-ppt-design-entry": "ppt062-entry",
    "sn-ppt-design-story": "ppt062-story",
    "sn-ppt-design-doctor": "ppt062-doctor",
    "sn-ppt-design-tools": "ppt062-tools",
    "sn-ppt-dsl": "ppt062-dsl",
}
ENTRY = """---
name: ppt-design-062
displayName: PPT 设计 0.6.2
description: 在当前客户端主动选择此技能，以 0.6.2 设计模式制作 PowerPoint 文件。
metadata:
  user_visible: true
---

# PPT 设计 0.6.2

用户点选本技能即选择设计模式。将原题、附件和宿主工作目录交给 `get_skill(skill_name="ppt062-entry")`，按该入口完成大纲、写页、导出和最终看图。四个辅助模块和入口均随此技能提供。
"""
NATIVE = ("sharp", "@img/colour", "detect-libc", "semver", "@img/sharp-darwin-arm64",
          "@img/sharp-libvips-darwin-arm64")
JS = {"@icon-park/svg": "lib/index.js", "jszip": "lib/index.js", "pptxgenjs": "dist/pptxgen.cjs.js"}


def add_renderer(renderer: Path, destination: Path, esbuild: Path) -> None:
    """Keep complete icon exports, native image processing and license notices."""
    packages = renderer / "node_modules"
    for name in NATIVE:
        shutil.copytree(packages / name, destination / "node_modules" / name)
    for name, main in JS.items():
        origin = packages / name
        target = destination / "node_modules" / name
        target.mkdir(parents=True)
        metadata = json.loads((origin / "package.json").read_text())
        (target / "package.json").write_text(json.dumps({
            "name": name, "version": metadata["version"], "main": "index.cjs",
            "license": metadata.get("license"),
        }, ensure_ascii=False, indent=2))
        command = [str(esbuild), str(Path("node_modules") / name / main),
                   "--platform=node", "--target=node22", "--format=cjs", "--bundle",
                   "--log-level=error", f"--outfile={target / 'index.cjs'}"]
        if name == "pptxgenjs":
            command.append("--external:jszip")
        subprocess.run(command, check=True, cwd=renderer)
    for license_file in packages.rglob("*"):
        if license_file.is_file() and license_file.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            output = destination / "licenses" / license_file.relative_to(packages)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(license_file, output)


def build(runtime: Path, esbuild: Path, output: Path) -> None:
    info = json.loads((runtime / "manifest.json").read_text())
    if (info.get("platform"), info.get("arch")) != ("darwin", "arm64"):
        raise ValueError("This client preview requires a staged darwin-arm64 runtime")
    source = runtime / "skills/presentation-design"
    if not source.is_dir():
        source = runtime / "bin/_internal/box_agent/skills/presentation-design"
    if json.loads((source / "source.json").read_text())["version"] != "0.6.2":
        raise ValueError("Expected the 0.6.2 presentation release")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "SKILL.md").write_text(ENTRY)
        for builtin, alias in NAMES.items():
            src = source / "skills" / builtin
            dst = root / "modules" / alias
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("node_modules", "__pycache__", "*.pyc", "tests"))
            for file in (p for p in dst.rglob("*") if p.is_file() and p.suffix in {".md", ".py", ".txt", ".json"}):
                original = file.read_text()
                text = original
                for standard, portable in NAMES.items():
                    text = text.replace(standard, portable)
                if text != original:
                    file.write_text(text)
        add_renderer(source / "skills/sn-ppt-dsl", root / "modules/ppt062-dsl", esbuild)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent) as staging:
            candidate = Path(staging) / output.name
            with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for file in sorted(p for p in root.rglob("*") if p.is_file()):
                    archive.write(file, file.relative_to(root).as_posix())
            with zipfile.ZipFile(candidate) as archive:
                members = archive.infolist()
                if len(members) > 512 or candidate.stat().st_size > 50 * 1024**2:
                    raise ValueError("ZIP exceeds the desktop entry-count or download-size limit")
                if any(m.file_size > 20 * 1024**2 for m in members):
                    raise ValueError("A skill file exceeds the desktop unpacked file size limit")
                if sum(m.file_size for m in members) > 100 * 1024**2:
                    raise ValueError("Skill exceeds desktop unpacked size limit")
                bad = archive.testzip()
                if bad:
                    raise ValueError(f"Corrupt ZIP entry {bad}")
            candidate.replace(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--esbuild", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.runtime.resolve(), args.esbuild.resolve(), args.output.resolve())
