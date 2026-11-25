import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from box_agent.schema import Message
from box_agent.tool_result_storage import (
    DEFAULT_MAX_RESULT_SIZE_CHARS,
    DEFAULT_TOOL_RESULTS_BUDGET_CHARS,
    ToolResultStorage,
    generate_preview,
)
from box_agent.tools.base import Tool
from box_agent.tools.file.read_tool import ReadTool


class _Tool(Tool):
    @property
    def name(self) -> str:
        return "bash"


def _message(content, tool_call_id="call-1", name="bash") -> Message:
    return Message(
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
        name=name,
    )


def test_default_budget_floors_are_20k_per_result_and_50k_per_fresh_batch() -> None:
    assert DEFAULT_MAX_RESULT_SIZE_CHARS == 20_000
    assert DEFAULT_TOOL_RESULTS_BUDGET_CHARS == 50_000


def test_generate_preview_keeps_head_and_tail_when_middle_is_omitted() -> None:
    content = "HEAD_FACT\n" + "x" * 3_000 + "\nTAIL_FACT"
    preview, has_more = generate_preview(content)

    assert preview.startswith("HEAD_FACT")
    assert preview.endswith("TAIL_FACT")
    assert "middle omitted from preview" in preview
    assert len(preview) <= 2_000
    assert has_more is True


def test_generate_preview_uses_exact_limit_for_one_long_line() -> None:
    content = "H" + "x" * 3_000 + "T"
    preview, has_more = generate_preview(content)

    assert len(preview) == 2_000
    assert preview.startswith("H")
    assert preview.endswith("T")
    assert has_more is True


def test_context_limit_scales_default_budgets_but_not_explicit_overrides(
    tmp_path: Path,
) -> None:
    contextual = ToolResultStorage(tmp_path / "contextual")
    contextual.set_context_token_limit(374_400)

    assert contextual.default_result_limit == 93_600
    assert contextual.aggregate_budget == 187_200

    explicit = ToolResultStorage(
        tmp_path / "explicit",
        default_result_limit=12_345,
        aggregate_budget=54_321,
    )
    explicit.set_context_token_limit(374_400)

    assert explicit.default_result_limit == 12_345
    assert explicit.aggregate_budget == 54_321


def test_immediate_large_results_with_reused_id_keep_their_own_files(tmp_path: Path) -> None:
    storage = ToolResultStorage(tmp_path, default_result_limit=10)
    original = _message("HEAD_FACT\n" + "x" * 2_500 + "\nTAIL_FACT")

    first = storage.process_message(original, tool=_Tool(), session_id="session-a")
    path = tmp_path / "session-a" / "tool-results" / "call-1.txt"
    assert path.read_text(encoding="utf-8") == original.content
    assert first.content.startswith("<persisted-output>\n")
    assert str(path) in first.content
    assert "HEAD_FACT" in first.content
    assert "TAIL_FACT" in first.content
    assert "middle omitted from preview" in first.content

    new_payload = _message("a different payload for the same id")
    second = storage.process_message(new_payload, tool=_Tool(), session_id="session-a")
    second_path = Path(second.content.split("Full output saved to: ", 1)[1].splitlines()[0])
    assert second_path != path
    assert second_path.read_text(encoding="utf-8") == new_payload.content
    assert path.read_text(encoding="utf-8") == original.content
    assert new_payload.content in second.content
    assert "HEAD_FACT" not in second.content


def test_complete_tool_payload_is_persisted_instead_of_bounded_host_view(
    tmp_path: Path,
) -> None:
    storage = ToolResultStorage(tmp_path, default_result_limit=10)
    bounded = _message("bounded host preview")
    complete = "complete beginning\n" + "x" * 2_500 + "\ncomplete end"

    result = storage.process_message(
        bounded,
        tool=_Tool(),
        session_id="session-a",
        persistence_content=complete,
    )

    path = tmp_path / "session-a" / "tool-results" / "call-1.txt"
    assert path.read_text(encoding="utf-8") == complete
    assert result.content.startswith("<persisted-output>")
    assert "Tool-bounded output" in result.content
    assert "bounded host preview" in result.content
    assert "complete beginning" not in result.content


def test_processed_model_context_is_not_persisted_or_reprocessed(
    tmp_path: Path,
) -> None:
    storage = ToolResultStorage(
        tmp_path,
        default_result_limit=5,
        aggregate_budget=5,
    )
    processed = _message("compact semantic context", tool_call_id="semantic-1")

    immediate = storage.process_message(
        processed,
        tool=_Tool(),
        session_id="session-a",
        content_already_processed=True,
    )
    messages = [Message.model_validate_json(immediate.model_dump_json())]
    aggregate = storage.enforce_fresh_budget(
        messages,
        tools={"bash": _Tool()},
        session_id="session-a",
    )

    assert immediate.content == processed.content
    assert aggregate.fresh_count == 0
    assert messages[0].content == "compact semantic context"
    assert not (tmp_path / "session-a" / "tool-results").exists()


def test_text_block_array_is_json_but_non_text_blocks_are_preserved(tmp_path: Path) -> None:
    storage = ToolResultStorage(tmp_path, default_result_limit=5)
    text_blocks = _message([{"type": "text", "text": "large text"}])
    persisted = storage.process_message(text_blocks, tool=_Tool(), session_id="s")
    path = tmp_path / "s" / "tool-results" / "call-1.json"
    assert json.loads(path.read_text(encoding="utf-8")) == text_blocks.content
    assert "<persisted-output>" in persisted.content

    image = _message(
        [{"type": "image", "source": {"type": "base64", "data": "x" * 100}}],
        tool_call_id="call-image",
    )
    assert storage.process_message(image, tool=_Tool(), session_id="s") is image
    assert not (tmp_path / "s" / "tool-results" / "call-image.json").exists()


def test_read_is_infinite_and_empty_output_gets_marker(tmp_path: Path) -> None:
    storage = ToolResultStorage(tmp_path, default_result_limit=5)
    read = ReadTool(workspace_dir=str(tmp_path))
    large = _message("x" * 1_000, name="read_file")
    empty = _message("  \n", tool_call_id="empty")

    assert storage.process_message(large, tool=read, session_id="s") is large
    assert storage.process_message(empty, tool=_Tool()).content == "(Bash completed with no output)"


def test_persistence_failure_keeps_original(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    storage = ToolResultStorage(blocker, default_result_limit=5)
    original = _message("x" * 100)

    assert storage.process_message(original, tool=_Tool(), session_id="s") is original


def test_fresh_budget_persists_largest_results_and_preserves_sent_history(tmp_path: Path) -> None:
    storage = ToolResultStorage(
        tmp_path,
        default_result_limit=1_000,
        aggregate_budget=1_000,
    )
    messages = [
        _message("a" * 600, "a"),
        _message("b" * 500, "b"),
        _message("c" * 400, "c"),
    ]
    tools = {"bash": _Tool()}

    first = storage.enforce_fresh_budget(messages, tools=tools, session_id="s")
    assert first.fresh_count == 3
    assert first.persisted_count >= 2
    assert first.remaining_chars == sum(len(str(message.content)) for message in messages)
    assert first.remaining_chars <= storage.aggregate_budget
    assert "<persisted-output>" in messages[0].content
    assert "<persisted-output>" in messages[1].content
    assert "Preview (head + tail" in messages[0].content
    assert "Preview (head + tail" in messages[1].content

    sent = tuple(message.model_dump_json() for message in messages)
    for index, edit in first.edits.items():
        assert edit["originalChars"] == (600, 500, 400)[index]
        assert edit["reason"] == "tool_result_budget"
        assert Path(edit["persistedPath"]).read_text() == "abc"[index] * edit["originalChars"]
        assert edit["persistedPath"] in messages[index].content
    assert len(first.edits) == first.persisted_count
    assert len(list((tmp_path / "s" / "tool-results").iterdir())) == first.persisted_count

    messages.append(Message(role="assistant", content="received"))
    messages.append(_message("another call using a" * 100, "a"))
    second = storage.enforce_fresh_budget(messages, tools=tools, session_id="s")
    assert second.fresh_count == 1
    assert second.persisted_count == 1
    assert second.edits[4]["persistedPath"] != first.edits[0]["persistedPath"]
    assert "another call using a" in messages[-1].content
    assert tuple(message.model_dump_json() for message in messages[:3]) == sent


def test_default_aggregate_budget_counts_actual_persisted_wrappers(tmp_path: Path) -> None:
    storage = ToolResultStorage(tmp_path)
    messages = [
        _message("x" * 1_000, f"call-{index}")
        for index in range(100)
    ]

    outcome = storage.enforce_fresh_budget(
        messages,
        tools={"bash": _Tool()},
        session_id="batch",
    )

    actual_chars = sum(len(str(message.content)) for message in messages)
    assert outcome.remaining_chars == actual_chars
    assert actual_chars <= DEFAULT_TOOL_RESULTS_BUDGET_CHARS
    assert outcome.persisted_count > 0


def test_aggregate_receipt_keeps_tail_fact_for_next_model_step(tmp_path: Path) -> None:
    storage = ToolResultStorage(
        tmp_path,
        default_result_limit=100_000,
        aggregate_budget=20_000,
    )
    messages = [
        _message(
            f"HEAD_{label}\n" + "x" * 15_000 + f"\nTAIL_FACT_{label}",
            f"call-{label}",
        )
        for label in ("A", "B")
    ]

    outcome = storage.enforce_fresh_budget(
        messages,
        tools={"bash": _Tool()},
        session_id="semantic-batch",
    )

    assert outcome.persisted_count >= 1
    for label, message in zip(("A", "B"), messages):
        if "<persisted-output>" not in str(message.content):
            continue
        assert f"HEAD_{label}" in message.content
        assert f"TAIL_FACT_{label}" in message.content


def test_empty_session_ids_use_isolated_storage_namespaces(tmp_path: Path) -> None:
    first_storage = ToolResultStorage(tmp_path, default_result_limit=5)
    second_storage = ToolResultStorage(tmp_path, default_result_limit=5)

    first = first_storage.process_message(_message("first payload"), tool=_Tool())
    second = second_storage.process_message(_message("second payload"), tool=_Tool())

    first_path = Path(str(first.content).split("Full output saved to: ", 1)[1].splitlines()[0])
    second_path = Path(str(second.content).split("Full output saved to: ", 1)[1].splitlines()[0])
    assert first_path != second_path
    assert first_path.read_text(encoding="utf-8") == "first payload"
    assert second_path.read_text(encoding="utf-8") == "second payload"


def test_literal_storage_tag_in_raw_output_does_not_exempt_it_from_the_budget(tmp_path):
    storage = ToolResultStorage(tmp_path, aggregate_budget=1_000)
    raw = "<persisted-output> raw transcript " + "x" * 5_000
    messages = [_message(raw)]
    outcome = storage.enforce_fresh_budget(messages, tools={})
    assert outcome.persisted_count == 1
    assert Path(outcome.edits[0]["persistedPath"]).read_text() == raw


def test_explicit_session_collision_never_points_at_stale_content(tmp_path: Path) -> None:
    first_storage = ToolResultStorage(tmp_path, default_result_limit=5)
    second_storage = ToolResultStorage(tmp_path, default_result_limit=5)
    first_storage.process_message(
        _message("first payload"),
        tool=_Tool(),
        session_id="shared",
    )

    second = second_storage.process_message(
        _message("second payload"),
        tool=_Tool(),
        session_id="shared",
    )

    second_path = Path(str(second.content).split("Full output saved to: ", 1)[1].splitlines()[0])
    assert second_path.name.startswith("call-1-")
    assert second_path.read_text(encoding="utf-8") == "second payload"


def test_concurrent_reuse_of_a_call_id_shares_only_complete_identical_files(tmp_path: Path) -> None:
    storage = ToolResultStorage(tmp_path, default_result_limit=1)
    prefix = "保存\r\n" * 4_000
    payloads = (prefix, prefix + "more", prefix + "more", prefix)

    def save(payload: str) -> Path:
        result = storage.process_message(_message(payload), tool=None, session_id="shared")
        return Path(result.content.split("Full output saved to: ", 1)[1].splitlines()[0])

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        paths = list(pool.map(save, payloads))

    assert [path.read_bytes() for path in paths] == [text.encode("utf-8") for text in payloads]
    assert paths[0] == paths[3] and paths[1] == paths[2]
    assert len(set(paths)) == 2


def test_persisted_receipt_references_complete_file_on_volumes_without_hard_links(
    tmp_path: Path, monkeypatch,
) -> None:
    def unavailable(*args):
        raise OSError("hard links unsupported on this volume")

    monkeypatch.setattr(Path, "hardlink_to", unavailable)
    storage = ToolResultStorage(tmp_path, default_result_limit=1)
    message = _message("preserve this file")
    receipt = storage.process_message(message, tool=None, session_id="s")
    path = Path(receipt.content.split("Full output saved to: ", 1)[1].splitlines()[0])
    assert path.read_text() == message.content


def test_aggregate_budget_persists_read_results_when_batch_exceeds_limit(
    tmp_path: Path,
) -> None:
    storage = ToolResultStorage(tmp_path, aggregate_budget=500)
    messages = [_message("x" * 10_000, "read-1", "read_file")]

    outcome = storage.enforce_fresh_budget(
        messages,
        tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
        session_id="s",
    )

    assert outcome.fresh_count == 1
    assert outcome.persisted_count == 1
    assert outcome.remaining_chars <= storage.aggregate_budget
    assert "<persisted-output>" in messages[0].content
    assert "Preview (head + tail" in messages[0].content
    assert "original path" in messages[0].content
    assert "smaller offset/limit" in messages[0].content
    assert (
        tmp_path / "s" / "tool-results" / "read-1.txt"
    ).read_text(encoding="utf-8") == "x" * 10_000


def test_resume_budgets_only_unsent_results_across_a_user_injection(tmp_path: Path) -> None:
    storage = ToolResultStorage(tmp_path, aggregate_budget=500)
    historical = _message("historical" * 1_000, "old")
    unsent = _message("still pending" * 1_000, "old")
    messages = [
        historical, Message(role="assistant", content="calling again"), unsent,
        Message(role="user", content="one more thing"),
    ]
    storage.initialize_history(messages)

    outcome = storage.enforce_fresh_budget(messages, tools={"bash": _Tool()}, session_id="s")

    assert outcome.fresh_count == outcome.persisted_count == 1
    assert set(outcome.edits) == {2}
    assert messages[0] is historical
    assert "still pending" in messages[2].content
    assert messages[-1].content == "one more thing"

    # A retry before the next assistant response sees the same stored receipt.
    retry = ToolResultStorage(tmp_path, aggregate_budget=500)
    retry.initialize_history(messages)
    assert retry.enforce_fresh_budget(messages, tools={}).persisted_count == 0
    assert Path(outcome.edits[2]["persistedPath"]).read_text() == unsent.content
