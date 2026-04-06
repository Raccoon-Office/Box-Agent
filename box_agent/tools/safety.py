"""Safety utilities for agent tools.

Provides dangerous command detection, path validation, file backup,
and user confirmation for destructive operations.
"""

import re
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Global trash directory for file backups
TRASH_DIR = Path.home() / ".box-agent" / "trash"

# Dangerous command patterns — each is (compiled_regex, human_readable_reason)
_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s"), "rm: removes files/directories"),
    (re.compile(r"\brmdir\s"), "rmdir: removes directories"),
    (re.compile(r"\bkill\s"), "kill: terminates processes"),
    (re.compile(r"\bkillall\s"), "killall: terminates processes by name"),
    (re.compile(r"\bpkill\s"), "pkill: terminates processes by pattern"),
    (re.compile(r"\bmkfs[\s.]"), "mkfs: formats filesystem"),
    (re.compile(r"\bdd\s"), "dd: raw disk write"),
    (re.compile(r"\bshutdown\b"), "shutdown: shuts down the system"),
    (re.compile(r"\breboot\b"), "reboot: reboots the system"),
    (re.compile(r"\bsudo\s"), "sudo: runs command as root"),
    (re.compile(r"\bchmod\s"), "chmod: changes file permissions"),
    (re.compile(r"\bchown\s"), "chown: changes file ownership"),
    (re.compile(r"(?<!2)>\s*/dev/null"), "redirect to /dev/null"),
    (re.compile(r"\bmv\s.*\s/dev/null\b"), "mv to /dev/null: destroys file"),
    (re.compile(r">\|?\s*/etc/"), "write to /etc: modifies system config"),
    (re.compile(r"\bformat\s"), "format: formats disk"),
    (re.compile(r"\bdiskutil\s+erase"), "diskutil erase: erases disk"),
    (re.compile(r"\blaunchctl\s"), "launchctl: manages system services"),
]

# Patterns indicating scope escape (absolute paths, cd to outside workspace)
_ESCAPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcd\s+/"), "cd to absolute path"),
    (re.compile(r"\bcd\s+~"), "cd to home directory"),
    (re.compile(r'(?:^|\s|;|&&|\|\|)(?:cat|less|head|tail|grep|awk|sed)\s+/'), "read from absolute path"),
    (re.compile(r'(?:^|\s|;|&&|\|\|)(?:cp|mv|ln)\s+.*/'), "file operation with absolute path"),
    (re.compile(r'>\s*/'), "redirect to absolute path"),
]


def detect_dangerous_command(command: str) -> str | None:
    """Check if a shell command contains dangerous patterns.

    Args:
        command: The shell command string to check.

    Returns:
        A human-readable reason string if the command is dangerous, or None if safe.
    """
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return reason
    return None


def detect_scope_escape(command: str, workspace_dir: str | None = None) -> str | None:
    """Check if a shell command attempts to escape the workspace.

    This is a heuristic check — not a security sandbox. It catches common
    patterns like `cd /`, absolute path references, etc.

    If ``workspace_dir`` is provided, absolute paths that stay within the
    workspace are allowed (e.g. ``cd /mnt/workspace/subdir`` when the
    workspace is ``/mnt/workspace``).

    Args:
        command: The shell command string to check.
        workspace_dir: Absolute path to the current workspace (optional).

    Returns:
        A reason string if escape is detected, or None if the command looks safe.
    """
    for pattern, reason in _ESCAPE_PATTERNS:
        match = pattern.search(command)
        if match:
            # If workspace_dir is set, check whether the absolute path
            # is inside the workspace — if so, it's not an escape.
            if workspace_dir:
                # Extract the absolute path from the command.
                # Strategy: find the first absolute path (starting with /)
                # in the matched region and onwards.
                path_token = None
                matched_text = command[match.start():]
                abs_match = re.search(r'(/[^\s;|&]*)', matched_text)
                if abs_match:
                    path_token = abs_match.group(1)
                # For "cd" specifically, grab the argument directly
                if reason.startswith("cd"):
                    cd_match = re.search(r'\bcd\s+([^\s;|&]+)', command)
                    if cd_match:
                        path_token = cd_match.group(1)
                if path_token:
                    try:
                        resolved = str(Path(path_token).resolve())
                        ws_resolved = str(Path(workspace_dir).resolve())
                        if resolved == ws_resolved or resolved.startswith(ws_resolved + "/"):
                            continue  # path is within workspace — not an escape
                    except Exception:
                        pass
            return reason
    return None


async def ask_user_confirmation(message: str, non_interactive: bool = False) -> bool:
    """Ask the user to confirm a dangerous operation via terminal.

    Args:
        message: Description of the dangerous operation.
        non_interactive: If True, always returns False (reject) without prompting.

    Returns:
        True if the user confirms, False otherwise.
    """
    if non_interactive:
        return False

    try:
        print(f"\n⚠️  {message}")
        response = input("Continue? [y/N] ").strip().lower()
        return response in ("y", "yes", "ok", "可以", "是", "确认", "好", "行")
    except (EOFError, KeyboardInterrupt):
        return False


def validate_path_in_workspace(file_path: Path, workspace_dir: Path) -> str | None:
    """Validate that a resolved path is within the workspace directory.

    Resolves both paths to catch ../ traversal and symlink escapes.

    Args:
        file_path: The path to validate (should already be absolute).
        workspace_dir: The workspace root directory.

    Returns:
        An error message if the path is outside workspace, or None if valid.
    """
    try:
        resolved = file_path.resolve()
        workspace_resolved = workspace_dir.resolve()
        if not str(resolved).startswith(str(workspace_resolved) + "/") and resolved != workspace_resolved:
            return (
                f"Access denied: {file_path} is outside the workspace ({workspace_dir}). "
                f"Set 'allow_full_access: true' in config to allow full system access."
            )
    except (OSError, ValueError) as e:
        return f"Path validation error: {e}"
    return None


def backup_file(file_path: Path) -> Path | None:
    """Backup a file to the global trash directory before modification.

    Copies the file to ~/.box-agent/trash/{timestamp}/{original_path}.
    Uses shutil.copy2 to preserve file metadata.

    Args:
        file_path: The file to backup (must exist).

    Returns:
        The backup path if successful, or None if the file doesn't exist or backup fails.
    """
    try:
        resolved = file_path.resolve()
        if not resolved.exists() or not resolved.is_file():
            return None

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        # Preserve original path structure under trash dir
        # e.g., /home/user/project/foo.py → ~/.box-agent/trash/2024-01-01_120000_000000/home/user/project/foo.py
        backup_path = TRASH_DIR / timestamp / str(resolved).lstrip("/")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, backup_path)
        return backup_path
    except Exception:
        # Backup is best-effort; don't block the operation
        return None


def extract_rm_targets(command: str, cwd: str | None = None) -> list[Path]:
    """Extract file/directory targets from an rm command (best-effort).

    Parses the command to find paths that rm would delete.
    Skips flags (arguments starting with -).

    Args:
        command: The shell command string containing rm.
        cwd: Current working directory for resolving relative paths.

    Returns:
        List of resolved Path objects that rm would target.
    """
    targets: list[Path] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return targets

    # Find the rm command and extract targets after it
    found_rm = False
    for token in tokens:
        if not found_rm:
            if token in ("rm", "rmdir"):
                found_rm = True
            # Also handle chained commands: ... && rm ...
            elif token in (";", "&&", "||"):
                continue
            continue

        # Skip flags
        if token.startswith("-"):
            continue
        # Skip command separators — reset rm search
        if token in (";", "&&", "||", "|"):
            found_rm = False
            continue

        path = Path(token)
        if not path.is_absolute() and cwd:
            path = Path(cwd) / path
        targets.append(path.resolve())

    return targets
