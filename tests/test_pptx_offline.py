"""Offline PPT authoring uses local media and exports without network access."""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from tests.pptx_test_support import skip_unavailable_pptx_runtime
from tests.test_pptx_web_image_search import _load_localize_module


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "box_agent/skills/document-skills/pptx"
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node.js is required")


def run(root, script, *args, env=None):
    return subprocess.run(
        [NODE, str(SKILL / "scripts" / script), *map(str, args)],
        cwd=root, env=env, capture_output=True, text=True, check=False,
    )


def scaffold(root, layout="cover-hero-v1", *args):
    result = run(root, "inspect_deck_contract.js", layout, "--title",
                 "Neymar Barcelona documentary portrait", "--image-mode", "local_only",
                 "--out", "deck.json", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((root / "assets/generated/manifest.json").read_text())


def test_offline_cover_does_not_schedule_search_or_generation(tmp_path):
    manifest = scaffold(tmp_path)
    entry = manifest["image_plan"][0]
    assert manifest["mode"] == "local_only"
    assert entry["decision"] == "skip"
    assert entry["required"] is False
    assert entry["acquire_via"] == "none"
    assert "search" not in entry
    assert "generate" not in entry["allowed_strategies"]
    result = run(tmp_path, "validate_image_manifest.js", "assets/generated/manifest.json")
    assert result.returncode == 0, result.stdout + result.stderr


def test_offline_required_image_preserves_a_supplied_local_asset(tmp_path):
    source = tmp_path / "provided.png"
    Image.new("RGB", (800, 600), "navy").save(source)
    manifest = scaffold(tmp_path, "image-hero-split-v1", "--image-asset", f"1:image={source}")
    entry = manifest["image_plan"][0]
    assert entry["decision"] == "use_existing"
    assert entry["acquire_via"] == "user"
    assert entry["required"] is True
    assert (tmp_path / entry["output_path"]).read_bytes() == source.read_bytes()
    result = run(tmp_path, "validate_image_manifest.js", "assets/generated/manifest.json")
    assert result.returncode == 0, result.stdout + result.stderr


def test_offline_missing_required_asset_is_reported_without_online_retry(tmp_path):
    manifest = scaffold(tmp_path, "image-hero-split-v1")
    entry = manifest["image_plan"][0]
    assert entry["decision"] == "skip"
    assert entry["required"] is True
    assert entry["prompt"] == ""
    result = run(tmp_path, "validate_image_manifest.js", "assets/generated/manifest.json")
    assert result.returncode != 0
    assert "bind a supplied local asset or revise the design" in result.stdout + result.stderr


def test_offline_image_import_rejects_download_before_request(tmp_path, monkeypatch):
    scaffold(tmp_path)
    module = _load_localize_module()
    monkeypatch.setattr(module.requests, "get", lambda *a, **kw: pytest.fail("Network request attempted"))
    with pytest.raises(ValueError, match="Offline package: image downloads are unavailable"):
        module.import_candidate(tmp_path / "assets/generated/manifest.json", tmp_path / "candidate.json")


@pytest.mark.parametrize("source", ["ai", "web"])
def test_offline_manifest_rejects_online_acquisition(tmp_path, source):
    manifest = scaffold(tmp_path)
    manifest["image_plan"][0].update(decision="generate", acquire_via=source)
    (tmp_path / "assets/generated/manifest.json").write_text(json.dumps(manifest))
    result = run(tmp_path, "validate_image_manifest.js", "assets/generated/manifest.json")
    assert result.returncode != 0
    assert "local_only requires" in result.stdout + result.stderr


def test_offline_finalizer_exports_pptx_with_browser_network_disabled(tmp_path):
    # Every renderer/exporter subprocess uses this host SDK. Actual external
    # requests are recorded even though the browser cannot send them.
    audit = tmp_path / "network-requests.txt"
    host = tmp_path / "offline-playwright.cjs"
    prefix = os.environ.get("BOX_AGENT_NODE_PREFIX") or os.environ.get("BOX_AGENT_RUNTIME_PREFIX")
    if not prefix:
        prefix = (Path.home() / "Library/Application Support/office-raccoon" if sys.platform == "darwin"
                  else Path(os.environ.get("APPDATA", Path.home())) / "office-raccoon" if sys.platform == "win32"
                  else Path.home() / ".config/office-raccoon")
    resolution = subprocess.run(
        [NODE, "-e", "console.log(require.resolve(process.argv[1], {paths: [process.argv[2]]}))",
         os.environ.get("BOX_AGENT_PLAYWRIGHT_MODULE_PATH", "playwright"), str(prefix)],
        capture_output=True, text=True,
    )
    if resolution.returncode:
        pytest.skip("Managed Playwright SDK is unavailable")
    host.write_text("const runtime = require(" + json.dumps(resolution.stdout.strip()) + ");\n" + """
const fs = require('fs');
const launch = runtime.chromium.launch.bind(runtime.chromium);
function observe(target) {
  target.on('request', request => {
    if (/^https?:/i.test(request.url())) {
      fs.appendFileSync(process.env.PPT_NETWORK_AUDIT, request.url() + '\\n');
    }
  });
  return target;
}
runtime.chromium.launch = async (...args) => {
  const browser = await launch(...args);
  const newPage = browser.newPage.bind(browser);
  browser.newPage = async options => observe(await newPage({...options, offline: true}));
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async options => observe(await newContext({...options, offline: true}));
  return browser;
};
module.exports = runtime;
""")
    env = {**os.environ, "BOX_AGENT_PLAYWRIGHT_MODULE_PATH": str(host),
           "PPT_NETWORK_AUDIT": str(audit)}
    deck = json.loads((SKILL / "examples/controlled-deck/deck.json").read_text())
    deck["slides"] = deck["slides"][:2]
    (tmp_path / "deck.json").write_text(json.dumps(deck))
    result = run(tmp_path, "finalize_controlled_deck.js", "deck.json",
                 "--out", "index.html", "--require-pptx", env=env)
    skip_unavailable_pptx_runtime(result)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "index.html").is_file()
    with zipfile.ZipFile(tmp_path / "index.pptx") as package:
        assert package.testzip() is None
        assert len([name for name in package.namelist()
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml")]) == 2
    assert not audit.exists() or not audit.read_text().strip()
