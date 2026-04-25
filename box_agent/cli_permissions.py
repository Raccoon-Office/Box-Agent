"""CLI permission negotiator — interactive terminal prompt."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .tools.permissions import GrantStore


class CLIPermissionNegotiator:
    """In-band permission negotiation via interactive terminal prompt.

    When a tool is denied with a ``permission_request``, this negotiator
    asks the user in the terminal whether to grant access.

    Filesystem grants are recorded at directory granularity (parent of the
    requested path, or the path itself when it is already a directory) —
    mirroring the ACP negotiator and matching the spec: "approve / allow
    once" only opens the *containing directory*, not the entire user_home.
    Memory grants continue to use the capability-level grant table.
    """

    def __init__(self, grant_store: GrantStore) -> None:
        self._store = grant_store

    async def negotiate(self, permission_request: dict) -> bool:
        scope = permission_request.get("scope", "")
        requested_scope = permission_request.get("requested_scope", "")
        path = permission_request.get("path", "")

        # Dedup: filesystem requests look up directory grants; other
        # capabilities use the legacy (scope, requested_scope) table.
        if scope == "filesystem" and path:
            try:
                target = Path(path).expanduser().resolve()
            except (OSError, RuntimeError):
                target = None
            if target is not None and self._store.has_filesystem_dir_grant(target):
                return True
        elif self._store.has_grant(scope, requested_scope):
            return True

        reason = permission_request.get("reason", "")

        print()
        print("\033[1m\033[33m🔒 权限申请\033[0m")
        if path:
            print(f"   路径: \033[36m{path}\033[0m")
        print(f"   原因: {reason}")
        print()
        print("   \033[1m[1]\033[0m 仅本次允许")
        print("   \033[1m[2]\033[0m 始终允许（本次会话）")
        print("   \033[1m[3]\033[0m 拒绝")

        choice = await _prompt_choice()

        if choice in ("1", "2"):
            grant_lifetime = "prompt" if choice == "1" else "session"
            label = "仅本次" if choice == "1" else "本次会话"
            if scope == "filesystem" and path:
                grant_dir = _derive_grant_dir(path)
                if grant_dir is None:
                    print("\033[31m   ✗ 无法解析路径，已拒绝\033[0m\n")
                    return False
                self._store.add_filesystem_dir_grant(grant_dir, grant_lifetime)
            else:
                self._store.add_grant(scope, requested_scope, grant_lifetime)
            print(f"\033[32m   ✓ 已允许（{label}）\033[0m\n")
            return True
        print("\033[31m   ✗ 已拒绝\033[0m\n")
        return False


def _derive_grant_dir(path: str) -> Path | None:
    """Return the directory that should be opened by a filesystem grant.

    For an existing directory, that directory itself; otherwise the
    parent of the resolved target. ``None`` if the path cannot be
    resolved (rare — bad characters or filesystem error).
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.is_dir():
        return resolved
    return resolved.parent


def _read_with_echo() -> str:
    """Read a line from stdin with echo forcibly enabled.

    prompt_toolkit may leave the terminal in raw mode (no echo) after
    its prompt session returns.  We restore canonical mode + echo via
    termios before calling ``input()``, then put the old state back.
    """
    prompt_text = "\n   请选择 [1/2/3]: "

    try:
        import termios

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        try:
            new_attrs = termios.tcgetattr(fd)
            # Enable ECHO and ICANON (canonical / cooked mode)
            new_attrs[3] |= termios.ECHO | termios.ICANON
            termios.tcsetattr(fd, termios.TCSADRAIN, new_attrs)
            return input(prompt_text).strip()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
    except (ImportError, termios.error, OSError):
        # Non-POSIX or no tty — fall back to plain input
        return input(prompt_text).strip()


async def _prompt_choice() -> str:
    """Read the user's permission choice asynchronously.

    Runs ``_read_with_echo`` in a thread-pool executor so the async
    event loop is not blocked while waiting for user input.
    """
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _read_with_echo)
    except (EOFError, KeyboardInterrupt):
        return ""
