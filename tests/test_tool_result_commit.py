"""A result becomes model history before its resource coverage is registered."""

import pytest

from box_agent.context_resources import ContextResourceLedger, ResourceClass, ResourceDescriptor
from box_agent.kernel.tool_result_pipeline import ToolResultPipelineInput, process_tool_result
from box_agent.schema import Message
from box_agent.tool_result_storage import ToolResultStorage
from box_agent.tools.base import ToolResult


def test_private_image_payload_reaches_committer_without_public_serialization(tmp_path):
    from box_agent.api.run import _json_value

    blocks = [{"type": "input_image", "media_type": "image/png", "data": "cGl4ZWxz"}]
    result = ToolResult(success=True, content="Image loaded.", image_content=blocks,
                        image_source="image_inspect", image_context_eligible=False)
    committed = []
    outcome = process_tool_result(ToolResultPipelineInput(
        messages=[], tool_call_id="image-1", tool_name="inspect_images", arguments={},
        result=result, visible_content=result.content, visible_error=None,
        result_storage=ToolResultStorage(tmp_path),
        commit_result=lambda message, event, step: committed.append(event),
    ))
    assert committed[0] is outcome.events[0]
    assert committed[0].image_content == blocks
    assert committed[0].image_source == "image_inspect"
    assert not committed[0].image_context_eligible
    assert "image_content" not in result.model_dump()
    assert "image_content" not in _json_value(committed[0])
    assert "cGl4ZWxz" not in repr(committed[0])


@pytest.mark.parametrize("commit_fails", [False, True], ids=["committed", "commit-failed"])
def test_pipeline_commits_once_before_registering_resource_history(tmp_path, commit_fails):
    messages = [Message(role="user", content="Read the fixture.")]
    ledger = ContextResourceLedger()
    descriptor = ResourceDescriptor(
        resource_id="fixture.txt", content_version="a" * 64,
        start_line=1, end_line=1, total_lines=1,
        resource_class=ResourceClass.RECONSTRUCTABLE,
    )
    result = ToolResult(
        success=True, content="The original file content.",
        raw_output={
            "truncated": False,
            "context_resource": descriptor.as_raw_output(),
            "mcp_inline_images": [{"data": "aW1hZ2U=", "mime_type": "image/png"}],
        },
    )
    commits = []

    def commit(message, event, step):
        assert len(messages) == 1
        assert ledger.source("read-1") is None
        assert event.raw_output["mcp_inline_images"] == [
            {"mime_type": "image/png", "encoded_chars": 8},
        ]
        commits.append((message, event, step))
        if commit_fails:
            raise RuntimeError("result commit failed")
        messages.append(message)

    pipeline_input = ToolResultPipelineInput(
        messages=messages, tool_call_id="read-1", tool_name="read_file",
        arguments={"path": "fixture.txt"}, result=result,
        visible_content=result.content, visible_error=None,
        result_storage=ToolResultStorage(tmp_path), resource_ledger=ledger,
        step=7, commit_result=commit,
    )
    if commit_fails:
        with pytest.raises(RuntimeError, match="result commit failed"):
            process_tool_result(pipeline_input)
        assert len(messages) == 1
        assert ledger.source("read-1") is None
    else:
        outcome = process_tool_result(pipeline_input)
        assert len(messages) == 2
        assert messages[-1] is outcome.tool_message is commits[0][0]
        assert outcome.events[0] is commits[0][1]
        assert ledger.source("read-1") is not None
    assert len(commits) == 1
    assert commits[0][2] == 7
    assert result.raw_output["mcp_inline_images"][0]["data"] == "aW1hZ2U="


@pytest.mark.parametrize("with_system", [False, True])
@pytest.mark.parametrize("targeted", [False, True], ids=["legacy", "targeted"])
def test_budget_commits_custom_store_edits_before_changing_live_results(
    tmp_path, caplog, with_system, targeted,
):
    from types import SimpleNamespace

    from box_agent.kernel.tool_messages import ToolMessageCommitter, session_log_messages
    from box_agent.session_log import SessionLog

    original = Message(role="tool", tool_call_id="one", name="lookup", content="full result")
    messages = [Message(role="user", content="look up"), original]
    if with_system:
        messages.insert(0, Message(role="system", content="base"))
    log = SessionLog.create(tmp_path, session_id="budget", cwd=tmp_path)
    writes = []

    class Store:
        append_unlogged_messages = log.append_unlogged_messages

        def replace_surface(self, surface, **kwargs):
            writes.append(("full", surface))
            return log.replace_surface(surface, **kwargs)

        def flush(self):
            assert messages[-1] is original
            log.flush()

    store = Store()
    if targeted:
        def replace_one(index, message, **kwargs):
            writes.append(("one", index, kwargs))
            return log.replace_surface_message(index, message, **kwargs)

        store.replace_surface_message = replace_one

    class Budget:
        def enforce_fresh_budget(self, surface, **kwargs):
            assert surface is not messages
            surface[-1] = surface[-1].model_copy(update={"content": "bounded"})
            # Third-party outcomes predate per-message edit metadata.
            return SimpleNamespace(persisted_count=1)

    try:
        committer = ToolMessageCommitter(messages, store, turn=2)
        committer.apply_fresh_budget(Budget(), tools={}, session_id="budget", step=3)
        assert messages[-1].content == "bounded"
        assert log.replay().messages == session_log_messages(messages)
        if targeted:
            assert writes == [("one", 1, {
                "turn": 2, "step": 3, "edit": {"reason": "tool_result_budget"},
            })]
        else:
            assert writes == [("full", session_log_messages(messages))]
        assert not caplog.records
    finally:
        log.close()


def test_failed_budget_flush_leaves_live_result_unchanged():
    from box_agent.kernel.tool_messages import ToolMessageCommitter

    original = Message(role="tool", tool_call_id="c", content="original")
    messages = [original]

    class Store:
        def replace_surface_message(self, index, message, **kwargs):
            assert index == 0 and message.content == "bounded"

        def flush(self):
            raise OSError("flush failed")

    committer = ToolMessageCommitter(messages, Store(), turn=1)
    with pytest.raises(OSError, match="flush failed"):
        committer.replace_result(0, original.model_copy(update={"content": "bounded"}),
                                 step=2, edit={"reason": "tool_result_budget"})
    assert messages[0] is original


def test_budget_event_records_file_used_by_the_preview(tmp_path):
    from contextlib import closing
    from pathlib import Path

    from box_agent.kernel.tool_messages import ToolMessageCommitter
    from box_agent.schema import FunctionCall, ToolCall
    from box_agent.session_log import SessionLog

    original = Message(role="tool", tool_call_id="one", name="read_file", content="abcd" * 1_000)
    call = ToolCall(id="one", type="function", function=FunctionCall(name="read_file", arguments={}))
    messages = [Message(role="assistant", content="", tool_calls=[call]), original]
    storage = ToolResultStorage(tmp_path, aggregate_budget=1_000)
    with closing(SessionLog.create(tmp_path, session_id="edits", cwd=tmp_path)) as log:
        result = ToolMessageCommitter(messages, log, turn=1).apply_fresh_budget(
            storage, tools={}, session_id="edits", step=2,
        )
        edit = log.events[-1]["data"]["edit"]
        assert result.edits == {1: edit}
        assert edit["reason"] == "tool_result_budget"
        assert edit["originalChars"] == len(original.content)
        assert Path(edit["persistedPath"]).read_text() == original.content
        assert edit["persistedPath"] in messages[1].content
        assert log.history_messages()[1][1] == original
