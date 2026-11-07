"""Offline evidence checks and explicit live ACP cache evaluation commands."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path

VERSION = "context-cache/v1"
ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "test_workspace/inputs/context_cache"
CASE_ID = "context-loading"
LOADS = ("local_tool", "skill_catalog", "skill_body", "mcp_tool")
RAW_FILES = ("acp-stdin.raw", "acp-stdout.raw", "protocol.jsonl", "stderr.log", "process.jsonl",
             "files-before.json", "files-after.json", "artifacts.json", "assistant.txt", "manifest.json")


def encoded(value: object) -> str:
    # Preserve key order: a re-ordered schema can change provider tokenization.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object: {path.name}")
    return value


def read_events(path: Path) -> list[dict]:
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not all(isinstance(event, dict) for event in events):
        raise ValueError(f"Invalid events: {path.name}")
    return events


def write_json(path: Path, value: object) -> None:
    # Reports, baselines and attempts are never silently replaced.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def token(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def usage_metrics(usage: dict | None) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    prompt = token(usage.get("prompt_tokens"))
    cached = token(usage.get("cached_tokens"))
    if prompt is None or (cached is not None and cached > prompt):
        cached = None
    return {"prompt_tokens": prompt, "completion_tokens": token(usage.get("completion_tokens")),
            "cached_tokens": cached, "cache_field_present": "cached_tokens" in usage,
            "cache_hit_ratio": cached / prompt if cached is not None and prompt else None}


def totals(calls: list[dict]) -> dict:
    known = [call for call in calls if call["cache_hit_ratio"] is not None]
    measured_prompt = sum(call["prompt_tokens"] for call in known)
    measured_cached = sum(call["cached_tokens"] for call in known)
    return {"calls": len(calls), "cache_known_calls": len(known),
            "prompt_tokens": sum(c["prompt_tokens"] for c in calls) if all(c["prompt_tokens"] is not None for c in calls) else None,
            "completion_tokens": sum(c["completion_tokens"] for c in calls) if all(c["completion_tokens"] is not None for c in calls) else None,
            "cached_tokens": measured_cached if len(known) == len(calls) else None,
            "known_prompt_tokens": measured_prompt, "known_cached_tokens": measured_cached,
            "known_cache_hit_ratio": measured_cached / measured_prompt if measured_prompt else None}


def analyze_events(events: list[dict], skill_receipt: str) -> dict:
    """Pair responses by call ID; measure the first MAIN request after each load."""
    if any(not isinstance(event.get("data"), dict) for event in events
           if event.get("event") in {"llm.request", "llm.response", "tool.request", "tool.response"}):
        raise ValueError("Malformed model/tool trace data")
    checks = []

    def check(name, passed):
        checks.append({"name": name, "passed": bool(passed)})

    responses = {e.get("llm_call_id"): e for e in events if e.get("event") == "llm.response"}
    requests = [(i, e) for i, e in enumerate(events) if e.get("event") == "llm.request"]
    check("unique_request_ids", len({e.get("llm_call_id") for _, e in requests}) == len(requests)
          and all(e.get("llm_call_id") for _, e in requests))
    check("unique_response_ids", len(responses) == sum(e.get("event") == "llm.response" for e in events))
    main = [(i, e) for i, e in requests if e["data"].get("call_kind") in (None, "", "main")]
    check("warmed_main_session", len(main) >= 3)
    check("paired_responses", all(e.get("llm_call_id") in responses for _, e in requests))
    calls, previous = [], None
    for i, request in requests:
        data = request["data"]
        response = responses.get(request.get("llm_call_id"), {})
        rd = response.get("data", {})
        messages = data.get("messages", [])
        kind = data.get("call_kind") or "main"
        prefix = previous is None or encoded(messages[:len(previous)]) == encoded(previous)
        calls.append({"request_line": i + 1, "llm_call_id": request.get("llm_call_id"),
                      "call_kind": kind, "model": data.get("model"),
                      "session_id": data.get("request_session_id"),
                      "provider_request_id": rd.get("provider_request_id"),
                      "provider_response_id": rd.get("provider_response_id"),
                      "timing": rd.get("timing", {}),
                      "system_sha256": digest([m for m in messages if m.get("role") == "system"]),
                      "tools_sha256": digest(data.get("tools", [])),
                      "tool_count": len(data.get("tools", [])),
                      "previous_messages_preserved": prefix if kind == "main" else None,
                      **usage_metrics(rd.get("usage"))})
        if kind == "main":
            previous = messages
    main_calls = [call for call in calls if call["call_kind"] == "main"]
    for field in ("system_sha256", "tools_sha256", "session_id", "model"):
        check(f"stable_{field}", len({call[field] for call in main_calls}) == 1)
    check("stable_request_options", len({digest({k: v for k, v in e["data"].items() if k != "messages"})
                                        for _, e in main}) == 1)
    check("append_only_messages", all(c["previous_messages_preserved"] for c in main_calls))
    by_line = {c["request_line"]: c for c in calls}
    loads = []
    for index, event in enumerate(events):
        data = event.get("data", {})
        if event.get("event") != "tool.response" or data.get("success") is not True:
            continue
        name, entry, kind = data.get("tool_name"), None, None
        if name in ("list_skills", "get_skill"):
            kind = "skill_catalog" if name == "list_skills" else "skill_body"
        elif name == "tool_search":
            payload = json.loads(data.get("model_content") or data["content"])
            entries = [item for item in payload.get("activated", [])
                       if item.get("name") in ("append_file", "cache_probe_echo")]
            if len(entries) == 1:
                entry = entries[0]
                kind = "local_tool" if entry["name"] == "append_file" else "mcp_tool"
        if kind is None:
            continue
        before = next(((i, e) for i, e in reversed(main) if i < index), None)
        after = next(((i, e) for i, e in main if i > index), None)
        check(f"{kind}_next_request_exists", before is not None and after is not None)
        if before is None or after is None:
            continue
        bi, be = before
        ai, ae = after
        old, new = be["data"]["messages"], ae["data"]["messages"]
        check(f"{kind}_after_warmup", sum(i <= bi for i, _ in main) >= 3)
        check(f"{kind}_tail_preserved", encoded(new[:len(old)]) == encoded(old))
        # Link the actual successful tool result to the immediately following request.
        delivered = [m for m in new if m.get("role") == "tool" and
                     m.get("tool_call_id") == event.get("tool_call_id")]
        check(f"{kind}_result_delivered", bool(delivered) and
              encoded(delivered[-1].get("content")) == encoded(data.get("model_content") or data.get("content")))
        if entry:
            ref = entry.get("tool_ref")
            check(f"{kind}_source", entry.get("source") == "local" if kind == "local_tool" else
                  entry.get("server_name") == "cache-probe-server" and entry.get("remote_name") == "cache_probe_echo")
            check(f"{kind}_complete_new_descriptor", ref and entry.get("input_schema")
                  and entry.get("already_active") is False and ref in encoded(new)
                  and all(ref not in encoded(e["data"]) for i, e in main if i < index))
            names = {t.get("name") or t.get("function", {}).get("name")
                     for _, e in main for t in e["data"].get("tools", [])}
            check(f"{kind}_not_in_top_level_tools", entry["name"] not in names)
            next_response = responses.get(ae.get("llm_call_id"), {}).get("data", {})
            routed = [t for t in next_response.get("tool_calls") or []
                      if t.get("function", {}).get("name") == "call_tool"
                      and t["function"].get("arguments", {}).get("tool_ref") == ref]
            ids = {t.get("id") for t in routed}
            check(f"{kind}_executed_via_ref", any(e.get("event") == "tool.response"
                  and e.get("tool_call_id") in ids and e.get("data", {}).get("tool_name") == entry["name"]
                  and e["data"].get("success") is True for e in events[ai:]))
        elif kind == "skill_catalog":
            check("catalog_without_body", "cache-probe-method" in str(data.get("content"))
                  and skill_receipt not in encoded(data))
        else:
            check("skill_body_new_in_tail", skill_receipt in encoded(new[len(old):])
                  and all(skill_receipt not in encoded(e["data"]) for i, e in main if i < index))
            check("skill_body_never_in_system", all(skill_receipt not in encoded(
                [m for m in e["data"]["messages"] if m.get("role") == "system"]) for _, e in main))
        prev, nxt = by_line[bi + 1], by_line[ai + 1]
        loads.append({"kind": kind, "tool_response_line": index + 1,
                      "before": prev, "next": nxt,
                      "cached_delta": nxt["cached_tokens"] - prev["cached_tokens"]
                      if nxt["cached_tokens"] is not None and prev["cached_tokens"] is not None else None})
    check("all_loading_boundaries_in_order", [load["kind"] for load in loads] == list(LOADS))
    return {"checks": checks, "calls": calls, "loads": loads,
            "history_rewrite_request_lines": [c["request_line"] for c in main_calls if not c["previous_messages_preserved"]],
            "main": totals(main_calls), "auxiliary": totals([c for c in calls if c["call_kind"] != "main"])}


def check_mcp(events: list[dict], server: list[dict], gate: dict, loads: list[dict], receipt: str) -> bool:
    """Both transport and agent evidence must prove registration DURING discovery."""
    try:
        pending = next(e for e in server if e["event"] == "tools_list_requested")
        returned = next(e for e in server if e["event"] == "tools_list_returned")
        executed = next(e for e in server if e["event"] == "tool_executed")
        load = next(item for item in loads if item["kind"] == "mcp_tool")
        search = next(e for e in events if e.get("event") == "tool.request"
                      and e.get("tool_call_id") == gate["tool_call_id"])
        response = events[load["tool_response_line"] - 1]
        after = events[load["next"]["request_line"] - 1]
        warmup = [e for e in events if e.get("event") == "llm.request"
                  and e.get("data", {}).get("call_kind") in (None, "", "main")][1]
        actual = next(e for e in events if e.get("event") == "tool.response"
                      and e.get("data", {}).get("tool_name") == "cache_probe_echo")
        times = [datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                 for e in (pending, search, gate, returned, response, after)]
        return (gate.get("reason") == "discovery" and not pending["gate_exists"]
                and datetime.fromisoformat(warmup["timestamp"].replace("Z", "+00:00")) < times[1]
                and search["data"]["tool_name"] == "tool_search"
                and search["tool_call_id"] == response["tool_call_id"]
                and times == sorted(times) and search["data"]["arguments"]["server_name"] == "cache-probe-server"
                and executed["arguments"] == {"text": "mid-task-mcp-ok"}
                and executed["result"]["receipt"] == receipt
                and json.loads(actual["data"]["content"]) == executed["result"])
    except (KeyError, ValueError, StopIteration, TypeError, IndexError):
        return False


def evidence_hashes(directory: Path) -> dict[str, str]:
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file() and not p.is_symlink()
            and not p.name.startswith(("cache-report", "cache-requests", "comparison", "evidence-index"))
            and p.name != "diagnosis.md"}


def verify_evidence(directory: Path) -> None:
    index = read_json(directory / "evidence-index.json")
    current = evidence_hashes(directory)
    if not index or index != current:
        raise ValueError("Evidence changed or is incomplete; preserve the original run and retry in a new run")


def analyze_run(directory: Path) -> dict:
    directory = directory.resolve()
    verify_evidence(directory)
    context = read_json(directory / "cache-context.json")
    if context.get("version") != VERSION:
        raise ValueError("Unsupported cache benchmark version")
    attempts = sorted(directory.glob(f"cases/{CASE_ID}/attempts/*"))
    if len(attempts) != 1:
        raise ValueError("Expected one immutable attempt; automatic retries are disabled")
    attempt = attempts[0]
    traces = sorted((attempt / "agent").glob("*.jsonl"))
    if len(traces) != 1:
        raise ValueError("Expected one complete ACP agent trace")
    events = read_events(traces[0])
    report = analyze_events(events, context["skill_receipt"])
    run = read_json(attempt / "run.json")
    complete = read_json(attempt / "completeness.json")
    checks = report["checks"]
    checks.append({"name": "raw_acp_evidence_present", "passed": all((attempt / name).is_file() for name in RAW_FILES)})
    checks.append({"name": "acp_completed", "passed": run.get("acp_status") == "completed"
                   and run.get("process_exit_code") == 0 and run.get("completeness_status") == "complete"
                   and complete.get("status") == "complete" and context.get("launcher_exit_code") == 0})
    for filename, expected in {"probe-result.txt": "before-tool\ntool-loaded-ok\n",
                               "skill-proof.txt": context["skill_receipt"] + "\n"}.items():
        path = attempt / "workspace" / filename
        checks.append({"name": f"artifact_{filename}", "passed": path.is_file() and path.read_text() == expected})
    server_path = directory / "mcp-server-events.jsonl"
    gate_path = directory / "mcp-gate.json"
    checks.append({"name": "real_mcp_registered_during_search", "passed": check_mcp(
        events, read_events(server_path) if server_path.exists() else [],
        read_json(gate_path) if gate_path.exists() else {}, report["loads"], context["mcp_receipt"])})
    checks.append({"name": "source_unchanged_during_run", "passed": context.get("source_unchanged") is True})
    checks.append({"name": "requested_model_used", "passed": all(c["model"] == context["conditions"]["model"]
                                                                 for c in report["calls"] if c["call_kind"] == "main")})
    passed = all(check["passed"] for check in checks)
    report.update(version=VERSION, run_dir=str(directory), attempt=str(attempt.relative_to(directory)),
                  trace=str(traces[0].relative_to(directory)), conditions=context["conditions"],
                  source=context["source"], completed=int(passed), quality="passed" if passed else "failed",
                  elapsed_seconds=(datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds(),
                  cache_measurement="measured" if passed and all(l["next"]["cache_hit_ratio"] is not None for l in report["loads"]) else "unverified")
    return report


def compare_reports(baseline: dict, candidate: dict) -> dict:
    mismatches = [key for key in sorted(baseline["conditions"].keys() | candidate["conditions"].keys())
                  if baseline["conditions"].get(key) != candidate["conditions"].get(key)]
    comparable = not mismatches and baseline.get("version") == candidate.get("version") == VERSION
    def delta(before, after):
        return after - before if comparable and before is not None and after is not None else None
    measured = comparable and all(r["cache_measurement"] == "measured" for r in (baseline, candidate))
    previous = {l["kind"]: l for l in baseline["loads"]}
    return {"version": VERSION, "baseline": baseline["run_dir"], "candidate": candidate["run_dir"],
            "comparable": comparable, "mismatches": mismatches, "cache_comparable": measured,
            "completion": {"baseline": baseline["completed"], "candidate": candidate["completed"]},
            "task_count_per_run": 1,
            "source": {side: {key: report["source"].get(key) for key in ("commit", "sha256")}
                       for side, report in (("baseline", baseline), ("candidate", candidate))},
            "elapsed_seconds": {"baseline": baseline["elapsed_seconds"], "candidate": candidate["elapsed_seconds"]},
            "main": {"baseline": baseline["main"], "candidate": candidate["main"]},
            "auxiliary": {"baseline": baseline["auxiliary"], "candidate": candidate["auxiliary"]},
            "elapsed_seconds_delta": delta(baseline["elapsed_seconds"], candidate["elapsed_seconds"]),
            "main_prompt_tokens_delta": delta(baseline["main"]["prompt_tokens"], candidate["main"]["prompt_tokens"]),
            "main_completion_tokens_delta": delta(baseline["main"]["completion_tokens"], candidate["main"]["completion_tokens"]),
            "loads": [{"kind": load["kind"], "baseline": previous.get(load["kind"], {}).get("next", {}).get("cache_hit_ratio"),
                       "candidate": load["next"]["cache_hit_ratio"],
                       "cache_percentage_points_delta": 100 * (load["next"]["cache_hit_ratio"] - previous[load["kind"]]["next"]["cache_hit_ratio"])
                       if measured else None} for load in candidate["loads"]]}


def render_report(report: dict) -> str:
    lines = ["# ACP context-loading cache evaluation", "",
             f"Quality: **{report['quality']}**; cache: **{report['cache_measurement']}**; elapsed: {report['elapsed_seconds']:.2f}s.", "",
             "| Load | Next trace line | Input tokens | Cached tokens | Cache rate | Cached delta |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for load in report["loads"]:
        call = load["next"]
        ratio = call["cache_hit_ratio"]
        rate = f"{ratio:.2%}" if ratio is not None else "unknown"
        lines.append(f"| {load['kind']} | {call['request_line']} | {call['prompt_tokens']} | {call['cached_tokens']} | {rate} | {load['cached_delta']} |")
    lines += ["", f"Main calls: `{encoded(report['main'])}`", "", f"Auxiliary calls: `{encoded(report['auxiliary'])}`", "",
              "Cache rate = upstream cached_tokens / prompt_tokens. Missing or invalid values are unknown, not zero.",
              "Compaction changes message history: this append-only loading benchmark cannot attribute a cache drop across compaction to loading.",
              "One run is an observation, not a statistical latency or provider routing guarantee.", "",
              f"Raw trace: [{report['trace']}]({report['trace']}). Evidence digests: [evidence-index.json](evidence-index.json).", "",
              "Failed checks: " + (", ".join(c["name"] for c in report["checks"] if not c["passed"]) or "none"), ""]
    return "\n".join(lines)


def save_report(directory: Path, report: dict) -> None:
    write_json(directory / "cache-report.json", report)
    with (directory / "cache-report.md").open("x", encoding="utf-8") as stream:
        stream.write(render_report(report))
    with (directory / "cache-requests.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(report["calls"][0]))
        writer.writeheader()
        writer.writerows(report["calls"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="explicitly send REAL model requests through the standard ACP runner")
    run.add_argument("--model", required=True, help="fixed model ID; auto routing is not a matched cache experiment")
    run.add_argument("--config", type=Path)
    run.add_argument("--auth-file", type=Path)
    run.add_argument("--timeout-seconds", type=float, default=900)
    run.add_argument("--title")
    run.add_argument("--baseline", type=Path)
    check = sub.add_parser("check", help="read evidence only; never call a model or change the run")
    check.add_argument("run_dir", type=Path)
    compare = sub.add_parser("compare", help="read two sealed runs, without modifying either")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, help="new JSON file; existing files are refused")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            from test_workspace.context_cache_run import run_probe
            return run_probe(args)
        if args.command == "check":
            report = analyze_run(args.run_dir)
            print(render_report(report))
            return 0 if report["cache_measurement"] == "measured" else 2
        report = compare_reports(analyze_run(args.baseline), analyze_run(args.candidate))
        if args.output:
            if any(args.output.resolve().is_relative_to(path.resolve()) for path in (args.baseline, args.candidate)):
                raise ValueError("Comparison output must be outside both sealed runs")
            write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["cache_comparable"] else 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(f"Cache evaluation unavailable: {type(error).__name__}: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
