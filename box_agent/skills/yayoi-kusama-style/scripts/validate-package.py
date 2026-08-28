#!/usr/bin/env python3
"""Lightweight local validator for yayoi-kusama-style skill package."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "SKILL.md",
    "README.md",
    "AGENTS.md",
    "package.json",
    "_meta.json",
    "source-map.md",
    "knowledge/source-article-notes.md",
    "references/visual-engine.md",
    "references/style-patterns.md",
    "references/workflow.md",
    "references/philosophy-rules.md",
    "references/boundary-rules.md",
    "playbooks/aigc-image.md",
    "playbooks/spatial-immersive.md",
    "playbooks/product-homage.md",
    "phrasebook/prompt-language.md",
    "templates/output-template.md",
    "examples/style-examples.md",
    "qa/test-prompts.md",
    "qa/quality-checklist.md",
]
SKILL_NAME = "yayoi-kusama-style"
DISPLAY_NAME = "草间弥生风格"
VERSION = "1.2.0"


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def main() -> None:
    missing = [p for p in REQUIRED if not (ROOT / p).exists()]
    if missing:
        fail("missing required files: " + ", ".join(missing))

    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        fail("SKILL.md missing YAML front matter")
    if yaml is None:
        fm = match.group(1)
        if f"name: {SKILL_NAME}" not in fm:
            fail(f"front matter name must be {SKILL_NAME}")
        if "description:" not in fm:
            fail("description is missing")
    else:
        meta = yaml.safe_load(match.group(1)) or {}
        if meta.get("name") != SKILL_NAME:
            fail(f"front matter name must be {SKILL_NAME}")
        desc = meta.get("description") or ""
        if len(desc) < 20:
            fail("description is too weak")
        if "disable-model-invocation" in meta and meta.get("disable-model-invocation") is True:
            fail("auto-trigger required: disable-model-invocation must not be true")
        if str(meta.get("version")) != VERSION:
            fail("SKILL.md version mismatch")

    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    if pkg.get("name") != SKILL_NAME:
        fail("package.json name mismatch")
    if pkg.get("version") != VERSION:
        fail("package.json version mismatch")
    if pkg.get("displayName") != DISPLAY_NAME:
        fail("package.json displayName mismatch")

    meta_json = json.loads((ROOT / "_meta.json").read_text(encoding="utf-8"))
    if meta_json.get("slug") != SKILL_NAME:
        fail("_meta.json slug mismatch")
    if meta_json.get("version") != VERSION:
        fail("_meta.json version mismatch")

    skill_links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
    for link in skill_links:
        if link.startswith("http") or link.startswith("#"):
            continue
        if not (ROOT / link).exists():
            fail(f"broken local link in SKILL.md: {link}")

    omitted_marker = "[Full" + " tool-call argument omitted"
    forbidden = ["/" + "Users/", "\\" + "Users\\", omitted_marker]
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if path.name == ".DS_Store":
            continue
        if "baiduyun" in path.name.lower():
            continue
        if path.name.startswith("."):
            fail(f"hidden file is not allowed: {rel}")
        if path.suffix == ".zip":
            fail(f"nested zip is not allowed inside skill package: {rel}")
        if path.is_file() and path.suffix in {".md", ".json", ".py"}:
            content = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in content:
                    fail(f"forbidden local/private marker in {rel}")

    print("PASS: skill package structure, metadata and privacy checks passed")


if __name__ == "__main__":
    main()
