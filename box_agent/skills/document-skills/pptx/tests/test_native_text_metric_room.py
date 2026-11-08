"""Keep tight controlled native text clear of its next block without rewriting copy."""

import json
import os
import re
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
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}
TITLE = "检索AI｜示例团队的资料归档"
LABEL = "数量汇总｜模拟2031年8月"
HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}html,body{margin:0}.slide{position:relative;width:1920px;height:1080px;background:#f5f7fc;color:#101828}
h3,p{margin:0}.open-side{position:absolute;left:90px;width:820px;top:120px}.open-side-title{font:700 60.25px/1.22 sans-serif;padding:0 24px 16px 0}.body{font:28px/1.4 sans-serif;margin-top:24px}
.roomy-title{top:540px;width:1100px}
.kpi-card{position:absolute;left:90px;top:150px;width:520px}.kpi-label{width:fit-content;font:600 20px/28px sans-serif;padding:7px 14px;border-radius:28px;background:#dadff7}.kpi-value{font:700 68px/1 sans-serif;margin-top:20px}
.roomy-label{left:920px}.roomy-label .kpi-label{width:500px}
</style></head><body><main id="deck-root">
<section class="slide">
<article class="open-side"><h3 class="open-side-title" data-prop-kind="text">检索AI｜示例团队的资料归档</h3><p class="body">输入材料与核对要求</p></article>
<article class="open-side roomy-title"><h3 class="open-side-title" data-prop-kind="text">检索AI｜示例团队的资料归档</h3><p class="body">输入材料与核对要求</p></article>
</section>
<section class="slide">
<article class="kpi-card"><p class="kpi-label" data-prop-kind="text">数量汇总｜模拟2031年8月</p><p class="kpi-value">640份</p></article>
<article class="kpi-card roomy-label"><p class="kpi-label" data-prop-kind="text">数量汇总｜模拟2031年8月</p><p class="kpi-value">640份</p></article>
</section><section class="slide">
<article class="kpi-card" style="top:80px;width:300px;padding-right:16px;border-right:4px solid">
<p class="kpi-label" data-prop-kind="text">限宽标签｜模拟2032年2月</p></article>
<article class="kpi-card" style="top:80px;left:550px;width:300px;padding-right:16px;border-right:4px solid">
<p class="kpi-label">限宽标签｜模拟2032年2月</p></article>
<article class="open-side" style="top:320px;width:300px">
<h3 class="open-side-title" data-prop-kind="text">整理资料时保留核对步骤</h3></article>
<article class="open-side" style="top:320px;left:550px;width:300px">
<h3 class="open-side-title">整理资料时保留核对步骤</h3></article>
<article class="kpi-card" style="top:80px;left:1100px">
<p class="kpi-label" data-prop-kind="text" style="margin-left:auto">右侧标签｜模拟2032年2月</p></article>
<article class="kpi-card" style="top:240px;left:1100px">
<p class="kpi-label" style="margin-left:auto">右侧标签｜模拟2032年2月</p></article>
<article class="kpi-card" style="top:450px;left:1100px">
<p class="kpi-label" data-prop-kind="text" style="width:100px">多行标签保留原有换行</p></article>
<article class="kpi-card" style="top:450px;left:1450px">
<p class="kpi-label" style="width:100px">多行标签保留原有换行</p></article>
<article class="kpi-card" style="top:800px;width:fit-content">
<p class="kpi-label" data-prop-kind="text">无余量标签｜模拟2032年2月</p></article>
<article class="kpi-card" style="top:800px;left:550px;width:fit-content">
<p class="kpi-label">无余量标签｜模拟2032年2月</p></article>
</section></main></body></html>"""


def _export(root, html):
    if not NODE:
        pytest.skip("Node.js is required for native text export")
    source, pptx = root / "source.html", root / "result.pptx"
    source.write_text(html, encoding="utf-8")
    result = subprocess.run(
        [str(NODE), str(EXPORTER), str(source), str(pptx), "--out", str(root / "previews")],
        cwd=root, env={**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(root),
                       "BOX_AGENT_HOME": str(root / ".profile"),
                       "BOX_AGENT_OUTPUT_DIR": str(root)},
        capture_output=True, text=True, timeout=90,
    )
    (root / "export.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (root / "export.stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert source.read_text(encoding="utf-8") == html
    with zipfile.ZipFile(pptx) as archive:
        canvas = ET.fromstring(archive.read("ppt/presentation.xml")).find("p:sldSz", NS)
        scale = int(canvas.get("cx")) / 1920
        slides = []
        entries = sorted(name for name in archive.namelist()
                         if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
        for entry in entries:
            slide = ET.fromstring(archive.read(entry))
            shapes = []
            for shape in slide.findall(".//p:sp", NS):
                text = "".join(n.text or "" for n in shape.findall(".//a:t", NS))
                if not text:
                    continue
                transform = shape.find("p:spPr/a:xfrm", NS)
                offset, extent = transform.find("a:off", NS), transform.find("a:ext", NS)
                body = shape.find("p:txBody/a:bodyPr", NS)
                shapes.append({
                    "text": text,
                    **{k: int(offset.get(k)) / scale for k in ("x", "y")},
                    "w": int(extent.get("cx")) / scale, "h": int(extent.get("cy")) / scale,
                    "right_padding": int(body.get("rIns", "0")) / scale,
                })
            slides.append(shapes)
    return root, pptx, slides


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    return _export(tmp_path_factory.mktemp("native-text"), HTML)


@pytest.fixture(scope="module")
def native(exported):
    root, pptx, _ = exported
    soffice = os.environ.get("BOX_AGENT_TEST_SOFFICE") or shutil.which("soffice")
    if not soffice and Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").is_file():
        soffice = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    pdftohtml = shutil.which("pdftohtml")
    if not soffice or not pdftohtml:
        pytest.skip("LibreOffice and pdftohtml are required for native-render checks")
    out = root / "native"
    out.mkdir()
    result = subprocess.run(
        [soffice, "-env:UserInstallation=" + (root / "lo-profile").as_uri(),
         "--headless", "--convert-to", "pdf", "--outdir", str(out), str(pptx)],
        capture_output=True, text=True, timeout=60,
    )
    (out / "render.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    pdf = out / "result.pdf"
    assert result.returncode == 0 and pdf.is_file(), result.stdout + result.stderr
    result = subprocess.run([pdftohtml, "-xml", "-i", "-stdout", str(pdf)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    (out / "text.xml").write_text(result.stdout, encoding="utf-8")
    document = ET.fromstring(result.stdout)
    fonts = {f.get("id"): float(f.get("size")) for f in document.findall(".//fontspec")}
    return [[{
        "text": "".join(t.itertext()), "size": fonts[t.get("font")],
        **{k: float(t.get(k)) for k in ("left", "top", "width", "height")},
    } for t in p.findall("text")] for p in document.findall("page")]


def _assert_single_line_before(items, following, expected):
    assert items and following
    # Latin/CJK font ascents can differ within one line; a second line is
    # separated by the authored line height, not that small baseline difference.
    assert max(i["top"] for i in items) - min(i["top"] for i in items) < max(i["size"] for i in items) / 2
    text = "".join(i["text"] for i in sorted(items, key=lambda i: i["left"]))
    assert re.sub(r"\s+", "", text) == expected
    assert max(i["top"] + i["height"] for i in items) <= min(i["top"] for i in following)


def test_tight_title_stays_on_one_native_line_clear_of_body(native):
    first = [i for i in native[0] if i["top"] < 300]
    _assert_single_line_before([i for i in first if i["size"] > 30],
                               [i for i in first if i["size"] <= 30], TITLE)


def test_tight_label_stays_on_one_native_line_clear_of_value(native):
    first = [i for i in native[1] if i["left"] < 500]
    _assert_single_line_before([i for i in first if i["size"] < 30],
                               [i for i in first if i["size"] >= 30], LABEL)


def test_roomy_controls_keep_native_fit_and_authored_boxes(native, exported):
    title = [i for i in native[0] if i["top"] >= 300]
    label = [i for i in native[1] if i["left"] >= 500]
    _assert_single_line_before([i for i in title if i["size"] > 30],
                               [i for i in title if i["size"] <= 30], TITLE)
    _assert_single_line_before([i for i in label if i["size"] < 30],
                               [i for i in label if i["size"] >= 30], LABEL)
    _, _, slides = exported
    roomy_title = next(s for s in slides[0] if s["text"] == TITLE and s["y"] > 300)
    roomy_label = next(s for s in slides[1] if s["text"] == LABEL and s["x"] > 500)
    assert roomy_title["w"] == pytest.approx(1100, abs=0.02)
    assert roomy_title["right_padding"] == pytest.approx(24, abs=0.02)
    assert roomy_label["w"] == pytest.approx(500, abs=0.02)
    assert roomy_label["right_padding"] == pytest.approx(14, abs=0.02)


def test_label_growth_stays_inside_parent_content_bounds(exported):
    _, _, slides = exported
    label = next(s for s in slides[2] if s["text"] == "限宽标签｜模拟2032年2月" and s["x"] < 500)
    assert label["x"] == pytest.approx(90, abs=0.02)
    # 300px parent minus 16px padding and 4px right border.
    assert label["x"] + label["w"] <= 370.02


@pytest.mark.parametrize("text", [
    "整理资料时保留核对步骤", "右侧标签｜模拟2032年2月",
    "多行标签保留原有换行", "无余量标签｜模拟2032年2月",
])
def test_multiline_and_anchored_text_match_unmarked_controls(exported, text):
    _, _, slides = exported
    marked, ordinary = [s for s in slides[2] if s["text"] == text]
    for key in ("w", "h", "right_padding"):
        assert marked[key] == pytest.approx(ordinary[key], abs=0.02)
    if text.startswith("右侧"):
        assert marked["x"] == pytest.approx(ordinary["x"], abs=0.02)


@pytest.mark.parametrize("layout", ["ledger-grid", "hero", "flex-row"])
def test_horizontal_kpi_label_keeps_its_column_and_neighbor(tmp_path, layout):
    if not NODE:
        pytest.skip("Node.js is required for native text export")
    skill = Path(__file__).resolve().parents[1]
    label = "数量汇总｜模拟2031年8月"
    slide = {
        "id": "horizontal-sample", "layout_id": "kpi-grid-v1",
        "props": {
            "eyebrow": "资料复核", "title": "横排指标保持各自区域", "subtitle": "",
            "variant": "hero" if layout == "hero" else "cards",
            "items": [
                {"label": label, "value": "740份", "detail": "抽样内容保持独立", "delta": "已核对"},
                {"label": "辅助指标", "value": "83份", "detail": "独立说明", "delta": "待核对"},
                {"label": "补充指标", "value": "26份", "detail": "补充说明", "delta": "已核对"},
            ],
        },
    }
    # Use the real registry markup and owning CSS, including ledger's 260/340px
    # columns without a gap and the hero's 260px label column with a 30px gap.
    rendered = subprocess.run(
        [str(NODE), "-e", "const r=require(process.argv[1]); console.log(r.getLayout('kpi-grid-v1').render(JSON.parse(process.argv[2]),0));",
         str(skill / "layouts/registry.js"), json.dumps(slide, ensure_ascii=False)],
        capture_output=True, text=True, timeout=30,
    )
    assert rendered.returncode == 0, rendered.stderr
    css = "\n".join((skill / "runtime" / name).read_text(encoding="utf-8")
                    for name in ("deck.css", "composition.css"))
    variant = "ledger-grid" if layout == "ledger-grid" else "balanced-grid"
    override = """
      :root{--deck-border-width:2px;--deck-border:#ccc;--deck-body:sans-serif;
        --deck-label:sans-serif;--deck-display:sans-serif;--deck-text:#111;
        --deck-muted:#333;--deck-primary-text:#111;--deck-bg:#fff}
      html,body{margin:0;padding:0}.slide{width:1920px;height:1080px}
      .kpi-label{font:600 20px/28px sans-serif}
    """
    if layout == "flex-row":
        override += """
          .kpi-card{display:flex;flex-direction:row;gap:0;width:1600px}
          .kpi-label{width:260px!important;flex:none}.kpi-value{width:340px;flex:none}
        """
    html = (f'<html><head><meta charset="utf-8"><style>{css}\n{override}</style></head>'
            f'<body data-deck-composition="institutional-grid" data-deck-composition-variant="{variant}">'
            f'<main id="deck-root">{rendered.stdout}</main></body></html>')
    _, _, slides = _export(tmp_path, html)
    label_box = next(s for s in slides[0] if s["text"] == label)
    value_box = next(s for s in slides[0] if s["text"] == "740份")
    assert label_box["w"] == pytest.approx(260, abs=0.02)
    assert label_box["right_padding"] == pytest.approx(0, abs=0.02)
    assert label_box["x"] + label_box["w"] <= value_box["x"] + 0.02
