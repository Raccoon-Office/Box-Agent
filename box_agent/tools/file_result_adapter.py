"""File-result model content, resource receipts, and placeholder recovery."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..artifacts import artifact_scan_root as _artifact_scan_root
from ..context_resources import ContextResourceLedger, ResourceDescriptor, build_resource_receipt
from ..model_history import is_model_history_placeholder
from ..schema import Message
from .base import ToolResult

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

# Pattern to match <!--PLOT_DATA:...--> markers embedded by code execution.
# These carry interactive chart payloads already sent to the frontend via SSE;
# they must NOT be fed back into the model context.
_PLOT_DATA_RE = re.compile(r"<!--PLOT_DATA:.+?-->", re.DOTALL)


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
    root = _artifact_scan_root(workspace_dir)
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


_MODEL_HISTORY_PLACEHOLDER_REPAIR_LIMIT: Final[int] = 1
_MODEL_HISTORY_PLACEHOLDER_TOOL_ERROR = (
    "INTERNAL_MODEL_HISTORY_PLACEHOLDER: the requested tool argument is an internal "
    "history summary, not executable content. Regenerate the real argument. For static "
    "artifacts, use ordered write_file chunks instead of moving the body into execute_code."
)
_MODEL_HISTORY_PLACEHOLDER_REPAIR_GUIDANCE = (
    "An internal model-history placeholder was returned as a tool argument. Regenerate "
    "the missing real content now. Never copy text beginning with "
    "`[Full tool-call argument omitted from model history]`, `[Full file content omitted "
    "from model history]`, or `[Full tool output omitted from model history]` into any "
    "tool argument. For long static artifacts, continue write_file from the "
    "next_chunk_index returned by the last successful call for that path, or use "
    "chunk_index=0 only if no chunk has been accepted. Keep final=false until the "
    "last chunk; do not move the file body into execute_code."
)
