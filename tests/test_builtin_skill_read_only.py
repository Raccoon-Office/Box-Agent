"""Task tools cannot rewrite the bundled code they use to validate output."""
from pathlib import Path

import pytest

from box_agent.tools import safety
from box_agent.tools.file_tools import WriteTool, AppendTool, EditTool
from box_agent.tools.staged_file_write_tool import StagedFileWriteTool
from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine


@pytest.fixture
def bundled(tmp_path, monkeypatch):
    root = tmp_path / "bundled"
    root.mkdir()
    monkeypatch.setattr(safety, "BUILTIN_SKILLS_ROOT", root)
    target = root / "validator.js"
    target.write_text("original")
    return root, target


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["write", "append", "edit", "staged"])
async def test_file_tools_reject_bundled_writes_even_with_full_access(tmp_path, bundled, kind):
    root, target = bundled
    link = tmp_path / "linked.js"
    link.symlink_to(target)
    classes = {"write": WriteTool, "append": AppendTool, "edit": EditTool, "staged": StagedFileWriteTool}
    tool = classes[kind](workspace_dir=str(tmp_path), allow_full_access=True)
    args = {"path": str(link)}
    if kind == "edit":
        args.update(old_str="original", new_str="bypass")
    elif kind == "staged":
        args.update(action="begin", expected_chunks=1)
    else:
        args.update(content="bypass")
    result = await tool.execute(**args)
    assert not result.success
    assert "BUILTIN_SKILL_READ_ONLY" in result.error
    assert target.read_text() == "original"
    assert not result.permission_request
    if kind != "staged":
        args["path"] = str(tmp_path / "output.txt")
        if kind == "edit":
            Path(args["path"]).write_text("original")
        assert (await tool.execute(**args)).success


@pytest.mark.parametrize("scope", ["session_workspace", "user_home", "full_access"])
def test_permission_scope_does_not_make_bundled_code_writable(tmp_path, bundled, scope):
    root, target = bundled
    engine = PermissionEngine(CapabilityPolicy(filesystem_scope=scope, allowed_directories=(str(root),)), tmp_path)
    engine._builtin_skills_dir = root
    denied = engine.check("filesystem.write", {"path": str(target)})
    assert not denied.allowed
    assert "BUILTIN_SKILL_READ_ONLY" in denied.reason
    assert denied.permission_request is None
    assert engine.check("filesystem.read", {"path": str(target)}, tool_name="bash").allowed


@pytest.mark.parametrize("command", [
    "sed -i '' s/original/bypass/ '{target}'",
    "cp patch.js '{target}'",
    "printf bypass > '{target}'",
    "cd '{root}' && node -e \"require('fs').writeFileSync('validator.js','bypass')\"",
    "python -c \"from pathlib import Path; Path('{target}').write_text('bypass')\"",
])
def test_shell_guard_rejects_direct_bundled_code_mutations(tmp_path, bundled, command):
    root, target = bundled
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command.format(root=root, target=target), tmp_path)
    assert target.read_text() == "original"


@pytest.mark.parametrize("command", [
    "node '{target}' --out index.html",
    "cat '{target}'",
    "node -e \"console.log(require('fs').readFileSync('{target}','utf8'))\"",
    "cp '{target}' './copy.js'",
])
def test_shell_guard_keeps_bundled_read_and_execute_available(tmp_path, bundled, command):
    root, target = bundled
    assert safety.builtin_skill_command_write_error(command.format(target=target), tmp_path) is None


@pytest.mark.asyncio
async def test_bash_blocks_bundled_mutation_but_writes_workspace_output(tmp_path, bundled):
    from box_agent.tools.bash_tool import BashTool
    _, target = bundled
    tool = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    denied = await tool.execute(command=f"node -e \"require('fs').writeFileSync('{target}','bypass')\"")
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert target.read_text() == "original"
    allowed = await tool.execute(command="printf 'finished' > result.txt")
    assert allowed.success
    assert (tmp_path / 'result.txt').read_text() == 'finished'
