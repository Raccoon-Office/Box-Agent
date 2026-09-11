from __future__ import annotations

import json
from pathlib import Path
import pytest

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


@pytest.mark.parametrize("include_identity", [False, True])
def test_effect_client_posts_identity_and_persists_response(
    monkeypatch, tmp_path: Path, include_identity: bool
) -> None:
    attempt = tmp_path / "attempt-20260826T120000-12345678"
    attempt.mkdir()
    captured: dict[str, object] = {}
    response = {
        "schema_version": "agent-eval-effect/v1",
        "status": "complete",
        "metrics": [],
    }
    if include_identity:
        response["source"] = {"case_id": "Q1", "attempt_id": attempt.name}

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
    assert json.loads((attempt / "effect_response.json").read_text()) == response


@pytest.mark.parametrize("source", [
    {"case_id": "OTHER-CASE"},
    {"attempt_id": "other-attempt"},
    {"case_id": None},
])
def test_effect_identity_conflict_is_not_published_as_current_score(monkeypatch, tmp_path, source):
    attempt = tmp_path / "current-attempt"
    attempt.mkdir()
    acp_result = attempt / "run.json"
    acp_result.write_text('{"acp_status":"completed","completeness_status":"complete"}')
    before = acp_result.read_bytes()
    response = {"schema_version": "agent-eval-effect/v1", "status": "complete",
                "source": source, "summary": {"total_score": 60}, "metrics": []}
    monkeypatch.setattr(effect_client.urllib.request, "urlopen", lambda *args, **kwargs: _Response(response))

    document = evaluate_attempt(attempt, {"id": "CURRENT-CASE"}, EffectEvaluationConfig("http://example.test"))

    assert document["status"] == "service_error"
    assert "does not match request" in document["client"]["error"]
    assert document["summary"]["total_score"] is None
    assert document["source"]["case_id"] == "CURRENT-CASE"
    assert document["source"]["attempt_id"] == attempt.name
    assert json.loads((attempt / "effect_response.json").read_text()) == response
    assert acp_result.read_bytes() == before


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
