"""Per-turn deliverable-language hint shared by service and ACP entry points.

Language detection supplies a fallback, not an explicit user choice. The hint
preserves the system contract's user choices and scoped host-provided Skill
defaults, while preventing reference material from silently choosing a language.

Only non-Chinese requests get a hint: the base prompt is Chinese, so Chinese
requests already default correctly, and very short messages ("ok", "continue")
carry no reliable language signal. Reply language is not addressed here; hosts
own it (ACP ``ui_language``).
"""

from __future__ import annotations

import re

# About six English words: short continuations ("continue after restart") carry no
# deliverable and should not gain a hint.
_MIN_LETTERS = 30
# Exclude ambiguous words such as a/on/as, which are also common in French.
_ENGLISH_CUES = frozenset({"the", "this", "please", "write", "create", "build", "save", "can", "you", "check", "whether", "should", "it",
                           "based", "and", "for", "from", "with", "into", "summarize"})
# Spans that say nothing about the user's language: code, URLs, paths, tags,
# and slash commands such as "/pptx".
_NOISE = re.compile(
    r"```.*?```|`[^`]*`|https?://\S+|(?<![\w.-])(?:[\w.-]+/)+[\w.-]+|<[^<>]+>|(?<!\S)/[\w-]+", re.DOTALL,
)

_HINTS = {
    "English": (
        "[Deliverable language fallback: English. The request is written in English; this alone "
        "is not an explicit deliverable-language choice. Priority: the user's explicit instruction "
        "this turn, then a language the user explicitly confirmed earlier for this task, then an "
        "applicable non-auto output-language default injected by the host for a currently loaded "
        "Skill from active Schema-validated configuration. That default applies only to that "
        "Skill's deliverables. If none applies, use English. Reference material, existing files, "
        "and an earlier model response do not establish a confirmed language choice. This hint "
        "does not change reply/progress language or grant tools or execution permission.]"
    ),
}


def request_language(text: str) -> str | None:
    """Return "English" for a sufficiently clear English request, else None."""
    content = _NOISE.sub(" ", text)
    latin = sum(1 for char in content if char.isascii() and char.isalpha())
    words = set(re.findall(r"\b[a-z]+\b", content.lower()))
    clear_english = latin >= _MIN_LETTERS and len(words & _ENGLISH_CUES) >= 3
    if (clear_english
            and not any(char.isalpha() and not char.isascii() for char in content)):
        return "English"
    return None


def deliverable_language_hint(text: str) -> str | None:
    """The hint to place beside this user message, or None."""
    language = request_language(text)
    return _HINTS.get(language) if language else None


def with_deliverable_language_hint(text: str) -> str:
    """Append the hint to a user message when its language warrants one."""
    hint = deliverable_language_hint(text)
    return f"{text.rstrip()}\n\n{hint}" if hint else text


__all__ = ["deliverable_language_hint", "request_language", "with_deliverable_language_hint"]
