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

    ``allowed_directories`` is an additive whitelist that extends
    ``session_workspace`` and ``custom`` scopes (the spec treats these two
    semantically identically).
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
            )
        elif capability == FILESYSTEM_WRITE:
            return self._check_filesystem(
                Path(resource["path"]),
                self._policy.filesystem_scope,
                "write",
            )
        elif capability == MEMORY_OPENCLAW_IMPORT:
            return self._check_memory_openclaw()
        return PermissionDecision(
            allowed=False, reason=f"Unknown capability: {capability}"
        )

    # ── filesystem ──

    def _check_filesystem(
        self, path: Path, scope: str, operation: str
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

        if self._path_allowed_by_scope(resolved, scope):
            return PermissionDecision(allowed=True)

        escalation = self._compute_escalation(resolved, scope)

        if escalation is None:
            log.debug(
                "permission/denied",
                extra={"path": str(path), "scope": scope, "escalation": "none"},
            )
            return PermissionDecision(
                allowed=False,
                reason=f"Access denied: {operation} to {path} is outside all allowed scopes.",
            )

        log.debug(
            "permission/denied_with_escalation",
            extra={"path": str(path), "scope": scope, "escalation": escalation},
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

    def _compute_escalation(self, resolved: Path, current_scope: str) -> str | None:
        """Determine which scope escalation would grant access, or None.

        Only suggest escalation when the target path actually falls
        within a broader scope that could be granted. ``user_home`` is
        currently the only escalation target: even when the base scope
        is ``custom`` (or future variants), the protocol-level
        ``requested_scope`` stays ``user_home``.
        """
        if current_scope in ("session_workspace", "custom"):
            if self._is_under_home(resolved):
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

    def _path_allowed_by_scope(self, resolved: Path, scope: str) -> bool:
        # workspace_dir is always allowed regardless of scope
        if self._is_inside(resolved, self._workspace_dir):
            return True

        if scope == "user_home":
            return self._is_under_home(resolved)

        # session_workspace and custom share the same semantics:
        # session_workspace_root + workspace_dir + allowed_directories.
        if scope in ("session_workspace", "custom"):
            if self._is_inside(resolved, self._session_workspace_root):
                return True
            for allowed in self._allowed_dirs:
                if self._is_inside(resolved, allowed):
                    return True
            return False

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


_ABS_PATH_RE = re.compile(r'(?:^|\s|["\'])(\/(?:[^\s"\'\\]|\\.)+)')
_TILDE_PATH_RE = re.compile(r'(?:^|\s|["\';=])(~(?:/[^\s"\'\\;|&]*)?)')
_HOME_VAR_RE = re.compile(r'(\$HOME(?:/[^\s"\'\\;|&]*)?)')


def extract_absolute_paths(command: str) -> list[str]:
    """Extract absolute paths from a shell command (best-effort).

    Handles literal absolute paths (/...), tilde paths (~/...), and
    $HOME paths ($HOME/...). Tilde and $HOME are expanded to the real
    home directory. Deduplicates results.
    """
    seen: set[str] = set()
    paths: list[str] = []

    for m in _ABS_PATH_RE.finditer(command):
        p = m.group(1).rstrip(";")
        if p in ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"):
            continue
        if p not in seen:
            seen.add(p)
            paths.append(p)

    home = str(Path.home())

    for m in _TILDE_PATH_RE.finditer(command):
        raw = m.group(1)  # e.g. "~" or "~/Downloads"
        suffix = raw[1:]  # strip leading ~
        expanded = home + suffix
        if expanded not in seen:
            seen.add(expanded)
            paths.append(expanded)

    for m in _HOME_VAR_RE.finditer(command):
        raw = m.group(1)  # e.g. "$HOME" or "$HOME/file.txt"
        suffix = raw[5:]  # strip leading $HOME
        expanded = home + suffix
        if expanded not in seen:
            seen.add(expanded)
            paths.append(expanded)

    return paths
