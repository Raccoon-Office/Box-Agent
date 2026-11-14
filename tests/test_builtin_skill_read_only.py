"""Task tools cannot rewrite the bundled code they use to validate output."""
import os
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
    command = "Set-Content -LiteralPath result.txt -Value finished -NoNewline" if os.name == "nt" else "printf 'finished' > result.txt"
    allowed = await tool.execute(command=command)
    assert allowed.success
    assert (tmp_path / 'result.txt').read_text() == 'finished'


@pytest.mark.parametrize("arguments", [
    "-R source/. '{target}'",
    "-R -t '{target}' source/.",
    "-R -t'{target}' source/.",
    "-Rt'{target}' source/.",
    "-R --target-directory '{target}' source/.",
    "-R --target-directory='{target}' source/.",
    "--rec source/. '{target}'",
    "--parents source/skills/validator.js '{target}'",
    "-R --target-dir='{target}' source/.",
])
@pytest.mark.parametrize("executable", ["cp", "cp.exe"])
def test_shell_copy_target_options_cannot_overwrite_bundled_ancestors(tmp_path, bundled, arguments, executable):
    root, _ = bundled
    command = f"{executable} " + arguments.format(target=root.parent)
    error = safety.builtin_skill_command_write_error(command, tmp_path)
    assert error is not None
    assert "BUILTIN_SKILL_READ_ONLY" in error


@pytest.mark.parametrize("arguments", [
    "-- -t copied.txt",
    "-S -t source copied.txt",
    "-St source copied.txt",
    "-- -R .",
    "-S -R source .",
])
def test_shell_copy_t_in_file_names_and_suffix_values_is_not_a_target_option(tmp_path, bundled, arguments):
    assert safety.builtin_skill_command_write_error(f"cp {arguments}", tmp_path) is None


@pytest.mark.parametrize("target_option", [
    "-t '{target}'", "-t'{target}'", "--target-directory '{target}'", "--target-directory='{target}'",
])
def test_install_target_options_reject_bundled_destinations(tmp_path, bundled, target_option):
    root, _ = bundled
    command = "install " + target_option.format(target=root) + " validator.js"
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command, tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX recursive cp syntax")
@pytest.mark.asyncio
@pytest.mark.parametrize("target_form", ["root", "alias", "relative"])
async def test_bash_rejects_recursive_overwrite_of_bundled_descendants(
    tmp_path, monkeypatch, target_form,
):
    from box_agent.tools.bash_tool import BashTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    product = tmp_path / "product"
    builtin = product / "skills"
    builtin.mkdir(parents=True)
    validator = builtin / "validator.js"
    validator.write_text("original")
    source = workspace / "input"
    (source / "skills").mkdir(parents=True)
    (source / "skills" / "validator.js").write_text("changed")
    entry = tmp_path / "product-entry"
    entry.symlink_to(product, target_is_directory=True)
    monkeypatch.setattr(safety, "BUILTIN_SKILLS_ROOT", builtin)
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(product))
    engine = PermissionEngine(CapabilityPolicy(session_workspace_root=str(workspace)), workspace)
    engine._builtin_skills_dir = builtin.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    target = entry if target_form == "alias" else product
    command = f'cp -R "{source}/." "{target}/"'
    if target_form == "relative":
        command = f'cd "{entry}" && cp -R "{source}/." .'
    result = await tool.execute(command=command)
    assert not result.success
    assert "BUILTIN_SKILL_READ_ONLY" in result.error
    assert result.permission_request is None
    assert validator.read_text() == "original"

    exported = workspace / "exported"
    result = await tool.execute(command=f'cp -R "{product}" "{exported}"')
    assert result.success, result.error
    assert (exported / "skills" / "validator.js").read_text() == "original"
    ordinary = workspace / "ordinary.txt"
    ordinary.write_text("ordinary product data")
    result = await tool.execute(command=f'cp "{ordinary}" "{product}/"')
    assert result.success, result.error
    result = await tool.execute(command=f'cp "{ordinary}" "{product / "ordinary.txt"}"')
    assert result.success, result.error
    result = await tool.execute(command=f'printf updated > "{product / "ordinary.txt"}"')
    assert result.success, result.error
    assert (product / "ordinary.txt").read_text() == "updated"
