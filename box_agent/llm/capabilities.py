"""Capability queries for configured LLM adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# Known vision model names matched as substrings (gateway prefixes and snapshots
# are common). Catalog tags and explicit declarations always win.
# Sources and update policy: docs/IMAGE_CONTEXT.md#image-input-capability
_IMAGE_INPUT_MODELS = frozenset(
    {
        # OpenAI
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        # Anthropic
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        # Google
        "gemini-4-argon",
        "gemini-3.8-flash",
        "gemini-3.1-pro-preview",
        # Alibaba
        "qwen3.8-max",
        "qwen3.8-flash",
        "qwen3.8-omni-flash",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.5-plus",
        "qwen3-vl-plus",
        # Moonshot
        "kimi-k3",
        "kimi-k2.7-code",
        "kimi-k2.6",
        "kimi-k2.5",
        # Z.ai
        "glm-5.3-flash",
        "glm-4.6v",
        # ByteDance
        "doubao-seed-evolving",
        "doubao-seed-2-1-pro",
        "doubao-seed-2-1-lite",
        "doubao-seed-2-1-turbo",
        "doubao-seed-2-0-pro",
        "doubao-seed-1-8",
        "doubao-seed-1-6-vision",
        # DeepSeek: V4 Pro remains text-only; only these models declare vision.
        "deepseek-flash",
        "deepseek-v4.1-flash",
        "deepseek-v4-1-flash",
        "deepseek-v4-flash-vision-exp",
        # MiniMax: M2.x is text-only; M3 supports image input.
        "minimax-m3",
        # xAI
        "grok-4.7",
        "grok-4.3",
    }
)


def model_candidate_has_tag(candidate: Any, tag: str) -> bool:
    """Return whether a validated-looking model candidate declares one tag."""
    if not isinstance(candidate, Mapping):
        return False
    tags = candidate.get("tags")
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return False
    return tag in tags


def image_input_support(llm: Any) -> bool | None:
    """Resolve explicit support, catalog tags, then the model ID allowlist."""
    supports = getattr(type(llm), "supports", None)
    if supports is not None:
        supports = supports.__get__(llm, type(llm))
    elif hasattr(llm, "__dict__"):
        supports = vars(llm).get("supports")
    if callable(supports):
        try:
            declared = supports("image_input")
        except (TypeError, ValueError):
            declared = None
        if isinstance(declared, bool):
            return declared

    capabilities = getattr(llm, "capabilities", None)
    if isinstance(capabilities, Mapping):
        declared = capabilities.get("image_input")
        if isinstance(declared, bool):
            return declared

    model = str(getattr(llm, "model", "") or "").strip()
    candidates = tuple(getattr(llm, "auto_model_candidates", ()) or ())
    current = next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping) and candidate.get("model") == model
        ),
        None,
    )
    if current is not None and isinstance(
        current.get("tags"), (list, tuple, set, frozenset)
    ):
        return model_candidate_has_tag(current, "vision")

    normalized_model = model.lower()
    if any(name in normalized_model for name in _IMAGE_INPUT_MODELS):
        return True
    return None


__all__ = ["image_input_support", "model_candidate_has_tag"]
