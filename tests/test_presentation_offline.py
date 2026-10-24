"""Offline design-mode boundaries and local static/dynamic delivery."""

import json
import os
import runpy
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import pytest

from tests.test_presentation_review_contract import check, deck  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "box_agent/skills/presentation-suite/skills"
STANDARD = SKILLS / "sn-ppt-standard"
PARTIAL = "## Final review contract\nstatus: partial\nfinal_pixels_inspected: no\nremaining: visual_unverified\n"


@pytest.mark.parametrize("script", ["web_search.py", "image_search.py", "image_generate.py", "fetch_image.py", "serper_images.py"])
def test_offline_tools_return_unavailable_without_network(script, monkeypatch, capsys):
    tools = SKILLS / "sn-ppt-tools/scripts"
    monkeypatch.syspath_prepend(str(tools))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: pytest.fail("Network request attempted"))
    path = STANDARD / "scripts" / script if script == "serper_images.py" else tools / script
    namespace = runpy.run_path(str(path))
    assert namespace["main"]() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unavailable"
    assert result["reason"] == "offline_package"


def test_offline_partial_export_discloses_missing_vision(deck):
    (deck / "_trace/review-issues.md").write_text(PARTIAL)
    result = check(deck)
    assert result.returncode == 0, result.stderr
    assert "visual_unverified" in result.stderr


@pytest.mark.parametrize("remaining", ["slide 1 overflows", "visual_unverified, missing data", "none"])
def test_offline_partial_export_does_not_hide_other_failures(deck, remaining):
    (deck / "_trace/review-issues.md").write_text(PARTIAL.replace("remaining: visual_unverified", f"remaining: {remaining}"))
    result = check(deck)
    assert result.returncode != 0


@pytest.mark.parametrize("resource", [
    '<img src="https://example.invalid/image.png">',
    '<script src="//example.invalid/library.js"></script>',
    '<link href="https://example.invalid/font.css" rel="stylesheet">',
    '<style>body{background:url(https://example.invalid/image.png)}</style>',
])
def test_offline_export_rejects_remote_dependencies_without_downloading(tmp_path, resource):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    page = tmp_path / "page.html"
    page.write_text(resource)
    module = STANDARD / "scripts/export_pptx/lib/image_downloader.mjs"
    result = subprocess.run([node, "--input-type=module", "-e", """
import http from 'node:http';
import https from 'node:https';
http.get = https.get = () => { throw new Error('NETWORK_ATTEMPT'); };
const {downloadRemoteImages} = await import(process.argv[1]);
await downloadRemoteImages(process.argv[2], [process.argv[3]]);
""", module.as_uri(), str(tmp_path), str(page)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Offline package: replace remote resources" in result.stderr
    assert "NETWORK_ATTEMPT" not in result.stderr
    assert page.read_text() == resource
    assert not (tmp_path / "images").exists()


def test_offline_overlay_refresh_is_repeatable_and_preserves_provenance(tmp_path):
    sync = runpy.run_path(str(ROOT / "scripts/sync_presentation_suite.py"))
    bundle = tmp_path / "bundle"
    shutil.copytree(SKILLS.parent, bundle, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    before = (bundle / "source.json").read_bytes()
    sync["refresh_host_overlays"](bundle)
    assert (bundle / "source.json").read_bytes() == before
    assert "desktop-offline-package" in json.loads(before)["overlays"]


def test_missing_offline_export_dependency_fails_without_installing(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    guard = tmp_path / "no-install.cjs"
    guard.write_text("""
const cp = require('node:child_process');
const fs = require('node:fs');
for (const key of ['exec', 'execSync', 'spawn', 'spawnSync']) {
  cp[key] = () => {
    fs.writeFileSync(process.env.PPT_INSTALL_AUDIT, key);
    throw new Error('INSTALL_ATTEMPT');
  };
}
""")
    audit = tmp_path / "installer-attempt.txt"
    env = {**os.environ, "BOX_AGENT_PLAYWRIGHT_MODULE_PATH": str(tmp_path / "absent-sdk.cjs"),
           "PPT_INSTALL_AUDIT": str(audit)}
    exporter = STANDARD / "scripts/export_pptx/html_to_pptx.mjs"
    result = subprocess.run([node, "--require", str(guard), str(exporter), "--deck-dir", str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt["reason"] == "export_dependencies_unavailable"
    assert "Offline package incomplete" in receipt["detail"]
    assert not audit.exists()
    assert not (tmp_path / "node_modules").exists()


@pytest.mark.skipif(os.environ.get("PPT_RENDER_BROWSER_TESTS") != "1", reason="requires provisioned local renderer")
def test_offline_static_pptx_and_dynamic_html_render_locally(tmp_path):
    node = shutil.which("node")
    assert node
    slides = tmp_path / "slides"
    slides.mkdir()
    (tmp_path / "_trace").mkdir()
    (tmp_path / "_trace/review-issues.md").write_text(PARTIAL)
    html = '''<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:white;color:#111}
.slide{width:1280px;height:720px;display:grid;place-items:center;font:48px Arial}
.dot{width:40px;height:40px;background:navy;animation:pulse 1s infinite alternate}
@keyframes pulse{to{transform:translateX(100px)}}
</style></head><body><section class="slide active"><h1>Offline local deck</h1><div class="dot"></div></section></body></html>'''
    (slides / "slide_01.html").write_text(html)
    exporter = STANDARD / "scripts/export_pptx/html_to_pptx.mjs"
    result = subprocess.run([node, str(exporter), "--deck-dir", str(tmp_path),
                             "--output", "offline.pptx"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "visual_unverified" in result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    with zipfile.ZipFile(receipt["output"]) as package:
        assert package.testzip() is None
        assert b"Offline local deck" in package.read("ppt/slides/slide1.xml")
    dynamic = tmp_path / "deck.html"
    dynamic.write_text(html)
    renderer = SKILLS / "sn-ppt-dazzle/scripts/render_deck.py"
    result = subprocess.run([sys.executable, str(renderer), str(dynamic), str(tmp_path / "renders"),
                             "--page", "1", "--no-motion-check"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((tmp_path / "renders").glob("*.png"))
