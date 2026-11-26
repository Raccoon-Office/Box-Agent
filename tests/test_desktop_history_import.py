"""Import durable text history through the deployed ACP wire surface."""
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import pytest

import box_agent.session_import as session_import_module
from box_agent.session_import import import_staged_history
from box_agent.session_log import SessionLog
from tests.acp_host.probe import AcpHostProbe, RpcError


@pytest.fixture
def staged_history(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    cwd = tmp_path / "files"
    cwd.mkdir()
    token, turn_id = str(uuid4()), str(uuid4())
    stage = profile / "history-import" / token
    stage.mkdir(parents=True)
    snapshot = stage / "snapshot.json"
    body = dict(version=1, id=token, workspace=str(cwd), messages=[
        dict(role="user", text="历史问题", timestamp=1700000000000, turnId=turn_id,
             references=[{"path": "/historical/report.txt"}]),
        dict(role="assistant", text="历史回答", timestamp=1700000000001, turnId=turn_id),
    ])
    snapshot.write_text(json.dumps(body), encoding="utf-8")
    destination = profile / "sessions" / hashlib.sha256(token.encode()).hexdigest()
    return SimpleNamespace(token=token, snapshot=snapshot, body=body, cwd=cwd,
                           destination=destination, log=destination / "session.jsonl")


@pytest.mark.skipif(os.name == "nt", reason="Windows does not sync directory descriptors")
@pytest.mark.parametrize("competing_publish", [False, True])
def test_import_verifies_published_history_before_syncing_directories(
    staged_history, monkeypatch, competing_publish,
):
    history = staged_history
    calls = []
    fsync, rename, open_file = os.fsync, os.rename, Path.open
    racing = False

    def record_sync(fd):
        calls.append("directory_sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_sync")
        fsync(fd)

    def publish(source, target):
        nonlocal racing
        if competing_publish and not racing:
            racing = True
            import_staged_history(history.token)
        rename(source, target)
        calls.append("publish")

    def record_open(path, mode="r", *args, **kwargs):
        if path == history.log and mode == "rb":
            calls.append("verify")
        return open_file(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_sync)
    monkeypatch.setattr(os, "rename", publish)
    monkeypatch.setattr(Path, "open", record_open)
    result = import_staged_history(history.token)
    calls.append("success")

    assert result["sessionId"].startswith("acp-v1-")
    assert calls.index("file_sync") < calls.index("publish") < calls.index("verify")
    confirmations = ["verify", "directory_sync", "directory_sync", "directory_sync"]
    assert calls[calls.index("publish") + 1:] == confirmations * (2 if competing_publish else 1) + ["success"]
    assert not list(history.destination.parent.glob(".import-*"))


@pytest.mark.skipif(os.name == "nt", reason="Windows does not sync directory descriptors")
@pytest.mark.parametrize("failed_level", [0, 1, 2])
def test_import_retry_requires_directory_durability_without_rewriting_history(
    staged_history, monkeypatch, failed_level,
):
    history = staged_history
    directories = [history.destination, history.destination.parent, history.destination.parent.parent]
    fsync = os.fsync
    failing = True
    synced = []

    def sync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            directory = next(path for path in directories if os.path.samestat(info, path.stat()))
            synced.append(directory)
            if failing and directory == directories[failed_level]:
                raise OSError(errno.EIO, "injected directory sync failure")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError, match="injected directory sync failure"):
        import_staged_history(history.token)
    before = history.log.read_bytes()
    inode = history.log.stat().st_ino
    snapshot = history.snapshot.read_bytes()

    def reject_rewrite(*args, **kwargs):
        pytest.fail("a retry must not create or rewrite a SessionLog")

    monkeypatch.setattr(SessionLog, "create", reject_rewrite)
    with pytest.raises(OSError, match="injected directory sync failure"):
        import_staged_history(history.token)
    failing = False
    synced.clear()
    assert import_staged_history(history.token)["sessionId"].startswith("acp-v1-")
    assert synced == directories
    assert history.log.read_bytes() == before
    assert history.log.stat().st_ino == inode
    assert history.snapshot.read_bytes() == snapshot


@pytest.mark.parametrize("damage", [
    "id", "origin", "cwd", "version", "user", "assistant", "turn", "sequence",
    "timestamp", "surface", "missing_event", "missing_end", "partial_line", "invalid_json",
])
def test_import_rejects_mismatched_or_incomplete_prefix_without_modifying_it(
    staged_history, monkeypatch, damage,
):
    history = staged_history
    import_staged_history(history.token)
    records = [json.loads(line) for line in history.log.read_bytes().splitlines()]
    if damage in {"id", "origin", "cwd", "version"}:
        records[0][damage] = "different"
    elif damage == "user":
        records[3]["data"]["content"] = "different"
    elif damage == "assistant":
        records[4]["data"]["message"]["content"] = "different"
    elif damage == "turn":
        records[2]["data"]["turn_id"] = str(uuid4())
    elif damage == "sequence":
        records[3]["seq"] = 99
    elif damage == "timestamp":
        records[3]["time"] += 1
    elif damage == "surface":
        records[3]["surfaceOp"] = "replace"
    elif damage == "missing_event":
        del records[3]
    elif damage == "missing_end":
        records.pop()
    data = ("\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n").encode()
    if damage == "partial_line":
        data = data[:-1]
    elif damage == "invalid_json":
        data = data[:data.index(b"\n") + 1] + b"not json\n"
    history.log.write_bytes(data)

    def reject_sync(fd):
        pytest.fail("invalid history must be rejected before any durability acknowledgement")

    monkeypatch.setattr(os, "fsync", reject_sync)
    with pytest.raises(ValueError):
        import_staged_history(history.token)
    assert history.log.read_bytes() == data
    assert history.snapshot.exists()


def test_import_accepts_original_timestamps_and_keeps_an_active_appended_tail(staged_history):
    history = staged_history
    expected = import_staged_history(history.token)
    records = [json.loads(line) for line in history.log.read_bytes().splitlines()]
    records[0]["createdAt"] = 1
    for event in records[1:]:
        if event["type"] not in {"user/message", "assistant/message"}:
            event["time"] = 2
    history.log.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    owner = SessionLog.open(history.destination.parent, session_id=history.token, cwd=history.cwd)
    try:
        owner.append("turn/start", {"turn": 2})
        owner.append("user/message", {"role": "user", "content": "new conversation"}, surface_op="append")
        owner.flush()
        # An unrelated in-progress tail must not be scanned, truncated or recovered.
        with history.log.open("ab") as stream:
            stream.write(b'{"type":')
        before = history.log.read_bytes()
        assert import_staged_history(history.token) == expected
        assert history.log.read_bytes() == before
    finally:
        owner.close()


def test_windows_import_still_checks_content_without_syncing_directories(staged_history, monkeypatch):
    history = staged_history
    monkeypatch.setattr(session_import_module, "os", SimpleNamespace(
        name="nt", path=os.path, fstat=os.fstat, rename=os.rename,
    ))
    expected = import_staged_history(history.token)
    assert import_staged_history(history.token) == expected
    original = history.log.read_text(encoding="utf-8")
    history.log.write_text(original.replace("历史回答", "different"), encoding="utf-8")
    with pytest.raises(ValueError):
        import_staged_history(history.token)


@pytest.mark.skipif(os.name == "nt", reason="Windows does not sync directory descriptors")
@pytest.mark.asyncio
async def test_directory_sync_failure_and_retry_leave_other_acp_sessions_usable():
    repository = Path(__file__).resolve().parents[1]
    sandbox = repository / "workspace"
    sandbox.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="history-import-sync-", dir=sandbox) as tmp:
        root = Path(tmp)
        cwd = root / "files"
        cwd.mkdir()
        token = str(uuid4())
        stage = root / "profile" / "history-import" / token
        stage.mkdir(parents=True)
        snapshot = stage / "snapshot.json"
        snapshot.write_text(json.dumps(dict(version=1, id=token, workspace=str(cwd), messages=[
            dict(role="user", text="original history", timestamp=1, turnId=str(uuid4())),
        ])), encoding="utf-8")
        # Fault injection stays in the test child, outside production protocol/config.
        worker = """
import errno, os, stat
from pathlib import Path
root = Path(os.environ['BOX_AGENT_DESKTOP_TEST_ROOT'])
original = os.fsync
def fsync(fd):
    if stat.S_ISDIR(os.fstat(fd).st_mode) and (root / 'fail-sync').exists():
        raise OSError(errno.EIO, 'injected directory sync failure')
    original(fd)
os.fsync = fsync
from box_agent.session_import_worker import main
main()
"""
        child = (
            "import sys\nimport box_agent.session_import_jobs as jobs\n"
            f"jobs.worker_command = lambda: [sys.executable, '-c', {worker!r}]\n"
            "from tests.desktop_runtime_fixture import main\nmain()\n"
        )
        probe = AcpHostProbe(command=[sys.executable, "-c", child], cwd=repository,
                             env={"BOX_AGENT_DESKTOP_TEST_ROOT": tmp,
                                  "BOX_AGENT_DESKTOP_TEST_FILE_INPUTS": "1"}, timeout_s=10)
        await probe.start()
        try:
            await probe.initialize()
            other = await probe.session_new(cwd=str(cwd))
            pid = probe._proc.pid
            failure = root / "fail-sync"
            failure.touch()
            for _ in range(2):
                with pytest.raises(RpcError) as raised:
                    await probe.ext_request("history/import", {"token": token})
                assert raised.value.code == -32602
                assert raised.value.message == "HISTORY_IMPORT_FAILED"
                assert snapshot.exists()
                assert (await probe.session_prompt(other, "fixture:file_inputs other"))["stopReason"] == "end_turn"
            log = root / "profile/sessions" / hashlib.sha256(token.encode()).hexdigest() / "session.jsonl"
            before = log.read_bytes()
            failure.unlink()
            imported = await probe.ext_request("history/import", {"token": token})
            assert log.read_bytes() == before
            await probe.request("session/resume", {
                "sessionId": imported["sessionId"], "cwd": str(cwd), "mcpServers": [],
            })
            assert (await probe.session_prompt(imported["sessionId"], "fixture:file_inputs continue"))["stopReason"] == "end_turn"
            assert probe._proc.pid == pid
            assert not probe._protocol.parse_errors
        finally:
            await probe.stop()


@pytest.mark.asyncio
async def test_import_restart_resume_and_failed_import_isolation():
    repository = Path(__file__).resolve().parents[1]
    sandbox = repository / "workspace"
    sandbox.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="history-import-", dir=sandbox) as tmp:
        root = Path(tmp)
        cwd = root / "files"
        cwd.mkdir()
        token = str(uuid4())
        directory = root / "profile" / "history-import" / token
        directory.mkdir(parents=True)
        messages = []
        for i in range(20):
            turn_id = str(uuid4())
            for role in ("user", "assistant"):
                messages.append(dict(role=role, turnId=turn_id, timestamp=1700000000000 + i,
                                     text=f"imported-{i:02}-{role}-marker"))
        snapshot = directory / "snapshot.json"
        snapshot.write_text(json.dumps(dict(version=1, id=token, workspace=str(cwd), messages=messages)))

        def probe():
            return AcpHostProbe(command=[sys.executable, "-m", "tests.desktop_runtime_fixture"],
                                cwd=repository, env={"BOX_AGENT_DESKTOP_TEST_ROOT": tmp,
                                "BOX_AGENT_DESKTOP_TEST_FILE_INPUTS": "1"}, timeout_s=30)

        first = probe()
        await first.start()
        try:
            assert (await first.initialize())["_meta"]["history_import"]["protocol_version"] == 1
            imported = await first.ext_request("history/import", {"token": token})
            assert await first.ext_request("history/import", {"token": token}) == imported
        finally:
            await first.stop()
        second = probe()
        await second.start()
        try:
            await second.initialize()
            identifier = imported["sessionId"]
            await second.request("session/resume", {"sessionId": identifier, "cwd": str(cwd), "mcpServers": []})
            await second.session_prompt(identifier, "fixture:file_inputs continue", meta={"turn_id": "follow-up"})
            events = [json.loads(line) for line in (root / "lifecycle.jsonl").read_text().splitlines()]
            sent = next(e["messages"] for e in events if e["event"] == "file_model_input")
            assert [(m["role"], m["content"]) for m in sent if str(m["content"]).startswith("imported-")] == [
                (m["role"], m["text"]) for m in messages
            ]
            assert any("fixture:file_inputs continue" in str(m["content"]) for m in sent)
            # A repeat after a live continuation must not replace the newer turn.
            assert await second.ext_request("history/import", {"token": token}) == imported
            snapshot.write_text(snapshot.read_text().replace("imported-00", "changed-00"))
            with pytest.raises(RpcError):
                await second.ext_request("history/import", {"token": token})
            with pytest.raises(RpcError):
                await second.ext_request("history/import", {"token": "../somewhere"})
            other = await second.session_new(cwd=str(cwd))
            assert (await second.session_prompt(other, "fixture:file_inputs other"))["stopReason"] == "end_turn"
            await second.request("session/load", {"sessionId": identifier, "cwd": str(cwd), "mcpServers": []})
            updates = [n["params"]["update"] for n in second.drain_notifications()
                       if n.get("method") == "session/update"]
            assert {m["turnId"] for m in messages}.issubset(
                {u.get("_meta", {}).get("turnId") for u in updates if u.get("_meta", {}).get("replay")}
            )
        finally:
            await second.stop()
