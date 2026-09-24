"""Bounded ripgrep-backed search tools for code-agent sessions."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import time
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .base import Tool, ToolResult
from .safety import validate_path_in_workspace

if TYPE_CHECKING:
    from .permissions import PermissionEngine


DEFAULT_RESULT_LIMIT = 100
MAX_RESULT_LIMIT = 200
MAX_OUTPUT_CHARS = 50_000
MAX_LINE_CHARS = 2_000
MAX_RECORD_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 8 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0
_FILTER_TYPE = "boxagent"
_MAX_GLOB_ALTERNATIVES = 256


def _brace_choices(value: str) -> list[str]:
    choices: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(value):
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            choices.append(value[start:index])
            start = index + 1
    choices.append(value[start:])
    return choices


def _expand_braces(pattern: str) -> tuple[str, ...]:
    """Expand ripgrep-style brace alternatives with a defensive size bound."""

    pending = [pattern]
    expanded: list[str] = []
    while pending:
        if len(pending) + len(expanded) > _MAX_GLOB_ALTERNATIVES:
            raise ValueError(
                f"glob pattern exceeds {_MAX_GLOB_ALTERNATIVES} brace alternatives"
            )
        value = pending.pop()
        search_from = 0
        while True:
            opening = value.find("{", search_from)
            if opening < 0:
                expanded.append(value)
                break
            depth = 0
            closing = -1
            for index in range(opening, len(value)):
                if value[index] == "{":
                    depth += 1
                elif value[index] == "}":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        break
            if closing < 0:
                expanded.append(value)
                break
            choices = _brace_choices(value[opening + 1 : closing])
            if len(choices) < 2:
                search_from = closing + 1
                continue
            replacements = [
                value[:opening] + choice + value[closing + 1 :]
                for choice in choices
            ]
            if (
                len(pending) + len(expanded) + len(replacements)
                > _MAX_GLOB_ALTERNATIVES
            ):
                raise ValueError(
                    f"glob pattern exceeds {_MAX_GLOB_ALTERNATIVES} brace alternatives"
                )
            pending.extend(reversed(replacements))
            break
    return tuple(expanded)


def _normalize_glob_pattern(pattern: str) -> str:
    while pattern.startswith("./"):
        pattern = pattern[2:]
    return pattern


def _compile_file_patterns(pattern: str) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(part for part in expanded.split("/") if part)
        for expanded in _expand_braces(_normalize_glob_pattern(pattern))
    )


def _file_filter_args(
    pattern: str, compiled_patterns: tuple[tuple[str, ...], ...]
) -> list[str]:
    """Let ripgrep filter name globs; path globs are filtered after discovery."""

    if any(len(parts) > 1 for parts in compiled_patterns):
        return []
    return [
        f"--type-add={_FILTER_TYPE}:{_normalize_glob_pattern(pattern)}",
        f"--type={_FILTER_TYPE}",
    ]


def _matches_file_pattern(
    raw_path: str, compiled_patterns: tuple[tuple[str, ...], ...]
) -> bool:
    path_parts = tuple(
        part
        for part in raw_path.replace("\\", "/").removeprefix("./").split("/")
        if part
    )
    if not path_parts:
        return False

    for pattern_parts in compiled_patterns:
        if not pattern_parts:
            continue
        if len(pattern_parts) == 1:
            if fnmatchcase(path_parts[-1], pattern_parts[0]):
                return True
            continue

        @lru_cache(maxsize=None)
        def matches(pattern_index: int, path_index: int) -> bool:
            if pattern_index == len(pattern_parts):
                return path_index == len(path_parts)
            part = pattern_parts[pattern_index]
            if part == "**":
                return matches(pattern_index + 1, path_index) or (
                    path_index < len(path_parts)
                    and matches(pattern_index, path_index + 1)
                )
            return (
                path_index < len(path_parts)
                and fnmatchcase(path_parts[path_index], part)
                and matches(pattern_index + 1, path_index + 1)
            )

        if matches(0, 0):
            return True
    return False


def resolve_ripgrep_executable(
    runtime_env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the OfficeV3-injected or system ripgrep executable."""

    configured = (runtime_env or {}).get("BOX_AGENT_RG") or os.environ.get(
        "BOX_AGENT_RG"
    )
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return shutil.which("rg")


class _RipgrepTool(Tool):
    parallel_safe = True
    cancel_on_agent_cancel = True
    max_result_size_chars = math.inf

    def __init__(
        self,
        *,
        workspace_dir: str,
        executable: str,
        allow_full_access: bool = True,
        permission_engine: PermissionEngine | None = None,
        relative_root_dir: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).absolute()
        self.relative_root_dir = (
            Path(relative_root_dir).absolute()
            if relative_root_dir
            else self.workspace_dir
        )
        self.executable = executable
        self.allow_full_access = allow_full_access
        self._perm = permission_engine
        self.timeout_seconds = max(0.01, float(timeout_seconds))

    def _resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        return path.absolute() if path.is_absolute() else self.relative_root_dir / path

    def _permission_error(self, path: Path) -> ToolResult | None:
        if self._perm:
            decision = self._perm.check(
                capability="filesystem.read",
                resource={"path": str(path)},
                tool_name=self.name,
            )
            if not decision.allowed:
                return ToolResult(
                    success=False,
                    error=decision.reason,
                    permission_request=decision.permission_request,
                )
        elif not self.allow_full_access:
            error = validate_path_in_workspace(path, self.workspace_dir)
            if error:
                return ToolResult(success=False, error=error)
        return None

    def _validate_search_path(
        self, raw_path: str, *, require_directory: bool
    ) -> tuple[Path | None, ToolResult | None]:
        search_path = self._resolve_path(raw_path)
        user_home = Path.home().resolve()
        if (
            search_path.resolve() == user_home
            and self.workspace_dir.resolve() != user_home
        ):
            return None, ToolResult(
                success=False,
                error=(
                    "BROAD_HOME_SEARCH_BLOCKED: Recursive search from the entire user "
                    "home is not allowed. Choose a more specific directory."
                ),
            )
        denied = self._permission_error(search_path)
        if denied:
            return None, denied
        if not search_path.exists():
            return None, ToolResult(
                success=False, error=f"Search path not found: {raw_path}"
            )
        if require_directory and not search_path.is_dir():
            return None, ToolResult(
                success=False, error=f"glob path must be a directory: {raw_path}"
            )
        return search_path, None

    def _display_path(self, cwd: Path, raw_path: str) -> str:
        absolute = (cwd / raw_path).absolute()
        try:
            return absolute.relative_to(self.relative_root_dir).as_posix()
        except ValueError:
            return str(absolute)

    @staticmethod
    def _normalize_limit(limit: int) -> int:
        if not isinstance(limit, int):
            return DEFAULT_RESULT_LIMIT
        return max(1, min(limit, MAX_RESULT_LIMIT))

    @staticmethod
    async def _read_stderr(stream: asyncio.StreamReader) -> str:
        retained = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = MAX_STDERR_BYTES - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        suffix = "\n...[stderr truncated]" if truncated else ""
        return bytes(retained).decode("utf-8", errors="replace") + suffix

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _spawn(
        self, *, cwd: Path, args: list[str]
    ) -> tuple[asyncio.subprocess.Process | None, ToolResult | None]:
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_RECORD_BYTES,
            )
        except (FileNotFoundError, OSError) as exc:
            return None, ToolResult(
                success=False, error=f"ripgrep is unavailable: {exc}"
            )
        return process, None


class GlobTool(_RipgrepTool):
    """Find non-ignored files with a native ripgrep glob filter."""

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find files by glob pattern with ripgrep. Patterns are relative to the selected "
            "search path and support forms such as 'src/**/*.ts' and '*.{ts,tsx}'. "
            "Basename-only patterns match files at any depth; this tool does not return "
            "directories. Do not use glob('*') to inspect a directory; use a read-only, "
            "non-recursive Bash directory listing instead. Respects ignore files and "
            "returns bounded paths."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Glob pattern relative to path, such as '**/*.py', "
                        "'src/**/*.ts', or '*.{ts,tsx}'"
                    ),
                },
                "path": {
                    "type": "string",
                    "default": ".",
                    "description": "Directory to search relative to the active project root",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULT_LIMIT,
                    "default": DEFAULT_RESULT_LIMIT,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> ToolResult:
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(success=False, error="glob requires a non-empty pattern")
        search_path, error = self._validate_search_path(path, require_directory=True)
        if error:
            return error
        assert search_path is not None

        try:
            compiled_patterns = _compile_file_patterns(pattern)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        normalized_limit = self._normalize_limit(limit)
        args = [
            "--no-config",
            "--no-messages",
            "--color=never",
            "--files",
            *_file_filter_args(pattern, compiled_patterns),
            "--glob=!**/.git/**",
            ".",
        ]
        process, spawn_error = await self._spawn(cwd=search_path, args=args)
        if spawn_error:
            return spawn_error
        assert process is not None and process.stdout is not None
        assert process.stderr is not None

        stderr_task = asyncio.create_task(self._read_stderr(process.stderr))
        deadline = time.monotonic() + self.timeout_seconds
        files: list[str] = []
        output_chars = 0
        truncated = False
        timed_out = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw_line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                if not raw_line:
                    break
                relative = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not relative:
                    continue
                if not _matches_file_pattern(relative, compiled_patterns):
                    continue
                display_path = self._display_path(search_path, relative)
                if len(files) >= normalized_limit:
                    truncated = True
                    break
                extra_chars = len(display_path) + (1 if files else 0)
                if output_chars + extra_chars > MAX_OUTPUT_CHARS - 200:
                    truncated = True
                    break
                files.append(display_path)
                output_chars += extra_chars

            if truncated or timed_out:
                await self._stop_process(process)
            else:
                await process.wait()
            stderr = await stderr_task
        except asyncio.CancelledError:
            await self._stop_process(process)
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            raise
        finally:
            if process.returncode is None:
                await self._stop_process(process)

        if not truncated and not timed_out and process.returncode not in {0, 1}:
            return ToolResult(
                success=False,
                error=stderr.strip()
                or f"ripgrep failed with exit code {process.returncode}",
            )

        content = "\n".join(files) or "No files found."
        if truncated:
            content += (
                "\n\n[Hint: Results are incomplete because the result limit was reached. "
                "Narrow path or pattern before drawing conclusions. Do not retry with '*' "
                "to inspect directory structure.]"
            )
        if timed_out:
            content += (
                f"\n\n[Warning: glob timed out after {self.timeout_seconds:g} seconds; "
                "partial results are shown.]"
            )
        model_context = None
        if truncated or timed_out:
            reason = "timed out" if timed_out else "result limit reached"
            model_context = (
                f"[Incomplete glob results: {reason}; pattern={pattern}; "
                f"path={search_path}; returned={len(files)}. "
                "These are examples, not all matches. Narrow path or pattern and "
                "search again before drawing conclusions. Do not use '*' for "
                "directory listing.]\n"
                + "\n".join(files[:10])
            )
        return ToolResult(
            success=True,
            content=content,
            model_context=model_context,
            raw_output={
                "path": str(search_path),
                "returned_files": len(files),
                "truncated": truncated or timed_out,
                "files": files,
            },
        )


class GrepTool(_RipgrepTool):
    """Search non-ignored file contents with ripgrep regular expressions."""

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents with ripgrep regular expressions. Optionally filter files "
            "with an include glob relative to the selected search path while retaining "
            "ignore-file semantics. Supports path and brace patterns."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Regular expression to search for",
                },
                "path": {
                    "type": "string",
                    "default": ".",
                    "description": "Directory or file relative to the active project root",
                },
                "include": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional file glob relative to path, such as '*.py', "
                        "'src/**/*.ts', or '*.{ts,tsx}'"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULT_LIMIT,
                    "default": DEFAULT_RESULT_LIMIT,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        include: str | None = None,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> ToolResult:
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(success=False, error="grep requires a non-empty pattern")
        if include is not None and (not isinstance(include, str) or not include):
            return ToolResult(success=False, error="include must be a non-empty string")
        search_path, error = self._validate_search_path(path, require_directory=False)
        if error:
            return error
        assert search_path is not None

        try:
            compiled_include = (
                _compile_file_patterns(include) if include is not None else None
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        normalized_limit = self._normalize_limit(limit)
        cwd = search_path if search_path.is_dir() else search_path.parent
        target = "." if search_path.is_dir() else search_path.name
        filter_args = (
            _file_filter_args(include, compiled_include)
            if include is not None and compiled_include is not None
            else []
        )
        args = [
            "--no-config",
            "--no-messages",
            "--color=never",
            "--json",
            *filter_args,
            "--glob=!**/.git/**",
            "--",
            pattern,
            target,
        ]
        process, spawn_error = await self._spawn(cwd=cwd, args=args)
        if spawn_error:
            return spawn_error
        assert process is not None and process.stdout is not None
        assert process.stderr is not None

        stderr_task = asyncio.create_task(self._read_stderr(process.stderr))
        deadline = time.monotonic() + self.timeout_seconds
        matches: list[dict[str, Any]] = []
        rendered: list[str] = []
        output_chars = 0
        truncated = False
        timed_out = False
        oversized_record = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw_line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                except ValueError:
                    oversized_record = True
                    continue
                if not raw_line:
                    break
                try:
                    event = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if event.get("type") != "match":
                    continue
                data = event.get("data", {})
                raw_path = data.get("path", {}).get("text")
                line_number = data.get("line_number")
                text = data.get("lines", {}).get("text")
                submatches = data.get("submatches") or []
                if (
                    not isinstance(raw_path, str)
                    or not isinstance(line_number, int)
                    or not isinstance(text, str)
                ):
                    continue
                if compiled_include is not None and not _matches_file_pattern(
                    raw_path, compiled_include
                ):
                    continue
                if len(matches) >= normalized_limit:
                    truncated = True
                    break
                preview = text.rstrip("\r\n")
                if len(preview) > MAX_LINE_CHARS:
                    preview = preview[:MAX_LINE_CHARS] + "... [line truncated]"
                first = (
                    submatches[0]
                    if submatches and isinstance(submatches[0], dict)
                    else {}
                )
                start = first.get("start")
                column = start + 1 if isinstance(start, int) else 1
                display_path = self._display_path(cwd, raw_path)
                line = f"{display_path}:{line_number}:{column}:{preview}"
                extra_chars = len(line) + (1 if rendered else 0)
                if output_chars + extra_chars > MAX_OUTPUT_CHARS - 200:
                    truncated = True
                    break
                matches.append(
                    {
                        "path": display_path,
                        "line": line_number,
                        "column": column,
                        "text": preview,
                    }
                )
                rendered.append(line)
                output_chars += extra_chars

            if truncated or timed_out:
                await self._stop_process(process)
            else:
                await process.wait()
            stderr = await stderr_task
        except asyncio.CancelledError:
            await self._stop_process(process)
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            raise
        finally:
            if process.returncode is None:
                await self._stop_process(process)

        if not truncated and not timed_out and process.returncode not in {0, 1}:
            return ToolResult(
                success=False,
                error=stderr.strip()
                or f"ripgrep failed with exit code {process.returncode}",
            )

        content = "\n".join(rendered) or "No matches found."
        if truncated:
            content += "\n\n[Hint: result limit reached; more matches available.]"
        if timed_out:
            content += (
                f"\n\n[Warning: grep timed out after {self.timeout_seconds:g} seconds; "
                "partial results are shown.]"
            )
        if oversized_record:
            content += "\n\n[Warning: one or more oversized matching lines were skipped.]"
        model_context = None
        if truncated or timed_out:
            reason = "timed out" if timed_out else "result limit reached"
            model_context = (
                f"[Incomplete grep results: {reason}; pattern={pattern}; "
                f"path={search_path}; include={include}; returned={len(matches)}. "
                "These are examples, not all matches. Narrow path or pattern and "
                "search again before drawing conclusions.]\n"
                + "\n".join(rendered[:10])
            )
        return ToolResult(
            success=True,
            content=content,
            model_context=model_context,
            raw_output={
                "path": str(search_path),
                "returned_matches": len(matches),
                "truncated": truncated or timed_out,
                "matches": matches,
            },
        )
