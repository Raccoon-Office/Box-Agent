"""Prepare official expert archives before the existing session assembly."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .expert_package_format import ExpertPackageError, load_expert_profile
from .payload_privacy import private_payload_scope
from .tools.file_change_receipts import destination_lock
from .user_paths import state_path

# Same official content origins used by the desktop package distribution.
_DOWNLOAD_HOSTS = frozenset({
    "sta-volc.xiaohuanxiong.com",
    "desktop-skills.tos-s3-cn-shanghai.volces.com",
    "desktop-skills-pre.tos-s3-cn-shanghai.volces.com",
})
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_ENTRIES = 20000
_PREPARE_TIMEOUT = 120
_PROFILE_FIELDS = frozenset({
    "instructions", "visibleRules", "internalRules", "constraints", "leader", "members",
    "workflow", "packageSnapshot", "defaultSkills", "requiredSkills", "optionalSkills",
    "skillBindings", "executionMode", "execution_mode", "orchestration",
    "reviewRules", "outputFormat", "teamPersona", "qualityGates", "blockedConditions",
    "packageReference",
})


def _invalid(detail: str, code: str = "EXPERT_PACKAGE_INVALID_REQUEST") -> ExpertPackageError:
    return ExpertPackageError(detail, code)


def _validate_url(value: Any) -> str:
    if (not isinstance(value, str) or len(value) > 16384
            or any(character.isspace() or ord(character) < 32 for character in value)):
        raise _invalid("Expert package requires a valid official HTTPS download URL")
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname in _DOWNLOAD_HOSTS
                 and url.port in (None, 443) and url.username is None
                 and url.password is None and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise _invalid("Expert package download origin is not allowed")
    return value


def _ticket(reference: dict[str, Any]) -> dict[str, Any]:
    ticket = reference["packageDownload"]
    if not isinstance(ticket, dict) or _PROFILE_FIELDS.intersection(reference):
        raise _invalid("A download reference cannot also supply expert rules or prepared resources")
    if (not isinstance(reference.get("id"), str) or not _ID.fullmatch(reference["id"])
            or not isinstance(reference.get("name"), str) or not reference["name"].strip()
            or reference.get("source", "builtin") != "builtin"):
        raise _invalid("An official download reference requires its catalog ID and name")
    if "id" in ticket and ticket["id"] != reference["id"]:
        raise _invalid("Download ticket does not match the selected expert")
    if not isinstance(ticket.get("digest"), str) or not _SHA256.fullmatch(ticket["digest"]):
        raise _invalid("Expert package requires its SHA256 digest")
    size = ticket.get("size")
    if type(size) is not int or not 0 < size <= _MAX_ARCHIVE_BYTES:
        raise _invalid("Expert package size is invalid or exceeds the supported limit")
    for key in ("description", "starterPrompt"):
        if key in reference and not isinstance(reference[key], str):
            raise _invalid("Expert display metadata must contain text")
    if "tags" in reference and (not isinstance(reference["tags"], list)
                               or any(not isinstance(tag, str) for tag in reference["tags"])):
        raise _invalid("Expert tags must be text")
    return {"downloadUrl": _validate_url(ticket.get("downloadUrl")),
            "digest": ticket["digest"], "size": size}


async def _finish_task(task: asyncio.Task) -> None:
    """Keep ownership through repeated cancellation until cleanup has finished."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        task.exception()


async def _disk_call(function: Callable, *arguments):
    # A cancelled preparation must not delete staging while its worker is writing.
    task = asyncio.create_task(asyncio.to_thread(function, *arguments))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _finish_task(task)
        raise


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if (len(members) > _MAX_ENTRIES
            or sum(member.file_size for member in members) > _MAX_EXPANDED_BYTES):
        raise _invalid("Expert package expands beyond the supported limit", "EXPERT_PACKAGE_INVALID")
    names = set()
    for member in members:
        name = member.filename.rstrip("/")
        path = PurePosixPath(name)
        mode = member.external_attr >> 16
        if (not name or path.is_absolute() or "\\" in name or ":" in name
                or any(part in ("", ".", "..") for part in name.split("/"))
                or name in names or member.file_size > _MAX_FILE_BYTES
                or member.flag_bits & 1
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
            raise _invalid("Expert package has an unsafe archive member", "EXPERT_PACKAGE_INVALID")
        names.add(name)
    return members


def _package_root(content: Path) -> Path:
    manifests = list(content.glob(".box-agent-plugin/plugin.json"))
    manifests.extend(content.glob("*/.box-agent-plugin/plugin.json"))
    if len(manifests) != 1:
        raise _invalid("Expert package must contain exactly one package manifest", "EXPERT_PACKAGE_INVALID")
    package = manifests[0].parent.parent
    if package.is_symlink() or not package.resolve().is_relative_to(content.resolve()):
        raise _invalid("Expert package root is outside its cache", "EXPERT_PACKAGE_INVALID")
    return package


def _verify_content(archive_path: Path, content: Path) -> Path:
    if (content.is_symlink() or not content.is_dir()
            or not content.resolve().is_relative_to(archive_path.parent.resolve())):
        raise _invalid("Expert cache content is outside its cache entry", "EXPERT_PACKAGE_CACHE_INVALID")
    with zipfile.ZipFile(archive_path) as archive:
        members = _members(archive)
        expected_files = {member.filename for member in members if not member.is_dir()}
        expected_dirs = {member.filename.rstrip("/") for member in members if member.is_dir()}
        for name in expected_files | expected_dirs:
            expected_dirs.update(parent.as_posix() for parent in PurePosixPath(name).parents if parent != PurePosixPath("."))
        for path in content.rglob("*"):
            relative = path.relative_to(content).as_posix()
            mode = path.lstat().st_mode
            if (path.is_symlink() or stat.S_ISREG(mode) and relative not in expected_files
                    or stat.S_ISDIR(mode) and relative not in expected_dirs
                    or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise _invalid("Expert cache contains resources not present in its archive", "EXPERT_PACKAGE_CACHE_INVALID")
        for member in members:
            target = content / member.filename
            if target.is_symlink() or not target.resolve().is_relative_to(content.resolve()):
                raise _invalid("Expert cache has an invalid resource path", "EXPERT_PACKAGE_CACHE_INVALID")
            if member.is_dir():
                if not target.is_dir():
                    raise _invalid("Expert cache is incomplete", "EXPERT_PACKAGE_CACHE_INVALID")
                continue
            if not target.is_file() or target.stat().st_size != member.file_size:
                raise _invalid("Expert cache is incomplete", "EXPERT_PACKAGE_CACHE_INVALID")
            digest = hashlib.sha256()
            with archive.open(member) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != _hash(target):
                raise _invalid("Expert cache resource differs from its archive", "EXPERT_PACKAGE_CACHE_INVALID")
    package = _package_root(content)
    files_path = package / "files.json"
    if not files_path.is_file():
        raise _invalid("Expert package is missing its file manifest", "EXPERT_PACKAGE_INVALID")
    try:
        files = json.loads(files_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise _invalid("Expert package file manifest is invalid", "EXPERT_PACKAGE_INVALID") from None
    if not isinstance(files, list):
        raise _invalid("Expert package file manifest is invalid", "EXPERT_PACKAGE_INVALID")
    seen = set()
    for record in files:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise _invalid("Expert package file record is invalid", "EXPERT_PACKAGE_INVALID")
        name = record["path"]
        relative = PurePosixPath(name)
        if (relative.is_absolute() or "\\" in name or ":" in name or ".." in relative.parts
                or name in seen or not isinstance(record.get("sha256"), str)
                or not _SHA256.fullmatch(record["sha256"])):
            raise _invalid("Expert package file record is invalid", "EXPERT_PACKAGE_INVALID")
        seen.add(name)
        target = package / name
        if (target.is_symlink() or not target.resolve().is_relative_to(package.resolve())
                or not target.is_file() or _hash(target) != record["sha256"]
                or ("size" in record and target.stat().st_size != record["size"])):
            raise _invalid("Expert package file integrity check failed", "EXPERT_PACKAGE_INVALID")
    return package


def _extract(archive_path: Path, content: Path) -> Path:
    content.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        members = _members(archive)
        for member in members:
            target = content / member.filename
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            target.chmod(0o700 if (member.external_attr >> 16) & 0o111 else 0o600)
    return _verify_content(archive_path, content)


async def _download(ticket: dict[str, Any], destination: Path,
                    transport: httpx.AsyncBaseTransport | None) -> None:
    url = ticket["downloadUrl"]
    digest, size = hashlib.sha256(), 0
    # Do not let httpx diagnostics retain signed URLs or use ambient credentials.
    with private_payload_scope():
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(60, connect=15)) as client:
            for _ in range(5):
                client.cookies.clear()
                async with client.stream("GET", url, headers={"Accept": "application/zip"}) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            break
                        url = _validate_url(urljoin(url, location))
                        continue
                    if response.status_code != 200:
                        raise _invalid("Expert package download failed; request a fresh ticket for the same digest",
                                       "EXPERT_PACKAGE_DOWNLOAD_FAILED")
                    with destination.open("xb") as stream:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > ticket["size"]:
                                raise _invalid("Expert package exceeds its declared size", "EXPERT_PACKAGE_HASH_MISMATCH")
                            digest.update(chunk)
                            stream.write(chunk)
                    if size != ticket["size"] or digest.hexdigest() != ticket["digest"]:
                        raise _invalid("Expert package SHA256 or size does not match its ticket", "EXPERT_PACKAGE_HASH_MISMATCH")
                    return
    raise _invalid("Expert package download redirect limit exceeded", "EXPERT_PACKAGE_DOWNLOAD_FAILED")


async def _prepare(reference: dict[str, Any], kind: str, ticket: dict[str, Any],
                   transport: httpx.AsyncBaseTransport | None) -> dict[str, Any]:
    root = state_path("experts/packages")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / ticket["digest"]
    async with destination_lock(target):
        if target.is_symlink():
            raise _invalid("Expert cache entry cannot be a symbolic link", "EXPERT_PACKAGE_CACHE_INVALID")
        if target.exists():
            archive = target / "package.zip"
            if (not target.is_dir() or archive.is_symlink() or not archive.is_file()
                    or archive.stat().st_size != ticket["size"]
                    or await _disk_call(_hash, archive) != ticket["digest"]):
                raise _invalid("Expert cache does not match the selected package", "EXPERT_PACKAGE_CACHE_INVALID")
            package = await _disk_call(_verify_content, archive, target / "content")
        else:
            staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=root))
            try:
                archive = staging / "package.zip"
                await _download(ticket, archive, transport)
                archive.chmod(0o600)
                package = await _disk_call(_extract, archive, staging / "content")
                # Validate package type/rules before publishing reusable bytes.
                await _disk_call(load_expert_profile, package, kind, reference, ticket["digest"])
                relative = package.relative_to(staging)
                staging.rename(target)
                package = target / relative
            finally:
                if staging.exists():
                    await _disk_call(shutil.rmtree, staging)
        return await _disk_call(load_expert_profile, package, kind, reference, ticket["digest"])


async def prepare_expert_meta(meta: dict[str, Any], *,
                              transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Expand download tickets; prepared full objects keep their existing contract."""
    resolved = meta
    team_key = "expert_team" if meta.get("expert_team") else "expertTeam"
    try:
        for key, kind in (("expert", "expert"), (team_key, "team")):
            reference = meta.get(key)
            if not isinstance(reference, dict):
                continue
            if set(reference) == {"id"}:
                raise _invalid("Supply an official download ticket or the existing complete expert object",
                               "EXPERT_PACKAGE_REFERENCE_REQUIRED")
            if "packageDownload" not in reference:
                continue
            ticket = _ticket(reference)
            preparation = asyncio.create_task(_prepare(reference, kind, ticket, transport))
            try:
                profile = await asyncio.wait_for(asyncio.shield(preparation), _PREPARE_TIMEOUT)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                # Python 3.10 wait_for can finish after a second cancellation
                # before its inner task. Own that task until its finally exits.
                preparation.cancel()
                await _finish_task(preparation)
                raise
            resolved = {**resolved, key: profile}
    except ExpertPackageError:
        raise
    except asyncio.TimeoutError:
        raise _invalid("Expert package preparation timed out", "EXPERT_PACKAGE_PREPARATION_TIMEOUT") from None
    except httpx.HTTPError:
        raise _invalid("Expert package storage is unreachable", "EXPERT_PACKAGE_DOWNLOAD_FAILED") from None
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
        raise _invalid("Expert package resources could not be prepared", "EXPERT_PACKAGE_INVALID") from None
    return resolved
