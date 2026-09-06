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
    unavailable = (
        "Cannot find module 'playwright'",
        "Missing dependency: playwright",
        "Executable doesn't exist",
        "Playwright Chromium is not available",
    )
    if result.returncode != 0 and any(
        marker in result.stdout + result.stderr for marker in unavailable
    ):
        pytest.skip("Managed Playwright browser is unavailable")
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


def test_editable_export_does_not_run_html_self_check(tmp_path: Path) -> None:
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
    )

    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert pptx_path.exists()
    assert not report_path.exists()
    assert "htmlSelfCheck" not in _last_json_object(exported.stdout)


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
