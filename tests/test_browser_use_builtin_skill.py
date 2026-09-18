from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import (
    build_auto_loaded_skills_prompt,
    host_runtime_preload_skill_names,
)


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "box_agent" / "skills"


def test_browser_use_skill_is_packaged_as_builtin() -> None:
    manifest = json.loads((SKILLS_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    entries = {item["name"]: item for item in manifest["skills"]}

    assert entries["browser-use"] == {
        "name": "browser-use",
        "path": "browser-use/SKILL.md",
    }

    loader = SkillLoader(sources=[(SKILLS_ROOT, "builtin")])
    loader.discover_skills()
    skill = loader.get_skill("browser-use")

    assert skill is not None
    assert skill.source == "builtin"
    assert skill.skill_path == SKILLS_ROOT / "browser-use" / "SKILL.md"


@pytest.mark.parametrize(
    "query",
    [
        "用系统浏览器打开这个网址",
        "读取当前页面",
        "后台抓取这个网页",
        "用爬虫批量抓取这些公开网页",
        "填写这个表单，填好让我检查，最后我点击提交",
        "use my current browser login",
    ],
)
def test_browser_use_skill_matches_browser_intent(query: str) -> None:
    loader = SkillLoader(sources=[(SKILLS_ROOT, "builtin")])
    loader.discover_skills()

    matched_names = {
        skill.name for skill in loader.filter_by_query(query, max_skills=20)
    }

    assert "browser-use" in matched_names


def test_browser_intent_preloads_full_builtin_instructions() -> None:
    query = "用我当前登录的真实浏览器打开这个网站"
    loader = SkillLoader(sources=[(SKILLS_ROOT, "builtin")])
    loader.discover_skills()
    matched_names = tuple(skill.name for skill in loader.filter_by_query(query))

    preload_names = host_runtime_preload_skill_names(
        matched_names,
        SimpleNamespace(
            browser_tools=SimpleNamespace(available=True),
            browser_connector=SimpleNamespace(available=True),
        ),
        query,
    )
    rendered = build_auto_loaded_skills_prompt(
        loader,
        "base system",
        preload_names,
    )

    assert rendered.loaded_names == ("browser-use",)
    assert "managed browser automation" in rendered.system_prompt
    assert "visible real browser" in rendered.system_prompt
    assert 'mcp_config(action="set_browser_mode", mode="headed")' in rendered.system_prompt
    assert 'mcp_config(action="update", name="playwright"' not in rendered.system_prompt
    assert "does not modify global configuration or restart other sessions" in rendered.system_prompt
    assert 'mcp_config(action="inspect_browser")' in rendered.system_prompt
    assert "fixed headless by default" not in rendered.system_prompt
    assert "Treat headed/headless only as the managed browser's window visibility" in rendered.system_prompt
    assert "Do not treat a visible managed window as proof" in rendered.system_prompt
    assert "There are exactly two browser modes" in rendered.system_prompt
    assert "Playwright MCP" in rendered.system_prompt
    assert "start with managed browser automation when it can be completed independently" in rendered.system_prompt
    assert "Use the user's real browser directly when the task depends on the current page" in rendered.system_prompt
    assert "infer the mode from the most recent successful browser interaction" in rendered.system_prompt
    assert "Explicit mode selection in the latest user message overrides earlier context" in rendered.system_prompt
    assert "Background retrieval, scraping, screenshots and testing default to headless" in rendered.system_prompt
    assert "Recover from headless-only failures" in rendered.system_prompt
    assert "This rebuilds only the current session" in rendered.system_prompt
    assert "Login and form state from the previous instance are not restored" in rendered.system_prompt
    assert "Retry the blocked step once in headed mode" in rendered.system_prompt
    assert "Do not solve, outsource, or circumvent the verification" in rendered.system_prompt
    assert "Switch between browser modes" in rendered.system_prompt
    assert "does not require a separate authorization prompt" in rendered.system_prompt
    assert "Browser routing does not authorize external side effects" in rendered.system_prompt
    assert "Obtain consent before using the real browser" not in rendered.system_prompt
    assert "Ask for affirmative consent" not in rendered.system_prompt


@pytest.mark.parametrize(
    ("query", "expected_instruction"),
    [
        (
            "用爬虫批量抓取这些公开网页",
            "use managed browser automation unless the task also depends on user-owned browser state",
        ),
        (
            "填写这个表单，填好让我检查，最后我点击提交",
            "user review, takeover, or personal submission",
        ),
    ],
)
def test_high_priority_browser_scenarios_preload_routing_guidance(
    query: str,
    expected_instruction: str,
) -> None:
    loader = SkillLoader(sources=[(SKILLS_ROOT, "builtin")])
    loader.discover_skills()
    matched_names = tuple(skill.name for skill in loader.filter_by_query(query))

    preload_names = host_runtime_preload_skill_names(matched_names, SimpleNamespace(), query)
    rendered = build_auto_loaded_skills_prompt(loader, "base system", preload_names)

    assert rendered.loaded_names == ("browser-use",)
    assert expected_instruction in rendered.system_prompt
