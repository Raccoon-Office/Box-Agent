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
from box_agent.tools.setup import add_workspace_tools, initialize_base_tools
from box_agent.config import Config
from box_agent.core import run_agent_loop
from box_agent.events import (
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    PPTProgressEvent,
    StepEnd,
    StepStart,
    StopReason,
    SubAgentEvent,
    ThinkingEvent,
    ToolCallResult as ToolCallResultEvent,
    ToolCallStart as ToolCallStartEvent,
)
from box_agent.llm import LLMClient
from box_agent.memory import MemoryManager
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
    session_mode: str | None = None  # e.g. "data_analysis" for /analysis pages


class BoxACPAgent:
    """Minimal ACP adapter wrapping the existing Agent runtime."""

    def __init__(
        self,
        conn: AgentSideConnection,
        config: Config,
        llm: LLMClient,
        base_tools: list,
        system_prompt: str,
        memory_manager: MemoryManager | None = None,
    ):
        self._conn = conn
        self._config = config
        self._llm = llm
        self._base_tools = base_tools
        self._system_prompt = system_prompt
        self._sessions: dict[str, SessionState] = {}
        self._memory = memory_manager

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

        # Extract session_mode from _meta (ACP extension point)
        # Pydantic aliases _meta to field_meta
        session_mode = None
        meta = getattr(params, "field_meta", None) or {}
        if isinstance(meta, dict):
            session_mode = meta.get("session_mode")

        log.info("session/new", session_id=session_id, message=f"Creating session, workspace={workspace}, session_mode={session_mode}")

        # Build per-session system prompt with conditional mode injection
        system_prompt = self._build_session_prompt(session_mode)

        # Inject memory context
        if self._memory:
            memory_block = self._memory.recall()
            if memory_block:
                system_prompt = f"{system_prompt.rstrip()}\n\n{memory_block}"
                log.info("session/memory", session_id=session_id, message="Memory context injected")

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
            llm=self._llm,
        )
        agent = Agent(llm_client=self._llm, system_prompt=system_prompt, tools=tools, max_steps=self._config.agent.max_steps, workspace_dir=str(workspace))

        # Conditionally add PPT tools based on session_mode
        if session_mode in ("ppt_plan_chat", "ppt_outline", "ppt_editor_standard_html"):
            from box_agent.tools.ppt_tools import PPTEditorHTMLTool, PPTOutlineTool, PPTPlanChatTool
            ppt_tool_map = {
                "ppt_plan_chat": PPTPlanChatTool,
                "ppt_outline": PPTOutlineTool,
                "ppt_editor_standard_html": PPTEditorHTMLTool,
            }
            ppt_tool = ppt_tool_map[session_mode]()
            agent.tools[ppt_tool.name] = ppt_tool
        # Sandbox workspace is a stable subdirectory under the workspace
        sandbox_ws = str(workspace / "sandbox")
        self._sessions[session_id] = SessionState(agent=agent, sandbox_workspace=sandbox_ws, session_mode=session_mode)

        tool_names = [t.name for t in tools]
        log.info("session/new", session_id=session_id, message=f"Session ready, {len(tools)} tools: {', '.join(tool_names)}")
        return NewSessionResponse(sessionId=session_id)

    def _build_session_prompt(self, session_mode: str | None) -> str:
        """Build system prompt with conditional mode-specific injection.

        Prompt structure:
            base system_prompt
            + [if session_mode matches] mode-specific prompt
            + skills metadata  (already appended in run_acp_server)

        The base prompt (with skills metadata) is stored in self._system_prompt.
        Mode-specific prompts are injected between the base and the skills section.
        """
        # Map session_mode → config attribute holding the prompt filename
        _MODE_PROMPT_MAP = {
            "data_analysis": "analysis_prompt_path",
            "ppt_plan_chat": "ppt_plan_chat_prompt_path",
            "ppt_outline": "ppt_outline_prompt_path",
            "ppt_editor_standard_html": "ppt_editor_prompt_path",
        }

        attr = _MODE_PROMPT_MAP.get(session_mode or "")
        if not attr:
            return self._system_prompt

        prompt_filename = getattr(self._config.agent, attr, None)
        if not prompt_filename:
            return self._system_prompt

        mode_path = Config.find_config_file(prompt_filename)
        if mode_path and mode_path.exists():
            mode_prompt = mode_path.read_text(encoding="utf-8").strip()
            return f"{self._system_prompt.rstrip()}\n\n{mode_prompt}"

        log.warn("session/prompt", message=f"Mode prompt not found: {prompt_filename}")
        return self._system_prompt

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

        # Save session summary in background (best-effort)
        if self._memory:
            try:
                await self._memory.generate_session_summary(
                    llm=self._llm,
                    messages=state.agent.messages,
                    session_id=session_id,
                )
            except Exception:
                log.warn("session/memory", session_id=session_id, message="Failed to save session summary")

        log.info("session/done", session_id=session_id, stop_reason=stop_reason, duration_ms=duration_ms)
        # Map box-agent stop reasons to ACP-valid StopReason values.
        # ACP only accepts: "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"
        _ACP_STOP_REASON_MAP = {
            "end_turn": "end_turn",
            "cancelled": "cancelled",
            "max_steps": "max_turn_requests",
            "error": "end_turn",
        }
        acp_stop_reason = _ACP_STOP_REASON_MAP.get(stop_reason, "end_turn")
        return PromptResponse(stopReason=acp_stop_reason)

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
                    case ThinkingEvent() if event._streaming:
                        # Stream thinking deltas in real-time
                        if not event._header and event.content:
                            await self._send(session_id, update_agent_thought(text_block(event.content)))

                    case ThinkingEvent(content=text):
                        log.debug("thinking", session_id=session_id, content=text)
                        await self._send(session_id, update_agent_thought(text_block(text)))

                    case ContentEvent() if event._streaming:
                        # Stream content deltas in real-time
                        if not event._header and event.content:
                            await self._send(session_id, update_agent_message(text_block(event.content)))

                    case ContentEvent(content=text):
                        log.debug("content", session_id=session_id, content=text)
                        await self._send(session_id, update_agent_message(text_block(text)))

                    case ToolCallStartEvent(tool_call_id=tid, tool_name=name, arguments=args):
                        log.info("tool/start", session_id=session_id, tool_call_id=tid, tool_name=name, arguments=args)
                        args_preview = (
                            ", ".join(f"{k}={repr(v)[:50]}" for k, v in list(args.items())[:2])
                            if isinstance(args, dict) else ""
                        )
                        label = f"🔧 {name}({args_preview})" if args_preview else f"🔧 {name}()"
                        await self._send(session_id, start_tool_call(tid, label, kind="execute", raw_input=args))

                    case ToolCallResultEvent(tool_call_id=tid, tool_name=tname, success=ok, content=text, error=err):
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

                    case SubAgentEvent(parent_tool_call_id=tid, task_preview=preview, event=inner):
                        # Send structured progress so officev3 can render sub-agent activity
                        progress: dict = {
                            "type": "sub_agent_progress",
                            "task_preview": preview,
                        }
                        match inner:
                            case StepStart(step=s, max_steps=mx):
                                progress["event"] = "step_start"
                                progress["step"] = s
                                progress["max_steps"] = mx
                            case ToolCallStartEvent(tool_name=name):
                                progress["event"] = "tool_start"
                                progress["tool_name"] = name
                            case ToolCallResultEvent(tool_name=name, success=ok):
                                progress["event"] = "tool_result"
                                progress["tool_name"] = name
                                progress["success"] = ok
                            case ArtifactEvent(artifact_type=atype, filename=fname, path=fpath, mime_type=mime, size_bytes=sz):
                                progress["event"] = "artifact"
                                progress["artifact_type"] = atype
                                progress["filename"] = fname
                                progress["path"] = fpath
                                progress["mime_type"] = mime
                                progress["size_bytes"] = sz
                                if state.sandbox_workspace:
                                    progress["sandbox_workspace"] = state.sandbox_workspace
                            case ErrorEvent(message=msg):
                                progress["event"] = "error"
                                progress["message"] = msg
                            case _:
                                progress["event"] = type(inner).__name__
                        log.debug("sub_agent/progress", session_id=session_id, tool_call_id=tid, progress=progress)
                        try:
                            await self._send(
                                session_id,
                                update_tool_call(tid, raw_output=progress),
                            )
                        except Exception as exc:
                            log.exception("sub_agent/send_error", exc, session_id=session_id, tool_call_id=tid)

                    case PPTProgressEvent(parent_tool_call_id=tid, payload=payload):
                        log.debug("ppt/progress", session_id=session_id, tool_call_id=tid, payload=payload)
                        try:
                            await self._send(
                                session_id,
                                update_tool_call(tid, raw_output=payload),
                            )
                        except Exception as exc:
                            log.exception("ppt/send_error", exc, session_id=session_id, tool_call_id=tid)

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

    # ── Stdout guard ────────────────────────────────────────
    # ACP protocol owns stdout exclusively.  Redirect sys.stdout to
    # stderr so stray print() calls don't corrupt the ACP stream.
    # Use sys.__stdout__ (the interpreter-original fd 1) because
    # runtime_entry.py may have already set sys.stdout = sys.stderr
    # before we get here, so sys.stdout would be stderr at this point.
    _real_stdout = sys.__stdout__  # always fd 1, even if pre-guarded
    sys.stdout = sys.stderr

    # Route stdlib logging to stderr only (never stdout)
    # Clear any pre-existing handlers first to prevent stdout leaks
    logging.root.handlers.clear()
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
        # Create memory manager if enabled
        memory_mgr = None
        if config.agent.enable_memory:
            memory_mgr = MemoryManager(
                memory_dir=config.agent.memory_dir,
                recall_days=config.agent.memory_recall_days,
            )

        base_tools, skill_loader = await initialize_base_tools(config, output=_stderr_print, memory_manager=memory_mgr)
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

        # Restore real stdout for ACP transport, then re-guard sys.stdout
        sys.stdout = _real_stdout
        reader, writer = await stdio_streams()
        sys.stdout = sys.stderr
        AgentSideConnection(lambda conn: BoxACPAgent(conn, config, llm, base_tools, system_prompt, memory_manager=memory_mgr), writer, reader)

        log.info("server/ready", message="ACP server ready, listening on stdio")
        await asyncio.Event().wait()

    except Exception as exc:
        log.exception("server/error", exc, message="ACP server failed to start")
        raise


def main() -> None:
    asyncio.run(run_acp_server())


__all__ = ["BoxACPAgent", "run_acp_server", "main"]
