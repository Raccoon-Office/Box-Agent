"""Controlled-presentation workflow policy.

The Agent kernel owns scheduling and tool execution.  This module owns the
presentation-specific stage machine and command/evidence restrictions.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import shlex
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Final
from urllib.parse import urlsplit

from ..config import ToolLimitsConfig
from ..artifacts import artifact_scan_root
from ..evidence import (
    extract_http_urls,
    extract_search_result_evidence,
    normalize_search_url,
)
from ..tools.base import ToolResult
from ..tools.browser_tool_names import is_browser_tool_name
from ..workflow_policy import WorkflowAction, WorkflowCheckpointUpdate
from ..workflow_checkpoint_store import (
    WorkflowPauseCheckpoint,
    checkpoint_resume_instruction,
)
from .presentation_checkpoint import build_checkpoint_text
from .presentation_contract import (
    CHECKPOINT_MARKER,
    WORKFLOW_KIND,
)

MANAGED_RESEARCH_NAVIGATE_TOOLS: Final[frozenset[str]] = frozenset(
    {"managed_browser_navigate"}
)
MANAGED_RESEARCH_SNAPSHOT_TOOLS: Final[frozenset[str]] = frozenset(
    {"managed_browser_snapshot"}
)
DIRECT_RESEARCH_READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "web_extract",
        "user_browser_open_tab_and_read",
        "user_browser_read_page",
        "user_browser_read_article",
        "user_browser_read_section",
        "managed_browser_navigate",
        "managed_browser_snapshot",
    }
)
RESEARCH_BUDGET_EXEMPT_TOOLS: Final[frozenset[str]] = (
    DIRECT_RESEARCH_READ_TOOLS | frozenset({"web_search"})
)
GATEWAY_RESEARCH_READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "user_browser_open_tab_and_read",
        "user_browser_read_page",
        "user_browser_read_article",
    }
)
RESEARCH_READ_BATCH_SIZE: Final[int] = 1
RESEARCH_DIRECT_READ_LIMIT: Final[int] = 5
RESEARCH_UNPRODUCTIVE_DIRECT_READ_LIMIT: Final[int] = 2
RESEARCH_DISCOVERY_ATTEMPT_LIMIT: Final[int] = 3
RESEARCH_ROUND_LIMIT: Final[int] = ToolLimitsConfig().presentation.research_rounds

_log = logging.getLogger(__name__)

_CONTENT_PATCH_BLOCKED_TOOLS: Final[frozenset[str]] = frozenset(
    {"read_file", "execute_code", "bash"}
)
_CONTENT_PATCH_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_PATCH_INPUT_READY: PATCH_INPUT in the latest checkpoint "
    "already contains the exact outline content, slide mapping, prop shapes, and ready "
    "media paths. Do not inspect files again. Write deck.patch.json now with write_file "
    "(use ordered chunk_index/final calls if one model response cannot hold the body)."
)
_PENDING_WRITE_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_WRITE_TRANSACTION_PENDING: an ordered write_file "
    "transaction is already active for {path}. Continue only that path with "
    "chunk_index={next_chunk_index}; do not restart at chunk_index=0, switch tools, "
    "or target another file."
)
_CONTENT_PATCH_REPAIR_ALLOWED_TOOLS: Final[frozenset[str]] = frozenset(
    {"read_file", "write_file", "append_file", "edit_file", "staged_file_write"}
)
_CONTENT_PATCH_REPAIR_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_PATCH_JSON_INCOMPLETE: deck.patch.json is not complete, "
    "valid JSON. Read, rewrite, edit, append, or complete ordered write_file chunks "
    "only for that file until it parses; do not run apply_deck_patch.js "
    "or mutate another artifact yet."
)
_CONTENT_PATCH_STAGED_ACTIVE_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_PATCH_STAGED_WRITE_ACTIVE: deck.patch.json already has "
    "an active staged_file_write transaction with write_id={write_id}. Continue that "
    "exact transaction with the next append_text/append_file chunk, commit it when "
    "complete, or abort it before starting over. Do not begin another transaction, "
    "read the unchanged target, or switch to another write tool."
)
_IMAGE_GENERATION_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_IMAGE_INPUT_READY: IMAGE_INPUT already contains the "
    "missing image paths, page intent, and theme palette. Call generate_image now "
    "with an exact listed output_path and watermark=false; do not inspect files or "
    "invent another path."
)
_IMAGE_AUTH_BLOCKED_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_IMAGE_AUTH_BLOCKED: the image service returned HTTP 401 "
    "for this presentation. Do not call generate_image or any other tool again in "
    "this turn. End the turn and report that image generation is blocked until the "
    "service authorization is refreshed."
)
_SCAFFOLD_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_SCAFFOLD_INPUT_READY: SCAFFOLD_INPUT in the latest "
    "checkpoint already contains every registered theme/layout id and every page "
    "intent. Invoke inspect_deck_contract.js once now with only --outline "
    "outline.json and --out deck.json; do not pass layout ids, --theme, "
    "--image-mode, --title, facts, or other optional flags, and do not reread files "
    "or list the registry. Invoke it on one physical command line using "
    "`cd <artifact-root> &&`; do not split `cd` and the inspector across lines. "
    "Keep the inspector as the only shell command: do not append a pipe, tail, "
    "redirection, or another command."
)
_SCAFFOLD_SHELL_SUFFIX_TOOL_ERROR = (
    f"{_SCAFFOLD_TOOL_ERROR} Rejected shell suffix: remove the entire pipe or "
    "redirection (for example `2>&1 | tail -N`) and invoke the inspector directly."
)
_OUTLINE_REPAIR_ALLOWED_TOOLS: Final[frozenset[str]] = frozenset(
    {"write_file", "staged_file_write"}
)
_REPAIR_ALLOWED_TOOLS: Final[frozenset[str]] = frozenset(
    {"write_file", "append_file", "staged_file_write"}
)
_MUTATION_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "write_file",
        "append_file",
        "edit_file",
        "bash",
        "execute_code",
        "generate_image",
    }
)
_REPAIR_STAGES: Final[frozenset[str]] = frozenset(
    {
        "outline_repair",
        "deck_spec_repair",
        "deck_redesign_repair",
        "content_patch_repair",
    }
)
_POLICY_REJECTION_STAGES: Final[frozenset[str]] = (
    _REPAIR_STAGES
    | frozenset({"research", "outline", "scaffold", "apply_patch", "apply_redesign"})
)
_REPEATED_EXECUTION_FAILURE_LIMIT: Final[int] = 2
_REPEATED_POLICY_REJECTION_LIMIT: Final[int] = 3
_REPAIR_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_REPAIR_INPUT_READY: REPAIR_INPUT in the latest "
    "checkpoint already contains the fresh hard deck-spec issues, affected current "
    "props, and outline context. Write the minimal deck.patch.json now; do not reread "
    "stale inputs or run another command first."
)
_REDESIGN_REPAIR_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_REDESIGN_INPUT_READY: REPAIR_INPUT contains a fresh "
    "layout-level deck-spec failure plus the affected slide's current layout and "
    "props. Write only deck.redesign.json with the minimal layout_id/props change; "
    "do not write deck.patch.json, reread stale inputs, or run another command first."
)
_OUTLINE_REPAIR_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_OUTLINE_REPAIR_INPUT_READY: REPAIR_INPUT in the "
    "latest checkpoint already contains the complete current outline and fresh "
    "validator issues. Write the corrected outline.json now with write_file, using "
    "ordered chunk_index/final calls if one model response cannot hold it; do not "
    "reread files, inspect the schema, update todos/plans, "
    "or run another command first."
)
_IMAGE_STATUS_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_IMAGE_STATUS_SYNC_REQUIRED: all planned image files "
    "exist. Run sync_image_manifest_status.js once with bash; do not reread/edit "
    "manifest.json or regenerate an existing image."
)
_IMAGE_POLICY_REBASE_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_IMAGE_POLICY_REBASE_REQUIRED: the latest checkpoint "
    "requires a deterministic image-policy rebase. Run the exact "
    "rebase_image_policy.js command from that checkpoint once; do not read or "
    "rewrite deck.json or the image manifest manually."
)
_FINALIZE_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_FINALIZE_REQUIRED: run the single deterministic "
    "finalizer now with bash using the absolute finalize_controlled_deck.js path "
    "from the latest checkpoint, followed by deck.json --out "
    "index.html. It enforces the hard deck-spec check, records advisory image/truth "
    "warnings, compiles HTML, runs self-check, and probes the editor in dependency "
    "order. Do "
    "not split that chain into separate validator/render commands or add another "
    "shell command."
)
_APPLY_PATCH_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_APPLY_PATCH_REQUIRED: run the single deterministic "
    "apply_deck_patch.js command from the latest checkpoint with deck.json and "
    "deck.patch.json. A trailing 2>&1 stderr merge is allowed, but do not add a pipe, "
    "another redirection, another command, substitute another script, or rewrite "
    "deck.json directly."
)
_APPLY_REDESIGN_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_APPLY_REDESIGN_REQUIRED: run the single deterministic "
    "apply_deck_redesign.js command from the latest checkpoint with deck.json and "
    "deck.redesign.json. Do not add pipes, redirections, or another command, and do "
    "not rewrite deck.json directly."
)
_APPLY_PATCH_REPAIR_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_APPLY_PATCH_REPAIR_REQUIRED: the latest deterministic "
    "apply_deck_patch.js call returned an actionable error. You may only read, edit, "
    "or rewrite deck.patch.json with the minimal named-field repair, "
    "replace a required unavailable fact with an explicit placeholder, omit an "
    "unsupported optional claim, or rerun the exact apply command. Do not ask for "
    "missing facts, read or rewrite deck.json, or run discovery commands."
)
_APPLY_PATCH_FIELD_MISMATCH = (
    "CONTROLLED_PRESENTATION_APPLY_PATCH_FIELD_MISMATCH: the proposed deck.patch.json "
    "repair does not change any field named by the latest deterministic error. "
    "Change one of these exact fields and leave unrelated slide content unchanged: {paths}."
)
_REPAIR_STALLED_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_REPAIR_STALLED: the bounded repair path exhausted "
    "consecutive no-progress attempts. Do not repeat the call or bypass the stage guard with "
    "a compound shell command, and do not ask for missing facts. End this turn and "
    "report the unresolved internal validation conflict."
)
_PLAN_SCOPE_ERROR = (
    "CONTROLLED_PRESENTATION_PLAN_SCOPE_INCOMPLETE: the user requested a finished "
    "presentation, so the execution plan cannot stop at outline/content planning. "
    "Publish a corrected plan that covers outline.json, deck.json scaffolding, "
    "content/media authoring, deterministic index.html finalization, and QA. Only "
    "an explicit user request for outline-only output may omit those delivery stages."
)
_PLAN_OUTLINE_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"(?:本轮|当前|这次)?[^。；;\n]{0,16}(?:仅|只)"
    r"[^。；;\n]{0,32}(?:outline|大纲|内容方案)"
    r"|(?:不进入|不生成|不制作|不渲染|不包含)"
    r"[^。；;\n]{0,32}(?:html|pptx?|页面|幻灯片|主题|版式|布局|脚手架|deck)"
    r"|\b(?:outline[- ]only|only\s+(?:produce|create|deliver)?\s*outline|"
    r"do\s+not\s+(?:generate|create|render|deliver)\s+"
    r"(?:slides?|pages?|html|deck))\b"
    r")",
    re.IGNORECASE,
)
_PLAN_DELIVERY_STEP_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:deck\.json|index\.html|finalize_controlled_deck|"
    r"(?:生成|制作|编译|渲染|交付|导出)[^。；;\n]{0,32}"
    r"(?:html|pptx?|页面|幻灯片|deck)|"
    r"\b(?:scaffold|render|compile|finalize|deliver|export)\b)",
    re.IGNORECASE,
)
_RESEARCH_HANDOFF_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_HANDOFF_READY: research QA is complete. "
    "Do not search/browse, create or update todos/plans, reread outline.md or the "
    "research QA report, or inspect/list the filesystem. Read only a Markdown "
    "handoff file explicitly named in RESEARCH_INPUT when its content is missing "
    "from context; otherwise write outline.json now. In output mode, call "
    "write_file with path=outline.json so it resolves inside the canonical artifact "
    "root; never use the absolute session-workspace path. If the complete JSON "
    "cannot fit in one model response, use ordered write_file calls for that same "
    "path: start with chunk_index=0 and final=false, increment the index, and set "
    "final=true on the last chunk."
)
_OUTLINE_TARGET_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_OUTLINE_TARGET_REQUIRED: write outline.json inside "
    "the canonical presentation artifact root. In output mode, use the exact "
    "artifact-relative path outline.json; never use the absolute session-workspace "
    "path. For a large outline, every ordered write_file chunk must use that same path."
)
_RESEARCH_ARTIFACT_TARGET_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_ARTIFACT_TARGET_REQUIRED: keep research "
    "artifacts under the canonical presentation artifact root. Use an "
    "artifact-relative research/... path; never use an absolute session-workspace "
    "research path."
)
_RESEARCH_SEARCH_COMPLETE_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_SEARCH_COMPLETE: bounded research searches "
    "are complete. Do not call web_search or tool_search again. URL-bound provider "
    "summaries may support medium-confidence evidence when the ledger excerpt is copied "
    "from that exact result; read a small set of exact authoritative pages only when "
    "stronger evidence is useful. Then complete the ledger and validation report."
)
_RESEARCH_EXECUTE_CODE_NETWORK_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_NETWORK_TOOL_REQUIRED: network access through "
    "execute_code bypasses research accounting and source provenance. Do not use "
    "Python network libraries, URL-reading data APIs, socket connections, or curl/wget "
    "subprocesses for research retrieval. Call tool_search with one short capability or "
    "exact tool name at a time, then use web_search or an activated exact-page reader."
)
_NETWORK_MODULE_PREFIXES: Final[tuple[str, ...]] = (
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "http.client",
    "socket",
)
_URL_READER_NAMES: Final[frozenset[str]] = frozenset(
    {"read_csv", "read_excel", "read_html", "read_json", "read_parquet", "read_xml"}
)
_PROCESS_CALL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "os.popen",
        "os.system",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.run",
    }
)
_RESEARCH_DIRECT_READ_COMPLETE_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_DIRECT_READ_COMPLETE: the bounded direct-source "
    "verification pass is complete after five attempts or two consecutive reads that "
    "did not yield usable source content. Do not retry exact-page reads. Mark any unread "
    "source rows unverified (or omit optional unsupported claims), then finish the "
    "evidence ledger and run validate_research_artifacts.py."
)
_RESEARCH_EXACT_SOURCE_URL_REQUIRED_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_EXACT_SOURCE_URL_REQUIRED: after bounded search, direct "
    "verification must use an exact article, report, filing, or data-page URL. Do not "
    "open an origin homepage or an empty URL. Mark the source unverified if no exact "
    "candidate URL is available."
)
_RESEARCH_DIRECT_URL_ALREADY_ATTEMPTED_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_DIRECT_URL_ALREADY_ATTEMPTED: this exact source URL was "
    "already attempted with the same page-reading backend. Do not retry it. Use one "
    "different exact candidate URL, use the alternate browser backend once, or mark "
    "the source unverified and continue."
)
_RESEARCH_SNAPSHOT_REQUIRED_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_SNAPSHOT_REQUIRED: managed_browser_navigate "
    "reached an exact source URL but returned navigation metadata only. Call "
    "managed_browser_snapshot now before navigating elsewhere; the snapshot body "
    "will be bound to that URL."
)
_RESEARCH_SNAPSHOT_NAVIGATION_REQUIRED_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_NAVIGATION_REQUIRED: managed_browser_snapshot "
    "can verify research evidence only immediately after one successful metadata-only "
    "managed_browser_navigate call. Navigate one exact candidate URL first."
)
_RESEARCH_LOCAL_READ_COMPLETE_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_LOCAL_READ_COMPLETE: bounded research is "
    "complete. Do not inspect/list files or reread skill references, validator "
    "source, or Markdown research notes. Use the evidence already in context to "
    "write the remaining research artifacts and run validate_research_artifacts.py "
    "with --report. After a failed validation, you may read its JSON report and the "
    "JSON evidence ledger once, then make a repair before reading either again."
)
_RESEARCH_BROWSER_REINSPECTION_COMPLETE_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_BROWSER_REINSPECTION_COMPLETE: bounded "
    "research is complete. Do not inspect browser tabs, execute page scripts, "
    "or use another browser-state side channel. Read only an "
    "exact candidate URL with user_browser_read_page, user_browser_read_article, "
    "user_browser_read_section, user_browser_open_tab_and_read, or "
    "managed_browser_navigate followed by managed_browser_snapshot before opening "
    "another URL. Then "
    "write the remaining research artifacts and run validate_research_artifacts.py."
)
_RESEARCH_BROWSER_CONNECTOR_UNAVAILABLE_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_BROWSER_CONNECTOR_UNAVAILABLE: the browser "
    "connector already returned source_unavailable in this research run. Do not "
    "retry user_browser_read_page, user_browser_read_article, or "
    "user_browser_open_tab_and_read. Use the available managed_browser_navigate plus "
    "managed_browser_snapshot pair with "
    "one exact candidate URL at a time, or mark the source unverified and continue to "
    "the research artifacts."
)
_RESEARCH_REVALIDATION_REQUIRED_TOOL_ERROR = (
    "CONTROLLED_PRESENTATION_RESEARCH_REVALIDATION_REQUIRED: research artifacts are "
    "newer than their QA report. Run the single validate_research_artifacts.py "
    "command from RESEARCH_INPUT.revalidation.command now. You may use it exactly as "
    "shown or prefix only `cd <artifact-root> &&`; do not search, browse, read, list, "
    "rewrite files, append shell commands, use pipes/redirections, or alter its "
    "arguments first."
)

_PPTX_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "document-skills"
    / "pptx"
    / "scripts"
)
_FINALIZER_SCRIPT = _PPTX_SCRIPTS_DIR / "finalize_controlled_deck.js"
_INSPECT_SCRIPT = _PPTX_SCRIPTS_DIR / "inspect_deck_contract.js"
_APPLY_PATCH_SCRIPT = _PPTX_SCRIPTS_DIR / "apply_deck_patch.js"
_APPLY_REDESIGN_SCRIPT = _PPTX_SCRIPTS_DIR / "apply_deck_redesign.js"
_REBASE_IMAGE_POLICY_SCRIPT = _PPTX_SCRIPTS_DIR / "rebase_image_policy.js"
_VALIDATE_OUTLINE_SCRIPT = _PPTX_SCRIPTS_DIR / "validate_outline.js"
_SYNC_IMAGE_STATUS_SCRIPT = _PPTX_SCRIPTS_DIR / "sync_image_manifest_status.js"
_JSON_MISSING = object()


def _plan_scope_error(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> str | None:
    if (
        stage in {None, "complete"}
        or tool_name != "plan_write"
        or str(arguments.get("action") or "").lower() != "set"
    ):
        return None
    restrictive_text = json.dumps(
        {
            "objective": arguments.get("objective"),
            "scope": arguments.get("scope"),
            "risks": arguments.get("risks"),
            "assumptions": arguments.get("assumptions"),
        },
        ensure_ascii=False,
        default=str,
    )
    if not _PLAN_OUTLINE_ONLY_RE.search(restrictive_text):
        return None
    delivery_text = json.dumps(
        {
            "steps": arguments.get("steps"),
            "verification": arguments.get("verification"),
        },
        ensure_ascii=False,
        default=str,
    )
    if _PLAN_DELIVERY_STEP_RE.search(delivery_text):
        return None
    return _PLAN_SCOPE_ERROR


def _image_status_error(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> str | None:
    if stage != "image_status_sync":
        return None
    command = arguments.get("command")
    if (
        tool_name == "bash"
        and isinstance(command, str)
        and "sync_image_manifest_status.js" in command
        and "assets/generated/manifest.json" in command
    ):
        return None
    return _IMAGE_STATUS_TOOL_ERROR


def _finalize_error(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> str | None:
    if stage != "finalize":
        return None
    command = arguments.get("command")
    if tool_name != "bash" or not isinstance(command, str):
        return _FINALIZE_TOOL_ERROR
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _FINALIZE_TOOL_ERROR
    script_indexes = [
        index
        for index, token in enumerate(tokens)
        if Path(token).name == "finalize_controlled_deck.js"
    ]
    if len(script_indexes) != 1:
        return _FINALIZE_TOOL_ERROR
    script_index = script_indexes[0]
    if script_index < 1:
        return _FINALIZE_TOOL_ERROR
    node_token = tokens[script_index - 1]
    if not (
        Path(node_token).name in {"node", "node.exe"}
        or "BOX_AGENT_NODE" in node_token
    ):
        return _FINALIZE_TOOL_ERROR
    supplied_script = Path(tokens[script_index])
    if (
        not supplied_script.is_absolute()
        or supplied_script.resolve() != _FINALIZER_SCRIPT
    ):
        return _FINALIZE_TOOL_ERROR
    command_prefix = tokens[: script_index - 1]
    if command_prefix and not (
        len(command_prefix) == 3
        and command_prefix[0] == "cd"
        and command_prefix[1]
        and command_prefix[2] == "&&"
    ):
        return _FINALIZE_TOOL_ERROR
    finalizer_args = tokens[script_index + 1 :]
    if (
        len(finalizer_args) == 3
        and Path(finalizer_args[0]).name == "deck.json"
        and finalizer_args[1] == "--out"
        and Path(finalizer_args[2]).name == "index.html"
    ):
        return None
    return _FINALIZE_TOOL_ERROR


def _finalizer_failure_signature(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
) -> str | None:
    if result.success or _finalize_error("finalize", tool_name, arguments):
        return None
    payload = "\n".join(
        part for part in (result.error, result.content) if isinstance(part, str) and part
    )
    if not payload.strip():
        return "empty-finalizer-failure"
    marker = payload.find("FINALIZE_STOP")
    semantic = payload[marker:] if marker >= 0 else payload
    return re.sub(r"\s+", " ", semantic).strip()[:4000]


def _is_outline_validation_call(
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    if tool_name != "bash":
        return False
    command = arguments.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    script_tokens = [
        token
        for token in tokens
        if Path(token).name == "validate_outline.js"
    ]
    if len(script_tokens) != 1:
        return False
    supplied_script = Path(script_tokens[0])
    return (
        supplied_script.is_absolute()
        and supplied_script.resolve() == _VALIDATE_OUTLINE_SCRIPT
    )


def _latest_valid_outline_research_handoff(artifact_root: Path) -> Path | None:
    """Return the newest valid handoff, including a framework fallback status."""
    qa_root = artifact_root / "research" / "qa"
    candidates = [
        *qa_root.glob("*_research_check.json"),
        *qa_root.glob("*_presentation_handoff.json"),
        qa_root / "research_status.json",
    ]
    valid: list[tuple[int, Path]] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            handoff = (
                payload.get("presentation_handoff")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(handoff, dict):
                handoff = payload if isinstance(payload, dict) else None
            delivery_mode = handoff.get("delivery_mode") if handoff else None
            verified_facts = handoff.get("verified_facts") if handoff else None
            schema_version = handoff.get("schema_version") if handoff else None
            if (
                schema_version != 1
                or delivery_mode not in {"full", "partial", "framework"}
                or not isinstance(verified_facts, list)
                or (
                    delivery_mode in {"full", "partial"}
                    and not verified_facts
                )
                or (delivery_mode == "framework" and verified_facts)
            ):
                continue
            valid.append((path.stat().st_mtime_ns, path))
        except (OSError, json.JSONDecodeError):
            continue
    return max(valid, key=lambda item: item[0])[1] if valid else None


def _outline_validation_failure_signature(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    workspace_dir: str | None,
) -> str | None:
    if result.success or not _is_outline_validation_call(tool_name, arguments):
        return None

    report_candidates: list[Path] = []
    command = arguments.get("command")
    try:
        tokens = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        tokens = []
    if "--report" in tokens:
        report_index = tokens.index("--report") + 1
        if report_index < len(tokens):
            requested_report = Path(tokens[report_index])
            if requested_report.is_absolute():
                report_candidates.append(requested_report)
            elif workspace_dir:
                root = Path(workspace_dir)
                report_candidates.extend(
                    (root / requested_report, root / "output" / requested_report)
                )
    if workspace_dir:
        report_candidates.extend(
            (Path(workspace_dir) / "output").rglob("outline_check.json")
        )
    existing_reports = [path for path in report_candidates if path.is_file()]
    if existing_reports:
        report_path = max(existing_reports, key=lambda path: path.stat().st_mtime_ns)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
        if isinstance(report, dict) and report.get("ok") is False:
            semantic = {
                "issues": report.get("issues") or [],
                "warnings": report.get("warnings") or [],
            }
            return json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )[:4000]

    payload = "\n".join(
        part for part in (result.error, result.content) if isinstance(part, str) and part
    )
    return re.sub(r"\s+", " ", payload).strip()[:4000] or "empty-outline-failure"


def _failure_field_paths(result: ToolResult) -> tuple[str, ...]:
    payload = "\n".join(
        part for part in (result.error, result.content) if isinstance(part, str) and part
    )
    return tuple(
        dict.fromkeys(
            re.findall(
                r"(?m)^((?:slides)(?:\.[A-Za-z0-9_-]+){2,}):",
                payload,
            )
        )
    )


def _patch_file(
    workspace_dir: str | None,
    requested_path: str,
) -> Path | None:
    requested = Path(requested_path)
    candidates: list[Path] = []
    if requested.is_absolute():
        candidates.append(requested)
    elif workspace_dir:
        root = Path(workspace_dir)
        candidates.extend((root / requested, root / "output" / requested))
        if requested.name == "deck.patch.json":
            candidates.extend((root / "output").rglob("deck.patch.json"))
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime_ns)


def _json_path_value(document: Any, field_path: str) -> Any:
    parts = field_path.split(".")
    if (
        parts[:1] == ["slides"]
        and isinstance(document, dict)
        and not isinstance(document.get("slides"), dict)
        and len(parts) > 1
        and isinstance(document.get(parts[1]), dict)
    ):
        parts = parts[1:]
    current = document
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, dict) and part.isdigit():
            slide_keys = sorted(
                (
                    key
                    for key in current
                    if isinstance(key, str) and re.fullmatch(r"slide-\d+", key)
                ),
                key=lambda key: int(key.rsplit("-", 1)[-1]),
            )
            index = int(part)
            if index >= len(slide_keys):
                return _JSON_MISSING
            current = current[slide_keys[index]]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _JSON_MISSING
    return current


def _patch_repair_changes_named_field(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    repair_paths: tuple[str, ...],
) -> bool:
    if not repair_paths:
        return True
    patch_arg = arguments.get("path")
    if not isinstance(patch_arg, str):
        return False
    patch_file = _patch_file(workspace_dir, patch_arg)
    try:
        before_text = patch_file.read_text(encoding="utf-8") if patch_file else "{}"
        before = json.loads(before_text)
        if tool_name == "write_file":
            after_text = arguments.get("content")
        elif tool_name == "edit_file":
            old_str = arguments.get("old_str")
            new_str = arguments.get("new_str")
            if (
                not isinstance(old_str, str)
                or not isinstance(new_str, str)
                or old_str not in before_text
            ):
                return False
            after_text = before_text.replace(old_str, new_str, 1)
        else:
            return False
        if not isinstance(after_text, str):
            return False
        after = json.loads(after_text)
    except (OSError, json.JSONDecodeError):
        return False
    return any(
        _json_path_value(before, path) != _json_path_value(after, path)
        for path in repair_paths
    )


def _apply_patch_error(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    repair_allowed: bool = False,
    repair_paths: tuple[str, ...] = (),
    workspace_dir: str | None = None,
    artifact_root_dir: str | Path | None = None,
    staged_write_id: str | None = None,
) -> str | None:
    if stage != "apply_patch":
        return None
    if repair_allowed:
        patch_path = arguments.get("path")
        safe_patch_path = (
            isinstance(patch_path, str)
            and Path(patch_path).name == "deck.patch.json"
            and ".." not in Path(patch_path).parts
        )
        if tool_name == "read_file" and safe_patch_path:
            return None
        if tool_name == "staged_file_write":
            action = arguments.get("action")
            if action == "begin" and safe_patch_path:
                return None
            if (
                action in {"append_text", "append_file", "commit", "abort"}
                and staged_write_id is not None
                and arguments.get("write_id") == staged_write_id
            ):
                return None
        if (
            tool_name == "write_file"
            and safe_patch_path
            and isinstance(arguments.get("content"), str)
        ):
            chunk_index = arguments.get("chunk_index", 0)
            if arguments.get("final", True) is False or (
                isinstance(chunk_index, int)
                and not isinstance(chunk_index, bool)
                and chunk_index > 0
            ):
                return None
            return (
                None
                if _patch_repair_changes_named_field(
                    tool_name,
                    arguments,
                    workspace_dir,
                    repair_paths,
                )
                else _APPLY_PATCH_FIELD_MISMATCH.format(
                    paths=", ".join(repair_paths)
                )
            )
        if (
            tool_name == "edit_file"
            and safe_patch_path
            and isinstance(arguments.get("old_str"), str)
            and isinstance(arguments.get("new_str"), str)
        ):
            return (
                None
                if _patch_repair_changes_named_field(
                    tool_name,
                    arguments,
                    workspace_dir,
                    repair_paths,
                )
                else _APPLY_PATCH_FIELD_MISMATCH.format(
                    paths=", ".join(repair_paths)
                )
            )
    command = arguments.get("command")
    if tool_name != "bash" or not isinstance(command, str):
        return _APPLY_PATCH_REPAIR_TOOL_ERROR if repair_allowed else _APPLY_PATCH_TOOL_ERROR
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _APPLY_PATCH_REPAIR_TOOL_ERROR if repair_allowed else _APPLY_PATCH_TOOL_ERROR
    if tokens and tokens[-1] == "2>&1":
        # Merging stderr into stdout does not change the deterministic mutation.
        # Models commonly add this harmless suffix when invoking Node scripts;
        # accept only this exact trailing token and keep every pipe, extra
        # redirection, and compound command rejected below.
        tokens = tokens[:-1]
    script_indexes = [
        index
        for index, token in enumerate(tokens)
        if Path(token).name == "apply_deck_patch.js"
    ]
    if len(script_indexes) != 1:
        return _APPLY_PATCH_REPAIR_TOOL_ERROR if repair_allowed else _APPLY_PATCH_TOOL_ERROR
    script_index = script_indexes[0]
    if script_index < 1:
        return _APPLY_PATCH_REPAIR_TOOL_ERROR if repair_allowed else _APPLY_PATCH_TOOL_ERROR
    node_token = tokens[script_index - 1]
    if not (
        Path(node_token).name in {"node", "node.exe"}
        or "BOX_AGENT_NODE" in node_token
    ):
        return _APPLY_PATCH_REPAIR_TOOL_ERROR if repair_allowed else _APPLY_PATCH_TOOL_ERROR
    supplied_script = Path(tokens[script_index])
    if (
        not supplied_script.is_absolute()
        or supplied_script.resolve() != _APPLY_PATCH_SCRIPT
    ):
        return _APPLY_PATCH_REPAIR_TOOL_ERROR if repair_allowed else _APPLY_PATCH_TOOL_ERROR
    command_prefix = tokens[: script_index - 1]
    if command_prefix and not (
        len(command_prefix) == 3
        and command_prefix[0] == "cd"
        and command_prefix[1]
        and command_prefix[2] == "&&"
    ):
        return _APPLY_PATCH_REPAIR_TOOL_ERROR if repair_allowed else _APPLY_PATCH_TOOL_ERROR
    supplied_paths = tokens[script_index + 1 :]
    if len(supplied_paths) != 2:
        return _APPLY_PATCH_REPAIR_TOOL_ERROR if repair_allowed else _APPLY_PATCH_TOOL_ERROR
    if not _is_canonical_artifact_target(
        supplied_paths[0],
        "deck.json",
        workspace_dir,
        artifact_root_dir,
    ) or not _is_canonical_artifact_target(
        supplied_paths[1],
        "deck.patch.json",
        workspace_dir,
        artifact_root_dir,
    ):
        return _APPLY_PATCH_REPAIR_TOOL_ERROR if repair_allowed else _APPLY_PATCH_TOOL_ERROR
    return None


def _apply_patch_failure_signature(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> str | None:
    if result.success or _apply_patch_error(
        "apply_patch",
        tool_name,
        arguments,
        workspace_dir=workspace_dir,
        artifact_root_dir=artifact_root_dir,
    ):
        return None
    payload = "\n".join(
        part for part in (result.error, result.content) if isinstance(part, str) and part
    )
    if not payload.strip():
        return "empty-apply-patch-failure"
    marker = payload.find("Error:")
    semantic = payload[marker:] if marker >= 0 else payload
    return re.sub(r"\s+", " ", semantic).strip()[:4000]


def _apply_redesign_error(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> str | None:
    if stage != "apply_redesign":
        return None
    command = arguments.get("command")
    if tool_name != "bash" or not isinstance(command, str):
        return _APPLY_REDESIGN_TOOL_ERROR
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _APPLY_REDESIGN_TOOL_ERROR
    script_indexes = [
        index
        for index, token in enumerate(tokens)
        if Path(token).name == "apply_deck_redesign.js"
    ]
    if len(script_indexes) != 1 or script_indexes[0] < 1:
        return _APPLY_REDESIGN_TOOL_ERROR
    script_index = script_indexes[0]
    node_token = tokens[script_index - 1]
    if not (
        Path(node_token).name in {"node", "node.exe"}
        or "BOX_AGENT_NODE" in node_token
    ):
        return _APPLY_REDESIGN_TOOL_ERROR
    supplied_script = Path(tokens[script_index])
    if (
        not supplied_script.is_absolute()
        or supplied_script.resolve() != _APPLY_REDESIGN_SCRIPT
    ):
        return _APPLY_REDESIGN_TOOL_ERROR
    command_prefix = tokens[: script_index - 1]
    if command_prefix and not (
        len(command_prefix) == 3
        and command_prefix[0] == "cd"
        and command_prefix[1]
        and command_prefix[2] == "&&"
    ):
        return _APPLY_REDESIGN_TOOL_ERROR
    supplied_paths = tokens[script_index + 1 :]
    if len(supplied_paths) != 2:
        return _APPLY_REDESIGN_TOOL_ERROR
    if not _is_canonical_artifact_target(
        supplied_paths[0],
        "deck.json",
        workspace_dir,
        artifact_root_dir,
    ) or not _is_canonical_artifact_target(
        supplied_paths[1],
        "deck.redesign.json",
        workspace_dir,
        artifact_root_dir,
    ):
        return _APPLY_REDESIGN_TOOL_ERROR
    return None


def _content_patch_repair_error(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    staged_write_id: str | None = None,
) -> str | None:
    if stage != "content_patch_repair":
        return None
    path = arguments.get("path")
    safe_patch_path = (
        isinstance(path, str)
        and Path(path).name == "deck.patch.json"
        and ".." not in Path(path).parts
    )
    if tool_name == "staged_file_write":
        action = arguments.get("action")
        if staged_write_id is not None:
            if (
                action in {"append_text", "append_file", "commit", "abort"}
                and arguments.get("write_id") == staged_write_id
            ):
                return None
            return _CONTENT_PATCH_STAGED_ACTIVE_TOOL_ERROR.format(
                write_id=staged_write_id
            )
        if action == "begin" and safe_patch_path:
            return None
        return _CONTENT_PATCH_REPAIR_TOOL_ERROR
    if staged_write_id is not None:
        return _CONTENT_PATCH_STAGED_ACTIVE_TOOL_ERROR.format(
            write_id=staged_write_id
        )
    if tool_name not in _CONTENT_PATCH_REPAIR_ALLOWED_TOOLS or not safe_patch_path:
        return _CONTENT_PATCH_REPAIR_TOOL_ERROR
    return None


def _image_policy_rebase_error(
    stage: str | None,
    expected_policy: str | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> str | None:
    if stage != "image_policy_rebase":
        return None
    if expected_policy not in {"forbidden", "unavailable", "retry"}:
        return _IMAGE_POLICY_REBASE_TOOL_ERROR
    command = arguments.get("command")
    if tool_name != "bash" or not isinstance(command, str):
        return _IMAGE_POLICY_REBASE_TOOL_ERROR
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _IMAGE_POLICY_REBASE_TOOL_ERROR
    script_indexes = [
        index
        for index, token in enumerate(tokens)
        if Path(token).name == "rebase_image_policy.js"
    ]
    if len(script_indexes) != 1 or script_indexes[0] < 1:
        return _IMAGE_POLICY_REBASE_TOOL_ERROR
    script_index = script_indexes[0]
    node_token = tokens[script_index - 1]
    supplied_script = Path(tokens[script_index])
    if not (
        Path(node_token).name in {"node", "node.exe"}
        or "BOX_AGENT_NODE" in node_token
    ):
        return _IMAGE_POLICY_REBASE_TOOL_ERROR
    if (
        not supplied_script.is_absolute()
        or supplied_script.resolve() != _REBASE_IMAGE_POLICY_SCRIPT
    ):
        return _IMAGE_POLICY_REBASE_TOOL_ERROR
    command_prefix = tokens[: script_index - 1]
    if command_prefix and not (
        len(command_prefix) == 3
        and command_prefix[0] == "cd"
        and command_prefix[1]
        and command_prefix[2] == "&&"
    ):
        return _IMAGE_POLICY_REBASE_TOOL_ERROR
    if tokens[script_index + 1 :] != [
        "deck.json",
        "--manifest",
        "assets/generated/manifest.json",
        "--policy",
        expected_policy,
    ]:
        return _IMAGE_POLICY_REBASE_TOOL_ERROR
    return None


def _repair_stalled_checkpoint() -> str:
    return (
        "Internal controlled-presentation checkpoint; the bounded repair path "
        "exhausted consecutive no-progress attempts, so filesystem writes are now "
        "stopped to prevent an unbounded loop.\n"
        f"{CHECKPOINT_MARKER}repair_stalled\n"
        "NEXT_ACTION=Do not call another write/apply/finalize or validation tool. "
        "Do not ask for missing facts; they must already have been represented by "
        "explicit placeholders or omitted when optional. If index.html already exists, "
        "return it as a degraded draft and name the failed QA check; do not claim that "
        "no deliverable was produced. Only state that delivery is incomplete when no "
        "HTML artifact exists."
    )


def _image_auth_blocked_checkpoint() -> str:
    return (
        "Internal controlled-presentation checkpoint; the image service returned "
        "HTTP 401, which is a non-retryable authorization failure for this turn. "
        "Further image requests are stopped to prevent an unbounded retry loop.\n"
        f"{CHECKPOINT_MARKER}image_auth_blocked\n"
        "NEXT_ACTION=Do not call generate_image or any other tool again. End the "
        "turn and state that delivery is incomplete because image generation "
        "authorization must be refreshed before retrying."
    )


def _checkpoint_json(
    checkpoint_text: str,
    label: str,
) -> dict[str, Any] | None:
    prefix = f"{label}="
    for line in checkpoint_text.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            value = json.loads(line[len(prefix) :])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _research_handoff_urls(
    checkpoint_text: str,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> set[str]:
    research_input = _checkpoint_json(checkpoint_text, "RESEARCH_INPUT")
    if not research_input or research_input.get("ready") is not True:
        return set()
    root = artifact_scan_root(workspace_dir, artifact_root_dir)
    if root is None:
        return set()
    root = root.resolve()
    urls: set[str] = set()
    files = research_input.get("files")
    if not isinstance(files, list):
        return urls
    for relative_path in files:
        if not isinstance(relative_path, str) or not relative_path.endswith(".md"):
            continue
        candidate = (root / relative_path).resolve()
        if not candidate.is_relative_to(root):
            continue
        try:
            if not candidate.is_file() or candidate.stat().st_size > 4 * 1024 * 1024:
                continue
            urls.update(extract_http_urls(candidate.read_text(encoding="utf-8")))
        except OSError:
            continue
    return urls


def _research_validation_ledger(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> Path | None:
    """Resolve the evidence ledger used by one research validator call."""
    if not _is_research_validation_call(tool_name, arguments):
        return None
    command = arguments.get("command")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if "--research-dir" not in tokens or "--topic" not in tokens:
        return None
    research_index = tokens.index("--research-dir") + 1
    topic_index = tokens.index("--topic") + 1
    if research_index >= len(tokens) or topic_index >= len(tokens):
        return None
    research_arg = Path(tokens[research_index])
    topic = tokens[topic_index].strip()
    if not topic or Path(topic).name != topic:
        return None

    roots: list[Path] = []
    if workspace_dir:
        workspace_root = Path(workspace_dir).expanduser().resolve()
        roots.append(workspace_root)
        if len(tokens) >= 3 and tokens[0] == "cd" and tokens[2] == "&&":
            cd_path = Path(tokens[1]).expanduser()
            roots.insert(
                0,
                (
                    cd_path if cd_path.is_absolute() else workspace_root / cd_path
                ).resolve(),
            )
    artifact_root = artifact_scan_root(workspace_dir, artifact_root_dir)
    if artifact_root is not None:
        roots.append(artifact_root.resolve())

    candidates = (
        [research_arg / f"{topic}_evidence.json"]
        if research_arg.is_absolute()
        else [root / research_arg / f"{topic}_evidence.json" for root in roots]
    )
    existing = [candidate for candidate in candidates if candidate.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime_ns) if existing else None


def _research_validation_report(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> Path | None:
    """Resolve the JSON report written by one research validator call."""
    if not _is_research_validation_call(tool_name, arguments):
        return None
    command = arguments.get("command")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if "--report" not in tokens:
        return None
    report_index = tokens.index("--report") + 1
    if report_index >= len(tokens):
        return None
    report_token = tokens[report_index].rstrip(";")
    if not report_token:
        return None
    report_arg = Path(report_token).expanduser()

    roots: list[Path] = []
    if workspace_dir:
        workspace_root = Path(workspace_dir).expanduser().resolve()
        roots.append(workspace_root)
        if len(tokens) >= 2 and tokens[0] == "cd":
            cd_path = Path(tokens[1]).expanduser()
            roots.insert(
                0,
                (
                    cd_path if cd_path.is_absolute() else workspace_root / cd_path
                ).resolve(),
            )
    artifact_root = artifact_scan_root(workspace_dir, artifact_root_dir)
    if artifact_root is not None:
        roots.insert(0, artifact_root.resolve())

    if report_arg.is_absolute():
        candidates = [report_arg.resolve()]
    else:
        candidates = [(root / report_arg).resolve() for root in roots]
    existing = [candidate for candidate in candidates if candidate.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime_ns) if existing else None


def _is_research_validation_call(
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    if tool_name != "bash":
        return False
    command = arguments.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return any(
        Path(token).name == "validate_research_artifacts.py" for token in tokens
    )


def _research_validation_failed(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> bool:
    """Return the validator outcome even when a trailing shell command masks it."""
    if not _is_research_validation_call(tool_name, arguments):
        return False
    if not result.success:
        return True

    payload = "\n".join(
        part
        for part in (result.content, result.error, result.model_context)
        if isinstance(part, str) and part
    )
    exit_markers = re.findall(r"(?im)^\s*EXIT\s*=\s*(-?\d+)\s*$", payload)
    if exit_markers:
        return int(exit_markers[-1]) != 0

    report = _research_validation_report(
        tool_name,
        arguments,
        workspace_dir,
        artifact_root_dir,
    )
    if report is None:
        return False
    try:
        report_payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(report_payload, dict):
        return False
    handoff = report_payload.get("presentation_handoff")
    generic_delivery_allowed = bool(
        isinstance(handoff, dict)
        and handoff.get("schema_version") == 1
        and handoff.get("delivery_mode") in {"full", "partial", "framework"}
    )
    return bool(
        report_payload.get("ok") is False
        and report_payload.get("delivery_allowed") is not True
        and not generic_delivery_allowed
    )


def _is_substantive_research_url(value: Any) -> bool:
    """Return whether a URL identifies content beyond an origin homepage."""
    normalized = normalize_search_url(value)
    if not normalized.startswith(("http://", "https://")):
        return False
    parsed = urlsplit(normalized)
    return bool(parsed.path.strip("/") or parsed.query)


_DIRECT_URL_ARGUMENT_NAMES: Final[tuple[str, ...]] = (
    "url",
    "URL",
    "href",
    "uri",
    "source_url",
    "target_url",
)
_NON_PAGE_READ_RESEARCH_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "web_search",
        "tool_search",
        "search",
        "search_files",
        "read_file",
        "write_file",
        "append_file",
        "edit_file",
        "staged_file_write",
        "bash",
        "execute_code",
        "generate_image",
    }
)


def _research_direct_url_argument(arguments: dict[str, Any]) -> str:
    """Return an exact URL explicitly bound by a tool invocation."""
    for name in _DIRECT_URL_ARGUMENT_NAMES:
        value = arguments.get(name)
        if isinstance(value, str) and value.strip():
            return normalize_search_url(value)
    return ""


def _is_direct_research_read_call(
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    """Classify exact-page reads by evidence shape, not provider identity.

    Known browser pairs remain explicit because snapshots inherit the URL from a
    preceding navigation. Other user-selected MCP tools qualify only when the
    invocation itself binds a substantive public URL. Search/query tools and
    browser state side channels stay discovery-only.
    """
    if tool_name in DIRECT_RESEARCH_READ_TOOLS:
        return True
    if tool_name in _NON_PAGE_READ_RESEARCH_TOOLS or is_browser_tool_name(tool_name):
        return False
    return _is_substantive_research_url(_research_direct_url_argument(arguments))


def _research_result_establishes_direct_source(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
) -> bool:
    """Return whether a successful direct read reached substantive source content."""
    if (
        not _is_direct_research_read_call(tool_name, arguments)
        or not result.success
        or _research_result_is_empty(result)
    ):
        return False
    page_text, resolved_url = _research_direct_source_content(arguments, result)
    if (
        not page_text
        or _research_navigation_result_is_metadata_only(page_text)
        or not _is_substantive_research_url(resolved_url)
    ):
        return False
    error_page_markers = (
        "404 not found",
        "404错误",
        "页面不存在",
        "页面丢失",
        "网页失联",
        "access denied",
        "captcha",
        "error page",
    )
    folded = page_text.casefold()
    return not any(marker in folded for marker in error_page_markers)


def _research_direct_source_content(
    arguments: dict[str, Any],
    result: ToolResult,
) -> tuple[str, str]:
    """Extract page text and the resolved URL from one direct-read result."""
    content = result.content if isinstance(result.content, str) else ""
    resolved_url = _research_direct_url_argument(arguments)
    page_url_match = re.search(r"(?im)^-\s*Page URL:\s*(\S+)\s*$", content)
    if page_url_match:
        resolved_url = page_url_match.group(1)
    extract_url_match = re.search(r"(?im)^\[URL\]:\s*(\S+)\s*$", content)
    if extract_url_match:
        resolved_url = extract_url_match.group(1)
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            resolved_url = str(data.get("url") or resolved_url)
            content = "\n".join(
                str(data.get(field) or "") for field in ("title", "content")
            )
    return content.strip(), normalize_search_url(resolved_url)


def _research_navigation_result_is_metadata_only(page_text: str) -> bool:
    """Return whether Playwright returned navigation metadata without page text."""
    return bool(
        re.search(r"(?im)^### Page\s*$", page_text)
        and re.search(r"(?im)^### Snapshot\s*$", page_text)
        and re.search(r"(?im)^- \[Snapshot\]\(", page_text)
    )


def _research_direct_read_key(
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, str] | None:
    """Return the exact URL and backend family for direct-read deduplication."""
    url = _research_direct_url_argument(arguments)
    if not url.startswith(("http://", "https://")):
        return None
    if tool_name in MANAGED_RESEARCH_NAVIGATE_TOOLS | MANAGED_RESEARCH_SNAPSHOT_TOOLS:
        backend = "playwright"
    elif tool_name in GATEWAY_RESEARCH_READ_TOOLS:
        backend = "browser-gateway"
    else:
        backend = tool_name
    return url, backend


def _normalized_source_text(value: object) -> str:
    """Normalize an excerpt and page text for exact provenance containment."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized)


def _research_unread_verified_urls(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
    verified_evidence_urls: set[str],
    direct_evidence_content: dict[str, str],
    search_evidence_content: dict[str, str],
) -> tuple[str, ...]:
    """Return verified ledger URLs unsupported by captured source content."""
    ledger = _research_validation_ledger(
        tool_name,
        arguments,
        workspace_dir,
        artifact_root_dir,
    )
    if ledger is None:
        return ()
    try:
        payload = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    if not isinstance(evidence, list):
        return ()
    trusted = {normalize_search_url(url) for url in verified_evidence_urls}
    unread: list[str] = []
    for row in evidence:
        if (
            not isinstance(row, dict)
            or str(row.get("status", "")).casefold() != "verified"
        ):
            continue
        url = row.get("source_url")
        normalized = normalize_search_url(url)
        excerpt = _normalized_source_text(row.get("evidence_excerpt"))
        source_text = direct_evidence_content.get(
            normalized,
            search_evidence_content.get(normalized, ""),
        )
        if normalized.startswith(("http://", "https://")) and (
            not _is_substantive_research_url(normalized)
            or normalized not in trusted
            or not excerpt
            or excerpt not in source_text
        ):
            unread.append(str(url))
    return tuple(dict.fromkeys(unread))


def _downgrade_unread_verified_evidence(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
    unread_urls: set[str],
) -> int:
    """Atomically downgrade unsupported verified rows after one validator call."""
    if not unread_urls:
        return 0
    ledger = _research_validation_ledger(
        tool_name,
        arguments,
        workspace_dir,
        artifact_root_dir,
    )
    if ledger is None:
        return 0
    try:
        payload = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    if not isinstance(evidence, list):
        return 0
    normalized_unread = {normalize_search_url(url) for url in unread_urls}
    changed = 0
    for row in evidence:
        if (
            not isinstance(row, dict)
            or str(row.get("status", "")).casefold() != "verified"
            or normalize_search_url(row.get("source_url")) not in normalized_unread
        ):
            continue
        row["status"] = "unverified"
        row["unverified_reason"] = (
            "Runtime could not bind this excerpt to captured content for the exact URL."
        )
        if (
            str(row.get("evidence_basis", "")).casefold() == "search_summary"
            and str(row.get("confidence", "")).casefold() == "high"
        ):
            row["confidence"] = "medium"
        changed += 1
    if not changed:
        return 0
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_path = ledger.with_name(f".{ledger.name}.tmp")
    try:
        temp_path.write_text(serialized, encoding="utf-8")
        temp_path.replace(ledger)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    return changed


def _research_handoff_error(
    stage: str | None,
    research_mode: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
    outline_staged_write_id: str | None,
) -> str | None:
    if stage != "outline" or research_mode != "deep":
        return None
    if tool_name in {"request_user_input", "request_user_decision"}:
        return None
    if tool_name == "write_file":
        if _is_canonical_artifact_target(
            arguments.get("path"),
            "outline.json",
            workspace_dir,
            artifact_root_dir,
        ):
            return None
    if tool_name == "staged_file_write":
        action = arguments.get("action")
        if action == "begin" and _is_canonical_artifact_target(
            arguments.get("path"),
            "outline.json",
            workspace_dir,
            artifact_root_dir,
        ):
            return None
        if (
            action in {"append_text", "commit", "abort"}
            and outline_staged_write_id is not None
            and arguments.get("write_id") == outline_staged_write_id
        ):
            return None
    if tool_name == "read_file":
        path = arguments.get("path")
        if isinstance(path, str):
            candidate = Path(path)
            if (
                candidate.suffix.casefold() == ".md"
                and candidate.name.casefold() != "outline.md"
                and "research" in {part.casefold() for part in candidate.parts}
            ):
                return None
    return _RESEARCH_HANDOFF_TOOL_ERROR


def _resolved_artifact_target(
    value: Any,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> tuple[Path | None, Path | None]:
    """Resolve a model-supplied path against the active artifact root."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    root = artifact_scan_root(workspace_dir, artifact_root_dir)
    if root is None:
        return None, None
    root = root.resolve(strict=False)
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
    else:
        workspace = (
            Path(workspace_dir).expanduser().resolve(strict=False)
            if workspace_dir
            else None
        )
        root_from_workspace: Path | None = None
        if workspace is not None:
            try:
                root_from_workspace = root.relative_to(workspace)
            except ValueError:
                pass
        if (
            workspace is not None
            and root_from_workspace is not None
            and candidate.parts[: len(root_from_workspace.parts)]
            == root_from_workspace.parts
        ):
            resolved = (workspace / candidate).resolve(strict=False)
        else:
            resolved = (root / candidate).resolve(strict=False)
    return resolved, root


def _is_canonical_artifact_target(
    value: Any,
    expected_name: str,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> bool:
    """Return whether a tool path resolves to the active artifact-root file."""
    resolved, root = _resolved_artifact_target(
        value,
        workspace_dir,
        artifact_root_dir,
    )
    if root is None:
        return _is_safe_named_path(value, expected_name)
    return resolved == (root / expected_name).resolve(strict=False)


def _research_artifact_target_error(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> str | None:
    """Reject durable research writes outside artifact-root/research/."""
    if stage != "research":
        return None
    candidate: Any = None
    if tool_name == "bash":
        command = arguments.get("command")
        if not isinstance(command, str):
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None
        if not any(Path(token).name in {"mkdir", "mkdir.exe"} for token in tokens):
            return None
        candidate = next(
            (
                token
                for token in tokens
                if Path(token).is_absolute()
                and "research" in {part.casefold() for part in Path(token).parts}
            ),
            None,
        )
    elif tool_name in {"write_file", "append_file", "edit_file"}:
        candidate = arguments.get("path")
    elif tool_name == "staged_file_write" and arguments.get("action") == "begin":
        candidate = arguments.get("path")
    if not isinstance(candidate, str):
        return None
    if "research" not in {part.casefold() for part in Path(candidate).parts}:
        return None
    resolved, root = _resolved_artifact_target(
        candidate,
        workspace_dir,
        artifact_root_dir,
    )
    if resolved is None or root is None:
        return None
    research_root = (root / "research").resolve(strict=False)
    if resolved.is_relative_to(research_root):
        return None
    return (
        f"{_RESEARCH_ARTIFACT_TARGET_TOOL_ERROR} "
        f"actual_path={candidate!r}; expected_path='research/...'; "
        f"artifact_root={str(root)!r}."
    )


def _outline_target_error(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
) -> str | None:
    """Reject outline writes that bypass the active presentation artifact root."""
    if stage != "outline":
        return None
    candidate: Any = None
    if tool_name == "write_file":
        candidate = arguments.get("path")
    elif tool_name == "staged_file_write" and arguments.get("action") == "begin":
        candidate = arguments.get("path")
    if not isinstance(candidate, str) or Path(candidate).name != "outline.json":
        return None
    if _is_canonical_artifact_target(
        candidate,
        "outline.json",
        workspace_dir,
        artifact_root_dir,
    ):
        return None
    _, root = _resolved_artifact_target(
        candidate,
        workspace_dir,
        artifact_root_dir,
    )
    return (
        f"{_OUTLINE_TARGET_TOOL_ERROR} "
        f"actual_path={candidate!r}; expected_path='outline.json'; "
        f"artifact_root={str(root) if root is not None else None!r}."
    )


def _repair_artifact_name(stage: str | None) -> str | None:
    if stage == "outline_repair":
        return "outline.json"
    if stage == "deck_redesign_repair":
        return "deck.redesign.json"
    if stage in {"deck_spec_repair", "content_patch_repair"}:
        return "deck.patch.json"
    return None


def _is_safe_named_path(value: Any, expected_name: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = Path(value)
    return candidate.name == expected_name and ".." not in candidate.parts


def _staged_repair_call_allowed(
    stage: str | None,
    arguments: dict[str, Any],
) -> bool:
    """Allow one transactional repair without opening unrelated file operations."""
    expected_name = _repair_artifact_name(stage)
    if expected_name is None:
        return False
    action = arguments.get("action")
    if action == "begin":
        return _is_safe_named_path(arguments.get("path"), expected_name)
    return action in {"append_text", "commit", "abort"}


def _repair_write_call_allowed(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    expected_name = _repair_artifact_name(stage)
    if expected_name is None:
        return False
    if tool_name == "staged_file_write":
        return _staged_repair_call_allowed(stage, arguments)
    if tool_name == "write_file":
        return _is_safe_named_path(arguments.get("path"), expected_name)
    if tool_name == "append_file":
        return _is_safe_named_path(arguments.get("path"), expected_name)
    return False


def _is_committed_repair_mutation(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
) -> bool:
    """Return whether a successful call committed the expected repair artifact."""
    if not result.success:
        return False
    expected_name = _repair_artifact_name(stage)
    if expected_name is None:
        return False
    if tool_name == "write_file":
        return (
            arguments.get("final", True) is not False
            and _is_safe_named_path(arguments.get("path"), expected_name)
        )
    if tool_name == "append_file":
        return _is_safe_named_path(arguments.get("path"), expected_name)
    return (
        tool_name == "staged_file_write"
        and arguments.get("action") == "commit"
    )


def _scaffold_error(
    tool_name: str,
    arguments: dict[str, Any],
    scaffold_input: dict[str, Any] | None,
) -> str | None:
    if scaffold_input is None:
        return None
    command = arguments.get("command")
    if tool_name != "bash" or not isinstance(command, str):
        return _SCAFFOLD_TOOL_ERROR
    if "\n" in command or "\r" in command:
        return _SCAFFOLD_TOOL_ERROR
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _SCAFFOLD_TOOL_ERROR
    script_indexes = [
        index
        for index, token in enumerate(tokens)
        if Path(token).name == "inspect_deck_contract.js"
    ]
    script_index = script_indexes[0] if script_indexes else None
    if script_index is None or "--outline" not in tokens or "--out" not in tokens:
        return _SCAFFOLD_TOOL_ERROR

    if len(script_indexes) != 1 or script_index < 1:
        return _SCAFFOLD_TOOL_ERROR
    node_token = tokens[script_index - 1]
    if not (
        Path(node_token).name in {"node", "node.exe"}
        or "BOX_AGENT_NODE" in node_token
    ):
        return _SCAFFOLD_TOOL_ERROR
    supplied_script = Path(tokens[script_index])
    if not supplied_script.is_absolute() or supplied_script.resolve() != _INSPECT_SCRIPT:
        return _SCAFFOLD_TOOL_ERROR
    command_prefix = tokens[: script_index - 1]
    if not (
        len(command_prefix) == 3
        and command_prefix[0] == "cd"
        and command_prefix[1]
        and command_prefix[2] == "&&"
    ):
        return _SCAFFOLD_TOOL_ERROR
    inspector_args = tokens[script_index + 1 :]
    if any(
        token in {"&&", "||", ";", "|", "&", ">", ">>", "<", "<<"}
        for token in inspector_args
    ):
        return _SCAFFOLD_SHELL_SUFFIX_TOOL_ERROR
    if tokens.count("--outline") != 1 or tokens.count("--out") != 1:
        return _SCAFFOLD_TOOL_ERROR
    allowed_flags = {"--outline", "--out"}
    if scaffold_input.get("image_generation_policy") == "forbidden_by_user":
        allowed_flags.add("--no-images")
    index = script_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token not in allowed_flags:
            return _SCAFFOLD_TOOL_ERROR
        if token == "--no-images":
            index += 1
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            return _SCAFFOLD_TOOL_ERROR
        index += 2
    if (
        scaffold_input.get("image_generation_policy")
        == "forbidden_by_user"
        and tokens.count("--no-images") != 1
    ):
        return _SCAFFOLD_TOOL_ERROR
    outline_index = tokens.index("--outline") + 1
    out_index = tokens.index("--out") + 1
    if (
        outline_index >= len(tokens)
        or out_index >= len(tokens)
        or Path(tokens[outline_index]).name != "outline.json"
        or Path(tokens[out_index]).name != "deck.json"
    ):
        return _SCAFFOLD_TOOL_ERROR
    return None


def _scaffold_failure_signature(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    scaffold_input: dict[str, Any] | None,
) -> str | None:
    if (
        result.success
        or scaffold_input is None
        or _scaffold_error(tool_name, arguments, scaffold_input)
    ):
        return None
    payload = "\n".join(
        part for part in (result.error, result.content) if isinstance(part, str) and part
    )
    if not payload.strip():
        return "empty-scaffold-failure"
    marker = payload.find("Error:")
    semantic = payload[marker:] if marker >= 0 else payload
    return re.sub(r"\s+", " ", semantic).strip()[:4000]


def _image_generation_error(
    stage: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    image_input: dict[str, Any] | None,
) -> str | None:
    if stage != "images" or image_input is None:
        return None
    entries = image_input.get("entries")
    expected_paths = {
        entry.get("output_path")
        for entry in entries or []
        if isinstance(entry, dict) and isinstance(entry.get("output_path"), str)
    }
    if (
        tool_name == "generate_image"
        and arguments.get("output_path") in expected_paths
        and arguments.get("watermark") is False
    ):
        return None
    return _IMAGE_GENERATION_TOOL_ERROR


def _stage(checkpoint_text: str) -> str | None:
    marker_index = checkpoint_text.find(CHECKPOINT_MARKER)
    if marker_index < 0:
        return None
    stage_text = checkpoint_text[marker_index + len(CHECKPOINT_MARKER) :]
    stage = stage_text.splitlines()[0].strip()
    return stage or None


def _normalized_outline_issue(issue: Any) -> str | None:
    """Collapse volatile page numbers and values into one outline issue class."""
    if not isinstance(issue, str) or not issue.strip():
        return None
    normalized = unicodedata.normalize("NFKC", issue).casefold().strip()
    normalized = re.sub(r"slide-\d+", "slide", normalized)
    normalized = re.sub(r"slides?\.\d+", "slide", normalized)
    normalized = re.sub(r"\b\d+\b", "#", normalized)
    normalized = re.sub(r",\s*got\s+.+$", ", got <value>", normalized)
    return re.sub(r"\s+", " ", normalized)


def _research_result_is_empty(result: ToolResult) -> bool:
    """Return whether a successful research tool call yielded no usable payload."""
    if not result.success:
        return False
    content = (result.model_context or result.content or "").strip()
    if not content:
        return True
    if content.casefold() in {
        "[]",
        "{}",
        "null",
        "no results",
        "no search results",
        "no results found",
    }:
        return True
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return False
    return bool(
        isinstance(payload, dict)
        and "activated" in payload
        and payload.get("activated") == []
    )


def _dotted_python_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_python_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _contains_http_literal(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value.strip().casefold().startswith(("http://", "https://"))
        for child in ast.walk(node)
    )


def _is_network_module(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in _NETWORK_MODULE_PREFIXES
    )


def _execute_code_uses_network(arguments: dict[str, Any]) -> bool:
    """Detect common network-capable Python operations structurally via AST."""
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        folded = code.casefold()
        return any(prefix in folded for prefix in _NETWORK_MODULE_PREFIXES) or bool(
            re.search(r"(?m)^\s*!\s*(?:\S*/)?(?:curl|wget)\b", folded)
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_is_network_module(alias.name) for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_network_module(module):
                return True
            if module in {"http", "urllib"} and any(
                alias.name in {"client", "request"} for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Call):
            call_name = _dotted_python_name(node.func) or ""
            if call_name.rsplit(".", 1)[-1] == "__import__" and node.args:
                imported = node.args[0]
                if (
                    isinstance(imported, ast.Constant)
                    and isinstance(imported.value, str)
                    and _is_network_module(imported.value)
                ):
                    return True
            if _is_network_module(call_name):
                return True
            if (
                call_name.rsplit(".", 1)[-1] in _URL_READER_NAMES
                and _contains_http_literal(node)
            ):
                return True
            if call_name in _PROCESS_CALL_NAMES and (
                _contains_http_literal(node)
                or any(
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and child.value.strip().casefold().rsplit("/", 1)[-1]
                    in {"curl", "wget"}
                    for child in ast.walk(node)
                )
            ):
                return True
    return False


def _image_result_is_unauthorized(result: ToolResult) -> bool:
    """Return whether image generation failed with a deterministic HTTP 401."""
    if result.success:
        return False
    raw_output = result.raw_output
    if isinstance(raw_output, dict) and raw_output.get("status_code") == 401:
        return True
    payload = "\n".join(
        part
        for part in (result.error, result.content, result.model_context)
        if isinstance(part, str) and part
    )
    return re.search(r"(?<!\d)401(?!\d)", payload) is not None


def _tool_failure_signature(result: ToolResult) -> str:
    payload = "\n".join(
        part for part in (result.error, result.content) if isinstance(part, str) and part
    )
    controlled_code = re.search(r"\b(CONTROLLED_PRESENTATION_[A-Z0-9_]+):", payload)
    if controlled_code is not None:
        return controlled_code.group(1)
    return re.sub(r"\s+", " ", payload).strip()[:4000] or "empty-tool-failure"


@dataclass(slots=True)
class ControlledPresentationPolicy:
    """Stateful policy for one controlled-presentation agent run."""

    workspace_dir: str | None
    artifact_root_dir: str | Path | None
    research_mode: str | None = None
    research_round_limit: int = RESEARCH_ROUND_LIMIT
    image_generation_policy: str | None = None
    available_tool_names: frozenset[str] | None = None
    stage: str | None = None
    scaffold_input: dict[str, Any] | None = None
    image_input: dict[str, Any] | None = None
    has_patch_input: bool = False
    has_scaffold_input: bool = False
    has_image_input: bool = False
    has_repair_input: bool = False
    research_revalidation: dict[str, Any] | None = None
    repair_stalled: bool = False
    image_auth_blocked: bool = False
    image_policy_rebase_policy: str | None = None
    research_search_exhausted: bool = False
    apply_patch_repair_allowed: bool = False
    apply_patch_repair_paths: tuple[str, ...] = ()
    _last_checkpoint_text: str | None = None
    _resume_checkpoint: WorkflowPauseCheckpoint | None = None
    _last_step_failure_signature: str | None = None
    _pending_write_path: str | None = None
    _pending_write_next_chunk_index: int | None = None
    _pending_write_size_bytes: int = 0
    _dispatched_deterministic_action_ids: set[str] = field(default_factory=set)

    _step_failure_streak: int = 0
    _repair_failure_stage: str | None = None
    _repair_failure_signature: str | None = None
    _repair_failure_streak: int = 0
    _policy_rejection_stage: str | None = None
    _policy_rejection_signature: str | None = None
    _policy_rejection_streak: int = 0
    _policy_rejection_decision_id: int | str | None = None
    _active_tool_decision_id: int | str | None = None
    _outline_staged_write_id: str | None = None
    _content_patch_staged_write_id: str | None = None
    _apply_patch_staged_write_id: str | None = None
    _research_tool_attempts: int = 0
    _research_successful_attempts: int = 0
    _research_failed_attempts: int = 0
    _research_empty_attempts: int = 0
    _research_discovery_attempts: int = 0
    _research_successful_discovery_attempts: int = 0
    _research_failed_discovery_attempts: int = 0
    _research_empty_discovery_attempts: int = 0
    _research_direct_read_attempts: int = 0
    _research_successful_direct_read_attempts: int = 0
    _research_consecutive_unproductive_direct_reads: int = 0
    _research_failed_validation_attempts: int = 0
    _research_calls_since_checkpoint: int = 0
    _research_rounds_without_handoff: int = 0
    _research_json_reads_since_mutation: set[str] = field(default_factory=set)
    _research_browser_connector_unavailable: bool = False
    _research_direct_read_keys: set[tuple[str, str]] = field(default_factory=set)
    _research_direct_read_tool_names: set[str] = field(default_factory=set)
    _research_direct_source_text: dict[str, str] = field(default_factory=dict)
    _research_search_source_text: dict[str, str] = field(default_factory=dict)
    _research_last_evidence_urls: set[str] = field(default_factory=set)
    _pending_unread_evidence_urls: set[str] = field(default_factory=set)
    _research_pending_playwright_url: str | None = None
    _research_last_direct_source_url: str | None = None
    _successful_mutation_since_checkpoint: bool = False
    _no_progress_mutation_streak: int = 0
    _previous_outline_issue_classes: frozenset[str] = frozenset()
    _previous_outline_issue_count: int | None = None

    kind: ClassVar[str] = WORKFLOW_KIND
    checkpoint_injection_id: ClassVar[str] = CHECKPOINT_MARKER
    evidence_read_batch_size: ClassVar[int] = RESEARCH_READ_BATCH_SIZE
    evidence_read_limit: ClassVar[int] = RESEARCH_DIRECT_READ_LIMIT

    def __post_init__(self) -> None:
        """Freeze configured tool availability."""
        if self.available_tool_names is not None:
            self.available_tool_names = frozenset(self.available_tool_names)

    def hidden_tool_names(self) -> frozenset[str]:
        """Return tools that cannot contribute to the current deck stage."""

        hidden = {
            "create_scheduled_task",
            "mcp_config",
            "obsidian_create_note",
            "obsidian_update_note",
            "obsidian_daily_note",
            "search",
            "search_task",
        }
        if self.stage not in {None, "research"}:
            hidden.update(
                {
                    "tool_search",
                    "web_search",
                    "managed_browser_navigate",
                    "managed_browser_navigate_back",
                    "managed_browser_snapshot",
                }
            )
        return frozenset(hidden)

    def llm_call_kind(self) -> str:
        """Expose the current stage for gateway routing and latency tracing."""

        stage = (self.stage or "bootstrap").strip().lower()
        normalized = re.sub(r"[^a-z0-9_]+", "_", stage).strip("_")
        return f"presentation_{normalized or 'bootstrap'}"

    @property
    def _research_direct_read_complete(self) -> bool:
        return bool(
            self._research_direct_read_attempts >= self.evidence_read_limit
            or self._research_consecutive_unproductive_direct_reads
            >= RESEARCH_UNPRODUCTIVE_DIRECT_READ_LIMIT
        )

    @property
    def _research_discovery_exhausted(self) -> bool:
        return self._research_discovery_attempts >= RESEARCH_DISCOVERY_ATTEMPT_LIMIT

    @property
    def _research_discovery_unavailable(self) -> bool:
        unavailable = (
            self._research_failed_discovery_attempts
            + self._research_empty_discovery_attempts
        )
        return bool(
            self._research_discovery_attempts > 0
            and unavailable == self._research_discovery_attempts
        )

    def attach_resume_checkpoint(
        self,
        checkpoint: WorkflowPauseCheckpoint,
    ) -> None:
        """Attach validated durable metadata loaded by the trusted registry."""
        self._resume_checkpoint = checkpoint

    def begin_tool_decision(self, decision_id: int | str) -> None:
        """Identify one model decision whose sibling tool calls share a fuse count."""
        self._active_tool_decision_id = decision_id

    def _pending_write_checkpoint(self) -> dict[str, int | str] | None:
        if (
            self._pending_write_path is None
            or self._pending_write_next_chunk_index is None
        ):
            return None
        return {
            "path": self._pending_write_path,
            "next_chunk_index": self._pending_write_next_chunk_index,
            "size_bytes": self._pending_write_size_bytes,
        }

    def _expected_pending_write_path(self) -> str | None:
        """Return the canonical target for a chunkable write in this stage."""
        if self.stage == "outline":
            return "outline.json"
        if self.stage == "content_patch":
            return "deck.patch.json"
        repair_path = _repair_artifact_name(self.stage)
        if repair_path is not None:
            return repair_path
        if self.stage == "apply_patch" and self.apply_patch_repair_allowed:
            return "deck.patch.json"
        return None

    def _pending_write_tool_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        pending_path = self._pending_write_path
        next_chunk_index = self._pending_write_next_chunk_index
        if pending_path is None or next_chunk_index is None:
            return None
        valid_continuation = (
            tool_name == "write_file"
            and _is_canonical_artifact_target(
                arguments.get("path"),
                pending_path,
                self.workspace_dir,
                self.artifact_root_dir,
            )
            and arguments.get("chunk_index") == next_chunk_index
        )
        if valid_continuation:
            return None
        return _PENDING_WRITE_TOOL_ERROR.format(
            path=pending_path,
            next_chunk_index=next_chunk_index,
        )

    def _clear_pending_write(self) -> None:
        self._pending_write_path = None
        self._pending_write_next_chunk_index = None
        self._pending_write_size_bytes = 0

    def _record_pending_write_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> None:
        if tool_name != "write_file":
            return
        raw_output = result.raw_output
        if not isinstance(raw_output, dict):
            return
        output_type = raw_output.get("type")
        transaction_state = raw_output.get("transaction_state")
        if transaction_state is None:
            if output_type == "write_file_transaction_discarded":
                transaction_state = "discarded"
            elif output_type == "artifact" and result.success:
                transaction_state = "committed"
            elif (
                output_type == "write_file_chunk"
                and result.success
                and raw_output.get("final") is False
            ):
                transaction_state = "active"
        if transaction_state in {"committed", "discarded", "none"}:
            if (
                self._pending_write_path is not None
                and _is_canonical_artifact_target(
                    raw_output.get("path"),
                    self._pending_write_path,
                    self.workspace_dir,
                    self.artifact_root_dir,
                )
            ):
                self._clear_pending_write()
            return
        if transaction_state != "active":
            return
        expected_path = self._expected_pending_write_path()
        next_chunk_index = raw_output.get("next_chunk_index")
        size_bytes = raw_output.get("size_bytes")
        if (
            expected_path is None
            or not _is_canonical_artifact_target(
                arguments.get("path"),
                expected_path,
                self.workspace_dir,
                self.artifact_root_dir,
            )
            or not _is_canonical_artifact_target(
                raw_output.get("path"),
                expected_path,
                self.workspace_dir,
                self.artifact_root_dir,
            )
            or not isinstance(next_chunk_index, int)
            or isinstance(next_chunk_index, bool)
            or next_chunk_index <= 0
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            return
        self._pending_write_path = expected_path
        self._pending_write_next_chunk_index = next_chunk_index
        self._pending_write_size_bytes = size_bytes

    def record_tool_cleanup(
        self,
        tool_name: str,
        records: list[dict[str, Any]],
    ) -> None:
        """Synchronize policy state after runtime-owned tool cleanup."""
        for raw_output in records:
            self._record_pending_write_result(
                tool_name,
                {"path": raw_output.get("path")},
                ToolResult(
                    success=False,
                    error="The runtime discarded an incomplete tool transaction.",
                    raw_output=raw_output,
                ),
            )

    def build_checkpoint(self) -> str | None:
        """Derive the current presentation stage from persisted artifacts."""
        if self.image_auth_blocked:
            return _image_auth_blocked_checkpoint()
        if self._research_calls_since_checkpoint:
            self._research_rounds_without_handoff += 1
            self._research_calls_since_checkpoint = 0
        round_limit_reached = (
            self._research_rounds_without_handoff >= self.research_round_limit
        )
        unavailable = self._research_failed_attempts + self._research_empty_attempts
        direct_source_verification_unavailable = (
            self._research_direct_read_complete
            and self._research_successful_direct_read_attempts == 0
        )
        repeated_research_validation_failure = (
            self._research_failed_validation_attempts
            >= _REPEATED_EXECUTION_FAILURE_LIMIT
        )
        repeated_research_progress_rejection = (
            self._policy_rejection_stage == "research"
            and self._policy_rejection_streak >= _REPEATED_EXECUTION_FAILURE_LIMIT
        )
        research_fallback_allowed = (
            round_limit_reached
            and self._research_tool_attempts > 0
            and (
                unavailable == self._research_tool_attempts
                or direct_source_verification_unavailable
                or repeated_research_validation_failure
                or repeated_research_progress_rejection
            )
        )
        fallback_allowed = (
            self._research_discovery_exhausted or research_fallback_allowed
        )
        self.research_search_exhausted = round_limit_reached and not fallback_allowed
        attempt_summary = {
            "rounds": self._research_rounds_without_handoff,
            "calls": self._research_tool_attempts,
            "successful": self._research_successful_attempts,
            "failed": self._research_failed_attempts,
            "empty": self._research_empty_attempts,
            "direct_reads": self._research_direct_read_attempts,
            "verified_pages": self._research_successful_direct_read_attempts,
            "consecutive_unproductive_reads": (
                self._research_consecutive_unproductive_direct_reads
            ),
        }
        fallback_reason = None
        if fallback_allowed:
            fallback_reason = (
                (
                    "research_tools_unavailable_after_discovery"
                    if self._research_discovery_unavailable
                    else "research_discovery_limit_reached"
                )
                if self._research_discovery_exhausted
                else (
                    "research_artifacts_incomplete_or_validation_failed"
                    if repeated_research_validation_failure
                    else (
                        "research_progress_stalled_after_bounded_search"
                        if repeated_research_progress_rejection
                        else (
                            "direct_source_verification_unavailable"
                            if direct_source_verification_unavailable
                            else (
                                "research_sources_unavailable"
                                if unavailable == self._research_tool_attempts
                                else (
                                    "research_round_limit_reached_without_validated_report"
                                )
                            )
                        )
                    )
                )
            )
        checkpoint_text = build_checkpoint_text(
            self.workspace_dir,
            self.research_mode,
            artifact_root_dir=self.artifact_root_dir,
            image_generation_policy=self.image_generation_policy,
            research_fallback_allowed=fallback_allowed,
            research_fallback_reason=fallback_reason,
            research_attempt_summary=attempt_summary,
            research_search_exhausted=self.research_search_exhausted,
            direct_research_read_complete=self._research_direct_read_complete,
            direct_research_read_available=(
                self.available_tool_names is None
                or "tool_search" in self.available_tool_names
                or bool(self.available_tool_names & DIRECT_RESEARCH_READ_TOOLS)
            ),
            pending_write=self._pending_write_checkpoint(),
        )
        research_input = (
            _checkpoint_json(checkpoint_text, "RESEARCH_INPUT")
            if checkpoint_text is not None
            else None
        )
        if research_input and research_input.get("fallback") is True:
            self._persist_research_fallback_status(research_input)
        if checkpoint_text is not None and self._resume_checkpoint is not None:
            checkpoint_text = (
                f"{checkpoint_resume_instruction(self._resume_checkpoint)}\n\n"
                f"{checkpoint_text}"
            )
            self._resume_checkpoint = None
        return checkpoint_text

    def _persist_research_fallback_status(
        self,
        research_input: dict[str, Any],
    ) -> None:
        """Persist why PPT generation continued without a validated report."""
        root = artifact_scan_root(self.workspace_dir, self.artifact_root_dir)
        if root is None:
            return
        status_path = root / "research" / "qa" / "research_status.json"
        payload = {
            "schema_version": 1,
            "workflow": WORKFLOW_KIND,
            "research_mode": self.research_mode,
            "status": "fallback",
            "report_available": False,
            "generation_continues": True,
            "continued_to": "outline",
            "reason": research_input.get("fallback_reason"),
            "message": research_input.get("fallback_message"),
            "attempt_summary": research_input.get("attempt_summary", {}),
            "presentation_handoff": {
                "schema_version": 1,
                "delivery_mode": "framework",
                "verified_facts": [],
                "gaps": [
                    str(
                        research_input.get("fallback_message")
                        or research_input.get("fallback_reason")
                        or "No validated public research facts are available."
                    )
                ],
                "quality_summary": {
                    "quality_ok": False,
                    "actual_dimensions": None,
                    "recommended_dimensions": None,
                },
                "context_files": [],
            },
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        try:
            if status_path.is_file():
                if status_path.read_text(encoding="utf-8") == serialized:
                    return
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            _log.warning(
                "controlled_presentation/research_status_write_failed "
                "path=%s error=%s",
                status_path,
                exc,
            )

    def _persist_image_auth_blocked(self) -> str | None:
        """Persist image authorization failure and return the manifest mode."""
        root = artifact_scan_root(self.workspace_dir, self.artifact_root_dir)
        if root is None or not root.is_dir():
            return None
        manifests = list(root.rglob("assets/generated/manifest.json"))
        if not manifests:
            return None
        try:
            manifest_path = max(
                manifests,
                key=lambda path: path.stat().st_mtime_ns,
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            mode = payload.get("mode")
            manifest_mode = mode if isinstance(mode, str) else None
            image_service = {
                "status": "blocked",
                "reason": "authorization_401",
            }
            if payload.get("image_service") == image_service:
                return manifest_mode
            payload["image_service"] = image_service
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ) + "\n"
            temp_path = manifest_path.with_name(
                f".{manifest_path.name}.tmp"
            )
            temp_path.write_text(serialized, encoding="utf-8")
            temp_path.replace(manifest_path)
            return manifest_mode
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning(
                "controlled_presentation/image_auth_state_write_failed "
                "error=%s",
                exc,
            )
            return None

    def update_checkpoint(
        self,
        checkpoint_text: str,
    ) -> WorkflowCheckpointUpdate:
        """Parse a fresh filesystem checkpoint and update policy state."""
        candidate_changed = checkpoint_text != self._last_checkpoint_text
        candidate_stage = _stage(checkpoint_text)
        if self._successful_mutation_since_checkpoint:
            # A committed repair is meaningful progress only when it leaves the
            # repair state machine. Comparing the full checkpoint text lets volatile
            # details such as a JSON parse column reset the fuse while the artifact
            # remains invalid or merely switches to another repair stage.
            if candidate_stage not in _REPAIR_STAGES:
                self._no_progress_mutation_streak = 0
            else:
                self._no_progress_mutation_streak += 1
            self._successful_mutation_since_checkpoint = False
            if self._no_progress_mutation_streak >= 2:
                self.repair_stalled = True
                _log.warning(
                    "controlled_presentation/repair_stalled "
                    "successful_mutations_without_progress=%d",
                    self._no_progress_mutation_streak,
                )
        repair_input = _checkpoint_json(checkpoint_text, "REPAIR_INPUT")
        if candidate_changed and candidate_stage == "outline_repair":
            normalized_issues = tuple(
                normalized
                for issue in (repair_input or {}).get("issues", [])
                if (normalized := _normalized_outline_issue(issue)) is not None
            )
            issue_classes = frozenset(normalized_issues)
            issue_count = len(normalized_issues)
            issue_count_improved = (
                self._previous_outline_issue_count is not None
                and issue_count < self._previous_outline_issue_count
            )
            recurring = (
                frozenset()
                if issue_count_improved
                else issue_classes & self._previous_outline_issue_classes
            )
            if recurring:
                self.repair_stalled = True
                _log.warning(
                    "controlled_presentation/repair_stalled "
                    "stage=outline_repair recurring_issue_classes=%s",
                    sorted(recurring),
                )
            self._previous_outline_issue_classes = issue_classes
            self._previous_outline_issue_count = issue_count
        elif candidate_stage not in {"outline", "outline_qa", "outline_repair"}:
            self._previous_outline_issue_classes = frozenset()
            self._previous_outline_issue_count = None
        if self.repair_stalled:
            checkpoint_text = _repair_stalled_checkpoint()
        next_stage = _stage(checkpoint_text)
        if next_stage != self._repair_failure_stage:
            self._repair_failure_stage = (
                next_stage if next_stage in _REPAIR_STAGES else None
            )
            self._repair_failure_signature = None
            self._repair_failure_streak = 0
        if next_stage != self._policy_rejection_stage:
            self._policy_rejection_stage = (
                next_stage if next_stage in _POLICY_REJECTION_STAGES else None
            )
            self._policy_rejection_signature = None
            self._policy_rejection_streak = 0
            self._policy_rejection_decision_id = None
        if next_stage != "outline":
            self._outline_staged_write_id = None
        if next_stage != "content_patch_repair":
            self._content_patch_staged_write_id = None
        if next_stage != "apply_patch":
            self._apply_patch_staged_write_id = None
        if next_stage == "image_policy_rebase":
            if "--policy retry" in checkpoint_text:
                self.image_policy_rebase_policy = "retry"
            elif "--policy unavailable" in checkpoint_text:
                self.image_policy_rebase_policy = "unavailable"
            elif "--policy forbidden" in checkpoint_text:
                self.image_policy_rebase_policy = "forbidden"
            else:
                self.image_policy_rebase_policy = None
        else:
            self.image_policy_rebase_policy = None
        self.stage = next_stage
        self.has_patch_input = "\nPATCH_INPUT=" in checkpoint_text
        self.has_scaffold_input = "\nSCAFFOLD_INPUT=" in checkpoint_text
        self.scaffold_input = _checkpoint_json(checkpoint_text, "SCAFFOLD_INPUT")
        self.has_image_input = "\nIMAGE_INPUT=" in checkpoint_text
        self.image_input = _checkpoint_json(checkpoint_text, "IMAGE_INPUT")
        self.has_repair_input = "\nREPAIR_INPUT=" in checkpoint_text
        research_input = _checkpoint_json(checkpoint_text, "RESEARCH_INPUT")
        raw_revalidation = (research_input or {}).get("revalidation")
        self.research_revalidation = (
            raw_revalidation if isinstance(raw_revalidation, dict) else None
        )

        changed = checkpoint_text != self._last_checkpoint_text
        recovered_urls = (
            _research_handoff_urls(
                checkpoint_text,
                self.workspace_dir,
                self.artifact_root_dir,
            )
            if changed
            else set()
        )
        recovered_urls.update(self._research_search_source_text)
        if changed:
            self._last_checkpoint_text = checkpoint_text
        return WorkflowCheckpointUpdate(
            text=checkpoint_text,
            changed=changed,
            recovered_evidence_urls=frozenset(recovered_urls),
        )

    def _runtime_bash_action(
        self,
        action_name: str,
        command: str,
        artifact_paths: tuple[Path, ...],
    ) -> WorkflowAction | None:
        artifact_root = artifact_scan_root(
            self.workspace_dir,
            self.artifact_root_dir,
        )
        if artifact_root is None or "\n" in command or "\r" in command:
            return None
        signature = [command]
        for path in artifact_paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            signature.append(f"{path}:{stat.st_size}:{stat.st_mtime_ns}")
        action_digest = hashlib.sha256(
            "\n".join(signature).encode("utf-8")
        ).hexdigest()[:16]
        action_id = f"controlled-presentation:{action_name}:{action_digest}"
        if action_id in self._dispatched_deterministic_action_ids:
            return None
        self._dispatched_deterministic_action_ids.add(action_id)
        return WorkflowAction(
            action_id=action_id,
            capability=f"controlled_presentation.{action_name}",
            tool_name="bash",
            arguments={
                "command": f"cd {shlex.quote(str(artifact_root))} && {command}",
                "timeout": 120,
            },
        )

    def next_deterministic_action(self) -> WorkflowAction | None:
        """Construct the next trusted workflow command from checkpoint state."""
        artifact_root = artifact_scan_root(
            self.workspace_dir,
            self.artifact_root_dir,
        )
        if self.stage == "research" and self.research_revalidation is not None:
            expected_command = self.research_revalidation.get("command")
            if (
                artifact_root is not None
                and isinstance(expected_command, str)
                and expected_command.strip()
                and "\n" not in expected_command
                and "\r" not in expected_command
            ):
                signature_paths: list[Path] = []
                stale_dependencies = self.research_revalidation.get(
                    "stale_dependencies"
                )
                if isinstance(stale_dependencies, list):
                    research_root = artifact_root / "research"
                    for relative_path in sorted(
                        path
                        for path in stale_dependencies
                        if isinstance(path, str) and path
                    ):
                        signature_paths.append(research_root / relative_path)
                return self._runtime_bash_action(
                    "research_validate",
                    expected_command,
                    tuple(signature_paths),
                )

        if artifact_root is not None and self.stage == "outline_qa":
            outline_path = artifact_root / "outline.json"
            report_path = artifact_root / "qa" / "outline_check.json"
            research_handoff = _latest_valid_outline_research_handoff(
                artifact_root
            )
            research_argument = (
                f" --research-handoff {shlex.quote(str(research_handoff))}"
                if self.research_mode == "deep" and research_handoff is not None
                else ""
            )
            command = (
                f"${{BOX_AGENT_NODE:-node}} {shlex.quote(str(_VALIDATE_OUTLINE_SCRIPT))} "
                f"{shlex.quote(str(outline_path))}{research_argument} "
                f"--report {shlex.quote(str(report_path))}"
            )
            return self._runtime_bash_action(
                "outline_validate",
                command,
                (
                    outline_path,
                    *((research_handoff,) if research_handoff is not None else ()),
                ),
            )

        if artifact_root is not None and self.stage == "image_status_sync":
            manifest_path = artifact_root / "assets" / "generated" / "manifest.json"
            command = (
                f"${{BOX_AGENT_NODE:-node}} {shlex.quote(str(_SYNC_IMAGE_STATUS_SCRIPT))} "
                f"{shlex.quote(str(manifest_path))}"
            )
            return self._runtime_bash_action(
                "image_status_sync",
                command,
                (manifest_path,),
            )

        if artifact_root is not None and self.stage == "image_policy_rebase":
            deck_path = artifact_root / "deck.json"
            manifest_path = artifact_root / "assets" / "generated" / "manifest.json"
            if self.image_policy_rebase_policy is not None:
                command = (
                    f"${{BOX_AGENT_NODE:-node}} "
                    f"{shlex.quote(str(_REBASE_IMAGE_POLICY_SCRIPT))} "
                    f"{shlex.quote(str(deck_path))} --manifest "
                    f"{shlex.quote(str(manifest_path))} --policy "
                    f"{self.image_policy_rebase_policy}"
                )
                return self._runtime_bash_action(
                    "image_policy_rebase",
                    command,
                    (deck_path, manifest_path),
                )

        if (
            artifact_root is not None
            and self.stage == "apply_patch"
            and not self.apply_patch_repair_allowed
        ):
            deck_path = artifact_root / "deck.json"
            patch_path = artifact_root / "deck.patch.json"
            command = (
                f"${{BOX_AGENT_NODE:-node}} {shlex.quote(str(_APPLY_PATCH_SCRIPT))} "
                f"{shlex.quote(str(deck_path))} {shlex.quote(str(patch_path))}"
            )
            return self._runtime_bash_action(
                "apply_patch",
                command,
                (deck_path, patch_path),
            )

        if artifact_root is not None and self.stage == "apply_redesign":
            deck_path = artifact_root / "deck.json"
            redesign_path = artifact_root / "deck.redesign.json"
            command = (
                f"${{BOX_AGENT_NODE:-node}} {shlex.quote(str(_APPLY_REDESIGN_SCRIPT))} "
                f"{shlex.quote(str(deck_path))} {shlex.quote(str(redesign_path))}"
            )
            return self._runtime_bash_action(
                "apply_redesign",
                command,
                (deck_path, redesign_path),
            )

        if artifact_root is not None and self.stage == "finalize":
            deck_path = artifact_root / "deck.json"
            manifest_path = artifact_root / "assets" / "generated" / "manifest.json"
            html_path = artifact_root / "index.html"
            command = (
                f"${{BOX_AGENT_NODE:-node}} {shlex.quote(str(_FINALIZER_SCRIPT))} "
                f"{shlex.quote(str(deck_path))} --out {shlex.quote(str(html_path))}"
            )
            return self._runtime_bash_action(
                "finalize",
                command,
                (deck_path, manifest_path),
            )

        if (
            self.repair_stalled
            or self.stage != "scaffold"
            or not self.has_scaffold_input
            or self.scaffold_input is None
        ):
            return None
        artifact_root = artifact_scan_root(
            self.workspace_dir,
            self.artifact_root_dir,
        )
        if artifact_root is None:
            return None
        outline_path = artifact_root / "outline.json"
        if not outline_path.is_file() or (artifact_root / "deck.json").exists():
            return None
        try:
            outline_digest = hashlib.sha256(outline_path.read_bytes()).hexdigest()[:16]
        except OSError:
            return None
        action_id = f"controlled-presentation:scaffold:{outline_digest}"
        if action_id in self._dispatched_deterministic_action_ids:
            return None

        command_parts = [
            "cd",
            shlex.quote(str(artifact_root)),
            "&&",
            "${BOX_AGENT_NODE:-node}",
            shlex.quote(str(_INSPECT_SCRIPT)),
            "--outline",
            "outline.json",
            "--out",
            "deck.json",
        ]
        if (
            self.scaffold_input.get("image_generation_policy")
            == "forbidden_by_user"
        ):
            command_parts.append("--no-images")

        self._dispatched_deterministic_action_ids.add(action_id)
        return WorkflowAction(
            action_id=action_id,
            capability="controlled_presentation.scaffold",
            tool_name="bash",
            arguments={
                "command": " ".join(command_parts),
                "timeout": 120,
            },
        )

    def plan_scope_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """Validate a structured plan before host approval handling."""
        return _plan_scope_error(self.stage, tool_name, arguments)

    def _research_json_read_key(self, arguments: dict[str, Any]) -> str | None:
        path_value = arguments.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            return None
        artifact_root = artifact_scan_root(
            self.workspace_dir,
            self.artifact_root_dir,
        )
        workspace_root = (
            Path(self.workspace_dir).expanduser().resolve()
            if self.workspace_dir
            else None
        )
        research_roots = [
            root / "research"
            for root in (artifact_root, workspace_root)
            if root is not None
        ]
        if not research_roots:
            return None
        path = Path(path_value)
        candidates = (
            [path]
            if path.is_absolute()
            else [root / path for root in (artifact_root, workspace_root) if root]
        )
        # Prefer the root that actually owns the file. Deep-research staging may
        # live at {workspace}/research while final artifacts live under output/.
        ordered_candidates = sorted(
            candidates,
            key=lambda candidate: not candidate.is_file(),
        )
        for candidate in ordered_candidates:
            resolved = candidate.expanduser().resolve(strict=False)
            for research_root in research_roots:
                try:
                    relative = resolved.relative_to(
                        research_root.resolve(strict=False)
                    )
                except ValueError:
                    continue
                if relative.suffix.casefold() == ".json":
                    return relative.as_posix()
        return None

    def _research_local_read_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        if self.stage != "research":
            return None
        if (
            self._research_browser_connector_unavailable
            and tool_name in GATEWAY_RESEARCH_READ_TOOLS
        ):
            return _RESEARCH_BROWSER_CONNECTOR_UNAVAILABLE_TOOL_ERROR
        if not self.research_search_exhausted:
            return None
        if (
            is_browser_tool_name(tool_name)
            and tool_name not in DIRECT_RESEARCH_READ_TOOLS
        ):
            return _RESEARCH_BROWSER_REINSPECTION_COMPLETE_TOOL_ERROR
        if tool_name == "search_files":
            return _RESEARCH_LOCAL_READ_COMPLETE_TOOL_ERROR
        if tool_name == "bash":
            if _is_research_validation_call(tool_name, arguments):
                return None
            return _RESEARCH_LOCAL_READ_COMPLETE_TOOL_ERROR
        if tool_name != "read_file":
            return None
        key = self._research_json_read_key(arguments)
        if key is None or key in self._research_json_reads_since_mutation:
            return _RESEARCH_LOCAL_READ_COMPLETE_TOOL_ERROR
        return None

    def _research_revalidation_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        revalidation = self.research_revalidation
        if self.stage != "research" or revalidation is None:
            return None
        expected = revalidation.get("command")
        command = arguments.get("command")
        if (
            tool_name != "bash"
            or not isinstance(expected, str)
            or not isinstance(command, str)
        ):
            return _RESEARCH_REVALIDATION_REQUIRED_TOOL_ERROR
        if "\n" in command or "\r" in command:
            return _RESEARCH_REVALIDATION_REQUIRED_TOOL_ERROR
        try:
            actual_tokens = shlex.split(command)
            expected_tokens = shlex.split(expected)
        except ValueError:
            return _RESEARCH_REVALIDATION_REQUIRED_TOOL_ERROR
        if actual_tokens == expected_tokens:
            return None
        if len(actual_tokens) != len(expected_tokens) + 3:
            return _RESEARCH_REVALIDATION_REQUIRED_TOOL_ERROR
        if actual_tokens[0] != "cd" or actual_tokens[2] != "&&":
            return _RESEARCH_REVALIDATION_REQUIRED_TOOL_ERROR
        root = artifact_scan_root(self.workspace_dir, self.artifact_root_dir)
        if root is None:
            return _RESEARCH_REVALIDATION_REQUIRED_TOOL_ERROR
        supplied_root = Path(actual_tokens[1]).expanduser().resolve(strict=False)
        if supplied_root != root.resolve(strict=False):
            return _RESEARCH_REVALIDATION_REQUIRED_TOOL_ERROR
        if actual_tokens[3:] != expected_tokens:
            return _RESEARCH_REVALIDATION_REQUIRED_TOOL_ERROR
        return None

    def tool_call_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        verified_evidence_urls: set[str],
        parallel: bool = False,
    ) -> str | None:
        """Return a blocking error for a workflow-invalid tool call."""
        direct_read_call = (
            self.stage == "research"
            and _is_direct_research_read_call(tool_name, arguments)
        )
        if direct_read_call:
            # Remember dynamically selected MCP readers before the kernel reserves
            # budgets. Trust is still established only after a successful result
            # contains substantive page text bound to the requested URL.
            self._research_direct_read_tool_names.add(tool_name)
        handoff_error = _research_handoff_error(
            self.stage,
            self.research_mode,
            tool_name,
            arguments,
            self.workspace_dir,
            self.artifact_root_dir,
            self._outline_staged_write_id,
        )
        outline_target_error = _outline_target_error(
            self.stage,
            tool_name,
            arguments,
            self.workspace_dir,
            self.artifact_root_dir,
        )
        research_artifact_target_error = _research_artifact_target_error(
            self.stage,
            tool_name,
            arguments,
            self.workspace_dir,
            self.artifact_root_dir,
        )
        if self.stage == "repair_stalled":
            return _REPAIR_STALLED_TOOL_ERROR
        if self.stage == "image_auth_blocked":
            return _IMAGE_AUTH_BLOCKED_TOOL_ERROR
        if outline_target_error is not None:
            return outline_target_error
        if research_artifact_target_error is not None:
            return research_artifact_target_error
        pending_write_error = self._pending_write_tool_error(tool_name, arguments)
        if pending_write_error is not None:
            return pending_write_error
        if handoff_error is not None:
            return handoff_error
        research_revalidation_error = self._research_revalidation_error(
            tool_name,
            arguments,
        )
        if research_revalidation_error is not None:
            return research_revalidation_error
        if (
            self.stage == "research"
            and tool_name == "execute_code"
            and _execute_code_uses_network(arguments)
        ):
            return _RESEARCH_EXECUTE_CODE_NETWORK_TOOL_ERROR
        if (
            self.stage == "research"
            and tool_name in MANAGED_RESEARCH_NAVIGATE_TOOLS
            and self._research_pending_playwright_url is not None
        ):
            return _RESEARCH_SNAPSHOT_REQUIRED_TOOL_ERROR
        if (
            self.stage == "research"
            and tool_name in MANAGED_RESEARCH_SNAPSHOT_TOOLS
            and self._research_pending_playwright_url is None
        ):
            return _RESEARCH_SNAPSHOT_NAVIGATION_REQUIRED_TOOL_ERROR
        if (
            self.stage == "research"
            and direct_read_call
            and self._research_direct_read_complete
        ):
            return _RESEARCH_DIRECT_READ_COMPLETE_TOOL_ERROR
        research_local_read_error = self._research_local_read_error(
            tool_name,
            arguments,
        )
        if research_local_read_error is not None:
            return research_local_read_error
        content_patch_repair_error = _content_patch_repair_error(
            self.stage,
            tool_name,
            arguments,
            self._content_patch_staged_write_id,
        )
        if content_patch_repair_error is not None:
            return content_patch_repair_error
        image_policy_rebase_error = _image_policy_rebase_error(
            self.stage,
            self.image_policy_rebase_policy,
            tool_name,
            arguments,
        )
        if image_policy_rebase_error is not None:
            return image_policy_rebase_error
        if (
            self.stage == "research"
            and _is_research_validation_call(tool_name, arguments)
        ):
            unread_urls = _research_unread_verified_urls(
                tool_name,
                arguments,
                self.workspace_dir,
                self.artifact_root_dir,
                verified_evidence_urls,
                self._research_direct_source_text,
                self._research_search_source_text,
            )
            if unread_urls:
                self._pending_unread_evidence_urls.update(unread_urls)
        if (
            self.stage == "research"
            and self._research_discovery_exhausted
            and tool_name == "tool_search"
        ):
            return _RESEARCH_SEARCH_COMPLETE_TOOL_ERROR
        if (
            self.stage == "research"
            and self.research_search_exhausted
            and tool_name == "web_search"
        ):
            return _RESEARCH_SEARCH_COMPLETE_TOOL_ERROR
        if (
            self.stage == "research"
            and self.research_search_exhausted
            and direct_read_call
        ):
            if tool_name in MANAGED_RESEARCH_SNAPSHOT_TOOLS:
                return None
            direct_read_key = _research_direct_read_key(tool_name, arguments)
            if direct_read_key is None or not _is_substantive_research_url(
                direct_read_key[0]
            ):
                return _RESEARCH_EXACT_SOURCE_URL_REQUIRED_TOOL_ERROR
            if direct_read_key in self._research_direct_read_keys:
                return _RESEARCH_DIRECT_URL_ALREADY_ATTEMPTED_TOOL_ERROR
        if (
            self.stage == "content_patch"
            and self.has_patch_input
            and tool_name in _CONTENT_PATCH_BLOCKED_TOOLS
        ):
            return _CONTENT_PATCH_TOOL_ERROR
        if self.stage == "scaffold" and self.has_scaffold_input:
            scaffold_error = _scaffold_error(
                tool_name,
                arguments,
                self.scaffold_input,
            )
            if scaffold_error is not None:
                return scaffold_error
        if self.stage == "images" and self.has_image_input:
            image_error = _image_generation_error(
                self.stage,
                tool_name,
                arguments,
                self.image_input,
            )
            if image_error is not None:
                return image_error
        if (
            self.stage == "outline_repair"
            and self.has_repair_input
            and (
                tool_name not in _OUTLINE_REPAIR_ALLOWED_TOOLS
                or not _repair_write_call_allowed(
                    self.stage,
                    tool_name,
                    arguments,
                )
            )
        ):
            return _OUTLINE_REPAIR_TOOL_ERROR
        if (
            self.stage == "deck_spec_repair"
            and self.has_repair_input
            and (
                tool_name not in _REPAIR_ALLOWED_TOOLS
                or not _repair_write_call_allowed(
                    self.stage,
                    tool_name,
                    arguments,
                )
            )
        ):
            return _REPAIR_TOOL_ERROR
        if (
            self.stage == "deck_redesign_repair"
            and self.has_repair_input
            and (
                tool_name not in _REPAIR_ALLOWED_TOOLS
                or not _repair_write_call_allowed(
                    self.stage,
                    tool_name,
                    arguments,
                )
            )
        ):
            return _REDESIGN_REPAIR_TOOL_ERROR
        image_status_error = _image_status_error(self.stage, tool_name, arguments)
        if image_status_error is not None:
            return image_status_error
        apply_patch_error = _apply_patch_error(
            self.stage,
            tool_name,
            arguments,
            repair_allowed=self.apply_patch_repair_allowed,
            repair_paths=self.apply_patch_repair_paths,
            workspace_dir=self.workspace_dir,
            artifact_root_dir=self.artifact_root_dir,
            staged_write_id=self._apply_patch_staged_write_id,
        )
        if apply_patch_error is not None:
            return apply_patch_error
        apply_redesign_error = _apply_redesign_error(
            self.stage,
            tool_name,
            arguments,
            self.workspace_dir,
            self.artifact_root_dir,
        )
        if apply_redesign_error is not None:
            return apply_redesign_error
        return _finalize_error(self.stage, tool_name, arguments)

    def _clear_step_failure(self, stage: str) -> None:
        if self._last_step_failure_signature is None:
            return
        if self._last_step_failure_signature.startswith(f"{stage}:"):
            self._last_step_failure_signature = None
            self._step_failure_streak = 0

    def _record_step_failure(self, signature: str) -> None:
        scoped_signature = f"{self.stage}:{signature}"
        if scoped_signature == self._last_step_failure_signature:
            self._step_failure_streak += 1
        else:
            self._last_step_failure_signature = scoped_signature
            self._step_failure_streak = 1
        if self._step_failure_streak >= _REPEATED_EXECUTION_FAILURE_LIMIT:
            self.repair_stalled = True
            _log.warning(
                "controlled_presentation/repair_stalled stage=%s repeated_failure=%d",
                self.stage,
                self._step_failure_streak,
            )

    def _record_policy_rejection(
        self,
        result: ToolResult,
    ) -> None:
        decision_id = self._active_tool_decision_id
        if self.stage not in _POLICY_REJECTION_STAGES:
            self._policy_rejection_stage = None
            self._policy_rejection_signature = None
            self._policy_rejection_streak = 0
            self._policy_rejection_decision_id = None
            return
        signature = _tool_failure_signature(result)
        if signature.startswith(
            "CONTROLLED_PRESENTATION_RESEARCH_BROWSER_CONNECTOR_UNAVAILABLE"
        ):
            # One model response may contain several gateway reads planned before
            # the first connector failure is observed. Reject the stale siblings,
            # but do not mistake that single preplanned batch for repeated model
            # non-compliance and terminate the whole presentation workflow.
            self._policy_rejection_signature = None
            self._policy_rejection_streak = 0
            self._policy_rejection_decision_id = None
            return
        if not signature.startswith("CONTROLLED_PRESENTATION_"):
            self._policy_rejection_stage = None
            self._policy_rejection_signature = None
            self._policy_rejection_streak = 0
            self._policy_rejection_decision_id = None
            return
        if (
            decision_id is not None
            and decision_id == self._policy_rejection_decision_id
            and self.stage == self._policy_rejection_stage
            and signature == self._policy_rejection_signature
        ):
            # One model decision may fan out several parallel calls before any
            # sibling rejection is visible. Count that decision once, not once
            # per preplanned tool call.
            return
        if (
            self.stage == self._policy_rejection_stage
            and signature == self._policy_rejection_signature
        ):
            self._policy_rejection_streak += 1
        else:
            self._policy_rejection_stage = self.stage
            self._policy_rejection_signature = signature
            self._policy_rejection_streak = 1
        self._policy_rejection_decision_id = decision_id
        if self.stage == "research" and self.research_revalidation is None:
            # Research has its own bounded fallback in build_checkpoint(). A
            # transition guard such as SEARCH_COMPLETE must move the workflow
            # toward an unverified ledger/outline fallback, not terminate the
            # whole presentation through the generic repair fuse.
            return
        if self._policy_rejection_streak >= _REPEATED_POLICY_REJECTION_LIMIT:
            self.repair_stalled = True
            _log.warning(
                "controlled_presentation/repair_stalled "
                "stage=%s consecutive_policy_rejections=%d",
                self.stage,
                self._policy_rejection_streak,
            )

    def record_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        *,
        executed: bool = True,
    ) -> None:
        """Update deterministic-repair state after one tool result."""
        self._research_last_evidence_urls.clear()
        if not executed:
            self._record_policy_rejection(result)
            return
        self._policy_rejection_stage = None
        self._policy_rejection_signature = None
        self._policy_rejection_streak = 0
        self._policy_rejection_decision_id = None
        self._record_pending_write_result(tool_name, arguments, result)
        if self.stage == "outline" and tool_name == "staged_file_write":
            action = arguments.get("action")
            if action == "begin" and result.success:
                raw_output = result.raw_output
                write_id = (
                    raw_output.get("write_id")
                    if isinstance(raw_output, dict)
                    else None
                )
                self._outline_staged_write_id = (
                    write_id if isinstance(write_id, str) and write_id else None
                )
            elif action in {"commit", "abort"} and result.success:
                self._outline_staged_write_id = None
        if self.stage == "content_patch_repair" and tool_name == "staged_file_write":
            action = arguments.get("action")
            if (
                action == "begin"
                and result.success
                and self._content_patch_staged_write_id is None
            ):
                raw_output = result.raw_output
                write_id = (
                    raw_output.get("write_id")
                    if isinstance(raw_output, dict)
                    else None
                )
                self._content_patch_staged_write_id = (
                    write_id if isinstance(write_id, str) and write_id else None
                )
            elif action in {"commit", "abort"} and result.success:
                self._content_patch_staged_write_id = None
        if self.stage == "apply_patch" and tool_name == "staged_file_write":
            action = arguments.get("action")
            if action == "begin" and result.success:
                raw_output = result.raw_output
                write_id = (
                    raw_output.get("write_id")
                    if isinstance(raw_output, dict)
                    else None
                )
                self._apply_patch_staged_write_id = (
                    write_id if isinstance(write_id, str) and write_id else None
                )
            elif action in {"commit", "abort"} and result.success:
                self._apply_patch_staged_write_id = None
        if _is_committed_repair_mutation(
            self.stage,
            tool_name,
            arguments,
            result,
        ):
            self._successful_mutation_since_checkpoint = True
        if (
            self.stage == "images"
            and tool_name == "generate_image"
            and _image_result_is_unauthorized(result)
        ):
            manifest_mode = self._persist_image_auth_blocked()
            self.image_auth_blocked = manifest_mode != "auto"
            _log.warning(
                "controlled_presentation/image_auth_blocked status=401 "
                "mode=%s hard_blocked=%s further_image_calls_stopped=true",
                manifest_mode or "unknown",
                self.image_auth_blocked,
            )

        if self.stage == "research" and tool_name == "tool_search":
            self._research_discovery_attempts += 1
            if not result.success:
                self._research_failed_discovery_attempts += 1
            elif _research_result_is_empty(result):
                self._research_empty_discovery_attempts += 1
            else:
                self._research_successful_discovery_attempts += 1

        direct_read_call = _is_direct_research_read_call(tool_name, arguments)
        if self.stage == "research" and (
            tool_name == "web_search" or direct_read_call
        ):
            self._research_tool_attempts += 1
            self._research_calls_since_checkpoint += 1
            self._research_last_direct_source_url = None
            establishes_direct_source = False
            counts_direct_read = direct_read_call
            effective_arguments = arguments
            if tool_name in MANAGED_RESEARCH_NAVIGATE_TOOLS and result.success:
                page_text, resolved_url = _research_direct_source_content(
                    arguments,
                    result,
                )
                if (
                    _research_navigation_result_is_metadata_only(page_text)
                    and _is_substantive_research_url(resolved_url)
                ):
                    self._research_pending_playwright_url = resolved_url
                    counts_direct_read = False
            elif tool_name in MANAGED_RESEARCH_SNAPSHOT_TOOLS:
                pending_url = self._research_pending_playwright_url
                self._research_pending_playwright_url = None
                effective_arguments = {**arguments, "url": pending_url or ""}
            if direct_read_call:
                self._research_direct_read_tool_names.add(tool_name)
                if counts_direct_read:
                    self._research_direct_read_attempts += 1
                    direct_read_key = _research_direct_read_key(
                        tool_name,
                        effective_arguments,
                    )
                    if direct_read_key is not None:
                        self._research_direct_read_keys.add(direct_read_key)
                    establishes_direct_source = (
                        _research_result_establishes_direct_source(
                            tool_name,
                            effective_arguments,
                            result,
                        )
                    )
            if not result.success:
                self._research_failed_attempts += 1
            elif _research_result_is_empty(result):
                self._research_empty_attempts += 1
            else:
                self._research_successful_attempts += 1
                if establishes_direct_source:
                    self._research_successful_direct_read_attempts += 1
                    page_text, resolved_url = _research_direct_source_content(
                        effective_arguments,
                        result,
                    )
                    if resolved_url:
                        self._research_direct_source_text[resolved_url] = (
                            _normalized_source_text(page_text)
                        )
                        self._research_last_direct_source_url = resolved_url
                        self._research_last_evidence_urls.add(resolved_url)
            if direct_read_call and counts_direct_read:
                if establishes_direct_source:
                    self._research_consecutive_unproductive_direct_reads = 0
                else:
                    self._research_consecutive_unproductive_direct_reads += 1
            if (
                tool_name in GATEWAY_RESEARCH_READ_TOOLS
                and not result.success
                and "source_unavailable"
                in f"{result.error or ''}\n{result.content or ''}".casefold()
            ):
                self._research_browser_connector_unavailable = True
        validation_call = (
            self.stage == "research"
            and executed
            and _is_research_validation_call(tool_name, arguments)
        )
        if self.stage == "research" and result.success:
            if tool_name in _MUTATION_TOOLS:
                self._research_json_reads_since_mutation.clear()
            elif tool_name == "read_file":
                key = self._research_json_read_key(arguments)
                if key is not None:
                    self._research_json_reads_since_mutation.add(key)
        if validation_call:
            downgraded = _downgrade_unread_verified_evidence(
                tool_name,
                arguments,
                self.workspace_dir,
                self.artifact_root_dir,
                self._pending_unread_evidence_urls,
            )
            self._pending_unread_evidence_urls.clear()
            if downgraded:
                _log.info(
                    "controlled_presentation/research_evidence_downgraded rows=%d",
                    downgraded,
                )
            if _research_validation_failed(
                tool_name,
                arguments,
                result,
                self.workspace_dir,
                self.artifact_root_dir,
            ):
                self._research_failed_validation_attempts += 1
            # The validator writes a fresh JSON report even when it exits non-zero.
            # Let the model inspect that report and the ledger once for repair.
            self._research_json_reads_since_mutation.clear()

        if self.stage in _REPAIR_STAGES:
            if result.success:
                self._repair_failure_signature = None
                self._repair_failure_streak = 0
            else:
                signature = _tool_failure_signature(result)
                if (
                    self._repair_failure_stage == self.stage
                    and self._repair_failure_signature == signature
                ):
                    self._repair_failure_streak += 1
                else:
                    self._repair_failure_stage = self.stage
                    self._repair_failure_signature = signature
                    self._repair_failure_streak = 1
                if (
                    self._repair_failure_streak
                    >= _REPEATED_EXECUTION_FAILURE_LIMIT
                ):
                    self.repair_stalled = True
                    _log.warning(
                        "controlled_presentation/repair_stalled "
                        "stage=%s consecutive_failed_tools=%d",
                        self.stage,
                        self._repair_failure_streak,
                    )
            return

        if self.stage == "apply_patch" and result.success:
            patch_path = arguments.get("path")
            wrote_patch = (
                tool_name in {"write_file", "edit_file"}
                and (
                    tool_name != "write_file"
                    or arguments.get("final", True) is not False
                )
                and isinstance(patch_path, str)
                and Path(patch_path).name == "deck.patch.json"
                and ".." not in Path(patch_path).parts
            )
            committed_staged_patch = (
                tool_name == "staged_file_write"
                and arguments.get("action") == "commit"
            )
            applied_patch = (
                tool_name == "bash"
                and _apply_patch_error(
                    self.stage,
                    tool_name,
                    arguments,
                    workspace_dir=self.workspace_dir,
                    artifact_root_dir=self.artifact_root_dir,
                )
                is None
            )
            if wrote_patch or committed_staged_patch or applied_patch:
                self._clear_step_failure("apply_patch")
            if applied_patch:
                self.apply_patch_repair_allowed = False
                self.apply_patch_repair_paths = ()

        if self.stage == "outline_qa":
            if result.success and _is_outline_validation_call(tool_name, arguments):
                self._clear_step_failure("outline_qa")
                signature = None
            else:
                signature = _outline_validation_failure_signature(
                    tool_name,
                    arguments,
                    result,
                    self.workspace_dir,
                )
        elif self.stage == "finalize":
            signature = _finalizer_failure_signature(
                tool_name,
                arguments,
                result,
            )
        elif self.stage == "apply_patch":
            signature = _apply_patch_failure_signature(
                tool_name,
                arguments,
                result,
                self.workspace_dir,
                self.artifact_root_dir,
            )
            if signature is not None:
                self.apply_patch_repair_allowed = True
                named_paths = _failure_field_paths(result)
                if named_paths:
                    self.apply_patch_repair_paths = named_paths
        elif self.stage == "scaffold":
            signature = _scaffold_failure_signature(
                tool_name,
                arguments,
                result,
                self.scaffold_input,
            )
        else:
            signature = None

        if signature is None:
            return
        self._record_step_failure(signature)

    def exempts_tool_budget(self, tool_name: str) -> bool:
        """Return whether a research-stage call is exempt from delivery budget."""
        return (
            self.stage == "research"
            and (
                tool_name in RESEARCH_BUDGET_EXEMPT_TOOLS
                or tool_name in self._research_direct_read_tool_names
            )
        )

    def record_visible_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        visible_content: str,
    ) -> None:
        """Capture URL-bound search summaries actually exposed to the model."""
        if self.stage != "research" or not result.success:
            return
        if _is_direct_research_read_call(tool_name, arguments):
            return
        extracted = extract_search_result_evidence(visible_content)
        for url, text in extracted.items():
            normalized_text = _normalized_source_text(text)
            if not normalized_text:
                continue
            existing = self._research_search_source_text.get(url, "")
            self._research_search_source_text[url] = existing + normalized_text
            self._research_last_evidence_urls.add(url)

    def evidence_urls(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> frozenset[str]:
        """Return URL evidence established by the latest visible tool result."""
        if not result.success:
            return frozenset()
        return frozenset(self._research_last_evidence_urls)

    def uses_evidence_read_budget(self, tool_name: str) -> bool:
        """Return whether this call uses the bounded direct-read batch."""
        return (
            self.stage == "research"
            and (
                tool_name in DIRECT_RESEARCH_READ_TOOLS
                or tool_name in self._research_direct_read_tool_names
            )
        )

    def is_direct_evidence_read_tool(self, tool_name: str) -> bool:
        """Return whether a successful result can establish URL provenance."""
        return (
            tool_name in DIRECT_RESEARCH_READ_TOOLS
            or tool_name in self._research_direct_read_tool_names
        )

    def direct_evidence_url(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> str | None:
        """Return the source URL established by the most recent direct-read result."""
        if not result.success or not self.is_direct_evidence_read_tool(tool_name):
            return None
        return self._research_last_direct_source_url

    def allows_completion_continuation(self) -> bool:
        return self.stage not in {"complete", "repair_stalled", "image_auth_blocked"}

    def suppresses_generic_final_summary(self) -> bool:
        return self.stage not in {
            None,
            "complete",
            "repair_stalled",
            "image_auth_blocked",
        }
