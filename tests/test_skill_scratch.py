from __future__ import annotations

import asyncio
import errno
import os
import threading
import zlib
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.artifact_publication import metadata_path, retain_chat_file, write_metadata
from box_agent.session_log import SessionLog, SessionLogDurabilityError
from box_agent.tools import skill_scratch
from box_agent.tools.chat_references import ChatReferences, retain_scratch_chat_references
from box_agent.tools.setup import add_workspace_tools
from box_agent.tools.skill_scratch import (
    SKILL_SCRATCH_DIR_NAME,
    SessionSkillScratch,
    SkillScratchUnavailable,
    cleanup_skill_scratch_dir,
    close_skill_scratch_dir,
    prepare_skill_scratch_dir,
)


@pytest.mark.parametrize(
    ("first_owner", "second_owner"),
    [(None, None), (None, "acp-session"), ("acp-session", None)],
)
@pytest.mark.asyncio
async def test_workspace_tool_cleanup_preserves_other_sessions_scratch(
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
    assert not (tmp_path / SKILL_SCRATCH_DIR_NAME).exists()
    assert cleanup_skill_scratch_dir(first) == []
    assert not (tmp_path / SKILL_SCRATCH_DIR_NAME).exists()
    first_file = (await first.ensure()) / "first-session.txt"
    first_file.write_text("remove", encoding="utf-8")
    second_file = (await second.ensure()) / "second-session.txt"
    second_file.write_text("keep", encoding="utf-8")

    cleanup_skill_scratch_dir(first)

    assert not first_file.exists()
    assert second_file.read_text(encoding="utf-8") == "keep"
    bash_tool = next(tool for tool in first_tools if tool.name == "bash")
    assert Path(bash_tool.workspace_dir) == tmp_path.resolve()


@pytest.mark.asyncio
async def test_scratch_retries_after_a_failed_preparation(tmp_path, monkeypatch):
    scratch = SessionSkillScratch(tmp_path)

    def deny(*args, **kwargs):
        raise PermissionError("scratch refused")

    with monkeypatch.context() as patch:
        patch.setattr(skill_scratch, "prepare_skill_scratch_dir", deny)
        with pytest.raises(SkillScratchUnavailable, match="scratch refused"):
            await scratch.ensure()
    assert not scratch.path.exists()
    assert (await scratch.ensure()).is_dir()


@pytest.mark.asyncio
async def test_scratch_timeouts_share_one_preparation(tmp_path, monkeypatch):
    scratch = SessionSkillScratch(tmp_path)
    prepare = skill_scratch.prepare_skill_scratch_dir
    release = threading.Event()
    attempts = []

    def delayed_prepare(*args, **kwargs):
        attempts.append(1)
        assert release.wait(3), "test did not release scratch preparation"
        return prepare(*args, **kwargs)

    monkeypatch.setattr(skill_scratch, "prepare_skill_scratch_dir", delayed_prepare)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(skill_scratch, "_PREPARE_TIMEOUT_SECONDS", 0.02)
            for _ in range(2):
                with pytest.raises(SkillScratchUnavailable, match="timed out"):
                    await scratch.ensure()
        assert not scratch.path.exists()
        assert cleanup_skill_scratch_dir(scratch) == []
    finally:
        release.set()
    assert (await asyncio.wait_for(scratch.ensure(), 2)).is_dir()
    assert attempts == [1]


@pytest.mark.asyncio
async def test_scratch_cleanup_error_does_not_fail_session_close(tmp_path, monkeypatch, caplog):
    scratch = SessionSkillScratch(tmp_path)
    disposable = (await scratch.ensure()) / "cleanup.txt"
    disposable.write_text("keep if denied")
    rename = Path.rename

    def deny_scratch_move(path, target):
        if path == disposable:
            raise PermissionError("cleanup refused")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", deny_scratch_move)
    await close_skill_scratch_dir(scratch)
    assert disposable.exists()
    assert "cleanup refused" in caplog.text


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


@pytest.mark.parametrize("reuse_path", [False, True], ids=["available", "reused"])
def test_scratch_retries_failed_deletion_without_nesting_quarantines(tmp_path, monkeypatch, reuse_path):
    scratch = prepare_skill_scratch_dir(tmp_path)
    preview = scratch.path / "draft.html"
    preview.write_text("discard")
    unlink = Path.unlink

    def refuse_once(path, *args, **kwargs):
        monkeypatch.setattr(Path, "unlink", unlink)
        if reuse_path:
            preview.write_text("new preview")
            ChatReferences(tmp_path).retain_text(f"[Preview]({preview.as_uri()})")
        raise PermissionError("busy")

    monkeypatch.setattr(Path, "unlink", refuse_once)
    with pytest.raises(PermissionError, match="busy"):
        cleanup_skill_scratch_dir(scratch)
    if reuse_path:
        isolated, = scratch.path.glob(".cleanup-*/draft.html")
        assert isolated.read_text() == "discard"
        assert cleanup_skill_scratch_dir(scratch) == [str(isolated.parent)]
        assert list(scratch.path.iterdir()) == [preview]
        assert preview.read_text() == "new preview"
        return
    assert list(scratch.path.iterdir()) == [preview]
    assert preview.read_text() == "discard"
    assert cleanup_skill_scratch_dir(scratch) == [str(preview)]
    assert not list(scratch.path.iterdir())


def test_restore_scratch_directory_to_an_available_path(tmp_path):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "draft.html").write_text("preview")
    target = tmp_path / "public"

    skill_scratch._restore_scratch_entry(isolated, target)

    assert (target / "draft.html").read_text() == "preview"
    assert not isolated.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows rename already refuses an existing directory")
def test_restore_scratch_directory_preserves_a_new_empty_directory(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "draft.html").write_text("old preview")
    target = tmp_path / "public"
    new_directory = None
    lstat = Path.lstat

    def create_target_before_move(path):
        nonlocal new_directory
        if path == isolated and new_directory is None:
            target.mkdir()
            new_directory = target.stat()
        return lstat(path)

    monkeypatch.setattr(Path, "lstat", create_target_before_move)
    with pytest.raises(FileExistsError):
        skill_scratch._restore_scratch_entry(isolated, target)

    assert new_directory is not None and os.path.samestat(new_directory, target.stat())
    assert list(target.iterdir()) == []
    assert (isolated / "draft.html").read_text() == "old preview"


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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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


def test_invalid_top_level_preview_retains_its_session_scratch(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        # Unknown dependencies fail closed: this session's scratch is kept whole.
        assert disposable.exists()
        references = ChatReferences(tmp_path, session_log=log)
        preview.unlink()
        references.deleted(frozenset({preview, scratch.path}))
        later = scratch.path / "later.txt"
        later.touch()
        cleanup_skill_scratch_dir(scratch)
        # The directory still exists, so its retention outlives the deleted
        # preview: later scratch files of this session are kept too.
        assert later.exists()
        assert disposable.exists()
        assert scratch.path in ChatReferences(tmp_path, session_log=log).paths
    finally:
        log.close()


def test_preview_html_base_does_not_retain_the_session_scratch_root(tmp_path):
    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
    # The failed scan also keeps undeclared neighbours of this session.
    assert disposable.exists()


@pytest.mark.parametrize("failure", ["scan-limit", "unreadable", "closure-limit"])
def test_failed_top_level_preview_scan_keeps_its_sibling_dependency(tmp_path, monkeypatch, failure):
    from box_agent.tools import chat_references

    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
    )
    other = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "other-session",
    )
    other_disposable = other.path / "disposable.txt"
    other_disposable.touch()
    stylesheet = scratch.path / "styles.css"
    stylesheet.write_text("body {}", encoding="utf-8")
    preview = scratch.path / "preview.html"
    links = '<link rel="stylesheet" href="styles.css">'
    if failure == "closure-limit":
        for index in range(2050):
            (scratch.path / f"asset{index}.png").write_bytes(b"asset")
        links += "".join(f'<img src="asset{index}.png">' for index in range(2050))
    preview.write_text(links, encoding="utf-8")
    if failure == "scan-limit":
        monkeypatch.setattr(chat_references, "_FULL_PARSE_LIMIT", 0)
        monkeypatch.setattr(chat_references, "_STREAMED_SCAN_LIMIT", 8)
    elif failure == "unreadable":
        scan = chat_references.local_dependencies

        def unreadable(path, root):
            if path == preview:
                raise PermissionError(errno.EACCES, "preview cannot be scanned", path)
            return scan(path, root)

        monkeypatch.setattr(chat_references, "local_dependencies", unreadable)

    retain_scratch_chat_references(tmp_path, f"[Preview]({preview.as_uri()})")
    cleanup_skill_scratch_dir(scratch)
    cleanup_skill_scratch_dir(other)

    assert preview.is_file()
    assert stylesheet.read_text(encoding="utf-8") == "body {}"
    # Fail-closed retention stays inside this session's scratch directory.
    assert not other_disposable.exists()


def test_large_preview_keeps_entity_encoded_dependencies(tmp_path, monkeypatch):
    from box_agent.tools import chat_references

    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
    )
    assets = scratch.path / "a&b"
    assets.mkdir()
    names = ("x&y.css", "small&1x.png", "large&2x.png", "chart&base.png")
    for name in names:
        target = scratch.path / name if name == "x&y.css" else assets / name
        target.write_bytes(name.encode())
    preview = scratch.path / "preview.html"
    preview.write_text(
        '<base href="a&amp;b/"><link rel="stylesheet" href="../x&amp;y.css">'
        '<img srcset="small&amp;1x.png 1x, large&#38;2x.png 2x" src="chart&amp;base.png">',
        encoding="utf-8",
    )
    disposable = scratch.path / "disposable.txt"
    disposable.touch()
    whole = chat_references.local_dependencies(preview, tmp_path)
    monkeypatch.setattr(chat_references, "_FULL_PARSE_LIMIT", 0)
    monkeypatch.setattr(chat_references, "_STREAMED_SCAN_CHUNK", 9)
    monkeypatch.setattr(chat_references, "_STREAMED_SCAN_OVERLAP", 48)
    assert chat_references.local_dependencies(preview, tmp_path) == whole

    retain_scratch_chat_references(tmp_path, f"[Preview]({preview.as_uri()})")
    cleanup_skill_scratch_dir(scratch)

    assert preview.is_file()
    assert (scratch.path / "x&y.css").read_bytes() == b"x&y.css"
    for name in names[1:]:
        assert (assets / name).read_bytes() == name.encode()
    assert not disposable.exists()


def test_scratch_cleanup_preserves_preview_after_temporary_permission_error(tmp_path, monkeypatch):
    from box_agent import artifact_publication

    scratch = prepare_skill_scratch_dir(
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
        tmp_path, scratch_root_dir=tmp_path / SKILL_SCRATCH_DIR_NAME / "session",
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
    assert not scratch.path.exists()
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


@pytest.mark.asyncio
async def test_scratch_failure_logs_diagnostics_once_and_recovery(tmp_path, monkeypatch, caplog):
    import errno
    import logging

    prepare = skill_scratch.prepare_skill_scratch_dir
    denied = [True]

    def flaky_prepare(*args, **kwargs):
        if denied[0]:
            error = PermissionError(errno.EACCES, "Access is denied", str(tmp_path / ".box-agent-scratch"))
            error.winerror = 5
            raise error
        return prepare(*args, **kwargs)

    monkeypatch.setattr(skill_scratch, "prepare_skill_scratch_dir", flaky_prepare)
    (tmp_path / ".box-agent-scratch").write_text("not a directory")
    scratch = SessionSkillScratch(tmp_path)
    caplog.set_level(logging.INFO, logger="box_agent.tools.skill_scratch")

    for _ in range(3):
        assert not await skill_scratch.try_prepare_scratch(scratch)
    failures = [r for r in caplog.records if "skill_scratch/unavailable" in r.getMessage()]
    assert len(failures) == 1  # the same failure is not repeated on every tool call
    message = failures[0].getMessage()
    for expected in (
        "error_type='PermissionError'", "winerror=5", f"errno={errno.EACCES}",
        "failed_path=", "workspace_state='dir", "scratch_root_state='file", "planned_state=",
        "hints=",
    ):
        assert expected in message, (expected, message)

    (tmp_path / ".box-agent-scratch").unlink()
    denied[0] = False
    assert await skill_scratch.try_prepare_scratch(scratch)
    assert any("skill_scratch/recovered" in r.getMessage() for r in caplog.records)


def test_location_hints_flag_onedrive_and_protected_folders(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("OneDrive", str(profile / "OneDrive"))
    assert skill_scratch._location_hints(profile / "Documents" / "proj") == ["user_documents"]
    assert skill_scratch._location_hints(profile / "OneDrive" / "proj") == ["onedrive"]
    assert skill_scratch._location_hints(tmp_path / "elsewhere") == []


def _link_scratch_root_outside(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / SKILL_SCRATCH_DIR_NAME).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available")
    return workspace, outside


@pytest.mark.asyncio
async def test_bash_never_receives_a_scratch_path_rejected_by_safety_checks(tmp_path):
    from box_agent.tools.bash_tool import BashTool

    workspace, outside = _link_scratch_root_outside(tmp_path)
    scratch = SessionSkillScratch(workspace)
    tool = BashTool(workspace_dir=str(workspace), non_interactive=True, skill_scratch_dir=scratch)
    result = await tool.execute(
        command='mkdir -p "$BOX_AGENT_SCRATCH_DIR" && echo escaped > "$BOX_AGENT_SCRATCH_DIR/f"',
    )
    assert not result.success
    assert result.raw_output["code"] == "SKILL_SCRATCH_UNAVAILABLE"
    assert list(outside.iterdir()) == []  # nothing was written through the link


@pytest.mark.asyncio
async def test_python_sandbox_never_receives_a_scratch_path_rejected_by_safety_checks(tmp_path, monkeypatch):
    from box_agent.tools.jupyter_tool import JupyterSandboxTool

    workspace, outside = _link_scratch_root_outside(tmp_path)
    scratch = SessionSkillScratch(workspace)
    tool = JupyterSandboxTool(workspace_dir=str(workspace), skill_scratch_dir=scratch)
    monkeypatch.setattr(tool, "_get_sandbox_env", lambda: pytest.fail("kernel initialization was reached"))
    result = await tool.execute("print('check')")
    assert not result.success
    assert result.raw_output["code"] == "SKILL_SCRATCH_UNAVAILABLE"
    assert skill_scratch.SKILL_SCRATCH_ENV not in tool.runtime_env
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_scratch_timeout_is_not_treated_as_a_validated_path(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(skill_scratch, "prepare_skill_scratch_dir",
                        lambda *a, **k: release.wait(3) and pytest.fail("released"))
    monkeypatch.setattr(skill_scratch, "_PREPARE_TIMEOUT_SECONDS", 0.02)
    try:
        with pytest.raises(SkillScratchUnavailable) as caught:
            await skill_scratch.try_prepare_scratch(SessionSkillScratch(tmp_path))
        assert caught.value.path_validated is False
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("is_directory", [False, True], ids=["file", "directory"])
async def test_late_scratch_deletion_preserves_reused_preview_path(tmp_path, monkeypatch, is_directory):
    scratch = SessionSkillScratch(tmp_path)
    directory = await scratch.ensure()
    entry = directory / ("bundle" if is_directory else "preview.html")
    if is_directory:
        entry.mkdir()
    preview = entry / "preview.html" if is_directory else entry
    preview.write_text("first turn")
    deleting = threading.Event()
    resume = threading.Event()

    def pause_delete(operation):
        def delete(path, *args, **kwargs):
            if Path(path).name == entry.name and not deleting.is_set():
                deleting.set()
                assert resume.wait(5)
            return operation(path, *args, **kwargs)
        return delete

    if is_directory:
        monkeypatch.setattr(skill_scratch.shutil, "rmtree", pause_delete(skill_scratch.shutil.rmtree))
    else:
        monkeypatch.setattr(Path, "unlink", pause_delete(Path.unlink))
    monkeypatch.setattr(skill_scratch, "_CLEANUP_TIMEOUT_SECONDS", 0.5)
    try:
        assert await skill_scratch.acleanup_skill_scratch_dir(scratch) == []
        assert deleting.is_set()
        assert cleanup_skill_scratch_dir(scratch.prepared) == []
        assert await scratch.ensure() == directory
        if is_directory:
            entry.mkdir(exist_ok=True)
        preview.write_text("second turn")
        ChatReferences(tmp_path).retain_text(f"[Preview]({preview.as_uri()})")
    finally:
        resume.set()
    assert await asyncio.wait_for(scratch._cleanup, 2) == [str(entry)]
    assert preview.read_text() == "second turn"


@pytest.mark.asyncio
@pytest.mark.parametrize(("is_directory", "retention_writable"), [
    pytest.param(False, True, id="file"),
    pytest.param(True, True, id="directory"),
    pytest.param(True, False, id="read-only-retention-cache"),
])
async def test_restore_conflict_keeps_the_published_preview_at_the_logged_path(
    tmp_path, monkeypatch, caplog, is_directory, retention_writable,
):
    scratch = SessionSkillScratch(tmp_path)
    root = await scratch.ensure()
    entry = root / ("bundle" if is_directory else "preview.html")
    if is_directory:
        entry.mkdir()
    preview = entry / "preview.html" if is_directory else entry
    preview.write_text("draft")
    moving, resume = threading.Event(), threading.Event()
    rename = Path.rename

    def move_then_reuse_path(path, target):
        if path != entry or moving.is_set():
            return rename(path, target)
        moving.set()
        assert resume.wait(5)
        result = rename(path, target)
        # Another writer takes the public path before the late move can be undone.
        if is_directory:
            entry.mkdir()
        preview.write_text("new occupant")
        return result

    monkeypatch.setattr(Path, "rename", move_then_reuse_path)
    if not retention_writable:
        def deny_retention(path):
            raise PermissionError("read-only retention cache")
        monkeypatch.setattr(skill_scratch, "write_retention", deny_retention)
    monkeypatch.setattr(skill_scratch, "_CLEANUP_TIMEOUT_SECONDS", 0.5)
    cleanup = asyncio.create_task(skill_scratch.acleanup_skill_scratch_dir(scratch))
    try:
        assert await asyncio.to_thread(moving.wait, 2)
        assert await asyncio.wait_for(cleanup, 2) == []
        assert await scratch.ensure() != root
        preview.write_text("published preview")
        with closing(SessionLog.create(tmp_path / "sessions", session_id="chat", cwd=tmp_path)) as log:
            references = ChatReferences(tmp_path, session_log=log)
            references.retain_text(f"[Preview]({preview.as_uri()})")
            assert preview in references.paths
    finally:
        resume.set()
    assert await asyncio.wait_for(scratch._cleanup, 2) == []

    isolated, = root.glob(f".cleanup-*/{entry.name}")
    recovered = isolated / preview.name if is_directory else isolated
    disposable = root / "discard.tmp"
    disposable.touch()
    assert str(disposable) in await skill_scratch.acleanup_skill_scratch_dir(scratch)
    assert recovered.read_text() == "published preview"
    assert preview.read_text() == "new occupant"
    warning = f"skill_scratch/cleanup_restore_failed path={str(entry)!r} isolated={str(isolated)!r}"
    assert warning in caplog.text
    if not retention_writable:
        assert "skill_scratch/cleanup_retention_failed" in caplog.text
        return

    # A fresh cleanup of this root must also find the recovery copy on disk.
    reopened = prepare_skill_scratch_dir(tmp_path, scratch_root_dir=root)
    cleanup_skill_scratch_dir(reopened)
    assert recovered.read_text() == "published preview"
    assert preview.read_text() == "new occupant"


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["timeout", "cancel"])
@pytest.mark.parametrize("phase", ["scan", "before-rename", "after-rename"])
async def test_interrupted_cleanup_releases_scratch_without_losing_new_files(
    tmp_path, monkeypatch, phase, interrupt,
):
    scratch = SessionSkillScratch(tmp_path)
    first_root = await scratch.ensure()
    preview = first_root / "preview.html"
    preview.write_text("old")
    entered, resume = threading.Event(), threading.Event()

    def pause():
        entered.set()
        assert resume.wait(5)

    if phase == "scan":
        check = skill_scratch.retained_chat_file_in

        def delayed_check(path):
            result = check(path)
            if path == preview:
                pause()
            return result

        monkeypatch.setattr(skill_scratch, "retained_chat_file_in", delayed_check)
    else:
        rename = Path.rename

        def delayed_rename(path, target):
            if path == preview and phase == "before-rename":
                pause()
            result = rename(path, target)
            if path == preview and phase == "after-rename":
                pause()
            return result

        monkeypatch.setattr(Path, "rename", delayed_rename)

    monkeypatch.setattr(skill_scratch, "_CLEANUP_TIMEOUT_SECONDS", 0.5 if interrupt == "timeout" else 10)
    cleanup = asyncio.create_task(skill_scratch.acleanup_skill_scratch_dir(scratch))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if interrupt == "cancel":
            cleanup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cleanup
        else:
            assert await asyncio.wait_for(cleanup, 2) == []
        next_root = await scratch.ensure()
        # Only an already-issued move requires new tools to change namespace.
        assert (next_root == first_root) == (phase == "scan")
        next_preview = next_root / preview.name
        next_preview.write_text("new preview")
        ChatReferences(tmp_path).retain_text(f"[Preview]({next_preview.as_uri()})")
        leftover = first_root / "leftover.tmp"
        leftover.touch()
        if first_root != next_root:
            # The model may also explicitly edit a path from the old conversation.
            preview.write_text("updated at the original path")
            ChatReferences(tmp_path).retain_text(f"[Existing]({preview.as_uri()})")
    finally:
        resume.set()
    assert await asyncio.wait_for(scratch._cleanup, 2) == []
    assert next_preview.read_text() == "new preview"
    assert leftover.is_file()
    assert str(leftover) in await skill_scratch.acleanup_skill_scratch_dir(scratch)
    assert not leftover.exists()
    assert next_preview.read_text() == "new preview"
    if first_root != next_root:
        assert preview.read_text() == "updated at the original path"


@pytest.mark.asyncio
async def test_cleanup_wait_is_bounded_and_does_not_overlap(tmp_path, monkeypatch, caplog):
    import logging

    scratch = SessionSkillScratch(tmp_path)
    await scratch.ensure()
    release = threading.Event()
    calls = []
    real_cleanup = skill_scratch._clean_prepared_scratch

    def stuck_cleanup(prepared, lease):
        calls.append(1)
        release.wait(3)
        return real_cleanup(prepared, lease)

    monkeypatch.setattr(skill_scratch, "_clean_prepared_scratch", stuck_cleanup)
    monkeypatch.setattr(skill_scratch, "_CLEANUP_TIMEOUT_SECONDS", 0.05)
    caplog.set_level(logging.WARNING, logger="box_agent.tools.skill_scratch")
    try:
        # A stuck file system returns control instead of holding the turn open.
        assert await asyncio.wait_for(skill_scratch.acleanup_skill_scratch_dir(scratch), 1) == []
        assert any("cleanup_timeout" in r.getMessage() for r in caplog.records)
        # The next turn does not start a second cleanup on top of the stuck one.
        assert await asyncio.wait_for(skill_scratch.acleanup_skill_scratch_dir(scratch), 1) == []
        assert calls == [1]
        assert any("cleanup_skipped" in r.getMessage() for r in caplog.records)
    finally:
        release.set()
    for _ in range(100):
        if scratch._cleanup.done():
            break
        await asyncio.sleep(0.01)
    disposable = scratch.path / "later.txt"
    disposable.write_text("x")
    assert await skill_scratch.acleanup_skill_scratch_dir(scratch) == [str(disposable)]


@pytest.mark.asyncio
async def test_stuck_cleanup_does_not_starve_other_sessions_preparation(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(skill_scratch, "_clean_prepared_scratch", lambda prepared, lease: release.wait(5) and [])
    monkeypatch.setattr(skill_scratch, "_CLEANUP_TIMEOUT_SECONDS", 0.01)
    stuck = []
    try:
        for index in range(8):  # more than any small fixed pool
            scratch = SessionSkillScratch(tmp_path / f"stuck-{index}")
            (tmp_path / f"stuck-{index}").mkdir()
            await scratch.ensure()
            await skill_scratch.acleanup_skill_scratch_dir(scratch)
            stuck.append(scratch)
        fresh = SessionSkillScratch(tmp_path)
        assert (await asyncio.wait_for(fresh.ensure(), 2)).is_dir()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_hanging_diagnosis_does_not_hold_preparation(tmp_path, monkeypatch, caplog):
    import logging

    release = threading.Event()

    def deny(*args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(skill_scratch, "prepare_skill_scratch_dir", deny)
    monkeypatch.setattr(skill_scratch, "_diagnose_failure", lambda *a: release.wait(3) and {})
    monkeypatch.setattr(skill_scratch, "_DIAGNOSE_TIMEOUT_SECONDS", 0.02)
    caplog.set_level(logging.WARNING, logger="box_agent.tools.skill_scratch")
    try:
        assert await asyncio.wait_for(skill_scratch.try_prepare_scratch(SessionSkillScratch(tmp_path)), 1) is False
        assert any("diagnostics='unavailable'" in r.getMessage() for r in caplog.records)
    finally:
        release.set()
