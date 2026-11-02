#!/usr/bin/env python3
"""Deterministic newline-delimited ACP server used by case-runner tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


MODES = {
    "normal",
    "malformed",
    "timeout",
    "initialize-timeout",
    "new-session-timeout",
    "late-completion",
    "trailing-stderr",
    "missing-trace",
    "large-frame",
    "descendant-pipes",
    "invalid-trace",
    "empty-trace",
    "raw-mismatch",
    "slow-drip",
    "initialize-error",
    "mismatch-upstream-session",
    "mismatch-update-session",
    "mismatch-trace-session",
    "mismatch-trace-acp-session",
    "rate-limit", "rate-limit-once", "rate-limit-after-tool", "provider-error",
    "rate-limit-after-content",
}


def receive() -> dict[str, Any]:
    raw = sys.stdin.buffer.readline()
    if not raw:
        raise EOFError
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("ACP message must be an object")
    return value


def send(message: dict[str, Any]) -> None:
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(raw + b"\n")
    sys.stdout.buffer.flush()


def write_trace(
    upstream_session_id: str,
    acp_session_id: str,
    mode: str,
) -> None:
    trace_dir = Path(os.environ["BOX_AGENT_SESSION_TRACE_DIR"])
    trace_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_version": "box-agent-session-trace/v1",
        "event": "session.start",
        "provider_stale_seconds": os.environ.get("BOX_AGENT_PROVIDER_STALE_SECONDS"),
        "session_id": (
            "eval-acp-wrong"
            if mode == "mismatch-trace-session"
            else upstream_session_id
        ),
        "acp_session_id": (
            "sess-wrong"
            if mode == "mismatch-trace-acp-session"
            else acp_session_id
        ),
        "turn_id": "",
    }
    trace_path = trace_dir / f"{upstream_session_id}.jsonl"
    if mode == "invalid-trace":
        trace_path.write_bytes(b'{"type":"agent.started"')
    elif mode == "empty-trace":
        trace_path.write_bytes(b"")
    else:
        trace_path.write_text(
            json.dumps(event, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def run(mode: str) -> int:
    if mode not in MODES:
        raise SystemExit(f"unknown fake ACP mode: {mode}")

    initialize = receive()
    if mode == "initialize-timeout":
        time.sleep(60)
    if mode == "initialize-error":
        write_trace(f"eval-acp-{mode}", "sess-initialize-error", mode)
        send(
            {
                "jsonrpc": "2.0",
                "id": initialize["id"],
                "error": {"code": -32000, "message": "injected initialize error"},
            }
        )
        return 0
    send(
        {
            "jsonrpc": "2.0",
            "id": initialize["id"],
            "result": {"protocolVersion": 1},
        }
    )

    new_session = receive()
    if mode == "new-session-timeout":
        time.sleep(60)
    params = new_session["params"]
    upstream_session_id = params["_meta"]["session_id"]
    acp_session_id = "sess-0-fake"
    workspace = Path(params["cwd"])
    send(
        {
            "jsonrpc": "2.0",
            "id": new_session["id"],
            "result": {"sessionId": acp_session_id},
        }
    )

    prompt = receive()
    if mode != "missing-trace":
        write_trace(upstream_session_id, acp_session_id, mode)

    if mode in {"rate-limit", "rate-limit-once", "rate-limit-after-tool", "provider-error", "rate-limit-after-content"}:
        marker = Path(__file__).with_suffix(".limited")
        if mode != "rate-limit-once" or not marker.exists():
            marker.touch()
            error_message = "请求过于频繁，已触发服务限流（429）。请稍候片刻再重试。"
            error_details = {"source": "llm_provider", "httpStatus": 500 if mode == "provider-error" else 429,
                             "retryable": True}
            partial_content = "Already started answering." if mode == "rate-limit-after-content" else ""
            trace_path = Path(os.environ["BOX_AGENT_SESSION_TRACE_DIR"]) / f"{upstream_session_id}.jsonl"
            with trace_path.open("a") as stream:
                events = [("llm.request", {}),
                          *([("tool.request", {})] if mode == "rate-limit-after-tool" else []),
                          ("llm.error", {"partial_content": partial_content, "partial_thinking": None}),
                          ("turn.error", {"message": error_message, "error_details": error_details})]
                for event, data in events:
                    stream.write(json.dumps({"event": event, "session_id": upstream_session_id,
                                             "acp_session_id": acp_session_id, "data": data}) + "\n")
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": acp_session_id, "update": {"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": partial_content + f"Error: {error_message}"}},
            }})
            send({"jsonrpc": "2.0", "id": prompt["id"], "result": {"stopReason": "end_turn", "_meta": {
                "ok": False, "completed": False, "runStatus": "error", "errorCategory": "rate_limit",
                "error": error_message, "errorDetails": error_details,
                "usage": {"sessionId": upstream_session_id, "turnId": prompt["params"]["_meta"]["turnId"]},
            }}})
            return 0

    if mode == "timeout":
        time.sleep(60)
        return 0

    if mode == "late-completion":
        cancel = receive()
        assert cancel["method"] == "session/cancel"
        send({"jsonrpc": "2.0", "id": prompt["id"], "result": {
            "stopReason": "end_turn", "_meta": {"completed": True, "ok": True},
        }})
        return 0

    if mode == "slow-drip":
        for _ in range(30):
            sys.stdout.buffer.write(b"{")
            sys.stdout.buffer.flush()
            time.sleep(0.05)
        return 0

    if mode == "malformed":
        sys.stdout.buffer.write(b'{"method": broken}\n')
        sys.stdout.buffer.flush()

    permission_id = 81
    send(
        {
            "jsonrpc": "2.0",
            "id": permission_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": acp_session_id,
                "options": [{"optionId": "allow"}],
            },
        }
    )
    permission_reply = receive()
    if permission_reply != {
        "jsonrpc": "2.0",
        "id": permission_id,
        "result": {"outcome": {"outcome": "cancelled"}},
    }:
        raise RuntimeError(f"unexpected permission reply: {permission_reply!r}")

    if mode == "large-frame":
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": acp_session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "x" * (5 * 1024 * 1024)},
                    },
                },
            }
        )
    else:
        update_session_id = (
            "sess-wrong" if mode == "mismatch-update-session" else acp_session_id
        )
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": update_session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello "},
                    },
                },
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": update_session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "world"},
                        "rawOutput": {
                            "type": "turn_usage",
                            "sessionId": upstream_session_id,
                            "session_id": upstream_session_id,
                            "acpSessionId": acp_session_id,
                            "turnId": f"eval-acp-{mode}-turn-1",
                            "turn_id": f"eval-acp-{mode}-turn-1",
                            "tokenUsage": {
                                "promptTokens": 10,
                                "completionTokens": 2,
                                "totalTokens": 12,
                            },
                        },
                    },
                },
            }
        )

    output = workspace / "output"
    output.mkdir(parents=True, exist_ok=True)
    (output / "answer.txt").write_text("artifact body\n", encoding="utf-8")
    if mode == "descendant-pipes":
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3)",
            ]
        )

    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": (
                    "sess-wrong"
                    if mode == "mismatch-update-session"
                    else acp_session_id
                ),
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "rawOutput": {
                        "type": "artifact",
                        "rel_path": "output/answer.txt",
                        "mime": "text/plain",
                    },
                },
            },
        }
    )
    send(
        {
            "jsonrpc": "2.0",
            "id": prompt["id"],
            "result": {
                "stopReason": "end_turn",
                "_meta": {
                    "ok": True,
                    "completed": True,
                    "runStatus": "completed",
                    "usage": {
                        "sessionId": (
                            "eval-acp-wrong"
                            if mode == "mismatch-upstream-session"
                            else upstream_session_id
                        ),
                        "session_id": (
                            "eval-acp-wrong"
                            if mode == "mismatch-upstream-session"
                            else upstream_session_id
                        ),
                        "turnId": f"eval-acp-{mode}-turn-1",
                        "turn_id": f"eval-acp-{mode}-turn-1",
                    },
                },
            },
        }
    )

    attempt_dir = Path(os.environ["BOX_AGENT_SESSION_TRACE_DIR"]).parent
    if mode == "raw-mismatch":
        with (attempt_dir / "acp-stdout.raw").open("ab") as stream:
            stream.write(b'{"jsonrpc":"2.0","method":"injected"}\n')

    if mode == "trailing-stderr":
        sys.stderr.write("2026-08-21T12:00:00Z [WARNING] trailing diagnostic\n")
        sys.stderr.flush()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: fake_acp.py MODE")
    raise SystemExit(run(sys.argv[1]))
