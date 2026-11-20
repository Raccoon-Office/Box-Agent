"""Structured debug logger for ACP server diagnostics.

All output goes to **stderr** (and optionally a log file) so that stdout
remains reserved for ACP/JSON-RPC protocol messages.

Configuration via environment variables:

    BOX_AGENT_LOG_LEVEL   error | warn | info | debug   (default: warn)
    BOX_AGENT_LOG_FILE    /absolute/path/to/file.log    (default: none)
    BOX_AGENT_LOG_FORMAT  text | json                   (default: text)

Usage::

    from box_agent.acp.debug_logger import acp_logger as log

    log.info("session/new", session_id="sess-0", message="Session created")
    log.debug("tool/start", tool_name="bash", tool_call_id="tc-1")
"""

from __future__ import annotations

import json as _json
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

from box_agent.log_files import open_private_log
from box_agent.user_paths import state_path
from box_agent.payload_privacy import diagnostic_event_fields, diagnostic_stream_fields

# ── Level constants ──────────────────────────────────────────

_LEVELS = {"error": 40, "warn": 30, "info": 20, "debug": 10}
_LEVEL_NAMES = {40: "ERROR", 30: "WARN", 20: "INFO", 10: "DEBUG"}

# ── Truncation helper ────────────────────────────────────────

_PREVIEW_LEN = 200


def _preview(value: Any, max_len: int = _PREVIEW_LEN) -> str:
    """Truncate a value for safe logging (no full prompts/tool output)."""
    if value is None:
        return ""
    s = str(value)
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"...({len(s)} chars)"


# ── Logger ───────────────────────────────────────────────────


class ACPDebugLogger:
    """Lightweight structured logger that writes to stderr + optional file."""

    def __init__(self) -> None:
        self._level: int = _LEVELS["warn"]
        self._file_path: str | None = None
        self._file_handle: Any = None
        self._format: str = "text"  # "text" or "json"
        self._configure_from_env()

    def _configure_from_env(self) -> None:
        level_str = os.environ.get("BOX_AGENT_LOG_LEVEL", "warn").lower()
        self._level = _LEVELS.get(level_str, _LEVELS["warn"])

        self._format = os.environ.get("BOX_AGENT_LOG_FORMAT", "text").lower()
        if self._format not in ("text", "json"):
            self._format = "text"

        file_path = os.environ.get("BOX_AGENT_LOG_FILE")
        if file_path:
            file_path = str(state_path("log/box-agent.log", file_path))
            try:
                self._file_handle = open_private_log(file_path, "a", buffering=1)
                self._file_path = file_path
            except Exception:
                # File open failure must not crash the server
                self._write_stderr(
                    f"[BOX-AGENT] WARNING: Cannot open log file {file_path}, file logging disabled\n"
                )

    @property
    def level(self) -> int:
        return self._level

    def reconfigure(self) -> None:
        """Re-read environment variables. Useful after test setup."""
        if self._file_handle:
            try:
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None
            self._file_path = None
        self._configure_from_env()

    # ── Public log methods ───────────────────────────────────

    def debug(self, event: str, **fields: Any) -> None:
        self._log(10, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(20, event, fields)

    def warn(self, event: str, **fields: Any) -> None:
        self._log(30, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._log(40, event, fields)

    def exception(self, event: str, exc: BaseException, **fields: Any) -> None:
        """Log an error with stack trace."""
        fields["error"] = str(exc)
        fields["traceback"] = traceback.format_exc()
        self._log(40, event, fields)

    # ── Internal ─────────────────────────────────────────────

    def _log(self, level: int, event: str, fields: dict[str, Any]) -> None:
        if level < self._level:
            return

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        level_name = _LEVEL_NAMES.get(level, "UNKNOWN")

        fields = diagnostic_event_fields(event, fields)

        # Sanitize: truncate known large fields
        for key in ("message", "content", "prompt", "arguments", "result"):
            if key in fields:
                fields[key] = _preview(fields[key])

        line = self._format_line(ts, level_name, event, fields)
        stream_fields = diagnostic_stream_fields(fields)
        self._write_stderr(
            line if stream_fields is fields else self._format_line(ts, level_name, event, stream_fields)
        )
        self._write_file(line)

    def _format_line(self, ts: str, level: str, event: str, fields: dict[str, Any]) -> str:
        if self._format == "json":
            record = {"timestamp": ts, "level": level, "event": event, **fields}
            return _json.dumps(record, ensure_ascii=False, default=str) + "\n"
        parts = [f"{ts} [{level}] {event}"]
        parts.extend(f"{key}={value}" for key, value in fields.items() if value is not None and value != "")
        return "  ".join(parts) + "\n"

    def _write_stderr(self, line: str) -> None:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass  # never crash the server

    def _write_file(self, line: str) -> None:
        if not self._file_handle:
            return
        try:
            self._file_handle.write(line)
            # line-buffered, but flush explicitly for safety
            self._file_handle.flush()
        except Exception:
            pass  # file write failure must not affect main flow

    def close(self) -> None:
        """Close file handle if open."""
        if self._file_handle:
            try:
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None


# ── Singleton ────────────────────────────────────────────────

acp_logger = ACPDebugLogger()
