#!/usr/bin/env python3
"""Reproduce the vendored App/REPL closure from Git objects plus ordered patches."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

PIN = "9ef2fa2604968f056326b2cbcc9ca8a3addc5c31"
BRANCH = "dev/codex_v2.0"
SOURCE = "src/mac_cua/data/guest"
MODULES = ("actions", "adapter", "driver", "evidence", "mcp", "native",
           "observation", "service", "windows")
FILES = tuple(f"cua_adapter/cua_{name}.py" for name in MODULES) + (
    "cua_adapter/cua_repl.mjs", "cua_images.py", "cua_api.md",
)
ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate(repo: Path, commit: str, patches: Path) -> dict[str, bytes]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Use a full, lowercase Git commit SHA")
    git(repo, "merge-base", "--is-ancestor", commit, BRANCH)
    original = {name: git(repo, "show", f"{commit}:{SOURCE}/{name}") for name in FILES}
    driver = git(repo, "ls-tree", commit, "third_party/cua-driver").decode().split()[2]
    patch_records = []
    with tempfile.TemporaryDirectory(prefix="box-mac-cua-") as directory:
        stage = Path(directory)
        for name, data in original.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for patch in sorted(patches.glob("*.patch")):
            subprocess.run(["git", "apply", "--check", str(patch.resolve())], cwd=stage, check=True)
            subprocess.run(["git", "apply", str(patch.resolve())], cwd=stage, check=True)
            patch_records.append({"file": patch.name, "sha256": digest(patch.read_bytes())})
        result = {name: (stage / name).read_bytes() for name in FILES}
    manifest = {
        "source_branch": BRANCH, "source_commit": commit, "source_root": SOURCE,
        "driver_commit": driver, "bridge_contract": 1, "patches": patch_records,
        "files": {name: {"source_sha256": digest(original[name]), "sha256": digest(result[name])}
                  for name in FILES},
    }
    result["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--commit", default=PIN)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "box_agent/_vendor/mac_cua"
    result = generate(args.repo, args.commit, ROOT / "scripts/mac_cua_patches")
    old_manifest = target / "manifest.json"
    previous = json.loads(old_manifest.read_text())["files"] if old_manifest.exists() else {}
    if args.check:
        mismatches = [name for name, data in result.items()
                      if not (target / name).exists() or (target / name).read_bytes() != data]
        mismatches += [name for name in previous if name not in result and (target / name).exists()]
        if mismatches:
            raise SystemExit("Vendor mismatch: " + ", ".join(mismatches))
        print("Vendored source and patches are reproducible")
        return
    # Validation and patch application finish before touching the destination.
    for name in previous:
        if name not in result:
            path = target / name
            if not path.resolve().is_relative_to(target.resolve()):
                raise ValueError("Invalid managed path")
            path.unlink(missing_ok=True)
    for name, data in result.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f"Imported {len(FILES)} files from {args.commit}")


if __name__ == "__main__":
    main()
