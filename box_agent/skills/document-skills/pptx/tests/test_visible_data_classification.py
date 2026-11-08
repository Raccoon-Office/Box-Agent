"""Hidden source context must not impose a data-display layout on visible copy."""

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


def _outline() -> dict:
    return {
        "deck_goal": "Explain milestones", "audience": "General readers",
        "source_mode": "public_authoritative_research", "storyline": "Earlier event to later event",
        "slides": [{
            "page": 1, "title": "项目里程碑", "message": "按时间回顾项目发展。",
            "bullets": ["2032年7月：首次发布", "2033年8月：版本更新"],
            "layout": "timeline", "visual": "两个节点的时间线", "notes": "",
            "evidence": ["2032年7月首次发布；2033年8月版本更新。 | History | https://example.org/history"],
        }],
    }


def _run(root: Path, outline: dict, *args: str) -> tuple[subprocess.CompletedProcess, dict]:
    path = root / "outline.json"
    path.write_text(json.dumps(outline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    before = path.read_bytes()
    result = subprocess.run([
        NODE, str(VALIDATOR), str(path), "--min-slides", "1", "--max-slides", "1",
        "--report", str(root / "check.json"), *args,
    ], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.stdout.strip(), result.stderr
    report = json.loads(result.stdout)
    assert report == json.loads((root / "check.json").read_text(encoding="utf-8"))
    if "--repair-flow" not in args:
        assert path.read_bytes() == before
    return result, report


@pytest.mark.parametrize("hidden_field", ["evidence", "notes", "visual"])
def test_hidden_metrics_do_not_reclassify_visible_timeline(case: Path, hidden_field: str) -> None:
    outline = _outline()
    first, first_report = _run(case, outline)
    assert first.returncode == 0 and first_report["ok"]
    slide = outline["slides"][0]
    hidden = "关联资料讨论收入增长，样本量为740项。"
    if hidden_field == "evidence":
        slide["evidence"].append(hidden + " | Appendix | https://example.org/appendix")
    elif hidden_field == "notes":
        slide["notes"] = hidden
    else:
        slide["visual"] = "两个节点的时间线，用于介绍市场背景"

    result, report = _run(case, outline)

    assert result.returncode == 0 and report["ok"], report
    assert report["issues"] == first_report["issues"] == []
    assert not report["repairable"]
    assert not any("data-heavy" in warning for warning in report["warnings"])


@pytest.mark.parametrize("field", ["title", "message", "bullets"])
def test_visible_metrics_still_require_data_display_even_on_timeline(case: Path, field: str) -> None:
    outline = _outline()
    slide = outline["slides"][0]
    claim = "收入从12万元增长到18万元"
    slide[field] = [claim, "比较同一口径。"] if field == "bullets" else claim
    slide["evidence"].append(claim + " | Report | https://example.org/report")

    result, report = _run(case, outline)

    assert result.returncode == 1 and not report["ok"]
    assert report["issues"] == [
        "slide-01: appears data-heavy but visual does not name a chart/table/KPI/dashboard data display"
    ]
    assert not report["repairable"]


def test_visible_metrics_accept_explicit_data_display(case: Path) -> None:
    outline = _outline()
    slide = outline["slides"][0]
    slide["message"] = "收入从12万元增长到18万元"
    slide["visual"] = "年度节点下使用收入对比柱状图"
    slide["evidence"].append(slide["message"] + " | Report | https://example.org/report")
    result, report = _run(case, outline)
    assert result.returncode == 0 and report["ok"] and not report["issues"]


@pytest.mark.parametrize(("claim", "source", "unsupported"), [
    ("2032年7月：首次发布", "2032年7月首次发布", []),
    ("2032年7月26日：首次发布", "2032年7月首次发布", ["26"]),
    ("2032年9月：首次发布", "2032年7月首次发布", ["9"]),
    ("2032.07：首次发布", "2032年7月首次发布", ["2032.07"]),
    ("2032.13：首次发布", "2032年7月首次发布", ["2032.13"]),
    ("投入2032.07万元", "投入2032.07万元", []),
    ("投入2032.07万元", "2032年7月首次发布", ["2032.07"]),
    ("增长率7.25%", "增长率7.2%", ["7.25%"]),
])
def test_date_and_decimal_support_is_not_waived(
    case: Path, claim: str, source: str, unsupported: list[str],
) -> None:
    outline = _outline()
    slide = outline["slides"][0]
    slide["bullets"] = [claim, "保留原文边界。"]
    slide["evidence"] = [source + " | Supplied source | https://example.org/source"]
    # Use a data display here to isolate factual support from layout classification.
    slide["visual"] = "带原文注释的对比表格"
    _, report = _run(case, outline)
    found = [item["token"] for item in report["repairable"] if item.get("path") == "bullets.0"]
    assert found == unsupported


def test_publication_context_is_not_imported_as_event_date(case: Path) -> None:
    outline = _outline()
    slide = outline["slides"][0]
    claim = "2032年7月26日：首次发布"
    canonical = "示例机构 | 2032年7月首次发布。 | first_party | https://example.org/release"
    slide["title"] = "示例机构项目里程碑"
    slide["bullets"] = [claim, "保留原文边界。"]
    slide["evidence"] = []
    slide["evidence_ids"] = ["release-fact"]
    handoff = {"presentation_handoff": {
        "schema_version": 1, "delivery_mode": "full", "quality_summary": {"quality_ok": True},
        "verified_facts": [{
            "id": "release-fact", "entity": "示例机构", "claim": "2032年7月首次发布。",
            "source_url": "https://example.org/release", "canonical": canonical,
            "evidence_context": ["Published 2032/07/26"],
        }],
    }}
    (case / "research.json").write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")

    _, report = _run(case, outline, "--research-handoff", str(case / "research.json"))

    assert {item.get("token") for item in report["repairable"]} == {"26"}


def test_unsupported_metric_keeps_existing_single_repair_then_degrade(case: Path) -> None:
    outline = _outline()
    slide = outline["slides"][0]
    slide["bullets"] = ["收入增长到42万元", "保留原文边界。"]
    slide["visual"] = "收入柱状图"
    _, first = _run(case, outline, "--repair-flow")
    assert first["repair_required"] and first["content_repair_used"]
    assert first["repair_round"] == "content"
    assert any(item.get("token") == "42" for item in first["repairable"])

    _, second = _run(case, outline, "--repair-flow")
    assert second["auto_degraded"] and second["content_repair_used"]
    assert not second.get("repair_required")
