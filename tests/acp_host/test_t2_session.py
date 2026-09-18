"""T2 — session/prompt, cancel, and process-kill failure modes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from .conftest import skip_if_llm_env_failure
from .issue_draft import to_issue_draft
from .probe import (
    AcpHostProbe,
    CaseResult,
    RpcError,
    collect_session_updates,
    default_acp_command,
    message_text_from_updates,
)


@pytest.mark.asyncio
async def test_t2_01_session_prompt_one_round(tmp_path: Path, require_llm: str) -> None:
    """T2-01: session/prompt one round has reply/updates (live opt-in)."""
    probe = AcpHostProbe(command=default_acp_command(), cwd=tmp_path, timeout_s=120.0)
    logs: list[str] = []
    try:
        await probe.start()
        await probe.initialize()
        session_id = await probe.session_new(cwd=str(tmp_path))
        logs.append(f"sessionId={session_id} llm={require_llm}")
        probe.drain_notifications()

        result = await probe.session_prompt(
            session_id,
            "Reply with exactly the word PONG and nothing else.",
        )
        notes = probe.drain_notifications()
        updates = collect_session_updates(notes)
        text = message_text_from_updates(updates)
        logs.append(f"prompt result={json.dumps(result, ensure_ascii=False)[:500]}")
        logs.append(f"updates={len(updates)} text_preview={text[:200]!r}")

        skip_if_llm_env_failure(probe.stderr_text, case_id="T2-01")
        stop = result.get("stopReason") if isinstance(result, dict) else None
        has_reply = bool(text.strip()) or len(updates) > 0
        ok = bool(has_reply) and stop is not None
        case = CaseResult(
            case_id="T2-01",
            ok=ok,
            expected="session/prompt returns stopReason and/or session/update message chunks",
            actual=f"stopReason={stop!r} updates={len(updates)} text={text[:300]!r}",
            logs="\n".join(logs) + "\n" + probe.stderr_text[-3000:],
            repro_steps=[
                "BOX_AGENT_ACP_HOST_LIVE=1",
                "start box_agent.acp.server",
                "initialize + session/new",
                "session/prompt with a short greeting",
                "expect session/update chunks and a final PromptResponse",
            ],
        )
        if not case.ok:
            draft = to_issue_draft(case)
            pytest.fail(f"T2-01 failed; IssueDraft:\n{draft['title']}\n{draft['body']}")
    except Exception as exc:
        draft = to_issue_draft(
            CaseResult(
                case_id="T2-01",
                ok=False,
                expected="session/prompt one round has reply/updates",
                actual=f"{type(exc).__name__}: {exc}",
                logs="\n".join(logs) + "\n" + probe.stderr_text[-4000:],
                repro_steps=[
                    "uv run pytest tests/acp_host/test_t2_session.py::test_t2_01_session_prompt_one_round -q",
                ],
            )
        )
        pytest.fail(f"T2-01 exception; IssueDraft:\n{draft['body']}")
    finally:
        await probe.stop()


@pytest.mark.asyncio
async def test_t2_02_session_cancel_mid_stream(tmp_path: Path, require_llm: str) -> None:
    """T2-02: session/cancel mid-stream matches protocol (stopReason=cancelled)."""
    probe = AcpHostProbe(command=default_acp_command(), cwd=tmp_path, timeout_s=120.0)
    logs: list[str] = []
    try:
        await probe.start()
        await probe.initialize()
        session_id = await probe.session_new(cwd=str(tmp_path))
        probe.drain_notifications()

        async def _prompt() -> object:
            return await probe.session_prompt(
                session_id,
                "Write a long detailed essay about the history of computing, "
                "at least 2000 words, with many sections. Take your time.",
            )

        task = asyncio.create_task(_prompt())
        # Let the model start streaming, then cancel.
        await asyncio.sleep(2.0)
        await probe.session_cancel(session_id)
        logs.append("sent session/cancel")

        try:
            result = await asyncio.wait_for(task, timeout=90.0)
        except Exception as exc:
            # Cancel may surface as RpcError if the agent drops the request — still protocol-relevant.
            case = CaseResult(
                case_id="T2-02",
                ok=False,
                expected="After session/cancel, prompt completes with stopReason='cancelled'",
                actual=f"prompt task raised {type(exc).__name__}: {exc}",
                logs="\n".join(logs) + "\n" + probe.stderr_text[-4000:],
                repro_steps=[
                    "session/prompt with a long task",
                    "session/cancel after ~2s",
                    "observe PromptResponse.stopReason == cancelled",
                ],
            )
            draft = to_issue_draft(case)
            pytest.fail(f"T2-02 failed; IssueDraft:\n{draft['body']}")
            return

        skip_if_llm_env_failure(probe.stderr_text, case_id="T2-02")
        stop = result.get("stopReason") if isinstance(result, dict) else None
        logs.append(f"result={json.dumps(result, ensure_ascii=False)[:500]}")
        # Cooperative cancel: cancelled is ideal; end_turn is acceptable if the
        # model finished before the cancel flag was observed — still record it.
        ok = stop in {"cancelled", "end_turn", "refusal"}
        case = CaseResult(
            case_id="T2-02",
            ok=ok and stop == "cancelled",
            expected="stopReason == 'cancelled' after mid-stream session/cancel",
            actual=f"stopReason={stop!r}",
            logs="\n".join(logs) + "\n" + probe.stderr_text[-3000:],
            repro_steps=[
                "session/prompt long task",
                "session/cancel mid-stream",
                "expect stopReason=cancelled",
            ],
        )
        if not case.ok:
            # Soft-fail path: produce IssueDraft but only fail hard when stop is missing.
            draft = to_issue_draft(
                CaseResult(
                    case_id="T2-02",
                    ok=False,
                    expected=case.expected,
                    actual=case.actual,
                    logs=case.logs,
                    repro_steps=case.repro_steps,
                )
            )
            if stop is None:
                pytest.fail(f"T2-02 missing stopReason; IssueDraft:\n{draft['body']}")
            # Non-cancelled but valid protocol stop — report via xfail-style message.
            pytest.xfail(
                f"T2-02 cooperative cancel observed stopReason={stop!r} "
                f"(IssueDraft prepared: {draft['title']})"
            )
    finally:
        await probe.stop()


@pytest.mark.asyncio
async def test_t2_03_kill_process_is_clear_failure(make_acp_probe, tmp_path: Path) -> None:
    """T2-03: kill stdin/process → clear failure, not false success.

    No LLM: after initialize/session_new, park a deterministic pending future
    (without session/prompt → provider) then kill the real ACP subprocess.
    """
    probe = make_acp_probe(cwd=tmp_path / "ws", timeout_s=60.0)
    logs: list[str] = []
    await probe.start()
    try:
        await probe.initialize()
        session_id = await probe.session_new(cwd=str(tmp_path / "ws"))
        logs.append(f"sessionId={session_id}")

        loop = asyncio.get_running_loop()
        pending: asyncio.Future[object] = loop.create_future()
        # Deterministic in-flight request without invoking the LLM provider.
        probe._pending["t2-03-sentinel"] = pending

        async def _await_pending() -> object:
            return await pending

        task = asyncio.create_task(_await_pending())
        await asyncio.sleep(0)  # let the task schedule
        assert "t2-03-sentinel" in probe._pending
        await probe.kill()
        logs.append("killed ACP subprocess with sentinel pending (no LLM)")

        with pytest.raises(RpcError) as ei:
            await task
        err = ei.value
        assert err.code in {"process_killed", "eof", "stdin_broken", "reader_error", "stopped"}
        assert "success" not in err.message.lower() or err.code != "ok"
        # Must not look like a successful PromptResponse
        assert not (isinstance(err.data, dict) and err.data.get("stopReason") == "end_turn")
        assert err.code != "ok"
    finally:
        await probe.stop()
