"""Read-only delegation evidence must belong to this exact pending invocation."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest


SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "native_delegation_status.js"
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")


def tool(name):
    return {
        "name": name,
        "description": "Public tool metadata",
        "input_schema": {"type": "object", "properties": {}},
    }


@pytest.fixture
def case(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "runtime"
    (home / "sessions").mkdir(parents=True)
    marker = str(uuid.uuid4())
    now = int(time.time() * 1000)
    session_id = "test-" + str(uuid.uuid4())
    records = [
        {"type": "session", "version": 1, "id": session_id,
         "cwd": str(workspace), "createdAt": now},
        {"type": "turn/start", "seq": 1, "time": now, "data": {"turn": 1}},
        {"type": "step/start", "seq": 2, "time": now, "data": {"turn": 1, "step": 1}},
        {"type": "request/header", "seq": 3, "time": now, "data": {
            "turn": 1, "step": 1, "header": {
                "system": "PRIVATE_SYSTEM_MUST_NOT_APPEAR",
                "tools": [tool("bash"), tool("sub_agent")],
            },
        }},
        {"type": "assistant/message", "seq": 4, "time": now, "data": {
            "message": {"content": "PRIVATE_MESSAGE_MUST_NOT_APPEAR"},
        }},
        {"type": "tool/call", "seq": 5, "time": now, "data": {
            "turn": 1, "step": 1, "callId": "call-current", "name": "bash",
            "arguments": {"command": f'"$BOX_AGENT_NODE" "{SCRIPT}" --invocation {marker}'},
        }},
    ]
    return {"workspace": workspace, "home": home, "marker": marker, "records": records}


def save(case, records=None, suffix=""):
    records = copy.deepcopy(records if records is not None else case["records"])
    session_id = records[0]["id"]
    directory = case["home"] / "sessions" / hashlib.sha256(session_id.encode()).hexdigest()
    directory.mkdir(exist_ok=True)
    file = directory / "session.jsonl"
    file.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records) + suffix)
    return file


def run(case, marker=None):
    if not NODE:
        pytest.skip("Node.js is required")
    env = {**os.environ, "BOX_AGENT_HOME": str(case["home"]),
           "BOX_AGENT_WORKSPACE_DIR": str(case["workspace"])}
    result = subprocess.run(
        [str(NODE), str(SCRIPT), "--invocation", marker or case["marker"]],
        cwd=case["workspace"], env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    assert "PRIVATE_SYSTEM" not in result.stdout
    assert "PRIVATE_MESSAGE" not in result.stdout
    return json.loads(result.stdout)


def test_present_receipt_is_bound_to_current_call_and_does_not_write(case):
    file = save(case)
    before = file.read_bytes()
    result = run(case)
    assert result["status"] == "present"
    assert result["tool_schema"]["name"] == "sub_agent"
    assert result["session_id"] == case["records"][0]["id"]
    assert (result["turn"], result["step"], result["request_seq"], result["call_id"]) == (1, 1, 3, "call-current")
    assert result["invocation"] == case["marker"]
    assert file.read_bytes() == before
    assert list(case["workspace"].iterdir()) == []


def test_complete_current_tool_inventory_can_prove_absence(case):
    case["records"][3]["data"]["header"]["tools"] = [tool("bash"), tool("read_file")]
    save(case)
    result = run(case)
    assert result["status"] == "absent"
    assert "tool_schema" not in result


def test_child_uses_its_own_header_even_when_parent_exposes_delegation(case):
    parent = copy.deepcopy(case["records"][:4])
    save(case, parent)
    child = copy.deepcopy(case["records"])
    child[0].update(id="subagent-" + str(uuid.uuid4()), origin="subagent", parentSession=parent[0]["id"])
    child[3]["data"]["header"]["tools"] = [tool("bash")]
    save(case, child)
    result = run(case)
    assert result["status"] == "absent"
    assert result["session_id"] == child[0]["id"]
    assert result["parent_session_id"] == parent[0]["id"]


def test_parent_header_cannot_fill_missing_child_inventory(case):
    save(case, case["records"][:4])
    child = copy.deepcopy(case["records"])
    child[0].update(id="subagent-" + str(uuid.uuid4()), origin="subagent", parentSession=case["records"][0]["id"])
    del child[3]["data"]["header"]["tools"]
    save(case, child)
    assert run(case)["status"] == "unknown"


@pytest.mark.parametrize("defect", [
    "completed", "turn_ended", "step_advanced", "turn_advanced", "request_advanced", "stale", "future",
    "header_missing", "wrong_step", "wrong_turn", "duplicate_header", "missing_tools",
    "unknown_schema", "duplicate_tools", "malformed_native", "no_bash", "wrong_version",
    "invalid_child", "foreign_cwd", "nested_cwd", "different_marker", "echo_only",
    "shell_generated_marker", "following_command",
])
def test_unproven_invocations_never_claim_tool_absence(case, defect):
    rows = case["records"]
    header = rows[3]["data"]
    call = rows[-1]
    if defect == "completed":
        rows.append({"type": "tool/result", "seq": 6, "data": {
            "turn": 1, "step": 1, "message": {"role": "tool", "tool_call_id": "call-current"},
            "result": {"success": True},
        }})
    elif defect in ("turn_ended", "step_advanced", "turn_advanced", "request_advanced"):
        event, data = {
            "turn_ended": ("turn/end", {"turn": 1}),
            "step_advanced": ("step/start", {"turn": 1, "step": 2}),
            "turn_advanced": ("turn/start", {"turn": 2}),
            "request_advanced": ("request/header", {"turn": 1, "step": 2, "header": {"tools": []}}),
        }[defect]
        rows.append({"type": event, "seq": 6, "data": data})
    elif defect in ("stale", "future"):
        call["time"] += -600_000 if defect == "stale" else 600_000
    elif defect == "header_missing":
        rows.pop(3)
    elif defect in ("wrong_step", "wrong_turn"):
        header["step" if defect == "wrong_step" else "turn"] = 2
    elif defect == "duplicate_header":
        extra = copy.deepcopy(rows[3])
        extra["seq"] = 4
        rows[4] = extra
    elif defect == "missing_tools":
        del header["header"]["tools"]
    elif defect == "unknown_schema":
        header["header"]["tools"] = [tool("bash"), {"name": "other"}]
    elif defect == "duplicate_tools":
        header["header"]["tools"].append(tool("sub_agent"))
    elif defect == "malformed_native":
        header["header"]["tools"][1]["input_schema"] = "not a schema"
    elif defect == "no_bash":
        header["header"]["tools"] = []
    elif defect == "wrong_version":
        rows[0]["version"] = 999
    elif defect == "invalid_child":
        rows[0]["origin"] = "subagent"
    elif defect in ("foreign_cwd", "nested_cwd"):
        other = case["workspace"].parent / "foreign" if defect == "foreign_cwd" else case["workspace"] / "nested"
        other.mkdir()
        rows[0]["cwd"] = str(other)
    elif defect == "different_marker":
        call["data"]["arguments"]["command"] = call["data"]["arguments"]["command"].replace(case["marker"], str(uuid.uuid4()))
    elif defect == "echo_only":
        call["data"]["arguments"]["command"] = f'echo "{SCRIPT}" --invocation {case["marker"]}'
    elif defect == "shell_generated_marker":
        call["data"]["arguments"]["command"] = call["data"]["arguments"]["command"].replace(case["marker"], '"$(uuidgen)"')
    elif defect == "following_command":
        call["data"]["arguments"]["command"] += " && echo done"
    save(case)
    assert run(case)["status"] == "unknown"


@pytest.mark.parametrize("damage", ["truncated_tail", "invalid_json", "no_newline", "oversized", "wrong_directory"])
def test_incomplete_log_cannot_produce_absence(case, damage):
    file = save(case)
    if damage == "truncated_tail":
        file.write_bytes(file.read_bytes() + b'{"type":"tool/result"')
    elif damage == "invalid_json":
        file.write_bytes(file.read_bytes() + b'not json\n')
    elif damage == "no_newline":
        file.write_bytes(file.read_bytes().rstrip(b"\n"))
    elif damage == "oversized":
        with file.open("ab") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
    else:
        file.parent.rename(file.parent.with_name("f" * 64))
    assert run(case)["status"] == "unknown"


@pytest.mark.parametrize("old_completed", [False, True])
def test_duplicate_invocation_marker_is_unknown_even_if_one_copy_completed(case, old_completed):
    save(case)
    other = copy.deepcopy(case["records"])
    other[0]["id"] = "test-" + str(uuid.uuid4())
    if old_completed:
        other.append({"type": "turn/end", "seq": 6, "data": {"turn": 1}})
    save(case, other)
    assert run(case)["status"] == "unknown"


def test_newest_same_cwd_is_not_used_instead_of_bound_invocation(case):
    save(case)
    newer = copy.deepcopy(case["records"][:4])
    newer[0].update(id="test-" + str(uuid.uuid4()), createdAt=newer[0]["createdAt"] + 1000)
    newer[3]["data"]["header"]["tools"] = [tool("bash")]
    save(case, newer)
    result = run(case)
    assert result["status"] == "present"
    assert result["session_id"] == case["records"][0]["id"]


@pytest.mark.parametrize("history_kind", ["completed", "truncated", "large"])
def test_stale_same_cwd_history_does_not_poison_current_invocation(case, history_kind):
    save(case)
    history = copy.deepcopy(case["records"])
    history[0]["id"] = "test-" + str(uuid.uuid4())
    history[0]["createdAt"] -= 600_000
    for record in history[1:]:
        record["time"] -= 600_000
    history[-1]["data"]["arguments"]["command"] = history[-1]["data"]["arguments"]["command"].replace(
        case["marker"], str(uuid.uuid4()),
    )
    history.append({"type": "turn/end", "seq": 6, "data": {"turn": 1}})
    file = save(case, history)
    if history_kind == "truncated":
        file.write_bytes(file.read_bytes() + b'{"type":"tool/result"')
    elif history_kind == "large":
        with file.open("ab") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
    old_time = time.time() - 600
    os.utime(file, (old_time, old_time))
    result = run(case)
    assert result["status"] == "present"
    assert result["session_id"] == case["records"][0]["id"]


@pytest.mark.parametrize("damage", ["truncated", "large"])
def test_recent_incomplete_same_cwd_history_still_fails_closed(case, damage):
    save(case)
    related = copy.deepcopy(case["records"][:1])
    related[0]["id"] = "test-" + str(uuid.uuid4())
    file = save(case, related)
    if damage == "truncated":
        file.write_bytes(file.read_bytes() + b'{"type":"tool/call"')
    else:
        with file.open("ab") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
    assert run(case)["status"] == "unknown"


def test_unrelated_session_body_is_ignored(case):
    save(case)
    foreign = copy.deepcopy(case["records"][:1])
    other_cwd = case["workspace"].parent / "other"
    other_cwd.mkdir()
    foreign[0].update(id="test-" + str(uuid.uuid4()), cwd=str(other_cwd))
    save(case, foreign, suffix="PRIVATE_MESSAGE_MUST_NOT_APPEAR invalid body\n")
    assert run(case)["status"] == "present"


def test_missing_log_root_and_invalid_marker_are_unknown(case):
    shutil.rmtree(case["home"])
    assert run(case)["status"] == "unknown"
    assert run(case, "not-a-uuid")["status"] == "unknown"


def test_existing_directory_prefix_is_supported(case):
    case["records"][-1]["data"]["arguments"]["command"] = (
        f'cd \'{case["workspace"]}\' && ' + case["records"][-1]["data"]["arguments"]["command"]
    )
    save(case)
    assert run(case)["status"] == "present"


def test_canonical_workspace_alias_is_exactly_the_same_directory(case):
    alias = case["workspace"].parent / "alias"
    alias.symlink_to(case["workspace"], target_is_directory=True)
    case["records"][0]["cwd"] = str(alias)
    save(case)
    assert run(case)["status"] == "present"


def test_real_session_inventory_replay_is_safely_projected(case):
    source = os.environ.get("BOX_AGENT_TEST_NATIVE_REPLAY")
    expected_cwd = os.environ.get("BOX_AGENT_TEST_NATIVE_REPLAY_CWD")
    if not source or not expected_cwd:
        pytest.skip("Explicit current-task Session Log and expected cwd required for replay")
    file = Path(source)
    assert file.stat().st_size <= 64 * 1024 * 1024
    with file.open() as stream:
        session = json.loads(stream.readline())
        assert Path(session["cwd"]).resolve() == Path(expected_cwd).resolve()
        before = hashlib.sha256(file.read_bytes()).hexdigest()
        headers = {}
        selected = None
        for line in stream:
            row = json.loads(line)
            data = row.get("data", {})
            if row.get("type") == "request/header":
                headers[(data["turn"], data["step"])] = row
            elif row.get("type") == "tool/call" and data.get("name") == "bash":
                header = headers.get((data["turn"], data["step"]))
                if header and any(t.get("name") == "sub_agent" for t in header["data"]["header"]["tools"]):
                    selected = (header, row)
                    break
    assert selected is not None
    header, call = copy.deepcopy(selected)
    # Keep the real event shape and tool schemas, never system/messages/content.
    header["data"]["header"] = {"tools": [
        {"name": item["name"], "input_schema": item["input_schema"]}
        for item in header["data"]["header"]["tools"]
    ]}
    call["time"] = int(time.time() * 1000)
    call["data"]["arguments"] = copy.deepcopy(case["records"][-1]["data"]["arguments"])
    save(case, [case["records"][0], header, call])
    result = run(case)
    assert result["status"] == "present"
    assert result["tool_schema"]["input_schema"]["required"] == ["task"]
    assert result["request_seq"] == header["seq"]
    assert hashlib.sha256(file.read_bytes()).hexdigest() == before
