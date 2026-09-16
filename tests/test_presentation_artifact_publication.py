"""Producer-to-discovery contracts for presentation preview publication."""

import hashlib
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest
from PIL import Image

from box_agent.tools.engine.artifact_results import _detect_tool_artifacts, _snapshot_workspace_signatures


REPO = Path(__file__).resolve().parents[1]
STANDARD = REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts"


@pytest.mark.parametrize("all_pages", [False, True], ids=["single-page", "whole-deck"])
def test_dynamic_renderer_keeps_pages_private_and_publishes_contact_sheet(tmp_path, monkeypatch, all_pages):
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
        "render", "bash", "[page_01.png]", None, {},
        _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    assert {event.filename for event in events if event.kind == "image"} == (
        {"contact_sheet.png"} if all_pages else set()
    )
    assert (tmp_path / "page_01.png").read_bytes() == sample["png"]


def test_review_contact_publishes_only_whole_deck_overview(tmp_path, monkeypatch):
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
        "render", "bash", "[renders/slide_01.png] [renders/contact-sheet-review-01.png]",
        None, {}, _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    assert {event.filename for event in events if event.kind == "image"} == {"contact-sheet.png"}
    assert len(list(renders.glob("slide_*.png"))) == 8
    assert (renders / "contact-sheet-review-01.png").is_file()
    assert (renders / "contact-sheet-focus.png").is_file()


def _old_bundle(tmp_path, *, first_missing="intermediate-render-artifacts"):
    sync = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    path = tmp_path / "bundle"
    path.mkdir()
    relative = "skills/sn-ppt-standard/scripts/render.py"
    target = path / relative
    target.parent.mkdir(parents=True)
    content = b'\ndef render(pg, out):\n        pg.screenshot(path=out)\n'
    target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    record = {"name": sync["BUNDLE_NAME"], "revision": sync["PINNED_REVISION"],
              "overlays": sync["OVERLAYS"][:sync["OVERLAYS"].index(first_missing)],
              "files": {relative: {"source_sha256": digest, "sha256": digest}}}
    (path / "source.json").write_text(json.dumps(record))
    return sync, path, target, digest


def _artifact_only_refresh_target(sync, monkeypatch):
    # Exercise the historical append-only upgrade in isolation. Later overlays
    # require the pinned upstream checkout and are tested with the current,
    # unmodified target list below; they must not enter the incremental allowlist.
    artifact = sync["OVERLAYS"].index("intermediate-render-artifacts")
    target = sync["OVERLAYS"][:artifact + 1]
    monkeypatch.setitem(sync["refresh_host_overlays"].__globals__, "OVERLAYS", target)
    return target


def test_offline_overlay_refresh_preserves_upstream_provenance_and_is_idempotent(tmp_path, monkeypatch):
    sync, path, target, original = _old_bundle(tmp_path)
    expected_overlays = _artifact_only_refresh_target(sync, monkeypatch)
    first = sync["refresh_host_overlays"](path)
    first_bytes = {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    second = sync["refresh_host_overlays"](path)
    assert first == second
    assert first["overlays"] == expected_overlays
    assert first_bytes == {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    assert b"_mark_intermediate_artifact(out)\n        pg.screenshot(path=out)" in target.read_bytes()
    record = first["files"][target.relative_to(path).as_posix()]
    assert record["source_sha256"] == original
    assert record["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert record["sha256"] != original


def test_offline_overlay_refresh_rejects_dirty_bundle_before_writing(tmp_path, monkeypatch):
    sync, path, target, _ = _old_bundle(tmp_path)
    _artifact_only_refresh_target(sync, monkeypatch)
    target.write_text("local changes")
    before = (path / "source.json").read_bytes()
    with pytest.raises(ValueError, match="differs from its provenance"):
        sync["refresh_host_overlays"](path)
    assert target.read_text() == "local changes"
    assert (path / "source.json").read_bytes() == before


@pytest.mark.parametrize("first_missing", [
    "intermediate-render-artifacts",
    "semantic-dynamic-presentation-scope",
    "dazzle-output-choice-precedence",
])
def test_current_nonincremental_overlays_require_full_sync_without_partial_writes(tmp_path, first_missing):
    sync, path, _, _ = _old_bundle(tmp_path, first_missing=first_missing)
    # Unlike the historical append-only tests, the production overlay target is
    # deliberately untouched. Even an eligible first overlay cannot be applied
    # when completing the upgrade would require a nonincremental transformation.
    before = {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="requires a full sync from the pinned upstream checkout"):
        sync["refresh_host_overlays"](path)
    assert before == {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}
