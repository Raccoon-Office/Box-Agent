"""Do not emit unverified legacy charset hints for native Unicode text/fonts.

OOXML checks guard the generator regression; native PowerPoint readability is a
separate integration check, not something an XML assertion can establish.
"""

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
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}
HTML = '''<!doctype html><meta charset="utf-8"><style>
body{margin:0}.slide{position:relative;width:1920px;height:1080px;background:white;
font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif;color:#173047}
h1{position:absolute;left:80px;top:45px;font-size:48px}
table{position:absolute;left:80px;top:170px;width:1760px;table-layout:fixed;border-collapse:collapse}
th,td{padding:22px;border:1px solid #778899;font-size:30px;line-height:1.4;text-align:left}
th>span,td>span{display:block}
.wrapped{width:440px}.latin{font-family:Arial,sans-serif}
ul{position:absolute;left:80px;top:600px;width:1700px;padding:0;list-style:none;font-size:36px;line-height:1.7}
li::before{content:"—"}code{font-family:monospace}
</style><main id="deck-root"><section class="slide">
<h1>字体交接中性样例 / Mixed script</h1>
<table><tr><th><span>项目</span></th><th><span>说明</span></th><th class="latin"><span>English control</span></th></tr>
<tr><td><span>样例甲</span></td><td><span>RECORD A17 测试记录与中文说明</span></td><td class="latin"><span>Record 123</span></td></tr>
<tr><td><span>换行对照</span></td><td><span>第一行中文说明<br>第二行中文说明</span></td><td class="latin"><span>Line one<br>Line two</span></td></tr></table>
<ul><li>普通中文段落：资料整理与步骤核验</li>
<li>REPORT A17 / ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789：中文核验</li>
<li class="latin">English-only prefix remains Latin</li>
<li>混合样式：<code>CODE_42</code> 保留代码字体</li></ul>
</section></main>'''


def _export(root, *, preload=None):
    if not NODE:
        pytest.skip("Node.js is required for HTML/PPTX export tests")
    source, pptx = root / "neutral.html", root / "neutral.pptx"
    source.write_text(HTML, encoding="utf-8")
    env = {**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(root),
           "BOX_AGENT_OUTPUT_DIR": str(root)}
    if preload:
        env["NODE_OPTIONS"] = f"--require={preload}"
    result = subprocess.run(
        [str(NODE), str(EXPORTER), str(source), str(pptx), "--out", str(root / "previews")],
        cwd=root, env=env, capture_output=True, text=True, timeout=90,
    )
    (root / "export.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    assert source.read_text(encoding="utf-8") == HTML
    return result, pptx


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    result, pptx = _export(tmp_path_factory.mktemp("unicode-font-charsets"))
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(pptx) as archive:
        return ET.fromstring(archive.read("ppt/slides/slide1.xml"))


def _text(node):
    return "".join(t.text or "" for t in node.findall(".//a:t", NS))


def _face(run):
    return run.find("a:rPr/a:ea", NS).get("typeface")


@pytest.mark.parametrize("scope", [".//a:rPr", ".//a:tc/a:txBody/a:p/a:endParaRPr"])
def test_native_runs_and_table_defaults_omit_unverified_charset(exported, scope):
    properties = exported.findall(scope, NS)
    fonts = [font for node in properties for script in ("latin", "ea", "cs")
             for font in node.findall(f"a:{script}", NS)]
    assert len(fonts) >= 9
    assert all(font.get("typeface") and "," not in font.get("typeface") for font in fonts)
    assert all("charset" not in font.attrib for font in fonts)


def test_unicode_lines_and_inline_font_choices_survive(exported):
    cell = next(c for c in exported.findall(".//a:tc", NS) if "第一行中文说明" in _text(c))
    assert [_text(p) for p in cell.findall("a:txBody/a:p", NS)] == ["第一行中文说明", "第二行中文说明"]
    paragraph = next(p for p in exported.findall(".//p:sp/p:txBody/a:p", NS) if "CODE_42" in _text(p))
    code = next(r for r in paragraph.findall("a:r", NS) if "CODE_42" in _text(r))
    cjk = next(r for r in paragraph.findall("a:r", NS) if "混合样式" in _text(r))
    assert _face(code) != _face(cjk)
    assert _text(paragraph).replace(" ", "") == "—混合样式：CODE_42保留代码字体"
    assert "REPORT A17 / ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789：中文核验" in _text(exported)
    latin_cell = next(c for c in exported.findall(".//a:tc", NS) if _text(c) == "Record 123")
    assert _face(latin_cell.find("a:txBody/a:p/a:r", NS)) == "Arial"
    assert all(int(row.get("h")) > 0 for row in exported.findall(".//a:tr", NS))


@pytest.mark.parametrize("template", ["runProps", "strSlideXml"])
def test_changed_font_template_fails_clearly(tmp_path, template):
    # Alter only the vendor integration point in memory, never its file on disk.
    preload = tmp_path / "simulate-font-template-drift.cjs"
    before = template + r' += "<a:latin typeface=\"".concat(opts.fontFace,'
    after = before.replace(" += ", " += /* vendor drift */ ")
    preload.write_text('''const fs = require("fs");
const original = fs.readFileSync;
fs.readFileSync = function(file, ...args) {
  const value = original.call(this, file, ...args);
  if (String(file).endsWith("dom-to-pptx.bundle.js") && typeof value === "string") {
''' + f"    return value.replace({before!r}, {after!r});\n" + '''  }
  return value;
};
''', encoding="utf-8")
    result, pptx = _export(tmp_path, preload=preload)
    assert result.returncode != 0
    assert "Unsupported dom-to-pptx font exporter; cannot omit unverified charset hints." in result.stderr
    assert not pptx.exists()
