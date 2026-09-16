"""Public Skill recall is a candidate hint, never authorization to route a task."""

from pathlib import Path

import pytest

from box_agent.tools.skill_loader import SkillLoader, SkillSelector


SKILLS = Path(__file__).resolve().parents[1] / "box_agent/skills"


@pytest.fixture
def public_entries(tmp_path):
    loader = SkillLoader(
        sources=[(SKILLS / name, "builtin") for name in ("pptx", "data-dashboard")],
        skill_settings_path=tmp_path / "skill-settings.json",
    )
    loader.discover_skills()
    return loader


@pytest.mark.parametrize("query", [
    "课件",
    "课堂演示",
    "给一年级讲四季的教学课件",
    "用户问题：\n想做个太阳系课件，行星能转起来。",
    "用户问题：\n想做个太阳系动态课件，行星能转起来。",
])
def test_courseware_enters_first_turn_catalog_without_dashboard(public_entries, query):
    selector = SkillSelector(public_entries)
    selector.bind(SkillSelector.SLOT)

    prompt = selector.update(query)

    assert "pptx" in selector.matched_skill_names
    assert "data-dashboard" not in selector.matched_skill_names
    assert '"name": "pptx"' in prompt


@pytest.mark.parametrize("query", [
    "用户问题：\n你好",
    "我是小学老师，太阳系有哪些行星？",
    "行星能转起来",
    "做个独立网页模拟器",
    "把按钮变蓝",
])
def test_wrapper_topic_or_local_change_alone_does_not_recall_these_entries(public_entries, query):
    assert public_entries.filter_by_query(query) == []


def test_data_analysis_dashboard_still_has_its_entry(public_entries):
    matches = public_entries.filter_by_query("把销售数据和分析结果做成数据看板")
    assert matches[0].name == "data-dashboard"


def test_candidate_recall_does_not_decide_negation_or_dynamic_mode(public_entries):
    # The full request must still be interpreted: lexical discovery is deliberately
    # not a semantic router, even when the named artifact is explicitly rejected.
    query = "不要做课件，只要独立网页模拟器，行星能转起来"
    assert "pptx" in {skill.name for skill in public_entries.filter_by_query(query)}
