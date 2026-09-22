"""Producer-to-discovery contracts for presentation preview publication."""

import hashlib
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest
from PIL import Image

from box_agent.tools.engine.artifact_results import (
    _detect_tool_artifacts,
    _snapshot_workspace_signatures,
)


REPO = Path(__file__).resolve().parents[1]
STANDARD = REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts"


@pytest.mark.parametrize("all_pages", [False, True], ids=["single-page", "whole-deck"])
def test_dynamic_renderer_skips_marked_pages_and_observes_contact_sheet(
    tmp_path, monkeypatch, all_pages
):
    namespace = runpy.run_path(str(STANDARD.parents[1] / "sn-ppt-dazzle/scripts/render_deck.py"))
    renderer = namespace["DeckRenderer"](tmp_path / "deck.html", tmp_path)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 18), "white").save(buffer, format="PNG")
    sample = {"png": buffer.getvalue(), "std": 10, "blocks": (),
              "entrance_cells": None, "live_cells": None}
    monkeypatch.setattr(renderer, "_load", lambda *args: None)
    monkeypatch.setattr(renderer, "_signal", lambda: (0, 1))
    monkeypatch.setattr(renderer, "_sample_page", lambda **kwargs: sample)
    renderer._page = SimpleNamespace(wait_for_timeout=lambda *args: None)
    monkeypatch.setitem(renderer.render_all.__func__.__globals__, "MAX_PAGES", 1)
    if all_pages:
        renderer.render_all()
    else:
        renderer.render_page(1)
    events = _detect_tool_artifacts(
        "render", "bash", "[page_01.png]" + (" [contact_sheet.png]" if all_pages else ""), None, {},
        _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    assert [event.filename for event in events if event.kind == "image"] == (
        ["contact_sheet.png"] if all_pages else []
    )
    assert (tmp_path / "page_01.png").read_bytes() == sample["png"]


def test_review_contact_delivers_overview_and_marks_review_images_as_process(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.syspath_prepend(str(STANDARD))
    deck = runpy.run_path(str(STANDARD / "deck.py"))
    renderer = runpy.run_path(str(STANDARD / "render.py"))
    renders = tmp_path / "renders"
    renders.mkdir()
    for number in range(1, 9):
        target = renders / f"slide_{number:02d}.png"
        renderer["_mark_intermediate_artifact"](target)
        Image.new("RGB", (32, 18), "white").save(target)
    deck["_build_contact"](tmp_path, expected=8)
    deck["_build_contact"](tmp_path, expected=8, focus="5")
    events = _detect_tool_artifacts(
        "render", "bash", capsys.readouterr().out,
        None, {}, _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    assert [event.filename for event in events if event.kind == "image"] == ["contact-sheet.png"]
    assert len(list(renders.glob("slide_*.png"))) == 8
    assert (renders / "contact-sheet-review-01.png").is_file()
    assert (renders / "contact-sheet-focus.png").is_file()


def _old_bundle(tmp_path):
    sync = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    path = tmp_path / "bundle"
    path.mkdir()
    relative = "skills/sn-ppt-standard/scripts/render.py"
    target = path / relative
    target.parent.mkdir(parents=True)
    content = (
        "\ndef _ensure_browser_available(p):\n"
        '    override = os.environ.get("PPT_SKILL_BROWSER_EXE")\n'
        "    if override:\n"
        "        override = os.path.abspath(os.path.expanduser(override))\n"
        "        if not os.path.isfile(override):\n"
        '            raise BrowserUnavailable(f"PPT_SKILL_BROWSER_EXE 不存在: {override}")\n'
        "\ndef render(pg, out):\n"
        "        pg.screenshot(path=out)\n"
    ).encode()
    target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    # Model the bundle before publication markers, even when later overlays exist.
    publication_index = sync["OVERLAYS"].index("intermediate-render-artifacts")
    # Keep this historical incremental-update fixture scoped to publication;
    # later overlays may add helper files and require a complete source sync.
    sync["refresh_host_overlays"].__globals__["OVERLAYS"] = sync["OVERLAYS"][:publication_index + 1]
    record = {"name": sync["BUNDLE_NAME"], "revision": sync["PINNED_REVISION"],
              "overlays": sync["OVERLAYS"][:publication_index],
              "files": {relative: {"source_sha256": digest, "sha256": digest}}}
    (path / "source.json").write_text(json.dumps(record))
    return sync, path, target, digest


def test_offline_overlay_refresh_preserves_upstream_provenance_and_is_idempotent(tmp_path):
    sync, path, target, original = _old_bundle(tmp_path)
    first = sync["refresh_host_overlays"](path)
    second = sync["refresh_host_overlays"](path)
    assert first == second
    record = first["files"][target.relative_to(path).as_posix()]
    assert record["source_sha256"] == original
    assert record["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert record["sha256"] != original


def test_offline_overlay_refresh_rejects_dirty_bundle_before_writing(tmp_path):
    sync, path, target, _ = _old_bundle(tmp_path)
    target.write_text("local changes")
    before = (path / "source.json").read_bytes()
    with pytest.raises(ValueError, match="differs from its provenance"):
        sync["refresh_host_overlays"](path)
    assert target.read_text() == "local changes"
    assert (path / "source.json").read_bytes() == before
