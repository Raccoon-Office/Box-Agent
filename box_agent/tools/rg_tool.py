"""Bounded ripgrep search for code-mode sessions."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .base import Tool, ToolResult
from .safety import validate_path_in_workspace

if TYPE_CHECKING:
    from .permissions import PermissionEngine


DEFAULT_RG_LIMIT = 100
MAX_RG_RESULTS = 200
MAX_RG_GLOBS = 20
MAX_RG_CONTEXT_LINES = 10
MAX_RG_OUTPUT_CHARS = 50_000
MAX_RG_LINE_CHARS = 2_000
MAX_RG_RECORD_BYTES = 1024 * 1024
MAX_RG_STDERR_BYTES = 8 * 1024
DEFAULT_RG_TIMEOUT_SECONDS = 60.0


def resolve_rg_executable(runtime_env: Mapping[str, str] | None = None) -> str | None:
    """Return an explicitly injected or system ripgrep executable."""

    configured = (runtime_env or {}).get("BOX_AGENT_RG") or os.environ.get("BOX_AGENT_RG")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return shutil.which("rg")


class RgTool(Tool):
    """Expose a constrained ripgrep surface without routing through a shell."""

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
        timeout_seconds: float = DEFAULT_RG_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).absolute()
        self.relative_root_dir = (
            Path(relative_root_dir).absolute() if relative_root_dir else self.workspace_dir
        )
        self.executable = executable
        self.allow_full_access = allow_full_access
        self._perm = permission_engine
        self.timeout_seconds = max(0.01, float(timeout_seconds))

    @property
    def name(self) -> str:
        return "rg"

    @property
    def description(self) -> str:
        return (
            "Search source code with ripgrep without constructing a shell command. "
            "mode='content' searches file contents by regular expression; mode='files' "
            "finds file paths by glob. Searches are bounded and respect ignore files by "
            "default. Use hidden or no_ignore only when the task explicitly requires them."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex for content mode or glob pattern for files mode",
                    "minLength": 1,
                },
                "mode": {
                    "type": "string",
                    "enum": ["content", "files"],
                    "default": "content",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search, relative to the active project root",
                    "default": ".",
                },
                "globs": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": MAX_RG_GLOBS,
                    "description": "Content-mode include/exclude globs, such as '*.py' or '!tests/**'",
                },
                "fixed_strings": {"type": "boolean", "default": False},
                "case_sensitive": {"type": "boolean", "default": True},
                "context": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_RG_CONTEXT_LINES,
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RG_RESULTS,
                    "default": DEFAULT_RG_LIMIT,
                },
                "hidden": {"type": "boolean", "default": False},
                "no_ignore": {"type": "boolean", "default": False},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    def _resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        return path if path.is_absolute() else self.relative_root_dir / path

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

    def _display_path(self, cwd: Path, raw_path: str) -> str:
        absolute = (cwd / raw_path).absolute()
        try:
            return absolute.relative_to(self.relative_root_dir).as_posix()
        except ValueError:
            return str(absolute)

    @staticmethod
    async def _read_stderr(stream: asyncio.StreamReader) -> str:
        data = await stream.read(MAX_RG_STDERR_BYTES + 1)
        suffix = "\n...[stderr truncated]" if len(data) > MAX_RG_STDERR_BYTES else ""
        return data[:MAX_RG_STDERR_BYTES].decode("utf-8", errors="replace") + suffix

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

    @staticmethod
    def _common_args(*, hidden: bool, no_ignore: bool) -> list[str]:
        return [
            "--no-config",
            "--no-messages",
            "--color=never",
            *(["--hidden"] if hidden else []),
            *(["--no-ignore"] if no_ignore else []),
        ]

    @staticmethod
    def _protected_globs(*, hidden: bool) -> list[str]:
        return [
            *([] if hidden else ["--glob=!**/.*"]),
            "--glob=!**/.git/**",
        ]

    async def execute(
        self,
        pattern: str,
        mode: str = "content",
        path: str = ".",
        globs: list[str] | None = None,
        fixed_strings: bool = False,
        case_sensitive: bool = True,
        context: int = 0,
        limit: int = DEFAULT_RG_LIMIT,
        hidden: bool = False,
        no_ignore: bool = False,
    ) -> ToolResult:
        """Run one bounded ripgrep content or file search."""

        if not isinstance(pattern, str) or not pattern:
            return ToolResult(success=False, error="rg requires a non-empty pattern")
        if mode not in {"content", "files"}:
            return ToolResult(success=False, error="mode must be 'content' or 'files'")
        if globs is not None and (
            not isinstance(globs, list)
            or len(globs) > MAX_RG_GLOBS
            or any(not isinstance(item, str) or not item for item in globs)
        ):
            return ToolResult(
                success=False,
                error=f"globs must contain at most {MAX_RG_GLOBS} non-empty strings",
            )
        normalized_limit = max(
            1,
            min(limit if isinstance(limit, int) else DEFAULT_RG_LIMIT, MAX_RG_RESULTS),
        )
        normalized_context = max(
            0,
            min(context if isinstance(context, int) else 0, MAX_RG_CONTEXT_LINES),
        )
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
            return ToolResult(success=False, error=f"Search path not found: {path}")
        if mode == "files" and not search_path.is_dir():
            return ToolResult(success=False, error=f"rg files path must be a directory: {path}")

        cwd = search_path if search_path.is_dir() else search_path.parent
        target = "." if search_path.is_dir() else search_path.name
        common = self._common_args(hidden=hidden, no_ignore=no_ignore)
        protected_globs = self._protected_globs(hidden=hidden)
        if mode == "files":
            args = [*common, "--files", *protected_globs, target]
        else:
            args = [
                *common,
                "--json",
                *(["--fixed-strings"] if fixed_strings else []),
                "--case-sensitive" if case_sensitive else "--ignore-case",
                *([f"--context={normalized_context}"] if normalized_context else []),
                *(f"--glob={item}" for item in globs or []),
                *protected_globs,
                "--",
                pattern,
                target,
            ]

        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_RG_RECORD_BYTES,
            )
        except (FileNotFoundError, OSError) as exc:
            return ToolResult(success=False, error=f"ripgrep is unavailable: {exc}")

        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(self._read_stderr(process.stderr))
        deadline = time.monotonic() + self.timeout_seconds
        matches: list[dict[str, Any]] = []
        files: list[str] = []
        rendered: list[str] = []
        rendered_chars = 0
        truncated = False
        timed_out = False
        record_too_large = False

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw_line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                except ValueError:
                    # readline drops the over-limit buffer. Keep consuming so a
                    # single minified line does not abort otherwise useful results.
                    record_too_large = True
                    continue
                if not raw_line:
                    break

                item: dict[str, Any] | None = None
                if mode == "files":
                    relative = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if relative:
                        display_path = self._display_path(cwd, relative)
                        if not (
                            fnmatch(Path(display_path).name, pattern)
                            or fnmatch(display_path, pattern)
                        ):
                            continue
                        item = {"path": display_path}
                        line = display_path
                else:
                    try:
                        event = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    event_type = event.get("type")
                    if event_type not in {"match", "context"}:
                        continue
                    data = event.get("data", {})
                    raw_match_path = data.get("path", {}).get("text")
                    line_number = data.get("line_number")
                    text = data.get("lines", {}).get("text")
                    submatches = data.get("submatches") or []
                    if (
                        not isinstance(raw_match_path, str)
                        or not isinstance(line_number, int)
                        or not isinstance(text, str)
                    ):
                        continue
                    preview = text.rstrip("\r\n")
                    if len(preview) > MAX_RG_LINE_CHARS:
                        preview = preview[:MAX_RG_LINE_CHARS] + "... [line truncated]"
                    display_path = self._display_path(cwd, raw_match_path)
                    if event_type == "context":
                        line = f"{display_path}:{line_number}:-{preview}"
                        extra_chars = len(line) + (1 if rendered else 0)
                        if rendered_chars + extra_chars > MAX_RG_OUTPUT_CHARS - 200:
                            truncated = True
                            break
                        rendered.append(line)
                        rendered_chars += extra_chars
                        continue
                    first = submatches[0] if submatches and isinstance(submatches[0], dict) else {}
                    start = first.get("start")
                    column = start + 1 if isinstance(start, int) else 1
                    item = {
                        "path": display_path,
                        "line": line_number,
                        "column": column,
                        "text": preview,
                    }
                    line = f"{display_path}:{line_number}:{column}:{preview}"

                if item is None:
                    continue
                result_count = len(files) if mode == "files" else len(matches)
                if result_count >= normalized_limit:
                    truncated = True
                    break
                extra_chars = len(line) + (1 if rendered else 0)
                if rendered_chars + extra_chars > MAX_RG_OUTPUT_CHARS - 200:
                    truncated = True
                    break
                rendered.append(line)
                rendered_chars += extra_chars
                if mode == "files":
                    files.append(item["path"])
                else:
                    matches.append(item)

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
            detail = stderr.strip() or f"ripgrep failed with exit code {process.returncode}"
            return ToolResult(success=False, error=detail)

        content = "\n".join(rendered) or "No matches found."
        if truncated:
            content += (
                "\n\n[Hint: result limit reached; more matches available. "
                "Narrow the path, pattern, or globs.]"
            )
        if timed_out:
            content += (
                f"\n\n[Warning: rg timed out after {self.timeout_seconds:g} seconds; "
                "partial results are shown.]"
            )
        if record_too_large:
            content += (
                "\n\n[Warning: one or more oversized matching lines were skipped; "
                "narrow the path or exclude generated and minified files if those "
                "matches are required.]"
            )

        raw_output: dict[str, Any] = {
            "mode": mode,
            "path": str(search_path.absolute()),
            "returned_matches": len(files) if mode == "files" else len(matches),
            "truncated": truncated or timed_out,
        }
        if mode == "files":
            raw_output["files"] = files
        else:
            raw_output["matches"] = matches
        return ToolResult(success=True, content=content, raw_output=raw_output)
