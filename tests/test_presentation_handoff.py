"""Verify the existing write-capable sub-agent input boundary without an LLM call."""

from unittest.mock import AsyncMock

import pytest

from box_agent.schema import StreamEvent
from box_agent.tools.file_tools import ReadTool, WriteTool
from box_agent.tools.sub_agent_tool import SubAgentTool


@pytest.mark.asyncio
@pytest.mark.parametrize("provided", [False, True])
async def test_page_plan_is_available_without_reading_only_when_task_contains_original(tmp_path, provided):
    original = "## 最终屏显文案\n销售目标 2800 万元；会员占比 42%。\n"
    plan = tmp_path / "slide_02.md"
    plan.write_text(original)
    captured = []

    async def capture_input(*, messages, **kwargs):
        captured.append(next(message.content for message in messages if message.role == "user"))
        yield StreamEvent(type="text", delta="Input capture only; no page or QA was produced.")
        yield StreamEvent(type="finish", finish_reason="stop")

    llm = AsyncMock()
    llm.generate_stream = capture_input
    reader = ReadTool(workspace_dir=str(tmp_path))
    tool = SubAgentTool(llm=llm, workspace_dir=str(tmp_path), parent_tools={
        "read_file": reader, "write_file": WriteTool(workspace_dir=str(tmp_path)),
    })
    task = "Slide Group growth [02]:\n制作所属页，不改变原文。"
    if provided:
        task += f"\n已提供原文：{plan}\n{original}"
    result = await tool.execute(
        task=task, files=[str(plan)], required_tools=["read_file", "write_file"],
        write_scope=[str(tmp_path / "slides/slide_02.html")],
    )
    assert result.success, result.error
    assert result.raw_output["strategy"] == "general_loop"
    assert str(plan) in captured[0]
    assert (original in captured[0]) is provided
    assert plan.read_text() == original
    assert not (tmp_path / "slides/slide_02.html").exists()
