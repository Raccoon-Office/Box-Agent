"""Validated batch orchestration for offline ACP evaluation attempts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import Any, Mapping, Sequence

from acp_eval import SCHEMA_VERSION
from acp_eval.case_runner import CaseConfig, run_case
from acp_eval.ids import new_run_id
from acp_eval.models import CaseMetadata, RunResult, WorkspaceSetup
from acp_eval.storage import atomic_write_json, sha256_file


TERMINAL_ATTEMPT_STATUSES = frozenset({"finished", "incomplete"})
COMPLETENESS_STATUSES = (
    "complete",
    "complete_with_warnings",
    "incomplete",
    "corrupt",
)
ATTEMPT_ID_PATTERN = re.compile(r"attempt-\d{8}T\d{6}-[0-9a-f]{8}")


class LatestIndexError(ValueError):
    """A latest-attempt index is unsafe and must not be followed."""


class InputFingerprintMismatch(ValueError):
    """The evaluated input bytes differ from the pre-execution fingerprint."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_mapping(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _case_fingerprint(
    record: Mapping[str, Any], dataset_root: Path
) -> dict[str, Any]:
    input_files = [
        {
            "path": relative_path,
            "sha256": sha256_file(dataset_root / relative_path),
        }
        for relative_path in record["input_files"]
    ]
    fingerprint_payload = {
        "record": dict(record),
        "input_files": input_files,
    }
    return {
        "algorithm": "sha256",
        "sha256": _canonical_sha256(fingerprint_payload),
        "record_sha256": _canonical_sha256(dict(record)),
        "input_files": input_files,
    }


def _dataset_fingerprint(
    records: Sequence[Mapping[str, Any]],
    case_fingerprints: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    cases = [
        {
            "case_id": record["id"],
            "sha256": case_fingerprints[record["id"]]["sha256"],
        }
        for record in records
    ]
    return {
        "algorithm": "sha256",
        "sha256": _canonical_sha256(cases),
        "case_count": len(cases),
    }


def _command_value(command: Sequence[str], cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _runtime_identity(repo_root: Path, python_executable: str) -> dict[str, Any]:
    python_output = _command_value(
        [
            python_executable,
            "-c",
            "import platform; "
            "print(platform.python_version()); "
            "print(platform.python_implementation())",
        ],
        repo_root,
    )
    python_parts = python_output.splitlines() if python_output is not None else []
    python_version = python_parts[0] if len(python_parts) == 2 else None
    python_implementation = python_parts[1] if len(python_parts) == 2 else None
    box_agent_version = _command_value(
        [
            python_executable,
            "-c",
            "from importlib.metadata import version; print(version('box-agent'))",
        ],
        repo_root,
    )
    git_commit = _command_value(
        ["git", "rev-parse", "--verify", "HEAD"],
        repo_root,
    )
    return {
        "python": {
            "executable": python_executable,
            "version": python_version,
            "implementation": python_implementation,
            "status": (
                "available"
                if python_version is not None and python_implementation is not None
                else "unavailable"
            ),
        },
        "box_agent": {
            "version": box_agent_version,
            "version_status": (
                "available" if box_agent_version is not None else "unavailable"
            ),
            "git_commit": git_commit,
            "git_commit_status": "available" if git_commit is not None else "unavailable",
        },
    }


def _timeout_budget(
    repo_root: Path, python_executable: str, requested_seconds: float | None,
) -> dict[str, Any]:
    # Read only numeric policy from the same interpreter/config/environment as
    # ACP. Do not duplicate runtime defaults or print the user's configuration.
    output = _command_value([
        python_executable, "-c",
        "import json; from box_agent.config import Config; "
        "from box_agent.kernel.loop import _resolve_provider_stale_seconds, MAX_PROVIDER_STALE_RECOVERIES; "
        "print(json.dumps({'provider_stale_seconds': "
        "_resolve_provider_stale_seconds(Config.from_yaml(Config.find_config_file('config.yaml')).agent.provider_stale_seconds), "
        "'max_provider_stale_recoveries': MAX_PROVIDER_STALE_RECOVERIES}))",
    ], repo_root)
    wait = recoveries = minimum = None
    try:
        policy = json.loads(output or "null")
        wait = policy["provider_stale_seconds"]
        recoveries = policy["max_provider_stale_recoveries"]
        if (type(wait) not in {float, int} or not math.isfinite(wait) or wait <= 0
                or type(recoveries) is not int or recoveries < 0):
            raise ValueError("invalid runtime wait policy")
        # One main request, its bounded stale recoveries, one final judge,
        # and 60s for ACP startup/transport. Tools and further steps may need more.
        minimum = (recoveries + 2) * wait + 60.0
        if not math.isfinite(minimum):
            raise ValueError("invalid runtime wait budget")
    except (ValueError, KeyError, TypeError):
        wait = recoveries = minimum = None
    effective = (requested_seconds if requested_seconds is not None
                 else max(2700.0, minimum or 0.0))
    status = ("unavailable" if minimum is None
              else "aligned" if effective >= minimum else "constrained")
    return {
        "mode": "auto" if requested_seconds is None else "explicit",
        "requested_task_seconds": requested_seconds,
        "effective_task_seconds": effective,
        "provider_stale_seconds": wait,
        "max_provider_stale_recoveries": recoveries,
        "startup_allowance_seconds": 60.0,
        "minimum_task_seconds": minimum,
        "status": status,
    }


def _stable_runtime_identity(runtime: Any) -> dict[str, Any] | None:
    if not isinstance(runtime, Mapping):
        return None
    python = runtime.get("python")
    box_agent = runtime.get("box_agent")
    if not isinstance(python, Mapping) or not isinstance(box_agent, Mapping):
        return None
    if (
        python.get("status") != "available"
        or box_agent.get("version_status") != "available"
        or box_agent.get("git_commit_status") != "available"
    ):
        return None
    python_version = python.get("version")
    python_implementation = python.get("implementation")
    box_agent_version = box_agent.get("version")
    git_commit = box_agent.get("git_commit")
    if not all(
        isinstance(value, str) and value
        for value in (
            python_version,
            python_implementation,
            box_agent_version,
            git_commit,
        )
    ):
        return None
    return {
        "python": {
            "version": python_version,
            "implementation": python_implementation,
            "status": python["status"],
        },
        "box_agent": {
            "version": box_agent_version,
            "version_status": box_agent["version_status"],
            "git_commit": git_commit,
            "git_commit_status": box_agent["git_commit_status"],
        },
    }


def _validate_case_id(value: Any, line_number: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"line {line_number} has invalid case id")
    return value


def _validate_input_files(
    dataset_root: Path,
    case_id: str,
    value: Any,
) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"case {case_id} input_files must be a list of paths")
    root = dataset_root.resolve()
    destination_names: set[str] = set()
    for relative_path in value:
        if not relative_path or "\x00" in relative_path:
            raise ValueError(f"case {case_id} has invalid input path")
        source = (root / relative_path).resolve()
        try:
            source.relative_to(root)
        except ValueError as error:
            raise ValueError(
                f"case {case_id} input escapes dataset root: {relative_path}"
            ) from error
        if not source.is_file():
            raise FileNotFoundError(f"missing input file: {source}")
        if source.name in destination_names:
            raise ValueError(
                f"case {case_id} has duplicate input basename: {source.name}"
            )
        destination_names.add(source.name)
    return list(value)


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """Load and fully validate one JSONL dataset before any attempt starts."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"dataset is not valid UTF-8: {path}") from error
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on line {line_number}: {error.msg}") from error
        if not isinstance(value, dict):
            raise ValueError(f"line {line_number} must contain a JSON object")
        case_id = _validate_case_id(value.get("id"), line_number)
        if case_id in seen_ids:
            raise ValueError(f"duplicate case id: {case_id}")
        query = value.get("query")
        if not isinstance(query, str) or not query:
            raise ValueError(f"case {case_id} query must be a non-empty string")
        CaseMetadata.from_record(value)
        setup = WorkspaceSetup.from_record(value)
        for item in (*setup.files, *setup.dirty_files):
            source = (path.parent / setup.source(item)).resolve()
            if not source.is_relative_to(path.parent.resolve()) or not source.is_file():
                raise ValueError(f"case {case_id}: workspace_setup file missing or outside pack")
        normalized = dict(value)
        normalized["input_files"] = _validate_input_files(
            path.parent,
            case_id,
            value.get("input_files"),
        )
        seen_ids.add(case_id)
        records.append(normalized)
    if not records:
        raise ValueError("dataset contains no cases")
    return records


def _select_records(
    records: Sequence[dict[str, Any]], case_ids: Sequence[str]
) -> list[dict[str, Any]]:
    if not case_ids:
        return list(records)
    requested: set[str] = set()
    for case_id in case_ids:
        if case_id in requested:
            raise ValueError(f"duplicate selected case id: {case_id}")
        requested.add(case_id)
    available = {record["id"]: record for record in records}
    for case_id in case_ids:
        if case_id not in available:
            raise ValueError(f"unknown case id: {case_id}")
    return [available[case_id] for case_id in case_ids]


def _existing_run_id(output_dir: Path) -> str | None:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    manifest = _read_mapping(manifest_path)
    if manifest is None or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"invalid batch manifest: {manifest_path}")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(f"batch manifest has no valid run_id: {manifest_path}")
    return run_id


def _validate_case_storage(output_dir: Path, case_id: str) -> None:
    cases_dir = output_dir / "cases"
    case_dir = cases_dir / case_id
    attempts_dir = case_dir / "attempts"
    for path in (cases_dir, case_dir, attempts_dir):
        if path.is_symlink():
            raise LatestIndexError(
                f"case {case_id} storage path contains a symlink: {path.name}"
            )
    if not case_dir.exists():
        return
    try:
        resolved_cases = cases_dir.resolve(strict=True)
        resolved_case = case_dir.resolve(strict=True)
    except OSError as error:
        raise LatestIndexError(f"case {case_id} storage path is unreadable") from error
    if resolved_case.parent != resolved_cases:
        raise LatestIndexError(f"case {case_id} is not a direct child of cases")
    if attempts_dir.exists():
        try:
            resolved_attempts = attempts_dir.resolve(strict=True)
        except OSError as error:
            raise LatestIndexError(
                f"case {case_id} attempts path is unreadable"
            ) from error
        if resolved_attempts.parent != resolved_case:
            raise LatestIndexError(
                f"case {case_id} attempts is not a direct child of the case"
            )


def _terminal_attempt(
    output_dir: Path,
    run_id: str,
    case_id: str,
    current_fingerprint: Mapping[str, Any],
    current_runtime: Mapping[str, Any],
    model_config_sha256: str,
    timeout_budget: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    _validate_case_storage(output_dir, case_id)
    case_dir = output_dir / "cases" / case_id
    latest_path = case_dir / "latest.json"
    if case_dir.is_symlink() or latest_path.is_symlink():
        raise LatestIndexError(f"case {case_id} latest path contains a symlink")
    if not latest_path.exists():
        return None
    latest = _read_mapping(latest_path)
    if latest is None:
        return None
    attempt_id = latest.get("attempt_id")
    relative_path = latest.get("path")
    if (
        not isinstance(attempt_id, str)
        or ATTEMPT_ID_PATTERN.fullmatch(attempt_id) is None
    ):
        raise LatestIndexError(f"case {case_id} latest has invalid attempt id")
    expected_path = f"attempts/{attempt_id}"
    if relative_path != expected_path or Path(relative_path).is_absolute():
        raise LatestIndexError(
            f"case {case_id} latest attempt path is not a direct child"
        )
    attempts_dir = case_dir / "attempts"
    attempt_dir = attempts_dir / attempt_id
    if attempts_dir.is_symlink() or attempt_dir.is_symlink():
        raise LatestIndexError(
            f"case {case_id} latest attempt path contains a symlink"
        )
    try:
        resolved_attempts = attempts_dir.resolve(strict=True)
        resolved_attempt = attempt_dir.resolve(strict=True)
    except OSError as error:
        raise LatestIndexError(f"case {case_id} latest attempt is missing") from error
    if resolved_attempt.parent != resolved_attempts:
        raise LatestIndexError(
            f"case {case_id} latest attempt is not a direct child"
        )
    manifest_path = attempt_dir / "manifest.json"
    run_path = attempt_dir / "run.json"
    if manifest_path.is_symlink() or run_path.is_symlink():
        raise LatestIndexError(f"case {case_id} latest metadata contains a symlink")
    manifest = _read_mapping(manifest_path)
    result = _read_mapping(run_path)
    if manifest is None or result is None:
        return None
    if manifest.get("status") not in TERMINAL_ATTEMPT_STATUSES:
        return None
    expected_identity = {
        "run_id": run_id,
        "case_id": case_id,
        "attempt_id": attempt_id,
    }
    if any(manifest.get(key) != value for key, value in expected_identity.items()):
        return None
    if any(result.get(key) != value for key, value in expected_identity.items()):
        return None
    if result.get("case_fingerprint") != current_fingerprint:
        return None
    if result.get("model_config_sha256") != model_config_sha256:
        return None
    if result.get("evaluation_timeout_budget", result.get("timeout_budget")) != timeout_budget:
        return None
    stored_runtime = _stable_runtime_identity(result.get("runtime"))
    comparable_runtime = _stable_runtime_identity(current_runtime)
    if stored_runtime is None or comparable_runtime is None:
        return None
    if stored_runtime != comparable_runtime:
        return None
    return _case_summary(result, "skipped_terminal")


def _case_summary(result: Mapping[str, Any], disposition: str) -> dict[str, Any]:
    case_id = result.get("case_id")
    attempt_id = result.get("attempt_id")
    stderr_counts = result.get("stderr_counts")
    return {
        "case_id": case_id,
        "disposition": disposition,
        "attempt_id": attempt_id,
        "attempt_path": f"cases/{case_id}/attempts/{attempt_id}",
        "acp_status": result.get("acp_status"),
        "process_exit_code": result.get("process_exit_code"),
        "stderr_counts": (
            dict(stderr_counts) if isinstance(stderr_counts, Mapping) else {}
        ),
        "completeness_status": result.get("completeness_status"),
        "case_fingerprint": result.get("case_fingerprint"),
        "runtime": result.get("runtime"),
    }


def _counts(cases: Sequence[Mapping[str, Any]], selected: int) -> dict[str, int]:
    counts = {
        "selected": selected,
        "executed": 0,
        "skipped_terminal": 0,
        "acp_completed": 0,
        "acp_failed": 0,
        "complete": 0,
        "complete_with_warnings": 0,
        "incomplete": 0,
        "corrupt": 0,
    }
    for case in cases:
        disposition = case.get("disposition")
        if disposition == "skipped_terminal":
            counts["skipped_terminal"] += 1
        elif disposition in {"executed", "indexing_error"}:
            counts["executed"] += 1
        if case.get("acp_status") == "completed":
            counts["acp_completed"] += 1
        else:
            counts["acp_failed"] += 1
        completeness = case.get("completeness_status")
        if completeness in COMPLETENESS_STATUSES:
            counts[completeness] += 1
    return counts


def _successful(case: Mapping[str, Any]) -> bool:
    return (
        case.get("disposition") in {"executed", "skipped_terminal"}
        and case.get("acp_status") == "completed"
        and case.get("completeness_status")
        in {"complete", "complete_with_warnings"}
    )


def _python_executable(repo_root: Path) -> str:
    relative = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    candidate = repo_root / ".venv" / relative[0] / relative[1]
    return str(candidate) if candidate.is_file() else sys.executable


def _write_attempt_batch_metadata(
    output_dir: Path,
    result: RunResult,
    case_fingerprint: Mapping[str, Any],
    expected_fingerprint: Mapping[str, Any],
    runtime: Mapping[str, Any],
    model_config_sha256: str,
    evaluation_timeout_budget: Mapping[str, Any] | None = None,
) -> None:
    attempt_dir = (
        output_dir / "cases" / result.case_id / "attempts" / result.attempt_id
    )
    run_path = attempt_dir / "run.json"
    run_document = _read_mapping(run_path)
    if run_document is None:
        raise ValueError(f"attempt run metadata is unreadable: {run_path}")
    run_document["case_fingerprint"] = dict(case_fingerprint)
    run_document["runtime"] = dict(runtime)
    run_document["model_config_sha256"] = model_config_sha256
    if evaluation_timeout_budget is not None:
        run_document["evaluation_timeout_budget"] = dict(evaluation_timeout_budget)
    matches = case_fingerprint == expected_fingerprint
    run_document["input_consistency"] = {
        "status": "matched" if matches else "mismatch",
        "expected_sha256": expected_fingerprint.get("sha256"),
        "actual_sha256": case_fingerprint.get("sha256"),
    }
    if not matches:
        message = "input fingerprint mismatch between source and copied workspace"
        prior_error = run_document.get("error")
        run_document["error"] = (
            f"{prior_error}; {message}" if isinstance(prior_error, str) else message
        )
        run_document["completeness_status"] = "corrupt"
        completeness_path = attempt_dir / "completeness.json"
        completeness = _read_mapping(completeness_path)
        if completeness is None:
            raise ValueError(
                f"attempt completeness metadata is unreadable: {completeness_path}"
            )
        issues = completeness.get("issues")
        normalized_issues = list(issues) if isinstance(issues, list) else []
        if "input_fingerprint_mismatch" not in normalized_issues:
            normalized_issues.append("input_fingerprint_mismatch")
        completeness["status"] = "corrupt"
        completeness["issues"] = normalized_issues
        manifest_path = attempt_dir / "manifest.json"
        manifest = _read_mapping(manifest_path)
        if manifest is None:
            raise ValueError(
                f"attempt manifest metadata is unreadable: {manifest_path}"
            )
        manifest["status"] = "incomplete"
        atomic_write_json(completeness_path, completeness)
        atomic_write_json(manifest_path, manifest)
    atomic_write_json(run_path, run_document)


def _copied_case_fingerprint(
    output_dir: Path,
    record: Mapping[str, Any],
    result: RunResult,
) -> dict[str, Any]:
    snapshot_path = (
        output_dir
        / "cases"
        / result.case_id
        / "attempts"
        / result.attempt_id
        / "files-before.json"
    )
    snapshot = _read_mapping(snapshot_path)
    files = snapshot.get("files") if snapshot is not None else None
    if not isinstance(files, list):
        raise ValueError(f"attempt input snapshot is unreadable: {snapshot_path}")
    by_path: dict[str, Mapping[str, Any]] = {}
    for item in files:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        if isinstance(path, str):
            by_path[path] = item
    input_files: list[dict[str, str]] = []
    for relative_path in record["input_files"]:
        copied_path = Path(relative_path).name
        item = by_path.get(copied_path)
        digest = item.get("sha256") if item is not None else None
        if (
            item is None
            or item.get("kind") != "file"
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValueError(
                f"copied input is absent from snapshot: {relative_path}"
            )
        input_files.append({"path": relative_path, "sha256": digest})
    fingerprint_payload = {
        "record": dict(record),
        "input_files": input_files,
    }
    return {
        "algorithm": "sha256",
        "sha256": _canonical_sha256(fingerprint_payload),
        "record_sha256": _canonical_sha256(dict(record)),
        "input_files": input_files,
    }


def _batch_error(
    case_id: str, disposition: str, error: BaseException
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "disposition": disposition,
        "attempt_id": None,
        "attempt_path": None,
        "acp_status": "error",
        "process_exit_code": None,
        "stderr_counts": {},
        "completeness_status": "incomplete",
        "case_fingerprint": None,
        "runtime": None,
        "error": f"{type(error).__name__}: {error}",
    }


class _RateLimitCooldown:
    """One batch-wide gate; in-flight attempts finish, queued work cools down."""

    def __init__(self, base_seconds: float):
        self.base_seconds = base_seconds
        self.not_before = 0.0
        self.failures = 0
        self.stopped = False
        self.lock = Lock()

    def defer(self) -> float:
        with self.lock:
            self.failures += 1
            delay = min(600.0, self.base_seconds * (2 ** min(self.failures - 1, 10)))
            self.not_before = max(self.not_before, monotonic() + delay)
            return delay

    def stop(self) -> None:
        with self.lock:
            self.stopped = True

    def wait(self, deadline: float) -> tuple[float, str | None]:
        started = monotonic()
        waited = False
        while True:
            with self.lock:
                now = monotonic()
                reason = 'circuit_open' if self.stopped else None
                delay = max(0.0, self.not_before - now)
            if reason or now + delay >= deadline:
                return (now - started if waited else 0.0), reason or 'evaluation_deadline'
            if not delay:
                return (now - started if waited else 0.0), None
            sleep(min(delay, 1.0))
            waited = True


def _provider_rate_limit(run: Mapping[str, Any]) -> bool:
    metadata = run.get('response_metadata') or {}
    details = metadata.get('errorDetails') or {}
    return (run.get('acp_status') == 'error' and details.get('source') == 'llm_provider'
            and details.get('httpStatus') == 429)


def _rate_limit_before_tools(attempt: Path) -> bool:
    """Replay only a fully captured, explicitly retryable provider 429 with no effects."""
    run = _read_mapping(attempt / 'run.json') or {}
    details = (run.get('response_metadata') or {}).get('errorDetails') or {}
    if not (_provider_rate_limit(run) and details.get('retryable') is True
            and run.get('completeness_status') in {'complete', 'complete_with_warnings'}
            and run.get('input_consistency', {}).get('status') == 'matched'
            and run.get('permission_request_count', 0) == 0):
        return False
    paths = list((attempt / 'agent').glob('*.jsonl'))
    if not paths:
        return False
    requests = 0
    errors = []
    turn_errors = []
    try:
        for path in paths:
            with path.open() as stream:
                for line in stream:
                    event = json.loads(line)
                    if event.get('event') in {'tool.request', 'tool.response', 'llm.response'}:
                        return False
                    requests += event.get('event') == 'llm.request'
                    if event.get('event') == 'llm.error':
                        errors.append(event.get('data') or {})
                    if event.get('event') == 'turn.error':
                        turn_errors.append(event.get('data') or {})
        if any(e.get('partial_content') or e.get('partial_thinking') for e in errors):
            return False
        if run.get('assistant_text_length') == 0:
            return requests > 0
        # ACP renders fatal ErrorEvents as an assistant message. Accept only
        # that exact notice, corroborated by a single empty provider attempt.
        message = (run.get('response_metadata') or {}).get('error')
        notice = f'Error: {message}'
        return (requests == 1 and len(errors) == 1
                and errors[0].get('partial_content') == ''
                and 'partial_thinking' in errors[0]
                and len(turn_errors) == 1 and isinstance(message, str) and bool(message)
                and turn_errors[0].get('message') == message
                and turn_errors[0].get('error_details') == details
                and run.get('assistant_text_length') == len(notice)
                and (attempt / 'assistant.txt').read_text(encoding='utf-8') == notice)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def run_batch(
    dataset: Path,
    output_dir: Path,
    repo_root: Path,
    timeout_seconds: float | None,
    parallelism: int,
    case_ids: Sequence[str],
    *,
    retry_terminal: bool = False,
    effect_eval_url: str | None = None,
    effect_eval_timeout_seconds: float = 180.0,
    model: str | None = None,
    model_max_tokens: int | None = None,
    model_binding: Mapping[str, Any] | None = None,
    rate_limit_retries: int = 2,
    rate_limit_backoff_seconds: float = 120.0,
) -> int:
    """Run selected cases, atomically index attempts, and return a process code."""

    if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("timeout_seconds must be finite and greater than zero")
    if parallelism < 1:
        raise ValueError("parallelism must be at least one")
    if type(rate_limit_retries) is not int or not 0 <= rate_limit_retries <= 5:
        raise ValueError('rate_limit_retries must be an integer between 0 and 5')
    if not math.isfinite(rate_limit_backoff_seconds) or not 0 < rate_limit_backoff_seconds <= 600:
        raise ValueError('rate_limit_backoff_seconds must be finite and in (0, 600]')
    if not math.isfinite(effect_eval_timeout_seconds) or effect_eval_timeout_seconds <= 0:
        raise ValueError(
            "effect_eval_timeout_seconds must be finite and greater than zero"
        )
    if model_max_tokens is not None and model_max_tokens < 1:
        raise ValueError("model_max_tokens must be at least one")
    if model_binding is not None:
        if model is not None:
            raise ValueError("model_binding cannot be combined with model")
        if (
            model_binding.get("source") != "builtin"
            or not isinstance(model_binding.get("model"), str)
            or not str(model_binding.get("model")).strip()
        ):
            raise ValueError("model_binding must contain a builtin model")
    # Freeze the requested model/routing settings before fingerprinting and
    # execution. A caller must not mutate a nested binding during the batch.
    model_binding = deepcopy(dict(model_binding)) if model_binding is not None else None
    model_config_sha256 = _canonical_sha256({
        "model": model,
        "model_max_tokens": model_max_tokens,
        "model_binding": model_binding,
    })
    dataset = Path(dataset).resolve()
    output_dir = Path(output_dir).resolve()
    repo_root = Path(repo_root).resolve()
    all_records = load_dataset(dataset)
    records = _select_records(all_records, case_ids)
    if not repo_root.is_dir():
        raise FileNotFoundError(f"repository root does not exist: {repo_root}")

    prior_run_id = _existing_run_id(output_dir)
    run_id = prior_run_id or new_run_id()
    python_executable = _python_executable(repo_root)
    runtime = _runtime_identity(repo_root, python_executable)
    timeout_budget = _timeout_budget(repo_root, python_executable, timeout_seconds)
    timeout_seconds = timeout_budget["effective_task_seconds"]
    if timeout_budget["status"] != "aligned":
        print(f"acp-eval: timeout budget {timeout_budget['status']}: "
              f"task={timeout_seconds:g}s, minimum={timeout_budget['minimum_task_seconds']}; "
              "timeouts do not establish task-quality failure", file=sys.stderr)
    case_fingerprints = {
        record["id"]: _case_fingerprint(record, dataset.parent)
        for record in all_records
    }
    dataset_fingerprint = _dataset_fingerprint(all_records, case_fingerprints)
    started_at = _iso_now()
    selected_ids = [record["id"] for record in records]
    selected_records = {record["id"]: record for record in records}
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "dataset": str(dataset),
        "repo_root": str(repo_root),
        "selected_case_ids": selected_ids,
        "timeout_seconds": timeout_seconds,
        "timeout_budget": timeout_budget,
        "parallelism": parallelism,
        "retry_terminal": retry_terminal,
        "rate_limit_retry_policy": {"max_retries": rate_limit_retries, "base_seconds": rate_limit_backoff_seconds,
                                    "max_delay_seconds": 600, "budget_scope": "case_including_backoff"},
        "dataset_fingerprint": dataset_fingerprint,
        "runtime": runtime,
        "model_config_sha256": model_config_sha256,
        "started_at": started_at,
        "finished_at": None,
    }
    atomic_write_json(output_dir / "manifest.json", manifest)

    summaries: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for record in records:
        try:
            _validate_case_storage(output_dir, record["id"])
            prior = None
            if not retry_terminal:
                prior = _terminal_attempt(
                    output_dir,
                    run_id,
                    record["id"],
                    case_fingerprints[record["id"]],
                    runtime,
                    model_config_sha256,
                    timeout_budget,
                )
        except LatestIndexError as error:
            summaries[record["id"]] = _batch_error(
                record["id"], "resume_index_error", error
            )
            continue
        if prior is None:
            pending.append(record)
        else:
            summaries[record["id"]] = prior

    config = CaseConfig(
        repo_root=repo_root,
        evaluation_dir=output_dir,
        dataset_root=dataset.parent,
        timeout_seconds=timeout_seconds,
        timeout_budget=timeout_budget,
        python_executable=python_executable,
        effect_eval_url=effect_eval_url,
        effect_eval_timeout_seconds=effect_eval_timeout_seconds,
        model=model,
        model_max_tokens=model_max_tokens,
        model_binding=dict(model_binding) if model_binding is not None else None,
    )
    cooldown = _RateLimitCooldown(rate_limit_backoff_seconds)

    def execute_attempt(record: dict[str, Any], attempt_config: CaseConfig) -> dict[str, Any]:
        case_id = record['id']
        try:
            result = run_case(record, attempt_config)
        except Exception as error:
            return _batch_error(case_id, 'batch_error', error)
        result_payload = result.to_dict()
        try:
            copied_fingerprint = _copied_case_fingerprint(
                output_dir,
                record=selected_records[case_id],
                result=result,
            )
        except Exception as error:
            case_summary = _case_summary(result_payload, "indexing_error")
            case_summary["error"] = f"{type(error).__name__}: {error}"
            return case_summary
        result_payload["case_fingerprint"] = copied_fingerprint
        result_payload["runtime"] = runtime
        mismatch = copied_fingerprint != case_fingerprints[case_id]
        if mismatch:
            result_payload["completeness_status"] = "corrupt"
        case_summary = _case_summary(result_payload, "executed")
        try:
            _write_attempt_batch_metadata(
                output_dir,
                result,
                copied_fingerprint,
                case_fingerprints[case_id],
                runtime,
                model_config_sha256,
                timeout_budget,
            )
            if mismatch:
                raise InputFingerprintMismatch(
                    "input fingerprint mismatch between source and copied workspace"
                )
            atomic_write_json(
                output_dir / "cases" / case_id / "latest.json",
                {
                    "attempt_id": result.attempt_id,
                    "path": f"attempts/{result.attempt_id}",
                },
            )
        except Exception as error:
            case_summary["disposition"] = "indexing_error"
            case_summary["error"] = f"{type(error).__name__}: {error}"
        return case_summary

    def execute_case(record: dict[str, Any]) -> dict[str, Any]:
        started = monotonic()
        deadline = started + timeout_seconds
        attempts: list[dict[str, Any]] = []
        wait_seconds = 0.0
        stop_reason = None
        while True:
            waited, stop_reason = cooldown.wait(deadline)
            wait_seconds += waited
            if stop_reason:
                break
            remaining = deadline - monotonic() if attempts or waited else timeout_seconds
            if remaining <= 0:
                stop_reason = 'evaluation_deadline'
                break
            attempt_budget = {**timeout_budget, 'effective_task_seconds': remaining}
            if timeout_budget['minimum_task_seconds'] is not None and remaining < timeout_budget['minimum_task_seconds']:
                attempt_budget['status'] = 'constrained'
            current = execute_attempt(record, replace(config, timeout_seconds=remaining, timeout_budget=attempt_budget))
            attempts.append(current)
            # Persist each attempt before waiting or retrying. Raw attempt files
            # and first-attempt failures remain available to scoring/billing.
            atomic_write_json(output_dir / 'cases' / record['id'] / 'retry-history.json', {
                'attempts': attempts, 'initial_timeout_budget': timeout_budget,
                'retry_wait_seconds': wait_seconds,
            })
            if (current['disposition'] != 'executed'
                    or not _rate_limit_before_tools(output_dir / current['attempt_path'])):
                stop_reason = 'not_retryable' if current['acp_status'] == 'error' else 'finished'
                if current.get('attempt_path') and _provider_rate_limit(
                        _read_mapping(output_dir / current['attempt_path'] / 'run.json') or {}):
                    # An unsafe-to-replay 429 still signals that queued work
                    # must stop; do not start fresh cases against that limit.
                    cooldown.stop()
                break
            if len(attempts) > rate_limit_retries:
                stop_reason = 'retry_limit'
                cooldown.stop()
                break
            cooldown.defer()
        final = dict(attempts[-1]) if attempts else _batch_error(
            record['id'], 'deferred_rate_limit', RuntimeError(stop_reason))
        final.update(attempts=attempts, retry_wait_seconds=wait_seconds,
                     evaluation_elapsed_seconds=monotonic() - started, retry_stop_reason=stop_reason)
        atomic_write_json(output_dir / 'cases' / record['id'] / 'retry-history.json', {
            'attempts': attempts, 'initial_timeout_budget': timeout_budget,
            'retry_wait_seconds': wait_seconds, 'evaluation_elapsed_seconds': final['evaluation_elapsed_seconds'],
            'stop_reason': stop_reason,
        })
        return final

    if pending:
        with ThreadPoolExecutor(max_workers=min(parallelism, len(pending))) as executor:
            futures = {executor.submit(execute_case, record): record['id'] for record in pending}
            for future in as_completed(futures):
                case_id = futures[future]
                try:
                    summaries[case_id] = future.result()
                except Exception as error:
                    summaries[case_id] = _batch_error(case_id, 'batch_error', error)

    ordered = [summaries[case_id] for case_id in selected_ids]
    counts = _counts(ordered, len(records))
    success = all(_successful(case) for case in ordered)
    finished_at = _iso_now()
    status = "completed" if success else "completed_with_failures"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "timeout_budget": timeout_budget,
        "started_at": started_at,
        "finished_at": finished_at,
        "dataset_fingerprint": dataset_fingerprint,
        "runtime": runtime,
        "model_config_sha256": model_config_sha256,
        "counts": counts,
        "attempt_counts": _counts([a for case in ordered for a in case.get('attempts', [])], len(records)),
        "initial_attempt_counts": _counts([case['attempts'][0] for case in ordered if case.get('attempts')], len(records)),
        "rate_limit_retry_policy": manifest['rate_limit_retry_policy'],
        "cases": ordered,
    }
    atomic_write_json(output_dir / "summary.json", summary)
    manifest["status"] = status
    manifest["finished_at"] = finished_at
    atomic_write_json(output_dir / "manifest.json", manifest)
    return 0 if success else 1
