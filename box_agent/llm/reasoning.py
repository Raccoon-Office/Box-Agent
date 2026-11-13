"""Validated, provider-independent session reasoning choices."""
from typing import Literal, cast

ReasoningEffort = Literal["fast", "medium", "high", "xhigh", "max"]
REASONING_EFFORTS = ("fast", "medium", "high", "xhigh", "max")


def parse_reasoning_effort(value: object) -> ReasoningEffort | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in REASONING_EFFORTS:
        raise ValueError("reasoning_effort must be fast, medium, high, xhigh or max")
    return cast(ReasoningEffort, value)
