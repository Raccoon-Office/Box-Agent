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
    """

    filesystem_scope: str = "session_workspace"
    openclaw_import_enabled: bool = True
    session_workspace_root: str = ""

    @classmethod
    def from_config(cls, config: Config) -> CapabilityPolicy:
        o = config.officev3
        return cls(
            filesystem_scope=o.permissions.filesystem.scope,
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

    def __init__(self, policy: CapabilityPolicy, workspace_dir: Path):
        self._policy = policy
        self._workspace_dir = workspace_dir.resolve()
        if policy.session_workspace_root:
            self._session_workspace_root = Path(policy.session_workspace_root).resolve()
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
            },
        )

    def _compute_escalation(self, resolved: Path, current_scope: str) -> str | None:
        """Determine which scope escalation would grant access, or None.

        Only suggest escalation when the target path actually falls
        within a broader scope that could be granted.
        """
        if current_scope == "session_workspace":
            if self._is_under_home(resolved):
                return "user_home"
        return None

    def _is_under_home(self, resolved: Path) -> bool:
        h = str(self._home_dir)
        r = str(resolved)
        return r == h or r.startswith(h + "/")

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
        ws = str(self._workspace_dir)
        r = str(resolved)

        # workspace_dir is always allowed regardless of scope
        if r == ws or r.startswith(ws + "/"):
            return True

        if scope == "user_home":
            return self._is_under_home(resolved)

        # session_workspace: allow session_workspace_root + workspace_dir
        sws = str(self._session_workspace_root)
        return r == sws or r.startswith(sws + "/")

    # ── memory ──

    def _check_memory_openclaw(self) -> PermissionDecision:
        if self._policy.openclaw_import_enabled:
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
