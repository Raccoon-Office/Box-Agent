"""Portable real Chromium smoke: opt in locally, mandatory in platform CI."""

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(os.environ.get("PPT_RENDER_BROWSER_TESTS") != "1",
                                reason="requires installed Chromium; platform CI enables this")
REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "box_agent/skills/presentation-suite/skills"
SCRIPTS = SKILLS / "sn-ppt-standard/scripts"


def test_real_renderer_handles_unicode_paths_batch_reports_and_player(tmp_path):
    # No importorskip: an enabled smoke must fail if provisioned deps are absent.
    from PIL import Image

    deck = tmp_path / "演示 文稿 #1"
    for directory in ("slides", "renders", "assets"):
        (deck / directory).mkdir(parents=True)
    shutil.copyfile(SCRIPTS.parent / "assets/vendor/echarts.min.js", deck / "assets/echarts.min.js")
    html = '''<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:1600px;height:900px;background:white;color:#111}
.slide{width:1600px;height:900px;display:grid;place-items:center}
#chart{width:600px;height:400px}
</style></head><body><section class="slide"><div id="chart"></div></section>
<script src="../assets/echarts.min.js"></script><script>
echarts.init(document.getElementById('chart')).setOption({animation:false,xAxis:{data:['A','B']},yAxis:{},series:[{type:'bar',data:[12,20]}]});
</script></body></html>'''
    for number in (1, 2):
        (deck / f"slides/slide_{number:02d}.html").write_text(html, encoding="utf-8")
    (deck / "present.html").write_text('''<!doctype html><html><body>
<iframe data-slide="1" data-ok="1" src="slides/slide_01.html" width="1600" height="900"></iframe>
<iframe data-slide="2" data-ok="1" src="slides/slide_02.html" width="1600" height="900"></iframe>
<script>window.cleanDeck={go(n){}};</script></body></html>''', encoding="utf-8")
    source = deck / "slides/slide_01.html"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    # Match the Windows Box-Agent Bash host's existing UTF-8 process contract.
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", RENDER_JOB_TIMEOUT="60")
    for args in ([str(source), str(deck / "single.png")], ["--batch", str(deck)], ["--audit-player", str(deck)]):
        result = subprocess.run([sys.executable, str(SCRIPTS / "render.py"), *args],
                                env=environment, capture_output=True, text=True,
                                encoding="utf-8", timeout=65)
        assert result.returncode == 0, result.stderr
        if args[0] == "--batch":
            receipt = json.loads(result.stdout.splitlines()[-1])
            assert receipt["status"] == "rendered" and receipt["qa"] == "not-run"
        if args[0] == "--audit-player":
            assert "PASS charts=2" in result.stdout
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    report = json.loads((deck / "renders/render.json").read_text(encoding="utf-8"))
    assert set(report["pages"]) == {"01", "02"}
    for number in (1, 2):
        with Image.open(deck / f"renders/slide_{number:02d}.png") as rendered:
            assert rendered.size == (3200, 1800)
        assert not report["pages"][f"{number:02d}"]["hard_issues"]


def test_real_doctor_checks_shipped_renderer(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    doctor = runpy.run_path(str(SKILLS / "sn-ppt-doctor/ppt_doctor/check_environment.py"))
    result = doctor["playwright_chromium_status"](SCRIPTS.parent)
    assert result["status"] == "available", result
    assert result["renderer_returncode"] == 0
