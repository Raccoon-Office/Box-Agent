"""Canonical evidence stays bounded, private and tied to its original user turn."""

import hashlib
import json

import pytest

from box_agent.desktop_memory.evidence import EvidenceCursor, EXCERPT_BYTES, MAX_BYTES, MAX_EVENTS
from box_agent.session_log import SessionLog


@pytest.fixture
def log(tmp_path):
    value = SessionLog.create(tmp_path, session_id="logical-session", cwd=tmp_path)
    yield value
    value.close()


def completed(log, user="question", assistant="answer", *, tool=False):
    log.append("turn/start", {"turn": 1})
    log.append("user/message", {"role": "user", "content": user, "source": "user"})
    if tool:
        log.append("tool/call", {"turn": 1, "callId": "call-1", "name": "test_tool",
                                 "arguments": {"access_token": "STRUCTURED_SECRET", "command": "Bearer TEXT_SECRET"}})
        log.append("tool/result", {"turn": 1, "message": {"tool_call_id": "call-1"},
                                   "result": {"success": True, "content": 'api_key="STRING_SECRET" validated',
                                              "error": None, "rawOutput": "RAW_OUTPUT_SECRET"}})
    log.append("assistant/message", {"turn": 1, "message": {"role": "assistant", "content": assistant,
                                                            "reasoning_content": "THINKING_SECRET"}})
    log.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    log.flush()


def capture(cursor, log, **overrides):
    return cursor.capture(session_id="canonical-session", turn_id="turn-id", event_id="event-id",
                          user=overrides.get("user", "question"), assistant=overrides.get("assistant", "answer"),
                          end=overrides.get("end", log.event_count))


def test_canonical_suffix_excludes_history_and_hashes_exact_redacted_tool_views(log):
    completed(log, "OLD_HISTORY_SECRET", "old answer")
    cursor = EvidenceCursor.begin(log)
    log.append("request/context", {"payload": "REQUEST_SECRET"})
    log.append("assistant/chunk", {"kind": "thinking", "content": "THINKING_SECRET"})
    completed(log, tool=True)
    bundle = capture(cursor, log)
    assert bundle["status"] == "captured"
    encoded = json.dumps(bundle, ensure_ascii=False)
    for secret in ["OLD_HISTORY_SECRET", "REQUEST_SECRET", "THINKING_SECRET", "RAW_OUTPUT_SECRET",
                   "STRUCTURED_SECRET", "TEXT_SECRET", "STRING_SECRET"]:
        assert secret not in encoded
    assert len(bundle["views"]) == 5
    for view in bundle["views"]:
        ref = view["source_ref"]
        assert len(ref) == 9 and ref["session_id"] == "canonical-session"
        assert ref["event_seqs"][0] >= cursor.start
        assert ref["visible_sha256"] == hashlib.sha256(view["text"].encode()).hexdigest()
        assert ref["source_id"].startswith("session-log:")
    assert "[REDACTED]" in encoded
    assert not any(view["source_ref"]["event_type"] == "tool/result" and '"success":false' in view["text"]
                   for view in bundle["views"])


def test_cursor_end_cannot_consume_later_turns_or_internal_messages(log):
    cursor = EvidenceCursor.begin(log)
    completed(log)
    end = log.event_count
    completed(log, "NEXT_TURN_SECRET", "next answer")
    bundle = capture(cursor, log, end=end)
    assert bundle["status"] == "captured"
    assert "NEXT_TURN_SECRET" not in json.dumps(bundle)
    assert all(v["source_ref"]["event_seqs"][0] < end for v in bundle["views"])


@pytest.mark.parametrize("kind", ["internal", "missing_user", "missing_answer", "missing_end"])
def test_incomplete_or_internal_only_events_never_become_completed_evidence(log, kind):
    cursor = EvidenceCursor.begin(log)
    if kind != "missing_user":
        log.append("user/message", {"content": "question", "source": "internal" if kind == "internal" else "user"})
    if kind != "missing_answer":
        log.append("assistant/message", {"message": {"content": "answer"}})
    if kind != "missing_end":
        log.append("turn/end", {"reason": {"kind": "completed"}})
    assert capture(cursor, log)["status"] == "no_evidence"


def test_unicode_excerpts_remain_valid_and_do_not_claim_full_coverage(log):
    cursor = EvidenceCursor.begin(log)
    text = "中文😀" * 3000
    completed(log, text, "answer")
    bundle = capture(cursor, log, user=text)
    assert bundle["status"] == "captured"
    first = bundle["views"][0]
    assert len(first["text"].encode()) <= EXCERPT_BYTES
    assert first["source_ref"]["coverage"] == "excerpt"
    assert len(json.dumps(bundle, ensure_ascii=False).encode()) <= MAX_BYTES


def test_event_budget_returns_explicit_reason_without_scanning_old_history(log, monkeypatch):
    cursor = EvidenceCursor.begin(log)
    for n in range(MAX_EVENTS + 1):
        log.append("assistant/chunk", {"content": str(n)})
    monkeypatch.setattr(log, "event_page", lambda *a, **kw: pytest.fail("must not read over-budget suffix"))
    assert capture(cursor, log)["reason"] == "event_budget"


def test_model_assertion_alone_never_becomes_verified_tool_evidence(log):
    cursor = EvidenceCursor.begin(log)
    completed(log, assistant="I successfully tested everything")
    bundle = capture(cursor, log, assistant="I successfully tested everything")
    assert bundle["status"] == "captured"
    assert len(bundle["views"]) == 3
    assert all(v["source_ref"]["event_type"] != "tool/result" for v in bundle["views"])
    assert json.loads(bundle["views"][1]["text"])["claim_only"] is True


def test_event_pages_are_detached_and_bounded_without_copying_history(log):
    completed(log)
    page = log.event_page(1, limit=1)
    assert len(page) == 1
    page[0]["data"]["content"] = "mutated"
    assert log.event_page(1, limit=1)[0]["data"]["content"] == "question"
    assert log.event_page(log.event_count) == ()
    for start, limit in [(-1, 1), (True, 1), (log.event_count + 1, 1), (0, 0), (0, 257)]:
        with pytest.raises(ValueError):
            log.event_page(start, limit=limit)


def test_no_log_is_not_an_empty_success_or_historical_fallback():
    assert EvidenceCursor.begin(None) is None


def test_pending_current_user_can_precede_refresh_but_completed_history_cannot(log):
    completed(log)
    old_end = log.event_count
    assert EvidenceCursor.begin(log, "question").start == old_end
    event = log.append("user/message", {"content": "question", "source": "user"})
    log.append("user/message", {"content": "question", "source": "runtime"})
    cursor = EvidenceCursor.begin(log, "question")
    assert cursor.start == event["seq"]
    log.append("assistant/message", {"message": {"content": "answer"}})
    log.append("turn/end", {"reason": {"kind": "completed"}})
    bundle = capture(cursor, log)
    assert bundle["status"] == "captured"
    assert bundle["views"][0]["source_ref"]["event_seqs"] == [event["seq"]]


def test_runtime_messages_never_become_user_evidence_even_with_equal_text(log):
    cursor = EvidenceCursor.begin(log)
    log.append("user/message", {"content": "question", "source": "runtime"})
    log.append("assistant/message", {"message": {"content": "answer"}})
    log.append("turn/end", {"reason": {"kind": "completed"}})
    assert capture(cursor, log)["status"] == "no_evidence"


def test_framework_hints_are_verified_but_only_user_words_enter_the_view(log):
    from box_agent.turn_hints import with_turn_hints
    user = "Please explain how this system works and how I can use it."
    wrapped = with_turn_hints(user)
    assert wrapped != user
    log.append("user/message", {"content": wrapped, "source": "user"})
    cursor = EvidenceCursor.begin(log, user)
    log.append("assistant/message", {"message": {"content": "answer"}})
    log.append("turn/end", {"reason": {"kind": "completed"}})
    bundle = capture(cursor, log, user=user)
    assert bundle["status"] == "captured"
    assert json.loads(bundle["views"][0]["text"])["content"] == user
    assert bundle["views"][0]["source_ref"]["coverage"] == "excerpt"


def test_inline_attachment_bytes_are_omitted_from_tool_evidence(log):
    cursor = EvidenceCursor.begin(log)
    log.append("user/message", {"content": "question", "source": "user"})
    log.append("tool/call", {"callId": "image", "name": "synthetic", "arguments": {}})
    log.append("tool/result", {"message": {"tool_call_id": "image"}, "result": {
        "success": True, "content": "data:image/png;base64," + "A" * 2000 + " result"}})
    log.append("assistant/message", {"message": {"content": "answer"}})
    log.append("turn/end", {"reason": {"kind": "completed"}})
    bundle = capture(cursor, log)
    assert bundle["status"] == "captured"
    encoded = json.dumps(bundle)
    assert "[OMITTED_ATTACHMENT]" in encoded and "A" * 100 not in encoded
