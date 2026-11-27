"""Interval estimates and the batch gate over quality_baseline runs.

``quality_baseline compare`` answers "did any case's observed pass rate go
down". This module answers the questions the context-optimization plan needs,
without touching ``quality_baseline.py`` (its own hash is the oracle identity,
so editing it would make every recorded run incomparable):

* ``calibrate``: from two A/A groups of the same source, classify each case as
  stable-pass / stable-fail / flaky and measure the natural spread of each
  per-attempt metric, giving the pairs needed for a chosen minimum effect.
* ``gate``: evaluate a candidate C against the original baseline B0 and the
  last accepted version P under a pre-registered primary metric.

Pass rates use Wilson intervals, their differences Newcombe's hybrid score
interval. Per-attempt metrics (tokens, rounds, latency, tool calls) are paired
by run index: the i-th baseline run directory pairs with the i-th candidate
run directory, case by case, so interleave A/B runs and pass them in order.
Paired mean differences get a seeded percentile bootstrap interval; results are
deterministic for the same inputs.

Missing usage is unknown, never zero: a metric that is unknown on either side
of a pair drops that pair and the count is reported.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from statistics import mean, stdev

from test_workspace import quality_baseline as qb
from test_workspace.context_cache import usage_metrics

Z95 = 1.959963984540054
Z80 = 0.8416212335729143
BOOTSTRAP_SAMPLES = 4000
SEED = 20261124
METRICS = ("first_prompt_tokens", "main_prompt_tokens", "main_uncached_prompt_tokens",
           "main_calls", "total_tokens", "tool_calls", "failed_tool_calls", "latency_seconds")


# ---------------------------------------------------------------- intervals

def wilson(passed: int, total: int, z: float = Z95) -> tuple[float, float] | None:
    if total <= 0:
        return None
    p = passed / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return round(max(0.0, centre - half), 12), round(min(1.0, centre + half), 12)


def newcombe(before: tuple[int, int], after: tuple[int, int], z: float = Z95) -> dict | None:
    """Interval for after_rate - before_rate (Newcombe hybrid score, method 10)."""
    (k1, n1), (k2, n2) = before, after
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    delta = p2 - p1
    low = delta - math.sqrt((p2 - l2) ** 2 + (u1 - p1) ** 2)
    high = delta + math.sqrt((u2 - p2) ** 2 + (p1 - l1) ** 2)
    return {"delta": delta, "low": max(-1.0, low), "high": min(1.0, high)}


def bootstrap_mean(values: list[float], samples: int = BOOTSTRAP_SAMPLES, seed: int = SEED) -> dict | None:
    """Mean with a seeded percentile bootstrap 95% interval."""
    if not values:
        return None
    if len(values) == 1:
        return {"n": 1, "mean": values[0], "low": None, "high": None}
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(samples))
    return {"n": n, "mean": mean(values), "low": means[int(0.025 * samples)],
            "high": means[min(samples - 1, int(0.975 * samples))]}


def required_pairs(spread: float, min_effect: float) -> int | None:
    """Pairs for 80% power at two-sided 5% given the A/A paired-difference SD."""
    if min_effect <= 0 or spread is None:
        return None
    if spread == 0:
        return 2
    return max(2, math.ceil(((Z95 + Z80) * spread / min_effect) ** 2))


# ---------------------------------------------------------------- evidence

def _attempt_metrics(attempt: Path, result: dict) -> dict:
    """Per-attempt metrics from the captured trace; None means unknown."""
    metrics = {key: None for key in METRICS}
    usage = result.get("token_usage") or {}
    metrics["total_tokens"] = qb._total_tokens(usage)
    for key in ("tool_calls", "failed_tool_calls", "latency_seconds"):
        value = result.get(key)
        metrics[key] = value if isinstance(value, (int, float)) and value >= 0 else None
    try:
        records = qb.trace_records(attempt)
    except (OSError, ValueError):
        return metrics
    responses = {record.get("llm_call_id"): record.get("data", {}) for record in records
                 if record.get("event") == "llm.response"}
    main = [usage_metrics(responses.get(record.get("llm_call_id"), {}).get("usage"))
            for record in records if record.get("event") == "llm.request"
            and isinstance(record.get("data"), dict)
            and (record["data"].get("call_kind") or "main") == "main"]
    if not main:
        return metrics
    metrics["main_calls"] = len(main)
    metrics["first_prompt_tokens"] = main[0]["prompt_tokens"]
    prompts = [call["prompt_tokens"] for call in main]
    if all(value is not None for value in prompts):
        metrics["main_prompt_tokens"] = sum(prompts)
        if all(call["cached_tokens"] is not None for call in main):
            metrics["main_uncached_prompt_tokens"] = sum(call["prompt_tokens"] - call["cached_tokens"]
                                                         for call in main)
    return metrics


def collect(directory: Path, pack: Path) -> dict[str, list[dict]]:
    """case_id -> attempts (quality + metrics) for one run directory."""
    report = qb.evaluate_run(directory.resolve(), pack)
    cases: dict[str, list[dict]] = {}
    for result in report["results"]:
        record = {"quality": result["quality"], "metrics": {key: None for key in METRICS}}
        name = result.get("attempt")
        if name:
            attempt = qb.contained(directory.resolve(), f"cases/{result['case_id']}/attempts/{name}")
            if result["quality"] != "unverified":
                record["metrics"] = _attempt_metrics(attempt, result)
        cases.setdefault(result["case_id"], []).append(record)
    return cases


def collect_side(directories: list[Path], pack: Path) -> list[dict[str, list[dict]]]:
    return [collect(directory, pack) for directory in directories]


# ---------------------------------------------------------------- summaries

def _counts(side: list[dict[str, list[dict]]], case: str) -> dict:
    attempts = [attempt for run in side for attempt in run.get(case, [])]
    passed = sum(a["quality"] == "passed" for a in attempts)
    failed = sum(a["quality"] == "failed" for a in attempts)
    return {"passed": passed, "failed": failed, "verified": passed + failed,
            "unverified": sum(a["quality"] == "unverified" for a in attempts)}


def paired_differences(before: list[dict], after: list[dict], case: str, metric: str) -> tuple[list[float], int]:
    """after - before for each run index where both sides have one verified value."""
    diffs, dropped = [], 0
    for index in range(min(len(before), len(after))):
        sides = []
        for run in (before[index], after[index]):
            values = [a["metrics"][metric] for a in run.get(case, []) if a["quality"] != "unverified"]
            sides.append(values[0] if len(values) == 1 else None)
        if None in sides:
            dropped += 1
        else:
            diffs.append(sides[1] - sides[0])
    return diffs, dropped + abs(len(before) - len(after))


def compare_sides(before: list[dict], after: list[dict]) -> dict:
    cases = sorted({case for run in [*before, *after] for case in run})
    rows = {}
    for case in cases:
        b, a = _counts(before, case), _counts(after, case)
        metrics = {}
        for metric in METRICS:
            diffs, dropped = paired_differences(before, after, case, metric)
            metrics[metric] = {"paired": bootstrap_mean(diffs), "dropped_pairs": dropped}
        rows[case] = {"before": b, "after": a,
                      "before_interval": wilson(b["passed"], b["verified"]),
                      "after_interval": wilson(a["passed"], a["verified"]),
                      "pass_rate_difference": newcombe((b["passed"], b["verified"]),
                                                       (a["passed"], a["verified"])),
                      "metrics": metrics}
    return rows


def calibrate(group_a: list[dict], group_b: list[dict]) -> dict:
    """Natural variation of one source: case classes and per-metric spread."""
    cases = sorted({case for run in [*group_a, *group_b] for case in run})
    result = {}
    for case in cases:
        counts = _counts([*group_a, *group_b], case)
        if counts["verified"] == 0:
            kind = "unverified"
        elif counts["failed"] == 0:
            kind = "stable_pass"
        elif counts["passed"] == 0:
            kind = "stable_fail"
        else:
            kind = "flaky"
        spread = {}
        for metric in METRICS:
            diffs, _ = paired_differences(group_a, group_b, case, metric)
            spread[metric] = {"n": len(diffs), "paired_sd": stdev(diffs) if len(diffs) >= 2 else None}
        result[case] = {"class": kind, **counts, "interval": wilson(counts["passed"], counts["verified"]),
                        "metrics": spread}
    return {"schema_version": 1, "cases": result}


def _pooled(rows: dict, cases: list[str], side: str) -> tuple[int, int]:
    return (sum(rows[c][side]["passed"] for c in cases if c in rows),
            sum(rows[c][side]["verified"] for c in cases if c in rows))


# ---------------------------------------------------------------- gate

def _footprint_value(report: dict, path: str) -> int:
    scenario, field = path.split(".", 1)
    return report["scenarios"][scenario][field]


def _primary(prereg: dict, p: list[dict], c: list[dict], versus_p: dict,
             footprints: tuple[dict, dict] | None) -> dict:
    metric = prereg["primary_metric"]
    min_effect = float(prereg["min_effect"])
    if metric.startswith("footprint:"):
        if footprints is None:
            return {"metric": metric, "status": "missing_evidence", "reason": "footprint files required"}
        delta = _footprint_value(footprints[1], metric[10:]) - _footprint_value(footprints[0], metric[10:])
        return {"metric": metric, "kind": "deterministic", "delta": delta, "min_effect": min_effect,
                "status": "proven" if -delta >= min_effect else "not_proven"}
    cases = prereg["beneficiary_cases"]
    if metric == "pass_rate":
        interval = newcombe(_pooled(versus_p, cases, "before"), _pooled(versus_p, cases, "after"))
        n = _pooled(versus_p, cases, "after")[1]
        proven = interval is not None and interval["low"] > 0 and interval["delta"] >= min_effect
    else:
        # One bootstrap over every (case, run index) pair of the beneficiary cases.
        diffs = [d for case in cases for d in paired_differences(p, c, case, metric)[0]]
        interval = bootstrap_mean(diffs)
        n = len(diffs)
        proven = (interval is not None and interval["high"] is not None
                  and interval["high"] < 0 and -interval["mean"] >= min_effect)
    max_pairs = int(prereg.get("max_pairs", 0))
    min_pairs = int(prereg.get("min_pairs", 3))
    if proven and n >= min_pairs:
        status = "proven"
    elif n < max_pairs:
        status = "continue_sampling"
    else:
        status = "not_proven"
    return {"metric": metric, "kind": "stochastic", "interval": interval, "pairs_or_attempts": n,
            "min_effect": min_effect, "max_pairs": max_pairs, "status": status}


def gate(prereg: dict, b0: list[dict], p: list[dict], c: list[dict], calibration: dict,
         footprints: tuple[dict, dict] | None = None) -> dict:
    """Decision for candidate C; see the plan's merge rules."""
    versus_b0, versus_p = compare_sides(b0, c), compare_sides(p, c)
    classes = {case: info["class"] for case, info in calibration["cases"].items()}
    margin = float(prereg.get("noninferiority_margin", 0.15))
    blockers, investigate, notes = [], [], []
    for case in prereg.get("critical_cases", []):
        failed = versus_p.get(case, {}).get("after", {}).get("failed", 0)
        if failed:
            blockers.append(f"critical case {case} failed {failed} time(s)")
        elif not versus_p.get(case, {}).get("after", {}).get("verified"):
            blockers.append(f"critical case {case} has no verified attempt")
    # Coverage: every case the comparison expects must have verified evidence
    # in the candidate. A missing or unverifiable case can hide a regression,
    # so it never counts as passing.
    expected = {*classes, *prereg.get("critical_cases", []), *prereg.get("beneficiary_cases", []),
                *(case for run in [*b0, *p] for case in run)}
    for case in sorted(expected - set(prereg.get("critical_cases", []))):
        if not versus_p.get(case, {}).get("after", {}).get("verified"):
            investigate.append(f"case {case} has no verified candidate attempt (missing or unverified)")
    for label, rows in (("B0", versus_b0), ("P", versus_p)):
        stable = [case for case, kind in classes.items() if kind == "stable_pass"]
        for case in stable:
            if rows.get(case, {}).get("after", {}).get("failed"):
                investigate.append(f"stable case {case} failed vs {label}: re-run paired under the same conditions")
        uncalibrated = [case for case in rows if case not in classes]
        for case in uncalibrated:
            # Without A/A evidence a new failure cannot be dismissed as noise.
            if rows[case]["after"]["failed"] and not rows[case]["before"]["failed"]:
                investigate.append(f"uncalibrated case {case} failed vs {label} without baseline failures: "
                                   "calibrate it or re-run paired")
        flaky = [case for case, kind in classes.items() if kind == "flaky"] + uncalibrated
        if flaky:
            # Small samples cannot prove non-inferiority; act on evidence of a drop.
            interval = newcombe(_pooled(rows, flaky, "before"), _pooled(rows, flaky, "after"))
            if interval and (interval["high"] < 0 or interval["delta"] <= -margin):
                notes.append(f"flaky cases vs {label}: pooled pass-rate difference "
                             f"{interval['delta']:+.2f} [{interval['low']:+.2f}, {interval['high']:+.2f}] "
                             f"dropped (margin -{margin:.2f})")
        for case in rows:
            if case not in classes:
                notes.append(f"case {case} is not calibrated; treat as flaky until A/A covers it")
    primary = _primary(prereg, p, c, versus_p, footprints)
    if blockers:
        decision = "block"
    elif investigate:
        decision = "investigate"
    elif any("dropped (margin" in note for note in notes):
        decision = "continue_sampling" if primary.get("status") == "continue_sampling" else "defer"
    elif primary["status"] == "proven":
        decision = "accept"
    elif primary["status"] == "continue_sampling":
        decision = "continue_sampling"
    else:
        decision = "not_proven"
    return {"schema_version": 1, "batch": prereg.get("batch"), "decision": decision,
            "blockers": blockers, "investigate": sorted(set(investigate)), "notes": sorted(set(notes)),
            "primary": primary, "versus_b0": versus_b0, "versus_p": versus_p}


# ---------------------------------------------------------------- CLI

def _fmt_interval(interval) -> str:
    if not interval:
        return "n/a"
    if isinstance(interval, tuple):
        return f"[{interval[0]:.2f}, {interval[1]:.2f}]"
    if interval.get("low") is None:
        return f"{interval.get('mean', interval.get('delta'))} (n={interval.get('n', '?')})"
    centre = interval.get("mean", interval.get("delta"))
    return f"{centre:+.1f} [{interval['low']:+.1f}, {interval['high']:+.1f}] n={interval.get('n', '?')}"


def render(rows: dict, metrics=("first_prompt_tokens", "main_prompt_tokens", "main_calls", "latency_seconds")) -> str:
    lines = ["| case | before pass | after pass | Δ pass rate [95%] | " + " | ".join(f"Δ {m}" for m in metrics) + " |",
             "|---|---|---|---|" + "---|" * len(metrics)]
    for case, row in rows.items():
        d = row["pass_rate_difference"]
        diff = f"{d['delta']:+.2f} [{d['low']:+.2f}, {d['high']:+.2f}]" if d else "n/a"
        lines.append(f"| {case} | {row['before']['passed']}/{row['before']['verified']} "
                     f"| {row['after']['passed']}/{row['after']['verified']} | {diff} | "
                     + " | ".join(_fmt_interval(row["metrics"][m]["paired"]) for m in metrics) + " |")
    return "\n".join(lines)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmp = sub.add_parser("compare", help="paired intervals, before vs after")
    cal = sub.add_parser("calibrate", help="A/A classes and spread")
    gat = sub.add_parser("gate", help="batch decision for a candidate")
    for command in (cmp, cal, gat):
        command.add_argument("--pack", type=Path, default=qb.PACK)
        command.add_argument("--output", type=Path)
    cmp.add_argument("--before", type=Path, nargs="+", required=True)
    cmp.add_argument("--after", type=Path, nargs="+", required=True)
    cal.add_argument("--group-a", type=Path, nargs="+", required=True)
    cal.add_argument("--group-b", type=Path, nargs="+", required=True)
    cal.add_argument("--min-effect", action="append", default=[], metavar="METRIC=VALUE",
                     help="report pairs needed to detect this effect, e.g. main_calls=1")
    gat.add_argument("--prereg", type=Path, required=True)
    gat.add_argument("--calibration", type=Path, required=True)
    gat.add_argument("--b0", type=Path, nargs="+", required=True)
    gat.add_argument("--p", type=Path, nargs="+", required=True)
    gat.add_argument("--candidate", type=Path, nargs="+", required=True)
    gat.add_argument("--footprint", type=Path, nargs=2, metavar=("P_JSON", "C_JSON"))
    args = parser.parse_args(argv)
    pack = args.pack.resolve()
    try:
        if args.command == "compare":
            # Reuse the runner's comparability checks before computing anything.
            qb.compare_runs(args.before, args.after, pack)
            result = compare_sides(collect_side(args.before, pack), collect_side(args.after, pack))
            text = render(result)
        elif args.command == "calibrate":
            qb.compare_runs(args.group_a, args.group_b, pack)
            result = calibrate(collect_side(args.group_a, pack), collect_side(args.group_b, pack))
            efforts = dict(item.split("=", 1) for item in args.min_effect)
            for info in result["cases"].values():
                for metric, value in efforts.items():
                    spread = info["metrics"].get(metric, {}).get("paired_sd")
                    info["metrics"].setdefault(metric, {})["pairs_for_min_effect"] = required_pairs(spread, float(value))
            text = "\n".join(f"{case}: {info['class']} {info['passed']}/{info['verified']} "
                             f"(unverified {info['unverified']})" for case, info in result["cases"].items())
        else:
            for side in (args.p, args.candidate):
                qb.compare_runs(args.b0, side, pack)
            footprints = tuple(_load(path) for path in args.footprint) if args.footprint else None
            result = gate(_load(args.prereg), collect_side(args.b0, pack), collect_side(args.p, pack),
                          collect_side(args.candidate, pack), _load(args.calibration), footprints)
            text = "\n".join([f"decision: {result['decision']}", f"primary: {json.dumps(result['primary'], ensure_ascii=False)}",
                              *(f"BLOCK: {item}" for item in result["blockers"]),
                              *(f"INVESTIGATE: {item}" for item in result["investigate"]),
                              *(f"NOTE: {item}" for item in result["notes"]),
                              "", "vs P:", render(result["versus_p"])])
    except (OSError, ValueError, KeyError) as error:
        print(f"unavailable: {type(error).__name__}: {error}")
        return 2
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(text)
    if args.command == "gate":
        return {"accept": 0, "block": 1, "investigate": 1}.get(result["decision"], 2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
