"""Shared agent execution core.

This module contains the **single source of truth** for the agent loop.
It yields structured ``AgentEvent`` objects via an ``AsyncGenerator``.
CLI, ACP, and any future consumer all drive the same generator.

No ``print()`` or ``input()`` calls live here — all I/O is delegated
to the consumer through the event stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import traceback
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tiktoken

from .events import (
    AgentEvent,
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LLMOutputEvent,
    LogFileEvent,
    MemoryProposalEvent,
    MemoryPromotionCandidate,
    PermissionRequestEvent,
    PlanSnapshotEvent,
    ProgressEvent,
    StepEnd,
    StepStart,
    StopReason,
    SubAgentEvent,
    SummarizationEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolCallResult,
    ToolCallStart,
    WebSearchEvent,
)
from .hooks import HookManager
from .logger import AgentLogger
from .llm.debug_logging import reset_llm_debug_sink, set_llm_debug_sink
from .loop_guards import (
    EMPTY_ARGS_LIMIT,
    FINAL_SUMMARY_EXCLUDED_TOOLS,
    TOOL_CALL_LIMITS,
    WEB_SEARCH_BATCH_SIZE,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOTAL_LIMIT,
    WRAPUP_REMAINING,
    CompletionGate,
    completion_gate_gaps,
    completion_gate_text,
    format_injected_message,
    looks_like_truncated_output,
    near_limit_wrapup_text,
    no_progress_wrapup_text,
    reply_is_substantial,
    tool_call_budget_message,
    tool_call_budget_wrapup_text,
    truncation_continuation_text,
)

# Re-exported for backward compatibility: ``CompletionGate`` now lives in
# ``loop_guards`` but callers historically import it from ``core``.
__all__ = ["run_agent_loop", "CompletionGate"]

_log = logging.getLogger(__name__)
from .schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from .tools.base import EventEmittingTool, Tool, ToolResult

# Type alias — consumers supply a zero-arg callable that returns True
# when the execution should be cancelled.
CancelChecker = Callable[[], bool]

_PLAN_START_TRIGGERS = (
    "先做规划",
    "先规划",
    "先做计划",
    "先给规划",
    "先给计划",
    "先给方案",
    "先出计划",
    "先出一个计划",
    "先出方案",
    "规划一下",
    "计划一下",
    "制定计划",
    "制定方案",
    "执行方案",
    "任务规划",
    "任务计划",
    "出一个计划",
    "出个计划",
    "给我一个计划",
    "给个计划",
    "做一个计划",
    "做个计划",
    "做个规划",
    "生成计划",
    "创建计划",
    "使用plan",
    "做一个plan",
    "做个plan",
    "生成plan",
    "创建plan",
    "make a plan",
    "make plan",
    "create a plan",
    "write a plan",
    "plan first",
    "planning first",
    "use plan",
    "plan mode",
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
    "不需要方案",
    "无需方案",
    "不要方案",
    "不用方案",
    "不使用plan",
    "不用plan",
    "不要plan",
    "no plan",
    "without plan",
    "without a plan",
)

_PLAN_START_KEYWORDS = ("计划", "规划")
_STANDALONE_PLAN_RE = re.compile(r"(^|[^a-z])plan([^a-z]|$)")
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

_FORCED_PLAN_GUIDANCE = (
    "Host UI requires a structured execution plan for this turn. "
    "Before giving the substantive answer, call `plan_write` with action `set` "
    "to publish the task objective, scope, steps, verification, risks, and assumptions. "
    "Keep the plan concise and relevant to the user's latest request."
)

_FORCED_PLAN_RETRY_GUIDANCE = (
    "The host is still waiting for the structured plan card. "
    "Call `plan_write` with action `set` now before continuing the answer."
)

_FORCED_PLAN_APPROVAL_GUIDANCE = (
    "Host UI requires an explicit user approval before execution. "
    "Call `plan_write` with action `set` to publish the task objective, scope, "
    "steps, verification, risks, and assumptions. Do not call execution tools "
    "such as file, bash, code, or sub-agent tools in this turn. After publishing "
    "the plan, stop and wait for the host to approve it. Do not publish a new "
    "plan when the latest user message is only a greeting, acknowledgement, "
    "thanks, or approval such as ok, continue, confirmed, 好的, 收到, or 继续 "
    "without a concrete task."
)

_PLAN_APPROVAL_SKIP_MESSAGE = (
    "Execution is paused until the user approves the published plan. "
    "Do not retry this tool yet; publish or revise the plan first."
)

_PLAN_APPROVAL_DONE_CONTENT = "计划已生成，等待用户确认后再执行。"

FINAL_SUMMARY_TOOL_CALL_THRESHOLD: Final[int] = 50


def final_summary_wrapup_text(tool_call_count: int) -> str:
    return (
        "This turn has used many visible tool calls "
        f"({tool_call_count}, threshold {FINAL_SUMMARY_TOOL_CALL_THRESHOLD}). "
        "Keep continuing only if a required deliverable is still incomplete. "
        "When you are ready to stop, the final user-visible response must be a concise conclusion, "
        "not a process log: state the result, list created/changed files or concrete outputs when relevant, "
        "mention only important caveats, and give the next action if one is needed. "
        "Do not enumerate every tool call."
    )


def final_summary_empty_retry_text(tool_call_count: int) -> str:
    return (
        "The previous natural end produced no visible final answer after a long tool-heavy turn "
        f"({tool_call_count} visible tool calls). "
        "Answer the user now with a concise final conclusion. Do not call tools unless the task is impossible "
        "to summarize without one."
    )


# Regex to match file references like [foo.png] in tool output.
_ARTIFACT_REF_RE = re.compile(r"\[([^\]\n]+\.\w{1,10})\]", re.IGNORECASE)

# Coarse classification by MIME type — exposed to hosts via ArtifactEvent.kind.
# Order matters: the first matching prefix/value wins.
_MIME_KIND_PREFIX = (
    ("image/", "image"),
    ("video/", "video"),
    ("audio/", "audio"),
    ("text/csv", "data"),
    ("text/tab-separated-values", "data"),
    ("application/json", "data"),
    ("application/x-ndjson", "data"),
    ("application/xml", "data"),
    ("text/x-python", "code"),
    ("text/x-", "code"),
    ("application/javascript", "code"),
    ("application/typescript", "code"),
    ("text/markdown", "document"),
    ("text/html", "document"),
    ("application/pdf", "document"),
    ("application/msword", "document"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml", "document"),
    ("application/vnd.ms-excel", "spreadsheet"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml", "spreadsheet"),
    ("application/vnd.ms-powerpoint", "presentation"),
    ("application/vnd.openxmlformats-officedocument.presentationml", "presentation"),
    ("application/zip", "archive"),
    ("application/x-tar", "archive"),
    ("application/gzip", "archive"),
    ("application/x-7z-compressed", "archive"),
    ("text/", "document"),
)

# Extension fallback when MIME guess returns None.
_EXT_KIND = {
    ".csv": "data", ".tsv": "data", ".json": "data", ".jsonl": "data",
    ".ndjson": "data", ".parquet": "data", ".xml": "data", ".yaml": "data", ".yml": "data",
    ".py": "code", ".js": "code", ".ts": "code", ".jsx": "code", ".tsx": "code",
    ".rs": "code", ".go": "code", ".java": "code", ".c": "code", ".cpp": "code",
    ".rb": "code", ".sh": "code",
    ".md": "document", ".rst": "document", ".html": "document", ".htm": "document",
    ".pdf": "document", ".doc": "document", ".docx": "document", ".txt": "document",
    ".xlsx": "spreadsheet", ".xls": "spreadsheet", ".ods": "spreadsheet",
    ".pptx": "presentation", ".ppt": "presentation", ".key": "presentation",
    ".zip": "archive", ".tar": "archive", ".gz": "archive", ".7z": "archive", ".rar": "archive",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".svg": "image", ".webp": "image", ".bmp": "image", ".tiff": "image",
    ".mp4": "video", ".webm": "video", ".mov": "video",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".flac": "audio",
}


def _message_text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            value = block.get("text") or block.get("content")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _latest_user_text(messages: list[Message]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            return _message_text(msg.content)
    return ""


def text_requests_plan_start(text: str) -> bool:
    normalized = text.lower()
    compact = "".join(normalized.split())
    if any(negation in compact for negation in _PLAN_START_NEGATIONS):
        return False
    return (
        any(trigger in normalized for trigger in _PLAN_START_TRIGGERS)
        or any(keyword in normalized for keyword in _PLAN_START_KEYWORDS)
        or bool(_STANDALONE_PLAN_RE.search(normalized))
    )


def text_is_short_acknowledgement(text: str) -> bool:
    compact = _SHORT_ACK_STRIP_RE.sub("", text.strip().lower())
    if not compact or len(compact) > 40:
        return False
    return compact in _SHORT_ACKNOWLEDGEMENTS


def text_is_short_non_task_reply(text: str) -> bool:
    compact = _SHORT_ACK_STRIP_RE.sub("", text.strip().lower())
    if not compact or len(compact) > 40:
        return False
    return compact in _SHORT_NON_TASK_REPLIES


def _should_emit_plan_start(
    messages: list[Message],
    tools: dict[str, Tool],
    *,
    plan_start_text: str | None = None,
) -> bool:
    if "plan_write" not in tools:
        return False
    candidate = _latest_user_text(messages) if plan_start_text is None else plan_start_text
    return text_requests_plan_start(candidate)


def _plan_approval_is_approved(plan_approval: dict[str, Any] | None) -> bool:
    if not isinstance(plan_approval, dict):
        return False
    decision = str(plan_approval.get("decision") or "").strip().lower()
    return decision in {
        "approve",
        "approved",
        "accept",
        "accepted",
        "confirm",
        "confirmed",
        "execute",
        "proceed",
        "yes",
    }


def _plan_approval_payload(
    *,
    request_id: str,
    state: str,
    plan_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "required": True,
        "state": state,
        "request_id": request_id,
    }
    if plan_id:
        payload["plan_id"] = plan_id
    return payload


def _attach_plan_approval_payload(
    raw_output: dict[str, Any] | None,
    *,
    request_id: str,
    state: str = "pending",
) -> dict[str, Any]:
    output = dict(raw_output or {})
    if output.get("type") != "plan_snapshot":
        output = {
            "type": "plan_snapshot",
            "version": 1,
            "action": "set",
            "plan": None,
            "summary": {
                "steps": 0,
                "verification": 0,
                "risks": 0,
                "assumptions": 0,
            },
        }

    plan = output.get("plan")
    plan_id: str | None = None
    if isinstance(plan, dict):
        plan = dict(plan)
        plan["status"] = "draft" if state == "pending" else str(plan.get("status") or "active")
        output["plan"] = plan
        raw_plan_id = plan.get("id")
        if raw_plan_id is not None:
            plan_id = str(raw_plan_id)

    output["approval"] = _plan_approval_payload(
        request_id=request_id,
        state=state,
        plan_id=plan_id,
    )
    return output


def _plan_start_payload(approval: dict[str, Any] | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    plan = {
        "id": "pending",
        "title": "正在制定执行方案",
        "objective": "根据当前请求梳理目标、范围、步骤、验证方式和风险。",
        "scope": "",
        "status": "draft",
        "steps": [],
        "verification": [],
        "risks": [],
        "assumptions": [],
        "created_at": now,
        "updated_at": now,
    }
    payload = {
        "type": "plan_snapshot",
        "version": 1,
        "action": "start",
        "plan": plan,
        "summary": {
            "steps": 0,
            "verification": 0,
            "risks": 0,
            "assumptions": 0,
        },
    }
    if approval is not None:
        payload["approval"] = approval
    return payload


def _classify_kind(filename: str, mime: str | None) -> str:
    """Map (filename, mime) → coarse artifact kind."""
    m = (mime or "").lower()
    for prefix, kind in _MIME_KIND_PREFIX:
        if m.startswith(prefix) or m == prefix:
            return kind
    ext = Path(filename).suffix.lower()
    return _EXT_KIND.get(ext, "file")


# ── Artifact directory contract ─────────────────────────────────
#
# In output mode artifacts land under the active artifact root. By default
# that is ``{workspace}/output/``; desktop hosts may pass a per-session root
# so concurrent sessions do not share one visible output workspace.

OUTPUT_SUBDIR: Final[str] = "output"


def ensure_output_dir(workspace_dir: str | Path) -> Path:
    """Return ``{workspace}/output/``, creating it if needed."""
    out = Path(workspace_dir).expanduser().resolve() / OUTPUT_SUBDIR
    out.mkdir(parents=True, exist_ok=True)
    return out


_SAFE_NAME_RE = re.compile(r"[^a-z0-9._-]+")


def safe_output_name(name: str, *, default_ext: str = "") -> str:
    """Normalize a proposed artifact name: lowercase, ascii, kebab-safe."""
    stem = name.strip()
    if not stem:
        stem = "artifact"
    suffix = Path(stem).suffix.lower()
    base = Path(stem).stem.lower()
    base = _SAFE_NAME_RE.sub("-", base).strip("-._") or "artifact"
    if not suffix and default_ext:
        suffix = default_ext if default_ext.startswith(".") else f".{default_ext}"
    return f"{base}{suffix}"


def avoid_collision(directory: Path, filename: str) -> Path:
    """Return a non-existing path inside ``directory`` by appending ``-N``."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    n = 2
    while True:
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


# Filled in below from _EXT_KIND. Adds explicit MIME for extensions that
# Python's mimetypes module doesn't always know (e.g. .md, .jsonl).
_EXT_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".rst": "text/x-rst",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".parquet": "application/vnd.apache.parquet",
    ".tsv": "text/tab-separated-values",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".webp": "image/webp",
    ".key": "application/vnd.apple.keynote",
}


def _make_artifact(tool_call_id: str, abs_file: Path, workspace_root: Path) -> ArtifactEvent:
    """Build an ArtifactEvent from a real on-disk file."""
    abs_resolved = abs_file.resolve()
    try:
        rel = abs_resolved.relative_to(workspace_root.resolve())
        rel_str = rel.as_posix()
    except ValueError:
        rel_str = abs_resolved.name

    mime, _ = mimetypes.guess_type(str(abs_resolved))
    if not mime:
        mime = _EXT_MIME_OVERRIDES.get(abs_resolved.suffix.lower())
    mime = mime or "application/octet-stream"
    kind = _classify_kind(abs_resolved.name, mime)
    try:
        size = abs_resolved.stat().st_size
    except OSError:
        size = -1

    digest = ""
    try:
        if 0 <= size <= 64 * 1024 * 1024:
            h = hashlib.sha256()
            with abs_resolved.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
            digest = h.hexdigest()[:16]
    except OSError:
        digest = ""

    return ArtifactEvent(
        tool_call_id=tool_call_id,
        kind=kind,
        filename=abs_resolved.name,
        rel_path=rel_str,
        abs_path=str(abs_resolved),
        uri=abs_resolved.as_uri(),
        mime=mime,
        size=size,
        sha256=digest,
        produced_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    )


def _artifact_scan_root(workspace_dir: str | None, artifact_root_dir: str | Path | None = None) -> Path | None:
    if artifact_root_dir:
        return Path(artifact_root_dir).expanduser().resolve()
    if not workspace_dir:
        return None
    return Path(workspace_dir).expanduser().resolve() / OUTPUT_SUBDIR

# Pattern to match <!--PLOT_DATA:...--> markers embedded by code execution.
# These carry interactive chart payloads already sent to the frontend via SSE;
# they must NOT be fed back into the model context.
_PLOT_DATA_RE = re.compile(r"<!--PLOT_DATA:.+?-->", re.DOTALL)

_MODEL_CONTEXT_PATH_EXTS = {".html", ".htm", ".json", ".md", ".txt", ".log", ".xml"}
_MODEL_CONTEXT_PATH_NAMES = {"qa.json", "html_self_check.json", "visual_review.md", "vision-review-prompt.txt"}
_MODEL_CONTEXT_PATH_PARTS = {"qa", "rendered", "slides", "vision_inputs"}
_MODEL_CONTEXT_CONTENT_THRESHOLD = 12_000


def _strip_plot_data(text: str) -> str:
    """Remove ``<!--PLOT_DATA:...-->`` markers from code-execution stdout.

    The markers contain chart data already delivered to the frontend through
    SSE events.  Keeping them in the model context wastes tokens and can
    cause context-length issues.

    Returns a short placeholder when stripping leaves the string empty.
    """
    cleaned = _PLOT_DATA_RE.sub("", text).strip()
    return cleaned if cleaned else "图表已生成"


def _path_needs_compact_model_context(path_value: Any, content: str) -> bool:
    """Detect generated artifacts that should not stay verbatim in LLM history."""
    if not isinstance(path_value, str) or not path_value:
        return len(content) > _MODEL_CONTEXT_CONTENT_THRESHOLD

    path = Path(path_value)
    suffix = path.suffix.lower()
    if path.name in _MODEL_CONTEXT_PATH_NAMES:
        return True
    if suffix in {".html", ".htm"}:
        return True
    if any(part in _MODEL_CONTEXT_PATH_PARTS for part in path.parts) and suffix in _MODEL_CONTEXT_PATH_EXTS:
        return True
    return len(content) > _MODEL_CONTEXT_CONTENT_THRESHOLD and suffix in _MODEL_CONTEXT_PATH_EXTS


def _compact_visible_tool_content_for_model(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    content: str,
) -> str:
    """Fallback compaction for tool content before it is appended to history."""
    if tool_name != "read_file" or not _path_needs_compact_model_context(arguments.get("path"), content):
        return content

    lines = content.splitlines()
    preview_limit = 20
    preview = "\n".join(lines[:preview_limit])
    path = arguments.get("path", "unknown")
    return (
        "[Full tool output omitted from model history]\n"
        f"Tool: {tool_name}\n"
        f"Path: {path}\n"
        f"Lines returned: {len(lines)}\n"
        f"Characters returned: {len(content)}\n"
        "Reason: generated/QA artifact content can bloat future LLM turns; "
        "call read_file again with offset/limit if exact content is needed.\n\n"
        f"Preview first {min(preview_limit, len(lines))} lines:\n"
        f"{preview}"
    )


def _summarize_tool_argument_for_model(
    *,
    tool_name: str,
    argument_name: str,
    value: str,
    path: str | None = None,
) -> str:
    """Return a compact placeholder for large tool-call arguments in history."""
    lines = value.splitlines()
    path_obj = Path(path) if path else None
    preview_limit = 12 if (path_obj and path_obj.suffix.lower() in {".html", ".htm"}) else 20
    preview = ""
    is_generated_file_write = (
        tool_name in {"write_file", "append_file"}
        and argument_name == "content"
        and path_obj is not None
        and path_obj.suffix.lower() in _MODEL_CONTEXT_PATH_EXTS
    )
    is_generated_file_edit = (
        tool_name == "edit_file"
        and argument_name in {"old_str", "new_str"}
        and path_obj is not None
        and path_obj.suffix.lower() in _MODEL_CONTEXT_PATH_EXTS
    )
    if not (
        is_generated_file_write
        or is_generated_file_edit
        or (
            path_obj
            and (
                path_obj.name in _MODEL_CONTEXT_PATH_NAMES
                or ("qa" in path_obj.parts and path_obj.suffix.lower() in _MODEL_CONTEXT_PATH_EXTS)
            )
        )
    ):
        preview = "\n".join(lines[:preview_limit])
        if len(preview) > 1200:
            preview = preview[:1200] + "\n..."
    summary = [
        "[Full tool-call argument omitted from model history]",
        f"Tool: {tool_name}",
        f"Argument: {argument_name}",
        f"Path: {path or 'unknown'}",
        f"Lines: {len(lines)}",
        f"Characters: {len(value)}",
        "Reason: generated artifact/script content was already written to disk; read the file with offset/limit if exact content is needed.",
    ]
    if preview:
        summary.extend(["", f"Preview first {min(preview_limit, len(lines))} lines:", preview])
    return "\n".join(summary)


def _tool_argument_needs_compaction(tool_name: str, argument_name: str, value: Any, path: str | None) -> bool:
    """Detect large/generated tool-call arguments that should not stay verbatim."""
    if not isinstance(value, str):
        return False

    if tool_name in {"write_file", "append_file"} and argument_name == "content":
        if path and Path(path).suffix.lower() in _MODEL_CONTEXT_PATH_EXTS:
            return True
        return _path_needs_compact_model_context(path, value)

    if tool_name == "edit_file" and argument_name in {"old_str", "new_str"}:
        if path and _path_needs_compact_model_context(path, value):
            return True
        return len(value) > _MODEL_CONTEXT_CONTENT_THRESHOLD

    # Catch accidental inline scripts/HTML in generic tool arguments, while
    # leaving normal short commands and prompts intact.
    return len(value) > _MODEL_CONTEXT_CONTENT_THRESHOLD


def _compact_tool_call_arguments_for_model(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Compact tool-call arguments before storing assistant calls in history.

    ToolCallStart events, logs, and actual tool execution keep the original
    arguments.  This affects only future LLM turns, preventing generated files
    such as ``deck.html`` from being resent after every step.
    """
    path = arguments.get("path")
    path_value = path if isinstance(path, str) else None
    compacted: dict[str, Any] = {}
    for key, value in arguments.items():
        if _tool_argument_needs_compaction(tool_name, key, value, path_value):
            compacted[key] = _summarize_tool_argument_for_model(
                tool_name=tool_name,
                argument_name=key,
                value=value,
                path=path_value,
            )
        else:
            compacted[key] = value
    return compacted


def _tool_calls_for_model_history(tool_calls: list[ToolCall] | None) -> list[ToolCall] | None:
    """Return tool calls safe to keep in model-facing message history."""
    if not tool_calls:
        return None
    return [
        ToolCall(
            id=tc.id,
            type=tc.type,
            function=FunctionCall(
                name=tc.function.name,
                arguments=_compact_tool_call_arguments_for_model(tc.function.name, tc.function.arguments),
            ),
        )
        for tc in tool_calls
    ]


def _tool_message_content_for_model(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    visible_content: str,
    visible_error: str | None,
) -> str:
    """Return the content stored in conversation history for a tool result.

    ToolCallResult events and logs keep full visible output.  This path controls
    only what future LLM calls receive in ``messages``.
    """
    if not result.success:
        return f"Error: {visible_error}"

    if result.model_context is not None and visible_content == result.content:
        return result.model_context

    compacted = _compact_visible_tool_content_for_model(
        tool_name=tool_name,
        arguments=arguments,
        content=visible_content,
    )
    return _strip_plot_data(compacted)


def _permission_event_kwargs(permission_request: dict[str, Any]) -> dict[str, Any]:
    """Normalize a tool permission_request dict for PermissionRequestEvent."""
    temporary_supported = permission_request.get("temporary_supported")
    persistent_supported = permission_request.get("persistent_supported")
    return {
        "scope": str(permission_request.get("scope") or ""),
        "requested_scope": str(permission_request.get("requested_scope") or ""),
        "reason": str(permission_request.get("reason") or ""),
        "path": str(permission_request.get("path") or ""),
        "temporary_supported": (
            True if temporary_supported is None else bool(temporary_supported)
        ),
        "persistent_supported": (
            True if persistent_supported is None else bool(persistent_supported)
        ),
        "persistent_label": str(permission_request.get("persistent_label") or ""),
        "command": str(permission_request.get("command") or ""),
        "risk": str(permission_request.get("risk") or ""),
    }


def _approve_tool_permission(tool: Tool, permission_request: dict[str, Any]) -> None:
    """Let a tool consume one-shot approval state before core retries it."""
    approver = getattr(tool, "approve_permission_request", None)
    if not callable(approver):
        return
    try:
        approver(permission_request)
    except Exception as exc:
        _log.warning(
            "tool/permission_approval_hook_failed tool=%s error=%s",
            getattr(tool, "name", type(tool).__name__),
            exc,
        )


def _policy_decision_payload(
    *,
    tool_name: str,
    permission_request: dict[str, Any],
    decision: str,
    retry_count: int = 0,
    error: str = "",
) -> dict[str, Any]:
    """Build a host-facing policy decision payload for a permission request."""
    payload = {
        "type": "policy_decision",
        "tool_name": tool_name,
        "decision": decision,
        "retry_count": retry_count,
        **_permission_event_kwargs(permission_request),
    }
    if error:
        payload["error"] = error
    return payload


def _extract_web_search_payload(tool_name: str, content: str) -> dict[str, Any] | None:
    """Return a frontend-friendly web_search payload when tool output has refs."""
    if tool_name != "web_search" or not content:
        return None

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict) or not isinstance(payload.get("refs"), list):
        return None

    return payload


def _auto_match_memory_for_latest_prompt(messages: list[Message], memory_manager: Any) -> ToolCallResult | None:
    """Conservatively match CONTEXT.md against the latest user prompt.

    Matches are injected as weak, one-turn context: the model is told these
    memories may be relevant and must ignore them when the user is starting a
    new task.  This avoids depending on the model deciding to call
    ``memory_search`` while keeping the memory signal non-authoritative.
    """
    latest_user = next((msg for msg in reversed(messages) if msg.role == "user"), None)
    if latest_user is None:
        return None

    user_text = latest_user.content if isinstance(latest_user.content, str) else str(latest_user.content)
    try:
        matches = memory_manager.auto_match_context(user_text)
    except Exception:
        return None

    if not matches:
        return None

    memory_lines = "\n".join(item["text"] for item in matches)
    latest_user.content = (
        f"{user_text.rstrip()}\n\n"
        "## Possibly relevant memory\n"
        "The following memories were automatically matched from prior context. "
        "Use them only if they are clearly relevant to the user's current request. "
        "If the user is starting a new task or the memories do not fit, ignore them and do not assume continuity.\n\n"
        f"{memory_lines}"
    )

    raw_output = {
        "type": "memory_search",
        "trigger": "auto",
        "query": user_text,
        "matched_memories": matches,
    }
    return ToolCallResult(
        tool_call_id="memory-auto-match",
        tool_name="memory_search",
        success=True,
        content=f"Auto-matched {len(matches)} possible context memor{'y' if len(matches) == 1 else 'ies'}.",
        raw_output=raw_output,
    )


def _detect_artifacts(
    tool_call_id: str,
    tool_name: str,
    content: str,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None = None,
) -> list[ArtifactEvent]:
    """Scan tool output for ``[filename.ext]`` references that resolve under
    the active artifact output directory."""
    if not workspace_dir or not content:
        return []

    ws = Path(workspace_dir).resolve()
    out = _artifact_scan_root(workspace_dir, artifact_root_dir)
    if out is None:
        return []
    if not out.is_dir():
        return []

    artifacts: list[ArtifactEvent] = []
    seen_paths: set[Path] = set()
    for match in _ARTIFACT_REF_RE.finditer(content):
        filename = match.group(1)
        candidate = (out / filename).resolve()
        try:
            candidate.relative_to(out)
        except ValueError:
            continue
        if candidate in seen_paths or not candidate.is_file():
            continue
        seen_paths.add(candidate)
        artifacts.append(_make_artifact(tool_call_id, candidate, ws))

    return artifacts


# ── Workspace diff-based artifact detection ─────────────────────

# Directories under output/ to skip when snapshotting.
_IGNORE_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".ipynb_checkpoints"}


def _snapshot_workspace(workspace_dir: str, artifact_root_dir: str | Path | None = None) -> set[Path]:
    """Snapshot files under the active artifact output directory (recursive).

    Only the canonical output directory is scanned — files the user keeps in
    the workspace root are intentionally ignored so they are never re-emitted
    as new artifacts.
    """
    out = _artifact_scan_root(workspace_dir, artifact_root_dir)
    if out is None:
        return set()
    if not out.is_dir():
        return set()

    files: set[Path] = set()
    for entry in out.rglob("*"):
        if not entry.is_file():
            continue
        if any(p in entry.parts for p in _IGNORE_DIRS):
            continue
        if entry.name.startswith(".") or entry.suffix == ".tmp":
            continue
        files.add(entry)
    return files


def _detect_new_files(
    tool_call_id: str,
    pre_files: set[Path],
    post_files: set[Path],
    already_emitted: set[str],
    workspace_dir: str,
) -> list[ArtifactEvent]:
    """Create ArtifactEvents for files that appeared after tool execution."""
    new_files = post_files - pre_files
    if not new_files:
        return []

    ws = Path(workspace_dir).resolve()
    artifacts: list[ArtifactEvent] = []
    for fpath in sorted(new_files):
        if fpath.name.startswith(".") or fpath.name.startswith("~") or fpath.suffix == ".tmp":
            continue
        if str(fpath.resolve()) in already_emitted:
            continue
        artifacts.append(_make_artifact(tool_call_id, fpath, ws))

    return artifacts


def _detect_regex_artifacts(
    tool_call_id: str,
    tool_name: str,
    content: str,
    raw_output: Any,
    workspace_dir: str,
    artifact_root_dir: str | Path | None,
) -> tuple[list[ArtifactEvent], set[str]]:
    """Layer-1 (regex) artifacts for one tool result.

    Returns the regex-detected artifacts plus the set of absolute paths that
    should be excluded from the later diff layer (those already surfaced here,
    or carried on a ``type:"artifact"`` ``raw_output``).
    """
    regex_artifacts = _detect_artifacts(
        tool_call_id,
        tool_name,
        content,
        workspace_dir,
        artifact_root_dir,
    )
    already = {a.abs_path for a in regex_artifacts}
    if isinstance(raw_output, dict) and raw_output.get("type") == "artifact":
        for key in ("abs_path", "absolute_path"):
            raw_path = raw_output.get(key)
            if isinstance(raw_path, str) and raw_path.strip():
                already.add(str(Path(raw_path).expanduser().resolve()))
    return regex_artifacts, already


def _detect_tool_artifacts(
    tool_call_id: str,
    tool_name: str,
    content: str,
    raw_output: Any,
    pre_files: set[Path],
    post_files: set[Path],
    workspace_dir: str,
    artifact_root_dir: str | Path | None,
) -> list[ArtifactEvent]:
    """Two-layer artifact detection for a single tool result (sequential path).

    Layer 1 (regex): scan ``content`` for ``[filename.ext]`` references that
    resolve under the artifact root. Layer 2 (diff): catch files created by the
    tool that weren't referenced in the output text, using a per-tool pre/post
    workspace snapshot. The parallel branch can't take per-tool snapshots under
    concurrency, so it composes :func:`_detect_regex_artifacts` per result with
    a single diff pass instead (see the parallel block in ``run_agent_loop``).
    """
    regex_artifacts, already = _detect_regex_artifacts(
        tool_call_id, tool_name, content, raw_output, workspace_dir, artifact_root_dir
    )
    diff_artifacts = _detect_new_files(
        tool_call_id, pre_files, post_files, already, workspace_dir
    )
    return [*regex_artifacts, *diff_artifacts]


# ── Token estimation helpers ────────────────────────────────────


def _estimate_tokens(messages: list[Message]) -> int:
    """Estimate token count using tiktoken (cl100k_base)."""
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        return _estimate_tokens_fallback(messages)

    total = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total += len(encoding.encode(msg.content))
        elif isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict):
                    total += len(encoding.encode(str(block)))
        if msg.thinking:
            total += len(encoding.encode(msg.thinking))
        if msg.tool_calls:
            total += len(encoding.encode(str(msg.tool_calls)))
        total += 4  # per-message overhead
    return total


def _estimate_tokens_fallback(messages: list[Message]) -> int:
    """Rough fallback when tiktoken is unavailable."""
    total_chars = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total_chars += len(msg.content)
        elif isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict):
                    total_chars += len(str(block))
        if msg.thinking:
            total_chars += len(msg.thinking)
        if msg.tool_calls:
            total_chars += len(str(msg.tool_calls))
    return int(total_chars / 2.5)


# ── Summarization ───────────────────────────────────────────────


async def _create_summary(
    llm,
    messages: list[Message],
    round_num: int,
    session_id: str = "",
) -> str:
    """Summarize one execution round via an LLM call.

    Raises on LLM failure rather than returning the un-summarized concatenation,
    which would *increase* token usage (the original bloat bug). Callers should
    degrade gracefully — typically by dropping the round's exec messages.
    """
    if not messages:
        return ""

    summary_content = f"Round {round_num} execution process:\n\n"
    for msg in messages:
        if msg.role == "assistant":
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            summary_content += f"Assistant: {text}\n"
            if msg.tool_calls:
                names = [tc.function.name for tc in msg.tool_calls]
                summary_content += f"  → Called tools: {', '.join(names)}\n"
        elif msg.role == "tool":
            preview = msg.content if isinstance(msg.content, str) else str(msg.content)
            summary_content += f"  ← Tool returned: {preview}...\n"

    prompt = (
        "Summarize the following Agent execution process concisely.\n\n"
        f"{summary_content}\n\n"
        "Requirements:\n"
        "1. Focus on what tasks were completed and which tools were called\n"
        "2. Keep key execution results and important findings\n"
        "3. Keep the summary under ~800 tokens\n"
        "4. Use the same language as the input content above\n"
        "5. Do not include \"user\" related content, only summarize the Agent's execution process"
    )
    response: LLMResponse = await llm.generate(
        messages=[
            Message(role="system", content="You are an assistant skilled at summarizing Agent execution processes."),
            Message(role="user", content=prompt),
        ],
        tools=None,
        thinking_enabled=False,
        session_id=session_id,
    )
    return response.content


async def _maybe_summarize(
    llm,
    messages: list[Message],
    token_limit: int,
    api_total_tokens: int,
    skip_check: bool,
    session_id: str = "",
) -> tuple[list[Message] | None, bool, int]:
    """Check token usage and summarize if needed.

    Returns:
        (new_messages_or_None, skip_next, estimated_tokens)
    """
    if skip_check:
        return None, False, 0

    estimated = _estimate_tokens(messages)
    if estimated <= token_limit and api_total_tokens <= token_limit:
        return None, False, estimated

    # Build summarized message list
    user_indices = [i for i, m in enumerate(messages) if m.role == "user" and i > 0]
    if len(user_indices) < 1:
        return None, False, estimated

    new_messages: list[Message] = [messages[0]]  # system prompt

    for idx, user_idx in enumerate(user_indices):
        user_msg = messages[user_idx]

        next_boundary = user_indices[idx + 1] if idx < len(user_indices) - 1 else len(messages)
        exec_msgs = messages[user_idx + 1 : next_boundary]

        # If this user message is itself a prior summary marker and there is
        # no fresh exec after it, drop it — keeps stale summaries from piling
        # up across many compaction cycles.
        if _is_summary_marker(user_msg) and not exec_msgs:
            continue

        new_messages.append(user_msg)

        if exec_msgs:
            try:
                summary = await _create_summary(llm, exec_msgs, idx + 1, session_id=session_id)
            except Exception as exc:
                _log.warning(
                    "summarization failed for round %d: %s — dropping exec_msgs",
                    idx + 1, exc,
                )
                summary = ""
            if summary:
                new_messages.append(
                    Message(role="user", content=f"{_SUMMARY_MARKER}\n\n{summary}")
                )
            # On failure: drop exec_msgs entirely. Token usage strictly
            # decreases, never increases. The user message itself is kept,
            # so the conversation flow stays intact.

    return new_messages, True, estimated


# ── Summarization helpers ───────────────────────────────────


# Marker prefix on a user-role message that signals "this is an
# already-summarized round, do not re-summarize". Kept stable across releases
# because it is also visible to the model and used as a re-entry guard.
_SUMMARY_MARKER = "[Assistant Execution Summary]"


def _is_summary_marker(msg: Message) -> bool:
    """Return True when ``msg`` is a synthetic summary placeholder."""
    if msg.role != "user":
        return False
    content = msg.content if isinstance(msg.content, str) else ""
    return content.startswith(_SUMMARY_MARKER)


# ── Micro-compact (Layer 1) ─────────────────────────────────

# Number of recent tool messages to keep intact (lower bound).
_KEEP_RECENT_TOOL_RESULTS = 3
# Tool results shorter than this are not worth compacting.
_MIN_COMPACT_LEN = 200
# Soft cap on cumulative tokens spent by the "recent kept" tool results.
# When the last ``_KEEP_RECENT_TOOL_RESULTS`` messages alone exceed this
# budget, we shrink the keep-window from the oldest side so a few
# very-large tool outputs cannot bypass micro-compaction entirely.
# Calibrated against tiktoken cl100k_base — provider-agnostic enough that
# the same threshold is safe across Anthropic/OpenAI/DeepSeek/Qwen paths.
_KEEP_RECENT_TOOL_TOKEN_BUDGET = 12_000


def _approx_tokens_for_content(content: Any) -> int:
    """Cheap per-message token estimate for the Layer-1 keep window.

    Uses tiktoken when available, falls back to char/4 — matches the
    behavior of ``_estimate_tokens_fallback`` so single-platform absence
    of tiktoken does not break compaction.
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(str(b) for b in content)
    else:
        text = str(content)
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _short_tool_text(value: Any, limit: int = 180) -> str:
    """Return a one-line text fragment suitable for compacted history."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    lower_mapping = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        value = lower_mapping.get(key.lower())
        if value not in (None, ""):
            return value
    return None


_WEB_SEARCH_RESULT_KEYS: Final[tuple[str, ...]] = (
    "refs",
    "results",
    "Results",
    "web_results",
    "items",
    "value",
    "organic_results",
    "data",
)

_URL_TRACKING_PARAMS: Final[set[str]] = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def _normalize_web_search_query(arguments: dict[str, Any]) -> str:
    query = _first_present(
        arguments,
        (
            "query",
            "Query",
            "q",
            "search_query",
            "searchQuery",
            "search_terms",
            "keywords",
        ),
    )
    if query is None:
        return ""
    return " ".join(str(query).casefold().split())


def _normalize_search_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.casefold()

    scheme = parts.scheme.casefold()
    netloc = parts.netloc.casefold()
    path = parts.path.rstrip("/") or parts.path
    query_items = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        key_l = key.casefold()
        if key_l.startswith("utm_") or key_l in _URL_TRACKING_PARAMS:
            continue
        query_items.append((key, val))
    query = urlencode(query_items, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _normalize_search_title(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _web_search_result_key(item: dict[str, Any]) -> str:
    url = _first_present(item, ("url", "Url", "href", "link", "Link"))
    normalized_url = _normalize_search_url(url)
    if normalized_url:
        return f"url:{normalized_url}"

    title = _normalize_search_title(_first_present(item, ("title", "Title", "name", "Name")))
    if not title:
        return ""
    domain = str(_first_present(item, ("domain", "Domain", "source", "Source", "site", "Site")) or "").casefold()
    return f"title:{domain}:{title}"


def _with_filtered_search_items(payload: Any, filtered_items: list[dict[str, Any]]) -> Any:
    if isinstance(payload, list):
        return filtered_items
    if not isinstance(payload, dict):
        return payload

    for key in _WEB_SEARCH_RESULT_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            updated = dict(payload)
            updated[key] = filtered_items
            return updated

    return payload


def _candidate_search_items(payload: Any) -> list[dict[str, Any]]:
    """Extract likely search-result rows from common web_search payload shapes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    for key in _WEB_SEARCH_RESULT_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            items = [item for item in value if isinstance(item, dict)]
            if items:
                return items

    for value in payload.values():
        if isinstance(value, dict):
            nested = _candidate_search_items(value)
            if nested:
                return nested
        elif isinstance(value, list):
            items = [item for item in value if isinstance(item, dict)]
            if any(_first_present(item, ("title", "Title", "url", "Url", "href", "link")) for item in items):
                return items

    return []


def _dedupe_web_search_content(
    content: str,
    seen_result_keys: set[str],
) -> tuple[str, int, int, list[str], bool]:
    """Filter duplicate web_search rows for this turn.

    Returns ``(content, new_count, duplicate_count, new_labels, inspected)``.
    ``inspected`` is true only when structured search rows were found; plain
    text results should not count as "no new evidence" just because they
    cannot be deduped structurally.
    """
    if not content:
        return content, 0, 0, [], False

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content, 0, 0, [], False

    items = _candidate_search_items(payload)
    if not items:
        return content, 0, 0, [], False

    filtered_items: list[dict[str, Any]] = []
    new_labels: list[str] = []
    duplicate_count = 0
    for item in items:
        key = _web_search_result_key(item)
        if key and key in seen_result_keys:
            duplicate_count += 1
            continue
        if key:
            seen_result_keys.add(key)
        filtered_items.append(item)
        label = _first_present(item, ("title", "Title", "name", "Name")) or _first_present(
            item, ("url", "Url", "href", "link", "Link")
        )
        if label:
            new_labels.append(_short_tool_text(label, 100))

    if duplicate_count == 0:
        return content, len(filtered_items), 0, new_labels, True

    updated_payload = _with_filtered_search_items(payload, filtered_items)
    if isinstance(updated_payload, dict):
        updated_payload = {
            **updated_payload,
            "DedupedDuplicateCount": duplicate_count,
            "DedupedNewCount": len(filtered_items),
        }
    return json.dumps(updated_payload, ensure_ascii=False), len(filtered_items), duplicate_count, new_labels, True


def _compact_web_search_result_for_model(content: str) -> str | None:
    """Preserve usable search evidence when compacting old web_search results."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None

    query = None
    result_count = None
    auth_level = None
    if isinstance(payload, dict):
        query = _first_present(payload, ("query", "Query", "q"))
        result_count = _first_present(payload, ("result_count", "ResultCount", "count", "Count"))
        auth_level = _first_present(payload, ("auth_level", "AuthLevel"))

    details: list[str] = []
    if query:
        details.append(f"query={_short_tool_text(query, 120)}")
    if result_count is not None:
        details.append(f"count={result_count}")
    if auth_level is not None:
        details.append(f"auth_level={auth_level}")

    lines = ["[Previous result from web_search; compacted evidence retained"]
    if details:
        lines[0] += ": " + ", ".join(details)
    lines[0] += "]"

    items = _candidate_search_items(payload)
    for index, item in enumerate(items[:5], start=1):
        title = _first_present(item, ("title", "Title", "name", "Name"))
        url = _first_present(item, ("url", "Url", "href", "link", "Link"))
        snippet = _first_present(
            item,
            (
                "snippet",
                "Snippet",
                "summary",
                "Summary",
                "description",
                "Description",
                "content",
                "Content",
            ),
        )
        parts = []
        if title:
            parts.append(_short_tool_text(title, 120))
        if url:
            parts.append(_short_tool_text(url, 160))
        if snippet:
            parts.append(_short_tool_text(snippet, 220))
        if parts:
            lines.append(f"{index}. " + " | ".join(parts))

    if len(lines) == 1:
        first_line = _short_tool_text(content.split("\n", 1)[0], 180)
        lines.append(first_line)

    return "\n".join(lines)


def _micro_compact(messages: list[Message]) -> int:
    """Replace old tool-result content with short placeholders.

    Walks the message list, finds tool-role messages, keeps the last
    ``_KEEP_RECENT_TOOL_RESULTS`` intact, and replaces earlier ones
    whose content exceeds ``_MIN_COMPACT_LEN`` with a one-liner.

    Additionally, if the cumulative token cost of the "kept" recent
    messages exceeds ``_KEEP_RECENT_TOOL_TOKEN_BUDGET``, the keep window
    is shrunk from the oldest side (but always preserves at least the
    most recent tool message) so a few very-large outputs cannot bypass
    Layer 1 entirely.

    This is a cheap, zero-LLM-call operation that runs every step.

    Returns:
        Number of messages compacted.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
    if len(tool_indices) <= 1:
        return 0

    # Start with the conservative N-recent keep window.
    keep_count = min(_KEEP_RECENT_TOOL_RESULTS, len(tool_indices))

    # Shrink keep window if the recent block alone busts the budget.
    # Always preserve at least one message (the latest tool result).
    while keep_count > 1:
        recent_indices = tool_indices[-keep_count:]
        cum_tokens = sum(_approx_tokens_for_content(messages[i].content) for i in recent_indices)
        if cum_tokens <= _KEEP_RECENT_TOOL_TOKEN_BUDGET:
            break
        keep_count -= 1

    if len(tool_indices) <= keep_count:
        return 0

    compacted = 0
    for idx in tool_indices[:-keep_count]:
        msg = messages[idx]
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if len(content) <= _MIN_COMPACT_LEN:
            continue
        tool_name = msg.name or "unknown"
        if tool_name == "web_search":
            compacted_content = _compact_web_search_result_for_model(content)
        else:
            compacted_content = None
        # Preserve the first line as a hint (often contains the key result)
        first_line = content.split("\n", 1)[0][:100]
        messages[idx] = Message(
            role="tool",
            content=compacted_content or f"[Previous result from {tool_name}: {first_line}...]",
            tool_call_id=msg.tool_call_id,
            name=msg.name,
        )
        compacted += 1

    return compacted


# ── Cleanup helper ──────────────────────────────────────────────


_INTERRUPTED_TOOL_STUB = (
    "[Tool execution interrupted — no result available. "
    "The previous run was terminated before this tool produced output.]"
)


def _sanitize_dangling_tool_calls(messages: list[Message]) -> int:
    """Synthesize stub tool replies for any assistant.tool_calls lacking a response.

    Heals message histories where a previous turn's tool execution was
    interrupted (process crash, SIGKILL, mid-flight cancellation that skipped
    the result-append path) before every tool response was recorded. Without
    this, the next LLM request would fail with the OpenAI/Anthropic protocol
    error ``assistant message with tool_calls must be followed by tool
    messages``. Returns count of synthesized stubs.
    """
    synthesized = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.role != "assistant" or not msg.tool_calls:
            i += 1
            continue
        seen_ids: set[str] = set()
        j = i + 1
        while j < len(messages) and messages[j].role == "tool":
            if messages[j].tool_call_id:
                seen_ids.add(messages[j].tool_call_id)
            j += 1
        insert_at = j
        for tc in msg.tool_calls:
            if tc.id and tc.id not in seen_ids:
                messages.insert(
                    insert_at,
                    Message(
                        role="tool",
                        content=_INTERRUPTED_TOOL_STUB,
                        tool_call_id=tc.id,
                        name=tc.function.name,
                    ),
                )
                insert_at += 1
                synthesized += 1
        i = insert_at if insert_at > i else i + 1
    return synthesized


def _cleanup_incomplete_messages(messages: list[Message]) -> int:
    """Remove trailing incomplete assistant + tool messages. Returns removed count.

    Called from abort paths (cancel / max_tokens / error / no-output) to leave
    the message list in a state safe to resend to the LLM on the next turn.

    A trailing assistant turn is considered *incomplete* when:
      - It has ``tool_calls`` but the number of trailing tool messages does
        not match (some tool responses are missing).
      - Its content is empty AND it has no tool_calls (an LLM that was cut
        off before emitting anything).

    A trailing assistant turn that has no tool_calls AND has content is
    treated as complete and left in place — deleting it would discard a
    fully-formed answer the LLM already produced.
    """
    last_assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            last_assistant_idx = i
            break
    if last_assistant_idx == -1:
        return 0

    last = messages[last_assistant_idx]
    trailing_tool_count = len(messages) - last_assistant_idx - 1

    expected_tool_count = len(last.tool_calls or [])
    has_content = bool(last.content) or bool(last.thinking)

    is_incomplete = False
    if expected_tool_count > 0:
        # tool_calls present — incomplete unless every call has a tool response
        if trailing_tool_count < expected_tool_count:
            is_incomplete = True
    elif not has_content:
        # Empty assistant turn with no tool_calls → cut off before output
        is_incomplete = True

    if not is_incomplete:
        return 0

    removed = len(messages) - last_assistant_idx
    del messages[last_assistant_idx:]
    return removed


# ── Main loop ───────────────────────────────────────────────────


async def run_agent_loop(
    *,
    llm,
    messages: list[Message],
    tools: dict[str, Tool],
    max_steps: int = 200,
    token_limit: int = 113400,
    is_cancelled: CancelChecker | None = None,
    logger: AgentLogger | None = None,
    workspace_dir: str | None = None,
    permission_negotiator: Any | None = None,
    hooks: list | None = None,
    memory_manager: Any | None = None,
    memory_extractor: Any | None = None,
    memory_promotion_enabled: bool = False,
    memory_promotion_hit_threshold: int = 5,
    memory_promotion_cooldown_days: int = 14,
    inject_queue: asyncio.Queue[str] | None = None,
    thinking_enabled: bool = False,
    session_id: str = "",
    force_plan_start: bool = False,
    require_plan_approval: bool = False,
    plan_approval: dict[str, Any] | None = None,
    plan_start_text: str | None = None,
    pause_after_plan_write: bool = False,
    no_progress_limit: int | None = None,
    max_parallel_tools: int = 8,
    completion_gate: CompletionGate | None = None,
    truncation_continuation_enabled: bool = True,
    max_truncation_continuations: int = 1,
    artifact_detection_enabled: bool = True,
    artifact_root_dir: str | Path | None = None,
) -> AsyncIterator[AgentEvent]:
    """Execute the agent loop, yielding structured events.

    This is the single source of truth for the agent execution loop.
    It does **not** print anything to stdout.  Consumers (CLI, ACP,
    JSON-RPC) decide how to render each event.

    Args:
        llm: LLM client (must have an async ``generate()`` method).
        messages: Message history (mutated in-place).
        tools: ``{name: Tool}`` dict.
        max_steps: Maximum LLM call iterations.
        token_limit: Token threshold for triggering summarization.
        is_cancelled: Optional callable — return ``True`` to stop.
        logger: Optional ``AgentLogger`` for file-based logging.
        workspace_dir: Workspace directory for artifact detection.
        permission_negotiator: Optional negotiator (has async
            ``negotiate(permission_request)`` method) for in-band
            permission escalation.  When present, denied tool calls
            with ``permission_request`` are negotiated with the host
            and retried on grant.  When absent, ``PermissionRequestEvent``
            is yielded for backward compatibility.
        hooks: Optional list of lifecycle hook objects.  Each hook may
            implement any subset of the ``BaseHook`` interface.  Hooks
            are called at key lifecycle points (step start/end, tool
            start/result, done, error).  Loaded identically by CLI
            and ACP from ``config.yaml``.
        memory_manager: Optional ``MemoryManager`` instance for conservative
            prompt-level context memory auto matching.
        memory_extractor: Optional ``MemoryExtractor`` instance for
            lifecycle-triggered memory extraction.  When present,
            extraction is attempted before context compression and
            every N steps.
        inject_queue: Optional queue for in-stream message injection.
            When present, queued user messages are drained at each
            step boundary and appended to the conversation before
            the next LLM call.
        require_plan_approval: If True, the loop must publish a plan and
            stop before executing non-plan tools unless ``plan_approval``
            carries an approved decision.
        plan_approval: Host-supplied decision metadata for a previously
            published plan.
        plan_start_text: Optional host-sanitized latest user request for
            plan-start detection. When omitted, the latest user message is used.
        pause_after_plan_write: If True, an organic ``plan_write`` call also
            becomes an approval boundary: the plan is published with pending
            approval and the turn ends before sibling or later tools execute.
        artifact_detection_enabled: If False, skip output-directory artifact
            snapshotting and detection for sessions that edit an existing
            project tree directly.
        truncation_continuation_enabled: If True (default), re-prompt the
            model once when a reply ends mid-sentence while the provider
            reported a normal finish, so the answer completes in the same
            message. See ``loop_guards.looks_like_truncated_output``.
        max_truncation_continuations: Per-turn cap on truncation
            continuations (loop guard against repeated false positives).
        artifact_root_dir: Optional explicit artifact directory supplied by a
            host session. Defaults to ``{workspace_dir}/output``.
    """
    cancelled = is_cancelled or (lambda: False)
    hook_mgr = HookManager(hooks)

    if logger:
        logger.start_new_run()
        log_path = logger.get_log_file_path()
        if log_path:
            yield LogFileEvent(path=str(log_path))

    if hook_mgr.hooks:
        await hook_mgr.fire_agent_start(messages=messages, tools=tools, max_steps=max_steps)

    if memory_manager:
        injected = _auto_match_memory_for_latest_prompt(messages, memory_manager)
        if injected is not None:
            yield injected

    api_total_tokens = 0
    skip_next_token_check = False
    run_start = perf_counter()

    # Defensive: heal any dangling assistant.tool_calls from a prior interrupted
    # turn (process crash, SIGKILL) before the first LLM request, so the
    # protocol-state precondition holds.
    healed = _sanitize_dangling_tool_calls(messages)
    if healed:
        logging.getLogger(__name__).warning(
            "Healed %d dangling assistant tool_call(s) on loop entry — "
            "synthesized interrupted-stub tool responses.",
            healed,
        )

    def _build_proposal_event() -> MemoryProposalEvent | None:
        """Read promotion candidates from memory and bump their last_proposed."""
        if not (memory_promotion_enabled and memory_manager):
            return None
        try:
            entries = memory_manager.list_promotion_candidates(
                hit_threshold=memory_promotion_hit_threshold,
                cooldown_days=memory_promotion_cooldown_days,
            )
        except Exception:
            return None
        if not entries:
            return None
        try:
            memory_manager.mark_proposed([e.id for e in entries])
        except Exception:
            pass
        return MemoryProposalEvent(
            candidates=tuple(
                MemoryPromotionCandidate(
                    entry_id=e.id,
                    content=e.content,
                    hits=e.hits,
                    confidence=e.confidence,
                )
                for e in entries
            )
        )

    async def _build_proposal_event_with_plan() -> MemoryProposalEvent | None:
        """Same as ``_build_proposal_event`` but also asks the LLM to draft a
        single core rewrite consuming the hot candidates.  On any planner
        failure, falls back to the legacy per-candidate proposal (plan=None).
        """
        event = _build_proposal_event()
        if event is None:
            return None
        wanted = {c.entry_id for c in event.candidates}
        try:
            entries = [
                e for e in memory_manager._read_context_entries() if e.id in wanted
            ]
        except Exception as exc:
            _log.warning(
                "proposal_with_plan: failed to read context entries, falling back to legacy event: %s",
                exc,
            )
            return event
        if not entries:
            _log.warning(
                "proposal_with_plan: no entries match candidate ids %s, falling back to legacy event",
                sorted(wanted),
            )
            return event
        try:
            plan = await memory_manager.plan_promotion(entries, llm)
        except Exception as exc:
            _log.warning(
                "proposal_with_plan: plan_promotion raised, falling back to legacy event: %s",
                exc,
            )
            return event
        if plan is None:
            _log.warning(
                "proposal_with_plan: plan_promotion returned None (see prior warnings), falling back to legacy event for %d candidates",
                len(entries),
            )
            return event
        return MemoryProposalEvent(candidates=event.candidates, plan=plan)

    # Loop-guard state: detect when the model emits the same tool_call
    # signature with empty arguments two turns in a row. With a healthy LLM
    # this should never happen — it's the fingerprint of a relay/provider
    # bug or a model stuck after seeing "missing required argument" errors,
    # and continuing burns max_steps without progress.
    empty_args_signature: tuple[str, ...] | None = None
    empty_args_repeats = 0

    # Near-limit wrap-up: when only WRAPUP_REMAINING steps are left, inject a
    # one-shot instruction telling the model to stop gathering more material
    # (tool calls / searches) and synthesize a final answer from what it
    # already has, instead of burning the last steps and exiting with a
    # "couldn't be completed" failure.
    wrapup_injected = False

    # No-progress circuit breaker (opt-in via ``no_progress_limit``). Counts
    # consecutive steps in which no tool call returned a success with usable
    # (non-empty) content. After the limit is hit, inject the same wrap-up
    # synthesis nudge instead of letting a stuck agent flail to max_steps —
    # the failure mode seen when a sub-agent has no web_search and retries raw
    # curl scraping dozens of times. Disabled (None) for the top-level agent to
    # preserve existing behavior.
    no_progress_steps = 0

    # Completion gate (opt-in via ``completion_gate``). ``succeeded_tools``
    # accumulates tool names that produced ≥1 successful, non-empty result;
    # ``gate_continuations`` bounds how many times the gate may force the
    # loop to continue past a natural END_TURN. Both inert when the gate is
    # disabled (None).
    succeeded_tools: set[str] = set()
    gate_continuations = 0

    # Suspected-truncation continuation (opt-in via
    # ``truncation_continuation_enabled``). Bounds how many times the loop
    # may re-prompt the model to finish a reply that ended mid-sentence
    # while the provider reported a normal finish.
    truncation_continuations = 0

    # Per-turn guard for tools that can be repeatedly requested by the model
    # after it already has enough evidence. Once a budget is reached, later
    # calls are answered with synthetic tool errors so the protocol remains
    # valid while nudging the model to synthesize.
    tool_call_counts: dict[str, int] = {}
    tool_budget_wrapup_injected: set[str] = set()
    visible_tool_call_total = 0
    final_summary_guidance_injected = False
    final_summary_empty_retry_injected = False
    web_search_seen_queries: set[str] = set()
    web_search_seen_result_keys: set[str] = set()
    web_search_unique_results = 0
    web_search_duplicate_results = 0
    web_search_no_new_batches = 0
    plan_start_emitted = False
    forced_plan_guidance_injected = False
    forced_plan_retry_injected = False
    plan_approval_approved = _plan_approval_is_approved(plan_approval)
    plan_approval_gate_completed = False
    plan_approval_request_id = "plan-" + hashlib.sha1(
        f"{run_start}:{_latest_user_text(messages)}".encode("utf-8", errors="ignore")
    ).hexdigest()[:10]

    for step in range(max_steps):
        # ── Cancellation check (top of step) ────────────────
        # No cleanup needed here — messages are consistent at step boundaries.
        if cancelled():
            if hook_mgr.hooks:
                await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            return

        step_start = perf_counter()
        web_search_step_seen = False
        web_search_step_executed = 0
        web_search_step_deferred = 0
        web_search_step_duplicate_queries = 0
        web_search_step_new_results = 0
        web_search_step_duplicate_results = 0
        web_search_step_structured_results = 0
        web_search_step_labels: list[str] = []

        # ── Drain inject queue (in-stream injection) ───────
        if inject_queue:
            while not inject_queue.empty():
                injected_item = inject_queue.get_nowait()
                injection_id = None
                if isinstance(injected_item, dict):
                    injected_text = str(injected_item.get("content") or "")
                    raw_injection_id = injected_item.get("id")
                    if isinstance(raw_injection_id, str):
                        injection_id = raw_injection_id
                else:
                    injected_text = str(injected_item)
                if not injected_text:
                    continue
                messages.append(
                    Message(role="user", content=format_injected_message(injected_text))
                )
                yield InjectedMessageEvent(content=injected_text, injection_id=injection_id)

        has_plan_tool = "plan_write" in tools
        latest_user_text = _latest_user_text(messages)
        latest_user_is_short_non_task = text_is_short_non_task_reply(latest_user_text)
        plan_approval_gate_enabled = (
            require_plan_approval
            and not plan_approval_approved
            and has_plan_tool
            and not latest_user_is_short_non_task
        )
        force_plan_for_turn = (force_plan_start or plan_approval_gate_enabled) and has_plan_tool
        if force_plan_for_turn and not forced_plan_guidance_injected:
            forced_plan_guidance_injected = True
            guidance = (
                _FORCED_PLAN_APPROVAL_GUIDANCE
                if plan_approval_gate_enabled
                else _FORCED_PLAN_GUIDANCE
            )
            messages.append(
                Message(role="user", content=format_injected_message(guidance))
            )
            yield InjectedMessageEvent(
                content=guidance,
                injection_id=None,
                user_visible=False,
            )

        if not plan_start_emitted and (
            force_plan_for_turn
            or _should_emit_plan_start(messages, tools, plan_start_text=plan_start_text)
        ):
            plan_start_emitted = True
            approval = (
                _plan_approval_payload(
                    request_id=plan_approval_request_id,
                    state="drafting",
                    plan_id="pending",
                )
                if plan_approval_gate_enabled
                else None
            )
            yield PlanSnapshotEvent(payload=_plan_start_payload(approval))

        for tool_name, limit in TOOL_CALL_LIMITS.items():
            if (
                tool_call_counts.get(tool_name, 0) >= limit
                and tool_name not in tool_budget_wrapup_injected
            ):
                tool_budget_wrapup_injected.add(tool_name)
                budget_text = tool_call_budget_wrapup_text(tool_name, limit)
                messages.append(
                    Message(role="user", content=format_injected_message(budget_text))
                )
                yield InjectedMessageEvent(content=budget_text, injection_id=None, user_visible=False)

        # ── Micro-compact (Layer 1) ────────────────────────
        # Cheap: replace old tool results with placeholders
        _micro_compact(messages)

        # ── Summarization (Layer 2) ────────────────────────
        result = await _maybe_summarize(llm, messages, token_limit, api_total_tokens, skip_next_token_check, session_id=session_id)
        new_msgs, skip_next_token_check, est_before = result
        if new_msgs is not None:
            # Snapshot messages before compression, then extract in background
            if memory_extractor:
                _snapshot = list(messages)
                asyncio.create_task(memory_extractor.maybe_extract(_snapshot, "pre_summarize"))
            yield SummarizationEvent(estimated_tokens=est_before, api_tokens=api_total_tokens, token_limit=token_limit)
            messages.clear()
            messages.extend(new_msgs)

        # ── Near-limit wrap-up nudge (one-shot) ─────────────
        # Reserve the final few steps for synthesis: stop further
        # research and force a self-contained answer from gathered
        # material before the step budget is exhausted.
        if (
            not wrapup_injected
            and max_steps > WRAPUP_REMAINING
            and step >= max_steps - WRAPUP_REMAINING
        ):
            wrapup_injected = True
            wrapup_text = near_limit_wrapup_text(step, max_steps)
            messages.append(
                Message(role="user", content=format_injected_message(wrapup_text))
            )
            yield InjectedMessageEvent(content=wrapup_text, injection_id=None, user_visible=False)

        # ── No-progress circuit breaker (one-shot) ──────────
        # The agent has gone no_progress_limit consecutive steps without a
        # single useful tool result. Stop the flailing and force a synthesis
        # from whatever was gathered, rather than burning the rest of the
        # step budget on the same failing approach.
        if (
            not wrapup_injected
            and no_progress_limit
            and no_progress_steps >= no_progress_limit
        ):
            wrapup_injected = True
            stall_text = no_progress_wrapup_text(no_progress_steps)
            messages.append(
                Message(role="user", content=format_injected_message(stall_text))
            )
            yield InjectedMessageEvent(content=stall_text, injection_id=None, user_visible=False)

        # ── Step start ──────────────────────────────────────
        yield StepStart(step=step + 1, max_steps=max_steps)
        if hook_mgr.hooks:
            await hook_mgr.fire_step_start(step=step + 1, max_steps=max_steps)

        # ── LLM call (streaming) ──────────────────────────────
        tool_list = list(tools.values())
        if logger:
            logger.log_request(messages=messages, tools=tool_list)

        llm_debug_sink_token = (
            set_llm_debug_sink(logger.log_llm_debug_record) if logger else None
        )
        try:
            # Stream thinking and visible text deltas as soon as the provider
            # yields them. Structured progress surfaces such as plan/todo are
            # emitted as separate events, so visible text does not need a
            # leading buffer to protect host UI ordering.
            text_content = ""
            thinking_content = ""
            finish_event: StreamEvent | None = None
            thinking_header_yielded = False

            async for chunk in llm.generate_stream(
                messages=messages, tools=tool_list, thinking_enabled=thinking_enabled,
                session_id=session_id,
            ):
                if cancelled():
                    break
                if chunk.type == "thinking":
                    if not thinking_header_yielded:
                        yield ThinkingEvent(content="", _streaming=True, _header=True)
                        thinking_header_yielded = True
                    thinking_content += chunk.delta
                    yield ThinkingEvent(content=chunk.delta, _streaming=True)
                elif chunk.type == "text":
                    text_content += chunk.delta
                    yield ContentEvent(content=chunk.delta, _streaming=True)
                elif chunk.type == "finish":
                    finish_event = chunk

            if cancelled():
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                return

            if finish_event is None:
                msg = "LLM stream ended without a finish event"
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                    await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
                yield ErrorEvent(message=msg, is_fatal=True)
                yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
                return

            # Build LLMResponse equivalent from streamed data
            response = LLMResponse(
                content=text_content,
                thinking=thinking_content if thinking_content else None,
                tool_calls=finish_event.tool_calls,
                finish_reason=finish_event.finish_reason or "stop",
                usage=finish_event.usage,
                truncated_tool_calls=finish_event.truncated_tool_calls,
            )
            provider_request_id = finish_event.provider_request_id
            yield LLMOutputEvent(
                step=step + 1,
                content=response.content,
                thinking=response.thinking,
                tool_calls=[tc.model_dump() for tc in response.tool_calls] if response.tool_calls else None,
                finish_reason=response.finish_reason,
                usage=response.usage.model_dump() if response.usage else None,
                provider_request_id=provider_request_id,
            )

        except Exception as exc:
            from .llm.error_messages import classify_llm_error
            from .retry import StreamInterrupted

            provider_request_id = None
            if isinstance(exc, StreamInterrupted):
                partial_text = exc.partial_text or ""
                partial_thinking = exc.partial_thinking or ""
                if partial_text or partial_thinking:
                    messages.append(
                        Message(
                            role="assistant",
                            content=partial_text,
                            thinking=partial_thinking or None,
                            tool_calls=None,
                        )
                    )
                msg = (
                    f"LLM stream interrupted: {exc.last_exception!s} "
                    f"(preserved partial content: {len(partial_text)} chars text, "
                    f"{len(partial_thinking)} chars thinking)"
                )
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=False, exception=exc)
                    await hook_mgr.fire_done(stop_reason=StopReason.INTERRUPTED, final_content=partial_text)
                yield ErrorEvent(message=msg, is_fatal=False, exception=exc)
                yield DoneEvent(stop_reason=StopReason.INTERRUPTED, final_content=partial_text)
                return
            # classify_llm_error unwraps RetryExhaustedError to inspect the
            # underlying provider error.
            friendly = classify_llm_error(exc)
            msg = friendly.message
            if friendly.is_soft:
                # Model refusal (e.g. content moderation): present as a normal
                # assistant reply — the turn ended cleanly, it's not a crash.
                # No "Error:" prefix, no red banner; persisted to history.
                messages.append(Message(role="assistant", content=msg, tool_calls=None))
                if hook_mgr.hooks:
                    await hook_mgr.fire_done(stop_reason=StopReason.END_TURN, final_content=msg)
                yield ContentEvent(content=msg)
                yield DoneEvent(stop_reason=StopReason.END_TURN, final_content=msg)
                return
            if hook_mgr.hooks:
                await hook_mgr.fire_error(message=msg, is_fatal=True, exception=exc)
                await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
            yield ErrorEvent(message=msg, is_fatal=True, exception=exc)
            yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
            return
        finally:
            if llm_debug_sink_token is not None:
                reset_llm_debug_sink(llm_debug_sink_token)

        # ── Token tracking ──────────────────────────────────
        if response.usage:
            api_total_tokens = response.usage.total_tokens
            yield TokenUsageEvent(total_tokens=api_total_tokens)

        # ── Hook: LLM response ─────────────────────────────
        if hook_mgr.hooks:
            await hook_mgr.fire_llm_response(response=response)

        # ── Log response ────────────────────────────────────
        if logger:
            logger.log_response(
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
                finish_reason=response.finish_reason,
                usage=response.usage,
                provider_request_id=provider_request_id,
            )

        # ── Suspected-truncation diagnostic (always on) ─────
        # A normal finish ("stop"/"end_turn"/None) with no tool calls but a
        # body that ends mid-thought means the provider likely clipped the
        # turn without admitting it (vs the honest "length" path below).
        # Logged unconditionally — independent of the continuation feature —
        # so the frequency is visible in box-agent-stderr.log for triage.
        if (
            not response.tool_calls
            and response.finish_reason in (None, "stop", "end_turn")
            and response.content
            and reply_is_substantial(
                len(response.content),
                response.usage.completion_tokens if response.usage else None,
            )
            and looks_like_truncated_output(response.content)
        ):
            _tail = response.content.rstrip()[-40:]
            _log.warning(
                "suspected_truncation: finish_reason=%r completion_tokens=%s "
                "content_len=%d request_id=%s tail=%r",
                response.finish_reason,
                response.usage.completion_tokens if response.usage else None,
                len(response.content),
                provider_request_id,
                _tail,
            )

        # ── Append assistant message ────────────────────────
        assistant_msg = Message(
            role="assistant",
            content=response.content,
            thinking=response.thinking,
            tool_calls=_tool_calls_for_model_history(response.tool_calls),
        )
        messages.append(assistant_msg)

        # ── Output truncated by provider token limit ────────
        # finish_reason="length" means the LLM was cut off mid-response — for
        # tool-calling models this often means tool_call arguments are
        # incomplete (invalid JSON dropped by the client). Continuing would
        # either feed the model empty/partial args and trigger a retry loop,
        # or run a tool with the wrong arguments. Abort the turn with a
        # clear reason instead.
        if response.finish_reason in ("length", "max_tokens"):
            # Consolidate the diagnostics that already flow through the stream
            # but were previously invisible unless BOX_AGENT_LLM_DEBUG was on.
            # This is the only place we can confirm whether the gateway clipped
            # max_tokens below what we requested (completion_tokens ≈ effective
            # cap) and *what* the model was writing when cut off.
            usage = response.usage
            diag_parts: list[str] = []
            if usage is not None:
                diag_parts.append(f"completion_tokens={usage.completion_tokens}")
                diag_parts.append(f"total_tokens={usage.total_tokens}")
            requested_max = getattr(llm, "max_output_tokens", None)
            if requested_max is not None:
                diag_parts.append(f"requested_max_tokens={requested_max}")
            if provider_request_id:
                diag_parts.append(f"request_id={provider_request_id}")
            if response.truncated_tool_calls:
                rendered = ", ".join(
                    f"{tc.get('name') or '?'}(args≈{tc.get('arguments_len', 0)} chars)"
                    for tc in response.truncated_tool_calls
                )
                diag_parts.append(f"truncated_tool_calls=[{rendered}]")
            diag = ("  Diagnostics: " + "; ".join(diag_parts)) if diag_parts else ""
            msg = (
                "LLM output truncated by provider max_tokens limit. "
                "Tool-call arguments may be incomplete. Try a smaller task "
                "per turn (e.g. write the file in sections instead of one call) "
                "or raise the provider's output token limit." + diag
            )
            _cleanup_incomplete_messages(messages)
            if hook_mgr.hooks:
                await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                await hook_mgr.fire_done(stop_reason=StopReason.MAX_TOKENS, final_content=msg)
            yield ErrorEvent(message=msg, is_fatal=True)
            yield DoneEvent(stop_reason=StopReason.MAX_TOKENS, final_content=msg)
            return

        # ── No tool calls → done (or continue if injected) ──
        if not response.tool_calls:
            if (
                force_plan_for_turn
                and "plan_write" not in succeeded_tools
                and not forced_plan_retry_injected
            ):
                forced_plan_retry_injected = True
                messages.append(
                    Message(
                        role="user",
                        content=format_injected_message(_FORCED_PLAN_RETRY_GUIDANCE),
                    )
                )
                yield InjectedMessageEvent(
                    content=_FORCED_PLAN_RETRY_GUIDANCE,
                    injection_id=None,
                    user_visible=False,
                )
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                continue

            # Check inject queue — if messages are pending, continue
            # the loop so the LLM sees them on the next iteration.
            if inject_queue and not inject_queue.empty():
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                continue

            # ── Completion gate (opt-in) ────────────────────
            # Intercept this natural END_TURN: if a verifiable requirement is
            # unmet and we're still within the continuation/time budget, inject
            # a continuation nudge naming the gaps and keep looping instead of
            # finishing. The bounded counter + optional deadline guarantee the
            # gate releases rather than trapping the agent forever.
            if (
                completion_gate is not None
                and gate_continuations < completion_gate.max_continuations
                and (
                    completion_gate.deadline_seconds is None
                    or (perf_counter() - run_start) < completion_gate.deadline_seconds
                )
            ):
                gaps = completion_gate_gaps(completion_gate, succeeded_tools, workspace_dir)
                if gaps:
                    gate_continuations += 1
                    nudge = completion_gate_text(gaps)
                    messages.append(
                        Message(role="user", content=format_injected_message(nudge))
                    )
                    yield InjectedMessageEvent(content=nudge, injection_id=None, user_visible=False)
                    elapsed = perf_counter() - step_start
                    total = perf_counter() - run_start
                    if hook_mgr.hooks:
                        await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                    yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                    continue

            # ── Suspected-truncation continuation (opt-in) ──
            # The provider reported a normal finish with no tool calls, but
            # the body ends mid-thought. Re-prompt once (bounded) to finish
            # the reply in the *same* message: the truncated assistant text
            # is already appended above, and we do NOT emit a DoneEvent, so
            # the continuation streams into the same prompt turn. Skipped for
            # short replies (legitimately end without punctuation).
            if (
                truncation_continuation_enabled
                and truncation_continuations < max_truncation_continuations
                and response.finish_reason in (None, "stop", "end_turn")
                and response.content.strip()
                and reply_is_substantial(
                    len(response.content),
                    response.usage.completion_tokens if response.usage else None,
                )
                and looks_like_truncated_output(response.content)
            ):
                truncation_continuations += 1
                tail = response.content.rstrip()[-40:]
                cont_text = truncation_continuation_text(tail)
                messages.append(Message(role="user", content=cont_text))
                yield InjectedMessageEvent(content=cont_text, injection_id=None, user_visible=False)
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                continue

            if (
                visible_tool_call_total > FINAL_SUMMARY_TOOL_CALL_THRESHOLD
                and final_summary_guidance_injected
                and not final_summary_empty_retry_injected
                and not response.content.strip()
            ):
                final_summary_empty_retry_injected = True
                retry_text = final_summary_empty_retry_text(visible_tool_call_total)
                messages.append(Message(role="user", content=format_injected_message(retry_text)))
                yield InjectedMessageEvent(content=retry_text, injection_id=None, user_visible=False)
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(
                        step=step + 1,
                        elapsed_seconds=elapsed,
                        total_elapsed_seconds=total,
                    )
                yield StepEnd(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                continue

            elapsed = perf_counter() - step_start
            total = perf_counter() - run_start
            if hook_mgr.hooks:
                await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                await hook_mgr.fire_done(stop_reason=StopReason.END_TURN, final_content=response.content)
            # Extract memory at agent loop end (background)
            if memory_extractor:
                asyncio.create_task(memory_extractor.maybe_extract(messages, "loop_end"))
            yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
            proposal = await _build_proposal_event_with_plan()
            if proposal is not None:
                yield proposal
            yield DoneEvent(stop_reason=StopReason.END_TURN, final_content=response.content)
            return

        # ── Cancellation check (before tools) ──────────────
        if cancelled():
            _cleanup_incomplete_messages(messages)
            if hook_mgr.hooks:
                await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            return

        # ── Execute tool calls ──────────────────────────────
        # Loop-guard: bail out if the model emits the same all-empty-args
        # tool_call set as the previous turn. This is the signature of an
        # upstream protocol bug (e.g. relay truncation) where empty args
        # come back, error responses get fed back, and the model just
        # repeats — without this check the loop runs to max_steps.
        all_empty = all(not tc.function.arguments for tc in response.tool_calls)
        if all_empty:
            sig = tuple(sorted(tc.function.name for tc in response.tool_calls))
            if sig == empty_args_signature:
                empty_args_repeats += 1
            else:
                empty_args_signature = sig
                empty_args_repeats = 1
            if empty_args_repeats >= EMPTY_ARGS_LIMIT:
                msg = (
                    f"Aborting: model emitted empty-arguments tool_calls "
                    f"{empty_args_repeats}x in a row ({list(sig)}). "
                    "This usually indicates an upstream relay bug or model "
                    "loop. See logs for the raw stream."
                )
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                    await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
                yield ErrorEvent(message=msg, is_fatal=True)
                yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
                return
        else:
            empty_args_signature = None
            empty_args_repeats = 0

        # Split into regular (sequential) and parallel_safe groups.
        regular_calls = []
        parallel_calls = []
        for tc in response.tool_calls:
            fn_name = tc.function.name
            if fn_name in tools and getattr(tools[fn_name], "parallel_safe", False):
                parallel_calls.append(tc)
            else:
                regular_calls.append(tc)

        step_contains_plan_write = any(
            tc.function.name == "plan_write" for tc in [*regular_calls, *parallel_calls]
        )
        organic_plan_approval_gate_enabled = (
            pause_after_plan_write
            and not plan_approval_approved
            and not plan_approval_gate_enabled
            and has_plan_tool
            and step_contains_plan_write
        )
        plan_approval_gate_active = (
            plan_approval_gate_enabled or organic_plan_approval_gate_enabled
        )

        # Track whether this step produced any useful tool result, for the
        # no-progress circuit breaker. Set True in either execution branch.
        step_made_progress = False

        def _reserve_tool_budget(tool_name: str) -> tuple[bool, str | None]:
            limit = TOOL_CALL_LIMITS.get(tool_name)
            if limit is None:
                return True, None
            if tool_call_counts.get(tool_name, 0) >= limit:
                return False, tool_call_budget_message(tool_name, limit)
            tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1
            return True, None

        def _reserve_web_search_call(arguments: dict[str, Any]) -> tuple[bool, str | None]:
            nonlocal web_search_step_seen
            nonlocal web_search_step_executed
            nonlocal web_search_step_deferred
            nonlocal web_search_step_duplicate_queries

            web_search_step_seen = True
            query_key = _normalize_web_search_query(arguments)
            if query_key and query_key in web_search_seen_queries:
                web_search_step_duplicate_queries += 1
                return (
                    False,
                    "Duplicate web_search query skipped by runtime batching. "
                    "Use the evidence already returned for this query and search only remaining gaps.",
                )
            if web_search_step_executed >= WEB_SEARCH_BATCH_SIZE:
                web_search_step_deferred += 1
                return (
                    False,
                    f"web_search deferred by runtime batching (batch size {WEB_SEARCH_BATCH_SIZE}). "
                    "Review the current batch results and re-issue only still-missing, non-duplicate queries.",
                )

            allowed_by_budget, budget_error = _reserve_tool_budget(WEB_SEARCH_TOOL_NAME)
            if not allowed_by_budget:
                return False, budget_error
            if query_key:
                web_search_seen_queries.add(query_key)
            web_search_step_executed += 1
            return True, None

        # 1. Sequential execution for regular tools (preserves ordering)
        for tc in regular_calls:
            tc_id = tc.id
            fn_name = tc.function.name
            fn_args = tc.function.arguments

            if plan_approval_gate_active and fn_name != "plan_write":
                allowed_to_execute = False
                internal_skip_error = _PLAN_APPROVAL_SKIP_MESSAGE
            elif fn_name == WEB_SEARCH_TOOL_NAME:
                allowed_to_execute, internal_skip_error = _reserve_web_search_call(fn_args)
            else:
                allowed_to_execute, internal_skip_error = _reserve_tool_budget(fn_name)
            tool_user_visible = allowed_to_execute
            if tool_user_visible and fn_name not in FINAL_SUMMARY_EXCLUDED_TOOLS:
                visible_tool_call_total += 1

            yield ToolCallStart(
                tool_call_id=tc_id,
                tool_name=fn_name,
                arguments=fn_args,
                user_visible=tool_user_visible,
            )

            # Hook: tool start (interceptor — may modify arguments)
            if hook_mgr.hooks and tool_user_visible:
                fn_args = await hook_mgr.fire_tool_start(
                    tool_call_id=tc_id, tool_name=fn_name, arguments=fn_args,
                )

            # Snapshot workspace before tool execution for diff-based artifact detection
            pre_files: set[Path] = set()
            if artifact_detection_enabled and tool_user_visible and workspace_dir:
                pre_files = _snapshot_workspace(workspace_dir, artifact_root_dir)

            if not allowed_to_execute:
                result = ToolResult(success=False, content="", error=internal_skip_error or "")
            elif fn_name not in tools:
                result = ToolResult(success=False, content="", error=f"Unknown tool: {fn_name}")
            else:
                tool = tools[fn_name]
                if isinstance(tool, EventEmittingTool):
                    # Wire queue, run in background, drain in foreground
                    event_queue: asyncio.Queue = asyncio.Queue()
                    tool._event_queue = event_queue
                    tool._parent_tool_call_id = tc_id

                    exec_done = asyncio.Event()
                    exec_result: ToolResult | None = None

                    async def _seq_exec(t=tool, a=fn_args):
                        nonlocal exec_result
                        try:
                            exec_result = await t.execute(**a)
                        except Exception as exc:
                            detail = f"{type(exc).__name__}: {exc!s}"
                            trace = traceback.format_exc()
                            exec_result = ToolResult(
                                success=False,
                                content="",
                                error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
                            )
                        finally:
                            exec_done.set()

                    exec_task = asyncio.create_task(_seq_exec())
                    while not exec_done.is_set() or not event_queue.empty():
                        try:
                            evt = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                            yield evt
                        except (asyncio.TimeoutError, TimeoutError):
                            continue
                    while not event_queue.empty():
                        yield event_queue.get_nowait()
                    await exec_task
                    tool._event_queue = None
                    tool._parent_tool_call_id = ""
                    result = exec_result  # type: ignore[assignment]
                else:
                    try:
                        result = await tools[fn_name].execute(**fn_args)
                    except Exception as exc:
                        detail = f"{type(exc).__name__}: {exc!s}"
                        trace = traceback.format_exc()
                        result = ToolResult(
                            success=False,
                            content="",
                            error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
                        )

            if plan_approval_gate_active and fn_name == "plan_write" and result.success:
                result = result.model_copy(
                    update={
                        "raw_output": _attach_plan_approval_payload(
                            result.raw_output,
                            request_id=plan_approval_request_id,
                        )
                    }
                )
                plan_approval_gate_completed = True

            policy_decision: dict[str, Any] | None = None
            # Log tool result
            if logger:
                logger.log_tool_result(
                    tool_name=fn_name,
                    arguments=fn_args,
                    result_success=result.success,
                    result_content=result.content if result.success else None,
                    result_error=result.error if not result.success else None,
                    raw_output=result.raw_output,
                )

            # ── Permission negotiation + retry ──────────────
            if not result.success and result.permission_request and permission_negotiator:
                policy_decision = _policy_decision_payload(
                    tool_name=fn_name,
                    permission_request=result.permission_request,
                    decision="requested",
                )
                try:
                    granted = await permission_negotiator.negotiate(result.permission_request)
                except Exception as exc:
                    policy_decision = _policy_decision_payload(
                        tool_name=fn_name,
                        permission_request=result.permission_request,
                        decision="error",
                        error=str(exc),
                    )
                    _log.warning(
                        "permission/negotiator_error tool=%s error=%s",
                        fn_name,
                        exc,
                    )
                    granted = False
                if granted:
                    policy_decision = _policy_decision_payload(
                        tool_name=fn_name,
                        permission_request=result.permission_request,
                        decision="approved",
                        retry_count=1,
                    )
                    _approve_tool_permission(tools[fn_name], result.permission_request)
                    try:
                        result = await tools[fn_name].execute(**fn_args)
                    except Exception as exc:
                        detail = f"{type(exc).__name__}: {exc!s}"
                        trace = traceback.format_exc()
                        result = ToolResult(
                            success=False,
                            content="",
                            error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
                        )
                    # Re-log after retry
                    if logger:
                        logger.log_tool_result(
                            tool_name=fn_name,
                            arguments=fn_args,
                            result_success=result.success,
                            result_content=result.content if result.success else None,
                            result_error=result.error if not result.success else None,
                            raw_output=result.raw_output,
                        )
                elif policy_decision is not None and policy_decision.get("decision") != "error":
                    policy_decision = _policy_decision_payload(
                        tool_name=fn_name,
                        permission_request=result.permission_request,
                        decision="denied",
                    )
            elif not result.success and result.permission_request:
                policy_decision = _policy_decision_payload(
                    tool_name=fn_name,
                    permission_request=result.permission_request,
                    decision="requested",
                )

            # Progress signal for the no-progress breaker: a successful tool
            # call with non-empty content counts as making progress.
            if result.success and (result.content or "").strip():
                step_made_progress = True
                succeeded_tools.add(fn_name)

            # Hook: tool result (interceptor — may modify content/error)
            tc_content = result.content
            tc_error = result.error
            if hook_mgr.hooks and tool_user_visible:
                tc_content, tc_error = await hook_mgr.fire_tool_result(
                    tool_call_id=tc_id, tool_name=fn_name,
                    success=result.success, content=tc_content, error=tc_error,
                )

            if result.success and fn_name == WEB_SEARCH_TOOL_NAME:
                (
                    tc_content,
                    new_count,
                    duplicate_count,
                    new_labels,
                    inspected,
                ) = _dedupe_web_search_content(tc_content, web_search_seen_result_keys)
                web_search_step_new_results += new_count
                web_search_step_duplicate_results += duplicate_count
                web_search_unique_results += new_count
                web_search_duplicate_results += duplicate_count
                if inspected:
                    web_search_step_structured_results += 1
                web_search_step_labels.extend(new_labels[:3])

            # Append the tool message BEFORE yielding any events. The yields
            # below hand control back to the consumer, which may suspend or
            # raise; if we yielded first and only appended on resumption,
            # the conversation could be left with an assistant tool_calls
            # message that has no matching tool response — a fatal protocol
            # state for the next LLM call.
            msg_content = _tool_message_content_for_model(
                tool_name=fn_name,
                arguments=fn_args,
                result=result,
                visible_content=tc_content,
                visible_error=tc_error,
            )
            tool_msg = Message(
                role="tool",
                content=msg_content,
                tool_call_id=tc_id,
                name=fn_name,
            )
            messages.append(tool_msg)

            yield ToolCallResult(
                tool_call_id=tc_id,
                tool_name=fn_name,
                success=result.success,
                content=tc_content,
                error=tc_error,
                raw_output=result.raw_output,
                user_visible=tool_user_visible,
                policy_decision=policy_decision,
            )
            if result.success and tool_user_visible:
                web_search_payload = _extract_web_search_payload(fn_name, tc_content)
                if web_search_payload is not None:
                    yield WebSearchEvent(tool_call_id=tc_id, payload=web_search_payload)

            # Emit permission request event if tool was denied with escalation info
            # (only for legacy consumers without a negotiator)
            if not result.success and result.permission_request and not permission_negotiator:
                yield PermissionRequestEvent(
                    tool_call_id=tc_id,
                    **_permission_event_kwargs(result.permission_request),
                )

            # Detect and yield structured artifacts (images, files) from tool output
            if artifact_detection_enabled and result.success and workspace_dir:
                post_files = _snapshot_workspace(workspace_dir, artifact_root_dir)
                for artifact in _detect_tool_artifacts(
                    tc_id,
                    fn_name,
                    tc_content,
                    result.raw_output,
                    pre_files,
                    post_files,
                    workspace_dir,
                    artifact_root_dir,
                ):
                    yield artifact

            # Cancellation check after each tool
            if cancelled():
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                return

        # 2. Parallel execution for parallel_safe tools (e.g. sub_agent)
        if parallel_calls:
            # Snapshot the workspace BEFORE any parallel tool runs. Per-tool
            # snapshots are impossible under concurrency, so the diff layer uses
            # one pre/post pair for the whole batch (see after the result loop).
            par_pre_files: set[Path] = set()
            if artifact_detection_enabled and workspace_dir:
                par_pre_files = _snapshot_workspace(workspace_dir, artifact_root_dir)
            # Emit all ToolCallStart events and apply hook interceptors
            par_args_map: dict[str, dict[str, Any]] = {}  # tc.id → (possibly modified) args
            par_budget_errors: dict[str, str] = {}
            par_user_visible: dict[str, bool] = {}
            for tc in parallel_calls:
                par_fn_args = tc.function.arguments
                if plan_approval_gate_active and tc.function.name != "plan_write":
                    allowed_to_execute = False
                    internal_skip_error = _PLAN_APPROVAL_SKIP_MESSAGE
                elif tc.function.name == WEB_SEARCH_TOOL_NAME:
                    allowed_to_execute, internal_skip_error = _reserve_web_search_call(par_fn_args)
                else:
                    allowed_to_execute, internal_skip_error = _reserve_tool_budget(tc.function.name)
                par_user_visible[tc.id] = allowed_to_execute
                if allowed_to_execute and tc.function.name not in FINAL_SUMMARY_EXCLUDED_TOOLS:
                    visible_tool_call_total += 1
                yield ToolCallStart(
                    tool_call_id=tc.id,
                    tool_name=tc.function.name,
                    arguments=par_fn_args,
                    user_visible=allowed_to_execute,
                )
                if hook_mgr.hooks and allowed_to_execute:
                    par_fn_args = await hook_mgr.fire_tool_start(
                        tool_call_id=tc.id, tool_name=tc.function.name, arguments=par_fn_args,
                    )
                par_args_map[tc.id] = par_fn_args
                if not allowed_to_execute:
                    par_budget_errors[tc.id] = internal_skip_error or ""

            # Wire a shared event queue onto EventEmittingTool instances
            par_event_queue: asyncio.Queue[SubAgentEvent] = asyncio.Queue()
            emitting_tools: list[EventEmittingTool] = []
            for tc in parallel_calls:
                tool = tools.get(tc.function.name)
                if isinstance(tool, EventEmittingTool):
                    tool._event_queue = par_event_queue
                    tool._parent_tool_call_id = tc.id
                    emitting_tools.append(tool)

            # Hard concurrency cap: even if the model emits dozens of
            # parallel_safe calls in one step, only max_parallel_tools run at
            # once; the rest queue on the semaphore. Bounds resource use (LLM
            # rate limits, subprocesses, memory) against runaway fan-out.
            par_semaphore = asyncio.Semaphore(max(1, max_parallel_tools))

            async def _run_parallel(tc):
                fn_name = tc.function.name
                fn_args = par_args_map[tc.id]
                if tc.id in par_budget_errors:
                    return tc, ToolResult(success=False, content="", error=par_budget_errors[tc.id])
                if fn_name not in tools:
                    return tc, ToolResult(success=False, content="", error=f"Unknown tool: {fn_name}")
                try:
                    async with par_semaphore:
                        r = await tools[fn_name].execute(**fn_args)
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc!s}"
                    trace = traceback.format_exc()
                    r = ToolResult(
                        success=False,
                        content="",
                        error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
                    )
                return tc, r

            # Run gather in a background task; drain the queue in the
            # foreground generator loop so events are yielded in real-time.
            gather_done = asyncio.Event()
            per_tc_tasks: dict[str, asyncio.Task] = {}

            async def _gather_wrapper():
                try:
                    coros = []
                    for tc in parallel_calls:
                        t = asyncio.ensure_future(_run_parallel(tc))
                        per_tc_tasks[tc.id] = t
                        coros.append(t)
                    return await asyncio.gather(*coros, return_exceptions=True)
                finally:
                    gather_done.set()

            gather_task = asyncio.create_task(_gather_wrapper())

            # Yield progress events as they arrive (real-time). Bail out early
            # on cooperative cancellation so the in-flight tools don't block
            # progress reporting back to the host.
            cancel_observed = False
            while not gather_done.is_set() or not par_event_queue.empty():
                try:
                    evt = await asyncio.wait_for(par_event_queue.get(), timeout=0.1)
                    yield evt
                except (asyncio.TimeoutError, TimeoutError):
                    if cancelled() and not cancel_observed:
                        cancel_observed = True
                        for t in per_tc_tasks.values():
                            if not t.done():
                                t.cancel()
                    continue
            # Drain any stragglers enqueued between the last get() and now
            while not par_event_queue.empty():
                yield par_event_queue.get_nowait()

            try:
                gathered_raw = await gather_task  # already done
            except Exception:
                gathered_raw = []

            # Build {tc_id: (tc, ToolResult)} mapping from gather output.
            # asyncio.gather(..., return_exceptions=True) hands back the
            # exception object for any task that raised — including
            # CancelledError from cooperative cancellation above.
            results_by_id: dict[str, tuple[Any, ToolResult]] = {}
            for tc_obj, raw in zip(parallel_calls, gathered_raw or []):
                if isinstance(raw, BaseException):
                    if isinstance(raw, asyncio.CancelledError):
                        err = "Tool execution cancelled before completion."
                    else:
                        err = f"Tool execution failed: {type(raw).__name__}: {raw!s}"
                    results_by_id[tc_obj.id] = (tc_obj, ToolResult(success=False, content="", error=err))
                elif isinstance(raw, tuple) and len(raw) == 2:
                    results_by_id[raw[0].id] = (raw[0], raw[1])

            # Ensure every parallel tc has a result entry — synthesize a stub
            # if gather returned short for any reason. This guarantees one
            # ToolCallResult event + one tool message per ToolCallStart event.
            for tc_obj in parallel_calls:
                if tc_obj.id not in results_by_id:
                    results_by_id[tc_obj.id] = (
                        tc_obj,
                        ToolResult(
                            success=False,
                            content="",
                            error="Tool execution interrupted — no result returned.",
                        ),
                    )

            gathered = [results_by_id[tc.id] for tc in parallel_calls]

            # Clean up queue references
            for tool in emitting_tools:
                tool._event_queue = None
                tool._parent_tool_call_id = ""

            # Accumulates absolute paths surfaced by the per-result regex layer
            # (and artifact raw_outputs), so the single post-batch diff pass
            # below doesn't re-emit them.
            par_already_emitted: set[str] = set()

            for tc, result in gathered:
                tc_id = tc.id
                fn_name = tc.function.name
                fn_args = par_args_map[tc_id]
                tool_user_visible = par_user_visible.get(tc_id, True)
                policy_decision: dict[str, Any] | None = None

                if plan_approval_gate_active and fn_name == "plan_write" and result.success:
                    result = result.model_copy(
                        update={
                            "raw_output": _attach_plan_approval_payload(
                                result.raw_output,
                                request_id=plan_approval_request_id,
                            )
                        }
                    )
                    plan_approval_gate_completed = True

                if logger:
                    logger.log_tool_result(
                        tool_name=fn_name,
                        arguments=fn_args,
                        result_success=result.success,
                        result_content=result.content if result.success else None,
                        result_error=result.error if not result.success else None,
                        raw_output=result.raw_output,
                    )

                # ── Permission negotiation + retry ──────────────
                if not result.success and result.permission_request and permission_negotiator:
                    policy_decision = _policy_decision_payload(
                        tool_name=fn_name,
                        permission_request=result.permission_request,
                        decision="requested",
                    )
                    try:
                        granted = await permission_negotiator.negotiate(result.permission_request)
                    except Exception as exc:
                        policy_decision = _policy_decision_payload(
                            tool_name=fn_name,
                            permission_request=result.permission_request,
                            decision="error",
                            error=str(exc),
                        )
                        _log.warning(
                            "permission/negotiator_error tool=%s error=%s",
                            fn_name,
                            exc,
                        )
                        granted = False
                    if granted:
                        policy_decision = _policy_decision_payload(
                            tool_name=fn_name,
                            permission_request=result.permission_request,
                            decision="approved",
                            retry_count=1,
                        )
                        _approve_tool_permission(tools[fn_name], result.permission_request)
                        try:
                            result = await tools[fn_name].execute(**fn_args)
                        except Exception as exc:
                            detail = f"{type(exc).__name__}: {exc!s}"
                            trace = traceback.format_exc()
                            result = ToolResult(
                                success=False,
                                content="",
                                error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
                            )
                        if logger:
                            logger.log_tool_result(
                                tool_name=fn_name,
                                arguments=fn_args,
                                result_success=result.success,
                                result_content=result.content if result.success else None,
                                result_error=result.error if not result.success else None,
                                raw_output=result.raw_output,
                            )
                    elif policy_decision is not None and policy_decision.get("decision") != "error":
                        policy_decision = _policy_decision_payload(
                            tool_name=fn_name,
                            permission_request=result.permission_request,
                            decision="denied",
                        )
                elif not result.success and result.permission_request:
                    policy_decision = _policy_decision_payload(
                        tool_name=fn_name,
                        permission_request=result.permission_request,
                        decision="requested",
                    )

                # Progress signal for the no-progress breaker.
                if result.success and (result.content or "").strip():
                    step_made_progress = True
                    succeeded_tools.add(fn_name)

                # Hook: tool result (interceptor)
                par_content = result.content
                par_error = result.error
                if hook_mgr.hooks and tool_user_visible:
                    par_content, par_error = await hook_mgr.fire_tool_result(
                        tool_call_id=tc_id, tool_name=fn_name,
                        success=result.success, content=par_content, error=par_error,
                    )

                if result.success and fn_name == WEB_SEARCH_TOOL_NAME:
                    (
                        par_content,
                        new_count,
                        duplicate_count,
                        new_labels,
                        inspected,
                    ) = _dedupe_web_search_content(par_content, web_search_seen_result_keys)
                    web_search_step_new_results += new_count
                    web_search_step_duplicate_results += duplicate_count
                    web_search_unique_results += new_count
                    web_search_duplicate_results += duplicate_count
                    if inspected:
                        web_search_step_structured_results += 1
                    web_search_step_labels.extend(new_labels[:3])

                # Append the tool message BEFORE yielding any events — see
                # the equivalent comment in the sequential branch above for
                # the protocol-state rationale.
                msg_content = _tool_message_content_for_model(
                    tool_name=fn_name,
                    arguments=fn_args,
                    result=result,
                    visible_content=par_content,
                    visible_error=par_error,
                )
                tool_msg = Message(
                    role="tool",
                    content=msg_content,
                    tool_call_id=tc_id,
                    name=fn_name,
                )
                messages.append(tool_msg)

                yield ToolCallResult(
                    tool_call_id=tc_id,
                    tool_name=fn_name,
                    success=result.success,
                    content=par_content,
                    error=par_error,
                    raw_output=result.raw_output,
                    user_visible=tool_user_visible,
                    policy_decision=policy_decision,
                )
                if result.success and tool_user_visible:
                    web_search_payload = _extract_web_search_payload(fn_name, par_content)
                    if web_search_payload is not None:
                        yield WebSearchEvent(tool_call_id=tc_id, payload=web_search_payload)

                # Emit permission request event if tool was denied with escalation info
                # (only for legacy consumers without a negotiator)
                if not result.success and result.permission_request and not permission_negotiator:
                    yield PermissionRequestEvent(
                        tool_call_id=tc_id,
                        **_permission_event_kwargs(result.permission_request),
                    )

                # Artifact detection — layer 1 (regex) per result. The diff
                # layer runs once after the loop (single batch snapshot).
                if artifact_detection_enabled and result.success and tool_user_visible and workspace_dir:
                    regex_artifacts, regex_already = _detect_regex_artifacts(
                        tc_id, fn_name, par_content, result.raw_output,
                        workspace_dir, artifact_root_dir,
                    )
                    for artifact in regex_artifacts:
                        yield artifact
                    par_already_emitted |= regex_already

            # Artifact detection — layer 2 (diff), once for the whole batch.
            # Concurrency rules out per-tool snapshots, so new files are
            # attributed to the first parallel call's id.
            if artifact_detection_enabled and workspace_dir and parallel_calls:
                par_post_files = _snapshot_workspace(workspace_dir, artifact_root_dir)
                for artifact in _detect_new_files(
                    parallel_calls[0].id,
                    par_pre_files,
                    par_post_files,
                    par_already_emitted,
                    workspace_dir,
                ):
                    yield artifact

            # Cancellation check after all parallel results emitted — every
            # tool message is now appended, so the message list is in a
            # protocol-valid state for the next turn.
            if cancelled():
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                return

        if plan_approval_gate_completed:
            elapsed = perf_counter() - step_start
            total = perf_counter() - run_start
            if hook_mgr.hooks:
                await hook_mgr.fire_step_end(
                    step=step + 1,
                    elapsed_seconds=elapsed,
                    total_elapsed_seconds=total,
                )
                await hook_mgr.fire_done(
                    stop_reason=StopReason.END_TURN,
                    final_content=_PLAN_APPROVAL_DONE_CONTENT,
                )
            yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
            yield DoneEvent(
                stop_reason=StopReason.END_TURN,
                final_content=_PLAN_APPROVAL_DONE_CONTENT,
            )
            return

        if web_search_step_seen:
            if web_search_step_executed > 0 and web_search_step_structured_results > 0:
                if web_search_step_new_results == 0:
                    web_search_no_new_batches += 1
                else:
                    web_search_no_new_batches = 0

            total_web_search_calls = tool_call_counts.get(WEB_SEARCH_TOOL_NAME, 0)
            guidance_lines = [
                "Search batch controller update (internal; do not mention this controller to the user):",
                (
                    f"- Executed this batch: {web_search_step_executed}; "
                    f"total executed this turn: {total_web_search_calls}/{WEB_SEARCH_TOTAL_LIMIT}; "
                    f"batch size: {WEB_SEARCH_BATCH_SIZE}."
                ),
            ]
            if web_search_step_deferred:
                guidance_lines.append(f"- Deferred this batch: {web_search_step_deferred}.")
            if web_search_step_duplicate_queries:
                guidance_lines.append(f"- Duplicate queries skipped this batch: {web_search_step_duplicate_queries}.")
            if web_search_step_structured_results:
                guidance_lines.append(
                    f"- New structured results this batch: {web_search_step_new_results}; "
                    f"duplicate structured results this batch: {web_search_step_duplicate_results}; "
                    f"unique structured results this turn: {web_search_unique_results}; "
                    f"duplicates filtered this turn: {web_search_duplicate_results}."
                )
            if web_search_step_labels:
                examples = "; ".join(web_search_step_labels[:5])
                guidance_lines.append(f"- New result examples: {examples}.")
            if total_web_search_calls >= WEB_SEARCH_TOTAL_LIMIT:
                guidance_lines.append(
                    "- The web_search total limit has been reached. Do not call web_search again; "
                    "synthesize the final answer from gathered evidence and briefly mark gaps."
                )
            elif web_search_no_new_batches >= 2:
                guidance_lines.append(
                    "- Two consecutive structured search batches added no new results. Stop searching unless "
                    "a critical first-party source is still missing."
                )
            else:
                guidance_lines.append(
                    f"- Before searching again, inspect the deduped evidence. If gaps remain, issue at most "
                    f"{WEB_SEARCH_BATCH_SIZE} new, specific, non-duplicate web_search queries."
                )
            guidance_text = "\n".join(guidance_lines)
            messages.append(Message(role="user", content=format_injected_message(guidance_text)))
            yield InjectedMessageEvent(content=guidance_text, injection_id=None, user_visible=False)

        if (
            visible_tool_call_total > FINAL_SUMMARY_TOOL_CALL_THRESHOLD
            and not final_summary_guidance_injected
        ):
            final_summary_guidance_injected = True
            summary_text = final_summary_wrapup_text(visible_tool_call_total)
            messages.append(Message(role="user", content=format_injected_message(summary_text)))
            yield InjectedMessageEvent(content=summary_text, injection_id=None, user_visible=False)

        # ── Step end ────────────────────────────────────────
        # Update the no-progress counter (only steps that ran tools reach
        # here — the no-tool-call path returns earlier with END_TURN).
        if no_progress_limit:
            if step_made_progress:
                no_progress_steps = 0
            else:
                no_progress_steps += 1

        elapsed = perf_counter() - step_start
        total = perf_counter() - run_start
        yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
        if hook_mgr.hooks:
            await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)

        # ── Periodic memory extraction (background) ──────────
        if memory_extractor:
            asyncio.create_task(memory_extractor.maybe_extract(messages, "step_interval"))

    # ── Max steps exhausted ─────────────────────────────────
    msg = f"Task couldn't be completed after {max_steps} steps."
    if memory_extractor:
        asyncio.create_task(memory_extractor.maybe_extract(messages, "loop_end"))
    if hook_mgr.hooks:
        await hook_mgr.fire_done(stop_reason=StopReason.MAX_STEPS, final_content=msg)
    proposal = await _build_proposal_event_with_plan()
    if proposal is not None:
        yield proposal
    yield DoneEvent(stop_reason=StopReason.MAX_STEPS, final_content=msg)
