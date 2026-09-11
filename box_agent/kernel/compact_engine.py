"""Replaceable compaction capability over the existing compression algorithm."""
from __future__ import annotations

from .context_engine import _maybe_summarize
from .context_types import CompactionInput, CompactionOutcome


class DefaultCompactEngine:
    """Preserve current summary, fallback, budgets and retained tool groups."""

    async def compact_if_needed(self, inputs: CompactionInput) -> CompactionOutcome:
        return await _maybe_summarize(
            inputs.llm, list(inputs.history), inputs.token_limit,
            inputs.api_total_tokens, inputs.skip_check, inputs.session_id,
            turn_id=inputs.turn_id, title=inputs.title,
            api_prompt_tokens=inputs.api_prompt_tokens, tools=inputs.tools,
            summary_llm=inputs.summary_llm,
            allow_llm_summary=inputs.allow_llm_summary,
            before_summary=inputs.before_summary,
            force=inputs.force, estimate_tools=inputs.estimate_tools,
            summary_input_token_limit=inputs.summary_input_token_limit,
        )
