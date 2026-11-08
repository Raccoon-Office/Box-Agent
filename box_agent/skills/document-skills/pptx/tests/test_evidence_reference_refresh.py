"""Refresh derived citations without weakening authored-evidence validation."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


VALIDATOR = Path(__file__).resolve().parents[1] / "scripts/validate_outline.js"
NODE = shutil.which("node")


def _fact(identifier: str, claim: str) -> dict:
    source_url = "https://example.com/news/release"
    return {
        "id": identifier,
        "entity": "Example Corp",
        "claim": claim,
        "source_type": "first_party",
        "source_url": source_url,
        "canonical": f"Example Corp | {claim} | first_party | {source_url}",
    }


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if not NODE:
        pytest.skip("node is required")
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / ".profile"))
    monkeypatch.setenv("BOX_AGENT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("BOX_AGENT_PPT_STRICT_RESEARCH", "1")
    monkeypatch.delenv("BOX_AGENT_SOURCE_TEXT_B64", raising=False)
    return tmp_path


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _handoff(root: Path, *facts: dict) -> None:
    _write(root / "research.json", {"presentation_handoff": {
        "schema_version": 1,
        "delivery_mode": "full",
        "verified_facts": list(facts),
        "quality_summary": {"quality_ok": True},
    }})


def _outline(root: Path, evidence: list[str], ids: list[str] | None = None) -> None:
    slide = {
        "page": 1,
        "title": "Example Corp release",
        "message": "Example Corp announced a dated product release.",
        "bullets": ["2026年8月25日：产品发布", "Start with a defined task"],
        "layout": "timeline",
        "visual": "Timeline of product milestones",
        "evidence": evidence,
        "notes": "",
    }
    if ids is not None:
        slide["evidence_ids"] = ids
    _write(root / "outline.json", {
        "deck_goal": "Explain the product",
        "audience": "Customers",
        "source_mode": "public_authoritative_research",
        "storyline": "Product release to practical evaluation",
        "slides": [slide],
    })


def _replace_ids(root: Path, ids: list[str]) -> None:
    # Retain whatever provenance the CLI persisted; do not name private fields.
    outline = json.loads((root / "outline.json").read_text(encoding="utf-8"))
    outline["slides"][0]["evidence_ids"] = ids
    _write(root / "outline.json", outline)


def _run(root: Path) -> tuple[subprocess.CompletedProcess, dict]:
    result = subprocess.run(
        [
            NODE, str(VALIDATOR), str(root / "outline.json"),
            "--min-slides", "1", "--max-slides", "1",
            "--research-handoff", str(root / "research.json"),
            "--report", str(root / "qa/check.json"), "--repair-flow",
        ],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.stdout.strip(), result.stderr
    return result, json.loads(result.stdout)


def _accepted(root: Path) -> dict:
    result, report = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["ok"] and not report.get("repair_required"), report
    assert not report.get("auto_degraded"), report
    return json.loads((root / "outline.json").read_text(encoding="utf-8"))


def test_replacing_fact_and_id_refreshes_previously_resolved_evidence(case: Path) -> None:
    original = _fact("fact-release-v1", "Example Corp released Alpha on 2026-08-25.")
    revised = _fact("fact-release-v2", "Example Corp released Beta on 2026-08-25.")
    _handoff(case, original)
    _outline(case, [], [original["id"]])
    first = _accepted(case)
    assert first["slides"][0]["evidence"] == [original["canonical"]]

    _handoff(case, revised)
    _replace_ids(case, [revised["id"]])
    refreshed = _accepted(case)
    assert refreshed["slides"][0]["evidence"] == [revised["canonical"]]
    assert refreshed["slides"][0]["bullets"] == first["slides"][0]["bullets"]


def test_removing_an_id_removes_its_derived_evidence(case: Path) -> None:
    release = _fact("fact-release", "Example Corp released Alpha on 2026-08-25.")
    guide = _fact("fact-guide", "Example Corp provides a product guide.")
    _handoff(case, release, guide)
    _outline(case, [], [release["id"], guide["id"]])
    _accepted(case)

    _replace_ids(case, [release["id"]])
    refreshed = _accepted(case)
    assert refreshed["slides"][0]["evidence"] == [release["canonical"]]


def test_explicit_canonical_survives_refresh_of_mixed_id_references(case: Path) -> None:
    original = _fact("fact-release-v1", "Example Corp released Alpha on 2026-08-25.")
    revised = _fact("fact-release-v2", "Example Corp released Beta on 2026-08-25.")
    manual = _fact("fact-guide", "Example Corp provides a product guide.")
    _handoff(case, original, manual)
    _outline(case, [manual["canonical"]], [original["id"]])
    _accepted(case)

    _handoff(case, revised, manual)
    _replace_ids(case, [revised["id"]])
    refreshed = _accepted(case)
    assert set(refreshed["slides"][0]["evidence"]) == {
        revised["canonical"], manual["canonical"],
    }


def test_unknown_replacement_id_fails_without_rewriting_input(case: Path) -> None:
    release = _fact("fact-release", "Example Corp released Alpha on 2026-08-25.")
    _handoff(case, release)
    _outline(case, [], [release["id"]])
    _accepted(case)
    _replace_ids(case, ["missing-fact"])
    before = (case / "outline.json").read_bytes()

    result, report = _run(case)
    assert result.returncode != 0 and not report["ok"]
    assert any("unknown verified fact ID" in issue for issue in report["issues"])
    assert (case / "outline.json").read_bytes() == before


def test_invalidated_handwritten_canonical_is_retained_and_rejected(case: Path) -> None:
    release = _fact("fact-release", "Example Corp released Alpha on 2026-08-25.")
    manual = _fact("fact-guide", "Example Corp provides a product guide.")
    _handoff(case, release, manual)
    _outline(case, [manual["canonical"]], [release["id"]])
    _accepted(case)
    _handoff(case, release)

    result, report = _run(case)
    assert result.returncode != 0 and not report["ok"]
    assert any("not an exact canonical item" in issue for issue in report["issues"])
    saved = json.loads((case / "outline.json").read_text(encoding="utf-8"))
    assert manual["canonical"] in saved["slides"][0]["evidence"]


def test_unmarked_legacy_evidence_is_not_guessed_to_be_a_derived_cache(case: Path) -> None:
    original = _fact("fact-release-v1", "Example Corp released Alpha on 2026-08-25.")
    revised = _fact("fact-release-v2", "Example Corp released Beta on 2026-08-25.")
    _handoff(case, revised)
    # Construct a legacy artifact directly, without any prior resolver receipt.
    _outline(case, [original["canonical"]], [revised["id"]])

    result, report = _run(case)
    assert result.returncode != 0 and not report["ok"]
    assert any("not an exact canonical item" in issue for issue in report["issues"])
    saved = json.loads((case / "outline.json").read_text(encoding="utf-8"))
    assert original["canonical"] in saved["slides"][0]["evidence"]


def test_legacy_canonical_without_ids_remains_accepted(case: Path) -> None:
    release = _fact("fact-release", "Example Corp released Alpha on 2026-08-25.")
    _handoff(case, release)
    _outline(case, [release["canonical"]])

    accepted = _accepted(case)
    assert accepted["slides"][0]["evidence"] == [release["canonical"]]


@pytest.mark.parametrize("update", ["revised", "removed"])
def test_explicit_canonical_matching_an_id_remains_authored(
    case: Path, update: str,
) -> None:
    original = _fact("fact-release-v1", "Example Corp released Alpha on 2026-08-25.")
    revised = _fact("fact-release-v2", "Example Corp released Beta on 2026-08-25.")
    guide = _fact("fact-guide", "Example Corp provides a product guide.")
    _handoff(case, original)
    _outline(case, [original["canonical"]], [original["id"]])
    _accepted(case)

    if update == "revised":
        _handoff(case, revised)
        _replace_ids(case, [revised["id"]])
    else:
        _handoff(case, guide)
        _replace_ids(case, [])

    result, report = _run(case)
    assert result.returncode != 0 and not report["ok"]
    assert any("not an exact canonical item" in issue for issue in report["issues"])
    saved = json.loads((case / "outline.json").read_text(encoding="utf-8"))
    assert original["canonical"] in saved["slides"][0]["evidence"]
