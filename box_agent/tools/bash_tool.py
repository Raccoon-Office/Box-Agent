"""Shell command execution tool with background process management.

Supports both bash (Unix/Linux/macOS) and PowerShell (Windows).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field, model_validator

from ..config import ToolsConfig
from .base import Tool, ToolResult
from .argument_limits import MAX_BASH_COMMAND_CHARS
from .pptx_safety import (
    detect_pptx_image_status_command_bypass,
    detect_pptx_self_check_bypass,
)
from .runtime import bundled_win_bash
from .shell_inspection import inspect_shell_command
from .safety import (
    backup_file,
    detect_dangerous_command,
    detect_scope_escape,
    extract_rm_targets,
    trusted_runtime_executable_references,
)

if TYPE_CHECKING:
    from .permissions import PermissionEngine

log = logging.getLogger(__name__)

# Maximum stdout+stderr payload retained in a single bash tool result. Beyond
# this we keep the beginning and end and add a marker guiding the model to
# narrow the command. Motivated by a live incident where two `rg` full-tree
# searches returned 3.7M chars combined and the next LLM request exceeded its
# context limit. The marker itself is intentionally additional to this payload
# budget. This mirrors Hermes terminal's 50K, 40%-head/60%-tail policy.
MAX_BASH_OUTPUT_CHARS = 50_000
_DEFAULT_TOOLS_CONFIG = ToolsConfig()
BASH_LIFETIME_TURN = "turn"
BASH_LIFETIME_RUNTIME = "runtime"
_BASH_LIFETIMES = frozenset({BASH_LIFETIME_TURN, BASH_LIFETIME_RUNTIME})


def _truncate_bash_output(text: str, label: str, limit: int = MAX_BASH_OUTPUT_CHARS) -> tuple[str, int]:
    """Truncate bash output to ``limit`` payload chars using head 40% + tail 60%.

    Returns ``(truncated_text, dropped_chars)``. Empty / short-enough text is
    returned unchanged with ``dropped_chars == 0``.

    The middle marker embeds an actionable hint (use narrower patterns,
    ``head -N``, ``rg -m N``, ...) so the model sees the guidance in the
    same visual position where it would otherwise conclude "no more matches
    beyond here" mid-scroll.
    """
    if not text or len(text) <= limit:
        return text, 0
    head_n = limit * 2 // 5
    tail_n = limit - head_n
    dropped = len(text) - limit
    head, tail = text[:head_n], text[-tail_n:]
    marker = (
        f"\n\n...[{label} truncated: dropped {dropped:,} chars "
        f"(showing first {head_n:,} + last {tail_n:,} of {len(text):,}). "
        f"Tip: use narrower search patterns, `rg -m N`, or restrict the "
        f"search path; cap displayed lines with `head -N` on POSIX or "
        f"`Select-Object -First N` on PowerShell.]...\n\n"
    )
    return head + marker + tail, dropped


def _truncate_bash_streams(
    stdout: str,
    stderr: str,
    limit: int = MAX_BASH_OUTPUT_CHARS,
) -> tuple[str, str, int]:
    """Apply one shared payload budget to a command's stdout and stderr.

    Small outputs preserve the structured streams. Oversized output is first
    formatted exactly as the model would see it, then truncated as one unit so
    stdout and stderr cannot each consume a separate 50K allowance. The
    bounded combined representation is returned in ``stdout``; ``raw_output``
    metadata tells hosts that the original streams were combined.
    """
    combined = stdout
    if stderr:
        combined += f"\n[stderr]:\n{stderr}"
    if len(combined) <= limit:
        return stdout, stderr, 0
    truncated, dropped = _truncate_bash_output(combined, "combined stdout/stderr", limit)
    return truncated, "", dropped


def _format_bash_result_content(
    stdout: str,
    stderr: str,
    *,
    bash_id: str | None = None,
    lifetime: str | None = None,
    exit_code: int = 0,
) -> str:
    """Format Bash streams once for host display or shared persistence."""

    output = stdout
    if stderr:
        output += f"\n[stderr]:\n{stderr}"
    if bash_id:
        output += f"\n[bash_id]:\n{bash_id}"
    if lifetime:
        output += f"\n[lifetime]:\n{lifetime}"
        output += (
            "\n[will_survive_turn_end]:\n"
            f"{'true' if lifetime == BASH_LIFETIME_RUNTIME else 'false'}"
        )
    if exit_code:
        output += f"\n[exit_code]:\n{exit_code}"
    return output or "(no output)"


# Shells whose syntax is POSIX-compatible (supports &&, ||, for/do/done, etc.)
_POSIX_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "ksh", "ash"})
_PIPEFAIL_SHELLS = frozenset({"bash", "zsh", "ksh"})
_LARK_CLI_RE = re.compile(
    r"(?:\blark-cli(?:\.(?:cmd|exe))?\b|\$BOX_AGENT_LARK_CLI\b|\$\{BOX_AGENT_LARK_CLI\}|%BOX_AGENT_LARK_CLI%)",
    re.IGNORECASE,
)
_LARK_USER_FLAG_RE = re.compile(r"--as(?:=|\s+)user\b", re.IGNORECASE)
_LARK_BOT_FLAG_RE = re.compile(r"--as(?:=|\s+)bot\b", re.IGNORECASE)
_LARK_BOT_ONLY_RE = re.compile(r"--(?:identity|auth)(?:=|\s+)bot-only\b", re.IGNORECASE)
_LARK_USER_DEFAULT_RE = re.compile(r"--identity(?:=|\s+)user-default\b", re.IGNORECASE)
_LARK_CONFIG_BIND_RE = re.compile(r"\bconfig\s+bind\b", re.IGNORECASE)
_LARK_STRICT_MODE_RE = re.compile(r"\bconfig\s+strict-mode\b", re.IGNORECASE)
_LARK_COMMAND_SEPARATOR_RE = re.compile(r"&&|\|\||[;\n|]")
_LARK_CLI_ENV_ASSIGNMENT_RE = re.compile(
    r"^(?:export\s+|set\s+)BOX_AGENT_LARK_CLI\s*=",
    re.IGNORECASE,
)
_DINGTALK_DWS_RE = re.compile(
    r"(?:\bdws(?:\.(?:cmd|exe))?\b|\$BOX_AGENT_DINGTALK_CLI\b|\$\{BOX_AGENT_DINGTALK_CLI\}|%BOX_AGENT_DINGTALK_CLI%)",
    re.IGNORECASE,
)
_DINGTALK_DWS_EXECUTABLE_NAMES = frozenset({"dws", "dws.exe", "dws.cmd"})
_DINGTALK_CONTROL_FLAGS = frozenset({
    "--profile",
    "--client-id",
    "--client-secret",
    "--client_id",
    "--client_secret",
})
_DINGTALK_CONTROL_ENV_ASSIGNMENT_RE = re.compile(
    r"^(?:DWS|DINGTALK)_(?:PROFILE|CLIENT_ID|CLIENT_SECRET)=",
    re.IGNORECASE,
)
_OBSIDIAN_COMMAND_SEPARATOR_RE = re.compile(r"&&|\|\||[;\n|]")
_OBSIDIAN_CLI_NAMES = frozenset({"obsidian", "obsidian.exe", "obsidian.cmd"})
_OBSIDIAN_WRITE_COMMANDS = frozenset({
    "create",
    "append",
    "prepend",
    "open",
    "daily",
    "daily:read",
    "daily:append",
    "daily:prepend",
    "delete",
    "move",
    "rename",
    "property:set",
})
_SAFETY_SCOPE = "safety"
_DANGEROUS_COMMAND_SCOPE = "dangerous_command"


def _is_shell_output_redirect_path(command: str, path: str) -> bool:
    """Return whether *path* is the target of a shell output redirect."""
    escaped = re.escape(path)
    return (
        re.search(
            rf"(?:^|[\s;|&])\d*>{{1,2}}\s*['\"]?{escaped}"
            rf"(?:['\"]|(?=\s|;|\||&|$))",
            command,
        )
        is not None
    )


def _is_temp_shell_redirect_path(command: str, path: str) -> bool:
    """Allow bash-only scratch files written via redirect under temp roots."""
    return Path(path).name.startswith("box_agent_") and _is_shell_output_redirect_path(
        command, path
    )


_MUTATING_SHELL_COMMAND_RE = re.compile(
    r"\b(cp|mv|rsync|tee|dd|install|scp|tar\s+.*-x|sed\s+-i)\b"
)


def _path_requires_write_permission(command: str, path: str) -> bool:
    """Classify one extracted path instead of upgrading the whole command.

    File-descriptor redirects such as ``2>&1`` have no filesystem target and
    therefore never turn a referenced script into a write. Mutating utilities
    remain conservative because their source/destination grammar varies by
    platform and flags.
    """
    return bool(_MUTATING_SHELL_COMMAND_RE.search(command)) or _is_shell_output_redirect_path(
        command, path
    )


def _resolve_login_shell() -> str:
    """Return a POSIX-compatible login shell path.

    Uses ``$SHELL`` if it names a known POSIX shell **and** the path exists
    and is executable; otherwise walks a fallback chain.  This avoids
    failures from stale paths (Nix store, shell upgrades, devcontainers).
    """
    shell = os.environ.get("SHELL", "")
    if shell and os.path.basename(shell) in _POSIX_SHELLS and os.access(shell, os.X_OK):
        return shell
    for fallback in ("/bin/bash", "/bin/sh"):
        if os.access(fallback, os.X_OK):
            return fallback
    return "/bin/sh"  # last resort — always present on Unix


def _detect_lark_user_mode_violation(command: str) -> str | None:
    """Reject lark-cli calls that can switch away from user identity.

    officev3 treats Feishu as a user-authorized integration.  The agent may
    receive bundled ``lark-cli`` through PATH, but it must not mutate the
    user's global CLI workspace into bot-only/strict bot mode.
    """
    for raw_part in _LARK_COMMAND_SEPARATOR_RE.split(command):
        part = raw_part.strip()
        if _LARK_CLI_ENV_ASSIGNMENT_RE.match(part):
            continue
        cli_match = _LARK_CLI_RE.search(part)
        if not cli_match:
            continue
        lowered = part.lower()
        cli_args = part[cli_match.end() :].lstrip()
        # A quoted absolute executable path leaves its closing quote immediately
        # after the regex match, for example `"C:\\...\\lark-cli.cmd" auth login`.
        # Normalize only that boundary so setup/OAuth commands are classified by
        # their real first argument on Windows as well as POSIX hosts.
        cli_args = cli_args.lstrip("'\"").lstrip()

        if _LARK_BOT_FLAG_RE.search(part):
            return "lark-cli bot identity is disabled in officev3 local-agent sessions; use `--as user`."
        if _LARK_BOT_ONLY_RE.search(part):
            return "lark-cli bot-only binding is disabled; officev3 requires user identity."
        if _LARK_STRICT_MODE_RE.search(part):
            return "lark-cli strict-mode changes are disabled; the product owns Feishu identity policy."
        if _LARK_CONFIG_BIND_RE.search(part) and not _LARK_USER_DEFAULT_RE.search(part):
            return "lark-cli config bind must not change Feishu to bot-only; use user-default identity or product settings."

        # Diagnostics, setup, and OAuth commands either do not accept --as or
        # are specifically how user identity is obtained.
        if (
            "--help" in lowered
            or re.search(r"\s(?:--version|-v)\b", lowered)
            or re.match(r"(?:auth|config|schema|doctor|update)\b", cli_args, re.IGNORECASE)
            or re.match(r"skills(?:\s+(?:list|read)\b|\s*$)", cli_args, re.IGNORECASE)
        ):
            continue

        if not _LARK_USER_FLAG_RE.search(part):
            return "lark-cli business commands must pass `--as user` in officev3 local-agent sessions."
    return None


def _detect_dingtalk_workspace_violation(command: str) -> str | None:
    """Allow only the officev3-supported DWS surface for the current OAuth profile.

    DWS persists its own profile/configuration.  Changing that control plane or
    invoking raw APIs would let an agent escape the desktop product's consent
    and scope model, so the runtime—not the UI prompt—enforces this allowlist.
    """
    def is_dws_executable(token: str) -> bool:
        if token in {
            "$BOX_AGENT_DINGTALK_CLI",
            "${BOX_AGENT_DINGTALK_CLI}",
            "%BOX_AGENT_DINGTALK_CLI%",
        }:
            return True
        # `os.path.basename` does not understand the opposite platform's path
        # separator, so normalize first. OfficeV3 deliberately passes the
        # bundled absolute path here on macOS/Windows.
        base = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
        return base in _DINGTALK_DWS_EXECUTABLE_NAMES

    def mentions_dws(value: str) -> bool:
        if bool(
            _DINGTALK_DWS_RE.search(value)
            or _DINGTALK_DWS_RE.search(re.sub(r"\\([A-Za-z])", r"\1", value))
        ):
            return True
        # Dynamic executable construction may split the spelling across quotes,
        # parameter expansions, or static printf escapes. Normalize only for
        # evidence that a dynamic command may target DWS; ordinary static
        # invocations still use the exact executable-name check above.
        normalized = re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )
        normalized = re.sub(
            r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}\n]+\}|[@*#?$!0-9_-])",
            "",
            normalized,
        )
        normalized = re.sub(r"[^A-Za-z]", "", normalized).casefold()
        normalized = re.sub(r"(?:echo|printf)", "", normalized)
        return "dws" in normalized

    inspection = inspect_shell_command(
        command,
        posix=platform.system() != "Windows",
    )
    dws_invocations = [
        invocation
        for invocation in inspection.invocations
        if is_dws_executable(invocation.executable)
    ]
    if any(mentions_dws(region) for region in inspection.ambiguous_regions):
        return "DWS command could not be parsed and is blocked by the DingTalk integration policy."

    # A substitution used as the executable can assemble `dws` from fragments
    # with no complete policy keyword visible before execution. Fail closed;
    # substitutions used only in assignments or arguments remain allowed.
    dynamic_invocations = [
        invocation
        for invocation in inspection.invocations
        if invocation.dynamic_executable_sources
        and not is_dws_executable(invocation.executable)
    ]
    if any(
        mentions_dws(invocation.dynamic_executable_evidence)
        for invocation in dynamic_invocations
    ):
        return (
            "Dynamic executable construction with DWS evidence is "
            "disabled by the DingTalk integration policy."
        )
    if not dws_invocations:
        return None
    if (
        inspection.has_control_operators
        or inspection.substitutions
        or any(invocation.indirect for invocation in dws_invocations)
    ):
        return (
            "Shell chaining and command substitution are disabled for DWS commands. "
            "Run one allowed DWS command directly."
        )

    invocation = dws_invocations[0]
    tokens = list(invocation.words)
    dws_index = invocation.executable_index
    prefix = tokens[:dws_index]
    if any(_DINGTALK_CONTROL_ENV_ASSIGNMENT_RE.match(token) for token in prefix):
        return "DWS OAuth profile and client credentials are managed by officev3 and cannot be overridden by the agent."
    if prefix:
        # Do not let a shell prefix replace PATH or otherwise turn a bare `dws`
        # invocation into an arbitrary binary. OfficeV3 supplies either `dws`,
        # its absolute bundled path, or $BOX_AGENT_DINGTALK_CLI directly.
        return "DWS command must invoke the bundled `dws` binary directly."

    raw_args = tokens[dws_index + 1 :]
    if any(
        token.split("=", 1)[0].lower() in _DINGTALK_CONTROL_FLAGS
        for token in raw_args
    ):
        return "DWS OAuth profile and client credentials are managed by officev3 and cannot be overridden by the agent."

    # `-v` is DWS verbose mode, not a version query. Only standalone version
    # invocations bypass the command allowlist.
    if raw_args in (["--version"], ["version"]):
        return None
    # Help does not perform a business operation, including e.g. `auth login --help`.
    if "--help" in raw_args or "-h" in raw_args:
        return None

    args = [token.lower() for token in raw_args if not token.startswith("-")]
    if not args:
        return None
    if args[:2] == ["auth", "status"]:
        return None
    if args[0] in {"auth", "profile", "config", "skill", "skills", "plugin", "plugins", "upgrade", "update", "api", "raw"}:
        return "DWS authentication, profile/configuration, skill/plugin, upgrade, and raw API commands are managed by officev3 and cannot be run by the agent."

    allowed = (
        (args[0] == "doc" and len(args) >= 2 and args[1] in {"search", "list", "read", "create", "update"})
        or (args[:3] in (["wiki", "space", "list"], ["wiki", "node", "list"]))
        or (
            args[0] == "drive"
            and len(args) >= 2
            and args[1] in {"list", "list-spaces", "info", "download", "upload", "mkdir"}
        )
    )
    if not allowed:
        return (
            "This DWS command is outside the officev3 DingTalk v1 scope. Only document/wiki/drive "
            "reads, `doc create`/`doc update`, and `drive upload`/`drive mkdir` are allowed."
        )
    return None


def _detect_obsidian_cli_violation(command: str) -> str | None:
    """Reject direct Obsidian writes/opens that should go through native tools."""
    for raw_part in _OBSIDIAN_COMMAND_SEPARATOR_RE.split(command):
        part = raw_part.strip()
        if not part:
            continue
        try:
            tokens = shlex.split(part, posix=platform.system() != "Windows")
        except ValueError:
            continue
        if not tokens:
            continue

        # Diagnostics are allowed through bash.
        if len(tokens) >= 2 and tokens[0] in {"which", "where"} and os.path.basename(tokens[1]).lower() in _OBSIDIAN_CLI_NAMES:
            continue

        exe_index = None
        for index, token in enumerate(tokens):
            if token in {"env", "command"}:
                continue
            if "=" in token and not token.startswith("-"):
                continue
            base = os.path.basename(token).lower()
            if base in _OBSIDIAN_CLI_NAMES or token in {"$BOX_AGENT_OBSIDIAN_CLI", "${BOX_AGENT_OBSIDIAN_CLI}", "%BOX_AGENT_OBSIDIAN_CLI%"}:
                exe_index = index
            break
        if exe_index is None:
            continue

        args = tokens[exe_index + 1 :]
        if not args:
            continue
        first = args[0].lower()
        if first in {"help", "--help", "-h", "version", "--version", "-v"}:
            continue
        if first in _OBSIDIAN_WRITE_COMMANDS:
            return (
                "Obsidian 写入、打开或修改命令必须通过原生工具执行；"
                "请改用 `obsidian_create_note`、`obsidian_update_note` 或 `obsidian_daily_note`。"
            )
    return None


def _safety_approval_key(command: str, risk: str) -> str:
    return f"{_DANGEROUS_COMMAND_SCOPE}\0{risk}\0{command}"


def _dangerous_command_permission_request(command: str, risk: str) -> dict[str, Any]:
    return {
        "type": "permission_request",
        "scope": _SAFETY_SCOPE,
        "requested_scope": _DANGEROUS_COMMAND_SCOPE,
        "path": "",
        "reason": f"Dangerous command detected: {risk}",
        "temporary_supported": True,
        "persistent_supported": False,
        "persistent_label": "",
        "command": command,
        "risk": risk,
    }


class BashOutputResult(ToolResult):
    """Bash command execution result with separated stdout and stderr.

    Inherits from ToolResult which provides:
    - success: bool
    - content: str (used for formatted output message, auto-generated from stdout/stderr)
    - error: str | None (used for error messages)
    """

    stdout: str = Field(description="The command's standard output")
    stderr: str = Field(description="The command's standard error output")
    exit_code: int = Field(description="The command's exit code")
    bash_id: str | None = Field(default=None, description="Shell process ID (only when run_in_background=True)")
    lifetime: str | None = Field(
        default=None,
        description="Background process lifetime: turn or runtime",
    )

    @model_validator(mode="after")
    def format_content(self) -> "BashOutputResult":
        """Auto-format content from stdout and stderr if content is empty."""
        self.content = _format_bash_result_content(
            self.stdout,
            self.stderr,
            bash_id=self.bash_id,
            lifetime=self.lifetime,
            exit_code=self.exit_code,
        )
        return self


class BackgroundShell:
    """Background shell data container.

    Pure data class that only stores state and output.
    IO operations are managed externally by BackgroundShellManager.
    """

    def __init__(
        self,
        bash_id: str,
        command: str,
        process: "asyncio.subprocess.Process",
        start_time: float,
        owner_id: str | None = None,
        lifetime: str = BASH_LIFETIME_TURN,
    ):
        self.bash_id = bash_id
        self.command = command
        self.process = process
        self.start_time = start_time
        self.owner_id = owner_id
        self.lifetime = lifetime
        self.output_lines: list[str] = []
        self.last_read_index = 0
        self.status = "running"
        self.exit_code: int | None = None

    def add_output(self, line: str):
        """Add new output line."""
        self.output_lines.append(line)

    def get_new_output(self, filter_pattern: str | None = None) -> list[str]:
        """Get new output since last check, optionally filtered by regex."""
        new_lines = self.output_lines[self.last_read_index :]
        self.last_read_index = len(self.output_lines)

        if filter_pattern:
            try:
                pattern = re.compile(filter_pattern)
                new_lines = [line for line in new_lines if pattern.search(line)]
            except re.error:
                # Invalid regex, return all lines
                pass

        return new_lines

    def update_status(self, is_alive: bool, exit_code: int | None = None):
        """Update process status."""
        if not is_alive:
            self.status = "completed" if exit_code == 0 else "failed"
            self.exit_code = exit_code
        else:
            self.status = "running"

    async def terminate(self):
        """Terminate the background process and its whole subtree.

        Delegates to ``BashTool._kill_process_tree`` — same tree-kill logic
        used by the foreground timeout path. Without this, a Windows
        background command whose wrapper spawned children (e.g. a Python
        server that forked workers) would leave orphans holding the merged
        stdout pipe alive after ``bash_kill``; on Unix ``terminate()`` alone
        (SIGTERM to the wrapper) misses grandchildren the same way.
        """
        # Always target the process group. The shell wrapper may already have
        # exited while a backgrounded grandchild still owns the group and its
        # stdout pipe; checking only ``returncode`` would miss that orphan.
        await BashTool._kill_process_tree(self.process)
        self.status = "terminated"
        self.exit_code = self.process.returncode


class BackgroundShellManager:
    """Manager for all background shell processes."""

    _shells: dict[str, BackgroundShell] = {}
    _monitor_tasks: dict[str, asyncio.Task] = {}

    @classmethod
    def add(cls, shell: BackgroundShell) -> None:
        """Add a background shell to management."""
        cls._shells[shell.bash_id] = shell

    @classmethod
    def get(
        cls, bash_id: str, owner_id: str | None = None
    ) -> BackgroundShell | None:
        """Get a background shell by ID."""
        shell = cls._shells.get(bash_id)
        if shell is not None and owner_id is not None and shell.owner_id != owner_id:
            return None
        return shell

    @classmethod
    def get_available_ids(
        cls,
        owner_id: str | None = None,
        *,
        lifetime: str | None = None,
    ) -> list[str]:
        """Get all available bash IDs."""
        return [
            bash_id
            for bash_id, shell in cls._shells.items()
            if (owner_id is None or shell.owner_id == owner_id)
            and (lifetime is None or shell.lifetime == lifetime)
        ]

    @classmethod
    def _remove(cls, bash_id: str) -> None:
        """Remove a background shell from management (internal use only)."""
        if bash_id in cls._shells:
            del cls._shells[bash_id]

    @classmethod
    async def start_monitor(cls, bash_id: str) -> None:
        """Start monitoring a background shell's output."""
        shell = cls.get(bash_id)
        if not shell:
            return

        async def monitor():
            try:
                process = shell.process
                # Continuously read output until process ends
                while process.returncode is None:
                    try:
                        if process.stdout:
                            line = await asyncio.wait_for(process.stdout.readline(), timeout=0.1)
                            if line:
                                decoded_line = line.decode("utf-8", errors="replace").rstrip("\n")
                                shell.add_output(decoded_line)
                            else:
                                break
                    except asyncio.TimeoutError:
                        await asyncio.sleep(0.1)
                        continue
                    except Exception:
                        await asyncio.sleep(0.1)
                        continue

                # A short-lived process can exit before the monitor gets its
                # first iteration. Drain everything still buffered after exit
                # so fast commands do not silently lose their output.
                if process.stdout:
                    remaining = await process.stdout.read()
                    if remaining:
                        decoded = remaining.decode("utf-8", errors="replace")
                        for line in decoded.splitlines():
                            shell.add_output(line)

                # Process ended, wait for exit code
                try:
                    returncode = await process.wait()
                except Exception:
                    returncode = -1

                shell.update_status(is_alive=False, exit_code=returncode)

            except Exception as e:
                if bash_id in cls._shells:
                    cls._shells[bash_id].status = "error"
                    cls._shells[bash_id].add_output(f"Monitor error: {str(e)}")
            finally:
                if bash_id in cls._monitor_tasks:
                    del cls._monitor_tasks[bash_id]

        task = asyncio.create_task(monitor())
        cls._monitor_tasks[bash_id] = task

    @classmethod
    def _cancel_monitor(cls, bash_id: str) -> None:
        """Cancel and remove a monitoring task (internal use only)."""
        if bash_id in cls._monitor_tasks:
            task = cls._monitor_tasks[bash_id]
            if not task.done():
                task.cancel()
            del cls._monitor_tasks[bash_id]

    @classmethod
    async def terminate(
        cls, bash_id: str, owner_id: str | None = None
    ) -> BackgroundShell:
        """Terminate a background shell and clean up all resources.

        Args:
            bash_id: The unique identifier of the background shell

        Returns:
            The terminated BackgroundShell object

        Raises:
            ValueError: If shell not found
        """
        shell = cls.get(bash_id, owner_id)
        if not shell:
            raise ValueError(f"Shell not found: {bash_id}")

        # Terminate the process
        await shell.terminate()

        # Clean up monitoring and remove from manager
        cls._cancel_monitor(bash_id)
        cls._remove(bash_id)

        return shell

    @classmethod
    async def terminate_owner(
        cls,
        owner_id: str,
        *,
        lifetime: str | None = None,
    ) -> list[str]:
        """Terminate and forget every background process owned by one session."""
        bash_ids = cls.get_available_ids(owner_id, lifetime=lifetime)
        if not bash_ids:
            return []

        results = await asyncio.gather(
            *(cls.terminate(bash_id, owner_id) for bash_id in bash_ids),
            return_exceptions=True,
        )
        terminated: list[str] = []
        for bash_id, result in zip(bash_ids, results):
            if isinstance(result, BaseException):
                log.warning(
                    "bash/session_cleanup_failed owner_id=%s bash_id=%s error=%s",
                    owner_id,
                    bash_id,
                    result,
                )
                continue
            terminated.append(bash_id)
        return terminated

    @classmethod
    async def terminate_all(cls) -> list[str]:
        """Terminate every managed background process before runtime exit."""
        bash_ids = cls.get_available_ids()
        if not bash_ids:
            return []

        results = await asyncio.gather(
            *(cls.terminate(bash_id) for bash_id in bash_ids),
            return_exceptions=True,
        )
        terminated: list[str] = []
        for bash_id, result in zip(bash_ids, results):
            if isinstance(result, BaseException):
                log.warning(
                    "bash/runtime_cleanup_failed bash_id=%s error=%s",
                    bash_id,
                    result,
                )
                continue
            terminated.append(bash_id)
        return terminated


class BashTool(Tool):
    """Execute shell commands in foreground or background.

    Automatically detects OS and uses appropriate shell:
    - Windows: PowerShell
    - Unix/Linux/macOS: bash
    """

    aliases = ("exec", "terminal")
    max_result_size_chars = math.inf

    def __init__(
        self,
        workspace_dir: str | None = None,
        scope_root_dir: str | None = None,
        allow_full_access: bool = True,
        non_interactive: bool = False,
        sandbox_venv_path: str | None = None,
        permission_engine: PermissionEngine | None = None,
        runtime_env: dict[str, str] | None = None,
        process_owner_id: str | None = None,
        bypass_dangerous_command_approval: bool = False,
        default_timeout_seconds: int = (
            _DEFAULT_TOOLS_CONFIG.bash_default_timeout_seconds
        ),
        max_timeout_seconds: int = _DEFAULT_TOOLS_CONFIG.bash_max_timeout_seconds,
    ):
        """Initialize BashTool with OS-specific shell detection.

        Args:
            workspace_dir: Working directory for command execution.
                           If provided, all commands run in this directory.
                           If None, commands run in the process's cwd.
            scope_root_dir: Optional security boundary when the command cwd is
                            a nested artifact directory.
            allow_full_access: If False, block commands that escape the workspace.
            non_interactive: If True, never prompt on stdin; dangerous commands
                             return an approval request for the core/host.
            sandbox_venv_path: If set, prepend venv bin to PATH and set VIRTUAL_ENV
                               so subprocess commands use the sandbox Python.
            permission_engine: If provided, use capability-based permission checks.
            runtime_env: Extra runtime environment variables exposed to commands.
            bypass_dangerous_command_approval: If True, skip approval for dangerous
                                                commands in a trusted session.
            default_timeout_seconds: Foreground timeout when the caller omits it.
            max_timeout_seconds: Maximum accepted foreground timeout.
        """
        if default_timeout_seconds < 1:
            raise ValueError("default_timeout_seconds must be positive")
        if max_timeout_seconds < default_timeout_seconds:
            raise ValueError(
                "max_timeout_seconds cannot be lower than default_timeout_seconds"
            )
        self.default_timeout_seconds = default_timeout_seconds
        self.max_timeout_seconds = max_timeout_seconds
        self.is_windows = platform.system() == "Windows"
        self.shell_name = "PowerShell" if self.is_windows else "bash"
        # Win-only: prefer the PortableGit ``bash.exe`` shipped inside the
        # frozen runtime when available so the LLM can use bash syntax + the
        # unix coreutils that the skills assume. Falls back to PowerShell
        # when the bundle is absent (dev installs / older runtimes).
        # Mac/Linux behavior unchanged.
        self._bundled_win_bash: str | None = None
        self._bundled_win_path_dirs: list[str] = []
        if self.is_windows:
            bundled = bundled_win_bash()
            if bundled is not None:
                self._bundled_win_bash = str(bundled)
                self.shell_name = "bash"
                # bash.exe lives at <PortableGit>/usr/bin/bash.exe. We launch
                # bash without ``--login`` to avoid MSYS profile slow-start,
                # which means /etc/profile never runs and coreutils dirs are
                # absent from PATH. Prepend the three PortableGit search dirs
                # explicitly so the LLM's `find`/`grep`/`sed`/`git` resolve.
                portable_git_root = bundled.parent.parent.parent
                self._bundled_win_path_dirs = [
                    str(portable_git_root / "usr" / "bin"),
                    str(portable_git_root / "mingw64" / "bin"),
                    str(portable_git_root / "cmd"),
                ]
                # Some Electron launchers (and the restart-electron-foreground
                # PowerShell helper) strip the Win system dirs from the env
                # they hand to box-agent-acp, which leaves `powershell.exe`,
                # `cmd.exe`, `where`, etc. unreachable from the LLM's bash.
                # Append them defensively after the PortableGit dirs so the
                # bundled coreutils still take precedence.
                sys_root = os.environ.get("SystemRoot") or r"C:\Windows"
                self._bundled_win_path_dirs.extend([
                    os.path.join(sys_root, "system32"),
                    sys_root,
                    os.path.join(sys_root, "system32", "Wbem"),
                    os.path.join(sys_root, "system32", "WindowsPowerShell", "v1.0"),
                ])
            log.info(
                "bash_tool/init shell=%s bundled_bash=%s frozen=%s path_dirs=%s",
                self.shell_name, self._bundled_win_bash,
                getattr(sys, "frozen", False), self._bundled_win_path_dirs,
            )
        # Unix: resolve login shell so subprocess inherits user's PATH.
        # Only trust known POSIX-compatible shells; fall back to /bin/bash
        # for fish, csh, and other non-POSIX shells whose syntax is
        # incompatible with the commands the LLM generates.
        if not self.is_windows:
            self._login_shell = _resolve_login_shell()
        self.workspace_dir = workspace_dir
        self.scope_root_dir = scope_root_dir or workspace_dir
        self.allow_full_access = allow_full_access
        self.non_interactive = non_interactive
        self._perm = permission_engine
        self.process_owner_id = process_owner_id
        self.bypass_dangerous_command_approval = bypass_dangerous_command_approval
        self._approved_safety_commands: set[str] = set()
        self._subprocess_env = None
        self._use_login_shell = True
        if sandbox_venv_path or runtime_env:
            self._subprocess_env = os.environ.copy()
        if sandbox_venv_path:
            self._subprocess_env["VIRTUAL_ENV"] = sandbox_venv_path
            venv_bin = os.path.join(sandbox_venv_path, "bin")
            self._subprocess_env["PATH"] = venv_bin + os.pathsep + self._subprocess_env.get("PATH", "")
            # Don't use login shell when sandbox venv is active — profile
            # scripts (pyenv, asdf, conda) would override the venv PATH.
            self._use_login_shell = False
        if runtime_env:
            if "NPM_CONFIG_PREFIX" in runtime_env:
                for key in tuple(self._subprocess_env):
                    if key.lower() in {"npm_config_prefix", "npm_config_cache"}:
                        self._subprocess_env.pop(key, None)
            self._subprocess_env.update(runtime_env)

    def update_runtime_env(self, values: dict[str, str | None]) -> None:
        """Update environment values inherited by future subprocesses.

        ACP uses this for turn-scoped provenance such as the accumulated user
        source text.  Existing commands are unaffected; only subprocesses
        created after this call inherit the new values.
        """
        if self._subprocess_env is None:
            self._subprocess_env = os.environ.copy()
        for key, value in values.items():
            if value is None:
                self._subprocess_env.pop(key, None)
            elif isinstance(value, str):
                self._subprocess_env[key] = value

    async def cleanup_background_processes(
        self,
        *,
        lifetime: str | None = None,
    ) -> list[str]:
        """Reclaim background processes created by this ACP session."""
        if self.process_owner_id is None:
            return []
        return await BackgroundShellManager.terminate_owner(
            self.process_owner_id,
            lifetime=lifetime,
        )

    def approve_permission_request(self, permission_request: dict[str, Any]) -> None:
        """Record a one-shot approval before core retries a safety-gated command."""
        if permission_request.get("scope") != _SAFETY_SCOPE:
            return
        if permission_request.get("requested_scope") != _DANGEROUS_COMMAND_SCOPE:
            return
        command = permission_request.get("command")
        risk = permission_request.get("risk") or permission_request.get("reason")
        if isinstance(command, str) and isinstance(risk, str):
            self._approved_safety_commands.add(_safety_approval_key(command, risk))

    def _consume_safety_approval(self, command: str, risk: str) -> bool:
        key = _safety_approval_key(command, risk)
        if key not in self._approved_safety_commands:
            return False
        self._approved_safety_commands.remove(key)
        return True

    def _has_safety_approval(self, command: str, risk: str) -> bool:
        return _safety_approval_key(command, risk) in self._approved_safety_commands

    async def _create_subprocess(
        self, command: str, *, merge_stderr: bool = False,
    ) -> asyncio.subprocess.Process:
        """Create subprocess with platform-appropriate shell.

        On Windows uses PowerShell; on Unix uses the user's login shell
        (``$SHELL -l -c``) so that PATH from profile scripts is inherited.
        When a sandbox venv is active, ``-l`` is omitted to keep the venv
        PATH authoritative.
        """
        stderr = asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE
        if self.is_windows:
            # Win-only: detach stdin so the child shell does not inherit the
            # ACP stdin pipe (parent box-agent-acp has stdio piped to Electron).
            # Without this, MSYS bash.exe / powershell.exe may keep the handle
            # alive even after the command finishes, leading to hangs.
            if self._bundled_win_bash is not None:
                # Copy env so we can prepend PortableGit search dirs without
                # mutating the cached ``_subprocess_env``. Fall back to the
                # process env when no custom env was prepared.
                spawn_env = (
                    dict(self._subprocess_env)
                    if self._subprocess_env is not None
                    else os.environ.copy()
                )
                if self._bundled_win_path_dirs:
                    existing_path = spawn_env.get("PATH", "")
                    path_parts = list(self._bundled_win_path_dirs)
                    if existing_path:
                        path_parts.append(existing_path)
                    spawn_env["PATH"] = os.pathsep.join(path_parts)
                log.info(
                    "bash/spawn shell=bundled_bash cmd=%r cwd=%s merge_stderr=%s path_head=%s",
                    command[:500], self.workspace_dir, merge_stderr,
                    spawn_env.get("PATH", "")[:200],
                )
                guarded_command = f"set -o pipefail\n{command}"
                return await asyncio.create_subprocess_exec(
                    self._bundled_win_bash, "-c", guarded_command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr,
                    cwd=self.workspace_dir,
                    env=spawn_env,
                )
            log.info(
                "bash/spawn shell=powershell cmd=%r cwd=%s merge_stderr=%s",
                command[:500], self.workspace_dir, merge_stderr,
            )
            return await asyncio.create_subprocess_exec(
                "powershell.exe", "-NoProfile", "-Command", command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                cwd=self.workspace_dir,
                env=self._subprocess_env,
            )
        else:
            args = [self._login_shell]
            if self._use_login_shell:
                args.append("-l")
                command = self._restore_skill_path_after_login(command)
            if Path(self._login_shell).name.lower() in _PIPEFAIL_SHELLS:
                command = f"set -o pipefail\n{command}"
            args.extend(["-c", command])
            # start_new_session=True puts the shell in its own session/process
            # group so a timeout can kill the *whole* tree (shell + any children
            # it spawned, e.g. ``python server.py``) via killpg, not just the
            # shell — otherwise grandchildren are orphaned and keep running.
            return await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                cwd=self.workspace_dir,
                env=self._subprocess_env,
                start_new_session=True,
            )

    def _restore_skill_path_after_login(self, command: str) -> str:
        """Reapply Box-Agent's trusted PATH prefix after login profiles run."""
        if not self._subprocess_env or not self._subprocess_env.get(
            "BOX_AGENT_SKILL_PATH_PREFIX"
        ):
            return command
        return (
            'PATH="${BOX_AGENT_SKILL_PATH_PREFIX}${PATH:+:${PATH}}"; '
            f"export PATH; {command}"
        )

    @staticmethod
    async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
        """Kill a foreground process tree/group and reap the shell.

        The shell can outlive its own foreground command in two ways that
        both leave ``communicate()`` waiting on a never-closed pipe:

        - Backgrounded grandchild inside the command (``foo &`` or
          ``find | xargs grep`` where the xargs-spawned grep still holds the
          write end of the pipe when its parent dies).
        - Windows Git-Bash ``bash.exe`` spawning native ``find.exe``/``grep.exe``
          grandchildren that survive the wrapper's TerminateProcess.

        In both cases point-killing only the wrapper leaves the grandchild
        alive, keeps the pipe open, and hangs the tool call. So we kill the
        whole tree.

        Unix (two-beat, mirrors Hermes' ``LocalEnvironment._kill_process``):
          1. ``killpg(pgid, SIGTERM)`` — give trap handlers a chance.
          2. Poll the group up to 1s; return if empty.
          3. ``killpg(pgid, SIGKILL)`` — unconditional reap.
          4. Poll up to 2s so ``process.wait()`` actually sees exits.

        Windows: ``taskkill /PID <pid> /T /F`` — ``/T`` recurses the whole
        subtree, ``/F`` force-terminates every descendant. Falls back to
        ``process.kill()`` if ``taskkill`` is missing or times out.

        Always ``await process.wait()`` at the end to avoid a zombie.
        """
        if not sys.platform.startswith("win"):
            await BashTool._kill_process_tree_unix(process)
        else:
            await BashTool._kill_process_tree_windows(process)
        try:
            await process.wait()
        except Exception:
            # Reaping is best-effort; never let cleanup mask the timeout error.
            pass

    @staticmethod
    async def _kill_process_tree_unix(process: asyncio.subprocess.Process) -> None:
        """Unix two-beat: SIGTERM → wait → SIGKILL → wait, on the process group.

        The process was spawned with ``start_new_session=True`` so its pgid
        equals its pid. We signal ``process.pid`` directly rather than calling
        ``os.getpgid`` — once the group leader is reaped ``getpgid`` raises,
        which would defeat the purpose in exactly the case that matters
        (wrapper already dead, grandchild still alive holding the pipe).
        """
        pgid = process.pid

        def _group_alive() -> bool:
            try:
                os.killpg(pgid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                # Group exists even if we cannot signal it.
                return True
            except OSError:
                return False

        async def _wait_for_group_exit(timeout: float) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not _group_alive():
                    return True
                await asyncio.sleep(0.05)
            return not _group_alive()

        # Beat 1: SIGTERM the whole group.
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            # Cannot signal the group — fall through to point-kill the wrapper.
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            return

        if await _wait_for_group_exit(1.0):
            return

        # Beat 2: SIGKILL — unconditional.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            return

        await _wait_for_group_exit(2.0)

    @staticmethod
    async def _kill_process_tree_windows(process: asyncio.subprocess.Process) -> None:
        """Windows subtree kill via ``taskkill /T /F``.

        ``process.kill()`` on Windows maps to ``TerminateProcess(pid)`` which
        does NOT touch child processes. When a Git-for-Windows ``bash.exe``
        launches ``find | xargs grep``, killing bash leaves ``find.exe`` and
        ``grep.exe`` alive — they keep the stdout pipe open and the caller's
        ``communicate()`` never returns. ``taskkill /T /F`` walks the child
        tree by parent-PID and force-terminates every descendant.
        """
        if process.returncode is not None:
            return  # already reaped by the OS

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except (FileNotFoundError, OSError):
            # taskkill missing (unusual — it's a Windows built-in). Point-kill.
            try:
                process.kill()
            except ProcessLookupError:
                pass
            return

        try:
            await asyncio.wait_for(killer.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            try:
                killer.kill()
            except ProcessLookupError:
                pass
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        except Exception:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        shell_examples = {
            "Windows": f"""Execute PowerShell commands in foreground or background.

Do NOT use Get-Content/type to read files; use read_file instead.
Do NOT use Select-String/Get-ChildItem/dir to search or list files; use search_files instead.
Reserve bash for git, builds, tests, package managers, processes, scripts, and system commands.

Parameters:
  - command (required): PowerShell command to execute
  - timeout (optional): Timeout in seconds (default: {self.default_timeout_seconds}, max: {self.max_timeout_seconds}) for foreground commands
  - run_in_background (optional): Set true for commands that must continue after the tool call
  - lifetime (optional): `turn` (default) is reclaimed when the current turn ends; `runtime` survives the turn and is reclaimed when explicitly killed or when Box-Agent exits

Tips:
  - Quote file paths with spaces: cd "My Documents"
  - Chain dependent commands with semicolon: git add . ; git commit -m "msg"
  - Use absolute paths instead of cd when possible
  - For background commands, monitor with bash_output and terminate with bash_kill
  - Use lifetime=runtime only when the user explicitly needs a service or command to remain available after the final response

Examples:
  - git status
  - npm test
  - python -u -m http.server 0 --bind 127.0.0.1 (with run_in_background=true; read the dynamic port with bash_output)""",
            "Unix": f"""Execute bash commands in foreground or background.

Do NOT use cat/head/tail to read files; use read_file instead.
Do NOT use grep/rg/find/ls to search or list files; use search_files instead.
Reserve bash for git, builds, tests, package managers, processes, scripts, and system commands.

Parameters:
  - command (required): Bash command to execute
  - timeout (optional): Timeout in seconds (default: {self.default_timeout_seconds}, max: {self.max_timeout_seconds}) for foreground commands
  - run_in_background (optional): Set true for commands that must continue after the tool call
  - lifetime (optional): `turn` (default) is reclaimed when the current turn ends; `runtime` survives the turn and is reclaimed when explicitly killed or when Box-Agent exits

Tips:
  - Quote file paths with spaces: cd "My Documents"
  - Chain dependent commands with &&: git add . && git commit -m "msg"
  - Use absolute paths instead of cd when possible
  - For background commands, monitor with bash_output and terminate with bash_kill
  - Use lifetime=runtime only when the user explicitly needs a service or command to remain available after the final response

Examples:
  - git status
  - npm test
  - ${{BOX_AGENT_PYTHON:-python3}} -u -m http.server 0 --bind 127.0.0.1 (with run_in_background=true; read the dynamic port with bash_output)""",
        }
        # When the bundled Git-for-Windows bash is active on Win, the shell is
        # POSIX bash with coreutils — use the Unix description so the LLM
        # picks bash syntax (`&&`, `$()`, `grep`, `sed`) instead of PowerShell.
        if self.is_windows and self._bundled_win_bash is None:
            return shell_examples["Windows"]
        return shell_examples["Unix"]

    @property
    def parameters(self) -> dict[str, Any]:
        cmd_desc = f"The {self.shell_name} command to execute. Quote file paths with spaces using double quotes."
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "maxLength": MAX_BASH_COMMAND_CHARS,
                    "description": cmd_desc,
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Optional: Timeout in seconds "
                        f"(default: {self.default_timeout_seconds}, "
                        f"max: {self.max_timeout_seconds}). "
                        "Only applies to foreground commands."
                    ),
                    "minimum": 1,
                    "maximum": self.max_timeout_seconds,
                    "default": self.default_timeout_seconds,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Optional: Set to true to run the command in the background. Use this for long-running commands like servers. You can monitor output using bash_output tool.",
                    "default": False,
                },
                "lifetime": {
                    "type": "string",
                    "enum": [BASH_LIFETIME_TURN, BASH_LIFETIME_RUNTIME],
                    "description": (
                        "Optional background-process lifetime. 'turn' (default) "
                        "is automatically reclaimed when the current turn ends. "
                        "Use 'runtime' only when the user explicitly asks for a "
                        "service or command to remain available after the final "
                        "response; it remains manageable with bash_output/bash_kill "
                        "until Box-Agent exits."
                    ),
                    "default": BASH_LIFETIME_TURN,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        command: str,
        timeout: int | None = None,
        run_in_background: bool = False,
        lifetime: str = BASH_LIFETIME_TURN,
    ) -> ToolResult:
        """Execute shell command with optional background execution.

        Args:
            command: The shell command to execute
            timeout: Timeout in seconds. Uses the configured default when omitted.
            run_in_background: Set true to run command in background
            lifetime: Background process lifetime (`turn` or `runtime`)

        Returns:
            BashExecutionResult with command output and status
        """

        try:
            if lifetime not in _BASH_LIFETIMES:
                error = (
                    "BASH_INVALID_LIFETIME: lifetime must be 'turn' or 'runtime'."
                )
                return BashOutputResult(
                    success=False,
                    error=error,
                    stdout="",
                    stderr=error,
                    exit_code=1,
                )
            if lifetime == BASH_LIFETIME_RUNTIME and not run_in_background:
                error = (
                    "BASH_INVALID_LIFETIME: lifetime='runtime' requires "
                    "run_in_background=true."
                )
                return BashOutputResult(
                    success=False,
                    error=error,
                    stdout="",
                    stderr=error,
                    exit_code=1,
                )
            if len(command) > MAX_BASH_COMMAND_CHARS:
                error = (
                    "BASH_ARGUMENT_TOO_LARGE: bash.command is "
                    f"{len(command)} characters; limit is {MAX_BASH_COMMAND_CHARS}. "
                    "Do not put generated file bodies, heredocs, or base64 payloads in bash. "
                    "Use ordered write_file chunks for large text artifacts."
                )
                return BashOutputResult(
                    success=False,
                    error=error,
                    stdout="",
                    stderr=error,
                    exit_code=1,
                )
            # --- Safety checks ---
            bypass_error = detect_pptx_self_check_bypass(None, command)
            if bypass_error:
                return BashOutputResult(
                    success=False,
                    error=bypass_error,
                    stdout="",
                    stderr=bypass_error,
                    exit_code=1,
                )
            image_status_error = detect_pptx_image_status_command_bypass(
                command,
                workspace_dir=self.workspace_dir,
                runtime_env=self._subprocess_env,
                shell_style=(
                    "powershell"
                    if self.is_windows and self._bundled_win_bash is None
                    else "posix"
                ),
            )
            if image_status_error:
                return BashOutputResult(
                    success=False,
                    error=image_status_error,
                    stdout="",
                    stderr=image_status_error,
                    exit_code=1,
                )

            lark_identity_error = _detect_lark_user_mode_violation(command)
            if lark_identity_error:
                return BashOutputResult(
                    success=False,
                    error=f"Blocked: {lark_identity_error}\nCommand: {command}",
                    stdout="",
                    stderr=f"Blocked: {lark_identity_error}",
                    exit_code=1,
                )

            dingtalk_workspace_error = _detect_dingtalk_workspace_violation(command)
            if dingtalk_workspace_error:
                return BashOutputResult(
                    success=False,
                    error=f"Blocked: {dingtalk_workspace_error}\nCommand: {command}",
                    stdout="",
                    stderr=f"Blocked: {dingtalk_workspace_error}",
                    exit_code=1,
                )

            obsidian_cli_error = _detect_obsidian_cli_violation(command)
            if obsidian_cli_error:
                return BashOutputResult(
                    success=False,
                    error=f"Blocked: {obsidian_cli_error}\nCommand: {command}",
                    stdout="",
                    stderr=f"Blocked: {obsidian_cli_error}",
                    exit_code=1,
                )

            # 1. Only an explicitly trusted full-access session may bypass the
            # approval prompt. Hard product blocks above and deletion backups remain.
            danger_reason = detect_dangerous_command(
                command,
                trusted_executable_references=trusted_runtime_executable_references(
                    self._subprocess_env
                ),
            )
            if danger_reason:
                if (
                    not self.bypass_dangerous_command_approval
                    and not self._has_safety_approval(command, danger_reason)
                ):
                    return BashOutputResult(
                        success=False,
                        error=(
                            f"Dangerous command requires approval: {danger_reason}. "
                            f"Command: {command}"
                        ),
                        stdout="",
                        stderr=f"Approval required: {danger_reason}",
                        exit_code=-1,
                        permission_request=_dangerous_command_permission_request(
                            command,
                            danger_reason,
                        ),
                    )
            # 2. Scope control (capability-based or legacy)
            if self._perm:
                from .permissions import (
                    FILESYSTEM_READ,
                    FILESYSTEM_WRITE,
                    expand_inline_shell_path_variables,
                    extract_absolute_paths,
                )

                permission_command = expand_inline_shell_path_variables(command)
                escape_reason = detect_scope_escape(
                    permission_command,
                    workspace_dir=self.scope_root_dir,
                )

                abs_paths = extract_absolute_paths(permission_command)
                log.debug(
                    "bash/perm/extracted paths=%s reason=%s cmd=%r",
                    abs_paths, escape_reason, command[:200],
                )

                # CONSERVATIVE: if an escape pattern has no extractable path,
                # deny it rather than silently allowing an unverifiable command.
                if escape_reason and not abs_paths:
                    return BashOutputResult(
                        success=False,
                        error=(
                            f"Command blocked (phase 1 permission engine): {escape_reason}. "
                            f"Cannot verify path permissions for this command pattern. "
                            f"Use absolute paths or request broader access."
                        ),
                        stdout="",
                        stderr=f"Blocked: {escape_reason}",
                        exit_code=1,
                    )

                for p in abs_paths:
                    if _is_temp_shell_redirect_path(permission_command, p):
                        continue
                    # Classify each concrete path separately. A redirect to
                    # another path (or 2>&1) must not make this path writable.
                    cap = (
                        FILESYSTEM_WRITE
                        if _path_requires_write_permission(permission_command, p)
                        else FILESYSTEM_READ
                    )
                    decision = self._perm.check(
                        capability=cap,
                        resource={"path": p},
                        tool_name="bash",
                    )
                    if not decision.allowed:
                        log.warning(
                            "bash/perm/denied path=%s cap=%s extracted=%s cmd=%r",
                            p, cap, abs_paths, command[:200],
                        )
                        extracted_summary = (
                            f" Extracted paths from command: {abs_paths}."
                            if len(abs_paths) > 1
                            else ""
                        )
                        return BashOutputResult(
                            success=False,
                            error=(decision.reason or "Permission denied") + extracted_summary,
                            stdout="",
                            stderr=decision.reason or "Permission denied",
                            exit_code=1,
                            permission_request=decision.permission_request,
                        )
            elif not self.allow_full_access:
                escape_reason = detect_scope_escape(command, workspace_dir=self.scope_root_dir)
                if escape_reason:
                    return BashOutputResult(
                        success=False,
                        error=(
                            f"Command blocked: {escape_reason}. "
                            f"Tools are restricted to workspace ({self.scope_root_dir}). "
                            f"Set 'allow_full_access: true' in config to allow full system access."
                        ),
                        stdout="",
                        stderr=f"Blocked: {escape_reason}",
                        exit_code=-1,
                    )

            # --- End safety checks ---

            # Consume the one-shot approval only after every other permission
            # gate has passed. Otherwise a filesystem denial between approval
            # and execution would discard the user's decision and prompt for
            # the same dangerous command again on the next retry.
            if danger_reason:
                if not self.bypass_dangerous_command_approval:
                    self._consume_safety_approval(command, danger_reason)
                # Backups also belong after scope authorization; probing or
                # copying an out-of-scope target before filesystem approval
                # would cross the very boundary being enforced.
                if "rm" in command or "rmdir" in command:
                    for target in extract_rm_targets(command, self.workspace_dir):
                        backup_file(target)

            # Validate timeout against the configured foreground bounds.
            if timeout is None or timeout < 1:
                timeout = self.default_timeout_seconds
            elif timeout > self.max_timeout_seconds:
                timeout = self.max_timeout_seconds

            if run_in_background:
                # Background execution: Create isolated process
                bash_id = str(uuid.uuid4())[:8]

                process = await self._create_subprocess(command, merge_stderr=True)

                # Create background shell and add to manager
                bg_shell = BackgroundShell(
                    bash_id=bash_id,
                    command=command,
                    process=process,
                    start_time=time.time(),
                    owner_id=self.process_owner_id,
                    lifetime=lifetime,
                )
                BackgroundShellManager.add(bg_shell)

                # Start monitoring task
                await BackgroundShellManager.start_monitor(bash_id)

                # Return immediately with bash_id
                message = f"Command started in background. Use bash_output to monitor (bash_id='{bash_id}')."
                formatted_content = f"{message}\n\nCommand: {command}\nBash ID: {bash_id}"

                return BashOutputResult(
                    success=True,
                    content=formatted_content,
                    stdout=f"Background command started with ID: {bash_id}",
                    stderr="",
                    exit_code=0,
                    bash_id=bash_id,
                    lifetime=lifetime,
                )

            else:
                # Foreground execution: Create isolated process
                process = await self._create_subprocess(command, merge_stderr=False)

                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    # Kill the whole process group and reap, so the shell and any
                    # children it spawned are torn down (no orphans, no zombie).
                    await self._kill_process_tree(process)
                    error_msg = f"Command timed out after {timeout} seconds"
                    return BashOutputResult(
                        success=False,
                        error=error_msg,
                        stdout="",
                        stderr=error_msg,
                        exit_code=-1,
                    )

                # Decode output
                stdout_text = stdout.decode("utf-8", errors="replace")
                stderr_text = stderr.decode("utf-8", errors="replace")

                original_stdout_chars = len(stdout_text)
                original_stderr_chars = len(stderr_text)
                complete_content = _format_bash_result_content(
                    stdout_text,
                    stderr_text,
                    exit_code=process.returncode or 0,
                )
                stdout_text, stderr_text, dropped = _truncate_bash_streams(
                    stdout_text, stderr_text
                )
                raw_output: dict | None = None
                if dropped:
                    raw_output = {
                        "dropped_chars": dropped,
                        "original_stdout_chars": original_stdout_chars,
                        "original_stderr_chars": original_stderr_chars,
                        "streams_combined": True,
                        "max_output_chars": MAX_BASH_OUTPUT_CHARS,
                    }
                    log.warning(
                        "bash/output_truncated command=%r dropped=%d stdout_chars=%d stderr_chars=%d",
                        command[:120], dropped, original_stdout_chars, original_stderr_chars,
                    )

                # Create result (content auto-formatted by model_validator)
                is_success = process.returncode == 0
                error_msg = None
                if not is_success:
                    error_msg = f"Command failed with exit code {process.returncode}"
                    if dropped:
                        # Failed tool messages use ``error`` (not ``content``)
                        # in model history. Keep a smaller bounded diagnostic
                        # there so the model can still diagnose the failure
                        # without duplicating the full 50K visible result.
                        diagnostic, _ = _truncate_bash_output(
                            stdout_text, "error context", limit=8_000
                        )
                        error_msg += f"\n{diagnostic.strip()}"
                    elif stderr_text:
                        error_msg += f"\n{stderr_text.strip()}"

                return BashOutputResult(
                    success=is_success,
                    error=error_msg,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    exit_code=process.returncode or 0,
                    raw_output=raw_output,
                    persistence_content=complete_content if dropped else None,
                )

        except Exception as e:
            return BashOutputResult(
                success=False,
                error=str(e),
                stdout="",
                stderr=str(e),
                exit_code=-1,
            )


class BashOutputTool(Tool):
    """Retrieve output from background bash shells."""

    max_result_size_chars = math.inf

    def __init__(self, process_owner_id: str | None = None):
        self.process_owner_id = process_owner_id

    @property
    def name(self) -> str:
        return "bash_output"

    @property
    def description(self) -> str:
        return """Retrieves output from a running or completed background bash shell.

        - Takes a bash_id parameter identifying the shell
        - Always returns only new output since the last check
        - Returns stdout and stderr output along with shell status
        - Supports optional regex filtering to show only lines matching a pattern
        - Use this tool when you need to monitor or check the output of a long-running shell
        - Shell IDs can be found using the bash tool with run_in_background=true
        - A `turn` shell is reclaimed when its turn ends; a `runtime` shell remains available until bash_kill or Box-Agent exit

        Process status values:
          - "running": Still executing
          - "completed": Finished successfully
          - "failed": Finished with error
          - "terminated": Was terminated
          - "error": Error occurred

        Example: bash_output(bash_id="abc12345")"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bash_id": {
                    "type": "string",
                    "description": "The ID of the background shell to retrieve output from. Shell IDs are returned when starting a command with run_in_background=true.",
                },
                "filter_str": {
                    "type": "string",
                    "description": "Optional regular expression to filter the output lines. Only lines matching this regex will be included in the result. Any lines that do not match will no longer be available to read.",
                },
            },
            "required": ["bash_id"],
        }

    async def execute(
        self,
        bash_id: str,
        filter_str: str | None = None,
    ) -> BashOutputResult:
        """Retrieve output from background shell.

        Args:
            bash_id: The unique identifier of the background shell
            filter_str: Optional regex pattern to filter output lines

        Returns:
            BashOutputResult with shell output including stdout, stderr, status, and success flag
        """

        try:
            # Get background shell from manager
            bg_shell = BackgroundShellManager.get(bash_id, self.process_owner_id)
            if not bg_shell:
                available_ids = BackgroundShellManager.get_available_ids(
                    self.process_owner_id
                )
                return BashOutputResult(
                    success=False,
                    error=f"Shell not found: {bash_id}. Available: {available_ids or 'none'}",
                    stdout="",
                    stderr="",
                    exit_code=-1,
                )

            # Get new output
            new_lines = bg_shell.get_new_output(filter_pattern=filter_str)
            stdout = "\n".join(new_lines) if new_lines else ""
            exit_code = bg_shell.exit_code if bg_shell.exit_code is not None else 0
            complete_content = _format_bash_result_content(
                stdout,
                "",
                bash_id=bash_id,
                exit_code=exit_code,
            )

            # Truncate large batches for the same reason foreground execution
            # does — `bash_output` can pull a very large accumulated stream
            # (e.g. a long-running server that emitted MBs since the last read).
            stdout, _stderr, dropped = _truncate_bash_streams(stdout, "")
            raw_output: dict | None = None
            if dropped:
                raw_output = {
                    "dropped_chars": dropped,
                    "original_stdout_chars": len("\n".join(new_lines)),
                    "original_stderr_chars": 0,
                    "streams_combined": False,
                    "max_output_chars": MAX_BASH_OUTPUT_CHARS,
                }
                log.warning(
                    "bash/background_output_truncated bash_id=%s stdout_dropped=%d",
                    bash_id, dropped,
                )

            return BashOutputResult(
                success=True,
                stdout=stdout,
                stderr="",  # Background shells combine stdout/stderr
                exit_code=exit_code,
                bash_id=bash_id,
                lifetime=bg_shell.lifetime,
                raw_output=raw_output,
                persistence_content=complete_content if dropped else None,
            )

        except Exception as e:
            return BashOutputResult(
                success=False,
                error=f"Failed to get bash output: {str(e)}",
                stdout="",
                stderr=str(e),
                exit_code=-1,
            )


class BashKillTool(Tool):
    """Terminate a running background bash shell."""

    def __init__(self, process_owner_id: str | None = None):
        self.process_owner_id = process_owner_id

    @property
    def name(self) -> str:
        return "bash_kill"

    @property
    def description(self) -> str:
        return """Kills a running background bash shell by its ID.

        - Takes a bash_id parameter identifying the shell to kill
        - Attempts graceful termination (SIGTERM) first, then forces (SIGKILL) if needed
        - Returns the final status and any remaining output before termination
        - Cleans up all resources associated with the shell
        - Use this tool when you need to terminate a long-running shell
        - Shell IDs can be found using the bash tool with run_in_background=true
        - Both `turn` and `runtime` shells remain explicitly stoppable by their owning session

        Example: bash_kill(bash_id="abc12345")"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bash_id": {
                    "type": "string",
                    "description": "The ID of the background shell to terminate. Shell IDs are returned when starting a command with run_in_background=true.",
                },
            },
            "required": ["bash_id"],
        }

    async def execute(self, bash_id: str) -> BashOutputResult:
        """Terminate a background shell process.

        Args:
            bash_id: The unique identifier of the background shell to terminate

        Returns:
            BashOutputResult with termination status and remaining output
        """

        try:
            # Get remaining output before termination
            bg_shell = BackgroundShellManager.get(bash_id, self.process_owner_id)
            if bg_shell:
                remaining_lines = bg_shell.get_new_output()
            else:
                remaining_lines = []

            # Terminate through manager (handles all cleanup)
            bg_shell = await BackgroundShellManager.terminate(
                bash_id, self.process_owner_id
            )

            # Get remaining output
            stdout = "\n".join(remaining_lines) if remaining_lines else ""

            return BashOutputResult(
                success=True,
                stdout=stdout,
                stderr="",
                exit_code=bg_shell.exit_code if bg_shell.exit_code is not None else 0,
                bash_id=bash_id,
                lifetime=bg_shell.lifetime,
            )

        except ValueError as e:
            # Shell not found
            available_ids = BackgroundShellManager.get_available_ids()
            return BashOutputResult(
                success=False,
                error=f"{str(e)}. Available: {available_ids or 'none'}",
                stdout="",
                stderr=str(e),
                exit_code=-1,
            )
        except Exception as e:
            return BashOutputResult(
                success=False,
                error=f"Failed to terminate bash shell: {str(e)}",
                stdout="",
                stderr=str(e),
                exit_code=-1,
            )
