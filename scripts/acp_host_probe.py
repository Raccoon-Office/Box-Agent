#!/usr/bin/env python3
"""Optional CLI for the ACP Host-like probe (real subprocess, no fake server).

Examples:
    uv run python scripts/acp_host_probe.py
    uv run python scripts/acp_host_probe.py --cwd /tmp/ws --prompt "ping"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

# Allow running from a checkout without installing the tests package.
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tests.acp_host.probe import (  # noqa: E402
    AcpHostProbe,
    collect_session_updates,
    default_acp_command,
    message_text_from_updates,
)


async def _run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd) if args.cwd else Path(tempfile.mkdtemp(prefix="acp-host-probe-"))
    cwd.mkdir(parents=True, exist_ok=True)
    probe = AcpHostProbe(
        command=args.command.split() if args.command else default_acp_command(),
        cwd=cwd,
        timeout_s=args.timeout,
    )
    print(f"[acp-host-probe] cwd={cwd}")
    print(f"[acp-host-probe] cmd={probe.command}")
    await probe.start()
    try:
        init = await probe.initialize()
        print("[acp-host-probe] initialize:", json.dumps(init, ensure_ascii=False)[:300])
        session_id = await probe.session_new(cwd=str(cwd))
        print(f"[acp-host-probe] sessionId={session_id}")
        if args.list_skills:
            skills = await probe.ext_request("list_skills", {})
            count = len((skills or {}).get("skills") or []) if isinstance(skills, dict) else "?"
            print(f"[acp-host-probe] skills={count}")
        if args.prompt:
            probe.drain_notifications()
            result = await probe.session_prompt(session_id, args.prompt)
            updates = collect_session_updates(probe.drain_notifications())
            text = message_text_from_updates(updates)
            print("[acp-host-probe] stopReason:", (result or {}).get("stopReason") if isinstance(result, dict) else result)
            print("[acp-host-probe] message preview:", text[:500])
        return 0
    finally:
        await probe.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="ACP Host-like probe CLI")
    parser.add_argument("--cwd", default=None, help="session workspace cwd")
    parser.add_argument("--command", default=None, help="override ACP spawn command")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--prompt", default=None, help="optional session/prompt text")
    parser.add_argument("--list-skills", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
