"""Deterministic desktop scenarios also run through real ACP stdio in CI."""

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory

import pytest

from tests.acp_host.probe import (
    AcpHostProbe, RpcError, collect_session_updates, message_text_from_updates,
)
from acp_eval.transport import _send


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["approve", "cancel"])
async def test_desktop_fixture_busy_error_preserves_original_prompt(finish):
    workspace = Path(__file__).resolve().parents[1] / "workspace"
    workspace.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="desktop-overlap-", dir=workspace) as directory:
        root = Path(directory)
        probe = AcpHostProbe(
            command=[sys.executable, "-m", "tests.desktop_runtime_fixture"], cwd=root,
            env={"BOX_AGENT_DESKTOP_TEST_ROOT": str(root), "BOX_AGENT_HOME": str(root / "profile"),
                 "PYTHONUTF8": "1"}, timeout_s=10,
        )
        await probe.start()
        process = probe._proc

        async def read_until(predicate):
            while True:
                eof, message = await probe._reader.read_frame(deadline=None)
                assert not eof, "ACP exited while the original prompt was active"
                assert not probe._protocol.parse_errors
                if message and predicate(message):
                    return message

        try:
            await probe.initialize()
            session = await probe.session_new(cwd=str(root / "profile/workspaces/test"))
            await _send(process, probe._protocol, {
                "jsonrpc": "2.0", "id": 900, "method": "session/prompt",
                "params": {"sessionId": session,
                           "prompt": [{"type": "text", "text": "fixture:permission"}]},
            })
            permission = await asyncio.wait_for(read_until(
                lambda m: m.get("method") == "session/request_permission",
            ), 5)
            supplement = {"sessionId": session, "text": "keep supplement", "injectionId": "keep"}
            assert (await probe.request("_inject", supplement))["ok"] is True
            with pytest.raises(RpcError) as raised:
                await probe.session_prompt(session, "fixture:normal")
            assert raised.value.code == -32010
            assert raised.value.data == {"code": "SESSION_BUSY", "sessionId": session}
            assert "Wait for it to finish" in raised.value.message
            assert "wait for cancellation to complete" in raised.value.message
            assert (await probe.request("_inject", supplement))["deduplicated"] is True
            assert (await probe.request("_inject", {
                "sessionId": session, "text": "new supplement", "injectionId": "new",
            }))["ok"] is True
            if finish == "approve":
                option = next(o["optionId"] for o in permission["params"]["options"]
                              if o["kind"] == "allow_once")
                await _send(process, probe._protocol, {
                    "jsonrpc": "2.0", "id": permission["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": option}},
                })
            else:
                await probe.notify("session/cancel", {"sessionId": session})
            response = await asyncio.wait_for(read_until(lambda m: m.get("id") == 900), 5)
            assert "error" not in response
            assert response["result"]["stopReason"] == ("end_turn" if finish == "approve" else "cancelled")
            record = json.loads((root / "results.jsonl").read_text().splitlines()[-1])
            assert len(record["executions"]) == (1 if finish == "approve" else 0)
            assert (await probe.session_prompt(session, "fixture:normal"))["_meta"]["ok"] is True
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0
            lifecycle = [json.loads(line) for line in (root / "lifecycle.jsonl").read_text().splitlines()]
            assert any(r["event"] == "adapter_closed" and r["active_runs"] == 0 for r in lifecycle)
            assert any(r["event"] == "llm_closed" for r in lifecycle)
            assert not probe._protocol.parse_errors
        finally:
            await probe.stop()
        assert "Task exception was never retrieved" not in probe.stderr_text
        assert "Task was destroyed" not in probe.stderr_text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "slow", "oversize", "timeout", "budget", "nested"])
async def test_desktop_fixture_reports_real_run_outcome(mode):
    workspace = Path(__file__).resolve().parents[1] / "workspace"
    workspace.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="desktop-fixture-", dir=workspace) as directory:
        root = Path(directory)
        probe = AcpHostProbe(
            command=[sys.executable, "-m", "tests.desktop_runtime_fixture"], cwd=root,
            env={"BOX_AGENT_DESKTOP_TEST_ROOT": str(root), "BOX_AGENT_HOME": str(root / "profile"),
                 "PYTHONUTF8": "1"}, timeout_s=20,
        )
        await probe.start()
        try:
            await probe.initialize()
            session = await probe.session_new(cwd=str(root / "profile/workspaces/test"))
            if mode in {"oversize", "timeout"}:
                with pytest.raises(RpcError) as raised:
                    await probe.session_prompt(session, f"fixture:{mode}")
                assert raised.value.code == -32603
            else:
                response = await probe.session_prompt(session, f"fixture:{mode}")
                assert response["stopReason"] == "end_turn"
            record = json.loads((root / "results.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            assert record["scenario"] == mode
            if mode in {"oversize", "timeout"}:
                assert record["status"] == "failed"
                assert record["error"]["code"] == {
                    "oversize": "RUN_EVENT_TOO_LARGE", "timeout": "RUN_EVENT_CONSUMER_TIMEOUT",
                }[mode]
            else:
                assert record["status"] == "completed"
            if mode == "slow":
                text = message_text_from_updates(collect_session_updates(probe.drain_notifications()))
                assert text == "".join(f"piece-{index};" for index in range(40))
            if mode in {"budget", "nested"}:
                assert len(record["executions"]) == 3
            if mode == "nested":
                assert sorted(item["tool"] for item in record["executions"]) == [
                    "fixture_count", "fixture_nested", "fixture_nested",
                ]
        finally:
            await probe.stop()


@pytest.mark.asyncio
async def test_request_cleanup_preserves_interleaved_sessions_and_host_permissions():
    workspace = Path(__file__).resolve().parents[1] / "workspace"
    workspace.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="desktop-rpc-state-", dir=workspace) as directory:
        root = Path(directory)
        probe = AcpHostProbe(
            command=[sys.executable, "-m", "tests.desktop_runtime_fixture"], cwd=root,
            env={"BOX_AGENT_DESKTOP_TEST_ROOT": str(root), "BOX_AGENT_HOME": str(root / "profile"),
                 "PYTHONUTF8": "1"}, timeout_s=10,
        )
        await probe.start()
        process = probe._proc

        async def read_until(predicate):
            while True:
                eof, message = await probe._reader.read_frame(deadline=None)
                assert not eof
                assert not probe._protocol.parse_errors
                if message and predicate(message):
                    return message

        async def waiting_permission(session, request_id):
            await _send(process, probe._protocol, {
                "jsonrpc": "2.0", "id": request_id, "method": "session/prompt",
                "params": {"sessionId": session,
                           "prompt": [{"type": "text", "text": "fixture:permission"}]},
            })
            return await asyncio.wait_for(read_until(
                lambda m: m.get("method") == "session/request_permission",
            ), 5)

        try:
            await probe.initialize()
            cwd = str(root / "profile/workspaces/test")
            handles = {}
            for product in ("a", "b", "c", "d"):
                handles[product] = await probe.session_new(cwd=cwd, meta={"session_id": product})
                response = await probe.session_prompt(handles[product], f"fixture:normal seed-{product}")
                assert response["stopReason"] == "end_turn"

            # Two independent host callbacks remain live while other sessions run.
            permission_a = await waiting_permission(handles["a"], 900)
            permission_d = await waiting_permission(handles["d"], 901)
            assert permission_a["id"] != permission_d["id"]
            response = await probe.session_prompt(handles["b"], "fixture:normal continuation-b")
            assert response["stopReason"] == "end_turn"
            with pytest.raises(RpcError) as raised:
                await probe.session_prompt(handles["c"], "fixture:oversize")
            assert raised.value.code == -32603
            assert (await probe.ext_request("inject", {
                "sessionId": handles["a"], "text": "supplement-a", "injectionId": "a-1",
            }))["ok"]
            state = await probe.ext_request("fixture_rpc_state")
            assert state["incoming_records"] == 0
            assert state["outgoing_requests"] == 2
            assert set(state["sessions"]) == {"a", "b", "c", "d"}

            # Complete in reverse order; approval must reach D, cancellation A.
            option = next(o["optionId"] for o in permission_d["params"]["options"]
                          if o["kind"] == "allow_once")
            await _send(process, probe._protocol, {
                "jsonrpc": "2.0", "id": permission_d["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": option}},
            })
            response = await asyncio.wait_for(read_until(lambda m: m.get("id") == 901), 5)
            assert response["result"]["stopReason"] == "end_turn"
            await probe.session_cancel(handles["a"])
            response = await asyncio.wait_for(read_until(lambda m: m.get("id") == 900), 5)
            assert response["result"]["stopReason"] == "cancelled"
            # Cancellation retires the ID even when the host never replies.
            state = await probe.ext_request("fixture_rpc_state")
            assert state["outgoing_requests"] == 0
            # A host reply arriving after cancellation is harmless.
            await _send(process, probe._protocol, {
                "jsonrpc": "2.0", "id": permission_a["id"],
                "result": {"outcome": {"outcome": "cancelled"}},
            })

            for index in range(12):
                product = ("a", "b", "c", "d")[index % 4]
                old = handles[product]
                handles[product] = await probe.session_new(cwd=cwd, meta={
                    "session_id": product, "fixture_payload": "x" * (256 * 1024),
                })
                assert handles[product] != old
                response = await probe.session_prompt(handles[product], f"fixture:normal resumed-{product}")
                assert response["stopReason"] == "end_turn"

            state = await probe.ext_request("fixture_rpc_state")
            assert state["incoming_records"] == state["outgoing_requests"] == 0
            assert len(state["sessions"]) == 4
            for product, session in state["sessions"].items():
                assert session["handle"] == handles[product]
                assert any(f"seed-{product}" in text for text in session["users"])
                assert any(f"resumed-{product}" in text for text in session["users"])
                assert not any(f"seed-{peer}" in text for peer in handles if peer != product
                               for text in session["users"])
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert not probe._protocol.parse_errors
        finally:
            await probe.stop()
        assert "Task exception was never retrieved" not in probe.stderr_text
        assert "Task was destroyed" not in probe.stderr_text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["idle", "permission", "stream_wait"])
@pytest.mark.parametrize("shutdown", ["eof", "sigterm", "eof_and_sigterm"])
async def test_desktop_fixture_closes_resources_when_host_disconnects(mode, shutdown):
    if shutdown != "eof" and os.name == "nt":
        pytest.skip("Windows terminate() does not deliver a catchable SIGTERM")
    workspace = Path(__file__).resolve().parents[1] / "workspace"
    workspace.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="desktop-shutdown-", dir=workspace) as directory:
        root = Path(directory)
        probe = AcpHostProbe(
            command=[sys.executable, "-m", "tests.desktop_runtime_fixture"], cwd=root,
            env={"BOX_AGENT_DESKTOP_TEST_ROOT": str(root), "BOX_AGENT_HOME": str(root / "profile"),
                 "PYTHONUTF8": "1"}, timeout_s=20,
        )
        await probe.start()
        process = probe._proc
        try:
            await probe.initialize()
            session = await probe.session_new(cwd=str(root / "profile/workspaces/test"))
            if mode == "idle":
                response = await probe.session_prompt(session, "fixture:normal")
                assert response["stopReason"] == "end_turn"
            else:
                await _send(process, probe._protocol, {
                    "jsonrpc": "2.0", "id": 900, "method": "session/prompt",
                    "params": {"sessionId": session,
                               "prompt": [{"type": "text", "text": f"fixture:{mode}"}]},
                })

                async def wait_for_active_request():
                    while True:
                        eof, message = await probe._reader.read_frame(deadline=None)
                        assert not eof, "ACP exited before the test reached an active request"
                        if mode == "permission" and message.get("method") == "session/request_permission":
                            return
                        update = message.get("params", {}).get("update", {})
                        if mode == "stream_wait" and update.get("content", {}).get("text") == "STREAM_WAIT_READY":
                            return

                await asyncio.wait_for(wait_for_active_request(), 5)
            assert process.returncode is None
            if shutdown in {"eof", "eof_and_sigterm"}:
                process.stdin.close()
            if shutdown in {"sigterm", "eof_and_sigterm"}:
                process.send_signal(signal.SIGTERM)
            # Assert natural completion before probe.stop() can send TERM/KILL.
            assert await asyncio.wait_for(process.wait(), 5) == 0
            records = [json.loads(line) for line in (root / "lifecycle.jsonl").read_text().splitlines()]
            assert [r for r in records if r["event"] == "adapter_closed"] == [{
                "event": "adapter_closed", "remaining_sessions": 0,
                "closed_sessions": True, "active_runs": 0,
            }]
            assert sum(r["event"] == "llm_closed" for r in records) == 1
            if mode == "stream_wait":
                assert sum(r["event"] == "stream_closed" for r in records) == 1
            assert not probe._protocol.parse_errors
        finally:
            await probe.stop()
        assert "Task was destroyed" not in probe.stderr_text
        assert "Task exception was never retrieved" not in probe.stderr_text


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,existing", [
    ("close", True), ("browser", True), ("initialization", True),
    ("initialization", False),
])
@pytest.mark.parametrize("shutdown", ["eof", "eof_and_sigterm"])
async def test_desktop_fixture_shutdown_settles_inflight_session_creation(phase, existing, shutdown):
    if shutdown != "eof" and os.name == "nt":
        pytest.skip("Windows terminate() does not deliver a catchable SIGTERM")
    workspace = Path(__file__).resolve().parents[1] / "workspace"
    workspace.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="desktop-binding-shutdown-", dir=workspace) as directory:
        root = Path(directory)
        probe = AcpHostProbe(
            command=[sys.executable, "-m", "tests.desktop_runtime_fixture"], cwd=root,
            env={"BOX_AGENT_DESKTOP_TEST_ROOT": str(root), "BOX_AGENT_HOME": str(root / "profile"),
                 "BOX_AGENT_DESKTOP_TEST_BINDING_PHASE": phase, "PYTHONUTF8": "1"},
            timeout_s=10,
        )
        await probe.start()
        process = probe._proc
        try:
            await probe.initialize()
            params = {"cwd": str(root / "profile/workspaces/test"), "mcpServers": []}
            # Also cover the first anonymous session: it has no product-ID reservation.
            meta = {"session_id": "shutdown-product"} if existing else {}
            if existing:
                await probe.request("session/new", {**params, "_meta": meta})
            await _send(process, probe._protocol, {
                "jsonrpc": "2.0", "id": 900, "method": "session/new",
                "params": {**params, "_meta": {**meta, "fixture_block_binding": True}},
            })

            async def wait_for_binding():
                while not (root / "binding-waiting").exists():
                    assert process.returncode is None
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_binding(), 5)
            process.stdin.close()
            if shutdown == "eof_and_sigterm":
                with suppress(ProcessLookupError):
                    process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(process.wait(), 5) == 0
            records = [json.loads(line) for line in (root / "lifecycle.jsonl").read_text().splitlines()]
            assert sum(r["event"] == "binding_wait_closed" for r in records) == 1
            assert [r for r in records if r["event"] == "binding_shutdown"] == [{
                "event": "binding_shutdown", "creating": 0, "binding": 0,
                "opening": 0, "plugin_sessions": 0,
            }]
            assert sum(r["event"] == "adapter_closed" for r in records) == 1
            assert sum(r["event"] == "llm_closed" for r in records) == 1
            assert not probe._protocol.parse_errors
        finally:
            await probe.stop()
        assert "KeyError" not in probe.stderr_text
        assert "Task was destroyed" not in probe.stderr_text
        assert "Task exception was never retrieved" not in probe.stderr_text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["oversized_stdin", "stdout_pressure"])
@pytest.mark.parametrize("shutdown", ["eof", "sigterm"])
async def test_desktop_fixture_exits_after_transport_failure(mode, shutdown):
    if os.name == "nt":
        pytest.skip("POSIX pipe backpressure and catchable SIGTERM regression")
    from box_agent.acp.stdio_compat import _READ_LIMIT

    workspace = Path(__file__).resolve().parents[1] / "workspace"
    workspace.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="desktop-transport-", dir=workspace) as directory:
        root = Path(directory)
        probe = AcpHostProbe(
            command=[sys.executable, "-m", "tests.desktop_runtime_fixture"], cwd=root,
            env={"BOX_AGENT_DESKTOP_TEST_ROOT": str(root), "BOX_AGENT_HOME": str(root / "profile"),
                 "PYTHONUTF8": "1"}, timeout_s=20,
        )
        await probe.start()
        process = probe._proc
        try:
            await probe.initialize()
            session = await probe.session_new(cwd=str(root / "profile/workspaces/test"))
            if mode == "oversized_stdin":
                text = "x" * (_READ_LIMIT + 1)
            else:
                text = "fixture:stdout_pressure"
            with suppress(BrokenPipeError, ConnectionResetError):
                await _send(process, probe._protocol, {
                    "jsonrpc": "2.0", "id": 900, "method": "session/prompt",
                    "params": {"sessionId": session, "prompt": [{"type": "text", "text": text}]},
                })
            if mode == "stdout_pressure":
                async def wait_for_backpressure():
                    while not (root / "stdout-paused").exists():
                        assert process.returncode is None
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_for_backpressure(), 5)
            if shutdown == "eof":
                process.stdin.close()
            elif process.returncode is None:
                process.send_signal(signal.SIGTERM)

            # Process.wait() also waits for buffered stdout to drain. Observe
            # OS exit without consuming stdout or sending fallback signals.
            async def wait_for_exit():
                while process.returncode is None:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_exit(), 5)
            assert process.returncode == 0
            records = [json.loads(line) for line in (root / "lifecycle.jsonl").read_text().splitlines()]
            assert [r for r in records if r["event"] == "adapter_closed"] == [{
                "event": "adapter_closed", "remaining_sessions": 0,
                "closed_sessions": True, "active_runs": 0,
            }]
            assert sum(r["event"] == "llm_closed" for r in records) == 1
        finally:
            # Release buffered pipe data only after the natural-exit assertion.
            drain = asyncio.create_task(process.stdout.read())
            try:
                await asyncio.wait_for(probe.stop(), 8)
            finally:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 3)
                await asyncio.wait_for(drain, 3)
        assert "Task was destroyed" not in probe.stderr_text
        assert "Task exception was never retrieved" not in probe.stderr_text
