"""Canonical evidence stays bounded, private and tied to its original user turn."""

import hashlib
import json

import pytest

from box_agent.desktop_memory.evidence import EvidenceCursor, EXCERPT_BYTES, MAX_BYTES, MAX_EVENTS
from box_agent.session_log import SessionLog


CREDENTIAL_SAMPLES = [
    "AWS_SECRET_ACCESS_KEY=SYNTHETIC_AWS_SECRET",
    "AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP",
    "AWS_SESSION_TOKEN=SYNTHETIC_SESSION_SECRET",
    "DB_PASSWORD=SYNTHETIC_DB_SECRET",
    "MY_API_KEY=SYNTHETIC_API_SECRET",
    "API key: SYNTHETIC_API_SECRET",
    "GITHUB_TOKEN=SYNTHETIC_GITHUB_SECRET",
    "MY_TOKEN=SYNTHETIC_CUSTOM_SECRET",
    "token=SYNTHETIC_BARE_SECRET",
    "ghp_SYNTHETIC_GITHUB_TOKEN_1234567890",
    "github_pat_SYNTHETIC_GITHUB_TOKEN_1234567890",
    "AKIAABCDEFGHIJKLMNOP",
    "postgres://admin:SYNTHETIC_URL_SECRET@db",
    "Cookie: session=SYNTHETIC_COOKIE_SECRET; other=SYNTHETIC_SECOND_SECRET",
    "Set-Cookie: session=SYNTHETIC_COOKIE_SECRET",
    'password="SYNTHETIC password with spaces"',
    "env.DB_PASSWORD: 'SYNTHETIC secret with quotes'",
    "-----BEGIN PRIVATE KEY-----\nSYNTHETIC_PRIVATE_KEY\n-----END PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nSYNTHETIC_UNTERMINATED_KEY",
    "密码：SYNTHETIC_SECRET",
    "密码=SYNTHETIC_SECRET",
    "密钥: SYNTHETIC_SECRET",
    "令牌=SYNTHETIC_SECRET",
    "password：SYNTHETIC_SECRET",
    "PGPASSWORD=SYNTHETIC_SECRET",
    "DB_PASS=SYNTHETIC_SECRET",
    "passphrase=SYNTHETIC_SECRET",
    "password1=SYNTHETIC_SECRET",
    "credentials=SYNTHETIC_SECRET",
    "mysql --password SYNTHETIC_SECRET",
    "mysql -pSYNTHETIC_SECRET",
    "curl -u user:SYNTHETIC_SECRET https://example.test",
    "curl -s --user user:SYNTHETIC_SECRET https://example.test",
    '["--password", "SYNTHETIC_SECRET"]',
    r'{\"password\":\"SYNTHETIC_SECRET\"}',
    "glpat-SYNTHETIC_SECRET123456789",
    "xoxb-SYNTHETIC_SECRET123456789",
    "AIzaSYNTHETIC_SECRET123456789",
    "sk_live_SYNTHETIC_SECRET123456789",
    "hf_SYNTHETIC_SECRET123456789",
    "npm_SYNTHETIC_SECRET123456789",
]


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
        assert ref["view_spec"]["excerpt_limit"] == (None if ref["coverage"] == "full" else EXCERPT_BYTES)
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


@pytest.mark.parametrize("mismatch", ["content", "original", "source"])
def test_canonical_input_facts_cannot_qualify_a_different_user_event(log, mismatch):
    data = {"role": "user", "source": "user", "content": "question", "input_facts": {
        "blocks": [{"type": "text", "text": "question"}],
        "context": {"input_context": {"schemaVersion": 1, "userRequest": "question"}}}}
    if mismatch == "content":
        data["content"] = "a different question"
    elif mismatch == "original":
        data["input_facts"]["context"]["input_context"]["userRequest"] = "a different question"
    else:
        data["source"] = "internal"
    seq = log.event_count
    log.append("user/message", data)
    with pytest.raises(ValueError, match="current user event does not match"):
        EvidenceCursor.begin(log, "question", user_seq=seq)


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
    assert first["source_ref"]["view_spec"]["excerpt_limit"] == EXCERPT_BYTES
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


@pytest.mark.parametrize("sample", CREDENTIAL_SAMPLES)
def test_suspect_tool_strings_are_omitted_whole_before_view_hashing(log, sample):
    cursor = EvidenceCursor.begin(log)
    log.append("user/message", {"content": "question", "source": "user"})
    log.append("tool/call", {"callId": "secret", "name": "fixture", "arguments": {"command": sample}})
    log.append("tool/result", {"message": {"tool_call_id": "secret"}, "result": {
        "success": True, "content": sample, "error": sample}})
    log.append("assistant/message", {"message": {"content": "answer"}})
    log.append("turn/end", {"reason": {"kind": "completed"}})
    bundle = capture(cursor, log)
    assert bundle["status"] == "captured"
    for view in bundle["views"]:
        assert "SYNTHETIC" not in view["text"] and "AKIAABCDEFGHIJKLMNOP" not in view["text"]
        assert view["source_ref"]["visible_sha256"] == hashlib.sha256(view["text"].encode()).hexdigest()
    assert any(event["data"].get("result", {}).get("content") == sample for event in log.events)


@pytest.mark.parametrize("arguments,result,call_coverage,result_coverage", [
    ({"command": "DB_PASSWORD=SYNTHETIC_SECRET check"}, "passed", "excerpt", "full"),
    ({"command": "check"}, "API_KEY=SYNTHETIC_SECRET", "full", "excerpt"),
    ({"image": "data:image/png;base64,QUJDRA=="}, "passed", "excerpt", "full"),
    ({"command": "check"}, "passed", "full", "full"),
])
def test_filtered_tool_views_are_not_described_as_complete(log, arguments, result, call_coverage, result_coverage):
    cursor = EvidenceCursor.begin(log)
    log.append("user/message", {"content": "question", "source": "user"})
    log.append("tool/call", {"callId": "call", "name": "fixture", "arguments": arguments})
    log.append("tool/result", {"message": {"tool_call_id": "call"}, "result": {
        "success": True, "content": result, "error": None}})
    log.append("assistant/message", {"message": {"content": "answer"}})
    log.append("turn/end", {"reason": {"kind": "completed"}})
    bundle = capture(cursor, log)
    views = {view["source_ref"]["event_type"]: view for view in bundle["views"]}
    for kind, expected in (("tool/call", call_coverage), ("tool/result", result_coverage)):
        view = views[kind]
        assert view["source_ref"]["coverage"] == expected
        assert view["source_ref"]["view_spec"]["excerpt_limit"] == (4096 if expected == "excerpt" else None)
        assert view["source_ref"]["visible_sha256"] == hashlib.sha256(view["text"].encode()).hexdigest()


def test_prefixed_nested_credential_fields_are_filtered_without_losing_safe_results():
    from box_agent.desktop_memory.evidence import _redact
    value = {"env": {"DB_PASSWORD": "SYNTHETIC_DB", "AWS_SECRET_ACCESS_KEY": "SYNTHETIC_AWS"},
             "items": [{"MY_API_KEY": "SYNTHETIC_API", "GITHUB_TOKEN": "SYNTHETIC_TOKEN"}],
             "success": True, "content": "7 validations passed"}
    clean = _redact(value)
    assert "SYNTHETIC" not in json.dumps(clean)
    assert clean["success"] is True and clean["content"] == "7 validations passed"
    assert value["env"]["DB_PASSWORD"] == "SYNTHETIC_DB"


@pytest.mark.parametrize("text", [
    "The key: persistence matters",
    '{"key": "value", "count": 2}',
    'Use key=value in this example',
    'cache_key = "query-cache"',
    'primary_key=True',
    'total_token: 1200',
    '使用 Bearer token 认证',
    'python -u script.py',
    'pip install --user example-package',
])
def test_ordinary_key_text_survives_evidence_capture(log, text):
    cursor = EvidenceCursor.begin(log)
    completed(log, text, text)
    bundle = capture(cursor, log, user=text, assistant=text)
    assert bundle["status"] == "captured"
    assert json.loads(bundle["views"][0]["text"])["content"] == text
    assert json.loads(bundle["views"][1]["text"])["content"] == text


def test_prefixed_key_is_still_treated_as_sensitive():
    from box_agent.desktop_memory.evidence import _redact
    assert _redact("DB_KEY=SYNTHETIC_SECRET") == "[REDACTED]"
    assert _redact({"DB_KEY": "SYNTHETIC_SECRET"})["DB_KEY"] == "[REDACTED]"


@pytest.mark.parametrize("field", ["密码", "密钥", "令牌", "PGPASSWORD", "DB_PASS", "passphrase", "password1", "credentials", "MY_TOKEN"])
def test_labeled_credential_fields_are_removed_from_nested_data(field):
    from box_agent.desktop_memory.evidence import _redact

    assert _redact({"env": [{field: "SYNTHETIC_SECRET"}]}) == {"env": [{field: "[REDACTED]"}]}


def test_ordinary_metadata_fields_survive_structured_redaction():
    from box_agent.desktop_memory.evidence import _redact

    value = {"cache_key": "cached", "primary_key": True, "total_token": 1200}
    assert _redact(value) == value


@pytest.mark.parametrize("size", [64 * 1024, 256 * 1024])
def test_colon_heavy_url_filter_finishes_without_blocking(size):
    # Bound the child process so an old quadratic regex cannot hang the runner.
    # Measure redaction separately from interpreter/dependency cold imports.
    import subprocess
    import sys

    subprocess.run([sys.executable, "-c", (
        "from box_agent.desktop_memory.evidence import _redact; "
        "from time import perf_counter; started = perf_counter(); "
        f"text = 'postgres://' + ':' * {size}; "
        "assert _redact(text) == text; "
        "assert _redact(text + '@db') == '[REDACTED]'; "
        "assert _redact('postgres://admin:密码@db') == '[REDACTED]'; "
        "assert perf_counter() - started < 3, 'redaction exceeded 3 seconds'"
    )], check=True, timeout=30)


def test_pending_current_user_can_precede_refresh_but_completed_history_cannot(log):
    completed(log)
    old_end = log.event_count
    assert EvidenceCursor.begin(log, "question").start == old_end
    event = log.append("user/message", {"content": "question", "source": "user"})
    log.append("user/message", {"content": "question", "source": "runtime"})
    cursor = EvidenceCursor.begin(log, "question", user_seq=event["seq"])
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
    event = log.append("user/message", {"content": wrapped, "source": "user"})
    cursor = EvidenceCursor.begin(log, user, user_seq=event["seq"])
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


@pytest.mark.parametrize("prelogged", [False, True])
@pytest.mark.parametrize("metadata", [None, "", 123, "<connector-status>different</connector-status>"])
def test_connector_text_requires_matching_trusted_metadata(log, prelogged, metadata):
    from box_agent.connector_context import append_connector_status

    content = append_connector_status([], "question", "<connector-status>connected</connector-status>")
    if not prelogged:
        cursor = EvidenceCursor.begin(log, "question")
    log.append("user/message", {"content": content, "source": "user", "host_connector_status": metadata})
    if prelogged:
        cursor = EvidenceCursor.begin(log, "question")
        assert cursor.start == log.event_count
    log.append("assistant/message", {"message": {"content": "answer"}})
    log.append("turn/end", {"reason": {"kind": "completed"}})
    assert capture(cursor, log)["status"] == "no_evidence"


@pytest.mark.parametrize("prelogged", [False, True])
@pytest.mark.parametrize("user", ["好", "好  \n\t", "好的，把 a.txt 删掉"])
def test_short_user_reply_does_not_collect_unfinished_previous_turn(log, prelogged, user):
    from box_agent.agent import Agent
    from box_agent.schema import Message

    agent = Agent(llm_client=object(), tools=[], system_prompt="system", session_log=log)
    snapshot = "<connector-status>connected</connector-status>"
    agent.add_user_message("好的，把 a.txt 删掉", host_connector_status=snapshot)
    agent._persist_unlogged_messages(turn=1, step=None)
    log.append("tool/call", {"callId": "old", "name": "old_tool", "arguments": {}})
    old_result = Message(role="tool", content="OLD_TURN_RESULT", tool_call_id="old")
    agent.messages.append(old_result)
    agent._persist_unlogged_messages(turn=1, step=None, tool_result_metadata={
        "old": {"success": True, "content": "OLD_TURN_RESULT"},
    })
    # There is deliberately no turn/end for the old user message.
    start = log.event_count
    agent.add_user_message(user, host_connector_status="<connector-status>disconnected</connector-status>")
    current = agent.messages[-1]
    if prelogged:
        agent._persist_unlogged_messages(turn=2, step=None)
    user_seq = log.surface_message_sequence(agent.messages[1:], current)
    cursor = EvidenceCursor.begin(log, user, user_seq=user_seq)
    assert cursor.start == start
    agent._persist_unlogged_messages(turn=2, step=None)
    log.append("assistant/message", {"message": {"content": "answer"}})
    log.append("turn/end", {"reason": {"kind": "completed"}})
    bundle = capture(cursor, log, user=user)
    assert bundle["status"] == "captured"
    assert json.loads(bundle["views"][0]["text"]) == {"content": user}
    assert all(v["source_ref"]["event_seqs"][0] >= start for v in bundle["views"])
    assert "OLD_TURN_RESULT" not in json.dumps(bundle)
    assert len(bundle["views"]) == 3


@pytest.mark.parametrize("name", ["memory_search", "memory_experience_search", "memory_read", "memory_managed_read"])
def test_memory_recall_results_cannot_become_execution_evidence(log, name):
    cursor = EvidenceCursor.begin(log)
    log.append("user/message", {"content": "question", "source": "user"})
    for call_id, tool in [("recalled", name), ("actual", "run_check")]:
        log.append("tool/call", {"callId": call_id, "name": tool, "arguments": {}})
        log.append("tool/result", {"message": {"tool_call_id": call_id},
                                   "result": {"success": True, "content": call_id}})
    log.append("assistant/message", {"message": {"content": "answer"}})
    log.append("turn/end", {"reason": {"kind": "completed"}})
    bundle = capture(cursor, log)
    assert bundle["status"] == "captured" and bundle["omitted"] == 1
    assert {v["source_ref"]["call_id"] for v in bundle["views"] if v["source_ref"]["call_id"]} == {"actual"}
    assert "recalled" not in json.dumps(bundle)
