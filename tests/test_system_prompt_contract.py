from pathlib import Path


def test_system_prompt_keeps_todo_separate_from_factual_evidence():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "`plan_write` 表达“准备怎么做”" in prompt
    assert "不是进度追踪" in prompt
    assert "不含新任务的确认词而新建 plan" in prompt
    assert "todo_write` 只记录执行进度" in prompt
    assert "不是事实证据、检索策略或结论来源" in prompt
    assert "完成并验证后立即用 `transition` 推进" in prompt
    assert "已有 plan，新建或重建 todo 前必须先调用 `plan_read`" in prompt
    assert "todo 必须按 `plan.steps` 的原顺序派生" in prompt
    assert "先用 `plan_write` 把 plan 标为 `revised`" in prompt
    assert "初始化清单或 plan 实质修订后" in prompt
    assert "正常推进用 `action=\"transition\"`" in prompt
    assert "为未变化的已有 Todo 保留 `id`" in prompt
    assert "首次初始化 Todo 时不要传 `id`" in prompt
    assert "Plan step 的 `id` 不是 Todo `id`" in prompt
    assert "列表为空或全部完成时允许没有 `in_progress`" in prompt
    assert "否则必须恰好有一个 `in_progress`" in prompt
    assert "只执行唯一的 `in_progress` 项" in prompt
    assert "任务计划显示完成只代表步骤执行完毕，不代表事实已核实" in prompt


def test_system_prompt_requires_authoritative_sources_for_current_facts():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "Factual & Search Reliability" in prompt
    assert "必须按今天日期检索或核对当前权威来源" in prompt
    assert "优先使用官方公告、模型卡、开发者文档、透明度页面或权威一手来源" in prompt
    assert "不要用“公开资料不多”替代答案" in prompt


def test_system_prompt_separates_source_content_from_search_clues():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "只有成功读取该 URL/文件/原始材料正文" in prompt
    assert "才可声称“已读到原文/完整内容”" in prompt
    assert "`web_search` 搜索结果、标题、摘要、转载页或相近内容只能作为线索" in prompt
    assert "不能替代原文" in prompt
    assert "明确说明失败原因和证据缺口" in prompt
    assert "禁止把它包装成对原文的总结、核对或引用" in prompt
    assert "只有对应工具成功返回目标内容时，才可这样表述" in prompt


def test_system_prompt_forbids_reusing_model_history_placeholders():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "[Full tool-call argument omitted from model history]" in prompt
    assert "是内部历史摘要，不是真实文件内容" in prompt
    assert "绝不能复制到任何工具参数" in prompt
    assert "不要为绕过摘要保护而改用 `execute_code`" in prompt


def test_system_prompt_routes_large_jsonl_to_bounded_query_tool():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "JSONL/NDJSON" in prompt
    assert "使用 `query_jsonl` 做字段投影和游标分页" in prompt
    assert "不要因 JSONL 超长记录改用 `execute_code` 整体读取" in prompt


def test_system_prompt_uses_mode_specific_file_delivery_guidance():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "{FILE_DELIVERY_INFO}" in prompt
    assert "local-file://C:/" not in prompt
    assert "所有交付物落 `{workspace}/output/`" not in prompt


def test_system_prompt_sub_agent_uses_one_loop_and_inherits_parent_boundary():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "独立上下文、并行耗时或证据隔离收益明显高于启动和合并成本" in prompt
    assert "不要仅因单元数量达到 5 个就强制并行" in prompt
    assert "始终运行同一种通用 agent loop" in prompt
    assert "不存在调用方或运行时策略选择" in prompt
    assert "顶层 `skills`" in prompt
    assert "顶层 `required_tools`" in prompt
    assert "省略 `skills` 等价于空列表" in prompt
    assert "省略 `required_tools` 会继承主 agent 当前全部可用工具" in prompt
    assert "显式列表只用于缩小工具范围" in prompt
    assert "继承主 agent 当前权限和约束" in prompt
    assert (
        "不要传 `execution`、`capabilities`、`inputs` 或 `constraints`"
        in prompt
    )
    assert "最终合并、交叉校验" in prompt
    assert '`budget` 必须直接传对象' in prompt
    assert "互斥的精确输出路径" in prompt


def test_system_prompt_makes_missing_input_a_resumable_pause():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "调用一次 `request_user_input`" in prompt
    assert "只问一个聚焦问题并列出最少必要字段" in prompt
    assert "用户补充后从当前检查点继续" in prompt
    assert "可省略或可标为待补充的非必要内容不得阻塞" in prompt


def test_system_prompt_separates_user_decisions_from_missing_input():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "调用 `request_user_decision`" in prompt
    assert "不要只在普通文本里列方案并等待回复" in prompt
    assert "内部实现和恢复策略应自行选择" in prompt
    assert "最终是否允许由运行时决定" in prompt


def test_system_prompt_checks_explicit_requirements_before_claiming_completion():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "产物文件存在不等于用户要求已经满足" in prompt
    assert "逐项核对用户明确要求的内容、数据、时效和格式" in prompt
    assert "搜索链接、占位符或“请自行查看”不能冒充已取得的实时结果" in prompt
    assert "明确标记未完成及其证据缺口，不得宣称任务已完成" in prompt
