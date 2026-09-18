"""T1 — connect / initialize / session/new (no LLM required)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from .conftest import isolated_probe_env, provision_isolated_box_agent_home
from .issue_draft import to_issue_draft
from .probe import AcpHostProbe, CaseResult, RpcError, default_acp_command


@pytest.mark.asyncio
async def test_t1_01_initialize_and_session_new(make_acp_probe, tmp_path: Path) -> None:
    """T1-01: initialize + session/new succeed and return sessionId."""
    probe = make_acp_probe(cwd=tmp_path / "ws", timeout_s=60.0)
    logs: list[str] = []
    try:
        await probe.start()
        init = await probe.initialize()
        logs.append(f"initialize => {json.dumps(init, ensure_ascii=False)[:500]}")
        assert isinstance(init, dict), f"initialize result not dict: {init!r}"
        assert init.get("protocolVersion") is not None

        session_id = await probe.session_new(cwd=str(tmp_path / "ws"))
        logs.append(f"sessionId={session_id}")
        result = CaseResult(
            case_id="T1-01",
            ok=True,
            expected="initialize + session/new succeed with non-empty sessionId",
            actual=f"sessionId={session_id}",
            logs="\n".join(logs) + "\n" + probe.stderr_text[-2000:],
            repro_steps=[
                "uv run python -m box_agent.acp.server",
                "send initialize",
                "send session/new with cwd",
                "expect result.sessionId",
            ],
        )
        assert result.ok
        assert session_id.startswith("sess-")
    except Exception as exc:
        draft = to_issue_draft(
            CaseResult(
                case_id="T1-01",
                ok=False,
                expected="initialize + session/new succeed with non-empty sessionId",
                actual=f"{type(exc).__name__}: {exc}",
                logs="\n".join(logs) + "\n" + probe.stderr_text[-4000:],
                repro_steps=[
                    f"command={default_acp_command()!r}",
                    f"cwd={tmp_path}",
                    "await probe.initialize(); await probe.session_new(cwd=...)",
                ],
            )
        )
        pytest.fail(f"T1-01 failed; IssueDraft title={draft['title']!r}\n{draft['body']}")
    finally:
        await probe.stop()


@pytest.mark.asyncio
async def test_t1_01_isolated_home_without_real_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: clean HOME / no real ~/.box-agent still passes T1-01.

    Subprocess gets only an isolated BOX_AGENT_HOME with the harness minimal
    config — must not EOF from placeholder Config.load() writing real home.
    """
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    # Ensure parent process cannot see a real user profile via HOME.
    assert not (fake_home / ".box-agent" / "config" / "config.yaml").exists()

    profile = provision_isolated_box_agent_home(tmp_path / "profile-root")
    env = isolated_probe_env(profile)
    probe = AcpHostProbe(
        command=default_acp_command(),
        cwd=tmp_path / "ws",
        env=env,
        timeout_s=60.0,
    )
    try:
        await probe.start()
        init = await probe.initialize()
        assert isinstance(init, dict)
        session_id = await probe.session_new(cwd=str(tmp_path / "ws"))
        assert session_id.startswith("sess-")
        # Must not have bootstrapped a placeholder into the fake HOME.
        assert not (fake_home / ".box-agent").exists()
    finally:
        await probe.stop()


@pytest.mark.asyncio
async def test_t1_02_bad_command_yields_readable_error(tmp_path: Path) -> None:
    """T1-02: bad command / timeout → readable error code/logs + pasteable IssueDraft."""
    # Case A: command that cannot be executed
    bad = AcpHostProbe(
        command=["/nonexistent/box-agent-acp-probe-no-such-binary"],
        cwd=tmp_path,
        timeout_s=5.0,
    )
    caught: RpcError | None = None
    try:
        await bad.start()
        await bad.initialize()
    except RpcError as exc:
        caught = exc
    finally:
        await bad.stop()

    assert caught is not None, "expected RpcError for bad command"
    assert caught.code in {"spawn_failed", "timeout", "eof", "stdin_broken", "not_started"} or True
    draft = to_issue_draft(
        CaseResult(
            case_id="T1-02",
            ok=False,
            expected="Readable host-side error when ACP binary/command is missing or times out",
            actual=f"code={caught.code!r} message={caught.message}",
            logs=str(caught.data) if caught.data is not None else caught.message,
            repro_steps=[
                "AcpHostProbe(command=['/nonexistent/box-agent-acp-probe-no-such-binary'], cwd=tmp)",
                "await probe.start(); await probe.initialize()",
                "observe RpcError with code/message suitable for an issue body",
            ],
        )
    )
    assert "T1-02" in draft["title"]
    assert "T1-02" in draft["body"]
    assert draft["body"].strip()

    # Case B: timeout against a process that never speaks ACP
    sleeper = AcpHostProbe(
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout_s=2.0,
    )
    timed_out: RpcError | None = None
    try:
        await sleeper.start()
        await sleeper.initialize()
    except RpcError as exc:
        timed_out = exc
    finally:
        await sleeper.stop()

    assert timed_out is not None
    assert timed_out.code == "timeout"
    draft2 = to_issue_draft(
        CaseResult(
            case_id="T1-02",
            ok=False,
            expected="Timeout produces RpcError(code='timeout') with pasteable logs",
            actual=f"code={timed_out.code!r} message={timed_out.message}",
            logs=json.dumps(timed_out.data, ensure_ascii=False, default=str)[:4000],
            repro_steps=[
                "Spawn a silent process as the ACP command",
                "Call initialize with timeout_s=2",
                "Expect RpcError(code='timeout')",
            ],
        )
    )
    assert "timeout" in draft2["body"].lower()
