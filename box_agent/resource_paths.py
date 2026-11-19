"""Locate immutable release resources without changing Python package paths."""

from __future__ import annotations

import sys
from pathlib import Path

_RESOURCE_DIRECTORIES = frozenset({"skills", "config"})


def bundled_resource_dir(name: str, *, package_dir: Path | None = None) -> Path:
    """Use sibling release resources in a standalone runtime, package data otherwise.

    The executable locates release resources independently of cwd and
    BOX_AGENT_HOME. Missing release resources must not silently select a stale
    copy inside ``_internal``. Source and wheel installations retain package data.
    """
    if name not in _RESOURCE_DIRECTORIES:
        raise ValueError(f"Unknown bundled resource directory: {name}")
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if executable.parent.name != "bin":  # /box-agent-runtime/bin/box-agent-acp
            raise ValueError(f"Invalid runtime layout: executable must be inside bin/: {executable}")
        resource = executable.parent.parent / name  # box-agent-runtime/{skills,config}
        if resource.resolve() != resource:
            raise ValueError(f"Bundled resource directory must not be redirected: {name}")
        return resource
    package = Path(package_dir) if package_dir is not None else Path(__file__).resolve().parent
    return package / name
