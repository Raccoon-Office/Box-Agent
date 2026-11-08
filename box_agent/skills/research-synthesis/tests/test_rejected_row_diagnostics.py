"""Locate rejected ledger rows without changing evidence or fact identity."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


VALIDATOR = Path(__file__).resolve().parents[1] / "scripts/validate_research_artifacts.py"
CONTEXT_PREFIX = "REJECTED_EVIDENCE: "


def _row(**overrides: object) -> dict:
    return {
        "entity": "Example Corp",
        "claim": "Example Corp launched Product One in 2026.",
        "source_url": "https://example.com/product-one",
        "source_type": "first_party",
        "evidence_excerpt": "Example Corp launched Product One in 2026.",
        "confidence": "high",
        "status": "verified",
        **overrides,
    }


def _validate(tmp_path: Path, rows: list[object]) -> tuple:
    for suffix in ("dim01", "cross_verification", "insight"):
        (tmp_path / f"topic_{suffix}.md").write_text("# Research\n\nSource finding.\n")
    ledger = tmp_path / "topic_evidence.json"
    ledger.write_text(json.dumps({
        "schema_version": 1,
        "topic": "topic",
        "target_entities": [{
            "entity": "Example Corp",
            "aliases": ["Example"],
            "official_domains": ["example.com"],
        }],
        "evidence": rows,
    }, ensure_ascii=False), encoding="utf-8")
    before = ledger.read_bytes()
    report_path = tmp_path / "qa/report.json"
    result = subprocess.run([
        sys.executable, str(VALIDATOR), "--research-dir", str(tmp_path),
        "--topic", "topic", "--route", "B", "--min-dimensions", "1",
        "--report", str(report_path),
    ], capture_output=True, text=True, check=False)
    assert ledger.read_bytes() == before
    assert report_path.is_file(), result.stdout + result.stderr
    return result, json.loads(report_path.read_text(encoding="utf-8"))


def _contexts(result) -> list[dict]:
    return [json.loads(line.removeprefix(CONTEXT_PREFIX))
            for line in result.stderr.splitlines() if line.startswith(CONTEXT_PREFIX)]


def _context(index: int, row: object) -> dict:
    values = row if isinstance(row, dict) else {}
    return {
        "record": f"topic_evidence.json: evidence.{index}",
        **{field: values.get(field) if isinstance(values.get(field), str) else None
           for field in ("entity", "claim", "source_url")},
    }


def test_rejected_row_context_is_reported_once_with_current_values(tmp_path: Path) -> None:
    rejected = _row(
        claim='Example Corp processed 75 "samples" in 2028.\nCurrent wording.',
        evidence_excerpt="Example Corp processed 12 samples in 2026.",
        source_url="https://other.example/report?version=2&locale=en",
    )
    result, report = _validate(tmp_path, [_row(), rejected])
    expected = [_context(1, rejected)]
    assert report.get("rejected_evidence_context") == expected
    assert _contexts(result) == expected
    assert len(report["issues"]) == 2
    assert all("evidence.1" in issue for issue in report["issues"])
    assert any("missing claim number(s): 2028, 75" in issue for issue in report["issues"])
    assert any("does not match an official domain" in issue for issue in report["issues"])
    assert result.returncode == 0
    assert report["presentation_handoff"]["delivery_mode"] == "partial"
    assert [fact["claim"] for fact in report["presentation_handoff"]["verified_facts"]] == [_row()["claim"]]
    assert "rejected_evidence_context" not in report["presentation_handoff"]
    assert report["presentation_handoff"]["gaps"] == report["issues"] + report["warnings"]


@pytest.mark.parametrize("rejected", [
    ["not a record"],
    _row(claim=None),
    _row(entity="Another Organization"),
    _row(status="unknown-status"),
    _row(confidence="low"),
    _row(claim="Example Corp " + "reported results " * 30,
         evidence_excerpt="Example Corp " + "reported results " * 30),
], ids=["non-object", "missing-claim", "unknown-entity", "invalid-status",
        "low-confidence", "long-canonical"])
def test_early_rejection_keeps_record_context(tmp_path: Path, rejected: object) -> None:
    result, report = _validate(tmp_path, [rejected])
    expected = [_context(0, rejected)]
    assert report.get("rejected_evidence_context") == expected
    assert _contexts(result) == expected
    assert report["presentation_handoff"]["verified_facts"] == []
    assert report["issues"]


def test_duplicate_rejection_identifies_only_the_later_record(tmp_path: Path) -> None:
    row = _row()
    result, report = _validate(tmp_path, [row, row])
    assert report.get("rejected_evidence_context") == [_context(1, row)]
    assert _contexts(result) == [_context(1, row)]
    assert len(report["presentation_handoff"]["verified_facts"]) == 1
    assert report["issues"] == ["topic_evidence.json: evidence.1: duplicate verified evidence"]


def test_excerpt_supplement_does_not_change_fact_id(tmp_path: Path) -> None:
    _, initial = _validate(tmp_path, [_row()])
    _, updated = _validate(tmp_path, [_row(
        evidence_excerpt="Example Corp launched Product One for customers in 2026.",
    )])
    before = initial["presentation_handoff"]["verified_facts"][0]
    after = updated["presentation_handoff"]["verified_facts"][0]
    assert before == after
    assert updated.get("rejected_evidence_context", []) == []


def test_supported_claim_revision_still_changes_content_hash(tmp_path: Path) -> None:
    _, initial = _validate(tmp_path, [_row()])
    _, updated = _validate(tmp_path, [_row(
        claim="Example Corp launched Product One for customers in 2026.",
        evidence_excerpt="Example Corp launched Product One for customers in 2026.",
    )])
    before = initial["presentation_handoff"]["verified_facts"][0]
    after = updated["presentation_handoff"]["verified_facts"][0]
    assert before["id"] != after["id"]
    assert before["canonical"] != after["canonical"]
    assert updated["presentation_handoff"]["delivery_mode"] == "full"


def test_valid_legacy_row_has_no_rejection_diagnostic(tmp_path: Path) -> None:
    result, report = _validate(tmp_path, [_row()])
    assert result.returncode == 0
    assert report["ok"] is True
    assert report["issues"] == []
    assert report.get("rejected_evidence_context", []) == []
    assert _contexts(result) == []
    fact = report["presentation_handoff"]["verified_facts"][0]
    assert fact["canonical"] == (
        "Example Corp | Example Corp launched Product One in 2026. | "
        "first_party | https://example.com/product-one"
    )
    assert "evidence_basis" not in fact


@pytest.mark.parametrize("status, note", [
    ("unverified", "unverified_reason"), ("conflicting", "conflict_note"),
])
def test_intentionally_excluded_rows_do_not_add_rejection_context(
    tmp_path: Path, status: str, note: str,
) -> None:
    result, report = _validate(tmp_path, [_row(status=status, **{note: "Not accepted."})])
    assert report["presentation_handoff"]["verified_facts"] == []
    assert report["presentation_handoff"]["delivery_mode"] == "framework"
    assert report.get("rejected_evidence_context", []) == []
    assert _contexts(result) == []
