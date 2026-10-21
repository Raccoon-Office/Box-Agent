"""Streaming JSONL query tool."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from ..base import ToolResult
from .read_tool import MAX_READ_CHARS, ReadTool


DEFAULT_JSONL_LIMIT = 20
MAX_JSONL_RESULTS = 100
DEFAULT_JSONL_SCAN_LIMIT = 500
MAX_JSONL_SCAN_RECORDS = 5_000
MAX_JSONL_RECORD_BYTES = 8_000_000
MAX_JSONL_RECORD_OUTPUT_CHARS = 8_000
MAX_JSONL_OUTPUT_CHARS = 80_000

def _normalize_jsonl_pagination(
    limit: int | None,
    scan_limit: int | None,
) -> tuple[int, int]:
    normalized_limit = (
        limit if isinstance(limit, int) and not isinstance(limit, bool) else DEFAULT_JSONL_LIMIT
    )
    normalized_scan_limit = (
        scan_limit
        if isinstance(scan_limit, int) and not isinstance(scan_limit, bool)
        else DEFAULT_JSONL_SCAN_LIMIT
    )
    return (
        max(1, min(normalized_limit, MAX_JSONL_RESULTS)),
        max(1, min(normalized_scan_limit, MAX_JSONL_SCAN_RECORDS)),
    )


def _jsonl_source_id(file_path: Path, *, device: int, inode: int) -> str:
    identity = f"{file_path.resolve()}\0{device}\0{inode}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(identity).hexdigest()[:16]


def _jsonl_cursor_checksum(source_id: str, byte_offset: int, line_number: int) -> str:
    value = f"{source_id}:{byte_offset}:{line_number}".encode("ascii")
    return hashlib.sha256(value).hexdigest()[:12]


def _encode_jsonl_cursor(byte_offset: int, line_number: int, source_id: str) -> str:
    checksum = _jsonl_cursor_checksum(source_id, byte_offset, line_number)
    return f"v1:{byte_offset}:{line_number}:{source_id}:{checksum}"


def _decode_jsonl_cursor(cursor: str | None) -> tuple[int, int, str | None] | None:
    if cursor is None:
        return 0, 1, None
    parts = cursor.split(":")
    if len(parts) != 5 or parts[0] != "v1":
        return None
    try:
        byte_offset = int(parts[1])
        line_number = int(parts[2])
    except ValueError:
        return None
    if byte_offset < 0 or line_number < 1:
        return None
    source_id = parts[3]
    if not source_id or parts[4] != _jsonl_cursor_checksum(
        source_id,
        byte_offset,
        line_number,
    ):
        return None
    return byte_offset, line_number, source_id


_JSONL_MISSING = object()


def _bounded_jsonl_key(value: Any, max_chars: int = 200) -> str:
    key = str(value)
    if len(key) <= max_chars:
        return key
    return key[:max_chars] + "..."


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    """Resolve one RFC 6901 JSON pointer without copying the source value."""
    if not pointer.startswith("/"):
        return _JSONL_MISSING
    current = value
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _JSONL_MISSING
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return _JSONL_MISSING
            current = current[index]
        else:
            return _JSONL_MISSING
    return current


def _jsonl_value_summary(value: Any, *, string_preview: int = 400) -> Any:
    if isinstance(value, str):
        if len(value) <= string_preview:
            return value
        return {
            "$truncated": True,
            "$type": "string",
            "characters": len(value),
            "preview": value[:string_preview],
        }
    if isinstance(value, dict):
        return {
            "$summarized": True,
            "$type": "object",
            "size": len(value),
            "keys": [_bounded_jsonl_key(key) for key in list(value)[:20]],
        }
    if isinstance(value, list):
        return {
            "$summarized": True,
            "$type": "array",
            "size": len(value),
        }
    return value


def _compact_jsonl_projection(value: Any) -> tuple[Any, bool]:
    """Return a valid JSON value that fits one record's model-output budget."""
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return _jsonl_value_summary(str(value)), True
    if len(rendered) <= MAX_JSONL_RECORD_OUTPUT_CHARS:
        return value, False
    if isinstance(value, dict):
        preview = [
            {"key": _bounded_jsonl_key(key), "value": _jsonl_value_summary(item)}
            for key, item in list(value.items())[:20]
        ]
        return {
            "$truncated": True,
            "$type": "object",
            "size": len(value),
            "keys": [_bounded_jsonl_key(key) for key in list(value)[:50]],
            "preview": preview,
        }, True
    if isinstance(value, list):
        return {
            "$truncated": True,
            "$type": "array",
            "size": len(value),
            "preview": [_jsonl_value_summary(item) for item in value[:5]],
        }, True
    return _jsonl_value_summary(value), True


def _summarize_jsonl_record(record: Any) -> tuple[Any, bool]:
    if not isinstance(record, dict):
        return _compact_jsonl_projection(record)
    summary = {
        _bounded_jsonl_key(key): _jsonl_value_summary(value)
        for key, value in record.items()
    }
    return summary, any(
        isinstance(value, dict) and (value.get("$truncated") or value.get("$summarized"))
        for value in summary.values()
    )


def _read_bounded_jsonl_line(stream) -> tuple[bytes, bool]:
    """Read one record without ever retaining an unbounded physical line."""
    raw_line = stream.readline(MAX_JSONL_RECORD_BYTES + 1)
    if len(raw_line) <= MAX_JSONL_RECORD_BYTES:
        return raw_line, False
    preview = raw_line[:4_096]
    while raw_line and not raw_line.endswith(b"\n"):
        raw_line = stream.readline(MAX_JSONL_RECORD_BYTES + 1)
    return preview, True

class JsonlQueryTool(ReadTool):
    """Stream, filter, and project JSONL records without exposing raw large lines."""

    aliases = ()
    parallel_safe = True
    max_result_size_chars = math.inf

    @property
    def name(self) -> str:
        return "query_jsonl"

    @property
    def description(self) -> str:
        return (
            "Stream and inspect a JSONL/NDJSON file with bounded structured output. Use this "
            "instead of read_file or execute_code when JSONL records may be large. Optionally "
            "filter exact scalar values with where (RFC 6901 JSON Pointer keys) and return only "
            "selected fields. Every fields entry and where key must start with '/' (no leading whitespace): use '/id', "
            "not 'id' or '#id'; use '/data/error' for a nested field. "
            'Example: {"path":"events.jsonl","fields":["/id","/data/error"],"where":{"/id":42}}. '
            "Results remain valid JSON; oversized values are replaced by "
            "explicit summaries with original sizes. Results are parsed data, not original source text. "
            f"For source lines within read_file's {MAX_READ_CHARS:,}-character output limit "
            "(including the newline), copy verbatim with offset set to the returned record's line "
            "and limit=1. If a one-line read exceeds that limit, use another permitted file-copy "
            "method without printing the record, or report the limit; do not reserialize query "
            "data as the original text. "
            "Continue with next_cursor until has_more "
            "is false. The source file is never modified."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        scalar_schema = {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "null"},
            ]
        }
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to a .jsonl/.ndjson file",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^/"},
                    "description": (
                        "Optional RFC 6901 JSON Pointers to return. Each must start with '/' "
                        "without leading whitespace: "
                        "use '/id', not 'id' or '#id'. Nested fields use '/data/error', not "
                        "'data.error'. Example: ['/event', '/timestamp', '/data/error']"
                    ),
                    "maxItems": 50,
                },
                "where": {
                    "type": "object",
                    "description": (
                        "Optional exact scalar filters keyed by JSON Pointer. Every key must "
                        "start with '/' without leading whitespace: for id=42 use {'/id': 42}, not {'id': 42} or "
                        "{'#id': 42}. Nested example: {'/data/success': false}"
                    ),
                    "additionalProperties": scalar_schema,
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque continuation cursor returned by a previous call",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Maximum matching records to return (default: {DEFAULT_JSONL_LIMIT}, "
                        f"max: {MAX_JSONL_RESULTS})"
                    ),
                    "minimum": 1,
                    "maximum": MAX_JSONL_RESULTS,
                    "default": DEFAULT_JSONL_LIMIT,
                },
                "scan_limit": {
                    "type": "integer",
                    "description": (
                        "Maximum physical records to scan in one call when filters are sparse "
                        f"(default: {DEFAULT_JSONL_SCAN_LIMIT}, max: {MAX_JSONL_SCAN_RECORDS})"
                    ),
                    "minimum": 1,
                    "maximum": MAX_JSONL_SCAN_RECORDS,
                    "default": DEFAULT_JSONL_SCAN_LIMIT,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        fields: list[str] | None = None,
        where: dict[str, Any] | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        scan_limit: int | None = None,
    ) -> ToolResult:
        try:
            limit, scan_limit = _normalize_jsonl_pagination(limit, scan_limit)
            decoded_cursor = _decode_jsonl_cursor(cursor)
            if decoded_cursor is None:
                return ToolResult(
                    success=False,
                    error=(
                        "Invalid JSONL cursor. Omit cursor to start from the beginning or reuse "
                        "next_cursor exactly as returned by query_jsonl."
                    ),
                )
            byte_offset, line_number, cursor_source_id = decoded_cursor
            fields = list(fields or [])
            where = dict(where or {})
            invalid_pointers = [
                pointer
                for pointer in [*fields, *where]
                if not isinstance(pointer, str) or not pointer.startswith("/")
            ]
            if invalid_pointers:
                return ToolResult(
                    success=False,
                    error=(
                        "fields and where keys must be RFC 6901 JSON Pointers beginning with '/'. "
                        f"Invalid values: {invalid_pointers[:5]}"
                    ),
                )
            if len(fields) > 50:
                return ToolResult(success=False, error="query_jsonl accepts at most 50 fields.")

            file_path = self._resolve_file_path(path)
            validation_error = self._validate_readable_file(file_path, path)
            if validation_error:
                return validation_error
            if file_path.suffix.casefold() not in {".jsonl", ".ndjson"}:
                return ToolResult(
                    success=False,
                    error=(
                        f"query_jsonl requires a .jsonl or .ndjson file: {path}. "
                        "Use read_file for ordinary text files."
                    ),
                )

            records: list[dict[str, Any]] = []
            parse_errors: list[dict[str, Any]] = []
            parse_error_count = 0
            oversized_record_count = 0
            scanned_records = 0
            returned_chars = 0
            truncated_fields: set[str] = set()
            next_byte_offset = byte_offset
            next_line_number = line_number

            with file_path.open("rb") as stream:
                file_stat = os.fstat(stream.fileno())
                file_size = file_stat.st_size
                source_id = _jsonl_source_id(
                    file_path,
                    device=file_stat.st_dev,
                    inode=file_stat.st_ino,
                )
                if cursor_source_id is not None and cursor_source_id != source_id:
                    return ToolResult(
                        success=False,
                        error=(
                            "JSONL cursor belongs to a different or replaced file. Restart "
                            "without cursor so records cannot be skipped or mislabeled."
                        ),
                    )
                if byte_offset > file_size:
                    return ToolResult(
                        success=False,
                        error=(
                            f"JSONL cursor offset {byte_offset:,} is beyond the current file size "
                            f"{file_size:,}. Restart without cursor because the file changed."
                        ),
                    )
                if byte_offset:
                    stream.seek(byte_offset - 1)
                    if stream.read(1) != b"\n":
                        return ToolResult(
                            success=False,
                            error=(
                                "JSONL cursor does not point to a record boundary. Restart without "
                                "cursor because the file changed or the cursor was modified."
                            ),
                        )
                stream.seek(byte_offset)

                while scanned_records < scan_limit and len(records) < limit:
                    record_offset = stream.tell()
                    current_line_number = next_line_number
                    raw_line, oversized = _read_bounded_jsonl_line(stream)
                    if not raw_line:
                        next_byte_offset = stream.tell()
                        break

                    scanned_records += 1
                    next_byte_offset = stream.tell()
                    next_line_number += 1
                    record_cursor = _encode_jsonl_cursor(
                        record_offset,
                        current_line_number,
                        source_id,
                    )

                    if oversized:
                        oversized_record_count += 1
                        parse_error_count += 1
                        if len(parse_errors) < 20:
                            parse_errors.append(
                                {
                                    "line": current_line_number,
                                    "cursor": record_cursor,
                                    "code": "RECORD_TOO_LARGE",
                                    "max_record_bytes": MAX_JSONL_RECORD_BYTES,
                                }
                            )
                        continue

                    try:
                        decoded_line = raw_line.decode("utf-8")
                        record = json.loads(decoded_line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        parse_error_count += 1
                        if len(parse_errors) < 20:
                            parse_errors.append(
                                {
                                    "line": current_line_number,
                                    "cursor": record_cursor,
                                    "code": "INVALID_JSONL_RECORD",
                                    "error": str(exc),
                                    "preview": raw_line[:500].decode("utf-8", errors="replace"),
                                }
                            )
                        continue

                    if any(
                        _resolve_json_pointer(record, pointer) is _JSONL_MISSING
                        or _resolve_json_pointer(record, pointer) != expected
                        for pointer, expected in where.items()
                    ):
                        continue

                    record_truncated_fields: list[str] = []
                    if fields:
                        projected: dict[str, Any] = {}
                        for pointer in fields:
                            value = _resolve_json_pointer(record, pointer)
                            if value is _JSONL_MISSING:
                                projected[pointer] = {"$missing": True}
                                continue
                            compacted, was_truncated = _compact_jsonl_projection(value)
                            projected[pointer] = compacted
                            if was_truncated:
                                record_truncated_fields.append(pointer)
                        record_data: Any = projected
                    else:
                        record_data, was_truncated = _summarize_jsonl_record(record)
                        if was_truncated:
                            record_truncated_fields.append("/")

                    item: dict[str, Any] = {
                        "line": current_line_number,
                        "cursor": record_cursor,
                        "data": record_data,
                    }
                    if record_truncated_fields:
                        item["truncated_fields"] = record_truncated_fields
                    rendered_item = json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if records and returned_chars + len(rendered_item) > MAX_JSONL_OUTPUT_CHARS:
                        scanned_records -= 1
                        next_byte_offset = record_offset
                        next_line_number = current_line_number
                        break
                    records.append(item)
                    truncated_fields.update(record_truncated_fields)
                    returned_chars += len(rendered_item)

            has_more = next_byte_offset < file_size
            next_cursor = (
                _encode_jsonl_cursor(next_byte_offset, next_line_number, source_id)
                if has_more
                else None
            )
            payload = {
                "records": records,
                "page": {
                    "returned_records": len(records),
                    "scanned_records": scanned_records,
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                    "projection_truncated": bool(truncated_fields),
                    "truncated_fields": sorted(truncated_fields),
                },
                "parse_errors": parse_errors,
                "parse_error_count": parse_error_count,
            }
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            return ToolResult(
                success=True,
                content=content,
                raw_output={
                    "source_size_bytes": file_size,
                    "returned_records": len(records),
                    "scanned_records": scanned_records,
                    "parse_error_count": parse_error_count,
                    "oversized_record_count": oversized_record_count,
                    "projection_truncated": bool(truncated_fields),
                    "truncated_fields": sorted(truncated_fields),
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                },
            )
        except Exception as exc:
            return ToolResult(success=False, content="", error=str(exc))
