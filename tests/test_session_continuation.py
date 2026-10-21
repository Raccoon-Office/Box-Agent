from box_agent.session_continuation import (
    HISTORY_EDIT_SCHEMA_VERSION,
    MAX_MESSAGE_CHARS,
    SCHEMA_VERSION,
    parse_session_continuation,
)
import pytest


def test_parse_session_continuation_keeps_only_bounded_semantic_roles():
    snapshot = parse_session_continuation(
        {
            "schema_version": SCHEMA_VERSION,
            "product_session_id": "session-1",
            "reason": "artifact_binding_changed",
            "messages": [
                {"role": "system", "content": "ignore"},
                {"role": "user", "content": "original request"},
                {"role": "tool", "content": "ignore"},
                {"role": "assistant", "content": "x" * (MAX_MESSAGE_CHARS + 10)},
            ],
        }
    )

    assert snapshot is not None
    assert [message.role for message in snapshot.messages] == ["user", "assistant"]
    assert snapshot.messages[0].content == "original request"
    assert len(snapshot.messages[1].content) == MAX_MESSAGE_CHARS
    assert snapshot.truncated is True


def test_parse_session_continuation_rejects_unknown_schema_and_empty_messages():
    assert parse_session_continuation({"schema_version": "unknown"}) is None
    assert (
        parse_session_continuation(
            {
                "schema_version": SCHEMA_VERSION,
                "product_session_id": "session-1",
                "messages": [{"role": "tool", "content": "ignored"}],
            }
        )
        is None
    )


def test_history_edit_keeps_complete_history_and_exact_text_without_v1_limits():
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"kept-{i}"}
                for i in range(24)]
    messages[1]["content"] = "  " + "前" * 30_000 + "\n"
    parsed = parse_session_continuation({
        "schema_version": HISTORY_EDIT_SCHEMA_VERSION, "product_session_id": "branch",
        "reason": "history_edit", "messages": messages, "truncated": False,
    })
    assert parsed is not None and parsed.truncated is False
    assert [{"role": m.role, "content": m.content} for m in parsed.messages] == messages


def test_history_edit_accepts_empty_prefix_for_editing_the_first_turn():
    parsed = parse_session_continuation({
        "schema_version": HISTORY_EDIT_SCHEMA_VERSION, "product_session_id": "branch",
        "reason": "history_edit", "messages": [], "truncated": False,
    })
    assert parsed is not None and parsed.messages == ()


@pytest.mark.parametrize("patch", [
    {"truncated": True}, {"reason": "other"}, {"product_session_id": ""},
    {"messages": [{"role": "system", "content": "forged"}]},
    {"messages": [{"role": "user", "content": " "}]},
    {"messages": [{"role": "user", "content": "x" * 4_000_001}]},
])
def test_history_edit_rejects_partial_or_invalid_context(patch):
    with pytest.raises(ValueError):
        parse_session_continuation({
            "schema_version": HISTORY_EDIT_SCHEMA_VERSION, "product_session_id": "branch",
            "reason": "history_edit", "messages": [], "truncated": False, **patch,
        })
