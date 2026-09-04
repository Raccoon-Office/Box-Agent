from __future__ import annotations

import json
from pathlib import Path

import acp_eval.effect_client as effect_client
from acp_eval.effect_client import EffectEvaluationConfig, evaluate_attempt


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_effect_client_posts_identity_and_persists_response(
    monkeypatch, tmp_path: Path
) -> None:
    attempt = tmp_path / "attempt-20260826T120000-12345678"
    attempt.mkdir()
    captured: dict[str, object] = {}
    response = {
        "schema_version": "agent-eval-effect/v1",
        "status": "complete",
        "metrics": [],
    }

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response(response)

    monkeypatch.setattr(effect_client.urllib.request, "urlopen", fake_urlopen)
    document = evaluate_attempt(
        attempt,
        {"id": "Q1", "benchmark_case_id": "case-05"},
        EffectEvaluationConfig("http://127.0.0.1:8766/", 12.5),
    )

    assert captured == {
        "url": "http://127.0.0.1:8766/api/v1/effect-evaluations",
        "payload": {
            "attempt_path": str(attempt),
            "case_id": "Q1",
            "attempt_id": attempt.name,
            "benchmark_case_id": "case-05",
        },
        "timeout": 12.5,
    }
    assert document["status"] == "complete"
    assert document["client"]["error"] is None
    assert json.loads((attempt / "effect_evaluation.json").read_text()) == document


def test_effect_client_persists_service_error_without_raising(
    monkeypatch, tmp_path: Path
) -> None:
    attempt = tmp_path / "attempt-20260826T120000-12345678"
    attempt.mkdir()

    def fail(*args, **kwargs):
        raise OSError("service unavailable")

    monkeypatch.setattr(effect_client.urllib.request, "urlopen", fail)
    document = evaluate_attempt(
        attempt,
        {"id": "Q1"},
        EffectEvaluationConfig("http://127.0.0.1:8766"),
    )

    assert document["status"] == "service_error"
    assert "service unavailable" in document["client"]["error"]
    assert json.loads((attempt / "effect_evaluation.json").read_text()) == document
