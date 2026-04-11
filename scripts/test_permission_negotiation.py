#!/usr/bin/env python3
"""Manual E2E test for permission negotiation.

Spawns box-agent-acp as a subprocess and simulates the officev3 host:
  1. initialize
  2. session/new  (with officev3 policy: session_workspace scope)
  3. session/prompt  (asks agent to read a file outside workspace)
  4. When agent sends session/request_permission → reply with granted/denied

Usage:
    # Test with GRANT (approve for this prompt):
    uv run python scripts/test_permission_negotiation.py --grant prompt

    # Test with GRANT (approve for session):
    uv run python scripts/test_permission_negotiation.py --grant session

    # Test with DENY:
    uv run python scripts/test_permission_negotiation.py --deny

Prerequisites:
    - box_agent/config/config.yaml must exist with valid LLM API key
    - officev3 block must be configured (see below for auto-injection)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path


# ── JSON-RPC helpers ────────────────────────────────────────

_next_id = 0


def _make_request(method: str, params: dict) -> dict:
    global _next_id
    _next_id += 1
    return {"jsonrpc": "2.0", "id": _next_id, "method": method, "params": params}


def _make_response(request_id: int, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


async def _send(proc: asyncio.subprocess.Process, msg: dict) -> None:
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line.encode())
    await proc.stdin.drain()
    print(f"  → SEND: {json.dumps(msg, ensure_ascii=False)[:200]}")


async def _recv(proc: asyncio.subprocess.Process, timeout: float = 30.0) -> dict:
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    if not line:
        raise EOFError("ACP process closed stdout")
    msg = json.loads(line)
    summary = json.dumps(msg, ensure_ascii=False)
    if len(summary) > 300:
        summary = summary[:300] + "..."
    print(f"  ← RECV: {summary}")
    return msg


async def _recv_until_response(
    proc: asyncio.subprocess.Process,
    expected_id: int | None = None,
    on_request: dict | None = None,
    timeout: float = 120.0,
) -> dict:
    """Read messages until we get a response matching expected_id.

    If we encounter a request from the agent (like session/request_permission),
    call on_request handler and send the reply.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for response id={expected_id}")
        msg = await _recv(proc, timeout=remaining)

        # It's a response to our request
        if "id" in msg and "method" not in msg:
            if expected_id is None or msg["id"] == expected_id:
                return msg

        # It's a request FROM the agent (reverse RPC)
        if "id" in msg and "method" in msg:
            yield msg  # yield to caller for handling

        # It's a notification (session/update etc.) — just log and continue


async def run_test(grant_mode: str | None) -> None:
    """Run the E2E permission negotiation test."""

    # ── Setup workspace ──────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="perm-test-") as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        # Create a file OUTSIDE the workspace but under tmpdir
        outside_file = Path(tmpdir) / "secret.txt"
        outside_file.write_text("This is a secret file outside the workspace.\n")

        # Also create a file OUTSIDE tmpdir, under home
        home_outside = Path.home() / ".box-agent-perm-test-temp.txt"
        home_outside.write_text("This file is under user home but outside workspace.\n")

        print(f"\n{'='*60}")
        print(f"Permission Negotiation E2E Test")
        print(f"{'='*60}")
        print(f"  Workspace:    {workspace}")
        print(f"  Outside file: {home_outside}")
        print(f"  Grant mode:   {grant_mode or 'DENY'}")
        print(f"{'='*60}\n")

        # ── Spawn ACP server ─────────────────────────────
        env = os.environ.copy()
        # Ensure officev3 policy is active via env override
        # (config.yaml must have officev3 block — we'll rely on existing config)

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "box_agent.acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            # ── Step 1: initialize ───────────────────────
            print("[1] Sending initialize...")
            init_req = _make_request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "test-host", "version": "0.0.1"},
            })
            await _send(proc, init_req)
            init_resp = await _recv(proc, timeout=10)
            assert "result" in init_resp, f"initialize failed: {init_resp}"
            print(f"    Agent: {init_resp['result'].get('agentInfo', {}).get('name')} "
                  f"v{init_resp['result'].get('agentInfo', {}).get('version')}\n")

            # ── Step 2: session/new ──────────────────────
            print("[2] Sending session/new...")
            new_req = _make_request("session/new", {
                "cwd": str(workspace),
                "mcpServers": [],
                "_meta": {
                    "session_mode": None,
                    # No officev3_permissions_override — negotiation handles it
                },
            })
            await _send(proc, new_req)
            new_resp = await _recv(proc, timeout=15)
            assert "result" in new_resp, f"session/new failed: {new_resp}"
            session_id = new_resp["result"]["sessionId"]
            print(f"    Session: {session_id}\n")

            # ── Step 3: session/prompt ───────────────────
            print("[3] Sending prompt (read file outside workspace)...")
            prompt_text = (
                f"Please read the file at {home_outside} and tell me what it contains. "
                f"Use the read_file tool with the exact path."
            )
            prompt_req = _make_request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt_text}],
            })
            await _send(proc, prompt_req)

            # ── Step 4: Read messages, handle permission requests ──
            print("\n[4] Waiting for agent responses...\n")
            permission_request_count = 0

            while True:
                try:
                    msg = await _recv(proc, timeout=120)
                except (TimeoutError, asyncio.TimeoutError):
                    print("\n  ⚠ Timeout waiting for message")
                    break
                except EOFError:
                    print("\n  ⚠ ACP process closed")
                    break

                # Response to our prompt request
                if "id" in msg and "method" not in msg and msg.get("id") == prompt_req["id"]:
                    stop_reason = msg.get("result", {}).get("stopReason", "?")
                    print(f"\n{'='*60}")
                    print(f"  Prompt finished: stopReason={stop_reason}")
                    print(f"  Permission requests received: {permission_request_count}")
                    print(f"{'='*60}")
                    break

                # Reverse RPC: session/request_permission
                if "id" in msg and "method" in msg:
                    method = msg["method"]
                    req_id = msg["id"]

                    if method == "session/request_permission":
                        permission_request_count += 1
                        params = msg.get("params", {})
                        tool_call = params.get("toolCall", {})
                        options = params.get("options", [])

                        print(f"\n  ★ PERMISSION REQUEST (id={req_id}):")
                        print(f"    Tool:    {tool_call.get('toolCallId')}")
                        print(f"    Payload: {json.dumps(tool_call.get('rawInput', {}), ensure_ascii=False)}")
                        print(f"    Options: {[o.get('optionId') for o in options]}")

                        if grant_mode == "prompt":
                            reply = _make_response(req_id, {
                                "outcome": {"outcome": "selected", "optionId": "approve"},
                            })
                            print(f"    → Granting (prompt scope)")
                        elif grant_mode == "session":
                            reply = _make_response(req_id, {
                                "outcome": {"outcome": "selected", "optionId": "approve_session"},
                            })
                            print(f"    → Granting (session scope)")
                        else:
                            reply = _make_response(req_id, {
                                "outcome": {"outcome": "cancelled"},
                            })
                            print(f"    → Denying")

                        await _send(proc, reply)
                    else:
                        print(f"\n  ? Unknown request from agent: {method}")

                # Notification (session/update) — just logged by _recv

        finally:
            # Cleanup
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

            home_outside.unlink(missing_ok=True)

            # Print stderr for debugging
            stderr = await proc.stderr.read()
            if stderr:
                print(f"\n--- Agent stderr (last 2000 chars) ---")
                print(stderr.decode(errors="replace")[-2000:])


def main():
    parser = argparse.ArgumentParser(description="E2E test for permission negotiation")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--grant", choices=["prompt", "session"],
                       help="Grant the permission request (prompt or session scope)")
    group.add_argument("--deny", action="store_true",
                       help="Deny the permission request")
    args = parser.parse_args()

    grant_mode = args.grant if args.grant else None
    asyncio.run(run_test(grant_mode))


if __name__ == "__main__":
    main()
