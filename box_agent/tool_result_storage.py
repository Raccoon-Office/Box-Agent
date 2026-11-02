"""Persist oversized tool results and bound their model/host representations."""

from __future__ import annotations

import json
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .schema import Message
from .tools.base import Tool
from .tools.output_capture import OUTPUT_REPLY_BYTES, bound_output_text


DEFAULT_MAX_RESULT_SIZE_CHARS = 20_000
DEFAULT_TOOL_RESULTS_BUDGET_CHARS = 50_000
PREVIEW_SIZE_CHARS = 2_000
CONTEXT_RESULT_LIMIT_RATIO = 0.25
CONTEXT_AGGREGATE_BUDGET_RATIO = 0.50
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
# Preserve valid read_file pages (up to 100k Unicode characters) and existing
# semantic model projections. The final budget also accounts for JSON escaping.
MAX_INLINE_OUTPUT_BYTES = 512 * 1024
MAX_RAW_OUTPUT_BYTES = 64 * 1024
MAX_TOOL_RESULT_BYTES = 1024 * 1024


def _json_chunks(value: Any, *, ascii_only: bool = False) -> Iterator[str]:
    """Encode JSON in bounded string chunks, rejecting cycles/invalid values.

    JSONEncoder.iterencode still constructs one huge string for a huge leaf.
    Escape string leaves in pieces instead, so persistence and size checks do
    not allocate another copy of an unbounded tool payload.
    """
    parents: set[int] = set()

    def encode(item: Any, depth: int) -> Iterator[str]:
        if depth > 100:
            raise ValueError("tool output nesting exceeds serialization limit")
        if isinstance(item, str):
            yield '"'
            for offset in range(0, len(item), 8192):
                yield json.dumps(item[offset:offset + 8192], ensure_ascii=ascii_only)[1:-1]
            yield '"'
        elif item is None or isinstance(item, (bool, int, float)):
            yield json.dumps(item, allow_nan=False)
        elif isinstance(item, (dict, list, tuple)):
            identity = id(item)
            if identity in parents:
                raise ValueError("circular tool output")
            parents.add(identity)
            try:
                mapping = isinstance(item, dict)
                yield "{" if mapping else "["
                for index, entry in enumerate(item.items() if mapping else item):
                    if index:
                        yield ","
                    if mapping:
                        key, child = entry
                        if not isinstance(key, str):
                            raise TypeError("tool output JSON keys must be strings")
                        yield from encode(key, depth + 1)
                        yield ":"
                    else:
                        child = entry
                    yield from encode(child, depth + 1)
                yield "}" if mapping else "]"
            finally:
                parents.remove(identity)
        else:
            raise TypeError("tool output is not JSON serializable")

    yield from encode(value, 0)


def _json_fits(value: Any, limit: int) -> bool:
    try:
        size = 0
        for chunk in _json_chunks(value):
            size += len(chunk.encode("utf-8"))
            if size > limit:
                return False
        return True
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class ToolResultDelivery:
    content: str
    error: str | None
    raw_output: dict[str, Any] | None
    model_content: str


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

    Model budgets keep their existing policy. prepare_delivery applies a final
    shared output budget before model history, public events and trace commit.
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

    def prepare_delivery(
        self,
        *,
        tool_call_id: str,
        session_id: str,
        content: str,
        error: str | None,
        raw_output: dict[str, Any] | None,
        model_content: str,
        original_model_content: str,
        event_metadata: dict[str, Any],
        raw_output_preview: dict[str, Any] | None = None,
    ) -> ToolResultDelivery:
        """Bound all tool results without changing execution/commit semantics."""
        directory = (
            self.root_dir / _safe_path_segment(session_id or self._default_session_id, "local")
            / "tool-results"
        )
        label = _safe_path_segment(tool_call_id, "result")
        stored_model_content = model_content
        # Reuse an existing model-side file instead of persisting stdout twice.
        if (
            content == original_model_content
            and model_content.startswith(PERSISTED_OUTPUT_TAG)
            and not _json_fits(content, MAX_INLINE_OUTPUT_BYTES)
        ):
            content = model_content
        elif (
            error and original_model_content == f"Error: {error}"
            and model_content.startswith(PERSISTED_OUTPUT_TAG)
            and not _json_fits(error, MAX_INLINE_OUTPUT_BYTES)
        ):
            if content == error:
                content = model_content
            error = model_content
        original_content = content
        content = bound_output_text(content, directory, label, inline_limit=MAX_INLINE_OUTPUT_BYTES)
        if error == original_content:
            error = content
        elif error is not None:
            error = bound_output_text(error, directory, label + "-error", inline_limit=MAX_INLINE_OUTPUT_BYTES)
        model_content = (
            content if model_content == original_content else
            bound_output_text(model_content, directory, label + "-model", inline_limit=MAX_INLINE_OUTPUT_BYTES)
        )
        if raw_output is not None and not _json_fits(raw_output, MAX_RAW_OUTPUT_BYTES):
            raw_output, notice = self._externalize_raw_output(raw_output, directory, label, raw_output_preview)
            content += "\n\n" + notice
            model_content += "\n\n" + notice
            if error is not None:
                error += "\n\n" + notice

        fields = {**event_metadata, "content": content, "error": error, "raw_output": raw_output}
        if not _json_fits(fields, MAX_TOOL_RESULT_BYTES):
            # Aggregate/escaping fallback. It uses the same full-file + preview
            # representation, rather than rejecting the completed invocation.
            previous_content = content
            content = bound_output_text(content, directory, label + "-event", inline_limit=OUTPUT_REPLY_BYTES)
            if error == previous_content:
                error = content
            elif error is not None:
                error = bound_output_text(error, directory, label + "-event-error", inline_limit=OUTPUT_REPLY_BYTES)
        if model_content != stored_model_content:
            # The later aggregate model budget must not restore the old receipt
            # and discard the structured-output reference or failure notice.
            self._replacements[tool_call_id] = model_content
        return ToolResultDelivery(content, error, raw_output, model_content)

    def record_delivery(self, tool_call_id: str, model_content: str) -> None:
        """Keep the aggregate budget from restoring a stale pre-fallback reply."""
        self._replacements[tool_call_id] = model_content

    def _externalize_raw_output(
        self, raw_output: dict[str, Any], directory: Path, label: str,
        tool_preview: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        path: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            candidate = directory / f"{label}-raw-{uuid4().hex[:12]}.json"
            with candidate.open("x", encoding="utf-8", newline="\n") as stream:
                path = candidate
                # ASCII escapes preserve even malformed Unicode in a valid JSON
                # file, without permitting it in the UTF-8 public event itself.
                for chunk in _json_chunks(raw_output, ascii_only=True):
                    stream.write(chunk)
            reference = {"path": str(path), "format": "json", "file_complete": True, "output_truncated": True}
            notice = (
                f"Structured output externalized. Full JSON saved to: {path}\n"
                "Inline metadata is incomplete. Inspect selected fields or bounded ranges; do not print the whole file."
            )
        except Exception as exc:
            reference = {"format": "json", "file_complete": False, "output_truncated": True, "error": type(exc).__name__}
            if path is not None:
                reference["partial_path"] = str(path)
            notice = (
                f"Full structured output unavailable ({type(exc).__name__}); "
                "only bounded metadata is retained. Omitted fields cannot be recovered."
            )
        projected: dict[str, Any] = {"_persisted_output": reference}
        # The capability owns business-field semantics. The common boundary only
        # validates its preview; it never recursively rewrites arbitrary data.
        if tool_preview is not None:
            projected.update({key: value for key, value in tool_preview.items() if key != "_persisted_output"})
            if _json_fits(projected, MAX_RAW_OUTPUT_BYTES):
                return projected, notice
            projected = {"_persisted_output": reference}
        # Unknown result shapes get a file receipt and only verbatim small
        # fields. No inferred schema or string/list-to-object substitutions.
        for containers in (False, True):
            for key, value in raw_output.items():
                if isinstance(value, (dict, list, tuple)) != containers:
                    continue
                if not isinstance(key, str) or key == "_persisted_output" or len(key) > 256 or not _json_fits(key, 2048):
                    continue
                if len(projected) >= 65:
                    break
                if _json_fits(value, 4096):
                    projected[key] = value
                if not _json_fits(projected, MAX_RAW_OUTPUT_BYTES):
                    del projected[key]
                    break
        return projected, notice

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

        def write_content(target: Path) -> None:
            with target.open("x", encoding="utf-8", newline="") as stream:
                # TextIOWrapper.write otherwise encodes one unbounded UTF-8
                # copy before the shared delivery guard can reduce the reply.
                for offset in range(0, len(content_text), 8192):
                    stream.write(content_text[offset:offset + 8192])

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                write_content(path)
            except FileExistsError:
                try:
                    existing = path.read_text(encoding="utf-8")
                except OSError:
                    return None
                if existing != content_text:
                    digest = hashlib.sha256(content_text.encode("utf-8")).hexdigest()[:12]
                    path = path.with_name(f"{path.stem}-{digest}{path.suffix}")
                    try:
                        write_content(path)
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
