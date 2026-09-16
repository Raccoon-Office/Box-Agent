"""Offline delivery contracts; synthetic decks are not live-render evidence."""

import asyncio
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import zipfile

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "box_agent/skills/pptx/scripts"))
from PIL import Image


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts"


@pytest.fixture
def module():
    assert importlib.util.find_spec("presentation_delivery"), "delivery adapter missing"
    return importlib.import_module("presentation_delivery")


@pytest.fixture
def deck(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "box-home"))
    root = tmp_path / "workspace" / "deck with spaces"
    (root / "slides").mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "assets/icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    for i in (1, 2):
        (root / f"slides/slide_{i:02}.html").write_text(
            '<html><link rel="stylesheet" href="../base.css">'
            '<img src="../assets/icon.svg"><body>Slide</body></html>'
        )
    (root / "base.css").write_text(".slide {width:1600px;height:900px}")
    (root / "outline.md").write_text("Two pages")
    return root


def pptx(path, *, pages=2, tiny=False, width=9144000, height=5143500):
    ns = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    relns = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
    image = io.BytesIO()
    Image.new("RGB", (160, 90), "navy").save(image, format="PNG")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="png" ContentType="image/png"/>'
                   '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                   + ''.join(f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>' for i in range(1, pages + 1)) + '</Types>')
        z.writestr("_rels/.rels", f'<Relationships {relns}><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>')
        z.writestr("ppt/presentation.xml", f'<p:presentation {ns} {rns}><p:sldIdLst>' + ''.join(f'<p:sldId id="{255+i}" r:id="rId{i}"/>' for i in range(1, pages + 1)) + f'</p:sldIdLst><p:sldSz cx="{width}" cy="{height}"/></p:presentation>')
        z.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships {relns}>' + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>' for i in range(1, pages + 1)) + '</Relationships>')
        for i in range(1, pages + 1):
            shape = f'<p:pic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:blipFill><a:blip r:embed="image1"/></p:blipFill><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{914400 if tiny else width}" cy="{914400 if tiny else height}"/></a:xfrm></p:spPr></p:pic>'
            z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld {ns} {rns}><p:cSld><p:spTree>{shape}</p:spTree></p:cSld></p:sld>')
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", f'<Relationships {relns}><Relationship Id="image1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image{i}.png"/></Relationships>')
            z.writestr(f"ppt/media/image{i}.png", image.getvalue())


def rewrite_zip(path, edit):
    with zipfile.ZipFile(path) as z:
        entries = {name: z.read(name) for name in z.namelist()}
    edit(entries)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items(): z.writestr(name, data)


@pytest.mark.parametrize("fault", ["missing_blip", "empty_media", "invalid_media", "wrong_target", "external_target"])
def test_actual_pictures_require_embedded_decodable_media(module, deck, fault):
    path = deck / "broken-image.pptx"
    pptx(path)
    def corrupt(entries):
        slide = "ppt/slides/slide1.xml"
        rels = "ppt/slides/_rels/slide1.xml.rels"
        if fault == "missing_blip": entries[slide] = entries[slide].replace(b'<a:blip r:embed="image1"/>', b'')
        if fault == "empty_media": entries["ppt/media/image1.png"] = b""
        if fault == "invalid_media": entries["ppt/media/image1.png"] = b"not-an-image"
        if fault == "wrong_target": entries[rels] = entries[rels].replace(b"../media/image1.png", b"../media/missing.png")
        if fault == "external_target": entries[rels] = entries[rels].replace(b'Target="', b'TargetMode="External" Target="')
    rewrite_zip(path, corrupt)
    with pytest.raises(ValueError, match="(?i)(picture|image|media)"):
        module.inspect_pptx(path, expected_pages=2)


def test_text_and_vector_pages_do_not_require_picture_relationships(module, deck):
    path = deck / "vector.pptx"
    pptx(path, pages=1)
    def remove_pictures(entries):
        ns = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        entries["ppt/slides/slide1.xml"] = f'<p:sld {ns}><p:cSld><p:spTree><p:sp><p:txBody/></p:sp></p:spTree></p:cSld></p:sld>'.encode()
        del entries["ppt/slides/_rels/slide1.xml.rels"]
        del entries["ppt/media/image1.png"]
    rewrite_zip(path, remove_pictures)
    assert module.inspect_pptx(path, expected_pages=1)["pages"] == 1


def adapter(module, deck):
    return module.PresentationDeliveryAdapter(deck.parent, python_bin=Path(sys.executable), node_bin=Path(sys.executable))


def fake_steps(monkeypatch, obj, deck, *, failure=None, missing=None, tiny=False, mutate=False):
    calls = []

    async def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        step = "export" if "--deck-dir" in argv else argv[2]
        if step == failure:
            return {"returncode": 3, "stdout": "", "stderr": "real failure", "timed_out": False}
        if step == "build" and step != missing:
            (deck / "present.html").write_text('<html>slides/slide_01.html slides/slide_02.html</html>')
            (deck / "speech.md").write_text("Page 1\nPage 2")
        if step == "export" and step != missing:
            pptx(Path(argv[argv.index("--output") + 1]), tiny=tiny)
        if step == "export" and mutate:
            (deck / "slides/slide_01.html").write_text("changed during export")
        return {"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False}

    monkeypatch.setattr(obj, "_run", run)
    return calls


async def finish(obj, deck, formats=("html", "pptx")):
    return await obj.finalize(deck_dir=deck, revision="revision-1", required_formats=formats, expected_pages=2)


@pytest.mark.asyncio
async def test_runs_formal_build_audit_export_and_validates_actual_ooxml(module, deck, monkeypatch):
    obj = adapter(module, deck)
    calls = fake_steps(monkeypatch, obj, deck)
    result = await finish(obj, deck)
    assert result["status"] == "complete", result
    assert [args[2] for args, _ in calls[:2]] == ["build", "audit"]
    assert Path(calls[0][0][1]) == SCRIPTS / "deck.py"
    assert Path(calls[2][0][1]) == SCRIPTS / "export_pptx/html_to_pptx.mjs"
    assert all(kw["env"]["BOX_AGENT_PPTX_NO_INSTALL"] == "1" for _, kw in calls)
    assert {a["format"] for a in result["artifacts"]} == {"html", "pptx"}
    artifact = next(a for a in result["artifacts"] if a["format"] == "pptx")
    assert artifact["pages"] == 2 and artifact["width_emu"] == 9144000
    assert obj.verify_receipt(result, deck_dir=deck, revision="revision-1", required_formats=("html", "pptx"), expected_pages=2)["valid"]


@pytest.mark.asyncio
async def test_html_only_never_calls_node(module, deck, monkeypatch):
    obj = adapter(module, deck)
    obj.node_bin = None
    calls = fake_steps(monkeypatch, obj, deck)
    result = await finish(obj, deck, ("html",))
    assert result["status"] == "complete", result
    assert len(calls) == 2
    assert not list(deck.glob("*.pptx"))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing,status", [("build", "error"), ("export", "partial")])
async def test_zero_exit_without_required_output_is_not_success(module, deck, monkeypatch, missing, status):
    obj = adapter(module, deck)
    fake_steps(monkeypatch, obj, deck, missing=missing)
    assert (await finish(obj, deck))["status"] == status


@pytest.mark.asyncio
async def test_preexisting_player_does_not_prove_noop_build_success(module, deck, monkeypatch):
    (deck / "present.html").write_text("slides/slide_01.html slides/slide_02.html")
    obj = adapter(module, deck)
    fake_steps(monkeypatch, obj, deck, missing="build")
    assert (await finish(obj, deck))["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", [("build", "error"), ("audit", "error"), ("export", "partial")])
async def test_nonzero_exit_retains_failure_and_partial_html(module, deck, monkeypatch, failure, status):
    obj = adapter(module, deck)
    calls = fake_steps(monkeypatch, obj, deck, failure=failure)
    result = await finish(obj, deck)
    assert result["status"] == status
    assert "real failure" in json.dumps(result)
    assert len(calls) == {"build": 1, "audit": 2, "export": 3}[failure]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "input", "artifact", "formats"])
async def test_receipt_cannot_approve_changed_revision_inputs_or_outputs(module, deck, monkeypatch, change):
    obj = adapter(module, deck)
    fake_steps(monkeypatch, obj, deck)
    receipt = await finish(obj, deck)
    revision, formats = "revision-1", ("html", "pptx")
    if change == "revision": revision = "revision-2"
    if change == "input": (deck / "assets/icon.svg").write_text("changed")
    if change == "artifact": (deck / f"{deck.name}.pptx").write_bytes(b"PKfake")
    if change == "formats": formats = ("html",)
    assert not obj.verify_receipt(receipt, deck_dir=deck, revision=revision, required_formats=formats, expected_pages=2)["valid"]


@pytest.mark.asyncio
async def test_input_change_during_export_cannot_complete(module, deck, monkeypatch):
    obj = adapter(module, deck)
    fake_steps(monkeypatch, obj, deck, mutate=True)
    assert (await finish(obj, deck))["status"] != "complete"


@pytest.mark.asyncio
async def test_single_picture_shrunk_to_top_left_is_rejected(module, deck, monkeypatch):
    obj = adapter(module, deck)
    fake_steps(monkeypatch, obj, deck, tiny=True)
    result = await finish(obj, deck)
    assert result["status"] == "partial"
    assert "cover" in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["escape", "symlink", "missing_asset", "page_gap"])
async def test_untrusted_paths_missing_assets_and_missing_pages_cannot_complete(module, deck, monkeypatch, bad):
    obj = adapter(module, deck)
    calls = fake_steps(monkeypatch, obj, deck)
    target = deck
    if bad == "escape": target = deck.parent.parent
    if bad == "symlink":
        (deck / "assets/icon.svg").unlink()
        (deck / "assets/icon.svg").symlink_to(__file__)
    if bad == "missing_asset": (deck / "assets/icon.svg").unlink()
    if bad == "page_gap": (deck / "slides/slide_02.html").rename(deck / "slides/slide_03.html")
    result = await finish(obj, target)
    assert result["status"] != "complete"
    if bad in {"escape", "symlink", "page_gap"}: assert not calls


@pytest.mark.parametrize("kind", ["fake_zip", "missing_slide", "wrong_count", "zero_size", "zip_escape"])
def test_actual_package_structure_not_pk_signature(module, deck, kind):
    path = deck / "test.pptx"
    pptx(path, pages=1 if kind == "wrong_count" else 2, width=0 if kind == "zero_size" else 9144000)
    if kind == "fake_zip": path.write_bytes(b"PKfake-pptx")
    if kind == "missing_slide":
        with zipfile.ZipFile(path) as z:
            entries = {name: z.read(name) for name in z.namelist() if name != "ppt/slides/slide2.xml"}
        with zipfile.ZipFile(path, "w") as z:
            for name, data in entries.items(): z.writestr(name, data)
    if kind == "zip_escape":
        with zipfile.ZipFile(path, "a") as z:
            z.writestr("../escape", "bad")
    with pytest.raises(ValueError):
        module.inspect_pptx(path, expected_pages=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancel_reaps_own_group_and_preserves_unrelated_process(module, deck, cancel, monkeypatch):
    if os.name != "posix": pytest.skip("POSIX process group contract")
    obj = adapter(module, deck)
    import psutil
    monkeypatch.setattr(psutil, "pids", lambda: pytest.fail("must not enumerate all system processes"))
    pidfile = deck / "child.pid"
    child_code = "import time; time.sleep(60)"
    script = f"import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(60)"
    unrelated = subprocess.Popen([sys.executable, "-c", child_code], start_new_session=True)
    task = asyncio.create_task(obj._run([sys.executable, "-c", script], env=dict(os.environ), timeout=0.6 if not cancel else 60))
    try:
        for _ in range(100):
            if pidfile.exists(): break
            await asyncio.sleep(0.02)
        assert pidfile.exists()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            assert (await task)["timed_out"]
        child_pid = int(pidfile.read_text())
        assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        assert unrelated.poll() is None
    finally:
        if not task.done(): task.cancel()
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_cancellation_and_reports_error(module, deck, monkeypatch, caplog):
    if os.name != "posix":
        pytest.skip("POSIX process group contract")
    obj = adapter(module, deck)
    pidfile = deck / "cancel-cleanup.pid"
    code = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    task = asyncio.create_task(obj._run([sys.executable, "-c", code], env=dict(os.environ), timeout=60))
    killpg = os.killpg
    pid = None
    try:
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(0.02)
        assert pidfile.exists()
        pid = int(pidfile.read_text())

        def cleanup_error(group, sig):
            killpg(group, sig)
            if group == pid and sig == signal.SIGTERM:
                raise OSError("injected cleanup failure")

        monkeypatch.setattr(os, "killpg", cleanup_error)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert "delivery cleanup failed: injected cleanup failure" in caplog.text
    finally:
        if not task.done():
            task.cancel()
        if pid:
            try:
                killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_managed_exporter_signal_closes_only_its_detached_browser(tmp_path):
    """Real Node signals/processes; fake Playwright browser owns a Node child."""
    if os.name != "posix": pytest.skip("POSIX signal contract")
    node = shutil.which("node")
    if not node: pytest.skip("Node unavailable")
    source = (SCRIPTS / "export_pptx/lib/dom_extractor.mjs").read_text()
    function = source[source.index("export async function extractPages(htmlPaths)"):]
    pidfile, closed = tmp_path / "owned.pid", tmp_path / "closed"
    script = tmp_path / "owned-browser.mjs"
    script.write_text("import {spawn} from 'node:child_process'; import {writeFileSync} from 'node:fs';\n"
        + f"const pidfile={json.dumps(str(pidfile))}, closed={json.dumps(str(closed))};\n"
        + "const pickBrowserExe=()=>'/fake-browser'; const bundledEchartsSource=null;\n"
        + "const chromium={launch:async()=>{const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{detached:true,stdio:'ignore'});writeFileSync(pidfile,String(child.pid));return {newPage:async()=>new Promise(()=>{}),close:async()=>{await new Promise(resolve=>{child.once('exit',resolve);child.kill('SIGTERM');});writeFileSync(closed,'closed');}};}};\n"
        + function + "\nawait extractPages(['slide.html']);\n")
    env = {**os.environ, "BOX_AGENT_PPTX_MANAGED_DELIVERY": "1"}
    process = subprocess.Popen([node, str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    child_pid = None
    try:
        import time
        for _ in range(100):
            if pidfile.exists(): break
            time.sleep(0.02)
        assert pidfile.exists(), process.communicate(timeout=1)
        child_pid = int(pidfile.read_text())
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        assert closed.exists(), "managed exporter skipped owned browser.close on SIGTERM"
        with pytest.raises(ProcessLookupError): os.kill(child_pid, 0)
    finally:
        if process.poll() is None: os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        if child_pid:
            try: os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError: pass


@pytest.mark.asyncio
async def test_cancellation_during_timeout_cleanup_still_reaps_owned_process(module, deck):
    if os.name != "posix": pytest.skip("POSIX process group contract")
    obj = adapter(module, deck)
    pidfile = deck / "cleanup.pid"
    code = f"import os,signal,time; signal.signal(signal.SIGTERM,lambda *_:None); open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    task = asyncio.create_task(obj._run([sys.executable, "-c", code], env=dict(os.environ), timeout=0.15))
    pid = None
    try:
        for _ in range(100):
            if pidfile.exists(): break
            await asyncio.sleep(0.01)
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        await asyncio.sleep(0.25)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 16)
        with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally:
        if not task.done(): task.cancel()
        if pid:
            try: os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError: pass


@pytest.mark.parametrize("no_install", [True, False])
@pytest.mark.parametrize("dependencies_present", [True, False])
def test_exporter_explicit_no_install_fails_before_install_attempt(tmp_path, no_install, dependencies_present):
    node = shutil.which("node")
    if not node: pytest.skip("Node unavailable for no-install function probe")
    source = (SCRIPTS / "export_pptx/html_to_pptx.mjs").read_text()
    function = source[source.index("async function ensureDependencies()"):source.index("// These helpers")]
    function = function.replace("await import('./lib/browser_picker.mjs')", "({pickBrowserExe:()=>null})")
    probe = "const resolve=(...a)=>a.join('/'); const __dirname='fixture'; const existsSync=()=>" + str(dependencies_present).lower() + "; let installs=0; const execSync=()=>{installs++;throw Error('forbidden install');};\n" + function + "\nensureDependencies().then(()=>process.exit(9)).catch(e=>{console.log(JSON.stringify({installs,error:e.message}));});"
    result = subprocess.run([node, "-e", probe], text=True, capture_output=True, env={**os.environ, "BOX_AGENT_PPTX_NO_INSTALL": "1" if no_install else "0"}, timeout=10)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["installs"] == (0 if no_install else 1), payload
    if no_install:
        assert ("chromium" if dependencies_present else "dependenc") in payload["error"].lower()


def test_standard_uses_skill_finalizer_and_keeps_formal_exporter():
    skill = (SCRIPTS.parent / "SKILL.md").read_text()
    assert "finalize.py" in skill
    assert "`presentation_delivery`" not in skill
    assert 'node "$SKILL_ROOT/scripts/export_pptx/html_to_pptx.mjs"' in skill


def test_dynamic_render_receipt_hashes_actual_current_deck_without_browser(tmp_path):
    module = runpy.run_path(str(SCRIPTS.parents[1] / "sn-ppt-dazzle/scripts/render_deck.py"))
    html = tmp_path / "deck.html"
    html.write_text("<html>current dynamic deck</html>")
    renderer = module["DeckRenderer"](html, tmp_path, motion_check=False)
    renderer.finalize()
    receipt = json.loads((tmp_path / "render.json").read_text())
    assert receipt["deck_sha256"] == hashlib.sha256(html.read_bytes()).hexdigest()
