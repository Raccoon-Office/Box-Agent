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
    # Home directory references: ~ and $HOME anywhere as path tokens
    (re.compile(r'(?<!\w)~(?=/|\s|;|"|\'|&|\||$)'), "command references home directory via ~"),
    (re.compile(r'\$HOME\b'), "command references home directory via $HOME"),
]

# /dev/ special files that are safe to redirect to / read from.
# Only sinks and standard streams — NOT unbounded sources like
# /dev/zero, /dev/random, /dev/urandom which can OOM via communicate().
_DEV_ALLOWLIST = re.compile(
    r"^/dev/(null|stdin|stdout|stderr)$"
)


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


def _extract_path_token(command: str, match: re.Match, reason: str) -> str | None:
    """Extract the path token from a scope-escape match.

    Handles absolute paths, ``cd`` arguments, ``~`` and ``$HOME`` expansion.
    Factored out so both the /dev/ allowlist and workspace check can use it
    regardless of whether ``workspace_dir`` is set.
    """
    path_token = None
    matched_text = command[match.start():]

    # Try absolute path first
    abs_match = re.search(r'(/[^\s;|&]*)', matched_text)
    if abs_match:
        path_token = abs_match.group(1)

    # For "cd" specifically, grab the argument directly
    if reason.startswith("cd"):
        cd_match = re.search(r'\bcd\s+([^\s;|&]+)', command)
        if cd_match:
            path_token = cd_match.group(1)

    # For ~ and $HOME patterns, extract and expand the path
    if path_token is None or path_token.startswith("~") or "$HOME" in (path_token or ""):
        home_str = str(Path.home())
        tilde_match = re.search(r'(?<!\w)(~(?:/[^\s;|&"\']*)?)', matched_text)
        home_var_match = re.search(r'(\$HOME(?:/[^\s;|&"\']*)?)', matched_text)
        if tilde_match:
            path_token = home_str + tilde_match.group(1)[1:]  # strip leading ~
        elif home_var_match:
            path_token = home_str + home_var_match.group(1)[5:]  # strip leading $HOME

    return path_token


# Regex to extract literal absolute paths from a command string.
# Excludes URL schemes (://path) and protocol-relative (//host) patterns.
_ABS_PATH_SCAN_RE = re.compile(r'(?<![:/])(/(?!/)[^\s;|&"\']+)')

# URL pattern stripped before scanning for filesystem paths.
_URL_RE = re.compile(r'https?://[^\s;|&"\']+')


def _path_is_safe(path: str, workspace_dir: str | None) -> bool:
    """Return True if *path* is a /dev/ special file or inside the workspace."""
    if _DEV_ALLOWLIST.match(path):
        return True
    if workspace_dir:
        try:
            resolved = str(Path(path).resolve())
            ws_resolved = str(Path(workspace_dir).resolve())
            if resolved == ws_resolved or resolved.startswith(ws_resolved + "/"):
                return True
        except Exception:
            pass
    return False


def _command_has_unsafe_paths(command: str, workspace_dir: str | None) -> bool:
    """Scan *command* for absolute paths that are not /dev/ and not in workspace.

    This is used as a secondary check when the primary matched path is
    allowlisted — the command may still reference other unsafe paths
    (e.g. ``cat /dev/null /etc/passwd``).

    URLs (``https://...``) are stripped before scanning so their path
    components are not misclassified as filesystem paths.
    """
    cleaned = _URL_RE.sub("", command)
    for m in _ABS_PATH_SCAN_RE.finditer(cleaned):
        p = m.group(1).rstrip(";")
        if not _path_is_safe(p, workspace_dir):
            return True
    return False


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
            path_token = _extract_path_token(command, match, reason)

            # If the primary matched path is safe (/dev/ or workspace),
            # still scan the full command for other unsafe absolute paths
            # before skipping — prevents bypass via mixed paths like
            # ``cat /dev/null /etc/passwd``.
            if path_token and _path_is_safe(path_token, workspace_dir):
                if not _command_has_unsafe_paths(command, workspace_dir):
                    continue  # all paths in command are safe
                # Fall through — other unsafe paths exist

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
