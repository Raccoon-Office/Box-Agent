"""Interval estimates and batch gate for the context-optimization evaluations."""

import json

import pytest

from test_workspace import paired_stats as ps


def test_wilson_matches_reference_values():
    low, high = ps.wilson(0, 3)
    assert low == 0 and high == pytest.approx(0.5615, abs=1e-4)
    low, high = ps.wilson(3, 3)
    assert low == pytest.approx(0.4385, abs=1e-4) and high == 1
    assert ps.wilson(0, 0) is None


def test_newcombe_matches_published_example():
    # Newcombe (1998), method 10: 56/70 vs 48/80 -> 0.0524 to 0.3339.
    interval = ps.newcombe((48, 80), (56, 70))
    assert interval["delta"] == pytest.approx(0.2)
    assert interval["low"] == pytest.approx(0.0524, abs=1e-4)
    assert interval["high"] == pytest.approx(0.3339, abs=1e-4)


def test_bootstrap_is_deterministic_and_brackets_the_mean():
    values = [-120.0, -80.0, -100.0, -95.0, -110.0]
    first, second = ps.bootstrap_mean(values), ps.bootstrap_mean(values)
    assert first == second
    assert first["low"] <= first["mean"] == -101.0 <= first["high"] < 0
    assert ps.bootstrap_mean([5.0]) == {"n": 1, "mean": 5.0, "low": None, "high": None}
    assert ps.bootstrap_mean([]) is None


def test_required_pairs_grows_with_spread():
    assert ps.required_pairs(0, 1) == 2
    assert ps.required_pairs(1.0, 1.0) == 8
    assert ps.required_pairs(2.0, 1.0) == 32
    assert ps.required_pairs(1.0, 0) is None


def attempt(quality, **metrics):
    return {"quality": quality, "metrics": {key: metrics.get(key) for key in ps.METRICS}}


def run(**cases):
    return {case: [value] for case, value in cases.items()}


def test_calibrate_classifies_cases_and_measures_paired_spread():
    a = [run(stable=attempt("passed", main_calls=4), flaky=attempt("passed"), broken=attempt("failed")),
         run(stable=attempt("passed", main_calls=6), flaky=attempt("failed"), broken=attempt("failed"))]
    b = [run(stable=attempt("passed", main_calls=5), flaky=attempt("passed"), broken=attempt("failed")),
         run(stable=attempt("passed", main_calls=5), flaky=attempt("passed"),
             broken=attempt("unverified"))]
    cases = ps.calibrate(a, b)["cases"]
    assert {case: info["class"] for case, info in cases.items()} == {
        "stable": "stable_pass", "flaky": "flaky", "broken": "stable_fail"}
    assert cases["broken"]["unverified"] == 1
    spread = cases["stable"]["metrics"]["main_calls"]
    assert spread["n"] == 2 and spread["paired_sd"] == pytest.approx(1.4142, abs=1e-4)


def test_paired_differences_drop_unknown_and_unmatched_runs():
    before = [run(x=attempt("passed", total_tokens=100)), run(x=attempt("passed", total_tokens=None)),
              run(x=attempt("passed", total_tokens=90))]
    after = [run(x=attempt("passed", total_tokens=80)), run(x=attempt("passed", total_tokens=70))]
    diffs, dropped = ps.paired_differences(before, after, "x", "total_tokens")
    assert diffs == [-20] and dropped == 2


CALIBRATION = {"cases": {"safety": {"class": "stable_pass"}, "chat": {"class": "stable_pass"},
                         "noisy": {"class": "flaky"}}}


def sides(candidate_safety="passed", tokens=(1000, 1000, 1000), candidate_tokens=(900, 905, 895)):
    def side(values, safety):
        return [run(safety=attempt(safety), chat=attempt("passed", first_prompt_tokens=value),
                    noisy=attempt("passed")) for value in values]
    return side(tokens, "passed"), side(tokens, "passed"), side(candidate_tokens, candidate_safety)


def test_gate_blocks_on_any_critical_failure_regardless_of_gains():
    b0, p, c = sides(candidate_safety="failed")
    prereg = {"batch": "x", "primary_metric": "first_prompt_tokens", "beneficiary_cases": ["chat"],
              "min_effect": 50, "max_pairs": 10, "critical_cases": ["safety"]}
    result = ps.gate(prereg, b0, p, c, CALIBRATION)
    assert result["decision"] == "block" and "safety" in result["blockers"][0]


def test_gate_continues_sampling_until_max_pairs_then_reports_not_proven():
    b0, p, c = sides(candidate_tokens=(990, 1010, 1000))
    prereg = {"primary_metric": "first_prompt_tokens", "beneficiary_cases": ["chat"],
              "min_effect": 50, "max_pairs": 10}
    assert ps.gate(prereg, b0, p, c, CALIBRATION)["decision"] == "continue_sampling"
    prereg["max_pairs"] = 3
    assert ps.gate(prereg, b0, p, c, CALIBRATION)["decision"] == "not_proven"


def test_gate_accepts_a_consistent_stochastic_gain():
    b0, p, c = sides()
    prereg = {"primary_metric": "first_prompt_tokens", "beneficiary_cases": ["chat"],
              "min_effect": 50, "max_pairs": 3, "critical_cases": ["safety"]}
    result = ps.gate(prereg, b0, p, c, CALIBRATION)
    assert result["decision"] == "accept"
    assert result["primary"]["interval"]["high"] < 0


def test_gate_sends_a_stable_case_failure_to_paired_rerun():
    b0, p, c = sides()
    c[0]["chat"] = [attempt("failed", first_prompt_tokens=900)]
    prereg = {"primary_metric": "first_prompt_tokens", "beneficiary_cases": ["chat"],
              "min_effect": 50, "max_pairs": 3}
    result = ps.gate(prereg, b0, p, c, CALIBRATION)
    assert result["decision"] == "investigate"


def test_gate_uses_exact_footprint_deltas_for_deterministic_metrics():
    b0, p, c = sides(candidate_tokens=(1000, 1000, 1000))
    before = {"scenarios": {"acp_general_chat": {"system_bytes": 35000}}}
    after = {"scenarios": {"acp_general_chat": {"system_bytes": 34500}}}
    prereg = {"primary_metric": "footprint:acp_general_chat.system_bytes", "min_effect": 300}
    result = ps.gate(prereg, b0, p, c, CALIBRATION, (before, after))
    assert result["primary"] == {"metric": prereg["primary_metric"], "kind": "deterministic",
                                 "delta": -500, "min_effect": 300.0, "status": "proven"}
    assert result["decision"] == "accept"
    assert ps.gate(prereg, b0, p, c, CALIBRATION)["primary"]["status"] == "missing_evidence"


def test_attempt_metrics_separate_main_calls_and_keep_unknown_cache_unknown(tmp_path):
    (tmp_path / "agent").mkdir()
    events = []
    for index, (kind, usage) in enumerate([
        ("main", {"prompt_tokens": 1000, "completion_tokens": 10, "cached_tokens": 0}),
        ("main", {"prompt_tokens": 1200, "completion_tokens": 10, "cached_tokens": 900}),
        ("completion_judge", {"prompt_tokens": 400, "completion_tokens": 5}),
    ]):
        events.append({"event": "llm.request", "llm_call_id": str(index), "data": {"call_kind": kind}})
        events.append({"event": "llm.response", "llm_call_id": str(index), "data": {"usage": usage}})
    (tmp_path / "agent" / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    metrics = ps._attempt_metrics(tmp_path, {"token_usage": {"totalTokens": 2635}, "tool_calls": 3,
                                             "failed_tool_calls": 0, "latency_seconds": 12.5})
    assert metrics == {"first_prompt_tokens": 1000, "main_prompt_tokens": 2200,
                       "main_uncached_prompt_tokens": 1300, "main_calls": 2, "total_tokens": 2635,
                       "tool_calls": 3, "failed_tool_calls": 0, "latency_seconds": 12.5}
    events[3]["data"]["usage"].pop("cached_tokens")
    (tmp_path / "agent" / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    assert ps._attempt_metrics(tmp_path, {})["main_uncached_prompt_tokens"] is None


def test_gate_holds_when_flaky_cases_drop_beyond_the_margin():
    b0, p, c = sides()
    for index in range(3):
        c[index]["noisy"] = [attempt("failed")]
    prereg = {"primary_metric": "first_prompt_tokens", "beneficiary_cases": ["chat"],
              "min_effect": 50, "max_pairs": 3}
    result = ps.gate(prereg, b0, p, c, CALIBRATION)
    assert result["decision"] == "defer"
    assert any("dropped" in note for note in result["notes"])


def test_gate_does_not_accept_when_a_non_critical_case_is_missing_or_unverified():
    prereg = {"primary_metric": "first_prompt_tokens", "beneficiary_cases": ["chat"],
              "min_effect": 50, "max_pairs": 3, "critical_cases": ["safety"]}
    for candidate_noisy in (None, "unverified"):
        b0, p, c = sides()
        for index in range(3):
            if candidate_noisy is None:
                del c[index]["noisy"]
            else:
                c[index]["noisy"] = [attempt("unverified")]
        result = ps.gate(prereg, b0, p, c, CALIBRATION)
        assert result["decision"] == "investigate"
        assert any("noisy has no verified candidate attempt" in item for item in result["investigate"])


def test_gate_does_not_accept_an_uncalibrated_case_that_starts_failing():
    b0, p, c = sides()
    for side, quality in ((b0, "passed"), (p, "passed"), (c, "failed")):
        side[0]["fresh"] = [attempt(quality)]  # 1/1 -> 0/1, never calibrated
    prereg = {"primary_metric": "first_prompt_tokens", "beneficiary_cases": ["chat"],
              "min_effect": 50, "max_pairs": 3}
    result = ps.gate(prereg, b0, p, c, CALIBRATION)
    assert result["decision"] == "investigate"
    assert any("uncalibrated case fresh failed" in item for item in result["investigate"])
