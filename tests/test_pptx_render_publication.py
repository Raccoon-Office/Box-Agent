"""PPTX QA renderer outputs stay on disk without becoming image deliveries."""

from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

from box_agent.tools.engine.artifact_results import _detect_tool_artifacts, _snapshot_workspace_signatures


SCRIPTS = Path(__file__).resolve().parents[1] / "box_agent/skills/document-skills/pptx/scripts"


def published_images(root):
    references = " ".join(f"[{p.relative_to(root).as_posix()}]" for p in root.rglob("*")
                          if p.suffix in {".png", ".jpg"})
    events = _detect_tool_artifacts("render", "bash", references, None, {},
                                    _snapshot_workspace_signatures(str(root)), str(root))
    return {e.filename for e in events if e.kind == "image"}


@pytest.mark.parametrize("backend,fmt", [("poppler", "png"), ("poppler", "jpeg"), ("quicklook", "png")])
@pytest.mark.parametrize("fails", [False, True])
def test_external_renderer_marks_successful_and_partial_images(tmp_path, monkeypatch, backend, fmt, fails):
    main = runpy.run_path(str(SCRIPTS / "render_pptx.py"))["main"]
    state = main.__globals__
    pptx = tmp_path / "社区方案.pptx"
    pptx.write_bytes(b"fixture")
    out = tmp_path / "renders"
    out.mkdir()
    (out / "unrelated.png").write_bytes(b"keep")
    (out / "slide-99.png").write_bytes(b"pre-existing standalone image")
    (tmp_path / "deck-overview.png").write_bytes(b"overview")
    name = f"{pptx.name}.png" if backend == "quicklook" else f"slide-01.{'png' if fmt == 'png' else 'jpg'}"
    # Exercise overwriting an existing image as well as new files.
    (out / name).write_bytes(b"old")
    binaries = iter([None, None] if backend == "quicklook" else ["soffice", "pdftoppm"])
    monkeypatch.setitem(state, "find_binary", lambda _: next(binaries))
    monkeypatch.setattr(state["platform"], "system", lambda: "Darwin")
    monkeypatch.setattr(state["shutil"], "which", lambda _: "qlmanage")
    monkeypatch.setitem(state, "render_pdf_with_node", lambda *args: False)

    def run(cmd):
        if cmd[0] == "soffice":
            (out / f"{pptx.stem}.pdf").write_bytes(b"pdf")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        (out / name).write_bytes(b"new rendered page")
        return subprocess.CompletedProcess(cmd, 1 if fails else 0, "", "failed" if fails else "")

    monkeypatch.setitem(state, "run", run)
    monkeypatch.setattr(sys, "argv", ["render_pptx.py", str(pptx), "--out", str(out), "--format", fmt])
    assert main() == (1 if fails else 0)
    assert (out / name).read_bytes() == b"new rendered page"
    assert published_images(tmp_path) == {"unrelated.png", "slide-99.png", "deck-overview.png"}


def test_renderer_exception_still_marks_partial_output(tmp_path, monkeypatch):
    render = runpy.run_path(str(SCRIPTS / "render_pptx.py"))["run_image_renderer"]

    def fail(cmd):
        (tmp_path / "slide-1.png").write_bytes(b"partial")
        raise OSError("renderer failed")

    monkeypatch.setitem(render.__globals__, "run", fail)
    with pytest.raises(OSError, match="renderer failed"):
        render(["renderer"], tmp_path, "slide-*.png")
    assert published_images(tmp_path) == set()


@pytest.mark.parametrize("fails", [False, True])
def test_pdfjs_script_marks_each_page_even_if_a_later_page_fails(tmp_path, fails):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the pdf.js script check")
    script = tmp_path / "pdfjs_render.js"
    shutil.copyfile(SCRIPTS / script.name, script)
    # Only the rendering dependencies are faked; run the shipped entry point unchanged.
    pdfjs = tmp_path / "node_modules/pdfjs-dist/legacy/build/pdf.mjs"
    pdfjs.parent.mkdir(parents=True)
    pdfjs.write_text("export function getDocument() { return {promise: Promise.resolve({numPages: 2, "
                    "async getPage(n) { return {getViewport() {return {width:1,height:1}}, "
                    f"render() {{ if (n === 2 && {str(fails).lower()}) throw Error('page failed'); "
                    "return {promise:Promise.resolve()}}}}})};}")
    canvas = tmp_path / "node_modules/@napi-rs/canvas/index.js"
    canvas.parent.mkdir(parents=True)
    canvas.write_text("exports.createCanvas = () => ({getContext:()=>({}), toBuffer:()=>Buffer.from('png')});")
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(b"pdf fixture")
    out = tmp_path / "renders"
    result = subprocess.run([node, str(script), str(pdf), "--out", str(out)], capture_output=True, text=True)
    assert result.returncode == (1 if fails else 0), result.stderr
    assert len(list(out.glob("slide-*.png"))) == (1 if fails else 2)
    (out / "deck-overview.png").write_bytes(b"overview")
    assert published_images(out) == {"deck-overview.png"}
