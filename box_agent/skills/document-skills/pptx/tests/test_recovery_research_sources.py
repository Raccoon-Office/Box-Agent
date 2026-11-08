"""Keep validated selected research in recovery without promoting raw evidence."""

import base64
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest


SKILL = Path(os.environ.get("BOX_AGENT_TEST_RECOVERY_SKILL", Path(__file__).resolve().parents[1]))
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(root, *args):
    return subprocess.run([str(NODE), str(SKILL / "scripts/design_plan.js"), *args],
                          cwd=root, capture_output=True, text=True, timeout=60, check=False)


def output(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def recovery(root):
    report = json.loads((root / "qa/design_delivery.json").read_text())
    deck = json.loads(Path(report["deck"]).read_text())
    html = Path(report["primary_artifact"]).read_text()
    match = re.search(r'<script[^>]*id="deck-document"[^>]*>([\s\S]*?)</script>', html)
    assert match, "Recovery must retain the normal editable document metadata"
    embedded = json.loads(match.group(1))
    assert embedded.get("truth_contract", {}) == deck.get("truth_contract", {})
    return report, deck


def sources(deck):
    return deck.get("truth_contract", {}).get("research_facts", [])


@pytest.fixture
def case(tmp_path, monkeypatch):
    if not NODE:
        pytest.skip("Node.js is required")
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / ".profile"))
    monkeypatch.setenv("BOX_AGENT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("BOX_AGENT_PPT_STRICT_RESEARCH", "1")
    monkeypatch.delenv("BOX_AGENT_SOURCE_TEXT_B64", raising=False)
    facts = []
    for key, claim in [("guide", "Example Lab publishes a public guide."),
                       ("checklist", "Example Lab provides an evaluation checklist."),
                       ("training", "Example Lab offers optional training.")]:
        url = f"https://example.com/{key}"
        facts.append({"id": f"fact-{key}", "entity": "Example Lab", "claim": claim,
                      "source_type": "first_party", "source_url": url,
                      "canonical": f"Example Lab | {claim} | first_party | {url}"})
    write(tmp_path / "research.json", {"presentation_handoff": {
        "schema_version": 1, "delivery_mode": "full", "verified_facts": facts,
        "quality_summary": {"quality_ok": True},
    }})
    outline = {
        "deck_goal": "Explain the documented evaluation resources", "audience": "Readers",
        "source_mode": "public_authoritative_research",
        "storyline": "Guide to evaluation criteria to a scoped decision",
        "slides": [
            {"page": index, "title": title, "message": message,
             "bullets": ["Use a scoped task", "Keep human review and the original conditions"],
             "layout": "cards", "visual": "Resource cards", "evidence": [fact["canonical"]],
             "notes": "EDITORIAL_ONLY: retain this planning reminder outside the audience copy"}
            for index, (title, message, fact) in enumerate([
                ("Example Lab guide", facts[0]["claim"], facts[0]),
                ("Example Lab checklist", facts[1]["claim"], facts[1]),
                ("Example Lab next decision", "Example Lab supports a scoped evaluation.", facts[0]),
            ], 1)
        ],
    }
    write(tmp_path / "outline.json", outline)
    return tmp_path, outline, facts


def prepare(root, handoff=True):
    args = ["prepare", "outline.json"]
    if handoff:
        args += ["--research-handoff", "research.json"]
    result = output(run(root, *args))
    assert result.get("ok") is True and "input" in result, result
    return json.loads((root / "design_input.json").read_text())


def test_validated_recovery_keeps_only_selected_sources_in_deck_html_and_pptx(case):
    root, _, facts = case
    prepare(root)
    report, deck = recovery(root)
    expected = [fact["canonical"] for fact in facts[:2]]
    assert sources(deck) == expected
    audience = json.dumps([slide["props"] for slide in deck["slides"]])
    assert "https://example.com/" not in audience and "EDITORIAL_ONLY" not in audience
    pptx = root / "recovery.pptx"
    exported = subprocess.run([
        str(NODE), str(SKILL / "scripts/html_to_editable_pptx.js"),
        report["primary_artifact"], str(pptx),
    ], cwd=root, capture_output=True, text=True, timeout=120, check=False)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    with zipfile.ZipFile(pptx) as archive:
        page_notes = {int(re.search(r"notesSlide(\d+)", name).group(1)):
                      "".join(ET.fromstring(archive.read(name)).itertext())
                      for name in archive.namelist()
                      if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)}
    assert all(fact in page_notes[1] for fact in expected)
    header = "整份演示的研究来源清单"
    assert {page: text.count(header) for page, text in page_notes.items()} == {1: 1, 2: 0, 3: 0}
    notes = "\n".join(page_notes.values())
    assert facts[2]["source_url"] not in notes and "EDITORIAL_ONLY" not in notes


def test_repeat_accept_and_prepare_preserve_sources_hash_budget_and_manual_html(case):
    root, _, facts = case
    before = prepare(root)
    brief = Path(before["request_file"]).read_bytes()
    outline_bytes = (root / "outline.json").read_bytes()
    check_bytes = (root / "qa/outline_check.json").read_bytes()
    for _ in range(2):
        assert output(run(root, "accept", "design_input.json"))["terminal"]
        assert sources(recovery(root)[1]) == [fact["canonical"] for fact in facts[:2]]
        assert (root / "outline.json").read_bytes() == outline_bytes
        assert (root / "qa/outline_check.json").read_bytes() == check_bytes
    for name in ("index.html", "fallback.html"):
        (root / name).write_text(f"<html>Manual {name}</html>")
    after = prepare(root)
    assert after["input_hash"] == before["input_hash"]
    assert after["request_created_at"] == before["request_created_at"]
    assert Path(after["request_file"]).read_bytes() == brief
    assert sources(recovery(root)[1]) == [fact["canonical"] for fact in facts[:2]]
    for name in ("index.html", "fallback.html"):
        assert (root / name).read_text() == f"<html>Manual {name}</html>"


@pytest.mark.parametrize("invalid", ["message", "source_mode", "handoff"])
def test_prevalidation_and_failed_validation_do_not_promote_raw_evidence(case, invalid):
    root, outline, _ = case
    if invalid == "message":
        outline["slides"][0]["message"] = ""
    elif invalid == "source_mode":
        outline["source_mode"] = 42
        outline["slides"][0]["message"] = ""
    else:
        (root / "research.json").write_text("invalid handoff")
    write(root / "outline.json", outline)
    for _ in range(2):
        output(run(root, "prepare", "outline.json", "--research-handoff", "research.json"))
        assert sources(recovery(root)[1]) == []
    assert not (root / "design_input.json").exists()


@pytest.mark.parametrize("boundary", ["no_handoff", "file_only", "strict"])
def test_unverified_or_source_limited_input_does_not_gain_public_research(case, monkeypatch, boundary):
    root, outline, facts = case
    if boundary == "file_only":
        outline["source_mode"] = "user_provided"
        write(root / "outline.json", outline)
    if boundary == "strict":
        text = "Only use the provided facts. Example Lab publishes a public guide and provides a checklist."
        monkeypatch.setenv("BOX_AGENT_SOURCE_TEXT_B64", base64.b64encode(text.encode()).decode())
    data = prepare(root, handoff=boundary != "no_handoff")
    assert sources(recovery(root)[1]) == []
    if boundary == "strict":
        # readInput supports later CLI calls using the hash-bound saved request.
        monkeypatch.delenv("BOX_AGENT_SOURCE_TEXT_B64")
    # Added metadata cannot confer authority that the original validation lacked.
    data["research_facts"] = [fact["canonical"] for fact in facts]
    data["research_handoff"] = str(root / "research.json")
    write(root / "design_input.json", data)
    output(run(root, "accept", "design_input.json"))
    assert sources(recovery(root)[1]) == []


def test_modified_metadata_and_extra_handoff_rows_cannot_expand_selected_sources(case):
    root, _, facts = case
    data = prepare(root)
    data["research_facts"] = [facts[2]["canonical"], "RAW_UNVERIFIED_INPUT"]
    write(root / "design_input.json", data)
    handoff = json.loads((root / "research.json").read_text())
    handoff["presentation_handoff"]["verified_facts"].append({
        **facts[2], "id": "fact-added", "canonical": "Example Lab | Added unselected claim | first_party | https://example.com/added",
    })
    write(root / "research.json", handoff)
    original = (root / "outline.json").read_bytes()
    output(run(root, "accept", "design_input.json"))
    assert sources(recovery(root)[1]) == [fact["canonical"] for fact in facts[:2]]
    assert (root / "outline.json").read_bytes() == original


@pytest.mark.parametrize("change", ["removed", "unverified", "invalid_json"])
def test_changed_handoff_cannot_reuse_stale_research_metadata(case, change):
    root, _, _ = case
    prepare(root)
    handoff_path = root / "research.json"
    handoff = json.loads(handoff_path.read_text())
    if change == "removed":
        handoff["presentation_handoff"]["verified_facts"].pop(0)
    elif change == "unverified":
        handoff["presentation_handoff"]["verified_facts"][0]["status"] = "unverified"
    if change == "invalid_json":
        handoff_path.write_text("invalid json")
    else:
        write(handoff_path, handoff)
    frozen = {name: (root / name).read_bytes() for name in
              ("outline.json", "design_input.json", "qa/outline_check.json")}
    report = output(run(root, "accept", "design_input.json"))
    assert report["status"] == "degraded" and Path(report["primary_artifact"]).exists()
    assert sources(recovery(root)[1]) == []
    assert any("research" in warning.lower() for warning in report["warnings"])
    assert all((root / name).read_bytes() == value for name, value in frozen.items())


def test_outline_hash_still_rejects_stale_recovery_input(case):
    root, outline, _ = case
    prepare(root)
    baseline = (root / "qa/design_delivery.json").read_bytes()
    outline["slides"][0]["message"] += " New unvalidated wording."
    write(root / "outline.json", outline)
    result = run(root, "accept", "design_input.json")
    assert result.returncode != 0 and "stale outline" in result.stderr
    assert (root / "qa/design_delivery.json").read_bytes() == baseline


def test_changed_canonical_behind_same_id_does_not_export_old_bound_fact(case):
    root, outline, facts = case
    for slide, fact in zip(outline["slides"], [facts[0], facts[1], facts[0]]):
        slide.update(evidence=[], evidence_ids=[fact["id"]])
    write(root / "outline.json", outline)
    prepare(root)
    frozen = {name: (root / name).read_bytes() for name in
              ("outline.json", "design_input.json", "qa/outline_check.json")}
    handoff = json.loads((root / "research.json").read_text())
    changed = handoff["presentation_handoff"]["verified_facts"][0]
    changed["claim"] = "Example Lab publishes a refreshed public guide."
    changed["canonical"] = f"Example Lab | {changed['claim']} | first_party | {changed['source_url']}"
    write(root / "research.json", handoff)
    report = output(run(root, "accept", "design_input.json"))
    assert sources(recovery(root)[1]) == []
    assert any("research" in warning.lower() for warning in report["warnings"])
    assert all((root / name).read_bytes() == value for name, value in frozen.items())


def test_research_contract_limit_does_not_destroy_the_recovery_artifact(case):
    root, outline, facts = case
    # The outline accepts more selected references than the existing deck bucket.
    selected = [{**facts[0], "id": f"fact-guide-{index}",
                 "source_url": f"https://example.com/guide/{index}",
                 "canonical": f"Example Lab | Public guide | first_party | https://example.com/guide/{index}"}
                for index in range(81)]
    handoff = json.loads((root / "research.json").read_text())
    handoff["presentation_handoff"]["verified_facts"] = selected
    write(root / "research.json", handoff)
    for index, slide in enumerate(outline["slides"]):
        slide["evidence"] = [fact["canonical"] for fact in selected[index * 27:(index + 1) * 27]]
    write(root / "outline.json", outline)
    prepare(root)
    assert json.loads((root / "qa/outline_check.json").read_text())["ok"]
    report, deck = recovery(root)
    assert Path(report["primary_artifact"]).exists() and sources(deck) == []
    assert any("research" in warning.lower() and "80" in warning for warning in report["warnings"])
