"""Synthetic policy expectations; these do not measure model answer quality."""

import pytest

from box_agent.llm.model_routing import select_auto_model


MODELS = [
    {"model": "light", "abilityLevel": 1, "tags": ["fast", "chat", "general", "rewrite", "summary"],
     "contextWindow": 16_000, "maxTokens": 2_000},
    {"model": "office", "abilityLevel": 2, "tags": ["office", "document", "presentation"],
     "contextWindow": 64_000, "maxTokens": 4_000},
    {"model": "advanced", "abilityLevel": 3, "tags": ["analysis", "reasoning", "code", "debug", "data-analysis", "research", "vision"],
     "contextWindow": 128_000, "maxTokens": 8_000},
]


@pytest.mark.parametrize("task,options,model,ability", [
    pytest.param("你好", {}, "light", 1, id="chat"),
    pytest.param("把这句话翻译成英文：会议明天开始。", {}, "light", 1, id="translation"),
    pytest.param("润色这句话：明天见。", {}, "light", 1, id="rewrite"),
    pytest.param("总结这句话：今天下雨，活动延期。", {}, "light", 1, id="summary"),
    pytest.param("写一封会议邀请邮件。", {}, "office", 2, id="office"),
    pytest.param("制作三页 PPT，介绍团队分工。", {}, "office", 2, id="presentation"),
    pytest.param("分析 CSV 中各产品的销售额。", {}, "advanced", 3, id="data"),
    pytest.param("编写 Python 代码并修复测试错误。", {}, "advanced", 3, id="code"),
    pytest.param("比较两种方案并说明推理过程。", {}, "advanced", 3, id="reasoning"),
    pytest.param("搜索资料并给出来源。", {}, "advanced", 3, id="research"),
    pytest.param("读取图片中的文字。", {}, "advanced", 3, id="vision-text"),
    pytest.param("这是什么？", {"files": ["synthetic.png"]}, "advanced", 3, id="vision-file"),
    pytest.param("请处理附件。", {"files": ["synthetic.pdf"]}, "office", 2, id="document-file"),
    pytest.param("完成任务。", {"required_tools": ["execute_code"]}, "advanced", 3, id="explicit-tool"),
    pytest.param("制作成稿。", {"skills": ["sn-ppt-standard"]}, "office", 2, id="explicit-skill"),
    pytest.param("你好", {"estimated_input_tokens": 100_000}, "advanced", 1, id="long-context"),
    pytest.param("把这句话翻译成英文：这个错误已经修复。", {}, "light", 1,
                 id="translation-keyword-contamination"),
],
)
def test_synthetic_task_routes_to_the_expected_capability(task, options, model, ability):
    selected, decision = select_auto_model(MODELS, task=task, **options)
    assert decision["required_ability_level"] == ability
    assert selected["model"] == model


def test_synthetic_context_exhaustion_rejects_instead_of_claiming_a_route():
    with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED"):
        select_auto_model(MODELS, task="你好", estimated_input_tokens=108_001)


def test_independent_child_tasks_do_not_inherit_each_others_capability_floor():
    selected = [select_auto_model(MODELS, task=task)[0]["model"] for task in (
        "编写 Python 代码。", "你好", "读取图片中的文字。",
    )]
    assert selected == ["advanced", "light", "advanced"]


@pytest.mark.parametrize("task", [
    "请翻译成英文：这个错误已经修复。",
    "把“这个错误已经修复。”翻译成英文。",
    "将「这个错误已经修复。」翻译成英文。",
    '请翻译成英文：“这个错误已经修复。”',
    'Translate the following into Chinese: The error has been fixed.',
    'Please translate "The error has been fixed." into Chinese.',
    "Translate 'The error has been fixed.' into Chinese.",
    'Translate into Chinese: The error has been fixed and tested.',
    '翻译成英文：“分析代码中的错误，然后修复。”',
])
def test_translation_source_words_do_not_add_coding_tags_or_raise_ability(task):
    selected, decision = select_auto_model(MODELS, task=task)
    assert selected["model"] == "light"
    assert decision["task_tags"] == ["rewrite"]
    assert decision["required_ability_level"] == 1
    assert decision["estimated_input_tokens"] == (len(task) + 1) // 2


@pytest.mark.parametrize("task", [
    "请排查并修复这个错误。",
    "翻译下面的报错，并分析原因：连接错误。",
    "把“错误已经修复”翻译成英文，然后排查代码。",
    "翻译成英文：错误已经修复。然后帮我排查代码。",
    "翻译成英文：错误已经修复。请分析原因。",
    "翻译成英文：错误已经修复。\n排查代码中的问题。",
    'Translate "The error is fixed" and then debug the code.',
    'Translate into Chinese: The error is fixed. Then fix the code.',
    'Translate into Chinese: The error is fixed. Fix the code.',
    'Translate "The error is fixed" into Chinese. Explain the root cause.',
    'Translate into Chinese. Explain the root cause: The error was fixed.',
    'Translate "The error is fixed" into Chinese.\nExplain the root cause.',
    "翻译成英文：错误已经修复，并分析原因。",
    'Translate into Chinese: "The error is fixed',
    '解释“把这句话翻译成英文：错误已经修复”为什么被误判。',
])
def test_real_or_ambiguous_multi_action_requests_keep_their_advanced_route(task):
    selected, decision = select_auto_model(MODELS, task=task)
    assert selected["model"] == "advanced"
    assert decision["required_ability_level"] == 3


@pytest.mark.parametrize("options", [
    {"required_tools": ["execute_code"]},
    {"files": ["synthetic.png"]},
    {"task_tags": ["analysis"]},
    {"required_ability_level": 3},
])
def test_translation_preserves_explicit_capability_requirements(options):
    selected, decision = select_auto_model(
        MODELS, task="把这句话翻译成英文：这个错误已经修复。", **options,
    )
    assert selected["model"] == "advanced"
    assert decision["required_ability_level"] == 3


def test_translation_keeps_full_length_and_capacity_accounting():
    task = "翻译成英文：“" + "错误已经修复。" * 100 + "”"
    _, decision = select_auto_model(MODELS, task=task)
    assert decision["task_tags"] == ["rewrite"]
    assert decision["required_ability_level"] == 2
    assert decision["estimated_input_tokens"] == (len(task) + 1) // 2
    selected, _ = select_auto_model(MODELS, task=task, estimated_input_tokens=100_000)
    assert selected["model"] == "advanced"
    with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED"):
        select_auto_model(MODELS, task=task, estimated_input_tokens=108_001)
