"""Regression coverage for research-synthesis handoff validation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


VALIDATOR = (
    Path(__file__).resolve().parents[1]
    / "box_agent"
    / "skills"
    / "research-synthesis"
    / "scripts"
    / "validate_research_artifacts.py"
)
SKILL_ROOT = VALIDATOR.parents[1]


def _write_focused_research(
    research: Path,
    *,
    dimensions: int = 3,
    evidence: list[dict[str, str]] | None = None,
) -> None:
    research.mkdir(parents=True)
    for index in range(1, dimensions + 1):
        (research / f"topic_dim{index:02d}.md").write_text(
            f"# Dimension {index}\n\nEvidence for dimension {index}.\n",
            encoding="utf-8",
        )
    (research / "topic_cross_verification.md").write_text(
        "# Cross Verification\n\n## High Confidence\n\nConfirmed.\n",
        encoding="utf-8",
    )
    (research / "topic_insight.md").write_text(
        "# Insight\n\nCross-dimension conclusion.\n",
        encoding="utf-8",
    )
    (research / "topic_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": "topic",
                "target_entities": [
                    {
                        "entity": "Example Corp",
                        "aliases": ["Example"],
                        "official_domains": ["example.com"],
                    }
                ],
                "evidence": evidence
                or [
                    {
                        "entity": "Example Corp",
                        "claim": "Example Corp launched Product One in 2026.",
                        "source_url": "https://example.com/news/product-one",
                        "source_type": "first_party",
                        "evidence_excerpt": (
                            "Example Corp launched Product One for customers in 2026."
                        ),
                        "confidence": "high",
                        "status": "verified",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_validator_writes_success_report_for_reduced_focused_route(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_focused_research(research)
    report = research / "qa" / "topic_research_check.json"

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--research-dir",
            str(research),
            "--topic",
            "topic",
            "--route",
            "B",
            "--min-dimensions",
            "3",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["quality_ok"] is True
    assert payload["delivery_allowed"] is True
    assert payload["handoff_status"] == "full"
    assert payload["validator"] == "research-synthesis"
    assert payload["route"] == "B"
    assert payload["min_dimensions"] == 3
    assert payload["dimension_count"] == 3
    assert len(payload["files_checked"]) == 6
    assert payload["evidence_schema_version"] == 1
    assert payload["verified_evidence_count"] == 1
    handoff = payload["presentation_handoff"]
    assert handoff["schema_version"] == 1
    assert handoff["delivery_mode"] == "full"
    assert handoff["verified_facts"][0]["canonical"] == payload[
        "verified_evidence"
    ][0]["canonical"]
    assert handoff["quality_summary"]["quality_ok"] is True
    assert payload["first_party_entity_count"] == 1
    assert payload["verified_evidence"][0]["canonical"] == (
        "Example Corp | Example Corp launched Product One in 2026. | "
        "first_party | https://example.com/news/product-one"
    )


def test_validator_writes_failed_report_when_research_is_too_shallow(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_focused_research(research, dimensions=2)
    report = research / "qa" / "topic_research_check.json"

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--research-dir",
            str(research),
            "--topic",
            "topic",
            "--route",
            "B",
            "--min-dimensions",
            "3",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["delivery_allowed"] is True
    assert payload["handoff_status"] == "partial"
    assert payload["presentation_handoff"]["delivery_mode"] == "partial"
    assert payload["presentation_handoff"]["quality_summary"]["quality_ok"] is False
    assert payload["dimension_count"] == 2
    assert "expected at least 3 dimension files, found 2" in payload["issues"]


def test_validator_rejects_cross_entity_excerpt_mismatch(tmp_path: Path) -> None:
    research = tmp_path / "research"
    _write_focused_research(
        research,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp launched Product One in 2026.",
                "source_url": "https://example.com/news/product-one",
                "source_type": "first_party",
                "evidence_excerpt": "Another Company launched Product One in 2026.",
                "confidence": "high",
                "status": "verified",
            }
        ],
    )
    report = research / "qa" / "topic_research_check.json"

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--research-dir",
            str(research),
            "--topic",
            "topic",
            "--route",
            "B",
            "--min-dimensions",
            "3",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert any(
        "evidence_excerpt does not name entity" in issue
        for issue in payload["issues"]
    )


def test_validator_rejects_first_party_source_on_wrong_domain(tmp_path: Path) -> None:
    research = tmp_path / "research"
    _write_focused_research(
        research,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp launched Product One in 2026.",
                "source_url": "https://unrelated.example/news/product-one",
                "source_type": "first_party",
                "evidence_excerpt": (
                    "Example Corp launched Product One for customers in 2026."
                ),
                "confidence": "high",
                "status": "verified",
            }
        ],
    )
    report = research / "qa" / "topic_research_check.json"

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--research-dir",
            str(research),
            "--topic",
            "topic",
            "--route",
            "B",
            "--min-dimensions",
            "3",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert any(
        "does not match an official domain" in issue for issue in payload["issues"]
    )
    assert any(
        "no verified first_party evidence" in warning
        for warning in payload["warnings"]
    )


def test_validator_allows_partial_delivery_with_verified_subset(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_focused_research(
        research,
        dimensions=2,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp launched Product One in 2026.",
                "source_url": "https://example.com/news/product-one",
                "source_type": "first_party",
                "evidence_excerpt": (
                    "Example Corp launched Product One for customers in 2026."
                ),
                "confidence": "high",
                "status": "verified",
            },
            {
                "entity": "Example Corp",
                "claim": "Example Corp reached 48.1% in 2026.",
                "source_url": "https://example.com/news/unread",
                "source_type": "secondary",
                "evidence_excerpt": "A search result mentioned market growth.",
                "confidence": "low",
                "status": "unverified",
                "unverified_reason": "The exact page could not be read.",
            },
        ],
    )
    report = research / "qa" / "topic_research_check.json"

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--research-dir",
            str(research),
            "--topic",
            "topic",
            "--route",
            "B",
            "--min-dimensions",
            "3",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["quality_ok"] is False
    assert payload["delivery_allowed"] is True
    assert payload["handoff_status"] == "partial"
    assert payload["verified_evidence_count"] == 1
    assert payload["unverified_evidence_count"] == 1
    assert any("48.1%" in warning for warning in payload["warnings"])
    assert not any("48.1%" in issue for issue in payload["issues"])
    assert len(payload["presentation_handoff"]["verified_facts"]) == 1
    assert any(
        "48.1%" in gap for gap in payload["presentation_handoff"]["gaps"]
    )


def test_validator_allows_framework_delivery_without_verified_evidence(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_focused_research(
        research,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp reached 62.2% in 2026.",
                "source_url": "https://example.com/news/unread",
                "source_type": "secondary",
                "evidence_excerpt": "Unverified search discovery only.",
                "confidence": "low",
                "status": "unverified",
                "unverified_reason": "The source page was unavailable.",
            }
        ],
    )
    report = research / "qa" / "topic_research_check.json"

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--research-dir",
            str(research),
            "--topic",
            "topic",
            "--route",
            "B",
            "--min-dimensions",
            "3",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["quality_ok"] is False
    assert payload["delivery_allowed"] is True
    assert payload["handoff_status"] == "framework"
    assert payload["verified_evidence"] == []
    assert payload["presentation_handoff"]["delivery_mode"] == "framework"
    assert payload["presentation_handoff"]["verified_facts"] == []
    assert "no verified evidence available" in "\n".join(payload["issues"])


def test_validator_does_not_allow_delivery_before_minimum_structure_exists(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    research.mkdir()
    (research / "topic_dim01.md").write_text("# One dimension\n", encoding="utf-8")
    report = research / "qa" / "topic_research_check.json"

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--research-dir",
            str(research),
            "--topic",
            "topic",
            "--route",
            "B",
            "--min-dimensions",
            "3",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["delivery_allowed"] is False
    assert payload["handoff_status"] == "invalid"
    assert payload["presentation_handoff"]["delivery_mode"] == "invalid"


def test_validator_excludes_conflicting_user_input_from_verified_handoff(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_focused_research(
        research,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp launched Product One in 2026.",
                "source_url": "https://example.com/news/product-one",
                "source_type": "first_party",
                "evidence_excerpt": (
                    "Example Corp launched Product One for customers in 2026."
                ),
                "confidence": "high",
                "status": "verified",
            },
            {
                "entity": "Example Corp",
                "claim": "Example Corp did not launch Product One in 2025.",
                "source_url": "https://example.com/news/product-one",
                "source_type": "first_party",
                "evidence_excerpt": (
                    "Example Corp launched Product One for customers in 2026, not 2025."
                ),
                "confidence": "high",
                "status": "conflicting",
                "user_input_claim": "Example Corp launched Product One in 2025.",
                "user_input_alignment": "conflicting",
                "conflict_note": "The official launch year conflicts with the user input.",
            },
        ],
    )
    report = research / "qa" / "topic_research_check.json"

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--research-dir",
            str(research),
            "--topic",
            "topic",
            "--route",
            "B",
            "--min-dimensions",
            "3",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["verified_evidence_count"] == 1
    assert payload["conflicting_evidence_count"] == 1
    assert len(payload["verified_evidence"]) == 1
    assert any(
        "excluded from downstream verified evidence" in warning
        for warning in payload["warnings"]
    )


def test_research_instructions_preserve_depth_without_rephrased_query_loops() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    routes = (SKILL_ROOT / "references" / "routes.md").read_text(encoding="utf-8")
    prompts = (SKILL_ROOT / "references" / "prompts.md").read_text(encoding="utf-8")
    output_contract = (
        SKILL_ROOT / "references" / "output_contract.md"
    ).read_text(encoding="utf-8")

    assert "covering distinct evidence gaps" in skill
    assert "do not rerun a near-equivalent" in skill
    assert "standalone Playwright MCP tools are separate" in skill
    assert "source_preference: playwright" in skill
    assert "five distinct evidence intents" in routes
    assert "reworded versions of an already-run entity/fact query do not add depth" in routes
    assert "`research/{topic}_evidence.json`" in skill
    assert "search-result snippet alone is not evidence" in prompts
    assert "user_input_alignment" in output_contract
    assert "verified_evidence" in output_contract


def test_research_instructions_use_artifact_relative_validator_paths() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert '--research-dir "research"' in skill
    assert '--report "research/qa/{topic}_research_check.json"' in skill
    assert "do not use `$(pwd)/output/research`" in skill
    assert '--research-dir "{workspace}/research"' not in skill
