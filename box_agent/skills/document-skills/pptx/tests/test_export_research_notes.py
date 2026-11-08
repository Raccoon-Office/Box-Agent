"""Export current embedded research sources without inventing slide attribution."""

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest


EXPORTER = Path(os.environ.get(
    "BOX_AGENT_TEST_EXPORT_SCRIPT",
    Path(__file__).resolve().parents[1] / "scripts/html_to_editable_pptx.js",
))
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")
HEADER = "整份演示的研究来源清单（非逐页引用）"
ABSENT = object()
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


def _export(tmp_path, metadata=ABSENT, *, raw_metadata=None, pages=2, preload=None):
    if not NODE:
        pytest.skip("Node.js is required for HTML/PPTX export tests")
    source = tmp_path / "source"
    source.mkdir()
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir()
    # Both locations deliberately disagree with the actual embedded document.
    for parent in (source, cwd):
        for filename in ("outline.json", "deck.json"):
            (parent / filename).write_text(json.dumps({
                "truth_contract": {"research_facts": ["STALE_SIBLING_SOURCE"]},
                "slides": [{"notes": "STALE_SIBLING_PLANNING"}],
            }), encoding="utf-8")
    embedded = ""
    if metadata is not ABSENT or raw_metadata is not None:
        payload = raw_metadata if raw_metadata is not None else json.dumps(metadata)
        embedded = '<script type="application/json" id="deck-document">' + payload.replace("<", "\\u003c") + "</script>"
    html = source / "current.html"
    html.write_text(
        '<html><head><meta charset="utf-8"><style>'
        'html,body{margin:0}.slide{position:relative;width:1920px;height:1080px;'
        'font:36px sans-serif}</style></head><body>' + embedded
        + '<main id="deck-root">'
        + "".join(f'<section class="slide"><h1>正文 {i + 1}</h1></section>' for i in range(pages))
        + '</main></body></html>', encoding="utf-8",
    )
    pptx = tmp_path / "result.pptx"
    env = {
        **os.environ,
        "BOX_AGENT_WORKSPACE_DIR": str(tmp_path),
        "BOX_AGENT_HOME": str(tmp_path / ".profile"),
    }
    if preload:
        env["NODE_OPTIONS"] = f"--require={preload}"
    result = subprocess.run(
        [str(NODE), str(EXPORTER), str(html), str(pptx),
         "--out", str(tmp_path / "previews"), "--bg-capture", "never"],
        cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=90,
    )
    (tmp_path / "export.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "export.stderr.txt").write_text(result.stderr, encoding="utf-8")
    return result, pptx


def _notes_and_slides(result, pptx, pages=2):
    assert result.returncode == 0, result.stdout + result.stderr
    notes, slides = [], []
    with zipfile.ZipFile(pptx) as archive:
        for index in range(1, pages + 1):
            note = ET.fromstring(archive.read(f"ppt/notesSlides/notesSlide{index}.xml"))
            body = next(shape for shape in note.findall(".//p:sp", NS)
                        if shape.find("p:nvSpPr/p:nvPr/p:ph[@type='body']", NS) is not None)
            notes.append("".join(node.text or "" for node in body.findall(".//a:t", NS)))
            slide = ET.fromstring(archive.read(f"ppt/slides/slide{index}.xml"))
            slides.append("".join(node.text or "" for node in slide.findall(".//a:t", NS)))
    return notes, slides


def test_deck_research_sources_survive_once_without_sibling_or_private_notes(tmp_path):
    facts = [
        '松林工作台 | “样本”137项；52小时含11小时核对，12.6%→8.2% & <边界> | first_party | https://example.com/研究?a=1&b=%22x%22#片段',
        'Second source | 43.7元/季度起；2026/04/17\n换行与 emoji 🦝 | reputable_media | https://example.org/a?x=<2>&y="q"',
    ]
    result, pptx = _export(tmp_path, {
        "truth_contract": {"research_facts": facts, "source_facts": ["PRIVATE_SOURCE_FACT"]},
        "notes": "PRIVATE_DECK_PLANNING",
        "slides": [{"notes": "PRIVATE_SLIDE_PLANNING"}],
    }, pages=3)
    notes, slides = _notes_and_slides(result, pptx, pages=3)
    assert notes == [HEADER + "\n\n" + "\n\n".join(facts), "", ""]
    assert slides == ["正文 1", "正文 2", "正文 3"]
    assert "STALE_SIBLING" not in "".join(notes)
    assert "PRIVATE_" not in "".join(notes)


@pytest.mark.parametrize("metadata", [
    ABSENT, {}, {"truth_contract": {"research_facts": []}},
    {"truth_contract": {"source_facts": ["PRIVATE_SOURCE_FACT"]}, "notes": "PRIVATE_PLANNING"},
])
def test_html_without_research_keeps_empty_notes(tmp_path, metadata):
    result, pptx = _export(tmp_path, metadata)
    notes, slides = _notes_and_slides(result, pptx)
    assert notes == ["", ""]
    assert slides == ["正文 1", "正文 2"]
    assert "research" not in result.stderr.lower()


def test_invalid_metadata_is_warned_without_being_evaluated(tmp_path):
    result, pptx = _export(tmp_path, raw_metadata='({truth_contract: {research_facts: ["JS_EXPRESSION"]}})')
    notes, slides = _notes_and_slides(result, pptx)
    assert notes == ["", ""]
    assert slides == ["正文 1", "正文 2"]
    assert "invalid JSON" in result.stderr


@pytest.mark.parametrize("facts", [None, 7, "NOT_AN_ARRAY", {"claim": "NOT_AN_ARRAY"}])
def test_non_array_research_is_warned_and_not_coerced(tmp_path, facts):
    result, pptx = _export(tmp_path, {"truth_contract": {"research_facts": facts}})
    notes, _ = _notes_and_slides(result, pptx)
    assert notes == ["", ""]
    assert "research_facts must be an array" in result.stderr


def test_only_valid_strings_export_and_code_like_text_stays_inert(tmp_path):
    fact = '公司 | "; document.querySelector(".slide").textContent="UNSAFE_EXECUTED"; // & <tag> | first_party | https://example.com/?x=1&y=2'
    result, pptx = _export(tmp_path, {"truth_contract": {"research_facts": [
        fact, None, 42, False, {"claim": "NOT_A_STRING"}, ["NOT_A_STRING"], "  ",
        "INVALID_XML_\u0000", "INVALID_XML_\ud800",
    ]}})
    notes, slides = _notes_and_slides(result, pptx)
    assert notes == [HEADER + "\n\n" + fact, ""]
    assert slides == ["正文 1", "正文 2"]
    assert "Skipped 8 invalid research_facts entries" in result.stderr


def test_changed_bundle_fails_clearly_instead_of_losing_research(tmp_path):
    # Simulate vendor drift in memory; never modify the generated bundle file.
    preload = tmp_path / "simulate-bundle-drift.cjs"
    preload.write_text('''const fs = require("fs");
const original = fs.readFileSync;
fs.readFileSync = function(file, ...args) {
  const value = original.call(this, file, ...args);
  if (String(file).endsWith("dom-to-pptx.bundle.js") && typeof value === "string") {
    return value.replace("      const slide = pptx.addSlide();", "      const slide = pptx.addSlide({});");
  }
  return value;
};
''', encoding="utf-8")
    result, pptx = _export(tmp_path, {"truth_contract": {"research_facts": [
        "Company | Fact | first_party | https://example.com/",
    ]}}, preload=preload)
    assert result.returncode != 0
    assert "Unsupported dom-to-pptx notes exporter; cannot preserve research sources." in result.stderr
    assert not pptx.exists()
