"""Tests for the host-neutral execution result contract."""

import pytest

from box_agent.tools.execution_result_tool import ReportExecutionResultTool


@pytest.fixture
def tool():
    return ReportExecutionResultTool()


@pytest.mark.asyncio
async def test_reports_completed_execution_result(tool):
    result = await tool.execute(
        outcome="completed",
        summary="Implemented the requested API and verified it.",
        changes=[
            {
                "kind": "code",
                "summary": "Added the project member directory endpoint.",
                "reference": "src/routes/projects.ts",
            }
        ],
        checks=[
            {
                "name": "integration tests",
                "status": "passed",
                "summary": "Client-readiness scenarios passed.",
                "reference": "tests/integration/client-readiness.test.ts",
            }
        ],
        criteria_evaluations=[
            {
                "criterion_index": 0,
                "status": "passed",
                "summary": "The directory endpoint is implemented and verified.",
                "evidence": ["tests/integration/client-readiness.test.ts"],
            }
        ],
        known_limitations=["Production identity mapping remains host-owned."],
        questions=[],
    )

    assert result.success
    assert result.raw_output == {
        "type": "execution_result",
        "version": 1,
        "outcome": "completed",
        "summary": "Implemented the requested API and verified it.",
        "changes": [
            {
                "kind": "code",
                "summary": "Added the project member directory endpoint.",
                "reference": "src/routes/projects.ts",
            }
        ],
        "checks": [
            {
                "name": "integration tests",
                "status": "passed",
                "summary": "Client-readiness scenarios passed.",
                "reference": "tests/integration/client-readiness.test.ts",
            }
        ],
        "criteriaEvaluations": [
            {
                "criterionIndex": 0,
                "status": "passed",
                "summary": "The directory endpoint is implemented and verified.",
                "evidence": ["tests/integration/client-readiness.test.ts"],
            }
        ],
        "knownLimitations": ["Production identity mapping remains host-owned."],
        "questions": [],
    }


@pytest.mark.asyncio
async def test_completed_result_requires_a_real_change(tool):
    result = await tool.execute(
        outcome="completed",
        summary="Nothing changed.",
        changes=[],
        checks=[],
        criteria_evaluations=[],
        known_limitations=[],
        questions=[],
    )

    assert not result.success
    assert result.error == "Completed execution results require at least one change."


@pytest.mark.asyncio
async def test_needs_input_result_requires_a_question(tool):
    result = await tool.execute(
        outcome="needs_input",
        summary="A product decision is required.",
        changes=[],
        checks=[],
        criteria_evaluations=[],
        known_limitations=[],
        questions=[],
    )

    assert not result.success
    assert result.error == "needs_input execution results require at least one question."


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "skipped"])
async def test_completed_result_rejects_unpassed_checks(tool, status):
    result = await tool.execute(
        outcome="completed",
        summary="The implementation is not ready to submit.",
        changes=[
            {
                "kind": "code",
                "summary": "Changed the implementation.",
                "reference": "src/example.py",
            }
        ],
        checks=[
            {
                "name": "focused tests",
                "status": status,
                "summary": f"The check was {status}.",
            }
        ],
        criteria_evaluations=[
            {
                "criterion_index": 0,
                "status": "passed",
                "summary": "The criterion appears implemented.",
                "evidence": ["src/example.py"],
            }
        ],
        known_limitations=[],
        questions=[],
    )

    assert not result.success
    assert result.error == "Completed execution results require all checks to pass."


@pytest.mark.asyncio
async def test_completed_result_allows_advisory_check(tool):
    result = await tool.execute(
        outcome="completed",
        summary="The editable artifact is ready with a non-blocking image notice.",
        changes=[
            {"kind": "document", "summary": "Generated the editable deck.", "reference": "index.html"}
        ],
        checks=[
            {
                "name": "optional image sync",
                "status": "skipped",
                "summary": "An optional visual was deferred.",
                "advisory": True,
            },
            {
                "name": "render",
                "status": "passed",
                "summary": "The deck renders successfully.",
            },
        ],
        criteria_evaluations=[
            {
                "criterion_index": 0,
                "status": "passed",
                "summary": "The requested editable deck exists.",
                "evidence": ["index.html"],
            }
        ],
        known_limitations=["One optional image was deferred."],
        questions=[],
    )

    assert result.success
    assert result.raw_output["checks"][0]["advisory"] is True


@pytest.mark.asyncio
async def test_completed_result_rejects_failed_acceptance_criterion(tool):
    result = await tool.execute(
        outcome="completed",
        summary="The implementation still misses one criterion.",
        changes=[
            {
                "kind": "code",
                "summary": "Changed the implementation.",
                "reference": "src/example.py",
            }
        ],
        checks=[
            {
                "name": "focused tests",
                "status": "passed",
                "summary": "The focused tests passed.",
            }
        ],
        criteria_evaluations=[
            {
                "criterion_index": 0,
                "status": "failed",
                "summary": "The response is not backward compatible.",
                "evidence": ["tests/example_test.py"],
            }
        ],
        known_limitations=[],
        questions=[],
    )

    assert not result.success
    assert result.error == "Completed execution results cannot contain failed criteria."


@pytest.mark.asyncio
async def test_blocked_result_allows_failed_acceptance_criterion(tool):
    result = await tool.execute(
        outcome="blocked",
        summary="The required upstream API is unavailable.",
        changes=[],
        checks=[],
        criteria_evaluations=[
            {
                "criterion_index": 0,
                "status": "failed",
                "summary": "The integration cannot be verified while the API is down.",
                "evidence": ["curl: connection refused"],
            }
        ],
        known_limitations=["The upstream API must be restored before retrying."],
        questions=[],
    )

    assert result.success
    assert result.raw_output["outcome"] == "blocked"
    assert result.raw_output["criteriaEvaluations"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_result_rejects_unknown_criterion_status(tool):
    result = await tool.execute(
        outcome="blocked",
        summary="The execution is blocked.",
        changes=[],
        checks=[],
        criteria_evaluations=[
            {
                "criterion_index": 0,
                "status": "probably",
                "summary": "The criterion state is not valid.",
                "evidence": ["invalid status fixture"],
            }
        ],
        known_limitations=[],
        questions=[],
    )

    assert not result.success
    assert result.error == "Execution result contains an invalid criterion evaluation."


@pytest.mark.asyncio
async def test_result_rejects_duplicate_criterion_indices(tool):
    criterion = {
        "criterion_index": 0,
        "status": "passed",
        "summary": "The criterion is verified.",
        "evidence": ["tests/example_test.py"],
    }
    result = await tool.execute(
        outcome="completed",
        summary="The same criterion was reported twice.",
        changes=[
            {
                "kind": "code",
                "summary": "Changed the implementation.",
                "reference": "src/example.py",
            }
        ],
        checks=[
            {
                "name": "focused tests",
                "status": "passed",
                "summary": "The focused tests passed.",
            }
        ],
        criteria_evaluations=[criterion, criterion],
        known_limitations=[],
        questions=[],
    )

    assert not result.success
    assert result.error == "Criterion indices must be unique."


def test_schema_is_host_neutral(tool):
    schema = tool.to_openai_schema()["function"]

    assert schema["name"] == "report_execution_result"
    assert "organization" not in str(schema).lower()
    assert "jwt" not in str(schema).lower()
    assert "service url" not in str(schema).lower()
    assert schema["parameters"]["additionalProperties"] is False
