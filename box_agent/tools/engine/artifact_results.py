"""Tool-output artifact detection with workspace changes as corroboration only."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from time import perf_counter
from typing import Any

from ...artifacts import (
    artifact_scan_root as _artifact_scan_root,
    make_artifact as _make_artifact,
)
from ...events import ArtifactEvent
from ...artifact_publication import SUFFIX, intermediate_fingerprints, is_intermediate

_log = logging.getLogger("box_agent.core")


# Regex to match file references like [foo.png] in tool output. Keep the
# candidate bounded: structured tool payloads such as web_search commonly use
# a top-level JSON array, and an unbounded match can otherwise consume the
# entire payload and misclassify it as one enormous filename.
_MAX_ARTIFACT_REF_CHARS = 512

_MAX_ARTIFACT_COMPONENT_BYTES = 255

_ARTIFACT_REF_RE = re.compile(
    r"\[([^\]\n]{1,512}\.\w{1,10})\]",
    re.IGNORECASE,
)


def _detect_artifacts(
    tool_call_id: str,
    tool_name: str,
    content: str,
    workspace_dir: str | None,
) -> list[ArtifactEvent]:
    """Scan tool output for file references that resolve under the session cwd."""
    if not workspace_dir or not content:
        return []
    references = list(_ARTIFACT_REF_RE.finditer(content))
    if not references:
        return []

    try:
        ws = Path(workspace_dir).resolve()
        out = _artifact_scan_root(workspace_dir)
    except (OSError, RuntimeError, ValueError):
        # Artifact discovery is best-effort and must never fail the tool call.
        return []
    if out is None:
        return []
    try:
        if not out.is_dir():
            return []
    except OSError:
        return []

    fingerprints = _publication_fingerprints(workspace_dir)
    artifacts: list[ArtifactEvent] = []
    seen_paths: set[Path] = set()
    for match in references:
        filename = match.group(1)
        try:
            if len(filename) > _MAX_ARTIFACT_REF_CHARS or any(
                len(os.fsencode(part)) > _MAX_ARTIFACT_COMPONENT_BYTES
                for part in Path(filename).parts
            ):
                continue
            candidate = (out / filename).resolve()
            candidate.relative_to(out)
            if candidate in seen_paths or not candidate.is_file():
                continue
            if is_intermediate(candidate, fingerprints, workspace_dir):
                continue
            artifact = _make_artifact(tool_call_id, candidate, ws)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            # Invalid, overlong, racy, or otherwise unresolvable references are
            # ordinary false positives in arbitrary tool output.
            continue
        seen_paths.add(candidate)
        artifacts.append(artifact)

    return artifacts


# ── Workspace diff-based artifact detection ─────────────────────

# Bulky dependency, VCS, cache, and Box-Agent-internal directories are never
# useful artifact candidates. The limits keep a selected monorepo or data lake
# from turning every tool call into an unbounded recursive walk.
_IGNORE_DIRS = frozenset(
    {
        ".box-agent",
        ".box-agent-scratch",
        ".git",
        ".hg",
        ".ipynb_checkpoints",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "env",
        "node_modules",
        "venv",
    }
)
_ARTIFACT_SCAN_MAX_FILES_ENV = "BOX_AGENT_ARTIFACT_SCAN_MAX_FILES"
_ARTIFACT_SCAN_TIMEOUT_ENV = "BOX_AGENT_ARTIFACT_SCAN_TIMEOUT_SECONDS"
_DEFAULT_ARTIFACT_SCAN_MAX_FILES = 50_000
_DEFAULT_ARTIFACT_SCAN_TIMEOUT_SECONDS = 2.0
_WARNED_SCAN_LIMITS: set[tuple[str, str]] = set()


def _positive_env_number(name: str, default: int | float) -> int | float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw) if isinstance(default, float) else int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _warn_scan_limit(root: Path, reason: str) -> None:
    key = (str(root), reason)
    if key in _WARNED_SCAN_LIMITS:
        return
    _WARNED_SCAN_LIMITS.add(key)
    _log.warning(
        "Artifact scan skipped for %s after reaching %s; explicit tool file paths remain available",
        root,
        reason,
    )


def _snapshot_workspace(workspace_dir: str, *, include_publication_metadata: bool = False) -> set[Path] | None:
    """Return a bounded recursive file snapshot rooted at the session cwd.

    An incomplete walk returns None, distinct from a valid empty snapshot, so
    callers can skip the entire pre/post diff. Structured and text-referenced
    tool outputs remain the fallback artifact-discovery path.
    """
    try:
        out = _artifact_scan_root(workspace_dir)
        if out is None or not out.is_dir():
            return set()
    except (OSError, RuntimeError, ValueError):
        return None

    max_files = int(
        _positive_env_number(
            _ARTIFACT_SCAN_MAX_FILES_ENV,
            _DEFAULT_ARTIFACT_SCAN_MAX_FILES,
        )
    )
    timeout_seconds = float(
        _positive_env_number(
            _ARTIFACT_SCAN_TIMEOUT_ENV,
            _DEFAULT_ARTIFACT_SCAN_TIMEOUT_SECONDS,
        )
    )
    deadline = perf_counter() + timeout_seconds
    files: set[Path] = set()

    def raise_scan_error(error: OSError) -> None:
        # os.walk otherwise silently returns a partial tree on access errors.
        raise error

    try:
        for current_root, dirnames, filenames in os.walk(
            out, topdown=True, onerror=raise_scan_error,
        ):
            if perf_counter() >= deadline:
                _warn_scan_limit(out, f"{timeout_seconds:g}s timeout")
                return None
            dirnames[:] = sorted(
                name for name in dirnames if name not in _IGNORE_DIRS
            )
            current = Path(current_root)
            for filename in sorted(filenames):
                if perf_counter() >= deadline:
                    _warn_scan_limit(out, f"{timeout_seconds:g}s timeout")
                    return None
                entry = current / filename
                is_metadata = entry.name.startswith(".") and entry.name.endswith(SUFFIX) and len(entry.name) > len(SUFFIX) + 1
                if (entry.name.startswith(".") and not (include_publication_metadata and is_metadata)) or entry.suffix == ".tmp":
                    continue
                if not entry.is_file():
                    continue
                files.add(entry)
                if len(files) > max_files:
                    _warn_scan_limit(out, f"{max_files} file limit")
                    return None
    except OSError:
        return None
    return files


def _publication_fingerprints(workspace_dir: str) -> dict[int, set[str]]:
    files = _snapshot_workspace(workspace_dir, include_publication_metadata=True)
    return intermediate_fingerprints(
        path for path in (files or ()) if path.name.startswith(".") and path.name.endswith(SUFFIX)
    )


def _snapshot_workspace_signatures(
    workspace_dir: str,
) -> dict[Path, tuple[int, int]] | None:
    """Snapshot artifact paths plus stat signatures for revision detection."""
    files = _snapshot_workspace(workspace_dir)
    if files is None:
        return None
    signatures: dict[Path, tuple[int, int]] = {}
    for file_path in files:
        try:
            stat = file_path.stat()
        except OSError:
            return None
        signatures[file_path] = (stat.st_size, stat.st_mtime_ns)
    return signatures


def _detect_new_files(
    tool_call_id: str,
    pre_files: set[Path] | None,
    post_files: set[Path] | None,
    already_emitted: set[str],
    workspace_dir: str,
) -> list[ArtifactEvent]:
    """Create ArtifactEvents for files that appeared after tool execution."""
    if pre_files is None or post_files is None:
        return []
    new_files = post_files - pre_files
    if not new_files:
        return []

    ws = Path(workspace_dir).resolve()
    fingerprints = _publication_fingerprints(workspace_dir)
    artifacts: list[ArtifactEvent] = []
    for fpath in sorted(new_files):
        if fpath.name.startswith(".") or fpath.name.startswith("~") or fpath.suffix == ".tmp":
            continue
        if str(fpath.resolve()) in already_emitted:
            continue
        if is_intermediate(fpath, fingerprints, workspace_dir):
            continue
        artifacts.append(_make_artifact(tool_call_id, fpath, ws))

    return artifacts


def _detect_changed_files(
    tool_call_id: str,
    pre_files: dict[Path, tuple[int, int]] | None,
    post_files: dict[Path, tuple[int, int]] | None,
    already_emitted: set[str],
    workspace_dir: str,
    *,
    content: str = "",
) -> list[ArtifactEvent]:
    """Report changed files only when this tool returned their exact paths.

    A shared-workspace diff is not evidence of who produced a file. Require
    the current result to name the absolute or workspace-relative path; never
    infer ownership from a directory, a nested basename, or a sidecar alone.
    """
    if not content or pre_files is None or post_files is None:
        return []
    changed_files = {
        path
        for path, signature in post_files.items()
        if pre_files.get(path) != signature
    }
    if not changed_files:
        return []

    ws = Path(workspace_dir).resolve()
    fingerprints = _publication_fingerprints(workspace_dir)
    artifacts: list[ArtifactEvent] = []
    for file_path in sorted(changed_files):
        try:
            relative = file_path.resolve().relative_to(ws).as_posix()
        except (OSError, RuntimeError, ValueError):
            continue
        paths = {str(file_path.resolve()), relative}
        if os.name == "nt":
            paths.add(relative.replace("/", "\\"))
            paths.add(file_path.resolve().as_posix())
        if not any(
            re.search(r"(?<![\w./\\-])" + re.escape(path) + r"(?![\w./\\-])", content)
            for path in paths
        ):
            continue
        if (
            file_path.name.startswith(".")
            or file_path.name.startswith("~")
            or file_path.suffix == ".tmp"
        ):
            continue
        if str(file_path.resolve()) in already_emitted:
            continue
        if is_intermediate(file_path, fingerprints, workspace_dir):
            continue
        artifacts.append(_make_artifact(tool_call_id, file_path, ws))
    return artifacts


def _detect_regex_artifacts(
    tool_call_id: str,
    tool_name: str,
    content: str,
    raw_output: Any,
    workspace_dir: str,
) -> tuple[list[ArtifactEvent], set[str]]:
    """Layer-1 (regex) artifacts for one tool result.

    Returns the regex-detected artifacts plus the set of absolute paths that
    should be excluded from the later diff layer (those already surfaced here,
    or carried on an artifact/intermediate-asset ``raw_output``).
    """
    regex_artifacts = _detect_artifacts(
        tool_call_id,
        tool_name,
        content,
        workspace_dir,
    )
    already = {a.abs_path for a in regex_artifacts}
    if isinstance(raw_output, dict) and raw_output.get("type") in (
        "artifact",
        "intermediate_asset",
    ):
        raw_type = raw_output["type"]
        workspace_root = Path(workspace_dir).resolve()
        raw_path: str | None = None
        for key in ("abs_path", "absolute_path", "path"):
            value = raw_output.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                candidate = Path(value).expanduser()
                if key != "path" and not candidate.is_absolute():
                    continue
                if not candidate.is_absolute():
                    candidate = (workspace_root / candidate).resolve()
                else:
                    candidate = candidate.resolve()
                candidate.relative_to(workspace_root)
                if candidate.is_file():
                    raw_path = str(candidate)
                    break
            except (OSError, RuntimeError, ValueError):
                continue
        if raw_path:
            already.add(raw_path)
            if raw_type == "intermediate_asset" or is_intermediate(
                Path(raw_path), _publication_fingerprints(workspace_dir), workspace_dir
            ):
                regex_artifacts = [
                    artifact for artifact in regex_artifacts if artifact.abs_path != raw_path
                ]
                return regex_artifacts, already
        raw_description = raw_output.get("description") or raw_output.get("alt_text")
        description = raw_description if isinstance(raw_description, str) else None
        if raw_path:
            try:
                normalized_artifact = _make_artifact(
                    tool_call_id,
                    Path(raw_path),
                    workspace_root,
                    description=description,
                )
            except (OSError, RuntimeError, ValueError):
                return regex_artifacts, already
            regex_artifacts = [
                artifact
                for artifact in regex_artifacts
                if artifact.abs_path != normalized_artifact.abs_path
            ]
            regex_artifacts.append(normalized_artifact)
    return regex_artifacts, already


def _detect_tool_artifacts(
    tool_call_id: str,
    tool_name: str,
    content: str,
    raw_output: Any,
    pre_files: dict[Path, tuple[int, int]] | None,
    post_files: dict[Path, tuple[int, int]] | None,
    workspace_dir: str,
) -> list[ArtifactEvent]:
    """Discover only files identified by the current tool result.

    Layer 1 (regex): scan ``content`` for ``[filename.ext]`` references that
    resolve under the session cwd. Layer 2 accepts other exact output paths
    corroborated by a pre/post signature change. Workspace changes alone never
    establish task ownership, including files with publication sidecars.
    """
    regex_artifacts, already = _detect_regex_artifacts(
        tool_call_id, tool_name, content, raw_output, workspace_dir
    )
    diff_artifacts = _detect_changed_files(
        tool_call_id, pre_files, post_files, already, workspace_dir, content=content
    )
    return [*regex_artifacts, *diff_artifacts]
