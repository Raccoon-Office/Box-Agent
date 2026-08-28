"""Behavior tests for the durable append-only Session Log."""

import json
import os
import subprocess
import sys

import pytest

from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.session_log import (
    SessionLog,
    SessionLogCorrupted,
    SessionLogInUseError,
    SessionLogWorkspaceMismatch,
)


def test_session_log_rejects_second_writer_until_owner_closes(tmp_path):
    owner = SessionLog.create(tmp_path, session_id="single-writer", cwd=tmp_path)

    with pytest.raises(SessionLogInUseError, match="already has an active writer"):
        SessionLog.open(tmp_path, session_id="single-writer", cwd=tmp_path)

    owner.close()
    successor = SessionLog.open(tmp_path, session_id="single-writer", cwd=tmp_path)
    successor.close()


def test_session_log_writer_ownership_is_released_when_process_exits(tmp_path):
    created = SessionLog.create(
        tmp_path,
        session_id="process-owner",
        cwd=tmp_path,
    )
    created.close()
    script = """
import os
import sys
from box_agent.session_log import SessionLog

log = SessionLog.open(sys.argv[1], session_id="process-owner", cwd=sys.argv[1])
print("ready", flush=True)
sys.stdin.readline()
os._exit(0)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", script, os.fspath(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline() == "ready\n"
        with pytest.raises(SessionLogInUseError):
            SessionLog.open(tmp_path, session_id="process-owner", cwd=tmp_path)

        assert owner.stdin is not None
        owner.stdin.write("exit\n")
        owner.stdin.flush()
        assert owner.wait(timeout=30) == 0

        successor = SessionLog.open(tmp_path, session_id="process-owner", cwd=tmp_path)
        successor.close()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=30)


def test_session_log_does_not_repair_tail_without_writer_ownership(tmp_path):
    owner = SessionLog.create(tmp_path, session_id="locked-tail", cwd=tmp_path)
    owner.flush()
    with owner.path.open("ab") as external_writer:
        external_writer.write(b'{"type":"turn/start"')
    torn_bytes = owner.path.read_bytes()

    with pytest.raises(SessionLogInUseError):
        SessionLog.open(tmp_path, session_id="locked-tail", cwd=tmp_path)

    assert owner.path.read_bytes() == torn_bytes
    owner.close()
    successor = SessionLog.open(tmp_path, session_id="locked-tail", cwd=tmp_path)
    successor.close()
    assert owner.path.read_bytes().endswith(b"\n")


def test_session_log_replays_a_durable_user_message(tmp_path):
    log = SessionLog.create(
        tmp_path,
        session_id="product-session-1",
        cwd=tmp_path,
    )
    log.append(
        "user/message",
        Message(role="user", content="keep this context").model_dump(
            mode="json",
            exclude_none=True,
        ),
        surface_op="append",
    )
    log.flush()
    path = log.path
    log.close()

    restored = SessionLog.open(
        tmp_path,
        session_id="product-session-1",
        cwd=tmp_path,
    )

    assert path.name == "session.jsonl"
    assert restored.header["id"] == "product-session-1"
    assert restored.replay().messages == [
        Message(role="user", content="keep this context")
    ]
    restored.close()


def test_session_log_rejects_workspace_change_without_mutating_log(tmp_path):
    root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    log = SessionLog.create(root, session_id="fixed-cwd", cwd=workspace)
    log.append(
        "user/message",
        Message(role="user", content="keep me").model_dump(mode="json"),
        surface_op="append",
    )
    log.flush()
    path = log.path
    log.close()
    before = path.read_bytes()

    with pytest.raises(SessionLogWorkspaceMismatch, match="immutable workspace"):
        SessionLog.open(root, session_id="fixed-cwd", cwd=other_workspace)

    assert path.read_bytes() == before
    restored = SessionLog.open(root, session_id="fixed-cwd", cwd=workspace)
    assert restored.replay().messages == [Message(role="user", content="keep me")]
    restored.close()


def test_session_log_rejects_symlink_alias_for_same_workspace(tmp_path):
    if os.name == "nt":
        pytest.skip("symlink creation is not reliably available on Windows")
    root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    alias = tmp_path / "workspace-alias"
    workspace.mkdir()
    alias.symlink_to(workspace, target_is_directory=True)
    log = SessionLog.create(root, session_id="fixed-cwd-alias", cwd=workspace)
    log.close()

    with pytest.raises(SessionLogWorkspaceMismatch, match="immutable workspace"):
        SessionLog.open(root, session_id="fixed-cwd-alias", cwd=alias)


def test_session_log_accepts_only_equivalent_cwd_syntax(tmp_path):
    root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    equivalent = os.path.join(os.fspath(workspace), "unused", os.pardir)
    log = SessionLog.create(root, session_id="normalized-cwd", cwd=equivalent)
    log.close()

    restored = SessionLog.open(root, session_id="normalized-cwd", cwd=workspace)
    assert restored.header["cwd"] == os.path.abspath(os.fspath(workspace))
    restored.close()


def test_replay_filters_only_known_legacy_workflow_context(tmp_path):
    log = SessionLog.create(tmp_path, session_id="legacy-workflow", cwd=tmp_path)
    messages = [
        "Please explain [Post-Compaction Workflow Checkpoint] to me.",
        "[Post-Compaction Workflow Checkpoint]\n\nlegacy state",
        (
            "The host runtime supplied the following internal state update while the "
            "current task was running.\n\n"
            "Runtime state update:\nCONTROLLED_PRESENTATION_STAGE=scaffold"
        ),
        (
            "The user sent the following message while the current task was already "
            "running.\n\n"
            "Mid-turn user message:\n[BOX_AGENT_EXTERNAL_SKILL_CHECKPOINT]"
        ),
        (
            "The host runtime supplied the following internal state update while the "
            "current task was running.\n\nRuntime state update:\nordinary generic state"
        ),
    ]
    for content in messages:
        log.append(
            "user/message",
            Message(role="user", content=content).model_dump(mode="json"),
            surface_op="append",
        )
    log.flush()
    log.close()

    restored = SessionLog.open(
        tmp_path,
        session_id="legacy-workflow",
        cwd=tmp_path,
    )

    assert [message.content for message in restored.replay().messages] == [
        messages[0],
        messages[4],
    ]
    restored.close()


def test_session_log_truncates_only_an_incomplete_final_record(tmp_path):
    log = SessionLog.create(tmp_path, session_id="crash-tail", cwd=tmp_path)
    log.append(
        "user/message",
        Message(role="user", content="committed").model_dump(
            mode="json",
            exclude_none=True,
        ),
        surface_op="append",
    )
    log.flush()
    path = log.path
    log.close()
    with path.open("ab") as handle:
        handle.write(b'{"type":"assistant/message"')

    restored = SessionLog.open(tmp_path, session_id="crash-tail", cwd=tmp_path)

    assert restored.replay().messages == [Message(role="user", content="committed")]
    restored.close()
    assert path.read_bytes().endswith(b"\n")


def test_session_log_rejects_corruption_before_the_final_record(tmp_path):
    log = SessionLog.create(tmp_path, session_id="middle-corrupt", cwd=tmp_path)
    log.append(
        "user/message",
        Message(role="user", content="first").model_dump(mode="json"),
        surface_op="append",
    )
    log.append(
        "user/message",
        Message(role="user", content="second").model_dump(mode="json"),
        surface_op="append",
    )
    log.flush()
    path = log.path
    log.close()
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + b"not-json\n" + lines[2])

    for _ in range(2):
        with pytest.raises(SessionLogCorrupted, match="record 1"):
            SessionLog.open(tmp_path, session_id="middle-corrupt", cwd=tmp_path)


def test_session_log_rejects_non_contiguous_seq_and_second_header(tmp_path):
    seq_log = SessionLog.create(tmp_path, session_id="bad-seq", cwd=tmp_path)
    seq_log.append("turn/start", {"turn": 1})
    seq_log.flush()
    seq_path = seq_log.path
    seq_log.close()
    records = seq_path.read_text(encoding="utf-8").splitlines()
    event = json.loads(records[1])
    event["seq"] = 4
    records[1] = json.dumps(event)
    seq_path.write_text("\n".join(records) + "\n", encoding="utf-8")

    with pytest.raises(SessionLogCorrupted, match="non-contiguous seq"):
        SessionLog.open(tmp_path, session_id="bad-seq", cwd=tmp_path)

    header_log = SessionLog.create(tmp_path, session_id="second-header", cwd=tmp_path)
    header_log.append("session", {"id": "second-header"})
    header_log.flush()
    header_log.close()

    with pytest.raises(SessionLogCorrupted, match="unknown required event"):
        SessionLog.open(tmp_path, session_id="second-header", cwd=tmp_path)


def test_surface_replacement_restores_compacted_context_without_deleting_history(
    tmp_path,
):
    log = SessionLog.create(tmp_path, session_id="compacted", cwd=tmp_path)
    first = log.append(
        "user/message",
        Message(role="user", content="large request").model_dump(mode="json"),
        surface_op="append",
    )
    second = log.append(
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "message": Message(role="assistant", content="large response").model_dump(
                mode="json"
            ),
        },
        surface_op="append",
    )
    log.append(
        "user/message",
        Message(role="user", content="summary checkpoint").model_dump(mode="json"),
        surface_op={"op": "replace", "start": first["seq"], "end": second["seq"]},
        source_event_seqs=[first["seq"], second["seq"]],
    )
    log.flush()
    log.close()

    restored = SessionLog.open(tmp_path, session_id="compacted", cwd=tmp_path)

    assert restored.replay().messages == [
        Message(role="user", content="summary checkpoint")
    ]
    assert [event["type"] for event in restored.events] == [
        "user/message",
        "assistant/message",
        "user/message",
    ]
    restored.close()


def test_compaction_replacement_commits_exact_new_surface_and_keeps_old_events(
    tmp_path,
):
    log = SessionLog.create(tmp_path, session_id="exact-compaction", cwd=tmp_path)
    old_messages = [
        Message(role="user", content="large request"),
        Message(role="assistant", content="large response"),
    ]
    log.append_unlogged_messages(old_messages, turn=1, step=1)
    old_event_count = len(log.events)

    new_messages = [
        Message(role="user", content="summary"),
        Message(role="user", content="recent request"),
        Message(role="assistant", content="recent response"),
    ]
    replacement = log.replace_surface(
        new_messages,
        turn=1,
        step=2,
    )
    log.flush()

    assert log.replay().messages == new_messages
    assert len(log.events) == old_event_count + len(new_messages)
    assert replacement[0]["surfaceOp"] == {
        "op": "replace",
        "start": 0,
        "end": 1,
    }
    assert replacement[0]["sourceEventSeqs"] == [0, 1]
    log.close()


def test_live_surface_rewrite_is_reconciled_without_failing_the_task(
    tmp_path,
    caplog,
):
    log = SessionLog.create(tmp_path, session_id="result-budget", cwd=tmp_path)
    original_messages = [
        Message(role="user", content="research"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="search-1",
                    type="function",
                    function=FunctionCall(name="web_search", arguments={"q": "x"}),
                )
            ],
        ),
        Message(
            role="tool",
            name="web_search",
            tool_call_id="search-1",
            content="x" * 1_000,
        ),
    ]
    log.append_unlogged_messages(original_messages, turn=1, step=1)
    original_event_count = len(log.events)
    rewritten_messages = [
        *original_messages[:2],
        original_messages[2].model_copy(
            update={"content": "<persisted-output>tool-results/search-1.txt</persisted-output>"}
        ),
    ]

    with caplog.at_level("WARNING", logger="box_agent.session_log"):
        replacement = log.append_unlogged_messages(
            rewritten_messages,
            turn=1,
            step=2,
        )
    log.flush()

    assert log.replay().messages == rewritten_messages
    assert len(log.events) == original_event_count + len(rewritten_messages)
    assert replacement[0]["surfaceOp"] == {
        "op": "replace",
        "start": 0,
        "end": 2,
    }
    assert "session_log/surface_diverged" in caplog.text
    assert "action=replace_surface" in caplog.text
    log.close()


def test_compaction_recovery_uses_replacement_as_commit_point(tmp_path):
    before = SessionLog.create(tmp_path, session_id="before-replace", cwd=tmp_path)
    before.append_unlogged_messages(
        [Message(role="user", content="old surface")],
        turn=1,
        step=1,
    )
    before.append("compaction/start", {"turn": 1, "step": 2})
    before.append(
        "compaction/summary",
        {"turn": 1, "step": 2, "message": {"role": "user", "content": "new"}},
    )
    before.flush()
    before.close()

    restored_before = SessionLog.open(
        tmp_path,
        session_id="before-replace",
        cwd=tmp_path,
    )
    assert restored_before.replay().messages == [
        Message(role="user", content="old surface")
    ]
    restored_before.close()

    after = SessionLog.create(tmp_path, session_id="after-replace", cwd=tmp_path)
    after.append_unlogged_messages(
        [Message(role="user", content="old surface")],
        turn=1,
        step=1,
    )
    after.append("compaction/start", {"turn": 1, "step": 2})
    after.replace_surface(
        [Message(role="user", content="new surface")],
        turn=1,
        step=2,
    )
    after.flush()
    after.close()

    restored_after = SessionLog.open(
        tmp_path,
        session_id="after-replace",
        cwd=tmp_path,
    )
    assert restored_after.replay().messages == [
        Message(role="user", content="new surface")
    ]
    restored_after.close()


def test_repair_closes_a_dispatched_tool_with_unknown_outcome(tmp_path):
    log = SessionLog.create(tmp_path, session_id="tool-crash", cwd=tmp_path)
    log.append("turn/start", {"turn": 1})
    log.append("step/start", {"turn": 1, "step": 1})
    call = ToolCall(
        id="call-unknown",
        type="function",
        function=FunctionCall(name="write_file", arguments={"path": "x"}),
    )
    log.append(
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "message": Message(
                role="assistant",
                content="",
                tool_calls=[call],
            ).model_dump(mode="json"),
        },
        surface_op="append",
    )
    call_event = log.append(
        "tool/call",
        {
            "turn": 1,
            "step": 1,
            "callId": call.id,
            "name": call.function.name,
            "arguments": call.function.arguments,
        },
    )
    log.flush()
    log.close()

    restored = SessionLog.open(tmp_path, session_id="tool-crash", cwd=tmp_path)
    closers = restored.repair_interrupted_turn()
    restored.flush()

    assert [event["type"] for event in closers] == [
        "tool/result",
        "step/end",
        "turn/end",
    ]
    assert closers[0]["data"]["error"]["code"] == "TOOL_OUTCOME_UNKNOWN"
    assert closers[0]["sourceEventSeqs"] == [call_event["seq"]]
    assert closers[-1]["data"]["reason"] == {"kind": "interrupted"}
    assert restored.replay().messages[-1].tool_call_id == "call-unknown"
    restored.close()


def test_repair_marks_an_undispatched_assistant_call_not_started(tmp_path):
    log = SessionLog.create(tmp_path, session_id="tool-not-started", cwd=tmp_path)
    log.append("turn/start", {"turn": 1})
    log.append("step/start", {"turn": 1, "step": 1})
    call = ToolCall(
        id="call-not-started",
        type="function",
        function=FunctionCall(name="write_file", arguments={"path": "x"}),
    )
    log.append(
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "message": Message(
                role="assistant",
                content="",
                tool_calls=[call],
            ).model_dump(mode="json"),
        },
        surface_op="append",
    )
    log.flush()
    log.close()

    restored = SessionLog.open(
        tmp_path,
        session_id="tool-not-started",
        cwd=tmp_path,
    )
    closers = restored.repair_interrupted_turn()

    assert closers[0]["data"]["error"]["code"] == "TOOL_NOT_STARTED"
    assert "sourceEventSeqs" not in closers[0]
    restored.close()


def test_prepare_resume_closes_interrupted_prefix_before_end_seed(tmp_path):
    log = SessionLog.create(tmp_path, session_id="resume-boundary", cwd=tmp_path)
    log.append("turn/start", {"turn": 1})
    log.append("step/start", {"turn": 1, "step": 1})
    log.flush()
    log.close()

    restored = SessionLog.open(
        tmp_path,
        session_id="resume-boundary",
        cwd=tmp_path,
    )
    appended = restored.prepare_resume()

    assert [event["type"] for event in appended] == [
        "step/end",
        "turn/end",
        "session/end-seed",
    ]
    assert appended[-2]["data"]["reason"] == {"kind": "interrupted"}
    restored.close()


def test_unknown_required_event_rejects_restore_but_ignorable_event_is_skipped(
    tmp_path,
):
    required = SessionLog.create(tmp_path, session_id="future-required", cwd=tmp_path)
    required.append("future/state-change", {"value": 1})
    required.flush()
    required.close()

    with pytest.raises(SessionLogCorrupted, match="unknown required event"):
        SessionLog.open(tmp_path, session_id="future-required", cwd=tmp_path)

    ignorable = SessionLog.create(tmp_path, session_id="future-info", cwd=tmp_path)
    ignorable.append("future/diagnostic", {"value": 1}, ignorable=True)
    ignorable.flush()
    ignorable.close()

    restored = SessionLog.open(tmp_path, session_id="future-info", cwd=tmp_path)
    assert restored.replay().messages == []
    restored.close()


def test_replay_restores_latest_box_agent_domain_state(tmp_path):
    log = SessionLog.create(tmp_path, session_id="domain-state", cwd=tmp_path)
    log.append("goal/change", {"goal": {"objective": "ship", "status": "active"}})
    log.append("plan/write", {"plan": {"title": "Implementation", "steps": []}})
    log.append("todo/write", {"todos": [{"content": "test", "status": "pending"}]})
    log.append(
        "skill/change",
        {
            "skills": [
                {"name": "pdfs", "sha256": "abc", "loadOrder": 1},
            ]
        },
    )
    log.append("goal/change", {"goal": None})
    log.flush()
    log.close()

    restored = SessionLog.open(tmp_path, session_id="domain-state", cwd=tmp_path)
    projection = restored.replay()

    assert projection.goal is None
    assert projection.plan == {"title": "Implementation", "steps": []}
    assert projection.todos == [{"content": "test", "status": "pending"}]
    assert projection.skills == [
        {"name": "pdfs", "sha256": "abc", "loadOrder": 1}
    ]
    restored.close()
