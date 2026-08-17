"""Prompt-aware startup configuration for new presentation tasks."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Final, Literal

from ..delivery import (
    is_meta_prompt_rewrite_request,
    strip_negated_format_clauses,
)
from .presentation_contract import PRESENTATION_DELIVERY_KEYWORDS


_PREFLIGHT_CONFIG_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "document-skills"
    / "pptx"
    / "references"
    / "presentation-preflight.json"
)
_EDIT_EXISTING_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:修改|编辑|调整|优化|润色|美化|改一下|改这个|继续(?:做|完成)|"
    r"接着(?:做|完成)|补完|edit|modify|revise|polish|continue|resume)",
    re.IGNORECASE,
)
_NEW_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:新建|从零|重新(?:做|制作|生成)|另(?:做|出)(?:一份|一版)?|"
    r"再做一版|new\s+(?:deck|presentation)|from\s+scratch|remake)",
    re.IGNORECASE,
)
_CREATE_PRESENTATION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:做一份|做一个|做个|做份|做成|制作成|整理成|制作|生成|创建|新建|另出|另做|重新做|"
    r"create|generate|make|build|produce|draft|remake)"
    r"(?!\s*的)"
    r"[^，。；;.!?\n]{0,48}"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)",
    re.IGNORECASE,
)
_EXPORT_PRESENTATION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:导出|转换|转成|另存为|export|convert|save\s+as)"
    r"[^，。；;.!?\n]{0,48}"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)",
    re.IGNORECASE,
)
_CONTINUE_PRESENTATION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"(?:继续|接着)(?:做|制作|生成|创建|完成|补完|渲染|导出)"
    r"[^，。；;.!?\n]{0,32}"
    r"|(?:继续|接着)\s*(?:这个|这份)?\s*"
    r"|(?:补完|完成)\s*"
    r"|(?:continue|resume|finish)(?:\s+(?:making|creating|finishing|rendering))?\s+"
    r")"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)",
    re.IGNORECASE,
)
_REQUEST_NEW_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:我要|我想要|我需要|需要|请给我|请帮我|麻烦给我|给我|帮我)"
    r"\s*(?:(?:做|制作|生成|创建|出|来)\s*)?"
    r"(?:一份|一个|一版|个|份)?"
    r"[^，。；;.!?\n]{0,40}?"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)"
    r"|(?:帮我\s*)?(?:出|来)\s*(?:一份|一个|一版|个|份)"
    r"[^，。；;.!?\n]{0,40}?"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)",
    re.IGNORECASE,
)
_NON_PRESENTATION_ARTIFACT_RE: Final[str] = (
    r"(?:柱状图|折线图|饼图|散点图|流程图|图表|图形|图片|配图|图像|"
    r"数据表|统计表|表格|excel|xlsx|文案|文字|摘要|讲稿|视频|素材|"
    r"charts?|graphs?|images?|figures?|tables?|spreadsheets?|copy|summary|video)"
)
_PRESENTATION_USAGE_CONTAINER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)"
    r"\s*(?:里|中|内|上|里的|中的|内的|上的|的|用的|所用的|"
    r"[-–—]?\s*(?:ready|compatible)\b)"
    rf"[^，。；;.!?\n]{{0,64}}{_NON_PRESENTATION_ARTIFACT_RE}",
    re.IGNORECASE,
)
_ARTIFACT_FOR_PRESENTATION_RE: Final[re.Pattern[str]] = re.compile(
    rf"{_NON_PRESENTATION_ARTIFACT_RE}"
    r"[^，。；;.!?\n]{0,40}?"
    r"(?:用于|用在|用到|放入|放进|放到|加入|插入|贴到|"
    r"for(?:\s+use)?\s+in|for|into)"
    r"[^，。；;.!?\n]{0,24}?"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)",
    re.IGNORECASE,
)
_INSPECT_EXISTING_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:我要|我想要|我需要|需要|请给我|请帮我|麻烦给我|给我|帮我)"
    r"\s*(?:分析|解读|总结|评价|检查|审查|审核|看看|阅读|理解|了解|比较)"
    r"(?:一下|下)?\s*(?:这个|该|现有|这份|附件(?:里|中|里的|中的)?)?\s*"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)"
    r"|(?:这个|该|现有|这份|附件(?:里|中|里的|中的)?)?\s*"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)"
    r"\s*(?:分析|解读|总结|评价|检查|审查|审核|看看|阅读|理解|了解|比较)",
    re.IGNORECASE,
)
_OUTLINE_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:只|仅).{0,12}(?:大纲|内容方案)|"
    r"(?:不要|无需|不需要).{0,12}(?:页面|幻灯片|html|pptx?)|"
    r"\b(?:outline\s+only|only\s+(?:need|want).{0,12}outline)\b",
    re.IGNORECASE,
)
_NEGATED_PRESENTATION_REQUEST_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:不是|并非)\s*(?:要|想|需要)?"
    r"[^，。；;.!?\n]{0,24}"
    r"(?:制作|生成|创建|新建|导出|create|generate|make|build|export)"
    r"[^，。；;.!?\n]{0,32}"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)",
    re.IGNORECASE,
)
_STRONG_REFERENCE_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:解释|说明|翻译|总结|讨论|比较|研究|阅读|解读|"
    r"explain|translate|summarize|discuss|compare|study|read)",
    re.IGNORECASE,
)
_REFERENCE_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:关于|提到|引用|如何|怎么|为什么|方法|教程|文章|原文|文字|文本|"
    r"句子|概念|机制|问题|区别|含义|whether|how|why|article|text|sentence|tutorial)",
    re.IGNORECASE,
)
_QUOTED_REFERENCE_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:提到|关于|文章|原文|文字|文本|句子|提示词|quoted|article|text|sentence|prompt)",
    re.IGNORECASE,
)
_REFERENCE_EXECUTION_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:根据|按照|按(?:照)?|并|然后|再按|"
    r"\b(?:use|using|then|and)\b)",
    re.IGNORECASE,
)
_PRESENTATION_PROMPT_TARGET_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)"
    r"\s*(?:的\s*)?"
    r"(?:(?:生成|创建|编写|撰写|写|拟定|起草|generate|create|write|draft)\s*)?"
    r"(?:一份|一个|一版|个|份|a\s+)?\s*"
    r"(?:prompt(?:s)?|提示词|提示语|系统提示|指令|需求描述|任务描述)",
    re.IGNORECASE,
)
_PROMPT_FOR_PRESENTATION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:生成|创建|编写|撰写|写|拟定|起草|generate|create|write|draft)"
    r"[^，。；;.!?\n]{0,24}"
    r"(?:prompt(?:s)?|提示词|提示语|系统提示|指令|需求描述|任务描述)"
    r"[^，。；;.!?\n]{0,32}"
    r"(?:用于|用来|来|以便|for|to)"
    r"[^，。；;.!?\n]{0,32}"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s+deck|slides?|presentation)",
    re.IGNORECASE,
)
_FOLLOW_ON_EXECUTION_CONNECTOR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:后|之后|然后|再|接着|随后|并(?:根据|按照|按|使用)?|"
    r"\b(?:then|afterwards|and(?:\s+then)?)\b)",
    re.IGNORECASE,
)
_REFERENCE_CONTEXT_REQUEST_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:基于|根据|按照|参考|使用|沿用|结合)\s*"
    r"(?:以上|上述|上面|前述|前面|刚才|这些|该(?:内容|提示词)|"
    r"the\s+(?:above|previous)|(?:above|previous|earlier)\s+(?:content|prompt))"
    r"|(?:以上|上述|上面|前述|前面|刚才)(?:内容|提示词|方案|回答)",
    re.IGNORECASE,
)
_REFERENCE_CONTEXT_MAX_CHARS: Final[int] = 12_000
_JSON_OBJECT_RE: Final[re.Pattern[str]] = re.compile(r"\{.*\}", re.DOTALL)
_PAGE_TOKEN_RE: Final[str] = (
    r"(?:\d{1,2}|[一二两三四五六七八九]?十[一二三四五六七八九]?|[一二两三四五六七八九])"
)
_PAGE_RANGE_RE: Final[re.Pattern[str]] = re.compile(
    rf"({_PAGE_TOKEN_RE})\s*(?:-|~|～|—|–|至|到)\s*({_PAGE_TOKEN_RE})\s*(?:页|pages?|slides?)",
    re.IGNORECASE,
)
_PAGE_COUNT_RE: Final[re.Pattern[str]] = re.compile(
    rf"({_PAGE_TOKEN_RE})\s*(?:页|pages?|slides?)",
    re.IGNORECASE,
)
_POSITIONAL_PAGE_SPAN_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?:前|首|最前)\s*{_PAGE_TOKEN_RE}"
    rf"(?:\s*(?:-|~|～|—|–|至|到)\s*{_PAGE_TOKEN_RE})?\s*页(?:内容)?"
    rf"|\b(?:first|initial)\s+{_PAGE_TOKEN_RE}"
    rf"(?:\s*(?:-|~|to|through)\s*{_PAGE_TOKEN_RE})?\s*(?:pages?|slides?)\b",
    re.IGNORECASE,
)
_ROLE_PREFIXES: Final[tuple[str, ...]] = (
    "我是",
    "我作为",
    "作为",
    "以",
    "身为",
    "本人是",
    "我们的角色是",
    "我的身份是",
)
_AUDIENCE_PREFIXES: Final[tuple[str, ...]] = (
    "给",
    "给到",
    "发给",
    "汇报给",
    "面向",
    "针对",
    "向",
    "适合",
)
_AUDIENCE_SUFFIXES: Final[tuple[str, ...]] = (
    "看",
    "汇报",
    "展示",
    "讲解",
    "介绍",
    "演示",
)
_RICH_PROMPT_MIN_CHARS: Final[int] = 80
_RICH_PROMPT_SIGNALS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?:内容要求|每页|目录|章节|结构|主题|核心观点|大纲)", re.IGNORECASE),
    re.compile(r"(?:避免|不要|不能|最多|至少|必须|不超过|只讲|只能)", re.IGNORECASE),
    re.compile(
        r"(?:风格|设计|视觉|整体感觉|调性|字号|配色|版式|图表|思维导图|关系图)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:用于|面向|适合|目标|希望|交流|汇报|分享)", re.IGNORECASE),
)


def load_presentation_preflight_config() -> dict[str, Any]:
    """Load and minimally validate the Skill-owned preflight configuration."""
    data = json.loads(_PREFLIGHT_CONFIG_PATH.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("presentation preflight config must use version 1")
    fields = data.get("fields")
    defaults = data.get("defaults")
    required_fields = data.get("required_fields")
    high_impact_fields = data.get("high_impact_fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("presentation preflight config fields must be a non-empty array")
    if (
        not isinstance(defaults, dict)
        or not isinstance(required_fields, list)
        or not isinstance(high_impact_fields, list)
    ):
        raise ValueError(
            "presentation preflight config defaults/required_fields/high_impact_fields "
            "are invalid"
        )

    field_ids: set[str] = set()
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("id"), str):
            raise ValueError("presentation preflight field is missing an id")
        field_id = field["id"]
        if field_id in field_ids:
            raise ValueError(f"duplicate presentation preflight field: {field_id}")
        field_ids.add(field_id)
        options = field.get("options")
        if not isinstance(options, list) or not options:
            raise ValueError(f"presentation preflight field has no options: {field_id}")
        if field_id == "page_count" and field.get("boundary_policy") not in {
            None,
            "prefer_range_ending_at_count",
        }:
            raise ValueError("invalid page_count boundary_policy")
        option_ids = {
            option.get("id")
            for option in options
            if isinstance(option, dict) and isinstance(option.get("id"), str)
        }
        if defaults.get(field_id) not in option_ids:
            raise ValueError(f"invalid default for presentation preflight field: {field_id}")

    if not set(required_fields).issubset(field_ids):
        raise ValueError("presentation preflight required_fields contains an unknown field")
    if not set(high_impact_fields).issubset(set(required_fields)):
        raise ValueError(
            "presentation preflight high_impact_fields must be required fields"
        )
    return copy.deepcopy(data)


PresentationRequestKind = Literal["create", "export", "continue"]


def _is_referenced_creation(
    text: str,
    creation_match: re.Match[str],
) -> bool:
    clause_start = max(
        text.rfind(separator, 0, creation_match.start())
        for separator in ("，", ",", "。", ";", "；", "\n")
    )
    clause_end_candidates = [
        index
        for separator in ("，", ",", "。", ";", "；", "\n")
        if (index := text.find(separator, creation_match.end())) >= 0
    ]
    clause_end = min(clause_end_candidates, default=len(text))
    clause = text[clause_start + 1 : clause_end]
    prefix = text[clause_start + 1 : creation_match.start()]
    if _REFERENCE_EXECUTION_PREFIX_RE.search(prefix):
        return False
    return bool(
        (_STRONG_REFERENCE_ACTION_RE.search(prefix) and _REFERENCE_MARKER_RE.search(clause))
        or _QUOTED_REFERENCE_MARKER_RE.search(prefix)
    )


def _uses_presentation_as_context(
    text: str,
    creation_match: re.Match[str],
) -> bool:
    """Return whether PPT describes where another requested artifact will be used."""
    clause_start = max(
        text.rfind(separator, 0, creation_match.start())
        for separator in ("，", ",", "。", ";", "；", "\n")
    )
    clause_end_candidates = [
        index
        for separator in ("，", ",", "。", ";", "；", "\n")
        if (index := text.find(separator, creation_match.end())) >= 0
    ]
    clause_end = min(clause_end_candidates, default=len(text))
    clause = text[clause_start + 1 : clause_end]
    return bool(
        _PRESENTATION_USAGE_CONTAINER_RE.search(clause)
        or _ARTIFACT_FOR_PRESENTATION_RE.search(clause)
    )


def _is_presentation_prompt_authoring_request(text: str) -> bool:
    """Return whether the requested artifact is a prompt, not a deck.

    A presentation word can qualify the prompt itself (for example,
    ``生成一份产品介绍 PPT 提示词``).  An explicit follow-on request to use
    that prompt to create a deck still counts as presentation execution.
    """
    matches = list(_PRESENTATION_PROMPT_TARGET_RE.finditer(text))
    matches.extend(_PROMPT_FOR_PRESENTATION_RE.finditer(text))
    if not matches:
        return False

    prompt_target = min(matches, key=lambda match: match.start())
    suffix = text[prompt_target.end() :]
    connector = _FOLLOW_ON_EXECUTION_CONNECTOR_RE.search(suffix)
    execution = _CREATE_PRESENTATION_RE.search(suffix)
    if (
        connector is not None
        and execution is not None
        and connector.start() <= execution.start()
    ):
        return False
    return True


def classify_presentation_request(
    user_text: str,
    *,
    has_existing_presentation: bool = False,
) -> PresentationRequestKind | None:
    """Classify a directly requested presentation deliverable.

    Creation and export actions must bind to the presentation target inside a
    single punctuation-delimited clause. References to creating a presentation
    inside an explanation, summary, translation, or quoted text are rejected.
    """
    text = user_text.strip()
    if not text:
        return None
    if is_meta_prompt_rewrite_request(text):
        return None
    if _NEGATED_PRESENTATION_REQUEST_RE.search(text):
        return None
    positive_text = strip_negated_format_clauses(text).casefold()
    if not any(keyword in positive_text for keyword in PRESENTATION_DELIVERY_KEYWORDS):
        return None
    if _OUTLINE_ONLY_RE.search(positive_text):
        return None
    if _is_presentation_prompt_authoring_request(positive_text):
        return None

    direct_create_match = _CREATE_PRESENTATION_RE.search(positive_text)
    request_new_match = _REQUEST_NEW_RE.search(positive_text)
    export_match = _EXPORT_PRESENTATION_RE.search(positive_text)
    continuation_match = _CONTINUE_PRESENTATION_RE.search(positive_text)
    if _INSPECT_EXISTING_RE.search(positive_text) and direct_create_match is None:
        request_new_match = None
    editing_existing = _EDIT_EXISTING_RE.search(positive_text) is not None
    if (
        editing_existing
        and not _NEW_VERSION_RE.search(positive_text)
        and continuation_match is None
    ):
        request_new_match = None
    if export_match is not None and direct_create_match is None:
        request_new_match = None

    create_matches = [
        match
        for match in (direct_create_match, request_new_match)
        if match is not None and not _uses_presentation_as_context(positive_text, match)
    ]
    create_match = min(create_matches, key=lambda match: match.start(), default=None)
    if create_match is not None and _is_referenced_creation(positive_text, create_match):
        create_match = None

    if (
        editing_existing
        and not _NEW_VERSION_RE.search(positive_text)
        and create_match is None
        and continuation_match is None
    ):
        return None
    if (
        has_existing_presentation
        and editing_existing
        and not _NEW_VERSION_RE.search(positive_text)
        and create_match is None
        and continuation_match is None
    ):
        return None
    if create_match is not None:
        return "create"
    if continuation_match is not None:
        return "continue"
    if export_match is not None:
        return "export"
    return None


def build_presentation_preflight_analysis_text(
    user_text: str,
    reference_context: str = "",
) -> str:
    """Add bounded prior context only for an explicitly referential request."""
    context = reference_context.strip()
    if not context or not _REFERENCE_CONTEXT_REQUEST_RE.search(user_text):
        return user_text
    bounded_context = context[-_REFERENCE_CONTEXT_MAX_CHARS:]
    return (
        "<presentation_reference_context>\n"
        f"{bounded_context}\n"
        "</presentation_reference_context>\n"
        "<current_request>\n"
        f"{user_text}\n"
        "</current_request>"
    )


def is_new_presentation_request(
    user_text: str,
    *,
    has_existing_presentation: bool = False,
) -> bool:
    """Return whether a turn should enter new-deck preflight."""
    return (
        classify_presentation_request(
            user_text,
            has_existing_presentation=has_existing_presentation,
        )
        == "create"
    )


def _option_ids_by_field(config: dict[str, Any]) -> dict[str, set[str]]:
    return {
        field["id"]: {
            option["id"]
            for option in field["options"]
            if isinstance(option, dict) and isinstance(option.get("id"), str)
        }
        for field in config["fields"]
    }


def _page_count_option(field: dict[str, Any], count: int) -> str | None:
    matches: list[dict[str, Any]] = []
    for option in field["options"]:
        minimum = option.get("min")
        maximum = option.get("max")
        if not isinstance(minimum, int):
            continue
        if count >= minimum and (not isinstance(maximum, int) or count <= maximum):
            matches.append(option)
    if not matches:
        return None
    if field.get("boundary_policy") == "prefer_range_ending_at_count":
        ending_match = next(
            (option for option in matches if option.get("max") == count),
            None,
        )
        if ending_match is not None:
            return ending_match["id"]
    return matches[0]["id"]


def _alias_matches(normalized_text: str, alias: str) -> bool:
    normalized_alias = alias.casefold()
    if re.fullmatch(r"[a-z0-9+-]{1,3}", normalized_alias):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])",
            normalized_text,
        ) is not None
    return normalized_alias in normalized_text


def _parse_page_token(token: str) -> int | None:
    normalized = token.strip()
    if normalized.isdigit():
        return int(normalized)
    digit_map = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if normalized == "十":
        return 10
    if normalized == "二十":
        return 20
    if normalized.startswith("十") and len(normalized) == 2:
        return 10 + digit_map.get(normalized[1], 0)
    if normalized.endswith("十") and len(normalized) == 2:
        tens = digit_map.get(normalized[0])
        return tens * 10 if tens else None
    if "十" in normalized and len(normalized) == 3:
        tens = digit_map.get(normalized[0])
        ones = digit_map.get(normalized[2])
        if tens and ones:
            return tens * 10 + ones
    return digit_map.get(normalized)


def _extract_page_count_value(normalized_text: str, field: dict[str, Any]) -> str | None:
    # A positional span such as "前 2 页内容" identifies which pages to preview;
    # it is not a request for a two-page deck. Remove it before looking for an
    # explicit total so a later phrase such as "完整版 8 页" remains authoritative.
    page_count_text = _POSITIONAL_PAGE_SPAN_RE.sub(" ", normalized_text)
    for option in field["options"]:
        if isinstance(option.get("min"), int):
            continue
        aliases = option.get("aliases")
        if isinstance(aliases, list) and any(
            isinstance(alias, str) and _alias_matches(page_count_text, alias)
            for alias in aliases
        ):
            return option["id"]

    range_match = _PAGE_RANGE_RE.search(page_count_text)
    if range_match:
        end_count = _parse_page_token(range_match.group(2))
        if end_count is not None:
            return _page_count_option(field, end_count)
    page_match = _PAGE_COUNT_RE.search(page_count_text)
    if not page_match:
        return None
    count = _parse_page_token(page_match.group(1))
    if count is None:
        return None
    return _page_count_option(field, count)


def _alias_in_context(
    normalized_text: str,
    field_id: str,
    alias: str,
) -> bool:
    normalized_alias = alias.casefold()
    if field_id == "role":
        return any(
            re.search(
                rf"{re.escape(prefix)}\s*"
                rf"[^，。；;.!?\n]{{0,12}}?{re.escape(normalized_alias)}",
                normalized_text,
            )
            for prefix in _ROLE_PREFIXES
        )
    if field_id == "audience":
        return any(
            re.search(
                rf"{re.escape(prefix)}\s*"
                rf"[^，。；;.!?\n]{{0,24}}?{re.escape(normalized_alias)}",
                normalized_text,
            )
            for prefix in _AUDIENCE_PREFIXES
        ) or any(
            re.search(rf"{re.escape(normalized_alias)}\s*{re.escape(suffix)}", normalized_text)
            for suffix in _AUDIENCE_SUFFIXES
        )
    return False


def _requires_contextual_match(field_id: str, alias: str) -> bool:
    if field_id not in {"role", "audience"}:
        return False
    if re.search(r"[a-z]", alias, re.IGNORECASE):
        return False
    return len(alias) <= 3


def infer_explicit_presentation_values(
    user_text: str,
    config: dict[str, Any],
) -> dict[str, str]:
    """Extract only values the user stated explicitly."""
    normalized = user_text.casefold()
    values: dict[str, str] = {}
    for field in config["fields"]:
        field_id = field["id"]
        if field_id == "page_count":
            option_id = _extract_page_count_value(normalized, field)
            if option_id:
                values[field_id] = option_id
            continue
        matches: list[tuple[int, str]] = []
        for option in field["options"]:
            aliases = option.get("aliases")
            if not isinstance(aliases, list):
                continue
            matched_alias_lengths = [
                len(alias)
                for alias in aliases
                if isinstance(alias, str)
                and _alias_matches(normalized, alias)
                and (
                    not _requires_contextual_match(field_id, alias)
                    or _alias_in_context(normalized, field_id, alias)
                )
            ]
            if matched_alias_lengths:
                matches.append((max(matched_alias_lengths), option["id"]))
        if matches:
            values[field_id] = max(matches, key=lambda item: item[0])[1]
    return values


def build_presentation_recommendation_prompt(
    user_text: str,
    config: dict[str, Any],
    missing_fields: list[str],
) -> str:
    """Build the compact, tool-free recommendation prompt."""
    recommendation_fields = [
        field_id for field_id in missing_fields if field_id != "page_count"
    ]
    allowed = {
        field["id"]: [
            {"id": option["id"], "label": option.get("label", option["id"])}
            for option in field["options"]
        ]
        for field in config["fields"]
        if field["id"] in recommendation_fields or field["id"] == "mode"
    }
    return (
        "根据用户的演示文稿需求，从允许值中为缺失配置选择最合适的一项。"
        "不要补写事实，不要解释，只输出一个 JSON 对象；键只能来自允许值中的字段，"
        "值必须是对应 id。\n"
        f"缺失字段：{json.dumps(recommendation_fields, ensure_ascii=False)}\n"
        f"允许值：{json.dumps(allowed, ensure_ascii=False)}\n"
        f"用户需求：{user_text}"
    )


def parse_presentation_recommendations(
    raw_text: str,
    config: dict[str, Any],
) -> dict[str, str]:
    """Parse and constrain a lightweight-model recommendation."""
    match = _JSON_OBJECT_RE.search(raw_text.strip())
    if not match:
        return {}
    try:
        candidate = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(candidate, dict):
        return {}
    allowed = _option_ids_by_field(config)
    return {
        field_id: value
        for field_id, value in candidate.items()
        if isinstance(field_id, str)
        and isinstance(value, str)
        and value in allowed.get(field_id, set())
    }


def _is_rich_presentation_prompt(user_text: str) -> bool:
    compact_text = re.sub(r"\s+", "", user_text)
    if len(compact_text) < _RICH_PROMPT_MIN_CHARS:
        return False
    signal_count = sum(
        pattern.search(user_text) is not None for pattern in _RICH_PROMPT_SIGNALS
    )
    return signal_count >= 2 or len(compact_text) >= _RICH_PROMPT_MIN_CHARS * 2


def _should_show_preflight(
    user_text: str,
    config: dict[str, Any],
    missing_fields: list[str],
) -> bool:
    high_impact_missing = [
        field_id
        for field_id in config["high_impact_fields"]
        if field_id in missing_fields
    ]
    if not high_impact_missing:
        return False
    if len(high_impact_missing) > 1:
        return True
    return not _is_rich_presentation_prompt(user_text)


def build_presentation_preflight_result(
    user_text: str,
    *,
    model_text: str = "",
    has_existing_presentation: bool = False,
    reference_context: str = "",
) -> dict[str, Any]:
    """Build the normalized host response with safe defaults."""
    config = load_presentation_preflight_config()
    if not is_new_presentation_request(
        user_text,
        has_existing_presentation=has_existing_presentation,
    ):
        return {
            "matched": False,
            "shouldShow": False,
            "schemaVersion": config["version"],
        }

    analysis_text = build_presentation_preflight_analysis_text(
        user_text,
        reference_context,
    )
    explicit_values = infer_explicit_presentation_values(analysis_text, config)
    missing_fields = [
        field_id
        for field_id in config["required_fields"]
        if field_id not in explicit_values
    ]
    recommendations = parse_presentation_recommendations(model_text, config)
    if "page_count" in missing_fields:
        recommendations.pop("page_count", None)
    values = dict(config["defaults"])
    values.update(recommendations)
    values.update(explicit_values)
    sources = {
        field["id"]: (
            "explicit"
            if field["id"] in explicit_values
            else "recommended"
            if field["id"] in recommendations
            else "default"
        )
        for field in config["fields"]
    }
    return {
        "matched": True,
        "shouldShow": _should_show_preflight(analysis_text, config, missing_fields),
        "schemaVersion": config["version"],
        "requiredFields": list(config["required_fields"]),
        "missingFields": missing_fields,
        "fields": config["fields"],
        "values": values,
        "sources": sources,
        "autoStartSeconds": 30,
    }
