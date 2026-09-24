"""Batch stdout receipts keep the rendering and visual-review stages separate."""

import atexit
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
from types import SimpleNamespace

import pytest
from PIL import Image


STANDARD = Path(os.environ.get(
    "PRESENTATION_STANDARD_SOURCE",
    Path(__file__).resolve().parents[1]
    / "box_agent/skills/presentation-suite/skills/sn-ppt-standard",
))


@pytest.fixture
def renderer(tmp_path, monkeypatch):
    """Keep batch selection, gates, reports and CLI real; isolate browser I/O."""
    skill = tmp_path / "skill with spaces"
    shutil.copytree(STANDARD / "scripts", skill / "scripts")
    shutil.copytree(STANDARD / "subagents", skill / "subagents")
    monkeypatch.syspath_prepend(str(skill / "scripts"))
    monkeypatch.setattr(atexit, "register", lambda *args: None)
    state = runpy.run_path(str(skill / "scripts/render.py"))["main"].__globals__
    control = {"failure": None, "cleanup_error": False}

    class Browser:
        def close(self):
            if control["cleanup_error"]:
                raise RuntimeError("injected browser cleanup failure")

    browser = Browser()
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser), stop=lambda: None)
    manager = SimpleNamespace(start=lambda: playwright, __exit__=lambda *args: None)
    monkeypatch.setitem(state, "_sync_playwright", lambda: lambda: manager)
    monkeypatch.setitem(state, "_ensure_browser_available", lambda _: "/mock/chromium")
    monkeypatch.setitem(state, "_setup_libs", lambda: None)
    monkeypatch.setitem(state, "_acquire_render_slot", lambda: None)
    monkeypatch.setenv("_PPT_RENDER_DIRECTORY", str(tmp_path))

    def render_page(_browser, html, output, width, height, **kwargs):
        number = int(Path(html).stem.split("_")[1])
        Image.new("RGB", (160, 90), "white").save(output)
        report = {"broken": [], "overflow": [], "layout": {}, "runtime": {}}
        if number == 2 and control["failure"] == "runtime":
            report["runtime"]["page_errors"] = ["injected chart initialization failure"]
        if number == 2 and control["failure"] == "quality":
            report["layout"]["footerPushed"] = {"belowViewport": 24}
        return report

    monkeypatch.setitem(state, "_render_once", render_page)
    return state, skill, control


@pytest.fixture
def deck(tmp_path):
    root = tmp_path / "deck with spaces"
    (root / "slides").mkdir(parents=True)
    (root / "renders").mkdir()
    for number in (1, 2, 3):
        (root / f"slides/slide_{number:02d}.html").write_text(f"<html>Page {number}</html>")
        (root / f"renders/slide_{number:02d}.png").write_bytes(b"stale PNG")
    (root / "renders/slide_09.png").write_bytes(b"unrequested PNG")
    return root


def run_batch(state, args, monkeypatch):
    monkeypatch.setattr(sys, "argv", [state["__file__"], "--batch", *args])
    with pytest.raises(SystemExit) as exit_status:
        state["main"]()
    return exit_status.value.code


def receipts(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def test_batch_receipt_lists_only_current_selected_pngs_and_resolved_paths(renderer, deck, tmp_path, capsys, monkeypatch):
    state, skill, _ = renderer
    linked = tmp_path / "linked deck"
    linked.symlink_to(deck, target_is_directory=True)
    other_cwd = tmp_path / "another working directory"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    assert run_batch(state, [str(linked), "--pages", "3,1"], monkeypatch) == 0
    captured = capsys.readouterr()
    assert receipts(captured.out) == [{
        "status": "rendered", "qa": "not-run", "rendered_pages": [1, 3],
        "images": [str(deck / "renders/slide_01.png"), str(deck / "renders/slide_03.png")],
        "deck_dir": str(deck.resolve()), "skill_root": str(skill.resolve()),
        "next_instructions": str(skill / "subagents/slide.md"),
        "review_ledger": str(deck / "_trace/review-issues.md"),
    }]
    assert (deck / "renders/slide_02.png").read_bytes() == b"stale PNG"
    assert (deck / "renders/slide_09.png").read_bytes() == b"unrequested PNG"
    manifest = json.loads((deck / "renders/render.json").read_text())
    assert set(manifest["pages"]) == {"01", "03"}
    assert "PASS" not in captured.out + captured.err and '"ready"' not in captured.out
    assert not (deck / "_trace/review-issues.md").exists()


@pytest.mark.parametrize("failure", ["runtime", "quality"])
def test_batch_partial_failure_preserves_diagnostics_without_success_receipt(renderer, deck, capsys, monkeypatch, failure):
    state, _, control = renderer
    control["failure"] = failure
    assert run_batch(state, [str(deck), "--pages", "1,2"], monkeypatch) == 1
    captured = capsys.readouterr()
    assert not receipts(captured.out)
    assert "batch render failed" in captured.err
    assert "slide_01.png clean" in captured.out
    assert "PASS" not in captured.out + captured.err and '"ready"' not in captured.out
    assert (deck / "renders/slide_03.png").read_bytes() == b"stale PNG"
    issue = json.loads((deck / "_trace/render-issues.json").read_text())["pages"]["02"]
    assert issue["hard_issues"][0]["type"] == ("page_errors" if failure == "runtime" else "footerPushed")
    assert not (deck / "_trace/review-issues.md").exists()


def test_batch_cleanup_failure_cannot_emit_a_success_receipt(renderer, deck, capsys, monkeypatch):
    state, _, control = renderer
    control["cleanup_error"] = True
    # The upstream CLI reports final retry failure; the supervised adapter lets
    # the cleanup exception reach its process supervisor. Neither may claim success.
    try:
        assert run_batch(state, [str(deck), "--pages", "1"], monkeypatch) == 1
    except RuntimeError as error:
        assert "injected browser cleanup failure" in str(error)
    captured = capsys.readouterr()
    assert not receipts(captured.out)
    assert "slide_01.png clean" in captured.out
    assert not (deck / "_trace/review-issues.md").exists()


@pytest.mark.parametrize("failure", [None, "ack", "cleanup", "nonzero"])
def test_supervised_batch_publishes_receipt_only_after_successful_cleanup(
        renderer, deck, capsys, monkeypatch, failure):
    state, _, _ = renderer
    if "supervise" not in state:
        pytest.skip("Supervisor belongs to the generated Box adapter")
    runtime = state["supervise"].__globals__
    cleanup_error = runtime["RenderCleanupError"]

    class Supervisor:
        def __init__(self, renderer_path, args, directory, deadline, stdout, stderr, slot):
            self.stdout = stdout

        def run(self):
            # Real batch CLI output, followed by a failure outside the browser
            # context: this is later than test_batch_cleanup_failure exercises.
            output = io.StringIO()
            with redirect_stdout(output):
                code = run_batch(state, [str(deck), "--pages", "1"], monkeypatch)
            self.stdout.write(output.getvalue().encode())
            self.stdout.write(b'{"status":"warning","message":"preserve diagnostic"}\n')
            self.stdout.write(b'not JSON: preserve progress\n')
            if failure == "ack":
                raise cleanup_error("injected worker ack failure")
            return runtime["CLEANUP_EXIT"] if failure == "nonzero" else code

        def close(self):
            if failure == "cleanup":
                raise cleanup_error("injected supervisor cleanup failure")

    monkeypatch.setitem(runtime, "_Supervisor", Supervisor)
    monkeypatch.setitem(runtime, "_acquire_render_slot", lambda deadline: None)
    code = state["supervise"](state["__file__"], ["--batch", str(deck), "--pages", "1"])
    captured = capsys.readouterr()
    assert code == (runtime["CLEANUP_EXIT"] if failure else 0)
    summaries = [item for item in receipts(captured.out) if item.get("status") == "rendered"]
    assert len(summaries) == (0 if failure else 1)
    assert "slide_01.png clean" in captured.out
    assert '"message":"preserve diagnostic"' in captured.out
    assert "not JSON: preserve progress" in captured.out
    if failure in {"ack", "cleanup"}:
        assert f"injected {'worker ack' if failure == 'ack' else 'supervisor cleanup'} failure" in captured.err
