"""Offline checks for numeric-report evidence and original table validation order.

These checks consume captured ACP evidence. They neither invoke a model nor
claim to infer arbitrary prose semantics. Named metrics are exact assertions;
other decimal literals are surfaced for independent review when unmapped.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any

_NUMBER = r"[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace("−", "-").replace(",", ""))


def audit_numeric_claims(text: str, expected: Mapping[str, str], *,
                         known_values: Sequence[str] = ()) -> dict[str, Any]:
    """Check every required named metric and flag unexplained decimal literals.

    expected values may end in '%'; percentage units must match. Labels must
    appear once, on their own line, as requested by the evaluation fixture.
    A known number elsewhere is not proof that its surrounding claim is true.
    """
    claims = []
    known = {_decimal(value.rstrip("%")) for value in [*expected.values(), *known_values]}
    for name, wanted in expected.items():
        pattern = re.compile(
            rf"^\s*(?:[-*]\s+)?[*`]*{re.escape(name)}[*`]*\s*[:：]\s*"
            rf"[*`]*({_NUMBER})\s*(%?)[*`]*\s*$", re.MULTILINE,
        )
        found = pattern.findall(text)
        passed = (len(found) == 1 and _decimal(found[0][0]) == _decimal(wanted.rstrip("%"))
                  and bool(found[0][1]) == wanted.endswith("%"))
        claims.append({"name": name, "expected": wanted,
                       "observed": [number + unit for number, unit in found], "passed": passed})
    unmapped = []
    # A separate pattern avoids backtracking the optional decimal part above.
    literals = re.compile(r"(?<![\w.])([+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d+)(?![\w.])")
    for line_number, line in enumerate(text.splitlines(), 1):
        for match in literals.finditer(line):
            if _decimal(match[1]) not in known:
                unmapped.append({"line": line_number, "literal": match[1], "text": line})
    return {"named_metrics_passed": all(c["passed"] for c in claims), "claims": claims,
            "unmapped_numeric_lines": unmapped,
            "requires_semantic_review": True}


def audit_validation_order(events: Sequence[dict]) -> list[dict]:
    """Find successful execution before trusted per-turn input receipts.

    Failed attempts rejected by the admission gate do not count as execution.
    Completion checkpoints and their final-state checks remain separate.
    """
    sources: dict[str, set[str]] = {}
    receipts: dict[str, set[str]] = {}
    requests: dict[str, tuple[int, dict]] = {}
    outcomes = []
    for line, event in enumerate(events, 1):
        turn = event.get("turn_id", "")
        kind, data = event.get("event"), event.get("data", {})
        if kind == "table.inputs":
            sources.setdefault(turn, set()).update(data["sources"])
        elif kind == "tool.request":
            requests[event["tool_call_id"]] = (line, event)
        elif kind == "tool.response":
            request_line, request = requests.get(event.get("tool_call_id"), (None, {}))
            name = data.get("tool_name")
            if name == "validate_table" and data.get("success"):
                payload = data.get("raw_output") or {}
                if payload.get("validation_complete") and payload.get("data_valid"):
                    path = request.get("data", {}).get("arguments", {}).get("path", "")
                    # Raw receipts use a resolved source path; match that first.
                    resolved = payload.get("path") or path
                    receipts.setdefault(turn, set()).add(str(resolved))
            elif name in {"execute_code", "bash", "sub_agent"} and data.get("success"):
                required = sources.get(turn, set())
                validated = receipts.get(turn, set())
                missing = sorted(p for p in required if p not in validated)
                outcomes.append({"turn": turn, "tool": name, "request_line": request_line,
                                 "response_line": line, "passed": not missing,
                                 "missing_sources": missing})
    return outcomes


def audit_json_artifacts(workspace: Path, expected: Mapping[str, dict]) -> list[dict]:
    """Verify required JSON deliveries against captured source responses.

    Correct numbers embedded in another report do not substitute for a required
    source receipt. Paths are explicit evaluation contracts, not model claims.
    """
    results = []
    for name, response in expected.items():
        path = workspace / name
        try:
            if not path.resolve().is_relative_to(workspace.resolve()):
                raise ValueError("artifact resolves outside workspace")
            actual = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            results.append({"path": name, "passed": False, "error": str(exc)})
        else:
            results.append({"path": name, "passed": actual == response,
                            "expected": response, "actual": actual})
    return results
