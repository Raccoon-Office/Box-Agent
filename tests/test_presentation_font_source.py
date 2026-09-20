"""Uploaded fonts retain their original family through real subsetting and PPTX export.

Install the Standard skill's Python requirements for font checks. Node checks use
PPTX_TEST_NODE_MODULES pointing to an npm-ci install of its locked exporter deps;
only temporary exporter copies receive a node_modules symlink.
"""

import hashlib
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


REPO = Path(__file__).resolve().parents[1]
SUITE = REPO / "box_agent/skills/presentation-suite"
STANDARD = SUITE / "skills/sn-ppt-standard"


@pytest.fixture
def font_module(monkeypatch):
    pytest.importorskip("fontTools")
    monkeypatch.setenv("PPT_FONT_SOURCE_DIRS", str(SUITE / "fonts"))
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    return runpy.run_path(str(STANDARD / "scripts/font_bundle.py"))


def _upload(root, filename="BebasNeue-Regular.ttf", font_id="corporate", weight=400):
    source = root / "materials" / f"{font_id}.ttf"
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SUITE / "fonts" / filename, source)
    return {"id": font_id, "source_path": source.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "family": "Untrusted configured display name", "weight": weight}


def _config(root, fonts, roles=None, acknowledged=True):
    config = {"license_acknowledged": acknowledged, "fonts": fonts,
              "roles": roles or {"title": {"kind": "custom", "font_id": fonts[0]["id"]}}}
    (root / "materials/font-config.json").write_text(json.dumps(config))


def _bundle(root, module):
    pytest.importorskip("brotli", reason="Real WOFF2 subsetting requires fonttools[woff]")
    assert shutil.which("pyftsubset"), "Install the Standard skill's fonttools[woff] requirement"
    css = ":root {" + "".join(f"{token}: 'Noto Sans SC';" for token in module["DEFAULT_TOKENS"]) + "}"
    css = module["bundle_fonts"](root, css, ["<h1>Corporate Growth 2026</h1>"], scan_named_families=False)
    (root / "base.css").write_text(css)
    manifest = json.loads((root / "assets/fonts/manifest.json").read_text())
    return css, manifest


def test_upload_manifest_uses_original_name_table_and_keeps_registry_and_subset_alias(font_module, tmp_path):
    from fontTools.ttLib import TTFont

    upload = _upload(tmp_path)
    _config(tmp_path, [upload])
    css, manifest = _bundle(tmp_path, font_module)
    custom = next(face for face in manifest["faces"] if face["user_authorized"])
    assert custom["source_family"] == "Bebas Neue"
    assert manifest["token_families"]["--font-title"] == "User::corporate"
    assert custom["delivery_family"] == manifest["delivery_families"]["User::corporate"]
    assert custom["delivery_family"].startswith("Deck-")
    assert custom["delivery_family"] in css
    with TTFont(tmp_path / custom["path"]) as subset:
        assert subset["name"].getBestFamilyName() == custom["delivery_family"]
    assert custom["original_sha256"] == upload["sha256"]
    assert custom["source_path"] == upload["source_path"]
    assert custom["license"] == "user-provided"
    assert all(face["source_family"] == "Noto Sans SC" for face in manifest["faces"] if not face["user_authorized"])
    assert font_module["validate_font_bundle"](tmp_path) == []


@pytest.mark.parametrize("records, expected", [
    ([(1, 3, 1, 0x409, "Legacy Family"), (16, 3, 1, 0x409, "Typographic Family")], "Typographic Family"),
    ([(16, 3, 1, 0x804, "中文字体"), (16, 3, 1, 0x409, "English Family")], "English Family"),
    ([(16, 3, 1, 0x804, "中文字体")], "中文字体"),
    ([(16, 3, 1, 0x409, " "), (1, 3, 1, 0x409, "Legacy Family")], "Legacy Family"),
    ([(16, 3, 1, 0x409, "User::internal"), (1, 1, 0, 0, "Macintosh Family")], "Macintosh Family"),
    ([(16, 3, 1, 0x409, "Broken\ufffdName"), (1, 3, 1, 0x409, "Legacy Family")], "Legacy Family"),
])
def test_original_family_uses_valid_typographic_then_legacy_names(font_module, tmp_path, records, expected):
    from fontTools.ttLib import TTFont

    source = tmp_path / "misleading-filename.ttf"
    with TTFont(SUITE / "fonts/BebasNeue-Regular.ttf") as font:
        font["name"].names = []
        for name_id, platform, encoding, language, value in records:
            font["name"].setName(value, name_id, platform, encoding, language)
        font.save(source)
    metadata = font_module["_font_source_metadata"](str(source))
    assert metadata.get("source_family") == expected


@pytest.mark.parametrize("invalid", [None, "", "User::corporate", " deck-private ", "\ufffd", "\x00Font", "---"])
def test_original_font_without_usable_family_is_rejected(font_module, tmp_path, invalid):
    from fontTools.ttLib import TTFont

    source = tmp_path / "corporate.ttf"
    with TTFont(SUITE / "fonts/BebasNeue-Regular.ttf") as font:
        if invalid is None:
            del font["name"]
        else:
            font["name"].names = []
            font["name"].setName(invalid, 1, 3, 1, 0x409)
        font.save(source)
    with pytest.raises(ValueError, match="(?i)source font.*family.*name table"):
        font_module["_font_source_metadata"](str(source))


def test_upload_family_validation_preserves_authorization_hash_and_path_checks(font_module, tmp_path):
    upload = _upload(tmp_path)
    for updates, acknowledged, error in [
        ({}, False, "authorization"),
        ({"sha256": "0" * 64}, True, "hash mismatch"),
        ({"source_path": "../../outside.ttf"}, True, "escapes workspace"),
    ]:
        _config(tmp_path, [{**upload, **updates}], acknowledged=acknowledged)
        with pytest.raises(ValueError, match=error):
            font_module["_load_custom_config"](tmp_path)


def test_multiple_uploaded_weights_keep_same_original_family(font_module, tmp_path):
    uploads = [_upload(tmp_path, "IBMPlexMono-Regular.ttf", "regular", 400),
               _upload(tmp_path, "IBMPlexMono-SemiBold.ttf", "semibold", 600)]
    _config(tmp_path, uploads, {"title": {"kind": "custom", "font_id": "semibold"},
                              "body": {"kind": "custom", "font_id": "regular"}})
    _, manifest = _bundle(tmp_path, font_module)
    custom = [face for face in manifest["faces"] if face["user_authorized"]]
    assert {face["source_family"] for face in custom} == {"IBM Plex Mono"}
    assert {face["weight"] for face in custom} == {"400", "600"}
    assert len({face["delivery_family"] for face in custom}) == 2


@pytest.mark.parametrize("family", [None, "", " User::corporate ", "Deck-old-subset", "\ufffd", "\x00Font", "---"])
def test_validation_rejects_legacy_bad_source_family_before_export(font_module, tmp_path, family):
    _config(tmp_path, [_upload(tmp_path)])
    _, manifest = _bundle(tmp_path, font_module)
    custom = next(face for face in manifest["faces"] if face["user_authorized"])
    custom["source_family"] = family
    (tmp_path / "assets/fonts/manifest.json").write_text(json.dumps(manifest))
    errors = font_module["validate_font_bundle"](tmp_path)
    assert any("source font family" in error and "rebuild" in error for error in errors), errors


@pytest.fixture
def node_exporter(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node is required for the actual PPTX export regression")
    installed = os.environ.get("PPTX_TEST_NODE_MODULES")
    if not installed:
        pytest.skip("Set PPTX_TEST_NODE_MODULES to the exporter's locked npm-ci node_modules")
    dependencies = Path(installed).resolve()
    lock = json.loads((STANDARD / "scripts/export_pptx/package-lock.json").read_text())
    for name in ("pptxgenjs", "jszip", "playwright"):
        actual = json.loads((dependencies / name / "package.json").read_text())["version"]
        assert actual == lock["packages"][f"node_modules/{name}"]["version"]
    exporter = tmp_path / "exporter"
    shutil.copytree(STANDARD / "scripts/export_pptx/lib", exporter / "lib")
    (exporter / "node_modules").symlink_to(dependencies, target_is_directory=True)
    runner = exporter / "font-regression.mjs"
    runner.write_text("""
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { buildPptx } from './lib/pptx_builder.mjs';
const [deck, primary, rejected] = process.argv.slice(2);
const styles = { color: 'rgb(0, 0, 0)', fontSize: '32px', fontFamily: `"${primary}", sans-serif` };
const ir = { canvasWidth: 1600, canvasHeight: 900, ct: {
  tag: 'DIV', bounds: { x: 80, y: 80, w: 900, h: 150 }, styles, text: 'Corporate Growth 2026',
  textRuns: [{ text: 'Corporate Growth ', ...styles },
             { text: '2026', ...styles, fontFamily: 'Arial', fontWeight: '700' }],
} };
const original = JSON.stringify(ir);
const output = `${deck}/fonts.pptx`;
if (rejected === 'yes') {
  await assert.rejects(() => buildPptx([{ path: 'slide_01.html', ir }], deck, output), /font.*rebuild|rebuild.*font/i);
  assert.equal(existsSync(output), false);
} else {
  const result = await buildPptx([{ path: 'slide_01.html', ir }], deck, output);
  assert.equal(result.successCount, 1);
  assert.equal(result.failCount, 0);
}
assert.equal(JSON.stringify(ir), original, 'export must not mutate browser IR');
""")
    return runner


def _export(runner, root, primary, reject=False):
    result = subprocess.run(["node", str(runner), str(root), primary, "yes" if reject else "no"],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    if reject:
        return set()
    with zipfile.ZipFile(root / "fonts.pptx") as pptx:
        for name in pptx.namelist():
            if name.endswith(".xml"):
                ET.fromstring(pptx.read(name))
        xml = ET.fromstring(pptx.read("ppt/slides/slide1.xml"))
    return {node.attrib["typeface"] for node in xml.iter() if "typeface" in node.attrib}


def test_real_uploaded_subset_exports_original_family_and_preserves_ir(font_module, node_exporter, tmp_path):
    upload = _upload(tmp_path)
    _config(tmp_path, [upload])
    _, manifest = _bundle(tmp_path, font_module)
    custom = next(face for face in manifest["faces"] if face["user_authorized"])
    families = _export(node_exporter, tmp_path, custom["delivery_family"])
    assert "Bebas Neue" in families
    assert "Arial" in families
    assert not any(name.startswith(("User::", "Deck-")) for name in families)


@pytest.mark.parametrize("family", ["User::corporate", "Deck-old-subset", " user::corporate ", "", None])
def test_export_rejects_legacy_invalid_source_family_with_rebuild_error(node_exporter, tmp_path, family):
    fonts = tmp_path / "assets/fonts"
    fonts.mkdir(parents=True)
    (fonts / "manifest.json").write_text(json.dumps({"faces": [
        {"delivery_family": "Deck-test-font", "source_family": family}]}))
    _export(node_exporter, tmp_path, "Deck-test-font", reject=True)


@pytest.mark.parametrize("family", ["Noto Sans SC", "Xiaolai", "Caveat", "Corporate, Inc", 'Corporate "Display"', r"Corporate\Display", "Corporate & Co"])
def test_export_preserves_valid_source_family_exactly(node_exporter, tmp_path, family):
    fonts = tmp_path / "assets/fonts"
    fonts.mkdir(parents=True)
    (fonts / "manifest.json").write_text(json.dumps({"faces": [
        {"delivery_family": "Deck-test-font", "source_family": family}]}))
    families = _export(node_exporter, tmp_path, "Deck-test-font")
    assert family in families
    assert "Arial" in families
    assert "Deck-test-font" not in families


@pytest.mark.parametrize("manifest", ["{broken", '{"faces":{}}', '{}'])
def test_export_rejects_existing_broken_font_manifest(node_exporter, tmp_path, manifest):
    fonts = tmp_path / "assets/fonts"
    fonts.mkdir(parents=True)
    (fonts / "manifest.json").write_text(manifest)
    _export(node_exporter, tmp_path, "Deck-test-font", reject=True)


def test_export_rejects_unmapped_private_alias_but_keeps_plain_fonts(node_exporter, tmp_path):
    assert "Arial" in _export(node_exporter, tmp_path, "Arial")
    (tmp_path / "fonts.pptx").unlink()
    _export(node_exporter, tmp_path, "Deck-missing", reject=True)
