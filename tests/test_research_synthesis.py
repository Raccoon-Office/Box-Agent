"""Regression coverage for the compact research-synthesis handoff."""

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


def _write_research(
    research: Path,
    *,
    evidence: list[dict[str, str]] | None = None,
    schema_version: int = 1,
    narrative: str = "# Research\n\nA concise source-backed conclusion.\n",
) -> None:
    research.mkdir(parents=True)
    (research / "topic_research.md").write_text(narrative, encoding="utf-8")
    records = evidence
    if records is None:
        records = [
            {
                "entity": "Example Corp",
                "claim": "Example Corp launched Product One in 2026.",
                "source_url": "https://example.com/news/product-one",
                "source_type": "first_party",
                "evidence_excerpt": "Example Corp launched Product One in 2026.",
                "confidence": "high",
                "status": "verified",
            }
        ]
    (research / "topic_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "topic": "topic",
                "evidence": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _run_validator(
    research: Path,
    *,
    extra_args: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], dict]:
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
            "--report",
            str(report),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, json.loads(report.read_text(encoding="utf-8"))


def test_validator_accepts_one_consolidated_narrative_without_dimensions(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_research(research)

    result, payload = _run_validator(research)

    assert result.returncode == 0, result.stdout + result.stderr
    assert payload["ok"] is True
    assert payload["quality_ok"] is True
    assert payload["delivery_allowed"] is True
    assert payload["handoff_status"] == "full"
    assert payload["min_dimensions"] is None
    assert payload["dimension_count"] == 0
    assert payload["evidence_schema_version"] == 1
    assert payload["verified_evidence_count"] == 1
    assert payload["presentation_handoff"]["delivery_mode"] == "full"
    assert payload["presentation_handoff"]["quality_summary"][
        "recommended_dimensions"
    ] is None


def test_deprecated_dimension_option_is_ignored_instead_of_blocking(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_research(research)

    result, payload = _run_validator(
        research,
        extra_args=("--min-dimensions", "20"),
    )

    assert result.returncode == 0
    assert payload["delivery_allowed"] is True
    assert payload["handoff_status"] == "full"
    assert payload["dimension_count"] == 0


def test_validator_accepts_optional_extra_metadata_and_schema_two(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        schema_version=2,
        evidence=[
            {
                "claim": "Example Corp launched Product One in 2026.",
                "source_url": "https://example.com/news/product-one",
                "evidence_excerpt": "The company launched Product One in 2026.",
                "status": "verified",
                "fact_key": "product.launch",
                "time_basis": "2026",
            }
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["evidence_schema_version"] == 2
    fact = payload["presentation_handoff"]["verified_facts"][0]
    assert fact["entity"] == "topic"
    assert fact["source_type"] == "secondary"


def test_validator_accepts_file_reference_for_file_only_evidence(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        evidence=[
            {
                "claim": "The supplied report states the project goal.",
                "source_url": "file:strategy.pdf#page=3",
                "source_type": "user_input",
                "evidence_excerpt": "Project goal: improve delivery quality.",
                "status": "verified",
            }
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["verified_evidence_count"] == 1


def test_validator_excludes_search_results_and_homepages_without_repair_loop(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        evidence=[
            {
                "claim": "Discovery result",
                "source_url": "https://www.google.com/search?q=example",
                "evidence_excerpt": "Search snippet",
                "status": "verified",
            },
            {
                "claim": "Homepage result",
                "source_url": "https://example.com/",
                "evidence_excerpt": "Homepage text",
                "status": "verified",
            },
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["delivery_allowed"] is True
    assert payload["handoff_status"] == "framework"
    assert payload["verified_evidence_count"] == 0
    warnings = "\n".join(payload["warnings"])
    assert "search-results page" in warnings
    assert "origin homepage" in warnings


def test_validator_keeps_verified_subset_and_reports_unverified_as_gap(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        evidence=[
            {
                "claim": "Supported claim",
                "source_url": "https://example.com/report/one",
                "evidence_excerpt": "Supported claim",
                "status": "verified",
            },
            {
                "claim": "Candidate only",
                "source_url": "https://example.com/report/two",
                "status": "unverified",
                "unverified_reason": "The page could not be read.",
            },
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["handoff_status"] == "partial"
    assert payload["verified_evidence_count"] == 1
    assert payload["unverified_evidence_count"] == 1
    assert len(payload["presentation_handoff"]["verified_facts"]) == 1


def test_markdown_format_findings_are_advisory(tmp_path: Path) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        narrative="# Research\n\nA supported statement.[^missing]\n",
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["delivery_allowed"] is True
    assert payload["handoff_status"] == "partial"
    assert any("missing footnote definitions" in item for item in payload["warnings"])


def test_missing_core_artifacts_remains_invalid(tmp_path: Path) -> None:
    research = tmp_path / "research"
    research.mkdir()
    (research / "topic_evidence.json").write_text(
        '{"schema_version": 1, "topic": "topic", "evidence": []}\n',
        encoding="utf-8",
    )

    result, payload = _run_validator(research)

    assert result.returncode == 1
    assert payload["delivery_allowed"] is False
    assert payload["handoff_status"] == "invalid"
    assert "missing narrative research artifact" in "\n".join(payload["issues"])


def test_validator_excludes_automatically_detected_fact_conflicts(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp launched Product One in 2026.",
                "source_url": "https://example.com/news/launch-a",
                "evidence_excerpt": "Example Corp launched Product One in 2026.",
                "status": "verified",
            },
            {
                "entity": "Example Corp",
                "claim": "Example Corp launched Product One in 2025.",
                "source_url": "https://example.com/news/launch-b",
                "evidence_excerpt": "Example Corp launched Product One in 2025.",
                "status": "verified",
            },
            {
                "entity": "Example Corp",
                "claim": "Example Corp operates Product Two in 2026.",
                "source_url": "https://example.com/news/product-two",
                "evidence_excerpt": "Example Corp operates Product Two in 2026.",
                "status": "verified",
            },
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["handoff_status"] == "partial"
    assert payload["verified_evidence_count"] == 1
    assert payload["auto_conflicting_evidence_count"] == 2
    assert payload["detected_conflict_count"] == 1
    assert payload["verified_evidence"][0]["claim"].endswith("Product Two in 2026.")
    assert any("conflict for the same fact" in item for item in payload["warnings"])


def test_validator_allows_same_fact_at_explicitly_different_times(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        schema_version=2,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp revenue was 10 million in 2025.",
                "source_url": "https://example.com/filing/2025",
                "evidence_excerpt": "Example Corp revenue was 10 million in 2025.",
                "fact_key": "revenue.total",
                "fact_value": "10",
                "time_basis": "2025",
                "scope": "global",
                "unit": "USD-million",
                "status": "verified",
            },
            {
                "entity": "Example Corp",
                "claim": "Example Corp revenue was 12 million in 2026.",
                "source_url": "https://example.com/filing/2026",
                "evidence_excerpt": "Example Corp revenue was 12 million in 2026.",
                "fact_key": "revenue.total",
                "fact_value": "12",
                "time_basis": "2026",
                "scope": "global",
                "unit": "USD-million",
                "status": "verified",
            },
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["handoff_status"] == "full"
    assert payload["verified_evidence_count"] == 2
    assert payload["detected_conflict_count"] == 0


def test_validator_excludes_opposite_claim_excerpt_direction(tmp_path: Path) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp revenue rose 20% in 2026.",
                "source_url": "https://example.com/filing/revenue",
                "evidence_excerpt": "Example Corp revenue fell 20% in 2026.",
                "status": "verified",
            }
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["handoff_status"] == "framework"
    assert payload["verified_evidence_count"] == 0
    assert any("claim direction" in item for item in payload["warnings"])


def test_validator_excludes_chinese_direction_mismatch(tmp_path: Path) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp 2026年营收增长20%。",
                "source_url": "https://example.com/filing/revenue-cn",
                "evidence_excerpt": "Example Corp 2026年营收下降20%。",
                "status": "verified",
            }
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["verified_evidence_count"] == 0
    assert any("claim direction" in item for item in payload["warnings"])


def test_validator_excludes_negated_claim_against_affirmed_excerpt(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp did not launch Product One in 2026.",
                "source_url": "https://example.com/news/launch",
                "evidence_excerpt": "Example Corp launched Product One in 2026.",
                "status": "verified",
            }
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["verified_evidence_count"] == 0
    assert any("negates term" in item for item in payload["warnings"])


def test_validator_excludes_excerpt_missing_claim_number(tmp_path: Path) -> None:
    research = tmp_path / "research"
    _write_research(
        research,
        evidence=[
            {
                "entity": "Example Corp",
                "claim": "Example Corp revenue reached 48.1% in 2026.",
                "source_url": "https://example.com/filing/share",
                "evidence_excerpt": "Example Corp reported growth in 2026.",
                "status": "verified",
            }
        ],
    )

    result, payload = _run_validator(research)

    assert result.returncode == 0
    assert payload["verified_evidence_count"] == 0
    assert any("missing claim number" in item for item in payload["warnings"])


def test_research_instructions_keep_source_checks_but_remove_fixed_structure() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    routes = (SKILL_ROOT / "references" / "routes.md").read_text(encoding="utf-8")
    output_contract = (SKILL_ROOT / "references" / "output_contract.md").read_text(
        encoding="utf-8"
    )
    prompts = (SKILL_ROOT / "references" / "prompts.md").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((skill, routes, output_contract, prompts))

    assert "there is no default" in skill
    assert "minimum, or target count" in skill
    assert "Default to only two research artifacts" in skill
    assert "Focused search has no fixed five-query or ten-dimension requirement" in routes
    assert "exact opened article, report, filing, or data page" in output_contract
    assert "Do not create one artifact per agent or dimension" in prompts
    assert "fact_key" in output_contract
    assert "no `fact_key`" in output_contract
    assert "schema v2 exactly" not in combined
    assert "10-20 dimensions" not in combined


def test_research_instructions_use_artifact_relative_validator_paths() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "--research-dir research" in skill
    assert '--report "research/qa/{topic}_research_check.json"' in skill
    assert '"$RESEARCH_SYNTHESIS_SKILL_DIR/scripts/validate_research_artifacts.py"' in skill
    assert "$(pwd)/output/research" not in skill
