"""Simple readers for box-agent-acp-eval/v1 output directories."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from trace_viewer.timeline import source_records


SCHEMA_VERSION = "box-agent-acp-eval/v1"


class NotFoundError(LookupError):
    pass


class EvaluationRepository:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.outputs_root = self.repo_root / "test_workspace" / "outputs"

    @staticmethod
    def _json(path: Path, default: Any = None) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return default

    def _under(self, root: Path, *parts: str) -> Path:
        path = root.joinpath(*parts).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as error:
            raise NotFoundError("path is outside the evaluation output") from error
        return path

    def run_path(self, run_name: str) -> Path:
        path = self._under(self.outputs_root, run_name)
        if not path.is_dir() or path.parent != self.outputs_root.resolve():
            raise NotFoundError(run_name)
        return path

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.outputs_root.is_dir():
            return []
        runs: list[dict[str, Any]] = []
        for path in sorted(self.outputs_root.iterdir(), key=lambda item: item.name):
            if path.name.startswith(".") or not path.is_dir():
                continue
            manifest = self._json(path / "manifest.json", {})
            if manifest.get("schema_version") != SCHEMA_VERSION:
                continue
            summary = self._json(path / "summary.json", {})
            cases = summary.get("cases") if isinstance(summary.get("cases"), list) else []
            if not cases:
                cases_dir = path / "cases"
                cases = list(cases_dir.iterdir()) if cases_dir.is_dir() else []
            runs.append(
                {
                    "name": path.name,
                    "task_count": len(cases),
                    "finished_at": summary.get("finished_at") or manifest.get("finished_at"),
                    "status": summary.get("status") or manifest.get("status"),
                }
            )
        return runs

    def _attempt_path(self, case_path: Path) -> Path:
        latest = self._json(case_path / "latest.json", {})
        attempt_id = latest.get("attempt_id")
        if isinstance(attempt_id, str):
            candidate = self._under(case_path / "attempts", attempt_id)
            if candidate.is_dir():
                return candidate
        attempts = case_path / "attempts"
        choices = sorted((path for path in attempts.iterdir() if path.is_dir()), reverse=True) if attempts.is_dir() else []
        if not choices:
            raise NotFoundError(case_path.name)
        return choices[0]

    @staticmethod
    def _duration(run: dict[str, Any]) -> float | None:
        try:
            start = datetime.fromisoformat(str(run["started_at"]).replace("Z", "+00:00"))
            finish = datetime.fromisoformat(str(run["finished_at"]).replace("Z", "+00:00"))
            return round((finish - start).total_seconds(), 3)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _task_type(input_data: dict[str, Any]) -> str | None:
        task_type = input_data.get("task_type")
        if isinstance(task_type, str) and task_type.strip():
            return task_type.strip()
        task_types = input_data.get("task_types")
        if isinstance(task_types, list):
            values = [
                value.strip()
                for value in task_types
                if isinstance(value, str) and value.strip()
            ]
            if values:
                return " / ".join(dict.fromkeys(values))
        return None

    @staticmethod
    def _effect_summary(attempt: Path) -> dict[str, Any]:
        def compact_number(value: Any) -> Any:
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return value

        effect = EvaluationRepository._json(
            attempt / "effect_evaluation.json",
            None,
        )
        if not isinstance(effect, dict):
            return {
                "status": "missing",
                "process_score": None,
                "process_weight": 40,
                "result_score": None,
                "result_weight": 60,
                "cost": None,
            }

        summary = effect.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        metrics = effect.get("metrics")
        if not isinstance(metrics, list):
            metrics = []

        phase_weights: dict[str, int | float] = {"process": 0, "result": 0}
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            phase = metric.get("phase")
            weight = metric.get("weight")
            if (
                phase in phase_weights
                and isinstance(weight, (int, float))
                and not isinstance(weight, bool)
            ):
                phase_weights[phase] += weight

        raw_cost = effect.get("cost")
        cost_metrics = raw_cost if isinstance(raw_cost, list) else []
        cost = next(
            (
                metric
                for metric in cost_metrics
                if isinstance(metric, dict)
                and metric.get("metric_id") == "agent_total_tokens"
                and metric.get("value") is not None
            ),
            None,
        )
        if cost is None:
            cost = next(
                (
                    metric
                    for metric in cost_metrics
                    if isinstance(metric, dict)
                    and metric.get("scope") == "agent"
                    and metric.get("value") is not None
                    and metric.get("status") in {None, "available", "complete"}
                ),
                None,
            )

        metrics_complete = (
            isinstance(summary.get("total_metrics"), int)
            and not isinstance(summary.get("total_metrics"), bool)
            and summary.get("total_metrics") > 0
            and summary.get("total_metrics") == len(metrics)
        )
        return {
            "status": effect.get("status") or "unknown",
            "process_score": compact_number(summary.get("process_score")),
            "process_weight": compact_number(
                phase_weights["process"] if metrics_complete else 40
            ),
            "result_score": compact_number(summary.get("result_score")),
            "result_weight": compact_number(
                phase_weights["result"] if metrics_complete else 60
            ),
            "cost": dict(cost) if isinstance(cost, dict) else None,
        }

    def _case_summary(self, case_path: Path) -> dict[str, Any]:
        input_data = self._json(case_path / "input.json", {})
        try:
            attempt = self._attempt_path(case_path)
        except NotFoundError:
            return {
                "case_id": case_path.name,
                "query": input_data.get("query"),
                "task_type": self._task_type(input_data),
                "acp_status": "missing",
                "completeness_status": "incomplete",
                "duration": None,
                "stderr_counts": {"error": 0, "timeout": 0, "warning": 0},
                "effect_summary": self._effect_summary(case_path),
            }
        run = self._json(attempt / "run.json", {})
        return {
            "case_id": case_path.name,
            "query": input_data.get("query"),
            "task_type": self._task_type(input_data),
            "attempt_id": attempt.name,
            "attempt_path": attempt,
            "acp_status": run.get("acp_status") or "unknown",
            "completeness_status": run.get("completeness_status") or "incomplete",
            "duration": self._duration(run),
            "stderr_counts": run.get("stderr_counts") or {"error": 0, "timeout": 0, "warning": 0},
            "effect_summary": self._effect_summary(attempt),
            "run": run,
        }

    def list_cases(self, run_name: str, query: str = "") -> list[dict[str, Any]]:
        cases_dir = self.run_path(run_name) / "cases"
        if not cases_dir.is_dir():
            return []
        needle = query.casefold().strip()
        cases = [self._case_summary(path) for path in sorted(cases_dir.iterdir()) if path.is_dir()]
        if needle:
            cases = [case for case in cases if needle in str(case["case_id"]).casefold() or needle in str(case.get("query") or "").casefold()]
        return cases

    def get_case(self, run_name: str, case_id: str) -> dict[str, Any]:
        case_path = self._under(self.run_path(run_name) / "cases", case_id)
        if not case_path.is_dir():
            raise NotFoundError(case_id)
        result = self._case_summary(case_path)
        if "attempt_path" not in result:
            raise NotFoundError(case_id)
        attempt = result["attempt_path"]
        result.update(
            {
                "input": self._json(case_path / "input.json", {}),
                "assistant": self._final_answer(attempt),
                "completeness": self._json(attempt / "completeness.json", {}),
                "effect_evaluation": self._json(
                    attempt / "effect_evaluation.json", None
                ),
                "case_path": case_path,
            }
        )
        return result

    def _final_answer(self, attempt: Path) -> str:
        latest = ""
        for record in source_records(attempt, "agent"):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("event") != "turn.output" and payload.get("type") != "turn.output":
                continue
            data = payload.get("data")
            if not isinstance(data, dict):
                continue
            content = data.get("content") or data.get("output")
            if isinstance(content, str) and content:
                latest = content
        return latest or self._text(attempt / "assistant.txt")

    def diagnosis_path(self, run_name: str, case_id: str) -> Path | None:
        case_path = self.get_case(run_name, case_id)["case_path"]
        path = case_path / "diagnosis.md"
        return path if path.is_file() else None

    def diagnosis_text(self, run_name: str, case_id: str) -> str | None:
        path = self.diagnosis_path(run_name, case_id)
        return self._text(path) if path else None

    @staticmethod
    def _text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def resolve_case_path(self, run_name: str, case_id: str, relative: str) -> Path:
        attempt = self.get_case(run_name, case_id)["attempt_path"]
        path = self._under(attempt, relative)
        if not path.is_file():
            raise NotFoundError(relative)
        return path
