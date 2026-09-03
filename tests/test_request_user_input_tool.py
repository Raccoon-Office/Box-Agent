import pytest

from box_agent.tools.request_user_input_tool import RequestUserInputTool


@pytest.mark.asyncio
async def test_request_user_input_tool_returns_resumable_payload():
    tool = RequestUserInputTool()
    assert tool.ends_turn_on_success is True
    assert "Never use it for audience, purpose, style, page count" in tool.description
    assert "request_user_decision with a recommended default and countdown" in tool.description
    result = await tool.execute(
        question="请补充市场规模口径。",
        missing_fields=["TAM", "SAM", "SOM"],
        reason="融资路演不能虚构市场数字。",
    )

    assert result.success is True
    assert result.raw_output == {
        "type": "user_input_request",
        "version": 1,
        "status": "waiting",
        "question": "请补充市场规模口径。",
        "missingFields": ["TAM", "SAM", "SOM"],
        "reason": "融资路演不能虚构市场数字。",
        "resumeBehavior": "continue_existing_task",
    }
    assert "Preserve existing artifacts" in (result.model_context or "")


@pytest.mark.asyncio
async def test_request_user_input_tool_requires_question_and_fields():
    missing_question = await RequestUserInputTool().execute(
        question="  ",
        missing_fields=["TAM"],
    )
    missing_fields = await RequestUserInputTool().execute(
        question="请补充信息。",
        missing_fields=[],
    )
    invalid_fields = await RequestUserInputTool().execute(
        question="请补充信息。",
        missing_fields="TAM",  # type: ignore[arg-type]
    )

    assert missing_question.success is False
    assert "question" in (missing_question.error or "")
    assert missing_fields.success is False
    assert "missing_fields" in (missing_fields.error or "")
    assert invalid_fields.success is False
    assert "array" in (invalid_fields.error or "")


@pytest.mark.asyncio
async def test_request_user_input_rejects_recommendable_preferences():
    result = await RequestUserInputTool().execute(
        question="这份内马尔 PPT 主要做什么用途、面向谁？",
        missing_fields=[
            "用途（如课堂演讲或球迷科普）",
            "受众（如同学或球迷）",
        ],
        reason="用于决定演示方向。",
    )

    assert result.success is False
    assert "request_user_decision" in (result.error or "")
    assert "recommended default" in (result.error or "")
    assert "30-second timeout" in (result.error or "")
