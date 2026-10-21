from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.artifact_publication import metadata_path, retain_chat_file
from box_agent.tools.setup import add_workspace_tools
from box_agent.tools.skill_scratch import (
    SKILL_SCRATCH_DIR_NAME,
    cleanup_skill_scratch_dir,
    prepare_skill_scratch_dir,
)


@pytest.mark.parametrize(
    ("first_owner", "second_owner"),
    [(None, None), (None, "acp-session"), ("acp-session", None)],
)
def test_workspace_tool_cleanup_preserves_other_sessions_scratch(
    tmp_path: Path, first_owner: str | None, second_owner: str | None
) -> None:
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    first_tools = []
    first = add_workspace_tools(
        first_tools, config, tmp_path, process_owner_id=first_owner
    )
    second = add_workspace_tools(
        [], config, tmp_path, process_owner_id=second_owner
    )
    first_file = first.path / "first-session.txt"
    first_file.write_text("remove", encoding="utf-8")
    second_file = second.path / "second-session.txt"
    second_file.write_text("keep", encoding="utf-8")

    cleanup_skill_scratch_dir(first)

    assert not first_file.exists()
    assert second_file.read_text(encoding="utf-8") == "keep"
    bash_tool = next(tool for tool in first_tools if tool.name == "bash")
    assert Path(bash_tool.workspace_dir) == tmp_path.resolve()


def test_prepare_and_cleanup_skill_scratch_dir(tmp_path: Path) -> None:
    scratch = prepare_skill_scratch_dir(tmp_path)
    scratch_dir = scratch.path
    task_dir = scratch_dir / "roadmap-task"
    task_dir.mkdir()
    (task_dir / "draft.json").write_text("{}", encoding="utf-8")
    link = scratch_dir / "external-link"
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")

    removed = cleanup_skill_scratch_dir(scratch)

    assert sorted(Path(item).name for item in removed) == [
        "external-link",
        "roadmap-task",
    ]
    assert scratch_dir.is_dir()
    assert not list(scratch_dir.iterdir())
    assert outside.read_text(encoding="utf-8") == "keep"


def test_prepare_skill_scratch_dir_accepts_session_private_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch_root = workspace / ".box-agent" / "scratch" / "session-a"

    scratch = prepare_skill_scratch_dir(
        workspace,
        scratch_root_dir=scratch_root,
    )

    assert scratch.path == scratch_root.resolve()
    assert scratch.path.is_dir()
    assert not (workspace / SKILL_SCRATCH_DIR_NAME).exists()


def test_scratch_cleanup_preserves_images_already_referenced_by_chat(tmp_path: Path) -> None:
    scratch = prepare_skill_scratch_dir(tmp_path)
    folder = scratch.path / "qa"
    folder.mkdir()
    image = folder / "shown.png"
    image.write_bytes(b"shown QA")
    retain_chat_file(image)
    disposable = scratch.path / "unshown.png"
    disposable.write_bytes(b"temporary QA")

    removed = cleanup_skill_scratch_dir(scratch)

    assert str(disposable) in removed
    assert not disposable.exists()
    assert image.read_bytes() == b"shown QA"
    assert metadata_path(image).exists()


def test_scratch_cleanup_preserves_directories_with_incomplete_retention_checks(tmp_path, monkeypatch):
    import box_agent.artifact_publication as publication

    scratch = prepare_skill_scratch_dir(tmp_path)
    folder = scratch.path / "cache"
    folder.mkdir()
    for i in range(4):
        (folder / str(i)).write_text("keep")
    monkeypatch.setattr(publication, "_REFERENCE_SCAN_MAX_ENTRIES", 2)
    assert cleanup_skill_scratch_dir(scratch) == []
    assert len(list(folder.iterdir())) == 4


def test_prepare_skill_scratch_dir_rejects_root_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch_root = tmp_path / "runtime" / "session-a"

    with pytest.raises(RuntimeError, match="must stay within the workspace"):
        prepare_skill_scratch_dir(workspace, scratch_root_dir=scratch_root)

    assert not scratch_root.exists()


def test_prepare_skill_scratch_dir_rejects_parent_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    parent_link = workspace / ".box-agent"
    try:
        parent_link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")

    with pytest.raises(RuntimeError, match="must contain only real directories"):
        prepare_skill_scratch_dir(
            workspace,
            scratch_root_dir=workspace / ".box-agent" / "scratch" / "session-a",
        )

    assert not (outside / "scratch").exists()


def test_prepare_skill_scratch_dir_rejects_nested_parent_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch_parent = workspace / ".box-agent"
    scratch_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    nested_link = scratch_parent / "scratch"
    try:
        nested_link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")

    with pytest.raises(RuntimeError, match="must contain only real directories"):
        prepare_skill_scratch_dir(
            workspace,
            scratch_root_dir=nested_link / "session-a",
        )

    assert not (outside / "session-a").exists()


def test_prepare_skill_scratch_dir_rejects_reserved_path_symlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    scratch_dir = tmp_path / SKILL_SCRATCH_DIR_NAME
    try:
        scratch_dir.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")

    with pytest.raises(RuntimeError, match="must contain only real directories"):
        prepare_skill_scratch_dir(tmp_path)


def test_cleanup_skill_scratch_dir_rejects_replaced_root(tmp_path: Path) -> None:
    scratch = prepare_skill_scratch_dir(tmp_path)
    moved = tmp_path / "moved-scratch"
    scratch.path.rename(moved)
    scratch.path.mkdir()
    replacement = scratch.path / "keep.txt"
    replacement.write_text("keep", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must be a real directory"):
        cleanup_skill_scratch_dir(scratch)

    assert replacement.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_qa_scratch_outputs_are_reclaimed_without_removing_retained_qa(
    tmp_path: Path, posix_shell,
) -> None:
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    tools = []
    scratch = add_workspace_tools(tools, config, tmp_path)
    bash = next(tool for tool in tools if tool.name == "bash")
    retained = tmp_path / "qa" / "visual" / "existing.png"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"retained preview")
    report = tmp_path / "qa" / "report.json"
    report.write_text('{"ok": true}', encoding="utf-8")

    result = await bash.execute(
        command='mkdir -p "$BOX_AGENT_SCRATCH_DIR/visual" && '
        'printf thumbnail > "$BOX_AGENT_SCRATCH_DIR/visual/crop.png"'
    )
    assert result.success, result.error
    assert result.permission_request is None
    crop = scratch.path / "visual" / "crop.png"
    assert crop.read_bytes() == b"thumbnail"

    cleanup_skill_scratch_dir(scratch)

    assert not crop.exists()
    assert retained.read_bytes() == b"retained preview"
    assert report.read_text(encoding="utf-8") == '{"ok": true}'
