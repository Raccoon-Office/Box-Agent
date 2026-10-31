"""Bounded text-file reading tool."""

from __future__ import annotations

import codecs
import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from ...context_resources import (
    CONTEXT_RESOURCE_RAW_KEY,
    ResourceDescriptor,
    classify_read_resource,
)
from ..base import Tool, ToolResult
from ..file_tools import _binary_file_error, _resolve_from_active_root
from ..safety import validate_path_in_workspace

if TYPE_CHECKING:
    from ..permissions import PermissionEngine


DEFAULT_READ_LIMIT = 500
MAX_READ_LINES = 2_000
MAX_READ_CHARS = 100_000
READ_BLOCK_BYTES = 64 * 1024
READ_FRAGMENT_BYTES = 16 * 1024
_BLOCKED_POSIX_DEVICES = {
    "/dev/full", "/dev/null", "/dev/random", "/dev/stdin", "/dev/tty",
    "/dev/urandom", "/dev/zero",
}
_BLOCKED_WINDOWS_DEVICE_NAMES = {
    "aux", "clock$", "con", "nul", "prn",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _normalize_read_pagination(
    offset: int | None,
    limit: int | None,
) -> tuple[int, int]:
    """Return bounded 1-indexed pagination values."""
    normalized_offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 1
    normalized_limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else DEFAULT_READ_LIMIT
    return max(1, normalized_offset), max(1, min(normalized_limit, MAX_READ_LINES))


@dataclass
class _ReadPage:
    lines: list[str]
    source_chars: int
    selected_chars: int
    selected_lines: int
    total_lines: int
    start_byte: int
    content_version: str
    replacement_seen: bool


def _scan_read_page(stream: BinaryIO, offset: int, limit: int) -> _ReadPage:
    """Keep at most one normal page while hashing/counting in bounded pieces."""
    hasher = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    source_chars = selected_chars = selected_lines = total_lines = 0
    start_byte = 0
    byte_offset = 0
    lines: list[str] = []
    line_parts: list[str] = []
    pending_cr = ""
    replacement_seen = False
    selected_started = False
    raw = stream.readline(READ_BLOCK_BYTES)
    while raw:
        following = stream.readline(READ_BLOCK_BYTES)
        line_end = raw.endswith(b"\n") or not following
        text = pending_cr + decoder.decode(raw, final=line_end)
        pending_cr = ""
        if not line_end and text.endswith("\r"):
            text, pending_cr = text[:-1], "\r"
        text = text.replace("\r\n", "\n")
        if line_end and text.endswith("\r"):
            text = text[:-1] + "\n"
        source_chars += len(text)
        replacement_seen |= "\ufffd" in text
        hasher.update(raw)
        line_number = total_lines + 1
        if offset <= line_number < offset + limit:
            if not selected_started:
                start_byte = byte_offset
                selected_started = True
            selected_chars += len(text)
            if selected_chars <= MAX_READ_CHARS:
                line_parts.append(text)
            else:
                # Even an unselected or unterminated huge line must not collect
                # in memory. Byte mode will seek back to the selected start.
                lines.clear()
                line_parts.clear()
            if line_end:
                selected_lines += 1
                if selected_chars <= MAX_READ_CHARS:
                    lines.append("".join(line_parts))
                line_parts.clear()
        if line_end:
            total_lines += 1
            decoder.reset()
        byte_offset += len(raw)
        raw = following
    return _ReadPage(
        lines, source_chars, selected_chars, selected_lines, total_lines,
        start_byte, hasher.hexdigest(), replacement_seen,
    )


def _blocked_device_error(file_path: Path) -> str | None:
    """Reject special device paths before performing any I/O."""
    normalized = file_path.as_posix().casefold()
    if normalized in _BLOCKED_POSIX_DEVICES or normalized.startswith("/dev/fd/"):
        return f"Cannot read device file: {file_path}"
    device_name = file_path.name.split(".", 1)[0].casefold().rstrip(":")
    if device_name in _BLOCKED_WINDOWS_DEVICE_NAMES:
        return f"Cannot read Windows device path: {file_path}"
    return None


def _similar_file_suggestions(file_path: Path, limit: int = 5) -> list[str]:
    """Return deterministic nearby filename suggestions for a missing path."""
    parent = file_path.parent
    try:
        candidates = [candidate for candidate in parent.iterdir() if candidate.is_file()]
    except OSError:
        return []
    wanted_name = file_path.name.casefold()
    wanted_stem = file_path.stem.casefold()

    def score(candidate: Path) -> tuple[int, str]:
        name = candidate.name.casefold()
        stem = candidate.stem.casefold()
        value = 0
        if stem == wanted_stem:
            value = 90
        elif name.startswith(wanted_name) or wanted_name.startswith(name):
            value = 70
        elif wanted_name in name or name in wanted_name:
            value = 60
        else:
            overlap = len(set(wanted_stem) & set(stem))
            value = overlap
        return value, candidate.name

    ranked = sorted(candidates, key=lambda candidate: (-score(candidate)[0], score(candidate)[1]))
    return [str(candidate) for candidate in ranked[:limit] if score(candidate)[0] > 0]


class ReadTool(Tool):
    """Read file content."""

    aliases = ("read",)
    max_result_size_chars = math.inf

    def __init__(
        self,
        workspace_dir: str = ".",
        allow_full_access: bool = True,
        permission_engine: PermissionEngine | None = None,
        relative_root_dir: str | None = None,
    ):
        """Initialize ReadTool with workspace directory.

        Args:
            workspace_dir: Security boundary for filesystem access
            allow_full_access: If False, restrict reads to workspace directory
            permission_engine: If provided, use capability-based permission checks
            relative_root_dir: Optional base directory for resolving relative paths
        """
        self.workspace_dir = Path(workspace_dir).absolute()
        self.relative_root_dir = (
            Path(relative_root_dir).absolute() if relative_root_dir else self.workspace_dir
        )
        self.allow_full_access = allow_full_access
        self._perm = permission_engine

    def _resolve_file_path(self, path: str) -> Path:
        file_path = _resolve_from_active_root(
            path,
            workspace_dir=self.workspace_dir,
            relative_root_dir=self.relative_root_dir,
        )
        if not file_path.exists() and not Path(path).is_absolute():
            workspace_candidate = self.workspace_dir / path
            if workspace_candidate.exists():
                return workspace_candidate
        return file_path

    def _validate_readable_file(
        self, file_path: Path, requested_path: str, *, text_only: bool = True,
    ) -> ToolResult | None:
        if self._perm:
            decision = self._perm.check(
                capability="filesystem.read",
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

        device_error = _blocked_device_error(file_path)
        if device_error:
            return ToolResult(success=False, content="", error=device_error)
        if not file_path.exists():
            suggestions = _similar_file_suggestions(file_path)
            suggestion_text = f" Did you mean: {', '.join(suggestions)}" if suggestions else ""
            return ToolResult(
                success=False,
                content="",
                error=f"File not found: {requested_path}.{suggestion_text}",
            )
        if not file_path.is_file():
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Path is not a file: {requested_path}. Use search_files with "
                    "target='files' to inspect a directory."
                ),
            )
        if text_only:
            binary_error = _binary_file_error(file_path)
            if binary_error:
                return ToolResult(success=False, content="", error=binary_error)
        return None

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read a text file with line numbers and bounded pagination. Use this instead of "
            "cat/head/tail or shell commands that print file contents. Output uses "
            "'LINE_NUMBER|LINE_CONTENT' (1-indexed). The default page is 500 lines and the "
            "maximum is 2000; use offset and limit to continue through large files. "
            "For JSONL logs, especially those with large records, use query_jsonl to filter "
            "and project fields instead of returning raw records. "
            "For .eml emails use read_email to decode MIME bodies and attachments. "
            "Binary and structured document files are rejected with an actionable error. "
            "Call it repeatedly to read different files or page through one file. "
            "For large text or long lines, set byte_offset (zero-based) to read up to 16 KiB "
            "without line numbering. Continue with next_byte_offset until it is null. "
            "Byte mode overrides offset/limit and reads toward EOF. Oversized line pages "
            "automatically return the first byte chunk from their starting line."
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
                "offset": {
                    "type": "integer",
                    "description": "Starting line number (1-indexed, default: 1)",
                    "default": 1,
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum lines to read (default: 500, max: 2000)",
                    "default": DEFAULT_READ_LIMIT,
                    "minimum": 1,
                    "maximum": MAX_READ_LINES,
                },
                "refresh": {
                    "type": "boolean",
                    "description": (
                        "Force the exact page back into model context even when the same "
                        "file version and line range are already available"
                    ),
                    "default": False,
                },
                "byte_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Starting byte (zero-based) for a text chunk of up to 16 KiB. "
                        "Use the returned next_byte_offset to continue, including mid-line. "
                        "Overrides offset/limit."
                    ),
                },
            },
            "required": ["path"],
        }

    def _read_fragment_result(
        self, file_path: Path, stream: BinaryIO, byte_offset: int,
    ) -> ToolResult:
        size = os.fstat(stream.fileno()).st_size
        stream.seek(byte_offset)
        raw = stream.read(READ_FRAGMENT_BYTES)
        # Leave an incomplete UTF-8 character for the next chunk. Byte mode
        # preserves original newlines, so CRLF needs no cross-chunk state.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(raw, final=byte_offset + len(raw) >= size)
        pending = decoder.getstate()[0]
        if pending:
            raw = raw[:-len(pending)]
        end_byte = byte_offset + len(raw)
        next_byte_offset = end_byte if raw and end_byte < size else None
        hint = (
            f"Continue with the same path and byte_offset={next_byte_offset}."
            if next_byte_offset is not None else "End of file."
        )
        if file_path.suffix.casefold() in {".jsonl", ".ndjson"}:
            hint += " For JSONL fields/filtering, use query_jsonl with fields/where."
        elif file_path.suffix.casefold() == ".eml":
            hint += " Use read_email to decode email bodies and attachments."
        if "\ufffd" in text:
            hint += " Non-UTF-8 bytes were replaced with \\ufffd."
        # A fragment cannot claim full-line resource coverage: doing so would
        # let context deduplication suppress later parts of the same line.
        return ToolResult(
            success=True,
            content=(
                f"[File fragment: bytes {byte_offset}:{end_byte} of {size} "
                "(zero-based, end exclusive; byte mode reads toward EOF).]\n"
                f"{text}\n[Hint: {hint}]"
            ),
            raw_output={
                "source_size_bytes": size,
                "selected_char_count": len(text),
                "start_byte": byte_offset, "end_byte": end_byte,
                "partial": byte_offset > 0 or next_byte_offset is not None,
                "truncated": False, "has_more": next_byte_offset is not None,
                "next_byte_offset": next_byte_offset,
            },
        )

    async def execute(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
        refresh: bool = False,
        byte_offset: int | None = None,
    ) -> ToolResult:
        """Execute read file."""
        try:
            offset, limit = _normalize_read_pagination(offset, limit)
            file_path = self._resolve_file_path(path)
            validation_error = self._validate_readable_file(file_path, path)
            if validation_error:
                return validation_error

            start = offset - 1
            end = start + limit
            with open(file_path, "rb") as stream:
                if byte_offset is not None:
                    if not isinstance(byte_offset, int) or isinstance(byte_offset, bool) or byte_offset < 0:
                        return ToolResult(success=False, error="byte_offset must be a non-negative integer.")
                    return self._read_fragment_result(file_path, stream, byte_offset)
                page = _scan_read_page(stream, offset, limit)
                if page.selected_chars > MAX_READ_CHARS:
                    return self._read_fragment_result(file_path, stream, page.start_byte)

            selected_lines = page.lines
            selected_char_count = page.selected_chars
            selected_line_count = page.selected_lines
            total_lines = page.total_lines

            # Format with line numbers (1-indexed)
            numbered_lines: list[str] = []
            for i, line in enumerate(selected_lines, start=start + 1):
                # Remove trailing newline for formatting
                line_content = line.rstrip("\n")
                numbered_lines.append(f"{i:6d}|{line_content}")

            if page.replacement_seen:
                numbered_lines.insert(
                    0,
                    "[Warning: File contains non-UTF-8 bytes; invalid bytes were replaced with \ufffd.]",
                )
            if file_path.suffix.casefold() == ".eml":
                numbered_lines.insert(
                    0, "[Raw email source: use read_email to decode headers, body and attachments for analysis.]",
                )
            content = "\n".join(numbered_lines)
            has_more = end < total_lines
            if has_more:
                next_offset = offset + selected_line_count
                content += (
                    f"\n\n[Hint: showing lines {offset}-{offset + selected_line_count - 1} "
                    f"of {total_lines}. Use offset={next_offset}, limit={limit} to continue.]"
                )

            descriptor = ResourceDescriptor(
                resource_id=str(file_path.resolve()),
                content_version=page.content_version,
                start_line=offset,
                end_line=(offset + selected_line_count - 1),
                total_lines=total_lines,
                resource_class=classify_read_resource(
                    str(file_path.resolve()),
                    requested_path=path,
                ),
            )
            return ToolResult(
                success=True,
                content=content,
                raw_output={
                    "source_char_count": page.source_chars,
                    "selected_char_count": selected_char_count,
                    "selected_line_count": selected_line_count,
                    "total_lines": total_lines,
                    "truncated": False,
                    "has_more": has_more,
                    "next_offset": offset + selected_line_count if has_more else None,
                    CONTEXT_RESOURCE_RAW_KEY: descriptor.as_raw_output(),
                },
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))
