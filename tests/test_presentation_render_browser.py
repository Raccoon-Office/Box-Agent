"""Opt-in real Chromium checks; run with PPT_RENDER_BROWSER_TESTS=1.

Uses the installed Skill dependencies/browser. No downloads, app launches or
unrelated process termination are performed by these tests.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(os.environ.get("PPT_RENDER_BROWSER_TESTS") != "1",
                                reason="opt-in installed Chromium lifecycle check")
REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts"


@pytest.fixture
def runtime(monkeypatch):
    pytest.importorskip("playwright")
    pytest.importorskip("psutil")
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import render_runtime
    return render_runtime


@pytest.fixture
def deck(tmp_path):
    (tmp_path / "slides").mkdir()
    (tmp_path / "renders").mkdir()
    (tmp_path / "assets/vendor").mkdir(parents=True)
    shutil.copyfile(SCRIPTS.parent / "assets/vendor/echarts.min.js", tmp_path / "assets/vendor/echarts.min.js")
    source = '''<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:1600px;height:900px;background:white;color:#111}
.slide{width:1600px;height:900px;display:grid;place-items:center;font:48px sans-serif}
#chart{width:600px;height:400px}
</style></head><body><section class="slide"><div id="chart"></div></section>
<script src="../assets/vendor/echarts.min.js"></script><script>
echarts.init(document.getElementById('chart')).setOption({animation:false,xAxis:{data:['A','B']},yAxis:{},series:[{type:'bar',data:[12,20]}]});
</script></body></html>'''
    for number in (1, 2):
        (tmp_path / f"slides/slide_{number:02d}.html").write_text(source)
    (tmp_path / "present.html").write_text('''<!doctype html><html><body>
<iframe data-slide="1" data-ok="1" src="slides/slide_01.html" width="1600" height="900"></iframe>
<iframe data-slide="2" data-ok="1" src="slides/slide_02.html" width="1600" height="900"></iframe>
<script>window.cleanDeck={go(n){}};</script></body></html>''')
    return tmp_path


def _process_alive(identity):
    import psutil
    try:
        return identity.is_running() and identity.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_real_single_batch_and_player_audit_reap_owned_processes(runtime, deck, monkeypatch):
    import psutil
    observed = {}
    escaped = []
    original = runtime._Supervisor._message

    def inspect(self, connection, message):
        original(self, connection, message)
        if message.get("event") == "phase" and message.get("name") == "page":
            groups = {self.process.pid} | {peer["identity"].pid for peer in self.peers.values() if "identity" in peer}
            for child in psutil.Process(self.process.pid).children(recursive=True):
                try:
                    observed[(child.pid, child.create_time())] = child
                    if os.getpgid(child.pid) not in groups:
                        escaped.append((child.pid, child.name(), os.getpgid(child.pid)))
                except (psutil.NoSuchProcess, ProcessLookupError):
                    pass

    monkeypatch.setattr(runtime._Supervisor, "_message", inspect)
    html = deck / "slides/slide_01.html"
    before = hashlib.sha256(html.read_bytes()).hexdigest()
    for args in ([str(html), str(deck / "renders/slide_01.png")], ["--batch", str(deck)], ["--audit-player", str(deck)]):
        result = runtime.run_renderer(SCRIPTS / "render.py", args, timeout=45)
        assert result.returncode == 0, result.stderr
    assert observed
    assert not escaped, escaped
    assert not [identity.pid for identity in observed.values() if _process_alive(identity)]
    assert hashlib.sha256(html.read_bytes()).hexdigest() == before
    report = json.loads((deck / "renders/render.json").read_text())
    assert set(report["pages"]) == {"01", "02"}
    from box_agent.tools.engine.artifact_results import _detect_tool_artifacts, _snapshot_workspace_signatures
    artifacts = _detect_tool_artifacts(
        "render", "bash", "[renders/slide_01.png]", None, {},
        _snapshot_workspace_signatures(str(deck)), str(deck),
    )
    assert not [artifact for artifact in artifacts if artifact.kind == "image"]
    assert (deck / "renders/slide_01.png").is_file()
    assert all(not page["report"]["runtime"]["charts_missing"] for page in report["pages"].values())
    assert (deck / "renders/slide_01.png").stat().st_size > 1000


def test_environment_launch_failure_is_not_retried(runtime, deck, tmp_path, monkeypatch):
    executable = tmp_path / "missing library browser"
    attempts = tmp_path / "launches"
    executable.write_text(f"#!/bin/sh\nprintf 'launch\\n' >> '{attempts}'\nprintf 'error while loading shared libraries: libMissing.so\\n' >&2\nexit 127\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PPT_SKILL_BROWSER_EXE", str(executable))
    result = runtime.run_renderer(SCRIPTS / "render.py", ["--batch", str(deck)], timeout=20)
    assert result.returncode != 0
    assert "libMissing.so" in result.stderr
    assert attempts.read_text() == "launch\n"


def test_repeated_cancel_reaps_real_browser_before_next_admission(runtime, deck, tmp_path, monkeypatch):
    import psutil
    html = deck / "slides/slide_01.html"
    html.write_text("<html><script>while(true){}</script></html>")
    locks = tmp_path / "locks"
    monkeypatch.setenv("RENDER_GLOBAL_LIMIT", "1")
    monkeypatch.setenv("RENDER_LOCK_DIR", str(locks))
    env = dict(os.environ, RENDER_JOB_TIMEOUT="30")
    parent = subprocess.Popen([sys.executable, str(SCRIPTS / "render.py"), "--batch", str(deck)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=True)
    observed = {}
    try:
        limit = time.monotonic() + 10
        while time.monotonic() < limit:
            assert parent.poll() is None
            children = psutil.Process(parent.pid).children(recursive=True)
            for child in children:
                try:
                    observed[(child.pid, child.create_time())] = child
                except psutil.NoSuchProcess:
                    pass
            if any("chrome" in child.name().lower() for child in children if child.is_running()):
                break
            time.sleep(0.05)
        assert any("chrome" in child.name().lower() for child in observed.values() if child.is_running())
        parent.send_signal(signal.SIGTERM)
        time.sleep(0.02)
        if parent.poll() is None:
            parent.send_signal(signal.SIGTERM)
        stdout, stderr = parent.communicate(timeout=8)
        assert parent.returncode == 130, (stdout, stderr)
        assert not [identity.pid for identity in observed.values() if _process_alive(identity)]
        fd = runtime._acquire_render_slot(time.monotonic() + 1)
        runtime._release_render_slot(fd)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.communicate(timeout=5)


def test_repeated_jobs_release_descriptors_and_preserve_another_browser(runtime, deck):
    import psutil
    from playwright.sync_api import sync_playwright
    process = psutil.Process()
    with sync_playwright() as playwright:
        other_browser = playwright.chromium.launch()
        other_page = other_browser.new_page()
        other_page.set_content("<title>independent browser</title>")
        before = process.num_fds()
        try:
            for _ in range(4):
                result = runtime.run_renderer(SCRIPTS / "render.py", [str(deck / "slides/slide_01.html"),
                    str(deck / "renders/slide_01.png")], timeout=30)
                assert result.returncode == 0, result.stderr
                assert other_page.title() == "independent browser"
                assert process.num_fds() <= before + 1
            (deck / "slides/slide_01.html").write_text("<html><script>while(true){}</script></html>")
            failed = runtime.run_renderer(SCRIPTS / "render.py", ["--batch", str(deck)], timeout=3)
            assert failed.returncode != 0
            assert "timed out" in failed.stderr
            assert other_page.title() == "independent browser"
            assert process.num_fds() <= before + 1
        finally:
            other_browser.close()
