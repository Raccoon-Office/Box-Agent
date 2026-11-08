"""Background flattening must retain decoration space in authored text layouts."""

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
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
EYEBROW = "资料整理 · Review notes"
TITLE = "独立核对让资料更清楚"


def _html():
    slides = []
    for layout in ("flex", "block", "grid", "nested", "absolute", "hidden", "collapsed-table"):
        decoration = '<div class="rule"><div class="inner"></div></div>'
        control = '<div class="rule control"><div class="inner">spacing control</div></div>'
        if layout == "collapsed-table":
            decoration = ('<table class="table-rule"><tr><td><div></div></td></tr>'
                          '<tr><td><div></div></td></tr></table>')
            control = ('<table class="table-rule control"><tr><td><div></div></td></tr>'
                       '<tr><td><div>spacing control</div></td></tr></table>')
        columns = []
        for index, rule in enumerate((decoration, control)):
            columns.append(f'''<article class="stack {layout}" style="left:{80 + index * 960}px">
              <p class="eyebrow">{EYEBROW}</p>{rule}
              <h1>{TITLE}</h1><p class="body">Source text and adjacent conditions stay separate.</p>
            </article>''')
        slides.append('<section class="slide">' + ''.join(columns) + '</section>')
    return '''<!doctype html><html><head><meta charset="utf-8"><style>
      *{box-sizing:border-box}html,body{margin:0}p,h1{margin:0}
      .slide{position:relative;width:1920px;height:1080px;background:white;color:#111}
      .stack{position:absolute;top:100px;width:800px;height:800px}
      .flex,.nested,.absolute,.hidden{display:flex;flex-direction:column;justify-content:center}
      .grid{display:grid;align-content:center;grid-template-columns:1fr}
      .eyebrow{font:700 22px/27.5px sans-serif}
      h1{font:700 92px/0.96 sans-serif}
      .body{font:26px/1.4 sans-serif;margin-top:34px}
      .rule{width:86px;height:8px;margin:28px 0 38px;background:#3456d8;flex:none}
      .inner{height:8px}.control{visibility:hidden}
      .nested .rule{height:auto}.nested .inner{height:8px}
      .absolute .rule{position:absolute;top:80px;left:0}
      .hidden .rule{display:none}
      .table-rule{border-collapse:collapse;width:86px;margin:28px 0 38px}
      .table-rule td{padding:0}.table-rule div{height:8px;background:#3456d8}
      .table-rule tr:first-child{visibility:collapse}
      .table-rule tr:first-child div{height:120px}
      .table-rule.control tr:first-child{display:none}
    </style></head><body>''' + ''.join(slides) + '</body></html>'


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    node = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for HTML export")
    root = tmp_path_factory.mktemp("decoration-flow")
    html, pptx = root / "source.html", root / "result.pptx"
    text = _html()
    html.write_text(text, encoding="utf-8")
    result = subprocess.run(
        [node, str(EXPORTER), str(html), str(pptx), "--out", str(root / "previews")],
        cwd=root, env={**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(root),
                       "BOX_AGENT_HOME": str(root / ".profile"),
                       "BOX_AGENT_OUTPUT_DIR": str(root)},
        capture_output=True, text=True, timeout=90,
    )
    (root / "export.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert html.read_text(encoding="utf-8") == text
    with zipfile.ZipFile(pptx) as archive:
        size = ET.fromstring(archive.read("ppt/presentation.xml")).find("p:sldSz", NS)
        scale = int(size.get("cx")) / 1920
        slides = []
        for number in range(1, 8):
            shapes = []
            slide = ET.fromstring(archive.read(f"ppt/slides/slide{number}.xml"))
            for shape in slide.findall(".//p:sp", NS):
                copy = ''.join(n.text or '' for n in shape.findall(".//a:t", NS))
                if not copy:
                    continue
                transform = shape.find("p:spPr/a:xfrm", NS)
                off, extent = transform.find("a:off", NS), transform.find("a:ext", NS)
                shapes.append({"text": copy, "x": int(off.get("x")) / scale,
                               "y": int(off.get("y")) / scale,
                               "h": int(extent.get("cy")) / scale})
            slides.append(shapes)
    return root, pptx, slides


@pytest.mark.parametrize("number", [0, 1, 2, 3, 6],
                         ids=["flex", "block", "grid", "nested", "collapsed-table"])
def test_captured_flow_decoration_keeps_text_at_authored_positions(exported, number):
    _, _, slides = exported
    for copy in (EYEBROW, TITLE, "Source text and adjacent conditions stay separate."):
        actual, control = sorted([s for s in slides[number] if s["text"] == copy],
                                 key=lambda s: s["x"])
        assert actual["y"] == pytest.approx(control["y"], abs=0.02)
        assert actual["h"] == pytest.approx(control["h"], abs=0.02)


@pytest.mark.parametrize("number", [4, 5], ids=["absolute", "authored-hidden"])
def test_nonflow_and_authored_hidden_decoration_do_not_add_spacing(exported, number):
    _, _, slides = exported
    for copy in (EYEBROW, TITLE):
        actual, control = sorted([s for s in slides[number] if s["text"] == copy],
                                 key=lambda s: s["x"])
        assert actual["y"] == pytest.approx(control["y"], abs=0.02)
    assert all("spacing control" not in s["text"] for s in slides[number])


def test_native_cover_title_stays_below_eyebrow(exported):
    root, pptx, _ = exported
    soffice = os.environ.get("BOX_AGENT_TEST_SOFFICE") or shutil.which("soffice")
    if not soffice and Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").is_file():
        soffice = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    pdftohtml = shutil.which("pdftohtml")
    if not soffice or not pdftohtml:
        pytest.skip("LibreOffice and pdftohtml are required for native rendering")
    out = root / "native"
    out.mkdir()
    result = subprocess.run(
        [soffice, "-env:UserInstallation=" + (root / "lo-profile").as_uri(),
         "--headless", "--convert-to", "pdf", "--outdir", str(out), str(pptx)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0 and (out / "result.pdf").is_file(), result.stdout + result.stderr
    result = subprocess.run([pdftohtml, "-xml", "-i", "-stdout", str(out / "result.pdf")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    (out / "text.xml").write_text(result.stdout, encoding="utf-8")
    doc = ET.fromstring(result.stdout)
    page = doc.find("page")
    fonts = {f.get("id"): float(f.get("size")) for f in page.findall("fontspec")}
    text = [t for t in page.findall("text") if float(t.get("left")) < 700]
    labels = [t for t in text if fonts[t.get("font")] < 18]
    title = [t for t in text if fonts[t.get("font")] > 50]
    assert labels and title
    assert max(float(t.get("top")) + float(t.get("height")) for t in labels) <= min(float(t.get("top")) for t in title)
    assert re.sub(r"\s+", "", ''.join(''.join(t.itertext()) for t in title)) == TITLE
