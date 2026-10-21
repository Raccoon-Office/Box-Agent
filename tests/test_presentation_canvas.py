"""Explicit task canvas dimensions survive init, default rendering and PPTX."""

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts"


@pytest.mark.parametrize("width,height", [(1600, 900), (1920, 1080), (1200, 900), (900, 1600)])
def test_init_records_exact_canvas_and_preserves_existing_theme(tmp_path, width, height):
    command = [sys.executable, str(SCRIPTS / "deck.py"), "init", str(tmp_path),
               "--width", str(width), "--height", str(height)]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["canvas"] == [width, height]
    original = (tmp_path / "base.css").read_bytes()
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert (tmp_path / "base.css").read_bytes() == original
    mismatch = subprocess.run(command[:-3] + [str(width + 1), "--height", str(height)], capture_output=True, text=True)
    assert mismatch.returncode != 0
    assert "existing base.css" in mismatch.stderr
    assert (tmp_path / "base.css").read_bytes() == original


@pytest.mark.parametrize("arguments", [["--width", "0", "--height", "900"], ["--width", "1920"]])
def test_init_rejects_invalid_or_partial_dimensions_without_writing_css(tmp_path, arguments):
    result = subprocess.run([sys.executable, str(SCRIPTS / "deck.py"), "init", str(tmp_path), *arguments],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "both be positive" in result.stderr
    assert not (tmp_path / "base.css").exists()


@pytest.mark.parametrize("width,height", [(1600, 900), (1920, 1080), (1200, 900), (900, 1600)])
def test_default_batch_render_uses_same_canvas_as_player(tmp_path, monkeypatch, width, height):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    deck = runpy.run_path(str(SCRIPTS / "deck.py"))
    deck["_init_workspace"](tmp_path, width, height)
    render = runpy.run_path(str(SCRIPTS / "render.py"))["render_batch"]
    state = render.__globals__
    (tmp_path / "slides").mkdir()
    (tmp_path / "slides/slide_01.html").write_text("<html></html>")

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    seen = []

    def once(session, slide, target, w, h):
        seen.append((w, h))
        Path(target).write_bytes(b"PNG fixture")
        return {}

    monkeypatch.setitem(state, "RenderSession", Session)
    monkeypatch.setitem(state, "_setup_libs", lambda: None)
    monkeypatch.setitem(state, "_sync_playwright", lambda: None)
    monkeypatch.setitem(state, "_render_once", once)
    monkeypatch.setitem(state, "_record_render_report", lambda *args: None)
    receipt = render(str(tmp_path))
    assert seen == [deck["_detect_canvas"](tmp_path)] == [(width, height)]
    assert receipt["canvas"] == {"width": width, "height": height, "device_scale": 2}


@pytest.mark.skipif(not os.environ.get("PPT_EXPORT_NODE_MODULES"), reason="native PPTX exporter dependencies opt in")
@pytest.mark.parametrize("width,height", [(1920, 1080), (1200, 900), (900, 1600)])
def test_native_pptx_has_actual_html_canvas_aspect_ratio(tmp_path, width, height):
    exporter = tmp_path / "exporter"
    shutil.copytree(SCRIPTS / "export_pptx", exporter, ignore=shutil.ignore_patterns("node_modules"))
    (exporter / "node_modules").symlink_to(os.environ["PPT_EXPORT_NODE_MODULES"], target_is_directory=True)
    output = tmp_path / "result.pptx"
    program = (f"import {{buildPptx}} from {json.dumps((exporter / 'lib/pptx_builder.mjs').as_uri())};\n"
               f"console.log(await buildPptx([{{path:'slide.html',ir:{{canvasWidth:{width},canvasHeight:{height}}}}}],"
               f"{json.dumps(str(tmp_path))},{json.dumps(str(output))}));")
    result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as archive:
        root = ET.fromstring(archive.read("ppt/presentation.xml"))
    size = root.find("{http://schemas.openxmlformats.org/presentationml/2006/main}sldSz")
    assert int(size.attrib["cx"]) / int(size.attrib["cy"]) == pytest.approx(width / height, abs=1e-5)
