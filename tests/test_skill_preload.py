from types import SimpleNamespace

import pytest

from box_agent.config import ToolLimitsConfig
from box_agent.core import _maybe_summarize
from box_agent.schema import LLMResponse, Message
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import (
    ACTIVE_SKILLS_HEADING,
    AUTO_LOADED_SKILLS_HEADING,
    build_active_skills_prompt,
    build_auto_loaded_skills_prompt,
    has_browser_operation_intent,
    host_runtime_preload_skill_names,
    resolve_skill_preload_attributions,
    strip_active_skills,
    turn_preload_skill_names,
    web_search_total_limit_for_active_skills,
)


def test_deep_presentation_preloads_research_synthesis_with_pptx() -> None:
    assert turn_preload_skill_names(
        ("pptx", "research-synthesis"),
        None,
        "Create a researched presentation",
    ) == [
        "pptx",
        "research-synthesis",
    ]


def test_non_deep_presentation_keeps_the_normal_pptx_preload() -> None:
    assert turn_preload_skill_names(("pptx",), None, "Create a deck") == ["pptx"]


def test_semantic_presentation_preloads_pptx_when_keyword_filter_misses_it() -> None:
    assert turn_preload_skill_names(
        ("lark-noise",),
        None,
        "做一份 12 页新员工入职培训 PPT，1920×1080 可编辑",
    ) == ["pptx"]


def test_no_match_or_host_selection_does_not_preload_document_skill() -> None:
    assert turn_preload_skill_names((), None, "Create an HTML page") == []


def test_host_selected_skill_is_authoritative_over_semantic_preloads() -> None:
    assert turn_preload_skill_names(
        ("pptx",),
        None,
        "Create a presentation",
        selected_skill_names=("custom-decks",),
    ) == ["custom-decks"]


def test_explicit_skill_selection_preloads_exact_skill() -> None:
    assert turn_preload_skill_names(
        ("sn-ppt-entry",),
        None,
        "/sn-ppt-entry topic",
        selected_skill_names=("sn-ppt-entry",),
    ) == [
        "sn-ppt-entry"
    ]


def test_research_synthesis_expands_web_search_budget() -> None:
    assert web_search_total_limit_for_active_skills(
        ("research-synthesis",),
    ) == 150
    assert web_search_total_limit_for_active_skills(
        (),
        ("research-synthesis",),
    ) == 150
    assert web_search_total_limit_for_active_skills(
        ("research-synthesis",),
        tool_limits=ToolLimitsConfig(
            web_search={"deep_research_total_calls": 48}
        ),
    ) == 48
    assert web_search_total_limit_for_active_skills(("pptx",)) is None
    assert web_search_total_limit_for_active_skills(
        ("research-synthesis",),
        execution_profile="fast",
    ) is None
    assert web_search_total_limit_for_active_skills(
        (),
        ("research-synthesis",),
        execution_profile="fast",
    ) == 150


class SummaryLLM:
    async def generate(self, messages, tools=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="Execution summary", finish_reason="stop")


@pytest.fixture
def available_hyperframes_env():
    return SimpleNamespace(hyperframes=SimpleNamespace(available=True))


@pytest.fixture
def browser_runtime_env():
    return SimpleNamespace(
        browser_tools=SimpleNamespace(available=True),
        browser_connector=SimpleNamespace(available=True),
    )


@pytest.mark.parametrize(
    "user_text",
    [
        "用真实浏览器打开百度",
        "读取当前标签页并帮我翻页",
        "后台抓取这个网页",
        "用爬虫批量抓取这些公开网页",
        "填写这个表单，填好让我检查，最后我点击提交",
        "请使用无头浏览器测试 https://example.com",
        "use my current Chrome login to open the website",
    ],
)
def test_browser_use_preloads_for_browser_operations(
    browser_runtime_env,
    user_text: str,
) -> None:
    assert has_browser_operation_intent(user_text) is True
    assert host_runtime_preload_skill_names(
        ("browser-use",),
        browser_runtime_env,
        user_text,
    ) == ["browser-use"]


@pytest.mark.parametrize(
    "user_text",
    [
        "打开这个本地 Word 文件",
        "帮我总结这段文字",
        "生成一张海报",
    ],
)
def test_browser_use_does_not_preload_for_unrelated_operations(
    browser_runtime_env,
    user_text: str,
) -> None:
    assert has_browser_operation_intent(user_text) is False
    assert host_runtime_preload_skill_names(
        ("browser-use",),
        browser_runtime_env,
        user_text,
    ) == []


def test_browser_use_preloads_without_host_state_so_it_can_explain_availability() -> None:
    assert host_runtime_preload_skill_names(
        ("browser-use",),
        SimpleNamespace(),
        "用浏览器打开网页",
    ) == ["browser-use"]


@pytest.mark.parametrize(
    "user_text",
    [
        "帮我做一个 8 秒 MP4 视频",
        "把这个网页转成视频",
        "render a motion graphic",
        "导出 GIF 动图",
        "use HyperFrames to make a title animation",
    ],
)
def test_hyperframes_preload_accepts_explicit_video_deliverables(
    available_hyperframes_env,
    user_text: str,
) -> None:
    assert host_runtime_preload_skill_names(
        ("hyperframes-video",),
        available_hyperframes_env,
        user_text,
    ) == ["hyperframes-video"]


@pytest.mark.parametrize(
    "user_text",
    [
        "生成一张海报",
        "生成一个 PPT",
        "开发 HTML 抽奖工具，加入动画效果并支持导出结果",
        "render this chart",
        "hyperframes-video 触发词是什么",
        "解释 MP4 和 GIF 的区别",
    ],
)
def test_hyperframes_preload_rejects_non_video_or_informational_requests(
    available_hyperframes_env,
    user_text: str,
) -> None:
    assert (
        host_runtime_preload_skill_names(
            ("hyperframes-video",),
            available_hyperframes_env,
            user_text,
        )
        == []
    )


def test_active_skills_render_once_at_system_prompt_tail() -> None:
    skill_prompt = "# Skill: pptx\n\nFollow the PPT workflow."

    rendered = build_active_skills_prompt("base system", {"pptx": skill_prompt})
    rendered_again = build_active_skills_prompt(rendered, {"pptx": skill_prompt})

    assert rendered_again == rendered
    assert rendered.count(ACTIVE_SKILLS_HEADING) == 1
    assert rendered.endswith(skill_prompt)
    assert strip_active_skills(rendered) == "base system"


def test_active_skills_replace_changed_content_by_name() -> None:
    first = build_active_skills_prompt(
        "base system",
        {"pptx": "# Skill: pptx\n\nOld instructions."},
    )
    updated = build_active_skills_prompt(
        first,
        {"pptx": "# Skill: pptx\n\nNew instructions."},
    )

    assert "Old instructions." not in updated
    assert updated.endswith("New instructions.")


def test_auto_loaded_skills_stay_before_existing_active_skills(tmp_path) -> None:
    skill_dir = tmp_path / "pptx"
    skill_dir.mkdir()
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: pptx\ndescription: Build decks\n---\n\nAUTO_RULE",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)
    loader.discover_skills()
    active_prompts = {"manual": "# Skill: manual\n\nACTIVE_RULE"}
    active_system = build_active_skills_prompt("base system", active_prompts)

    auto_result = build_auto_loaded_skills_prompt(
        loader,
        active_system,
        ["pptx"],
    )
    final_prompt = build_active_skills_prompt(
        auto_result.system_prompt,
        active_prompts,
    )

    assert final_prompt.index(AUTO_LOADED_SKILLS_HEADING) < final_prompt.index(
        ACTIVE_SKILLS_HEADING
    )
    assert "AUTO_RULE" in final_prompt
    assert final_prompt.endswith("ACTIVE_RULE")


def test_empty_turn_preload_removes_previous_auto_loaded_block(tmp_path) -> None:
    skill_dir = tmp_path / "pptx"
    skill_dir.mkdir()
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: pptx\ndescription: Build decks\n---\n\nPPT_RULE",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)
    loader.discover_skills()
    loaded = build_auto_loaded_skills_prompt(loader, "base system", ["pptx"])

    unloaded = build_auto_loaded_skills_prompt(loader, loaded.system_prompt, [])

    assert unloaded.loaded_names == ()
    assert unloaded.changed is True
    assert unloaded.system_prompt == "base system"
    assert AUTO_LOADED_SKILLS_HEADING not in unloaded.system_prompt
    assert "PPT_RULE" not in unloaded.system_prompt


def test_required_skill_is_attributed_as_non_billable_dependency(tmp_path) -> None:
    pptx_dir = tmp_path / "pptx"
    pptx_dir.mkdir()
    pptx_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Build decks\n"
        "required_skills: [html-templates]\n"
        "---\n\nPPT_RULE",
        encoding="utf-8",
    )
    html_dir = tmp_path / "html-templates"
    html_dir.mkdir()
    html_dir.joinpath("SKILL.md").write_text(
        "---\nname: html-templates\ndescription: Choose a visual style\n---\n\nHTML_RULE",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)
    loader.discover_skills()

    attributions = resolve_skill_preload_attributions(loader, ["pptx"])

    assert [
        (item.skill_name, item.usage_role, item.dependency_of)
        for item in attributions
    ] == [
        ("pptx", "primary", None),
        ("html-templates", "dependency", "pptx"),
    ]


def test_explicit_skill_stays_primary_when_another_skill_requires_it(tmp_path) -> None:
    pptx_dir = tmp_path / "pptx"
    pptx_dir.mkdir()
    pptx_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Build decks\n"
        "required_skills: [html-templates]\n"
        "---\n\nPPT_RULE",
        encoding="utf-8",
    )
    html_dir = tmp_path / "html-templates"
    html_dir.mkdir()
    html_dir.joinpath("SKILL.md").write_text(
        "---\nname: html-templates\ndescription: Choose a visual style\n---\n\nHTML_RULE",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)
    loader.discover_skills()

    attributions = resolve_skill_preload_attributions(
        loader,
        ["pptx", "html-templates"],
    )

    assert [(item.skill_name, item.usage_role) for item in attributions] == [
        ("pptx", "primary"),
        ("html-templates", "primary"),
    ]


@pytest.mark.asyncio
async def test_layer_two_summary_preserves_active_skills_in_system_prompt() -> None:
    active_system = build_active_skills_prompt(
        "base system",
        {"pptx": "# Skill: pptx\n\nMANDATORY_SKILL_RULE"},
    )
    messages = [
        Message(role="system", content=active_system),
        Message(role="user", content="build a deck"),
        Message(role="assistant", content="working"),
        Message(
            role="tool",
            name="bash",
            tool_call_id="tool-1",
            content="x" * 500,
        ),
    ]

    summarized, _, _ = await _maybe_summarize(
        SummaryLLM(),
        messages,
        token_limit=1,
        api_total_tokens=0,
        skip_check=False,
    )

    assert summarized is not None
    assert "MANDATORY_SKILL_RULE" in summarized[0].content
    assert summarized[0].content.endswith("MANDATORY_SKILL_RULE")
