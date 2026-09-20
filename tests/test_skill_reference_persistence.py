"""Durable, content-addressed Skill references owned by a Session Log."""

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

import box_agent.session_log as session_log_module
from box_agent.session_log import (
    SessionLog,
    SessionLogCorrupted,
)


@pytest.fixture
def log(tmp_path):
    session = SessionLog.create(tmp_path / "sessions", session_id="skill", cwd=tmp_path)
    yield session
    session.close()


def test_request_reference_rebuilds_exact_text_without_source_or_trace(tmp_path):
    source = tmp_path / "SKILL.md"
    content = "# 方法资料\r\n\r\n原始空格  \n最后一行不带换行 🌱"
    source.write_bytes(content.encode("utf-8"))
    log = SessionLog.create(tmp_path / "sessions", session_id="restore", cwd=tmp_path)
    legacy = {"name": "fixture", "sha256": "old-rendered-prompt-hash", "loadOrder": 1}
    log.append("skill/change", {"skills": [legacy]})
    ref = log.store_skill_reference(source.read_bytes().decode("utf-8"))
    log.append(
        "request/context",
        {"turn": 1, "step": 1, "skillReferences": [{**ref, "requestId": "request-1"}]},
    )
    log.flush()
    log.close()
    source.unlink()

    restored = SessionLog.open(tmp_path / "sessions", session_id="restore", cwd=tmp_path)
    try:
        reference = restored.events[-1]["data"]["skillReferences"][0]
        assert restored.read_skill_reference(reference) == content
        assert reference["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert restored.replay().skills == [legacy]
        assert restored.replay().messages == []
        assert restored.header["version"] == 1
        assert [event["type"] for event in restored.events] == ["skill/change", "request/context"]
        assert not list(tmp_path.rglob("*trace*"))
    finally:
        restored.close()


def test_identical_reference_reuses_snapshot_without_rewriting_or_appending_events(log):
    before = log.path.read_bytes()
    first = log.store_skill_reference("same reference")
    path = log.path.parent / first["contentRef"]
    first_stat = path.stat()
    second = log.store_skill_reference("same reference")

    assert first == second
    assert path.stat().st_ino == first_stat.st_ino
    assert path.stat().st_mtime_ns == first_stat.st_mtime_ns
    assert sorted(path.parent.iterdir()) == [path]
    assert log.events == ()
    assert log.path.read_bytes() == before


def test_different_versions_keep_both_immutable_contents(log):
    old = log.store_skill_reference("old")
    new = log.store_skill_reference("new")
    assert old != new
    assert log.read_skill_reference(old) == "old"
    assert log.read_skill_reference(new) == "new"


def test_empty_reference_has_a_valid_hash_and_round_trips(log):
    ref = log.store_skill_reference("")
    assert ref["sha256"] == hashlib.sha256(b"").hexdigest()
    assert log.read_skill_reference(ref) == ""


@pytest.mark.parametrize("replacement", [b"different content", b"\xff\xfe"])
def test_corrupt_reference_is_rejected_and_never_silently_replaced(log, replacement):
    ref = log.store_skill_reference("original")
    path = log.path.parent / ref["contentRef"]
    path.write_bytes(replacement)

    with pytest.raises(SessionLogCorrupted, match="hash"):
        log.read_skill_reference(ref)
    fallback = log.store_skill_reference("original")
    assert fallback == {"inlineContent": "original", "sha256": ref["sha256"]}
    assert not log.failed
    assert path.read_bytes() == replacement


def test_missing_reference_has_an_explicit_integrity_error(log):
    ref = log.store_skill_reference("missing")
    (log.path.parent / ref["contentRef"]).unlink()
    with pytest.raises(SessionLogCorrupted, match="read|missing"):
        log.read_skill_reference(ref)


def test_existing_snapshot_read_failure_preserves_current_content(log, monkeypatch):
    ref = log.store_skill_reference("current content")
    path = log.path.parent / ref["contentRef"]
    original_read = Path.read_bytes

    def fail_snapshot_read(candidate):
        if candidate == path:
            raise PermissionError("snapshot read denied")
        return original_read(candidate)

    monkeypatch.setattr(Path, "read_bytes", fail_snapshot_read)
    fallback = log.store_skill_reference("current content")
    assert fallback == {"inlineContent": "current content", "sha256": ref["sha256"]}
    assert not log.failed
    log.append("request/context", {"skillReferences": [fallback]})
    log.flush()


def test_unavailable_snapshot_directory_does_not_disable_session(log):
    directory = log.path.parent / "skill-references"
    directory.write_text("unrelated file", encoding="utf-8")
    fallback = log.store_skill_reference("current content")
    assert fallback["inlineContent"] == "current content"
    assert not log.failed
    log.append("request/context", {"skillReferences": [fallback]})
    log.flush()
    assert directory.read_text(encoding="utf-8") == "unrelated file"


@pytest.mark.skipif(os.name == "nt", reason="POSIX named pipe")
def test_non_regular_snapshot_is_rejected_before_a_blocking_read(log, monkeypatch):
    ref = log.store_skill_reference("original")
    path = log.path.parent / ref["contentRef"]
    path.unlink()
    os.mkfifo(path)
    original_read = Path.read_bytes

    def guarded_read(candidate):
        if candidate == path:
            pytest.fail("a named-pipe read would block session recovery")
        return original_read(candidate)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    with pytest.raises(SessionLogCorrupted, match="reference"):
        log.read_skill_reference(ref)


@pytest.mark.parametrize(
    "content_ref",
    [
        "../outside.txt",
        "/tmp/outside.txt",
        "skill-references/../outside.txt",
        "skill-references\\outside.txt",
    ],
)
def test_reference_paths_cannot_escape_snapshot_namespace(log, content_ref):
    with pytest.raises(SessionLogCorrupted, match="reference"):
        log.read_skill_reference({"contentRef": content_ref, "sha256": "a" * 64})


def test_reference_filename_must_match_declared_hash(log):
    ref = log.store_skill_reference("original")
    with pytest.raises(SessionLogCorrupted, match="reference"):
        log.read_skill_reference({**ref, "sha256": "0" * 64})


@pytest.mark.skipif(os.name == "nt", reason="symlinks may require Windows privileges")
def test_snapshot_file_symlink_is_rejected_even_when_content_hash_matches(log, tmp_path):
    ref = log.store_skill_reference("original")
    path = log.path.parent / ref["contentRef"]
    target = tmp_path / "outside.txt"
    target.write_text("original", encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(SessionLogCorrupted, match="reference"):
        log.read_skill_reference(ref)
    assert log.store_skill_reference("original")["inlineContent"] == "original"
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(os.name == "nt", reason="symlinks may require Windows privileges")
def test_snapshot_directory_symlink_cannot_redirect_reads_or_writes(log, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = log.path.parent / "skill-references"
    directory.symlink_to(outside, target_is_directory=True)
    digest = hashlib.sha256(b"reference").hexdigest()
    ref = {"contentRef": f"skill-references/{digest}.txt", "sha256": digest}

    assert log.store_skill_reference("reference")["inlineContent"] == "reference"
    with pytest.raises(SessionLogCorrupted, match="reference"):
        log.read_skill_reference(ref)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("operation", ["write", "flush", "fsync", "publish"])
def test_snapshot_io_failure_falls_back_without_poisoning_session(log, monkeypatch, operation, caplog):
    if operation == "fsync":
        def fail_fsync(_fd):
            raise OSError("snapshot fsync failed")
        monkeypatch.setattr(session_log_module.os, "fsync", fail_fsync)
    elif operation == "publish":
        def fail_publish(*_args, **_kwargs):
            raise OSError("snapshot publication failed")
        monkeypatch.setattr(session_log_module.os, "link", fail_publish)
    else:
        original_fdopen = session_log_module.os.fdopen

        class FailingFile:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.handle.close()

            def write(self, data):
                if operation == "write":
                    raise OSError("snapshot write failed")
                return self.handle.write(data)

            def flush(self):
                raise OSError("snapshot flush failed")

            def fileno(self):
                return self.handle.fileno()

        monkeypatch.setattr(
            session_log_module.os,
            "fdopen",
            lambda *args, **kwargs: FailingFile(original_fdopen(*args, **kwargs)),
        )

    ref = log.store_skill_reference("preserved inline")
    assert ref == {"inlineContent": "preserved inline",
                   "sha256": hashlib.sha256(b"preserved inline").hexdigest()}
    assert "using inline request/context content" in caplog.text
    assert not log.failed
    assert log.events == ()
    assert list((log.path.parent / "skill-references").iterdir()) == []
    monkeypatch.undo()
    log.append("request/context", {"skillReferences": [ref]})
    log.flush()
    assert log.events[-1]["data"]["skillReferences"] == [ref]


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is a POSIX durability boundary")
def test_directory_sync_failure_falls_back_to_inline_content(log, monkeypatch):
    original = session_log_module.os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failed")
        return original(fd)
    monkeypatch.setattr(session_log_module.os, "fsync", fail_directory_sync)

    ref = log.store_skill_reference("published but durability uncertain")
    assert ref["inlineContent"] == "published but durability uncertain"
    assert "contentRef" not in ref
    assert not log.failed
    assert log.events == ()


def test_closed_log_cannot_publish_new_reference(log):
    log.close()
    with pytest.raises(RuntimeError, match="closed"):
        log.store_skill_reference("closed")


def test_legacy_log_without_reference_records_opens_unchanged(tmp_path):
    log = SessionLog.create(tmp_path / "sessions", session_id="legacy", cwd=tmp_path)
    skills = [{"name": "legacy", "sha256": "old-hash", "loadOrder": 7}]
    log.append("skill/change", {"skills": skills})
    log.flush()
    path = log.path
    log.close()
    before = path.read_bytes()

    restored = SessionLog.open(tmp_path / "sessions", session_id="legacy", cwd=tmp_path)
    try:
        assert restored.replay().skills == skills
        assert path.read_bytes() == before
        assert not (path.parent / "skill-references").exists()
        assert json.loads(before.splitlines()[0])["version"] == 1
    finally:
        restored.close()
