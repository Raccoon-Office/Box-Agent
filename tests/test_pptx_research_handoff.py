"""Verified facts survive research-to-outline transfer without weakened checks."""

import base64
import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.test_research_synthesis import VALIDATOR, _write_focused_research


SCRIPTS = VALIDATOR.parents[2] / "document-skills/pptx/scripts"
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")


def _research(tmp_path, claim, excerpt):
    research = tmp_path / "research"
    _write_focused_research(research, evidence=[{
        "entity": "Example Corp", "claim": claim,
        "source_url": "https://example.com/news/launch", "source_type": "first_party",
        "evidence_excerpt": excerpt, "confidence": "high", "status": "verified",
    }])
    report = research / "qa/check.json"
    result = subprocess.run([
        sys.executable, str(VALIDATOR), "--research-dir", str(research),
        "--topic", "topic", "--route", "B", "--min-dimensions", "3",
        "--report", str(report),
    ], capture_output=True, text=True, timeout=30)
    return result, json.loads(report.read_text())


@pytest.mark.parametrize(("claim_value", "excerpt_value", "accepted"), [
    ("2026年8月25日", "2026/08/25", True),
    ("1500", "1,500.00", True),
    ("2026年8月25日", "2026/09/25", False),
    ("8%", "0.08", False),
])
def test_research_matches_equivalent_numbers_without_changing_their_value(
    tmp_path, claim_value, excerpt_value, accepted,
):
    result, report = _research(
        tmp_path, f"Example Corp reported {claim_value} for the launch.",
        f"Example Corp reported {excerpt_value} for the launch.",
    )
    facts = report["presentation_handoff"]["verified_facts"]
    assert bool(facts) is accepted, result.stdout + result.stderr
    if accepted:
        assert claim_value in facts[0]["claim"]
        assert "Example" in facts[0]["aliases"]
        assert facts[0]["id"]
    else:
        assert any("missing claim number" in issue for issue in report["issues"])


def test_research_fact_identity_survives_row_reordering(tmp_path):
    _, first = _research(
        tmp_path, "Example Corp supports 20 formats.",
        "Example Corp supports 20 formats.",
    )
    ledger_path = tmp_path / "research/topic_evidence.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["evidence"].insert(0, {
        **ledger["evidence"][0], "claim": "Example Corp supports private deployment.",
        "evidence_excerpt": "Example Corp supports private deployment.",
    })
    ledger_path.write_text(json.dumps(ledger))
    report_path = tmp_path / "second.json"
    subprocess.run([
        sys.executable, str(VALIDATOR), "--research-dir", str(ledger_path.parent),
        "--topic", "topic", "--route", "B", "--min-dimensions", "3",
        "--report", str(report_path),
    ], check=True, capture_output=True, text=True, timeout=30)
    second = json.loads(report_path.read_text())["presentation_handoff"]["verified_facts"]
    original = first["presentation_handoff"]["verified_facts"][0]
    assert second[1]["id"] == original["id"]
    assert second[0]["id"] != original["id"]


@pytest.mark.parametrize("price,accepted", [("1599", True), ("15990", False)])
def test_original_language_price_evidence_can_support_translated_page_without_value_drift(
    tmp_path, price, accepted,
):
    _, research = _research(
        tmp_path, f"Example Corp Team costs CNY {price} per user per year.",
        "Example Corp Team costs CNY 1,599 per user per year, billed annually.",
    )
    facts = research["presentation_handoff"]["verified_facts"]
    assert bool(facts) is accepted
    if not accepted:
        assert any("missing claim number" in issue for issue in research["issues"])
        return
    (tmp_path / "research.json").write_text(json.dumps(research))
    (tmp_path / "outline.json").write_text(json.dumps({
        "deck_goal": "介绍团队版采用条件", "audience": "企业买方",
        "source_mode": "public_authoritative_research", "storyline": "任务价值到采用条件",
        "slides": [{
            "page": 1, "title": "Example Team 订阅条件", "message": "Example 团队版按年付费。",
            "bullets": ["每位用户每年人民币 1599 元", "试点前确认席位与预算"],
            "layout": "comparison", "visual": "订阅条件对照表", "evidence": [],
            "evidence_ids": [facts[0]["id"]],
        }],
    }, ensure_ascii=False))
    result, outline_check = _check(tmp_path, "--repair-flow")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not outline_check.get("repair_required") and not outline_check.get("auto_degraded")
    assert json.loads((tmp_path / "outline.json").read_text())["slides"][0]["evidence"] == [facts[0]["canonical"]]


@pytest.mark.parametrize("title,warns", [
    ("匿名案例", False), ("未具名企业案例", False), ("Anonymous customer case study", False),
    ("Unnamed organization case", False),
    ("匿名案例：某企业", True), ("Anonymous case study: Example Corp", True),
    ("Invented Client Project", True), ("某知名公司的确定项目名", True),
])
def test_neutral_anonymous_case_heading_is_not_treated_as_a_customer_name(tmp_path, title, warns):
    if not NODE:
        pytest.skip("node is required")
    deck = {"truth_contract": {"mode": "source_bound", "source_facts": [], "research_facts": []},
            "slides": [{"id": "case", "layout_id": "project-case-study-v1",
                        "props": {"eyebrow": "客户案例", "title": title,
                                  "positioning": "已提升效率 30%"}}]}
    result = subprocess.run([
        NODE, "-e", "const truth=require(process.argv[1]); const deck=JSON.parse(process.argv[2]); console.log(JSON.stringify(truth.validateSourceBoundDeck(deck)));",
        str(SCRIPTS / "validate_deck_truth.js"), json.dumps(deck),
    ], capture_output=True, text=True, check=True, timeout=30)
    warnings = json.loads(result.stdout)["warnings"]
    assert any("project name is not source-backed" in warning for warning in warnings) is warns
    assert any("no source_facts or research_facts" in warning for warning in warnings)
    assert any("numeric claim" in warning and "30%" in warning for warning in warnings)
    assert any("performance/award/publication claim" in warning for warning in warnings)


@pytest.mark.parametrize("title", ["未具名机构案例", "Anonymous customer case study"])
def test_strict_source_cleanup_preserves_anonymous_heading_but_removes_unbacked_claims(
    monkeypatch, title,
):
    if not NODE:
        pytest.skip("node is required")
    monkeypatch.setenv("BOX_AGENT_SOURCE_TEXT_B64", base64.b64encode(
        "客户案例。只使用我提供的事实。".encode()).decode())
    deck = {"truth_contract": {"mode": "source_bound", "source_facts": ["客户案例"]},
            "slides": [{"id": "case", "layout_id": "project-case-study-v1",
                        "props": {"eyebrow": "客户案例", "title": title,
                                  "positioning": "已提升效率 30%"}}]}
    result = subprocess.run([
        NODE, "-e", "const truth=require(process.argv[1]); const deck=JSON.parse(process.argv[2]); console.log(JSON.stringify(truth.sanitizeStrictSourceDeck(deck)));",
        str(SCRIPTS / "validate_deck_truth.js"), json.dumps(deck),
    ], capture_output=True, text=True, check=True, timeout=30)
    cleaned = json.loads(result.stdout)["deck"]["slides"][0]["props"]
    assert cleaned["title"] == title
    assert cleaned["positioning"] == "待补充"


@pytest.fixture
def handoff_case(tmp_path):
    fact = {
        "id": "fact-launch", "entity": "Example Corp", "aliases": ["Example"],
        "claim": "Example Corp launched on 2026-08-25.", "source_type": "first_party",
        "source_url": "https://example.com/news/launch",
        "canonical": "Example Corp | Example Corp launched on 2026-08-25. | first_party | https://example.com/news/launch",
    }
    (tmp_path / "research.json").write_text(json.dumps({"presentation_handoff": {
        "schema_version": 1, "delivery_mode": "full", "verified_facts": [fact],
        "quality_summary": {"quality_ok": True},
    }}))
    outline = {
        "deck_goal": "Explain the product", "audience": "Customers",
        "source_mode": "public_authoritative_research", "storyline": "Problem to product to adoption",
        "slides": [{
            "page": 1, "title": "Example launch", "message": "Example became available in August.",
            "bullets": ["2026年8月25日：产品正式发布", "Start with a real task"],
            "layout": "timeline", "visual": "Timeline of product milestones",
            "evidence_ids": ["fact-launch"], "evidence": [], "notes": "",
        }],
    }
    (tmp_path / "outline.json").write_text(json.dumps(outline, ensure_ascii=False))
    return tmp_path, outline, fact


def _check(root, *extra):
    if not NODE:
        pytest.skip("node is required")
    result = subprocess.run([
        NODE, str(SCRIPTS / "validate_outline.js"), "outline.json", "--min-slides", "1",
        "--research-handoff", "research.json", "--report", "qa/check.json", *extra,
    ], cwd=root, capture_output=True, text=True, timeout=30)
    return result, json.loads(result.stdout)


def test_verified_ids_and_declared_alias_preserve_dates_without_degradation(handoff_case):
    root, original, fact = handoff_case
    for _ in range(2):
        result, report = _check(root, "--repair-flow")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not report.get("repair_required") and not report.get("auto_degraded")
        saved = json.loads((root / "outline.json").read_text())
        assert saved["slides"][0]["evidence"] == [fact["canonical"]]
        assert saved["slides"][0]["bullets"] == original["slides"][0]["bullets"]


def test_standalone_reference_check_does_not_rewrite_outline(handoff_case):
    root, _, _ = handoff_case
    before = (root / "outline.json").read_bytes()
    result, report = _check(root)
    assert result.returncode == 0 and report["ok"], result.stdout
    assert (root / "outline.json").read_bytes() == before


def test_prepare_passes_resolved_evidence_to_the_designer(handoff_case, monkeypatch):
    from tests.test_pptx_design_plan import run

    root, outline, fact = handoff_case
    monkeypatch.setenv("BOX_AGENT_HOME", str(root / ".profile"))
    outline["slides"] = [
        {**outline["slides"][0], "page": page,
         "title": f"Example {title}", "message": f"Example {message}"}
        for page, (title, message) in enumerate([
            ("launch", "made the desktop client available."),
            ("availability", "announced a dated product release."),
            ("adoption", "offers a starting point for evaluation."),
        ], 1)
    ]
    (root / "outline.json").write_text(json.dumps(outline))
    result = run("design_plan.js", "prepare", "outline.json",
                 "--research-handoff", "research.json", cwd=root)
    assert result.returncode == 0, result.stdout + result.stderr
    prepared = json.loads((root / "design_input.json").read_text())
    assert all(slide["evidence"] == [fact["canonical"]]
               for slide in prepared["outline"]["slides"])
    assert "暂无可验证公开数据" not in json.dumps(prepared, ensure_ascii=False)


def test_unknown_reference_is_reported_without_dropping_the_claim(handoff_case):
    root, outline, _ = handoff_case
    outline["slides"][0]["evidence_ids"] = ["missing-fact"]
    (root / "outline.json").write_text(json.dumps(outline))
    before = (root / "outline.json").read_bytes()
    for _ in range(2):
        result, report = _check(root, "--repair-flow")
        assert result.returncode == 1
        assert any("missing-fact" in issue for issue in report["issues"])
        assert not report.get("auto_degraded")
        assert (root / "outline.json").read_bytes() == before


def test_verified_reference_cannot_be_attached_to_a_different_entity(handoff_case):
    root, outline, _ = handoff_case
    outline["slides"][0].update(title="Other Corp launch", message="Other Corp became available.")
    (root / "outline.json").write_text(json.dumps(outline))
    result, report = _check(root)
    assert result.returncode == 1
    assert any("does not name that entity" in issue for issue in report["issues"])


def test_call_to_action_needs_no_fake_citation_but_numbers_still_need_evidence(handoff_case):
    root, outline, _ = handoff_case
    slide = outline["slides"][0]
    slide.update(title="Start with your next task", message="Try the workflow with your team.",
                 bullets=["Choose a real task", "Review the delivered result"],
                 layout="closing", visual="Call to action", evidence_ids=[])
    (root / "outline.json").write_text(json.dumps(outline))
    result, report = _check(root)
    assert result.returncode == 0 and report["ok"], result.stdout
    slide["bullets"] = ["Adopted by 9000 companies", "Review the delivered result"]
    (root / "outline.json").write_text(json.dumps(outline))
    _, report = _check(root, "--repair-flow")
    assert report["repair_required"]
    assert any(item.get("token") == "9000" for item in report["repairable"])
