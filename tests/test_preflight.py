"""Full-suite entry points preserve coverage and stop on failed gates."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
TEST_SCRIPT = ROOT / "general_review/ci/test.sh"
PREFLIGHT = ROOT / "general_review/ci/preflight.sh"
TIMEOUT_TEST = "tests/test_mcp.py::test_connection_timeout_on_unreachable_server"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="Bash CI entry points run on Unix/WSL")


@pytest.fixture
def fake_uv(tmp_path, monkeypatch):
    if not shutil.which("bash"):
        pytest.skip("Bash is required for the CI entry points")
    calls = tmp_path / "calls.jsonl"
    executable = tmp_path / "uv"
    code = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['PREFLIGHT_TEST_CALLS']).open('a') as f:\n"
        "    f.write(json.dumps({'args': args, 'workers': os.environ.get('PYTEST_XDIST_AUTO_NUM_WORKERS')}) + '\\n')\n"
        "sys.exit(23 if os.environ.get('PREFLIGHT_FAIL_COMMAND') in args else 0)\n"
    )
    executable.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(code)} \"$@\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PREFLIGHT_TEST_CALLS", str(calls))
    monkeypatch.delenv("PREFLIGHT_FAIL_COMMAND", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    return calls


def _run(script):
    return subprocess.run(["bash", str(script)], cwd=ROOT, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("entry", ["local", "preflight", "github"])
def test_full_suite_entries_share_bounded_parallelism_and_original_coverage(fake_uv, entry):
    if entry == "github":
        workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
        step = next(step for step in workflow["jobs"]["test"]["steps"] if step.get("name") == "Run pytest")
        result = subprocess.run(["bash", "-c", step["run"]], cwd=ROOT, capture_output=True, text=True, timeout=10)
    else:
        result = _run(PREFLIGHT if entry == "preflight" else TEST_SCRIPT)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line)["args"] for line in fake_uv.read_text().splitlines()]
    pytest_call, = [args for args in calls if args[:2] == ["run", "pytest"]]
    assert pytest_call[:3] == ["run", "pytest", "tests/"]
    assert pytest_call[pytest_call.index("-n") + 1] == "auto"
    assert "--maxprocesses=4" in pytest_call
    assert "--dist=loadfile" in pytest_call
    assert "--durations=20" in pytest_call
    assert not {"-k", "-m", "--lf", "--last-failed"}.intersection(pytest_call)
    assert not any(arg.startswith("--ignore") for arg in pytest_call)
    assert pytest_call.count("--deselect") == 1
    assert pytest_call[pytest_call.index("--deselect") + 1] == TIMEOUT_TEST
    if entry == "preflight":
        assert calls[0] == ["sync", "--frozen", "--all-extras"]
        assert calls[1] == ["run", "python", "-m", "compileall", "-q", "box_agent"]
        assert calls[2] == pytest_call
        assert calls[3:] == [["build"]]
    else:
        assert calls == [pytest_call]


@pytest.mark.parametrize("failed_gate,count", [("sync", 1), ("compileall", 2), ("pytest", 3)])
def test_preflight_stops_before_later_gates_when_a_command_fails(fake_uv, monkeypatch, failed_gate, count):
    monkeypatch.setenv("PREFLIGHT_FAIL_COMMAND", failed_gate)
    result = _run(PREFLIGHT)
    assert result.returncode == 23
    calls = [json.loads(line)["args"] for line in fake_uv.read_text().splitlines()]
    assert len(calls) == count
    assert failed_gate in calls[-1]


def test_full_suite_preserves_standard_serial_worker_override(fake_uv, monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "0")
    result = _run(TEST_SCRIPT)
    assert result.returncode == 0, result.stderr
    call, = [json.loads(line) for line in fake_uv.read_text().splitlines()]
    assert call["workers"] == "0"
