"""Persist oversized model-facing tool results without losing recoverability."""

from __future__ import annotations

import json
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .schema import Message
from .tools.base import Tool


DEFAULT_MAX_RESULT_SIZE_CHARS = 20_000
DEFAULT_TOOL_RESULTS_BUDGET_CHARS = 50_000
PREVIEW_SIZE_CHARS = 2_000
CONTEXT_RESULT_LIMIT_RATIO = 0.25
CONTEXT_AGGREGATE_BUDGET_RATIO = 0.50
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"


@dataclass(frozen=True, slots=True)
class PersistedToolResult:
    """Metadata needed to render a stable model-facing persisted preview."""

    path: Path
    original_size: int
    is_json: bool
    preview: str
    has_more: bool


@dataclass(frozen=True, slots=True)
class ToolResultBudgetOutcome:
    """Observable result of one aggregate fresh-result budget pass."""

    fresh_count: int = 0
    persisted_count: int = 0
    original_chars: int = 0
    remaining_chars: int = 0


def generate_preview(
    content: str,
    max_chars: int = PREVIEW_SIZE_CHARS,
) -> tuple[str, bool]:
    """Return a bounded head-and-tail preview of one persisted result."""

    if len(content) <= max_chars:
        return content, False
    if max_chars <= 0:
        return "", True

    marker = "\n...[middle omitted from preview]...\n"
    if max_chars <= len(marker) + 1:
        head_chars = max_chars // 2
        tail_chars = max_chars - head_chars
        return content[:head_chars] + content[-tail_chars:], True

    available = max_chars - len(marker)
    head_chars = available // 2
    tail_chars = available - head_chars
    head = content[:head_chars]
    tail = content[-tail_chars:]

    last_newline = head.rfind("\n")
    if last_newline > len(head) * 0.5:
        head = head[:last_newline]
    first_newline = tail.find("\n")
    if 0 <= first_newline < len(tail) * 0.5:
        tail = tail[first_newline + 1 :]
    return f"{head}{marker}{tail}", True


def build_persisted_output(
    result: PersistedToolResult,
    *,
    preview_label: str | None = None,
) -> str:
    """Build the stable preview shown to the model for a persisted result."""

    message = (
        f"{PERSISTED_OUTPUT_TAG}\n"
        f"Output too large ({_format_size(result.original_size)}). "
        f"Full output saved to: {result.path}\n"
    )
    if result.preview:
        label = preview_label or (
            f"Preview (head + tail, up to {_format_size(PREVIEW_SIZE_CHARS)})"
        )
        message += f"\n{label}:\n{result.preview}"
        message += "\n"
    return message + PERSISTED_OUTPUT_CLOSING_TAG


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)}B" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def _content_text(content: str | list[dict[str, Any]]) -> tuple[str, bool] | None:
    """Return serialized persistable content and whether it is JSON."""

    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return None
    if any(
        not isinstance(block, dict)
        or block.get("type") != "text"
        or not isinstance(block.get("text"), str)
        for block in content
    ):
        return None
    return json.dumps(content, ensure_ascii=False, indent=2), True


def _content_size(content: str | list[dict[str, Any]]) -> int:
    serialized = _content_text(content)
    return len(serialized[0]) if serialized is not None else 0


def _content_is_empty(content: str | list[dict[str, Any]]) -> bool:
    if isinstance(content, str):
        return not content.strip()
    if not content:
        return True
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return False
        text = block.get("text")
        if not isinstance(text, str) or text.strip():
            return False
    return True


def _safe_path_segment(value: str, fallback: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe[:180] or fallback


class ToolResultStorage:
    """Own oversized-result persistence and stable per-conversation decisions.

    The interface intentionally accepts model-facing ``Message`` objects. Full
    visible tool events and logs stay outside this module and remain unchanged.
    """

    def __init__(
        self,
        root_dir: str | Path,
        *,
        default_result_limit: int | None = None,
        aggregate_budget: int | None = None,
    ) -> None:
        self.root_dir = Path(root_dir)
        self._contextual_result_limit = default_result_limit is None
        self._contextual_aggregate_budget = aggregate_budget is None
        self.default_result_limit = (
            DEFAULT_MAX_RESULT_SIZE_CHARS
            if default_result_limit is None
            else default_result_limit
        )
        self.aggregate_budget = (
            DEFAULT_TOOL_RESULTS_BUDGET_CHARS
            if aggregate_budget is None
            else aggregate_budget
        )
        self._default_session_id = f"local-{uuid4().hex}"
        self._seen_ids: set[str] = set()
        self._replacements: dict[str, str] = {}
        self._initialized = False

    def set_context_token_limit(self, token_limit: int) -> None:
        """Scale default result budgets to the active model's safe input window."""

        if token_limit <= 0:
            raise ValueError("token_limit must be positive")
        if self._contextual_result_limit:
            self.default_result_limit = max(
                DEFAULT_MAX_RESULT_SIZE_CHARS,
                int(token_limit * CONTEXT_RESULT_LIMIT_RATIO),
            )
        if self._contextual_aggregate_budget:
            self.aggregate_budget = max(
                DEFAULT_TOOL_RESULTS_BUDGET_CHARS,
                int(token_limit * CONTEXT_AGGREGATE_BUDGET_RATIO),
            )

    def initialize_history(self, messages: list[Message]) -> None:
        """Freeze pre-existing results so resumed history is never retroactively changed."""

        if self._initialized:
            return
        for message in messages:
            if message.role != "tool" or not message.tool_call_id:
                continue
            self._seen_ids.add(message.tool_call_id)
            if (
                isinstance(message.content, str)
                and message.content.startswith(PERSISTED_OUTPUT_TAG)
            ):
                self._replacements[message.tool_call_id] = message.content
        self._initialized = True

    def process_message(
        self,
        message: Message,
        *,
        tool: Tool | None,
        session_id: str = "",
        persistence_content: str | list[dict[str, Any]] | None = None,
        content_already_processed: bool = False,
    ) -> Message:
        """Apply empty-result and immediate per-tool persistence policy.

        ``persistence_content`` lets a tool keep a bounded host-facing result
        while handing the complete text to this single persistence owner. It
        is never used as a second model-facing representation.

        ``content_already_processed`` freezes a semantic ``model_context``
        projection so neither the immediate limit nor the later fresh-result
        budget replaces it again.
        """

        if message.role != "tool" or not message.tool_call_id:
            return message
        cached_replacement = self._replacements.get(message.tool_call_id)
        if cached_replacement is not None:
            return message.model_copy(update={"content": cached_replacement})
        model_content = message.content
        if _content_is_empty(model_content):
            display_name = message.name or (tool.name if tool is not None else "Tool")
            if display_name.casefold() == "bash":
                display_name = "Bash"
            return message.model_copy(
                update={"content": f"({display_name} completed with no output)"}
            )

        if persistence_content is not None:
            self._seen_ids.add(message.tool_call_id)
            replacement = self._persist_content(
                persistence_content,
                message.tool_call_id,
                session_id=session_id,
                model_content=model_content,
            )
            if replacement is None:
                return message
            self._replacements[message.tool_call_id] = replacement
            return message.model_copy(update={"content": replacement})

        if content_already_processed:
            self._seen_ids.add(message.tool_call_id)
            return message

        declared_limit = getattr(tool, "max_result_size_chars", None)
        if isinstance(declared_limit, (int, float)) and math.isinf(declared_limit):
            return message
        threshold = self.default_result_limit
        if isinstance(declared_limit, (int, float)):
            threshold = min(int(declared_limit), threshold)
        if _content_size(model_content) <= threshold:
            return message

        replacement = self._persist_content(
            model_content,
            message.tool_call_id,
            session_id=session_id,
        )
        if replacement is None:
            return message
        self._replacements[message.tool_call_id] = replacement
        return message.model_copy(update={"content": replacement})

    def enforce_fresh_budget(
        self,
        messages: list[Message],
        *,
        tools: Mapping[str, Tool],
        session_id: str = "",
    ) -> ToolResultBudgetOutcome:
        """Persist largest fresh results until their aggregate fits the budget."""

        candidates: list[tuple[int, int, Message]] = []
        for index, message in enumerate(messages):
            tool_use_id = message.tool_call_id
            if message.role != "tool" or not tool_use_id:
                continue
            replacement = self._replacements.get(tool_use_id)
            if replacement is not None and message.content != replacement:
                messages[index] = message.model_copy(update={"content": replacement})
                self._seen_ids.add(tool_use_id)
                continue
            if replacement is not None:
                self._seen_ids.add(tool_use_id)
                continue
            if tool_use_id in self._seen_ids:
                continue
            size = _content_size(message.content)
            self._seen_ids.add(tool_use_id)
            if size > 0:
                candidates.append((size, index, message))

        original_chars = sum(size for size, _, _ in candidates)
        remaining_chars = original_chars
        persisted_count = 0
        for size, index, message in sorted(candidates, key=lambda item: -item[0]):
            if remaining_chars <= self.aggregate_budget:
                break
            required_reduction = remaining_chars - self.aggregate_budget
            target_replacement_size = max(1, size - required_reduction)
            preview_limit = PREVIEW_SIZE_CHARS

            def build_replacement(limit: int) -> str | None:
                value = self._persist_content(
                    message.content,
                    message.tool_call_id or "tool-result",
                    session_id=session_id,
                    preview_max_chars=limit,
                )
                if value is not None and message.name == "read_file":
                    value += (
                        "\nRe-read the original path from the matching read_file call with "
                        "a smaller offset/limit to recover exact content."
                    )
                if value is not None and message.name == "read_email":
                    value += (
                        "\nThis email page is incomplete in this preview. Do not advance next_offset "
                        "until the full page is recovered. Re-read the original email at the same "
                        "offset with a smaller limit and expected_sha256 from the matching call/result, "
                        "or read the complete persisted output."
                    )
                return value

            replacement = build_replacement(preview_limit)
            if replacement is None:
                continue
            if len(replacement) > target_replacement_size:
                serialized = _content_text(message.content)
                current_preview_size = (
                    len(generate_preview(serialized[0], preview_limit)[0])
                    if serialized is not None
                    else preview_limit
                )
                preview_limit = max(
                    1,
                    current_preview_size
                    - (len(replacement) - target_replacement_size),
                )
                replacement = build_replacement(preview_limit)
                if replacement is None:
                    continue
            replacement_size = len(replacement)
            if replacement_size >= size:
                continue
            self._replacements[message.tool_call_id or ""] = replacement
            messages[index] = message.model_copy(update={"content": replacement})
            remaining_chars += replacement_size - size
            persisted_count += 1

        return ToolResultBudgetOutcome(
            fresh_count=len(candidates),
            persisted_count=persisted_count,
            original_chars=original_chars,
            remaining_chars=remaining_chars,
        )

    def _persist_content(
        self,
        content: str | list[dict[str, Any]],
        tool_use_id: str,
        *,
        session_id: str,
        model_content: str | list[dict[str, Any]] | None = None,
        preview_max_chars: int = PREVIEW_SIZE_CHARS,
    ) -> str | None:
        serialized = _content_text(content)
        if serialized is None:
            return None
        content_text, is_json = serialized
        effective_session_id = session_id or self._default_session_id
        session = _safe_path_segment(effective_session_id, "local")
        identifier = _safe_path_segment(tool_use_id, "tool-result")
        path = self.root_dir / session / "tool-results" / (
            f"{identifier}.json" if is_json else f"{identifier}.txt"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(content_text)
            except FileExistsError:
                try:
                    existing = path.read_text(encoding="utf-8")
                except OSError:
                    return None
                if existing != content_text:
                    digest = hashlib.sha256(content_text.encode("utf-8")).hexdigest()[:12]
                    path = path.with_name(f"{path.stem}-{digest}{path.suffix}")
                    try:
                        with path.open("x", encoding="utf-8") as stream:
                            stream.write(content_text)
                    except FileExistsError:
                        try:
                            if path.read_text(encoding="utf-8") != content_text:
                                return None
                        except OSError:
                            return None
        except OSError:
            return None
        preview_label = None
        if model_content is None:
            preview, has_more = generate_preview(
                content_text,
                max_chars=preview_max_chars,
            )
            preview_label = (
                "Preview (head + tail, up to "
                f"{_format_size(preview_max_chars)})"
            )
        else:
            serialized_model_content = _content_text(model_content)
            if serialized_model_content is None:
                preview, has_more = generate_preview(
                    content_text,
                    max_chars=preview_max_chars,
                )
                preview_label = (
                    "Preview (head + tail, up to "
                    f"{_format_size(preview_max_chars)})"
                )
            else:
                preview = serialized_model_content[0]
                has_more = False
                preview_label = "Tool-bounded output"
        return build_persisted_output(
            PersistedToolResult(
                path=path,
                original_size=len(content_text),
                is_json=is_json,
                preview=preview,
                has_more=has_more,
            ),
            preview_label=preview_label,
        )
