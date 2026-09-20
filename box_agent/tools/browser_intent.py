"""Current-page browser intent guards shared by ACP and CLI turns."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from box_agent.schema import Message

_CURRENT_PAGE_ONLY_TOOLS = frozenset(
    {
        "user_browser_read_current_page",
        "user_browser_snapshot",
        "user_browser_click",
        "user_browser_fill",
        "user_browser_submit",
    }
)
_BROWSER_CONTEXT_TOOLS = frozenset(
    {
        "user_browser_open_tab_and_read",
        "user_browser_read_page",
        "user_browser_read_article",
        "user_browser_read_current_page",
        "user_browser_read_section",
        "user_browser_extract_structured_data",
        "user_browser_snapshot",
        "user_browser_click",
        "user_browser_fill",
        "user_browser_submit",
        "user_browser_session_start",
        "user_browser_session_call",
        "user_browser_session_end",
    }
)
_OPTIONAL_URL_CURRENT_PAGE_TOOLS = frozenset(
    {
        "user_browser_read_page",
        "user_browser_read_article",
    }
)
_CURRENT_PAGE_REFERENCE_RE = re.compile(
    r"(?:"
    r"当前(?:页|页面|网页|标签页|网站)"
    r"|(?:这个|这一个|此|本)(?:页面|网页|标签页|网站)"
    r"|(?:我的|我现在的)?浏览器(?:里|中|当前)(?:打开的)?(?:页面|网页|标签页)?"
    r"|我(?:现在|正在|刚刚|刚)?(?:打开|正在看|看|浏览)的(?:这个|那个)?(?:页面|网页|标签页|网站)"
    r"|(?:我)?(?:已经|已)?登录(?:后(?:的)?|着|的)?(?:页面|网页|网站)"
    r"|(?:这篇|当前|这个)公众号(?:文章|页面)?"
    r"|current\s+(?:browser\s+)?(?:page|tab)"
    r"|this\s+(?:browser\s+)?(?:page|tab|website|site)\b"
    r"|(?:page|tab)\s+i(?:'|’)m\s+(?:currently\s+)?(?:viewing|on)"
    r"|(?:page|tab)\s+i\s+have\s+open"
    r")",
    re.IGNORECASE,
)
_CONNECTOR_CONTEXT_RE = re.compile(
    r"(?:"
    r"真实浏览器|浏览器连接器|浏览器插件"
    r"|登录态|已经登录|已登录|登录后的?"
    r"|(?:当前|我的|现有|已有|保留|使用|依赖).{0,8}cookie"
    r"|cookie.{0,8}(?:登录|会话|状态)"
    r"|内网(?:页面|网站|系统|地址|链接|数据|后台)|公司内部网站"
    r"|公众号(?:文章|页面|链接)"
    r"|\bbrowser\s+connector\b|\breal\s+browser\b|\blogged[\s-]?in\b"
    r"|\b(?:existing|current|my)\s+cookies?\b|\bintranet\s+(?:page|site|system)\b"
    r")",
    re.IGNORECASE,
)
_FORM_INTERACTION_RE = re.compile(
    r"(?:填表|填写|填好|填完|录入|表单|字段|\bfill(?:ing)?\b|\bform\b|\binput\b)",
    re.IGNORECASE,
)
_HUMAN_REVIEW_RE = re.compile(
    r"(?:"
    r"(?:让我|给我|由我|我来|用户|人工|手动|最后人|最后用户).{0,16}"
    r"(?:看|查看|检查|确认|审核|点击|提交|接管)"
    r"|(?:填好|填完|填写完成).{0,16}(?:让我|给我|由我|我来|用户|人工|手动|看着|查看|确认|提交)"
    r"|(?:看着|显示浏览器|打开浏览器给我看|不要关闭浏览器)"
    r"|\b(?:let\s+me|i(?:'|’)ll|user|human|manually)\b.{0,40}"
    r"\b(?:review|inspect|verify|confirm|click|submit|take\s+over)\b"
    r"|\bhand(?:\s|-)?off\b"
    r")",
    re.IGNORECASE,
)
_BROWSER_CONTINUATION_RE = re.compile(
    r"^\s*(?:请)?(?:"
    r"(?:继续|接着)(?:(?:帮我|给我)?(?:看|看看|往下看|分析|对比|总结|解读))?"
    r"|(?:帮我|给我)?(?:看看|看一下)"
    r"|往下看"
    r"|再往下"
    r"|下一页"
    r"|翻页"
    r"|展开(?:一下)?"
    r"|再看(?:一下)?"
    r"|继续对比(?:一下)?"
    r"|(?:现在)?提交(?:吧|表单)?"
    r"|确认提交"
    r"|continue"
    r"|keep\s+going"
    r"|next\s+page"
    r")(?:一下|下)?(?:吧|呢|好吗|可以吗)?\s*[。.!！?？]?\s*$",
    re.IGNORECASE,
)
_CURRENT_PAGE_INTENT_ERROR = (
    "CURRENT_PAGE_INTENT_REQUIRED: the latest user request does not explicitly "
    "refer to the current browser page or continue a recent successful browser "
    "interaction. Do not inspect the user's active tab. Answer directly, use "
    "web_search, or use a URL-based browser tool instead."
)
_HUMAN_FINAL_ACTION_REQUIRED_ERROR = (
    "HUMAN_FINAL_ACTION_REQUIRED: the user asked to review, take over, or "
    "perform the final click themselves. Fill the visible real-browser form, "
    "stop before submission, and ask the user to review it."
)


def has_explicit_current_page_intent(user_text: str | None) -> bool:
    """Return whether the user explicitly refers to their current browser page."""
    if not user_text or not user_text.strip():
        return False
    return _CURRENT_PAGE_REFERENCE_RE.search(user_text.strip()) is not None


def is_browser_continuation(user_text: str | None) -> bool:
    """Return whether a short request continues the immediately prior browser task."""
    if not user_text or not user_text.strip():
        return False
    return _BROWSER_CONTINUATION_RE.fullmatch(user_text.strip()) is not None


def has_human_browser_handoff_intent(user_text: str | None) -> bool:
    """Return whether a visible browser must remain available for human review."""
    if not user_text or not user_text.strip():
        return False
    text = user_text.strip()
    return _HUMAN_REVIEW_RE.search(text) is not None and (
        _FORM_INTERACTION_RE.search(text) is not None
        or re.search(
            r"(?:浏览器|页面|网页|标签页|\bbrowser\b|\bpage\b)",
            text,
            re.IGNORECASE,
        )
        is not None
        or re.search(
            r"(?:最后人|最后用户|用户|人工|手动|我来).{0,16}(?:点击|提交|接管)",
            text,
            re.IGNORECASE,
        )
        is not None
    )


def has_recent_successful_browser_context(messages: Sequence[Message]) -> bool:
    """Return whether the prior turn successfully used the real-browser context."""
    user_indices = [index for index, message in enumerate(messages) if message.role == "user"]
    if len(user_indices) < 2:
        return False

    previous_turn = messages[user_indices[-2] + 1 : user_indices[-1]]
    for message in reversed(previous_turn):
        if (
            message.role != "tool"
            or (message.name or "") not in _BROWSER_CONTEXT_TOOLS
        ):
            continue
        content = message.content if isinstance(message.content, str) else str(message.content)
        if content.lstrip().startswith("Error:"):
            continue
        return True
    return False


def _latest_user_text(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if message.role != "user" or message.source != "user":
            continue
        if isinstance(message.content, str):
            return message.content
        return "\n".join(
            str(block.get("text", ""))
            for block in message.content
            if isinstance(block, dict)
        )
    return ""


@dataclass(frozen=True, slots=True)
class BrowserToolIntentPolicy:
    """Guard tools that access the user's active browser tab."""

    allow_current_page: bool
    human_handoff: bool

    @classmethod
    def for_turn(
        cls,
        *,
        current_turn_text: str | None,
        messages: Sequence[Message],
    ) -> "BrowserToolIntentPolicy":
        user_text = current_turn_text if current_turn_text is not None else _latest_user_text(messages)
        human_handoff = has_human_browser_handoff_intent(user_text)
        allow_current_page = (
            has_explicit_current_page_intent(user_text)
            or human_handoff
            or _CONNECTOR_CONTEXT_RE.search(user_text or "") is not None
            or (
                is_browser_continuation(user_text)
                and has_recent_successful_browser_context(messages)
            )
        )
        return cls(
            allow_current_page=allow_current_page,
            human_handoff=human_handoff,
        )

    def is_tool_visible(self, tool_name: str) -> bool:
        """Hide active-tab tools unless the current turn explicitly needs them."""
        if not self.allow_current_page and tool_name in _CURRENT_PAGE_ONLY_TOOLS:
            return False
        if self.human_handoff and tool_name == "user_browser_submit":
            return False
        return True

    def tool_call_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """Return an internal denial reason for a disallowed active-tab call."""
        if self.human_handoff and tool_name == "user_browser_submit":
            return _HUMAN_FINAL_ACTION_REQUIRED_ERROR
        if self.allow_current_page:
            return None
        if tool_name in _CURRENT_PAGE_ONLY_TOOLS:
            return _CURRENT_PAGE_INTENT_ERROR
        if tool_name in _OPTIONAL_URL_CURRENT_PAGE_TOOLS and not arguments.get("url"):
            return _CURRENT_PAGE_INTENT_ERROR
        if tool_name == "user_browser_session_call":
            action = str(arguments.get("action") or "")
            nested_args = arguments.get("args")
            if (
                action in {"read_page", "read_article"}
                and not isinstance(nested_args, dict)
            ):
                return _CURRENT_PAGE_INTENT_ERROR
            if (
                action in {"read_page", "read_article"}
                and isinstance(nested_args, dict)
                and not nested_args.get("url")
            ):
                return _CURRENT_PAGE_INTENT_ERROR
        return None
