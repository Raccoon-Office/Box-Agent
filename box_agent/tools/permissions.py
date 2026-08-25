"""Capability-based permission engine.

Phase 1 capabilities:
- filesystem.read  — read file/directory
- filesystem.write — write/edit/delete file
- memory.openclaw_import — import memory from OpenClaw

PermissionDecision = PermissionEngine(CapabilityPolicy).check(capability, resource)

permission_request payload format (canonical, matches box-agent-permissions.md):
{
    "type": "permission_request",
    "scope": "filesystem",          # capability namespace
    "requested_scope": "user_home", # scope being requested
    "path": "/Users/.../file",      # flat path field (filesystem only)
    "reason": "...",
    "temporary_supported": true,
    "persistent_supported": true
}
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from box_agent.config import Config

log = logging.getLogger(__name__)

# Phase 1 capability constants
FILESYSTEM_READ = "filesystem.read"
FILESYSTEM_WRITE = "filesystem.write"
MEMORY_OPENCLAW_IMPORT = "memory.openclaw_import"


class CapabilityPolicy(BaseModel):
    """Immutable capability policy. Constructed once, never mutated.

    Canonical config field is a single ``filesystem_scope`` (maps to
    ``officev3.permissions.filesystem.scope``). Read and write share the same
    scope — no read/write split in the protocol.

    ``allowed_directories`` is an additive whitelist layered on top of the
    selected base scope.
    """

    filesystem_scope: str = "session_workspace"
    allowed_directories: tuple[str, ...] = ()
    openclaw_import_enabled: bool = True
    session_workspace_root: str = ""

    @classmethod
    def from_config(cls, config: Config) -> CapabilityPolicy:
        o = config.officev3
        return cls(
            filesystem_scope=o.permissions.filesystem.scope,
            allowed_directories=tuple(o.permissions.filesystem.allowed_directories),
            openclaw_import_enabled=o.permissions.memory.openclaw_import,
            session_workspace_root=o.paths.session_workspace_root,
        )

    def with_overrides(self, overrides: dict) -> CapabilityPolicy:
        """Produce a new CapabilityPolicy by applying session-level overrides.

        Canonical override key is ``filesystem.scope``.
        Never mutates self. Returns a new instance.
        """
        updates: dict = {}
        fs = overrides.get("filesystem", {})
        if isinstance(fs, dict) and "scope" in fs:
            updates["filesystem_scope"] = fs["scope"]

        mem = overrides.get("memory", {})
        if isinstance(mem, dict) and "openclaw_import" in mem:
            updates["openclaw_import_enabled"] = mem["openclaw_import"]

        if not updates:
            return self
        return self.model_copy(update=updates)

    def with_filesystem_overrides(
        self,
        session_workspace_root: str | None = None,
        allowed_directories: tuple[str, ...] | list[str] | None = None,
        filesystem_scope: str | None = None,
        replace_allowed_directories: bool = False,
    ) -> CapabilityPolicy:
        """Apply per-session filesystem context from a trusted host.

        Unlike ``with_overrides`` this is intended for the host (e.g. an ACP
        client like office-raccoon) to declare *where* the session lives —
        the workspace root and additional whitelisted directories — rather
        than for runtime escalation. Escalation should still go through
        in-band ``permission/request`` negotiation.

        Any field left as ``None`` is left unchanged.
        """
        updates: dict = {}
        if session_workspace_root is not None:
            updates["session_workspace_root"] = session_workspace_root
        if allowed_directories is not None:
            if replace_allowed_directories:
                updates["allowed_directories"] = tuple(dict.fromkeys(allowed_directories))
            else:
                # Legacy host context remains additive for clients that have
                # not opted into the session permission protocol.
                merged = list(self.allowed_directories) + [
                    d for d in allowed_directories if d not in self.allowed_directories
                ]
                updates["allowed_directories"] = tuple(merged)
        if filesystem_scope is not None:
            updates["filesystem_scope"] = filesystem_scope
        if not updates:
            return self
        return self.model_copy(update=updates)


class PermissionDecision(BaseModel):
    """Result of a permission check."""

    allowed: bool
    reason: str | None = None
    permission_request: dict | None = None  # None means "denied without escalation option"


class PermissionEngine:
    """Capability-based permission enforcement.

    Immutable after construction. Takes a frozen CapabilityPolicy
    and a workspace_dir. Never mutated in place.
    """

    def __init__(
        self,
        policy: CapabilityPolicy,
        workspace_dir: Path,
        grant_store: GrantStore | None = None,
    ):
        self._policy = policy
        self._workspace_dir = workspace_dir.resolve()
        self._grant_store = grant_store
        if policy.session_workspace_root:
            self._session_workspace_root = Path(policy.session_workspace_root).expanduser().resolve()
        else:
            log.warning(
                "permission/no_session_workspace_root: "
                "officev3.paths.session_workspace_root is not set; "
                "falling back to workspace_dir=%s for session_workspace scope. "
                "officev3 should always write this field.",
                workspace_dir,
            )
            self._session_workspace_root = self._workspace_dir
        self._home_dir = Path.home().resolve()
        # Pre-resolve the allow-list once. Skip entries that fail to resolve
        # (e.g. the user typed garbage into config) so a single bad entry
        # doesn't disable the whole engine.
        resolved_dirs: list[Path] = []
        for d in policy.allowed_directories:
            try:
                resolved_dirs.append(Path(d).expanduser().resolve())
            except (OSError, RuntimeError) as exc:
                log.warning("permission/bad_allowed_dir", extra={"path": d, "error": str(exc)})
        self._allowed_dirs: tuple[Path, ...] = tuple(resolved_dirs)

        # Box-Agent owns ~/.box-agent (skills, runtime-packages, browsers,
        # trash, log, ...). It is engine-internal data, not user business
        # data — never prompt for it, regardless of scope or config.
        try:
            self._box_agent_dir: Path | None = (self._home_dir / ".box-agent").resolve()
        except (OSError, RuntimeError):
            self._box_agent_dir = None

        # Built-in skills are trusted, engine-owned code. Packaged runtimes
        # place them beside this module (for example under an application
        # bundle), which is intentionally outside the user's filesystem
        # scope. Allow Bash to read that exact tree so bundled skill scripts
        # can run, while keeping writes and unrelated application paths
        # blocked. Resolving both the root and requested path preserves the
        # existing symlink-escape protection.
        try:
            self._builtin_skills_dir: Path | None = (
                Path(__file__).resolve().parent.parent / "skills"
            ).resolve()
        except (OSError, RuntimeError):
            self._builtin_skills_dir = None

        app_candidates = [
            "/Applications",
            "/System/Applications",
            "/usr/bin",
            "/usr/local/bin",
            "/opt",
            "/snap/bin",
        ]
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            if value := os.environ.get(env_name):
                app_candidates.append(value)
        resolved_app_dirs: list[Path] = []
        for d in app_candidates:
            try:
                resolved = Path(d).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if resolved not in resolved_app_dirs:
                resolved_app_dirs.append(resolved)
        self._app_read_dirs: tuple[Path, ...] = tuple(resolved_app_dirs)

    @property
    def policy(self) -> CapabilityPolicy:
        return self._policy

    def check(
        self,
        capability: str,
        resource: dict,
        tool_name: str | None = None,
    ) -> PermissionDecision:
        if capability == FILESYSTEM_READ:
            return self._check_filesystem(
                Path(resource["path"]),
                self._policy.filesystem_scope,
                "read",
                tool_name,
            )
        elif capability == FILESYSTEM_WRITE:
            return self._check_filesystem(
                Path(resource["path"]),
                self._policy.filesystem_scope,
                "write",
                tool_name,
            )
        elif capability == MEMORY_OPENCLAW_IMPORT:
            return self._check_memory_openclaw()
        return PermissionDecision(
            allowed=False, reason=f"Unknown capability: {capability}"
        )

    # ── filesystem ──

    def _check_filesystem(
        self, path: Path, scope: str, operation: str, tool_name: str | None = None
    ) -> PermissionDecision:
        resolved = self._resolve_for_check(path)

        # Directory-level grants take precedence over scope checks. These are
        # recorded by the negotiator after the user approves a permission
        # request; subsequent reads under the granted directory must succeed
        # without another round-trip.
        if self._grant_store and self._grant_store.has_filesystem_dir_grant(resolved):
            return PermissionDecision(allowed=True)

        # Legacy elevation: an ACP caller (or a test) may still record a
        # broad ("filesystem", "user_home") grant via add_grant(). Honor it
        # by elevating the working scope for this check only.
        if self._grant_store and scope == "session_workspace":
            if self._grant_store.has_grant("filesystem", "user_home"):
                scope = "user_home"

        if self._path_allowed_by_scope(resolved, scope, operation, tool_name):
            return PermissionDecision(allowed=True)

        escalation = self._compute_escalation(resolved, scope, requested_path=path)

        if escalation is None:
            log.warning(
                "permission/denied path=%s resolved=%s scope=%s op=%s reason=no-escalation",
                path, resolved, scope, operation,
            )
            return PermissionDecision(
                allowed=False,
                reason=(
                    f"Access denied: {operation} to {path} (resolved: {resolved}) "
                    f"is outside all allowed scopes (active scope: {scope})."
                ),
            )

        log.warning(
            "permission/denied path=%s resolved=%s scope=%s op=%s escalation=%s",
            path, resolved, scope, operation, escalation,
        )
        return PermissionDecision(
            allowed=False,
            reason=f"Access denied: {operation} to {path} is outside {scope}.",
            permission_request={
                "type": "permission_request",
                "scope": "filesystem",
                "requested_scope": escalation,
                "path": str(path),
                "reason": f"Path is outside {scope}",
                "temporary_supported": True,
                "persistent_supported": True,
                "persistent_label": "始终允许此目录",
            },
        )

    def _compute_escalation(
        self,
        resolved: Path,
        current_scope: str,
        *,
        requested_path: Path,
    ) -> str | None:
        """Return the host protocol escalation for an out-of-scope path.

        Existing home-directory escalation semantics remain unchanged.
        officev3 also persists explicit Windows drive/UNC approvals as
        path-specific custom directories. The host protocol currently uses
        ``requested_scope=user_home`` as the filesystem approval discriminator
        even when the approved grant is a narrower directory outside home.
        """
        if current_scope not in ("session_workspace", "custom", "user_home"):
            return None
        if current_scope in ("session_workspace", "custom"):
            if self._is_under_home(resolved):
                return "user_home"
        raw = str(requested_path)
        explicit_windows_path = bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", raw))
        if explicit_windows_path:
            return "user_home"
        return None

    def _is_inside(self, target: Path, root: Path) -> bool:
        """True if ``target`` equals or is contained within ``root``.

        Both paths must already be resolved. Uses ``Path.relative_to`` for
        component-wise containment so ``/Users/x/Download`` does not match
        ``/Users/x/Downloads`` (the bug spec section 6 calls out).
        """
        try:
            target.relative_to(root)
            return True
        except ValueError:
            return False

    def _is_under_home(self, resolved: Path) -> bool:
        return self._is_inside(resolved, self._home_dir)

    def _resolve_for_check(self, path: Path) -> Path:
        """Resolve a path for permission checking.

        For existing paths: full resolve (follows symlinks).
        For non-existing paths: resolve the existing parent, then append
        remaining components.
        """
        if path.exists():
            return path.resolve()
        parts_below: list[str] = []
        cursor = path
        while not cursor.exists():
            parts_below.append(cursor.name)
            parent = cursor.parent
            if parent == cursor:
                break
            cursor = parent
        resolved_parent = cursor.resolve()
        for part in reversed(parts_below):
            resolved_parent = resolved_parent / part
        return resolved_parent

    def _path_allowed_by_scope(
        self, resolved: Path, scope: str, operation: str, tool_name: str | None = None
    ) -> bool:
        # workspace_dir is always allowed regardless of scope
        if self._is_inside(resolved, self._workspace_dir):
            return True

        # ~/.box-agent is engine-owned data — always allowed.
        if self._box_agent_dir is not None and self._is_inside(resolved, self._box_agent_dir):
            return True

        # Packaged built-in skills are executable product resources rather
        # than user data. Bash may read them, but never write them.
        if (
            operation == "read"
            and self._builtin_skills_dir is not None
            and self._is_inside(resolved, self._builtin_skills_dir)
        ):
            return True

        # Box-Agent scratch files are allowed under OS temp roots without
        # granting broad access to arbitrary pytest/user temp directories.
        if resolved.name.startswith("box_agent_"):
            for temp_dir in (Path("/tmp"), Path("/var/tmp")):
                try:
                    if self._is_inside(resolved, temp_dir.resolve()):
                        return True
                except (OSError, RuntimeError):
                    continue

        # Read-only access to common application / executable install roots is
        # allowed so tools can probe or run dependencies such as LibreOffice
        # without requiring broad user-home access. Bash command paths stay
        # gated so absolute binaries like /bin/echo do not bypass negotiation.
        # Writes remain blocked.
        if operation == "read" and tool_name != "bash":
            for app_dir in self._app_read_dirs:
                if self._is_inside(resolved, app_dir):
                    return True

        if scope == "user_home":
            if self._is_under_home(resolved):
                return True
            return any(self._is_inside(resolved, allowed) for allowed in self._allowed_dirs)

        # session_workspace and custom share the same semantics:
        # session_workspace_root + workspace_dir + allowed_directories.
        if scope in ("session_workspace", "custom"):
            if self._is_inside(resolved, self._session_workspace_root):
                return True
            return any(self._is_inside(resolved, allowed) for allowed in self._allowed_dirs)

        # Unknown scope — fail closed.
        log.warning("permission/unknown_scope", extra={"scope": scope})
        return False

    # ── memory ──

    def _check_memory_openclaw(self) -> PermissionDecision:
        if self._policy.openclaw_import_enabled:
            return PermissionDecision(allowed=True)
        # Check grant store override
        if self._grant_store and self._grant_store.has_grant("memory", "openclaw_import"):
            return PermissionDecision(allowed=True)
        return PermissionDecision(
            allowed=False,
            reason="OpenClaw memory import is disabled by officev3 policy.",
            permission_request={
                "type": "permission_request",
                "scope": "memory",
                "requested_scope": "openclaw_import",
                "reason": "OpenClaw memory import is disabled",
                "temporary_supported": True,
                "persistent_supported": True,
            },
        )

class GrantStore:
    """Tracks in-band permission grants at prompt and session scope.

    Two grant tables coexist:

    * Capability grants — keyed by ``(scope, requested_scope)`` tuples, e.g.
      ``("memory", "openclaw_import")``. Used for non-filesystem capabilities
      (and as a legacy back-compat path for ``("filesystem", "user_home")``).
    * Filesystem directory grants — resolved ``Path`` objects. Recorded by the
      ACP negotiator after the user approves a filesystem request, so
      subsequent reads under the same directory bypass the prompt.

    Prompt grants are cleared between prompts; session grants persist for the
    ACP session lifetime.
    """

    def __init__(self) -> None:
        self._session_grants: set[tuple[str, str]] = set()
        self._prompt_grants: set[tuple[str, str]] = set()
        self._session_dirs: set[Path] = set()
        self._prompt_dirs: set[Path] = set()

    # ── capability-style grants ──

    def has_grant(self, scope: str, requested_scope: str) -> bool:
        key = (scope, requested_scope)
        return key in self._session_grants or key in self._prompt_grants

    def add_grant(self, scope: str, requested_scope: str, grant_scope: str) -> None:
        """Record a grant.  *grant_scope* is ``"prompt"`` or ``"session"``."""
        key = (scope, requested_scope)
        if grant_scope == "session":
            self._session_grants.add(key)
        else:
            self._prompt_grants.add(key)

    # ── filesystem directory grants ──

    def add_filesystem_dir_grant(self, directory: Path, grant_scope: str) -> None:
        """Allow reads/writes under ``directory`` for the lifetime of *grant_scope*.

        ``directory`` is resolved before storage so equality / containment
        checks are stable against symlinks and ``..`` segments.
        """
        resolved = directory.expanduser().resolve()
        if grant_scope == "session":
            self._session_dirs.add(resolved)
        else:
            self._prompt_dirs.add(resolved)

    def has_filesystem_dir_grant(self, target: Path) -> bool:
        """True if any granted dir equals or contains *target*.

        ``target`` should already be resolved by the caller (the engine does
        this via ``_resolve_for_check``).
        """
        for d in self._session_dirs:
            if target == d:
                return True
            try:
                target.relative_to(d)
                return True
            except ValueError:
                pass
        for d in self._prompt_dirs:
            if target == d:
                return True
            try:
                target.relative_to(d)
                return True
            except ValueError:
                pass
        return False

    def clear_prompt_grants(self) -> None:
        """Called at the start of each prompt to reset prompt-level grants."""
        self._prompt_grants.clear()
        self._prompt_dirs.clear()


# ── Bash helper ──


# Absolute paths: a leading '/' followed by chars that are plausibly part of a
# real filesystem path. We exclude shell/regex metacharacters (`<>$(){};|&`) so
# that fragments embedded in `sed`/HTML/redirects do not get mis-extracted as
# paths (e.g. `/<strong>$1<\/strong>/g`). Backslash is also excluded; real
# POSIX paths never need a `\X` escape inside the path literal.
#
# Prefix `["\']` may anchor a path (e.g. `cp "/tmp/a"`), but only when it is
# an *opening* quote — a closing quote after `"$VAR"` / `"${VAR}"` followed by
# `/slide-*.png` would otherwise produce a phantom `/slide-*.png` absolute
# path. The negative lookbehind `(?<![\w}])` enforces that the quote is not
# preceded by a word char or `}` (variable close).
_ABS_PATH_RE = re.compile(r'(?:^|\s|(?<![\w}])["\'])(\/[^\s"\'\\<>$(){};|&]+)')
_TILDE_PATH_RE = re.compile(r'(?:^|\s|["\';=])(~(?:/[^\s"\'\\;|&]*)?)')
_HOME_VAR_RE = re.compile(r'(\$HOME(?:/[^\s"\'\\;|&]*)?)')
_SHELL_PATH_ASSIGNMENT_RE = re.compile(
    r"(?m)(?:^|[;\n]\s*)(?:export\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)="
    r"(?:\"([^\"]*)\"|'([^']*)'|([^\s;]+))"
)

# Real shell redirect operators (`>`, `>>`, `2>`, `2>>`, ...). When followed by
# a path, that path is what we want to extract — but we must distinguish a real
# redirect from a `>` that happens to be the closing bracket of an HTML/XML
# tag (e.g. `</strong>/g`). A redirect is preceded by whitespace, start-of-
# string, or an fd digit. We rewrite those to whitespace before extraction so
# the existing whitespace-prefix path regex picks the target up cleanly.
_REDIRECT_RE = re.compile(r'(^|[\s\d])>{1,2}')

# Balanced quoted segments: `"..."` or `'...'`. Used by the quote-aware
# pre-pass so paths containing spaces (e.g. `"/Users/me/Box 工作区/x"`,
# `"C:\Users\foo bar\x"`) get extracted as a single token instead of being
# truncated at the first space by `_ABS_PATH_RE`'s whitespace-bounded char
# class. Non-greedy and matches `"` / `'` symmetrically.
_QUOTED_SEGMENT_RE = re.compile(r'(["\'])([^"\']*?)\1')
_QUOTED_ABSOLUTE_SUFFIX_RE = re.compile(
    r'(["\'])(/[^"\']*)\1(/[^\s"\'<>;|&]*)'
)

# Windows-style absolute path: drive letter + `:` + `\` or `/`.
_WIN_DRIVE_RE = re.compile(r'^[A-Za-z]:[\\/]')

# Unquoted Windows drive paths: `C:\foo\bar` or `D:/foo/bar`. Must be a single
# letter (so `https:` / `file:` URL schemes are not mistaken for drives) at a
# non-word boundary. The wider boundary also supports cmd's escaped quotes
# (`\"D:\path\"`). Stops at whitespace, shell metachars, or quotes; backslash
# IS kept (it's a path separator on Windows, not a shell escape inside an
# unquoted path on cmd/PowerShell). Regular quoted Windows drive paths are
# already handled by the `_QUOTED_SEGMENT_RE` pre-pass.
_WIN_DRIVE_PATH_RE = re.compile(
    r'(?<![A-Za-z0-9])([A-Za-z]:[\\/][^\s"\'<>;|&]*)'
)

# ``cmd.exe`` and its built-ins use slash-prefixed switches (``/d``, ``/c``,
# ``exit /b``). They look like short POSIX absolute paths to
# ``_ABS_PATH_RE`` even though they are command syntax, not filesystem
# resources. Only suppress these ambiguous one-letter tokens when the command
# explicitly invokes the Windows command processor; ``/d`` remains a valid
# path for ordinary POSIX shell commands.
_WINDOWS_CMD_INVOKE_RE = re.compile(
    r'(?:^|[\s;&|])cmd(?:\.exe)?(?=\s|$)',
    re.IGNORECASE,
)
_WINDOWS_CMD_SWITCH_RE = re.compile(
    r'(?<!\S)/(?:[a-z?](?::(?:on|off))?)(?=\s|["\');&|]|$)',
    re.IGNORECASE,
)

# ``exit /b`` is valid cmd syntax even when the command string is passed
# directly instead of being wrapped in ``cmd.exe /c``. In that form the
# general cmd-wrapper check above cannot identify the dialect, but ``/b`` is
# still unambiguously an option to the ``exit`` builtin rather than a path.
_WINDOWS_EXIT_SWITCH_RE = re.compile(
    r'(?P<prefix>(?:^|[\s;&|])exit\s+)/b(?=\s|["\');&|]|$)',
    re.IGNORECASE,
)


def expand_inline_shell_path_variables(command: str) -> str:
    """Expand absolute path variables declared inside one shell command.

    This is intentionally narrower than shell evaluation: only variables
    assigned in the command itself and resolving to absolute filesystem paths
    are substituted. It lets permission analysis understand common Skill
    commands such as ``OUT=/abs/path; mkdir -p "$OUT/assets"`` without reading
    ambient environment values or executing shell code.
    """

    path_vars: dict[str, str] = {}

    def substitute_known(value: str) -> str:
        for name, resolved in path_vars.items():
            value = re.sub(rf"\$\{{{re.escape(name)}\}}|\${re.escape(name)}\b", resolved, value)
        return value

    for match in _SHELL_PATH_ASSIGNMENT_RE.finditer(command):
        name = match.group(1)
        raw_value = next(
            (value for value in match.groups()[1:] if value is not None),
            "",
        )
        expanded = substitute_known(raw_value)
        expanded = os.path.expanduser(expanded)
        if expanded.startswith("$HOME/"):
            expanded = str(Path.home()) + expanded[5:]
        if os.path.isabs(expanded):
            path_vars[name] = expanded

    return substitute_known(command)

# sed/perl-style substitution: `s<delim>pattern<delim>replacement<delim>[flags]`.
# Stripped before path extraction so the regex bodies of `sed 's/.../.../g'`,
# `perl -pe 's|...|...|'` etc. don't get scanned for absolute paths.
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

# Bare system roots that almost never represent a real user write/read target.
# When the regex catches one of these (typically because of shell punctuation
# like `cd /; ls` collapsing to `/` after rstrip), we drop it rather than
# triggering a permission denial that confuses both the LLM and the user.
_SYSTEM_ROOT_NOISE: frozenset[str] = frozenset({
    "/", "//",
    "/.",
    "/bin", "/sbin", "/usr", "/etc", "/var", "/opt", "/lib", "/lib64",
    "/proc", "/sys", "/run", "/boot", "/dev",
    "/private", "/Library", "/System", "/Applications",
    "/cores", "/Volumes", "/Network", "/tmp",
})


def _is_source_comment_path(command: str, path: str, position: int) -> bool:
    """Reject CSS/HTML comment delimiters misread as filesystem paths.

    The permission scanner intentionally still sees real paths elsewhere in
    the same Python/JavaScript payload.  Only the delimiter-shaped match is
    suppressed, so a path such as ``/etc/hosts`` remains enforceable even if
    source text around it contains comments.
    """
    if path.startswith("/*"):
        for match in re.finditer(r"/\*.*?\*/", command, flags=re.DOTALL):
            if match.start() <= position < match.end():
                return True
    for match in re.finditer(r"<!--.*?-->", command, flags=re.DOTALL):
        if match.start() <= position < match.end():
            return True
    return False


def _classify_quoted_path(body: str, home: str) -> str | None:
    """Return the absolute path a quoted token resolves to, or None.

    Supports POSIX absolute (`/...`), Windows drive letter (`C:\\...`,
    `C:/...`), bare `~`, `~/...`, bare `$HOME`, and `$HOME/...`. The body
    may contain spaces — the caller is responsible for having already
    pulled it from a balanced quoted segment.
    """
    if not body:
        return None
    if body.startswith("/"):
        return body
    if _WIN_DRIVE_RE.match(body):
        return body
    if body == "~":
        return home
    if body.startswith("~/") or body.startswith("~\\"):
        return home + body[1:]
    if body == "$HOME":
        return home
    if body.startswith("$HOME/") or body.startswith("$HOME\\"):
        return home + body[5:]
    return None


def extract_absolute_paths(command: str) -> list[str]:
    """Extract absolute paths from a shell command (best-effort).

    Handles literal absolute paths (/...), tilde paths (~/...), and
    $HOME paths ($HOME/...). Tilde and $HOME are expanded to the real
    home directory. Deduplicates results.

    Quoted segments (`"..."`, `'...'`) are handled by a pre-pass so paths
    containing spaces — e.g. `cat "/Users/me/Box 工作区/file.txt"` or
    `Get-Content "C:\\Users\\foo bar\\x"` — are kept whole rather than
    truncated at the first space. Windows drive letter paths are only
    detected inside quotes.
    """
    seen: set[str] = set()
    paths: list[str] = []
    home = str(Path.home())

    # Strip sed/perl substitution expressions first so the regex bodies don't
    # get mis-parsed as absolute paths (e.g. the `/g` flag at the end of
    # `sed 's|a|b|g'` or the literal `/` delimiters in `s/foo/bar/`).
    command = expand_inline_shell_path_variables(command)
    # Normalize shell tokens such as `"/abs/root"/slide-*.png` into one
    # quoted path before the quote-aware extractor runs. Otherwise consuming
    # the quoted prefix leaves a phantom `/slide-*.png` absolute path behind.
    command = _QUOTED_ABSOLUTE_SUFFIX_RE.sub(
        lambda match: (
            f'{match.group(1)}{match.group(2).rstrip("/")}'
            f'{match.group(3)}{match.group(1)}'
        ),
        command,
    )
    sanitized = _SED_SUBST_RE.sub(" ", command)

    sanitized = _WINDOWS_EXIT_SWITCH_RE.sub(r'\g<prefix> ', sanitized)

    if _WINDOWS_CMD_INVOKE_RE.search(command):
        sanitized = _WINDOWS_CMD_SWITCH_RE.sub(" ", sanitized)

    # Quote-aware pre-pass: extract absolute paths from `"..."` / `'...'`
    # bodies, then replace the entire quoted span (including the quote
    # chars) with whitespace so the downstream regex doesn't double-match
    # and so the lookbehind `(?<![\w}])["\']` heuristics aren't confused.
    def _consume_quoted(m: re.Match) -> str:
        body = m.group(2)
        expanded = _classify_quoted_path(body, home)
        if expanded is not None and expanded not in _SYSTEM_ROOT_NOISE \
                and not _is_source_comment_path(sanitized, expanded, m.start(2)) \
                and expanded not in ("/dev/null", "/dev/stdin",
                                     "/dev/stdout", "/dev/stderr"):
            if expanded not in seen:
                seen.add(expanded)
                paths.append(expanded)
            return " " * (m.end() - m.start())
        return m.group(0)

    sanitized = _QUOTED_SEGMENT_RE.sub(_consume_quoted, sanitized)

    # Normalize real shell redirects (`>`, `>>`, `2>`) to whitespace so the
    # path immediately following them is picked up via the whitespace-prefix
    # branch of `_ABS_PATH_RE`. We do this only for redirects preceded by
    # whitespace/SOL/fd-digit so that `>` inside an HTML/XML tag like
    # `</strong>/g` is left alone (and therefore not treated as a path
    # boundary).
    sanitized = _REDIRECT_RE.sub(lambda m: m.group(1) + " ", sanitized)

    for m in _ABS_PATH_RE.finditer(sanitized):
        p = m.group(1).rstrip(";")
        if p in ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"):
            continue
        if _is_source_comment_path(sanitized, p, m.start(1)):
            continue
        # Drop bare system roots that almost certainly came from shell
        # punctuation collapsing the regex match (e.g. `cd /; ls` →  "/").
        # These are not real targets the AI is trying to access.
        if p in _SYSTEM_ROOT_NOISE:
            continue
        if p not in seen:
            seen.add(p)
            paths.append(p)

    for m in _TILDE_PATH_RE.finditer(sanitized):
        raw = m.group(1)  # e.g. "~" or "~/Downloads"
        suffix = raw[1:]  # strip leading ~
        expanded = home + suffix
        if expanded not in seen:
            seen.add(expanded)
            paths.append(expanded)

    for m in _HOME_VAR_RE.finditer(sanitized):
        raw = m.group(1)  # e.g. "$HOME" or "$HOME/file.txt"
        suffix = raw[5:]  # strip leading $HOME
        expanded = home + suffix
        if expanded not in seen:
            seen.add(expanded)
            paths.append(expanded)

    # Unquoted Windows drive-letter paths (`C:\…`, `D:/…`). Quoted variants
    # are already consumed by the quote-aware pre-pass. Backslash-trailing
    # punctuation that some shells append (e.g. `;`) is stripped.
    for m in _WIN_DRIVE_PATH_RE.finditer(sanitized):
        p = m.group(1).rstrip(";")
        if p not in seen:
            seen.add(p)
            paths.append(p)

    return paths
