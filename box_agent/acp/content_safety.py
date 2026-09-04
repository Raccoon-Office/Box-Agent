"""ACP user-input safety checks for the Kanshan desktop product."""

from __future__ import annotations

import re


PUBLIC_IDENTITY_REPLY = "我是看山工作台，一款 AI 桌面工作平台。"
SAFETY_REFUSAL_REPLY = (
    "这个问题我没办法回答。\n\n"
    "涉及政治敏感话题，我无法展开讨论。如果你有其他方面的问题——比如技术、文档处理、"
    "数据分析、学习知识等——我很乐意帮忙。"
)

_IDENTITY_QUERY = re.compile(
    r"(?:你是谁|你是什么模型|什么模型|底层模型|模型身份|who\s+are\s+you|"
    r"what\s+model\s+are\s+you|are\s+you\s+(?:claude|anthropic|openai))",
    re.IGNORECASE,
)
_POLITICAL_SENSITIVE_QUERY = re.compile(
    r"(?:习近平|习近|毛泽东|毛遮洞|江泽民|彭德怀|齐奥塞斯库|中国总统|"
    r"解放军|百日无孩|老毛|小彭|连任|下台)",
    re.IGNORECASE,
)
_CIRCUMVENTION_QUERY = re.compile(
    r"(?:翻墙|vpn|蓝灯|lantern|getlantern)",
    re.IGNORECASE,
)


def safe_acp_reply_for_user_text(user_text: str) -> str | None:
    """Return a local response when a query must not reach an upstream model."""
    text = user_text.strip()
    if not text:
        return None
    if _IDENTITY_QUERY.search(text):
        return PUBLIC_IDENTITY_REPLY
    if _POLITICAL_SENSITIVE_QUERY.search(text) or _CIRCUMVENTION_QUERY.search(text):
        return SAFETY_REFUSAL_REPLY
    return None
