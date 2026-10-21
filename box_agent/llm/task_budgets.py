"""Per-call output budgets for auxiliary work; never mutate execution clients."""

from __future__ import annotations

from typing import Any

from .model_routing import resolve_model_client


SMALL_TASK_MAX_OUTPUT_TOKENS = 8_192
TITLE_MAX_OUTPUT_TOKENS = 8_000
LONG_TASK_MAX_OUTPUT_TOKENS = 16_384
CONTINUATION_THINKING_MAX_OUTPUT_TOKENS = 12_288

_LIGHTWEIGHT_PURPOSES = {
    "openclaw-memory", "questionnaire-memory", "tag-extract",
    "voice_transcript_cleanup", "voice_summary", "local_conversation_context_summary",
    "local_share_summary", "expert-draft", "schedule-draft",
    "summary", "context_summary", "rewrite",
}


def replacement_output_budget(
    text: str, *, default: int = SMALL_TASK_MAX_OUTPUT_TOKENS,
) -> int | None:
    """Leave space for a full replacement plus JSON; retain large caller budgets.

    Two tokens per character is a conservative estimate for mixed-language text.
    This is a tier selector, not a model tokenizer or an input truncation rule.
    """
    estimated = len(text) * 2 + 1_024
    if estimated <= default:
        return default
    if estimated <= LONG_TASK_MAX_OUTPUT_TOKENS:
        return LONG_TASK_MAX_OUTPUT_TOKENS
    return None


def lightweight_output_budget(purpose: str, prompt: str) -> int | None:
    """Budget known utility purposes, leaving unspecified workflows unchanged."""
    normalized = purpose.strip().lower()
    if "title" in normalized:
        return TITLE_MAX_OUTPUT_TOKENS
    if normalized not in _LIGHTWEIGHT_PURPOSES:
        return None
    if normalized in {"voice_transcript_cleanup", "rewrite"}:
        return replacement_output_budget(prompt)
    return SMALL_TASK_MAX_OUTPUT_TOKENS


def cap_auxiliary_client(llm: Any, budget: int) -> Any:
    """Clone the already selected model with a smaller cap, without rerouting."""
    client, _ = resolve_model_client(
        llm, task="", auto_model_candidates=(), max_output_tokens_cap=budget,
    )
    return client


def require_complete_response(response: Any) -> None:
    """Reject provider output-limit finishes before consuming auxiliary results."""
    if getattr(response, "finish_reason", None) in ("length", "max_tokens"):
        raise ValueError("Auxiliary response reached its output token limit")
