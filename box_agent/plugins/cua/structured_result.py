"""Expose driver-issued grounding data without duplicating trees or image bytes."""
from __future__ import annotations

import json
from typing import Any


def cua_structured_payload(payload: Any) -> Any:
    if not isinstance(payload, (dict, list)) or not payload:
        return None

    def clean(value):
        if isinstance(value, dict):
            result = {}
            # Grounding identifiers must survive ordinary tool-result limits.
            priority = ("snapshot_id", "pid", "window_id", "element_token", "element_index")
            keys = [key for key in priority if key in value]
            keys.extend(key for key in value if key not in priority)
            for key in keys:
                item = value[key]
                if key == "tree_markdown":
                    continue  # The normal text content already contains this tree.
                if isinstance(item, str) and (
                    "base64" in key.lower()
                    or key in {"image", "screenshot", "data"} and len(item) > 1024
                ):
                    continue
                result[key] = clean(item)
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(payload)


def cua_structured_text(payload: Any) -> str | None:
    cleaned = cua_structured_payload(payload)
    if cleaned is None:
        return None
    return (
        "CUA structured result (use only the exact returned window/snapshot/element identifiers; "
        "never invent tokens or combine identifiers from different snapshots):\n"
        + json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )
