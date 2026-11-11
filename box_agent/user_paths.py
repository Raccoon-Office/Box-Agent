"""Box-Agent-owned profile paths. Set BOX_AGENT_HOME before importing the runtime.

This relocates engine state; it is not an OS sandbox or a tool permission grant.
No directories are created here and the operating-system HOME is never changed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

PROFILE_ENV = "BOX_AGENT_HOME"


def configured_box_agent_home(env: Mapping[str, str] | None = None) -> Path | None:
    source = os.environ if env is None else env
    if PROFILE_ENV not in source:
        return None
    raw = source[PROFILE_ENV].strip()
    candidate = Path(raw)
    if not raw or "\0" in raw or not candidate.is_absolute():
        raise ValueError("BOX_AGENT_HOME must be an absolute dedicated directory")
    candidate = candidate.resolve()
    if candidate == candidate.parent or Path.home().resolve().is_relative_to(candidate):
        raise ValueError("BOX_AGENT_HOME must not be the system root or a user-home ancestor")
    return candidate


def box_agent_home(
    home_dir: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    configured = configured_box_agent_home(env)
    return configured if configured is not None else (home_dir or Path.home()) / ".box-agent"


def state_path(
    relative: str,
    override: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> Path:
    """Resolve an owned state path; explicit profiles reject escaping overrides/symlinks."""
    configured = configured_box_agent_home(env)
    candidate = (
        Path(override).expanduser()
        if override is not None
        else box_agent_home(home_dir, env=env) / relative
    )
    if configured is not None:
        candidate = (configured / candidate).resolve()
        if not candidate.is_relative_to(configured):
            raise ValueError("State path is outside BOX_AGENT_HOME")
    return candidate


def browser_cache_path(
    *, env: Mapping[str, str] | None = None, home_dir: Path | None = None,
) -> str:
    """Resolve a relocatable tool cache without inspecting its target directory."""
    source = os.environ if env is None else env
    configured = configured_box_agent_home(source)
    override = source.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        if override == "0" or configured is None:
            return override
        candidate = Path(override).expanduser()
        # Anchor relative overrides before tools change cwd, without resolving
        # browser-cache symlinks/junctions or applying owned-data containment.
        return str(configured / candidate)
    return str((configured or box_agent_home(home_dir, env=source)) / "browsers")


def default_memory_dir() -> str:
    if configured_box_agent_home() is None:
        return "~/.box-agent/memory"
    return str(state_path("memory"))


def default_workspace_dir() -> str:
    if configured_box_agent_home() is None:
        return "./workspace"
    return str(state_path("workspace"))
