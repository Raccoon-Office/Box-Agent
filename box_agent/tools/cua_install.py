"""Explicit CUA installation; shared bytes, never shared daemon ownership."""
from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4
import zipfile

VERSION = "0.28.2"
HASHES = {
    "darwin-universal": "e273181b26709c88b1d809474deb3c592b4efae3530b11d76318f1887fc3fbb1",
    "linux-arm64": "55e8a32839a4ac369a773df4dac87b345bd4567779221ade4a5e39223a45a2e8",
    "linux-x86_64": "a1d99fd04bb4927ef5ffdbe60eb91ed8b51a2bab60e10fc604a75bd59ce69c3e",
    "windows-arm64": "578b88ff2dd56f06eb7e984d73aaf5e76f59c6fde9542c967d6a30d00213c680",
    "windows-x86_64": "1f4bfceeab64cb7f56be7aad774c3dc2d2910d1427e4be1d79939c706e8029ba",
}
DIRECTORY = re.compile(r"^0\.28\.2-(darwin|linux|win32)-(arm64|x64)-[a-f0-9-]+$")


def shared_root() -> Path:
    override = os.environ.get("RACCOON_CUA_RESOURCE_DIR")
    if override:
        root = Path(override)
        if not root.is_absolute():
            raise ValueError("RACCOON_CUA_RESOURCE_DIR must be absolute")
        return root
    # User-wide resources, deliberately independent of BOX_AGENT_HOME/profile.
    return Path.home() / ".box-agent" / "resources" / "cua-driver"


def executable(directory: Path) -> Path:
    if sys.platform == "darwin":
        return directory / "CuaDriver.app" / "Contents" / "MacOS" / "cua-driver"
    return directory / ("cua-driver.exe" if sys.platform == "win32" else "cua-driver")


def shared_driver() -> Path | None:
    try:
        directory = json.loads((shared_root() / "current.json").read_text())["directory"]
        if not isinstance(directory, str) or not DIRECTORY.fullmatch(directory):
            return None
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        if not directory.startswith(f"{VERSION}-{sys.platform}-{arch}-"):
            return None
        driver = executable(shared_root() / directory)
        return driver if driver.is_file() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def release(base: str | None = None) -> tuple[str, str]:
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(platform.machine().lower())
    system = {"darwin": "darwin", "win32": "windows", "linux": "linux"}.get(sys.platform)
    if not arch or not system:
        raise ValueError("Unsupported CUA platform")
    target = "darwin-universal" if system == "darwin" else f"{system}-{arch}"
    suffix = ".tar.gz" if system == "darwin" else "-binary." + ("zip" if system == "windows" else "tar.gz")
    root = (base or "").strip() or "https://github.com/trycua/cua/releases/download/"
    parsed = urlsplit(root)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("CUA download base must be an HTTPS directory without credentials, query or fragment")
    return f"{root.rstrip('/')}/cua-driver-rs-v{VERSION}/cua-driver-rs-{VERSION}-{target}{suffix}", HASHES[target]


def safe_path(name: str) -> Path:
    if not name or "\\" in name or "\0" in name or name.startswith("/") or re.match(r"^[a-z]:", name, re.I) or ".." in name.split("/"):
        raise ValueError("Unsafe CUA archive entry")
    return Path(name)


def extract(archive: Path, destination: Path) -> None:
    count = size = 0

    def check(name: str, length: int) -> Path:
        nonlocal count, size
        count += 1
        size += length
        if count > 5000 or size > 500 * 1024 * 1024:
            raise ValueError("CUA archive exceeds extraction limits")
        return destination / safe_path(name)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for entry in source.infolist():
                target = check(entry.filename, entry.file_size)
                mode = entry.external_attr >> 16
                if entry.flag_bits & 1 or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise ValueError("Unsupported CUA archive entry")
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(entry) as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
    else:
        with tarfile.open(archive) as source:
            for entry in source:
                target = check(entry.name, entry.size)
                if entry.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif entry.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(entry) as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    target.chmod(entry.mode & 0o777)
                else:
                    raise ValueError("Unsupported CUA archive entry")


def verify(driver: Path) -> None:
    output = subprocess.run([str(driver), "--version"], check=True, capture_output=True, text=True, timeout=20).stdout
    if not re.search(r"\b0\.28\.2\b", output):
        raise ValueError(f"CUA driver must be version {VERSION}")
    if sys.platform == "darwin":
        bundle = driver.parents[2]
        for args in (["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)], ["/usr/sbin/spctl", "--assess", "--type", "execute", str(bundle)]):
            subprocess.run(args, check=True, capture_output=True, timeout=20)


def install_cua() -> Path:
    url, expected = release(os.environ.get("RACCOON_CUA_DOWNLOAD_BASE_URL"))
    existing = shared_driver()
    if existing:
        try:
            verify(existing)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # Repair in a new directory; keep the old pointer until verified.
        else:
            return existing
    root = shared_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Unique destinations + atomic pointer replacement allow concurrent hosts
    # without replacing binaries used by a running daemon.
    with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as task:
        archive = Path(task) / "archive"
        digest = hashlib.sha256()
        downloaded = 0
        deadline = time.monotonic() + 300
        with build_opener(HttpsRedirect()).open(url, timeout=30) as response, archive.open("xb") as output:
            if urlsplit(response.url).scheme != "https":
                raise ValueError("CUA download redirected outside HTTPS")
            while chunk := response.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > 200 * 1024 * 1024 or time.monotonic() > deadline:
                    raise ValueError("CUA download exceeds size/time limit")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("CUA archive SHA-256 mismatch")
        stage = Path(task) / "unpacked"
        stage.mkdir()
        extract(archive, stage)
        payload = stage
        if not executable(payload).is_file():
            candidates = [item for item in stage.iterdir() if item.is_dir() and executable(item).is_file()]
            if len(candidates) != 1:
                raise ValueError("CUA archive has no unique executable payload")
            payload = candidates[0]
        driver = executable(payload)
        if sys.platform != "win32":
            driver.chmod(0o755)
        verify(driver)
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        directory = f"{VERSION}-{sys.platform}-{arch}-{uuid4()}"
        destination = root / directory
        payload.rename(destination)
        pointer = root / f".current-{uuid4()}.json"
        try:
            pointer.write_text(json.dumps({"directory": directory}), encoding="utf-8")
            pointer.replace(root / "current.json")
        finally:
            pointer.unlink(missing_ok=True)
        return executable(destination)


class HttpsRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        if urlsplit(url).scheme != "https":
            raise ValueError("CUA download redirected outside HTTPS")
        return super().redirect_request(request, fp, code, message, headers, url)
