import json
import base64
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from trace_viewer.evaluation import (
    EvaluationLaunchError,
    EvaluationRunner,
    OpsClient,
    OpsSettings,
)


class StaticOpsClient(OpsClient):
    def __init__(self, responses):
        super().__init__(OpsSettings("http://ops.test", "office-raccoon"))
        self.responses = responses

    def _get(self, path, query=None):
        return self.responses[path]


def test_ops_options_expose_dataset_summaries_and_model_bound_cases_only():
    client = StaticOpsClient(
        {
            "/api/query-sets": [
                {
                    "query_set_id": "qs-one",
                    "name": "数据集一",
                    "task_types": ["ppt"],
                    "items": [
                        {"attachments": [{"path": "/tmp/input.xlsx"}]},
                        {"attachments": []},
                    ],
                }
            ],
            "/api/cases": [
                {
                    "case_id": "case-data-driven",
                    "name": "DeepSeek 数据驱动评测",
                    "definition": {
                        "prompt": "{query}",
                        "meta": {
                            "llm_binding": {
                                "model": "sn-deepseek-v4-pro",
                                "maxTokens": 100000,
                            }
                        },
                    },
                },
                {
                    "case_id": "case-fixed-prompt",
                    "definition": {
                        "prompt": "固定提示词",
                        "meta": {"llm_binding": {"model": "not-selectable"}},
                    },
                },
            ],
        }
    )

    options = client.list_options()

    assert options["datasets"] == [
        {
            "id": "qs-one",
            "name": "数据集一",
            "item_count": 2,
            "attachment_count": 1,
            "task_types": ["ppt"],
            "task_type_stats": [
                {"name": "ppt", "item_count": 2, "attachment_count": 1}
            ],
        }
    ]
    assert options["models"] == [
        {
            "id": "sn-deepseek-v4-pro",
            "name": "sn-deepseek-v4-pro",
            "max_tokens": 100000,
            "case_config_id": "case-data-driven",
            "case_name": "DeepSeek 数据驱动评测",
        }
    ]


def test_runner_prefers_configured_multi_model_catalog(tmp_path: Path):
    repo_root = tmp_path / "repo"
    catalog = repo_root / "test_workspace" / "evaluation_models.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "raccoonwork-auto",
                        "name": "RaccoonWork-Auto",
                        "multiplier": "1x",
                        "binding": {
                            "source": "builtin",
                            "model": "sn-sensenova-6-8-flash-lite",
                            "evaluationMode": "auto",
                        },
                    },
                    {
                        "id": "sn-glm-5-2",
                        "name": "GLM-5-2",
                        "multiplier": "0.5x",
                        "binding": {"source": "builtin", "model": "sn-glm-5-2"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    ops = StaticOpsClient({"/api/query-sets": [], "/api/cases": []})

    options = EvaluationRunner(repo_root, ops=ops).list_options()

    assert [item["name"] for item in options["models"]] == [
        "RaccoonWork-Auto",
        "GLM-5-2",
    ]
    assert options["models"][0]["binding"]["evaluationMode"] == "auto"


class QuerySetOps:
    def __init__(self, query_set):
        self.query_set = query_set

    def get_query_set(self, query_set_id):
        assert query_set_id == "qs-one"
        return self.query_set


def _unsigned_jwt(expiry: int) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': expiry})}.signature"


def test_runner_refreshes_expired_builtin_auth_before_fetching_dataset(
    monkeypatch,
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    refresh_tool = repo_root / "test_workspace" / "refresh_box_agent_auth.py"
    refresh_tool.parent.mkdir(parents=True)
    refresh_tool.write_text("# test refresh tool\n", encoding="utf-8")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "access_token": _unsigned_jwt(int(time.time()) - 1),
                "refresh_token": "secret-refresh-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOX_AGENT_EVAL_AUTH_FILE", str(auth_path))
    output = repo_root / "test_workspace" / "outputs" / "260827-1200-auto"
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if str(command[1]).endswith("refresh_box_agent_auth.py"):
            auth_path.write_text(
                json.dumps(
                    {
                        "access_token": _unsigned_jwt(int(time.time()) + 3600),
                        "refresh_token": "rotated-secret-refresh-token",
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        output.mkdir(parents=True)
        return SimpleNamespace(returncode=0, stdout=f"输出目录: {output}\n")

    monkeypatch.setattr("trace_viewer.evaluation.subprocess.run", fake_run)
    runner = EvaluationRunner(
        repo_root,
        ops=QuerySetOps(
            {
                "query_set_id": "qs-one",
                "items": [{"query_id": "Q1", "query": "hello"}],
            }
        ),
    )

    result = runner.run(
        dataset_id="qs-one",
        model="sn-deepseek-v4-pro",
        model_max_tokens=100000,
        model_binding={"source": "builtin", "model": "sn-deepseek-v4-pro"},
        launch_id="launch-expired-auth",
    )

    assert len(calls) == 2
    assert str(calls[0][1]).endswith("refresh_box_agent_auth.py")
    assert result["return_code"] == 0


def test_runner_stops_before_fetching_dataset_when_builtin_auth_refresh_fails(
    monkeypatch,
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    refresh_tool = repo_root / "test_workspace" / "refresh_box_agent_auth.py"
    refresh_tool.parent.mkdir(parents=True)
    refresh_tool.write_text("# test refresh tool\n", encoding="utf-8")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "access_token": _unsigned_jwt(int(time.time()) - 1),
                "refresh_token": "secret-refresh-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOX_AGENT_EVAL_AUTH_FILE", str(auth_path))

    class UnexpectedOps:
        def get_query_set(self, _query_set_id):
            raise AssertionError("dataset must not be fetched after refresh failure")

    monkeypatch.setattr(
        "trace_viewer.evaluation.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="认证刷新失败",
        ),
    )

    with pytest.raises(EvaluationLaunchError, match="自动刷新失败"):
        EvaluationRunner(repo_root, ops=UnexpectedOps()).run(
            dataset_id="qs-one",
            model="sn-deepseek-v4-pro",
            model_max_tokens=100000,
            model_binding={"source": "builtin", "model": "sn-deepseek-v4-pro"},
            launch_id="launch-refresh-failed",
        )


def test_runner_materializes_ops_dataset_and_invokes_existing_acp_entry(
    monkeypatch,
    tmp_path: Path,
):
    attachment = tmp_path / "source" / "brief.txt"
    attachment.parent.mkdir()
    attachment.write_text("source body", encoding="utf-8")
    query_set = {
        "query_set_id": "qs-one",
        "project_key": "office-raccoon",
        "items": [
            {
                "query_id": "Q1",
                "query": "根据附件生成报告",
                "task_type": "doc",
                "task_types": ["doc"],
                "benchmark_case_id": "case-01",
                "tags": ["file"],
                "metadata": {"owner": "qa"},
                "attachments": [{"path": str(attachment), "name": "brief.txt"}],
            }
        ],
    }
    repo_root = tmp_path / "repo"
    output = repo_root / "test_workspace" / "outputs" / "260827-1200-auto"
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        dataset = Path(command[command.index("--dataset") + 1])
        captured["records"] = [
            json.loads(line)
            for line in dataset.read_text(encoding="utf-8").splitlines()
        ]
        copied = dataset.parent / "inputs" / "Q1" / "brief.txt"
        captured["attachment"] = copied.read_text(encoding="utf-8")
        output.mkdir(parents=True)
        return SimpleNamespace(returncode=0, stdout=f"输出目录: {output}\n")

    monkeypatch.setattr("trace_viewer.evaluation.subprocess.run", fake_run)
    runner = EvaluationRunner(repo_root, ops=QuerySetOps(query_set))

    result = runner.run(
        dataset_id="qs-one",
        model="sn-deepseek-v4-pro",
        model_max_tokens=100000,
        launch_id="launch-123456789abc",
    )

    command = captured["command"]
    assert command[command.index("--parallelism") + 1] == "1"
    assert command[command.index("--model") + 1] == "sn-deepseek-v4-pro"
    assert command[command.index("--model-max-tokens") + 1] == "100000"
    assert captured["records"] == [
        {
            "id": "Q1",
            "query": "根据附件生成报告",
            "input_files": ["inputs/Q1/brief.txt"],
            "benchmark_case_id": "case-01",
            "task_type": "doc",
            "task_types": ["doc"],
            "tags": ["file"],
            "metadata": {"owner": "qa"},
            "source": {"type": "raccoon-ops", "query_set_id": "qs-one"},
        }
    ]
    assert captured["attachment"] == "source body"
    assert result["run_name"] == "260827-1200-auto"


def test_runner_filters_task_type_and_limits_execution_count(monkeypatch, tmp_path: Path):
    query_set = {
        "query_set_id": "qs-one",
        "project_key": "office-raccoon",
        "items": [
            {"query_id": "P1", "query": "生成 PPT", "task_types": ["PPT生成"]},
            {"query_id": "D1", "query": "分析数据", "task_types": ["数据分析"]},
            {"query_id": "D2", "query": "分析表格", "task_types": ["数据分析"]},
        ],
    }
    repo_root = tmp_path / "repo"
    output = repo_root / "test_workspace" / "outputs" / "260827-1201-auto"
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        dataset = Path(command[command.index("--dataset") + 1])
        captured["ids"] = [
            json.loads(line)["id"]
            for line in dataset.read_text(encoding="utf-8").splitlines()
        ]
        output.mkdir(parents=True)
        return SimpleNamespace(returncode=0, stdout=f"输出目录: {output}\n")

    monkeypatch.setattr("trace_viewer.evaluation.subprocess.run", fake_run)
    runner = EvaluationRunner(repo_root, ops=QuerySetOps(query_set))

    runner.run(
        dataset_id="qs-one",
        model="sn-glm-5-2",
        model_max_tokens=100000,
        launch_id="launch-count-filter",
        task_type="数据分析",
        execution_count=1,
    )

    assert captured["ids"] == ["D1", "D2"]
    command = captured["command"]
    assert command[command.index("--count") + 1] == "1"


def test_runner_rejects_an_ops_attachment_without_a_local_path(tmp_path: Path):
    runner = EvaluationRunner(
        tmp_path,
        ops=QuerySetOps(
            {
                "query_set_id": "qs-one",
                "items": [
                    {
                        "query_id": "Q1",
                        "query": "读取附件",
                        "attachments": [{"uri": "https://example.test/input.pdf"}],
                    }
                ],
            }
        ),
    )

    with pytest.raises(EvaluationLaunchError, match="同机可读路径"):
        runner.run(
            dataset_id="qs-one",
            model="model-one",
            model_max_tokens=None,
            launch_id="launch-missing-path",
        )
