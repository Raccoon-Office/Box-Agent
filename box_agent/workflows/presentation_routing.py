"""Completion-gate routing for presentation deliverables."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from ..artifacts import OUTPUT_SUBDIR
from ..config import ToolLimitsConfig
from ..delivery import strip_negated_format_clauses
from ..loop_guards import (
    FINAL_SUMMARY_EXCLUDED_TOOLS,
    CompletionGate,
    artifact_signatures_for_globs,
)
from .presentation_contract import (
    PRESENTATION_DELIVERY_KEYWORDS,
    RESEARCH_MODE_OPTION,
    RESEARCH_ROUND_LIMIT_OPTION,
    WORKFLOW_KIND,
)
from .presentation_preflight import classify_presentation_request

_PPTX_SKILL_REFERENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(?<![a-z0-9])pptx(?![a-z0-9])\s*(?:skill|技能)"
    r"|(?:skill|技能)\s*(?<![a-z0-9])pptx(?![a-z0-9]))",
    re.IGNORECASE,
)
_EXPLICIT_PPTX_DELIVERY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"(?:导出|交付|输出|生成|制作|创建|保存|另存为|转换|转成)"
    r"[^，。；;.!?\n]{0,80}(?:\.pptx(?![a-z0-9])|(?<![a-z0-9])pptx(?![a-z0-9]))"
    r"|(?:export|deliver|output|generate|create|save|convert)"
    r"[^,.;!?\n]{0,80}(?:\.pptx(?![a-z0-9])|(?<![a-z0-9])pptx(?![a-z0-9]))"
    r"|(?:\.pptx(?![a-z0-9])|(?<![a-z0-9])pptx(?![a-z0-9]))"
    r"[^，。；;.!?\n]{0,12}(?:文件|格式|版本|交付物|file|format|version)"
    r"|\.pptx(?![a-z0-9])"
    r")",
    re.IGNORECASE,
)
_EXPLICIT_PPT_FILE_DELIVERY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"(?:导出|交付|保存|另存为|转换|转成)"
    r"[^，。；;.!?\n]{0,24}(?:(?<![a-z0-9])ppt(?![a-z0-9])|powerpoint)"
    r"(?:\s*(?:文件|格式|版本|交付物))?"
    r"|(?:输出|生成|制作|创建)"
    r"[^，。；;.!?\n]{0,24}(?:(?<![a-z0-9])ppt(?![a-z0-9])|powerpoint)"
    r"\s*(?:文件|格式|版本|交付物)"
    r"|(?:(?<![a-z0-9])ppt(?![a-z0-9])|powerpoint)"
    r"\s*(?:文件|格式|版本|交付物)"
    r"|(?:export|deliver|save|convert)"
    r"[^,.;!?\n]{0,24}(?:(?<![a-z0-9])ppt(?![a-z0-9])|powerpoint)"
    r"(?:\s*(?:file|format|version|deliverable))?"
    r"|(?:output|generate|create)"
    r"[^,.;!?\n]{0,24}(?:(?<![a-z0-9])ppt(?![a-z0-9])|powerpoint)"
    r"\s*(?:file|format|version|deliverable)"
    r"|(?:(?<![a-z0-9])ppt(?![a-z0-9])|powerpoint)"
    r"\s*(?:file|format|version|deliverable)"
    r")",
    re.IGNORECASE,
)

_RESEARCH_SOURCE_FIRST_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:不要(?:联网|搜索)|不(?:要|用)搜索|仅(?:根据|基于)|只(?:根据|基于)|"
    r"基于(?:附件|文件|我提供)|根据(?:附件|文件)|我们的|我司|本公司|内部|"
    r"新员工|入职培训|no\s+(?:web|search)|without\s+(?:web|search)|"
    r"use\s+only\s+(?:the\s+)?(?:provided|attached))",
    re.IGNORECASE,
)
_RESEARCH_SEARCH_FORBIDDEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"不要(?:联网|搜索)|不(?:要|用)搜索|"
    r"仅(?:根据|基于)|只(?:根据|基于)|"
    r"no\s+(?:web|search)|without\s+(?:web|search)|"
    r"use\s+only\s+(?:the\s+)?(?:provided|attached)"
    r")",
    re.IGNORECASE,
)
_RESEARCH_CREATIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:创意(?:视觉|插画|海报)?|插画|海报|氛围感|想象式|艺术化|视觉故事|"
    r"image[- ]rich|illustration|poster|purely\s+visual|atmospheric)",
    re.IGNORECASE,
)
_SOLUTION_DESIGN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:系统架构|技术架构|架构图|系统方案|技术方案|解决方案|系统设计|"
    r"架构设计|系统集成|集成方案|数据管道|数据处理流程|业务流程|事件流|"
    r"solution\s+(?:architecture|design)|system\s+(?:architecture|design)|"
    r"technical\s+architecture|integration\s+(?:architecture|design)|"
    r"data\s+(?:pipeline|flow)|event\s+flow)",
    re.IGNORECASE,
)
_EXTERNAL_EVIDENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:市场(?:规模|分析|研究|趋势|格局)|行业(?:分析|研究|趋势|现状|格局)|"
    r"竞品|竞争格局|商业价值|投资价值|市占率|增长率|政策|法规|监管|"
    r"最新(?:数据|趋势|进展|动态)|调研数据|统计数据|引用|出处|来源|证据|"
    r"market\s+(?:size|analysis|research|trend)|industry\s+(?:analysis|research|trend)|"
    r"competitive\s+(?:analysis|landscape)|business\s+value|investment\s+case|"
    r"market\s+share|growth\s+rate|policy|regulation|citation|sources?|evidence)",
    re.IGNORECASE,
)
_EXPLICIT_EXTERNAL_RESEARCH_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"搜索|检索|查找|核实|验证|查证|"
    r"(?:开展|进行|先做|先行|请做|帮我做|需要|要求)\s*(?:一次|相关|专项)?调研|"
    r"调研(?:一下|后|并|再|然后|资料|数据|市场|行业|竞品|公司|产品)|"
    r"(?:使用|采用|优先使用|优先采用)?(?:官方|权威)(?:来源|资料|网站|数据)?|"
    r"补充(?:外部)?资料|补充来源|引用来源|引用资料|"
    r"\b(?:search|research|investigate|verify|fact[- ]?check|look\s+up)\b|"
    r"\b(?:use|prefer|cite)\s+(?:official|authoritative|primary)\s+sources?\b|"
    r"\b(?:add|include|provide)\s+(?:citations?|sources?|references?)\b"
    r")",
    re.IGNORECASE,
)
_PAGE_PLAN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:第\s*\d+\s*页|slide\s*\d+\s*[:：.-])",
    re.IGNORECASE,
)
_HOST_PRESENTATION_CONFIG_RE: Final[re.Pattern[str]] = re.compile(
    r"<presentation_config\b[^>]*>.*?</presentation_config>",
    re.IGNORECASE | re.DOTALL,
)
_SUCCESS_REPORT_GLOBS: Final[tuple[str, ...]] = (
    f"{OUTPUT_SUBDIR}/**/qa/outline_check.json",
    f"{OUTPUT_SUBDIR}/**/qa/deck_contract.json",
    f"{OUTPUT_SUBDIR}/**/qa/deck_spec.json",
    f"{OUTPUT_SUBDIR}/**/qa/image_manifest.json",
    f"{OUTPUT_SUBDIR}/**/qa/html_self_check.json",
    f"{OUTPUT_SUBDIR}/**/qa/runtime_probe.json",
)
_CONTROLLED_ARTIFACT_GLOBS: Final[tuple[str, ...]] = (
    f"{OUTPUT_SUBDIR}/**/*.html",
    f"{OUTPUT_SUBDIR}/**/*.htm",
)
_PRESENTATION_BUDGET_EXEMPT_TOOLS: Final[frozenset[str]] = (
    FINAL_SUMMARY_EXCLUDED_TOOLS
    | frozenset({"request_user_input", "request_user_decision"})
)


def _research_mode(user_text: str) -> str:
    # Host metadata contains routing words such as ``source`` and role labels
    # such as ``市场``. It informs deck planning, but must not masquerade as the
    # user's request to search or research external evidence.
    text = _HOST_PRESENTATION_CONFIG_RE.sub(" ", user_text).strip()
    if _RESEARCH_SEARCH_FORBIDDEN_RE.search(text):
        return "source_first"
    # An explicit request to acquire or verify external evidence outranks the
    # amount of material already present in the prompt. Long references and a
    # detailed page plan can guide the deck, but they do not satisfy a request
    # to search, fact-check, or use authoritative sources.
    if _EXPLICIT_EXTERNAL_RESEARCH_ACTION_RE.search(text):
        return "deep"
    if _RESEARCH_SOURCE_FIRST_RE.search(text):
        return "source_first"
    if _RESEARCH_CREATIVE_RE.search(text):
        return "creative"
    if len(_PAGE_PLAN_RE.findall(text)) >= 2:
        return "content_ready"
    substantive_lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullet_lines = [
        line
        for line in substantive_lines
        if re.match(r"^(?:[-*•]|\d+[.、)])\s*", line)
    ]
    if len(bullet_lines) >= 3:
        return "content_ready"
    if _SOLUTION_DESIGN_RE.search(text) and not _EXTERNAL_EVIDENCE_RE.search(text):
        return "content_ready"
    topic = re.sub(
        r"(?:做|制作|生成|创建|输出|导出|帮我|请|一份|一个|可编辑|"
        r"pptx?|powerpoint|presentation|演示文稿|幻灯片|slide\s*deck|slides?|"
        r"\d+\s*页|\d+\s*[x×]\s*\d+)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    topic = re.sub(r"[\s，。；;:：,.!?！？()（）]+", "", topic)
    if len(topic) < 4:
        return "auto"
    if len(substantive_lines) <= 2 and len(text) <= 400:
        return "deep"
    return "auto"


def _explicit_pptx_delivery_requested(positive_format_text: str) -> bool:
    """Distinguish a requested PowerPoint file from a reference to the Skill."""
    delivery_text = _PPTX_SKILL_REFERENCE_RE.sub(" ", positive_format_text)
    return (
        _EXPLICIT_PPTX_DELIVERY_RE.search(delivery_text) is not None
        or _EXPLICIT_PPT_FILE_DELIVERY_RE.search(delivery_text) is not None
    )


def build_presentation_completion_gate(
    user_text: str,
    workspace_dir: str | Path,
    *,
    confirmed_presentation: bool = False,
    tool_limits: ToolLimitsConfig | None = None,
) -> CompletionGate | None:
    """Build the presentation workflow gate, or return None for another router."""
    if not confirmed_presentation and classify_presentation_request(user_text) is None:
        return None
    text = user_text.strip().lower()
    positive_format_text = strip_negated_format_clauses(text)

    explicit_pptx = _explicit_pptx_delivery_requested(positive_format_text)
    patterns = (
        (f"{OUTPUT_SUBDIR}/**/*.pptx",)
        if explicit_pptx
        else _CONTROLLED_ARTIFACT_GLOBS
    )
    workspace = str(workspace_dir)
    if explicit_pptx:
        return CompletionGate(
            required_changed_artifact_globs=patterns,
            baseline_artifact_signatures=artifact_signatures_for_globs(
                patterns,
                workspace,
            ),
            max_continuations=3,
            deadline_seconds=900.0,
        )

    research_mode = _research_mode(user_text)
    effective_tool_limits = tool_limits or ToolLimitsConfig()
    limits = effective_tool_limits.presentation
    return CompletionGate(
        required_changed_artifact_globs=patterns,
        baseline_artifact_signatures=artifact_signatures_for_globs(
            patterns,
            workspace,
        ),
        required_success_report_globs=_SUCCESS_REPORT_GLOBS,
        success_report_artifact_suffixes=frozenset({".html", ".htm"}),
        baseline_success_report_signatures=artifact_signatures_for_globs(
            _SUCCESS_REPORT_GLOBS,
            workspace,
        ),
        max_continuations=3,
        deadline_seconds=900.0,
        max_tool_calls=(
            limits.deep_research_max_tool_calls
            if research_mode == "deep"
            else limits.max_tool_calls
        ),
        web_search_total_limit=(
            effective_tool_limits.web_search.deep_research_total_calls
            if research_mode == "deep"
            else None
        ),
        budget_exempt_tools=_PRESENTATION_BUDGET_EXEMPT_TOOLS,
        completion_reserve_tool_calls=limits.completion_reserve_calls,
        pause_tools=frozenset({"request_user_input", "request_user_decision"}),
        workflow_checkpoint_kind=WORKFLOW_KIND,
        workflow_options={
            RESEARCH_MODE_OPTION: research_mode,
            RESEARCH_ROUND_LIMIT_OPTION: limits.research_rounds,
        },
    )
