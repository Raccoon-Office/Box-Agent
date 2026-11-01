"""Whole-input table evidence and its capability-owned completion gate."""

from __future__ import annotations

import asyncio
from copy import deepcopy
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
        self.validation_locks: dict[Path, asyncio.Lock] = {}
        self.contracts: dict[Path, dict] = {}
        self.purpose: str | None = None

    def snapshot(self) -> dict:
        return deepcopy({
            "version": 1,
            "sources": {str(p): sha for p, sha in self.sources.items()},
            "contracts": {str(p): value for p, value in self.contracts.items()},
            "source_modes": {str(p): mode for p, mode in self.source_modes.items()},
            "receipts": {str(p): asdict(value) for p, value in self.receipts.items()},
            "original_receipts": {str(p): asdict(value) for p, value in self.original_receipts.items()},
            "original_is_current": [str(p) for p, value in self.receipts.items()
                                    if value is self.original_receipts.get(p)],
            "purpose": self.purpose,
        })

    @classmethod
    def restore(cls, tool: ValidateTableTool, context: CompletionContext, state: dict) -> TableCompletionGate:
        if state.get("version") != 1:
            raise ValueError("Unsupported table completion checkpoint")
        gate = cls(tool, context, set())
        gate.sources = {Path(p): sha for p, sha in state["sources"].items()}
        gate.contracts = {Path(p): deepcopy(value) for p, value in state["contracts"].items()}
        gate.source_modes = {Path(p): mode for p, mode in state["source_modes"].items()}
        gate.receipts = {Path(p): TableScan(**deepcopy(value)) for p, value in state["receipts"].items()}
        gate.original_receipts = {Path(p): TableScan(**deepcopy(value)) for p, value in state["original_receipts"].items()}
        for path in state["original_is_current"]:
            gate.receipts[Path(path)] = gate.original_receipts[Path(path)]
        gate.purpose = state["purpose"]
        return gate

    async def include_inputs(self, context: CompletionContext) -> None:
        latest = next((m.content for m in reversed(context.messages)
                       if m.role == "user" and m.source == "user"), "")
        # A reply may refer to the new name after a move. Its original gate
        # still verifies absence, destination permissions, full hash and data.
        moved_paths = {Path(contract["destination_path"]) for path, contract in self.contracts.items()
                       if self.source_modes.get(path) == "move_target" and "destination_path" in contract}
        added = _input_paths(latest, self.tool) - self.sources.keys() - moved_paths
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
        if self.source_modes.get(path) == "replace_target":
            return path in self.contracts
        return self.source_modes.get(path) == "edit_target" and original is not None and original.complete

    def prepared_operation(self, path: Path) -> ToolResult:
        payload = {"path": str(path), "source_mode": self.source_modes[path],
                   "source_sha256": self.sources[path], "operation_ready": True,
                   "validation_complete": None, "data_valid": None}
        self.context.record("table.file_operation", payload)
        return ToolResult(success=True, content=json.dumps(payload, ensure_ascii=False), raw_output=payload)

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

    async def check_move(self, source: Path, expected: str | None) -> CompletionIssue | None:
        destination = self.contracts.get(source, {}).get("destination_path")
        if not destination or expected is None:
            return CompletionIssue("TABLE_MOVE_UNVERIFIED", f"{source}: missing pre-move validation contract.", False)
        target = Path(destination)
        try:
            try:
                source.lstat()
            except FileNotFoundError:
                pass
            else:
                return CompletionIssue("TABLE_MOVE_PENDING", f"{source}: original path still exists; move to {target}.")
            if target.is_symlink() or target.resolve() != target:
                return CompletionIssue("TABLE_MOVE_UNVERIFIED", f"{target}: destination resolves through a changed link.", False)
            denial = self.tool._validate_readable_file(target, str(target), text_only=False)
            if denial:
                return CompletionIssue("TABLE_MOVE_UNVERIFIED", f"{target}: {denial.error}", False)
            digest, _ = await _in_thread(fingerprint, target, cancelled=self.context.is_cancelled)
            if digest != expected:
                return CompletionIssue("TABLE_SOURCE_CHANGED", f"{target}: moved content differs from the validated original.", False)
        except (OSError, ValueError, RuntimeError) as exc:
            return CompletionIssue("TABLE_MOVE_UNVERIFIED", f"{target}: {exc}", False)
        return None

    async def check(self) -> CompletionIssue | None:
        missing = []
        for path, expected in self.sources.items():
            if self.source_modes.get(path) == "delete_target":
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    return CompletionIssue("TABLE_SOURCE_UNVERIFIED", f"{path}: {exc}", False)
                return CompletionIssue("TABLE_DELETE_PENDING", f"{path}: declared deletion has not completed.")
            receipt = self.receipts.get(path)
            if self.source_modes.get(path) == "move_target":
                issue = await self.check_move(path, expected)
                if issue:
                    return issue
                denial = None
            else:
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
            elif ("output_fields" in self.contracts.get(path, {})
                  and receipt is self.original_receipts.get(path)):
                return CompletionIssue(
                    "TABLE_EDIT_VALIDATION_MISSING",
                    f"{path}: the declared final schema still requires a complete final scan.",
                )
            elif receipt.source_sha256 != expected:
                return CompletionIssue("TABLE_SOURCE_CHANGED", str(path), False)
            elif not receipt.complete:
                return CompletionIssue(
                    "TABLE_VALIDATION_INCOMPLETE",
                    f"{path}: parsing did not reach EOF; full validation is unverified.",
                    False,
                )
            elif not receipt.valid and (self.purpose != "diagnosis" or self.source_modes.get(path) in {"edit_target", "replace_target"}):
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
            "version; final edited data must be valid. For schema edits, declare output_fields "
            "alongside the original fields before the first scan; both contracts are locked. "
            "Omit fields on later scans to inherit the applicable contract. For user-requested "
            "whole-file replacement without processing old records, first declare replace_target "
            "with the final fields, then replace and validate the full new file. For deletion, "
            "first declare delete_target without fields, then delete through an authorized writer. "
            "For rename/move, first scan ALL original data with source_mode='move_target', fields "
            "and destination_path naming a different, absent destination. That destination is locked. "
            "Then use an authorized writer to move the file without changing its bytes. Completion "
            "requires the original path to be absent, the destination to be readable without changed "
            "symlinks, its full hash to match the original scan, and all original data checks to pass "
            "for the declared purpose. Copying, changing content or skipping validation does not suffice. "
            "Deletion/replacement preparation calls return operation_ready, not data-validation evidence; "
            "completion checks the actual final file state. Roles never authorize a write. "
            "Returns hashes, full row and "
            "field counts, and at most 20 error samples. Declare purpose='diagnosis' BEFORE the first "
            "scan only when the task is to inspect/report data errors; otherwise use 'computation' "
            "(initial default). Purpose is locked for the run; omitted later calls inherit it. "
            "For scan calls, success/validation_complete mean the scan finished; data_valid reports data quality "
            "separately. A complete diagnosis may finish with data_valid=false; computation still "
            "requires valid input. Both require every input/field, EOF and hashes matching the validated version. "
            "Report all required findings and deliver/read back requested diagnostic artifacts. "
            "This validates source data, not the correctness of output calculations."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "profile": {
                    "type": "string", "enum": ["declared_fields", "transaction_v1"],
                    "description": "Explicit domain contract, locked with fields. Default declared_fields follows the user's actual rules. Choose transaction_v1 only for tasks requiring its order/refund constraints, never from column names alone. Omit later calls to inherit.",
                },
                "source_mode": {
                    "type": "string", "enum": ["input", "edit_target", "replace_target", "delete_target", "move_target"],
                    "description": "Declare before any file operation; role is locked. input is immutable. edit_target validates original and final data. replace_target prepares a user-requested whole-file replacement without processing old records: fields describes the required final schema. delete_target prepares a user-requested deletion without parsing data or fields. move_target requires a full original scan with fields and a locked destination_path before moving unchanged bytes. These declarations do not authorize writes. Omit later calls to inherit.",
                },
                "destination_path": {
                    "type": "string", "minLength": 1,
                    "description": "Only for move_target: declare a different, absent destination before the first scan. Locked with the contract; omit later calls to inherit. Does not perform or authorize a move.",
                },
                "purpose": {
                    "type": "string", "enum": ["diagnosis", "computation"],
                    "description": "Task purpose, fixed before the first scan. Omit to inherit; initially computation. Diagnosis only inspects/reports data errors, not calculations requiring valid input.",
                },
                "fields": {
                    "type": "object", "minProperties": 1,
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
            }, "required": ["path"],
        }
        schema["properties"]["output_fields"] = {
            **schema["properties"]["fields"],
            "description": "Only for edit_target: declare a different final schema with the original fields before the first scan. Both schemas are locked; later scans use the final schema. Omit fields on later calls to inherit the applicable schema.",
        }
        return schema

    async def create_completion_gate(self, context: CompletionContext) -> TableCompletionGate | None:
        gate = TableCompletionGate(self, context, set())
        await gate.include_inputs(context)
        return gate if gate.sources else None

    async def restore_completion_gate(self, context: CompletionContext, state: dict) -> TableCompletionGate:
        return TableCompletionGate.restore(self, context, state)

    async def _invoke_validated(self, arguments: dict, *, context: ToolInvocationContext | None) -> ToolResult:
        gate = context.completion_gate if context is not None else None
        if gate is not None:
            try:
                source = self._resolve_file_path(arguments["path"]).resolve()
            except (OSError, ValueError) as exc:
                return ToolResult(success=False, error=f"TABLE_VALIDATION_FAILED: {exc}")
            # One file's role and contracts must not race parallel first calls.
            async with gate.validation_locks.setdefault(source, asyncio.Lock()):
                return await self._validate(**arguments, gate=gate)
        return await self._validate(**arguments, gate=gate)

    async def execute(self, path: str, fields: dict | None = None, encoding: str = "utf-8-sig",
                      purpose: str | None = None, source_mode: str | None = None,
                      profile: str | None = None, output_fields: dict | None = None,
                      destination_path: str | None = None) -> ToolResult:
        return await self._validate(path, fields, encoding, purpose=purpose, source_mode=source_mode,
                                    profile=profile, output_fields=output_fields, destination_path=destination_path)

    async def _validate(self, path: str, fields: dict | None = None, encoding: str = "utf-8-sig",
                        gate: TableCompletionGate | None = None,
                        purpose: str | None = None, source_mode: str | None = None,
                        profile: str | None = None, output_fields: dict | None = None,
                        destination_path: str | None = None) -> ToolResult:
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
            if selected_mode not in {"input", "edit_target", "replace_target", "delete_target", "move_target"}:
                raise ValueError("Invalid table source mode")
            if gate is not None and source in gate.source_modes and gate.source_modes[source] != selected_mode:
                return ToolResult(success=False, error="TABLE_SOURCE_MODE_CHANGED: cannot change a file's declared role")
            if selected_mode in {"delete_target", "replace_target", "move_target"} and gate is None:
                raise ValueError("File operations require an explicitly referenced source and an active completion gate")
            if destination_path is not None and selected_mode != "move_target":
                raise ValueError("destination_path is only supported for move_target")
            if selected_mode == "delete_target":
                if fields is not None or output_fields is not None or profile is not None or purpose is not None:
                    raise ValueError("Deletion preparation does not accept data-validation contracts")
                denial = await gate.pin(source)
                if denial:
                    return denial
                gate.source_modes[source] = selected_mode
                return gate.prepared_operation(source)

            previous = gate.contracts.get(source) if gate else None
            expected_fields = previous["fields"] if previous else None
            if previous and gate.can_validate_edit(source):
                expected_fields = previous.get("output_fields", expected_fields)
            selected_profile = profile if profile is not None else (previous or {}).get("profile", "declared_fields")
            scan_fields = normalize_fields(fields if fields is not None else expected_fields, profile=selected_profile)
            contract = {"fields": scan_fields, "encoding": encoding, "profile": selected_profile}
            if selected_mode == "move_target":
                destination = destination_path if destination_path is not None else (previous or {}).get("destination_path")
                if not isinstance(destination, str) or not destination.strip():
                    raise ValueError("move_target requires destination_path before the first scan")
                target = self._resolve_file_path(destination)
                if previous is None:
                    try:
                        target.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError("Move destination must be absent and different from the original input")
                target = target.resolve()
                if target == source:
                    raise ValueError("Move destination must differ from the original input")
                contract["destination_path"] = str(target)
            if output_fields is not None:
                if selected_mode != "edit_target":
                    raise ValueError("output_fields is only supported for edit_target")
                contract["output_fields"] = normalize_fields(output_fields, profile=selected_profile)
            if previous:
                if (scan_fields != expected_fields or encoding != previous["encoding"]
                        or selected_profile != previous["profile"]
                        or contract.get("destination_path") != previous.get("destination_path")
                        or (output_fields is not None and contract["output_fields"] != previous.get("output_fields"))):
                    return ToolResult(success=False, error="TABLE_CONTRACT_CHANGED: cannot relax or replace a declared contract")
            allow_edit = False
            if gate is not None:
                gate.source_modes[source] = selected_mode
                allow_edit = gate.can_validate_edit(source)
                denial = await gate.pin(source, allow_edit=allow_edit)
                if denial:
                    return denial
                gate.bind_purpose(selected_purpose)
                if selected_mode == "replace_target" and previous is None:
                    gate.contracts[source] = contract
                    return gate.prepared_operation(source)
            receipt = await _in_thread(
                scan_table, source, scan_fields, encoding=encoding, profile=selected_profile,
                cancelled=gate.context.is_cancelled if gate else lambda: False,
            )
            payload = {
                **asdict(receipt), "valid": receipt.valid,
                "validation_complete": receipt.complete,
                "data_valid": receipt.valid if receipt.complete else None,
                "purpose": selected_purpose,
                "source_mode": selected_mode,
            }
            if selected_mode == "move_target":
                payload["destination_path"] = contract["destination_path"]
            if gate is not None:
                if receipt.source_sha256 != gate.sources[source] and not allow_edit:
                    return ToolResult(success=False, error="TABLE_SOURCE_CHANGED: scan does not match the original input")
                gate.context.record("table.validation", {**payload, "contract": contract})
                gate.contracts.setdefault(source, contract)
                gate.receipts[source] = receipt
                if selected_mode != "replace_target":
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
