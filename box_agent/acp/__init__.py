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
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Final
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
from box_agent.tools.setup import (
    SANDBOX_INFO_PROMPT,
    add_workspace_tools,
    await_mcp_tools,
    initialize_base_tools,
    merge_mcp_tools,
    register_mcp_tools,
)
from box_agent.config import Config
from box_agent.core import run_agent_loop
from box_agent.events import (
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LLMOutputEvent,
    MemoryProposalEvent,
    PPTProgressEvent,
    StepEnd,
    StepStart,
    StopReason,
    SubAgentEvent,
    ThinkingEvent,
    ToolCallResult as ToolCallResultEvent,
    ToolCallStart as ToolCallStartEvent,
    WebSearchEvent,
)
from box_agent.llm import LLMClient
from box_agent.acp.action_hints import (
    build_action_hints_prompt,
    is_memory_scarce,
    is_playwright_unavailable,
)
from box_agent.acp.env_context import EnvContext, build_env_context_prompt
from box_agent.memory import MemoryManager
from box_agent.retry import RetryConfig as RetryConfigBase
from box_agent.schema import LLMProvider, Message
from box_agent.tools.permissions import CapabilityPolicy, GrantStore, PermissionEngine
from box_agent.tools.runtime import (
    SkillRuntimeContext,
    build_skill_runtime_context,
    build_skill_runtime_prompt,
)

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


def _artifact_envelope(art: ArtifactEvent, output_dir: str | None) -> dict[str, Any]:
    """Serialize an ArtifactEvent to the wire envelope hosts dispatch on.

    The ``type: "artifact"`` discriminator is stable; downstream consumers
    branch on ``kind`` for category-specific rendering.
    """
    payload: dict[str, Any] = {
        "type": "artifact",
        "kind": art.kind,
        "filename": art.filename,
        "rel_path": art.rel_path,
        "abs_path": art.abs_path,
        "uri": art.uri,
        "mime": art.mime,
        "size": art.size,
        "sha256": art.sha256,
        "produced_at": art.produced_at,
        "tool_call_id": art.tool_call_id,
    }
    if output_dir:
        payload["output_dir"] = output_dir
    return payload


@dataclass
class SessionState:
    agent: Agent
    cancelled: bool = False
    output_dir: str | None = None  # ``{workspace}/output/`` — the canonical artifact root
    session_mode: str | None = None  # e.g. "data_analysis" for /analysis pages
    permission_engine: PermissionEngine | None = None
    grant_store: GrantStore | None = None  # in-band permission grants
    memory_extractor: Any | None = None  # per-session instance to avoid cross-session state leaks
    inject_queue: asyncio.Queue = field(default_factory=asyncio.Queue)  # in-stream message injection
    turn_active: bool = False  # True while _run_turn is executing; guards inject_queue
    auto_classify_pending: bool = False  # True when caller didn't supply session_mode; classify on first prompt
    memory_block: str | None = None  # cached memory recall, re-applied when mode switches
    thinking_enabled: bool = False  # extended thinking toggle from _meta.deep_think
    env_context: "EnvContext | None" = None  # cached env_context, re-applied when mode switches
    skill_runtime_context: "SkillRuntimeContext | None" = None


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
        skill_loader: Any | None = None,
        mcp_task: asyncio.Task | None = None,
    ):
        self._conn = conn
        self._config = config
        self._llm = llm
        self._base_tools = base_tools
        self._system_prompt = system_prompt
        self._sessions: dict[str, SessionState] = {}
        self._memory = memory_manager
        self._hooks = hooks
        self._skill_loader = skill_loader
        self._mcp_task = mcp_task  # background-loaded MCP tools; awaited on first prompt
        self._mcp_loaded = mcp_task is None  # True once MCP has been injected

    async def _ensure_mcp_loaded(self) -> None:
        """Await background MCP loading (first caller blocks, rest are no-ops)."""
        if self._mcp_loaded:
            return
        mcp_tools = await await_mcp_tools(self._mcp_task)
        # Inject into base tool list so future sessions pick them up
        merge_mcp_tools(self._base_tools, mcp_tools)
        # Also inject into any already-created sessions
        for state in self._sessions.values():
            register_mcp_tools(state.agent.tools, mcp_tools)
        self._mcp_loaded = True
        log.info("mcp/ready", count=len(mcp_tools))

    def _skills_meta(self) -> list[dict] | None:
        """Return current skills metadata for ACP _meta payload, reloading if changed."""
        if not self._skill_loader:
            return None
        try:
            self._skill_loader.maybe_reload()
            return self._skill_loader.list_skills_metadata()
        except Exception as exc:
            log.warn("skills/meta_error", message=f"Failed to build skills metadata: {exc}")
            return None

    async def initialize(self, params: InitializeRequest) -> InitializeResponse:  # noqa: ARG002
        log.info("initialize", message="ACP initialize request received")
        kwargs: dict[str, Any] = dict(
            protocolVersion=PROTOCOL_VERSION,
            agentCapabilities=AgentCapabilities(loadSession=False),
            agentInfo=Implementation(name="box-agent", title="Box-Agent", version=__version__),
        )
        skills = self._skills_meta()
        if skills is not None:
            # Pydantic alias: _meta ↔ field_meta
            kwargs["field_meta"] = {"skills": skills}
        resp = InitializeResponse(**kwargs)
        log.info("initialize", message=f"Initialized box-agent v{__version__}, skills={len(skills) if skills else 0}")
        return resp

    async def newSession(self, params: NewSessionRequest) -> NewSessionResponse:
        session_id = f"sess-{len(self._sessions)}-{uuid4().hex[:8]}"
        workspace = Path(params.cwd or self._config.agent.workspace_dir).expanduser()
        if not workspace.is_absolute():
            workspace = workspace.resolve()

        # Extract session_mode from _meta (ACP extension point)
        # Pydantic aliases _meta to field_meta
        session_mode = None
        deep_think = False
        env_context: EnvContext | None = None
        meta = getattr(params, "field_meta", None) or {}
        if isinstance(meta, dict):
            session_mode = meta.get("session_mode")
            deep_think = bool(meta.get("deep_think", False))
            env_context = EnvContext.from_meta(meta.get("env_context"))

        log.info("session/new", session_id=session_id, message=f"Creating session, workspace={workspace}, session_mode={session_mode}, deep_think={deep_think}")

        # Build PermissionEngine via policy composition if officev3 block is configured
        perm_engine = None
        grant_store = None
        effective_policy: CapabilityPolicy | None = None
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

                # Host-supplied filesystem context: workspace root and any
                # extra allowed directories the host wants this session to
                # see. This is *context*, not escalation — escalation still
                # goes through in-band permission/request.
                fs_meta = meta.get("filesystem_policy") if isinstance(meta, dict) else None
                if isinstance(fs_meta, dict):
                    swr = fs_meta.get("session_workspace_root")
                    extra_dirs = fs_meta.get("allowed_directories")
                    fs_scope = fs_meta.get("filesystem_scope")
                    if isinstance(swr, str) and not swr.strip():
                        swr = None
                    if isinstance(extra_dirs, list):
                        extra_dirs = tuple(d for d in extra_dirs if isinstance(d, str) and d.strip())
                    else:
                        extra_dirs = None
                    if not isinstance(fs_scope, str):
                        fs_scope = None
                    base_policy = base_policy.with_filesystem_overrides(
                        session_workspace_root=swr,
                        allowed_directories=extra_dirs,
                        filesystem_scope=fs_scope,
                    )
                    log.info(
                        "session/permissions",
                        session_id=session_id,
                        message=(
                            f"filesystem_policy applied: session_workspace_root={swr!r}, "
                            f"extra_dirs={extra_dirs!r}, scope={fs_scope!r}"
                        ),
                    )

                effective_policy = base_policy

                grant_store = GrantStore()
                perm_engine = PermissionEngine(effective_policy, workspace, grant_store=grant_store)
                log.info("session/permissions", session_id=session_id,
                         message=f"PermissionEngine created: scope={effective_policy.filesystem_scope}, "
                                 f"openclaw={effective_policy.openclaw_import_enabled}, "
                                 f"swr={effective_policy.session_workspace_root!r}, "
                                 f"allowed_dirs={list(effective_policy.allowed_directories)!r}")
            except Exception as exc:
                log.error("permission/init", message=f"Failed to build PermissionEngine: {exc}")
                # Use a restrictive fallback engine (session_workspace scope, no openclaw)
                fallback_policy = CapabilityPolicy(
                    session_workspace_root=str(workspace),
                )
                effective_policy = fallback_policy
                grant_store = GrantStore()
                perm_engine = PermissionEngine(fallback_policy, workspace, grant_store=grant_store)

        skill_runtime_context = build_skill_runtime_context(
            sandbox_mode=True,
            env_context=env_context,
        )

        # Build per-session system prompt with conditional mode injection
        system_prompt = self._build_session_prompt(
            session_mode,
            workspace=workspace,
            policy=effective_policy,
            env_context=env_context,
            skill_runtime_context=skill_runtime_context,
        )

        # Inject memory context
        memory_block: str | None = None
        if self._memory:
            recalled = self._memory.recall()
            if recalled:
                memory_block = recalled
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
            skill_runtime_context=skill_runtime_context,
        )
        agent = Agent(
            llm_client=self._llm,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=self._config.agent.max_steps,
            workspace_dir=str(workspace),
            token_limit=self._config.llm.context_token_limit,
            thinking_enabled=deep_think,
            memory_promotion_enabled=self._config.agent.memory_promotion_proposal_enabled,
            memory_promotion_hit_threshold=self._config.agent.memory_promotion_hit_threshold,
            memory_promotion_cooldown_days=self._config.agent.memory_promotion_cooldown_days,
        )

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
        # Canonical artifact directory (created eagerly so write tools and the
        # sandbox kernel can chdir into it without race conditions).
        from box_agent.core import ensure_output_dir
        output_dir = str(ensure_output_dir(workspace))

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
            agent=agent, output_dir=output_dir, session_mode=session_mode,
            permission_engine=perm_engine, grant_store=grant_store,
            memory_extractor=session_extractor,
            auto_classify_pending=(session_mode is None),
            memory_block=memory_block,
            thinking_enabled=deep_think,
            env_context=env_context,
            skill_runtime_context=skill_runtime_context,
        )

        tool_names = [t.name for t in tools]
        log.info("session/new", session_id=session_id, message=f"Session ready, {len(tools)} tools: {', '.join(tool_names)}")

        kwargs: dict[str, Any] = {"sessionId": session_id}
        response_meta: dict[str, Any] = {}
        skills = self._skills_meta()
        if skills is not None:
            response_meta["skills"] = skills
        if response_meta:
            kwargs["field_meta"] = response_meta
        return NewSessionResponse(**kwargs)

    def _filesystem_access_prompt(self, workspace: Path, policy: CapabilityPolicy | None) -> str:
        """Build per-session filesystem guidance for the model.

        Tools still enforce permissions. This prompt only prevents the model
        from assuming workspace-only access when officev3 has granted extra
        roots such as ~/Documents.
        """
        if policy is None:
            return (
                "## File Access Context\n"
                f"- Current workspace: `{workspace}`\n"
                "- File tools and bash may access paths allowed by the active runtime policy.\n"
                "- If a file is outside the allowed scope, the tool will return a permission error; "
                "try the tool instead of assuming denial."
            )

        allowed_roots = [workspace]
        if policy.session_workspace_root:
            allowed_roots.append(Path(policy.session_workspace_root).expanduser())
        for directory in policy.allowed_directories:
            allowed_roots.append(Path(directory).expanduser())

        seen: set[str] = set()
        root_lines: list[str] = []
        for root in allowed_roots:
            root_s = str(root)
            if root_s not in seen:
                seen.add(root_s)
                root_lines.append(f"- `{root_s}`")

        if policy.filesystem_scope == "user_home":
            scope_line = "- Active filesystem scope: `user_home`; paths under the user home directory are allowed."
        elif policy.filesystem_scope in ("session_workspace", "custom"):
            scope_line = (
                f"- Active filesystem scope: `{policy.filesystem_scope}`; the workspace, "
                "session workspace root, and configured allowed directories are allowed."
            )
        else:
            scope_line = f"- Active filesystem scope: `{policy.filesystem_scope}`; unknown scopes fail closed in tools."

        return (
            "## File Access Context\n"
            f"{scope_line}\n"
            "- Allowed filesystem roots for this session include:\n"
            + "\n".join(root_lines)
            + "\n- Prefer absolute paths when the user names a location such as ~/Documents."
            + "\n- Do not claim you can only access the workspace unless a tool call actually returns a permission denial."
        )

    def _build_action_hints_prompt(self) -> str:
        """Detect onboarding / browser-tools scenarios and build the hint contract."""
        memory_scarce = is_memory_scarce(self._memory.read_core() if self._memory else None)

        try:
            mcp_path = Config.find_config_file(self._config.tools.mcp_config_path)
        except Exception:
            mcp_path = None
        playwright_unavailable = is_playwright_unavailable(
            mcp_path,
            mcp_globally_enabled=self._config.tools.enable_mcp,
        )

        return build_action_hints_prompt(
            memory_scarce=memory_scarce,
            playwright_unavailable=playwright_unavailable,
        )

    def _build_session_prompt(
        self,
        session_mode: str | None,
        workspace: Path | None = None,
        policy: CapabilityPolicy | None = None,
        env_context: EnvContext | None = None,
        skill_runtime_context: SkillRuntimeContext | None = None,
    ) -> str:
        """Build system prompt with conditional mode-specific injection."""
        _MODE_PROMPT_MAP = {
            "data_analysis": "analysis_prompt_path",
            "ppt_plan_chat": "ppt_plan_chat_prompt_path",
            "ppt_outline": "ppt_outline_prompt_path",
            "ppt_editor_standard_html": "ppt_editor_prompt_path",
        }

        base_prompt = self._system_prompt
        if workspace is not None:
            base_prompt = f"{base_prompt.rstrip()}\n\n{self._filesystem_access_prompt(workspace, policy)}"

        env_prompt = build_env_context_prompt(env_context)
        if env_prompt:
            base_prompt = f"{base_prompt.rstrip()}\n\n{env_prompt}"

        runtime_context = skill_runtime_context or build_skill_runtime_context(
            sandbox_mode=True,
            env_context=env_context,
        )
        base_prompt = f"{base_prompt.rstrip()}\n\n{build_skill_runtime_prompt(runtime_context)}"

        hints_prompt = self._build_action_hints_prompt()
        if hints_prompt:
            base_prompt = f"{base_prompt.rstrip()}\n\n{hints_prompt}"

        attr = _MODE_PROMPT_MAP.get(session_mode or "")
        if not attr:
            return base_prompt

        prompt_filename = getattr(self._config.agent, attr, None)
        if not prompt_filename:
            return base_prompt

        mode_path = Config.find_config_file(prompt_filename)
        if mode_path and mode_path.exists():
            mode_prompt = mode_path.read_text(encoding="utf-8").strip()
            return f"{base_prompt.rstrip()}\n\n{mode_prompt}"

        log.warn("session/prompt", message=f"Mode prompt not found: {prompt_filename}")
        return base_prompt

    _PPT_TOOL_MODES: Final[frozenset[str]] = frozenset(
        {"ppt_plan_chat", "ppt_outline", "ppt_editor_standard_html"}
    )

    def _apply_session_mode(self, state: SessionState, mode: str | None) -> None:
        """Retroactively apply a ``session_mode`` to an existing session.

        Used when the caller did not supply ``_meta.session_mode`` and we
        auto-classified the user's first message. Rewrites the agent's system
        message (preserving the workspace-info footer injected by
        ``Agent.__init__``) and registers the mode-specific PPT tool if any.
        Safe to call when ``mode`` resolves to the general agent (``None``) —
        the session already runs as general, so the call is a no-op in that
        case.
        """
        if mode is None or mode == state.session_mode:
            state.session_mode = mode
            return

        # Recompose system prompt: base + mode + memory + preserved workspace info
        new_prompt = self._build_session_prompt(
            mode,
            workspace=state.agent.workspace_dir,
            policy=state.permission_engine.policy if state.permission_engine else None,
            env_context=state.env_context,
            skill_runtime_context=state.skill_runtime_context,
        )
        if state.memory_block:
            new_prompt = f"{new_prompt.rstrip()}\n\n{state.memory_block}"
        workspace_info = (
            f"\n\n## Current Workspace\n"
            f"You are currently working in: `{state.agent.workspace_dir.absolute()}`\n"
            f"All relative paths will be resolved relative to this directory."
        )
        if "Current Workspace" not in new_prompt:
            new_prompt = new_prompt + workspace_info

        state.agent.system_prompt = new_prompt
        if state.agent.messages and state.agent.messages[0].role == "system":
            state.agent.messages[0] = Message(role="system", content=new_prompt)
        else:
            state.agent.messages.insert(0, Message(role="system", content=new_prompt))

        if mode in self._PPT_TOOL_MODES:
            from box_agent.tools.ppt_tools import (
                PPTEditorHTMLTool,
                PPTOutlineTool,
                PPTPlanChatTool,
            )
            ppt_tool_map = {
                "ppt_plan_chat": PPTPlanChatTool,
                "ppt_outline": PPTOutlineTool,
                "ppt_editor_standard_html": PPTEditorHTMLTool,
            }
            ppt_tool = ppt_tool_map[mode]()
            state.agent.tools.setdefault(ppt_tool.name, ppt_tool)

        state.session_mode = mode

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

        # Ensure background-loaded MCP tools are available before running the turn
        await self._ensure_mcp_loaded()

        # Refresh skills so officev3-authored skills are available mid-session
        if self._skill_loader:
            try:
                self._skill_loader.maybe_reload()
            except Exception as exc:
                log.warn("skills/reload_error", session_id=session_id, message=str(exc))

        # Auto-classify session_mode if the caller did not supply one.
        # Runs once per session, before the user message is appended so that the
        # classifier sees only the raw first prompt. Failures fall back to the
        # general agent — never blocks the turn.
        if state.auto_classify_pending and user_text.strip():
            from .intent_classifier import classify_session_mode
            classified = await classify_session_mode(self._llm, user_text)
            self._apply_session_mode(state, classified)
            state.auto_classify_pending = False
            log.info(
                "session/mode_resolved",
                session_id=session_id,
                mode=state.session_mode or "general",
                source="auto",
            )

        state.agent.messages.append(Message(role="user", content=user_text))

        # Drain any stale injections from a previous turn
        while not state.inject_queue.empty():
            stale = state.inject_queue.get_nowait()
            log.warn("session/inject_stale", session_id=session_id, text=stale[:80])

        prompt_start = perf_counter()
        state.turn_active = True
        try:
            stop_reason = await self._run_turn(state, session_id)
        finally:
            state.turn_active = False
        duration_ms = int((perf_counter() - prompt_start) * 1000)

        log.info("session/done", session_id=session_id, stop_reason=stop_reason, duration_ms=duration_ms)
        # Map box-agent stop reasons to ACP-valid StopReason values.
        # ACP only accepts: "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"
        _ACP_STOP_REASON_MAP = {
            "end_turn": "end_turn",
            "cancelled": "cancelled",
            "max_steps": "max_turn_requests",
            "max_tokens": "max_tokens",
            "error": "end_turn",
        }
        acp_stop_reason = _ACP_STOP_REASON_MAP.get(stop_reason, "end_turn")
        return PromptResponse(stopReason=acp_stop_reason)

    async def cancel(self, params: CancelNotification) -> None:
        state = self._sessions.get(params.sessionId)
        if state:
            state.cancelled = True
            log.info("session/cancel", session_id=params.sessionId, message="Cancel requested")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle custom ACP extension methods (called as ``_<method>``)."""
        if method == "inject":
            session_id = params.get("sessionId", "")
            text = params.get("text", "")
            state = self._sessions.get(session_id)
            if not state:
                return {"error": "session_not_found"}
            if not text:
                return {"error": "empty_text"}
            if not state.turn_active:
                return {"error": "no_active_turn"}
            state.inject_queue.put_nowait(text)
            log.info("session/inject", session_id=session_id, text=text[:80])
            return {"ok": True}
        if method == "list_skills":
            skills = self._skills_meta()
            if skills is None:
                return {"skills": []}
            log.info("skills/list", count=len(skills))
            return {"skills": skills}
        if method == "memory_proposal_list":
            return self._memory_proposal_list(params)
        if method == "memory_proposal_apply":
            return self._memory_proposal_apply(params)
        return {"error": f"unknown_method: {method}"}

    # Backward-compat alias: agent-client-protocol < 0.6.x dispatched extension
    # methods via camelCase getattr(agent, "extMethod"). Newer (>= 0.6.x) uses
    # snake_case getattr(agent, "ext_method"). Keep both pointing at the same
    # handler so the agent works across framework versions without forcing
    # every consumer to `uv sync` to the latest.
    extMethod = ext_method

    def _memory_proposal_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return CONTEXT.md entries eligible for promotion to core.

        Request: ``{sessionId, includeCooldown?: bool}``. When
        ``includeCooldown`` is true, cooldown filtering is bypassed
        (mirrors the CLI ``/memory review`` behaviour).
        """
        session_id = params.get("sessionId", "")
        if session_id and session_id not in self._sessions:
            return {"error": "session_not_found"}
        if self._memory is None:
            return {"candidates": []}
        cooldown_days = (
            0
            if bool(params.get("includeCooldown"))
            else self._config.agent.memory_promotion_cooldown_days
        )
        entries = self._memory.list_promotion_candidates(
            hit_threshold=self._config.agent.memory_promotion_hit_threshold,
            cooldown_days=cooldown_days,
        )
        candidates = [
            {
                "id": e.id,
                "content": e.content,
                "hits": e.hits,
                "confidence": e.confidence,
                "created": e.created,
                "last_used": e.last_used,
                "last_proposed": e.last_proposed,
            }
            for e in entries
        ]
        log.info(
            "memory/proposal_list",
            session_id=session_id,
            count=len(candidates),
            include_cooldown=bool(params.get("includeCooldown")),
        )
        return {"candidates": candidates}

    def _memory_proposal_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply user decisions to promotion candidates.

        Request: ``{sessionId, decisions: {id: "pin"|"skip"|"reject"}}``.
        Returns counts plus the full updated ``MEMORY.md`` text so the
        host can refresh its core editor without an extra fetch.
        """
        session_id = params.get("sessionId", "")
        if session_id and session_id not in self._sessions:
            return {"error": "session_not_found"}
        if self._memory is None:
            return {"error": "memory_unavailable"}
        raw = params.get("decisions") or {}
        if not isinstance(raw, dict):
            return {"error": "invalid_decisions"}
        decisions: dict[str, str] = {
            str(entry_id): value
            for entry_id, value in raw.items()
            if isinstance(value, str) and value in ("pin", "skip", "reject")
        }
        counts = self._memory.consume_core_proposal(decisions)
        log.info(
            "memory/proposal_apply",
            session_id=session_id,
            **counts,
        )
        return {
            "pinned": counts["pinned"],
            "rejected": counts["rejected"],
            "skipped": counts["skipped"],
            "core": self._memory.read_core(),
        }

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
            memory_manager=self._memory,
            memory_extractor=state.memory_extractor,
            memory_promotion_enabled=self._config.agent.memory_promotion_proposal_enabled,
            memory_promotion_hit_threshold=self._config.agent.memory_promotion_hit_threshold,
            memory_promotion_cooldown_days=self._config.agent.memory_promotion_cooldown_days,
            inject_queue=state.inject_queue,
            thinking_enabled=agent.thinking_enabled,
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

                    case LLMOutputEvent(
                        step=s,
                        content=content,
                        thinking=thinking,
                        tool_calls=tool_calls,
                        finish_reason=finish_reason,
                        usage=usage,
                        provider_request_id=provider_request_id,
                    ):
                        payload = {
                            "type": "llm_output",
                            "step": s,
                            "content": content,
                            "thinking": thinking,
                            "tool_calls": tool_calls,
                            "finish_reason": finish_reason,
                            "usage": usage,
                            "provider_request_id": provider_request_id,
                        }
                        log.debug(
                            "llm/output",
                            session_id=session_id,
                            step=s,
                            finish_reason=finish_reason,
                            payload=payload,
                        )
                        await self._send(
                            session_id,
                            update_tool_call(f"llm-output-{s}", raw_output=payload),
                        )

                    case ToolCallStartEvent(tool_call_id=tid, tool_name=name, arguments=args):
                        log.info("tool/start", session_id=session_id, tool_call_id=tid, tool_name=name, arguments=args)
                        args_preview = (
                            ", ".join(f"{k}={repr(v)[:50]}" for k, v in list(args.items())[:2])
                            if isinstance(args, dict) else ""
                        )
                        label = f"🔧 {name}({args_preview})" if args_preview else f"🔧 {name}()"
                        await self._send(session_id, start_tool_call(tid, label, kind="execute", raw_input=args))

                    case ToolCallResultEvent(tool_call_id=tid, tool_name=tname, success=ok, content=text, error=err, raw_output=raw_output):
                        if ok:
                            log.info("tool/end", session_id=session_id, tool_call_id=tid, tool_name=tname, result=text)
                        else:
                            log.warn("tool/fail", session_id=session_id, tool_call_id=tid, tool_name=tname, error=err)
                        status = "completed" if ok else "failed"
                        prefix = "[OK]" if ok else "[ERROR]"
                        result_text = f"{prefix} {text if ok else err or 'Tool execution failed'}"
                        output = raw_output if isinstance(raw_output, dict) else result_text
                        await self._send(
                            session_id,
                            update_tool_call(tid, status=status, content=[tool_content(text_block(result_text))], raw_output=output),
                        )

                    case ArtifactEvent() as art:
                        log.info(
                            "artifact",
                            session_id=session_id,
                            tool_call_id=art.tool_call_id,
                            kind=art.kind,
                            rel_path=art.rel_path,
                            size=art.size,
                            sha256=art.sha256,
                        )
                        # ACP SessionUpdate has no native "artifact" variant —
                        # we ride on tool_call_update.rawOutput, with a stable
                        # ``type: "artifact"`` discriminator the host dispatches on.
                        artifact_meta = _artifact_envelope(art, state.output_dir)
                        log.debug("artifact/payload", session_id=session_id, tool_call_id=art.tool_call_id, payload=artifact_meta)
                        try:
                            await self._send(
                                session_id,
                                update_tool_call(art.tool_call_id, raw_output=artifact_meta),
                            )
                        except Exception as exc:
                            log.exception("artifact/send_error", exc, session_id=session_id, tool_call_id=art.tool_call_id, payload=artifact_meta)

                    case WebSearchEvent(tool_call_id=tid, payload=payload):
                        web_search_payload = {**payload, "type": "web_search"}
                        log.debug("web_search/payload", session_id=session_id, tool_call_id=tid, payload=web_search_payload)
                        await self._send(session_id, update_tool_call(tid, raw_output=web_search_payload))

                    case ErrorEvent(message=msg, is_fatal=True):
                        log.error("error", session_id=session_id, message=msg, is_fatal=True)
                        await self._send(session_id, update_agent_message(text_block(f"Error: {msg}")))
                        # Don't return yet — let the loop consume the subsequent DoneEvent
                        # so the async generator is properly exhausted.

                    case InjectedMessageEvent(content=text):
                        log.info("session/injected", session_id=session_id, text=text[:80])
                        await self._send(session_id, update_agent_message(text_block(f"[Injected] {text}")))

                    case StepEnd(step=s, elapsed_seconds=el, total_elapsed_seconds=tot):
                        log.debug("step/end", session_id=session_id, step=s, duration_ms=int(el * 1000), total_ms=int(tot * 1000))

                    case DoneEvent(stop_reason=reason):
                        log.debug("done", session_id=session_id, stop_reason=reason.value)
                        return reason.value

                    case SubAgentEvent(parent_tool_call_id=tid, task_preview=preview, event=inner, sub_agent_id=sub_agent_id):
                        if isinstance(inner, WebSearchEvent):
                            web_search_payload = {**inner.payload, "type": "web_search"}
                            log.debug("sub_agent/web_search", session_id=session_id, tool_call_id=tid, payload=web_search_payload)
                            await self._send(session_id, update_tool_call(tid, raw_output=web_search_payload))
                            continue

                        # Send structured progress so officev3 can render sub-agent activity
                        progress: dict = {
                            "type": "sub_agent_progress",
                            "parent_tool_call_id": tid,
                            "sub_agent_id": sub_agent_id,
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
                            case ArtifactEvent() as art:
                                progress["event"] = "artifact"
                                progress["artifact"] = _artifact_envelope(art, state.output_dir)
                            case ErrorEvent(message=msg):
                                progress["event"] = "error"
                                progress["message"] = msg
                            case LLMOutputEvent(
                                step=s,
                                content=content,
                                thinking=thinking,
                                tool_calls=tool_calls,
                                finish_reason=finish_reason,
                                usage=usage,
                                provider_request_id=provider_request_id,
                            ):
                                progress["event"] = "llm_output"
                                progress["step"] = s
                                progress["content"] = content
                                progress["thinking"] = thinking
                                progress["tool_calls"] = tool_calls
                                progress["finish_reason"] = finish_reason
                                progress["usage"] = usage
                                progress["provider_request_id"] = provider_request_id
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

                    case MemoryProposalEvent():
                        if self._memory is not None:
                            negotiator_mem = _MemoryProposalNegotiator(
                                conn=self._conn,
                                session_id=session_id,
                                memory_manager=self._memory,
                            )
                            try:
                                await negotiator_mem.negotiate(event)
                            except Exception as exc:
                                log.exception("memory/proposal_unhandled", exc, session_id=session_id)

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
        path_hint = permission_request.get("path", "")

        # Dedup: filesystem requests check the directory grant table; other
        # capabilities (memory) use the legacy (scope, requested_scope) key.
        if scope == "filesystem" and path_hint:
            try:
                target = Path(path_hint).expanduser().resolve()
            except (OSError, RuntimeError):
                target = None
            if target is not None and self._store.has_filesystem_dir_grant(target):
                log.info(
                    "permission/grant_hit",
                    scope=scope,
                    path=path_hint,
                    message="Filesystem dir grant hit — skipping RPC",
                )
                return True
        elif self._store.has_grant(scope, requested_scope):
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
            if scope == "filesystem" and path_hint:
                # Record at directory granularity. Use the path itself when it
                # is already a directory; otherwise fall back to its parent.
                # Spec section 4: only open the requested directory, never the
                # entire user_home, on a "allow once" / "always allow" choice.
                grant_dir = self._derive_grant_dir(path_hint)
                if grant_dir is not None:
                    self._store.add_filesystem_dir_grant(grant_dir, grant_scope)
                    log.info(
                        "permission/granted",
                        scope=scope,
                        directory=str(grant_dir),
                        grant_scope=grant_scope,
                    )
                    return True
                log.warn(
                    "permission/grant_path_invalid",
                    scope=scope,
                    path=path_hint,
                    message="Could not derive grant directory from path; rejecting",
                )
                return False
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

    @staticmethod
    def _derive_grant_dir(path: str) -> Path | None:
        """Resolve *path* and return its directory.

        For an existing directory, returns the directory itself. For an
        existing file or a non-existent target, returns the parent. ``None``
        means the path could not be resolved.
        """
        try:
            resolved = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        if resolved.is_dir():
            return resolved
        return resolved.parent


class _MemoryProposalNegotiator:
    """Reverse-RPC bridge for ``MemoryProposalEvent`` over ACP.

    Sends ``_session/memory_proposal`` (ext method) to the host with a
    list of candidates; awaits a per-candidate decision map; applies
    decisions via ``MemoryManager.consume_core_proposal``.

    Hosts that don't implement the method get a ``method_not_found``
    response — we treat that as "skip all" so the turn still ends
    cleanly. ``last_proposed`` was already bumped at emit time, so the
    cooldown carries the user past the unanswered batch.
    """

    _VALID_DECISIONS = {"pin", "skip", "reject"}

    def __init__(
        self,
        conn: AgentSideConnection,
        session_id: str,
        memory_manager: Any,
    ) -> None:
        self._conn = conn
        self._session_id = session_id
        self._mgr = memory_manager

    async def negotiate(self, event: Any) -> None:
        candidates = getattr(event, "candidates", ()) or ()
        if not candidates:
            return

        payload = {
            "sessionId": self._session_id,
            "proposals": [
                {
                    "id": c.entry_id,
                    "content": c.content,
                    "hits": c.hits,
                    "confidence": c.confidence,
                }
                for c in candidates
            ],
        }

        log.info(
            "memory/proposal_request",
            count=len(candidates),
            message="Sending memory promotion proposals to host",
        )

        # Resolve outbound extension dispatcher across framework versions.
        # >= 0.6.x exposes ext_method; older releases exposed extMethod.
        send_ext = getattr(self._conn, "ext_method", None) or getattr(
            self._conn, "extMethod", None
        )
        if send_ext is None:
            log.warn(
                "memory/proposal_error",
                count=len(candidates),
                message="conn exposes neither ext_method nor extMethod; "
                "host does not support session/memory_proposal",
            )
            return

        try:
            response = await asyncio.wait_for(
                send_ext("session/memory_proposal", payload),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            log.warn(
                "memory/proposal_timeout",
                count=len(candidates),
                message="Host did not respond — treating as skip-all",
            )
            return
        except Exception as exc:
            log.warn(
                "memory/proposal_error",
                count=len(candidates),
                message=f"ext_method failed (host may not support session/memory_proposal): {exc}",
            )
            return

        if not isinstance(response, dict):
            return
        raw_decisions = response.get("decisions") or {}
        if not isinstance(raw_decisions, dict):
            return

        valid_ids = {c.entry_id for c in candidates}
        decisions: dict[str, str] = {}
        for entry_id, decision in raw_decisions.items():
            if entry_id not in valid_ids:
                continue
            if not isinstance(decision, str):
                continue
            d = decision.lower()
            if d not in self._VALID_DECISIONS:
                continue
            decisions[entry_id] = d

        if not decisions:
            return

        try:
            counts = self._mgr.consume_core_proposal(decisions)
            log.info(
                "memory/proposal_applied",
                pinned=counts.get("pinned", 0),
                rejected=counts.get("rejected", 0),
                skipped=counts.get("skipped", 0),
            )
        except Exception as exc:
            log.warn(
                "memory/proposal_apply_error",
                message=f"consume_core_proposal failed: {exc}",
            )


async def run_acp_server(config: Config | None = None) -> None:
    """Run Box-Agent as an ACP-compatible stdio server."""
    config = config or Config.load()

    # ── Playwright default cache path ──────────────────────
    # Host (e.g. officev3) can override by exporting PLAYWRIGHT_BROWSERS_PATH
    # before launching box-agent-acp. Otherwise we default to the shared
    # ~/.box-agent/browsers/ directory — same location `box-agent install-browser`
    # populates — so CLI installs are reusable from ACP.
    import os as _os
    _os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        str(Path.home() / ".box-agent" / "browsers"),
    )

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
        rcfg = config.llm.retry
        provider = LLMProvider.ANTHROPIC if config.llm.provider.lower() == "anthropic" else LLMProvider.OPENAI
        llm = LLMClient(
            api_key=config.llm.api_key,
            provider=provider,
            api_base=config.llm.api_base,
            model=config.llm.model,
            retry_config=RetryConfigBase(
                enabled=rcfg.enabled,
                max_retries=rcfg.max_retries,
                initial_delay=rcfg.initial_delay,
                max_delay=rcfg.max_delay,
                exponential_base=rcfg.exponential_base,
            ),
            max_output_tokens=config.llm.max_output_tokens,
            auth_file=config.llm.auth_file,
        )

        # Create memory manager if enabled
        memory_mgr = None
        if config.agent.enable_memory:
            memory_mgr = MemoryManager(
                memory_dir=config.agent.memory_dir,
                dedup_jaccard_threshold=config.agent.memory_dedup_jaccard,
            )

        # One-time OpenClaw import (LLM-filtered into Core)
        if memory_mgr:
            try:
                await memory_mgr.import_openclaw(llm)
            except Exception:
                log.warn("server/start", message="OpenClaw import failed (non-fatal)")

        # Memory maintenance (decay / archive cleanup / dedup / compact).
        # Off the critical path — the compact phase can issue a slow LLM call
        # on large CONTEXT.md, which used to blow the host's init timeout.
        # Same pattern as the background MCP loader: fire-and-forget, errors
        # logged but never block stdio readiness.
        maintainer_task: asyncio.Task | None = None
        if memory_mgr and config.agent.memory_maintainer_enabled:
            from box_agent.memory_maintainer import MemoryMaintainer

            async def _run_maintainer() -> None:
                try:
                    await MemoryMaintainer(memory_mgr, config.agent, llm=llm).run_if_due()
                except Exception:
                    log.warn("server/start", message="Memory maintainer failed (non-fatal)")

            maintainer_task = asyncio.create_task(_run_maintainer(), name="memory-maintainer")

        base_tools, skill_loader, mcp_task = await initialize_base_tools(config, output=_stderr_print, memory_manager=memory_mgr, llm=llm)
        prompt_path = Config.find_config_file(config.agent.system_prompt_path)
        if prompt_path and prompt_path.exists():
            system_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant."

        # Inject SANDBOX_INFO (ACP always enables sandbox)
        system_prompt = system_prompt.replace("{SANDBOX_INFO}", SANDBOX_INFO_PROMPT)

        if skill_loader:
            meta = skill_loader.get_skills_metadata_prompt()
            system_prompt = system_prompt.replace("{SKILLS_METADATA}", meta or "")
        else:
            system_prompt = system_prompt.replace("{SKILLS_METADATA}", "")

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
        AgentSideConnection(lambda conn: BoxACPAgent(conn, config, llm, base_tools, system_prompt, memory_manager=memory_mgr, hooks=_hooks, skill_loader=skill_loader, mcp_task=mcp_task), writer, reader)

        log.info("server/ready", message="ACP server ready, listening on stdio")
        await asyncio.Event().wait()

    except Exception as exc:
        log.exception("server/error", exc, message="ACP server failed to start")
        raise


def main() -> None:
    asyncio.run(run_acp_server())


__all__ = ["BoxACPAgent", "run_acp_server", "main"]
