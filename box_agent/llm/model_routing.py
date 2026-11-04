"""Pure model-catalog routing helpers for automatic child-agent selection."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Iterable
from uuid import uuid4

from ..config import derive_context_token_limit

logger = logging.getLogger(__name__)


class ModelRoutingError(ValueError):
    """A routing rejection with a bounded, task-content-free decision record."""

    def __init__(self, message: str, diagnostic: dict[str, Any]):
        super().__init__(message)
        self.diagnostic = diagnostic


def _record_routing(record: dict[str, Any]) -> None:
    """Use existing trace/log transports without making logging a dependency."""
    try:
        from ..session_trace import emit_session_trace

        emit_session_trace("model.routing", data=record)
    except Exception:
        pass
    try:
        logger.info("model.routing %s", json.dumps(record, ensure_ascii=False))
    except Exception:
        pass


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
_TRANSLATION_REQUEST_RE = re.compile(
    r"^(?:(?:请(?:帮我)?|帮我|麻烦你?)\s*)?(?:(?:把|将)[^:：\n]{0,80})?翻译|"
    r"^(?:please\s+)?translate\b", re.I,
)
_TRANSLATION_QUOTES = {'“': '”', '「': '」', '『': '』', '"': '"', "'": "'"}
_TRANSLATION_COMPOUND_RE = re.compile(r"然后|另外|接着|同时|并|以及|\b(?:and|then|also)\b", re.I)
_TRANSLATION_FOLLOWUP_RE = re.compile(
    r"(?:然后|另外|接着|同时)[，,\s]*(?:请|帮我)?|"
    r"(?:并(?:且)?|再|以及)\s*(?:分析|比较|评估|解释|排查|修复|重构|编写|研究)|"
    r"[\r\n。！？.!?；;]\s*(?:请|帮我|分析|解释|排查|修复|重构|编写|研究|"
    r"(?:explain|fix|debug|analy[sz]e|refactor|implement|research)\b)|"
    r"\b(?:then|also|and)\s+(?:please\s+)?(?:fix|debug|analy[sz]e|refactor|implement|research)\b", re.I,
)


def _routing_task_text(task: str) -> str:
    """Exclude clearly delimited translation source from lexical task signals only."""
    # Find one paired span without repeatedly scanning an unterminated quote.
    opening = re.search(r'''[“「『"']''', task)
    if opening:
        end = task.find(_TRANSLATION_QUOTES[opening.group()], opening.end())
        if end >= 0:
            instruction = (task[:opening.start()] + " " + task[end + 1:]).strip()
            if (_TRANSLATION_REQUEST_RE.search(instruction)
                    and not _TRANSLATION_COMPOUND_RE.search(instruction)
                    and not _TRANSLATION_FOLLOWUP_RE.search(instruction)):
                return instruction
    parts = re.split(r"[:：]", task, maxsplit=1)
    if len(parts) == 2:
        instruction, source = (part.strip() for part in parts)
        if (source and _TRANSLATION_REQUEST_RE.search(instruction)
                and not _TRANSLATION_COMPOUND_RE.search(instruction)
                and not _TRANSLATION_FOLLOWUP_RE.search(instruction)
                and not source.startswith(('“', '「', '『', '"', "'", '`'))
                and "\n" not in source and "\r" not in source
                and not _TRANSLATION_FOLLOWUP_RE.search(source)):
            return instruction
    return task


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
    max_output_tokens_cap: int | None = None,
    fallback_max_output_tokens: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Choose one allowlisted model for an isolated child task."""

    pool = list(candidates)
    if not pool:
        return None, {"mode": "inherit", "reason": "no_auto_model_pool"}

    file_names = list(files)
    classification_task = _routing_task_text(task)
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
            classification_task,
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
    advanced_task = bool(_ADVANCED_TASK_RE.search(classification_task)) or bool(
        task_tag_set & _ADVANCED_TASK_TAGS
    )
    standard_task = (
        bool(_STANDARD_TASK_RE.search(classification_task))
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
    if max_output_tokens_cap is not None and _positive_int(max_output_tokens_cap) is None:
        raise ValueError("max_output_tokens_cap must be positive")

    def input_budget(model: dict[str, Any]) -> int:
        context_window = model["contextWindow"]
        output_tokens = _positive_int(model.get("maxTokens")) or _positive_int(
            fallback_max_output_tokens
        )
        if max_output_tokens_cap is not None:
            output_tokens = (
                min(output_tokens, max_output_tokens_cap)
                if output_tokens is not None
                else max_output_tokens_cap
            )
        if output_tokens is None:
            return context_window * 4 // 5
        if context_window - output_tokens <= 1:
            return 0
        return derive_context_token_limit(context_window, output_tokens)

    known_context_models = [
        model for model in pool if _positive_int(model.get("contextWindow")) is not None
    ]
    # Entirely missing limits retain legacy routing, but unknown candidates cannot
    # bypass a known input budget. Never fall back to an insufficient window.
    if known_context_models:
        context_pool = [
            model
            for model in known_context_models
            if resolved_input_tokens <= input_budget(model)
        ]
    else:
        context_pool = pool
    required_tags = set(resolved_task_tags)

    def score_breakdown(model: dict[str, Any]) -> dict[str, int]:
        tags = set(model.get("tags", ()))
        return {
            "tags": len(tags & required_tags) * 100,
            "ability": int(model.get("abilityLevel", 1)) * (20 if required_ability > 1 else -5),
            "fast": 25 if required_ability <= 1 and "fast" in tags else 0,
            "default": 0,
        }

    def score(model: dict[str, Any]) -> int:
        return sum(score_breakdown(model).values())

    def diagnostic(reason: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
        shown = pool[:64]
        if selected is not None and all(model is not selected for model in shown):
            shown[-1] = selected
        candidates = []
        for model in shown:
            known = _positive_int(model.get("contextWindow")) is not None
            reasons = []
            if model not in context_pool:
                reasons.append("context_capacity" if known else "context_unknown")
            if model.get("abilityLevel", 1) < required_ability:
                reasons.append("ability")
            if "vision" in required_tags and "vision" not in model.get("tags", ()):
                reasons.append("vision")
            candidates.append(
                {
                    "model": str(model["model"])[:200],
                    "ability_level": model.get("abilityLevel", 1),
                    **(
                        {"context_window": model["contextWindow"], "input_token_budget": input_budget(model)}
                        if known else {}
                    ),
                    "matched_tags": [
                        tag[:64] for tag in sorted(set(model.get("tags", ())) & required_tags)[:32]
                    ],
                    "rejection_reasons": reasons,
                    "score": None if reasons else score(model),
                    "score_breakdown": score_breakdown(model),
                    "selected": model is selected,
                }
            )
        return {
            "schema_version": 1,
            "mode": "auto",
            "status": "selected" if selected is not None else "rejected",
            "reason": reason,
            **({"selected_model": str(selected["model"])[:200]} if selected is not None else {}),
            "task_tags": [tag[:64] for tag in resolved_task_tags[:32]],
            "required_ability_level": required_ability,
            "minimum_ability_level": required_ability,
            "estimated_input_tokens": resolved_input_tokens,
            "context_capacity": "estimated" if known_context_models else "unknown",
            **(
                {"input_token_budget": input_budget(selected)}
                if selected is not None and known_context_models else {}
            ),
            "candidate_count": len(pool),
            "candidates_truncated": len(pool) - len(shown),
            "candidates": candidates,
        }

    if not context_pool:
        raise ModelRoutingError(
            "AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED: "
            f"estimated_input_tokens={resolved_input_tokens}",
            diagnostic("no_context_candidate"),
        )
    ability_pool = [
        model for model in context_pool if model.get("abilityLevel", 1) >= required_ability
    ]
    if not ability_pool:
        raise ModelRoutingError(
            f"no auto model satisfies required ability level: {required_ability}",
            diagnostic("no_ability_candidate"),
        )

    if "vision" in required_tags:
        ability_pool = [
            model for model in ability_pool if "vision" in set(model.get("tags", ()))
        ]
        if not ability_pool:
            raise ModelRoutingError(
                "no auto model supports required capability: vision",
                diagnostic("no_vision_candidate"),
            )

    selected = max(enumerate(ability_pool), key=lambda item: (score(item[1]), -item[0]))[1]
    tied = sum(score(model) == score(selected) for model in ability_pool) > 1
    return selected, diagnostic("catalog_order_tiebreak" if tied else "highest_score", selected)


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
    raw_model = getattr(llm, "model", "")
    current_model = raw_model.strip() if isinstance(raw_model, str) else ""
    routing_id = uuid4().hex

    def record(phase: str, decision: dict[str, Any], **fields: Any) -> None:
        _record_routing(
            {
                "schema_version": 1,
                "engine": "agent",
                "routing_id": routing_id,
                "strategy": strategy[:64],
                "phase": phase,
                "previous_model": current_model[:200],
                "decision": decision,
                **fields,
            }
        )

    try:
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
            max_output_tokens_cap=max_output_tokens_cap,
            fallback_max_output_tokens=_positive_int(getattr(llm, "max_output_tokens", None)),
        )
    except ModelRoutingError as exc:
        record("decision", exc.diagnostic)
        raise
    record("decision", diagnostic)
    if selected is None and max_output_tokens_cap is None:
        record("inherited", diagnostic, actual_model=current_model[:200])
        return llm, diagnostic

    target_model = str((selected or {}).get("model") or current_model).strip()
    selected_limit = (selected or {}).get("maxTokens")
    target_limit = selected_limit if isinstance(selected_limit, int) else None
    if max_output_tokens_cap is not None:
        if max_output_tokens_cap <= 0:
            raise ValueError("max_output_tokens_cap must be positive")
        if target_limit is None:
            target_limit = _positive_int(getattr(llm, "max_output_tokens", None))
        target_limit = (
            min(target_limit, max_output_tokens_cap)
            if target_limit is not None
            else max_output_tokens_cap
        )

    if not target_model:
        record("inherited", diagnostic, binding_reason="current_model_unavailable", actual_model=current_model[:200])
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
        if diagnostic.get("context_capacity") == "estimated":
            record("binding_failed", diagnostic, binding_reason="model_clone_unavailable")
            raise ModelRoutingError("AUTO_MODEL_BINDING_FAILED: model_clone_unavailable", diagnostic)
        record("inherited", diagnostic, binding_reason="model_clone_unavailable", actual_model=current_model[:200])
        return llm, {
            **diagnostic,
            "mode": "inherit",
            "reason": "model_clone_unavailable",
        }
    try:
        client = clone_for_model(target_model, max_output_tokens=target_limit)
        selected_window = _positive_int((selected or {}).get("contextWindow"))
        if selected_window is not None:
            try:
                client.context_window = selected_window
            except AttributeError as exc:
                raise ValueError("selected model context window could not be bound") from exc
        record(
            "bound", diagnostic,
            actual_model=_clean_model_name(getattr(client, "model", "")),
            max_output_tokens=_positive_int(getattr(client, "max_output_tokens", None)),
        )
        return (
            client,
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
        if diagnostic.get("context_capacity") == "estimated":
            # The original client's capacity/output budget was not the one selected.
            record("binding_failed", diagnostic, binding_reason="model_clone_failed")
            raise ModelRoutingError("AUTO_MODEL_BINDING_FAILED: model_clone_failed", diagnostic) from exc
        record("inherited", diagnostic, binding_reason="model_clone_failed", actual_model=current_model[:200])
        return llm, {
            **diagnostic,
            "mode": "inherit",
            "reason": "model_clone_failed",
            "error": str(exc),
        }
    except Exception:
        record("binding_failed", diagnostic, binding_reason="model_clone_failed")
        raise
