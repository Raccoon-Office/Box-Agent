"""Standard review preparation: real validation/artifacts, isolated render boundary."""

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest
from PIL import Image


REPO = Path(__file__).resolve().parents[1]
STANDARD = Path(os.environ.get(
    "PRESENTATION_STANDARD_SOURCE",
    REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard",
))


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(STANDARD / "scripts"))
    monkeypatch.delitem(sys.modules, "font_bundle", raising=False)
    deck = runpy.run_path(str(STANDARD / "scripts/deck.py"))["main"].__globals__
    render = runpy.run_path(str(STANDARD / "scripts/render.py"))
    return deck, render


@pytest.fixture
def workspace(tmp_path):
    for directory in ("slides", "plan", "assets", "renders", "_trace"):
        (tmp_path / directory).mkdir()
    (tmp_path / "base.css").write_text(":root{--canvas-w:1600px;--canvas-h:900px;}\n")
    (tmp_path / "plan/deck.md").write_text("language: en\n")
    for number in (1, 2):
        (tmp_path / f"plan/slide_{number:02d}.md").write_text(
            f"- title: Page {number}\n- image_opportunity: none\n"
            f"## On-screen copy\nPage {number}\n## Spoken script\nTalk about page {number}.\n"
        )
        (tmp_path / f"slides/slide_{number:02d}.html").write_text(
            f'<html><link rel="stylesheet" href="../base.css"><h1>Page {number}</h1>'
            '<script src="https://cdn.example/echarts.min.js"></script></html>'
        )
    return tmp_path


@pytest.fixture
def isolated_runtime(modules, monkeypatch):
    """Subsetting and browser I/O are isolated; build, guards and contact remain real."""
    deck, render = modules
    events = []
    manifest = {"faces": [{"delivery_family": "Deck-current-test"}]}

    def bundle(root, **kwargs):
        events.append("fonts")
        css = root / "base.css"
        text = css.read_text()
        if "/* bundled */" not in text:
            css.write_text(text + "\n/* bundled */\n")
        return manifest

    def render_all(root):
        events.append("render")
        assert "/* bundled */" in (root / "base.css").read_text()
        for slide in sorted((root / "slides").glob("slide_*.html")):
            assert 'src="../assets/vendor/echarts.min.js"' in slide.read_text()
            number = int(slide.stem.split("_")[1])
            target = root / "renders" / f"slide_{number:02d}.png"
            Image.new("RGB", (160, 90), "white").save(target)
            render["_record_render_report"](str(root), number, str(slide), str(target), {
                "broken": [], "overflow": [], "layout": {},
                "overlap": [{"candidate": "not a verdict"}],
            })
        print("overlap=1 advisory: do not prime the first pixel review")

    monkeypatch.setitem(deck, "bundle_workspace", bundle)
    monkeypatch.setitem(deck, "validate_font_bundle", lambda root: [])
    monkeypatch.setitem(deck, "render_all", render_all)
    monkeypatch.setitem(deck, "_validate_player_runtime", lambda root: events.append("audit"))
    original_contact = deck["_build_contact"]

    def contact(*args, **kwargs):
        events.append("contact")
        return original_contact(*args, **kwargs)

    monkeypatch.setitem(deck, "_build_contact", contact)
    return deck, events


def prepare(deck, workspace):
    return deck["main"](["review-prep", str(workspace), "--expected", "2"])


def test_review_prep_orders_mutations_before_full_render_and_returns_only_paths(isolated_runtime, workspace, capsys):
    deck, events = isolated_runtime
    assert prepare(deck, workspace) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "prepared" and result["qa"] == "not-run"
    assert result["rendered_pages"] == [1, 2]
    assert events == ["fonts", "render", "contact", "audit"]
    assert "advisory" not in captured.out + captured.err
    assert "PASS" not in captured.out + captured.err
    for key in ("present_html", "render_manifest", "review_contact", "diagnostics"):
        assert Path(result[key]).is_file()
    assert [Path(path).name for path in result["images"]] == ["slide_01.png", "slide_02.png"]
    assert not (workspace / "_trace/review-issues.md").exists()
    assert not list(workspace.glob("*.pptx"))


@pytest.mark.parametrize("changed", ["assets/image.png", "assets/runtime.js", "base.css", "slides/slide_01.html"])
def test_review_prep_always_refreshes_all_pages_without_dependency_cache(isolated_runtime, workspace, capsys, changed):
    deck, events = isolated_runtime
    assert prepare(deck, workspace) == 0
    capsys.readouterr()
    path = workspace / changed
    path.write_text(path.read_text() + "\n<!-- revised -->" if path.exists() else "new dependency")
    assert prepare(deck, workspace) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["rendered_pages"] == [1, 2]
    assert events.count("render") == 2


def test_review_prep_rejects_partial_page_option(modules, workspace):
    with pytest.raises(SystemExit) as exc:
        modules[0]["main"](["review-prep", str(workspace), "--expected", "2", "--pages", "1"])
    assert exc.value.code == 2


@pytest.mark.parametrize("args", [["--expected", "2"], ["relative-deck", "--expected", "2"]])
def test_review_prep_requires_explicit_absolute_workspace(modules, args):
    with pytest.raises(SystemExit) as exc:
        modules[0]["main"](["review-prep", *args])
    assert exc.value.code == 2


@pytest.mark.parametrize("invalid", ["missing-page", "bad-resource", "missing-plan"])
def test_review_prep_reports_validation_failure_before_browser(isolated_runtime, workspace, capsys, invalid):
    deck, events = isolated_runtime
    if invalid == "missing-page":
        (workspace / "slides/slide_02.html").unlink()
    elif invalid == "missing-plan":
        (workspace / "plan/slide_02.md").unlink()
    else:
        with (workspace / "slides/slide_01.html").open("a") as stream:
            stream.write('<img src="../assets/missing.png">')
    assert prepare(deck, workspace) == 1
    failure = json.loads(capsys.readouterr().err)
    assert failure["stage"] == "validate" and failure["status"] == "failed"
    assert "render" not in events


def test_review_prep_preserves_failed_render_screenshot_and_diagnostic(isolated_runtime, workspace, capsys, monkeypatch):
    deck, _ = isolated_runtime
    original = deck["render_all"]

    def fail(root):
        original(root)
        raise RuntimeError("slide_02: runtime page error: chart initialization failed")

    monkeypatch.setitem(deck, "render_all", fail)
    assert prepare(deck, workspace) == 1
    captured = capsys.readouterr()
    failure = json.loads(captured.err)
    assert failure["stage"] == "render"
    assert "chart initialization failed" in failure["error"]
    assert (workspace / "renders/slide_01.png").is_file()
    assert (workspace / "_trace/render-issues.json").is_file()
    assert not (workspace / "present.html").exists()
    assert not captured.out


def test_review_prep_reports_upstream_subprocess_timeout(isolated_runtime, workspace, capsys, monkeypatch):
    def timeout(root):
        raise subprocess.TimeoutExpired("renderer", 600, output=b"page 1 rendered", stderr=b"page 2 stalled")

    deck, _ = isolated_runtime
    monkeypatch.setitem(deck, "render_all", timeout)
    assert prepare(deck, workspace) == 1
    failure = json.loads(capsys.readouterr().err)
    assert failure["stage"] == "render" and "page 2 stalled" in failure["error"]


def test_review_prep_rebuilds_an_older_player(isolated_runtime, workspace, capsys):
    (workspace / "present.html").write_text('const S=["slides/slide_01.html"];')
    assert prepare(isolated_runtime[0], workspace) == 0
    assert "slide_02.html" in (workspace / "present.html").read_text()
    assert json.loads(capsys.readouterr().out)["qa"] == "not-run"


def test_review_prep_returns_manifest_after_existing_build_fallback(isolated_runtime, workspace, capsys, monkeypatch):
    deck, events = isolated_runtime
    original_player = deck["_build_player"]

    def css_changed_before_player(root, expected, **kwargs):
        css = root / "base.css"
        css.write_text(css.read_text() + "\n/* changed after rendering */")
        old = css.stat().st_mtime_ns - 1_000_000_000
        for png in (root / "renders").glob("*.png"):
            os.utime(png, ns=(old, old))
        return original_player(root, expected, **kwargs)

    monkeypatch.setitem(deck, "_build_player", css_changed_before_player)
    assert prepare(deck, workspace) == 0
    result = json.loads(capsys.readouterr().out)
    assert events.count("render") == 2
    manifest = json.loads(Path(result["render_manifest"]).read_text())
    for number, image in zip(result["rendered_pages"], result["images"]):
        assert manifest["pages"][f"{number:02d}"]["png_sha256"] == hashlib.sha256(Path(image).read_bytes()).hexdigest()


@pytest.mark.parametrize("stage,hook", [("fonts", "bundle_workspace"), ("build", "_build_contact"), ("audit", "_validate_player_runtime")])
def test_review_prep_reports_stage_failures_without_success(isolated_runtime, workspace, capsys, monkeypatch, stage, hook):
    def fail(*args, **kwargs):
        raise RuntimeError("injected boundary failure")

    deck, _ = isolated_runtime
    monkeypatch.setitem(deck, hook, fail)
    assert prepare(deck, workspace) == 1
    output = capsys.readouterr()
    assert not output.out
    failure = json.loads(output.err)
    assert failure["stage"] == stage and "injected boundary failure" in failure["error"]
    if stage != "fonts":
        assert (workspace / "renders/slide_02.png").is_file()


def test_review_prep_blocks_stale_chart_font_alias_without_rewriting(isolated_runtime, workspace, capsys):
    deck, events = isolated_runtime
    page = workspace / "slides/slide_01.html"
    page.write_text(page.read_text() + '\n<script>const label = {fontFamily: "Deck-old-noto"};</script>')
    assert prepare(deck, workspace) == 1
    failure = json.loads(capsys.readouterr().err)
    assert failure["stage"] == "fonts"
    assert "slides/slide_01.html:2" in failure["error"]
    assert "Deck-old-noto" in failure["error"]
    assert "Deck-old-noto" in page.read_text() and "render" not in events


def test_review_prep_reports_linked_chart_alias_and_accepts_current_alias(isolated_runtime, workspace, capsys):
    deck, _ = isolated_runtime
    page = workspace / "slides/slide_01.html"
    page.write_text(page.read_text() + '<script src="../assets/chart.js"></script>')
    chart = workspace / "assets/chart.js"
    chart.write_text('const labels = {fontFamily: "Deck-old-noto"};')
    assert prepare(deck, workspace) == 1
    assert "assets/chart.js:1" in json.loads(capsys.readouterr().err)["error"]
    chart.write_text('const labels = {fontFamily: "Deck-current-test"};')
    assert prepare(deck, workspace) == 0


def test_font_inputs_include_new_html_glyphs_as_well_as_current_plan(modules, workspace):
    inputs = modules[0]["bundle_workspace"].__globals__["_read_inputs"]
    slide = workspace / "slides/slide_01.html"
    slide.write_text(slide.read_text() + "<p>新增龘</p>")
    _, fragments = inputs(workspace)
    assert "新增龘" in "".join(fragments)
    assert "Page 2" in "".join(fragments)
    assert "Talk about page" not in "".join(fragments)


def test_render_report_explains_signed_footer_geometry_without_design_advice(modules, workspace):
    page = workspace / "slides/slide_01.html"
    png = workspace / "renders/slide_01.png"
    Image.new("RGB", (160, 90)).save(png)
    report = {"layout": {"footerPushed": {
        "belowViewport": -60, "bodyOverFooter": 27, "worst": "footnote"
    }}, "boxoverflow": [{"cls": "formula-panel", "ob": 261, "orr": 0}]}
    modules[1]["_record_render_report"](str(workspace), 1, str(page), str(png), report)
    data = json.loads((workspace / "renders/render.json").read_text())["pages"]["01"]
    geometry = data["diagnostics"]
    footer = next(item for item in geometry if item["type"] == "footerPushed")
    assert footer["severity"] == "hard"
    assert footer["belowViewport"] == {"value": -60, "unit": "px", "direction": "inside", "distance": 60}
    assert footer["bodyOverFooter"]["value"] == 27
    assert footer["bodyOverFooter"]["meaning"] == "body deepest content bottom minus footer top"
    box = next(item for item in geometry if item["type"] == "boxoverflow")
    assert box["severity"] == "advisory"
    assert box["fields"]["ob"] == {"unit": "px", "direction": "bottom", "meaning": "maximum child extension past container content bottom"}
    assert box["fields"]["orr"] == {"unit": "px", "direction": "right", "meaning": "maximum child extension past container content right edge"}
    assert box["items"] == report["boxoverflow"]
    assert data["report"]["layout"]["footerPushed"] == report["layout"]["footerPushed"]
    assert data["source_sha256"] == hashlib.sha256(page.read_bytes()).hexdigest()


def test_updated_upstream_remains_compatible_with_box_owned_renderer_overlay():
    if "PRESENTATION_STANDARD_SOURCE" not in os.environ:
        pytest.skip("The synchronized bundle already has the overlay applied")
    apply = runpy.run_path(str(REPO / "scripts/presentation_suite_overlays/render_lifecycle.py"))["apply"]
    for name in ("deck.py", "render.py", "font_bundle.py"):
        adapted = apply(f"skills/sn-ppt-standard/scripts/{name}", (STANDARD / "scripts" / name).read_bytes())
        compile(adapted, name, "exec")
        assert b"run_renderer" in adapted
        if name == "render.py":
            assert b"def _geometry_diagnostics" in adapted


@pytest.mark.skipif(os.environ.get("PRESENTATION_REAL_RENDER") != "1", reason="opt-in real browser/font fixture")
def test_review_prep_real_fonts_browser_and_combined_box_overlays(workspace, tmp_path):
    """No mocks: only temporary skill/deck files; uses already installed runtime."""
    staged = tmp_path / "standard-runtime"
    shutil.copytree(STANDARD / "assets", staged / "assets")
    (staged / "scripts").mkdir()
    overlays = []
    if "PRESENTATION_STANDARD_SOURCE" in os.environ:
        sync = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
        overlays = [sync[name] for name in ("_apply_integration_overlay", "_host_playwright_overlay",
                                            "_image_inspection_recovery_overlay")]
    for name in ("deck.py", "render.py", "font_bundle.py"):
        content = (STANDARD / "scripts" / name).read_bytes()
        for apply in overlays:
            content = apply(f"skills/sn-ppt-standard/scripts/{name}", content)
        (staged / "scripts" / name).write_bytes(content)
    runtime = REPO / "scripts/presentation_suite_overlays/render_runtime.py"
    shutil.copyfile(runtime, staged / "scripts/render_runtime.py")
    fonts = REPO / "box_agent/skills/presentation-suite/fonts"
    python = os.environ.get("PRESENTATION_RENDER_PYTHON", sys.executable)
    env = {**os.environ, "PPT_FONT_SOURCE_DIRS": str(fonts),
           "PATH": str(Path(python).parent) + os.pathsep + os.environ["PATH"]}
    (workspace / "base.css").write_text(
        ':root{--canvas-w:1600px;--canvas-h:900px;--font-title:"Noto Sans SC";--font-body:"Noto Sans SC";}'
        '.slide{width:1600px;height:900px;padding:100px;background:white;color:#111;}'
        'h1{font:64px var(--font-title);}'
    )
    for number in (1, 2):
        (workspace / f"slides/slide_{number:02d}.html").write_text(
            '<!doctype html><html><head><meta charset="utf-8">'
            '<link rel="stylesheet" href="../base.css"></head><body>'
            f'<section class="slide"><h1>Page {number} 新</h1></section></body></html>'
        )
    result = subprocess.run([python, str(staged / "scripts/deck.py"), "review-prep", str(workspace),
                             "--expected", "2"], capture_output=True, text=True, env=env, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "prepared" and payload["qa"] == "not-run"
    assert payload["rendered_pages"] == [1, 2]
    manifest = json.loads(Path(payload["render_manifest"]).read_text())
    for number, path in zip(payload["rendered_pages"], payload["images"]):
        record = manifest["pages"][f"{number:02d}"]
        assert not record["hard_issues"]
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == record["png_sha256"]
        assert Path(path).stat().st_mtime_ns >= (workspace / "base.css").stat().st_mtime_ns
    coverage = subprocess.run([python, "-c", (
        "import json,sys;from pathlib import Path;from fontTools.ttLib import TTFont;"
        "root=Path(sys.argv[1]);m=json.loads((root/'assets/fonts/manifest.json').read_text());"
        "assert any(ord('新') in TTFont(root/f['path']).getBestCmap() for f in m['faces'])"
    ), str(workspace)], capture_output=True, text=True, env=env, timeout=30)
    assert coverage.returncode == 0, coverage.stderr
    assert json.loads(Path(payload["review_contact"]).read_text())["full"]["pages"] == [1, 2]
    assert not (workspace / "_trace/review-issues.md").exists()
