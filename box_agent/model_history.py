"""Shared markers for content omitted from model-facing history."""

from __future__ import annotations

from pathlib import Path
from typing import Any


MODEL_HISTORY_PLACEHOLDER_PREFIXES = (
    "[Full tool-call argument omitted from model history]",
    "[Full file content omitted from model history]",
    "[Full tool output omitted from model history]",
)


def is_model_history_placeholder(value: Any) -> bool:
    """Return true when a value is an internal model-history placeholder."""
    return isinstance(value, str) and any(
        value.startswith(prefix) for prefix in MODEL_HISTORY_PLACEHOLDER_PREFIXES
    )


def is_model_instruction_source_path(value: Any) -> bool:
    """Return true for skill instructions that must survive the next LLM turn.

    Generated artifacts are intentionally compacted, but a skill's ``SKILL.md``
    and files under its ``references`` or ``workflows`` directory are executable
    workflow contracts. Replacing them with an artifact placeholder before the
    model can act on them makes schema drift deterministic rather than saving
    useful context.
    """
    if not isinstance(value, (str, Path)):
        return False
    path = Path(value)
    parts = tuple(part.casefold() for part in path.parts)
    if not any(part in {"skills", "builtin-skills"} for part in parts):
        return False
    return (
        path.name.casefold() == "skill.md"
        or "references" in parts
        or "workflows" in parts
    )
