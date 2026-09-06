"""Validated batch orchestration for offline ACP evaluation attempts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from acp_eval import SCHEMA_VERSION
from acp_eval.case_runner import CaseConfig, run_case
from acp_eval.ids import new_run_id
from acp_eval.models import RunResult
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
    candidate = repo_root / ".venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else sys.executable


def _write_attempt_batch_metadata(
    output_dir: Path,
    result: RunResult,
    case_fingerprint: Mapping[str, Any],
    expected_fingerprint: Mapping[str, Any],
    runtime: Mapping[str, Any],
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


def run_batch(
    dataset: Path,
    output_dir: Path,
    repo_root: Path,
    timeout_seconds: float,
    parallelism: int,
    case_ids: Sequence[str],
    *,
    retry_terminal: bool = False,
    effect_eval_url: str | None = None,
    effect_eval_timeout_seconds: float = 180.0,
    model: str | None = None,
    model_max_tokens: int | None = None,
    model_binding: Mapping[str, Any] | None = None,
) -> int:
    """Run selected cases, atomically index attempts, and return a process code."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    if parallelism < 1:
        raise ValueError("parallelism must be at least one")
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
        "parallelism": parallelism,
        "retry_terminal": retry_terminal,
        "dataset_fingerprint": dataset_fingerprint,
        "runtime": runtime,
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
        python_executable=python_executable,
        effect_eval_url=effect_eval_url,
        effect_eval_timeout_seconds=effect_eval_timeout_seconds,
        model=model,
        model_max_tokens=model_max_tokens,
        model_binding=dict(model_binding) if model_binding is not None else None,
    )
    if pending:
        with ThreadPoolExecutor(max_workers=min(parallelism, len(pending))) as executor:
            futures = {
                executor.submit(run_case, record, config): record["id"]
                for record in pending
            }
            for future in as_completed(futures):
                case_id = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    summaries[case_id] = _batch_error(case_id, "batch_error", error)
                    continue
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
                    summaries[case_id] = case_summary
                    continue
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
                summaries[case_id] = case_summary

    ordered = [summaries[case_id] for case_id in selected_ids]
    counts = _counts(ordered, len(records))
    success = all(_successful(case) for case in ordered)
    finished_at = _iso_now()
    status = "completed" if success else "completed_with_failures"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "dataset_fingerprint": dataset_fingerprint,
        "runtime": runtime,
        "counts": counts,
        "cases": ordered,
    }
    atomic_write_json(output_dir / "summary.json", summary)
    manifest["status"] = status
    manifest["finished_at"] = finished_at
    atomic_write_json(output_dir / "manifest.json", manifest)
    return 0 if success else 1
