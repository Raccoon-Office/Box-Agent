import pytest

from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool


OPTIONS = [
    {
        "id": "keep_full",
        "label": "保持完整版本",
        "description": "继续原定全部页面",
    },
    {
        "id": "prototype",
        "label": "先做精简版本",
        "description": "先交付四页原型",
    },
]


def test_request_user_decision_contract_prefers_safe_progress():
    tool = RequestUserDecisionTool()

    assert tool.ends_turn_on_success is True
    assert "Prefer progress over waiting" in tool.description
    assert "safely continues the user's explicit request" in tool.description
    assert "do not call this tool" in tool.description
    assert "Request 30 seconds" in tool.parameters["properties"][
        "requested_auto_submit_seconds"
    ]["description"]
    assert tool.parameters["properties"]["requested_auto_submit_seconds"]["default"] == 30
    assert set(tool.parameters["required"]) == {"question", "decision_kind", "options"}


@pytest.mark.asyncio
async def test_manual_decision_waits_for_user_without_default_or_timeout():
    tool = RequestUserDecisionTool()
    arguments = dict(question="请选择制作模式。", decision_kind="presentation_mode", options=OPTIONS)
    result = await tool.invoke(arguments)

    assert result.success is True
    assert "defaultOptionId" not in result.raw_output
    assert result.raw_output["autoSubmit"] == {
        "allowed": False,
        "requestedSeconds": None,
        "denialReason": "not_requested",
    }
    assert result.raw_output["resumeBehavior"] == "continue_existing_task"
    assert tool.ends_turn_on_success


@pytest.mark.asyncio
async def test_request_user_decision_returns_public_structured_payload():
    result = await RequestUserDecisionTool().execute(
        question="请选择交付范围。",
        decision_kind="delivery_scope",
        options=OPTIONS,
        default_option_id="keep_full",
        requested_auto_submit_seconds=30,
        risk_level="low",
        reversible=True,
        preserves_user_intent=True,
        reason="两个选项会改变最终交付范围。",
    )

    assert result.success is True
    assert result.raw_output == {
        "type": "user_decision_request",
        "schemaVersion": 1,
        "requestId": result.raw_output["requestId"],
        "status": "waiting",
        "question": "请选择交付范围。",
        "decisionKind": "delivery_scope",
        "options": OPTIONS,
        "defaultOptionId": "keep_full",
        "allowFreeform": False,
        "reason": "两个选项会改变最终交付范围。",
        "autoSubmit": {
            "allowed": True,
            "requestedSeconds": 30,
            "effectiveSeconds": 30,
            "behavior": "submit_default",
        },
        "resumeBehavior": "continue_existing_task",
    }
    assert result.raw_output["requestId"].startswith("decision_")
    assert "without repeating" in (result.model_context or "")


@pytest.mark.asyncio
async def test_request_user_decision_downgrades_unsafe_timeout_to_manual():
    result = await RequestUserDecisionTool().execute(
        question="请选择是否发布。",
        decision_kind="publish",
        options=[
            {"id": "publish", "label": "立即发布"},
            {"id": "cancel", "label": "取消"},
        ],
        default_option_id="publish",
        requested_auto_submit_seconds=30,
        risk_level="low",
        reversible=True,
        preserves_user_intent=True,
    )

    assert result.success is True
    assert result.raw_output["autoSubmit"] == {
        "allowed": False,
        "requestedSeconds": 30,
        "denialReason": "sensitive_decision",
    }


@pytest.mark.asyncio
async def test_request_user_decision_requires_valid_unique_options_and_default():
    duplicate = await RequestUserDecisionTool().execute(
        question="请选择。",
        decision_kind="delivery_scope",
        options=[
            {"id": "same", "label": "A"},
            {"id": "same", "label": "B"},
        ],
    )
    missing_default = await RequestUserDecisionTool().execute(
        question="请选择。",
        decision_kind="delivery_scope",
        options=OPTIONS,
        default_option_id="missing",
    )

    assert duplicate.success is False
    assert "unique" in (duplicate.error or "")
    assert missing_default.success is False
    assert "default_option_id" in (missing_default.error or "")


@pytest.mark.asyncio
async def test_request_user_decision_never_auto_submits_missing_safety_declarations():
    result = await RequestUserDecisionTool().execute(
        question="请选择交付范围。",
        decision_kind="delivery_scope",
        options=OPTIONS,
        default_option_id="keep_full",
        requested_auto_submit_seconds=30,
    )

    assert result.success is False
    assert "risk_level is required" in (result.error or "")


@pytest.mark.asyncio
async def test_request_user_decision_rejects_out_of_range_timeout_at_runtime():
    result = await RequestUserDecisionTool().execute(
        question="请选择交付范围。",
        decision_kind="delivery_scope",
        options=OPTIONS,
        default_option_id="keep_full",
        requested_auto_submit_seconds=999,
        risk_level="low",
        reversible=True,
        preserves_user_intent=True,
    )

    assert result.success is False
    assert "integer from 10 to 120" in (result.error or "")


@pytest.mark.asyncio
async def test_timeout_requires_default_but_no_timeout_remains_manual():
    missing_default = await RequestUserDecisionTool().execute(
        question="请选择演示用途。",
        decision_kind="presentation_purpose",
        options=OPTIONS,
        requested_auto_submit_seconds=30,
        risk_level="low",
        reversible=True,
        preserves_user_intent=True,
    )
    missing_timeout = await RequestUserDecisionTool().execute(
        question="请选择演示用途。",
        decision_kind="presentation_purpose",
        options=OPTIONS,
        default_option_id="keep_full",
        risk_level="low",
        reversible=True,
        preserves_user_intent=True,
    )

    assert missing_default.success is False
    assert "default_option_id is required" in (missing_default.error or "")
    assert missing_timeout.success is True
    assert missing_timeout.raw_output["autoSubmit"]["allowed"] is False
