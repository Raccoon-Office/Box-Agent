"""Explicit local release installation and source identity for the imported REPL.

The release manifest is a user-supplied local build receipt, not a signature or
download trust root. Official install-cua verification remains unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
from uuid import uuid4

from box_agent.tools.cua_install import executable, extract, shared_root, verify

PINNED_VERSION = "0.38.0"


def source_pin() -> str:
    from box_agent._vendor import mac_cua
    return json.loads((Path(mac_cua.__file__).parent / "manifest.json").read_text())["driver_commit"]


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def validate_receipt(receipt: dict) -> None:
    if receipt.get("schema_version") != 1 or receipt.get("source_commit") != source_pin():
        raise ValueError("Driver release does not match the imported mac-cua source pin")
    if (sys.platform != "darwin" or platform.machine().lower() not in {"arm64", "aarch64"}
            or receipt.get("platform") != "darwin-arm64"):
        raise ValueError("This Driver release requires macOS ARM64")
    if receipt.get("version") != PINNED_VERSION:
        raise ValueError("Unsupported pinned Driver version")
    for key in ("archive_sha256", "executable_sha256"):
        if not isinstance(receipt.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", receipt[key]):
            raise ValueError(f"Invalid {key}")


def require_build(build: dict, driver: Path) -> None:
    if (not isinstance(build, dict) or build.get("git_sha") != source_pin() or build.get("version") != PINNED_VERSION
            or build.get("exe_sha256") != digest(driver)):
        raise RuntimeError("CUA Driver identity mismatch: install the pinned release and restart the host/daemon")


def verify_source(driver: Path) -> None:
    # doctor is read-only without --fix; a permissions failure may return nonzero
    # while still returning the binary identity. Do not request OS permissions.
    result = subprocess.run([str(driver), "doctor", "--json"], capture_output=True,
                            text=True, timeout=30,
                            env={**os.environ, "CUA_TELEMETRY": "0", "DO_NOT_TRACK": "1"})
    try:
        build = json.loads(result.stdout)["build"]
        require_build(build, driver)
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError("Cannot establish pinned CUA Driver identity; rebuild/install the matching release") from error


def installed_pinned_driver() -> str | None:
    try:
        root = shared_root()
        directory = json.loads((root / "current-repl.json").read_text())["directory"]
        if not isinstance(directory, str) or not re.fullmatch(r"repl-[a-f0-9]{40}-[a-f0-9]{32}", directory):
            return None
        payload = root / directory
        receipt = json.loads((payload / "driver-release.json").read_text())
        validate_receipt(receipt)
        driver = executable(payload)
        if digest(driver) != receipt["executable_sha256"]:
            return None
        return str(driver)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def install_release(archive: Path, manifest: Path, *, allow_development: bool = False) -> Path:
    receipt = json.loads(manifest.read_text())
    validate_receipt(receipt)
    development = receipt.get("signing") == "adhoc-development"
    if development and not allow_development:
        raise ValueError("Ad-hoc release requires explicit --allow-development-signature")
    if (archive.stat().st_size != receipt.get("size_bytes")
            or digest(archive) != receipt["archive_sha256"]):
        raise ValueError("Driver archive SHA-256 or size mismatch")
    root = shared_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".repl-install-", dir=root) as task:
        stage = Path(task) / "payload"
        stage.mkdir()
        extract(archive, stage)
        driver = executable(stage)
        if digest(driver) != receipt["executable_sha256"]:
            raise ValueError("Driver executable SHA-256 mismatch")
        if development:
            subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                            str(driver.parents[2])], check=True, capture_output=True, timeout=20)
        else:
            verify(driver)  # Retain Gatekeeper verification for non-development packages.
        verify_source(driver)
        (stage / "driver-release.json").write_text(json.dumps(receipt, indent=2) + "\n")
        directory = f"repl-{source_pin()}-{uuid4().hex}"
        stage.rename(root / directory)
        pointer = Path(task) / "current-repl.json"
        pointer.write_text(json.dumps({"directory": directory}) + "\n")
        pointer.replace(root / "current-repl.json")
    return executable(root / directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--allow-development-signature", action="store_true")
    args = parser.parse_args()
    print(install_release(args.archive, args.manifest,
                          allow_development=args.allow_development_signature))


if __name__ == "__main__":
    main()
