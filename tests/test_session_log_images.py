"""Version-one image metadata does not rewrite or contaminate message content."""

import gc
import json
import weakref

import pytest

from box_agent.schema import Message
from box_agent.session_log import SessionLog, SessionLogIncompatible


def reference(image_id="i1", **overrides):
    return {"imageId": image_id, "contentRef": "images/" + "a" * 64 + ".png",
            "sha256": "a" * 64, "media_type": "image/png", "source_bytes": 3,
            "base64Bytes": 4, "source": "user_upload", "createdRound": 1,
            "eligible": True, **overrides}


def test_optional_image_metadata_keeps_v1_and_leaves_existing_log_bytes_untouched(tmp_path):
    log = SessionLog.create(tmp_path, session_id="v1-images", cwd=tmp_path)
    old = Message(role="user", content="old text")
    log.append_unlogged_messages([old], turn=1, step=1)
    log.flush()
    original = log.path.read_bytes()
    user = Message(role="user", content="uploaded image")
    image = reference()
    log.queue_message_images(user, [image])
    image["source"] = "caller mutation"
    events = log.append_unlogged_messages([old, user], turn=2, step=1)
    assert len(events) == 1
    assert events[0]["data"]["images"][0]["source"] == "user_upload"
    # An old Message reader ignores the optional event field, so the ordinary
    # surface equality check remains stable in both reader generations.
    assert Message.model_validate(events[0]["data"]).model_dump() == user.model_dump()
    assert log.append_unlogged_messages([old, user], turn=2, step=2) == []
    log.flush()
    assert log.path.read_bytes().startswith(original)
    assert json.loads(log.path.read_text().splitlines()[0])["version"] == 1
    log.close()
    restored = SessionLog.open(tmp_path, session_id="v1-images", cwd=tmp_path, recover=True)
    try:
        assert restored.recovery_source is None
        projection = restored.replay()
        assert projection.images[0]["imageId"] == "i1"
        assert projection.images[0]["active"] is True
        assert projection.images[0]["messageSeq"] == events[0]["seq"]
        assert projection.messages == [old, user]
        copied = projection.images
        copied[0]["active"] = False
        assert projection.images[0]["active"] is True
    finally:
        restored.close()


def test_tool_images_are_event_metadata_and_survive_surface_replacement(tmp_path):
    log = SessionLog.create(tmp_path, session_id="tool-images", cwd=tmp_path)
    user = Message(role="user", content="inspect")
    tool = Message(role="tool", name="inspect_images", tool_call_id="call", content="loaded")
    log.queue_message_images(tool, [reference(source="image_inspect")])
    events = log.append_unlogged_messages([user, tool], turn=1, step=1)
    assert "images" in events[-1]["data"]
    assert "images" not in events[-1]["data"]["message"]
    log.replace_surface([Message(role="user", source="runtime", content="summary")], turn=1, step=2)
    log.flush()
    log.close()
    restored = SessionLog.open(tmp_path, session_id="tool-images", cwd=tmp_path)
    try:
        assert [message.content for message in restored.replay().messages] == ["summary"]
        assert restored.replay().images[0]["source"] == "image_inspect"
    finally:
        restored.close()


def test_eviction_stays_recorded_across_compaction_and_reset_clears_catalog(tmp_path):
    log = SessionLog.create(tmp_path, session_id="evictions", cwd=tmp_path)
    user = Message(role="user", content="images")
    log.queue_message_images(user, [reference(), reference("i2")])
    log.append_unlogged_messages([user], turn=1, step=1)
    log.append("request/context", {"imageContext": {
        "round": 1, "selected": ["i2"], "evicted": [{"imageId": "i1", "reason": "byte_limit"}],
    }})
    log.replace_surface([Message(role="user", content="summary")], turn=1, step=2)
    images = log.replay().images
    assert images[0]["active"] is False
    assert images[0]["evicted"] is True
    assert images[0]["evictionReason"] == "byte_limit"
    assert images[1]["active"] is True
    log.reset_surface(reason="clear")
    assert log.replay().images == []
    assert any(event["data"].get("images") for event in log.events)
    log.close()


def test_only_committed_responses_advance_image_rounds(tmp_path):
    log = SessionLog.create(tmp_path, session_id="rounds", cwd=tmp_path)
    log.append("request/context", {"imageContext": {"round": 100, "selected": []}})
    assert log.replay().model_round == 0
    response = Message(role="assistant", content="accepted response")
    log.mark_model_response(response, 3)
    assert log.replay().model_round == 0
    log.append_unlogged_messages([response], turn=1, step=1)
    assert log.events[-1]["data"]["modelRound"] == 3
    assert "modelRound" not in log.events[-1]["data"]["message"]
    log.reset_surface(reason="clear")
    log.close()
    restored = SessionLog.open(tmp_path, session_id="rounds", cwd=tmp_path)
    try:
        assert restored.replay().model_round == 3
    finally:
        restored.close()


def test_request_selection_is_delivered_only_with_its_committed_response(tmp_path):
    log = SessionLog.create(tmp_path, session_id="delivered", cwd=tmp_path)
    user = Message(role="user", content="images")
    log.queue_message_images(user, [reference(), reference("i2")])
    log.append_unlogged_messages([user], turn=1, step=1)
    log.append("request/context", {"imageContext": {"round": 1, "selected": ["i1"]}})
    assert not any(image["delivered"] for image in log.replay().images)
    # A retry may have a different selection; only the accepted attempt counts.
    log.append("request/context", {"imageContext": {"round": 1, "selected": ["i2"]}})
    response = Message(role="assistant", content="accepted")
    log.mark_model_response(response, 1)
    log.append_unlogged_messages([user, response], turn=1, step=1)
    images = log.replay().images
    assert images[0]["delivered"] is False
    assert images[1]["delivered"] is True
    assert images[1]["firstDeliveredRound"] == 1
    log.close()


def test_queued_metadata_keeps_message_identity_until_commit_or_reset(tmp_path):
    log = SessionLog.create(tmp_path, session_id="identity", cwd=tmp_path)
    message = Message(role="user", content="same text")
    pointer = weakref.ref(message)
    log.queue_message_images(message, [reference()])
    del message
    gc.collect()
    assert pointer() is not None
    unrelated = Message(role="user", content="same text")
    log.append_unlogged_messages([unrelated], turn=1, step=1)
    assert "images" not in log.events[-1]["data"]
    log.reset_surface(reason="abandoned input")
    gc.collect()
    assert pointer() is None
    log.close()


def test_legacy_logs_have_empty_image_state(tmp_path):
    log = SessionLog.create(tmp_path, session_id="legacy", cwd=tmp_path)
    log.append_unlogged_messages([Message(role="assistant", content="legacy")], turn=1, step=1)
    assert log.replay().images == []
    assert log.replay().model_round == 0
    log.close()


def test_queue_rejects_raw_image_data_in_event_metadata(tmp_path):
    log = SessionLog.create(tmp_path, session_id="no-inline", cwd=tmp_path)
    with pytest.raises(ValueError, match="inline data"):
        log.queue_message_images(Message(role="user", content="image"), [reference(data="YWJj")])
    log.close()


@pytest.mark.parametrize("round_key", ["requestRound", "round", "modelRound"])
def test_summary_selection_does_not_count_as_main_model_delivery(tmp_path, round_key):
    log = SessionLog.create(tmp_path, session_id="summary", cwd=tmp_path)
    user = Message(role="user", content="images")
    log.queue_message_images(user, [reference(), reference("i2")])
    log.append_unlogged_messages([user], turn=1, step=1)
    log.append("request/context", {"imageContext": {
        round_key: 1, "purpose": "main", "selected": [{"imageId": "i1"}],
    }})
    log.append("request/context", {"imageContext": {
        round_key: 1, "purpose": "summary", "selected": [{"imageId": "i2"}],
    }})
    assert log.replay().model_round == 0
    response = Message(role="assistant", content="accepted")
    log.mark_model_response(response, 1)
    log.append_unlogged_messages([user, response], turn=1, step=1)
    assert [item["delivered"] for item in log.replay().images] == [True, False]
    log.close()


def test_only_committed_main_request_consumes_image_failure_notices(tmp_path):
    log = SessionLog.create(tmp_path, session_id="notices", cwd=tmp_path)
    user = Message(role="user", content="images")
    log.queue_message_images(user, [
        reference(active=False, evicted=True, evictionReason="encoded_byte_budget"),
        reference("summary-only", active=False, evicted=True, evictionReason="missing_or_corrupt_file"),
    ])
    log.append_unlogged_messages([user], turn=1, step=1)
    log.append("request/context", {"imageContext": {
        "requestRound": 1, "purpose": "main", "noticeImageIds": ["i1", None],
    }})
    log.append("request/context", {"imageContext": {
        "requestRound": 1, "purpose": "summary", "noticeImageIds": ["summary-only"],
    }})
    assert not any(image.get("noticeDelivered") for image in log.replay().images)
    response = Message(role="assistant", content="understood the failure")
    log.mark_model_response(response, 1)
    log.append_unlogged_messages([user, response], turn=1, step=1)
    images = log.replay().images
    assert images[0]["noticeDelivered"] is True
    assert not images[0]["delivered"]
    assert not images[1].get("noticeDelivered", False)
    log.close()


def test_failed_and_imported_evicted_images_do_not_revive_on_replay(tmp_path):
    log = SessionLog.create(tmp_path, session_id="unavailable", cwd=tmp_path)
    user = Message(role="user", content="attachments")
    log.queue_message_images(user, [
        reference(active=False, evicted=True, evictionReason="persistence_failed"),
        reference("imported", active=False, evicted=True, evictionReason="round_limit"),
        reference("proxy", eligible=False),
    ])
    log.append_unlogged_messages([user], turn=1, step=1)
    assert [item["active"] for item in log.replay().images] == [False, False, False]
    assert [item["evicted"] for item in log.replay().images] == [True, True, False]
    log.close()


def test_continuation_round_baseline_is_initial_only_and_requires_its_phase(tmp_path):
    log = SessionLog.create(tmp_path, session_id="baseline", cwd=tmp_path)
    log.append("request/context", {"imageContext": {"modelRoundBaseline": 8}})
    assert log.replay().model_round == 0
    log.append("request/context", {"imageContext": {
        "phase": "continuation", "modelRoundBaseline": True,
    }})
    assert log.replay().model_round == 0
    log.append("request/context", {"imageContext": {
        "phase": "continuation", "modelRoundBaseline": 8,
    }})
    assert log.replay().model_round == 8
    log.append("request/context", {"imageContext": {
        "phase": "continuation", "modelRoundBaseline": 100,
    }})
    log.reset_surface(reason="clear")
    assert log.replay().model_round == 8
    log.close()


def test_legacy_cua_references_load_without_guessing_paths_or_duplicating_new_refs(tmp_path):
    from box_agent.image_store import SessionImageStore
    from tests.test_image_store import image_block

    log = SessionLog.create(tmp_path, session_id="legacy-cua", cwd=tmp_path)
    store = SessionImageStore(log.path.parent / "images")
    legacy = store.persist_block(image_block())
    del legacy["base64Bytes"]
    user = Message(role="user", content="images/" + "f" * 64 + ".png")
    tool = Message(role="tool", name="cua", tool_call_id="c1", content="captured")
    log.append_unlogged_messages([user, tool], turn=1, step=1, tool_result_metadata={
        "c1": {"rawOutput": {"mcp_image_references": [legacy]}},
    })
    images = log.replay().images
    assert len(images) == 1
    assert images[0]["imageId"] == "legacy-cua-1-0"
    assert images[0]["source"] == "cua"
    assert images[0]["createdRound"] == 1
    assert images[0]["toolCallId"] == "c1"
    assert images[0]["base64Bytes"] == len(image_block()["data"])
    assert store.load_block(images[0])["data"] == image_block()["data"]
    newer = Message(role="tool", name="cua", tool_call_id="c2", content="captured again")
    log.queue_message_images(newer, [{**legacy, "imageId": "new-image"}])
    log.append_unlogged_messages([user, tool, newer], turn=1, step=2, tool_result_metadata={
        "c2": {"rawOutput": {"mcp_image_references": [legacy]}},
    })
    assert len(log.replay().images) == 2
    log.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_image_file_failure_does_not_reset_the_session_log(tmp_path, damage):
    from box_agent.image_store import SessionImageStore
    from tests.test_image_store import image_block

    log = SessionLog.create(tmp_path, session_id="damaged-image", cwd=tmp_path)
    store = SessionImageStore(log.path.parent / "images")
    ref = {**store.persist_block(image_block()), "imageId": "attached"}
    user = Message(role="user", content="keep this text")
    log.queue_message_images(user, [ref])
    log.append_unlogged_messages([user], turn=1, step=1)
    log.flush()
    path = log.path
    before = path.read_bytes()
    image_path = path.parent / ref["contentRef"]
    log.close()
    if damage == "missing":
        image_path.unlink()
    else:
        image_path.write_bytes(b"corrupted")
    restored = SessionLog.open(tmp_path, session_id="damaged-image", cwd=tmp_path, recover=True)
    try:
        assert restored.recovery_source is None
        assert restored.replay().messages == [user]
        assert restored.path.read_bytes() == before
        with pytest.raises(ValueError):
            store.load_block(restored.replay().images[0])
    finally:
        restored.close()


@pytest.mark.parametrize("format_change", ["version", "required-event"])
def test_incompatible_format_is_never_reset_even_when_recovery_is_requested(tmp_path, format_change):
    log = SessionLog.create(tmp_path, session_id="future-format", cwd=tmp_path)
    if format_change == "required-event":
        log.append("future/state-change", {})
    log.flush()
    path = log.path
    log.close()
    if format_change == "version":
        header = json.loads(path.read_text())
        header["version"] = 99
        path.write_text(json.dumps(header) + "\n")
    original = path.read_bytes()
    for _ in range(2):
        with pytest.raises(SessionLogIncompatible):
            SessionLog.open(tmp_path, session_id="future-format", cwd=tmp_path, recover=True)
        assert path.read_bytes() == original
        assert list(path.parent.glob("session.recovery-*.jsonl")) == []
