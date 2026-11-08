"""Surface strong wording for review without deciding factual entailment."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


VALIDATOR = Path(os.environ.get(
    "BOX_AGENT_TEST_OUTLINE_VALIDATOR",
    Path(__file__).resolve().parents[1] / "scripts/validate_outline.js",
))
NODE = shutil.which("node")


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if not NODE:
        pytest.skip("node is required")
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / ".profile"))
    monkeypatch.setenv("BOX_AGENT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("BOX_AGENT_PPT_STRICT_RESEARCH", "1")
    monkeypatch.delenv("BOX_AGENT_SOURCE_TEXT_B64", raising=False)
    return tmp_path


def _outline(message: str, evidence: str = "") -> dict:
    return {
        "deck_goal": "Explain a supplied rule", "audience": "General readers",
        "source_mode": "user_provided", "storyline": "Rule, boundary and application",
        "slides": [{
            "page": 1, "title": "规则与边界", "message": message,
            "bullets": ["核对原文的适用范围", "保留例外与不确定性"],
            "layout": "cards", "visual": "Plain text cards", "notes": "",
            "evidence": [evidence] if evidence else [],
        }],
    }


def _run(root: Path, outline: dict, *args: str) -> tuple[subprocess.CompletedProcess, dict]:
    path = root / "outline.json"
    path.write_text(json.dumps(outline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    before = path.read_bytes()
    result = subprocess.run([
        NODE, str(VALIDATOR), str(path), "--min-slides", "1", "--max-slides", "40",
        "--report", str(root / "check.json"), *args,
    ], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.stdout.strip(), result.stderr
    report = json.loads(result.stdout)
    assert report == json.loads((root / "check.json").read_text(encoding="utf-8"))
    if "--repair-flow" not in args:
        assert path.read_bytes() == before
    return result, report


def test_summary_risk_keeps_exact_page_claim_and_evidence(case: Path) -> None:
    claim = "湿度持续升高且通风不恢复，才可能出现表面凝露。"
    source = "If humidity remains high, surface condensation may occur. | Manual | https://example.org/guide"
    outline = _outline("先说明适用范围。", source)
    closing = _outline(claim, source)["slides"][0]
    closing.update(page=2, title="总结", layout="closing", visual="Summary statement")
    outline["slides"].append(closing)

    result, report = _run(case, outline)

    assert result.returncode == 0 and report["ok"]
    risk = next(item for item in report["claim_strength_review"] if item["path"] == "slides.1.message")
    assert risk["page"] == 2 and risk["text"] == claim
    assert risk["signals"] == ["necessity_or_exclusivity"]
    assert risk["evidence"] == [source]
    assert not report["issues"] and not report["repairable"]
    assert not report.get("repair_required") and not report.get("auto_degraded")


@pytest.mark.parametrize(("claim", "signal"), [
    ("只有持证人员可以进入档案室。", "necessity_or_exclusivity"),
    ("提交申请是使用设备的必要条件。", "necessity_or_exclusivity"),
    ("操作者必须佩戴手套。", "necessity_or_exclusivity"),
    ("Access is permitted only if a supervisor is present.", "necessity_or_exclusivity"),
    ("A valid pass is required for entry.", "necessity_or_exclusivity"),
    ("只要按下按钮，指示灯就会亮起。", "sufficiency"),
    ("一次校验足以完成初始化。", "sufficiency"),
    ("A valid key is sufficient to unlock the cabinet.", "sufficiency"),
    ("服务无条件保证按时送达。", "certainty_or_unconditional"),
    ("This procedure always succeeds.", "certainty_or_unconditional"),
    ("The service guarantees delivery.", "certainty_or_unconditional"),
    ("The outcome is inevitable.", "certainty_or_unconditional"),
])
def test_strong_wording_is_review_input_not_an_automatic_error(case: Path, claim: str, signal: str) -> None:
    # Legitimate requirements/promises remain valid when the supplied source says so.
    result, report = _run(case, _outline(claim, f"{claim} | Supplied manual"))
    assert result.returncode == 0 and report["ok"]
    risks = [item for item in report["claim_strength_review"] if item["path"] == "slides.0.message"]
    assert len(risks) == 1 and signal in risks[0]["signals"]
    assert not report["issues"] and not report["repairable"]


@pytest.mark.parametrize(("claim", "source"), [
    ("只有验票后才可入场。", "Admission is permitted only after ticket validation. | Supplied rule"),
    ("Admission is permitted only after ticket validation.", "只有验票后才可入场。 | 所给规则"),
])
def test_equivalent_translation_is_not_rejected_for_missing_words(case: Path, claim: str, source: str) -> None:
    result, report = _run(case, _outline(claim, source))
    assert result.returncode == 0 and report["ok"] and not report["issues"]
    assert report["claim_strength_review"][0]["evidence"] == [source]


@pytest.mark.parametrize("claim", [
    "如果室内湿度持续升高，表面可能出现凝露。",
    "If the room remains humid, condensation may occur.",
    "When the switch is pressed, the lamp may flash.",
    "A mustard-colored cover marks the manual.",
])
def test_ordinary_conditional_risk_is_not_labeled_as_necessity(case: Path, claim: str) -> None:
    result, report = _run(case, _outline(claim, f"{claim} | Supplied note"))
    assert result.returncode == 0 and report["ok"]
    assert report["claim_strength_review"] == []


@pytest.mark.parametrize("field", ["title", "message", "bullets", "visual"])
def test_each_reviewed_copy_field_is_located_without_scanning_private_notes(case: Path, field: str) -> None:
    outline = _outline("介绍适用规则。", "只有持证人员可以操作。 | 所给规则")
    claim = "只有持证人员可以操作。"
    slide = outline["slides"][0]
    slide[field] = [claim, "核对例外。"] if field == "bullets" else claim
    slide["notes"] = "Planning note: always use a dark background."
    result, report = _run(case, outline)
    assert result.returncode == 0 and report["ok"]
    assert len(report["claim_strength_review"]) == 1
    risk = report["claim_strength_review"][0]
    assert risk["path"] == f"slides.0.{field}" + (".0" if field == "bullets" else "")
    assert risk["text"] == claim


def test_resolved_evidence_ids_are_available_in_review_input(case: Path) -> None:
    claim = "北亭档案要求必须先验票。"
    canonical = f"北亭档案 | {claim} | user_input | file:/provided-rule.txt"
    handoff = {"presentation_handoff": {
        "schema_version": 1, "delivery_mode": "full", "quality_summary": {"quality_ok": True},
        "verified_facts": [{"id": "entry-rule", "entity": "北亭档案", "claim": claim,
                            "source_url": "file:/provided-rule.txt", "canonical": canonical}],
    }}
    (case / "research.json").write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")
    outline = _outline(claim)
    outline["slides"][0]["evidence_ids"] = ["entry-rule"]

    result, report = _run(case, outline, "--research-handoff", str(case / "research.json"))

    assert result.returncode == 0 and report["ok"]
    assert report["claim_strength_review"][0]["evidence"] == [canonical]


def test_clean_risk_does_not_open_or_reset_saved_repair_budget(case: Path) -> None:
    prior = {"reference_repair_used": True, "content_repair_used": True}
    (case / "check.json").write_text(json.dumps(prior), encoding="utf-8")
    outline = _outline("操作者必须佩戴手套。", "操作者必须佩戴手套。 | 所给规范")
    result, report = _run(case, outline, "--repair-flow")
    assert result.returncode == 0 and report["ok"]
    assert report["reference_repair_used"] and report["content_repair_used"]
    assert not report.get("repair_required") and not report.get("auto_degraded")
    assert json.loads((case / "outline.json").read_text(encoding="utf-8")) == outline


def test_existing_numeric_repair_is_unchanged(case: Path) -> None:
    outline = _outline("该装置保证完成29次循环。", "该装置可进行循环测试。 | Manual | https://example.org/guide")
    outline["source_mode"] = "public_authoritative_research"
    result, report = _run(case, outline, "--repair-flow")
    # Existing repair-flow distinguishes structural success from a pending repair.
    assert result.returncode == 0 and report["ok"]
    assert report["repair_required"] and report["repair_round"] == "content"
    assert report["repairable"] == [{"kind": "number", "slide": 0, "path": "message", "token": "29"}]
    assert not report.get("auto_degraded")
    assert json.loads((case / "outline.json").read_text(encoding="utf-8")) == outline

    _, second = _run(case, outline, "--repair-flow")
    assert second["auto_degraded"] and not second.get("repair_required")
    assert second["content_repair_used"]
    current = json.loads((case / "outline.json").read_text(encoding="utf-8"))
    assert second["claim_strength_review"][0]["text"] == current["slides"][0]["message"]
    assert "29" not in second["claim_strength_review"][0]["text"]
