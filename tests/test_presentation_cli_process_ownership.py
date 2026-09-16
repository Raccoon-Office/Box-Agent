"""The real CLI must survive shell-level cancellation without orphaning work."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time

import psutil
import pytest

from box_agent.tools.bash_tool import BashTool
from tests.test_presentation_finalize_cli import contract

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "box_agent/skills/pptx/scripts"


def live(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def wait_path(path, process):
    deadline = time.monotonic() + 8
    while not path.exists() and time.monotonic() < deadline:
        assert process.returncode is None
        await asyncio.sleep(.02)
    assert path.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX outer shell process-group contract")
@pytest.mark.parametrize("cancel", ["shell", "kill", "timeout"])
@pytest.mark.parametrize("stage", ["build", "export"])
async def test_cli_cancel_and_timeout_reap_owned_work_and_replace_old_receipt(tmp_path, cancel, stage):
    node = shutil.which("node")
    if stage == "export" and not node:
        pytest.skip("Node unavailable")
    bundle = tmp_path / "copied skills"
    shutil.copytree(SCRIPTS, bundle / "pptx/scripts")
    standard = bundle / "presentation-suite/skills/sn-ppt-standard/scripts"
    standard.mkdir(parents=True)
    shutil.copyfile(REPO / "scripts/presentation_suite_overlays/render_runtime.py", standard / "render_runtime.py")
    deck = tmp_path / "workspace/deck"
    (deck / "slides").mkdir(parents=True)
    (deck / "slides/slide_01.html").write_text("<html>1</html>")
    (deck / "slides/slide_02.html").write_text("<html>2</html>")
    requirements, pack = contract(deck)
    (deck / "requirements.json").write_text(json.dumps(requirements))
    (deck / "task_pack.json").write_text(json.dumps(pack))
    trace = deck / "_trace"
    trace.mkdir()
    (trace / "finalize-receipt.json").write_text('{"status":"complete","old":true}')
    pidfile, marker = tmp_path / "build.pid", tmp_path / "after_cancel"
    # Ignore TERM so an implementation depending on the outer Bash 1 s grace
    # cannot hide a detached child waiting through a 12 s internal cleanup.
    slow = f'''import os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
Path({str(pidfile)!r}).write_text(str(os.getpid()))
time.sleep({1.5 if stage == 'build' else 3})
Path({str(marker)!r}).write_text("work after cancellation")
time.sleep(30)
'''
    if stage == "build":
        (standard / "deck.py").write_text(slow)
    else:
        # Execute the actual managed DOM extractor function with a fake
        # Playwright launch transport; its browser is a real detached process.
        # This checks the exported browser guard wiring, beyond the build group.
        (standard / "deck.py").write_text('''from pathlib import Path
import sys
if sys.argv[1]=='build':
    (Path(sys.argv[2])/'present.html').write_text('slides/slide_01.html slides/slide_02.html')
''')
        browser = tmp_path / "fake-browser"
        browser.write_text(f"#!{sys.executable}\n" + slow)
        browser.chmod(0o700)
        source = (REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts/export_pptx/lib/dom_extractor.mjs").read_text()
        function = source[source.index("export async function extractPages(htmlPaths)"):]
        (standard / "export_pptx").mkdir()
        (standard / "export_pptx/html_to_pptx.mjs").write_text(
            "import {spawn} from 'node:child_process';\n"
            + f"const pickBrowserExe=()=>{json.dumps(str(browser))}; const bundledEchartsSource=null;\n"
            + "const chromium={launch:async options=>{const child=spawn(options.executablePath,[],{detached:true,stdio:'ignore'});return {newPage:async()=>new Promise(()=>{}),close:async()=>{await new Promise(resolve=>{child.once('exit',resolve);child.kill('SIGTERM');});}};}};\n"
            + function + "\nawait extractPages(['slide.html']);\n")
    unrelated = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time;time.sleep(30)", start_new_session=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(bundle / "pptx/scripts/finalize.py"), "--workspace", str(deck.parent),
        "--deck-dir", str(deck), "--requirements", str(deck / "requirements.json"),
        "--task-pack", str(deck / "task_pack.json"), "--timeout", ("1.2" if stage == "build" else "3") if cancel == "timeout" else "20",
        env={**os.environ, "BOX_AGENT_PYTHON": sys.executable, "BOX_AGENT_NODE": node if stage == "export" else sys.executable},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    output = asyncio.create_task(process.communicate())
    owned = []
    try:
        await wait_path(pidfile, process)
        owned = psutil.Process(process.pid).children(recursive=True)
        if cancel == "shell":
            await BashTool._kill_process_tree(process)
        elif cancel == "kill":
            process.kill()
        await asyncio.wait_for(asyncio.shield(output), 15)
        await asyncio.sleep(.8)
        assert not live(int(pidfile.read_text())), "formal work survived outer CLI cancellation"
        assert not marker.exists(), "cancelled work performed a later side effect"
        assert unrelated.returncode is None
        receipt = json.loads((trace / "finalize-receipt.json").read_text())
        assert receipt["status"] != "complete", receipt
        if cancel != "kill":
            assert receipt["status"] in {"cancelled", "error", "partial"}, receipt
    finally:
        if process.returncode is None:
            process.kill()
        for child in owned:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        if pidfile.exists() and live(int(pidfile.read_text())):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        await asyncio.wait_for(output, 5)
        unrelated.kill()
        await unrelated.wait()
