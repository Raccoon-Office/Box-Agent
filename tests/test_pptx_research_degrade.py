"""Public-research outlines: strict once, then automatic degradation (never blocks)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "box_agent/skills/document-skills/pptx/scripts/validate_outline.js"
)
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


def _outline(source_mode="public_authoritative_research"):
    return {
        "deck_goal": "介绍内马尔",
        "audience": "球迷",
        "source_mode": source_mode,
        "storyline": "从桑托斯起步到世界舞台再回到桑托斯，用关键阶段理解他的职业生涯与影响",
        "slides": [
            {"page": 1, "title": "内马尔", "message": "巴西足球代表人物", "bullets": ["Neymar Jr."],
             "layout": "cover", "visual": "封面", "evidence": [], "notes": ""},
            {"page": 2, "title": "桑托斯起步", "message": "在桑托斯完成青训并成名",
             "bullets": ["2011年赢得解放者杯", "在桑托斯完成青训"], "layout": "timeline",
             "visual": "时间线", "evidence": ["解放者杯 | CONMEBOL | 官方资料"], "notes": ""},
            {"page": 3, "title": "国家队", "message": "巴西队核心",
             "bullets": ["2023年成为历史射手王 79 球"], "layout": "cards", "visual": "卡片",
             "evidence": ["进球纪录 | FIFA | https://www.fifa.com/x 2023年超越贝利"], "notes": ""},
            {"page": 4, "title": "总结", "message": "创造力的代表", "bullets": ["技术辨识度高", "影响力广"],
             "layout": "closing", "visual": "收束", "evidence": [], "notes": ""},
        ],
    }


def _run(tmp_path, *extra):
    result = subprocess.run(
        [NODE, str(SCRIPT), "outline.json", "--min-slides", "1",
         "--report", "qa/outline_check.json", "--repair-flow", *extra],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    return result.returncode, json.loads(result.stdout)


def _write(tmp_path, outline):
    (tmp_path / "qa").mkdir(exist_ok=True)
    (tmp_path / "outline.json").write_text(json.dumps(outline, ensure_ascii=False), encoding="utf8")


def test_first_run_asks_for_repair_without_blocking(tmp_path):
    _write(tmp_path, _outline())
    code, report = _run(tmp_path)
    assert code == 0 and report["ok"] is True
    assert report["repair_required"] is True and report["attempt"] == 1
    assert len(report["repairable"]) == 3
    # The outline itself is untouched on the first run.
    assert "2011年赢得解放者杯" in (tmp_path / "outline.json").read_text(encoding="utf8")


def test_second_run_degrades_and_keeps_a_backup(tmp_path):
    _write(tmp_path, _outline())
    _run(tmp_path)
    code, report = _run(tmp_path)
    assert code == 0 and report["ok"] is True and report["auto_degraded"] is True
    assert report["repairable"] == []
    actions = {item["action"] for item in report["degraded"]}
    assert {"dropped_evidence_without_url", "trimmed_unsupported_numbers",
            "replaced_bullets_with_placeholder"} <= actions
    slides = json.loads((tmp_path / "outline.json").read_text(encoding="utf8"))["slides"]
    # Gentle degrade: the unsupported year is cut, the qualitative claim stays.
    assert slides[1]["bullets"] == ["赢得解放者杯", "在桑托斯完成青训"] and slides[1]["evidence"] == []
    assert slides[2]["bullets"] == ["暂无可验证公开数据"]
    assert (tmp_path / "qa/outline.before_degrade.json").exists()
    code, third = _run(tmp_path)
    # The disclosure stays attached; no new repair round is opened.
    assert code == 0 and "repair_required" not in third and third["auto_degraded"] is True


def test_repair_fixes_everything_means_no_degrade(tmp_path):
    _write(tmp_path, _outline())
    _run(tmp_path)
    fixed = _outline()
    fixed["slides"][1]["evidence"] = ["解放者杯 | CONMEBOL | https://www.conmebol.com/x 2011年冠军"]
    fixed["slides"][2]["bullets"] = ["2023年成为历史射手王"]
    _write(tmp_path, fixed)
    code, report = _run(tmp_path)
    assert code == 0 and "repair_required" not in report and "auto_degraded" not in report


def test_user_provided_and_opt_out_are_untouched(tmp_path):
    _write(tmp_path, _outline("user_provided"))
    _, report = _run(tmp_path)
    assert "repair_required" not in report
    _write(tmp_path, _outline())
    _, report = _run(tmp_path, "--no-strict-research")
    assert "repair_required" not in report


def test_standalone_check_does_not_spend_the_repair_round(tmp_path):
    _write(tmp_path, _outline())
    _run(tmp_path)  # prepare-style run: repair_required, attempt 1
    standalone = subprocess.run(
        [NODE, str(SCRIPT), "outline.json", "--min-slides", "1", "--report", "qa/outline_check.json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    report = json.loads(standalone.stdout)
    assert report["repair_required"] is True and report["attempt"] == 1
    assert "auto_degraded" not in report
    assert "2011年赢得解放者杯" in (tmp_path / "outline.json").read_text(encoding="utf8")
    _, after = _run(tmp_path)  # the next prepare still degrades
    assert after["auto_degraded"] is True


def test_leftover_title_numbers_never_reopen_repair_after_degrade(tmp_path):
    outline = _outline()
    outline["slides"][3]["title"] = "总结 2026"  # a title number is reported, never rewritten
    _write(tmp_path, outline)
    _run(tmp_path)
    _, degraded = _run(tmp_path)
    assert degraded["auto_degraded"] is True
    for _ in range(2):
        code, again = _run(tmp_path)
        assert code == 0 and again["ok"] is True
        assert "repair_required" not in again and again["auto_degraded"] is True
        assert again["unverified_claims"] and again["degraded"]


def _numbers(value):
    import re
    return {str(float(n)).rstrip("0").rstrip(".") for n in re.findall(r"\d+(?:\.\d+)?", value)}


def test_repair_output_is_an_actionable_checklist_without_degrade_promises(tmp_path):
    _write(tmp_path, _outline())
    _, report = _run(tmp_path)
    instructions = report["repair_instructions"].lower()
    # Advertising the automatic fallback made the model skip the repair; describe the cost instead.
    for phrase in ("automatically", "never blocks", "degrade"):
        assert phrase not in instructions
    assert "cut from the slides" in instructions
    assert any("web_search" in step and "site:" in step for step in report["repair_how_to_fix"])
    checklist = {entry["slide"]: entry for entry in report["repair_checklist"]}
    assert set(checklist) == {"slide-02", "slide-03"}
    santos = checklist["slide-02"]
    assert santos["current_evidence"] == ["解放者杯 | CONMEBOL | 官方资料"]
    assert santos["claims"] == [{"path": "bullets.0", "text": "2011年赢得解放者杯", "unsupported_numbers": ["2011"]}]
    assert santos["evidence_without_url"] == [{"path": "evidence.0", "text": "解放者杯 | CONMEBOL | 官方资料"}]
    record = checklist["slide-03"]["claims"][0]
    assert record["unsupported_numbers"] == ["79"] and "evidence_without_url" not in checklist["slide-03"]


def test_gentle_degrade_never_keeps_an_unsupported_number(tmp_path):
    outline = _outline()
    evidence = "国家队纪录 | FIFA | https://www.fifa.com/y 第79球"
    outline["slides"][2]["evidence"] = [evidence]
    outline["slides"][2]["bullets"] = [
        "国家队进球达到79球，超越贝利的77球纪录",  # trims the unsupported clause
        "2016年里约奥运会夺得金牌",               # trims the leading date
        "参加2014、2018与2022三届世界杯",          # nothing meaningful left: removed
        "资料更新：2026年10月",                    # bare label left: removed
    ]
    _write(tmp_path, outline)
    _run(tmp_path)
    _, report = _run(tmp_path)
    assert report["ok"] is True and report["auto_degraded"] is True
    bullets = json.loads((tmp_path / "outline.json").read_text(encoding="utf8"))["slides"][2]["bullets"]
    assert bullets == ["国家队进球达到79球", "里约奥运会夺得金牌"]
    allowed = _numbers(evidence)
    for bullet in bullets:
        assert _numbers(bullet) <= allowed
    kept = {c["path"]: c.get("kept_as") for c in report["unverified_claims"] if c["slide"] == "slide-03"}
    assert kept == {"bullets.0": "国家队进球达到79球", "bullets.1": "里约奥运会夺得金牌",
                    "bullets.2": None, "bullets.3": None}
    actions = {(d["slide"], d["action"]): d.get("count") for d in report["degraded"]}
    assert actions[("slide-03", "trimmed_unsupported_numbers")] == 2
    assert actions[("slide-03", "removed_unsupported_bullets")] == 2


def test_standalone_check_shows_what_is_still_open(tmp_path):
    _write(tmp_path, _outline())
    _run(tmp_path)
    partly = _outline()
    partly["slides"][2]["bullets"] = ["2023年成为历史射手王"]
    _write(tmp_path, partly)
    standalone = subprocess.run(
        [NODE, str(SCRIPT), "outline.json", "--min-slides", "1", "--report", "qa/outline_check.json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    report = json.loads(standalone.stdout)
    assert report["repair_required"] is True and report["attempt"] == 1
    assert [entry["slide"] for entry in report["repair_checklist"]] == ["slide-02"]


def test_prepare_returns_checklist_then_always_proceeds(tmp_path, monkeypatch):
    from tests.pptx_test_support import skip_unavailable_pptx_runtime
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / ".profile"))
    design_plan = SCRIPT.parent / "design_plan.js"
    _write(tmp_path, _outline())

    def prepare():
        result = subprocess.run([NODE, str(design_plan), "prepare", "outline.json"],
                                cwd=tmp_path, capture_output=True, text=True, timeout=120)
        skip_unavailable_pptx_runtime(result)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    first = prepare()
    assert first["ok"] is False and first["repair_required"] is True
    assert first["checklist"] and first["how_to_fix"]
    wording = (first["instructions"] + " " + first["next"]).lower()
    for phrase in ("automatically", "never blocks", "degrade"):
        assert phrase not in wording
    assert not (tmp_path / "design_input.json").exists()
    second = prepare()
    assert second["ok"] is True and second["auto_degraded"] is True and second["unverified_claims"]
    assert (tmp_path / "design_input.json").exists()


def test_standalone_check_after_degrade_never_reopens_a_repair_round(tmp_path):
    _write(tmp_path, _outline())
    _run(tmp_path)
    _run(tmp_path)  # degraded
    _write(tmp_path, _outline())  # the model rewrites the unsourced claims back in
    standalone = subprocess.run(
        [NODE, str(SCRIPT), "outline.json", "--min-slides", "1", "--report", "qa/outline_check.json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert json.loads(standalone.stdout)["auto_degraded"] is True
    code, after = _run(tmp_path)
    assert code == 0 and after["ok"] is True
    assert "repair_required" not in after and after["auto_degraded"] is True


def _prepare(tmp_path):
    from tests.pptx_test_support import skip_unavailable_pptx_runtime
    result = subprocess.run([NODE, str(SCRIPT.parent / "design_plan.js"), "prepare", "outline.json"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=120)
    skip_unavailable_pptx_runtime(result)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _hard_issue_outline():
    outline = _outline()
    outline["slides"][3]["message"] = outline["slides"][3]["title"]  # hard issue: message duplicates title
    return outline


def test_hard_issues_get_one_repair_round_then_degrade(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / ".profile"))
    _write(tmp_path, _hard_issue_outline())
    code, first = _run(tmp_path)
    assert code == 1 and first["ok"] is False
    assert first["repair_required"] is True and first["repair_round"] == "hard_issues"
    assert first["repair_hard_issues"] and "auto_degraded" not in first
    assert "2011年赢得解放者杯" in (tmp_path / "outline.json").read_text(encoding="utf8")  # untouched
    code, second = _run(tmp_path)
    assert code == 1 and second["auto_degraded"] is True and second["degraded_with_hard_issues"] is True
    text = (tmp_path / "outline.json").read_text(encoding="utf8")
    assert "2011年赢得解放者杯" not in text and "79" not in text
    assert (tmp_path / "qa/outline.before_degrade.json").exists()


def test_prepare_with_hard_issue_is_non_terminal_then_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / ".profile"))
    _write(tmp_path, _hard_issue_outline())
    first = _prepare(tmp_path)
    assert first["ok"] is False and first["repair_required"] is True and first["issues"]
    assert not (tmp_path / "fallback.html").exists() or first.get("status") == "outline_needs_fixes"
    second = _prepare(tmp_path)  # unchanged outline: terminal fallback, never blocked
    assert second.get("terminal") is True and second.get("status") == "degraded"
    assert "79" not in (tmp_path / "outline.json").read_text(encoding="utf8")
    # The concrete disclosure travels with the terminal fallback result and its report.
    assert second["auto_degraded"] is True and second["degraded"] and second["unverified_claims"]
    delivery_report = json.loads((tmp_path / "qa/design_delivery.json").read_text(encoding="utf8"))
    assert delivery_report["auto_degraded"] is True and delivery_report["unverified_claims"]


def test_fixing_the_hard_issue_lets_prepare_continue(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / ".profile"))
    _write(tmp_path, _hard_issue_outline())
    assert _prepare(tmp_path)["repair_required"] is True
    fixed = _outline()
    fixed["slides"][1]["evidence"] = ["解放者杯 | CONMEBOL | https://www.conmebol.com/x 2011年冠军"]
    fixed["slides"][2]["bullets"] = ["2023年成为历史射手王"]
    _write(tmp_path, fixed)
    third = _prepare(tmp_path)
    assert third["ok"] is True and third.get("repair_required") is not True
    assert (tmp_path / "design_input.json").exists()


def _degrade(tmp_path, outline):
    _write(tmp_path, outline)
    _, first = _run(tmp_path)
    code, second = _run(tmp_path)
    return first, code, second, json.loads((tmp_path / "outline.json").read_text(encoding="utf8"))


def test_unsupported_numbers_are_stripped_from_title_and_message(tmp_path):
    outline = _outline()
    outline["slides"][3]["title"] = "总结 2026"
    outline["slides"][3]["message"] = "2026年的创造力代表"
    _, code, report, slides = _degrade(tmp_path, outline)
    assert code == 0 and report["auto_degraded"] is True
    last = slides["slides"][3]
    assert "2026" not in last["title"] and "2026" not in last["message"] and last["title"].strip()
    actions = {item["action"] for item in report["degraded"]}
    assert {"stripped_unsupported_numbers_from_title", "stripped_unsupported_numbers_from_message"} <= actions


def test_claims_added_after_a_degrade_are_degraded_too(tmp_path):
    _degrade(tmp_path, _outline())
    edited = json.loads((tmp_path / "outline.json").read_text(encoding="utf8"))
    edited["slides"][3]["bullets"] = ["技术辨识度高", "2030年再夺冠"]
    _write(tmp_path, edited)
    code, report = _run(tmp_path)
    assert code == 0 and report["auto_degraded"] is True and "repair_required" not in report
    assert "2030" not in (tmp_path / "outline.json").read_text(encoding="utf8")
    assert any(item.get("path") == "bullets.1" and item.get("slide") == "slide-04"
               for item in report["unverified_claims"])
    assert any(item["slide"] == "slide-02" for item in report["degraded"])  # first pass disclosure kept


def test_factual_slide_without_evidence_gets_a_repair_item(tmp_path):
    outline = _outline()
    outline["slides"][1]["evidence"] = []
    outline["slides"][1]["bullets"] = ["在桑托斯完成青训"]
    _write(tmp_path, outline)
    code, report = _run(tmp_path)
    assert code == 0 and report["repair_required"] is True
    entry = next(item for item in report["repair_checklist"] if item["slide"] == "slide-02")
    assert entry["missing_evidence"]
    assert all(item["slide"] != "slide-01" and item["slide"] != "slide-04"  # cover/closing are exempt
               for item in report["repair_checklist"] if item.get("missing_evidence"))


def test_discarded_evidence_leaves_qualitative_claims_disclosed(tmp_path):
    outline = _outline()
    outline["slides"][1]["bullets"] = ["在桑托斯完成青训"]
    outline["slides"][1]["evidence"] = ["青训资料 | Santos | 官方资料"]  # no URL, no numbers
    _, code, report, _ = _degrade(tmp_path, outline)
    assert code == 0 and report["auto_degraded"] is True
    assert any(item.get("path") == "evidence" and item.get("slide") == "slide-02"
               for item in report["unverified_claims"])


def test_degrade_never_ships_the_claim_when_other_contracts_block_removal(tmp_path):
    outline = _outline()
    outline["slides"][3] = {
        "page": 4, "title": "议程", "message": "本次分享三个部分", "layout": "agenda",
        "visual": "3 个议程项列表", "bullets": ["2011年桑托斯", "巴塞罗那", "国家队"],
        "evidence": [], "notes": "",
    }
    _, code, report, slides = _degrade(tmp_path, outline)
    text = json.dumps(slides, ensure_ascii=False)
    assert "2011" not in text  # the unsupported number never survives
    assert report["auto_degraded"] is True
    assert code == 0 or report.get("degraded_unvalidated") is True


def test_new_missing_evidence_after_a_degrade_updates_the_disclosure(tmp_path):
    _degrade(tmp_path, _outline())
    edited = json.loads((tmp_path / "outline.json").read_text(encoding="utf8"))
    edited["slides"][2]["evidence"] = []
    edited["slides"][2]["message"] = "国家队组织核心"
    edited["slides"][2]["bullets"] = ["巴西队的主要组织核心"]
    _write(tmp_path, edited)
    code, report = _run(tmp_path)
    assert code == 0 and report["auto_degraded"] is True and "repair_required" not in report
    assert any(item.get("slide") == "slide-03" and item.get("path") == "evidence"
               for item in report["unverified_claims"])
    assert any(item.get("slide") == "slide-02" for item in report["degraded"])  # earlier disclosure kept
    _, again = _run(tmp_path)  # repeated prepares are idempotent
    assert again["unverified_claims"] == report["unverified_claims"]
    assert again["degraded"] == report["degraded"]
