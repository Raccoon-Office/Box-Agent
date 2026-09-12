"""End-to-end SDK example covering streaming, controls, permissions and result DTOs.

Examples:
    uv run python examples/sdk/run_sdk.py --task "Inspect the workspace"
    uv run python examples/sdk/run_sdk.py --task "Long task" --pause-after 3 --resume-after 5
    uv run python examples/sdk/run_sdk.py --task "Read a file" --auto-approve

For direct providers, set BOX_AGENT_API_KEY. The template key is replaced in a
temporary config copy and is never written back to sdk-config.yaml.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from box_agent import (
    AgentClient,
    ControlCommand,
    PermissionBroker,
    RunRequest,
)
from box_agent.config import Config
from box_agent.events import PermissionRequestEvent
from box_agent.session_context import HostBindings, SessionOptions
from box_agent.agent_session import AgentSession


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("sdk-config.yaml"))
    parser.add_argument("--task", required=True, help="The user message for this run")
    parser.add_argument("--session-id", default="sdk-session")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--pause-after", type=float, default=None)
    parser.add_argument("--resume-after", type=float, default=2.0)
    parser.add_argument("--cancel-after", type=float, default=None)
    parser.add_argument("--inject", default=None, help="Message to inject while the run is active")
    parser.add_argument("--inject-after", type=float, default=2.0)
    parser.add_argument("--auto-approve", action="store_true")
    return parser.parse_args()


def _load_config(path: Path) -> Config:
    """Load a template config while keeping secrets out of the checked-in file."""

    api_key = os.environ.get("BOX_AGENT_API_KEY", "").strip()
    source = path.read_text(encoding="utf-8")
    if "YOUR_API_KEY_HERE" in source:
        if not api_key:
            raise SystemExit(
                "Set BOX_AGENT_API_KEY or replace YOUR_API_KEY_HERE in the SDK config."
            )
        source = source.replace("YOUR_API_KEY_HERE", api_key)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".yaml", dir=path.parent, delete=False,
        ) as temporary:
            temporary.write(source)
            temporary_path = Path(temporary.name)
        try:
            return Config.from_yaml(temporary_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return Config.from_yaml(path)


def _system_prompt() -> str | None:
    import box_agent

    prompt_path = Path(box_agent.__file__).with_name("config") / "system_prompt.md"
    return prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else None


async def _send_scheduled_controls(
    handle: Any,
    args: argparse.Namespace,
) -> None:
    async def pause_and_resume() -> None:
        await asyncio.sleep(max(args.pause_after, 0.0))
        if handle.is_active:
            await handle.send(ControlCommand.pause())
            print(json.dumps({"control": "pause", "state": handle.control_state}))
            await asyncio.sleep(max(args.resume_after, 0.0))
            if handle.is_active:
                await handle.send(ControlCommand.resume())
                print(json.dumps({"control": "resume", "state": handle.control_state}))

    async def inject_message() -> None:
        await asyncio.sleep(max(args.inject_after, 0.0))
        if handle.is_active:
            await handle.send(ControlCommand.inject_message(args.inject))
            print(json.dumps({"control": "inject_message"}, ensure_ascii=False))

    async def cancel_run() -> None:
        await asyncio.sleep(max(args.cancel_after, 0.0))
        if handle.is_active:
            await handle.send(ControlCommand.cancel())
            print(json.dumps({"control": "cancel"}))

    tasks = []
    if args.pause_after is not None:
        tasks.append(asyncio.create_task(pause_and_resume()))
    if args.inject is not None:
        tasks.append(asyncio.create_task(inject_message()))
    if args.cancel_after is not None:
        tasks.append(asyncio.create_task(cancel_run()))
    if tasks:
        await asyncio.gather(*tasks)


async def _permission_response(handle: Any, event: PermissionRequestEvent, auto: bool) -> None:
    if not event.request_id:
        # Legacy permission notifications have no correlation ID. The broker's
        # correlated event arrives immediately after it and is actionable.
        return
    if auto:
        approved = True
    else:
        answer = await asyncio.to_thread(
            input,
            f"Permission {event.scope}/{event.requested_scope} for {event.path or event.command}. "
            "Approve? [y/N] ",
        )
        approved = answer.strip().lower() in {"y", "yes", "approve", "approved"}
    await handle.send(ControlCommand.permission_response(
        event.request_id, approved=approved,
    ))


async def _run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = _load_config(config_path)
    workspace = (args.workspace or config_path.parent / "workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # Config.find_config_file is process-global for compatibility with CLI and
    # ACP. Point MCP loading at the SDK-owned file while retaining all other
    # session assembly behavior.
    os.environ.setdefault("BOX_AGENT_MCP_CONFIG_PATH", str(config_path.with_name("mcp.json")))
    config.agent.workspace_dir = str(workspace)
    session = await AgentSession.open(
        config=config,
        options=SessionOptions(
            workspace_dir=workspace,
            profile="python",
            sandbox_mode=False,
            non_interactive=False,
        ),
        host=HostBindings(system_prompt=_system_prompt()),
        session_id=args.session_id,
    )

    run_id = args.run_id or uuid4().hex
    broker = PermissionBroker(run_id=run_id, on_request=lambda _request: None)
    client = AgentClient(session)
    options = session.build_run_options(
        session_id=args.session_id,
        turn_id=run_id,
        current_turn_text=args.task,
    )
    from dataclasses import replace

    options = replace(options, permission_negotiator=broker)
    request = RunRequest(
        run_id=run_id,
        session_id=args.session_id,
        user_message=args.task,
        metadata={"client": "python-sdk-example"},
    )
    handle = await client.start(request, options=options)
    controls = asyncio.create_task(_send_scheduled_controls(handle, args))
    try:
        async for envelope in handle.events():
            print(json.dumps(envelope.to_dict(), ensure_ascii=False, default=str))
            if isinstance(envelope.payload, PermissionRequestEvent):
                await _permission_response(handle, envelope.payload, args.auto_approve)
        result = await handle.result()
        print(json.dumps({"result": result.to_dict()}, ensure_ascii=False, default=str))
        return 0 if result.status.value == "completed" else 1
    finally:
        controls.cancel()
        await asyncio.gather(controls, return_exceptions=True)
        await session.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_arguments())))
