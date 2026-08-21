from pathlib import Path

import pytest

from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import (
    build_auto_loaded_skills_prompt,
    has_roadmap_artifact_intent,
    roadmap_artifact_intent_signals,
    semantic_artifact_preload_skill_names,
    turn_preload_skill_names,
)


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "box_agent" / "skills"


def _loader() -> SkillLoader:
    loader = SkillLoader(sources=[(SKILLS_ROOT, "builtin")])
    loader.discover_skills()
    return loader


@pytest.mark.parametrize(
    "query",
    [
        "帮我做未来三个月的产品路线图，按团队分泳道",
        "生成项目排期泳道图，按月份刻度展示",
        "create a roadmap schedule with swimlanes for each team",
        "I want a 6-month roadmap schedule with milestones and swimlanes",
        "做三个月排期泳道图并标出里程碑",
    ],
)
def test_ordinary_chat_routes_and_preloads_roadmap_skill(query: str) -> None:
    loader = _loader()
    matched_names = tuple(skill.name for skill in loader.filter_by_query(query))

    assert "roadmap" in matched_names
    assert has_roadmap_artifact_intent(query) is True
    preload = semantic_artifact_preload_skill_names(
        matched_names,
        query,
    )
    assert preload == ["roadmap"]

    rendered = build_auto_loaded_skills_prompt(loader, "base system", preload)
    assert rendered.loaded_names == ("roadmap",)
    assert "# Roadmap Skill" in rendered.system_prompt


@pytest.mark.parametrize(
    "query",
    [
        "制作三阶段工作流程",
        "帮我做一个单张甘特表",
        "帮我做一个三个月的单张排期甘特表，按月份刻度展示",
        "解释 roadmap 排期有什么区别",
        "帮我解释 roadmap 排期有什么区别",
        "我想了解未来三个月路线图按团队分泳道是什么意思",
        "不要生成三个月路线图泳道，只说明排期原则",
        "do not create a 6-month roadmap with swimlanes; explain the idea",
        "给我未来三个月项目计划的建议，按月分析",
        "帮我做 PPT",
    ],
)
def test_non_roadmap_artifact_requests_do_not_preload_roadmap(query: str) -> None:
    loader = _loader()
    matched_names = tuple(skill.name for skill in loader.filter_by_query(query))

    preload = semantic_artifact_preload_skill_names(
        matched_names,
        query,
    )
    assert "roadmap" not in preload


def test_roadmap_intent_reports_stable_ordered_signals() -> None:
    assert roadmap_artifact_intent_signals(
        "生成三个月路线图，按团队泳道和按月刻度做甘特排期并标记里程碑"
    ) == (
        "roadmap",
        "schedule",
        "swimlane",
        "calendar-scale",
        "gantt",
        "milestone",
    )


def test_negated_adapter_request_keeps_later_positive_roadmap_request() -> None:
    assert has_roadmap_artifact_intent(
        "不要生成 PPT，改为生成三个月路线图并按团队分泳道"
    ) is True


def test_turn_preload_uses_semantic_roadmap_route_without_a_document_gate() -> None:
    assert turn_preload_skill_names(
        ("roadmap",),
        None,
        None,
        "生成未来三个月路线图，按团队分泳道",
    ) == ["roadmap"]


def test_plain_ppt_query_does_not_match_roadmap_metadata() -> None:
    loader = _loader()

    matched_names = tuple(
        skill.name for skill in loader.filter_by_query("帮我做 PPT")
    )

    assert "roadmap" not in matched_names


def test_roadmap_is_a_top_level_builtin_skill() -> None:
    loader = _loader()
    skill = loader.get_skill("roadmap")

    assert skill is not None
    assert skill.skill_path == SKILLS_ROOT / "roadmap" / "SKILL.md"
    assert "document-skills" not in skill.skill_path.parts


def test_roadmap_skill_keeps_output_directory_deliverable_only() -> None:
    skill = _loader().get_skill("roadmap")

    assert skill is not None
    assert "Treat `output/` as a deliverables-only boundary" in skill.content
    assert "Never create generator scripts" in skill.content
    assert "unique task\ndirectory below `$BOX_AGENT_SCRATCH_DIR`" in skill.content
    assert "after either success or failure" in skill.content
    assert "session runtime clears any residue" in skill.content
    assert "under `output/` is a versioned HTML" in skill.content
