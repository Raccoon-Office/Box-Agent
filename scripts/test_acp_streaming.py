#!/usr/bin/env python3
"""Test ACP streaming: launch box-agent-acp as subprocess, send a prompt, observe real-time events.

Usage:
    uv run python scripts/test_acp_streaming.py
    uv run python scripts/test_acp_streaming.py --mode data_analysis --prompt "分析这个 CSV"
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

# Reuse the repository's ACP framing instead of asyncio's limited readline().
_EVAL_SRC = Path(__file__).resolve().parents[1] / "test_workspace/acp_eval/src"
if str(_EVAL_SRC) not in sys.path:
    sys.path.insert(0, str(_EVAL_SRC))
from acp_eval.protocol import ProtocolRecorder
from acp_eval.transport import _ACPStdoutReader


async def send(proc, obj):
    """Send a JSON-RPC message to the subprocess."""
    line = json.dumps(obj) + "\n"
    proc.stdin.write(line.encode())
    await proc.stdin.drain()


async def read_line(reader: _ACPStdoutReader, timeout: float = 60) -> dict | None:
    """Read a complete JSONL frame, retaining partial bytes across timeouts."""
    eof, message = await reader.read_frame(deadline=time.monotonic() + timeout)
    if reader.protocol.parse_errors:
        raise ValueError(f"Invalid ACP stdout frame: {reader.protocol.parse_errors[-1].error}")
    return None if eof else message


async def main():
    parser = argparse.ArgumentParser(description="Test ACP streaming")
    parser.add_argument("--mode", default=None, help="session_mode (e.g. data_analysis)")
    parser.add_argument("--prompt", default="你好，简单介绍一下你自己", help="prompt text")
    parser.add_argument("--timeout", type=int, default=120, help="max wait seconds for response")
    args = parser.parse_args()

    # The shared reader captures frames; keep diagnostics temporary and local.
    with TemporaryDirectory(prefix="acp-streaming-") as capture_dir:
        await run_session(args, Path(capture_dir))


async def run_session(args: argparse.Namespace, capture_dir: Path) -> None:
    print(f"[test] Starting box-agent-acp subprocess...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "box_agent.acp.server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        protocol = ProtocolRecorder(
            capture_dir, lambda: datetime.now(timezone.utc), time.monotonic_ns
        )
        reader = _ACPStdoutReader(proc.stdout, protocol)

        # 1. Initialize
        print(f"[test] Sending initialize...")
        await send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "test-client", "version": "1.0"},
                "protocolVersion": 1,
            },
        })
        resp = await read_line(reader, timeout=10)
        print(f"[test] initialize response: {json.dumps(resp, ensure_ascii=False)[:200]}")

        # 2. New session
        print(f"[test] Creating session (mode={args.mode})...")
        session_params = {"cwd": ".", "mcpServers": []}
        if args.mode:
            session_params["_meta"] = {"session_mode": args.mode}

        await send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": session_params,
        })
        resp = await read_line(reader, timeout=30)
        print(f"[test] session/new response: {json.dumps(resp, ensure_ascii=False)[:200]}")

        session_id = resp.get("result", {}).get("sessionId", "")
        if not session_id:
            print(f"[test] ERROR: no sessionId in response")
            return

        # 3. Send prompt
        print(f"[test] Sending prompt: {args.prompt}")
        print(f"[test] ---- Streaming events below ----")
        await send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": args.prompt}],
            },
        })

        # 4. Read all events until we get the prompt response (id=3)
        start = time.monotonic()
        event_count = 0
        first_event_at = None
        last_event_at = None

        while True:
            elapsed = time.monotonic() - start
            if elapsed > args.timeout:
                print(f"\n[test] TIMEOUT after {args.timeout}s")
                break

            try:
                msg = await read_line(reader, timeout=min(5, args.timeout - elapsed))
            except asyncio.TimeoutError:
                # A quiet interval or a partial frame does not end the turn.
                # Only the matching response, EOF, or --timeout ends this loop.
                continue

            if msg is None:
                print(f"\n[test] EOF from subprocess")
                break

            now = time.monotonic()
            if first_event_at is None:
                first_event_at = now
            last_event_at = now
            event_count += 1

            # Is this the final response to our prompt request?
            if msg.get("id") == 3:
                print(f"\n[test] ---- Final response (id=3) ----")
                result = msg.get("result", {})
                stop = result.get("stopReason", "?")
                print(f"[test] stopReason: {stop}")
                break

            # It's a notification (session/update)
            params = msg.get("params", {})
            update = params.get("update", {})
            kind = update.get("sessionUpdate", update.get("kind", "?"))

            # Summarize the event
            ts = f"{now - start:.2f}s"
            if kind == "agent_thought_chunk":
                text = update.get("content", {}).get("text", "")
                preview = text[:60].replace("\n", "\\n")
                print(f"  [{ts}] 💭 think: {preview}")
            elif kind == "agent_message_chunk":
                text = update.get("content", {}).get("text", "")
                preview = text[:60].replace("\n", "\\n")
                print(f"  [{ts}] 💬 content: {preview}")
            elif kind == "tool_call_start":
                name = update.get("label", update.get("name", "?"))
                print(f"  [{ts}] 🔧 tool_start: {name}")
            elif kind == "tool_call_progress":
                status = update.get("status", "")
                raw = update.get("rawOutput", "")
                if isinstance(raw, dict):
                    raw_type = raw.get("type", "")
                    print(f"  [{ts}] 📦 tool_update: status={status} rawOutput.type={raw_type}")
                else:
                    preview = str(raw)[:80]
                    print(f"  [{ts}] 📦 tool_update: status={status} raw={preview}")
            else:
                print(f"  [{ts}] ❓ {kind}: {json.dumps(update, ensure_ascii=False)[:120]}")

        # Summary
        print(f"\n[test] ===== Summary =====")
        print(f"[test] Total events: {event_count}")
        if first_event_at and last_event_at:
            print(f"[test] First event at: {first_event_at - start:.2f}s")
            print(f"[test] Duration: {last_event_at - start:.2f}s")
            if event_count > 1:
                avg_interval = (last_event_at - first_event_at) / (event_count - 1)
                print(f"[test] Avg interval: {avg_interval*1000:.0f}ms")
        print(f"[test] Streaming: {'YES ✅' if event_count > 3 else 'NO ❌ (too few events, likely batched)'}")

    finally:
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()


if __name__ == "__main__":
    asyncio.run(main())
