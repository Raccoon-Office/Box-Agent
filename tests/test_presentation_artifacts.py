import hashlib
import io
import json
from pathlib import Path
import runpy

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "box_agent/skills/pptx/scripts"))

from tests.test_pptx_design_plan import design_case, run

from presentation_artifacts import inspect_existing


@pytest.mark.parametrize("changed", ["outline.json", "design_input.json"])
def test_real_fallback_writer_binds_current_content_inputs(design_case, changed):
    root, outline, _, _ = design_case
    prepared = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert prepared.returncode == 0, prepared.stderr
    before = inspect_existing(root, revision="same-pages:1", dynamic=False,
                              required_formats=["html"], expected_pages=3)
    assert before["status"] == "complete", before
    path = root / changed
    value = json.loads(path.read_text())
    if changed == "outline.json":
        value["slides"][0]["bullets"][0] = "Corrected fact: revenue is 99, not 42"
    else:
        value["source_text"] = "Corrected source: revenue is 99, not 42"
    path.write_text(json.dumps(value))
    after = inspect_existing(root, revision="same-pages:2", dynamic=False,
                             required_formats=["html"], expected_pages=3)
    assert after["status"] != "complete", after


@pytest.mark.parametrize("stage", ["prepare", "accept"])
def test_real_fallback_writer_preserves_custom_input_paths(design_case, stage):
    root, outline, _, _ = design_case
    outline["tone"] = "New custom-path content"
    write(root / "custom-outline.json", outline)
    prepared = run("design_plan.js", "prepare", "custom-outline.json", "--out", "metadata/input.json", cwd=root)
    assert prepared.returncode == 0, prepared.stderr
    if stage == "accept":
        accepted = run("design_plan.js", "accept", "metadata/input.json", cwd=root)
        assert accepted.returncode == 0, accepted.stderr
    result = inspect_existing(root, revision="custom:1", dynamic=False,
                              required_formats=["html"], expected_pages=3)
    assert result["status"] == "complete", result
    report = json.loads((root / "qa/design_delivery.json").read_text())
    assert Path(report["content_inputs"]["outline"]["path"]) == root / "custom-outline.json"
    assert Path(report["content_inputs"]["design_input"]["path"]) == root / "metadata/input.json"
    outline["slides"][0]["message"] = "Same pages, corrected fact"
    write(root / "custom-outline.json", outline)
    assert inspect_existing(root, revision="custom:2", dynamic=False,
                            required_formats=["html"], expected_pages=3)["status"] != "complete"


def test_real_dynamic_writer_accepts_relative_cli_paths(tmp_path, monkeypatch):
    from PIL import Image
    from types import SimpleNamespace

    root = tmp_path / "deck"
    (root / "shots").mkdir(parents=True)
    write(root / "deck.html", '<html><section class="slide">Content</section></html>')
    script = Path(__file__).resolve().parents[1] / "box_agent/skills/presentation-suite/skills/sn-ppt-dazzle/scripts/render_deck.py"
    module = runpy.run_path(str(script))
    monkeypatch.chdir(tmp_path)
    renderer = module["DeckRenderer"](Path("deck/deck.html"), Path("deck/shots"), motion_check=False)
    image = io.BytesIO()
    Image.new("RGB", (128, 72), "green").save(image, format="PNG")
    # Only browser sampling is replaced; render_all's real PNG/metadata writers
    # and finalize are exercised with the documented relative CLI paths.
    monkeypatch.setattr(renderer, "_load", lambda *args: None)
    monkeypatch.setattr(renderer, "_signal", lambda: (0, 1))
    monkeypatch.setattr(renderer, "_sample_page", lambda **kw: {
        "png": image.getvalue(), "std": 10, "blocks": module["_img_stats"](image.getvalue())[1],
        "entrance_cells": None, "live_cells": None,
    })
    renderer._page = SimpleNamespace(wait_for_timeout=lambda *args: None,
                                    keyboard=SimpleNamespace(press=lambda *args: None),
                                    evaluate=lambda *args: "0:1", screenshot=lambda **kw: image.getvalue())
    renderer.render_all()
    renderer.finalize()
    result = inspect_existing(root, revision="relative:1", dynamic=True,
                              required_formats=["html"], expected_pages=1)
    assert result["status"] == "complete", result


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)


def test_fast_qa_from_another_html_cannot_sign_new_revision(tmp_path):
    write(tmp_path / "index.html", '<html><section class="slide">New content</section></html>')
    for name in ("deck_spec", "truth_check", "html_self_check", "runtime_probe"):
        write(tmp_path / f"qa/{name}.json", {"ok": True})
    result = inspect_existing(tmp_path, revision="new:1", dynamic=False,
                              required_formats=["html"], expected_pages=1)
    assert result["status"] != "complete"


def test_dynamic_cannot_reuse_another_decks_manifest_or_missing_pngs(tmp_path):
    write(tmp_path / "deck.html", '<html><section class="slide">Content</section></html>')
    write(tmp_path / "shots/render.json", {"deck": "/reference/deck.html", "n_pages": 1,
        "pages": [{"page": 1, "png": "/reference/page_01.png", "blank": False}]})
    result = inspect_existing(tmp_path, revision="new:1", dynamic=True,
                              required_formats=["html"], expected_pages=1)
    assert result["status"] != "complete"


def test_dynamic_checks_current_html_and_real_rendered_png(tmp_path):
    from PIL import Image
    html = tmp_path / "deck.html"
    write(html, '<html><section class="slide">Content</section></html>')
    shot = tmp_path / "shots/page_01.png"
    shot.parent.mkdir()
    Image.new("RGB", (128, 72), "green").save(shot)
    write(tmp_path / "shots/render.json", {"deck": str(html), "deck_sha256": hashlib.sha256(html.read_bytes()).hexdigest(),
        "mode": "all", "n_pages": 1, "pages": [{"page": 1, "png": str(shot), "blank": False}]})
    result = inspect_existing(tmp_path, revision="new:1", dynamic=True,
                              required_formats=["html"], expected_pages=1)
    assert result["status"] == "complete", result
    shot.unlink()
    assert inspect_existing(tmp_path, revision="new:1", dynamic=True,
                            required_formats=["html"], expected_pages=1)["status"] != "complete"


@pytest.fixture
def prior_fast_success(tmp_path):
    html = tmp_path / "index.html"
    write(html, '<html>' + ''.join(
        f'<section class="slide">OLD fact {i}</section>' for i in range(40)) + '</html>')
    deck = tmp_path / "old.deck.json"
    write(deck, {"old": True})
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    reports = {}
    for name in ("deck_spec", "truth_check", "html_self_check", "runtime_probe"):
        path = tmp_path / "qa" / f"{name}.json"
        write(path, {"ok": True})
        reports[str(path)] = digest(path)
    write(tmp_path / "qa/delivery_receipt.json", {
        "html": str(html), "html_sha256": digest(html), "deck": str(deck),
        "deck_sha256": digest(deck), "report_sha256": reports})
    assert inspect_existing(tmp_path, revision="old:1", dynamic=False,
                            required_formats=["html"], expected_pages=40)["status"] == "complete"
    return tmp_path


def test_delivery_hashes_and_receipts_work_with_python310_hashlib(prior_fast_success, monkeypatch):
    from presentation_delivery import _sha
    from presentation_artifacts import _digest

    monkeypatch.delattr(hashlib, "file_digest", raising=False)
    root = prior_fast_success
    artifact = root / "large-artifact.bin"
    payload = b"multi-chunk artifact\x00" * 150_000
    artifact.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert _sha(artifact) == expected
    assert _digest(artifact) == expected
    receipt = inspect_existing(root, revision="portable:1", dynamic=False,
                               required_formats=["html"], expected_pages=40)
    assert receipt["status"] == "complete", receipt


def test_capacity_partial_cannot_reuse_prior_fast_success(prior_fast_success):
    import subprocess
    from tests.test_pptx_design_plan import NODE, SKILL

    if not NODE:
        pytest.skip("Node.js is unavailable")
    root = prior_fast_success
    original_html = (root / "index.html").read_bytes()
    outline = {"slides": [{"page": i, "layout": "cards", "title": f"Corrected {i}",
        "message": "Current content", "bullets": [f"{j} " + "new_fact " * 40 for j in range(13)]}
        for i in range(1, 41)]}
    write(root / "outline.json", outline)
    data = {"title": "Changed content", "outline": outline,
            "outline_file": str(root / "outline.json"), "user_constraints": {}}
    code = ('const mod=require(process.argv[1]);const input=JSON.parse(process.argv[2]);'
            'console.log(JSON.stringify(mod.fallback(input,process.argv[3],"new input")));')
    result = subprocess.run([NODE, "-e", code, str(SKILL / "scripts/design_recovery.js"),
                             json.dumps(data), str(root)],
                            check=True, capture_output=True, text=True, timeout=30)
    report = json.loads(result.stdout)
    assert report["status"] == "partial" and report["content_complete"] is False
    assert report.get("html_hash") is None
    receipt = inspect_existing(root, revision="updated:2", dynamic=False,
                               required_formats=["html"], expected_pages=40)
    assert receipt["status"] == "partial", receipt
    assert "page/content contract" in receipt["error"]
    assert receipt["warnings"] == report["warnings"]
    assert receipt["artifacts"][0]["path"] == str(root / "index.html")
    assert "qa/design_delivery.json" in receipt["inputs"]
    assert (root / "index.html").read_bytes() == original_html


@pytest.mark.parametrize("binding", ["missing", "mismatched", "current"])
def test_terminal_fallback_evidence_cannot_fall_through_to_old_receipt(prior_fast_success, binding):
    root = prior_fast_success
    html = root / "index.html"
    outline = root / "outline.json"
    write(outline, {"current": True})
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    report = {"terminal": True, "status": "degraded", "primary_artifact": str(html),
              "content_inputs": {"outline": {"path": str(outline), "sha256": digest(outline)}},
              "warnings": ["Fallback requires visual review"]}
    if binding != "missing":
        report["html_hash"] = digest(html) if binding == "current" else "0" * 64
    write(root / "qa/design_delivery.json", report)
    receipt = inspect_existing(root, revision="updated:2", dynamic=False,
                               required_formats=["html"], expected_pages=40)
    assert receipt["warnings"] == report["warnings"]
    if binding == "current":
        assert receipt["status"] == "complete", receipt
    else:
        assert receipt["status"] != "complete", receipt
        assert "not bound to current HTML" in receipt["error"]


def test_nonterminal_design_acceptance_keeps_normal_finalizer_path(prior_fast_success):
    root = prior_fast_success
    write(root / "qa/design_delivery.json", {"ok": True, "status": "design_accepted"})
    receipt = inspect_existing(root, revision="accepted:1", dynamic=False,
                               required_formats=["html"], expected_pages=40)
    assert receipt["status"] == "complete", receipt
