"""Executable command and reference contracts for the static production flow."""

import os
from pathlib import Path
import re


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


def test_box_group_input_command_returns_files_and_scope_without_budget_numbers():
    text = (STANDARD / "references/box-agent-tool-contract.md").read_text()
    command = next((line for line in text.splitlines() if "python" in line and "scripts/group_input.py" in line), "")
    assert "--expected" in command and "--tool-contract" in command
    assert "inputs" in text and "outputs" in text and "write_scope" in text
    assert "budget" in text and "省略" in text
    assert not re.search(r"max_(?:steps|tool_calls)[\"']?\s*[:=]\s*\d+", text)
