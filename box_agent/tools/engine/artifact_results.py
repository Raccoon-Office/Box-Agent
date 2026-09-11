"""Existing tool-output and workspace-diff artifact detection."""

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

    artifacts: list[ArtifactEvent] = []
    seen_paths: set[Path] = set()
    for match in _ARTIFACT_REF_RE.finditer(content):
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


def _snapshot_workspace(workspace_dir: str) -> set[Path] | None:
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
                if entry.name.startswith(".") or entry.suffix == ".tmp":
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
    artifacts: list[ArtifactEvent] = []
    for fpath in sorted(new_files):
        if fpath.name.startswith(".") or fpath.name.startswith("~") or fpath.suffix == ".tmp":
            continue
        if str(fpath.resolve()) in already_emitted:
            continue
        artifacts.append(_make_artifact(tool_call_id, fpath, ws))

    return artifacts


def _detect_changed_files(
    tool_call_id: str,
    pre_files: dict[Path, tuple[int, int]] | None,
    post_files: dict[Path, tuple[int, int]] | None,
    already_emitted: set[str],
    workspace_dir: str,
) -> list[ArtifactEvent]:
    """Create ArtifactEvents for files that appeared or changed."""
    if pre_files is None or post_files is None:
        return []
    changed_files = {
        path
        for path, signature in post_files.items()
        if pre_files.get(path) != signature
    }
    if not changed_files:
        return []

    ws = Path(workspace_dir).resolve()
    artifacts: list[ArtifactEvent] = []
    for file_path in sorted(changed_files):
        if (
            file_path.name.startswith(".")
            or file_path.name.startswith("~")
            or file_path.suffix == ".tmp"
        ):
            continue
        if str(file_path.resolve()) in already_emitted:
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
    or carried on an artifact/intermediate-asset ``raw_output``). Intermediate
    assets are also excluded from regex publication while remaining on disk.
    """
    regex_artifacts = _detect_artifacts(
        tool_call_id,
        tool_name,
        content,
        workspace_dir,
    )
    already = {a.abs_path for a in regex_artifacts}
    if isinstance(raw_output, dict) and raw_output.get("type") in ("artifact", "intermediate_asset"):
        raw_paths: set[str] = set()
        for key in ("abs_path", "absolute_path"):
            raw_path = raw_output.get(key)
            if isinstance(raw_path, str) and raw_path.strip():
                raw_paths.add(str(Path(raw_path).expanduser().resolve()))
        already.update(raw_paths)
        if raw_output.get("type") == "intermediate_asset":
            regex_artifacts = [a for a in regex_artifacts if a.abs_path not in raw_paths]
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
    """Two-layer artifact detection for a single tool result (sequential path).

    Layer 1 (regex): scan ``content`` for ``[filename.ext]`` references that
    resolve under the session cwd. Layer 2 (diff): catch files created or
    modified by the tool that weren't referenced in the output text, using a
    per-tool pre/post signature snapshot. The parallel branch can't take per-tool snapshots under
    concurrency, so it composes :func:`_detect_regex_artifacts` per result with
    a single diff pass instead (see the parallel block in ``run_agent_loop``).
    """
    regex_artifacts, already = _detect_regex_artifacts(
        tool_call_id, tool_name, content, raw_output, workspace_dir
    )
    diff_artifacts = _detect_changed_files(
        tool_call_id, pre_files, post_files, already, workspace_dir
    )
    return [*regex_artifacts, *diff_artifacts]
