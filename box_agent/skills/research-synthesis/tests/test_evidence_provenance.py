"""Exercise the bundled validator's evidence-to-presentation boundary."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


VALIDATOR = Path(__file__).resolve().parents[1] / "scripts/validate_research_artifacts.py"


def _validate(tmp_path: Path, basis: str, **overrides: object) -> dict:
    for suffix in ("dim01", "cross_verification", "insight"):
        (tmp_path / f"topic_{suffix}.md").write_text("# Research\n\nSource finding.\n")
    row = {
        "entity": "Example Corp",
        "claim": "Example Corp launched Product One in 2026.",
        "source_url": "https://example.com/product-one",
        "source_type": "first_party",
        "evidence_excerpt": "Example Corp launched Product One in 2026.",
        "evidence_basis": basis,
        "confidence": "medium",
        "status": "verified",
        **overrides,
    }
    ledger = {
        "schema_version": 1,
        "topic": "topic",
        "target_entities": [
            {
                "entity": "Example Corp",
                "aliases": ["Example"],
                "official_domains": ["example.com"],
            }
        ],
        "evidence": [row],
    }
    (tmp_path / "topic_evidence.json").write_text(json.dumps(ledger))
    report = tmp_path / "qa/report.json"
    result = subprocess.run(
        [
            sys.executable, str(VALIDATOR), "--research-dir", str(tmp_path),
            "--topic", "topic", "--route", "B", "--min-dimensions", "1",
            "--report", str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert report.is_file(), result.stdout + result.stderr
    return json.loads(report.read_text())


@pytest.mark.parametrize("basis", ["page_body", "search_summary", "page_summary"])
def test_handoff_preserves_the_actual_evidence_basis(tmp_path: Path, basis: str) -> None:
    report = _validate(tmp_path, basis)
    facts = report["presentation_handoff"]["verified_facts"]
    assert report["ok"] is True
    assert len(facts) == 1
    assert facts[0]["evidence_basis"] == basis
    assert report["verified_evidence"][0]["confidence"] == "medium"


@pytest.mark.parametrize("basis", ["search_summary", "page_summary"])
def test_summary_with_high_confidence_surfaces_a_warning(
    tmp_path: Path, basis: str,
) -> None:
    report = _validate(tmp_path, basis, confidence="high")
    assert any(
        basis in warning and "confidence=medium" in warning
        for warning in report["warnings"]
    )


def test_summary_cannot_bypass_claim_support_checks(tmp_path: Path) -> None:
    report = _validate(
        tmp_path, "page_summary", claim="Example Corp launched Product One in 2027."
    )
    assert report["presentation_handoff"]["verified_facts"] == []
    assert any("2027" in item for item in report["issues"] + report["warnings"])


def test_unknown_provenance_does_not_enter_verified_handoff(tmp_path: Path) -> None:
    report = _validate(tmp_path, "unrecognized_basis")
    assert report["presentation_handoff"]["verified_facts"] == []
    assert any("evidence_basis" in item for item in report["issues"] + report["warnings"])


def _capture(tmp_path: Path, **overrides: object) -> str:
    path = tmp_path / "sources/read-01.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "reader": "managed_browser_dom",
        "evidence_basis": "page_body",
        "requested_url": "https://example.com/old-link",
        "final_url": "https://example.com/product-one",
        "navigation_status": 200,
        "title": "Example Corp product announcement",
        "text": "Published in 2026.\n\nIt launched Product One for customers.",
        **overrides,
    }))
    return "sources/read-01.json"


def test_literal_excerpt_can_use_separate_bound_heading_and_date(tmp_path: Path) -> None:
    ref = _capture(tmp_path)
    context = ["Example Corp product announcement", "Published in 2026."]
    report = _validate(
        tmp_path, "page_body", source_capture=ref,
        evidence_excerpt="It launched Product One for customers.",
        evidence_context=context,
    )
    assert report["ok"] is True
    record = report["verified_evidence"][0]
    assert record["evidence_context"] == context
    assert record["source_capture"] == ref
    assert len(record["source_capture_sha256"]) == 64
    assert report["presentation_handoff"]["verified_facts"][0]["source_capture"] == ref


def test_capture_binding_allows_whitespace_layout_changes(tmp_path: Path) -> None:
    ref = _capture(tmp_path, text="Example Corp\n launched  Product One\t in 2026.")
    report = _validate(tmp_path, "page_body", source_capture=ref)
    assert report["ok"] is True


@pytest.mark.parametrize("excerpt", [
    "Example Corp launched Product One for customers.",
    "It launched Product One for customers in 2026.",
    "It successfully launched Product One for customers.",
])
def test_rewritten_excerpt_is_excluded_even_when_claim_tokens_match(
    tmp_path: Path, excerpt: str,
) -> None:
    ref = _capture(tmp_path)
    report = _validate(
        tmp_path, "page_body", source_capture=ref, evidence_excerpt=excerpt,
        evidence_context=["Example Corp product announcement", "Published in 2026."],
    )
    assert report["presentation_handoff"]["verified_facts"] == []
    assert any("verbatim" in item for item in report["issues"])


def test_invented_context_cannot_supply_entity_or_date(tmp_path: Path) -> None:
    ref = _capture(tmp_path)
    report = _validate(
        tmp_path, "page_body", source_capture=ref,
        evidence_excerpt="It launched Product One for customers.",
        evidence_context=["Example Corp published this on 8 November 2026."],
    )
    assert report["presentation_handoff"]["verified_facts"] == []
    assert any("evidence_context.0" in item for item in report["issues"])


def test_redirected_capture_must_bind_its_final_document_url(tmp_path: Path) -> None:
    ref = _capture(tmp_path, text="Example Corp launched Product One in 2026.")
    report = _validate(tmp_path, "page_body", source_capture=ref,
                       source_url="https://example.com/old-link")
    assert report["presentation_handoff"]["verified_facts"] == []
    assert any("final_url" in item for item in report["issues"])


@pytest.mark.parametrize("change", [
    {"reader": "generated_summary"},
    {"evidence_basis": "page_summary"},
    {"text": None},
    {"navigation_status": 403},
])
def test_invalid_capture_is_not_accepted_as_raw_source(tmp_path: Path, change: dict) -> None:
    ref = _capture(tmp_path, **change)
    report = _validate(tmp_path, "page_body", source_capture=ref)
    assert report["presentation_handoff"]["verified_facts"] == []
    assert any("source_capture" in item for item in report["issues"])


def test_missing_capture_does_not_fall_back_to_self_authored_excerpt(tmp_path: Path) -> None:
    report = _validate(tmp_path, "page_body", source_capture="sources/missing.json")
    assert report["presentation_handoff"]["verified_facts"] == []


def test_unbound_context_does_not_relax_legacy_support_checks(tmp_path: Path) -> None:
    report = _validate(
        tmp_path, "page_body", evidence_excerpt="It launched Product One.",
        evidence_context=["Example Corp", "2026"],
    )
    assert report["presentation_handoff"]["verified_facts"] == []
    assert any("requires source_capture" in item for item in report["issues"])


def test_legacy_evidence_remains_usable_with_explicit_binding_limit(tmp_path: Path) -> None:
    report = _validate(tmp_path, "page_body")
    assert len(report["presentation_handoff"]["verified_facts"]) == 1
    assert any("not checked against a source capture" in item for item in report["warnings"])


def test_capture_reference_cannot_escape_research_directory(tmp_path: Path) -> None:
    report = _validate(tmp_path, "page_body", source_capture="../outside.json")
    assert report["presentation_handoff"]["verified_facts"] == []
    assert any("inside the research directory" in item for item in report["issues"])


@pytest.mark.parametrize("reference", [None, 7, "bad\x00path"])
def test_invalid_capture_path_is_reported_without_crashing(tmp_path: Path, reference: object) -> None:
    report = _validate(tmp_path, "page_body", source_capture=reference)
    assert report["presentation_handoff"]["verified_facts"] == []
    assert any("source_capture" in item for item in report["issues"])


def test_adding_capture_binding_keeps_the_canonical_fact_id(tmp_path: Path) -> None:
    legacy = _validate(tmp_path, "page_body")
    ref = _capture(tmp_path, text="Example Corp launched Product One in 2026.")
    bound = _validate(tmp_path, "page_body", source_capture=ref)
    old_fact = legacy["presentation_handoff"]["verified_facts"][0]
    new_fact = bound["presentation_handoff"]["verified_facts"][0]
    assert new_fact["id"] == old_fact["id"]
    assert new_fact["canonical"] == old_fact["canonical"]
