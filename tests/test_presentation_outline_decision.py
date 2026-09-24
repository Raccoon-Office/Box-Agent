"""Execute Entry's decision example with the shipped tool, without a live card."""

import json
import os
from pathlib import Path
import re
import runpy

import pytest

from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool


REPO = Path(__file__).resolve().parents[1]
ENTRY = Path(os.environ.get("PRESENTATION_ENTRY_SOURCE", REPO
    / "box_agent/skills/presentation-suite/skills/sn-ppt-entry"))


def entry_text():
    data = (ENTRY / "SKILL.md").read_bytes()
    if "PRESENTATION_ENTRY_SOURCE" in os.environ:
        sync = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
        data = sync["_apply_integration_overlay"]("skills/sn-ppt-entry/SKILL.md", data)
    return data.decode()


def decision_example():
    examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", entry_text(), re.S)]
    return next(item for item in examples if item.get("decision_kind") == "outline_confirmation")


@pytest.mark.asyncio
async def test_entry_example_produces_60_second_outline_card():
    result = await RequestUserDecisionTool().invoke(decision_example())
    assert result.success, result.error
    payload = result.raw_output
    assert payload["decisionKind"] == "outline_confirmation"
    assert payload["defaultOptionId"] == "approve"
    assert [item["id"] for item in payload["options"]] == ["approve", "revise"]
    assert payload["autoSubmit"]["allowed"] is True
    assert payload["autoSubmit"]["effectiveSeconds"] == 60
    assert payload["allowFreeform"] is True
    assert payload["resumeBehavior"] == "continue_existing_task"


@pytest.mark.asyncio
async def test_outline_card_can_still_wait_for_explicit_personal_approval():
    arguments = decision_example()
    for key in ("default_option_id", "requested_auto_submit_seconds", "risk_level",
                "reversible", "preserves_user_intent"):
        arguments.pop(key, None)
    result = await RequestUserDecisionTool().invoke(arguments)
    assert result.success, result.error
    assert result.raw_output["autoSubmit"]["allowed"] is False


def test_entry_overlay_preserves_outline_card_and_both_existing_outputs():
    text = entry_text()
    assert decision_example()["requested_auto_submit_seconds"] == 60
    assert "`static_html` 调用 `sn-ppt-standard`" in text
    assert "`dynamic_html` 调用 `sn-ppt-dazzle`" in text
    public = (REPO / "box_agent/skills/pptx/SKILL.md").read_text()
    examples = [json.loads(block) for block in re.findall(r"```json\s*\n(.*?)\n\s*```", public, re.S)]
    mode = next(item for item in examples if item.get("decision_kind") == "presentation_mode")
    assert mode["requested_auto_submit_seconds"] == 30
