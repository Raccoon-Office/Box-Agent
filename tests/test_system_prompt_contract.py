from pathlib import Path

from box_agent.tools.setup import render_system_prompt_template


def _prompt() -> str:
    return Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")


def test_system_prompt_keeps_the_stable_template_compact():
    prompt = _prompt()

    assert len(prompt) <= 4_000
    assert prompt.count("{SKILLS_METADATA}") == 1
    assert prompt.count("{SANDBOX_INFO}") == 1
    assert prompt.count("{FILE_DELIVERY_INFO}") == 1


def test_system_prompt_renders_current_date_without_leaving_legacy_tokens():
    rendered = render_system_prompt_template(_prompt(), current_date="2026-08-26")

    assert "2026-08-26" in rendered
    assert "{{.CurrentDate}}" not in rendered
    assert "{{.Language}}" not in rendered


def test_system_prompt_forbids_plaintext_user_credentials():
    prompt = _prompt()

    assert "不得在回复、日志、命令参数或交付产物中明文显示用户提供的" in prompt
    assert "API Key、Access Token、Secret、密码等敏感凭据" in prompt
    assert "确需引用时仅显示脱敏片段" in prompt


def test_system_prompt_resolves_paths_without_broad_home_searches():
    prompt = _prompt()

    assert "相对路径由工具从当前 active project/artifact root 解析" in prompt
    assert "不要假定始终相对 workspace" in prompt
    assert "不递归搜索整个用户主目录" in prompt
    assert "候选均失败后再询问" in prompt


def test_system_prompt_distinguishes_missing_attachments_from_explicit_paths():
    prompt = _prompt()

    assert "用户明确说明文件“还没有上传/未上传/未提供”时" in prompt
    assert "视为确定缺失，不得调用 `search_files` 或猜测路径" in prompt
    assert "直接调用它请求上传文件或提供路径" in prompt
    assert "用户已经给出路径或位置时" in prompt
    assert "才先按当前路径与权限语义调用工具验证" in prompt
    assert "不要仅因缺少附件元信息就把请求判定为缺失输入" in prompt


def test_system_prompt_keeps_workflow_policy_without_duplicating_tool_schemas():
    prompt = _prompt()

    assert "Plan 表达方法，Todo 只记录进度" in prompt
    assert "不是事实证据或结论来源" in prompt
    assert "参数和状态转换以当前工具 schema 为准" in prompt
    assert "最终交付和验证由主 Agent 完成" in prompt
    assert "action=\"transition\"" not in prompt
    assert "write_scope=[" not in prompt
    assert '"max_tool_calls"' not in prompt


def test_system_prompt_requires_authoritative_current_evidence():
    prompt = _prompt()

    assert "必须按今天日期检索或核对当前权威来源" in prompt
    assert "优先使用官方公告、模型卡、开发者文档、透明度页面或权威一手来源" in prompt
    assert "不要用“公开资料不多”替代答案" in prompt
    assert "搜索链接、占位符或“请自行查看”不能冒充已取得的结果" in prompt


def test_system_prompt_separates_source_content_from_search_clues():
    prompt = _prompt()

    assert "才可声称“已读到原文/完整内容”" in prompt
    assert "搜索结果、标题、摘要、转载页或相近内容只能作为线索" in prompt
    assert "不能替代原文" in prompt
    assert "读取失败时明确说明原因和证据缺口" in prompt
    assert "不得声称已读取、打开或核对" in prompt


def test_system_prompt_routes_direct_sources_before_marketplace_search():
    prompt = _prompt()

    assert "先读取并核对该直接来源" in prompt
    assert "不要把直接来源请求改写为市场搜索" in prompt
    assert "单个目录、服务或 Skill 市场的空结果只代表该来源未命中" in prompt
    assert "存在其他安全可用来源时不得据此结束任务" in prompt


def test_system_prompt_forbids_reusing_model_history_placeholders():
    prompt = _prompt()

    assert "[Full tool-call argument omitted from model history]" in prompt
    assert "是内部历史摘要，不是真实文件内容" in prompt
    assert "绝不能复制到任何工具参数" in prompt
    assert "不要为绕过摘要保护而改用 `execute_code`" in prompt


def test_system_prompt_routes_large_jsonl_to_bounded_query_tool():
    prompt = _prompt()

    assert "JSONL/NDJSON" in prompt
    assert "使用 `query_jsonl` 做字段投影和游标分页" in prompt
    assert "不要因 JSONL 超长记录改用 `execute_code` 整体读取" in prompt


def test_system_prompt_keeps_file_delivery_mode_specific():
    prompt = _prompt()

    assert "{FILE_DELIVERY_INFO}" in prompt
    assert "同名直接覆盖" not in prompt
    assert "所有交付物落 `{workspace}/output/`" not in prompt


def test_system_prompt_pauses_only_for_blocking_input_or_sensitive_decisions():
    prompt = _prompt()

    assert "确实阻塞可信交付时" in prompt
    assert "若 `request_user_input` 可用，必须调用一次" in prompt
    assert "只问一个聚焦问题并列出最少必要字段" in prompt
    assert "保留已有产物并在用户补充后继续" in prompt
    assert "需要用户选择时使用 `request_user_decision`" in prompt
    assert "可推荐偏好，不得使用 `request_user_input`" in prompt
    assert "请求 30 秒倒计时" in prompt
    assert "敏感决策必须等待用户选择" in prompt


def test_system_prompt_checks_explicit_requirements_before_completion():
    prompt = _prompt()

    assert "结束前逐项核对用户要求的内容、数据、时效和格式" in prompt
    assert "产物存在不等于任务完成" in prompt
    assert "明确标记未完成和证据缺口" in prompt
