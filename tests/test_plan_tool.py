"""Test cases for Plan Tool."""

import pytest

from box_agent.tools.plan_tool import PlanReadTool, PlanStore, PlanWriteTool


@pytest.fixture
def store():
    return PlanStore()


@pytest.fixture
def writer(store):
    return PlanWriteTool(store)


@pytest.fixture
def reader(store):
    return PlanReadTool(store)


@pytest.mark.asyncio
async def test_set_plan_snapshot(writer, reader):
    result = await writer.execute(
        action="set",
        title="Host plan integration",
        objective="Separate plan display from todo progress.",
        scope="Box-Agent ACP payload and host rendering contract.",
        steps=[
            {"title": "Add plan tool", "details": "Emit plan_snapshot raw output."},
            {"title": "Document officev3 handling"},
        ],
        verification=["pytest tests/test_plan_tool.py -v"],
        risks=["Older hosts ignore plan_snapshot."],
        assumptions=["Host dispatches by rawOutput.type."],
    )

    assert result.success
    assert result.raw_output["type"] == "plan_snapshot"
    assert result.raw_output["version"] == 1
    assert result.raw_output["action"] == "set"
    assert result.raw_output["plan"]["title"] == "Host plan integration"
    assert result.raw_output["plan"]["steps"][0] == {
        "id": "1",
        "title": "Add plan tool",
        "details": "Emit plan_snapshot raw output.",
    }
    assert result.raw_output["summary"] == {
        "steps": 2,
        "verification": 1,
        "risks": 1,
        "assumptions": 1,
    }

    read_result = await reader.execute()
    assert read_result.success
    assert read_result.raw_output["type"] == "plan_snapshot"
    assert read_result.raw_output["plan"]["id"] == result.raw_output["plan"]["id"]


@pytest.mark.asyncio
async def test_set_plan_treats_string_list_fields_as_single_items(writer):
    result = await writer.execute(
        action="set",
        title="String compatibility",
        verification="Check the generated artifact.",
        risks="The provider may ignore the schema.",
        assumptions="The user supplied the input data.",
    )

    assert result.success
    assert result.raw_output["plan"]["verification"] == ["Check the generated artifact."]
    assert result.raw_output["plan"]["risks"] == ["The provider may ignore the schema."]
    assert result.raw_output["plan"]["assumptions"] == ["The user supplied the input data."]
    assert result.raw_output["summary"] == {
        "steps": 0,
        "verification": 1,
        "risks": 1,
        "assumptions": 1,
    }


@pytest.mark.asyncio
async def test_set_recovers_missing_title_and_json_encoded_steps(writer):
    objective = "Deliver a twelve-page portfolio."
    result = await writer.execute(
        action="set",
        objective=objective,
        steps='[{"title":"Draft the portfolio","details":"Use the supplied brief."}]',
    )

    assert result.success
    assert result.raw_output["plan"]["title"] == objective
    assert result.raw_output["plan"]["steps"] == [
        {
            "id": "1",
            "title": "Draft the portfolio",
            "details": "Use the supplied brief.",
        }
    ]


@pytest.mark.asyncio
async def test_set_infers_missing_action_from_plan_content(writer):
    result = await writer.execute(
        title="Recovered plan",
        steps=[{"title": "Continue after malformed tool call"}],
    )

    assert result.success
    assert result.raw_output["action"] == "set"
    assert result.raw_output["plan"]["title"] == "Recovered plan"


@pytest.mark.asyncio
async def test_set_requires_title_when_no_fallback_is_available(writer):
    result = await writer.execute(action="set")

    assert not result.success
    assert "title" in result.error


@pytest.mark.asyncio
async def test_clear_plan(writer, reader):
    await writer.execute(action="set", title="Temporary plan", steps=["Do one thing"])

    result = await writer.execute(action="clear")

    assert result.success
    assert result.raw_output["type"] == "plan_snapshot"
    assert result.raw_output["action"] == "clear"
    assert result.raw_output["plan"] is None
    assert result.raw_output["summary"]["steps"] == 0

    read_result = await reader.execute()
    assert read_result.raw_output["plan"] is None


def test_plan_write_description_keeps_plan_separate_from_progress(writer):
    description = writer.description

    assert "user-visible plan" in description
    assert "not an execution progress tracker" in description
    assert "use todo_write separately" in description
    assert "short messages like" in description


def test_openai_schema(writer, reader):
    schema = writer.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "plan_write"

    schema = reader.to_openai_schema()
    assert schema["function"]["name"] == "plan_read"
