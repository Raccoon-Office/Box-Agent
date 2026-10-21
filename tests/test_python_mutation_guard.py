"""Execute scripts, rather than predicting every branch from their source."""

import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.bash_tool import BashTool, BackgroundShellManager
from box_agent.schema import Message
from box_agent.tools.chat_references import ChatReferences, bind_chat_references
from box_agent.tools.engine.execution import invoke_tool_once


STANDARD = Path(__file__).resolve().parents[1] / "box_agent/skills/presentation-suite/skills/sn-ppt-standard"


@pytest.fixture(autouse=True)
def shell(posix_shell):
    pass


async def run_script(root, guard, script, *args, tool=None, **options):
    tool = tool or BashTool(workspace_dir=str(root), bypass_dangerous_command_approval=True)
    return await invoke_tool_once(tool, {
        "command": shlex.join([sys.executable, str(script), *map(str, args)]), **options,
    }, ToolInvocationContext(chat_references=guard))


@pytest.fixture
def retained(tmp_path):
    target = tmp_path / "chat report.txt"
    target.write_text("published")
    guard = ChatReferences(tmp_path)
    guard.retain(target)
    return target, guard


@pytest.mark.asyncio
@pytest.mark.parametrize("script,arguments", [
    ("deck.py", ["--help"]), ("render.py", ["--help"]), ("deck.py", ["init"]),
])
async def test_real_ppt_help_and_init_execute_with_existing_chat_references(tmp_path, retained, script, arguments):
    target, guard = retained
    deck = tmp_path / "new deck"
    deck.mkdir()
    args = arguments + ([deck] if arguments == ["init"] else [])
    result = await run_script(tmp_path, guard, STANDARD / "scripts" / script, *args)
    assert result.success, result.error
    assert target.read_text() == "published"
    if arguments == ["init"]:
        assert (deck / "base.css").read_bytes() == (STANDARD / "references/base-template.css").read_bytes()
    else:
        assert script in result.stdout


@pytest.mark.asyncio
async def test_real_ppt_prepare_preserves_chat_and_atomically_refreshes_speech(tmp_path, retained, monkeypatch):
    pytest.importorskip("brotli", reason="Standard Skill fonttools[woff] dependency")
    target, guard = retained
    deck = tmp_path / "new deck"
    (deck / "plan").mkdir(parents=True)
    shutil.copyfile(STANDARD / "references/base-template.css", deck / "base.css")
    (deck / "plan/slide_01.md").write_text(
        "# Slide 01 — Hello\n## 最终屏显文案\n- title: Hello\n## 视觉实现\n"
        "- image_opportunity: typography_only\n## 来源\n- user-provided\n"
        "## 口语讲稿\nHello, this is the full spoken script.\n", encoding="utf-8",
    )
    speech = deck / "speech.md"
    speech.write_text("previous speech")
    guard.retain(speech)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    result = await run_script(tmp_path, guard, STANDARD / "scripts/deck.py", "prepare", deck, "--expected", "1", timeout=60)
    assert result.success, result.error
    assert "prepare:PASS" in result.stdout
    assert "full spoken script" in speech.read_text()
    assert (deck / "assets/fonts/manifest.json").is_file()
    assert target.read_text() == "published"


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("PPT_RENDER_BROWSER_TESTS") != "1", reason="installed Chromium smoke; platform CI opts in")
@pytest.mark.parametrize("width,height", [(1600, 900), (1920, 1080), (1200, 900)])
async def test_real_ppt_render_batch_runs_guarded_children_and_preserves_chat(tmp_path, retained, width, height):
    target, guard = retained
    deck = tmp_path / "new deck"
    (deck / "slides").mkdir(parents=True)
    (deck / "base.css").write_text(f":root{{--canvas-w:{width}px;--canvas-h:{height}px}}")
    slide = deck / "slides/slide_01.html"
    slide.write_text(f'<html><link rel="stylesheet" href="../base.css"><style>html,body{{margin:0;width:var(--canvas-w);height:var(--canvas-h)}} .slide{{width:var(--canvas-w);height:var(--canvas-h)}}</style><body><section class="slide"><h1>Hello</h1></section></body></html>')
    guard.retain(slide)
    result = await run_script(tmp_path, guard, STANDARD / "scripts/render.py", "--batch", deck, "--pages", "1", timeout=60)
    assert result.success, result.error
    from PIL import Image
    with Image.open(deck / "renders/slide_01.png") as png:
        assert png.size == (width * 2, height * 2)
    assert (deck / "renders/slide_01.png").stat().st_size > 1000
    assert set(json.loads((deck / "renders/render.json").read_text())["pages"]) == {"01"}
    assert target.read_text() == "published"
    assert slide.is_file()


@pytest.mark.asyncio
async def test_unexecuted_deletion_branches_do_not_block_any_script_mode(tmp_path, retained):
    target, guard = retained
    script = tmp_path / "not allowlisted.py"
    script.write_text('''import os, sys, tempfile, shutil
from pathlib import Path
def unrelated_cleanup():
    shutil.rmtree(sys.argv[2])
    Path(sys.argv[2]).unlink()
    os.replace(sys.argv[2], "elsewhere")
with tempfile.TemporaryDirectory() as directory:
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
        stream.write(b"temporary")
    os.unlink(stream.name)
print(sys.argv[1])
''')
    for mode in ("init", "prepare", "render", "--help"):
        result = await run_script(tmp_path, guard, script, mode, target)
        assert result.success, result.error
        assert mode in result.stdout
    assert target.read_text() == "published"


@pytest.mark.asyncio
@pytest.mark.parametrize("include_dir_fd", [False, True], ids=["python310", "python311plus"])
async def test_rmtree_audit_signatures_preserve_retention_checks(tmp_path, retained, include_dir_fd):
    target, guard = retained
    script = tmp_path / "audit_cleanup.py"
    script.write_text(
        "import sys\n"
        + "sys.audit('shutil.rmtree', sys.argv[1]"
        + (", None" if include_dir_fd else "")
        + ")\nprint('allowed')\n"
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    allowed = await run_script(tmp_path, guard, script, scratch)
    assert allowed.success, allowed.error
    blocked = await run_script(tmp_path, guard, script, target.parent)
    assert not blocked.success
    assert "CHAT_REFERENCE_PROTECTED" in blocked.error
    assert "IndexError" not in blocked.error
    assert target.read_text() == "published"


@pytest.mark.asyncio
async def test_inline_python_checks_executed_io_instead_of_unexecuted_functions(tmp_path, retained):
    target, guard = retained
    code = f"import os\ndef unused_cleanup():\n    os.unlink({str(target)!r})\nprint('help')"
    tool = BashTool(workspace_dir=str(tmp_path), bypass_dangerous_command_approval=True)
    result = await invoke_tool_once(tool, {"command": shlex.join([sys.executable, "-c", code])},
                                    ToolInvocationContext(chat_references=guard))
    assert result.success, result.error
    assert result.stdout == "help\n"
    assert target.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [
    "Path(sys.argv[1]).unlink()",
    "os.rename(sys.argv[1], 'moved.txt')",
    "os.replace(sys.argv[1], 'moved.txt')",
    "shutil.rmtree(Path(sys.argv[1]).parent)",
    "os.unlink(Path(sys.argv[1]).name, dir_fd=os.open(Path(sys.argv[1]).parent, os.O_RDONLY))",
    "subprocess.run(['rm', sys.argv[1]], check=True)",
    "subprocess.run(['alias', sys.argv[1]], executable=shutil.which('rm'), check=True)",
    "os.execv(shutil.which('rm'), ['alias', sys.argv[1]])",
    "os.posix_spawn(shutil.which('rm'), ['alias', sys.argv[1]], dict(os.environ))",
    "subprocess.run('rm ' + shlex.quote(sys.argv[1]), shell=True, check=True)",
    "os.system('rm ' + shlex.quote(sys.argv[1]))",
    "subprocess.run([sys.executable, '-c', 'import os,sys;os.remove(sys.argv[1])', sys.argv[1]], check=True)",
    "import imported_cleanup; imported_cleanup.delete(sys.argv[1])",
])
async def test_executed_dynamic_deletions_and_moves_remain_protected(tmp_path, retained, operation):
    target, guard = retained
    script = tmp_path / "cleanup.py"
    script.write_text("import os,sys,shutil,subprocess,shlex\nfrom pathlib import Path\n" + operation)
    (tmp_path / "imported_cleanup.py").write_text("def delete(path):\n    from pathlib import Path\n    Path(path).unlink()\n")
    result = await run_script(tmp_path, guard, script, target)
    assert not result.success
    assert "CHAT_REFERENCE_PROTECTED" in result.error
    assert result.permission_request is None
    assert target.read_text() == "published"


@pytest.mark.asyncio
async def test_direct_bash_still_checks_other_sessions_retention_markers(tmp_path, retained):
    target, _ = retained
    script = tmp_path / "dynamic.py"
    script.write_text("import os,sys\nos.remove(sys.argv[1])")
    tool = BashTool(workspace_dir=str(tmp_path), bypass_dangerous_command_approval=True)
    result = await tool.execute(command=shlex.join([sys.executable, str(script), str(target)]))
    assert not result.success
    assert "CHAT_REFERENCE_PROTECTED" in result.error
    assert target.read_text() == "published"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["-S", "-I", "-E", "-sS", "-X utf8 -S", "-W ignore -E"])
async def test_disabling_python_startup_guard_fails_closed(tmp_path, retained, prefix):
    target, guard = retained
    script = tmp_path / "dynamic.py"
    script.write_text("import os,sys\nos.remove(sys.argv[1])")
    tool = BashTool(workspace_dir=str(tmp_path), bypass_dangerous_command_approval=True)
    result = await invoke_tool_once(tool, {"command": shlex.join([
        sys.executable, *shlex.split(prefix), str(script), str(target),
    ])}, ToolInvocationContext(chat_references=guard))
    assert not result.success
    assert "CHAT_REFERENCE_PROTECTED" in result.error
    assert target.is_file()


@pytest.mark.asyncio
async def test_child_cannot_drop_guard_with_explicit_environment(tmp_path, retained):
    target, guard = retained
    script = tmp_path / "dynamic.py"
    script.write_text("import subprocess,sys\nsubprocess.run([sys.executable, '-c', 'import os,sys;os.remove(sys.argv[1])', sys.argv[1]], env={}, check=True)")
    result = await run_script(tmp_path, guard, script, target)
    assert not result.success
    assert "CHAT_REFERENCE_PROTECTED" in result.error
    assert target.is_file()


@pytest.mark.asyncio
async def test_background_python_keeps_its_policy_after_execute_returns(tmp_path, retained):
    target, guard = retained
    script = tmp_path / "background.py"
    script.write_text("import os,sys,time\ntime.sleep(.15)\nos.remove(sys.argv[1])")
    result = await run_script(tmp_path, guard, script, target, run_in_background=True)
    assert result.success
    job = BackgroundShellManager.get(result.bash_id)
    try:
        await asyncio.wait_for(job.process.wait(), 5)
        await job.process._box_agent_guard_task
        assert job.process.returncode != 0
        assert target.read_text() == "published"
    finally:
        await BackgroundShellManager.terminate(result.bash_id)


@pytest.mark.asyncio
async def test_guard_endpoint_and_cached_environment_are_isolated_per_invocation(tmp_path, retained):
    target, guard = retained
    script = tmp_path / "cleanup.py"
    script.write_text("import os,sys,time\ntime.sleep(.1)\nos.remove(sys.argv[1])")
    tool = BashTool(workspace_dir=str(tmp_path), bypass_dangerous_command_approval=True)
    cached_env = tool._subprocess_env.copy() if tool._subprocess_env is not None else None
    other = tmp_path / "another.txt"
    other.write_text("other session")
    other_guard = ChatReferences(tmp_path)
    other_guard.retain(other)
    # An allowed deletion belongs only to this call; it cannot authorize the
    # second invocation, even when both use the same tool instance.
    guard.messages = [Message(role="user", content=f"请删除 {target}")]
    code = f"import os,time;time.sleep(.1);os.remove({str(target)!r})"
    arguments = {"command": shlex.join([sys.executable, "-c", code])}
    request, _ = guard.check("bash", arguments)
    assert request.permission_request
    async def approved():
        return await invoke_tool_once(tool, arguments, ToolInvocationContext(chat_references=guard),
                                      approved_permission_request=request.permission_request)
    first, second = await asyncio.gather(approved(), run_script(tmp_path, other_guard, script, other, tool=tool))
    assert first.success, first.error
    assert not second.success
    assert not target.exists()
    assert other.read_text() == "other session"
    assert tool._subprocess_env == cached_env


@pytest.mark.asyncio
async def test_shell_variable_python_still_guards_dynamic_deletion(tmp_path, retained):
    target, guard = retained
    script = tmp_path / "dynamic.py"
    script.write_text("import os,sys\nos.remove(sys.argv[1])")
    command = f"PYTHON={shlex.quote(sys.executable)}; \"$PYTHON\" {shlex.quote(str(script))} {shlex.quote(str(target))}"
    tool = BashTool(workspace_dir=str(tmp_path), bypass_dangerous_command_approval=True)
    result = await invoke_tool_once(tool, {"command": command}, ToolInvocationContext(chat_references=guard))
    assert not result.success
    assert "CHAT_REFERENCE_PROTECTED" in result.error
    assert target.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["env -i", "env --unset=PYTHONPATH", "unset PYTHONPATH;", "export PYTHONPATH=/tmp;"])
async def test_shell_cannot_strip_python_guard_environment(tmp_path, retained, prefix):
    target, guard = retained
    script = tmp_path / "dynamic.py"
    script.write_text("import os,sys\nos.remove(sys.argv[1])")
    command = prefix + " " + shlex.join([sys.executable, str(script), str(target)])
    tool = BashTool(workspace_dir=str(tmp_path), bypass_dangerous_command_approval=True)
    result = await invoke_tool_once(tool, {"command": command}, ToolInvocationContext(chat_references=guard))
    assert not result.success
    assert "CHAT_REFERENCE_PROTECTED" in result.error
    assert target.is_file()


@pytest.mark.parametrize("with_dir_fd", [False, True])
@pytest.mark.parametrize("protected", [False, True])
def test_rmtree_audit_signatures_preserve_deletion_policy(tmp_path, with_dir_fd, protected):
    from box_agent.tools.python_mutation_guard import PythonMutationGuard

    directory = tmp_path / "files"
    directory.mkdir()
    target = directory / "report.txt"
    target.write_text("keep")
    references = ChatReferences(tmp_path)
    if protected:
        references.retain(target)
    with bind_chat_references(references):
        guard = PythonMutationGuard(None)
    try:
        # Exercise both Python 3.10 and newer audit signatures on every runner.
        arguments = ", None" if with_dir_fd else ""
        result = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.audit('shutil.rmtree', sys.argv[1]{arguments})",
             str(directory)],
            env=guard.environment, capture_output=True, text=True, timeout=5,
        )
        if protected:
            assert result.returncode != 0
            assert "CHAT_REFERENCE_PROTECTED" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
        assert target.read_text() == "keep"
    finally:
        guard.close()


@pytest.mark.parametrize("failure", ["closed-endpoint", "broken-bootstrap"])
def test_guard_failure_never_falls_back_to_unguarded_python(tmp_path, monkeypatch, failure):
    import box_agent.tools.python_mutation_guard as module

    target = tmp_path / "keep.txt"
    target.write_text("keep")
    if failure == "broken-bootstrap":
        monkeypatch.setattr(module, "_BOOTSTRAP", "raise RuntimeError('injected startup failure')")
    guard = module.PythonMutationGuard(None)
    try:
        if failure == "closed-endpoint":
            guard._server.shutdown()
            guard._server.server_close()
        result = subprocess.run([sys.executable, "-c", "import os,sys;os.unlink(sys.argv[1])", str(target)],
                                env=guard.environment, capture_output=True, text=True, timeout=5)
        assert result.returncode != 0
        assert "CHAT_REFERENCE_PROTECTED" in result.stderr
        assert target.read_text() == "keep"
    finally:
        guard.close()
