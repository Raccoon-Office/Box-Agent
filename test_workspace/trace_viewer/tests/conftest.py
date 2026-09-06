import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trace_viewer.app import create_app


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, values: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    output = tmp_path / "test_workspace" / "outputs" / "eval-one"
    write_json(
        output / "manifest.json",
        {
            "schema_version": "box-agent-acp-eval/v1",
            "run_id": "run-test",
            "status": "completed_with_failures",
            "finished_at": "2026-08-21T10:00:00+00:00",
        },
    )
    write_json(
        output / "summary.json",
        {
            "schema_version": "box-agent-acp-eval/v1",
            "finished_at": "2026-08-21T10:00:00+00:00",
            "cases": [{"case_id": "Q1"}, {"case_id": "Q2"}],
        },
    )
    for case_id, status in (("Q1", "complete"), ("Q2", "incomplete")):
        case = output / "cases" / case_id
        attempt_id = f"attempt-{case_id.lower()}"
        attempt = case / "attempts" / attempt_id
        input_document = {"id": case_id, "query": f"question {case_id}"}
        if case_id == "Q1":
            input_document.update({"task_type": "text", "task_types": ["text"]})
        write_json(case / "input.json", input_document)
        write_json(case / "latest.json", {"attempt_id": attempt_id, "path": f"attempts/{attempt_id}"})
        write_json(
            attempt / "run.json",
            {
                "schema_version": "box-agent-acp-eval/v1",
                "case_id": case_id,
                "attempt_id": attempt_id,
                "acp_status": "completed" if case_id == "Q1" else "error",
                "completeness_status": status,
                "process_exit_code": -15,
                "started_at": "2026-08-21T09:59:58+00:00",
                "finished_at": "2026-08-21T10:00:00+00:00",
                "stderr_counts": {"error": 1 if case_id == "Q2" else 0, "timeout": 0, "warning": 1},
            },
        )
        write_json(attempt / "completeness.json", {"status": status, "issues": [] if status == "complete" else ["missing"]})
        (attempt / "assistant.txt").write_text(f"answer {case_id}", encoding="utf-8")
        (attempt / "stderr.log").write_text("warning: demo\nerror: demo\n", encoding="utf-8")
        write_jsonl(attempt / "protocol.jsonl", [{"sequence": 1, "direction": "sent", "timestamp": "2026-08-21T09:59:58+00:00", "message": {"method": "initialize"}}])
        write_jsonl(attempt / "agent" / "trace.jsonl", [{"type": "turn.start", "timestamp": "2026-08-21T09:59:59+00:00"}])
        write_jsonl(attempt / "process.jsonl", [{"event": "process.started", "timestamp": "2026-08-21T09:59:58.5+00:00"}])
        write_json(attempt / "files-before.json", {"files": []})
        write_json(attempt / "files-after.json", {"files": []})
        write_json(attempt / "artifacts.json", {"artifacts": []})
        if case_id == "Q1":
            write_json(
                attempt / "effect_evaluation.json",
                {
                    "schema_version": "agent-eval-effect/v1",
                    "status": "partial",
                    "generated_at": "2026-08-21T10:00:01+00:00",
                    "source": {"metric_source": "generic"},
                    "judge": {"model": "deepseek-v4-flash", "error": None},
                    "summary": {
                        "process_score": 32,
                        "result_score": 55,
                        "total_score": 87,
                        "score_coverage": 0.45,
                        "available_metrics": 4,
                        "total_metrics": 11,
                    },
                    "metrics": [
                        {
                            "metric_id": "planning_quality",
                            "label": "计划质量",
                            "phase": "process",
                            "weight": 8,
                            "score": None,
                            "status": "unavailable",
                            "judge_type": "hybrid",
                            "confidence": 0,
                            "rationale": "未配置效果评估模型 API",
                            "missing_evidence": [],
                            "evidence": [],
                        },
                        {
                            "metric_id": "task_completion",
                            "label": "任务完成度",
                            "phase": "result",
                            "weight": 5,
                            "score": 5,
                            "status": "complete",
                            "judge_type": "programmatic",
                            "confidence": 0.99,
                            "rationale": "ACP 已完成并交付最终回答",
                            "missing_evidence": [],
                            "evidence": [
                                {
                                    "evidence_id": "ev-final-answer",
                                    "locator": "assistant.txt",
                                    "excerpt": "answer Q1",
                                }
                            ],
                        }
                    ],
                    "unavailable_metrics": [
                        {
                            "metric_id": "longest_stall_ms",
                            "label": "最长界面停顿",
                            "phase": "process",
                            "missing_evidence": ["stream_visibility"],
                            "reason": "当前采集未记录逐块可见性时间线",
                        }
                    ],
                    "performance": [
                        {
                            "metric_id": "total_task_ms",
                            "label": "任务总耗时",
                            "value": 2000,
                            "unit": "ms",
                            "status": "available",
                            "reason": "",
                        }
                    ],
                    "cost": [
                        {
                            "metric_id": "agent_total_tokens",
                            "label": "Agent 总 Token",
                            "scope": "agent",
                            "value": 120,
                            "unit": "tokens",
                            "source": "agent trace",
                            "reason": "",
                        },
                        {
                            "metric_id": "agent_monetary_cost",
                            "label": "Agent 金额成本",
                            "scope": "agent",
                            "value": None,
                            "unit": "currency",
                            "source": "provider telemetry",
                            "reason": "当前采集没有可审计金额",
                        },
                    ],
                },
            )
    return tmp_path


@pytest.fixture
def client(repo_root: Path) -> TestClient:
    return TestClient(create_app(repo_root))
