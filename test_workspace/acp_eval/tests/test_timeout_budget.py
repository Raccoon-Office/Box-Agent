"""Evaluation deadlines must expose, rather than hide, runtime wait policy."""
import json

import pytest

from acp_eval import batch_runner


@pytest.mark.parametrize("wait,requested,effective,status", [
    (300, None, 2700, "aligned"),
    (600, None, 3060, "aligned"),
    (300, 180, 180, "constrained"),
    (300, 1800, 1800, "aligned"),
])
def test_task_budget_covers_recovery_and_completion_without_changing_model_wait(
    tmp_path, monkeypatch, wait, requested, effective, status,
):
    monkeypatch.setattr(batch_runner, "_command_value", lambda *_: json.dumps({
        "provider_stale_seconds": wait, "max_provider_stale_recoveries": 3,
    }))
    budget = batch_runner._timeout_budget(tmp_path, "python", requested)
    assert budget["effective_task_seconds"] == effective
    assert budget["provider_stale_seconds"] == wait
    assert budget["minimum_task_seconds"] == wait * 5 + 60
    assert budget["status"] == status


@pytest.mark.parametrize("probe", [None, "bad json", '{"provider_stale_seconds":NaN}',
                                    '{"provider_stale_seconds":300,"max_provider_stale_recoveries":-1}'])
def test_unavailable_policy_is_recorded_without_inventing_a_model_budget(tmp_path, monkeypatch, probe):
    monkeypatch.setattr(batch_runner, "_command_value", lambda *_: probe)
    budget = batch_runner._timeout_budget(tmp_path, "python", 180)
    assert budget["status"] == "unavailable"
    assert budget["provider_stale_seconds"] is None
    assert budget["effective_task_seconds"] == 180
