"""Pure model-catalog routing helpers for automatic child-agent selection."""

from __future__ import annotations

import math
import re
from typing import Any, Iterable


_TASK_TAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "vision",
        re.compile(
            r"inspect_images|看图|识图|分析.{0,6}(?:图片|图像|照片)|"
            r"(?:读取|查看|检查|提取|统计).{0,80}(?:图片|图像|截图|照片|[^\s]+\.(?:avif|bmp|gif|heic|heif|jpe?g|png|tiff?|webp))|"
            r"(?:图片|图像|截图|照片).{0,12}(?:内容|文字|文本|第一行|气泡|识别|分析)|视觉识别|"
            r"vision|image recognition|analy[sz]e.{0,8}image|screenshot|photo",
            re.I,
        ),
    ),
    ("html", re.compile(r"HTML|网页|网站|webpage|website", re.I)),
    ("frontend", re.compile(r"前端|页面|组件|样式|CSS|React|Vue|frontend", re.I)),
    ("visualization", re.compile(r"可视化|图表|dashboard|chart|visualization", re.I)),
    ("code", re.compile(r"代码|编程|开发|程序|脚本|code|coding|develop|program|script", re.I)),
    ("debug", re.compile(r"调试|排查|修复|报错|错误|bug|debug|fix|error", re.I)),
    ("data-analysis", re.compile(r"数据分析|数据|表格|统计|CSV|Excel|SQL|data|spreadsheet", re.I)),
    ("analysis", re.compile(r"分析|比较|评估|综合判断|analysis|compare|evaluate", re.I)),
    ("reasoning", re.compile(r"推理|论证|逻辑|证明|reasoning|reason|proof", re.I)),
    ("office", re.compile(r"办公|邮件|纪要|公文|office|email|minutes", re.I)),
    ("presentation", re.compile(r"PPT|PPTX|演示文稿|幻灯片|presentation|slides?", re.I)),
    ("research", re.compile(r"研究|调研|搜索|联网|资料|research|search|investigate", re.I)),
    ("summary", re.compile(r"总结|摘要|概括|提炼|summary|summarize", re.I)),
    ("rewrite", re.compile(r"改写|润色|校对|翻译|rewrite|polish|proofread|translate", re.I)),
    ("document", re.compile(r"文档|文件|报告|合同|PDF|Word|document|report|file", re.I)),
)
_TASK_TAG_ALIASES: dict[str, tuple[str, ...]] = {
    "frontend": ("html",),
    "visualization": ("analysis",),
    "data-analysis": ("analysis",),
    "presentation": ("office",),
    "research": ("analysis",),
    "document": ("office",),
}
_ADVANCED_TASK_RE = re.compile(
    r"分析|比较|评估|研究|调研|搜索|联网|数据|表格|图表|可视化|代码|编程|开发|项目|网站|网页|应用|调试|排查|修复|报错|错误|"
    r"重构|测试|数据库|架构|推理|论证|逻辑|证明|工作流|多步骤|analysis|compare|evaluate|research|search|data|chart|code|develop|debug|fix|error|refactor|test|database|architecture|reasoning|proof|workflow|multi-step",
    re.I,
)
_STANDARD_TASK_RE = re.compile(
    r"办公|邮件|纪要|公文|报告|文档|文件|合同|PDF|Word|PPT|PPTX|演示文稿|幻灯片|方案|生成图片|生图|海报|"
    r"office|email|minutes|report|document|file|presentation|slides?",
    re.I,
)
_ADVANCED_TASK_TAGS = {
    "analysis",
    "code",
    "data-analysis",
    "debug",
    "frontend",
    "reasoning",
    "research",
    "vision",
    "visualization",
}
_STANDARD_TASK_TAGS = {"document", "html", "office", "presentation"}
_IMAGE_FILE_RE = re.compile(r"\.(?:avif|bmp|gif|heic|heif|jpe?g|png|tiff?|webp)$", re.I)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _clean_model_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    model = value.strip()
    if not model or len(model) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in model):
        return ""
    return model


def _clean_tags(value: Any) -> list[str] | None:
    if not isinstance(value, list) or len(value) > 32:
        return None
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        tag = item.strip().lower()
        if not tag or len(tag) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in tag):
            return None
        if tag in {"image", "image-understanding"}:
            tag = "vision"
        if tag not in tags:
            tags.append(tag)
    return tags


def normalize_auto_routing(value: Any) -> dict[str, list[dict[str, Any]]] | None:
    """Validate the host-owned automatic model pool carried by ACP metadata."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("llm_binding.autoRouting is invalid")
    raw_models = value.get("models")
    if not isinstance(raw_models, list) or not raw_models or len(raw_models) > 64:
        raise ValueError("llm_binding.autoRouting is invalid")

    seen: set[str] = set()
    models: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            raise ValueError("llm_binding.autoRouting is invalid")
        model = _clean_model_name(raw.get("model"))
        tags = _clean_tags(raw.get("tags"))
        ability_level = _positive_int(raw.get("abilityLevel", raw.get("ability_level")))
        context_window = raw.get("contextWindow", raw.get("context_window"))
        max_tokens = raw.get("maxTokens", raw.get("max_tokens"))
        if (
            not model
            or model in seen
            or tags is None
            or ability_level is None
            or ability_level > 10
            or (context_window is not None and _positive_int(context_window) is None)
            or (max_tokens is not None and _positive_int(max_tokens) is None)
        ):
            raise ValueError("llm_binding.autoRouting is invalid")
        seen.add(model)
        models.append(
            {
                "model": model,
                "tags": tags,
                "abilityLevel": ability_level,
                **({"contextWindow": context_window} if context_window is not None else {}),
                **({"maxTokens": max_tokens} if max_tokens is not None else {}),
            }
        )
    return {"models": models}


def _task_tags(
    task: str,
    *,
    required_tools: Iterable[str] = (),
    skills: Iterable[str] = (),
    files: Iterable[str] = (),
) -> list[str]:
    tags: set[str] = set()
    for tag, pattern in _TASK_TAG_PATTERNS:
        if pattern.search(task):
            tags.add(tag)

    tool_names = {name.strip().lower() for name in required_tools if isinstance(name, str)}
    skill_names = {name.strip().lower() for name in skills if isinstance(name, str)}
    file_names = [name for name in files if isinstance(name, str)]
    if any("web" in name or "browser" in name for name in tool_names):
        tags.add("research")
    if any(name in {"execute_code", "jupyter", "python"} for name in tool_names):
        tags.update({"code", "data-analysis"})
    if any(name in {"bash", "write_file", "edit_file", "apply_patch"} for name in tool_names):
        tags.add("code")
    if any("ppt" in name or "slide" in name for name in skill_names):
        tags.add("presentation")
    if any("spreadsheet" in name or "data" in name for name in skill_names):
        tags.add("data-analysis")
    if file_names:
        tags.add("document")
    if any(_IMAGE_FILE_RE.search(name) for name in file_names):
        tags.add("vision")
    if not tags:
        tags.update({"general", "chat"})
    return sorted(tags)


def select_auto_model(
    candidates: Iterable[dict[str, Any]],
    *,
    task: str,
    strategy: str = "general_loop",
    required_tools: Iterable[str] = (),
    skills: Iterable[str] = (),
    files: Iterable[str] = (),
    task_tags: Iterable[str] | None = None,
    required_ability_level: int | None = None,
    estimated_input_tokens: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Choose one allowlisted model for an isolated child task."""

    pool = list(candidates)
    if not pool:
        return None, {"mode": "inherit", "reason": "no_auto_model_pool"}

    file_names = list(files)
    resolved_task_tags = (
        sorted(
            {
                tag.strip().lower()
                for tag in task_tags
                if isinstance(tag, str) and tag.strip()
            }
        )
        if task_tags is not None
        else _task_tags(
            task,
            required_tools=required_tools,
            skills=skills,
            files=file_names,
        )
    )
    if not resolved_task_tags:
        resolved_task_tags = ["general", "chat"]
    expanded_task_tags = set(resolved_task_tags)
    for tag in tuple(expanded_task_tags):
        expanded_task_tags.update(_TASK_TAG_ALIASES.get(tag, ()))
    resolved_task_tags = sorted(expanded_task_tags)
    task_tag_set = set(resolved_task_tags)
    advanced_task = bool(_ADVANCED_TASK_RE.search(task)) or bool(
        task_tag_set & _ADVANCED_TASK_TAGS
    )
    standard_task = (
        bool(_STANDARD_TASK_RE.search(task))
        or bool(task_tag_set & _STANDARD_TASK_TAGS)
        or len(task) > 600
        or (strategy == "general_loop" and bool(list(required_tools)))
    )
    required_ability = (
        required_ability_level
        if isinstance(required_ability_level, int) and required_ability_level > 0
        else 3 if advanced_task else 2 if standard_task else 1
    )
    if (
        estimated_input_tokens is not None
        and _positive_int(estimated_input_tokens) is None
    ):
        raise ValueError("estimated_input_tokens must be positive")
    resolved_input_tokens = estimated_input_tokens or max(
        1,
        math.ceil(len(task) / 2) + min(len(file_names), 8) * 16_000,
    )

    known_context_models = [model for model in pool if model.get("contextWindow")]
    if estimated_input_tokens is not None and known_context_models:
        context_pool = [
            model
            for model in known_context_models
            if resolved_input_tokens <= model["contextWindow"] * 0.8
        ]
    else:
        context_pool = [
            model
            for model in pool
            if not model.get("contextWindow")
            or resolved_input_tokens <= model["contextWindow"] * 0.8
        ]
    if not context_pool:
        if known_context_models:
            largest_context_window = max(
                model["contextWindow"] for model in known_context_models
            )
            context_pool = [
                model
                for model in known_context_models
                if model["contextWindow"] == largest_context_window
            ]
        else:
            context_pool = pool
    ability_pool = [
        model for model in context_pool if model.get("abilityLevel", 1) >= required_ability
    ]
    if not ability_pool:
        raise ValueError(
            f"no auto model satisfies required ability level: {required_ability}"
        )

    required_tags = set(resolved_task_tags)
    if "vision" in required_tags:
        ability_pool = [
            model for model in ability_pool if "vision" in set(model.get("tags", ()))
        ]
        if not ability_pool:
            raise ValueError("no auto model supports required capability: vision")

    def score(model: dict[str, Any]) -> int:
        tags = set(model.get("tags", ()))
        value = len(tags & required_tags) * 100
        if required_ability > 1:
            value += int(model.get("abilityLevel", 1)) * 20
        else:
            value -= int(model.get("abilityLevel", 1)) * 5
            if "fast" in tags:
                value += 25
        return value

    selected = max(enumerate(ability_pool), key=lambda item: (score(item[1]), -item[0]))[1]
    return selected, {
        "mode": "auto",
        "selected_model": selected["model"],
        "task_tags": resolved_task_tags,
        "required_ability_level": required_ability,
        "estimated_input_tokens": resolved_input_tokens,
    }


def resolve_model_client(
    llm: Any,
    *,
    task: str,
    strategy: str = "utility",
    required_tools: Iterable[str] = (),
    skills: Iterable[str] = (),
    files: Iterable[str] = (),
    max_output_tokens_cap: int | None = None,
    auto_model_candidates: Iterable[dict[str, Any]] | None = None,
    task_tags: Iterable[str] | None = None,
    required_ability_level: int | None = None,
    estimated_input_tokens: int | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Resolve one task client without violating the session routing mode.

    A client exposes ``auto_model_candidates`` only when the host explicitly
    selected automatic routing.  In that case the existing tag/ability/context
    selector may clone an allowlisted candidate.  With no candidate pool the
    current client is locked and is never switched to a different model.
    """

    candidates = (
        auto_model_candidates
        if auto_model_candidates is not None
        else getattr(llm, "auto_model_candidates", ())
    )
    if not isinstance(candidates, (list, tuple)):
        candidates = ()
    selected, diagnostic = select_auto_model(
        candidates,
        task=task,
        strategy=strategy,
        required_tools=required_tools,
        skills=skills,
        files=files,
        task_tags=task_tags,
        required_ability_level=required_ability_level,
        estimated_input_tokens=estimated_input_tokens,
    )
    if selected is None and max_output_tokens_cap is None:
        return llm, diagnostic

    current_model = str(getattr(llm, "model", "") or "").strip()
    target_model = str((selected or {}).get("model") or current_model).strip()
    selected_limit = (selected or {}).get("maxTokens")
    target_limit = selected_limit if isinstance(selected_limit, int) else None
    if max_output_tokens_cap is not None:
        if max_output_tokens_cap <= 0:
            raise ValueError("max_output_tokens_cap must be positive")
        target_limit = (
            min(target_limit, max_output_tokens_cap)
            if target_limit is not None
            else max_output_tokens_cap
        )

    if not target_model:
        return llm, {
            **diagnostic,
            "mode": "inherit",
            "reason": "current_model_unavailable",
        }

    should_clone = selected is not None or max_output_tokens_cap is not None
    if not should_clone:
        return llm, diagnostic

    clone_for_model = getattr(llm, "for_model", None)
    if not callable(clone_for_model):
        return llm, {
            **diagnostic,
            "mode": "inherit",
            "reason": "model_clone_unavailable",
        }
    try:
        return (
            clone_for_model(target_model, max_output_tokens=target_limit),
            {
                **diagnostic,
                "selected_model": target_model,
                **(
                    {"max_output_tokens": target_limit}
                    if target_limit is not None
                    else {}
                ),
            },
        )
    except (TypeError, ValueError) as exc:
        return llm, {
            **diagnostic,
            "mode": "inherit",
            "reason": "model_clone_failed",
            "error": str(exc),
        }
