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


RACCOON_THINKING_LEVELS = ("off", "level_1", "level_2", "level_3", "level_4", "level_5")


def parse_raccoon_thinking_level(value: object) -> str:
    if not isinstance(value, str) or value not in RACCOON_THINKING_LEVELS:
        raise ValueError("raccoon_thinking_level must be off or level_1 through level_5")
    return value


def normalize_raccoon_thinking(value: object) -> dict | None:
    """Unavailable/malformed catalog metadata must not enable unified controls."""
    if not isinstance(value, dict):
        return None
    supported, can_disable = value.get("supported"), value.get("can_disable")
    levels, default = value.get("display_levels"), value.get("default_level")
    if type(supported) is not bool or type(can_disable) is not bool or not isinstance(levels, list):
        return None
    if not supported:
        return dict(value) if can_disable and levels == [] and default == "off" else None
    names = []
    for item in levels:
        if not isinstance(item, dict) or item.get("value") not in RACCOON_THINKING_LEVELS[1:]:
            return None
        if not isinstance(item.get("label"), str) or not item["label"].strip():
            return None
        names.append(item["value"])
    if (not names or names != sorted(set(names)) or names[0] != "level_1"
            or (len(names) > 1 and names[-1] != "level_5")
            or default != ("off" if can_disable else "level_1")):
        return None
    return dict(value)


def unified_thinking_params(client: object, thinking_enabled: bool) -> dict | None:
    """None means legacy route; an empty dict means unified with backend defaults."""
    capabilities = getattr(client, "raccoon_thinking_models", {})
    capability = normalize_raccoon_thinking(capabilities.get(getattr(client, "model", None)))
    if capability is None:
        return None
    level = getattr(client, "raccoon_thinking_level", None)
    if not thinking_enabled or level is None or not capability["supported"]:
        return {}
    if level == "off" and not capability["can_disable"]:
        level = "level_1"
    return {"raccoon_thinking_level": level}
