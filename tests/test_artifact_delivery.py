from pathlib import Path
import runpy
import shutil
import subprocess

import pytest

from box_agent.artifact_publication import delivery_scope, apply_delivery_policy
from box_agent.artifact_publication import write_metadata
from box_agent.tools.engine.artifact_results import _detect_tool_artifacts, _snapshot_workspace_signatures

REPO = Path(__file__).resolve().parents[1]
FAST = REPO / "box_agent/skills/document-skills/pptx/scripts"
STANDARD = REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts"


def published(root):
    return {event.filename for event in _detect_tool_artifacts(
        "call", "bash", "", None, {}, _snapshot_workspace_signatures(str(root)), str(root))}


def test_scope_is_explicit_local_and_has_exact_delivery_membership(tmp_path):
    root = tmp_path / "unconventional-name"
    root.mkdir()
    (root / ".artifact-delivery.json").write_text('{"schema_version":1,"default":"intermediate"}')
    paths = [root / "renamed.png", root / "ordinary.html", root / "whole-deck.png", tmp_path / "standalone.png"]
    for file in paths:
        file.write_bytes(file.name.encode())
    write_metadata(paths[2], {"type": "artifact"})
    assert published(tmp_path) == {"whole-deck.png", "standalone.png"}
    assert delivery_scope(paths[0], root) == root
    assert delivery_scope(paths[3], root) is None
    assert delivery_scope(root, root) is None
    assert apply_delivery_policy({"type": "artifact", "path": "unconventional-name/renamed.png"}, str(tmp_path))["type"] == "intermediate_asset"


def test_scoped_work_file_stays_private_after_copy_outside_deck(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    (root / ".artifact-delivery.json").write_text('{"schema_version":1,"default":"intermediate"}')
    (root / "figure.png").write_bytes(b"private picture")
    assert published(tmp_path) == set()
    shutil.copyfile(root / "figure.png", tmp_path / "renamed.png")
    assert published(tmp_path) == set()
    write_metadata(tmp_path / "renamed.png", {"type": "artifact"})
    assert published(tmp_path) == {"renamed.png"}


def test_fast_producer_declaration_and_explicit_deliveries(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    code = "const d=require(process.argv[1]),fs=require('fs'),p=require('path'),r=process.argv[2];" \
           "d.declareScope(r); for(const f of ['draft.html','image.png','index.html','final.pptx','overview.png']) fs.writeFileSync(p.join(r,f),f);" \
           "for(const f of ['index.html','final.pptx','overview.png']) d.publishArtifact(p.join(r,f));"
    subprocess.run([node, "-e", code, str(FAST / "artifact_delivery.js"), str(tmp_path)], check=True)
    assert published(tmp_path) == {"index.html", "final.pptx", "overview.png"}
    before = _snapshot_workspace_signatures(str(tmp_path))
    result = subprocess.run([node, str(FAST / "artifact_delivery.js"), "publish", str(tmp_path / "image.png")],
                            check=True, capture_output=True, text=True)
    events = _detect_tool_artifacts("publish", "bash", result.stdout, None, before,
                                    _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path))
    assert [event.filename for event in events] == ["image.png"]


def test_standard_prepare_declares_scope_and_build_registers_final_deliveries(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(STANDARD))
    main = runpy.run_path(str(STANDARD / "deck.py"))["main"]
    state = main.__globals__

    def prepare(root, expected):
        assert delivery_scope(root / "assets/hero.png", root) == root
        (root / "hero.png").write_bytes(b"hero")
        (root / "contact-sheet-assets.png").write_bytes(b"working sheet")

    monkeypatch.setitem(state, "_prepare_workspace", prepare)
    assert main(["prepare", str(tmp_path)]) == 0
    assert published(tmp_path) == set()
    for name in ["_ensure_canvas_reset", "_ensure_runtime_assets", "_normalize_runtime_references",
                 "_validate_no_pictographs", "_validate_image_presentations", "_sync_speech",
                 "_validate_referenced_assets", "_validate_render_quality", "_validate_runtime_dependencies"]:
        monkeypatch.setitem(state, name, lambda *args, **kwargs: None)

    def build(root, expected):
        (root / "present.html").write_text("finished deck")
        return 0

    def contact(root, expected):
        (root / "renders").mkdir(exist_ok=True)
        (root / "renders/contact-sheet.png").write_bytes(b"final overview")

    monkeypatch.setitem(state, "_build_player", build)
    monkeypatch.setitem(state, "_build_contact", contact)
    assert main(["build", str(tmp_path)]) == 0
    assert published(tmp_path) == {"present.html", "contact-sheet.png"}


def test_standard_failed_build_does_not_publish_partial_html(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(STANDARD))
    main = runpy.run_path(str(STANDARD / "deck.py"))["main"]
    state = main.__globals__
    for name in ["_ensure_canvas_reset", "_ensure_runtime_assets", "_normalize_runtime_references",
                 "_validate_no_pictographs", "_validate_image_presentations", "_sync_speech",
                 "_validate_referenced_assets", "_validate_render_quality"]:
        monkeypatch.setitem(state, name, lambda *args, **kwargs: None)

    def build(root, expected):
        (root / "present.html").write_text("partial deck")
        return 1

    monkeypatch.setitem(state, "_build_player", build)
    assert main(["build", str(tmp_path)]) == 1
    assert published(tmp_path) == set()


def test_standard_asset_contact_is_private_until_explicitly_requested(tmp_path, monkeypatch, capsys):
    from PIL import Image

    monkeypatch.syspath_prepend(str(STANDARD))
    main = runpy.run_path(str(STANDARD / "deck.py"))["main"]
    (tmp_path / "assets").mkdir()
    Image.new("RGB", (32, 18), "orange").save(tmp_path / "assets/hero.png")
    assert main(["asset-register", str(tmp_path), "--path", "assets/hero.png", "--origin", "generated",
                 "--generator-model", "test-model", "--prompt", "travel illustration"]) == 0
    assert main(["asset-assign", str(tmp_path), "--path", "assets/hero.png", "--asset-id", "hero", "--group-id", "cover"]) == 0
    assert main(["asset-contact", str(tmp_path), "--group-id", "cover"]) == 0
    assert (tmp_path / "assets/contact-sheet-cover.png").is_file()
    assert published(tmp_path) == set()
    capsys.readouterr()
    before = _snapshot_workspace_signatures(str(tmp_path))
    assert main(["publish", str(tmp_path), "--path", "assets/hero.png"]) == 0
    events = _detect_tool_artifacts("publish", "bash", capsys.readouterr().out, None, before,
                                    _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path))
    assert [event.filename for event in events] == ["hero.png"]
    assert published(tmp_path) == {"hero.png"}


def test_standard_cannot_publish_files_outside_its_scope(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(STANDARD))
    main = runpy.run_path(str(STANDARD / "deck.py"))["main"]
    (tmp_path / "outside.png").write_bytes(b"outside")
    root = tmp_path / "deck"
    root.mkdir()
    assert main(["publish", str(root), "--path", "../outside.png"]) == 1
    assert not (tmp_path / ".outside.png.artifact.json").exists()


@pytest.mark.parametrize("deliver", [False, True])
def test_contact_sheet_requires_explicit_delivery_registration(tmp_path, deliver):
    from PIL import Image
    from tests.pptx_test_support import skip_unavailable_pptx_runtime

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    (tmp_path / ".artifact-delivery.json").write_text('{"schema_version":1,"default":"intermediate"}')
    images = tmp_path / "pages"
    images.mkdir()
    for n, color in enumerate(["red", "blue"]):
        Image.new("RGB", (32, 18), color).save(images / f"slide-{n}.png")
    out = tmp_path / "qa/contact.png"
    result = subprocess.run([node, str(FAST / "make_contact_sheet.js"), str(images), "--out", str(out),
                             *(["--publish-artifact"] if deliver else [])], capture_output=True, text=True)
    skip_unavailable_pptx_runtime(result)
    assert result.returncode == 0, result.stderr
    assert out.is_file()
    assert published(tmp_path) == ({"contact.png"} if deliver else set())
