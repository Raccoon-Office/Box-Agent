from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any

import pytest

import acp_eval.batch_runner as batch_runner_module
from acp_eval.batch_runner import load_dataset, run_batch
from acp_eval.cli import main


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def write_dataset(root: Path, records: list[dict[str, Any]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dataset = root / "dataset.jsonl"
    dataset.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return dataset


def record(case_id: str, input_files: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": case_id,
        "query": f"execute {case_id}",
        "domain": "test",
        "input_files": input_files or [],
    }


@pytest.mark.parametrize("metadata", [
    {"session_meta": {"deep_think": "true"}},
    {"session_meta": {"deep_think": 1}},
    {"session_meta": {"permission_mode": "bypass"}},
    {"session_meta": {"session_id": "override"}},
    {"session_meta": None},
    {"prompt_meta": {"selected_skill_names": "sn-ppt-web"}},
    {"prompt_meta": {"selected_skill_names": [1]}},
    {"prompt_meta": {"auto_approve_plan": "true"}},
    {"prompt_meta": {"turnId": "override"}},
    {"session_allowed_directories": "reference"},
    {"session_allowed_directories": ["relative/path"]},
    {"session_allowed_directories": ["/tmp/invalid\x00path"]},
])
def test_dataset_rejects_invalid_or_unsupported_case_metadata(
    tmp_path: Path, metadata: dict[str, Any]
) -> None:
    dataset = write_dataset(tmp_path, [{**record("metadata"), **metadata}])
    with pytest.raises(ValueError, match="case metadata"):
        load_dataset(dataset)


def test_dataset_preserves_explicit_false_and_metadata_record(tmp_path: Path) -> None:
    value = {
        **record("metadata"),
        "session_meta": {"deep_think": False},
        "prompt_meta": {"auto_approve_plan": False, "selected_skill_names": []},
        "session_allowed_directories": [],
    }
    dataset = write_dataset(tmp_path, [value])
    assert load_dataset(dataset) == [value]


def make_fake_repo(root: Path, mode: str = "normal") -> Path:
    package = root / "box_agent" / "acp"
    package.mkdir(parents=True)
    (root / "box_agent" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    fake_path = Path(__file__).with_name("fake_acp.py").resolve()
    dynamic_fake = root / "fake_acp.py"
    dynamic_fake.write_text(
        fake_path.read_text(encoding="utf-8").replace(
            'f"eval-acp-{mode}-turn-1"',
            'f"{upstream_session_id}-turn-1"',
        ),
        encoding="utf-8",
    )
    (package / "server.py").write_text(
        "import runpy, sys\n"
        f"sys.argv = [{str(dynamic_fake)!r}, {mode!r}]\n"
        f"runpy.run_path({str(dynamic_fake)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return root


def stable_runtime(label: str = "one") -> dict[str, Any]:
    return {
        "python": {
            "executable": "/stable/python",
            "version": "3.11.15",
            "implementation": "CPython",
            "status": "available",
        },
        "box_agent": {
            "version": f"0.8.{label}",
            "version_status": "available",
            "git_commit": (label[0] * 40),
            "git_commit_status": "available",
        },
    }


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ('{"id":"same","query":"one","input_files":[]}\n'
         '{"id":"same","query":"two","input_files":[]}\n', "duplicate case id"),
        ('{"id":"ok","query":"one","input_files":[]}\nnot-json\n', "line 2"),
        ('["not", "an", "object"]\n', "JSON object"),
        ('{"id":"../escape","query":"one","input_files":[]}\n', "case id"),
        ('{"id":"ok","query":"","input_files":[]}\n', "query"),
        ('{"id":"ok","query":"one","input_files":"file.txt"}\n', "input_files"),
        ('{"id":"ok","query":"one","input_files":[7]}\n', "input_files"),
    ],
)
def test_load_dataset_rejects_invalid_records(
    tmp_path: Path, lines: str, message: str
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(lines, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_dataset(dataset)


def test_load_dataset_rejects_missing_and_escaping_input_files(tmp_path: Path) -> None:
    missing = write_dataset(
        tmp_path / "missing", [record("missing", ["input_files/missing.txt"])]
    )
    with pytest.raises(FileNotFoundError, match="missing input file"):
        load_dataset(missing)

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    escaping = write_dataset(
        tmp_path / "escaping", [record("escaping", ["../outside.txt"])]
    )
    with pytest.raises(ValueError, match="escapes dataset root"):
        load_dataset(escaping)


def test_run_batch_rejects_duplicate_or_unknown_explicit_case_ids(
    tmp_path: Path,
) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("one"), record("two")])
    repo = make_fake_repo(tmp_path / "repo")

    with pytest.raises(ValueError, match="duplicate selected case id: one"):
        run_batch(dataset, tmp_path / "duplicate", repo, 2.0, 1, ["one", "one"])
    with pytest.raises(ValueError, match="unknown case id: absent"):
        run_batch(dataset, tmp_path / "unknown", repo, 2.0, 1, ["absent"])

    assert not (tmp_path / "duplicate").exists()
    assert not (tmp_path / "unknown").exists()


def test_parallel_batch_keeps_case_attempts_isolated_and_writes_indexes(
    tmp_path: Path,
) -> None:
    dataset = write_dataset(
        tmp_path / "dataset", [record("first"), record("second")]
    )
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "parallel"

    exit_code = run_batch(dataset, output, repo, 2.0, 2, [])

    assert exit_code == 0
    manifest = read_json(output / "manifest.json")
    summary = read_json(output / "summary.json")
    assert manifest["schema_version"] == "box-agent-acp-eval/v1"
    assert manifest["status"] == "completed"
    assert manifest["run_id"] == summary["run_id"]
    assert manifest["selected_case_ids"] == ["first", "second"]
    assert manifest["dataset_fingerprint"]["algorithm"] == "sha256"
    assert len(manifest["dataset_fingerprint"]["sha256"]) == 64
    assert manifest["runtime"]["python"]["version"] == platform.python_version()
    assert manifest["runtime"]["python"]["implementation"] == (
        platform.python_implementation()
    )
    assert manifest["runtime"]["python"]["status"] == "available"
    assert set(manifest["runtime"]["box_agent"]) == {
        "git_commit",
        "git_commit_status",
        "version",
        "version_status",
    }
    assert summary["counts"] == {
        "selected": 2,
        "executed": 2,
        "skipped_terminal": 0,
        "acp_completed": 2,
        "acp_failed": 0,
        "complete": 2,
        "complete_with_warnings": 0,
        "incomplete": 0,
        "corrupt": 0,
    }
    assert [item["case_id"] for item in summary["cases"]] == ["first", "second"]

    attempt_paths: list[Path] = []
    for case_id in ("first", "second"):
        case_dir = output / "cases" / case_id
        latest = read_json(case_dir / "latest.json")
        assert set(latest) == {"attempt_id", "path"}
        attempt = case_dir / latest["path"]
        attempt_paths.append(attempt)
        assert attempt.name == latest["attempt_id"]
        run_document = read_json(attempt / "run.json")
        assert run_document["case_id"] == case_id
        assert run_document["runtime"] == manifest["runtime"]
        assert run_document["case_fingerprint"]["algorithm"] == "sha256"
        assert len(run_document["case_fingerprint"]["sha256"]) == 64
        assert (attempt / "workspace" / "output" / "answer.txt").is_file()

    assert attempt_paths[0] != attempt_paths[1]
    assert not list(output.rglob("*.tmp"))


def test_batch_summary_counts_incomplete_result_and_returns_failure(
    tmp_path: Path,
) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("missing-trace-case")])
    repo = make_fake_repo(tmp_path / "repo", mode="missing-trace")
    output = tmp_path / "outputs" / "incomplete"

    exit_code = run_batch(dataset, output, repo, 2.0, 1, [])

    assert exit_code == 1
    summary = read_json(output / "summary.json")
    assert summary["status"] == "completed_with_failures"
    assert summary["counts"]["acp_completed"] == 1
    assert summary["counts"]["incomplete"] == 1


def test_resume_skips_terminal_attempt_and_retry_preserves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("resume")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "resume"
    monkeypatch.setattr(
        batch_runner_module, "_runtime_identity", lambda *_: stable_runtime()
    )

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0
    attempts_dir = output / "cases" / "resume" / "attempts"
    first_attempt = next(attempts_dir.iterdir())
    first_manifest = (first_attempt / "manifest.json").read_bytes()

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0
    assert [path.name for path in attempts_dir.iterdir()] == [first_attempt.name]
    assert read_json(output / "summary.json")["counts"]["skipped_terminal"] == 1

    assert run_batch(
        dataset,
        output,
        repo,
        2.0,
        1,
        [],
        retry_terminal=True,
    ) == 0
    attempts = sorted(attempts_dir.iterdir())
    assert len(attempts) == 2
    assert first_attempt in attempts
    assert (first_attempt / "manifest.json").read_bytes() == first_manifest
    latest = read_json(output / "cases" / "resume" / "latest.json")
    assert latest["attempt_id"] != first_attempt.name
    assert read_json(output / "summary.json")["counts"]["executed"] == 1


@pytest.mark.parametrize("first_settings, changed_settings", [
    ({"model": "model-A"}, {"model": "model-B"}),
    ({"model": "model-A", "model_max_tokens": 128},
     {"model": "model-A", "model_max_tokens": 256}),
    ({"model_binding": {"source": "builtin", "model": "model-A"}},
     {"model_binding": {"source": "builtin", "model": "model-B"}}),
    ({"model_binding": {"source": "builtin", "model": "model-A", "evaluationMode": "auto",
                        "autoRouting": {"models": [{"model": "model-A", "abilityLevel": 1}]}}},
     {"model_binding": {"source": "builtin", "model": "model-A", "evaluationMode": "auto",
                        "autoRouting": {"models": [{"model": "model-B", "abilityLevel": 1}]}}}),
])
def test_model_settings_change_produces_new_attempt_and_preserves_old_result(
    tmp_path, monkeypatch, first_settings, changed_settings,
):
    dataset = write_dataset(tmp_path / "dataset", [record("model-resume")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "models"
    monkeypatch.setattr(batch_runner_module, "_runtime_identity", lambda *_: stable_runtime())
    assert run_batch(dataset, output, repo, 2.0, 1, [], **first_settings) == 0
    attempts = output / "cases" / "model-resume" / "attempts"
    first = next(attempts.iterdir())
    original = (first / "run.json").read_bytes()
    first_identity = read_json(first / "run.json")["model_config_sha256"]

    assert run_batch(dataset, output, repo, 2.0, 1, [], **changed_settings) == 0
    assert len(list(attempts.iterdir())) == 2
    assert read_json(output / "summary.json")["counts"]["executed"] == 1
    latest = read_json(output / "cases" / "model-resume" / "latest.json")
    second = attempts / latest["attempt_id"]
    assert read_json(second / "run.json")["model_config_sha256"] != first_identity
    assert (first / "run.json").read_bytes() == original

    assert run_batch(dataset, output, repo, 2.0, 1, [], **changed_settings) == 0
    assert len(list(attempts.iterdir())) == 2
    assert read_json(output / "summary.json")["counts"]["skipped_terminal"] == 1


def test_legacy_attempt_without_model_identity_is_not_assumed_to_match(tmp_path, monkeypatch):
    dataset = write_dataset(tmp_path / "dataset", [record("legacy-model")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "legacy-model"
    monkeypatch.setattr(batch_runner_module, "_runtime_identity", lambda *_: stable_runtime())
    assert run_batch(dataset, output, repo, 2.0, 1, [], model="model-A") == 0
    attempts = output / "cases" / "legacy-model" / "attempts"
    first = next(attempts.iterdir())
    legacy = read_json(first / "run.json")
    legacy.pop("model_config_sha256")
    (first / "run.json").write_text(json.dumps(legacy))
    before = (first / "run.json").read_bytes()
    assert run_batch(dataset, output, repo, 2.0, 1, [], model="model-A") == 0
    assert len(list(attempts.iterdir())) == 2
    assert (first / "run.json").read_bytes() == before


def test_query_change_executes_new_attempt_without_retry_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_root = tmp_path / "dataset"
    dataset = write_dataset(dataset_root, [record("changed")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "query-change"
    monkeypatch.setattr(
        batch_runner_module, "_runtime_identity", lambda *_: stable_runtime()
    )

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0
    attempts_dir = output / "cases" / "changed" / "attempts"
    first = next(attempts_dir.iterdir())
    first_fingerprint = read_json(first / "run.json")["case_fingerprint"]
    changed = record("changed")
    changed["query"] = "a materially different query"
    write_dataset(dataset_root, [changed])

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0

    attempts = sorted(attempts_dir.iterdir())
    assert len(attempts) == 2
    latest = read_json(output / "cases" / "changed" / "latest.json")
    second = output / "cases" / "changed" / latest["path"]
    assert second != first
    assert read_json(second / "run.json")["case_fingerprint"] != first_fingerprint
    assert read_json(output / "summary.json")["counts"]["executed"] == 1


def test_input_byte_change_executes_new_attempt_without_retry_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_root = tmp_path / "dataset"
    input_path = dataset_root / "input_files" / "payload.txt"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"first bytes")
    dataset = write_dataset(
        dataset_root,
        [record("changed-input", ["input_files/payload.txt"])],
    )
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "input-change"
    monkeypatch.setattr(
        batch_runner_module, "_runtime_identity", lambda *_: stable_runtime()
    )

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0
    attempts_dir = output / "cases" / "changed-input" / "attempts"
    first = next(attempts_dir.iterdir())
    first_fingerprint = read_json(first / "run.json")["case_fingerprint"]
    input_path.write_bytes(b"second bytes")

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0

    attempts = sorted(attempts_dir.iterdir())
    assert len(attempts) == 2
    latest = read_json(output / "cases" / "changed-input" / "latest.json")
    second_fingerprint = read_json(
        output / "cases" / "changed-input" / latest["path"] / "run.json"
    )["case_fingerprint"]
    assert second_fingerprint != first_fingerprint
    assert second_fingerprint["input_files"] == [
        {
            "path": "input_files/payload.txt",
            "sha256": "1631322fe2b6e9697f4a43fd025ba02a292df5638092220c5e16bfbd875c5fae",
        }
    ]


def test_changed_runtime_identity_executes_new_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("runtime-change")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "runtime-change"
    current = {"value": stable_runtime("one")}
    monkeypatch.setattr(
        batch_runner_module,
        "_runtime_identity",
        lambda *_: current["value"],
    )

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0
    attempts_dir = output / "cases" / "runtime-change" / "attempts"
    first = next(attempts_dir.iterdir())
    assert read_json(first / "run.json")["runtime"] == stable_runtime("one")
    current["value"] = stable_runtime("two")

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0

    assert len(list(attempts_dir.iterdir())) == 2
    summary = read_json(output / "summary.json")
    assert summary["counts"]["executed"] == 1
    assert summary["counts"]["skipped_terminal"] == 0
    assert summary["runtime"] == stable_runtime("two")
    assert summary["cases"][0]["runtime"] == stable_runtime("two")


def test_unavailable_runtime_identity_is_not_resume_comparable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("runtime-unavailable")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "runtime-unavailable"
    current = {"value": stable_runtime("one")}
    monkeypatch.setattr(
        batch_runner_module,
        "_runtime_identity",
        lambda *_: current["value"],
    )
    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0
    unavailable = stable_runtime("one")
    unavailable["box_agent"]["version"] = None
    unavailable["box_agent"]["version_status"] = "unavailable"
    current["value"] = unavailable

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0

    attempts = list(
        (output / "cases" / "runtime-unavailable" / "attempts").iterdir()
    )
    assert len(attempts) == 2
    summary = read_json(output / "summary.json")
    assert summary["counts"]["executed"] == 1
    assert summary["counts"]["skipped_terminal"] == 0
    assert summary["cases"][0]["runtime"] == unavailable


def test_mixed_skip_and_execution_summaries_keep_per_case_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_root = tmp_path / "dataset"
    dataset = write_dataset(
        dataset_root,
        [record("unchanged"), record("changed")],
    )
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "mixed"
    old_runtime = stable_runtime("one")
    old_runtime["python"]["executable"] = "/old/python"
    current_runtime = {"value": old_runtime}
    monkeypatch.setattr(
        batch_runner_module,
        "_runtime_identity",
        lambda *_: current_runtime["value"],
    )
    assert run_batch(dataset, output, repo, 2.0, 2, []) == 0
    changed = record("changed")
    changed["query"] = "changed query"
    write_dataset(dataset_root, [record("unchanged"), changed])
    new_runtime = stable_runtime("one")
    new_runtime["python"]["executable"] = "/new/python"
    current_runtime["value"] = new_runtime

    assert run_batch(dataset, output, repo, 2.0, 2, []) == 0

    summary = read_json(output / "summary.json")
    assert [case["disposition"] for case in summary["cases"]] == [
        "skipped_terminal",
        "executed",
    ]
    assert summary["runtime"] == new_runtime
    assert [case["runtime"] for case in summary["cases"]] == [
        old_runtime,
        new_runtime,
    ]


def test_source_mutation_between_fingerprint_and_copy_is_not_indexed_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_root = tmp_path / "dataset"
    input_path = dataset_root / "input_files" / "payload.txt"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"bytes during fingerprint")
    dataset = write_dataset(
        dataset_root,
        [record("toctou", ["input_files/payload.txt"])],
    )
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "toctou"
    real_run_case = batch_runner_module.run_case

    def mutate_then_run(record_value: dict[str, Any], config: Any):
        input_path.write_bytes(b"mutated after fingerprint")
        return real_run_case(record_value, config)

    monkeypatch.setattr(batch_runner_module, "run_case", mutate_then_run)
    monkeypatch.setattr(
        batch_runner_module, "_runtime_identity", lambda *_: stable_runtime()
    )

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 1

    case_dir = output / "cases" / "toctou"
    assert not (case_dir / "latest.json").exists()
    attempt = next((case_dir / "attempts").iterdir())
    assert (attempt / "workspace" / "payload.txt").read_bytes() == (
        b"mutated after fingerprint"
    )
    run_document = read_json(attempt / "run.json")
    assert run_document["case_fingerprint"]["input_files"] == [
        {
            "path": "input_files/payload.txt",
            "sha256": "a381ca668d5b9da391b8b128520be1a395e7a1960a839b7264d3cceaba5c91a0",
        }
    ]
    assert run_document["input_consistency"]["status"] == "mismatch"
    assert run_document["completeness_status"] == "corrupt"
    completeness = read_json(attempt / "completeness.json")
    assert completeness["status"] == "corrupt"
    assert "input_fingerprint_mismatch" in completeness["issues"]
    assert read_json(attempt / "manifest.json")["status"] == "incomplete"
    summary = read_json(output / "summary.json")
    assert summary["status"] == "completed_with_failures"
    assert summary["cases"][0]["disposition"] == "indexing_error"
    assert summary["cases"][0]["case_fingerprint"] == run_document[
        "case_fingerprint"
    ]
    assert "input fingerprint mismatch" in summary["cases"][0]["error"]


def test_traversing_latest_index_is_rejected_without_reading_outside(
    tmp_path: Path,
) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("unsafe")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "traversal"
    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0
    attempts_dir = output / "cases" / "unsafe" / "attempts"
    attempt_count = len(list(attempts_dir.iterdir()))
    (output / "cases" / "unsafe" / "latest.json").write_text(
        json.dumps({"attempt_id": "../../outside", "path": "../../outside"}),
        encoding="utf-8",
    )

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 1

    assert len(list(attempts_dir.iterdir())) == attempt_count
    summary = read_json(output / "summary.json")
    assert summary["status"] == "completed_with_failures"
    assert summary["cases"][0]["disposition"] == "resume_index_error"
    assert "invalid attempt id" in summary["cases"][0]["error"]


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_symlinked_latest_attempt_directory_is_rejected(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("symlink")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "symlink"
    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0
    case_dir = output / "cases" / "symlink"
    latest = read_json(case_dir / "latest.json")
    attempt = case_dir / latest["path"]
    outside = tmp_path / "outside-attempt"
    attempt.rename(outside)
    attempt.symlink_to(outside, target_is_directory=True)

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 1

    summary = read_json(output / "summary.json")
    assert summary["cases"][0]["disposition"] == "resume_index_error"
    assert "symlink" in summary["cases"][0]["error"]
    assert read_json(outside / "run.json")["case_id"] == "symlink"


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_retry_rejects_symlinked_attempts_directory(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("retry-symlink")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "retry-symlink"
    assert run_batch(dataset, output, repo, 2.0, 1, []) == 0
    case_dir = output / "cases" / "retry-symlink"
    attempts_dir = case_dir / "attempts"
    outside = tmp_path / "outside-attempts"
    attempts_dir.rename(outside)
    attempts_dir.symlink_to(outside, target_is_directory=True)
    outside_count = len(list(outside.iterdir()))

    assert run_batch(
        dataset, output, repo, 2.0, 1, [], retry_terminal=True
    ) == 1

    assert len(list(outside.iterdir())) == outside_count
    summary = read_json(output / "summary.json")
    assert summary["cases"][0]["disposition"] == "resume_index_error"
    assert "symlink" in summary["cases"][0]["error"]


def test_latest_write_failure_is_reported_while_attempt_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("index-failure")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "index-failure"
    real_atomic_write = batch_runner_module.atomic_write_json

    def fail_latest(path: Path, payload: dict[str, Any]) -> None:
        if path.name == "latest.json":
            raise OSError("injected latest write failure")
        real_atomic_write(path, payload)

    monkeypatch.setattr(batch_runner_module, "atomic_write_json", fail_latest)

    assert run_batch(dataset, output, repo, 2.0, 1, []) == 1

    attempts = list((output / "cases" / "index-failure" / "attempts").iterdir())
    assert len(attempts) == 1
    assert (attempts[0] / "run.json").is_file()
    assert not (output / "cases" / "index-failure" / "latest.json").exists()
    summary = read_json(output / "summary.json")
    assert summary["status"] == "completed_with_failures"
    assert summary["cases"][0]["disposition"] == "indexing_error"
    assert "injected latest write failure" in summary["cases"][0]["error"]
    assert read_json(output / "manifest.json")["status"] == (
        "completed_with_failures"
    )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_run_batch_rejects_non_finite_timeout(
    tmp_path: Path, timeout: float
) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("finite")])
    repo = make_fake_repo(tmp_path / "repo")

    with pytest.raises(ValueError, match="finite"):
        run_batch(dataset, tmp_path / "output", repo, timeout, 1, [])


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_cli_rejects_non_finite_timeout(timeout: str) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "--repo-root",
                ".",
                "--dataset",
                "dataset.jsonl",
                "--run-dir",
                "run",
                "--timeout-seconds",
                timeout,
            ]
        )

    assert raised.value.code == 2


def test_cli_selects_exact_case_and_exposes_retry_terminal(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "dataset", [record("one"), record("two")])
    repo = make_fake_repo(tmp_path / "repo")
    output = tmp_path / "outputs" / "selected"

    exit_code = main(
        [
            "--repo-root",
            str(repo),
            "--dataset",
            str(dataset),
            "--run-dir",
            str(output),
            "--case-id",
            "two",
            "--parallelism",
            "1",
            "--timeout-seconds",
            "2",
        ]
    )

    assert exit_code == 0
    assert not (output / "cases" / "one").exists()
    assert (output / "cases" / "two" / "latest.json").is_file()
    assert main(
        [
            "--repo-root",
            str(repo),
            "--dataset",
            str(dataset),
            "--run-dir",
            str(output),
            "--case-id",
            "two",
            "--retry-terminal",
        ]
    ) == 0
    assert len(list((output / "cases" / "two" / "attempts").iterdir())) == 2
