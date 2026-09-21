"""Executable command and reference contracts for the static production flow."""

import os
from pathlib import Path
import re
import runpy

import pytest


STANDARD = Path(os.environ.get("PRESENTATION_STANDARD_SOURCE", Path(__file__).resolve().parents[1]
    / "box_agent/skills/presentation-suite/skills/sn-ppt-standard"))


def test_public_entry_uses_standard_review_preparation_before_final_pixels():
    entry = (Path(__file__).resolve().parents[1] / "box_agent/skills/pptx/SKILL.md").read_text()
    static_delivery = entry.split("设计模式的静态任务始终交付", 1)[1].split("恢复已有设计模式任务", 1)[0]
    assert "`deck.py review-prep`" in static_delivery
    assert "`deck.py build` 与 `deck.py audit`" not in static_delivery
    assert "最终全册像素" in static_delivery
    assert "不重复" in static_delivery and "验收" in static_delivery
    assert "present.html" in static_delivery and "HTML 链接" in static_delivery


def test_planning_quality_route_names_an_actual_unique_heading():
    plan = (STANDARD / "references/planning-contract.md").read_text()
    quality = (STANDARD / "references/quality-checklist.md").read_text()
    title = re.search(r"^- quality-checklist.md[:：]\s*(.+)$", plan, re.M).group(1)
    assert len(re.findall(r"^#{1,6} " + re.escape(title) + r"$", quality, re.M)) == 1


def test_subject_only_example_matches_existing_visual_field_parser():
    plan = (STANDARD / "references/planning-contract.md").read_text()
    visual = plan.split("## 视觉实现\n", 1)[1].split("## Reference route", 1)[0]
    assert re.search(r"^- subject_only[:：]", visual, re.M)


def test_formal_review_commands_prepare_full_deck_without_bypass():
    for relative in ("SKILL.md", "subagents/review.md", "references/box-agent-tool-contract.md"):
        text = (STANDARD / relative).read_text()
        commands = re.findall(r"^.*(?:python|node).*scripts/[^\n]+$", text, re.M)
        prep = [line for line in commands if "deck.py" in line and "review-prep" in line]
        assert prep, relative
        assert all("--expected" in line and "--pages" not in line for line in prep)
        assert all("--force" not in line for line in commands if "html_to_pptx" in line)
    review = (STANDARD / "subagents/review.md").read_text()
    assert "## Final review contract" in review
    assert "pending_parent_verification" in review


def test_box_delegation_uses_source_files_without_mandatory_input_packaging():
    text = (STANDARD / "references/box-agent-tool-contract.md").read_text()
    commands = re.findall(r"^.*python.*scripts/group_input\.py[^\n]*$", text, re.M)
    assert not commands, "New page groups must not require the formatting-sensitive packager"
    assert "files" in text and "write_scope" in text and "task_pack.deck_dir" in text
    assert "budget" in text and "省略" in text
    assert not re.search(r"max_(?:steps|tool_calls)[\"']?\s*[:=]\s*\d+", text)


@pytest.mark.parametrize("relative", [
    "SKILL.md",
    "subagents/slide.md",
    "references/planning-contract.md",
    "references/quality-checklist.md",
])
def test_production_instructions_do_not_require_parent_approval_between_drafts(relative):
    text = (STANDARD / relative).read_text()
    # These clauses caused a completed child to wait for a parent that cannot
    # resume it. A child with its own renderer can still inspect pages serially.
    conflicting_clauses = (
        "当前页达到 ready 后才进入下一页",
        "当前页 ready 后才进入下一页",
        "最后一次修改尚未被父级用新像素验证时不得进入下一页",
        "当前页由父级判为 ready 后",
        "页组规模应允许同一个 Slide 按页序完成每页独立像素闭环",
    )
    assert not any(clause in text for clause in conflicting_clauses), relative


def test_planning_routes_do_not_require_markdown_identity_for_input_packaging():
    text = (STANDARD / "references/planning-contract.md").read_text()
    route = text.split("### Reference route 的定位要求", 1)[1]
    box_route, other_route = route.split("其他环境使用", 1)
    assert "不要求为输入包装逐字匹配 Markdown 标题" in box_route
    assert "与实际 Markdown 标题逐字匹配" in other_route
    # These real design/export inputs remain required; removing the packager
    # must not remove their existing machine-readable contract.
    for field in ("image_opportunity", "presentation", "subject_only", "asset_id"):
        assert field in text


def test_box_integration_does_not_leave_conflicting_image_batch_limits():
    data = (STANDARD / "references/box-agent-tool-contract.md").read_bytes()
    if "PRESENTATION_STANDARD_SOURCE" in os.environ:
        repo = Path(__file__).resolve().parents[1]
        sync = runpy.run_path(str(repo / "scripts/sync_presentation_suite.py"))
        relative = "skills/sn-ppt-standard/references/box-agent-tool-contract.md"
        data = sync["_apply_integration_overlay"](relative, data)
    bundled = data.decode("utf-8")
    assert "每批最多 4 张图" in bundled
    assert "每次最多 6 张" not in bundled


def test_non_box_input_packaging_keeps_its_existing_command():
    text = (STANDARD / "SKILL.md").read_text()
    production = text.split("### 阶段 4：素材与页面制作", 1)[1].split("### 阶段 5", 1)[0]
    non_box = next(line for line in production.splitlines() if line.startswith("**其他环境的输入准备：**"))
    assert 'scripts/group_input.py" "$DECK_DIR" --expected N' in non_box
