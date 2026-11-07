"""Offline cache contracts: synthetic usage, sealed ACP evidence, local stdio only."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_workspace import context_cache as cache
from test_workspace import context_cache_run as driver


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Offline cache checks must not access the network")
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def sample_trace():
    events, messages = [], [{"role": "system", "content": "BASE"}, {"role": "user", "content": "task"}]
    tools = [{"name": "tool_search"}, {"name": "call_tool"}, {"name": "get_skill"}, {"name": "list_skills"}]
    mcp_result = {"receipt": "MCP_PROOF", "echo": "mid-task-mcp-ok",
                  "sha256": hashlib.sha256(b"mid-task-mcp-ok").hexdigest()}

    def event(kind, data, **ids):
        item = {"event": kind, "data": data, "timestamp": (
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=len(events) + 1)).isoformat(), **ids}
        events.append(item)
        return item

    def descriptor(name, ref, source):
        origin = {"source": "local"} if source == "local" else {"server_name": "cache-probe-server", "remote_name": name}
        return json.dumps({"activated": [{"name": name, "tool_ref": ref, **origin,
                                          "already_active": False, "input_schema": {"type": "object"}}]})

    steps = [
        ("read_file", {}, "read_file", "warm one"), ("read_file", {}, "read_file", "warm two"),
        ("tool_search", {"tool_names": ["append_file"]}, "tool_search", descriptor("append_file", "local-ref", "local")),
        ("call_tool", {"tool_ref": "local-ref"}, "append_file", "appended"),
        ("list_skills", {}, "list_skills", "cache-probe-method"),
        ("get_skill", {}, "get_skill", "SKILL_PROOF"),
        ("write_file", {}, "write_file", "written"),
        ("tool_search", {"server_name": "cache-probe-server"}, "tool_search", descriptor("cache_probe_echo", "mcp-ref", "mcp")),
        ("call_tool", {"tool_ref": "mcp-ref"}, "cache_probe_echo", json.dumps(mcp_result)),
        (None, {}, None, "done"),
    ]
    for number, (name, arguments, target, result) in enumerate(steps, 1):
        event("llm.request", {"model": "model-a", "request_session_id": "fixed-session",
                              "messages": deepcopy(messages), "tools": deepcopy(tools)}, llm_call_id=f"llm-{number}")
        call = {"id": f"tool-{number}", "function": {"name": name, "arguments": arguments}}
        event("llm.response", {"usage": {"prompt_tokens": 1000 + number * 100, "completion_tokens": 10,
                                          "cached_tokens": 900}, "tool_calls": [call] if name else []}, llm_call_id=f"llm-{number}")
        if name:
            messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
            event("tool.request", {"tool_name": target, "arguments": arguments}, tool_call_id=call["id"])
            event("tool.response", {"tool_name": target, "success": True, "content": result}, tool_call_id=call["id"])
            messages.append({"role": "tool", "name": name, "tool_call_id": call["id"], "content": result})
        if name == "get_skill":
            # Auxiliary work between a load and the next main request is not its cache boundary.
            event("llm.request", {"model": "judge", "call_kind": "judge", "messages": [], "tools": []}, llm_call_id="judge")
            event("llm.response", {"usage": {"prompt_tokens": 50, "completion_tokens": 3, "cached_tokens": 0}}, llm_call_id="judge")
    search = next(e for e in events if e["event"] == "tool.request" and e.get("tool_call_id") == "tool-8")
    returned = next(e for e in events if e["event"] == "tool.response" and e.get("tool_call_id") == "tool-8")
    stamp = datetime.fromisoformat(search["timestamp"])
    gate = {"reason": "discovery", "tool_call_id": "tool-8", "timestamp": (stamp + timedelta(milliseconds=100)).isoformat()}
    server = [{"event": "tools_list_requested", "gate_exists": False, "timestamp": "2026-01-01T00:00:00+00:00"},
              {"event": "tools_list_returned", "timestamp": (stamp + timedelta(milliseconds=200)).isoformat()},
              {"event": "tool_executed", "arguments": {"text": "mid-task-mcp-ok"}, "result": mcp_result,
               "timestamp": returned["timestamp"]}]
    return events, server, gate


def write_attempt(directory):
    events, server, gate = sample_trace()
    attempt = directory / f"cases/{cache.CASE_ID}/attempts/first"
    (attempt / "agent").mkdir(parents=True)
    (attempt / "workspace").mkdir()
    (attempt / "agent/main.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    for name in ("acp-stdin.raw", "acp-stdout.raw", "protocol.jsonl", "stderr.log", "process.jsonl",
                 "files-before.json", "files-after.json", "artifacts.json", "assistant.txt", "manifest.json"):
        (attempt / name).write_text("{}\n")
    cache.write_json(attempt / "run.json", {"acp_status": "completed", "process_exit_code": 0,
                     "completeness_status": "complete", "started_at": "2026-01-01T00:00:00+00:00",
                     "finished_at": "2026-01-01T00:01:00+00:00"})
    cache.write_json(attempt / "completeness.json", {"status": "complete"})
    (attempt / "workspace/probe-result.txt").write_text("before-tool\ntool-loaded-ok\n")
    (attempt / "workspace/skill-proof.txt").write_text("SKILL_PROOF\n")
    return server, gate


def sealed_run(directory):
    server, gate = write_attempt(directory)
    (directory / "mcp-server-events.jsonl").write_text("\n".join(json.dumps(e) for e in server) + "\n")
    cache.write_json(directory / "mcp-gate.json", gate)
    cache.write_json(directory / "cache-context.json", {
        "version": cache.VERSION, "conditions": {"model": "model-a", "fixture_sha256": "fixture"},
        "skill_receipt": "SKILL_PROOF", "mcp_receipt": "MCP_PROOF", "source": {"commit": "before"},
        "source_unchanged": True, "launcher_exit_code": 0,
    })
    cache.write_json(directory / "evidence-index.json", cache.evidence_hashes(directory))
    return directory


@pytest.mark.parametrize("value,expected", [(0, 0), (800, 800), (None, None), (-1, None),
                                           (True, None), (1.2, None), (1201, None)])
def test_cache_usage_never_converts_missing_or_invalid_to_zero(value, expected):
    metrics = cache.usage_metrics({"prompt_tokens": 1200, "cached_tokens": value})
    assert metrics["cached_tokens"] == expected
    assert metrics["cache_hit_ratio"] == (expected / 1200 if expected is not None else None)
    assert cache.usage_metrics({"prompt_tokens": 1200})["cache_hit_ratio"] is None
    assert cache.usage_metrics("malformed")["cache_hit_ratio"] is None


def test_four_boundaries_measure_the_immediate_main_response_separately_from_judge():
    report = cache.analyze_events(sample_trace()[0], "SKILL_PROOF")
    assert all(c["passed"] for c in report["checks"]), report["checks"]
    assert [l["kind"] for l in report["loads"]] == list(cache.LOADS)
    assert [l["next"]["llm_call_id"] for l in report["loads"]] == ["llm-4", "llm-6", "llm-7", "llm-9"]
    assert report["main"]["cached_tokens"] == 9000
    assert report["main"]["known_cache_hit_ratio"] == 9000 / 15500
    assert report["auxiliary"]["cached_tokens"] == 0
    assert report["auxiliary"]["calls"] == 1


def test_missing_usage_is_partial_and_history_rewrite_is_not_a_loading_pass():
    events, _, _ = sample_trace()
    next(e for e in events if e["event"] == "llm.response" and e["llm_call_id"] == "llm-7")["data"]["usage"].pop("cached_tokens")
    report = cache.analyze_events(events, "SKILL_PROOF")
    assert report["loads"][2]["next"]["cache_hit_ratio"] is None
    assert report["main"]["cached_tokens"] is None
    assert report["main"]["cache_known_calls"] == 9
    request = next(e for e in events if e["event"] == "llm.request" and e["llm_call_id"] == "llm-7")
    request["data"]["messages"] = [request["data"]["messages"][0], {"role": "user", "content": "compacted summary"}]
    report = cache.analyze_events(events, "SKILL_PROOF")
    assert not next(c["passed"] for c in report["checks"] if c["name"] == "append_only_messages")


@pytest.mark.parametrize("mutation", ["schema", "missing_response", "duplicate", "hidden_name", "missing_load"])
def test_bad_evidence_cannot_pass_loading_contract(mutation):
    events, _, _ = sample_trace()
    if mutation == "schema":
        events[-2]["data"]["tools"].append({"name": "append_file"})
    elif mutation == "missing_response":
        events.pop()
    elif mutation == "duplicate":
        events.append(deepcopy(events[-1]))
    elif mutation == "hidden_name":
        next(e for e in events if e["event"] == "llm.response" and e["llm_call_id"] == "llm-4")["data"]["tool_calls"][0]["function"]["name"] = "append_file"
    else:
        next(e for e in events if e["event"] == "tool.response" and e["tool_call_id"] == "tool-6")["data"]["success"] = False
    assert any(not c["passed"] for c in cache.analyze_events(events, "SKILL_PROOF")["checks"])


def test_mcp_requires_real_registration_order_and_matching_execution_receipt():
    events, server, gate = sample_trace()
    loads = cache.analyze_events(events, "SKILL_PROOF")["loads"]
    assert cache.check_mcp(events, server, gate, loads, "MCP_PROOF")
    for key, value in (("reason", "cleanup_after_turn_end"), ("timestamp", "2025-01-01T00:00:00+00:00")):
        assert not cache.check_mcp(events, server, {**gate, key: value}, loads, "MCP_PROOF")
    assert not cache.check_mcp(events, server, gate, loads, "wrong-receipt")


def test_malformed_trace_fails_with_explicit_evidence_error():
    with pytest.raises(ValueError, match="Malformed"):
        cache.analyze_events([{"event": "llm.request", "data": None}], "marker")


def test_sealed_reports_and_comparisons_leave_baselines_unchanged(tmp_path):
    root = sealed_run(tmp_path / "before")
    before = cache.evidence_hashes(root)
    baseline = cache.analyze_run(root)
    assert baseline["quality"] == "passed" and baseline["cache_measurement"] == "measured"
    candidate = deepcopy(baseline)
    candidate["source"] = {"commit": "after"}
    candidate["elapsed_seconds"] = 62
    candidate["loads"][0]["next"]["cache_hit_ratio"] -= 0.1
    comparison = cache.compare_reports(baseline, candidate)
    assert comparison["cache_comparable"] and comparison["elapsed_seconds_delta"] == 2
    assert comparison["loads"][0]["cache_percentage_points_delta"] == pytest.approx(-10)
    candidate["conditions"]["model"] = "other"
    mismatch = cache.compare_reports(baseline, candidate)
    assert mismatch["mismatches"] == ["model"] and not mismatch["cache_comparable"]
    assert mismatch["loads"][0]["cache_percentage_points_delta"] is None
    assert cache.evidence_hashes(root) == before
    cache.save_report(root, baseline)
    with pytest.raises(FileExistsError):
        cache.save_report(root, baseline)
    assert cache.analyze_run(root)["quality"] == "passed"  # Sidecar reports are not raw evidence.
    trace = next(root.glob("cases/*/attempts/*/agent/*.jsonl"))
    trace.write_text(trace.read_text() + "{}\n")
    with pytest.raises(ValueError, match="Evidence changed"):
        cache.analyze_run(root)


def test_failed_completion_or_missing_cache_is_never_a_verified_cache_comparison(tmp_path):
    report = cache.analyze_run(sealed_run(tmp_path / "run"))
    candidate = deepcopy(report)
    candidate.update(cache_measurement="unverified", completed=0, quality="failed")
    candidate["loads"][0]["next"]["cache_hit_ratio"] = None
    compared = cache.compare_reports(report, candidate)
    assert compared["comparable"] and not compared["cache_comparable"]
    assert compared["completion"] == {"baseline": 1, "candidate": 0}
    assert all(l["cache_percentage_points_delta"] is None for l in compared["loads"])


def test_cli_compare_is_offline_and_never_overwrites_reports(tmp_path):
    before = sealed_run(tmp_path / "before")
    after = sealed_run(tmp_path / "after")
    output = tmp_path / "comparison.json"
    arguments = ["compare", "--baseline", str(before), "--candidate", str(after), "--output", str(output)]
    assert cache.main(arguments) == 0
    assert cache.read_json(output)["cache_comparable"]
    assert cache.main(arguments) == 2
    assert cache.main([*arguments[:-1], str(before / "new-report.json")]) == 2
    assert not (before / "new-report.json").exists()
    assert cache.main(["check", str(before)]) == 0
    cache.verify_evidence(before)
    cache.verify_evidence(after)


@pytest.mark.parametrize("failure", ["missing_cache", "missing_raw", "wrong_artifact", "incomplete_acp", "source_changed"])
def test_run_status_requires_semantic_and_raw_evidence_not_just_acp_completed(tmp_path, failure):
    root = sealed_run(tmp_path / "run")
    attempt = next(root.glob("cases/*/attempts/*"))
    if failure == "missing_cache":
        trace = next((attempt / "agent").glob("*.jsonl"))
        events = cache.read_events(trace)
        next(e for e in events if e["event"] == "llm.response" and e["llm_call_id"] == "llm-7")["data"]["usage"].pop("cached_tokens")
        trace.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    elif failure == "missing_raw":
        (attempt / "acp-stdin.raw").unlink()
    elif failure == "wrong_artifact":
        (attempt / "workspace/skill-proof.txt").write_text("incorrect\n")
    elif failure == "incomplete_acp":
        (attempt / "completeness.json").write_text('{"status":"partial"}')
    else:
        context = cache.read_json(root / "cache-context.json")
        context["source_unchanged"] = False
        (root / "cache-context.json").write_text(json.dumps(context))
    # Seal the incomplete run as it would have been captured, without claiming it passed.
    (root / "evidence-index.json").unlink()
    cache.write_json(root / "evidence-index.json", cache.evidence_hashes(root))
    report = cache.analyze_run(root)
    assert report["cache_measurement"] == "unverified"
    assert report["quality"] == ("passed" if failure == "missing_cache" else "failed")


def test_config_comparison_ignores_credentials_and_temporary_profile_only(tmp_path):
    source = {"api_key": "do-not-archive", "auth_file": "/private/auth.json", "model": "old"}
    first = driver.isolated_config(source, tmp_path / "a", "model-a")
    second = driver.isolated_config({**source, "api_key": "rotated"}, tmp_path / "b", "model-a")
    assert driver.config_fingerprint(first, tmp_path / "a") == driver.config_fingerprint(second, tmp_path / "b")
    second["context_window"] = 32000
    assert driver.config_fingerprint(first, tmp_path / "a") != driver.config_fingerprint(second, tmp_path / "b")
    assert source["model"] == "old"
    assert first["agent"]["max_steps"] > 10 + 13  # Leave room for the kernel's wrap-up budget.


def test_output_titles_are_literal_and_mismatched_baseline_fails_before_auth(tmp_path, monkeypatch):
    special = tmp_path / "261107-1200-cache-[1]"
    special.mkdir()
    assert driver.output_directories(tmp_path, "cache-[1]") == [special]
    assert driver.output_directories(tmp_path, "cache-*") == []
    config = tmp_path / "source.yaml"
    config.write_text("provider: openai\n")
    monkeypatch.setattr(driver, "source_identity", lambda root: {"commit": "same"})
    from test_workspace import refresh_box_agent_auth
    def unexpected(**kwargs):
        pytest.fail("A mismatched baseline must fail before refreshing credentials or launching ACP")
    monkeypatch.setattr(refresh_box_agent_auth, "ensure_fresh_auth", unexpected)
    monkeypatch.setattr(driver, "launch", unexpected)
    args = SimpleNamespace(model="model-a", config=config, auth_file=None, timeout_seconds=900,
                           title="offline-mismatch", baseline=sealed_run(tmp_path / "baseline"))
    with pytest.raises(ValueError, match="Baseline settings"):
        driver.run_probe(args)


def test_gate_opens_only_after_explicit_mcp_search(tmp_path):
    events, _, _ = sample_trace()
    trace = tmp_path / "log/acp-eval/attempt/agent.jsonl"
    trace.parent.mkdir(parents=True)
    target = next(i for i, e in enumerate(events) if e["event"] == "tool.request" and e.get("tool_call_id") == "tool-8")
    trace.write_text("\n".join(json.dumps(e) for e in events[:target]))
    driver.release_gate(tmp_path)
    assert not (tmp_path / "mcp-gate.json").exists()
    trace.write_text("\n".join(json.dumps(e) for e in events[:target + 1]))
    driver.release_gate(tmp_path)
    assert cache.read_json(tmp_path / "mcp-gate.json")["tool_call_id"] == "tool-8"


@pytest.mark.asyncio
async def test_local_stdio_fixture_delays_catalog_then_executes_real_echo(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    gate, events = tmp_path / "gate.json", tmp_path / "events.jsonl"
    params = StdioServerParameters(command=sys.executable, args=[str(cache.PACK / "mcp_server.py"),
        "--gate", str(gate), "--events", str(events), "--receipt", "test-receipt", "--timeout", "8"])

    async def exercise():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listing = asyncio.create_task(client.list_tools())
                try:
                    while not events.exists() or "tools_list_requested" not in events.read_text():
                        await asyncio.sleep(0.01)
                    assert not listing.done()
                    cache.write_json(gate, {"reason": "offline_transport_smoke"})
                    assert [t.name for t in (await listing).tools] == ["cache_probe_echo"]
                    result = await client.call_tool("cache_probe_echo", {"text": "hello"})
                    assert json.loads(result.content[0].text)["receipt"] == "test-receipt"
                    bad = await client.call_tool("cache_probe_echo", {"text": 42})
                    assert bad.isError
                finally:
                    if not listing.done():
                        listing.cancel()
                        await asyncio.gather(listing, return_exceptions=True)
    await asyncio.wait_for(exercise(), timeout=15)


def test_live_driver_uses_standard_acp_entry_and_removes_private_config(tmp_path, monkeypatch):
    config = tmp_path / "source.yaml"
    config.write_text("api_key: do-not-archive\nprovider: openai\n")
    root = cache.ROOT
    isolated_profiles = []
    # Keep real command construction; no process or provider is launched in this test.
    monkeypatch.setattr(driver, "source_identity", lambda root: {"commit": "source", "sha256": "same"})
    monkeypatch.setattr(driver.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    def launch(command, env, profile, timeout, repo):
        assert command[1] == str(repo / "test_workspace/run_acp_eval.py")
        assert env["BOX_AGENT_HOME"] == str(profile)
        assert command[command.index("--parallelism") + 1] == "1"
        assert command[command.index("--rate-limit-retries") + 1] == "0"
        assert (profile / "config/config.yaml").stat().st_mode & 0o777 == 0o600
        isolated_profiles.append(profile)
        raise OSError("simulated launcher failure")
    monkeypatch.setattr(driver, "launch", launch)
    # Use a minimal repo layout for retained driver-failure evidence.
    for source in [root / "test_workspace/run_acp_eval.py", root / "test_workspace/context_cache_run.py"]:
        target = tmp_path / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    args = SimpleNamespace(model="model-a", config=config, auth_file=None, timeout_seconds=30,
                           title="offline-driver", baseline=None)
    with pytest.raises(ValueError, match="one immutable attempt"):
        driver.run_probe(args, root=tmp_path)
    assert len(isolated_profiles) == 1 and not isolated_profiles[0].exists()
    evidence = next((tmp_path / "test_workspace/outputs").iterdir())
    assert {"selection.json", "manifest.json", "summary.json", "cache-context.json", "evidence-index.json"} <= {p.name for p in evidence.iterdir()}
    assert all("do-not-archive" not in p.read_text() for p in evidence.rglob("*") if p.is_file())
    cache.verify_evidence(evidence)
