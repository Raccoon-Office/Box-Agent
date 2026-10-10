"""Capability-gated prompt fragments.

The system prompt should only tell the model to call tools that the session
actually registered. Each renderer takes ``tools`` — the session's tool names,
or ``None`` when unknown (compatibility callers, prepared host prompts) — and
returns the historical full text when ``tools`` is ``None`` or every tool it
mentions is present, so default sessions keep a byte-identical prompt.
"""

from __future__ import annotations

from collections.abc import Collection

PLANNING_TOOLS_PLACEHOLDER = "{PLANNING_TOOLS}"
CODE_SEARCH_TOOLS_PLACEHOLDER = "{CODE_SEARCH_TOOLS}"

_SCHEMA_NOTE = "参数和状态转换以当前工具 schema 为准。"
_SUB_AGENT_GUIDANCE = (
    "`sub_agent` 只在独立上下文、并行耗时或证据隔离的收益明显高于启动和合并成本时使用；"
    "冲突处理、最终交付和验证由主 Agent 完成。"
)


def _has(tools: Collection[str] | None, *names: str) -> bool:
    return tools is None or all(name in tools for name in names)


def render_planning_tools(tools: Collection[str] | None) -> str:
    """Planning/progress/delegation tool guidance for the Plan workflow step."""
    plan, todo = _has(tools, "plan_write"), _has(tools, "todo_write")
    if plan and todo:
        text = (
            "用户要求方案或需要先展示范围、步骤、验证和风险时，在工具可用时用 `plan_write`；"
            "≥3 步的执行进度用 `todo_write`。Plan 表达方法，Todo 只记录进度，"
            "不是事实证据或结论来源；" + _SCHEMA_NOTE
        )
    elif plan:
        text = (
            "用户要求方案或需要先展示范围、步骤、验证和风险时用 `plan_write`。"
            "Plan 表达方法，不是事实证据或结论来源；" + _SCHEMA_NOTE
        )
    elif todo:
        text = "≥3 步的执行进度用 `todo_write`。Todo 只记录进度，不是事实证据或结论来源；" + _SCHEMA_NOTE
    else:
        text = ""
    if _has(tools, "sub_agent"):
        text += _SUB_AGENT_GUIDANCE
    return text


def render_code_search_tools(tools: Collection[str] | None) -> str:
    """How to locate code: ripgrep-backed grep/glob when registered, else search_files."""
    if _has(tools, "glob", "grep"):
        return (
            "已知文件路径先用 `read_file`；只知文件名或扩展名时用 `glob`；查找符号或内容时用 `grep`；"
            "定位后用 `read_file` 阅读必要上下文。专用搜索工具不可用时回退到 `search_files`，避免凭印象修改。"
        )
    return (
        "已知文件路径先用 `read_file`；只知文件名、扩展名，或要查找符号、内容时用 `search_files`"
        "（`target=\"files\"` 找文件，`target=\"content\"` 找内容）；定位后用 `read_file` 阅读必要上下文，"
        "避免凭印象修改。"
    )


def resolve_capability_placeholders(text: str, tools: Collection[str] | None) -> str:
    """Fill every capability placeholder a template or mode prompt may carry."""
    if PLANNING_TOOLS_PLACEHOLDER in text:
        text = text.replace(PLANNING_TOOLS_PLACEHOLDER, render_planning_tools(tools))
    if CODE_SEARCH_TOOLS_PLACEHOLDER in text:
        text = text.replace(CODE_SEARCH_TOOLS_PLACEHOLDER, render_code_search_tools(tools))
    return text


__all__ = [
    "CODE_SEARCH_TOOLS_PLACEHOLDER",
    "PLANNING_TOOLS_PLACEHOLDER",
    "render_code_search_tools",
    "render_planning_tools",
    "resolve_capability_placeholders",
]
