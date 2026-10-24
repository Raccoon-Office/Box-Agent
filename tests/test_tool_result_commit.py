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
