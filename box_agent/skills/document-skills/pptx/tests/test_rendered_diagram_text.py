"""Detect lost diagram copy from the actual SVG without relying on model vision."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SKILL = Path(os.environ.get(
    "BOX_AGENT_TEST_DIAGRAM_SKILL", Path(__file__).resolve().parents[1],
))
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")


def run(script, *args):
    if not NODE:
        pytest.skip("Node.js is required for rendered diagram checks")
    result = subprocess.run(
        [str(NODE), str(SKILL / "scripts" / script), *map(str, args)],
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def quality(tmp_path, report):
    report_path = tmp_path / "observed.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    script = tmp_path / "quality.cjs"
    script.write_text(
        "const fs=require('fs');"
        "const q=require(process.argv[2]+'/scripts/render_quality.js');"
        "console.log(JSON.stringify(q.renderQuality(null,JSON.parse(fs.readFileSync(process.argv[3],'utf8')))));",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(NODE), str(script), str(SKILL), str(report_path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def observe(tmp_path, deck_path, deck):
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html = tmp_path / "index.html"
    run("render_deck_html.js", deck_path, "--out", html)
    return json.loads(run("probe_deck_runtime.js", html, "--viewport", "1440x900"))


def test_actual_svg_text_loss_identifies_affected_page_and_clears_after_content_repair(tmp_path):
    deck_path = tmp_path / "deck.json"
    run("inspect_deck_contract.js", "technical-diagram-v1", "technical-diagram-v1",
        "--theme", "technical-blueprint", "--no-images", "--out", deck_path)
    deck = json.loads(deck_path.read_text())
    for slide in deck["slides"]:
        slide["props"].update(
            diagram_kind="pipeline", direction="RIGHT",
            nodes=[
                {"id": "start", "label": "Input", "detail": "keep  spaces", "kind": "service"},
                {"id": "wait", "label": "Wait…", "detail": "cafe\u0301", "kind": "service"},
            ],
            edges=[{"id": "next", "source": "start", "target": "wait", "label": "next"}],
        )
    affected = deck["slides"][1]["props"]
    affected["nodes"][0].update(
        label="必须保留所有限定条件的节点名称" * 2,
        detail="人工检查通过之后才允许继续处理下一步" * 3,
    )
    affected["nodes"][1]["detail"] = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    affected["edges"][0]["label"] = "requires complete human approval"
    report = observe(tmp_path, deck_path, deck)
    result = quality(tmp_path, report)
    assert result["ok"] is False
    assert result["affected_pages"] == [2]
    assert {issue["kind"] for issue in result["issues"]} == {"truncated_diagram_text"}
    diagrams = report["editor"]["diagrams"]
    assert diagrams[0]["textLosses"] == []
    losses = diagrams[1]["textLosses"]
    assert {loss["field"] for loss in losses} == {"label", "detail", "edge label"}
    assert all(loss["expected"] != loss["rendered"] for loss in losses)

    # A bounded edit repairs content; no layout, font or QA threshold changes.
    affected["nodes"][0].update(label="先核验", detail="通过后继续")
    affected["nodes"][1]["detail"] = "review complete"
    affected["edges"][0]["label"] = "approved"
    repaired = observe(tmp_path, deck_path, deck)
    assert all(diagram["textLosses"] == [] for diagram in repaired["editor"]["diagrams"])
    assert quality(tmp_path, repaired)["ok"] is True


def test_unavailable_optional_inspection_and_older_reports_do_not_create_text_loss(tmp_path):
    if not NODE:
        pytest.skip("Node.js is required for rendered diagram checks")
    for report in (None, {"warnings": ["Optional vision unavailable"]},
                   {"editor": {"diagrams": [{"page": 1}]}}):
        assert quality(tmp_path, report) == {"ok": True, "issues": [], "affected_pages": []}
