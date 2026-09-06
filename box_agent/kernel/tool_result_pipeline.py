"""Tool-result preparation, persistence, normalization, and cleanup helpers."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Final
from urllib.parse import urlsplit

from ..artifacts import (
    artifact_scan_root as _artifact_scan_root,
    make_artifact as _make_artifact,
)
from ..context_resources import (
    ContextResourceLedger,
    ResourceDescriptor,
    build_resource_receipt,
)
from ..evidence import normalize_search_url as _normalize_search_url
from ..events import (
    AgentEvent,
    ArtifactEvent,
    PermissionRequestEvent,
    ToolCallResult,
    WebSearchEvent,
)
from ..model_history import is_model_history_placeholder
from ..schema import Message
from ..session_trace import emit_session_trace
from ..tool_result_storage import ToolResultStorage
from ..tools.base import Tool, ToolResult
from .permission_gateway import _permission_event_kwargs


_log = logging.getLogger("box_agent.core")


_MODEL_HISTORY_PLACEHOLDER_ARGUMENTS: Final[dict[str, tuple[str, ...]]] = {
    "write_file": ("content",),
    "append_file": ("content",),
    "edit_file": ("old_str", "new_str"),
    "execute_code": ("code",),
    "staged_file_write": ("content",),
}
_MODEL_HISTORY_FILE_MUTATION_TOOLS: Final[frozenset[str]] = frozenset(
    {"write_file", "append_file", "edit_file"}
)


_MODEL_HISTORY_PLACEHOLDER_RECOVERY_REQUIRED = (
    "INTERNAL_MODEL_HISTORY_PLACEHOLDER_RECOVERY_REQUIRED: a mutation argument was "
    "replaced by an internal history placeholder, so the intended update did not "
    "happen. Complete that exact mutation with regenerated real content before "
    "calling any downstream tool; do not validate, apply, render, or otherwise reuse "
    "the unchanged target. For a rejected file mutation, either retry a file mutation "
    "with real content for the same target, using ordered write_file chunks when needed."
)


_BROWSER_SNAPSHOT_OUTPUT_PATH_ERROR = (
    "BROWSER_SNAPSHOT_OUTPUT_PATH_INVALID: relative snapshot filenames must stay "
    "inside the current task artifact root. Use a path such as "
    "research/page-snapshot.md, or omit filename when no persisted snapshot is needed."
)


# Regex to match file references like [foo.png] in tool output. Keep the
# candidate bounded: structured tool payloads such as web_search commonly use
# a top-level JSON array, and an unbounded match can otherwise consume the
# entire payload and misclassify it as one enormous filename.
_MAX_ARTIFACT_REF_CHARS = 512
_MAX_ARTIFACT_COMPONENT_BYTES = 255
_ARTIFACT_REF_RE = re.compile(
    r"\[([^\]\n]{1,512}\.\w{1,10})\]",
    re.IGNORECASE,
)


def _prepare_browser_snapshot_output(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> tuple[Path | None, str | None]:
    """Turn a Playwright snapshot filename into Box-Agent-managed persistence.

    Standalone Playwright MCP servers run in their own process and therefore do
    not share Box-Agent's workspace cwd.  They also intentionally restrict file
    writes to their own temp roots.  For a filename inside the current artifact
    root, request an inline snapshot from Playwright and persist that returned
    Markdown in Box-Agent after the tool succeeds.
    """
    if tool_name != "managed_browser_snapshot":
        return None, None
    filename = arguments.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return None, None
    supplied_path = Path(filename).expanduser()
    artifact_root = _artifact_scan_root(workspace_dir, artifact_root_dir)
    if artifact_root is None:
        return None, None
    artifact_root = artifact_root.resolve()
    resolved_path = (
        supplied_path.resolve()
        if supplied_path.is_absolute()
        else (artifact_root / supplied_path).resolve()
    )
    if not resolved_path.is_relative_to(artifact_root):
        if supplied_path.is_absolute():
            return None, None
        return None, _BROWSER_SNAPSHOT_OUTPUT_PATH_ERROR
    arguments.pop("filename", None)
    return resolved_path, None


def _persist_browser_snapshot_output(
    result: ToolResult,
    target_path: Path | None,
) -> ToolResult:
    """Persist an inline browser snapshot to its requested artifact path."""
    if target_path is None or not result.success:
        return result
    content = result.content if isinstance(result.content, str) else ""
    if not content.strip():
        return result.model_copy(
            update={
                "success": False,
                "error": (
                    "managed_browser_snapshot returned no inline content to persist at "
                    f"{target_path}"
                ),
            }
        )
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return result.model_copy(
            update={
                "success": False,
                "error": f"Could not persist browser snapshot at {target_path}: {exc}",
            }
        )
    return result.model_copy(
        update={"content": f"{content.rstrip()}\n\nSnapshot persisted to {target_path}"}
    )

def _prepare_browser_screenshot_output(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> tuple[Path | None, str | None]:
    """Request an inline Playwright screenshot for Box-Agent-managed persistence."""
    if tool_name != "managed_browser_take_screenshot":
        return None, None
    filename = arguments.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return None, None
    supplied_path = Path(filename).expanduser()
    artifact_root = _artifact_scan_root(workspace_dir, artifact_root_dir)
    if artifact_root is None:
        return None, None
    artifact_root = artifact_root.resolve()
    resolved_path = (
        supplied_path.resolve()
        if supplied_path.is_absolute()
        else (artifact_root / supplied_path).resolve()
    )
    if not resolved_path.is_relative_to(artifact_root):
        if supplied_path.is_absolute():
            return None, None
        return None, (
            "BROWSER_SCREENSHOT_OUTPUT_PATH_INVALID: filename must stay inside "
            "the artifact root"
        )
    arguments.pop("filename", None)
    return resolved_path, None

def _persist_browser_screenshot_output(
    result: ToolResult,
    target_path: Path | None,
) -> ToolResult:
    """Persist an inline MCP image; persistence failure remains advisory."""
    if target_path is None or not result.success:
        return result
    raw_output = result.raw_output if isinstance(result.raw_output, dict) else {}
    images = raw_output.get("mcp_inline_images")
    image = images[0] if isinstance(images, list) and images else None
    content = (result.content or "").rstrip()
    if not isinstance(image, dict) or not isinstance(image.get("data"), str):
        warning = (
            "Browser screenshot was not returned inline; visual QA may be skipped: "
            f"{target_path}"
        )
        return result.model_copy(update={"content": f"{content}\n\n{warning}".strip()})
    try:
        import base64

        payload = base64.b64decode(image["data"], validate=True)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(payload)
    except (OSError, ValueError) as exc:
        warning = (
            f"Could not persist browser screenshot at {target_path}: {exc}. "
            "Visual QA may be skipped."
        )
        return result.model_copy(update={"content": f"{content}\n\n{warning}".strip()})
    return result.model_copy(
        update={"content": f"{content}\n\nScreenshot persisted to {target_path}".strip()}
    )

def _trace_safe_tool_raw_output(raw_output: Any) -> Any:
    """Keep inline MCP image payloads out of durable JSONL traces."""
    if not isinstance(raw_output, dict) or "mcp_inline_images" not in raw_output:
        return raw_output
    images = raw_output.get("mcp_inline_images")
    metadata = []
    if isinstance(images, list):
        for image in images:
            if isinstance(image, dict):
                data = image.get("data")
                metadata.append(
                    {
                        "mime_type": image.get("mime_type"),
                        "encoded_chars": len(data) if isinstance(data, str) else None,
                    }
                )
    return {**raw_output, "mcp_inline_images": metadata}

_WEB_SEARCH_IMAGE_URL_KEYS: Final[tuple[str, ...]] = (
    "image_url",
    "imageUrl",
    "ImageUrl",
    "thumbnail",
    "thumbnail_url",
    "thumbnailUrl",
)

_WEB_SEARCH_IMAGE_LIST_KEYS: Final[tuple[str, ...]] = (
    "images",
    "Images",
    "image_urls",
    "imageUrls",
)

def _web_search_http_url(value: Any) -> str:
    """Return an HTTP(S) URL without rewriting signed image query strings."""
    url = str(value or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return ""
    return url

def _web_search_image_detail(
    value: Any,
    *,
    allow_plain_url: bool = False,
) -> dict[str, Any] | None:
    if isinstance(value, str):
        url = _web_search_http_url(value)
        return {"url": url} if url else None
    if not isinstance(value, dict):
        return None

    nested = _first_present(value, ("image", "Image"))
    candidate = nested if isinstance(nested, dict) else value
    url_keys = (
        (*_WEB_SEARCH_IMAGE_URL_KEYS, "url", "Url")
        if isinstance(nested, dict) or allow_plain_url
        else _WEB_SEARCH_IMAGE_URL_KEYS
    )
    url = _web_search_http_url(_first_present(candidate, url_keys))
    if not url:
        return None

    detail: dict[str, Any] = {"url": url}
    for output_key, source_keys in (
        ("width", ("width", "Width")),
        ("height", ("height", "Height")),
    ):
        raw_value = _first_present(candidate, source_keys)
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            detail[output_key] = raw_value
    alt = _first_present(candidate, ("alt", "Alt", "alt_text", "altText"))
    if alt not in (None, ""):
        detail["alt"] = str(alt).strip()
    for output_key, source_keys in (
        ("shape", ("shape", "Shape")),
        ("clarity", ("blur_des", "blurDes", "BlurDes")),
        ("category", ("category", "Category")),
        ("watermark", ("watermark", "Watermark")),
    ):
        raw_value = _first_present(candidate, source_keys)
        if raw_value not in (None, ""):
            detail[output_key] = str(raw_value).strip()
    features = _first_present(candidate, ("features", "Features"))
    if isinstance(features, dict):
        for output_key, source_keys in (
            ("description", ("description", "Description")),
            ("style_type", ("style_type", "styleType", "StyleType")),
        ):
            raw_value = _first_present(features, source_keys)
            if raw_value not in (None, ""):
                detail[output_key] = str(raw_value).strip()
    return detail

def _search_item_image_details(item: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[tuple[Any, bool]] = [(item, False)]
    image_values = _first_present(item, _WEB_SEARCH_IMAGE_LIST_KEYS)
    if isinstance(image_values, list):
        candidates.extend((value, True) for value in image_values)

    snippets = _first_present(item, ("snippet", "Snippet"))
    if isinstance(snippets, list):
        candidates.extend((value, True) for value in snippets)
    elif isinstance(snippets, dict):
        candidates.append((snippets, True))

    details: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for candidate, allow_plain_url in candidates:
        detail = _web_search_image_detail(
            candidate,
            allow_plain_url=allow_plain_url,
        )
        if detail is None or detail["url"] in seen_urls:
            continue
        seen_urls.add(detail["url"])
        details.append(detail)
    return details

def _search_item_reference_tag(item: dict[str, Any], index: int) -> str:
    explicit = _first_present(item, ("reference_tag", "referenceTag"))
    if explicit not in (None, ""):
        value = str(explicit).strip()
        if value.casefold().startswith("ref_"):
            return value.casefold()
        if value.isdigit():
            return f"ref_{value}"

    sort_id = _first_present(item, ("sort_id", "sortId", "SortId"))
    if isinstance(sort_id, (int, float)) and not isinstance(sort_id, bool):
        return f"ref_{max(1, round(sort_id))}"

    rank = _first_present(item, ("rank", "Rank"))
    if isinstance(rank, (int, float)) and not isinstance(rank, bool):
        return f"ref_{max(1, round(rank) + 1)}"
    return f"ref_{index + 1}"

def _search_item_metadata(item: dict[str, Any], key: str) -> Any:
    for container_key in ("DocumentInfo", "documentInfo", "HostInfo", "hostInfo"):
        container = item.get(container_key)
        if isinstance(container, dict):
            value = _first_present(container, (key,))
            if value not in (None, ""):
                return value
    return None

def _normalize_web_search_refs(payload: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for index, item in enumerate(_candidate_search_items(payload)):
        image_details = _search_item_image_details(item)
        source_url = _web_search_http_url(_search_item_url(item))
        if not source_url and image_details:
            source_url = image_details[0]["url"]
        if not source_url:
            continue

        title = _search_item_title(item)
        if not title and image_details:
            title = str(image_details[0].get("alt") or "").strip()
        title = title or source_url

        domain = str(
            _first_present(
                item,
                (
                    "display_url",
                    "displayUrl",
                    "DisplayUrl",
                    "domain",
                    "Domain",
                    "site_name",
                    "siteName",
                    "SiteName",
                ),
            )
            or _search_item_metadata(item, "Hostname")
            or (urlsplit(source_url).hostname or "")
        ).strip()
        publish_time = _first_present(
            item,
            ("date", "Date", "published_at", "publishedAt", "publishTime", "PublishTime"),
        )
        if publish_time in (None, ""):
            publish_time = _search_item_metadata(item, "PublishTime")
        score = _first_present(item, ("score", "Score", "rankScore", "RankScore"))
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            score = 0

        ref: dict[str, Any] = {
            "date": str(publish_time or "").strip(),
            "images": [detail["url"] for detail in image_details],
            "score": score,
            "title": title,
            "url": source_url,
            "domain": domain,
            "passage": _search_item_snippet(item),
            "type": "web",
            "reference_tag": _search_item_reference_tag(item, index),
        }
        if image_details:
            ref["image_details"] = image_details
        refs.append(ref)
    return refs


# Pattern to match <!--PLOT_DATA:...--> markers embedded by code execution.
# These carry interactive chart payloads already sent to the frontend via SSE;
# they must NOT be fed back into the model context.
_PLOT_DATA_RE = re.compile(r"<!--PLOT_DATA:.+?-->", re.DOTALL)

_WEB_SEARCH_COMPACT_MAX_ITEMS = 8


def _strip_plot_data(text: str) -> str:
    """Remove ``<!--PLOT_DATA:...-->`` markers from code-execution stdout.

    The markers contain chart data already delivered to the frontend through
    SSE events.  Keeping them in the model context wastes tokens and can
    cause context-length issues.

    Returns a short placeholder when stripping leaves the string empty.
    """
    cleaned = _PLOT_DATA_RE.sub("", text).strip()
    return cleaned if cleaned else "图表已生成"


def _model_history_placeholder_argument(
    tool_name: str,
    arguments: dict[str, Any],
) -> str | None:
    """Return the first mutation argument that incorrectly reuses a history placeholder."""
    for argument_name in _MODEL_HISTORY_PLACEHOLDER_ARGUMENTS.get(tool_name, ()):
        if is_model_history_placeholder(arguments.get(argument_name)):
            return argument_name
    return None


@dataclass(slots=True)
class _ModelHistoryPlaceholderRecovery:
    """One mutation that must be completed before dependent work can continue."""

    tool_name: str
    argument_name: str
    target: Path | None
    action: str | None = None
    staged_write_id: str | None = None


def _model_history_recovery_target(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> Path | None:
    """Resolve the file target used to bind placeholder recovery to one artifact."""
    if tool_name not in _MODEL_HISTORY_FILE_MUTATION_TOOLS:
        return None
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    root = _artifact_scan_root(workspace_dir, artifact_root_dir)
    if root is None:
        root = Path(workspace_dir).expanduser() if workspace_dir else Path.cwd()
    root = root.resolve(strict=False)
    if workspace_dir:
        workspace = Path(workspace_dir).expanduser().resolve(strict=False)
        try:
            root_from_workspace = root.relative_to(workspace)
        except ValueError:
            root_from_workspace = None
        if (
            root_from_workspace is not None
            and candidate.parts[: len(root_from_workspace.parts)]
            == root_from_workspace.parts
        ):
            return (workspace / candidate).resolve(strict=False)
    return (root / candidate).resolve(strict=False)


def _model_history_placeholder_recovery_error(
    recovery: _ModelHistoryPlaceholderRecovery | None,
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> str | None:
    """Block stale downstream work until the rejected mutation is really completed."""
    if recovery is None:
        return None
    if recovery.tool_name == "staged_file_write":
        if tool_name == "staged_file_write" and arguments.get("action") == recovery.action:
            return None
    elif tool_name in _MODEL_HISTORY_FILE_MUTATION_TOOLS:
        if recovery.target is None or _model_history_recovery_target(
            tool_name,
            arguments,
            workspace_dir,
            artifact_root_dir,
        ) == recovery.target:
            return None
    if (
        recovery.tool_name in _MODEL_HISTORY_FILE_MUTATION_TOOLS
        and tool_name == "staged_file_write"
    ):
        action = arguments.get("action")
        if action == "begin":
            raw_path = arguments.get("path")
            if isinstance(raw_path, str):
                staged_target = _model_history_recovery_target(
                    "write_file",
                    {"path": raw_path},
                    workspace_dir,
                    artifact_root_dir,
                )
                if staged_target == recovery.target:
                    return None
        elif action in {"append_text", "append_file", "commit", "abort"}:
            supplied_id = arguments.get("write_id")
            if recovery.staged_write_id is not None and supplied_id in {
                None,
                recovery.staged_write_id,
            }:
                return None
    target = str(recovery.target) if recovery.target is not None else "not file-backed"
    return (
        f"{_MODEL_HISTORY_PLACEHOLDER_RECOVERY_REQUIRED} Pending mutation: "
        f"{recovery.tool_name}.{recovery.argument_name}; target: {target}."
    )


def _record_model_history_placeholder_recovery_result(
    recovery: _ModelHistoryPlaceholderRecovery | None,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
) -> _ModelHistoryPlaceholderRecovery | None:
    """Advance or clear the recovery gate only after an actual successful mutation."""
    if recovery is None or not result.success:
        return recovery
    if recovery.tool_name == "staged_file_write":
        if tool_name == "staged_file_write" and arguments.get("action") == recovery.action:
            return None
        return recovery
    if tool_name in _MODEL_HISTORY_FILE_MUTATION_TOOLS:
        if tool_name == "write_file" and arguments.get("final", True) is False:
            return recovery
        return None
    if tool_name != "staged_file_write":
        return recovery
    action = arguments.get("action")
    if action == "begin":
        raw_output = result.raw_output if isinstance(result.raw_output, dict) else {}
        write_id = raw_output.get("write_id")
        if isinstance(write_id, str) and write_id:
            recovery.staged_write_id = write_id
    elif action == "commit":
        return None
    elif action == "abort":
        recovery.staged_write_id = None
    return recovery


def _tool_message_content_for_model(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    visible_content: str,
    visible_error: str | None,
    resource_receipt: str | None = None,
) -> str:
    """Return the content stored in conversation history for a tool result.

    ToolCallResult events and logs keep full visible output.  This path controls
    only what future LLM calls receive in ``messages``.
    """
    if not result.success:
        return f"Error: {visible_error}"

    if resource_receipt is not None:
        return resource_receipt

    # read_file now enforces bounded line pagination and rejects pages above
    # its character safety limit. Preserve each successful page verbatim so
    # offset/limit can reliably retrieve content instead of replacing the
    # requested region with another history preview.
    if tool_name == "read_file" and (result.raw_output or {}).get("truncated") is False:
        return visible_content

    if (
        tool_name != "read_file"
        and result.model_context is not None
        and visible_content == result.content
    ):
        return result.model_context
    return _strip_plot_data(visible_content)


def _repeatable_framework_error(
    *,
    tool_name: str,
    result: ToolResult,
    visible_error: str | None,
) -> tuple[str, str] | None:
    """Return a stable signature and label for noisy framework-owned failures."""
    if result.success or not visible_error:
        return None
    raw_output = result.raw_output if isinstance(result.raw_output, dict) else {}
    if (
        tool_name == "sub_agent"
        and raw_output.get("type") == "sub_agent_delegation_error"
    ):
        code = str(raw_output.get("code") or "SUB_AGENT_DELEGATION_ERROR")
        return f"{tool_name}:{visible_error}", code
    if visible_error.startswith("INTERNAL_MODEL_HISTORY_PLACEHOLDER:"):
        return f"{tool_name}:{visible_error}", "INTERNAL_MODEL_HISTORY_PLACEHOLDER"
    return None


@dataclass(frozen=True, slots=True)
class _ContextResourceHistoryDecision:
    descriptor: ResourceDescriptor | None = None
    source_tool_call_ids: tuple[str, ...] = ()
    receipt: str | None = None


def _context_resource_history_decision(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    messages: list[Message],
    ledger: ContextResourceLedger | None,
) -> _ContextResourceHistoryDecision:
    """Choose full read content or a receipt from live source coverage."""
    if ledger is None or tool_name != "read_file" or not result.success:
        return _ContextResourceHistoryDecision()
    descriptor = ResourceDescriptor.from_raw_output(result.raw_output)
    if descriptor is None or not descriptor.has_content:
        return _ContextResourceHistoryDecision(descriptor=descriptor)
    source_ids = ledger.covering_source_ids(descriptor, messages)
    if not source_ids:
        return _ContextResourceHistoryDecision(descriptor=descriptor)
    refresh_requested = arguments.get("refresh") is True
    if refresh_requested and ledger.claim_refresh_reload(descriptor):
        return _ContextResourceHistoryDecision(descriptor=descriptor)
    return _ContextResourceHistoryDecision(
        descriptor=descriptor,
        source_tool_call_ids=source_ids,
        receipt=build_resource_receipt(
            descriptor,
            source_ids,
            refresh_unchanged=refresh_requested,
        ),
    )


def _record_context_resource_history(
    *,
    tool_call_id: str,
    decision: _ContextResourceHistoryDecision,
    result: ToolResult,
    visible_content: str,
    model_content: str,
    ledger: ContextResourceLedger | None,
) -> None:
    """Update the ledger only after the tool message is in model history."""
    descriptor = decision.descriptor
    if ledger is None or descriptor is None or not result.success:
        return
    if decision.receipt is not None:
        ledger.register_receipt(tool_call_id, decision.source_tool_call_ids)
        _log.info(
            "context_resource/read_repeat tool_call_id=%s version=%s lines=%d-%d "
            "sources=%s visible_chars=%d model_chars=%d",
            tool_call_id,
            descriptor.content_version[:12],
            descriptor.start_line,
            descriptor.end_line,
            ",".join(decision.source_tool_call_ids),
            len(visible_content),
            len(model_content),
        )
        return
    # Hook-modified or pre-compacted content is not an exact file body and
    # therefore cannot safely contribute coverage.
    if model_content != visible_content or visible_content != result.content:
        return
    ledger.register_full_source(tool_call_id, descriptor, model_content)
    if ledger.source(tool_call_id) is not None:
        _log.info(
            "context_resource/read_full tool_call_id=%s class=%s version=%s "
            "lines=%d-%d model_chars=%d",
            tool_call_id,
            descriptor.resource_class.value,
            descriptor.content_version[:12],
            descriptor.start_line,
            descriptor.end_line,
            len(model_content),
        )


@dataclass(frozen=True, slots=True)
class ToolResultPipelineInput:
    """Explicit inputs for one completed tool call's shared post-processing."""

    messages: list[Message]
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: ToolResult
    visible_content: str
    visible_error: str | None
    result_storage: ToolResultStorage
    tool: Tool | None = None
    session_id: str = ""
    resource_ledger: ContextResourceLedger | None = None
    web_search_seen_result_keys: set[str] | None = None
    framework_error_counts: dict[str, int] | None = None
    user_visible: bool = True
    emit_legacy_permission_request: bool = False
    policy_decision: dict[str, Any] | None = None
    tool_id: str | None = None
    server_name: str | None = None
    turn_id: str = ""
    step: int = 0
    started_at: float | None = None
    parallel: bool = False


@dataclass(frozen=True, slots=True)
class ToolResultPipelineOutcome:
    """Events and explicit counter deltas produced for one completed tool call."""

    events: tuple[AgentEvent, ...]
    tool_message: Message
    model_content: str
    visible_content: str
    visible_error: str | None
    web_search_new_results: int = 0
    web_search_duplicate_results: int = 0
    web_search_labels: tuple[str, ...] = ()
    web_search_inspected: bool = False


def process_tool_result(
    pipeline_input: ToolResultPipelineInput,
) -> ToolResultPipelineOutcome:
    """Append one stored tool reply before exposing its result-derived events."""

    result = pipeline_input.result
    visible_content = pipeline_input.visible_content
    visible_error = pipeline_input.visible_error
    new_count = 0
    duplicate_count = 0
    new_labels: list[str] = []
    inspected = False

    if result.success and pipeline_input.tool_name == "web_search":
        (
            visible_content,
            new_count,
            duplicate_count,
            new_labels,
            inspected,
        ) = _dedupe_web_search_content(
            visible_content,
            (
                pipeline_input.web_search_seen_result_keys
                if pipeline_input.web_search_seen_result_keys is not None
                else set()
            ),
            pipeline_input.arguments,
        )

    resource_decision = _context_resource_history_decision(
        tool_name=pipeline_input.tool_name,
        arguments=pipeline_input.arguments,
        result=result,
        messages=pipeline_input.messages,
        ledger=pipeline_input.resource_ledger,
    )
    model_content = _tool_message_content_for_model(
        tool_name=pipeline_input.tool_name,
        arguments=pipeline_input.arguments,
        result=result,
        visible_content=visible_content,
        visible_error=visible_error,
        resource_receipt=resource_decision.receipt,
    )
    repeated = _repeatable_framework_error(
        tool_name=pipeline_input.tool_name,
        result=result,
        visible_error=visible_error,
    )
    if repeated is not None and pipeline_input.framework_error_counts is not None:
        signature, label = repeated
        count = pipeline_input.framework_error_counts.get(signature, 0) + 1
        pipeline_input.framework_error_counts[signature] = count
        if count > 1:
            model_content = (
                f"Error: REPEATED_FRAMEWORK_FAILURE: {label} occurrence {count}. "
                "The first matching tool result contains the full diagnostic and repair "
                "guidance. Do not retry the unchanged call."
            )
    if result.success and pipeline_input.tool_name == "web_search":
        _log_web_search_model_results(
            pipeline_input.arguments,
            visible_content,
            model_content,
        )

    tool_message = Message(
        role="tool",
        content=model_content,
        tool_call_id=pipeline_input.tool_call_id,
        name=pipeline_input.tool_name,
    )
    tool_message = pipeline_input.result_storage.process_message(
        tool_message,
        tool=pipeline_input.tool,
        session_id=pipeline_input.session_id,
        persistence_content=result.persistence_content,
        content_already_processed=(
            result.success
            and result.model_context is not None
            and model_content == result.model_context
        ),
    )
    model_content = tool_message.content
    pipeline_input.messages.append(tool_message)
    _record_context_resource_history(
        tool_call_id=pipeline_input.tool_call_id,
        decision=resource_decision,
        result=result,
        visible_content=visible_content,
        model_content=model_content,
        ledger=pipeline_input.resource_ledger,
    )

    trace_data: dict[str, Any] = {
        "tool_name": pipeline_input.tool_name,
        "tool_id": pipeline_input.tool_id,
        "server_name": pipeline_input.server_name,
        "success": result.success,
        "content": visible_content,
        "error": visible_error,
        "raw_output": result.raw_output,
        "model_content": model_content,
        "policy_decision": pipeline_input.policy_decision,
        "user_visible": pipeline_input.user_visible,
    }
    if pipeline_input.parallel:
        trace_data["parallel"] = True
    trace_data["duration_ms"] = (
        max(0, int((perf_counter() - pipeline_input.started_at) * 1000))
        if pipeline_input.started_at is not None
        else 0
    )
    emit_session_trace(
        "tool.response",
        turn_id=pipeline_input.turn_id,
        step=pipeline_input.step,
        tool_call_id=pipeline_input.tool_call_id,
        data=trace_data,
    )

    events: list[AgentEvent] = [
        ToolCallResult(
            tool_call_id=pipeline_input.tool_call_id,
            tool_name=pipeline_input.tool_name,
            success=result.success,
            content=visible_content,
            error=visible_error,
            raw_output=_trace_safe_tool_raw_output(result.raw_output),
            user_visible=pipeline_input.user_visible,
            policy_decision=pipeline_input.policy_decision,
            tool_id=pipeline_input.tool_id,
            server_name=pipeline_input.server_name,
        )
    ]
    if result.success and pipeline_input.user_visible:
        web_search_payload = _extract_web_search_payload(
            pipeline_input.tool_name,
            visible_content,
        )
        if web_search_payload is not None:
            events.append(
                WebSearchEvent(
                    tool_call_id=pipeline_input.tool_call_id,
                    payload=web_search_payload,
                )
            )
    if (
        not result.success
        and result.permission_request
        and pipeline_input.emit_legacy_permission_request
    ):
        events.append(
            PermissionRequestEvent(
                tool_call_id=pipeline_input.tool_call_id,
                **_permission_event_kwargs(result.permission_request),
            )
        )

    return ToolResultPipelineOutcome(
        events=tuple(events),
        tool_message=tool_message,
        model_content=model_content,
        visible_content=visible_content,
        visible_error=visible_error,
        web_search_new_results=new_count,
        web_search_duplicate_results=duplicate_count,
        web_search_labels=tuple(new_labels),
        web_search_inspected=inspected,
    )


def _extract_web_search_payload(tool_name: str, content: str) -> dict[str, Any] | None:
    """Return frontend refs from supported structured web_search results."""
    if tool_name != "web_search" or not content:
        return None

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    if isinstance(payload.get("refs"), list):
        return payload

    refs = _normalize_web_search_refs(payload)
    if not refs:
        return None
    return {"type": "web_search", "refs": refs}


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

    try:
        ws = Path(workspace_dir).resolve()
        out = _artifact_scan_root(workspace_dir, artifact_root_dir)
    except (OSError, RuntimeError, ValueError):
        # Artifact discovery is best-effort and must never fail the tool call.
        return []
    if out is None:
        return []
    try:
        if not out.is_dir():
            return []
    except OSError:
        return []

    artifacts: list[ArtifactEvent] = []
    seen_paths: set[Path] = set()
    for match in _ARTIFACT_REF_RE.finditer(content):
        filename = match.group(1)
        try:
            if len(filename) > _MAX_ARTIFACT_REF_CHARS or any(
                len(os.fsencode(part)) > _MAX_ARTIFACT_COMPONENT_BYTES
                for part in Path(filename).parts
            ):
                continue
            candidate = (out / filename).resolve()
            candidate.relative_to(out)
            if candidate in seen_paths or not candidate.is_file():
                continue
            artifact = _make_artifact(tool_call_id, candidate, ws)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            # Invalid, overlong, racy, or otherwise unresolvable references are
            # ordinary false positives in arbitrary tool output.
            continue
        seen_paths.add(candidate)
        artifacts.append(artifact)

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


def _snapshot_workspace_signatures(
    workspace_dir: str,
    artifact_root_dir: str | Path | None = None,
) -> dict[Path, tuple[int, int]]:
    """Snapshot artifact paths plus stat signatures for revision detection."""
    signatures: dict[Path, tuple[int, int]] = {}
    for file_path in _snapshot_workspace(workspace_dir, artifact_root_dir):
        try:
            stat = file_path.stat()
        except OSError:
            continue
        signatures[file_path] = (stat.st_size, stat.st_mtime_ns)
    return signatures


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


def _detect_changed_files(
    tool_call_id: str,
    pre_files: dict[Path, tuple[int, int]],
    post_files: dict[Path, tuple[int, int]],
    already_emitted: set[str],
    workspace_dir: str,
) -> list[ArtifactEvent]:
    """Create ArtifactEvents for files that appeared or changed."""
    changed_files = {
        path
        for path, signature in post_files.items()
        if pre_files.get(path) != signature
    }
    if not changed_files:
        return []

    ws = Path(workspace_dir).resolve()
    artifacts: list[ArtifactEvent] = []
    for file_path in sorted(changed_files):
        if (
            file_path.name.startswith(".")
            or file_path.name.startswith("~")
            or file_path.suffix == ".tmp"
        ):
            continue
        if str(file_path.resolve()) in already_emitted:
            continue
        artifacts.append(_make_artifact(tool_call_id, file_path, ws))
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
    or carried on an artifact/intermediate-asset ``raw_output``). Intermediate
    assets are also excluded from regex publication while remaining on disk.
    """
    regex_artifacts = _detect_artifacts(
        tool_call_id,
        tool_name,
        content,
        workspace_dir,
        artifact_root_dir,
    )
    already = {a.abs_path for a in regex_artifacts}
    if isinstance(raw_output, dict) and raw_output.get("type") in ("artifact", "intermediate_asset"):
        raw_paths: set[str] = set()
        for key in ("abs_path", "absolute_path"):
            raw_path = raw_output.get(key)
            if isinstance(raw_path, str) and raw_path.strip():
                raw_paths.add(str(Path(raw_path).expanduser().resolve()))
        already.update(raw_paths)
        if raw_output.get("type") == "intermediate_asset":
            regex_artifacts = [a for a in regex_artifacts if a.abs_path not in raw_paths]
    return regex_artifacts, already


def _detect_tool_artifacts(
    tool_call_id: str,
    tool_name: str,
    content: str,
    raw_output: Any,
    pre_files: dict[Path, tuple[int, int]],
    post_files: dict[Path, tuple[int, int]],
    workspace_dir: str,
    artifact_root_dir: str | Path | None,
) -> list[ArtifactEvent]:
    """Two-layer artifact detection for a single tool result (sequential path).

    Layer 1 (regex): scan ``content`` for ``[filename.ext]`` references that
    resolve under the artifact root. Layer 2 (diff): catch files created or
    modified by the tool that weren't referenced in the output text, using a
    per-tool pre/post signature snapshot. The parallel branch can't take per-tool snapshots under
    concurrency, so it composes :func:`_detect_regex_artifacts` per result with
    a single diff pass instead (see the parallel block in ``run_agent_loop``).
    """
    regex_artifacts, already = _detect_regex_artifacts(
        tool_call_id, tool_name, content, raw_output, workspace_dir, artifact_root_dir
    )
    diff_artifacts = _detect_changed_files(
        tool_call_id, pre_files, post_files, already, workspace_dir
    )
    return [*regex_artifacts, *diff_artifacts]


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
    "webResults",
    "WebResults",
    "web_results",
    "imageResults",
    "ImageResults",
    "image_results",
    "items",
    "value",
    "organic_results",
    "data",
)

_SITE_QUERY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)site:([a-z0-9.-]+)",
    re.IGNORECASE,
)
_SITE_QUERY_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)site:[^\s]+",
    re.IGNORECASE,
)
_SEARCH_QUERY_TERM_RE: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9]+|[\u3400-\u9fff]+",
    re.IGNORECASE,
)
_SEARCH_QUERY_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "all",
        "and",
        "for",
        "in",
        "of",
        "official",
        "on",
        "search",
        "source",
        "sources",
        "the",
        "to",
        "verify",
        "查找",
        "搜索",
        "来源",
        "核实",
        "检索",
        "官方",
        "权威",
        "查证",
        "验证",
        "调研",
    }
)
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


def _web_search_query_terms(query: str) -> set[str]:
    """Return conservative intent terms for near-duplicate search detection."""
    site_match = _SITE_QUERY_RE.search(query)
    site_term = f"site-{site_match.group(1).strip('.').casefold()}" if site_match else ""
    without_site_path = _SITE_QUERY_TOKEN_RE.sub(" ", query)
    terms = {
        term.casefold()
        for term in _SEARCH_QUERY_TERM_RE.findall(without_site_path)
        if term.casefold() not in _SEARCH_QUERY_STOPWORDS
    }
    if site_term:
        terms.add(site_term)
    return terms


def _web_search_queries_are_near_duplicates(first: str, second: str) -> bool:
    """Detect only high-overlap rewrites while preserving distinct research gaps."""
    if not first or not second:
        return False
    if first == second:
        return True
    first_site = _SITE_QUERY_RE.search(first)
    second_site = _SITE_QUERY_RE.search(second)
    first_domain = first_site.group(1).strip(".").casefold() if first_site else ""
    second_domain = second_site.group(1).strip(".").casefold() if second_site else ""
    if first_domain != second_domain:
        return False
    first_terms = _web_search_query_terms(first)
    second_terms = _web_search_query_terms(second)
    if min(len(first_terms), len(second_terms)) < 3:
        return False
    overlap = len(first_terms & second_terms)
    containment = overlap / min(len(first_terms), len(second_terms))
    coverage = overlap / max(len(first_terms), len(second_terms))
    return containment >= 0.9 and coverage >= 0.65


def _requested_site_domain(arguments: dict[str, Any]) -> str:
    query = _normalize_web_search_query(arguments)
    match = _SITE_QUERY_RE.search(query)
    if match is None:
        return ""
    return match.group(1).strip(".").casefold()


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


def _search_item_url(item: dict[str, Any]) -> str:
    return str(_first_present(item, ("url", "Url", "href", "link", "Link")) or "").strip()


def _url_matches_domain(value: Any, domain: str) -> bool:
    if not domain:
        return True
    try:
        hostname = (urlsplit(str(value or "")).hostname or "").casefold().strip(".")
    except ValueError:
        return False
    return hostname == domain or hostname.endswith(f".{domain}")


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

    for key, value in payload.items():
        if isinstance(value, dict) and _candidate_search_items(value):
            updated = dict(payload)
            updated[key] = _with_filtered_search_items(value, filtered_items)
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


def _search_result_list_found(payload: Any) -> bool:
    """Return whether a structured result-list field exists, even when empty."""
    if isinstance(payload, list):
        return True
    if not isinstance(payload, dict):
        return False
    for key in _WEB_SEARCH_RESULT_KEYS:
        if isinstance(payload.get(key), list):
            return True
    return any(
        _search_result_list_found(value)
        for value in payload.values()
        if isinstance(value, dict)
    )


def _search_item_title(item: dict[str, Any]) -> str:
    return str(_first_present(item, ("title", "Title", "name", "Name")) or "").strip()


def _search_item_snippet(item: dict[str, Any]) -> str:
    value = _first_present(
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
    if isinstance(value, list):
        text_parts: list[str] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            text = _first_present(entry, ("text", "Text"))
            if text not in (None, ""):
                text_parts.append(str(text).strip())
        return "\n".join(part for part in text_parts if part)
    if isinstance(value, dict):
        text = _first_present(value, ("text", "Text"))
        return str(text or "").strip()
    return str(value or "").strip()


def _web_search_match_terms(query: str) -> tuple[str, ...]:
    """Extract stable entity/topic terms for result relevance scoring."""
    without_site = _SITE_QUERY_TOKEN_RE.sub(" ", query)
    terms: list[str] = []
    for raw in _SEARCH_QUERY_TERM_RE.findall(without_site):
        term = raw.casefold()
        if term in _SEARCH_QUERY_STOPWORDS:
            continue
        candidates = [term]
        if re.fullmatch(r"[\u3400-\u9fff]+", term) and len(term) > 2:
            candidates.extend(term[index : index + 2] for index in range(len(term) - 1))
        for candidate in candidates:
            if candidate and candidate not in terms:
                terms.append(candidate)
    return tuple(terms[:24])


def _web_search_item_rank(
    item: dict[str, Any],
    *,
    query_terms: tuple[str, ...],
    requested_site: str,
) -> tuple[int, int, int, int]:
    """Return relevance, domain, first-party, and coverage scores."""
    title = _normalize_search_title(_search_item_title(item))
    snippet = _normalize_search_title(_search_item_snippet(item))
    url = _search_item_url(item)
    host = ""
    try:
        host = (urlsplit(url).hostname or "").casefold().strip(".")
    except ValueError:
        pass
    entity_score = 0
    matched_terms = 0
    for term in query_terms:
        matched = False
        if term in title:
            entity_score += 6
            matched = True
        if term in snippet:
            entity_score += 2
            matched = True
        if term in host or term in url.casefold():
            entity_score += 3
            matched = True
        if matched:
            matched_terms += 1
    coverage_score = (
        round((matched_terms / len(query_terms)) * 20) if query_terms else 0
    )
    domain_score = 50 if requested_site and _url_matches_domain(url, requested_site) else 0
    explicit_source_type = str(
        _first_present(
            item,
            ("source_type", "SourceType", "sourceType", "authority", "Authority"),
        )
        or ""
    ).casefold()
    first_party_score = 0
    if domain_score:
        first_party_score = 3
    elif explicit_source_type in {"first_party", "official", "primary"}:
        first_party_score = 2
    elif "official" in title or "官网" in title or "官方" in title:
        first_party_score = 1
    return entity_score, domain_score, first_party_score, coverage_score


def _rank_web_search_items(
    items: list[dict[str, Any]],
    arguments: dict[str, Any],
) -> list[dict[str, Any]]:
    query = _normalize_web_search_query(arguments)
    query_terms = _web_search_match_terms(query)
    requested_site = _requested_site_domain(arguments)
    ranked = [
        (
            item,
            _web_search_item_rank(
                item,
                query_terms=query_terms,
                requested_site=requested_site,
            ),
            index,
        )
        for index, item in enumerate(items)
    ]
    ranked.sort(
        key=lambda entry: (
            -entry[1][1],
            -entry[1][0],
            -entry[1][2],
            -entry[1][3],
            entry[2],
        )
    )
    return [item for item, _, _ in ranked]


def _web_search_result_metadata(
    items: list[dict[str, Any]],
    arguments: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    query = _normalize_web_search_query(arguments)
    query_terms = _web_search_match_terms(query)
    requested_site = _requested_site_domain(arguments)
    ranking = []
    direct_read_candidates = []
    for item in items[:_WEB_SEARCH_COMPACT_MAX_ITEMS]:
        url = _search_item_url(item)
        entity_score, domain_score, first_party_score, coverage_score = (
            _web_search_item_rank(
                item,
                query_terms=query_terms,
                requested_site=requested_site,
            )
        )
        ranking.append(
            {
                "title": _short_tool_text(_search_item_title(item), 120),
                "url": _short_tool_text(url, 180),
                "entity_match_score": entity_score,
                "domain_match_score": domain_score,
                "first_party_level": first_party_score,
                "query_coverage_score": coverage_score,
            }
        )
        if url and (domain_score > 0 or first_party_score >= 2):
            direct_read_candidates.append(url)
    return {
        "SearchStatus": status,
        "NormalizedResultCount": len(items),
        "SearchResultRanking": ranking,
        "DirectReadCandidates": list(dict.fromkeys(direct_read_candidates))[:5],
        "DirectReadNotice": (
            "When an exact first-party URL is known, read that page with an available "
            "direct browser/page tool before using it as evidence."
        ),
    }


def _with_web_search_metadata(
    payload: Any,
    metadata: dict[str, Any],
) -> Any:
    if not isinstance(payload, dict):
        return payload
    return {**payload, **metadata}


def _log_web_search_model_results(
    arguments: dict[str, Any],
    visible_content: str,
    model_content: str,
) -> None:
    """Log the ranked rows that were actually eligible for model context."""
    try:
        payload = json.loads(visible_content)
    except json.JSONDecodeError:
        _log.info(
            "web_search/model_results query=%r structured=false model_chars=%d",
            _normalize_web_search_query(arguments),
            len(model_content),
        )
        return
    items = _candidate_search_items(payload)
    status = payload.get("SearchStatus") if isinstance(payload, dict) else None
    top = [
        {
            "title": _short_tool_text(_search_item_title(item), 120),
            "url": _short_tool_text(_search_item_url(item), 180),
        }
        for item in items[:5]
    ]
    _log.info(
        "web_search/model_results query=%r status=%s model_chars=%d top=%s",
        _normalize_web_search_query(arguments),
        status or "unknown",
        len(model_content),
        json.dumps(top, ensure_ascii=False, separators=(",", ":")),
    )


def _dedupe_web_search_content(
    content: str,
    seen_result_keys: set[str],
    arguments: dict[str, Any] | None = None,
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
    structured_result_list = _search_result_list_found(payload)
    if not items:
        if not structured_result_list:
            return content, 0, 0, [], False
        requested_site = _requested_site_domain(arguments or {})
        status = "site_no_results" if requested_site else "no_results"
        updated_payload = _with_web_search_metadata(
            payload,
            _web_search_result_metadata([], arguments or {}, status=status),
        )
        if isinstance(updated_payload, dict) and requested_site:
            updated_payload = {
                **updated_payload,
                "RequestedSiteDomain": requested_site,
                "SiteFilterDroppedCount": 0,
                "SiteFilterMatchedCount": 0,
                "SiteFilterNotice": (
                    f"No results were returned for site:{requested_site}. "
                    "Do not treat this as proof that no official page exists, do not "
                    "invent a URL, and use a known exact URL with a direct page-read "
                    "tool when available."
                ),
            }
        return json.dumps(updated_payload, ensure_ascii=False), 0, 0, [], True

    requested_site = _requested_site_domain(arguments or {})
    site_filtered_count = 0
    site_matched_count = len(items) if requested_site else 0
    if requested_site:
        matched_items = [
            item for item in items if _url_matches_domain(_search_item_url(item), requested_site)
        ]
        site_matched_count = len(matched_items)
        site_filtered_count = len(items) - len(matched_items)
        if site_filtered_count:
            payload = _with_filtered_search_items(payload, matched_items)
            if isinstance(payload, dict):
                payload = {
                    **payload,
                    "RequestedSiteDomain": requested_site,
                    "SiteFilterDroppedCount": site_filtered_count,
                    "SiteFilterMatchedCount": len(matched_items),
                    "SiteFilterNotice": (
                        f"Only URLs hosted on {requested_site} are valid for this site: query. "
                        "Do not cite or relabel dropped results, and do not invent a replacement URL."
                    ),
                }
            items = matched_items
            if not items:
                payload = _with_web_search_metadata(
                    payload,
                    _web_search_result_metadata(
                        [],
                        arguments or {},
                        status="site_no_results",
                    ),
                )
                if isinstance(payload, dict):
                    payload["SearchEmptyReason"] = "all_provider_results_were_off_domain"
                return json.dumps(payload, ensure_ascii=False), 0, 0, [], True

    items = _rank_web_search_items(items, arguments or {})
    payload = _with_filtered_search_items(payload, items)

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

    updated_payload = _with_filtered_search_items(payload, filtered_items)
    if isinstance(updated_payload, dict):
        updated_payload = {
            **updated_payload,
            "DedupedDuplicateCount": duplicate_count,
            "DedupedNewCount": len(filtered_items),
            **_web_search_result_metadata(
                filtered_items,
                arguments or {},
                status="ok" if filtered_items else "no_new_results",
            ),
        }
        if requested_site:
            updated_payload = {
                **updated_payload,
                "RequestedSiteDomain": requested_site,
                "SiteFilterDroppedCount": site_filtered_count,
                "SiteFilterMatchedCount": site_matched_count,
            }
    return json.dumps(updated_payload, ensure_ascii=False), len(filtered_items), duplicate_count, new_labels, True


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
