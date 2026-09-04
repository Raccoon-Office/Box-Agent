from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import acp_eval.case_runner as case_runner_module
from acp_eval.case_runner import CaseConfig, run_case


REQUIRED_ATTEMPT_ENTRIES = {
    "acp-stdin.raw",
    "acp-stdout.raw",
    "agent",
    "artifacts.json",
    "assistant.txt",
    "completeness.json",
    "files-after.json",
    "files-before.json",
    "manifest.json",
    "process.jsonl",
    "protocol.jsonl",
    "run.json",
    "stderr.log",
    "workspace",
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_fake_repo(root: Path, mode: str) -> Path:
    package = root / "box_agent" / "acp"
    package.mkdir(parents=True)
    (root / "box_agent" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    fake_path = Path(__file__).with_name("fake_acp.py").resolve()
    (package / "server.py").write_text(
        "import runpy, sys\n"
        f"sys.argv = [{str(fake_path)!r}, {mode!r}]\n"
        f"runpy.run_path({str(fake_path)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return root


def make_config(tmp_path: Path, mode: str, timeout: float = 2.0) -> CaseConfig:
    repo_root = make_fake_repo(tmp_path / "repo", mode)
    evaluation_dir = tmp_path / "outputs" / "evaluation"
    evaluation_dir.mkdir(parents=True)
    (evaluation_dir / "manifest.json").write_text(
        json.dumps({"schema_version": "box-agent-acp-eval/v1", "run_id": "run-test"}),
        encoding="utf-8",
    )
    dataset_root = tmp_path / "dataset"
    source = dataset_root / "input_files" / mode / "payload.txt"
    source.parent.mkdir(parents=True)
    source.write_text("input body\n", encoding="utf-8")
    return CaseConfig(
        repo_root=repo_root,
        evaluation_dir=evaluation_dir,
        dataset_root=dataset_root,
        timeout_seconds=timeout,
        python_executable=sys.executable,
    )


def run_mode(tmp_path: Path, mode: str, timeout: float = 2.0):
    config = make_config(tmp_path, mode, timeout)
    record = {
        "id": mode,
        "query": f"execute {mode}",
        "domain": "test",
        "input_files": [f"input_files/{mode}/payload.txt"],
    }
    result = run_case(record, config)
    attempts = list(
        (config.evaluation_dir / "cases" / mode / "attempts").glob("attempt-*")
    )
    assert len(attempts) == 1
    return result, attempts[0], config, record


def test_effect_evaluation_runs_after_original_attempt_is_terminal(
    monkeypatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path, "normal")
    config = CaseConfig(
        repo_root=config.repo_root,
        evaluation_dir=config.evaluation_dir,
        dataset_root=config.dataset_root,
        timeout_seconds=config.timeout_seconds,
        python_executable=config.python_executable,
        effect_eval_url="http://127.0.0.1:8766",
    )
    record = {
        "id": "normal",
        "query": "execute normal",
        "input_files": ["input_files/normal/payload.txt"],
    }
    observed: dict[str, object] = {}

    def fake_evaluate(attempt: Path, received_record, received_config) -> None:
        observed["manifest"] = read_json(attempt / "manifest.json")["status"]
        observed["completeness"] = read_json(attempt / "completeness.json")["status"]
        observed["record"] = received_record
        observed["url"] = received_config.service_url

    monkeypatch.setattr(case_runner_module, "evaluate_attempt", fake_evaluate)
    result = run_case(record, config)

    assert observed == {
        "manifest": "finished",
        "completeness": "complete",
        "record": record,
        "url": "http://127.0.0.1:8766",
    }
    assert result.acp_status == "completed"
    assert result.completeness_status == "complete"


def test_effect_evaluation_failure_cannot_change_case_result(
    monkeypatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path, "normal")
    config = CaseConfig(
        repo_root=config.repo_root,
        evaluation_dir=config.evaluation_dir,
        dataset_root=config.dataset_root,
        timeout_seconds=config.timeout_seconds,
        python_executable=config.python_executable,
        effect_eval_url="http://127.0.0.1:8766",
    )
    monkeypatch.setattr(
        case_runner_module,
        "evaluate_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    result = run_case(
        {
            "id": "normal",
            "query": "execute normal",
            "input_files": ["input_files/normal/payload.txt"],
        },
        config,
    )

    assert result.acp_status == "completed"
    assert result.completeness_status == "complete"


def test_normal_case_writes_self_contained_complete_attempt(tmp_path: Path) -> None:
    result, attempt, config, record = run_mode(tmp_path, "normal")

    assert {path.name for path in attempt.iterdir()} == REQUIRED_ATTEMPT_ENTRIES
    case_dir = config.evaluation_dir / "cases" / "normal"
    assert read_json(case_dir / "input.json") == record
    assert (attempt / "workspace" / "payload.txt").read_text() == "input body\n"
    assert (attempt / "workspace" / "output" / "answer.txt").read_text() == (
        "artifact body\n"
    )

    protocol = read_jsonl(attempt / "protocol.jsonl")
    sent = [entry["message"] for entry in protocol if entry["direction"] == "sent"]
    received = [
        entry["message"] for entry in protocol if entry["direction"] == "received"
    ]
    assert [message.get("method") for message in sent[:3]] == [
        "initialize",
        "session/new",
        "session/prompt",
    ]
    assert sent[3] == {
        "jsonrpc": "2.0",
        "id": 81,
        "result": {"outcome": {"outcome": "cancelled"}},
    }
    assert (attempt / "acp-stdin.raw").read_bytes() == b"".join(
        json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for message in sent
    )
    assert (attempt / "acp-stdout.raw").read_bytes() == b"".join(
        json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for message in received
    )

    trace_files = list((attempt / "agent").glob("*.jsonl"))
    assert [path.name for path in trace_files] == ["eval-acp-normal.jsonl"]
    trace = read_jsonl(trace_files[0])
    assert trace[0]["session_id"] == "eval-acp-normal"
    assert trace[0]["acp_session_id"] == "sess-0-fake"
    assert not list(config.repo_root.rglob("eval-acp-normal.jsonl"))

    before = read_json(attempt / "files-before.json")
    after = read_json(attempt / "files-after.json")
    assert [item["path"] for item in before["files"]] == ["payload.txt"]
    assert [item["path"] for item in after["files"]] == [
        "output/answer.txt",
        "payload.txt",
    ]
    artifacts = read_json(attempt / "artifacts.json")
    assert artifacts["artifact_events"][0]["exists"] is True
    assert artifacts["final_files"][0]["path"] == "output/answer.txt"
    assert (attempt / "assistant.txt").read_text(encoding="utf-8") == "hello world"
    run_document = read_json(attempt / "run.json")
    assert run_document["session_id"] == "eval-acp-normal"
    assert run_document["acp_session_id"] == "sess-0-fake"
    assert run_document["turn_id"] == "eval-acp-normal-turn-1"
    assert run_document["permission_request_count"] == 1
    assert run_document["token_usage"] == {
        "promptTokens": 10,
        "completionTokens": 2,
        "totalTokens": 12,
    }
    assert run_document["assistant_text_length"] == 11
    completeness_status = read_json(attempt / "completeness.json")["status"]
    assert completeness_status == "complete"
    assert run_document["completeness_status"] == completeness_status
    assert result.completeness_status == completeness_status
    assert read_json(attempt / "manifest.json")["status"] == "finished"
    assert result.acp_status == "completed"

    session_new = next(message for message in received if message.get("id") == 2)
    assert session_new["result"]["sessionId"] == "sess-0-fake"
    prompt = next(message for message in sent if message.get("method") == "session/prompt")
    assert prompt["params"]["sessionId"] == "sess-0-fake"
    updates = [
        message for message in received if message.get("method") == "session/update"
    ]
    assert updates
    assert {message["params"]["sessionId"] for message in updates} == {
        "sess-0-fake"
    }
    final_response = next(message for message in received if message.get("id") == 3)
    assert final_response["result"]["_meta"]["usage"]["sessionId"] == (
        "eval-acp-normal"
    )


def test_selected_model_is_bound_on_the_acp_session(tmp_path: Path) -> None:
    config = replace(
        make_config(tmp_path, "normal"),
        model="sn-deepseek-v4-pro",
        model_max_tokens=100000,
    )
    record = {
        "id": "normal",
        "query": "execute normal",
        "input_files": ["input_files/normal/payload.txt"],
    }

    result = run_case(record, config)

    attempt = next(
        (config.evaluation_dir / "cases/normal/attempts").glob("attempt-*")
    )
    sent = [
        entry["message"]
        for entry in read_jsonl(attempt / "protocol.jsonl")
        if entry["direction"] == "sent"
    ]
    session_new = next(
        message for message in sent if message.get("method") == "session/new"
    )
    assert session_new["params"]["_meta"]["llm_binding"] == {
        "source": "builtin",
        "model": "sn-deepseek-v4-pro",
        "maxTokens": 100000,
    }
    assert session_new["params"]["_meta"]["enableBuiltinModelRouting"] is True
    assert result.acp_status == "completed"


def test_auto_model_binding_is_resolved_per_task_before_acp_session(tmp_path: Path) -> None:
    candidates = [
        {
            "model": "sn-sensenova-6-8-flash-lite",
            "tags": ["general", "fast"],
            "abilityLevel": 1,
            "maxTokens": 63999,
        },
        {
            "model": "sn-deepseek-v4-pro",
            "tags": ["presentation", "analysis"],
            "abilityLevel": 3,
            "maxTokens": 100000,
        },
    ]
    config = replace(
        make_config(tmp_path, "normal"),
        model_binding={
            "source": "builtin",
            "model": "sn-sensenova-6-8-flash-lite",
            "evaluationMode": "auto",
            "autoRouting": {"models": candidates},
        },
    )
    record = {
        "id": "normal",
        "query": "请生成一份项目汇报 PPT",
        "input_files": ["input_files/normal/payload.txt"],
    }

    result = run_case(record, config)

    attempt = next(
        (config.evaluation_dir / "cases/normal/attempts").glob("attempt-*")
    )
    sent = [
        entry["message"]
        for entry in read_jsonl(attempt / "protocol.jsonl")
        if entry["direction"] == "sent"
    ]
    session_new = next(
        message for message in sent if message.get("method") == "session/new"
    )
    binding = session_new["params"]["_meta"]["llm_binding"]
    assert binding["model"] == "sn-deepseek-v4-pro"
    assert binding["maxTokens"] == 100000
    assert binding["autoRouting"] == {"models": candidates}
    assert "evaluationMode" not in binding
    assert result.acp_status == "completed"


def test_malformed_frame_remains_in_raw_and_marks_attempt_corrupt(tmp_path: Path) -> None:
    result, attempt, _, _ = run_mode(tmp_path, "malformed")

    assert b'{"method": broken}\n' in (attempt / "acp-stdout.raw").read_bytes()
    assert all(
        entry["message"] != {"method": "broken"}
        for entry in read_jsonl(attempt / "protocol.jsonl")
    )
    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "corrupt"
    assert completeness["parse_errors"][0]["raw_size"] == len(b'{"method": broken}\n')
    assert result.completeness_status == "corrupt"


def test_timeout_records_cancel_shutdown_and_terminal_stream_events(tmp_path: Path) -> None:
    result, attempt, _, _ = run_mode(tmp_path, "timeout", timeout=0.1)

    protocol = read_jsonl(attempt / "protocol.jsonl")
    assert any(
        entry["direction"] == "sent"
        and entry["message"].get("method") == "session/cancel"
        for entry in protocol
    )
    process_events = read_jsonl(attempt / "process.jsonl")
    names = [event["event"] for event in process_events]
    assert "attempt.timeout" in names
    assert "process.exited" in names
    assert {event.get("stream") for event in process_events if event["event"] == "stream.eof"} == {
        "stdout",
        "stderr",
    }
    assert result.acp_status == "timeout"
    assert read_json(attempt / "manifest.json")["status"] == "incomplete"


def test_slow_drip_without_newline_uses_one_absolute_attempt_deadline(
    tmp_path: Path,
) -> None:
    started = time.monotonic()

    result, attempt, _, _ = run_mode(tmp_path, "slow-drip", timeout=0.15)

    elapsed = time.monotonic() - started
    assert elapsed < 1.2
    assert result.acp_status == "timeout"
    raw = (attempt / "acp-stdout.raw").read_bytes()
    assert raw.endswith(b"{")
    assert b"\n" not in raw.rsplit(b"\n", 1)[-1]
    assert read_json(attempt / "completeness.json")["status"] == "corrupt"


def test_trailing_stderr_is_drained_and_classified(tmp_path: Path) -> None:
    result, attempt, _, _ = run_mode(tmp_path, "trailing-stderr")

    assert (attempt / "stderr.log").read_text(encoding="utf-8") == (
        "2026-08-21T12:00:00Z [WARNING] trailing diagnostic\n"
    )
    assert result.stderr_counts == {"error": 0, "timeout": 0, "warning": 1}
    assert read_json(attempt / "run.json")["stderr_counts"]["warning"] == 1


def test_missing_agent_trace_marks_collection_incomplete(tmp_path: Path) -> None:
    result, attempt, _, _ = run_mode(tmp_path, "missing-trace")

    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "incomplete"
    assert "agent_trace_missing" in completeness["issues"]
    assert result.acp_status == "completed"
    assert result.completeness_status == "incomplete"


def test_large_stdout_frame_does_not_deadlock_or_exceed_reader_limit(tmp_path: Path) -> None:
    result, attempt, _, _ = run_mode(tmp_path, "large-frame")

    assert result.acp_status == "completed"
    assert len((attempt / "assistant.txt").read_text(encoding="utf-8")) == 5 * 1024 * 1024
    assert (attempt / "acp-stdout.raw").stat().st_size > 5 * 1024 * 1024
    assert read_json(attempt / "completeness.json")["status"] == "complete"


def test_descendant_inheriting_pipes_is_killed_without_hanging_stream_finalization(
    tmp_path: Path,
) -> None:
    started = time.monotonic()

    result, attempt, _, _ = run_mode(tmp_path, "descendant-pipes")

    elapsed = time.monotonic() - started
    events = read_jsonl(attempt / "process.jsonl")
    assert elapsed < 2.0
    assert any(
        event["event"] == "signal.sent"
        and event.get("target") == "process_group"
        and event.get("signal") == "SIGKILL"
        for event in events
    )
    assert result.acp_status == "completed"
    assert result.completeness_status == "complete"


@pytest.mark.asyncio
async def test_forced_stream_finalization_records_cancelled_stream_error(
    tmp_path: Path,
) -> None:
    stream = case_runner_module.asyncio.StreamReader()
    protocol = case_runner_module.ProtocolRecorder(
        tmp_path,
        wall_clock=lambda: case_runner_module.datetime.now(
            case_runner_module.timezone.utc
        ),
        monotonic_ns=lambda: 1,
    )
    state = case_runner_module._AttemptState()
    recorder = case_runner_module.ProcessRecorder(tmp_path / "process.jsonl")
    stdout_reader = case_runner_module._ACPStdoutReader(stream, protocol)
    task = case_runner_module.asyncio.create_task(
        case_runner_module._drain_stdout(
            stdout_reader,
            case_runner_module.ACPAccumulator(),
            recorder,
            state,
        )
    )

    await case_runner_module._finish_stream_tasks(
        [task],
        2_147_483_647,
        recorder,
        state,
    )

    events = read_jsonl(tmp_path / "process.jsonl")
    assert events[-1]["event"] == "stream.error"
    assert events[-1]["stream"] == "stdout"
    assert events[-1]["cancelled"] is True
    assert state.collection_error is True


@pytest.mark.parametrize(
    ("mode", "expected_issue"),
    [
        ("invalid-trace", "agent_trace_invalid"),
        ("empty-trace", "agent_trace_invalid"),
        ("raw-mismatch", "raw_stdout_protocol_mismatch"),
    ],
)
def test_post_write_validation_detects_corrupt_evidence(
    tmp_path: Path,
    mode: str,
    expected_issue: str,
) -> None:
    result, attempt, _, _ = run_mode(tmp_path, mode)

    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "corrupt"
    assert expected_issue in completeness["issues"]
    assert result.completeness_status == "corrupt"


def test_post_write_validation_detects_truncated_protocol_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_build_inventory = case_runner_module.build_artifact_inventory

    def truncate_after_inventory(protocol_path: Path, workspace: Path):
        inventory = original_build_inventory(protocol_path, workspace)
        protocol_path.write_bytes(b'{"truncated":')
        return inventory

    monkeypatch.setattr(
        case_runner_module,
        "build_artifact_inventory",
        truncate_after_inventory,
    )

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "corrupt"
    assert "protocol_jsonl_invalid" in completeness["issues"]
    assert result.completeness_status == "corrupt"


def test_post_write_validation_detects_snapshot_hash_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_snapshot = case_runner_module.write_snapshot

    def corrupt_after_snapshot(root: Path, destination: Path):
        records = original_write_snapshot(root, destination)
        if destination.name == "files-after.json":
            document = read_json(destination)
            document["files"][0]["sha256"] = "0" * 64
            destination.write_text(json.dumps(document), encoding="utf-8")
        return records

    monkeypatch.setattr(case_runner_module, "write_snapshot", corrupt_after_snapshot)

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "corrupt"
    assert "snapshot_hash_mismatch:output/answer.txt" in completeness["issues"]
    assert result.completeness_status == "corrupt"


def test_post_write_validation_detects_run_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_atomic_write_json = case_runner_module.atomic_write_json

    def corrupt_run_identity(path: Path, payload: dict[str, Any]) -> None:
        document = dict(payload)
        if path.name == "run.json":
            document["attempt_id"] = "attempt-wrong"
        original_atomic_write_json(path, document)

    monkeypatch.setattr(case_runner_module, "atomic_write_json", corrupt_run_identity)

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "corrupt"
    assert "identity_mismatch:run.json:attempt_id" in completeness["issues"]
    assert result.completeness_status == "corrupt"


@pytest.mark.parametrize(
    ("mode", "expected_issue"),
    [
        (
            "mismatch-upstream-session",
            "identity_mismatch:protocol:upstream_session_id",
        ),
        (
            "mismatch-update-session",
            "identity_mismatch:protocol:update_acp_session_id",
        ),
        (
            "mismatch-trace-session",
            "identity_mismatch:agent_trace:session_id",
        ),
        (
            "mismatch-trace-acp-session",
            "identity_mismatch:agent_trace:acp_session_id",
        ),
    ],
)
def test_dual_session_identity_mismatches_are_reported_precisely(
    tmp_path: Path,
    mode: str,
    expected_issue: str,
) -> None:
    result, attempt, _, _ = run_mode(tmp_path, mode)

    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "corrupt"
    assert expected_issue in completeness["issues"]
    assert result.completeness_status == "corrupt"


@pytest.mark.parametrize("document_name", ["manifest.json", "run.json"])
def test_final_validation_detects_invalid_identity_document_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document_name: str,
) -> None:
    original_atomic_write_json = case_runner_module.atomic_write_json

    def corrupt_final_document(path: Path, payload: dict[str, Any]) -> None:
        is_final_manifest = (
            document_name == "manifest.json"
            and path.name == document_name
            and payload.get("status") in {"finished", "incomplete"}
        )
        is_final_run = (
            document_name == "run.json"
            and path.name == document_name
            and payload.get("completeness_status") == "complete"
        )
        if is_final_manifest or is_final_run:
            path.write_bytes(b'{"truncated":')
            return
        original_atomic_write_json(path, payload)

    monkeypatch.setattr(case_runner_module, "atomic_write_json", corrupt_final_document)

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "corrupt"
    assert f"invalid_json:{document_name}" in completeness["issues"]
    assert result.completeness_status == "corrupt"
    if document_name == "manifest.json":
        assert read_json(attempt / "run.json")["completeness_status"] == "corrupt"


@pytest.mark.parametrize(
    ("field_name", "bad_value", "expected_issue"),
    [
        (
            "attempt_id",
            "attempt-final-write-corruption",
            "identity_mismatch:run.json:attempt_id",
        ),
        (
            "session_id",
            "session-wrong",
            "identity_mismatch:run.json:session_id",
        ),
        (
            "acp_session_id",
            "sess-wrong",
            "identity_mismatch:run.json:acp_session_id",
        ),
        (
            "turn_id",
            "turn-wrong",
            "identity_mismatch:run.json:turn_id",
        ),
    ],
)
def test_final_validation_detects_final_run_identity_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    bad_value: str,
    expected_issue: str,
) -> None:
    original_atomic_write_json = case_runner_module.atomic_write_json

    def corrupt_final_run(path: Path, payload: dict[str, Any]) -> None:
        document = dict(payload)
        if path.name == "run.json" and payload.get("completeness_status") == "complete":
            document[field_name] = bad_value
        original_atomic_write_json(path, document)

    monkeypatch.setattr(case_runner_module, "atomic_write_json", corrupt_final_run)

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "corrupt"
    assert expected_issue in completeness["issues"]
    assert result.completeness_status == "corrupt"


def test_acp_orchestration_error_forces_manifest_incomplete(tmp_path: Path) -> None:
    result, attempt, _, _ = run_mode(tmp_path, "initialize-error")

    assert result.acp_status == "error"
    assert "ACP initialize failed" in read_json(attempt / "run.json")["error"]
    assert read_json(attempt / "manifest.json")["status"] == "incomplete"


def test_final_snapshot_failure_finishes_with_incomplete_manifest_and_terminal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_snapshot = case_runner_module.write_snapshot

    def fail_final_snapshot(root: Path, destination: Path):
        if destination.name == "files-after.json":
            raise OSError("injected final snapshot failure")
        return original_write_snapshot(root, destination)

    monkeypatch.setattr(case_runner_module, "write_snapshot", fail_final_snapshot)

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    events = read_jsonl(attempt / "process.jsonl")
    assert any(
        event["event"] == "attempt.error"
        and event.get("terminal") is True
        and event.get("stage") == "files-after"
        for event in events
    )
    assert read_json(attempt / "manifest.json")["status"] == "incomplete"
    assert read_json(attempt / "completeness.json")["status"] == "incomplete"
    assert result.completeness_status == "incomplete"


def test_final_manifest_write_failure_retries_as_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_atomic_write_json = case_runner_module.atomic_write_json
    manifest_writes = 0

    def fail_first_final_manifest(path: Path, payload: dict[str, Any]) -> None:
        nonlocal manifest_writes
        if path.name == "manifest.json":
            manifest_writes += 1
            if manifest_writes == 2:
                raise OSError("injected final manifest write failure")
        original_atomic_write_json(path, payload)

    monkeypatch.setattr(
        case_runner_module,
        "atomic_write_json",
        fail_first_final_manifest,
    )

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    events = read_jsonl(attempt / "process.jsonl")
    assert any(
        event["event"] == "attempt.error"
        and event.get("stage") == "manifest-final"
        and event.get("terminal") is True
        for event in events
    )
    assert read_json(attempt / "manifest.json")["status"] == "incomplete"
    assert read_json(attempt / "completeness.json")["status"] == "incomplete"
    assert read_json(attempt / "run.json")["completeness_status"] == "incomplete"
    assert result.completeness_status == "incomplete"


def test_final_run_write_failure_forces_incomplete_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_atomic_write_json = case_runner_module.atomic_write_json
    run_writes = 0

    def fail_first_final_run(path: Path, payload: dict[str, Any]) -> None:
        nonlocal run_writes
        if path.name == "run.json":
            run_writes += 1
            if run_writes == 1:
                raise OSError("injected final run write failure")
        original_atomic_write_json(path, payload)

    monkeypatch.setattr(
        case_runner_module,
        "atomic_write_json",
        fail_first_final_run,
    )

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    events = read_jsonl(attempt / "process.jsonl")
    assert any(
        event["event"] == "attempt.error"
        and event.get("stage") == "run-final"
        and event.get("terminal") is True
        for event in events
    )
    assert read_json(attempt / "manifest.json")["status"] == "incomplete"
    assert read_json(attempt / "completeness.json")["status"] == "incomplete"
    assert read_json(attempt / "run.json")["completeness_status"] == "incomplete"
    assert result.completeness_status == "incomplete"


def test_completeness_write_failure_retries_with_incomplete_final_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_atomic_write_json = case_runner_module.atomic_write_json
    completeness_writes = 0

    def fail_first_completeness(path: Path, payload: dict[str, Any]) -> None:
        nonlocal completeness_writes
        if path.name == "completeness.json":
            completeness_writes += 1
            if completeness_writes == 1:
                raise OSError("injected completeness write failure")
        original_atomic_write_json(path, payload)

    monkeypatch.setattr(
        case_runner_module,
        "atomic_write_json",
        fail_first_completeness,
    )

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    events = read_jsonl(attempt / "process.jsonl")
    assert any(
        event["event"] == "attempt.error"
        and event.get("stage") == "completeness-write"
        and event.get("terminal") is True
        for event in events
    )
    assert read_json(attempt / "manifest.json")["status"] == "incomplete"
    assert read_json(attempt / "run.json")["completeness_status"] == "incomplete"
    assert read_json(attempt / "completeness.json")["status"] == "incomplete"
    assert result.completeness_status == "incomplete"


def test_final_validation_failure_forces_incomplete_identity_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_completeness = case_runner_module._completeness

    def fail_final_validation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("validate_final_documents") is True:
            raise OSError("injected final validation failure")
        return original_completeness(*args, **kwargs)

    monkeypatch.setattr(
        case_runner_module,
        "_completeness",
        fail_final_validation,
    )

    result, attempt, _, _ = run_mode(tmp_path, "normal")

    events = read_jsonl(attempt / "process.jsonl")
    assert any(
        event["event"] == "attempt.error"
        and event.get("stage") == "final-completeness-validation"
        and event.get("terminal") is True
        for event in events
    )
    assert read_json(attempt / "manifest.json")["status"] == "incomplete"
    assert read_json(attempt / "run.json")["completeness_status"] == "incomplete"
    assert read_json(attempt / "completeness.json")["status"] == "incomplete"
    assert result.completeness_status == "incomplete"


def test_subprocess_start_failure_writes_terminal_event_and_incomplete_manifest(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, "normal")
    config = CaseConfig(
        repo_root=config.repo_root,
        evaluation_dir=config.evaluation_dir,
        dataset_root=config.dataset_root,
        timeout_seconds=config.timeout_seconds,
        python_executable=str(tmp_path / "missing-python"),
    )
    record = {
        "id": "normal",
        "query": "execute normal",
        "input_files": ["input_files/normal/payload.txt"],
    }

    result = run_case(record, config)

    [attempt] = list(
        (config.evaluation_dir / "cases" / "normal" / "attempts").glob("attempt-*")
    )
    events = read_jsonl(attempt / "process.jsonl")
    assert events[-1]["event"] == "attempt.error"
    assert events[-1]["terminal"] is True
    assert read_json(attempt / "manifest.json")["status"] == "incomplete"
    assert read_json(attempt / "completeness.json")["status"] == "incomplete"
    assert result.acp_status == "error"
