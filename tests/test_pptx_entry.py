"""The public entry loads without choosing or enabling its internal backends."""

import json
from pathlib import Path
import re

import pytest

from box_agent.tools.local_tool_exposure import SKILL_TOOL_HINTS
from box_agent.tools.skill_catalog_tool import ListSkillsTool
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool
from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool


@pytest.fixture
def loader(tmp_path):
    source = Path(__file__).parents[1] / "box_agent" / "skills"
    loader = SkillLoader(sources=[(source, "builtin")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    return loader


@pytest.mark.asyncio
async def test_reading_public_entry_does_not_require_or_deliver_a_backend(loader):
    result = await GetSkillTool(loader).invoke({"skill_name": "pptx"})
    assert result.success, result.error
    assert "request_user_decision" in result.content
    assert "# PPT Fast Skill" not in result.content
    assert loader.get_skill("pptx").required_skills in (None, [])


@pytest.mark.asyncio
async def test_entry_choice_payload_offers_both_modes_with_a_30_second_default(loader):
    example = re.search(r"```json\s*(.*?)\s*```", loader.get_skill("pptx").content, re.S)
    arguments = json.loads(example.group(1))
    result = await RequestUserDecisionTool().invoke(arguments)
    assert result.success, result.error
    assert result.raw_output["options"] == arguments["options"]
    assert {option["id"]: option["label"] for option in result.raw_output["options"]} == {
        "fast": "快速模式", "design": "设计模式",
    }
    assert all(option["description"].strip() for option in result.raw_output["options"])
    assert result.raw_output["defaultOptionId"] in {"fast", "design"}
    assert result.raw_output["options"][0]["id"] == result.raw_output["defaultOptionId"]
    assert result.raw_output["autoSubmit"] == {
        "allowed": True,
        "requestedSeconds": 30,
        "effectiveSeconds": 30,
        "behavior": "submit_default",
    }
    assert result.raw_output["resumeBehavior"] == "continue_existing_task"
    assert RequestUserDecisionTool().ends_turn_on_success is True


@pytest.mark.asyncio
@pytest.mark.parametrize("recommended_mode", ["fast", "design"])
async def test_entry_timeout_preserves_either_model_recommended_mode(loader, recommended_mode):
    example = re.search(r"```json\s*(.*?)\s*```", loader.get_skill("pptx").content, re.S)
    arguments = json.loads(example.group(1))
    arguments["default_option_id"] = recommended_mode
    result = await RequestUserDecisionTool().invoke(arguments)

    assert result.success, result.error
    assert result.raw_output["defaultOptionId"] == recommended_mode
    assert result.raw_output["autoSubmit"]["allowed"] is True
    assert result.raw_output["autoSubmit"]["effectiveSeconds"] == 30
    assert result.raw_output["resumeBehavior"] == "continue_existing_task"


@pytest.mark.asyncio
async def test_one_public_presentation_entry_and_exact_hidden_backend_reads(loader):
    catalog = await ListSkillsTool(loader).execute(query="ppt", limit=50)
    assert catalog.success, catalog.error
    data = catalog.raw_output or json.loads(catalog.content)
    names = {skill["name"] for skill in data["skills"]}
    assert "pptx" in names
    assert not names.intersection({"ppt-fast", "ppt-router", "sn-ppt-entry", "sn-ppt-standard", "sn-ppt-dazzle"})
    for name in ("pptx", "ppt-fast", "sn-ppt-entry", "sn-ppt-standard", "sn-ppt-dazzle"):
        skill = loader.get_skill(name)
        assert skill is not None
        assert skill.allow_override is False
        assert skill.user_visible is (name == "pptx")
        result = await GetSkillTool(loader).invoke({"skill_name": name})
        assert result.success, result.error


@pytest.mark.asyncio
async def test_disabled_fast_backend_does_not_disable_the_entry_or_other_mode(loader, tmp_path):
    (tmp_path / "settings.json").write_text('{"disabledSkillNames":["ppt-fast"]}')
    loader.discover_skills()
    assert (await GetSkillTool(loader).invoke({"skill_name": "pptx"})).success
    assert (await GetSkillTool(loader).invoke({"skill_name": "sn-ppt-entry"})).success
    denied = await GetSkillTool(loader).invoke({"skill_name": "ppt-fast"})
    assert not denied.success
    assert "disabled" in denied.error


def test_fast_name_keeps_the_original_tool_exposure_and_trusted_resource_path(loader):
    fast = loader.get_skill("ppt-fast")
    assert fast.skill_path.parent.name == "pptx"  # Preserve exporter safety/path bindings.
    assert (fast.skill_path.parent / "scripts" / "html_to_editable_pptx.js").is_file()
    assert SKILL_TOOL_HINTS["ppt-fast"] == {"append_file", "query_jsonl", "report_execution_result"}
