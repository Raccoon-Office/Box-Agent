"""Test cases for the Scheduled Task tool (create_scheduled_task).

Covers the cross-repo contract with officev3:
- success → raw_output.kind == "officev3_schedule_draft" with a complete draft
- empty cron is defaulted, but illegal cron / once-missing-fire_at fail loudly
  (no silent coercion — see plan feedback ④)
- the skill ships in the builtin manifest so SkillLoader can discover it (feedback ①)
"""

from pathlib import Path

import pytest

from box_agent.tools.schedule_tool import (
    DEFAULT_CRON_EXPR,
    SCHEDULE_DRAFT_KIND,
    CreateScheduledTaskTool,
)
from box_agent.tools.skill_loader import SkillLoader


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture
def tool():
    return CreateScheduledTaskTool()


# ── Metadata ────────────────────────────────────────────────


def test_metadata(tool):
    assert tool.name == "create_scheduled_task"
    assert tool.aliases == ()
    assert tool.parallel_safe is False
    schema = tool.parameters
    assert schema["required"] == ["name", "prompt"]
    # description must carry the hard precondition (model's only stable constraint).
    assert "三要素" in tool.description


# ── Success cases ───────────────────────────────────────────


async def test_cron_ok(tool):
    result = await tool.execute(
        name="世界杯每日战报",
        prompt="汇总当日世界杯赛果与次日赛程，输出 Markdown 简报。",
        cron_expr="0 9 * * *",
    )
    assert result.success
    assert result.raw_output["kind"] == SCHEDULE_DRAFT_KIND
    draft = result.raw_output["draft"]
    assert draft["name"] == "世界杯每日战报"
    assert draft["prompt"]
    assert draft["trigger_type"] == "cron"
    assert draft["cron_expr"] == "0 9 * * *"
    assert draft["fire_at"] is None


async def test_empty_cron_defaults(tool):
    # Empty value is the ONLY case we silently default.
    result = await tool.execute(name="t", prompt="p", cron_expr="")
    assert result.success
    assert result.raw_output["draft"]["cron_expr"] == DEFAULT_CRON_EXPR


async def test_cron_ranges_and_steps_ok(tool):
    for expr in ("0 10 * * 1", "0 16 * * 1-5", "0 9-18/2 * * *", "0 0 1,15 * *"):
        result = await tool.execute(name="t", prompt="p", cron_expr=expr)
        assert result.success, expr
        assert result.raw_output["draft"]["cron_expr"] == expr


@pytest.mark.parametrize("expr", [
    "* * * * *", "*/10 * * * *", "0,30 * * * *",
    "0-59 9 * * *", "*/15 9-18 * * 1-5", "0,59 9,10 * * *",
    "0/1 * * * *", "5/10 * * * *", "58/1 * * * *", "5/10,55 * * * *",
])
async def test_subhourly_schedules_fail_without_draft(tool, expr):
    result = await tool.execute(name="t", prompt="p", cron_expr=expr)
    assert not result.success
    assert "60 分钟" in result.error
    assert result.raw_output is None


@pytest.mark.parametrize("expr", [
    "0 * * * *", "17 * * * *", "59 23,0 * * *",
    "0,0 * * * *", "17-17 * * * *", "*/60 * * * *",
    "17/60 * * * *", "58/2 * * * *", "59/1 * * * *",
])
async def test_hourly_or_slower_schedules_preserve_expression(tool, expr):
    result = await tool.execute(name="t", prompt="p", cron_expr=expr)
    assert result.success
    assert result.raw_output["draft"]["cron_expr"] == expr


async def test_once_ok(tool):
    result = await tool.execute(
        name="一次性提醒",
        prompt="提醒我开会",
        trigger_type="once",
        fire_at="2026-06-21T09:00:00",
    )
    assert result.success
    draft = result.raw_output["draft"]
    assert draft["trigger_type"] == "once"
    assert draft["fire_at"] == "2026-06-21T09:00:00"
    assert draft["cron_expr"] is None


async def test_once_z_suffix_ok(tool):
    result = await tool.execute(
        name="t", prompt="p", trigger_type="once", fire_at="2026-06-21T01:00:00Z"
    )
    assert result.success


# ── Failure cases (no silent correction) ────────────────────


async def test_blank_name_or_prompt_fail(tool):
    assert not (await tool.execute(name="  ", prompt="p")).success
    assert not (await tool.execute(name="t", prompt="  ")).success


async def test_illegal_cron_segments_fail(tool):
    result = await tool.execute(name="t", prompt="p", cron_expr="0 9 *")
    assert not result.success
    assert result.error
    # must NOT silently fall back to the default
    assert result.raw_output is None


async def test_illegal_cron_range_fails(tool):
    # hour 99 is out of range → loud failure, not coercion to 0 9 * * *
    result = await tool.execute(name="t", prompt="p", cron_expr="0 99 * * *")
    assert not result.success
    assert result.error


async def test_once_missing_fire_at_fails(tool):
    result = await tool.execute(name="t", prompt="p", trigger_type="once")
    assert not result.success
    assert "fire_at" in result.error


async def test_once_bad_fire_at_fails(tool):
    result = await tool.execute(
        name="t", prompt="p", trigger_type="once", fire_at="not-a-date"
    )
    assert not result.success


async def test_invalid_trigger_type_fails(tool):
    result = await tool.execute(name="t", prompt="p", trigger_type="weekly")
    assert not result.success


# ── Skill discovery via the real builtin manifest (feedback ①) ──


def test_scheduled_task_skill_is_discoverable():
    skills_dir = Path(__file__).resolve().parent.parent / "box_agent" / "skills"
    loader = SkillLoader(sources=[(skills_dir, "builtin")])
    loader.discover_skills()
    skill = loader.get_skill("scheduled-task")
    assert skill is not None, "scheduled-task must be listed in _manifest.json"
    assert "scheduled-task" in loader.list_skills()
