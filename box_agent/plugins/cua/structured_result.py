"""Expose driver-issued grounding data without duplicating trees or image bytes."""

from __future__ import annotations

import json
from typing import Any


def _successful_query_result(result: Any) -> bool:
    """Only summarize recognized successes; retain uncertain/error evidence."""
    if not isinstance(result, dict):
        return False
    detail = result.get("structuredContent")
    if not isinstance(detail, dict):
        return False
    for envelope in (result, detail):
        if any(
            envelope.get(key) for key in ("isError", "is_error", "error", "refusal")
        ):
            return False
        if envelope.get("code") is not None or envelope.get("success") is False:
            return False
        if envelope.get("effect") not in (None, "confirmed", "success", "succeeded"):
            return False
        if envelope.get("status") not in (None, "ok", "success", "succeeded"):
            return False
    return True


def cua_model_payload(payload: Any) -> Any:
    """Keep observations/action evidence, not repeated successful query trees.

    The MCP loader preserves the original envelope in rawOutput. This projection
    applies only to the adaptor envelope, never to direct Driver responses.
    """
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or "output" not in payload
        or not isinstance(payload.get("backend_events"), list)
    ):
        return cua_structured_payload(payload)
    queries = {"list_apps", "list_windows", "get_window_state", "get_screen_size"}
    counts = {}
    reports = []
    for event in payload["backend_events"]:
        if not isinstance(event, dict):
            reports.append(event)
            continue
        tool, phase = event.get("tool"), event.get("phase")
        if tool in queries and phase == "dispatch":
            continue
        if tool in queries and phase == "result":
            counts[tool] = counts.get(tool, 0) + 1
            result = event.get("result")
            if _successful_query_result(result):
                continue
        reports.append(event)
    context = payload.get("adapter_context")
    if isinstance(context, dict) and context.get("fresh") is False:
        # Actions invalidate these IDs. Keep their historical copy in rawOutput,
        # not as a second stale tree in the model's next action result.
        context = {
            k: v for k, v in context.items() if k not in {"elements", "element_windows"}
        }
    if isinstance(context, dict) and isinstance(context.get("element_windows"), dict):
        windows, owners = {}, {}
        for element, window in context["element_windows"].items():
            if not isinstance(window, dict) or "window_id" not in window:
                break
            window_id = str(window["window_id"])
            if window_id in windows and windows[window_id] != window:
                break  # Preserve conflicting evidence rather than merging it.
            windows[window_id] = window
            owners[element] = window["window_id"]
        else:
            # The source window's bounds/title/PID were repeated for every AX
            # element. Reference one exact row instead; no grounding is lost.
            context = {
                **{k: v for k, v in context.items() if k != "element_windows"},
                "element_window_ids": owners,
                "windows_by_id": windows,
            }
    return cua_structured_payload(
        {
            **{key: value for key, value in payload.items() if key != "backend_events"},
            **({"adapter_context": context} if "adapter_context" in payload else {}),
            "backend_query_counts": counts,
            "backend_action_events": reports,
            "backend_evidence_location": "rawOutput.mcp_structured_content.backend_events",
        }
    )


def cua_structured_payload(payload: Any) -> Any:
    if not isinstance(payload, (dict, list)) or not payload:
        return None

    def clean(value):
        if isinstance(value, dict):
            result = {}
            # Grounding identifiers must survive ordinary tool-result limits.
            priority = (
                "snapshot_id",
                "pid",
                "window_id",
                "element_token",
                "element_index",
            )
            keys = [key for key in priority if key in value]
            keys.extend(key for key in value if key not in priority)
            for key in keys:
                item = value[key]
                if key == "tree_markdown":
                    continue  # The normal text content already contains this tree.
                if isinstance(item, str) and (
                    "base64" in key.lower()
                    or key in {"image", "screenshot", "data"}
                    and len(item) > 1024
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
        + json.dumps(
            cleaned, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    )
