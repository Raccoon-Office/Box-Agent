"""Attachment references cross session boundaries without trusting host paths."""

import base64
from dataclasses import replace
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from box_agent.image_store import SessionImageStore
from box_agent.image_context import ImageContext
from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.session_continuation import (
    ContinuationMessage, MAX_MESSAGE_CHARS, SCHEMA_VERSION, SessionContinuation,
    export_session_bundle, import_continuation_images, parse_session_continuation,
)
from box_agent.session_log import SessionLog


def _image(color="navy"):
    output = io.BytesIO()
    Image.new("RGB", (17, 11), color).save(output, format="PNG")
    return {"type": "input_image", "media_type": "image/png",
            "data": base64.b64encode(output.getvalue()).decode("ascii")}


@pytest.fixture
def logs(tmp_path):
    source = SessionLog.create(tmp_path / "sessions", session_id="original", cwd=tmp_path)
    target = SessionLog.create(tmp_path / "sessions", session_id="branch", cwd=tmp_path)
    try:
        yield source, target
    finally:
        source.close()
        target.close()


def _source_image(log, *, image_id="original-image", content="Please inspect this image", color="navy"):
    stored = SessionImageStore(log.path.parent / "images").persist_block(_image(color))
    ref = {**stored, "imageId": image_id, "source": "user_upload", "createdRound": 1, "eligible": True}
    message = Message(role="user", content=content)
    log.queue_message_images(message, [ref])
    log.append_unlogged_messages([*log.replay().messages, message], turn=1, step=1)
    return log.replay().images[-1]


def _continuation(source, refs, content="Please inspect this image"):
    return SessionContinuation("branch", "history_edit",
                               (ContinuationMessage("user", content, tuple(refs)),), False,
                               source_session_id=source.header["id"])


def _tool_image(log, source, *, image_id="tool-image", user_content="Please inspect this image", add_user=True):
    if add_user:
        user = Message(role="user", content=user_content)
        log.append_unlogged_messages([*log.replay().messages, user], turn=1, step=1)
    name = "inspect_images" if source == "image_inspect" else "cua_screenshot"
    call_id = "call-" + image_id
    assistant = Message(role="assistant", content="", tool_calls=[ToolCall(
        id=call_id, type="function", function=FunctionCall(name=name, arguments={}),
    )])
    log.mark_model_response(assistant, 1)
    log.append_unlogged_messages([*log.replay().messages, assistant], turn=1, step=1)
    stored = SessionImageStore(log.path.parent / "images").persist_block(_image())
    ref = {**stored, "imageId": image_id, "source": source, "createdRound": 2,
           "eligible": True, "toolCallId": call_id, "toolName": name}
    tool = Message(role="tool", content="Image loaded", name=name, tool_call_id=call_id)
    log.queue_message_images(tool, [ref])
    log.append_unlogged_messages([*log.replay().messages, tool], turn=1, step=1)
    return log.replay().images[-1]


def test_existing_protocol_accepts_optional_refs_and_preserves_old_text_shape():
    value = {"schema_version": SCHEMA_VERSION, "product_session_id": "branch",
             "messages": [{"role": "user", "content": "image"}]}
    text = parse_session_continuation(value)
    assert text.messages[0].image_refs == ()
    value["source_session_id"] = "source"
    value["messages"][0]["image_refs"] = [{"imageId": "image-1"}]
    parsed = parse_session_continuation(value)
    assert parsed.source_session_id == "source"
    assert parsed.messages[0].image_refs == ({"imageId": "image-1"},)


@pytest.mark.parametrize("ref", [
    {"imageId": "image", "contentRef": "../outside.png"},
    {"imageId": "image", "data": "base64"},
    {"imageId": "image", "sha256": "wrong"},
    {"imageId": "image", "messageSeq": True},
])
def test_continuation_rejects_paths_or_inline_data_in_host_metadata(ref):
    with pytest.raises(ValueError, match="INVALID_CONTINUATION_IMAGE_REFERENCES"):
        parse_session_continuation({
            "schema_version": SCHEMA_VERSION, "product_session_id": "branch",
            "source_session_id": "source",
            "messages": [{"role": "user", "content": "image", "image_refs": [ref]}],
        })


def test_branch_copies_snapshot_and_preserves_original_round_and_evicted_state(logs):
    source, target = logs
    ref = _source_image(source)
    source.append("request/context", {"imageContext": {"requestRound": 1, "selected": [ref["imageId"]]}})
    answer = Message(role="assistant", content="answer")
    source.mark_model_response(answer, 1)
    source.append_unlogged_messages([*source.replay().messages, answer], turn=1, step=1)
    source.append("request/context", {"imageContext": {
        "evicted": [{"imageId": ref["imageId"], "reason": "byte_limit"}],
    }})
    imported = import_continuation_images(_continuation(source, [{"imageId": ref["imageId"]}]),
                                          source_log=source, destination_log=target)
    assert imported.model_round == 1
    copied = imported.messages[0].image_refs[0]
    assert copied["createdRound"] == 1 and copied["delivered"] is True
    assert copied["active"] is False and copied["evicted"] is True and copied["eligible"] is False
    assert copied["evictionReason"] == "byte_limit"
    assert copied["sourceSessionId"] == "original" and copied["sourceMessageSeq"] == ref["messageSeq"]
    assert "messageSeq" not in copied
    assert (target.path.parent / copied["contentRef"]).read_bytes() == (source.path.parent / ref["contentRef"]).read_bytes()
    assert target.replay().messages == []  # The caller owns message commit.


@pytest.mark.parametrize("change,error", [
    ({"imageId": "unowned"}, "NOT_OWNED"),
    ({"sha256": "0" * 64}, "REFERENCE_MISMATCH"),
    ({"messageSeq": 999}, "REFERENCE_MISMATCH"),
])
def test_branch_rejects_forged_reference_before_copying(logs, change, error):
    source, target = logs
    ref = _source_image(source)
    with pytest.raises(ValueError, match=error):
        import_continuation_images(_continuation(source, [{**ref, **change}]),
                                   source_log=source, destination_log=target)
    assert not (target.path.parent / "images").exists()


def test_branch_rejects_changed_source_or_message_association(logs):
    source, target = logs
    ref = _source_image(source)
    continuation = _continuation(source, [ref])
    with pytest.raises(ValueError, match="SOURCE_MISMATCH"):
        import_continuation_images(replace(continuation, source_session_id="other"),
                                   source_log=source, destination_log=target)
    with pytest.raises(ValueError, match="MESSAGE_MISMATCH"):
        import_continuation_images(_continuation(source, [ref], content="edited content"),
                                   source_log=source, destination_log=target)


def test_legacy_continuation_truncation_preserves_verified_image_association(logs):
    source, target = logs
    content = "x" * (MAX_MESSAGE_CHARS + 100)
    ref = _source_image(source, content=content)
    continuation = parse_session_continuation({
        "schema_version": SCHEMA_VERSION, "product_session_id": "branch",
        "source_session_id": "original", "reason": "artifact_binding_changed",
        "messages": [{"role": "user", "content": content,
                      "image_refs": [{"imageId": ref["imageId"], "messageSeq": ref["messageSeq"]}]}],
    })
    assert continuation.truncated is True
    imported = import_continuation_images(continuation, source_log=source, destination_log=target)
    assert len(imported.messages[0].content) == MAX_MESSAGE_CHARS
    assert imported.messages[0].image_refs[0]["active"] is True
    assert (target.path.parent / ref["contentRef"]).exists()
    with pytest.raises(ValueError, match="MESSAGE_MISMATCH"):
        import_continuation_images(replace(continuation, truncated=False),
                                   source_log=source, destination_log=target)


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_branch_image_preserves_text_and_stays_unavailable(logs, corrupt):
    source, target = logs
    ref = _source_image(source)
    path = source.path.parent / ref["contentRef"]
    if corrupt:
        path.write_bytes(b"corrupted")
    else:
        path.unlink()
    imported = import_continuation_images(_continuation(source, [ref]), source_log=source, destination_log=target)
    assert imported.messages[0].content == "Please inspect this image"
    copied = imported.messages[0].image_refs[0]
    assert not copied["active"] and not copied["eligible"]
    assert copied["evictionReason"] == "missing_or_corrupt_file"
    assert source.replay().messages[0].content == imported.messages[0].content


def test_export_carries_exact_log_and_historical_images_after_reset(logs, tmp_path):
    source, _ = logs
    old = _source_image(source)
    source.append("request/context", {"imageContext": {
        "evicted": [{"imageId": old["imageId"], "reason": "round_limit"}],
    }})
    source.reset_surface(reason="clear")
    new = _source_image(source, image_id="new-image", color="green")
    exported = export_session_bundle(source, tmp_path / "exported-session")
    assert exported.read_bytes() == source.path.read_bytes()
    assert json.loads(exported.read_text().splitlines()[0])["version"] == 1
    for ref in (old, new):
        assert (exported.parent / ref["contentRef"]).read_bytes() == (source.path.parent / ref["contentRef"]).read_bytes()


def test_export_keeps_legacy_cua_sidecars(logs, tmp_path):
    source, _ = logs
    ref = SessionImageStore(source.path.parent / "images").persist_block(_image())
    source.append("tool/result", {
        "message": {"role": "tool", "content": "screenshot", "tool_call_id": "call"},
        "result": {"rawOutput": {"mcp_image_references": [ref]}},
    }, surface_op="append")
    exported = export_session_bundle(source, tmp_path / "legacy-export")
    assert (exported.parent / ref["contentRef"]).exists()


def test_export_never_overwrites_existing_directory(logs, tmp_path):
    source, _ = logs
    destination = tmp_path / "already-exists"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        export_session_bundle(source, destination)
    assert marker.read_text() == "keep"


def test_export_missing_image_keeps_original_log(logs, tmp_path):
    source, _ = logs
    ref = _source_image(source)
    (source.path.parent / ref["contentRef"]).unlink()
    exported = export_session_bundle(source, tmp_path / "missing-export")
    assert exported.read_bytes() == source.path.read_bytes()
    assert not (exported.parent / ref["contentRef"]).exists()


def test_branch_image_write_failure_does_not_discard_text_or_change_source(logs, monkeypatch):
    source, target = logs
    ref = _source_image(source)
    source.flush()
    before = source.path.read_bytes()
    monkeypatch.setattr(SessionImageStore, "persist_block", lambda self, block: None)
    imported = import_continuation_images(_continuation(source, [ref]),
                                          source_log=source, destination_log=target)
    assert imported.messages[0].content == "Please inspect this image"
    copied = imported.messages[0].image_refs[0]
    assert copied["active"] is False and copied["evictionReason"] == "persistence_failed"
    assert source.path.read_bytes() == before


def test_failed_export_removes_only_its_partial_destination(logs, tmp_path, monkeypatch):
    source, _ = logs
    ref = _source_image(source)
    source.flush()
    before = source.path.read_bytes()
    monkeypatch.setattr(SessionImageStore, "persist_block", lambda self, block: None)
    destination = tmp_path / "failed-export"
    with pytest.raises(OSError, match="SESSION_IMAGE_EXPORT_FAILED"):
        export_session_bundle(source, destination)
    assert not destination.exists()
    assert source.path.read_bytes() == before
    assert (source.path.parent / ref["contentRef"]).exists()


@pytest.mark.parametrize("source_kind", ["user_upload", "image_inspect", "cua"])
@pytest.mark.parametrize("evicted", [False, True])
def test_semantic_branch_carries_all_image_sources_and_keeps_lifecycle(logs, source_kind, evicted):
    source, target = logs
    ref = (_source_image(source) if source_kind == "user_upload"
           else _tool_image(source, source_kind))
    if evicted:
        source.append("request/context", {"imageContext": {
            "evicted": [{"imageId": ref["imageId"], "reason": "round_limit"}],
        }})
    continuation = parse_session_continuation({
        "schema_version": SCHEMA_VERSION, "product_session_id": "branch",
        "source_session_id": "original", "reason": "history_edit", "truncated": False,
        "messages": [{"role": "user", "content": "Please inspect this image",
                      "image_refs": [{"imageId": ref["imageId"], "messageSeq": ref["messageSeq"]}]}],
    })
    imported = import_continuation_images(continuation, source_log=source, destination_log=target)
    copied = imported.messages[0].image_refs[0]
    assert copied["source"] == source_kind
    assert copied["createdRound"] == ref["createdRound"]
    assert copied["evicted"] is evicted
    assert copied["active"] is not evicted
    assert (target.path.parent / copied["contentRef"]).read_bytes() == (source.path.parent / ref["contentRef"]).read_bytes()
    if source_kind != "user_upload":
        assert copied["toolCallId"] == ref["toolCallId"]
        assert copied["sourceToolResultSeq"] == ref["messageSeq"]
        assert copied["sourceToolSessionId"] == "original"
        assert copied["sourceUserMessageSeq"] < copied["sourceToolResultSeq"]
    seeded = Message(role="user", content=imported.messages[0].content)
    target.queue_message_images(seeded, list(imported.messages[0].image_refs))
    target.append_unlogged_messages([seeded], turn=1, step=1)
    target.append("request/context", {"imageContext": {
        "phase": "continuation", "modelRoundBaseline": imported.model_round,
    }})
    target.flush()
    restored = ImageContext(target)
    projection = restored.prepare([seeded], llm=SimpleNamespace(capabilities={"image_input": True}))
    blocks = [block for message in projection.messages if isinstance(message.content, list)
              for block in message.content if block.get("type") == "input_image"]
    assert len(blocks) == (0 if evicted else 1)
    if blocks:
        assert blocks[0]["data"] == _image()["data"]
        assert (blocks[0]["width"], blocks[0]["height"]) == (17, 11)


@pytest.mark.parametrize("source_kind", ["image_inspect", "cua"])
def test_tool_images_cannot_be_rebound_to_another_user_task(logs, source_kind):
    source, target = logs
    ref = _tool_image(source, source_kind)
    other = Message(role="user", content="A different user task")
    source.append_unlogged_messages([*source.replay().messages, other], turn=2, step=1)
    with pytest.raises(ValueError, match="MESSAGE_MISMATCH"):
        import_continuation_images(_continuation(source, [ref], content=other.content),
                                   source_log=source, destination_log=target)
    assert not (target.path.parent / "images").exists()


def test_tool_image_import_uses_original_call_owner_even_after_new_user_message(logs):
    source, target = logs
    ref = _tool_image(source, "cua")
    # Simulate a host appending a new user task before the late result commits.
    original_tool_event = source.events[-1]
    later = Message(role="user", content="Later task")
    source.append_unlogged_messages([*source.replay().messages, later], turn=2, step=1)
    changed = {**original_tool_event["data"], "images": [dict(ref, imageId="late-image")]}
    source.append("tool/result", changed, surface_op="append")
    late = source.replay().images[-1]
    imported = import_continuation_images(_continuation(source, [late]),
                                          source_log=source, destination_log=target)
    assert imported.messages[0].image_refs[0]["sourceUserMessageSeq"] == 0


def test_branch_orders_mixed_source_images_by_original_event_and_removes_omitted_links(logs):
    source, target = logs
    uploaded = _source_image(source)
    inspected = _tool_image(source, "image_inspect", image_id="inspect", add_user=False)
    cua = _tool_image(source, "cua", image_id="cua", add_user=False)
    imported = import_continuation_images(_continuation(source, [cua, uploaded, inspected]),
                                          source_log=source, destination_log=target)
    assert [ref["imageId"] for ref in imported.messages[0].image_refs] == [uploaded["imageId"], "inspect", "cua"]
    assert [ref["sourceImageOrder"] for ref in imported.messages[0].image_refs] == [0, 1, 2]
    omitted = import_continuation_images(_continuation(source, []), source_log=source, destination_log=target)
    assert omitted.messages[0].image_refs == ()
    assert len(source.replay().images) == 3


def test_export_restores_referenced_skill_snapshots_without_copying_unrelated_files(logs, tmp_path):
    source, _ = logs
    content = "# 引用资料\r\n保持字节与换行 🌱\n"
    ref = source.store_skill_reference(content)
    source.append("request/context", {"skillReferences": [ref]})
    (source.path.parent / "skill-references" / "unrelated.txt").write_text("do not copy")
    (source.path.parent / "temporary.log").write_text("do not copy")
    exported_root = tmp_path / "exported-root"
    exported_root.mkdir()
    exported = export_session_bundle(source, exported_root / source.path.parent.name)
    restored = SessionLog.open(exported_root, session_id=source.header["id"], cwd=tmp_path)
    try:
        imported_ref = restored.events[-1]["data"]["skillReferences"][0]
        assert restored.read_skill_reference(imported_ref) == content
        assert restored.path.read_bytes() == source.path.read_bytes()
        assert restored.header["version"] == 1
        assert not (exported.parent / "temporary.log").exists()
        assert not (exported.parent / "skill-references" / "unrelated.txt").exists()
    finally:
        restored.close()


def test_export_rejects_skill_reference_path_outside_session(logs, tmp_path):
    source, _ = logs
    source.append("request/context", {"skillReferences": [{
        "contentRef": "../outside.txt", "sha256": "a" * 64,
    }]})
    source.flush()
    before = source.path.read_bytes()
    destination = tmp_path / "invalid-export"
    with pytest.raises(ValueError, match="invalid Skill reference path"):
        export_session_bundle(source, destination)
    assert not destination.exists()
    assert source.path.read_bytes() == before


def test_rebranch_retains_original_tool_audit_links(logs, tmp_path):
    source, branch = logs
    ref = _tool_image(source, "cua")
    imported = import_continuation_images(_continuation(source, [ref]), source_log=source, destination_log=branch)
    message = Message(role="user", content=imported.messages[0].content)
    branch.queue_message_images(message, list(imported.messages[0].image_refs))
    branch.append_unlogged_messages([message], turn=1, step=1)
    second = SessionLog.create(tmp_path / "sessions", session_id="second-branch", cwd=tmp_path)
    try:
        copied_ref = branch.replay().images[0]
        copied = import_continuation_images(_continuation(branch, [copied_ref]),
                                            source_log=branch, destination_log=second)
        latest = copied.messages[0].image_refs[0]
        assert latest["sourceToolSessionId"] == "original"
        assert latest["sourceToolResultSeq"] == ref["messageSeq"]
        assert latest["sourceSessionId"] == "branch"
        assert latest["sourceImageOrder"] == 0
        assert latest["toolCallId"] == ref["toolCallId"]
    finally:
        second.close()
