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
import platform
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
from box_agent.schema import LLMProvider, Message
from box_agent.tools.permissions import CapabilityPolicy, GrantStore, PermissionEngine

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
    permission_engine: PermissionEngine | None = None
    grant_store: GrantStore | None = None  # in-band permission grants
    memory_extractor: Any | None = None  # per-session instance to avoid cross-session state leaks


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
        hooks: list | None = None,
    ):
        self._conn = conn
        self._config = config
        self._llm = llm
        self._base_tools = base_tools
        self._system_prompt = system_prompt
        self._sessions: dict[str, SessionState] = {}
        self._memory = memory_manager
        self._hooks = hooks

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

        # Build PermissionEngine via policy composition if officev3 block is configured
        perm_engine = None
        grant_store = None
        if self._has_officev3_policy():
            try:
                base_policy = CapabilityPolicy.from_config(self._config)

                # officev3_permissions_override is DEPRECATED — kept for parsing only.
                # In-band permission/request negotiation handles escalation now.
                permission_overrides = meta.get("officev3_permissions_override") if isinstance(meta, dict) else None
                if permission_overrides:
                    log.warn(
                        "session/permissions",
                        session_id=session_id,
                        message=(
                            "officev3_permissions_override is deprecated and has no effect; "
                            "use in-band permission/request negotiation instead"
                        ),
                    )
                # Always use the base policy — overrides no longer applied
                effective_policy = base_policy

                grant_store = GrantStore()
                perm_engine = PermissionEngine(effective_policy, workspace, grant_store=grant_store)
                log.info("session/permissions", session_id=session_id,
                         message=f"PermissionEngine created: scope={effective_policy.filesystem_scope}, "
                                 f"openclaw={effective_policy.openclaw_import_enabled}")
            except Exception as exc:
                log.error("permission/init", message=f"Failed to build PermissionEngine: {exc}")
                # Use a restrictive fallback engine (session_workspace scope, no openclaw)
                fallback_policy = CapabilityPolicy(
                    session_workspace_root=str(workspace),
                )
                grant_store = GrantStore()
                perm_engine = PermissionEngine(fallback_policy, workspace, grant_store=grant_store)

        # Build per-session system prompt with conditional mode injection
        system_prompt = self._build_session_prompt(session_mode)

        # Inject memory context
        if self._memory:
            memory_block = self._memory.recall(permission_engine=perm_engine)
            if memory_block:
                system_prompt = f"{system_prompt.rstrip()}\n\n{memory_block}"
                log.info("session/memory", session_id=session_id, message="Memory context injected")

        tools = list(self._base_tools)
        if perm_engine is None:
            log.info("session/permissions", session_id=session_id,
                     message="No officev3 policy — using legacy allow_full_access mode")
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
            permission_engine=perm_engine,
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

        # Per-session MemoryExtractor to avoid cross-session state leaks
        session_extractor = None
        if self._memory and self._config.agent.enable_memory_extraction:
            from box_agent.memory import MemoryExtractor
            session_extractor = MemoryExtractor(
                llm=self._llm,
                memory_manager=self._memory,
                cooldown=self._config.agent.memory_extraction_cooldown,
                step_interval=self._config.agent.memory_extraction_step_interval,
            )

        self._sessions[session_id] = SessionState(
            agent=agent, sandbox_workspace=sandbox_ws, session_mode=session_mode,
            permission_engine=perm_engine, grant_store=grant_store,
            memory_extractor=session_extractor,
        )

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

    def _has_officev3_policy(self) -> bool:
        """Check if officev3 capability policy is configured (not just defaults)."""
        return getattr(self._config.officev3, "_present", False)

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

        # Extract memory and save session summary (best-effort)
        if state.memory_extractor:
            try:
                await state.memory_extractor.maybe_extract(state.agent.messages, "session_end")
            except Exception:
                log.warn("session/memory", session_id=session_id, message="Failed to extract memory")

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

        # Clear prompt-level grants at the start of each prompt
        if state.grant_store:
            state.grant_store.clear_prompt_grants()

        # Build permission negotiator if engine is available
        negotiator = None
        if state.permission_engine and state.grant_store:
            negotiator = _PermissionNegotiator(
                conn=self._conn,
                session_id=session_id,
                grant_store=state.grant_store,
            )

        async for event in run_agent_loop(
            llm=agent.llm,
            messages=agent.messages,
            tools=agent.tools,
            max_steps=agent.max_steps,
            token_limit=agent.token_limit,
            is_cancelled=lambda: state.cancelled,
            logger=None,  # ACP uses its own logging via the connection
            workspace_dir=str(agent.workspace_dir),
            permission_negotiator=negotiator,
            hooks=self._hooks,
            memory_extractor=state.memory_extractor,
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

                    # PermissionRequestEvent: handled inline in core.py via negotiator.
                    # Falls through to case _: pass (no ACP notification sent).

                    case _:
                        pass  # StepStart, SummarizationEvent, PermissionRequestEvent, etc.

            except Exception as exc:
                log.exception("event/error", exc, session_id=session_id, event=type(event).__name__)
                # Don't break the loop — continue processing events

        return "end_turn"

    async def _send(self, session_id: str, update: Any) -> None:
        await self._conn.sessionUpdate(session_notification(session_id, update))


class _PermissionNegotiator:
    """In-band permission negotiation via ACP ``session/request_permission`` reverse RPC.

    Wraps the ACP ``AgentSideConnection.requestPermission()`` call with:
    - Grant-table deduplication (same scope only asked once per prompt)
    - 120-second timeout (timeout treated as denial)
    - Grant-scope mapping: optionId → "prompt" or "session"
    """

    _OPTION_TO_SCOPE: dict[str, str] = {
        "approve": "prompt",
        "approve_session": "session",
    }

    def __init__(
        self,
        conn: AgentSideConnection,
        session_id: str,
        grant_store: GrantStore,
    ) -> None:
        self._conn = conn
        self._session_id = session_id
        self._store = grant_store

    async def negotiate(self, permission_request: dict) -> bool:
        """Negotiate a permission request.  Returns ``True`` if granted."""
        scope = permission_request.get("scope", "")
        requested_scope = permission_request.get("requested_scope", "")

        # Grant-table dedup: already granted in this prompt or session?
        if self._store.has_grant(scope, requested_scope):
            log.info(
                "permission/grant_hit",
                scope=scope,
                requested_scope=requested_scope,
                message="Grant table hit — skipping RPC",
            )
            return True

        # Build ACP RequestPermissionRequest
        from acp.schema import (
            AllowedOutcome,
            PermissionOption,
            RequestPermissionRequest,
            ToolCall,
        )

        path_hint = permission_request.get("path", "")
        reason = permission_request.get("reason", "")
        description = reason + (f": {path_hint}" if path_hint else "")
        tool_call = ToolCall(
            toolCallId=f"perm-{scope}-{requested_scope}",
            rawInput=permission_request,
        )
        options = [
            PermissionOption(optionId="approve", name="仅本次允许", kind="allow_once"),
            PermissionOption(optionId="approve_session", name="始终允许", kind="allow_always"),
            PermissionOption(optionId="reject", name="拒绝", kind="reject_once"),
        ]
        request = RequestPermissionRequest(
            sessionId=self._session_id,
            toolCall=tool_call,
            options=options,
        )

        log.info(
            "permission/request",
            scope=scope,
            requested_scope=requested_scope,
            description=description,
        )

        try:
            response = await asyncio.wait_for(
                self._conn.requestPermission(request),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            log.warn(
                "permission/timeout",
                scope=scope,
                requested_scope=requested_scope,
                message="Timed out waiting for user decision — treating as denial",
            )
            return False
        except Exception as exc:
            log.warn(
                "permission/error",
                scope=scope,
                requested_scope=requested_scope,
                message=f"requestPermission failed: {exc}",
            )
            return False

        if isinstance(response.outcome, AllowedOutcome):
            grant_scope = self._OPTION_TO_SCOPE.get(response.outcome.optionId, "prompt")
            self._store.add_grant(scope, requested_scope, grant_scope)
            log.info(
                "permission/granted",
                scope=scope,
                requested_scope=requested_scope,
                grant_scope=grant_scope,
            )
            return True

        log.info(
            "permission/denied",
            scope=scope,
            requested_scope=requested_scope,
        )
        return False


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
            memory_mgr = MemoryManager(memory_dir=config.agent.memory_dir)

        base_tools, skill_loader = await initialize_base_tools(config, output=_stderr_print, memory_manager=memory_mgr)
        prompt_path = Config.find_config_file(config.agent.system_prompt_path)
        if prompt_path and prompt_path.exists():
            system_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant."

        # Inject SANDBOX_INFO (ACP always enables sandbox)
        sandbox_info = """
## Sandbox Execution Mode (Enabled)

You have access to the `execute_code` tool which runs Python code in an isolated Jupyter kernel.

**When to use execute_code:**
- Data analysis and visualization (pandas, matplotlib, seaborn)
- Processing files (CSV, Excel, JSON, images)
- Document operations (Excel, Word, PDF, PowerPoint)
- Running Python scripts with persistent state
- Complex calculations requiring multiple steps

**Sandbox workspace:** Code runs in an isolated directory. Files saved are stored in the sandbox workspace.

**Best practices:**
- Break complex analysis into smaller code blocks
- Use print() to output intermediate results
- Clean up large data structures when done
- Check for errors after each step

**Available packages:** pandas, numpy, matplotlib, seaborn, scikit-learn, openpyxl, xlrd, python-docx, pypdf, pdfplumber, reportlab, python-pptx, and more via standard library.
"""
        system_prompt = system_prompt.replace("{SANDBOX_INFO}", sandbox_info)

        if skill_loader:
            meta = skill_loader.get_skills_metadata_prompt()
            if meta:
                system_prompt = f"{system_prompt.rstrip()}\n\n{meta}"
        rcfg = config.llm.retry
        provider = LLMProvider.ANTHROPIC if config.llm.provider.lower() == "anthropic" else LLMProvider.OPENAI
        llm = LLMClient(api_key=config.llm.api_key, provider=provider, api_base=config.llm.api_base, model=config.llm.model, retry_config=RetryConfigBase(enabled=rcfg.enabled, max_retries=rcfg.max_retries, initial_delay=rcfg.initial_delay, max_delay=rcfg.max_delay, exponential_base=rcfg.exponential_base))

        log.info("server/start", message=f"LLM: {config.llm.model}, provider: {config.llm.provider}")
        log.info("server/start", message=f"Tools loaded: {len(base_tools)} base tools")

        # Restore real stdout for ACP transport, then re-guard sys.stdout
        sys.stdout = _real_stdout
        reader, writer = await stdio_streams()

        # Windows fix: the ACP dependency's _StdoutTransport.write() resolves
        # sys.stdout.buffer dynamically at each call.  After re-guarding
        # (sys.stdout = sys.stderr below), all protocol responses would be
        # routed to stderr and the client would never receive them.
        # Pin the real stdout buffer on the transport before re-guard.
        if platform.system() == "Windows":
            _stdout_buf = sys.stdout.buffer
            _win_transport = writer.transport

            def _pinned_write(data: bytes) -> None:
                if _win_transport._is_closing:
                    return
                try:
                    _stdout_buf.write(data)
                    _stdout_buf.flush()
                except Exception:
                    logging.exception("Error writing to stdout")

            _win_transport.write = _pinned_write  # type: ignore[method-assign]

        from box_agent.hooks import load_hooks
        _hooks = load_hooks(config.hooks.hooks) if config.hooks.hooks else None

        sys.stdout = sys.stderr
        AgentSideConnection(lambda conn: BoxACPAgent(conn, config, llm, base_tools, system_prompt, memory_manager=memory_mgr, hooks=_hooks), writer, reader)

        log.info("server/ready", message="ACP server ready, listening on stdio")
        await asyncio.Event().wait()

    except Exception as exc:
        log.exception("server/error", exc, message="ACP server failed to start")
        raise


def main() -> None:
    asyncio.run(run_acp_server())


__all__ = ["BoxACPAgent", "run_acp_server", "main"]
