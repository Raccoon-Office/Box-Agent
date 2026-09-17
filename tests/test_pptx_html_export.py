from __future__ import annotations

import io
import json
import os
import posixpath
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from tests.pptx_test_support import skip_unavailable_pptx_runtime


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "box_agent"
    / "skills"
    / "document-skills"
    / "pptx"
    / "scripts"
)
EXPORT_SCRIPT_PATH = SCRIPTS_DIR / "html_to_editable_pptx.js"
SELF_CHECK_SCRIPT_PATH = SCRIPTS_DIR / "html_self_check.js"
INSPECT_SCRIPT_PATH = SCRIPTS_DIR / "inspect_deck_contract.js"
RENDER_SCRIPT_PATH = SCRIPTS_DIR / "render_deck_html.js"
PROBE_SCRIPT_PATH = SCRIPTS_DIR / "probe_deck_runtime.js"
FINALIZE_SCRIPT_PATH = SCRIPTS_DIR / "finalize_controlled_deck.js"
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")


def _run_node(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    if NODE is None:
        pytest.skip("Node.js is required for HTML/PPTX export tests")
    result = subprocess.run(
        [str(NODE), str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    skip_unavailable_pptx_runtime(result)
    return result


def test_run_node_skips_when_managed_playwright_dependency_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(globals(), "NODE", "node")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="Missing dependency: playwright\n",
        ),
    )

    with pytest.raises(pytest.skip.Exception, match="Managed Playwright browser"):
        _run_node(Path("unused.js"))


def _last_json_object(output: str) -> dict:
    start = output.rfind("\n{")
    payload = output[start + 1 :] if start >= 0 else output
    return json.loads(payload)


@pytest.mark.parametrize("require_pptx", [False, True])
def test_finalizer_delivers_the_requested_format(tmp_path: Path, require_pptx: bool) -> None:
    deck = json.loads((SCRIPTS_DIR.parent / "examples/controlled-deck/deck.json").read_text())
    deck["slides"] = deck["slides"][:2]
    deck_path = tmp_path / "deck.json"
    deck_path.write_text(json.dumps(deck), encoding="utf-8")
    html_path = tmp_path / "index.html"
    result = _run_node(
        FINALIZE_SCRIPT_PATH, str(deck_path), "--out", str(html_path),
        *(["--require-pptx"] if require_pptx else []),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert html_path.is_file()
    pptx_path = html_path.with_suffix(".pptx")
    receipt = json.loads(result.stdout.splitlines()[-1])
    if require_pptx:
        assert pptx_path.is_file(), receipt
        assert receipt["pptx"] == str(pptx_path)
        with zipfile.ZipFile(pptx_path) as package:
            assert package.testzip() is None
            presentation = ET.fromstring(package.read("ppt/presentation.xml"))
            ns = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
            assert len(presentation.findall("p:sldIdLst/p:sldId", ns)) == 2
    else:
        assert not pptx_path.exists()


@pytest.mark.parametrize("export_failure", ["exit", "missing", "invalid"])
def test_finalizer_preserves_html_and_previous_pptx_when_export_fails(
    tmp_path: Path, export_failure: str,
) -> None:
    deck = json.loads((SCRIPTS_DIR.parent / "examples/controlled-deck/deck.json").read_text())
    deck["slides"] = deck["slides"][:1]
    deck_path = tmp_path / "deck.json"
    deck_path.write_text(json.dumps(deck), encoding="utf-8")
    html_path = tmp_path / "index.html"
    pptx_path = tmp_path / "requested.pptx"
    pptx_path.write_bytes(b"previous delivery must survive a failed export")
    preload = tmp_path / "export-failure.cjs"
    preload.write_text("""
const cp = require('child_process');
const run = cp.spawnSync;
cp.spawnSync = (command, args, options) => {
  if (require('path').basename(args[0]) === 'html_to_editable_pptx.js') {
    if (process.env.TEST_EXPORT_FAILURE === 'invalid') require('fs').writeFileSync(args[2], 'not a pptx');
    return {status: process.env.TEST_EXPORT_FAILURE === 'exit' ? 7 : 0,
            stdout: '', stderr: 'simulated export failure'};
  }
  return run(command, args, options);
};
""", encoding="utf-8")
    if NODE is None:
        pytest.skip("Node.js is required for HTML/PPTX export tests")
    result = subprocess.run(
        [str(NODE), str(FINALIZE_SCRIPT_PATH), str(deck_path), "--out", str(html_path),
         "--require-pptx", "--pptx", str(pptx_path)],
        capture_output=True, text=True,
        env={**os.environ, "NODE_OPTIONS": f"--require={preload}", "TEST_EXPORT_FAILURE": export_failure},
    )
    skip_unavailable_pptx_runtime(result)
    assert result.returncode != 0
    assert html_path.is_file(), result.stdout + result.stderr
    assert pptx_path.read_bytes() == b"previous delivery must survive a failed export"
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt["ok"] is False
    assert receipt["delivery_status"] == "partial"
    assert receipt["pptx"] is None
    assert receipt["blocking_issues"]
    assert not list(tmp_path.glob(".pptx-export-*"))


@pytest.mark.parametrize("target", ["deck.json", "index.html"])
def test_finalizer_rejects_export_overwriting_its_inputs(tmp_path: Path, target: str) -> None:
    deck_path = tmp_path / "deck.json"
    html_path = tmp_path / "index.html"
    deck_path.write_text('{"slides": []}', encoding="utf-8")
    html_path.write_text("existing HTML", encoding="utf-8")
    result = _run_node(FINALIZE_SCRIPT_PATH, str(deck_path), "--out", str(html_path),
                       "--require-pptx", "--pptx", str(tmp_path / target))
    assert result.returncode != 0
    assert "PPTX output must differ" in result.stderr
    assert deck_path.read_text() == '{"slides": []}'
    assert html_path.read_text() == "existing HTML"


def _diagram_html(*, marked: bool) -> str:
    svg = """<svg viewBox="0 0 1120 580" xmlns="http://www.w3.org/2000/svg">
        <rect x="40" y="40" width="1040" height="500" rx="24" fill="#f43f5e"/>
        <text x="560" y="300" text-anchor="middle" dominant-baseline="middle" fill="#ffffff" font-size="48">VECTOR_DIAGRAM_SENTINEL</text>
      </svg>"""
    diagram_markup = (
        '<div class="diagram" data-pptx-diagram '
        'data-diagram-spec-src="assets/diagrams/slide-01.json">'
        f"{svg}</div>"
        if marked
        else svg.replace("<svg ", '<svg class="diagram" ', 1)
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    html, body {{ margin: 0; padding: 0; }}
    .slide {{ width: 1920px; height: 1080px; position: relative; overflow: hidden; background: #ffffff; }}
    .diagram {{ position: absolute; left: 400px; top: 250px; width: 1120px; height: 580px; }}
    .diagram > svg {{ display: block; width: 1120px; height: 580px; }}
  </style>
</head>
<body>
  <section class="slide">
    {diagram_markup}
  </section>
</body>
</html>
"""


def _export_fixture(tmp_path: Path, *, marked: bool) -> tuple[dict, Path]:
    case_dir = tmp_path / ("marked" if marked else "unmarked")
    diagrams_dir = case_dir / "assets" / "diagrams"
    diagrams_dir.mkdir(parents=True)
    (diagrams_dir / "slide-01.json").write_text(
        json.dumps(
            {
                "version": 1,
                "type": "architecture",
                "nodes": [{"id": "service", "label": "Service"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    html_path = case_dir / "deck.html"
    html_path.write_text(_diagram_html(marked=marked), encoding="utf-8")
    pptx_path = case_dir / "deck.pptx"
    result = _run_node(
        EXPORT_SCRIPT_PATH,
        str(html_path),
        str(pptx_path),
        "--out",
        str(case_dir / "slides"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _last_json_object(result.stdout), pptx_path


def test_export_keeps_page_previews_without_publishing_them(tmp_path: Path) -> None:
    from box_agent.tools.engine.artifact_results import (
        _detect_tool_artifacts, _snapshot_workspace_signatures,
    )

    result, pptx = _export_fixture(tmp_path, marked=False)
    preview = pptx.parent / "slides/slide-01.png"
    assert preview.is_file()
    assert result["slideCount"] == 1
    events = _detect_tool_artifacts(
        "export", "bash", f"[{preview.relative_to(tmp_path).as_posix()}]", None, {},
        _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    published = {event.abs_path for event in events}
    assert str(pptx) in published
    assert str(preview) not in published


def _slide_picture_targets(
    archive: zipfile.ZipFile,
) -> tuple[list[str], list[str]]:
    presentation_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    office_rel_ns = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    )
    svg_ns = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    slide = ET.fromstring(archive.read("ppt/slides/slide1.xml"))
    rels = ET.fromstring(archive.read("ppt/slides/_rels/slide1.xml.rels"))
    rel_targets = {
        rel.attrib["Id"]: posixpath.normpath(
            posixpath.join("ppt/slides", rel.attrib["Target"])
        )
        for rel in rels.findall(f"{{{package_rel_ns}}}Relationship")
    }
    background_targets: list[str] = []
    vector_targets: list[str] = []
    for picture in slide.findall(f".//{{{presentation_ns}}}pic"):
        blip = picture.find(f".//{{{drawing_ns}}}blip")
        if blip is None:
            continue
        svg_blip = picture.find(f".//{{{svg_ns}}}svgBlip")
        if svg_blip is None:
            fallback_id = blip.attrib.get(f"{{{office_rel_ns}}}embed")
            if fallback_id:
                background_targets.append(rel_targets[fallback_id])
            continue
        svg_id = svg_blip.attrib[f"{{{office_rel_ns}}}embed"]
        vector_targets.append(rel_targets[svg_id])
    return background_targets, vector_targets


def _ordered_raster_picture_targets(archive: zipfile.ZipFile) -> list[str]:
    presentation_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    office_rel_ns = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    )
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    slide = ET.fromstring(archive.read("ppt/slides/slide1.xml"))
    rels = ET.fromstring(archive.read("ppt/slides/_rels/slide1.xml.rels"))
    rel_targets = {
        rel.attrib["Id"]: posixpath.normpath(
            posixpath.join("ppt/slides", rel.attrib["Target"])
        )
        for rel in rels.findall(f"{{{package_rel_ns}}}Relationship")
    }
    targets = []
    for picture in slide.findall(f".//{{{presentation_ns}}}pic"):
        blip = picture.find(f".//{{{drawing_ns}}}blip")
        if blip is None:
            continue
        relationship_id = blip.attrib.get(f"{{{office_rel_ns}}}embed")
        if relationship_id:
            targets.append(rel_targets[relationship_id])
    return targets


def test_source_previews_are_captured_before_export_dom_is_flattened() -> None:
    source = EXPORT_SCRIPT_PATH.read_text(encoding="utf-8")

    preview_capture = source.index(
        "await slideHandles[i].screenshot({ path: imagePath });"
    )
    background_flatten = source.index("await applyDecorationFlatten({")

    assert preview_capture < background_flatten


def test_background_capture_exports_below_authored_full_slide_image(
    tmp_path: Path,
) -> None:
    case_dir = tmp_path / "background-layer-order"
    case_dir.mkdir()
    hero_path = case_dir / "hero.png"
    Image.new("RGB", (64, 64), (220, 30, 40)).save(hero_path)
    html_path = case_dir / "deck.html"
    html_path.write_text(
        """<!doctype html><html><head><meta charset="utf-8"><style>
        html,body{margin:0;padding:0}
        .slide{width:1920px;height:1080px;position:relative;overflow:hidden;background:#fff}
        .slide>.slide-background{position:absolute;inset:0;z-index:0}
        .slide>:not(.slide-background){z-index:1}
        .slide-background img{display:block;width:100%;height:100%}
        </style></head><body><section class="slide">
        <div class="slide-background"><img src="hero.png" alt="red hero"></div>
        </section></body></html>""",
        encoding="utf-8",
    )
    pptx_path = case_dir / "deck.pptx"
    result = _run_node(
        EXPORT_SCRIPT_PATH,
        str(html_path),
        str(pptx_path),
        "--out",
        str(case_dir / "slides"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(pptx_path) as archive:
        targets = _ordered_raster_picture_targets(archive)
        assert len(targets) == 2
        bottom = Image.open(io.BytesIO(archive.read(targets[0]))).convert("RGB")
        top = Image.open(io.BytesIO(archive.read(targets[1]))).convert("RGB")
        bottom_center = bottom.getpixel((bottom.width // 2, bottom.height // 2))
        top_center = top.getpixel((top.width // 2, top.height // 2))
        assert all(channel >= 245 for channel in bottom_center)
        assert top_center[0] > 180 and top_center[1] < 80 and top_center[2] < 90


@pytest.mark.parametrize("legacy_flag", [[], ["--allow-self-check-issues"]])
def test_editable_export_does_not_run_html_self_check(
    tmp_path: Path, legacy_flag: list[str]
) -> None:
    case_dir = tmp_path / "export-with-advisory-layout-overflow"
    case_dir.mkdir()
    html_path = case_dir / "deck.html"
    html_path.write_text(
        """<!doctype html><html><head><meta charset="utf-8"><style>
        html,body{margin:0;padding:0}
        .slide{width:1920px;height:1080px;position:relative;overflow:hidden;background:#fff}
        .layout{position:absolute;left:100px;top:100px;width:400px;height:1100px}
        .card{width:400px;height:100px;background:#16a34a;color:#fff}
        </style></head><body><section class="slide">
        <div class="layout"><div class="card">Content remains inside the slide</div></div>
        </section></body></html>""",
        encoding="utf-8",
    )
    report_path = case_dir / "qa" / "html_self_check.json"

    checked = _run_node(
        SELF_CHECK_SCRIPT_PATH,
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )
    assert checked.returncode == 1
    assert "visible content extends outside the slide bounds" in checked.stdout

    pptx_path = case_dir / "deck.pptx"
    report_path.unlink()
    exported = _run_node(
        EXPORT_SCRIPT_PATH,
        str(html_path),
        str(pptx_path),
        "--out",
        str(case_dir / "slides"),
        "--bg-capture",
        "never",
        *legacy_flag,
    )

    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert pptx_path.exists()
    assert not report_path.exists()
    assert "htmlSelfCheck" not in _last_json_object(exported.stdout)


@pytest.mark.parametrize("mismatched_slide", [0, 1])
def test_editable_export_warns_without_blocking_mismatched_slide_sizes(
    tmp_path: Path, mismatched_slide: int
) -> None:
    slides = [
        '<section class="slide">First slide</section>',
        '<section class="slide">Second slide</section>',
    ]
    slides[mismatched_slide] = slides[mismatched_slide].replace(
        'class="slide"', 'class="slide" style="width:1000px;height:1000px"'
    )
    html_path = tmp_path / "deck.html"
    html_path.write_text(
        '<html><head><style>html,body{margin:0}'
        '.slide{position:relative;width:1920px;height:1080px;background:#fff}'
        '</style></head><body>' + "".join(slides) + '</body></html>',
        encoding="utf-8",
    )
    pptx_path = tmp_path / "deck.pptx"
    result = _run_node(
        EXPORT_SCRIPT_PATH, str(html_path), str(pptx_path),
        "--out", str(tmp_path / "slides"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = _last_json_object(result.stdout)
    assert summary["slideCount"] == 2
    assert len(summary["warnings"]) == 1
    assert f"Slide {mismatched_slide + 1}: size 1000x1000" in summary["warnings"][0]
    with zipfile.ZipFile(pptx_path) as archive:
        assert "ppt/slides/slide2.xml" in archive.namelist()
    assert not (tmp_path / "qa" / "html_self_check.json").exists()


@pytest.mark.parametrize("stale_svg", [False, True])
def test_editable_export_reports_incomplete_diagram_without_blocking(
    tmp_path: Path, stale_svg: bool
) -> None:
    svg = (
        '<svg viewBox="0 0 600 300"><rect width="600" height="300" fill="red"/></svg>'
        if stale_svg else ""
    )
    html_path = tmp_path / "deck.html"
    html_path.write_text(
        '<html><head><style>html,body{margin:0}'
        '.slide{width:1920px;height:1080px;position:relative;background:#fff}'
        '[data-pptx-diagram],svg{width:600px;height:300px}'
        '</style></head><body><section class="slide"><h1>Keep this content</h1>'
        '<div data-pptx-diagram data-diagram-render-state="error" '
        'data-diagram-spec-src="missing.json">' + svg + '</div></section>'
        '<script>window.__diagramReady=Promise.reject(new Error("render failed"));'
        'window.__diagramReady.catch(()=>{});</script></body></html>',
        encoding="utf-8",
    )
    pptx_path = tmp_path / "deck.pptx"
    result = _run_node(
        EXPORT_SCRIPT_PATH, str(html_path), str(pptx_path),
        "--out", str(tmp_path / "slides"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = _last_json_object(result.stdout)
    assert summary["diagramCount"] == 1
    assert summary["diagramVectorExport"] is False
    assert len(summary["warnings"]) == 1
    assert "Slide 1, diagram 1: missing or failed diagram" in summary["warnings"][0]
    with zipfile.ZipFile(pptx_path) as archive:
        assert "Keep this content" in archive.read("ppt/slides/slide1.xml").decode()


def test_html_self_check_rejects_invalid_technical_diagram_contract(
    tmp_path: Path,
) -> None:
    html_path = tmp_path / "invalid-diagrams.html"
    report_path = tmp_path / "report.json"
    html_path.write_text(
        """<!doctype html><html><head><meta charset="utf-8"><style>
        html,body{margin:0}.slide{width:1920px;height:1080px;position:relative;overflow:hidden}
        [data-pptx-diagram]{width:600px;height:300px}
        svg{width:600px;height:300px}
        </style></head><body>
        <section class="slide"><div data-pptx-diagram><svg></svg></div></section>
        <section class="slide"><div data-pptx-diagram data-diagram-spec='{"nodes":[],"edges":[]}'>
          <img src="missing.svg" alt="invalid svg image path">
        </div></section>
        <section class="slide"><div data-pptx-diagram data-diagram-spec='{"nodes":[],"edges":[]}'>
          <svg></svg><svg></svg>
        </div></section>
        <section class="slide"><div data-pptx-diagram data-diagram-spec='{"nodes":[],"edges":[]}'>
          <svg data-pptx-decoration></svg>
        </div></section>
        </body></html>""",
        encoding="utf-8",
    )
    result = _run_node(
        SELF_CHECK_SCRIPT_PATH,
        str(html_path),
        "--dom-to-pptx",
        "--allow-local-images",
        "--report",
        str(report_path),
    )

    assert result.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    issues = "\n".join(report["issues"])
    assert report["diagramCount"] == 4
    assert "requires a recoverable DiagramSpec" in issues
    assert "exactly one direct inline <svg> root; found 0" in issues
    assert 'must export from inline <svg>, not <img src="*.svg">' in issues
    assert "exactly one direct inline <svg> root; found 2" in issues
    assert "must not be marked data-pptx-decoration" in issues


def test_controlled_technical_diagram_supports_three_kinds_and_editor(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run_node(
        INSPECT_SCRIPT_PATH,
        "cover-editorial-v1",
        "technical-diagram-v1",
        "technical-diagram-v1",
        "technical-diagram-v1",
        "--theme",
        "blue-professional",
        "--family",
        "technical-schematic",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    kinds = ("architecture", "integration", "pipeline")
    for slide, kind in zip(deck["slides"][1:], kinds, strict=True):
        slide["props"]["diagram_kind"] = kind
        slide["props"]["direction"] = "RIGHT"
        slide["props"]["title"] = f"{kind} regression"
    architecture = deck["slides"][1]["props"]
    architecture["nodes"] = [
        {"id": "channel", "label": "渠道接入层", "kind": "client"},
        {"id": "ai", "label": "AI 能力层", "kind": "hub"},
        {"id": "gateway", "label": "业务集成层", "kind": "gateway"},
        {"id": "business", "label": "业务系统", "kind": "external"},
        {"id": "data", "label": "数据治理层", "kind": "data"},
        {"id": "ops", "label": "运营管理层", "kind": "service"},
    ]
    architecture["edges"] = [
        {"id": "a1", "source": "channel", "target": "ai", "label": "会话请求"},
        {"id": "a2", "source": "ai", "target": "gateway", "label": "工具调用"},
        {"id": "a3", "source": "gateway", "target": "business", "label": "业务读写"},
        {"id": "a4", "source": "business", "target": "data", "label": "服务记录"},
        {"id": "a5", "source": "data", "target": "ops", "label": "效果评估"},
        {"id": "a6", "source": "ops", "target": "ai", "label": "策略迭代"},
    ]
    pipeline = deck["slides"][3]["props"]
    pipeline["nodes"] = [
        {"id": "ingest", "label": "数据接入", "kind": "client"},
        {"id": "govern", "label": "数据治理", "kind": "gateway"},
        {"id": "knowledge", "label": "知识处理", "kind": "data"},
        {"id": "index", "label": "索引与检索", "kind": "service"},
        {"id": "model", "label": "模型应用", "kind": "hub"},
        {"id": "feedback", "label": "效果反馈", "kind": "data"},
    ]
    pipeline["edges"] = [
        {"id": "p1", "source": "ingest", "target": "govern", "label": "采集"},
        {"id": "p2", "source": "govern", "target": "knowledge", "label": "治理后入库"},
        {"id": "p3", "source": "knowledge", "target": "index", "label": "知识化"},
        {"id": "p4", "source": "index", "target": "model", "label": "检索生成"},
        {"id": "p5", "source": "model", "target": "feedback", "label": "服务结果"},
        {"id": "p6", "source": "feedback", "target": "knowledge", "label": "持续优化"},
    ]
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    rendered = _run_node(RENDER_SCRIPT_PATH, str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr

    html = html_path.read_text(encoding="utf-8")
    rendered_markup = html.split('<script type="application/json" id="deck-document">', 1)[0]
    assert rendered_markup.count(" data-pptx-diagram") == 3
    assert 'data-deck-runtime="elkjs" data-elk-version="0.12.0"' in html
    assert 'data-deck-runtime="diagram-runtime"' in html
    for kind in kinds:
        assert f'data-diagram-kind="{kind}"' in html

    report_path = tmp_path / "qa" / "html_self_check.json"
    checked = _run_node(
        SELF_CHECK_SCRIPT_PATH,
        str(html_path),
        "--dom-to-pptx",
        "--allow-local-images",
        "--report",
        str(report_path),
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["diagramCount"] == 3
    assert report["issues"] == []

    probed = _run_node(
        PROBE_SCRIPT_PATH,
        str(html_path),
        "--exercise-diagram-editor",
    )
    assert probed.returncode == 0, probed.stdout + probed.stderr
    probe = json.loads(probed.stdout)
    exercise = probe["editor"]["diagramExercise"]
    assert exercise["editedNodeObserved"] is True
    assert exercise["initial"] == exercise["final"]
    assert exercise["state"] == "ready"
    assert exercise["svgRoots"] == 1
    assert exercise["initial"]["slideIndex"] == 1
    assert [item["strategy"] for item in probe["editor"]["diagrams"]] == [
        "layered-architecture",
        "center-hub",
        "wrapped-pipeline",
    ]
    for diagram in probe["editor"]["diagrams"]:
        assert diagram["nodes"] == diagram["specNodes"]
        assert diagram["uniqueNodeIds"] == diagram["nodes"]
    assert probe["editor"]["diagrams"][0]["nodeSpread"]["height"] > 250


def test_marked_diagram_exports_as_vector_and_stays_out_of_background(
    tmp_path: Path,
) -> None:
    summary, pptx_path = _export_fixture(tmp_path, marked=True)

    assert summary["diagramCount"] == 1
    assert summary["diagramVectorExport"] is True
    with zipfile.ZipFile(pptx_path) as archive:
        background_targets, vector_targets = _slide_picture_targets(archive)
        assert len(vector_targets) == 1
        assert vector_targets[0].endswith(".svg")
        vector_svg = archive.read(vector_targets[0])
        assert b"VECTOR_DIAGRAM_SENTINEL" in vector_svg
        assert background_targets
        background = Image.open(io.BytesIO(archive.read(background_targets[0]))).convert(
            "RGB"
        )
        center = background.getpixel((background.width // 2, background.height // 2))
        assert all(channel >= 245 for channel in center)


def test_unmarked_inline_svg_keeps_existing_background_capture_behavior(
    tmp_path: Path,
) -> None:
    summary, pptx_path = _export_fixture(tmp_path, marked=False)

    assert summary["diagramCount"] == 0
    assert summary["diagramVectorExport"] is False
    with zipfile.ZipFile(pptx_path) as archive:
        media = [name for name in archive.namelist() if name.startswith("ppt/media/")]
        assert not any(name.endswith(".svg") for name in media)
        background_targets, vector_targets = _slide_picture_targets(archive)
        assert vector_targets == []
        assert background_targets
        background = Image.open(io.BytesIO(archive.read(background_targets[0]))).convert(
            "RGB"
        )
        center = background.getpixel((background.width // 2, background.height // 2))
        assert center[0] > 200 and center[1] < 120 and center[2] < 150


def _export_svg_background(tmp_path: Path, markup: str) -> Image.Image:
    html = tmp_path / "svg.html"
    html.write_text(
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        'html,body{margin:0}.slide{width:1920px;height:1080px;position:relative;'
        'overflow:hidden;background:white}.graphic{position:absolute;left:100px;'
        'top:160px;width:400px;height:240px}.graphic svg{width:100%;height:100%}'
        'h1{position:absolute;left:100px;top:20px;margin:0;font:40px Arial}'
        '</style></head><body><section class="slide">'
        '<h1>EDITABLE_SENTINEL</h1>' + markup + '</section></body></html>',
        encoding="utf-8",
    )
    pptx = tmp_path / "svg.pptx"
    result = _run_node(EXPORT_SCRIPT_PATH, str(html), str(pptx),
                       "--out", str(tmp_path / "previews"))
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(pptx) as archive:
        backgrounds, vectors = _slide_picture_targets(archive)
        # Ordinary SVGs retain the existing single-background representation.
        assert len(backgrounds) == 1
        assert vectors == []
        slide = ET.fromstring(archive.read("ppt/slides/slide1.xml"))
        texts = slide.findall(
            ".//{http://schemas.openxmlformats.org/drawingml/2006/main}t"
        )
        assert "EDITABLE_SENTINEL" in [node.text for node in texts]
        background = Image.open(io.BytesIO(archive.read(backgrounds[0]))).convert("RGB")
        # The managed browser may capture at a higher device pixel ratio.
        background = background.resize((1920, 1080), Image.Resampling.NEAREST)
        # HTML text is still native; it must not also be baked into the bitmap.
        assert background.crop((100, 20, 700, 70)).getextrema() == ((255, 255),) * 3
        return background


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("labelled", [False, True])
def test_background_capture_preserves_svg_graphics_and_labels(
    tmp_path: Path, nested: bool, labelled: bool,
) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 240">'
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto">'
        '<path d="M0,0 L10,5 L0,10 Z" fill="#ff0000"/></marker></defs>'
        '<rect x="20" y="20" width="80" height="60" fill="#00cc00"/>'
        '<line x1="40" y1="140" x2="300" y2="140" stroke="#ff0000" '
        'stroke-width="6" marker-end="url(#arrow)"/>'
        + ('<text x="130" y="65" font-family="Arial" font-size="48" '
           'fill="#0000ff">NODE</text>' if labelled else '') + '</svg>'
    )
    if nested:
        # An ordinary content ancestor is hidden only while capturing backgrounds.
        markup = '<div><p>Native sibling</p><div class="graphic">' + svg + '</div></div>'
    else:
        markup = svg.replace('<svg ', '<svg class="graphic" ', 1)
    background = _export_svg_background(tmp_path, markup)
    assert background.getpixel((150, 200)) == (0, 204, 0), "SVG node was lost"
    assert background.getpixel((280, 300)) == (255, 0, 0), "SVG edge was lost"
    red, green, blue = background.getpixel((377, 311))
    assert red > 240 and green < 32 and blue < 32, "SVG arrowhead was lost"
    if labelled:
        colors = background.crop((220, 180, 390, 230)).getcolors(10000)
        assert sum(count for count, (r, g, b) in colors
                   if b > 200 and r < 50 and g < 50) > 300, (
            "SVG node label was erased"
        )


def test_background_capture_preserves_authored_svg_visibility_and_clipping(
    tmp_path: Path,
) -> None:
    markup = '''<div><p>Native sibling</p><div class="graphic">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 240">
      <defs><clipPath id="clip"><rect x="260" y="20" width="20" height="60"/></clipPath></defs>
      <g visibility="hidden"><rect x="20" y="20" width="40" height="60" fill="#ff00ff"/>
        <rect x="80" y="20" width="40" height="60" fill="#ffff00" visibility="visible"/></g>
      <g opacity="0"><rect x="140" y="20" width="40" height="60" fill="#ff00ff"/></g>
      <g display="none"><rect x="200" y="20" width="40" height="60" fill="#ff00ff"/></g>
      <rect x="260" y="20" width="60" height="60" fill="#00ffff" clip-path="url(#clip)"/>
      <rect x="340" y="20" width="40" height="60" fill="#000000" opacity="0.5"/>
    </svg></div></div>'''
    background = _export_svg_background(tmp_path, markup)
    for x in (140, 260, 320, 400):
        assert background.getpixel((x, 200)) == (255, 255, 255)
    assert background.getpixel((200, 200)) == (255, 255, 0)
    assert background.getpixel((370, 200)) == (0, 255, 255)
    assert all(125 <= c <= 130 for c in background.getpixel((460, 200)))


@pytest.mark.parametrize("stale_decoration", [False, True])
def test_background_capture_keeps_svg_chart_preview_out_of_native_chart(
    tmp_path: Path, stale_decoration: bool,
) -> None:
    spec = json.dumps({"type": "column", "categories": ["A", "B"],
                       "series": [{"name": "Revenue", "values": [7, 13]}]})
    markup = (
        '<div class="graphic" data-pptx-chart data-native-chart="true" '
        "data-chart-spec='" + spec + "'>"
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 240">'
        '<rect width="400" height="240" fill="#ff00ff"/>'
        '<text x="20" y="50">CHART_PREVIEW</text></svg></div>'
    )
    if stale_decoration:
        for tag in ("svg", "rect", "text"):
            markup = markup.replace(f'<{tag} ', f'<{tag} data-pptx-decoration ', 1)
    background = _export_svg_background(tmp_path, markup)
    assert background.getpixel((200, 200)) == (255, 255, 255)
    with zipfile.ZipFile(tmp_path / "svg.pptx") as archive:
        charts = [name for name in archive.namelist()
                  if name.startswith("ppt/charts/chart") and name.endswith(".xml")]
        assert len(charts) == 1
        chart = ET.fromstring(archive.read(charts[0]))
        values = chart.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/chart}v")
        assert {"Revenue", "A", "B", "7", "13"}.issubset({node.text for node in values})
        assert any(name.startswith("ppt/embeddings/") and name.endswith(".xlsx")
                   for name in archive.namelist())


def test_expressive_export_keeps_text_native_and_resolves_missing_font(tmp_path: Path) -> None:
    html_path = tmp_path / "index.html"
    html_path.write_text('''<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0} .slide{position:relative;width:1920px;height:1080px;background:white;color:black;overflow:hidden}
[data-prop-kind="text"]{position:absolute;left:80px;width:1700px;height:420px;margin:0;line-height:1.4;padding:0 24px 32px 0;box-sizing:border-box;font-family:"Pptx Nonexistent Font Sentinel",Arial,sans-serif}
h1{top:50px;font-size:250px}p{top:550px;font-size:210px}
</style></head><body><main id="deck-root"><section class="slide expressive-slide">
<h1 data-prop-kind="text" data-prop-path="title">NOON</h1>
<p data-prop-kind="text" data-prop-path="value">28</p>
</section></main></body></html>''', encoding="utf-8")
    pptx_path = tmp_path / "editable.pptx"
    result = _run_node(EXPORT_SCRIPT_PATH, str(html_path), str(pptx_path), "--out", str(tmp_path / "slides"))
    assert result.returncode == 0, result.stdout + result.stderr
    report = _last_json_object(result.stdout)
    assert report["fontResolution"] == {"resolved": 2, "warnings": []}
    with zipfile.ZipFile(pptx_path) as archive:
        xml = archive.read("ppt/slides/slide1.xml").decode()
    assert "Pptx Nonexistent Font Sentinel" not in xml
    tree = ET.fromstring(xml)
    texts = [node.text for node in tree.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}t")]
    assert texts == ["NOON", "28"]


def test_expressive_font_resolution_reports_unavailable_probe_without_blocking(tmp_path: Path) -> None:
    probe = tmp_path / "probe.js"
    probe.write_text('''const {resolveExpressiveExportFonts} = require(process.argv[2]);
const page = {locator: () => ({count: async () => 2}), context: () => ({newCDPSession: async () => {throw Error('CDP unavailable')}})};
resolveExpressiveExportFonts(page).then(value => console.log(JSON.stringify(value)));
''', encoding="utf-8")
    result = _run_node(probe, str(SCRIPTS_DIR / "export_font_resolution.js"))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["resolved"] == 0
    assert report["warnings"] == ["Actual font resolution unavailable: CDP unavailable"]
