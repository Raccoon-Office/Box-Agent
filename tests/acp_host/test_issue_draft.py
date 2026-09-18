"""T5 — to_issue_draft contract."""

from __future__ import annotations

import pytest

from .issue_draft import to_issue_draft
from .probe import CaseResult


def test_to_issue_draft_includes_case_id() -> None:
    draft = to_issue_draft(
        CaseResult(
            case_id="T1-02",
            ok=False,
            expected="readable error",
            actual="timeout",
            logs="stderr...",
            repro_steps=["start bad command", "observe RpcError"],
        )
    )
    assert "T1-02" in draft["title"]
    assert "T1-02" in draft["body"]
    assert "readable error" in draft["body"]
    assert "timeout" in draft["body"]


def test_to_issue_draft_forbids_empty_and_success() -> None:
    with pytest.raises(ValueError):
        to_issue_draft(
            CaseResult(
                case_id="T1-01",
                ok=True,
                expected="ok",
                actual="ok",
                logs="",
                repro_steps=["x"],
            )
        )
    with pytest.raises(ValueError):
        to_issue_draft(
            CaseResult(
                case_id="T9-99",
                ok=False,
                expected="",
                actual="x",
                logs="",
                repro_steps=["step"],
            )
        )
    with pytest.raises(ValueError):
        to_issue_draft(
            CaseResult(
                case_id="T9-99",
                ok=False,
                expected="x",
                actual="y",
                logs="",
                repro_steps=[],
            )
        )
