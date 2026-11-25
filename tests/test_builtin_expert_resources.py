"""Shared expert packages stay readable but are never task output."""

from pathlib import Path

import pytest

from box_agent.tools import safety
from box_agent.tools.file_tools import ReadTool, WriteTool, AppendTool, EditTool
from box_agent.tools.staged_file_write_tool import StagedFileWriteTool
from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine


@pytest.fixture
def cached_expert(tmp_path, monkeypatch):
    root = tmp_path / "profile/experts/packages"
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    target = root / "example/package/skills/skill-0/references/example.txt"
    target.parent.mkdir(parents=True)
    target.write_text("EXPERT_RESOURCE_OK")
    return root, target


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session_workspace", "user_home", "full_access"])
async def test_expert_reference_is_readable_and_product_grants_cannot_make_it_writable(
    tmp_path, cached_expert, scope,
):
    root, target = cached_expert
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = PermissionEngine(CapabilityPolicy(filesystem_scope=scope), workspace)
    engine._product_dirs = (root.parent,)
    read = ReadTool(str(workspace), allow_full_access=False, permission_engine=engine)
    result = await read.execute(str(target))
    assert result.success and "EXPERT_RESOURCE_OK" in result.content
    assert engine.check("filesystem.read", {"path": str(target)}, tool_name="bash").allowed
    for tool in (None, "bash"):
        denied = engine.check("filesystem.write", {"path": str(target)}, tool_name=tool)
        assert not denied.allowed and "EXPERT_PACKAGE_READ_ONLY" in denied.reason
        assert denied.permission_request is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["write", "append", "edit", "staged"])
@pytest.mark.parametrize("linked", [False, True])
async def test_file_tools_cannot_modify_expert_resources_even_with_full_access(
    tmp_path, cached_expert, kind, linked,
):
    _, target = cached_expert
    requested = target
    if linked:
        requested = tmp_path / "linked.txt"
        try:
            requested.symlink_to(target)
        except OSError:
            pytest.skip("file symlinks unavailable on this platform")
    classes = {"write": WriteTool, "append": AppendTool, "edit": EditTool, "staged": StagedFileWriteTool}
    tool = classes[kind](workspace_dir=str(tmp_path), allow_full_access=True)
    args = {"path": str(requested)}
    if kind == "edit":
        args.update(old_str="EXPERT_RESOURCE_OK", new_str="changed")
    elif kind == "staged":
        args.update(action="begin", expected_chunks=1)
    else:
        args.update(content="changed")
    result = await tool.execute(**args)
    assert not result.success and "EXPERT_PACKAGE_READ_ONLY" in result.error
    assert target.read_text() == "EXPERT_RESOURCE_OK"


@pytest.mark.parametrize("command", [
    "printf changed > '{target}'",
    "python -c \"from pathlib import Path; Path('{target}').write_text('changed')\"",
    "rm -rf '{root}'",
    "rm -rf '{runtime}'",
])
def test_shell_guard_rejects_expert_mutations_and_removing_its_parent(tmp_path, cached_expert, command):
    root, target = cached_expert
    error = safety.builtin_skill_command_write_error(
        command.format(target=target, root=root, runtime=root.parent), tmp_path,
    )
    assert error and "EXPERT_PACKAGE_READ_ONLY" in error
    assert target.read_text() == "EXPERT_RESOURCE_OK"


def test_reading_and_copying_expert_resources_to_task_output_stays_available(tmp_path, cached_expert):
    _, target = cached_expert
    for command in (f"cat '{target}'", f"cp '{target}' '{tmp_path / 'copy.txt'}'"):
        assert safety.builtin_skill_command_write_error(command, tmp_path) is None
    assert safety.builtin_skill_write_error(tmp_path / "user-profile/experts/custom/output.txt") is None


def test_expert_resources_do_not_shadow_python_module():
    from box_agent import experts

    assert Path(experts.__file__).name == "experts.py"
    assert experts.ExpertSessionContext is not None
