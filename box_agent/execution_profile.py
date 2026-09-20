"""Execution-policy profile shared by host adapters and orchestration."""

from __future__ import annotations

from collections.abc import Collection
from typing import Final, Literal, cast

ExecutionProfile = Literal["fast", "standard", "deep"]

DEFAULT_EXECUTION_PROFILE: Final[ExecutionProfile] = "standard"
FAST_OPTIONAL_SKILLS: Final[frozenset[str]] = frozenset({"research-synthesis"})


def normalize_execution_profile(value: object) -> ExecutionProfile:
    """Normalize host metadata without changing legacy session behavior."""
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"fast", "standard", "deep"}:
            return cast(ExecutionProfile, normalized)
    return DEFAULT_EXECUTION_PROFILE


def is_skill_blocked(
    name: str,
    blocked_skill_names: Collection[str],
    explicitly_allowed_skill_names: Collection[str] | None = None,
) -> bool:
    """Apply the same execution-policy gate to discovery and reading."""
    return name in blocked_skill_names and name not in (explicitly_allowed_skill_names or ())
