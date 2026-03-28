"""ACP (Agent Client Protocol) bridge for Box-Agent.

Now consumes the shared execution core (``box_agent.core``) instead of
maintaining its own agent loop.  This gives ACP automatic access to
summarization, logging, and safety — features the old ``_run_turn``
reimplementation was missing.

PoC Behavior Boundaries
-----------------------
**Cancellation**: Cooperative — ``cancel()`` sets a flag that the core
checks at step boundaries (top of step, before tools, after each tool).
There is no preemptive kill; a long-running LLM call or tool execution
will finish before cancellation is observed.

**Safety confirmation**: NOT yet protocol-aware.  BashTool's
``ask_user_confirmation()`` calls ``input()`` which blocks forever in
a non-interactive ACP process.  As a workaround, ACP sessions are
created with ``non_interactive=True``, which causes dangerous commands
to be **rejected outright** instead of prompting.  A future phase will
yield ``ConfirmationRequired`` events so the ACP client can present
its own confirmation UI.

**Sandbox**: Enabled by default for ACP sessions.  Each session gets
a stable ``sandbox_workspace`` path (``{workspace}/sandbox/``) that
the client can use to retrieve generated files.  The sandbox Jupyter
kernel persists across prompts within the same session.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from acp import (
    PROTOCOL_VERSION,
    AgentSideConnection,
    CancelNotification,
    InitializeRequest,
    InitializeResponse,
    NewSessionRequest,
    NewSessionResponse,
    PromptRequest,
    PromptResponse,
    session_notification,
    start_tool_call,
    stdio_streams,
    text_block,
    tool_content,
    update_agent_message,
    update_agent_thought,
    update_tool_call,
)
from pydantic import field_validator
from acp.schema import AgentCapabilities, Implementation, McpCapabilities

from box_agent import __version__
from box_agent.agent import Agent
from box_agent.cli import add_workspace_tools, initialize_base_tools
from box_agent.config import Config
from box_agent.core import run_agent_loop
from box_agent.events import (
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    StepEnd,
    StopReason,
    ThinkingEvent,
    ToolCallResult,
    ToolCallStart,
)
from box_agent.llm import LLMClient
from box_agent.retry import RetryConfig as RetryConfigBase
from box_agent.schema import Message

from .debug_logger import acp_logger as log

# Keep stdlib logger for backward compat with existing log calls
logger = logging.getLogger(__name__)


try:
    class InitializeRequestPatch(InitializeRequest):
        @field_validator("protocolVersion", mode="before")
        @classmethod
        def normalize_protocol_version(cls, value: Any) -> int:
            if isinstance(value, str):
                try:
                    return int(value.split(".")[0])
                except Exception:
                    return 1
            if isinstance(value, (int, float)):
                return int(value)
            return 1

    InitializeRequest = InitializeRequestPatch
    InitializeRequest.model_rebuild(force=True)
except Exception:  # pragma: no cover - defensive
    logger.debug("ACP schema patch skipped")


@dataclass
class SessionState:
    agent: Agent
    cancelled: bool = False
    sandbox_workspace: str | None = None  # stable sandbox workspace path for this session


class BoxACPAgent:
    """Minimal ACP adapter wrapping the existing Agent runtime."""

    def __init__(
        self,
        conn: AgentSideConnection,
        config: Config,
        llm: LLMClient,
        base_tools: list,
        system_prompt: str,
    ):
        self._conn = conn
        self._config = config
        self._llm = llm
        self._base_tools = base_tools
        self._system_prompt = system_prompt
        self._sessions: dict[str, SessionState] = {}

    async def initialize(self, params: InitializeRequest) -> InitializeResponse:  # noqa: ARG002
        log.info("initialize", message="ACP initialize request received")
        resp = InitializeResponse(
            protocolVersion=PROTOCOL_VERSION,
            agentCapabilities=AgentCapabilities(loadSession=False),
            agentInfo=Implementation(name="box-agent", title="Box-Agent", version=__version__),
        )
        log.info("initialize", message=f"Initialized box-agent v{__version__}")
        return resp

    async def newSession(self, params: NewSessionRequest) -> NewSessionResponse:
        session_id = f"sess-{len(self._sessions)}-{uuid4().hex[:8]}"
        workspace = Path(params.cwd or self._config.agent.workspace_dir).expanduser()
        if not workspace.is_absolute():
            workspace = workspace.resolve()

        log.info("session/new", session_id=session_id, message=f"Creating session, workspace={workspace}")

        tools = list(self._base_tools)
        # Enable sandbox mode and restrict to workspace for ACP sessions
        add_workspace_tools(
            tools,
            self._config,
            workspace,
            sandbox_mode=True,
            allow_full_access=self._config.tools.allow_full_access,
            non_interactive=True,  # ACP cannot do interactive terminal prompts
            output=lambda msg: sys.stderr.write(msg + "\n"),
        )
        agent = Agent(llm_client=self._llm, system_prompt=self._system_prompt, tools=tools, max_steps=self._config.agent.max_steps, workspace_dir=str(workspace))
        # Sandbox workspace is a stable subdirectory under the workspace
        sandbox_ws = str(workspace / "sandbox")
        self._sessions[session_id] = SessionState(agent=agent, sandbox_workspace=sandbox_ws)

        tool_names = [t.name for t in tools]
        log.info("session/new", session_id=session_id, message=f"Session ready, {len(tools)} tools: {', '.join(tool_names)}")
        return NewSessionResponse(sessionId=session_id)

    async def prompt(self, params: PromptRequest) -> PromptResponse:
        session_id = params.sessionId
        state = self._sessions.get(session_id)
        if not state:
            # Auto-create session if not found (compatibility with clients that skip newSession)
            log.warn("session/prompt", session_id=session_id, message="Session not found, auto-creating")
            new_session = await self.newSession(NewSessionRequest(cwd=".", mcpServers=[]))
            session_id = new_session.sessionId  # use the NEW session id from here on
            state = self._sessions.get(session_id)
            if not state:
                log.error("session/prompt", session_id=session_id, message="Failed to auto-create session")
                return PromptResponse(stopReason="refusal")

        state.cancelled = False
        user_text = "\n".join(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "") for block in params.prompt)

        log.info("session/prompt", session_id=session_id, message=user_text)

        state.agent.messages.append(Message(role="user", content=user_text))

        prompt_start = perf_counter()
        stop_reason = await self._run_turn(state, session_id)
        duration_ms = int((perf_counter() - prompt_start) * 1000)

        log.info("session/done", session_id=session_id, stop_reason=stop_reason, duration_ms=duration_ms)
        return PromptResponse(stopReason=stop_reason)

    async def cancel(self, params: CancelNotification) -> None:
        state = self._sessions.get(params.sessionId)
        if state:
            state.cancelled = True
            log.info("session/cancel", session_id=params.sessionId, message="Cancel requested")

    async def _run_turn(self, state: SessionState, session_id: str) -> str:
        """Consume the shared execution core and translate events to ACP updates."""
        agent = state.agent

        async for event in run_agent_loop(
            llm=agent.llm,
            messages=agent.messages,
            tools=agent.tools,
            max_steps=agent.max_steps,
            token_limit=agent.token_limit,
            is_cancelled=lambda: state.cancelled,
            logger=None,  # ACP uses its own logging via the connection
            workspace_dir=str(agent.workspace_dir),
        ):
            try:
                match event:
                    case ThinkingEvent(content=text):
                        log.debug("thinking", session_id=session_id, content=text)
                        await self._send(session_id, update_agent_thought(text_block(text)))

                    case ContentEvent(content=text):
                        log.debug("content", session_id=session_id, content=text)
                        await self._send(session_id, update_agent_message(text_block(text)))

                    case ToolCallStart(tool_call_id=tid, tool_name=name, arguments=args):
                        log.info("tool/start", session_id=session_id, tool_call_id=tid, tool_name=name, arguments=args)
                        args_preview = (
                            ", ".join(f"{k}={repr(v)[:50]}" for k, v in list(args.items())[:2])
                            if isinstance(args, dict) else ""
                        )
                        label = f"🔧 {name}({args_preview})" if args_preview else f"🔧 {name}()"
                        await self._send(session_id, start_tool_call(tid, label, kind="execute", raw_input=args))

                    case ToolCallResult(tool_call_id=tid, tool_name=tname, success=ok, content=text, error=err):
                        if ok:
                            log.info("tool/end", session_id=session_id, tool_call_id=tid, tool_name=tname, result=text)
                        else:
                            log.warn("tool/fail", session_id=session_id, tool_call_id=tid, tool_name=tname, error=err)
                        status = "completed" if ok else "failed"
                        prefix = "[OK]" if ok else "[ERROR]"
                        result_text = f"{prefix} {text if ok else err or 'Tool execution failed'}"
                        await self._send(
                            session_id,
                            update_tool_call(tid, status=status, content=[tool_content(text_block(result_text))], raw_output=result_text),
                        )

                    case ArtifactEvent(tool_call_id=tid, artifact_type=atype, filename=fname, path=fpath, mime_type=mime, size_bytes=sz):
                        log.info("artifact", session_id=session_id, tool_call_id=tid, artifact_type=atype, artifact_path=fpath, filename=fname, mime_type=mime, size_bytes=sz)
                        # ACP SessionUpdate is a strict union — no "artifact" variant exists.
                        # Send artifact metadata as a tool_call_update with rawOutput carrying
                        # the structured artifact info, so officev3 can pick it up from there.
                        artifact_meta = {
                            "type": "artifact",
                            "artifact_type": atype,
                            "filename": fname,
                            "path": fpath,
                            "mime_type": mime,
                            "size_bytes": sz,
                            "sandbox_workspace": state.sandbox_workspace,
                        }
                        log.debug("artifact/payload", session_id=session_id, tool_call_id=tid, payload=artifact_meta)
                        try:
                            await self._send(
                                session_id,
                                update_tool_call(tid, raw_output=artifact_meta),
                            )
                        except Exception as exc:
                            log.exception("artifact/send_error", exc, session_id=session_id, tool_call_id=tid, payload=artifact_meta)

                    case ErrorEvent(message=msg, is_fatal=True):
                        log.error("error", session_id=session_id, message=msg, is_fatal=True)
                        await self._send(session_id, update_agent_message(text_block(f"Error: {msg}")))
                        # Don't return yet — let the loop consume the subsequent DoneEvent
                        # so the async generator is properly exhausted.

                    case StepEnd(step=s, elapsed_seconds=el, total_elapsed_seconds=tot):
                        log.debug("step/end", session_id=session_id, step=s, duration_ms=int(el * 1000), total_ms=int(tot * 1000))

                    case DoneEvent(stop_reason=reason):
                        log.debug("done", session_id=session_id, stop_reason=reason.value)
                        return reason.value

                    case _:
                        pass  # StepStart, SummarizationEvent, etc.

            except Exception as exc:
                log.exception("event/error", exc, session_id=session_id, event=type(event).__name__)
                # Don't break the loop — continue processing events

        return "end_turn"

    async def _send(self, session_id: str, update: Any) -> None:
        await self._conn.sessionUpdate(session_notification(session_id, update))


async def run_acp_server(config: Config | None = None) -> None:
    """Run Box-Agent as an ACP-compatible stdio server."""
    config = config or Config.load()

    # Route stdlib logging to stderr only (never stdout)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.root.addHandler(stderr_handler)
    logging.root.setLevel(logging.INFO)

    log.info("server/start", message=f"Box-Agent ACP server starting v{__version__}")

    # Redirect tool-loading status messages to stderr (stdout is ACP-only)
    def _stderr_print(msg: str) -> None:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    try:
        base_tools, skill_loader = await initialize_base_tools(config, output=_stderr_print)
        prompt_path = Config.find_config_file(config.agent.system_prompt_path)
        if prompt_path and prompt_path.exists():
            system_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant."
        if skill_loader:
            meta = skill_loader.get_skills_metadata_prompt()
            if meta:
                system_prompt = f"{system_prompt.rstrip()}\n\n{meta}"
        rcfg = config.llm.retry
        llm = LLMClient(api_key=config.llm.api_key, api_base=config.llm.api_base, model=config.llm.model, retry_config=RetryConfigBase(enabled=rcfg.enabled, max_retries=rcfg.max_retries, initial_delay=rcfg.initial_delay, max_delay=rcfg.max_delay, exponential_base=rcfg.exponential_base))

        log.info("server/start", message=f"LLM: {config.llm.model}, provider: {config.llm.provider}")
        log.info("server/start", message=f"Tools loaded: {len(base_tools)} base tools")

        reader, writer = await stdio_streams()
        AgentSideConnection(lambda conn: BoxACPAgent(conn, config, llm, base_tools, system_prompt), writer, reader)

        log.info("server/ready", message="ACP server ready, listening on stdio")
        await asyncio.Event().wait()

    except Exception as exc:
        log.exception("server/error", exc, message="ACP server failed to start")
        raise


def main() -> None:
    asyncio.run(run_acp_server())


__all__ = ["BoxACPAgent", "run_acp_server", "main"]
