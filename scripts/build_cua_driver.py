#!/usr/bin/env python3
"""Build a pinned macOS ARM64 Driver archive from clean Git objects.

Local development signing is explicit. This script never installs, starts a
daemon, changes TCC, notarizes, or publishes the resulting archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def build(repo: Path, output: Path, *, identity: str, offline: bool, target_dir: Path | None = None) -> Path:
    pin = json.loads((ROOT / "box_agent/_vendor/mac_cua/manifest.json").read_text())
    commit = pin["driver_commit"]
    # Never build the caller's dirty checkout or trust its current HEAD.
    subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"], check=True)
    output.mkdir(parents=True, exist_ok=True)
    source = output / "source"
    if source.exists():
        raise ValueError("Use a new output directory; source must be a clean export")
    source.mkdir()
    archive = output / "source.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "-C", str(repo), "archive", commit,
                        "libs/cua-driver", "libs/cua", "libs/images/sandbox-images.json", "LICENSE.md"],
                       stdout=stream, check=True)
    with tarfile.open(archive) as contents:
        contents.extractall(source, filter="data")
    archive.unlink()
    rust = source / "libs/cua-driver/rust"
    target_dir = (target_dir or output / "target").resolve()
    env = dict(os.environ, CUA_DRIVER_GIT_SHA=commit, CUA_TELEMETRY="0", DO_NOT_TRACK="1",
               CARGO_TARGET_DIR=str(target_dir))
    command = ["cargo", "build", "--locked", "--release", "--target", "aarch64-apple-darwin", "-p", "cua-driver"]
    if offline:
        command.append("--offline")
    subprocess.run(command, cwd=rust, env=env, check=True)
    binary = target_dir / "aarch64-apple-darwin/release/cua-driver"
    payload = output / "payload"
    payload.mkdir(exist_ok=True)
    app = payload / "CuaDriver.app"
    shutil.copytree(rust / "scripts/CuaDriverBundle", app,
                    ignore=shutil.ignore_patterns(".gitkeep"))
    destination = app / "Contents/MacOS/cua-driver"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, destination)
    import tomllib
    version = tomllib.loads((rust / "Cargo.toml").read_text())["workspace"]["package"]["version"]
    plist_path = app / "Contents/Info.plist"
    info = plistlib.loads(plist_path.read_bytes())
    info.update(CFBundleShortVersionString=version, CFBundleVersion="1", BoxAgentDriverSourceSHA=commit)
    # Distinct development identity: never masquerade as the official app.
    if identity == "-":
        info.update(CFBundleIdentifier="com.xhx.cua-driver.dev", CFBundleName="XHX CuaDriver Dev")
    plist_path.write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--sign", identity, "--timestamp=none",
                    "--entitlements", str(rust / "scripts/CuaDriver.entitlements"), str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    executable_hash = sha256(destination)
    artifact = output / f"cua-driver-{version}-{commit[:12]}-darwin-arm64.tar.gz"
    shutil.copy2(source / "LICENSE.md", payload / "LICENSE.md")
    with tarfile.open(artifact, "w:gz") as contents:
        contents.add(app, arcname="CuaDriver.app")
        contents.add(payload / "LICENSE.md", arcname="LICENSE.md")
    manifest = {
        "schema_version": 1, "source_commit": commit, "mac_cua_commit": pin["source_commit"],
        "version": version, "platform": "darwin-arm64", "archive": artifact.name,
        "archive_sha256": sha256(artifact), "size_bytes": artifact.stat().st_size,
        "executable_sha256": executable_hash,
        "signing": "adhoc-development" if identity == "-" else "certificate",
        "notarized": False,
        "cargo_lock_sha256": sha256(rust / "Cargo.lock"),
        "rustc": subprocess.check_output(["rustc", "--version"], text=True).strip(),
    }
    path = output / "driver-release.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signing-identity", required=True, help="Use '-' for local ad-hoc development only")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--target-dir", type=Path, help="Reuse Cargo's compilation cache; source is always exported afresh")
    args = parser.parse_args()
    build(args.repo.resolve(), args.output.resolve(), identity=args.signing_identity,
          offline=args.offline, target_dir=args.target_dir)


if __name__ == "__main__":
    main()
