"""Safety utilities for agent tools.

Provides dangerous command detection, path validation, file backup,
and user confirmation for destructive operations.
"""

import os
import re
import shutil
import stat
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from .shell_inspection import ShellInspection, ShellInvocation, inspect_shell_command

# Global trash directory for file backups
TRASH_DIR = Path.home() / ".box-agent" / "trash"

_DANGEROUS_EXECUTABLE_REASONS = {
    "chmod": "chmod: changes file permissions",
    "chown": "chown: changes file ownership",
    "dd": "dd: raw disk write",
    "format": "format: formats disk",
    "kill": "kill: terminates processes",
    "killall": "killall: terminates processes by name",
    "launchctl": "launchctl: manages system services",
    "pkill": "pkill: terminates processes by pattern",
    "reboot": "reboot: reboots the system",
    "rm": "rm: removes files/directories",
    "rmdir": "rmdir: removes directories",
    "shutdown": "shutdown: shuts down the system",
}
_DANGEROUS_CANDIDATE_RE = re.compile(
    r"(?i)(?:^|[;&|\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:command\s+|env(?:\s+\S+)*?\s+|exec\s+|nohup\s+|sudo\s+)?"
    r"(?:rm|rmdir|kill|killall|pkill|mkfs(?:\.[^\s;&|]+)?|dd|shutdown|"
    r"reboot|sudo|chmod|chown|format|diskutil|launchctl)(?:\s|$)"
)
_TRUSTED_DYNAMIC_EXECUTABLE_REFERENCES = frozenset(
    {
        "$BOX_AGENT_DINGTALK_CLI",
        "${BOX_AGENT_DINGTALK_CLI}",
    }
)
_RUNTIME_EXECUTABLE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "BOX_AGENT_NODE": ("node",),
    "BOX_AGENT_NPM": ("npm",),
    "BOX_AGENT_NPX": ("npx",),
    "BOX_AGENT_PYTHON": ("python3",),
    "BOX_AGENT_PYTHON3": ("python3",),
    "BOX_AGENT_SANDBOX_PYTHON": ("python3",),
    "BOX_AGENT_BUNDLED_PYTHON": ("python3",),
}
_SHELL_ASSIGNMENT_WORD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_SHELL_VARIABLE_NAME_RE = re.compile(
    r"^(?:\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}|%([A-Za-z_][A-Za-z0-9_]*)%)$"
)
_MALFORMED_RUNTIME_FALLBACK_RE = re.compile(
    r"(?P<quote>['\"]?)\$(?P<name>BOX_AGENT_(?:PYTHON|PYTHON3|NPM|NPX|NODE))"
    r":-(?P<fallback>[A-Za-z0-9_.+-]+)(?P=quote)"
)

# Patterns indicating scope escape (absolute paths, cd to outside workspace)
_ESCAPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcd\s+/"), "cd to absolute path"),
    (re.compile(r"\bcd\s+~"), "cd to home directory"),
    # Windows: `cd C:\...` / `cd D:/...`. Single drive letter only so URL
    # schemes (`http:`, `file:`) aren't mistaken for drives.
    (re.compile(r"\bcd\s+[A-Za-z]:[\\/]"), "cd to Windows absolute path"),
    (re.compile(r'(?:^|\s|;|&&|\|\|)(?:cat|less|head|tail|grep|awk|sed)\s+/'), "read from absolute path"),
    # Windows read tools: `cat/type/Get-Content C:\...`. Include PowerShell
    # equivalents that models reach for on Windows once POSIX tools are blocked.
    (re.compile(r'(?:^|\s|;|&&|\|\|)(?:cat|type|less|head|tail|grep|awk|sed|Get-Content|gc)\s+[A-Za-z]:[\\/]'),
     "read from Windows absolute path"),
    # `cp`/`mv`/`ln` writing to an absolute path. Match only when the FIRST
    # argument (source) starts with `/`. The previous form `\s+.*/` matched
    # any `cp ... /` anywhere on the line, which falsely flagged commands like
    # `cp slides/x.png output/rendered/` (all relative, just contains a slash).
    (re.compile(r'(?:^|\s|;|&&|\|\|)(?:cp|mv|ln)\s+/'), "file operation with absolute path"),
    # Windows copy/move first-arg drive-letter path.
    (re.compile(r'(?:^|\s|;|&&|\|\|)(?:cp|mv|ln|copy|move|Copy-Item|Move-Item)\s+[A-Za-z]:[\\/]'),
     "file operation with Windows absolute path"),
    # Real shell redirects: `>`/`>>` (optionally with fd prefix like `2>`) followed
    # by `/`. Must be preceded by whitespace, start-of-string, or an fd digit so
    # that `>/` appearing inside an HTML tag or a sed/perl substitution body
    # (e.g. `<\/h1>/<h1>` after the closing `>` of the previous tag) is not
    # mis-classified as a redirect to an absolute path.
    (re.compile(r'(?:^|[\s\d])>{1,2}\s*/'), "redirect to absolute path"),
    # Redirect to Windows absolute path.
    (re.compile(r'(?:^|[\s\d])>{1,2}\s*[A-Za-z]:[\\/]'), "redirect to Windows absolute path"),
    # Home directory references: ~ and $HOME anywhere as path tokens
    (re.compile(r'(?<!\w)~(?=/|\s|;|"|\'|&|\||$)'), "command references home directory via ~"),
    (re.compile(r'\$HOME\b'), "command references home directory via $HOME"),
]

# sed/perl-style substitution `s<delim>pattern<delim>replacement<delim>[flags]`.
# Stripped from the command BEFORE pattern matching so the regex bodies in
# `sed 's/.../.../g'`, `perl -0pi -e 's|...|...|'`, etc. do not yield false
# positives like "redirect to absolute path" via a stray `>/` inside the
# pattern (e.g. `<\/strong>/<strong>` ends with `>/<`). Mirror of the same
# constant in `permissions.py::extract_absolute_paths`.
_SED_SUBST_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_])           # `s` must not follow a word char
    s
    ([/|#,@!:%])                # the chosen delimiter (group 1)
    (?:\\.|(?!\1).)*            # pattern body: escaped char or non-delim
    \1
    (?:\\.|(?!\1).)*            # replacement body
    \1
    [a-zA-Z]*                   # optional flags (g, i, m, ...)
    """,
    re.VERBOSE,
)

# /dev/ special files that are safe to redirect to / read from.
# Only sinks and standard streams — NOT unbounded sources like
# /dev/zero, /dev/random, /dev/urandom which can OOM via communicate().
_DEV_ALLOWLIST = re.compile(
    r"^/dev/(null|stdin|stdout|stderr)$"
)


def trusted_runtime_executable_references(
    environment: Mapping[str, str] | None,
) -> frozenset[str]:
    """Return trusted shell references backed by injected executable paths."""

    if not environment:
        return frozenset()
    references: set[str] = set()
    for name, fallbacks in _RUNTIME_EXECUTABLE_FALLBACKS.items():
        value = environment.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        executable = Path(value)
        try:
            trusted = (
                executable.is_absolute()
                and executable.is_file()
                and os.access(executable, os.X_OK)
            )
        except OSError:
            trusted = False
        if not trusted:
            continue
        references.update({f"${name}", f"${{{name}}}", f"%{name}%"})
        references.update(f"${{{name}:-{fallback}}}" for fallback in fallbacks)
    return frozenset(references)


def detect_dangerous_command(
    command: str,
    *,
    trusted_executable_references: Iterable[str] = (),
) -> str | None:
    """Check whether the shell actually invokes a dangerous operation.

    Args:
        command: The shell command string to check.
        trusted_executable_references: Exact variable references whose current
            environment values were verified as executable absolute paths.

    Returns:
        A human-readable reason string if the command is dangerous, or None if safe.
    """
    trusted_references = _TRUSTED_DYNAMIC_EXECUTABLE_REFERENCES | frozenset(
        trusted_executable_references
    )
    inspection = inspect_shell_command(command)
    mutated_variables, unknown_environment_mutation = _shell_environment_mutations(
        inspection
    )
    for invocation in inspection.invocations:
        reference_variable = _shell_reference_variable(invocation.executable)
        trusted_dynamic_reference = (
            invocation.executable in trusted_references
            and invocation.dynamic_executable_evidence == invocation.executable
            and not unknown_environment_mutation
            and reference_variable not in mutated_variables
        )
        if (
            invocation.dynamic_executable_sources
            and not trusted_dynamic_reference
        ):
            return "Dynamically constructed shell executable"
        if reason := _dangerous_invocation_reason(invocation):
            return reason
    if any(_DANGEROUS_CANDIDATE_RE.search(region) for region in inspection.ambiguous_regions):
        return "Unparseable potentially dangerous shell command"
    for redirection in inspection.redirections:
        if ">" in redirection.operator and redirection.target.startswith("/etc/"):
            return "write to /etc: modifies system config"
    return None


def detect_invalid_runtime_executable_syntax(command: str) -> str | None:
    """Return guidance for a common malformed shell fallback expression."""

    match = _MALFORMED_RUNTIME_FALLBACK_RE.search(command)
    if match is None:
        return None
    name = match.group("name")
    fallback = match.group("fallback")
    return (
        "BASH_INVALID_RUNTIME_EXECUTABLE: malformed shell parameter expansion "
        f"'${name}:-{fallback}'. Use \"${name}\" when the injected runtime is "
        f"available, or \"${{{name}:-{fallback}}}\" for an explicit fallback."
    )


def is_safe_scratch_cleanup(command: str, scratch_root: str | Path | None) -> bool:
    """Return whether a simple rm/rmdir only targets real scratch descendants."""

    if not scratch_root:
        return False
    inspection = inspect_shell_command(command)
    if (
        inspection.has_control_operators
        or inspection.substitutions
        or inspection.redirections
        or inspection.ambiguous_regions
        or len(inspection.invocations) != 1
    ):
        return False
    invocation = inspection.invocations[0]
    executable = invocation.executable.casefold()
    if (
        executable not in {"rm", "rmdir"}
        or invocation.prefix
        or invocation.dynamic_executable_sources
    ):
        return False

    allowed_options = (
        {
            "-f",
            "-r",
            "-R",
            "-d",
            "-v",
            "-i",
            "-I",
            "--force",
            "--recursive",
            "--dir",
            "--verbose",
        }
        if executable == "rm"
        else {
            "-p",
            "-v",
            "--parents",
            "--verbose",
            "--ignore-fail-on-non-empty",
        }
    )
    targets: list[str] = []
    options_done = False
    for argument in invocation.arguments:
        if not options_done and argument == "--":
            options_done = True
        elif not options_done and argument.startswith("-"):
            if argument in allowed_options or (
                not argument.startswith("--")
                and set(argument[1:]).issubset(
                    set("fRrdviI") if executable == "rm" else set("pv")
                )
            ):
                continue
            return False
        else:
            targets.append(argument)
    if not targets:
        return False

    try:
        root = Path(scratch_root).expanduser().resolve(strict=True)
    except OSError:
        return False
    for target in targets:
        expanded = _expand_scratch_reference(target, root)
        if expanded is None or not _is_real_scratch_descendant(expanded, root):
            return False
    return True


def _expand_scratch_reference(target: str, root: Path) -> Path | None:
    for reference in ("${BOX_AGENT_SCRATCH_DIR}", "$BOX_AGENT_SCRATCH_DIR"):
        if target == reference:
            return root
        prefix = reference + "/"
        if target.startswith(prefix):
            suffix = target[len(prefix) :]
            return None if any(char in suffix for char in "*?[]{}") else root / suffix
    if any(char in target for char in "*?[]{}"):
        return None
    path = Path(target).expanduser()
    return path if path.is_absolute() else None


def _is_real_scratch_descendant(target: Path, root: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(target)))
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        return False
    if not relative.parts:
        return False
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            return False
        current /= part
        try:
            stats = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(stats.st_mode):
            return False
    return True


def _normalized_executable(value: str) -> str:
    executable = value.strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable.endswith((".exe", ".cmd", ".com")):
        executable = executable.rsplit(".", 1)[0]
    return executable


def _shell_reference_variable(reference: str) -> str | None:
    match = _SHELL_VARIABLE_NAME_RE.fullmatch(reference)
    if match is None:
        return None
    return next((value for value in match.groups() if value), None)


def _shell_environment_mutations(
    inspection: ShellInspection,
) -> tuple[set[str], bool]:
    mutated: set[str] = set()
    unknown = False
    for invocation in inspection.invocations:
        words = invocation.words
        executable = _normalized_executable(invocation.executable)
        for word in words:
            if match := _SHELL_ASSIGNMENT_WORD_RE.match(word):
                mutated.add(match.group(1))
        if executable in {"read", "unset"}:
            mutated.update(
                word
                for word in invocation.arguments
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", word)
            )
        if executable == "printf" and "-v" in invocation.arguments:
            index = invocation.arguments.index("-v")
            if index + 1 < len(invocation.arguments):
                name = invocation.arguments[index + 1]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    mutated.add(name)
        if executable in {".", "eval", "source"}:
            unknown = True
    return mutated, unknown


def _dangerous_invocation_reason(invocation: ShellInvocation) -> str | None:
    prefix_names = {_normalized_executable(value) for value in invocation.prefix}
    if "sudo" in prefix_names:
        return "sudo: runs command as root"

    executable = _normalized_executable(invocation.executable)
    if executable == "sudo":
        return "sudo: runs command as root"
    if executable == "mkfs" or executable.startswith("mkfs."):
        return "mkfs: formats filesystem"
    if executable == "mv" and any(
        argument.rstrip("/") == "/dev/null"
        for argument in invocation.arguments
    ):
        return "mv to /dev/null: destroys file"
    if executable == "diskutil" and any(
        argument.casefold() in {"erase", "erasedisk", "erasevolume", "secureerase"}
        for argument in invocation.arguments
    ):
        return "diskutil erase: erases disk"
    return _DANGEROUS_EXECUTABLE_REASONS.get(executable)


def _extract_path_token(command: str, match: re.Match, reason: str) -> str | None:
    """Extract the path token from a scope-escape match.

    Handles absolute paths (POSIX and Windows drive-letter), ``cd`` arguments,
    ``~`` and ``$HOME`` expansion.
    Factored out so both the /dev/ allowlist and workspace check can use it
    regardless of whether ``workspace_dir`` is set.
    """
    path_token = None
    matched_text = command[match.start():]

    # Try Windows drive-letter path first (`C:\...` / `D:/...`).
    win_match = re.search(r'([A-Za-z]:[\\/][^\s;|&"\']*)', matched_text)
    if win_match:
        path_token = win_match.group(1)

    # Try POSIX absolute path next
    if path_token is None:
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

# Windows drive-letter absolute path scanner. Must be at a token boundary
# (start-of-string or shell separator) with a single letter drive so URL
# schemes (`http:`, `file:`) are not mistaken for drives.
_WIN_ABS_PATH_SCAN_RE = re.compile(r'(?:^|[\s;|&])([A-Za-z]:[\\/][^\s;|&"\']*)')

# URL pattern stripped before scanning for filesystem paths.
_URL_RE = re.compile(r'https?://[^\s;|&"\']+')


def _is_path_inside(target: Path, root: Path) -> bool:
    """Component-wise containment check (both paths must be resolved).

    Uses ``Path.relative_to`` instead of ``str.startswith`` so it works with
    both ``/`` and ``\\`` separators (the old ``startswith(root + "/")`` form
    never matched on Windows) and does not false-match sibling prefixes like
    ``/x/Downloads`` against root ``/x/Download``.
    """
    if target == root:
        return True
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _path_is_safe(path: str, workspace_dir: str | None) -> bool:
    """Return True if *path* is a /dev/ special file or inside the workspace."""
    if _DEV_ALLOWLIST.match(path):
        return True
    if workspace_dir:
        try:
            return _is_path_inside(Path(path).resolve(), Path(workspace_dir).resolve())
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
    # Drop sed/perl substitution bodies so their literal `/…/` regex segments
    # are not scanned as filesystem paths (mirrors the same strip in
    # `extract_absolute_paths`).
    cleaned = _SED_SUBST_RE.sub(" ", cleaned)
    for m in _ABS_PATH_SCAN_RE.finditer(cleaned):
        p = m.group(1).rstrip(";")
        if not _path_is_safe(p, workspace_dir):
            return True
    for m in _WIN_ABS_PATH_SCAN_RE.finditer(cleaned):
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
    # Strip sed/perl substitution bodies first so their `s/.../.../`
    # regex literals can't be mis-read as redirects, `cat /…` reads, etc.
    # Replace with same-length spaces so all downstream string indices
    # (used by `_extract_path_token`) stay aligned with the original.
    sanitized = _SED_SUBST_RE.sub(lambda m: " " * (m.end() - m.start()), command)

    for pattern, reason in _ESCAPE_PATTERNS:
        match = pattern.search(sanitized)
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
        if not _is_path_inside(resolved, workspace_resolved):
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
    seen: set[Path] = set()
    for invocation in inspect_shell_command(command).invocations:
        if _normalized_executable(invocation.executable) not in {"rm", "rmdir"}:
            continue
        options_finished = False
        for token in invocation.arguments:
            if token == "--":
                options_finished = True
                continue
            if not options_finished and token.startswith("-"):
                continue
            if re.match(r"^\d*(?:>|>>|<)", token):
                continue
            path = Path(token)
            if not path.is_absolute() and cwd:
                path = Path(cwd) / path
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                targets.append(resolved)

    return targets
