"""Whole-input table evidence and its capability-owned completion gate."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import re
from threading import Event
from typing import Any
from urllib.parse import unquote, urlsplit

from ..completion_checks import CompletionContext, CompletionIssue
from ..base import ToolInvocationContext, ToolResult
from .read_tool import ReadTool
from .table_validation import FIELD_TYPES, TableScan, fingerprint, normalize_fields, scan_table


def _input_paths(content: Any, tool: ReadTool) -> set[Path]:
    """Only explicitly referenced existing inputs, never a workspace-wide scan."""
    if isinstance(content, list):
        return set().union(*(_input_paths(block, tool) for block in content))
    if isinstance(content, dict):
        return set().union(*(_input_paths(content[key], tool) for key in (
            "text", "path", "url", "uri", "file_url", "resource", "source",
        ) if key in content))
    if not isinstance(content, str):
        return set()
    # Quoted paths/Markdown preserve spaces. Bare names and paths cover ordinary
    # prose and attachments represented as JSON by hosts.
    candidates = []

    def capture_path(match: re.Match[str]) -> str:
        candidates.append(match.group(1))
        return " " * len(match.group(0))

    remaining = re.sub(r'\[[^\]\n]*\]\(([^)\n]+\.(?:csv|tsv))\)', capture_path, content, flags=re.I)
    remaining = re.sub(r'''["'`<]([^"'`<>\n]+\.(?:csv|tsv))["'`>]''', capture_path, remaining, flags=re.I)
    candidates += re.findall(r'''(?<![\w./\\:])[\w./\\:~%-]+\.(?:csv|tsv)(?!\w)''', remaining, re.I)
    paths = set()
    for value in candidates:
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"file", "local-file"} or parsed.netloc not in {"", "localhost"}:
                continue
            value = unquote(parsed.path)
        try:
            path = tool._resolve_file_path(value).resolve()
            if path.is_file():
                paths.add(path)
        except (OSError, ValueError):
            continue
    return paths


async def _in_thread(function, *args, cancelled=lambda: False, **kwargs):
    stopped = Event()
    try:
        return await asyncio.to_thread(
            function, *args, cancelled=lambda: stopped.is_set() or cancelled(), **kwargs,
        )
    finally:
        stopped.set()


class TableCompletionGate:
    def __init__(self, tool: ValidateTableTool, context: CompletionContext, paths: set[Path]):
        self.tool = tool
        self.context = context
        self.sources: dict[Path, str | None] = dict.fromkeys(sorted(paths))
        self.receipts: dict[Path, TableScan] = {}
        self.original_receipts: dict[Path, TableScan] = {}
        self.source_modes: dict[Path, str] = {}
        self.contracts: dict[Path, dict] = {}
        self.purpose: str | None = None

    async def include_inputs(self, context: CompletionContext) -> None:
        latest = next((m.content for m in reversed(context.messages)
                       if m.role == "user" and m.source == "user"), "")
        added = _input_paths(latest, self.tool) - self.sources.keys()
        for path in sorted(added):
            self.sources[path] = None
            # Read authorization can still be negotiated by the tool invocation.
            await self.pin(path)
        if added:
            context.record("table.inputs", {"sources": {str(p): sha for p, sha in self.sources.items()}})

    def bind_purpose(self, purpose: str) -> None:
        if self.purpose is not None and self.purpose != purpose:
            raise ValueError("TABLE_PURPOSE_CHANGED: cannot switch the run's purpose after validation starts")
        if self.purpose is None:
            self.context.record("table.purpose", {"purpose": purpose})
            self.purpose = purpose

    def can_validate_edit(self, path: Path) -> bool:
        original = self.original_receipts.get(path)
        return self.source_modes.get(path) == "edit_target" and original is not None and original.complete

    async def pin(self, path: Path, *, allow_edit: bool = False) -> ToolResult | None:
        denial = self.tool._validate_readable_file(path, str(path), text_only=False)
        if denial:
            return denial
        digest, _ = await _in_thread(fingerprint, path, cancelled=self.context.is_cancelled)
        expected = self.sources.get(path)
        if expected is not None and digest != expected:
            if not allow_edit:
                return ToolResult(success=False, error="TABLE_SOURCE_CHANGED: original input changed; completion is blocked")
        else:
            self.sources[path] = digest
        return None

    async def check(self) -> CompletionIssue | None:
        missing = []
        for path, expected in self.sources.items():
            receipt = self.receipts.get(path)
            denial = await self.pin(path)
            if denial:
                if self.can_validate_edit(path) and (denial.error or "").startswith("TABLE_SOURCE_CHANGED:"):
                    return CompletionIssue(
                        "TABLE_EDIT_VALIDATION_MISSING",
                        f"{path}: edited version needs validate_table with the locked field contract before completion.",
                    )
                return CompletionIssue("TABLE_SOURCE_UNVERIFIED", f"{path}: {denial.error}", False)
            if receipt is None:
                missing.append(str(path))
            elif receipt.source_sha256 != expected:
                return CompletionIssue("TABLE_SOURCE_CHANGED", str(path), False)
            elif not receipt.complete:
                return CompletionIssue(
                    "TABLE_VALIDATION_INCOMPLETE",
                    f"{path}: parsing did not reach EOF; full validation is unverified.",
                    False,
                )
            elif not receipt.valid and (self.purpose != "diagnosis" or self.source_modes.get(path) == "edit_target"):
                return CompletionIssue(
                    "TABLE_VALIDATION_FAILED",
                    f"{path}: EOF={receipt.complete}, original_records={receipt.records}, "
                    f"invalid_records={receipt.invalid_records}, field_errors={receipt.error_count}. "
                    "Preserve and report the validation diagnostics; this input cannot be certified valid.",
                    False,
                )
        if missing:
            return CompletionIssue(
                "TABLE_VALIDATION_MISSING",
                "Call validate_table on every original input with a contract for every column, "
                "including required IDs, status enums, booleans, dates, and finite amounts with the "
                "task's bounds/scale. No filtering or deduplication before validation. Inputs: "
                + json.dumps(missing, ensure_ascii=False),
            )
        return None


class ValidateTableTool(ReadTool):
    """A real parser produces evidence; model claims cannot credit this gate."""

    aliases = ()
    uses_invocation_context = True

    @property
    def name(self) -> str:
        return "validate_table"

    @property
    def description(self) -> str:
        return (
            "Validate EVERY original CSV/TSV record to EOF before filtering or deduplication. "
            "For explicitly supplied table inputs this is required before completion, even if Python "
            "already calculated outputs. Provide fields for ALL columns using the task's rules: "
            "text/integer/decimal/date(YYYY-MM-DD)/boolean(true,false)/enum. Required by default; "
            "use nullable=true only when allowed. Decimal is finite and supports minimum/maximum "
            "as strings and scale. Enum requires explicit values. Schema is locked after a scan, "
            "including failures. Default profile='declared_fields' uses the task's actual rules; "
            "column names never select domain rules. Explicit profile='transaction_v1' for matching "
            "order/refund tasks enforces required IDs, revision integer minimum>=1, amount decimal minimum>=0 scale<=2, "
            "status/region/currency enums, date as date and is_test as boolean when present; "
            "these checks cannot be weakened to text or nullable. Other tables use the declared rules. "
            "Inputs must stay unchanged by default. Only for a user-requested in-place edit/append, "
            "declare source_mode='edit_target' for that file BEFORE its first scan and before writing. "
            "This role is locked per file. Scan the full original first, then rescan the full edited "
            "version with the same field contract; final edited data must be valid. "
            "Returns hashes, full row and "
            "field counts, and at most 20 error samples. Declare purpose='diagnosis' BEFORE the first "
            "scan only when the task is to inspect/report data errors; otherwise use 'computation' "
            "(initial default). Purpose is locked for the run; omitted later calls inherit it. "
            "success/validation_complete mean the scan finished; data_valid reports data quality "
            "separately. A complete diagnosis may finish with data_valid=false; computation still "
            "requires valid input. Both require every input/field, EOF and hashes matching the validated version. "
            "Report all required findings and deliver/read back requested diagnostic artifacts. "
            "This validates source data, not the correctness of output calculations."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "profile": {
                    "type": "string", "enum": ["declared_fields", "transaction_v1"],
                    "description": "Explicit domain contract, locked with fields. Default declared_fields follows the user's actual rules. Choose transaction_v1 only for tasks requiring its order/refund constraints, never from column names alone. Omit later calls to inherit.",
                },
                "source_mode": {
                    "type": "string", "enum": ["input", "edit_target"],
                    "description": "Per-file role locked before its first scan. Default input is immutable. Use edit_target only for user-requested in-place edits/appends; validate the full original before writing and the valid final version afterward. Omit later calls to inherit.",
                },
                "purpose": {
                    "type": "string", "enum": ["diagnosis", "computation"],
                    "description": "Task purpose, fixed before the first scan. Omit to inherit; initially computation. Diagnosis only inspects/reports data errors, not calculations requiring valid input.",
                },
                "fields": {
                    "type": "object", "minProperties": 1, "maxProperties": 256,
                    "additionalProperties": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "type": {"type": "string", "enum": list(FIELD_TYPES)},
                            "nullable": {"type": "boolean"},
                            "minimum": {"type": "string"}, "maximum": {"type": "string"},
                            "scale": {"type": "integer", "minimum": 0, "maximum": 18},
                            "values": {"type": "array", "items": {"type": "string"}},
                        }, "required": ["type"],
                    },
                },
                "encoding": {"type": "string", "enum": ["utf-8-sig", "utf-8", "utf-16", "gb18030"]},
            }, "required": ["path", "fields"],
        }

    async def create_completion_gate(self, context: CompletionContext) -> TableCompletionGate | None:
        gate = TableCompletionGate(self, context, set())
        await gate.include_inputs(context)
        return gate if gate.sources else None

    async def _invoke_validated(self, arguments: dict, *, context: ToolInvocationContext | None) -> ToolResult:
        gate = context.completion_gate if context is not None else None
        return await self._validate(**arguments, gate=gate)

    async def execute(self, path: str, fields: dict, encoding: str = "utf-8-sig",
                      purpose: str | None = None, source_mode: str | None = None,
                      profile: str | None = None) -> ToolResult:
        return await self._validate(path, fields, encoding, purpose=purpose, source_mode=source_mode, profile=profile)

    async def _validate(self, path: str, fields: dict, encoding: str = "utf-8-sig",
                        gate: TableCompletionGate | None = None,
                        purpose: str | None = None, source_mode: str | None = None,
                        profile: str | None = None) -> ToolResult:
        try:
            source = self._resolve_file_path(path).resolve()
            if gate is not None and source not in gate.sources:
                # Validating a generated output or a sample cannot certify an
                # original input, nor turn that output into an immutable input.
                gate = None
            denial = self._validate_readable_file(source, path, text_only=False)
            if denial:
                return denial
            selected_purpose = purpose if purpose is not None else (gate.purpose if gate else None) or "computation"
            if selected_purpose not in {"diagnosis", "computation"}:
                raise ValueError("Invalid table task purpose")
            selected_mode = source_mode if source_mode is not None else (gate.source_modes.get(source) if gate else None) or "input"
            if selected_mode not in {"input", "edit_target"}:
                raise ValueError("Invalid table source mode")
            selected_profile = profile if profile is not None else (gate.contracts.get(source, {}).get("profile") if gate else None) or "declared_fields"
            contract = {"fields": normalize_fields(fields, profile=selected_profile), "encoding": encoding, "profile": selected_profile}
            allow_edit = False
            if gate is not None:
                if source in gate.source_modes and gate.source_modes[source] != selected_mode:
                    return ToolResult(success=False, error="TABLE_SOURCE_MODE_CHANGED: cannot change a scanned file's role")
                gate.source_modes[source] = selected_mode
                allow_edit = gate.can_validate_edit(source)
                denial = await gate.pin(source, allow_edit=allow_edit)
                if denial:
                    return denial
                if source in gate.contracts and gate.contracts[source] != contract:
                    return ToolResult(success=False, error="TABLE_CONTRACT_CHANGED: cannot relax or replace a scanned contract")
                gate.bind_purpose(selected_purpose)
            receipt = await _in_thread(
                scan_table, source, fields, encoding=encoding, profile=selected_profile,
                cancelled=gate.context.is_cancelled if gate else lambda: False,
            )
            payload = {
                **asdict(receipt), "valid": receipt.valid,
                "validation_complete": receipt.complete,
                "data_valid": receipt.valid if receipt.complete else None,
                "purpose": selected_purpose,
                "source_mode": selected_mode,
            }
            if gate is not None:
                if receipt.source_sha256 != gate.sources[source] and not allow_edit:
                    return ToolResult(success=False, error="TABLE_SOURCE_CHANGED: scan does not match the original input")
                gate.context.record("table.validation", {**payload, "contract": contract})
                gate.contracts[source] = contract
                gate.receipts[source] = receipt
                gate.original_receipts.setdefault(source, receipt)
                gate.sources[source] = receipt.source_sha256
            summary = json.dumps(payload, ensure_ascii=False)
            return ToolResult(
                success=receipt.complete, content=summary, raw_output=payload,
                # Failed tool history uses error rather than content. Keep the
                # bounded diagnostics visible there as well as in host payloads.
                error=None if receipt.complete else "TABLE_VALIDATION_INCOMPLETE: " + summary,
            )
        except (OSError, ValueError, LookupError) as exc:
            return ToolResult(success=False, error=f"TABLE_VALIDATION_FAILED: {exc}")
