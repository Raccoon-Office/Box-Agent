from box_agent.session_continuation import (
    MAX_MESSAGE_CHARS,
    SCHEMA_VERSION,
    parse_session_continuation,
)


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
