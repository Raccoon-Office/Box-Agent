"""Exercise reference diagnostics and bounded repair using neutral CLI fixtures."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
NODE = shutil.which("node")


@pytest.fixture
def case(tmp_path, monkeypatch):
    if not NODE:
        pytest.skip("node is required")
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / ".profile"))
    monkeypatch.setenv("BOX_AGENT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("BOX_AGENT_PPT_STRICT_RESEARCH", "1")
    monkeypatch.delenv("BOX_AGENT_SOURCE_TEXT_B64", raising=False)
    fact = {
        "id": "fact-capacity", "entity": "Example Corp",
        "claim": "Example Corp supports 20 files.", "source_type": "first_party",
        "source_url": "https://example.com/capacity",
        "canonical": "Example Corp | Example Corp supports 20 files. | first_party | https://example.com/capacity",
    }
    _write(tmp_path / "research.json", {"presentation_handoff": {
        "schema_version": 1, "delivery_mode": "full", "verified_facts": [fact],
        "quality_summary": {"quality_ok": True},
    }})
    return tmp_path


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _slide(title="Supported capacity", subject=False, reference="fact-capacity"):
    return {
        "page": 1, "title": title,
        "message": ("Example Corp " if subject else "The tool ") + "supports a defined workflow.",
        "bullets": ["Handle 20 files", "Use a scoped evaluation task"],
        "layout": "cards", "visual": "Capability cards",
        "evidence": [], "evidence_ids": [reference], "notes": "",
    }


def _outline(root, slides):
    for index, slide in enumerate(slides, 1):
        slide["page"] = index
    _write(root / "outline.json", {
        "deck_goal": "Explain a supported workflow", "audience": "Readers",
        "source_mode": "public_authoritative_research",
        "storyline": "Capabilities to evaluation to a bounded next decision",
        "slides": slides,
    })


def _edit(root, change):
    outline = json.loads((root / "outline.json").read_text())
    change(outline)
    _write(root / "outline.json", outline)


def _run(root, prepare=False, standalone=False):
    if prepare:
        command = [NODE, str(SCRIPTS / "design_plan.js"), "prepare", "outline.json",
                   "--research-handoff", "research.json", "--out", "design_input.json"]
    else:
        command = [NODE, str(SCRIPTS / "validate_outline.js"), "outline.json",
                   "--min-slides", "1", "--research-handoff", "research.json",
                   "--report", "qa/outline_check.json"]
        if not standalone:
            command.append("--repair-flow")
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
    assert result.stdout.strip(), result.stderr
    return result, json.loads(result.stdout)


def test_unknown_reference_does_not_hide_other_slides_or_mutate_input(case):
    _outline(case, [_slide(), _slide("Unresolved support", reference="unknown-fact")])
    original = (case / "outline.json").read_bytes()
    result, report = _run(case)
    assert result.returncode != 0
    assert (case / "outline.json").read_bytes() == original
    assert any("slide-01" in issue and "does not name" in issue for issue in report["issues"])
    assert not any("requires at least one exact" in issue for issue in report["issues"])
    assert not report["repairable"]


def test_unknown_only_page_gets_one_content_repair_after_its_reference_is_fixed(case):
    _outline(case, [_slide(reference="unknown-fact")])
    _, first = _run(case)
    assert first["repair_required"]
    _edit(case, lambda outline: outline["slides"][0].update(evidence_ids=["fact-capacity"]))
    _, content = _run(case)
    assert content.get("repair_required") is True
    assert any("does not name" in issue for issue in content["issues"])
    assert not content.get("auto_degraded")
    _, repeated = _run(case)
    assert not repeated.get("repair_required")
    assert repeated["ok"] is False


def test_repeating_an_unknown_reference_exhausts_only_its_bounded_repair(case):
    _outline(case, [_slide(reference="unknown-fact")])
    original = (case / "outline.json").read_bytes()
    assert _run(case)[1]["repair_required"]
    for _ in range(2):
        _, report = _run(case)
        assert not report.get("repair_required")
        assert report["ok"] is False
        assert (case / "outline.json").read_bytes() == original


def test_success_does_not_reopen_already_used_reference_or_content_repairs(case):
    _outline(case, [_slide(reference="unknown-fact")])
    assert _run(case)[1]["repair_required"]
    _edit(case, lambda outline: outline["slides"][0].update(evidence_ids=["fact-capacity"]))
    assert _run(case)[1].get("repair_required") is True
    _edit(case, lambda outline: outline["slides"][0].update(message="Example Corp supports a scoped workflow."))
    assert _run(case)[1]["ok"]

    _edit(case, lambda outline: outline["slides"][0].update(evidence_ids=["unknown-fact"]))
    assert not _run(case)[1].get("repair_required")
    _edit(case, lambda outline: outline["slides"][0].update(evidence_ids=["fact-capacity"]))
    assert _run(case)[1]["ok"]
    _edit(case, lambda outline: outline["slides"][0].update(message="The tool supports a scoped workflow."))
    assert not _run(case)[1].get("repair_required")


def test_standalone_check_does_not_spend_or_reset_reference_repair(case):
    _outline(case, [_slide(reference="unknown-fact")])
    assert _run(case)[1]["repair_required"]
    assert _run(case, standalone=True)[1]["repair_required"]
    assert not _run(case)[1].get("repair_required")
    _edit(case, lambda outline: outline["slides"][0].update(evidence_ids=["fact-capacity"]))
    assert _run(case)[1].get("repair_required") is True


def test_unrelated_entity_error_does_not_erase_supported_clause(case):
    slide = _slide()
    slide["bullets"] = ["Known capacity: 20 files; extra capacity: 999 files", "Keep human review"]
    _outline(case, [slide])
    assert _run(case)[1]["repair_required"]
    _, report = _run(case)
    assert not report["ok"] and report.get("degraded_unvalidated")
    saved = json.loads((case / "outline.json").read_text())
    bullets = saved["slides"][0]["bullets"]
    assert any("20 files" in item for item in bullets)
    assert "999" not in json.dumps(bullets)
    assert "暂无可验证公开数据" not in bullets


def test_placeholder_remains_available_for_a_real_item_count_constraint(case):
    slide = {
        "page": 1, "title": "议程", "message": "本次分享三个部分", "layout": "agenda",
        "visual": "3 个议程项列表", "bullets": ["999", "流程", "验收"],
        "evidence": [], "notes": "",
    }
    _outline(case, [slide])
    assert _run(case)[1]["repair_required"]
    _, report = _run(case)
    saved = json.loads((case / "outline.json").read_text())
    assert report["ok"] and report.get("auto_degraded")
    assert len(saved["slides"][0]["bullets"]) == 3
    assert "999" not in json.dumps(saved["slides"])


def test_prepare_exposes_reference_diagnostics_and_later_content_errors(case):
    slides = [_slide("Initial capacity", subject=True),
              _slide("Unresolved subject", reference="unknown-fact"),
              _slide("Evaluation scope", subject=True)]
    slides[2]["message"] = "Example Corp offers an evaluation starting point."
    _outline(case, slides)
    _, first = _run(case, prepare=True)
    assert first["repair_required"] and not first.get("terminal")
    _edit(case, lambda outline: outline["slides"][1].update(evidence_ids=["fact-capacity"]))
    _, second = _run(case, prepare=True)
    assert second.get("repair_required") is True and not second.get("terminal")
    assert any("does not name" in issue for issue in second["issues"])
    _, third = _run(case, prepare=True)
    assert third.get("terminal") is True
    assert any("does not name" in issue for issue in third.get("validation_issues", []))
