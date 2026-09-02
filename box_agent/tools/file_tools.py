"""File operation tools."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from ..events import ProgressEvent
from ..model_history import is_model_history_placeholder
from .base import EventEmittingTool, Tool, ToolResult
from .argument_limits import MAX_GENERATED_BODY_CHARS
from .file.path_candidates import home_relative_path_candidates
from .pptx_safety import detect_pptx_self_check_bypass
from .safety import backup_file, validate_path_in_workspace

if TYPE_CHECKING:
    from .permissions import PermissionEngine


_log = logging.getLogger(__name__)


MAX_FILE_TOOL_CONTENT_CHARS = MAX_GENERATED_BODY_CHARS
MAX_FILE_TOOL_CONTENT_CHARS_DISPLAY = f"{MAX_FILE_TOOL_CONTENT_CHARS:,}"
MAX_WRITE_FILE_BYTES = 10 * 1024 * 1024
MAX_WRITE_FILE_CHUNKS = 2_048
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_OFFSET = 10_000
MAX_SEARCH_OUTPUT_CHARS = 50_000
SEARCH_OUTPUT_HINT_RESERVE_CHARS = 1_000
DEFAULT_SEARCH_TIMEOUT_SECONDS = 60.0
SEARCH_HEARTBEAT_SECONDS = 10.0
_BINARY_EXTENSIONS = {
    ".7z", ".avi", ".bin", ".bmp", ".class", ".dll", ".dmg", ".doc",
    ".docx", ".exe", ".gif", ".gz", ".ico", ".jar", ".jpeg", ".jpg",
    ".mov", ".mp3", ".mp4", ".pdf", ".png", ".ppt", ".pptx", ".pyc",
    ".so", ".tar", ".tif", ".tiff", ".webp", ".xls", ".xlsx", ".zip",
}
def _normalize_search_pagination(offset: int | None, limit: int | None) -> tuple[int, int]:
    normalized_offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
    normalized_limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else DEFAULT_SEARCH_LIMIT
    return max(0, normalized_offset), max(1, min(normalized_limit, MAX_SEARCH_RESULTS))


def _binary_file_error(file_path: Path) -> str | None:
    """Return an actionable error for binary files, otherwise None."""
    suffix = file_path.suffix.casefold()
    if suffix in _BINARY_EXTENSIONS:
        if suffix in {".docx", ".xlsx", ".pptx", ".pdf"}:
            return (
                f"Cannot read structured binary file '{file_path.name}' with read_file. "
                "Use execute_code with the appropriate document library."
            )
        return f"Cannot read binary file '{file_path.name}' with read_file."
    try:
        with file_path.open("rb") as stream:
            sample = stream.read(8_192)
    except OSError:
        return None
    if b"\x00" in sample:
        return f"Cannot read binary file '{file_path.name}' with read_file."
    return None


def _resolve_from_active_root(
    path: str,
    *,
    workspace_dir: Path,
    relative_root_dir: Path,
) -> Path:
    """Resolve canonical artifact paths and legacy workspace-relative paths."""
    file_path = Path(path).expanduser()
    if file_path.is_absolute():
        return file_path

    try:
        root_from_workspace = relative_root_dir.relative_to(workspace_dir)
    except ValueError:
        root_from_workspace = None
    if (
        root_from_workspace
        and file_path.parts[: len(root_from_workspace.parts)] == root_from_workspace.parts
    ):
        return workspace_dir / file_path
    return relative_root_dir / file_path


def _model_history_placeholder_error(*values: str) -> str | None:
    """Reject internal history placeholders before they reach real files."""
    for value in values:
        if is_model_history_placeholder(value):
            return (
                "Refusing to write a model-history placeholder to disk. "
                "Regenerate the real file content, or read the existing file with read_file before editing."
            )
    return None


def _oversized_file_tool_argument_error(tool_name: str, argument_name: str, value: str) -> str | None:
    """Reject large generated bodies before they encourage provider-side truncation."""
    if len(value) <= MAX_FILE_TOOL_CONTENT_CHARS:
        return None
    return (
        f"FILE_TOOL_ARGUMENT_TOO_LARGE: {tool_name}.{argument_name} is "
        f"{len(value)} characters; limit is {MAX_FILE_TOOL_CONTENT_CHARS}. "
        "Split this append/edit operation into smaller calls. To replace a complete "
        "large artifact atomically, use ordered write_file chunks and validate with "
        "read_file or a render check."
    )


def __getattr__(name: str) -> Any:
    """Preserve legacy imports while file tool implementations live in a package."""
    if name == "ReadTool":
        from .file.read_tool import ReadTool

        return ReadTool
    if name == "JsonlQueryTool":
        from .file.jsonl_tool import JsonlQueryTool

        return JsonlQueryTool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class SearchFilesTool(EventEmittingTool):
    """Search file names or text content without routing through a shell."""

    parallel_safe = True
    cancel_on_agent_cancel = True
    max_result_size_chars = math.inf

    def __init__(
        self,
        workspace_dir: str = ".",
        allow_full_access: bool = True,
        permission_engine: PermissionEngine | None = None,
        relative_root_dir: str | None = None,
        search_timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
        heartbeat_seconds: float = SEARCH_HEARTBEAT_SECONDS,
    ):
        super().__init__()
        self.workspace_dir = Path(workspace_dir).absolute()
        self.relative_root_dir = (
            Path(relative_root_dir).absolute() if relative_root_dir else self.workspace_dir
        )
        self.allow_full_access = allow_full_access
        self._perm = permission_engine
        self.search_timeout_seconds = max(0.01, float(search_timeout_seconds))
        self.heartbeat_seconds = max(0.01, float(heartbeat_seconds))

    @property
    def name(self) -> str:
        return "search_files"

    @property
    def description(self) -> str:
        return (
            "Search file contents or find files by name. Use this instead of grep/rg/find/ls "
            "in bash. target='content' performs a regular-expression text search; "
            "target='files' finds files by glob pattern and is the correct way to inspect "
            "a directory. Results are bounded by count and total characters and support "
            "offset/limit pagination."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex for content search or glob pattern for file search",
                },
                "target": {
                    "type": "string",
                    "enum": ["content", "files"],
                    "default": "content",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search (default: active workspace root)",
                    "default": ".",
                },
                "file_glob": {
                    "type": "string",
                    "description": "Optional glob limiting files during content search",
                },
                "limit": {
                    "type": "integer",
                    "default": DEFAULT_SEARCH_LIMIT,
                    "minimum": 1,
                    "maximum": MAX_SEARCH_RESULTS,
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "maximum": MAX_SEARCH_OFFSET,
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_only", "count"],
                    "default": "content",
                },
                "context": {
                    "type": "integer",
                    "description": "Context lines before and after content matches (max: 10)",
                    "default": 0,
                    "minimum": 0,
                    "maximum": 10,
                },
            },
            "required": ["pattern"],
        }

    def _resolve_path(self, path: str) -> Path:
        search_path = _resolve_from_active_root(
            path,
            workspace_dir=self.workspace_dir,
            relative_root_dir=self.relative_root_dir,
        )
        if not search_path.exists() and not Path(path).is_absolute():
            workspace_candidate = self.workspace_dir / path
            if workspace_candidate.exists():
                return workspace_candidate
        return search_path

    def _permission_error(self, search_path: Path) -> ToolResult | None:
        if self._perm:
            decision = self._perm.check(
                capability="filesystem.read",
                resource={"path": str(search_path)},
                tool_name=self.name,
            )
            if not decision.allowed:
                return ToolResult(
                    success=False,
                    error=decision.reason,
                    permission_request=decision.permission_request,
                )
        elif not self.allow_full_access:
            error = validate_path_in_workspace(search_path, self.workspace_dir)
            if error:
                return ToolResult(success=False, error=error)
        return None

    def _can_inspect_home_path_candidates(self) -> bool:
        """Return whether the current policy already permits reading Home."""

        return self._permission_error(Path.home().resolve()) is None

    async def execute_with_event_context(
        self,
        *,
        event_queue: asyncio.Queue,
        parent_tool_call_id: str,
        **kwargs: Any,
    ) -> ToolResult:
        """Use per-call event state so parallel searches cannot race."""
        return await self.execute(
            **kwargs,
            _event_queue=event_queue,
            _parent_tool_call_id=parent_tool_call_id,
        )

    def _iter_files(
        self,
        search_path: Path,
        *,
        stop_event: threading.Event,
        deadline: float,
    ) -> Iterator[Path]:
        if search_path.is_file():
            if not stop_event.is_set() and time.monotonic() < deadline:
                yield search_path
            return
        for current_root, directories, filenames in os.walk(search_path, followlinks=False):
            if stop_event.is_set() or time.monotonic() >= deadline:
                return
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in {".git", ".hg", ".svn", "node_modules", "__pycache__"}
            )
            root_path = Path(current_root)
            for filename in sorted(filenames):
                if stop_event.is_set() or time.monotonic() >= deadline:
                    return
                yield root_path / filename

    def _file_allowed(self, file_path: Path) -> bool:
        if not self._perm:
            return True
        return self._perm.check(
            capability="filesystem.read",
            resource={"path": str(file_path)},
            tool_name=self.name,
        ).allowed

    @staticmethod
    def _line_text(line: str) -> str:
        return line if len(line) <= 2_000 else line[:2_000] + "... [line truncated]"

    def _search_sync(
        self,
        *,
        pattern: str,
        target: str,
        search_path: Path,
        file_glob: str | None,
        limit: int,
        offset: int,
        output_mode: str,
        context: int,
        result_char_budget: int,
        stop_event: threading.Event,
        deadline: float,
    ) -> dict[str, Any]:
        """Run a cooperative, streaming search outside the asyncio loop."""
        expression = re.compile(pattern) if target == "content" else None
        base = search_path if search_path.is_dir() else search_path.parent
        matches: list[str] = []
        selected_chars = 0
        scanned_files = 0
        matched_results = 0
        has_more = False
        output_limited = False

        def stopped() -> bool:
            return stop_event.is_set() or time.monotonic() >= deadline

        def add_result(value: str) -> bool:
            """Discard skipped matches and retain only one bounded result page."""
            nonlocal matched_results, has_more, output_limited, selected_chars
            matched_results += 1
            if matched_results <= offset:
                return False
            if len(matches) >= limit:
                has_more = True
                return True

            separator_chars = 1 if matches else 0
            remaining = result_char_budget - selected_chars - separator_chars
            if len(value) > remaining:
                output_limited = True
                has_more = True
                if not matches and remaining > 0:
                    marker = "\n...[match truncated to search_files output budget]"
                    keep = max(0, remaining - len(marker))
                    matches.append(value[:keep] + marker)
                    selected_chars += separator_chars + len(matches[-1])
                return True

            matches.append(value)
            selected_chars += separator_chars + len(value)
            return False

        for file_path in self._iter_files(
            search_path,
            stop_event=stop_event,
            deadline=deadline,
        ):
            if stopped():
                break
            if not self._file_allowed(file_path):
                continue
            scanned_files += 1
            relative = file_path.relative_to(base).as_posix()

            if target == "files":
                if fnmatch(file_path.name, pattern) or fnmatch(relative, pattern):
                    if add_result(relative):
                        break
                continue

            if file_glob and not (
                fnmatch(file_path.name, file_glob) or fnmatch(relative, file_glob)
            ):
                continue
            if _binary_file_error(file_path):
                continue

            file_match_count = 0
            try:
                if context > 0:
                    # Context rendering needs neighbouring lines, but remains
                    # bounded to one file rather than retaining the whole tree.
                    lines = file_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    line_source = enumerate(lines)
                else:
                    lines = None
                    stream = file_path.open(encoding="utf-8", errors="replace")
                    line_source = enumerate(stream)

                try:
                    for line_index, raw_line in line_source:
                        if stopped():
                            break
                        line = raw_line.rstrip("\r\n")
                        if expression is None or not expression.search(line):
                            continue
                        file_match_count += 1
                        if output_mode == "count":
                            continue
                        if output_mode == "files_only":
                            add_result(relative)
                            break

                        if lines is None:
                            rendered = f"{relative}:{line_index + 1}:>{self._line_text(line)}"
                        else:
                            first = max(0, line_index - context)
                            last = min(len(lines), line_index + context + 1)
                            rendered_lines = []
                            for context_index in range(first, last):
                                marker = ">" if context_index == line_index else " "
                                rendered_lines.append(
                                    f"{relative}:{context_index + 1}:{marker}"
                                    f"{self._line_text(lines[context_index])}"
                                )
                            rendered = "\n".join(rendered_lines)
                        if add_result(rendered):
                            break
                finally:
                    if lines is None:
                        stream.close()
            except OSError:
                continue

            if output_mode == "count" and file_match_count and not stopped():
                add_result(f"{relative}:{file_match_count}")

            if has_more:
                break

        timed_out = not stop_event.is_set() and time.monotonic() >= deadline
        exact_total = not has_more and not timed_out

        return {
            "selected": matches,
            "matched_results": matched_results,
            "scanned_files": scanned_files,
            "has_more": has_more,
            "output_limited": output_limited,
            "timed_out": timed_out,
            "cancelled": stop_event.is_set(),
            "exact_total": exact_total,
        }

    async def execute(
        self,
        pattern: str,
        target: str = "content",
        path: str = ".",
        file_glob: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        output_mode: str = "content",
        context: int = 0,
        *,
        _event_queue: asyncio.Queue | None = None,
        _parent_tool_call_id: str | None = None,
    ) -> ToolResult:
        """Execute a bounded file-name or content search."""
        try:
            if not isinstance(pattern, str) or not pattern:
                return ToolResult(success=False, error="search_files requires a non-empty pattern")
            if target not in {"content", "files"}:
                return ToolResult(success=False, error="target must be 'content' or 'files'")
            if output_mode not in {"content", "files_only", "count"}:
                return ToolResult(success=False, error="Invalid output_mode")
            if (
                isinstance(offset, int)
                and not isinstance(offset, bool)
                and offset > MAX_SEARCH_OFFSET
            ):
                return ToolResult(
                    success=False,
                    error=(
                        f"offset must be at most {MAX_SEARCH_OFFSET:,}; narrow the path or "
                        "pattern instead of skipping an unbounded result set"
                    ),
                )
            offset, limit = _normalize_search_pagination(offset, limit)
            context = max(0, min(context if isinstance(context, int) else 0, 10))
            search_path = self._resolve_path(path)
            user_home = Path.home().resolve()
            if (
                search_path.resolve() == user_home
                and self.workspace_dir.resolve() != user_home
            ):
                return ToolResult(
                    success=False,
                    error=(
                        "BROAD_HOME_SEARCH_BLOCKED: Recursive search from the entire user "
                        "home is not allowed. Choose a specific likely directory such as "
                        "~/Downloads or ~/Documents, or ask the user for the location."
                    ),
                )
            denied = self._permission_error(search_path)
            if denied:
                return denied
            if not search_path.exists():
                candidates = (
                    home_relative_path_candidates(
                        path,
                        active_roots=(self.relative_root_dir, self.workspace_dir),
                    )
                    if self._can_inspect_home_path_candidates()
                    else []
                )
                error = f"Search path not found: {path}"
                if candidates:
                    rendered = "\n".join(
                        f"- `{candidate['path']}` ({candidate['basis']})"
                        for candidate in candidates
                    )
                    error += (
                        "\nStructurally matching existing path candidate(s):\n"
                        f"{rendered}\n"
                        "If a candidate matches the user's intent, retry search_files "
                        "with this absolute path. Permissions will be checked on that call."
                    )
                return ToolResult(
                    success=False,
                    error=error,
                    raw_output={
                        "code": "PATH_NOT_FOUND",
                        "path": path,
                        "path_candidates": candidates,
                    },
                )

            if target == "content":
                try:
                    re.compile(pattern)
                except re.error as exc:
                    return ToolResult(success=False, error=f"Invalid search regex: {exc}")

            stop_event = threading.Event()
            deadline = time.monotonic() + self.search_timeout_seconds
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._search_sync,
                    pattern=pattern,
                    target=target,
                    search_path=search_path,
                    file_glob=file_glob,
                    limit=limit,
                    offset=offset,
                    output_mode=output_mode,
                    context=context,
                    result_char_budget=(
                        MAX_SEARCH_OUTPUT_CHARS - SEARCH_OUTPUT_HINT_RESERVE_CHARS
                    ),
                    stop_event=stop_event,
                    deadline=deadline,
                )
            )
            queue = _event_queue if _event_queue is not None else self._event_queue
            try:
                scan: dict[str, Any] | None = None
                while not worker.done():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        stop_event.set()
                        worker.cancel()
                        scan = {
                            "selected": [],
                            "matched_results": 0,
                            "scanned_files": 0,
                            "has_more": False,
                            "output_limited": False,
                            "timed_out": True,
                            "cancelled": False,
                            "exact_total": False,
                        }
                        break
                    done, _pending = await asyncio.wait(
                        {worker}, timeout=min(self.heartbeat_seconds, remaining)
                    )
                    if done:
                        break
                    if queue is not None:
                        queue.put_nowait(
                            ProgressEvent(
                                step=0,
                                content=(
                                    f"search_files is still scanning {search_path}; "
                                    "the search remains cancellable."
                                ),
                            )
                        )
                if scan is None:
                    scan = await worker
            except asyncio.CancelledError:
                stop_event.set()
                worker.cancel()
                raise

            selected = scan["selected"]
            output_limited = bool(scan.get("output_limited", False))
            truncated = scan["has_more"] or scan["timed_out"]
            content = "\n".join(selected)
            if not content:
                content = (
                    f"Search timed out after {self.search_timeout_seconds:g} seconds before "
                    "finding a match. Narrow the path, pattern, or file_glob."
                    if scan["timed_out"]
                    else "No matches found."
                )
            if scan["has_more"]:
                next_offset = offset + len(selected)
                limit_label = (
                    "output budget reached; " if output_limited else ""
                )
                content += (
                    f"\n\n[Hint: {limit_label}showing results "
                    f"{offset + 1}-{offset + len(selected)} with more available. "
                    f"Use offset={next_offset}, limit={limit} to continue.]"
                )
            if scan["timed_out"] and selected:
                content += (
                    f"\n\n[Warning: search timed out after {self.search_timeout_seconds:g} seconds. "
                    "Partial results are shown; narrow the search before retrying.]"
                )
            if len(content) > MAX_SEARCH_OUTPUT_CHARS:
                content = content[:MAX_SEARCH_OUTPUT_CHARS]

            limit_reason = None
            if scan["timed_out"]:
                limit_reason = "search_timeout"
            elif output_limited:
                limit_reason = "output_budget"
            elif scan["has_more"]:
                limit_reason = "result_limit"

            return ToolResult(
                success=True,
                content=content,
                raw_output={
                    "target": target,
                    "path": str(search_path),
                    "total_matches": (
                        scan["matched_results"] if scan["exact_total"] else None
                    ),
                    "matched_through": scan["matched_results"],
                    "total_is_exact": scan["exact_total"],
                    "returned_matches": len(selected),
                    "truncated": truncated,
                    "next_offset": offset + len(selected) if truncated else None,
                    "scanned_files": scan["scanned_files"],
                    "timed_out": scan["timed_out"],
                    "output_limited": output_limited,
                    "output_chars": len(content),
                    "max_output_chars": MAX_SEARCH_OUTPUT_CHARS,
                    "limit_reason": limit_reason,
                },
            )
        except Exception as exc:
            return ToolResult(success=False, content="", error=str(exc))


@dataclass(slots=True)
class _PendingTextWrite:
    """One path-keyed write transaction hidden from the model interface."""

    target: Path
    temporary: Path
    next_index: int = 0
    size_bytes: int = 0
    chunk_hashes: dict[int, str] = field(default_factory=dict)


@dataclass(slots=True)
class _CommittedTextWrite:
    """Terminal receipt retained for idempotent final-chunk replay."""

    final_chunk_index: int
    final_chunk_sha256: str
    file_sha256: str
    result: ToolResult


class WriteTool(Tool):
    """Atomically write a UTF-8 file in one call or ordered chunks."""

    aliases = ("write",)

    def __init__(
        self,
        workspace_dir: str = ".",
        allow_full_access: bool = True,
        permission_engine: PermissionEngine | None = None,
        relative_root_dir: str | None = None,
    ):
        """Initialize WriteTool with workspace directory.

        Args:
            workspace_dir: Security boundary for filesystem access
            allow_full_access: If False, restrict writes to workspace directory
            permission_engine: If provided, use capability-based permission checks
            relative_root_dir: Optional base directory for resolving relative paths
        """
        self.workspace_dir = Path(workspace_dir).absolute()
        self.relative_root_dir = (
            Path(relative_root_dir).absolute() if relative_root_dir else self.workspace_dir
        )
        self.allow_full_access = allow_full_access
        self._perm = permission_engine
        self._pending: dict[Path, _PendingTextWrite] = {}
        self._committed: dict[Path, _CommittedTextWrite] = {}

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Atomically write UTF-8 text to a file, replacing any existing file. "
            "Small files need only path and content. For a large file, send ordered "
            "chunks to the same path. Start a new transaction with chunk_index=0 and "
            "final=false; after an accepted chunk, continue from the next_chunk_index "
            "returned by that successful result. Set final=true on the last chunk. "
            "The target remains unchanged until the final chunk commits. Transactions "
            f"support at most {MAX_WRITE_FILE_CHUNKS:,} chunks and "
            f"{MAX_WRITE_FILE_BYTES:,} total UTF-8 bytes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Prefer a path relative to the active project/artifact root "
                        f"({self.relative_root_dir}). Absolute paths are used exactly "
                        "as supplied."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Complete file content or the current ordered chunk.",
                },
                "chunk_index": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Zero-based chunk sequence number (default: 0).",
                },
                "final": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Commit after this content. Omit or use true for a complete "
                        "small file; use false until the last chunk of a large file."
                    ),
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def _target(self, path: str) -> Path:
        return _resolve_from_active_root(
            path,
            workspace_dir=self.workspace_dir,
            relative_root_dir=self.relative_root_dir,
        ).resolve(strict=False)

    def _permission_error(self, target: Path) -> ToolResult | None:
        if self._perm:
            decision = self._perm.check(
                capability="filesystem.write",
                resource={"path": str(target)},
                tool_name=self.name,
            )
            if not decision.allowed:
                return ToolResult(
                    success=False,
                    error=decision.reason,
                    permission_request=decision.permission_request,
                )
        elif not self.allow_full_access:
            error = validate_path_in_workspace(target, self.workspace_dir)
            if error:
                return ToolResult(success=False, error=error)
        return None

    @staticmethod
    def _write_bytes(path: Path, data: bytes, *, append: bool) -> None:
        with path.open("ab" if append else "wb") as stream:
            written = stream.write(data)
            if written != len(data):
                raise OSError(
                    f"short write: expected {len(data)} bytes, wrote {written}"
                )
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _restore_temporary_size(path: Path, size: int) -> None:
        """Roll a failed append back to the last accepted byte boundary."""
        with path.open("r+b") as stream:
            stream.truncate(size)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _start(self, target: Path) -> _PendingTextWrite:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.box-agent-{uuid.uuid4().hex}.part"
        try:
            self._write_bytes(temporary, b"", append=False)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        state = _PendingTextWrite(target=target, temporary=temporary)
        self._committed.pop(target, None)
        self._pending[target] = state
        return state

    def _discard(self, state: _PendingTextWrite) -> str | None:
        """End a transaction even when its internal temp file cannot be removed."""
        cleanup_error: str | None = None
        try:
            state.temporary.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_error = str(exc)
            _log.warning(
                "write_file/transaction_temp_cleanup_failed path=%s error=%s",
                state.temporary,
                exc,
            )
        finally:
            self._pending.pop(state.target, None)
        return cleanup_error

    @staticmethod
    def _active_transaction_output(
        state: _PendingTextWrite,
        *,
        output_type: str = "write_file_transaction_active",
    ) -> dict[str, Any]:
        return {
            "type": output_type,
            "path": str(state.target),
            "transaction_state": "active",
            "next_chunk_index": state.next_index,
            "size_bytes": state.size_bytes,
            "chunks": state.next_index,
            "final": False,
        }

    def _active_result(
        self,
        state: _PendingTextWrite,
        result: ToolResult,
    ) -> ToolResult:
        """Attach the authoritative retained-transaction state to a result."""
        raw_output = (
            dict(result.raw_output) if isinstance(result.raw_output, dict) else {}
        )
        transaction_output = self._active_transaction_output(state)
        transaction_output.update(raw_output)
        transaction_output["transaction_state"] = "active"
        transaction_output["path"] = str(state.target)
        transaction_output["next_chunk_index"] = state.next_index
        transaction_output["size_bytes"] = state.size_bytes
        transaction_output["chunks"] = state.next_index
        transaction_output["final"] = False
        return result.model_copy(update={"raw_output": transaction_output})

    def _discarded_result(
        self,
        state: _PendingTextWrite,
        error: str,
        *,
        reason: str,
        final: bool = True,
    ) -> ToolResult:
        """Discard a terminally invalid write and expose the transaction outcome."""
        raw_output = {
            "type": "write_file_transaction_discarded",
            "path": str(state.target),
            "transaction_state": "discarded",
            "transaction_discarded": True,
            "reason": reason,
            "next_chunk_index": state.next_index,
            "size_bytes": state.size_bytes,
            "chunks": state.next_index,
            "final": final,
        }
        cleanup_error = self._discard(state)
        if cleanup_error is not None:
            raw_output["cleanup_error"] = cleanup_error
        return ToolResult(
            success=False,
            error=error,
            raw_output=raw_output,
        )

    def _append_chunk(
        self,
        state: _PendingTextWrite,
        chunk_index: int,
        data: bytes,
        *,
        final: bool,
    ) -> ToolResult | None:
        try:
            temporary_size = state.temporary.stat().st_size
        except OSError as exc:
            return self._discarded_result(
                state,
                f"WRITE_FILE_TRANSACTION_TEMPORARY_UNAVAILABLE: {exc}",
                reason="transaction_temporary_unavailable",
                final=final,
            )
        if temporary_size != state.size_bytes:
            return self._discarded_result(
                state,
                (
                    "WRITE_FILE_TRANSACTION_SIZE_MISMATCH: expected "
                    f"{state.size_bytes} bytes, found {temporary_size}."
                ),
                reason="transaction_size_mismatch",
                final=final,
            )
        digest = hashlib.sha256(data).hexdigest()
        if chunk_index < state.next_index:
            if state.chunk_hashes.get(chunk_index) == digest:
                return ToolResult(
                    success=True,
                    content=(
                        f"Chunk {chunk_index} was already accepted for {state.target}; "
                        f"next chunk_index={state.next_index}."
                    ),
                    raw_output={
                        "type": "write_file_chunk",
                        "path": str(state.target),
                        "transaction_state": "active",
                        "chunk_index": chunk_index,
                        "next_chunk_index": state.next_index,
                        "size_bytes": state.size_bytes,
                        "duplicate": True,
                        "final": False,
                    },
                )
            return self._active_result(
                state,
                ToolResult(
                    success=False,
                    error=(
                        "WRITE_FILE_CHUNK_CONFLICT: chunk_index="
                        f"{chunk_index} was already accepted with different content."
                    ),
                ),
            )
        if chunk_index > state.next_index:
            return self._active_result(
                state,
                ToolResult(
                    success=False,
                    error=(
                        f"WRITE_FILE_CHUNK_OUT_OF_ORDER: expected {state.next_index}, "
                        f"got {chunk_index}."
                    ),
                ),
            )
        if state.next_index >= MAX_WRITE_FILE_CHUNKS:
            return self._discarded_result(
                state,
                (
                    "WRITE_FILE_TOO_MANY_CHUNKS: limit is "
                    f"{MAX_WRITE_FILE_CHUNKS:,} chunks."
                ),
                reason="chunk_limit_reached",
                final=final,
            )
        if state.size_bytes + len(data) > MAX_WRITE_FILE_BYTES:
            return self._active_result(
                state,
                ToolResult(
                    success=False,
                    error=(
                        "WRITE_FILE_TOTAL_SIZE_EXCEEDED: limit is "
                        f"{MAX_WRITE_FILE_BYTES:,} UTF-8 bytes."
                    ),
                ),
            )
        try:
            self._write_bytes(state.temporary, data, append=True)
        except OSError as exc:
            try:
                self._restore_temporary_size(state.temporary, state.size_bytes)
            except OSError as rollback_exc:
                return self._discarded_result(
                    state,
                    (
                        f"WRITE_FILE_FAILED: {exc}; transaction rollback failed: "
                        f"{rollback_exc}"
                    ),
                    reason="chunk_write_rollback_failed",
                    final=final,
                )
            return self._active_result(
                state,
                ToolResult(success=False, error=f"WRITE_FILE_FAILED: {exc}"),
            )
        state.chunk_hashes[chunk_index] = digest
        state.next_index += 1
        state.size_bytes += len(data)
        return None

    def _commit(self, state: _PendingTextWrite) -> ToolResult:
        if error := self._permission_error(state.target):
            return self._active_result(state, error)
        try:
            assembled_content = state.temporary.read_text(encoding="utf-8")
        except UnicodeError as exc:
            return self._discarded_result(
                state,
                f"WRITE_FILE_TRANSACTION_INVALID_UTF8: {exc}",
                reason="transaction_content_invalid_utf8",
            )
        placeholder_error = _model_history_placeholder_error(assembled_content)
        if placeholder_error:
            return self._discarded_result(
                state,
                placeholder_error,
                reason="model_history_placeholder",
            )
        bypass_error = detect_pptx_self_check_bypass(
            str(state.target), assembled_content
        )
        if bypass_error:
            return self._discarded_result(
                state,
                bypass_error,
                reason="pptx_self_check_bypass",
            )
        digest = self._sha256_file(state.temporary)
        backup_path = backup_file(state.target)
        os.replace(state.temporary, state.target)
        self._pending.pop(state.target, None)
        result = ToolResult(
            success=True,
            content=(
                f"Successfully wrote {state.target} "
                f"({state.size_bytes} bytes, sha256={digest})."
            ),
            raw_output={
                "type": "artifact",
                "path": str(state.target),
                "transaction_state": "committed",
                "size_bytes": state.size_bytes,
                "sha256": digest,
                "chunks": state.next_index,
                "final": True,
                "backup_path": str(backup_path) if backup_path is not None else None,
            },
        )
        final_chunk_index = state.next_index - 1
        self._committed[state.target] = _CommittedTextWrite(
            final_chunk_index=final_chunk_index,
            final_chunk_sha256=state.chunk_hashes[final_chunk_index],
            file_sha256=digest,
            result=result.model_copy(deep=True),
        )
        return result

    def _committed_replay(
        self,
        target: Path,
        chunk_index: int,
        data: bytes,
        *,
        final: bool,
    ) -> ToolResult | None:
        if not final:
            return None
        receipt = self._committed.get(target)
        if receipt is None or chunk_index != receipt.final_chunk_index:
            return None
        digest = hashlib.sha256(data).hexdigest()
        if digest == receipt.final_chunk_sha256:
            target_matches = (
                target.is_file()
                and self._sha256_file(target) == receipt.file_sha256
            )
            if not target_matches:
                if chunk_index > 0:
                    return ToolResult(
                        success=False,
                        error=(
                            "WRITE_FILE_COMMITTED_STATE_CHANGED: the committed target "
                            "no longer matches the final receipt."
                        ),
                        raw_output={
                            "type": "write_file_committed_state_changed",
                            "path": str(target),
                            "transaction_state": "none",
                            "final": True,
                        },
                    )
                return None
            return receipt.result.model_copy(deep=True)
        if chunk_index > 0:
            return ToolResult(
                success=False,
                error=(
                    "WRITE_FILE_FINAL_CHUNK_CONFLICT: committed chunk_index="
                    f"{chunk_index} had different content."
                ),
                raw_output={
                    "type": "write_file_final_chunk_conflict",
                    "path": str(target),
                    "transaction_state": "none",
                    "final": True,
                },
            )
        return None

    async def execute(
        self,
        path: str,
        content: str,
        chunk_index: int = 0,
        final: bool = True,
    ) -> ToolResult:
        """Write one complete file or advance a path-keyed chunk transaction."""
        state: _PendingTextWrite | None = None
        try:
            target = self._target(path)
            state = self._pending.get(target)
            if error := self._permission_error(target):
                return self._active_result(state, error) if state is not None else error
            data = content.encode("utf-8")
            if state is None:
                replay = self._committed_replay(
                    target,
                    chunk_index,
                    data,
                    final=final,
                )
                if replay is not None:
                    return replay
                if chunk_index != 0:
                    return ToolResult(
                        success=False,
                        error=(
                            "WRITE_FILE_TRANSACTION_NOT_FOUND: no active write for "
                            f"{target}; start with chunk_index=0."
                        ),
                        raw_output={
                            "type": "write_file_transaction_missing",
                            "path": str(target),
                            "transaction_state": "none",
                            "final": final,
                        },
                    )
                state = self._start(target)
                started = True
            else:
                started = False

            append_result = self._append_chunk(
                state,
                chunk_index,
                data,
                final=final,
            )
            if append_result is not None:
                if (
                    started
                    and state.next_index == 0
                    and self._pending.get(state.target) is state
                ):
                    return self._discarded_result(
                        state,
                        append_result.error or "The initial write chunk was rejected.",
                        reason="initial_chunk_rejected",
                        final=final,
                    )
                return append_result
            if final:
                return self._commit(state)
            return ToolResult(
                success=True,
                content=(
                    f"Accepted chunk {chunk_index} for {target}; "
                    f"next chunk_index={state.next_index}."
                ),
                raw_output={
                    "type": "write_file_chunk",
                    "path": str(target),
                    "transaction_state": "active",
                    "chunk_index": chunk_index,
                    "next_chunk_index": state.next_index,
                    "size_bytes": state.size_bytes,
                    "duplicate": False,
                    "final": False,
                },
            )
        except (OSError, UnicodeError) as exc:
            result = ToolResult(success=False, error=f"WRITE_FILE_FAILED: {exc}")
            if state is not None and self._pending.get(state.target) is state:
                return self._active_result(state, result)
            return result

    def discard_pending_writes(self, *, reason: str) -> list[dict[str, Any]]:
        """Discard live transactions and return structured lifecycle records."""
        discarded: list[dict[str, Any]] = []
        for state in list(self._pending.values()):
            raw_output = {
                "type": "write_file_transaction_discarded",
                "path": str(state.target),
                "transaction_state": "discarded",
                "transaction_discarded": True,
                "reason": reason,
                "next_chunk_index": state.next_index,
                "size_bytes": state.size_bytes,
                "chunks": state.next_index,
                "final": False,
            }
            cleanup_error = self._discard(state)
            if cleanup_error is not None:
                raw_output["cleanup_error"] = cleanup_error
            discarded.append(raw_output)
        return discarded

    def cleanup_pending_writes(self) -> list[str]:
        """Discard incomplete chunk transactions at the end of a session turn."""
        cleaned = [
            str(record["path"])
            for record in self.discard_pending_writes(reason="turn_cleanup")
        ]
        self._committed.clear()
        return cleaned


class AppendTool(Tool):
    """Append content to a file."""

    def __init__(
        self,
        workspace_dir: str = ".",
        allow_full_access: bool = True,
        permission_engine: PermissionEngine | None = None,
        relative_root_dir: str | None = None,
    ):
        """Initialize AppendTool with workspace directory."""
        self.workspace_dir = Path(workspace_dir).absolute()
        self.relative_root_dir = (
            Path(relative_root_dir).absolute() if relative_root_dir else self.workspace_dir
        )
        self.allow_full_access = allow_full_access
        self._perm = permission_engine

    @property
    def name(self) -> str:
        return "append_file"

    @property
    def description(self) -> str:
        return (
            "Append content to a file, creating it if it does not exist. "
            f"Keep each content chunk under {MAX_FILE_TOOL_CONTENT_CHARS_DISPLAY} "
            "characters; use multiple append calls when needed. To atomically replace "
            "a complete large artifact, use ordered write_file chunks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file",
                },
                "content": {
                    "type": "string",
                    "maxLength": MAX_FILE_TOOL_CONTENT_CHARS,
                    "description": (
                        "Content chunk to append. Keep this under "
                        f"{MAX_FILE_TOOL_CONTENT_CHARS_DISPLAY} characters. "
                        "Use multiple append calls when needed, or ordered write_file "
                        "chunks to atomically replace a complete artifact."
                    ),
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str) -> ToolResult:
        """Execute append file."""
        try:
            file_path = _resolve_from_active_root(
                path,
                workspace_dir=self.workspace_dir,
                relative_root_dir=self.relative_root_dir,
            )

            if self._perm:
                decision = self._perm.check(
                    capability="filesystem.write",
                    resource={"path": str(file_path)},
                    tool_name=self.name,
                )
                if not decision.allowed:
                    return ToolResult(
                        success=False,
                        error=decision.reason,
                        permission_request=decision.permission_request,
                    )
            elif not self.allow_full_access:
                error = validate_path_in_workspace(file_path, self.workspace_dir)
                if error:
                    return ToolResult(success=False, content="", error=error)

            placeholder_error = _model_history_placeholder_error(content)
            if placeholder_error:
                return ToolResult(success=False, content="", error=placeholder_error)

            size_error = _oversized_file_tool_argument_error(self.name, "content", content)
            if size_error:
                return ToolResult(success=False, content="", error=size_error)

            existing = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
            bypass_error = detect_pptx_self_check_bypass(
                str(file_path), f"{existing}\n{content}"
            )
            if bypass_error:
                return ToolResult(success=False, content="", error=bypass_error)

            backup_file(file_path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with file_path.open("a", encoding="utf-8") as f:
                f.write(content)
            return ToolResult(success=True, content=f"Successfully appended to {file_path}")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))


class EditTool(Tool):
    """Edit file by replacing text."""

    aliases = ("edit",)

    def __init__(
        self,
        workspace_dir: str = ".",
        allow_full_access: bool = True,
        permission_engine: PermissionEngine | None = None,
        relative_root_dir: str | None = None,
    ):
        """Initialize EditTool with workspace directory.

        Args:
            workspace_dir: Security boundary for filesystem access
            allow_full_access: If False, restrict edits to workspace directory
            permission_engine: If provided, use capability-based permission checks
            relative_root_dir: Optional base directory for resolving relative paths
        """
        self.workspace_dir = Path(workspace_dir).absolute()
        self.relative_root_dir = (
            Path(relative_root_dir).absolute() if relative_root_dir else self.workspace_dir
        )
        self.allow_full_access = allow_full_access
        self._perm = permission_engine

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Perform exact string replacement in a file. The old_str must match exactly "
            "and appear uniquely in the file, otherwise the operation will fail. "
            "You must read the file first before editing. Preserve exact indentation from the source."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file",
                },
                "old_str": {
                    "type": "string",
                    "maxLength": MAX_FILE_TOOL_CONTENT_CHARS,
                    "description": "Exact string to find and replace (must be unique in file)",
                },
                "new_str": {
                    "type": "string",
                    "maxLength": MAX_FILE_TOOL_CONTENT_CHARS,
                    "description": "Replacement string (use for refactoring, renaming, etc.)",
                },
            },
            "required": ["path", "old_str", "new_str"],
        }

    async def execute(self, path: str, old_str: str, new_str: str) -> ToolResult:
        """Execute edit file."""
        try:
            # Resolve relative paths from the active project/artifact root.
            file_path = _resolve_from_active_root(
                path,
                workspace_dir=self.workspace_dir,
                relative_root_dir=self.relative_root_dir,
            )
            if not file_path.exists() and not Path(path).is_absolute():
                workspace_candidate = self.workspace_dir / path
                if workspace_candidate.exists():
                    file_path = workspace_candidate

            # Path validation
            if self._perm:
                decision = self._perm.check(
                    capability="filesystem.write",
                    resource={"path": str(file_path)},
                    tool_name=self.name,
                )
                if not decision.allowed:
                    return ToolResult(
                        success=False,
                        error=decision.reason,
                        permission_request=decision.permission_request,
                    )
            elif not self.allow_full_access:
                error = validate_path_in_workspace(file_path, self.workspace_dir)
                if error:
                    return ToolResult(success=False, content="", error=error)

            if not file_path.exists():
                return ToolResult(
                    success=False,
                    content="",
                    error=f"File not found: {path}",
                )

            content = file_path.read_text(encoding="utf-8")

            placeholder_error = _model_history_placeholder_error(old_str, new_str)
            if placeholder_error:
                return ToolResult(success=False, content="", error=placeholder_error)

            for argument_name, value in (("old_str", old_str), ("new_str", new_str)):
                size_error = _oversized_file_tool_argument_error(
                    self.name, argument_name, value
                )
                if size_error:
                    return ToolResult(success=False, content="", error=size_error)

            bypass_error = detect_pptx_self_check_bypass(
                str(file_path), f"{content}\n{old_str}\n{new_str}"
            )
            if bypass_error:
                return ToolResult(success=False, content="", error=bypass_error)

            if old_str not in content:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Text not found in file: {old_str}",
                )

            # Backup before editing
            backup_file(file_path)

            new_content = content.replace(old_str, new_str)
            file_path.write_text(new_content, encoding="utf-8")

            return ToolResult(success=True, content=f"Successfully edited {file_path}")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))
