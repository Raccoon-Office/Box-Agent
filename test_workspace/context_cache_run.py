"""Explicit live driver; ACP transport and evidence capture stay in run_acp_eval."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from uuid import uuid4

import yaml

from test_workspace import run_acp_eval
from test_workspace.context_cache import (
    CASE_ID, PACK, ROOT, VERSION, analyze_run, compare_reports, digest,
    evidence_hashes, save_report, write_json,
)


def source_identity(root: Path = ROOT) -> dict:
    paths = set(subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "box_agent", "uv.lock"],
        cwd=root, text=True,
    ).splitlines())
    hashes = {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
              for p in sorted(paths) if (root / p).is_file()}
    return {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "files": hashes, "sha256": digest(hashes)}


def isolated_config(data: dict, profile: Path, model: str) -> dict:
    data = deepcopy(data)
    data.update(model=model, max_output_tokens=4096, auth_file=str(profile / "config/auth.json"),
                retry={"enabled": False, "max_retries": 0})
    data.setdefault("agent", {}).update(max_steps=40, enable_memory=False, enable_memory_extraction=False,
                                         tool_interface_version=2, memory_dir=str(profile / "memory"),
                                         workspace_dir=str(profile / "workspace"))
    tools = data.setdefault("tools", {})
    tools.update(enable_skills=True, enable_mcp=True, enable_file_tools=True,
                 skills_dir=str(profile / "skills"), mcp_config_path=str(profile / "config/mcp.json"))
    tools.setdefault("mcp", {})["deferred_loading_enabled"] = True
    for section in ("lite_llm", "image_generation"):
        if isinstance(data.get(section), dict):
            data[section]["auth_file"] = str(profile / "config/auth.json")
    if isinstance(data.get("lite_llm"), dict):
        data["lite_llm"]["retry"] = {"enabled": False, "max_retries": 0}
    return data


def config_fingerprint(data: dict, profile: Path) -> str:
    """Credentials are neither copied to evidence nor hashed into comparison keys."""
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in sorted(value.items())
                    if key not in {"api_key", "auth_file", "access_token", "refresh_token"}}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return value.replace(str(profile), "<isolated-profile>")
        return value
    return digest(normalize(data))


def output_directories(outputs: Path, title: str) -> list[Path]:
    # Titles are literal directory suffixes, not glob expressions.
    return sorted(p for p in outputs.iterdir() if p.is_dir() and p.name.endswith(f"-{title}")) if outputs.exists() else []


def release_gate(profile: Path) -> None:
    gate = profile / "mcp-gate.json"
    if gate.exists():
        return
    for trace in sorted((profile / "log/acp-eval").glob("*/*.jsonl")):
        # A concurrent final line may be incomplete; revisit it on the next poll.
        for line in trace.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            data = event.get("data", {})
            search = (event.get("event") == "tool.request" and data.get("tool_name") == "tool_search"
                      and data.get("arguments", {}).get("server_name") == "cache-probe-server")
            if search or event.get("event") == "turn.end":
                write_json(gate, {"timestamp": datetime.now(timezone.utc).isoformat(),
                                  "reason": "discovery" if search else "cleanup_after_turn_end",
                                  "tool_call_id": event.get("tool_call_id")})
                return


def launch(command: list[str], env: dict, profile: Path, timeout: float, root: Path = ROOT) -> int:
    process = subprocess.Popen(command, cwd=root, env=env, start_new_session=os.name != "nt")
    deadline = time.monotonic() + timeout + 120  # ACP startup and evidence finalization allowance.
    try:
        while process.poll() is None:
            release_gate(profile)
            if time.monotonic() >= deadline:
                raise TimeoutError("ACP driver exceeded its bounded shutdown allowance")
            time.sleep(0.2)
        return process.returncode
    finally:
        if process.poll() is None:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()


def run_probe(args, root: Path = ROOT, pack: Path = PACK) -> int:
    from box_agent.config import Config
    from box_agent.llm.base import HOSTED_AUTH_API_KEY_PLACEHOLDERS
    from test_workspace.refresh_box_agent_auth import ensure_fresh_auth

    if not args.model.strip() or args.model.strip().lower() == "auto":
        raise ValueError("Choose an explicit fixed model ID")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be finite and positive")
    title = args.title or f"context-cache-{uuid4().hex[:8]}"
    run_acp_eval.run_name(title=title)  # Same validation as the owning runner.
    outputs = root / "test_workspace/outputs"
    if output_directories(outputs, title):
        raise FileExistsError("This evaluation title already exists; choose a new title")
    baseline = analyze_run(args.baseline) if args.baseline else None
    config_path = (args.config or Config.get_default_config_path()).expanduser().resolve()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Expected a YAML model configuration")
    source = source_identity(root)
    with tempfile.TemporaryDirectory(prefix="box-agent-context-cache-") as temporary:
        profile = Path(temporary).resolve()
        (profile / "config").mkdir(mode=0o700)
        fixtures = profile / "inputs"
        shutil.copytree(pack, fixtures, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        skill_receipt, mcp_receipt = f"SKILL_RECEIPT_{uuid4().hex}", f"MCP_RECEIPT_{uuid4().hex}"
        body = (fixtures / "SKILL.md").read_text(encoding="utf-8").replace("{{SKILL_RECEIPT}}", skill_receipt)
        (fixtures / "SKILL.md").write_text(body, encoding="utf-8")
        skill_dir = profile / "skills/cache-probe-method"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
        configured = isolated_config(data, profile, args.model)
        cfg = profile / "config/config.yaml"
        cfg.write_text(yaml.safe_dump(configured, allow_unicode=True, sort_keys=False), encoding="utf-8")
        cfg.chmod(0o600)
        mcp = {"mcpServers": {"cache-probe-server": {
            "command": str(root / ".venv/bin/python"),
            "args": [str(fixtures / "mcp_server.py"), "--gate", str(profile / "mcp-gate.json"),
                     "--events", str(profile / "mcp-server-events.jsonl"), "--receipt", mcp_receipt,
                     "--timeout", str(args.timeout_seconds)],
            "alwaysLoad": False, "connect_timeout": args.timeout_seconds + 30, "execute_timeout": 20,
        }}}
        write_json(profile / "config/mcp.json", mcp)
        conditions = {"model": args.model, "config_sha256": config_fingerprint(configured, profile),
                      "fixture_sha256": digest({p: sha for p, sha in evidence_hashes(pack).items()
                                                if p != "README.md" and "__pycache__" not in Path(p).parts}),
                      "timeout_seconds": args.timeout_seconds,
                      "max_steps": 40, "max_output_tokens": 4096, "tool_interface_version": 2,
                      "memory_enabled": False, "parallelism": 1, "rate_limit_retries": 0,
                      "mcp_transport": "stdio", "mcp_gate": "first_explicit_server_search",
                      "runner_sha256": digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                                               for p in [root / "test_workspace/run_acp_eval.py",
                                                         root / "test_workspace/context_cache_run.py",
                                                         *sorted((root / "test_workspace/acp_eval/src").rglob("*.py"))]})}
        if baseline and baseline["conditions"] != conditions:
            raise ValueError("Baseline settings or fixtures differ; use a matched baseline or run without --baseline")
        model_key = str(data.get("api_key") or "").strip()
        # Refresh the owning credential BEFORE copying it (refresh tokens can rotate).
        if model_key in HOSTED_AUTH_API_KEY_PLACEHOLDERS:
            auth = (args.auth_file or Path(data.get("auth_file") or config_path.parent / "auth.json")).expanduser()
            ensure_fresh_auth(auth_file=auth, refresh_window_seconds=int(args.timeout_seconds + 120))
            shutil.copyfile(auth, profile / "config/auth.json")
            (profile / "config/auth.json").chmod(0o600)
        env = os.environ.copy()
        env.update(BOX_AGENT_HOME=str(profile), BOX_AGENT_MCP_CONFIG_PATH=str(profile / "config/mcp.json"),
                   BOX_AGENT_EVAL_AUTH_FILE=str(profile / "config/auth.json"), PYTHONUNBUFFERED="1")
        # Validate without printing model configuration or credentials into evidence.
        validation = subprocess.run([str(root / ".venv/bin/python"), "-c",
                                     "from box_agent.config import Config; Config.load()"],
                                    cwd=root, env=env, capture_output=True, check=False)
        if validation.returncode:
            raise ValueError("The isolated profile configuration is invalid; no model request was sent")
        # The model is fixed in the isolated profile, so no alternate transport/binding is needed.
        command = [str(root / ".venv/bin/python"), str(root / "test_workspace/run_acp_eval.py"),
                   "--repo-root", str(root), "--dataset", str(fixtures / "dataset.jsonl"),
                   "--case-id", CASE_ID, "--title", title, "--seed", "13",
                   "--timeout-seconds", str(args.timeout_seconds), "--parallelism", "1", "--rate-limit-retries", "0"]
        code, failure = None, None
        try:
            code = launch(command, env, profile, args.timeout_seconds, root)
        except (OSError, KeyboardInterrupt) as error:
            failure = type(error).__name__
        finally:
            directories = output_directories(outputs, title)
            if len(directories) > 1:
                raise ValueError("Ambiguous evaluation directory")
            directory = directories[0] if directories else outputs / run_acp_eval.run_name(title=title)
            directory.mkdir(parents=True, exist_ok=True)
            for name in ("selection.json", "manifest.json", "summary.json"):
                if not (directory / name).exists():
                    write_json(directory / name, {"status": "driver_failed", "error_type": failure,
                                                  "case_ids": [CASE_ID], "launcher_exit_code": code})
            shutil.copytree(fixtures, directory / "inputs")
            for name in ("mcp-gate.json", "mcp-server-events.jsonl"):
                if (profile / name).exists():
                    shutil.copyfile(profile / name, directory / name)
            # The standard runner normally copies the trace. Preserve partial traces on interruption too.
            if not list(directory.glob("cases/*/attempts/*/agent/*.jsonl")) and (profile / "log/acp-eval").exists():
                shutil.copytree(profile / "log/acp-eval", directory / "partial-agent")
            write_json(directory / "cache-context.json", {
                "version": VERSION, "conditions": conditions, "source": source,
                "source_unchanged": source_identity(root) == source,
                "skill_receipt": skill_receipt, "mcp_receipt": mcp_receipt,
                "launcher_exit_code": code, "driver_error_type": failure,
                "command": command, "baseline": str(args.baseline.resolve()) if args.baseline else None,
            })
            write_json(directory / "evidence-index.json", evidence_hashes(directory))
            print(f"Preserved ACP evidence: {directory}", flush=True)
        # TemporaryDirectory removes the private config/auth, including after a failed attempt.
    report = analyze_run(directory)
    save_report(directory, report)
    if baseline:
        write_json(directory / "comparison.json", compare_reports(baseline, report))
    print(f"Report: {directory / 'cache-report.md'}", flush=True)
    return 0 if report["cache_measurement"] == "measured" else 2
