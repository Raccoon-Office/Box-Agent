from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.plugins.repl.documentation import manual
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import (
    build_auto_loaded_skills_prompt,
    host_runtime_preload_skill_names,
)


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "box_agent" / "skills"


def test_browser_guides_scope_click_options_and_redirect_recovery_to_their_modes():
    skill = (SKILLS_ROOT / "browser-use/SKILL.md").read_text()
    managed = manual("browser", "managed")
    user = manual("browser", "user")
    for guide in (skill, managed, user):
        assert "Web Access Routing" in guide
        assert "Never substitute another mode on error" not in guide
        assert "Do not fall back to another mode after a failure" not in guide
        assert "coordinates or click options" not in guide
    for guide in (skill, managed):
        assert "tab.click(ref, {button: 'right'})" in guide
    for guide in (skill, user):
        recovery = guide.split("url_mismatch", 1)[1].split("\n\n", 1)[0]
        assert "ask before reading" in recovery
        assert "browser.openAndRead(url)" in guide
        assert "URL" in guide and "title" in guide
    assert "without click options in user mode" in user
    assert "button: 'right'" not in user


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
    assert "browser_repl_js" in rendered.system_prompt
    assert "managed_browser_" not in rendered.system_prompt
    assert "user_browser_" not in rendered.system_prompt
    assert "managed browser automation" in rendered.system_prompt
    assert "visible real browser" in rendered.system_prompt
    assert 'mcp_config(action="set_browser_mode", mode="headed")' in rendered.system_prompt
    assert 'mcp_config(action="update", name="playwright"' not in rendered.system_prompt
    assert "does not modify global configuration or restart other sessions" in rendered.system_prompt
    assert 'mcp_config(action="inspect_browser")' in rendered.system_prompt
    assert "fixed headless by default" not in rendered.system_prompt
    assert "shared **Web Access Routing** policy" in rendered.system_prompt
    assert "Switch only as permitted by Web Access Routing" in rendered.system_prompt
    assert "use its presentation control and preserve the session" in rendered.system_prompt
    assert "There are exactly two browser modes" in rendered.system_prompt
    assert "Playwright MCP" in rendered.system_prompt
    assert "Recover from headless-only failures" in rendered.system_prompt
    assert "Switching an existing session resets tabs, cookies, login, forms" in rendered.system_prompt
    assert "No page state is automatically restored" in rendered.system_prompt
    assert "Retry the blocked step once in headed mode" not in rendered.system_prompt
    assert "Do not solve, outsource, or circumvent the verification" in rendered.system_prompt
    assert "submitting retain their confirmation requirements" in rendered.system_prompt
    assert "Obtain consent before using the real browser" not in rendered.system_prompt
    assert "Ask for affirmative consent" not in rendered.system_prompt


@pytest.mark.parametrize(
    ("query", "expected_instruction"),
    [
        (
            "用爬虫批量抓取这些公开网页",
            "shared **Web Access Routing** policy",
        ),
        (
            "填写这个表单，填好让我检查，最后我点击提交",
            "Stop before the final action whenever the user owns it, in either namespace",
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


def test_browser_guides_reobserve_in_a_separate_cell_after_a_stale_ref():
    skill = (SKILLS_ROOT / "browser-use/SKILL.md").read_text()
    for guide in (skill, manual("browser", "managed"), manual("browser", "user")):
        recovery = guide.split("StaleObservation", 1)[1].splitlines()[0].lower()
        assert "in a new cell run only `await tab.getstate()`" in recovery


def test_user_guides_bind_and_verify_the_opened_page_before_a_followup_click():
    skill = (SKILLS_ROOT / "browser-use/SKILL.md").read_text()
    for guide, marker in ((skill, "without assigning `tab`"),
                          (manual("browser", "user"), "does not assign `tab`")):
        continuation = guide.split(marker, 1)[1].split("\n\n", 1)[0]
        assert continuation.index("tab = await browser.getTab('current')") < continuation.index("URL and title")
        assert continuation.index("URL and title") < continuation.index("fresh ref")
        assert "otherwise stop and ask" in continuation


def test_managed_guides_hand_off_verification_without_replaying_the_action():
    skill = (SKILLS_ROOT / "browser-use/SKILL.md").read_text()
    for guide in (skill, manual("browser", "managed")):
        assert "wait for the user" in guide
        recovery = guide.split("Never replay the click, form submission or purchase", 1)[1]
        assert "fresh snapshot in the same browser" in recovery
