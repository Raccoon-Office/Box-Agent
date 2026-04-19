"""Lightweight session-mode classifier for ACP.

When an ACP caller does not supply ``_meta.session_mode`` at session creation,
Box-Agent can auto-classify the user's first message into one of the known
session modes via a single tool-free LLM call. The caller stays in control — if
a mode is supplied explicitly, this module is never invoked.
"""

from __future__ import annotations

import asyncio
import re
from typing import Final

from box_agent.llm import LLMClient
from box_agent.schema import Message

from .debug_logger import acp_logger as log

_VALID_MODES: Final[frozenset[str]] = frozenset(
    {
        "data_analysis",
        "ppt_plan_chat",
        "ppt_outline",
        "ppt_editor_standard_html",
    }
)

_GENERAL_LABEL: Final[str] = "general"

_CLASSIFIER_SYSTEM_PROMPT: Final[str] = """\
You are an intent classifier for a multi-mode AI assistant. Classify the user's \
message into exactly one of the following labels and respond with the label \
ONLY (no punctuation, no explanation, no surrounding text):

- data_analysis    : the user wants to analyze a dataset, spreadsheet, CSV, \
Excel, or run statistics/plots over data.
- ppt_plan_chat    : the user wants to discuss, brainstorm, or plan the \
high-level structure of a PPT/slide deck (not yet asking for an outline).
- ppt_outline      : the user wants a concrete slide-by-slide outline for a \
PPT/deck.
- ppt_editor_standard_html : the user wants to edit, modify, or generate the \
HTML content of an individual PPT slide.
- general          : none of the above; general chat, coding help, file \
operations, or anything ambiguous.

Respond with exactly one label from the list above."""


def _normalize(raw: str) -> str | None:
    """Extract a clean label from raw model output.

    Returns a valid mode name, or ``None`` for general/unclassifiable output.
    """
    if not raw:
        return None
    # Pull out word-like tokens (letters/digits/underscore) from the first line
    # and look for the first recognizable label. This tolerates wrappings like
    # backticks, quotes, punctuation, or trailing explanations.
    first_line = raw.strip().splitlines()[0].lower()
    tokens = re.findall(r"[a-z0-9_]+", first_line)
    for token in tokens:
        if token in _VALID_MODES:
            return token
        if token == _GENERAL_LABEL:
            return None
    return None


async def classify_session_mode(
    llm: LLMClient,
    user_text: str,
    *,
    timeout: float = 8.0,
) -> str | None:
    """Classify a user message into a known ``session_mode`` or ``None``.

    Args:
        llm: the shared LLM client (same model as the main agent loop).
        user_text: the user's first message text.
        timeout: seconds to wait for the classifier LLM call.

    Returns:
        One of ``_VALID_MODES`` on a confident classification, or ``None`` for
        general/fallback/failure. Never raises — all exceptions are swallowed
        and logged.
    """
    text = (user_text or "").strip()
    if not text:
        return None

    messages = [
        Message(role="system", content=_CLASSIFIER_SYSTEM_PROMPT),
        Message(role="user", content=text),
    ]

    try:
        response = await asyncio.wait_for(
            llm.generate(messages=messages, tools=None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warn(
            "session_mode/classify_timeout",
            message=f"Classifier timed out after {timeout}s; falling back to general",
        )
        return None
    except Exception as exc:  # noqa: BLE001 — classifier must never escalate
        log.warn(
            "session_mode/classify_error",
            message=f"Classifier failed: {exc}; falling back to general",
        )
        return None

    raw = getattr(response, "content", "") or ""
    mode = _normalize(raw)
    preview = text[:80].replace("\n", " ")
    log.info(
        "session_mode/auto_classified",
        mode=mode or _GENERAL_LABEL,
        raw=raw.strip()[:80],
        user_text_preview=preview,
    )
    return mode
