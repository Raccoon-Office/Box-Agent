"""Optional synchronous client for the independent agents-eval effect service."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Mapping

from acp_eval.storage import atomic_write_json


EFFECT_SCHEMA_VERSION = "agent-eval-effect/v1"
EFFECT_RESULT_NAME = "effect_evaluation.json"


@dataclass(frozen=True)
class EffectEvaluationConfig:
    service_url: str
    timeout_seconds: float = 180.0

    @property
    def endpoint(self) -> str:
        base = self.service_url.strip().rstrip("/")
        if base.endswith("/api/v1/effect-evaluations"):
            return base
        return f"{base}/api/v1/effect-evaluations"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_document(
    *,
    case_id: str,
    attempt_id: str,
    started_at: str,
    elapsed_ms: int,
    error: str,
) -> dict[str, Any]:
    return {
        "schema_version": EFFECT_SCHEMA_VERSION,
        "status": "service_error",
        "generated_at": _iso_now(),
        "source": {
            "case_id": case_id,
            "attempt_id": attempt_id,
            "metric_source": None,
        },
        "client": {
            "started_at": started_at,
            "elapsed_ms": elapsed_ms,
            "error": error,
        },
        "judge": {
            "configured": None,
            "provider": None,
            "base_url": None,
            "model": None,
            "status": "not_reached",
            "error": error,
            "usage": {},
        },
        "summary": {
            "process_score": None,
            "result_score": None,
            "total_score": None,
            "score_coverage": 0.0,
            "available_metrics": 0,
            "total_metrics": 0,
        },
        "metrics": [],
        "unavailable_metrics": [],
        "performance": [],
        "cost": [],
    }


def evaluate_attempt(
    attempt_dir: Path,
    record: Mapping[str, Any],
    config: EffectEvaluationConfig,
) -> dict[str, Any]:
    """Call the service, persist its exact response, and never expose credentials."""

    attempt_dir = Path(attempt_dir).resolve()
    case_id = str(record.get("id") or "")
    attempt_id = attempt_dir.name
    payload = {
        "attempt_path": str(attempt_dir),
        "case_id": case_id,
        "attempt_id": attempt_id,
        "benchmark_case_id": record.get("benchmark_case_id"),
    }
    started_at = _iso_now()
    started = monotonic()
    try:
        request = urllib.request.Request(
            config.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(
            request,
            timeout=config.timeout_seconds,
        ) as response:
            document = json.loads(response.read().decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("effect service response must be a JSON object")
        if document.get("schema_version") != EFFECT_SCHEMA_VERSION:
            raise ValueError("effect service response has an unsupported schema")
        document["client"] = {
            "started_at": started_at,
            "elapsed_ms": round((monotonic() - started) * 1000),
            "error": None,
        }
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        document = _error_document(
            case_id=case_id,
            attempt_id=attempt_id,
            started_at=started_at,
            elapsed_ms=round((monotonic() - started) * 1000),
            error=f"{type(error).__name__}: {error}",
        )
    atomic_write_json(attempt_dir / EFFECT_RESULT_NAME, document)
    return document
