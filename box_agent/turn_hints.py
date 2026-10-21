"""Per-turn notes placed after the user's own words, shared by CLI and ACP.

Each note is derived only from the user's text (never from host prefixes or
continuations) and is absent when it does not apply, so ordinary turns are
unchanged.
"""

from __future__ import annotations

from .turn_language import deliverable_language_hint
from .turn_secrets import credential_notice


def turn_hints(text: str) -> list[str]:
    return [hint for hint in (deliverable_language_hint(text), credential_notice(text)) if hint]


def with_turn_hints(text: str) -> str:
    hints = turn_hints(text)
    return "\n\n".join([text.rstrip(), *hints]) if hints else text


__all__ = ["turn_hints", "with_turn_hints"]
