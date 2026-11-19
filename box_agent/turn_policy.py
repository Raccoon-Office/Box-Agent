"""Shared, side-effect-free policies for classifying user turns."""

from __future__ import annotations

import re

__all__ = [
    "text_is_short_acknowledgement",
    "text_is_short_non_task_reply",
    "text_requests_plan_revision",
    "text_requests_plan_start",
]

_PLAN_COMMANDS = {"计划", "规划", "规划一下", "计划 plan", "plan", "plan mode"}
_PLAN_COMMAND_PREFIX = re.compile(r"^(?:/plan(?=$|[\s:：])|plan\s+mode[:：])")
_PLAN_QUOTED_TEXT = re.compile(
    r'```[\s\S]*?(?:```|\Z)|`[^`\n]*`|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"'
    r"|(?<!\w)'[^'\n]+'(?!\w)"
)
_PLAN_REQUEST_PREFIX_ZH = r"(?:(?:请(?:你|帮我)?|麻烦(?:你|帮我)?|帮我|你)\s*)?"
_PLAN_START_ZH = re.compile(
    rf"(?:^|[，,。！？!?；;])\s*{_PLAN_REQUEST_PREFIX_ZH}"
    r"(?:先\s*(?:帮我|给我)?(?:规划|进行规划|计划(?=$|[\s，,：:。！？!?；;]|一下|再|然后)|"
    r"(?:给(?:我|出)?|出|做|列(?:出)?|写(?:出)?|制定|拟定|提供)"
    r"(?:一个|一份|一版|个|份|版)?(?:执行|实施)?(?:计划|规划|方案|plan)"
    r"(?=$|[\s，,：:。！？!?；;]|再|然后|给我|我看))"
    r"|(?:出|给我)(?:一个|一份|个)(?:执行)?计划(?:给我看|我看一下)"
    r"|使用\s*plan(?=$|[\s，,：:。！？!?；;])(?!\s+(?:模板|文件|文档|表格)))"
)
_MAKE_PLAN_EN = (
    r"(?:make|create|write|draft|outline|show me|give me)\s+(?:(?:an?|the)\s+)?"
    r"(?:execution\s+)?plan"
)
_PLAN_START_EN = re.compile(
    r"(?:^|[,.!?;]\s+)(?:please\s+|i\s+(?:want|need)\s+you\s+to\s+)?"
    r"(?:plan(?:ning)?\s+first(?=$|[,.:!?;]|\s+(?:and|then|before|for)\b)|"
    r"use\s+plan\s+mode\b|"
    rf"first\s+{_MAKE_PLAN_EN}\b(?![.-]\w)|{_MAKE_PLAN_EN}\s+(?:first\b|before\s+(?:you\s+)?"
    r"(?:start\w*|execut\w*|implement\w*|making changes)\b))"
)
_CURRENT_PLAN_ZH = r"(?:当前|这个|这份|刚才的|上一轮的|上面的|原来的)?(?:执行)?(?:计划|规划|方案)"
_PLAN_REVISION_ZH = re.compile(
    rf"^{_PLAN_REQUEST_PREFIX_ZH}(?:先\s*)?"
    rf"(?:(?:修改|调整|更新|重做)(?:一下)?{_CURRENT_PLAN_ZH}"
    r"(?=$|[\s，,：:。！？!?；;]|(?:[里中]的?|的)?第|再|然后)"
    rf"|(?:把|将|在){_CURRENT_PLAN_ZH}"
    r"(?=[里中\s，,]|的?第|改成|改为|加上|增加|删掉|删除|去掉)"
    r"[^。；;\n]*?(?:改成|改为|加上|增加|删掉|删除|去掉))"
)
_PLAN_REVISION_EN = re.compile(
    r"^(?:please\s+)?(?:revise|update|adjust|change)\s+"
    r"(?:this|the(?:\s+(?:current|previous|above))?)\s+(?:execution\s+)?"
    r"plan(?!\.\w)(?=$|[\s,.:!?;])"
)

_PLAN_START_NEGATIONS = (
    "不需要计划",
    "无需计划",
    "不要计划",
    "不用计划",
    "别计划",
    "不需要规划",
    "无需规划",
    "不要规划",
    "不用规划",
    "别规划",
    "不需要方案",
    "无需方案",
    "不要方案",
    "不用方案",
    "不使用plan",
    "不用plan",
    "不要plan",
    "noplan",
    "withoutplan",
    "withoutaplan",
)

_SHORT_ACK_STRIP_RE = re.compile(r"[\s,，.。!！?？;；:：\"'“”‘’`]+")
_SHORT_ACKNOWLEDGEMENTS = {
    "ok",
    "okay",
    "k",
    "yes",
    "yeah",
    "yep",
    "sure",
    "approve",
    "approved",
    "confirm",
    "confirmed",
    "continue",
    "proceed",
    "goahead",
    "execute",
    "runit",
    "run",
    "好",
    "好的",
    "可以",
    "可以的",
    "行",
    "行的",
    "没问题",
    "收到",
    "明白",
    "了解",
    "嗯",
    "嗯嗯",
    "继续",
    "继续执行",
    "执行",
    "确认",
    "已确认",
    "同意",
    "批准",
    "开始",
    "开始吧",
    "开始执行",
}
_SHORT_NON_TASK_REPLIES = _SHORT_ACKNOWLEDGEMENTS | {
    "hi",
    "hello",
    "hey",
    "thanks",
    "thankyou",
    "thx",
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "谢谢",
    "谢谢你",
}


def text_requests_plan_start(text: str) -> bool:
    """Return whether ``text`` explicitly asks to start with a plan."""
    normalized = _PLAN_QUOTED_TEXT.sub("…", text.strip().lower())
    compact = "".join(normalized.split()).replace("先", "")
    if any(negation in compact for negation in _PLAN_START_NEGATIONS):
        return False
    return (
        " ".join(normalized.rstrip("。.!！").split()) in _PLAN_COMMANDS
        or bool(_PLAN_COMMAND_PREFIX.match(normalized))
        or bool(_PLAN_START_ZH.search(normalized))
        or bool(_PLAN_START_EN.search(normalized))
    )


def text_requests_plan_revision(text: str) -> bool:
    """Return whether the request explicitly edits the current execution plan."""
    normalized = _PLAN_QUOTED_TEXT.sub("…", text.strip().lower())
    return bool(_PLAN_REVISION_ZH.match(normalized) or _PLAN_REVISION_EN.match(normalized))


def text_is_short_acknowledgement(text: str) -> bool:
    """Return whether ``text`` is a short approval/acknowledgement."""
    compact = _SHORT_ACK_STRIP_RE.sub("", text.strip().lower())
    if not compact or len(compact) > 40:
        return False
    return compact in _SHORT_ACKNOWLEDGEMENTS


def text_is_short_non_task_reply(text: str) -> bool:
    """Return whether ``text`` is a short reply without a concrete task."""
    compact = _SHORT_ACK_STRIP_RE.sub("", text.strip().lower())
    if not compact or len(compact) > 40:
        return False
    return compact in _SHORT_NON_TASK_REPLIES
