import json
from datetime import datetime
from pathlib import Path

import pytest

from test_workspace.refresh_box_agent_auth import AuthRefreshError
from test_workspace.run_acp_eval import (
    DEFAULT_DATASET,
    build_command,
    build_parser,
    choose_cases,
    main,
    run_name,
)


def test_default_dataset_is_committed_text_only_smoke_cases():
    assert DEFAULT_DATASET == Path("test_workspace/inputs/smoke_test/dataset.jsonl")
    dataset = Path(__file__).resolve().parent.parent / DEFAULT_DATASET
    records = [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(records) == 3
    assert all(
        set(record) == {"id", "query", "input_files"} for record in records
    )
    assert all(
        isinstance(record["query"], str) and record["query"].strip()
        for record in records
    )
    assert all(record["input_files"] == [] for record in records)


def test_default_case_count_runs_all_committed_smoke_cases():
    assert build_parser().parse_args([]).count == 3


def test_run_name_uses_required_format():
    assert run_name(datetime(2026, 8, 21, 20, 8)) == "260821-2008-smoke-test"


def test_run_name_uses_custom_title_as_directory_suffix():
    assert run_name(datetime(2026, 8, 21, 20, 8), "first") == "260821-2008-first"


@pytest.mark.parametrize("title", ["", ".", "..", "nested/title", r"nested\title"])
def test_run_name_rejects_title_that_is_not_one_directory_segment(title):
    with pytest.raises(ValueError, match="--title"):
        run_name(datetime(2026, 8, 21, 20, 8), title)


def test_random_selection_is_reproducible():
    available = [f"Q{index}" for index in range(10)]
    assert choose_cases(available, 5, 7, []) == choose_cases(available, 5, 7, [])
    assert len(choose_cases(available, 5, 7, [])) == 5


def test_command_uses_only_acp_eval(tmp_path: Path):
    command = build_command(tmp_path, tmp_path / "dataset.jsonl", tmp_path / "out", ["Q1"], 30, 1)
    assert "acp-eval" in command
    assert "box_agent.acp.server" not in command
    assert "box-agent" not in command


def test_command_passes_optional_effect_service_without_credentials(tmp_path: Path):
    command = build_command(
        tmp_path,
        tmp_path / "dataset.jsonl",
        tmp_path / "out",
        ["Q1"],
        30,
        1,
        "http://127.0.0.1:8766",
        45,
    )

    assert command[command.index("--effect-eval-url") + 1] == "http://127.0.0.1:8766"
    assert command[command.index("--effect-eval-timeout-seconds") + 1] == "45"
    assert all("api_key" not in item.casefold() for item in command)


def test_command_passes_selected_tested_model_to_acp_eval(tmp_path: Path):
    command = build_command(
        tmp_path,
        tmp_path / "dataset.jsonl",
        tmp_path / "out",
        ["Q1"],
        30,
        1,
        model="sn-deepseek-v4-pro",
        model_max_tokens=100000,
    )

    assert command[command.index("--model") + 1] == "sn-deepseek-v4-pro"
    assert command[command.index("--model-max-tokens") + 1] == "100000"


def test_command_passes_complete_auto_model_binding_to_acp_eval(tmp_path: Path):
    binding = {
        "source": "builtin",
        "model": "sn-sensenova-6-8-flash-lite",
        "evaluationMode": "auto",
        "autoRouting": {
            "models": [
                {
                    "model": "sn-deepseek-v4-pro",
                    "tags": ["presentation"],
                    "abilityLevel": 3,
                }
            ]
        },
    }

    command = build_command(
        tmp_path,
        tmp_path / "dataset.jsonl",
        tmp_path / "out",
        ["Q1"],
        30,
        1,
        model_binding=binding,
    )

    encoded = command[command.index("--model-binding-json") + 1]
    assert json.loads(encoded) == binding
    assert "--model" not in command


def test_main_records_selection_and_invokes_acp(monkeypatch, tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        "".join(json.dumps({"id": f"Q{index}"}) + "\n" for index in range(8)),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        "test_workspace.run_acp_eval.run_name",
        lambda now=None, title="smoke-test": f"260821-2008-{title}",
    )
    monkeypatch.setattr(
        "test_workspace.run_acp_eval.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs)) or type("Result", (), {"returncode": 0})(),
    )
    assert main(["--repo-root", str(tmp_path), "--dataset", str(dataset), "--count", "5", "--seed", "7"]) == 0
    selection = json.loads((tmp_path / "test_workspace/outputs/260821-2008-smoke-test/selection.json").read_text())
    assert len(selection["case_ids"]) == 5
    assert selection["seed"] == 7
    assert "acp-eval" in calls[0][0]


def test_main_uses_title_for_directory_and_records_it(monkeypatch, tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"id":"Q1"}\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "test_workspace.run_acp_eval.run_name",
        lambda now=None, title="smoke-test": f"260821-2008-{title}",
    )
    monkeypatch.setattr(
        "test_workspace.run_acp_eval.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs)) or type("Result", (), {"returncode": 0})(),
    )

    assert main(
        [
            "--repo-root",
            str(tmp_path),
            "--dataset",
            str(dataset),
            "--count",
            "1",
            "--seed",
            "7",
            "--title",
            "first",
        ]
    ) == 0

    output = tmp_path / "test_workspace/outputs/260821-2008-first"
    selection = json.loads((output / "selection.json").read_text())
    assert selection["title"] == "first"
    assert calls[0][0][calls[0][0].index("--run-dir") + 1] == str(output)


def test_main_ensures_builtin_auth_before_creating_output(monkeypatch, tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"id":"Q1"}\n', encoding="utf-8")
    events = []
    monkeypatch.setattr(
        "test_workspace.run_acp_eval.ensure_fresh_auth",
        lambda: events.append("auth"),
    )
    monkeypatch.setattr(
        "test_workspace.run_acp_eval.run_name",
        lambda now=None, title="smoke-test": f"260821-2008-{title}",
    )

    def fake_run(_command, **_kwargs):
        assert events == ["auth"]
        events.append("acp")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("test_workspace.run_acp_eval.subprocess.run", fake_run)

    assert main(
        [
            "--repo-root",
            str(tmp_path),
            "--dataset",
            str(dataset),
            "--count",
            "1",
            "--model-binding-json",
            json.dumps({"source": "builtin", "model": "sn-deepseek-v4-pro"}),
        ]
    ) == 0
    assert events == ["auth", "acp"]


def test_main_auth_refresh_failure_leaves_no_output_directory(
    monkeypatch,
    tmp_path: Path,
):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"id":"Q1"}\n', encoding="utf-8")

    def fail_auth():
        raise AuthRefreshError("refresh rejected")

    monkeypatch.setattr("test_workspace.run_acp_eval.ensure_fresh_auth", fail_auth)

    with pytest.raises(RuntimeError, match="尚未创建评测输出"):
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--dataset",
                str(dataset),
                "--count",
                "1",
                "--model",
                "sn-deepseek-v4-pro",
            ]
        )

    assert not (tmp_path / "test_workspace" / "outputs").exists()
