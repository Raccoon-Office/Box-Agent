from __future__ import annotations

import asyncio
from typing import Any

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.schema import LLMResponse, StreamEvent
from box_agent.workflows.presentation_preflight import (
    classify_presentation_request,
    build_presentation_preflight_result,
    build_presentation_recommendation_prompt,
    infer_explicit_presentation_values,
    is_new_presentation_request,
    load_presentation_preflight_config,
    parse_presentation_recommendations,
)


class _FakeLLM:
    provider = "openai"
    model = "preflight-test"

    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages,
        tools=None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "session_id": session_id,
                "turn_id": turn_id,
                "title": title,
                "call_kind": call_kind,
            }
        )
        return LLMResponse(content=self.content, finish_reason="stop")

    async def generate_stream(
        self,
        messages,
        tools=None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "session_id": session_id,
                "turn_id": turn_id,
                "title": title,
                "call_kind": call_kind,
            }
        )
        yield StreamEvent(type="text", delta=self.content)
        yield StreamEvent(type="finish", finish_reason="stop")


class _FailingLLM(_FakeLLM):
    async def generate_stream(self, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": messages, "tools": tools, "call_kind": kwargs.get("call_kind", "")}
        )
        raise RuntimeError("provider unavailable")
        yield

    async def generate(
        self,
        messages,
        tools=None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "call_kind": call_kind})
        raise RuntimeError("provider unavailable")


class _SlowLLM(_FakeLLM):
    async def generate_stream(self, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": messages, "tools": tools, "call_kind": kwargs.get("call_kind", "")}
        )
        await asyncio.sleep(0.05)
        yield StreamEvent(type="text", delta=self.content)
        yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(
        self,
        messages,
        tools=None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "call_kind": call_kind})
        await asyncio.sleep(0.05)
        return LLMResponse(content=self.content, finish_reason="stop")


class _StubAgent:
    def __init__(self, llm: _FakeLLM):
        self._llm = llm
        self._lite_llm = llm

    extMethod = BoxACPAgent.extMethod
    _llm_prompt = BoxACPAgent._llm_prompt
    _presentation_preflight = BoxACPAgent._presentation_preflight
    _llm_for_binding = BoxACPAgent._llm_for_binding
    _utility_llm_for_meta = BoxACPAgent._utility_llm_for_meta


def test_skill_owned_preflight_config_is_valid():
    config = load_presentation_preflight_config()

    assert config["version"] == 1
    assert config["required_fields"] == ["role", "scene", "audience", "page_count"]
    assert config["high_impact_fields"] == ["scene", "audience"]
    assert config["defaults"]["page_count"] == "page_count_auto"
    page_count = next(
        field for field in config["fields"] if field["id"] == "page_count"
    )
    assert page_count["boundary_policy"] == "prefer_range_ending_at_count"
    assert page_count["options"][0]["id"] == "page_count_auto"
    assert {field["id"] for field in config["fields"]} == {
        "role",
        "scene",
        "audience",
        "page_count",
        "mode",
    }


@pytest.mark.parametrize(
    "text,has_existing",
    [
        ("分析一下这个 PPT 的结构", False),
        ("我要分析一下这个 PPT", False),
        ("需要修改现有 PPT", False),
        ("请给我把 HTML 导出成 PPTX", False),
        ("把这个 PPT 美化一下", True),
        ("将现有 HTML 导出成 PPTX", False),
        ("只需要一份 PPT 大纲，不用生成页面", False),
        ("生成一个普通 HTML 数据看板", False),
        ("帮我优化这个制作 PPT 的 prompt", False),
        ("把这个制作 PPT 的 prompt 改一下", False),
        ("把下面这段“生成 PPT”的提示词润色专业些", False),
        ("把“重新制作 PPT”这句提示词改自然", False),
        ("Polish this text: create a PPT and editable HTML", False),
        ("生成一份摘要，原文讨论了 PPT 的制作方法", False),
        ("请解释制作 PPT 时如何选择字体", False),
        ("总结这篇关于生成 PPT 的文章", False),
        ("把下面提到创建 PPT 的文字翻译成英文", False),
        ("继续分析 PPT skill 的加载机制", False),
        ("我不是要生成 PPT，只是想知道为什么会触发", False),
        ("生成 一份哈利波特主题介绍PPT 提示词", False),
        ("帮我写一个制作哈利波特介绍 PPT 的提示词", False),
        ("为哈利波特主题 PPT 生成提示词", False),
        ("Generate a prompt for a Harry Potter presentation", False),
        (
            "制作一份介绍四家酒庄的 PPT，使用公开资料。\n"
            "优化以上 prompt 的格式",
            False,
        ),
        (
            "请帮我处理细胞增殖实验数据，计算均值和误差并进行 ANOVA。"
            "最后生成可以直接放在这周组会 PPT 里汇报的带误差棒和显著性星号的"
            "漂亮柱状图和折线图，并附带 Excel 统计表。",
            False,
        ),
        ("生成一张用于 PPT 的配图", False),
        ("生成图表用于组会 PPT", False),
        ("Create presentation-ready charts and an Excel summary", False),
        ("Generate a chart for use in a presentation", False),
    ],
)
def test_non_new_deck_requests_skip_preflight(text: str, has_existing: bool):
    assert (
        is_new_presentation_request(
            text,
            has_existing_presentation=has_existing,
        )
        is False
    )


@pytest.mark.parametrize(
    "text",
    [
        "帮我制作一份季度工作汇报 PPT",
        "我要一份季度工作汇报 PPT",
        "需要一个产品发布 PPT",
        "请给我一份年度总结 PPT",
        "帮我出一份市场调研 PPT",
        "来一份新人培训 PPT",
        "出个产品介绍 PPT",
        "生成一个面向客户的 HTML 演示文稿",
        "参考附件重新做一版产品发布 presentation",
        "优化上面的提示词后，再按优化结果制作 PPT",
        "优化这个 prompt，并制作 PPT",
        "重新制作 PPT 并优化这个文字",
        "重新制作 PPT 并优化这个 prompt",
        "Remake the presentation and polish this text",
        "Remake the presentation and polish this prompt",
        "帮我制作一份 PPT，并优化每页文字",
        "根据以下提示词制作改革开放主题 PPT",
        "根据以下 prompt 制作 PPT，并优化布局",
        "根据以下提示词制作流程优化主题 PPT",
        "制作一个用于优化提示词的 PPT",
        "Use this prompt to create a PPT in PowerPoint format",
        "Use this prompt to create a PowerPoint and polish the layout",
        "Polish this prompt and create the presentation",
        "先生成一份哈利波特主题 PPT 提示词，然后根据它制作 PPT",
        "把分析结果整理成一份组会 PPT，并附 Excel 统计表",
        "把分析结果做成一份组会 PPT",
        "制作10页AI质检与智能排产平台融资BP，面向VC",
        "帮我生成一份商业计划书，包含市场和融资计划",
        "Create an investor pitch deck for this product",
    ],
)
def test_new_deck_requests_enter_preflight(text: str):
    assert is_new_presentation_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "生成一份摘要，原文讨论了 PPT 的制作方法",
        "请解释制作 PPT 时如何选择字体",
        "总结这篇关于生成 PPT 的文章",
        "把下面提到创建 PPT 的文字翻译成英文",
    ],
)
def test_referenced_creation_phrases_do_not_become_delivery_intent(text: str):
    assert classify_presentation_request(text) is None


def test_explicit_values_are_extracted_without_model_guessing():
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        "我是产品经理，做一份给投资人的 12 页商业提案 PPT，使用创意模式",
        config,
    )

    assert values == {
        "role": "role_product_manager",
        "scene": "scene_business_proposal",
        "audience": "audience_investors",
        "page_count": "page_count_10_15",
        "mode": "creative",
    }


def test_audience_management_does_not_infer_manager_role():
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        "给管理层做市场调研 PPT",
        config,
    )

    assert values == {
        "scene": "scene_market_research",
        "audience": "audience_internal",
    }


def test_topic_text_does_not_treat_manager_as_explicit_role():
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        "给经理做个汇报 PPT",
        config,
    )

    assert values == {}


def test_topic_text_does_not_treat_client_as_explicit_audience():
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        "客户案例 PPT",
        config,
    )

    assert values == {}


def test_contextual_role_alias_is_recognized():
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        "我是经理，帮我做个季度汇报 PPT",
        config,
    )

    assert values["role"] == "role_manager"


def test_contextual_audience_alias_is_recognized():
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        "做一份产品介绍 PPT，面向客户",
        config,
    )

    assert values == {
        "audience": "audience_external_clients",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("制作一份融资路演 PPT，面向一线 VC 投资人", "audience_investors"),
        (
            "制作一份读书分享 PPT，面向互联网产品和运营团队",
            "audience_internal",
        ),
    ],
)
def test_contextual_audience_alias_allows_bounded_modifiers(
    text: str,
    expected: str,
):
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(text, config)

    assert values["audience"] == expected


@pytest.mark.parametrize(
    "scene_text",
    ["读书分享", "读书会", "知识分享", "内部分享", "分享会"],
)
def test_knowledge_sharing_scene_alias_is_recognized(scene_text: str):
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        f"制作一份《纳瓦尔宝典》{scene_text} PPT",
        config,
    )

    assert values["scene"] == "scene_knowledge_sharing"


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (10, "page_count_5_10"),
        (15, "page_count_10_15"),
        (20, "page_count_15_20"),
        (21, "page_count_20_plus"),
    ],
)
def test_exact_page_count_uses_range_ending_at_boundary(
    count: int,
    expected: str,
):
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        f"制作一份 {count} 页 PPT",
        config,
    )

    assert values["page_count"] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("制作一份十页 PPT", "page_count_5_10"),
        ("制作一份十二页 PPT", "page_count_10_15"),
        ("制作一份二十页 PPT", "page_count_15_20"),
        ("制作一份二十五页 PPT", "page_count_20_plus"),
        ("制作一份10-15页 PPT", "page_count_10_15"),
        ("制作一份十到十五页 PPT", "page_count_10_15"),
        ("制作一份二十到二十五页 PPT", "page_count_20_plus"),
    ],
)
def test_chinese_and_range_page_count_extraction(
    text: str,
    expected: str,
):
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(text, config)

    assert values["page_count"] == expected


def test_positional_preview_span_does_not_override_total_page_count():
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        "先完成封面、目录和前 2 页内容；完整版 8 页为默认项。",
        config,
    )

    assert values["page_count"] == "page_count_5_10"


def test_positional_preview_span_alone_is_not_a_total_page_count():
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(
        "先完成封面、目录和前两页内容，再让我预览。",
        config,
    )

    assert "page_count" not in values


@pytest.mark.parametrize(
    "text",
    [
        "制作一份 PPT，页数根据内容安排",
        "Create a presentation with a content-driven slide count",
    ],
)
def test_content_driven_page_count_is_explicit(text: str):
    config = load_presentation_preflight_config()

    values = infer_explicit_presentation_values(text, config)

    assert values["page_count"] == "page_count_auto"


def test_model_recommendations_are_constrained_to_skill_options():
    config = load_presentation_preflight_config()

    values = parse_presentation_recommendations(
        """```json
        {
          "role": "role_researcher",
          "scene": "scene_training",
          "audience": "audience_students",
          "page_count": "not_allowed",
          "unknown": "x"
        }
        ```""",
        config,
    )

    assert values == {
        "role": "role_researcher",
        "scene": "scene_training",
        "audience": "audience_students",
    }


def test_missing_page_count_is_not_recommended_as_a_fixed_range():
    config = load_presentation_preflight_config()
    prompt = build_presentation_recommendation_prompt(
        "制作一份面向客户的产品介绍 PPT",
        config,
        ["role", "scene", "audience", "page_count"],
    )
    result = build_presentation_preflight_result(
        "制作一份面向客户的产品介绍 PPT",
        model_text=(
            '{"role":"role_marketing","scene":"scene_business_proposal",'
            '"audience":"audience_external_clients",'
            '"page_count":"page_count_5_10","mode":"normal"}'
        ),
    )

    assert '"page_count"' not in prompt
    assert result["values"]["page_count"] == "page_count_auto"
    assert result["sources"]["page_count"] == "default"


def test_result_preserves_explicit_values_and_recommends_missing_fields():
    result = build_presentation_preflight_result(
        "做一份面向投资人的 8 页 PPT",
        model_text=(
            '{"role":"role_manager","scene":"scene_business_proposal",'
            '"audience":"audience_internal","page_count":"page_count_1_4",'
            '"mode":"creative"}'
        ),
    )

    assert result["matched"] is True
    assert result["shouldShow"] is True
    assert result["missingFields"] == ["role", "scene"]
    assert result["values"] == {
        "role": "role_manager",
        "scene": "scene_business_proposal",
        "audience": "audience_investors",
        "page_count": "page_count_5_10",
        "mode": "creative",
    }
    assert result["sources"]["audience"] == "explicit"
    assert result["sources"]["scene"] == "recommended"
    assert result["autoStartSeconds"] == 30


def test_complete_explicit_config_skips_card():
    result = build_presentation_preflight_result(
        "我是市场人员，做一份 8 页的工作汇报 PPT，给公司内部看",
    )

    assert result["matched"] is True
    assert result["shouldShow"] is False
    assert result["missingFields"] == []


def test_missing_only_role_and_page_count_skips_card():
    result = build_presentation_preflight_result(
        "制作一份工作汇报 PPT，给公司内部看",
    )

    assert result["matched"] is True
    assert result["shouldShow"] is False
    assert result["missingFields"] == ["role", "page_count"]


def test_sparse_prompt_missing_scene_and_audience_shows_card():
    result = build_presentation_preflight_result(
        "帮我做个纳瓦尔宝典 PPT",
    )

    assert result["matched"] is True
    assert result["shouldShow"] is True
    assert result["missingFields"] == [
        "role",
        "scene",
        "audience",
        "page_count",
    ]


def test_sparse_prompt_missing_one_high_impact_field_shows_card():
    result = build_presentation_preflight_result(
        "给公司内部做个产品趋势 PPT",
    )

    assert result["matched"] is True
    assert result["shouldShow"] is True
    assert result["missingFields"] == ["role", "scene", "page_count"]


def test_rich_prompt_missing_one_high_impact_field_skips_card():
    result = build_presentation_preflight_result(
        "面向公司内部制作一份未来工作方式 PPT。"
        "内容要求：每页只讲一个核心观点，避免堆砌文字；"
        "整体感觉要有思考、有态度，关键关系请用一张图表达，"
        "并统一字号、配色和版式。",
    )

    assert result["matched"] is True
    assert result["shouldShow"] is False
    assert result["missingFields"] == ["role", "scene", "page_count"]


def test_naval_rich_prompt_skips_card_with_safe_soft_defaults():
    result = build_presentation_preflight_result(
        "使用pptx skill，制作一份创意的设计的《纳瓦尔宝典》读书分享 PPT，"
        "面向互联网产品和运营团队 内容要求：每页只讲一个核心观点，避免堆砌原文。"
        "每页最多引用一句原文，放在“金句卡片”中。"
        "观点要用我自己的语言提炼成一句有洞察的话，不要写成"
        "“作者认为……所以……”这种转述。整体感觉要适合同事交流，"
        "不像课堂读书报告，要有思考、有态度。"
        "“全书地图”请用一张图表达五个主题之间的关系，"
        "可以是思维导图、关系图或五边形结构。“金句摘录”页可以放大字号",
    )

    assert result["matched"] is True
    assert result["shouldShow"] is False
    assert result["missingFields"] == ["role", "page_count"]
    assert result["values"]["scene"] == "scene_knowledge_sharing"
    assert result["values"]["audience"] == "audience_internal"
    assert result["values"]["mode"] == "creative"


def test_ai_quality_pitch_prompt_is_complete_enough_to_skip_card():
    result = build_presentation_preflight_result(
        "请为以下项目制作一份 10 页融资路演 PPT，面向一线 VC 投资人。"
        "项目是一家 AI 科技公司，产品为面向中小制造工厂的 AI 质检 + 智能排产平台。"
        "已有 30 家试点客户，当前年化收入 800 万元，本轮计划融资 3000 万元。"
        "每页只讲一个核心观点；市场规模展示市场规模图，竞争格局使用竞争矩阵，"
        "业务进展包含增长曲线，未提供数据使用合理假设并标注示意 / 假设。",
    )

    assert result["matched"] is True
    assert result["shouldShow"] is False
    assert result["missingFields"] == ["role"]
    assert result["values"]["scene"] == "scene_business_proposal"
    assert result["values"]["audience"] == "audience_investors"
    assert result["values"]["page_count"] == "page_count_5_10"


def test_referential_request_uses_rich_prior_prompt_to_skip_card():
    reference_context = (
        "助手：\n"
        "请制作一份 10 页的哈利波特主题介绍 PPT，面向中学生，用于课堂知识分享。"
        "内容要求：从魔法世界观、霍格沃茨学院、主要人物、核心故事线、主题价值五部分展开；"
        "每页只讲一个观点，结尾设计互动问答。"
        "视觉风格采用深蓝与金色，使用羊皮纸、魔杖、城堡剪影等元素，避免大段文字。"
    )

    result = build_presentation_preflight_result(
        "基于以上内容制作 PPT",
        reference_context=reference_context,
    )

    assert result["matched"] is True
    assert result["shouldShow"] is False
    assert result["values"]["scene"] == "scene_knowledge_sharing"
    assert result["values"]["audience"] == "audience_students"
    assert result["values"]["page_count"] == "page_count_5_10"


def test_non_referential_request_does_not_inherit_unrelated_rich_context():
    result = build_presentation_preflight_result(
        "制作一份全新的产品介绍 PPT",
        reference_context=(
            "上一轮是一份 10 页教学 PPT，面向学生，包含详细内容结构、视觉风格和互动问答。"
        ),
    )

    assert result["matched"] is True
    assert result["shouldShow"] is True


@pytest.mark.asyncio
async def test_acp_preflight_uses_main_model_without_session():
    llm = _FakeLLM(
        '{"role":"role_teacher","scene":"scene_training",'
        '"audience":"audience_students","page_count":"page_count_5_10","mode":"normal"}'
    )
    agent = _StubAgent(llm)

    result = await agent.extMethod(
        "presentation/preflight",
        {"prompt": "制作一个人工智能基础课程 PPT", "timeoutMs": 5000},
    )

    assert result["matched"] is True
    assert result["shouldShow"] is True
    assert result["values"]["role"] == "role_teacher"
    assert result["values"]["page_count"] == "page_count_auto"
    assert result["sources"]["page_count"] == "default"
    assert len(llm.calls) == 1
    assert llm.calls[0]["call_kind"] == "utility"
    assert not hasattr(agent, "_sessions")


@pytest.mark.asyncio
async def test_acp_preflight_skips_model_for_rich_referenced_context():
    llm = _FakeLLM('{"role":"role_teacher"}')
    agent = _StubAgent(llm)

    result = await agent.extMethod(
        "presentation/preflight",
        {
            "prompt": "基于以上内容制作 PPT",
            "referenceContext": (
                "助手：请制作一份 10 页哈利波特主题介绍 PPT，面向中学生，用于课堂知识分享。"
                "内容要求包括世界观、学院、人物、故事线和主题价值，每页只讲一个观点。"
                "视觉风格使用深蓝和金色，并设计结尾互动问答。"
            ),
        },
    )

    assert result["matched"] is True
    assert result["shouldShow"] is False
    assert llm.calls == []


@pytest.mark.asyncio
async def test_acp_preflight_rejects_reference_without_calling_model():
    llm = _FakeLLM('{"scene":"scene_training"}')
    agent = _StubAgent(llm)

    result = await agent.extMethod(
        "presentation/preflight",
        {"prompt": "生成一份摘要，原文讨论了 PPT 的制作方法"},
    )

    assert result == {"matched": False, "shouldShow": False, "schemaVersion": 1}
    assert llm.calls == []


@pytest.mark.asyncio
async def test_acp_preflight_rejects_presentation_prompt_authoring_without_model():
    llm = _FakeLLM('{"scene":"scene_training"}')
    agent = _StubAgent(llm)

    result = await agent.extMethod(
        "presentation/preflight",
        {"prompt": "生成 一份哈利波特主题介绍PPT 提示词"},
    )

    assert result == {"matched": False, "shouldShow": False, "schemaVersion": 1}
    assert llm.calls == []


@pytest.mark.asyncio
async def test_acp_preflight_forwards_host_correlation_metadata():
    llm = _FakeLLM(
        '{"role":"role_teacher","scene":"scene_training",'
        '"audience":"audience_students"}'
    )
    agent = _StubAgent(llm)

    await agent.extMethod(
        "presentation/preflight",
        {
            "prompt": "制作一个人工智能基础课程 PPT",
            "_meta": {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "title": "人工智能基础课程",
            },
        },
    )

    assert llm.calls[0]["session_id"] == "session-1"
    assert llm.calls[0]["turn_id"] == "turn-1"
    assert llm.calls[0]["title"] == "人工智能基础课程"
    assert llm.calls[0]["call_kind"] == "utility"


@pytest.mark.asyncio
async def test_acp_preflight_skips_model_when_required_config_is_explicit():
    llm = _FakeLLM('{"role":"role_teacher"}')
    agent = _StubAgent(llm)

    result = await agent.extMethod(
        "presentation/preflight",
        {
            "prompt": (
                "我是市场人员，做一份 8 页的工作汇报 PPT，给公司内部看"
            )
        },
    )

    assert result["matched"] is True
    assert result["shouldShow"] is False
    assert result["missingFields"] == []
    assert llm.calls == []


@pytest.mark.asyncio
async def test_acp_preflight_skips_model_for_rich_prompt_that_does_not_need_card():
    llm = _FakeLLM('{"role":"role_teacher","page_count":"page_count_10_15"}')
    agent = _StubAgent(llm)

    result = await agent.extMethod(
        "presentation/preflight",
        {
            "prompt": (
                "制作一份创意设计的《纳瓦尔宝典》读书分享 PPT，面向互联网产品和运营团队。"
                "内容要求：每页只讲一个核心观点，避免堆砌原文；每页最多引用一句原文，"
                "放在金句卡片中。整体感觉适合同事交流，要有思考、有态度。"
                "全书地图请用一张图表达五个主题之间的关系。"
            )
        },
    )

    assert result["matched"] is True
    assert result["shouldShow"] is False
    assert result["missingFields"] == ["role", "page_count"]
    assert result["sources"]["role"] == "default"
    assert result["sources"]["page_count"] == "default"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_acp_preflight_falls_back_to_defaults_on_model_error():
    llm = _FailingLLM("{}")
    agent = _StubAgent(llm)

    result = await agent.extMethod(
        "presentation/preflight",
        {"prompt": "帮我出一份人工智能基础课程 PPT"},
    )

    assert result["matched"] is True
    assert result["shouldShow"] is True
    assert result["values"]["role"] == "role_marketing"
    assert result["sources"]["role"] == "default"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_acp_preflight_falls_back_to_defaults_on_model_timeout():
    llm = _SlowLLM('{"role":"role_teacher"}')
    agent = _StubAgent(llm)

    result = await agent.extMethod(
        "presentation/preflight",
        {
            "prompt": "需要一个人工智能基础课程 PPT",
            "timeoutMs": 1,
        },
    )

    assert result["matched"] is True
    assert result["shouldShow"] is True
    assert result["values"]["role"] == "role_marketing"
    assert result["sources"]["role"] == "default"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_acp_preflight_skips_model_for_non_deck_request():
    llm = _FakeLLM("{}")
    agent = _StubAgent(llm)

    result = await agent.extMethod(
        "presentation/preflight",
        {"prompt": "解释一下 PPT 和 HTML 的区别"},
    )

    assert result == {"matched": False, "shouldShow": False, "schemaVersion": 1}
    assert llm.calls == []
