"""First-request size snapshot: the fixed-input metric for context trimming.

Any change to the system prompt, a tool schema or the first-turn messages
shows up here as an exact byte delta per scenario and per tool. Regenerate
after an intentional change (together with the prompt snapshots)::

    BOX_AGENT_UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest tests/test_request_footprint.py tests/test_session_prompt_snapshots.py

Compare two source trees with ``python -m test_workspace.request_footprint
--compare BEFORE.json AFTER.json``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from test_workspace import request_footprint as fp

SNAPSHOT = Path(__file__).parent / "fixtures" / "request_footprint.json"
UPDATE = os.environ.get("BOX_AGENT_UPDATE_PROMPT_SNAPSHOTS") == "1"


@pytest.fixture(scope="module")
def report():
    import asyncio

    return asyncio.run(fp.measure())


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash tool describes PowerShell on Windows")
def test_first_request_footprint_matches_snapshot(report):
    if UPDATE:
        SNAPSHOT.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    assert SNAPSHOT.exists(), "missing snapshot; regenerate with BOX_AGENT_UPDATE_PROMPT_SNAPSHOTS=1"
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if report != expected:
        lines = []
        for name, row in fp.compare(expected, report)["scenarios"].items():
            if "status" in row:
                lines.append(f"{name}: {row['status']}")
                continue
            deltas = {key: value["delta"] for key, value in row.items()
                      if isinstance(value, dict) and "delta" in value and value["delta"]}
            if deltas or row["tools"]:
                lines.append(f"{name}: {deltas or 'same size'}; changed tools {row['tools']}")
        if not lines:
            lines.append("system prompt content changed at the same size")
        pytest.fail("first-request footprint changed; review and regenerate with "
                    "BOX_AGENT_UPDATE_PROMPT_SNAPSHOTS=1:\n" + "\n".join(lines))


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash tool describes PowerShell on Windows")
def test_footprint_is_normalized_and_scenarios_cover_modes(report):
    text = json.dumps(report, ensure_ascii=False)
    assert "<current_date>" not in text  # dates live only inside normalized payloads
    assert set(report["scenarios"]) == {scenario.name for scenario in fp.SCENARIOS}
    general = report["scenarios"]["acp_general_chat"]
    code = report["scenarios"]["acp_code_agent_chat"]
    assert {"grep", "glob"} <= set(code["tools"]) and not {"grep", "glob"} & set(general["tools"])
    for row in report["scenarios"].values():
        assert row["tools_bytes"] >= sum(tool["bytes"] for tool in row["tools"].values())
        assert row["request_bytes"] > row["system_bytes"] + row["tools_bytes"]


def test_normalize_replaces_workspace_root_and_current_date(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = {"messages": [
        {"role": "system", "content": f"cwd `{workspace}`; rg at {tmp_path}/bin/rg"},
        {"role": "user", "content": "<current_date>2031-02-03</current_date>"},
    ]}
    normalized = json.dumps(fp.normalize(payload, workspace), ensure_ascii=False)
    assert str(tmp_path) not in normalized
    assert "<WORKSPACE>" in normalized and "<ROOT>/bin/rg" in normalized
    assert "2031-02-03" not in normalized


def test_compare_reports_byte_deltas_and_changed_tools_only():
    def row(system, tools):
        return {"system_bytes": system, "tools_bytes": sum(b for _, b, _ in tools), "tool_count": len(tools),
                "messages_bytes": 10, "request_bytes": 10 + system + sum(b for _, b, _ in tools),
                "system_sha256": "s", "tools": {name: {"bytes": b, "sha256": digest} for name, b, digest in tools}}

    before = {"unit": "u", "scenarios": {"chat": row(100, [("bash", 50, "a"), ("read_file", 20, "r")]),
                                         "gone": row(1, [])}}
    after = {"unit": "u", "scenarios": {"chat": row(90, [("bash", 40, "b"), ("read_file", 20, "r"),
                                                         ("call_tool", 5, "c")])}}
    result = fp.compare(before, after)["scenarios"]
    assert result["chat"]["system_bytes"] == {"before": 100, "after": 90, "delta": -10}
    assert result["chat"]["tools_bytes"]["delta"] == -5
    assert result["chat"]["tools"] == {"bash": -10, "call_tool": 5}
    assert result["gone"] == {"status": "missing_in_after"}


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash tool describes PowerShell on Windows")
def test_footprint_does_not_depend_on_installed_managed_runtimes(monkeypatch):
    # A machine with managed Python/Node ready renders a different Skill
    # Runtime Context; the sampler must pin it so the snapshot is portable.
    import asyncio

    from box_agent.tools import runtime

    def ready(kind):
        return lambda *_args, **_kwargs: runtime.SkillRuntime(
            kind, "available", "box_agent", executable_path=f"/opt/managed/{kind}",
            env_vars={"BOX_AGENT_NODE": "/opt/managed/node"} if kind == "node" else {})

    monkeypatch.setattr(runtime, "_build_python_runtime", ready("python"))
    monkeypatch.setattr(runtime, "_build_node_runtime", ready("node"))
    scenario = fp.SCENARIOS[0]
    report = asyncio.run(fp.measure((scenario,)))
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["scenarios"][scenario.name]
    assert report["scenarios"][scenario.name] == expected
