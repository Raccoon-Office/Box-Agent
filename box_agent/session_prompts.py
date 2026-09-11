"""Shared model-facing capability hint contracts, independent of host transport."""
from __future__ import annotations

import json
from pathlib import Path

GENERAL_DIRECTORY_ORGANIZATION_PROMPT = """## General Task Directory Organization
- 保持当前会话工作目录（cwd）不变。你创建的任务子目录只是文件组织行为，不是新的 workspace。
- 在写入独立任务的产物前，先查看 cwd 的顶层结构。修改现有项目时直接在项目树中的合适位置工作，不要另建任务目录。
- 目录选择遵循 File & Bash Operations 的规则，不要使用固定文件数量阈值。
- 目录通常是 cwd 的直接子目录，使用简短、语义明确的名称。创建前检查同名路径；只在确认属于同一任务时复用，否则添加简短后缀，禁止覆盖无关内容。
- 用户明确指定输出目录或文件路径时优先遵循用户路径，只要工具权限允许。
- 目录以任务为生命周期：相关追问继续复用；用户切换到无关任务时重新判断。上下文摘要应保留当前任务采用的目录；若恢复后该信息缺失，重新检查目录，不要自动移动或合并已有文件。
- PPT 和深度研究任务必须显式把选定目录的绝对路径传给 Skill 或脚本；若未创建独立目录，则显式使用 cwd。不要依赖隐式 output root 或输出目录环境变量。"""


_MEMORY_MIN_CHARS = 30
# Lower-cased identity keywords. If any is present we consider the user has
# shared at least their name and skip the onboarding hint.
_IDENTITY_KEYWORDS = ("name", "姓名", "我叫", "我是", "叫我")

_HINT_FORMAT_BLOCK = (
    "```action_hint\n"
    "{\n"
    '  "action": "open_settings",\n'
    '  "params": {"tab": "<tab-name>"},\n'
    '  "display_text": "<面向用户的一句话引导文案>"\n'
    "}\n"
    "```"
)

def is_memory_scarce(memory_text: str | None) -> bool:
    """Return True when MEMORY.md is empty, very short, or has no name hint."""
    if not memory_text:
        return True
    stripped = memory_text.strip()
    if len(stripped) < _MEMORY_MIN_CHARS:
        return True
    lowered = stripped.lower()
    return not any(keyword in lowered for keyword in _IDENTITY_KEYWORDS)


def is_playwright_unavailable(
    mcp_config_path: Path | None,
    *,
    mcp_globally_enabled: bool = True,
) -> bool:
    """Return True when the Playwright MCP server is absent or disabled.

    Missing file or unreadable JSON is treated as "unavailable" — the model
    is told it cannot rely on a browser tool either way. ``mcp_globally_enabled``
    short-circuits to True when the runtime has MCP turned off entirely
    (config ``tools.enable_mcp = false``); in that case no entry in mcp.json
    is going to load.
    """
    if not mcp_globally_enabled:
        return True
    if mcp_config_path is None or not mcp_config_path.exists():
        return True
    try:
        data = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True

    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        return True

    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        haystack = f"{name} {entry.get('command', '')} {' '.join(map(str, entry.get('args', []) or []))}".lower()
        if "playwright" not in haystack:
            continue
        if entry.get("disabled", False):
            continue
        return False  # Found an enabled playwright entry
    return True


def is_playwright_unavailable_from_env_context(env_context: object | None) -> bool:
    """Return True when host env_context says Playwright is not actually usable."""
    browser_tools = getattr(env_context, "browser_tools", None)
    return getattr(browser_tools, "available", None) is False


def build_action_hints_prompt(
    *,
    memory_scarce: bool,
    playwright_unavailable: bool,
) -> str:
    """Build the system-prompt section that defines the action_hint contract.

    Returns an empty string when no scenario is active so the prompt stays
    lean. Each enabled scenario contributes one bullet describing the tab
    and when to use it.
    """
    rules: list[str] = []
    if memory_scarce:
        rules.append(
            '- 用户问候、自我介绍、问"你是谁/你能做什么"等关系建立类话题，且当前对用户了解很少时 → '
            '使用 `"tab": "onboarding"`，引导用户去"个人记忆"页完善信息。'
        )
    if playwright_unavailable:
        rules.append(
            "- 用户提出不依赖当前真实浏览器状态的自动化测试、截图、网络检查、批量网页操作等需求，"
            '但当前会话没有可用的 Playwright 工具时 → 使用 `"tab": "browser-tools"`，引导用户启用 Playwright。'
            "若需求依赖当前页、登录态或内网，且 `user_browser_*` 工具可用，"
            "应直接使用用户浏览器，不要仅因受管浏览器缺失就输出该提示。"
        )

    if not rules:
        return ""

    return (
        "## 用户引导提示 (Action Hint)\n"
        "当用户的当前问题真正契合下述场景时，在你的回复末尾追加一个 `action_hint` 围栏块。"
        "前端会解析它并渲染为可点击链接，引导用户打开对应的设置页。\n\n"
        "### 格式契约\n"
        "只接受下面这种三反引号 `action_hint` 围栏（不要用 XML 标签）：\n"
        f"{_HINT_FORMAT_BLOCK}\n\n"
        "### 触发场景（仅以下场景启用）\n"
        + "\n".join(rules)
        + "\n\n"
        "### 约束\n"
        "- 必须使用三个反引号包裹的 ```action_hint``` 代码围栏，"
        "禁止使用 `<action_hint>...</action_hint>` 这类 XML/HTML 标签包裹，"
        "否则前端无法识别。\n"
        "- 开始围栏这一行只能写 ```action_hint，不要在同一行追加 `{...}` 或其他内容；"
        "JSON 必须从下一行开始。\n"
        "- 一次回复最多输出一个 `action_hint` 块。\n"
        "- 块内必须是合法 JSON，且 `tab` 字段必须取自上述列表；"
        "`display_text` 必须是一行短文案，不要包含换行符。\n"
        "- 用户语境不契合时不要输出，避免打扰。\n"
        "- 正文先正常回答用户的问题，再追加这个块；不要把它放在正文中间。"
    )


def build_follow_up_suggestions_prompt() -> str:
    """Return the opt-in response-metadata contract for local-agent sessions."""

    return (
        "## 后续建议（仅供本地 Agent 输入框使用）\n"
        "当且仅当你已经完成当前用户任务、无需用户补充信息、也没有错误或待执行步骤时，"
        "在可见回复的最后追加一个 `follow_up_suggestions` 围栏块。\n\n"
        "格式必须严格如下：\n"
        "```follow_up_suggestions\n"
        '{"suggestions":["基于刚才结果可以继续做的具体事项", "另一个自然的下一步"]}\n'
        "```\n\n"
        "约束：\n"
        "- 给 1 到 3 条建议；每条是一句可直接发出的后续请求，使用可见回复的主要语言。\n"
        "- 建议必须基于刚完成的结果，具体且互不重复；不要给泛泛的“还有问题吗”。\n"
        "- 简单问候、仅确认/致谢、任务失败、正在执行、需要用户确认或补充信息时不要输出该块。\n"
        "- 围栏块是宿主读取的元数据，不要在可见正文解释它，也不要输出其他字段。"
    )
