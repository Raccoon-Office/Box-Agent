"""Small independent task oracles over the existing ACP evaluation evidence."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
from uuid import uuid4
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "test_workspace/inputs/quality_baseline"
VERSION = "p0-q0/v1"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def contained(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("evidence path escapes root")
    return path


def source_identity() -> dict:
    files = subprocess.check_output(
        ["git", "ls-files", "box_agent", "uv.lock"], cwd=ROOT, text=True,
    ).splitlines()
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
              for name in files if (ROOT / name).is_file()}
    from box_agent.config import Config
    config_path = Config.find_config_file("config.yaml")
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path else None
    return {
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": digest(hashes), "source_files": hashes,
        "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pack_sha256": digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(PACK.iterdir()) if p.is_file()}),
        "python": platform.python_version(), "platform": platform.platform(),
        "profile_config_sha256": config_hash,
        "environment": {key: os.environ.get(key) for key in
                        ("PYTHONUTF8", "BOX_AGENT_SESSION_TRACE_ENABLED", "BOX_AGENT_LOG_FULL_PAYLOAD")},
    }


def trace_records(attempt: Path) -> list[dict]:
    paths = sorted((attempt / "agent").glob("*.jsonl"))
    if not paths:
        raise ValueError("missing agent trace")
    records = []
    for path in paths:
        contained(attempt, str(path.relative_to(attempt)))
        records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return records


def tool_calls(records: list[dict]) -> list[dict]:
    calls: dict[str, dict] = {}
    for record in records:
        if record.get("event") in {"tool.request", "tool.response"}:
            call_id = record.get("tool_call_id")
            if call_id:
                calls.setdefault(call_id, {}).update(record["data"])
    return list(calls.values())


def request_inventory(records: list[dict]) -> list[dict]:
    requests = []
    for record in records:
        if record.get("event") != "llm.request":
            continue
        data = record["data"]
        system = [m for m in data["messages"] if m.get("role") == "system"]
        tools = data["tools"]
        requests.append({"model": data.get("model"), "provider": data.get("provider"),
                         "thinking_enabled": data.get("thinking_enabled"),
                         "system_sha256": digest(system), "tools_sha256": digest(tools),
                         "messages_sha256": digest(data["messages"]),
                         "system_chars": len(json.dumps(system, ensure_ascii=False)),
                         "tool_definitions": tools})
    return requests


def check_rule(rule: dict, attempt: Path, calls: list[dict]) -> bool:
    kind = rule["kind"]
    if kind == "tool":
        return any(call.get("tool_name") == rule["name"]
                   and (not rule.get("success") or call.get("success") is True)
                   and all(call.get("arguments", {}).get(key) == value
                           for key, value in rule.get("arguments", {}).items())
                   for call in calls)
    if kind == "recovery":
        def matches(call: dict, name: str, success: bool) -> bool:
            path = str(call.get("arguments", {}).get("path", "")).replace("\\", "/")
            return (call.get("tool_name") == "read_file" and call.get("success") is success
                    and path.rsplit("/", 1)[-1] == name)
        return any(matches(call, rule["missing"], False)
                   and any(matches(later, rule["source"], True) for later in calls[index + 1:])
                   for index, call in enumerate(calls))
    if kind == "no_tool":
        return not any(call.get("tool_name") in rule["names"] for call in calls)
    path = contained(attempt / "workspace", rule["path"])
    if kind == "absent":
        return not path.exists()
    if not path.is_file():
        return False
    if kind == "json":
        return json.loads(path.read_text(encoding="utf-8")) == rule["value"]
    if kind == "text":
        return path.read_text(encoding="utf-8").strip() == rule["value"]
    if kind == "xlsx":
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with ZipFile(path) as archive:
            if not {"[Content_Types].xml", "xl/workbook.xml"}.issubset(archive.namelist()):
                return False
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = ["".join(node.itertext()) for node in
                          ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))]
            sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            cells = {}
            for cell in sheet.findall(".//s:c", ns):
                if cell.get("r") in rule.get("numeric", []) and cell.get("t", "n") != "n":
                    return False
                value = cell.findtext("s:v", default="", namespaces=ns)
                if cell.get("t") == "s":
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(cell.find("s:is", ns).itertext())
                cells[cell.get("r")] = value
            return all(cells.get(key) == value for key, value in rule["cells"].items())
    raise ValueError(f"unknown oracle: {kind}")


def evaluate_attempt(attempt: Path, spec: dict) -> dict:
    result = {"case_id": spec["id"], "split": spec["split"], "attempt": attempt.name,
              "quality": "unverified", "checks": [], "latency_seconds": None,
              "token_usage": None, "cost": None, "cost_reason": "no verified price/billing source"}
    try:
        run = read_json(attempt / "run.json")
        result["acp_status"] = run.get("acp_status")
        result["token_usage"] = run.get("token_usage")
        result["runtime"] = run.get("runtime")
        result["model_config_sha256"] = run.get("model_config_sha256")
        result["case_fingerprint"] = run.get("case_fingerprint")
        if run.get("started_at") and run.get("finished_at"):
            result["latency_seconds"] = (datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
                                         - datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))).total_seconds()
        completeness = read_json(attempt / "completeness.json")
        if (run.get("completeness_status") not in {"complete", "complete_with_warnings"}
                or completeness.get("status") not in {"complete", "complete_with_warnings"}
                or run.get("input_consistency", {}).get("status") != "matched"):
            result["reason"] = "incomplete or inconsistent capture"
            return result
        records = trace_records(attempt)
        result["requests"] = request_inventory(records)
        if not result["requests"]:
            raise ValueError("missing model request")
        calls = tool_calls(records)
        result["tool_calls"] = len(calls)
        result["failed_tool_calls"] = sum(call.get("success") is False for call in calls)
        result["parameter_error_calls"] = sum("INVALID_TOOL_ARGUMENTS" in str(call.get("error", "")) for call in calls)
        result["discovery_calls"] = sum(call.get("tool_name") == "tool_search" for call in calls)
        result["llm_timings"] = [r["data"].get("timing") for r in records
                                 if r.get("event") in {"llm.response", "llm.error"}]
        # Dataset input bytes must still match the fixed synthetic task pack.
        before = read_json(attempt / "files-before.json")
        fingerprints = {entry["path"]: entry.get("sha256") for entry in before["files"]}
        input_record = read_json(attempt.parent.parent / "input.json")
        for name in input_record["input_files"]:
            expected = hashlib.sha256(contained(PACK, name).read_bytes()).hexdigest()
            if fingerprints.get(Path(name).name) != expected:
                raise ValueError("input bytes differ from task pack")
            if spec["id"] != "q0-file":
                actual = hashlib.sha256(contained(attempt / "workspace", Path(name).name).read_bytes()).hexdigest()
                if actual != expected:
                    result["quality"] = "failed"
                    result["reason"] = "input preservation constraint violated"
                    return result
        for rule in spec["checks"]:
            try:
                result["checks"].append(check_rule(rule, attempt, calls))
            except (json.JSONDecodeError, BadZipFile, ElementTree.ParseError):
                # Captured evidence is valid but the delivered artifact is malformed.
                result["checks"].append(False)
        state_matches = run.get("acp_status") == "completed"
        if spec.get("expected_state") == "waiting_for_user":
            metadata = run.get("response_metadata") or {}
            state_matches = (run.get("acp_status") == "incomplete"
                             and metadata.get("ok") is True
                             and metadata.get("runStatus") == "waiting_for_user"
                             and metadata.get("lastStopReason") == "waiting_for_user")
        result["quality"] = "passed" if all(result["checks"]) and state_matches else "failed"
    except (OSError, ValueError, KeyError, TypeError, IndexError, BadZipFile, ElementTree.ParseError) as error:
        result["reason"] = f"invalid evidence: {type(error).__name__}"
    return result


def evaluate_run(directory: Path) -> dict:
    specs = read_json(PACK / "oracles.json")["cases"]
    selection = read_json(directory / "selection.json")
    selected = selection["case_ids"]
    known = {spec["id"] for spec in specs}
    if not selected or set(selected) - known or len(selected) != len(set(selected)):
        raise ValueError("invalid baseline case selection")
    results = []
    for spec in specs:
        if spec["id"] not in selected:
            continue
        case_dir = contained(directory, f"cases/{spec['id']}")
        # Report every immutable attempt, including failures; never cherry-pick latest.
        attempts = sorted((case_dir / "attempts").glob("attempt-*"))
        if not attempts:
            results.append({"case_id": spec["id"], "split": spec["split"],
                            "quality": "unverified", "reason": "no captured attempt"})
        for attempt in attempts:
            contained(directory, str(attempt.relative_to(directory)))
            try:
                actual_input = read_json(case_dir / "input.json")
            except (OSError, ValueError):
                results.append({"case_id": spec["id"], "quality": "unverified", "reason": "missing or invalid task evidence"})
                continue
            expected_input = next(json.loads(line) for line in
                                  (PACK / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
                                  if json.loads(line)["id"] == spec["id"])
            if actual_input != expected_input:
                results.append({"case_id": spec["id"], "quality": "unverified", "reason": "task differs from baseline"})
            else:
                results.append(evaluate_attempt(attempt, spec))
    return {"schema_version": VERSION, "evidence_kind": "real_model_acp_capture_or_unverified",
            "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "results": results, "counts": {status: sum(r["quality"] == status for r in results)
                                            for status in ("passed", "failed", "unverified")}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    models = run.add_mutually_exclusive_group(required=True)
    models.add_argument("--model")
    models.add_argument("--catalog-model")
    run.add_argument("--case-id", action="append", required=True)
    check = sub.add_parser("check")
    check.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "run":
        from test_workspace import run_acp_eval
        title = f"q0-{uuid4().hex[:8]}"
        identity = source_identity()
        arguments = ["--dataset", str(PACK / "dataset.jsonl"), "--title", title,
                     "--timeout-seconds", "180", "--parallelism", "1", "--seed", "13"]
        if args.catalog_model:
            catalog = read_json(ROOT / "test_workspace/evaluation_models.json")
            binding = next(item["binding"] for item in catalog["models"] if item["id"] == args.catalog_model)
            binding["maxTokens"] = 4096
            for candidate in binding.get("autoRouting", {}).get("models", []):
                candidate["maxTokens"] = 4096
            arguments += ["--model-binding-json", json.dumps(binding)]
        else:
            arguments += ["--model", args.model, "--model-max-tokens", "4096"]
        for case in args.case_id:
            arguments += ["--case-id", case]
        try:
            code = run_acp_eval.main(arguments)
        except (RuntimeError, OSError) as error:
            # The standard runner deliberately creates no directory on auth failure.
            # Keep a separate failed preflight record without copying exception text.
            directory = ROOT / "test_workspace/outputs" / run_acp_eval.run_name(title=title)
            if directory.exists():
                raise
            directory.mkdir(parents=True)
            for name, document in {
                "selection.json": {"case_ids": args.case_id, "model": args.model,
                                   "catalog_model": args.catalog_model},
                "manifest.json": {"status": "preflight_failed", "error_type": type(error).__name__},
                "summary.json": {"model_calls": 0, "quality": "unverified"},
            }.items():
                (directory / name).write_text(json.dumps(document, indent=2), encoding="utf-8")
            code = 2
        directories = list((ROOT / "test_workspace/outputs").glob(f"*-{title}"))
        if len(directories) != 1:
            return code or 2
        directory = directories[0]
        (directory / "baseline-context.json").write_text(json.dumps(identity, indent=2), encoding="utf-8")
    else:
        directory = args.run_dir.resolve()
    report = evaluate_run(directory)
    target = directory / f"quality-{uuid4().hex[:8]}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(target), "counts": report["counts"]}, ensure_ascii=False))
    return 0 if report["counts"]["failed"] == report["counts"]["unverified"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
