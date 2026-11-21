from __future__ import annotations

import errno
import os
import zlib
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.artifact_publication import metadata_path, write_metadata
from box_agent.session_log import SessionLog, SessionLogDurabilityError
from box_agent.tools.chat_references import ChatReferences, retain_scratch_chat_references
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
    legacy_preview = tmp_path / ".box-agent-scratch" / "old-session" / "preview.png"
    legacy_preview.parent.mkdir(parents=True)
    legacy_preview.write_bytes(b"old preview")
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

    assert first.path.parent == tmp_path.resolve() / ".box-agent" / "scratch"
    assert second.path.parent == first.path.parent
    assert not first_file.exists()
    assert second_file.read_text(encoding="utf-8") == "keep"
    assert legacy_preview.read_bytes() == b"old preview"
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


@pytest.mark.parametrize("location", [".box-agent-scratch", ".box-agent/scratch"])
def test_scratch_cleanup_preserves_images_already_referenced_by_chat(
    tmp_path: Path, location: str,
) -> None:
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / location / "session",
    )
    folder = scratch.path / "qa"
    folder.mkdir()
    image = folder / "shown.png"
    image.write_bytes(b"shown QA")
    ChatReferences(tmp_path).retain_text(f"![Preview]({image.as_uri()})")
    disposable = scratch.path / "unshown.png"
    disposable.write_bytes(b"temporary QA")

    removed = cleanup_skill_scratch_dir(scratch)

    assert str(disposable) in removed
    assert not disposable.exists()
    assert image.read_bytes() == b"shown QA"
    assert metadata_path(image).exists()


def test_scratch_cleanup_preserves_nested_preview_symlink_referenced_by_chat(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    target = tmp_path / "rendered.png"
    target.write_bytes(b"preview")
    preview = scratch.path / "bundle" / "preview.png"
    preview.parent.mkdir()
    try:
        preview.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")

    retain_scratch_chat_references(tmp_path, f"![Preview]({preview.as_uri()})")
    cleanup_skill_scratch_dir(scratch)

    assert preview.is_symlink()
    assert preview.read_bytes() == b"preview"


def test_scratch_retention_keeps_other_previews_when_archive_decoding_fails(tmp_path, monkeypatch):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    document = scratch.path / "bundle" / "preview.docx"
    document.parent.mkdir()
    with ZipFile(document, "w") as archive:
        archive.writestr("_rels/.rels", "<Relationships/>")
    image = scratch.path / "preview.png"
    image.write_bytes(b"preview")
    disposable = scratch.path / "unshown.txt"
    disposable.touch()

    def fail_decompression(*args, **kwargs):
        raise zlib.error("corrupt compressed relationship")

    monkeypatch.setattr(ZipFile, "read", fail_decompression)
    retain_scratch_chat_references(
        tmp_path, f"[Document]({document.as_uri()}) [Image]({image.as_uri()})",
    )
    cleanup_skill_scratch_dir(scratch)

    assert document.is_file()
    assert image.read_bytes() == b"preview"
    assert not disposable.exists()


def test_invalid_top_level_preview_does_not_retain_the_session_scratch_root(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    preview = scratch.path / "preview.docx"
    with ZipFile(preview, "w") as archive:
        archive.writestr("_rels/.rels", "<incomplete")
    disposable = scratch.path / "disposable.txt"
    disposable.touch()

    log = SessionLog.create(tmp_path / "sessions", session_id="preview", cwd=tmp_path)
    try:
        retain_scratch_chat_references(tmp_path, f"[Preview]({preview.as_uri()})", log)
        cleanup_skill_scratch_dir(scratch)
        assert preview.is_file()
        assert not disposable.exists()
        references = ChatReferences(tmp_path, session_log=log)
        preview.unlink()
        references.deleted(frozenset({preview, scratch.path}))
        later = scratch.path / "later.txt"
        later.touch()
        cleanup_skill_scratch_dir(scratch)
        assert not later.exists()
    finally:
        log.close()


def test_preview_html_base_does_not_retain_the_session_scratch_root(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    preview = scratch.path / "preview.html"
    preview.write_text('<base href="./"><img src="chart.png">', encoding="utf-8")
    chart = scratch.path / "chart.png"
    chart.write_bytes(b"chart")
    disposable = scratch.path / "disposable.txt"
    disposable.touch()

    with closing(SessionLog.create(tmp_path / "sessions", session_id="preview", cwd=tmp_path)) as log:
        retain_scratch_chat_references(tmp_path, f"[Preview]({preview.as_uri()})", log)
        cleanup_skill_scratch_dir(scratch)

        assert preview.is_file()
        assert chart.read_bytes() == b"chart"
        assert not disposable.exists()
        assert scratch.path not in ChatReferences(tmp_path, session_log=log).paths


def test_invalid_top_level_preview_keeps_its_declared_scratch_dependency(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    preview = scratch.path / "preview.docx"
    with ZipFile(preview, "w") as archive:
        archive.writestr("_rels/.rels", "<incomplete")
    chart = scratch.path / "chart.png"
    chart.write_bytes(b"chart")
    write_metadata(preview, {"dependencies": ["chart.png"]}, tmp_path)
    disposable = scratch.path / "disposable.txt"
    disposable.touch()

    retain_scratch_chat_references(tmp_path, f"[Preview]({preview.as_uri()})")
    cleanup_skill_scratch_dir(scratch)

    assert preview.is_file()
    assert chart.read_bytes() == b"chart"
    assert not disposable.exists()


def test_scratch_cleanup_preserves_preview_after_temporary_permission_error(tmp_path, monkeypatch):
    from box_agent import artifact_publication

    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    preview = scratch.path / "bundle" / "preview.png"
    preview.parent.mkdir()
    preview.write_bytes(b"preview")
    disposable = scratch.path / "disposable.txt"
    disposable.write_text("temporary", encoding="utf-8")

    def fail_cache_write(*args, **kwargs):
        raise PermissionError("retention cache is read-only")

    monkeypatch.setattr(artifact_publication, "write_metadata", fail_cache_write)
    retain_scratch_chat_references(tmp_path, f"![Preview]({preview.as_uri()})")
    stat_path = Path.stat
    path_exists = Path.exists

    def fail_preview_stat(path, *args, **kwargs):
        if path == preview:
            raise PermissionError(errno.EACCES, "preview cannot be inspected", path)
        return stat_path(path, *args, **kwargs)

    def hide_inaccessible_preview(path, *args, **kwargs):
        return path != preview and path_exists(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        # Python 3.14's exists() hides the permission error reported by stat().
        patch.setattr(Path, "stat", fail_preview_stat)
        patch.setattr(Path, "exists", hide_inaccessible_preview)
        assert cleanup_skill_scratch_dir(scratch) == [str(disposable)]

    cleanup_skill_scratch_dir(scratch)
    assert preview.read_bytes() == b"preview"


def test_scratch_cleanup_removes_replacement_after_explicit_reference_deletion(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    preview = scratch.path / "preview.png"
    preview.write_bytes(b"preview")
    references = ChatReferences(tmp_path)
    references.retain(preview)

    preview.unlink()
    references.deleted(frozenset({preview}))
    preview.write_bytes(b"disposable replacement")
    cleanup_skill_scratch_dir(scratch)

    assert not preview.exists()


def test_reload_does_not_retain_replacement_of_deleted_preview_symlink(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    original = tmp_path / "original.png"
    original.touch()
    preview = scratch.path / "preview.png"
    try:
        preview.symlink_to(original)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")
    link = f"[Preview]({preview.as_uri()})"

    log = SessionLog.create(tmp_path / "sessions", session_id="preview", cwd=tmp_path)
    try:
        log.append("user/message", {"content": link})
        retain_scratch_chat_references(tmp_path, link, log)
        references = ChatReferences(tmp_path, session_log=log)
        preview.unlink()
        references.deleted(frozenset({preview}))
        replacement = scratch.path / "replacement.png"
        replacement.touch()
        preview.symlink_to(replacement)

        ChatReferences(tmp_path, session_log=log)
        cleanup_skill_scratch_dir(scratch)

        assert not preview.is_symlink()
        assert not replacement.exists()
        assert original.is_file()
    finally:
        log.close()


def test_scratch_cleanup_preserves_streamed_previews_when_session_log_append_fails(
    tmp_path, monkeypatch,
):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    preview = scratch.path / "preview.png"
    preview.write_bytes(b"preview")
    other = scratch.path / "other.png"
    other.write_bytes(b"other")
    disposable = scratch.path / "disposable.png"
    disposable.write_bytes(b"disposable")
    log = SessionLog.create(tmp_path / "sessions", session_id="preview", cwd=tmp_path)

    def fail_reference_append(*args, **kwargs):
        raise SessionLogDurabilityError("could not write reference")

    try:
        monkeypatch.setattr(log, "append", fail_reference_append)
        with pytest.raises(SessionLogDurabilityError, match="could not write reference"):
            retain_scratch_chat_references(
                tmp_path, f"![Preview]({preview.as_uri()}) ![Other]({other.as_uri()})", log,
            )
    finally:
        log.close()
    cleanup_skill_scratch_dir(scratch)

    assert preview.read_bytes() == b"preview"
    assert other.read_bytes() == b"other"
    assert not disposable.exists()


@pytest.mark.parametrize("via_symlink", [False, True], ids=["direct", "symlink"])
def test_scratch_retention_preserves_a_direct_preview_during_a_transient_stat_failure(
    tmp_path, monkeypatch, via_symlink,
):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    preview = scratch.path / "preview.png"
    preview.write_bytes(b"preview")
    inaccessible = scratch.path / "unavailable.png"
    inaccessible.write_bytes(b"temporarily unavailable")
    linked = inaccessible
    if via_symlink:
        linked = tmp_path / "preview-link.png"
        try:
            linked.symlink_to(inaccessible)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"symlinks are unavailable in this environment: {error}")
    disposable = scratch.path / "disposable.txt"
    disposable.touch()
    stat_path = os.stat

    def fail_stat(path, *args, **kwargs):
        if path == linked:
            raise PermissionError("path cannot be inspected")
        return stat_path(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", fail_stat)
        retain_scratch_chat_references(
            tmp_path,
            f"[Missing]({(scratch.path / 'missing.png').as_uri()}) [Other]({linked.as_uri()}) "
            f"![Preview]({preview.as_uri()})",
        )
    cleanup_skill_scratch_dir(scratch)

    assert preview.read_bytes() == b"preview"
    assert inaccessible.read_bytes() == b"temporarily unavailable"
    assert not disposable.exists()


def test_workspace_report_keeps_its_scratch_dependency(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    stylesheet = scratch.path / "report.css"
    stylesheet.write_text("body {}", encoding="utf-8")
    report = tmp_path / "report.html"
    report.write_text(f'<link rel="stylesheet" href="{stylesheet.as_uri()}">', encoding="utf-8")
    unrelated = tmp_path / "unrelated.html"
    unrelated.write_text("<p>Standalone</p>", encoding="utf-8")
    disposable = scratch.path / "disposable.txt"
    disposable.touch()

    log = SessionLog.create(tmp_path / "sessions", session_id="preview", cwd=tmp_path)
    try:
        retain_scratch_chat_references(
            tmp_path, f"[Report]({report.as_uri()}) [Other]({unrelated.as_uri()})", log,
        )
        cleanup_skill_scratch_dir(scratch)

        assert stylesheet.read_text(encoding="utf-8") == "body {}"
        assert not disposable.exists()
        recorded = list(log.events_of_type("chat/reference"))
        assert [event["data"]["paths"] for event in recorded] == [sorted(map(str, (report, stylesheet)))]
    finally:
        log.close()


def test_workspace_report_with_damaged_attachment_does_not_retain_workspace_root(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    stylesheet = scratch.path / "report.css"
    stylesheet.write_text("body {}", encoding="utf-8")
    appendix = tmp_path / "appendix.docx"
    with ZipFile(appendix, "w") as archive:
        archive.writestr("_rels/.rels", "<incomplete")
    report = tmp_path / "report.html"
    report.write_text(
        f'<link rel="stylesheet" href="{stylesheet.as_uri()}"><a href="appendix.docx">Appendix</a>',
        encoding="utf-8",
    )
    disposable = scratch.path / "disposable.txt"
    disposable.touch()

    with closing(SessionLog.create(tmp_path / "sessions", session_id="preview", cwd=tmp_path)) as log:
        retain_scratch_chat_references(tmp_path, f"[Report]({report.as_uri()})", log)
        cleanup_skill_scratch_dir(scratch)

        assert stylesheet.read_text(encoding="utf-8") == "body {}"
        assert not disposable.exists()
        assert tmp_path not in ChatReferences(tmp_path, session_log=log).paths


@pytest.mark.parametrize(
    ("operation", "suppress_error"),
    [("exists", False), ("resolve", False), ("exists", True)],
    ids=["exists-error", "resolve-error", "exists-hides-error"],
)
def test_scratch_reference_replay_keeps_preview_when_inspection_temporarily_fails(
    tmp_path, monkeypatch, operation, suppress_error,
):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    preview = scratch.path / "preview.png"
    preview.write_bytes(b"preview")
    other = scratch.path / "other.png"
    other.write_bytes(b"other")
    inspect = getattr(Path, operation)

    def fail_inspection(path, *args, **kwargs):
        if path == preview:
            if suppress_error:
                return False
            raise PermissionError("preview cannot be inspected")
        return inspect(path, *args, **kwargs)

    with closing(SessionLog.create(tmp_path / "sessions", session_id="preview", cwd=tmp_path)) as log:
        log.append(
            "chat/reference", {"root": str(preview), "paths": [str(preview), str(other)]},
            ignorable=True,
        )
        with monkeypatch.context() as patch:
            patch.setattr(Path, operation, fail_inspection)
            restored = ChatReferences(tmp_path, session_log=log)

        assert {preview, other} <= restored.paths
        cleanup_skill_scratch_dir(scratch)
        assert preview.read_bytes() == b"preview"
        assert other.read_bytes() == b"other"


@pytest.mark.parametrize("operation", ["resolve", "stat", "exists"])
def test_scratch_retention_preserves_preview_with_unstatable_dependency(
    tmp_path, monkeypatch, operation,
):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    dependency = scratch.path / "styles.css"
    dependency.write_text("body {}", encoding="utf-8")
    disposable = scratch.path / "disposable.txt"
    disposable.write_text("temporary", encoding="utf-8")
    preview = scratch.path / "preview.html"
    html = f'<link rel="stylesheet" href="{dependency.as_uri()}">'
    preview.write_text(html, encoding="utf-8")
    inspect_path = getattr(Path, operation)

    def fail_dependency(path, *args, **kwargs):
        if path == dependency:
            raise PermissionError("dependency cannot be inspected")
        return inspect_path(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, fail_dependency)
        retain_scratch_chat_references(tmp_path, f"[Preview]({preview.as_uri()})")
    cleanup_skill_scratch_dir(scratch)

    assert preview.read_text(encoding="utf-8") == html
    assert dependency.read_text(encoding="utf-8") == "body {}"
    assert not disposable.exists()


def test_scratch_retention_reads_reference_facts_without_copying_session_history(
    tmp_path, monkeypatch,
):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / ".box-agent" / "scratch" / "session",
    )
    preview = scratch.path / "preview.png"
    preview.write_bytes(b"preview")
    log = SessionLog.create(tmp_path / "sessions", session_id="preview", cwd=tmp_path)
    try:
        log.append("user/message", {"content": "Show the preview"})
        link = f"[Preview]({preview.as_uri()})"
        ChatReferences(tmp_path, session_log=log).retain_text(link)

        def reject_full_history(_log):
            pytest.fail("Retaining a preview must not copy the complete session history")

        monkeypatch.setattr(SessionLog, "events", property(reject_full_history))
        retain_scratch_chat_references(tmp_path, link, log)
        assert len(list(log.events_of_type("chat/reference"))) == 1
    finally:
        log.close()


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
