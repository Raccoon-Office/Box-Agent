"""Streaming, whole-source CSV/TSV field validation.

This module has no model verdict, row filter, sampling or deduplication path.
Every parsed original record is checked before any downstream calculation.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


MAX_ERROR_SAMPLES = 20
FIELD_TYPES = ("text", "integer", "decimal", "date", "boolean", "enum")
_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise InterruptedError("Table validation cancelled; coverage is unverified")


@contextmanager
def _open_regular(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Table source must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def _stamp(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def fingerprint(path: Path, cancelled: Callable[[], bool] = lambda: False) -> tuple[str, tuple[int, ...]]:
    """Hash the complete regular file, rejecting changes during the read."""
    digest = hashlib.sha256()
    with _open_regular(path) as stream:
        before = _stamp(os.fstat(stream.fileno()))
        while True:
            _check_cancelled(cancelled)
            block = stream.read(64 * 1024)
            if not block:
                break
            digest.update(block)
        if before != _stamp(os.fstat(stream.fileno())) or before != _stamp(path.stat()):
            raise ValueError("Table source changed during validation")
    return digest.hexdigest(), before


def validation_profile(fields: dict) -> str:
    """Recognize the transaction contract by columns, never by filenames."""
    return "transaction_v1" if (
        {"amount", "revision", "status"} <= fields.keys()
        and {"id", "order_id", "refund_id"} & fields.keys()
    ) else "declared_fields"


def _require_transaction_fields(fields: dict[str, dict[str, Any]]) -> None:
    if validation_profile(fields) != "transaction_v1":
        return
    types = {"amount": "decimal", "revision": "integer", "status": "enum",
             "date": "date", "is_test": "boolean", "region": "enum", "currency": "enum"}
    for name, rule in fields.items():
        kind = "text" if name == "id" or name.endswith("_id") else types.get(name)
        if kind and (rule["type"] != kind or rule["nullable"]):
            raise ValueError(f"TRANSACTION_CONTRACT: {name} must be required {kind}")
    for name, minimum in (("amount", 0), ("revision", 1)):
        rule = fields[name]
        if "minimum" not in rule or Decimal(rule["minimum"]) < minimum:
            raise ValueError(f"TRANSACTION_CONTRACT: {name} requires minimum >= {minimum}")
    if "scale" not in fields["amount"] or fields["amount"]["scale"] > 2:
        raise ValueError("TRANSACTION_CONTRACT: amount requires scale <= 2")


def normalize_fields(fields: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Validate the entire declarative contract; no omitted/unknown rules."""
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 256:
        raise ValueError("A contract for every column is required (1–256 columns)")
    normalized = {}
    for name, supplied in fields.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or not isinstance(supplied, dict):
            raise ValueError("Invalid column contract")
        if set(supplied) - {"type", "nullable", "minimum", "maximum", "scale", "values"}:
            raise ValueError(f"Unknown field constraint: {name}")
        rule = dict(supplied)
        kind = rule.get("type")
        if kind not in FIELD_TYPES or type(rule.get("nullable", False)) is not bool:
            raise ValueError(f"Invalid field type or nullable constraint: {name}")
        rule.setdefault("nullable", False)
        if "scale" in rule and (kind != "decimal" or type(rule["scale"]) is not int or not 0 <= rule["scale"] <= 18):
            raise ValueError(f"Invalid scale: {name}")
        for bound in ("minimum", "maximum"):
            if bound in rule:
                value = rule[bound]
                if (kind not in {"integer", "decimal"} or not isinstance(value, str)
                        or not 1 <= len(value) <= 128 or not _DECIMAL.fullmatch(value)):
                    raise ValueError(f"Invalid {bound}: {name}")
                try:
                    if not Decimal(value).is_finite():
                        raise ValueError(f"Invalid {bound}: {name}")
                except InvalidOperation as exc:
                    raise ValueError(f"Invalid {bound}: {name}") from exc
        if "minimum" in rule and "maximum" in rule and Decimal(rule["minimum"]) > Decimal(rule["maximum"]):
            raise ValueError(f"Minimum exceeds maximum: {name}")
        if kind == "enum":
            values = rule.get("values")
            if (not isinstance(values, list) or not 1 <= len(values) <= 100
                    or any(not isinstance(v, str) or not 1 <= len(v) <= 128 for v in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f"Enum requires explicit distinct values: {name}")
        elif "values" in rule:
            raise ValueError(f"Only enum fields accept values: {name}")
        normalized[name] = rule
    _require_transaction_fields(normalized)
    return normalized


def _field_error(value: str | None, rule: dict[str, Any]) -> str | None:
    if value is None or not value.strip():
        return None if rule["nullable"] else "missing"
    kind = rule["type"]
    if kind == "text":
        return None
    if kind == "enum":
        return None if value in rule["values"] else "enum"
    if kind == "boolean":
        return None if value.casefold() in {"true", "false"} else "boolean"
    if kind == "date":
        try:
            if not _DATE.fullmatch(value):
                return "date"
            date.fromisoformat(value)
            return None
        except ValueError:
            return "date"
    if not (_INTEGER if kind == "integer" else _DECIMAL).fullmatch(value):
        return kind
    try:
        number = Decimal(value)
        if not number.is_finite():
            return "non_finite"
        if "minimum" in rule and number < Decimal(rule["minimum"]):
            return "minimum"
        if "maximum" in rule and number > Decimal(rule["maximum"]):
            return "maximum"
        if "scale" in rule and number.as_tuple().exponent < -rule["scale"]:
            return "scale"
    except (InvalidOperation, ValueError):
        return kind
    return None


@dataclass(frozen=True)
class TableScan:
    path: str
    source_sha256: str
    schema_sha256: str
    records: int
    complete: bool
    invalid_records: int
    error_count: int
    fields: dict[str, dict[str, int]]
    errors: list[dict[str, Any]]
    profile: str

    @property
    def valid(self) -> bool:
        return self.complete and self.error_count == 0


def scan_table(
    path: Path,
    fields: dict[str, dict[str, Any]],
    *,
    encoding: str = "utf-8-sig",
    cancelled: Callable[[], bool] = lambda: False,
) -> TableScan:
    """Read to EOF and validate every original field with bounded diagnostics."""
    # The caller resolves and authorizes the source. Do not follow a replacement
    # symlink between permission checking and opening the descriptor.
    path = path.absolute()
    if path.suffix.lower() not in {".csv", ".tsv"}:
        raise ValueError("Only CSV and TSV are supported")
    contract = normalize_fields(fields)
    schema_sha = hashlib.sha256(json.dumps(
        {"fields": contract, "encoding": encoding, "profile": validation_profile(contract)},
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    source_sha, stamp = fingerprint(path, cancelled)
    counts = {name: {"checked": 0, "missing": 0, "invalid": 0} for name in contract}
    errors: list[dict[str, Any]] = []
    records = invalid_records = error_count = 0
    complete = False

    def error(record: int, field: str, value: str | None, code: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < MAX_ERROR_SAMPLES:
            errors.append({"record": record, "field": field, "value": (value or "")[:160], "code": code})

    with _open_regular(path) as raw:
        if _stamp(os.fstat(raw.fileno())) != stamp:
            raise ValueError("Table source changed before parsing")
        with io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline="") as text:
            reader = csv.reader(text, delimiter="\t" if path.suffix.lower() == ".tsv" else ",", strict=True)
            header = next(reader, None)
            if header is None or not header or any(not v for v in header):
                raise ValueError("Table requires nonempty column names")
            if len(header) != len(set(header)):
                raise ValueError("Table has duplicate columns")
            if set(header) != set(contract):
                preview = [name[:80] for name in header[:10]]
                raise ValueError(
                    f"Contract must cover exactly all {len(header)} columns; "
                    f"first column names (bounded): {preview!r}"
                )
            try:
                for values in reader:
                    _check_cancelled(cancelled)
                    records += 1
                    before_errors = error_count
                    if len(values) != len(header):
                        error(records, "", None, "column_count")
                    for index, name in enumerate(header):
                        value = values[index] if index < len(values) else None
                        counts[name]["checked"] += 1
                        counts[name]["missing"] += int(value is None or not value.strip())
                        issue = _field_error(value, contract[name])
                        if issue:
                            counts[name]["invalid"] += 1
                            error(records, name, value, issue)
                    invalid_records += int(error_count > before_errors)
                complete = True
            except (csv.Error, UnicodeError) as exc:
                error(records + 1, "", str(exc), "parse_error")
            if _stamp(os.fstat(raw.fileno())) != stamp:
                raise ValueError("Table source changed while parsing")
    if fingerprint(path, cancelled) != (source_sha, stamp):
        raise ValueError("Table source changed after parsing")
    return TableScan(str(path), source_sha, schema_sha, records, complete,
                     invalid_records, error_count, counts, errors, validation_profile(contract))
