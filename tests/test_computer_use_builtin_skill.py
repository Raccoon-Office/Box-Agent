from __future__ import annotations

import json
from pathlib import Path

import pytest

from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import (
    build_auto_loaded_skills_prompt,
    turn_preload_skill_names,
)


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "box_agent" / "skills"


def test_computer_use_skill_is_packaged_as_builtin() -> None:
    manifest = json.loads((SKILLS_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    entries = {item["name"]: item for item in manifest["skills"]}

    assert entries["computer-use"] == {
        "name": "computer-use",
        "path": "computer-use/SKILL.md",
    }


@pytest.mark.parametrize(
    "query",
    [
        "打开系统计算器并计算结果",
        "打开飞书找到马林的会话",
        "control this native desktop app",
    ],
)
def test_computer_use_skill_matches_native_desktop_intent(query: str) -> None:
    loader = SkillLoader(sources=[(SKILLS_ROOT, "builtin")])
    loader.discover_skills()

    matched_names = tuple(skill.name for skill in loader.filter_by_query(query))
    preload_names = turn_preload_skill_names(matched_names, None, query)
    rendered = build_auto_loaded_skills_prompt(loader, "base system", preload_names)

    assert "computer-use" in matched_names
    assert rendered.loaded_names == ("computer-use",)
    assert "ensure_cua_ready()" in rendered.system_prompt
    assert 'server_name="cua-computer-use"' in rendered.system_prompt
    assert "Do not start another daemon or switch to standalone mode" in rendered.system_prompt
