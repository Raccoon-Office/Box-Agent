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

**Safety confirmation**: protocol-aware. Dangerous commands return a
canonical permission request with ``scope="safety"``. The shared core
uses the same in-band ``session/request_permission`` reverse RPC as
filesystem and memory escalation, then retries the tool only if the
host explicitly approves.

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
    text_block,
    tool_content,
    update_agent_message,
    update_agent_thought,
    update_tool_call,
)
from pydantic import field_validator
from acp.schema import AgentCapabilities, Implementation, McpCapabilities

from box_agent import __version__
from box_agent.acp.stdio_compat import stdio_streams_largebuf
from box_agent.agent import (
    Agent,
    goal_autopilot_prompt,
    goal_autopilot_progress_signature,
    goal_payload,
    should_continue_goal_autopilot,
)
from box_agent.tools.setup import (
    add_workspace_tools,
    await_mcp_tools,
    build_sandbox_info_prompt,
    initialize_base_tools,
    merge_mcp_tools,
    register_mcp_tools,
)
from box_agent.config import Config
from box_agent.core import (
    run_agent_loop,
    text_is_short_acknowledgement,
    text_requests_plan_start,
)
from box_agent.events import (
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LLMOutputEvent,
    MemoryProposalEvent,
    PlanSnapshotEvent,
    ProgressEvent,
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
from box_agent.llm.token_meter import get_token_meter, reset_token_meter, start_token_meter
from box_agent.loop_guards import CompletionGate, build_auto_completion_gate
from box_agent.acp.action_hints import (
    ActionHintStreamNormalizer,
    build_action_hints_prompt,
    is_memory_scarce,
    is_playwright_unavailable,
    is_playwright_unavailable_from_env_context,
    normalize_action_hint_blocks,
)
from box_agent.acp.env_context import EnvContext, build_env_context_prompt
from box_agent.acp.project_context import build_project_startup_context_prompt
from box_agent.experts import ExpertSessionContext
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

_AUTO_LOADED_SKILLS_HEADING = "## Auto-Loaded Skill Instructions"
_DOCUMENT_SKILL_ARTIFACT_SUFFIXES: dict[str, tuple[str, ...]] = {
    "pptx": (".pptx", ".ppt"),
    "docx": (".docx",),
    "xlsx": (".xlsx", ".xls"),
    "pdf": (".pdf",),
}
_GATE_REQUIRED_DOCUMENT_SKILL_ARTIFACT_SUFFIXES: dict[str, tuple[str, ...]] = {
    "pptx": _DOCUMENT_SKILL_ARTIFACT_SUFFIXES["pptx"],
    "docx": _DOCUMENT_SKILL_ARTIFACT_SUFFIXES["docx"],
    "xlsx": _DOCUMENT_SKILL_ARTIFACT_SUFFIXES["xlsx"],
}


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


def _artifact_envelope(
    art: ArtifactEvent,
    output_dir: str | None,
    session_id: str | None = None,
) -> dict[str, Any]:
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
    if session_id:
        payload["session_id"] = session_id
        payload["sessionId"] = session_id
    return payload


def _inject_item_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("content") or "")
    return str(item or "")


def _inject_item_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    item_id = item.get("id")
    return item_id if isinstance(item_id, str) else None


class _ActionHintNormalizingLLM:
    """Normalize action_hint protocol drift before core/history see content."""

    def __init__(self, wrapped: Any):
        self._wrapped = wrapped

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    async def generate_stream(self, *args: Any, **kwargs: Any):
        normalizer = ActionHintStreamNormalizer()
        text_template = None
        async for event in self._wrapped.generate_stream(*args, **kwargs):
            if getattr(event, "type", None) != "text":
                for text in normalizer.finish():
                    if text:
                        yield _text_stream_event_like(event, text)
                yield event
                continue

            text_template = event
            for text in normalizer.push(event.delta or ""):
                if text:
                    yield event.model_copy(update={"delta": text})

        if text_template is not None:
            for text in normalizer.finish():
                if text:
                    yield text_template.model_copy(update={"delta": text})

    async def generate(self, *args: Any, **kwargs: Any):
        response = await self._wrapped.generate(*args, **kwargs)
        content = normalize_action_hint_blocks(response.content)
        if content == response.content:
            return response
        return response.model_copy(update={"content": content})


def _text_stream_event_like(event: Any, text: str) -> Any:
    return event.model_copy(
        update={
            "type": "text",
            "delta": text,
            "finish_reason": None,
            "usage": None,
            "tool_calls": None,
            "provider_request_id": None,
            "truncated_tool_calls": None,
        }
    )


def _injected_marker(text: str, injection_id: str | None = None) -> str:
    if injection_id:
        return f"[Injected:{injection_id}] {text}"
    return f"[Injected] {text}"


def _remove_inject_queue_item(queue: asyncio.Queue, injection_id: str) -> bool:
    kept: list[Any] = []
    removed = False
    while not queue.empty():
        item = queue.get_nowait()
        if _inject_item_id(item) == injection_id:
            removed = True
            continue
        kept.append(item)
    for item in kept:
        queue.put_nowait(item)
    return removed


def _meta_bool(meta: Any, *keys: str) -> bool:
    if not isinstance(meta, dict):
        return False
    return any(bool(meta.get(key, False)) for key in keys)


def _plan_approval_from_meta(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None
    raw = meta.get("planApproval") or meta.get("plan_approval")
    if isinstance(raw, dict):
        return dict(raw)
    decision = meta.get("planApprovalDecision") or meta.get("plan_approval_decision")
    if isinstance(decision, str) and decision.strip():
        request_id = meta.get("planApprovalRequestId") or meta.get("plan_approval_request_id")
        payload: dict[str, Any] = {"decision": decision.strip()}
        if isinstance(request_id, str) and request_id.strip():
            payload["request_id"] = request_id.strip()
        return payload
    return None


def _plan_approval_is_approved(plan_approval: dict[str, Any] | None) -> bool:
    if not isinstance(plan_approval, dict):
        return False
    decision = str(plan_approval.get("decision") or "").strip().lower()
    return decision in {
        "approve",
        "approved",
        "accept",
        "accepted",
        "confirm",
        "confirmed",
        "execute",
        "proceed",
        "yes",
    }


def _looks_like_plan_approval_text(text: str) -> bool:
    if text_is_short_acknowledgement(text):
        return True
    compact = "".join(ch for ch in text.strip().lower() if ch not in " \t\r\n,，.。!！?？;；:：")
    if not compact or len(compact) > 40:
        return False
    if compact in {
        "同意",
        "同意执行",
        "确认",
        "确认执行",
        "继续",
        "继续执行",
        "执行",
        "开始执行",
        "可以执行",
        "可以继续",
        "按计划执行",
        "按这个计划执行",
        "就这样执行",
        "没问题",
        "没问题继续",
    }:
        return True
    english = " ".join(text.strip().lower().split())
    return english in {
        "yes",
        "ok",
        "approve",
        "approved",
        "confirm",
        "confirmed",
        "continue",
        "proceed",
        "go ahead",
        "execute",
        "run it",
    }


def _plan_approval_from_pending_text(
    pending: dict[str, Any] | None,
    text: str,
) -> dict[str, Any] | None:
    if not isinstance(pending, dict) or not _looks_like_plan_approval_text(text):
        return None
    payload: dict[str, Any] = {
        "decision": "approved",
        "source": "text",
    }
    for key in ("request_id", "plan_id"):
        value = pending.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = value.strip()
    return payload


_USER_QUESTION_MARKERS = (
    "用户问题：",
    "用户问题:",
    "当前用户问题：",
    "当前用户问题:",
    "User question:",
    "Current user question:",
)
_USER_ROLE_LABELS = {"用户:", "用户：", "user:", "User:"}
_ASSISTANT_ROLE_LABELS = {"助手:", "助手：", "assistant:", "Assistant:"}


def _strip_history_text_prefix(text: str) -> str:
    stripped = text.strip()
    lowered = stripped.lower()
    for prefix in ("text:", "content:"):
        if lowered.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def _latest_user_request_for_plan_detection(prompt_text: str) -> str:
    """Extract the newest real user request from a host-wrapped prompt.

    officev3 may restore a new ACP session by sending a large prompt that starts
    with recent chat history. That history can contain old ``plan:`` snapshots,
    so plan-start detection must not scan the whole wrapper.
    """
    text = (prompt_text or "").strip()
    if not text:
        return ""

    marker_index = -1
    marker_text = ""
    for marker in _USER_QUESTION_MARKERS:
        index = text.rfind(marker)
        if index > marker_index:
            marker_index = index
            marker_text = marker
    if marker_index >= 0:
        return text[marker_index + len(marker_text):].strip()

    user_blocks: list[str] = []
    current_role: str | None = None
    current_lines: list[str] = []

    def flush_user_block() -> None:
        if current_role != "user":
            return
        block = _strip_history_text_prefix("\n".join(current_lines))
        if block:
            user_blocks.append(block)

    for line in text.splitlines():
        label = line.strip()
        if label in _USER_ROLE_LABELS:
            flush_user_block()
            current_role = "user"
            current_lines = []
            continue
        if label in _ASSISTANT_ROLE_LABELS:
            flush_user_block()
            current_role = "assistant"
            current_lines = []
            continue
        if current_role == "user":
            current_lines.append(line)

    flush_user_block()
    if user_blocks:
        return user_blocks[-1]
    return text


def _update_pending_plan_approval_from_raw(
    state: "SessionState",
    raw_output: Any,
) -> None:
    if not isinstance(raw_output, dict) or raw_output.get("type") != "plan_snapshot":
        return
    approval = raw_output.get("approval")
    if not isinstance(approval, dict) or not approval.get("required"):
        return
    approval_state = str(approval.get("state") or "").strip().lower()
    if approval_state == "pending":
        pending = dict(approval)
        plan = raw_output.get("plan")
        if isinstance(plan, dict):
            for source_key, target_key in (("id", "plan_id"), ("title", "title")):
                value = plan.get(source_key)
                if isinstance(value, str) and value.strip() and target_key not in pending:
                    pending[target_key] = value.strip()
        state.pending_plan_approval = pending
    elif approval_state in {"approved", "cancelled", "canceled", "rejected", "none"}:
        state.pending_plan_approval = None


def _normalize_artifact_mode(meta: Any) -> str:
    if isinstance(meta, dict):
        value = meta.get("artifact_mode") or meta.get("artifactMode")
        if isinstance(value, str) and value.strip().lower() == "project":
            return "project"
    return "output"


def _artifact_root_from_meta(meta: Any, workspace: Path, artifact_mode: str) -> Path | None:
    if artifact_mode == "project" or not isinstance(meta, dict):
        return None
    layout = meta.get("workspace_layout") or meta.get("workspaceLayout")
    if not isinstance(layout, dict):
        return None
    raw = layout.get("artifact_root_dir") or layout.get("artifactRootDir")
    if not isinstance(raw, str) or not raw.strip():
        return None
    root = Path(raw.strip()).expanduser()
    if not root.is_absolute():
        root = workspace / root
    return root.resolve()


def _goal_payload(agent: Agent) -> dict[str, Any] | None:
    return goal_payload(agent.goal)


def _goal_request_from_meta(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None

    raw_goal = meta.get("goal")
    if isinstance(raw_goal, str):
        objective = raw_goal.strip()
        return {"action": "set", "objective": objective} if objective else None
    if isinstance(raw_goal, dict):
        request = dict(raw_goal)
        if "action" not in request and isinstance(request.get("objective"), str):
            request["action"] = "set"
        return request

    raw_objective = meta.get("goal_objective") or meta.get("goalObjective")
    if isinstance(raw_objective, str) and raw_objective.strip():
        return {"action": "set", "objective": raw_objective.strip()}
    return None


def _tool_result_raw_output(
    raw_output: Any,
    result_text: str,
    policy_decision: dict[str, Any] | None,
    *,
    session_id: str | None = None,
    output_dir: str | None = None,
) -> Any:
    if isinstance(raw_output, dict):
        payload = dict(raw_output)
        if payload.get("type") == "artifact":
            if output_dir:
                payload.setdefault("output_dir", output_dir)
            if session_id:
                payload.setdefault("session_id", session_id)
                payload.setdefault("sessionId", session_id)
        if policy_decision is not None:
            payload["policy_decision"] = policy_decision
        return payload
    if policy_decision is None:
        return result_text
    return {
        "type": "tool_result",
        "text": result_text,
        "policy_decision": policy_decision,
    }


@dataclass
class SessionState:
    agent: Agent
    cancelled: bool = False
    output_dir: str | None = None  # ``{workspace}/output/`` — the canonical artifact root
    artifact_mode: str = "output"
    session_mode: str | None = None  # e.g. "data_analysis" for /analysis pages
    permission_engine: PermissionEngine | None = None
    grant_store: GrantStore | None = None  # in-band permission grants
    memory_extractor: Any | None = None  # per-session instance to avoid cross-session state leaks
    inject_queue: asyncio.Queue = field(default_factory=asyncio.Queue)  # in-stream message injection
    turn_active: bool = False  # True while _run_turn is executing; guards inject_queue
    seen_injection_ids: set[str] = field(default_factory=set)  # per-turn dedup of inject IDs (idempotent retries)
    memory_block: str | None = None  # cached memory recall, re-applied when mode switches
    thinking_enabled: bool = False  # extended thinking toggle from _meta.deep_think
    env_context: "EnvContext | None" = None  # cached env_context, re-applied when mode switches
    skill_runtime_context: "SkillRuntimeContext | None" = None
    skill_selector: Any | None = None  # SkillSelector — filters skill metadata per turn
    expert_context: ExpertSessionContext | None = None
    upstream_session_id: str = ""  # caller-owned session id from _meta.session_id; forwarded as X-RACCOON-Session-ID
    force_plan_start: bool = False  # host-controlled deterministic plan skeleton toggle
    require_plan_approval: bool = False  # host requires approval after plan_write before execution
    pending_plan_approval: dict[str, Any] | None = None
    preloaded_skill_names: list[str] = field(default_factory=list)


class BoxACPAgent:
    """Minimal ACP adapter wrapping the existing Agent runtime."""

    # Soft deadline for the first prompt to wait on background MCP loading.
    # Beyond this we proceed without MCP tools and inject them once ready, so a
    # single slow/broken MCP server cannot hang the user's first message.
    _MCP_PROMPT_WAIT_SECONDS: float = 5.0
    # Session updates are local ACP notifications. If the host stops draining
    # them, one stuck update must not freeze the agent loop forever.
    _SESSION_UPDATE_TIMEOUT_SECONDS: float = 15.0

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
        *,
        lite_llm: LLMClient | None = None,
    ):
        self._conn = conn
        self._config = config
        self._llm = llm
        self._lite_llm = lite_llm or llm
        self._base_tools = base_tools
        self._system_prompt = system_prompt
        self._sessions: dict[str, SessionState] = {}
        self._memory = memory_manager
        self._hooks = hooks
        self._skill_loader = skill_loader
        self._mcp_task = mcp_task  # background-loaded MCP tools; awaited on first prompt
        self._mcp_loaded = mcp_task is None  # True once MCP has been injected

    def _set_agent_system_prompt(self, agent: Agent, system_prompt: str) -> None:
        """Update all live holders of the current system prompt."""
        agent.system_prompt = system_prompt
        if agent.messages and agent.messages[0].role == "system":
            agent.messages[0] = Message(role="system", content=system_prompt)
        for tool in agent.tools.values():
            if hasattr(tool, "set_parent_system_prompt"):
                tool.set_parent_system_prompt(system_prompt)

    def _strip_auto_loaded_skills(self, system_prompt: str) -> str:
        marker = f"\n\n{_AUTO_LOADED_SKILLS_HEADING}\n"
        if marker in system_prompt:
            return system_prompt.split(marker, 1)[0].rstrip()
        if system_prompt.startswith(f"{_AUTO_LOADED_SKILLS_HEADING}\n"):
            return ""
        return system_prompt

    def _document_preload_skill_names(
        self,
        matched_skill_names: tuple[str, ...],
        completion_gate: CompletionGate | None,
    ) -> list[str]:
        if completion_gate is None:
            return []
        patterns = tuple(completion_gate.required_changed_artifact_globs)
        preload: list[str] = []
        for skill_name, suffixes in _GATE_REQUIRED_DOCUMENT_SKILL_ARTIFACT_SUFFIXES.items():
            if any(suffix in pattern for pattern in patterns for suffix in suffixes):
                preload.append(skill_name)
        for skill_name in matched_skill_names:
            suffixes = _DOCUMENT_SKILL_ARTIFACT_SUFFIXES.get(skill_name)
            if (
                skill_name not in preload
                and suffixes
                and any(suffix in pattern for pattern in patterns for suffix in suffixes)
            ):
                preload.append(skill_name)
        return preload

    def _apply_auto_loaded_skills(
        self,
        state: SessionState,
        session_id: str,
        skill_names: list[str],
    ) -> None:
        if not self._skill_loader:
            return
        include_disabled = state.expert_context is not None
        requested_names = list(state.preloaded_skill_names)
        for skill_name in skill_names:
            if skill_name not in requested_names:
                requested_names.append(skill_name)
        expanded_names: list[str] = []
        for skill_name in requested_names:
            if skill_name not in expanded_names:
                expanded_names.append(skill_name)
            skill = self._skill_loader.get_skill(
                skill_name,
                include_disabled=include_disabled,
            )
            if skill is None:
                continue
            for required_skill_name in skill.required_skills or []:
                if required_skill_name not in expanded_names:
                    expanded_names.append(required_skill_name)
        requested_names = expanded_names
        if not requested_names:
            return

        blocks: list[str] = []
        loaded_names: list[str] = []
        for skill_name in requested_names:
            skill = self._skill_loader.get_skill(
                skill_name,
                include_disabled=include_disabled,
            )
            if skill is None:
                log.warn("skills/preload_missing", session_id=session_id, skill=skill_name)
                continue
            loaded_names.append(skill_name)
            blocks.append(skill.to_prompt())
        state.preloaded_skill_names = loaded_names
        if not blocks:
            return

        base_prompt = self._strip_auto_loaded_skills(state.agent.system_prompt).rstrip()
        preloaded_prompt = (
            f"{base_prompt}\n\n{_AUTO_LOADED_SKILLS_HEADING}\n"
            "The following matched or required document skills are preloaded because this turn "
            "requires an editable deliverable. Follow their full instructions before "
            "planning, delegating, or authoring the artifact.\n\n"
            + "\n\n".join(blocks)
        )
        if state.agent.system_prompt == preloaded_prompt:
            return
        self._set_agent_system_prompt(state.agent, preloaded_prompt)
        log.info(
            "skills/preloaded",
            session_id=session_id,
            skills=",".join(state.preloaded_skill_names),
            prompt_chars=len(preloaded_prompt),
        )

    async def _ensure_mcp_loaded(self) -> None:
        """Await background MCP loading with a soft deadline.

        First caller waits up to ``_MCP_PROMPT_WAIT_SECONDS``; on timeout we
        let the prompt proceed without MCP tools and inject them later via
        ``_finalize_mcp_load``. One slow/broken MCP server can no longer
        block the user's first message.
        """
        if self._mcp_loaded:
            return
        if self._mcp_task is None:
            self._mcp_loaded = True
            return
        try:
            mcp_tools = await asyncio.wait_for(
                asyncio.shield(self._mcp_task),
                timeout=self._MCP_PROMPT_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warn(
                "mcp/slow_load",
                message=(
                    f"MCP load exceeded {self._MCP_PROMPT_WAIT_SECONDS}s; "
                    "proceeding without MCP tools, will inject on completion"
                ),
            )
            asyncio.create_task(self._finalize_mcp_load(), name="mcp-finalize")
            return
        merge_mcp_tools(self._base_tools, mcp_tools)
        for state in self._sessions.values():
            register_mcp_tools(state.agent.tools, mcp_tools)
        self._mcp_loaded = True
        log.info("mcp/ready", count=len(mcp_tools))

    async def _finalize_mcp_load(self) -> None:
        """Background drain of the MCP task after the prompt has already started."""
        if self._mcp_loaded or self._mcp_task is None:
            return
        mcp_tools = await await_mcp_tools(self._mcp_task)
        if self._mcp_loaded:
            return
        merge_mcp_tools(self._base_tools, mcp_tools)
        for state in self._sessions.values():
            register_mcp_tools(state.agent.tools, mcp_tools)
        self._mcp_loaded = True
        log.info("mcp/ready", count=len(mcp_tools), source="deferred")

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
        expert_context: ExpertSessionContext | None = None
        upstream_session_id = ""
        force_plan_start = False
        require_plan_approval = False
        artifact_mode = "output"
        initial_goal_request: dict[str, Any] | None = None
        # Lightweight one-shot utility session (e.g. host-side title/tag
        # generation). When set, the session carries no tools, skips memory
        # recall injection, and skips auto memory-extraction — it is a pure
        # text transform, not a real user conversation.
        utility = False
        meta = getattr(params, "field_meta", None) or {}
        if isinstance(meta, dict):
            session_mode = meta.get("session_mode")
            deep_think = bool(meta.get("deep_think", False))
            utility = bool(meta.get("utility", False))
            force_plan_start = _meta_bool(meta, "force_plan_start", "forcePlanStart")
            require_plan_approval = _meta_bool(
                meta,
                "require_plan_approval",
                "requirePlanApproval",
            )
            artifact_mode = _normalize_artifact_mode(meta)
            initial_goal_request = _goal_request_from_meta(meta)
            env_context = EnvContext.from_meta(meta.get("env_context"))
            expert_context = ExpertSessionContext.from_meta(meta)
            # Caller-owned session id (e.g. officev3 chat session id). Forwarded
            # verbatim as X-RACCOON-Session-ID so the gateway groups LLM calls
            # under that Langfuse session. Distinct from the ACP `session_id`
            # above (``sess-N-xxxx``), which is our own per-connection handle.
            raw_upstream = meta.get("session_id")
            if isinstance(raw_upstream, str):
                upstream_session_id = raw_upstream.strip()

        # Canonical artifact directory is only part of output mode. Existing
        # project workspaces are edited in place and must not get an implicit
        # output/ directory. Hosts may supply a per-session artifact root so
        # concurrent desktop tasks never share a visible output workspace.
        output_dir: str | None = None
        artifact_root_dir = _artifact_root_from_meta(meta, workspace, artifact_mode)
        if artifact_mode != "project":
            from box_agent.core import ensure_output_dir

            output_path = artifact_root_dir or ensure_output_dir(workspace)
            output_path.mkdir(parents=True, exist_ok=True)
            output_dir = str(output_path)

        log.info(
            "session/new",
            session_id=session_id,
            message=(
                f"Creating session, workspace={workspace}, session_mode={session_mode}, "
                f"artifact_mode={artifact_mode}, deep_think={deep_think}, "
                f"force_plan_start={force_plan_start}, "
                f"require_plan_approval={require_plan_approval}, "
                f"artifact_root={output_dir}, "
                f"expert={expert_context.to_metadata() if expert_context else None}"
            ),
        )

        # Build PermissionEngine via policy composition if officev3 block is configured
        perm_engine = None
        grant_store = GrantStore()
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
            expert_context=expert_context,
            artifact_mode=artifact_mode,
        )

        # Inject memory context (skipped for lightweight utility sessions)
        memory_block: str | None = None
        if self._memory and not utility:
            recalled = self._memory.recall()
            if recalled:
                memory_block = recalled
                system_prompt = f"{system_prompt.rstrip()}\n\n{memory_block}"
                log.info("session/memory", session_id=session_id, message="Memory context injected")

        if utility:
            # Pure text transform: no base tools, no workspace/sandbox tools.
            tools: list = []
            log.info("session/new", session_id=session_id,
                     message="Utility session: tools disabled (no memory recall/extraction)")
        else:
            tools = list(self._base_tools)
            if expert_context and self._skill_loader:
                from box_agent.tools.skill_tool import GetSkillTool

                tools = [
                    GetSkillTool(self._skill_loader, include_disabled=True)
                    if getattr(tool, "name", "") == "get_skill"
                    else tool
                    for tool in tools
                ]
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
                use_output_dir=artifact_mode != "project",
                artifact_root_dir=output_dir,
                env_context=env_context,
            )
        agent = Agent(
            llm_client=self._llm,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=self._config.agent.max_steps,
            workspace_dir=str(workspace),
            token_limit=self._config.llm.context_token_limit,
            thinking_enabled=deep_think,
            max_parallel_tools=self._config.agent.max_parallel_tools,
            memory_promotion_enabled=self._config.agent.memory_promotion_proposal_enabled,
            memory_promotion_hit_threshold=self._config.agent.memory_promotion_hit_threshold,
            memory_promotion_cooldown_days=self._config.agent.memory_promotion_cooldown_days,
            truncation_continuation_enabled=self._config.agent.retry_on_suspected_truncation,
            max_truncation_continuations=self._config.agent.max_truncation_continuations,
        )

        if initial_goal_request is not None:
            goal_result = self._apply_goal_action(agent, initial_goal_request)
            if "error" in goal_result:
                log.warn(
                    "session/goal_init_error",
                    session_id=session_id,
                    message=str(goal_result["error"]),
                )
            else:
                goal_payload = goal_result.get("goal") or {}
                log.info(
                    "session/goal_init",
                    session_id=session_id,
                    status=goal_payload.get("status"),
                )

        # Per-session MemoryExtractor to avoid cross-session state leaks
        session_extractor = None
        if self._memory and self._config.agent.enable_memory_extraction and not utility:
            from box_agent.memory import MemoryExtractor
            session_extractor = MemoryExtractor(
                llm=self._llm,
                memory_manager=self._memory,
                session_id=upstream_session_id,
                cooldown=self._config.agent.memory_extraction_cooldown,
                step_interval=self._config.agent.memory_extraction_step_interval,
            )

        self._sessions[session_id] = SessionState(
            agent=agent, output_dir=output_dir, session_mode=session_mode,
            artifact_mode=artifact_mode,
            permission_engine=perm_engine, grant_store=grant_store,
            memory_extractor=session_extractor,
            memory_block=memory_block,
            thinking_enabled=deep_think,
            env_context=env_context,
            skill_runtime_context=skill_runtime_context,
            expert_context=expert_context,
            upstream_session_id=upstream_session_id,
            force_plan_start=force_plan_start,
            require_plan_approval=require_plan_approval,
        )

        # Skill selector: per-turn keyword-based filter on the skill catalog.
        # Bound after Agent.__init__ has appended the workspace footer so the
        # captured prefix/suffix include all surrounding system-prompt content.
        if self._skill_loader:
            from box_agent.tools.skill_loader import SkillSelector
            selector = SkillSelector(
                self._skill_loader,
                include_disabled=expert_context is not None,
            )
            selector.bind(agent.messages[0].content)
            if expert_context:
                expert_skill_prompt = selector.update(expert_context.skill_query())
                if expert_skill_prompt is not None:
                    self._set_agent_system_prompt(agent, expert_skill_prompt)
            self._sessions[session_id].skill_selector = selector

        tool_names = [t.name for t in tools]
        log.info("session/new", session_id=session_id, message=f"Session ready, {len(tools)} tools: {', '.join(tool_names)}")

        kwargs: dict[str, Any] = {"sessionId": session_id}
        response_meta: dict[str, Any] = {}
        skills = self._skills_meta()
        if skills is not None:
            response_meta["skills"] = skills
        if expert_context is not None:
            response_meta["expert_context"] = expert_context.to_metadata()
        if artifact_mode == "project":
            response_meta["artifact_mode"] = artifact_mode
        if agent.goal is not None:
            response_meta["goal"] = _goal_payload(agent)
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

    def _build_action_hints_prompt(self, env_context: EnvContext | None = None) -> str:
        """Detect onboarding / browser-tools scenarios and build the hint contract."""
        memory_scarce = is_memory_scarce(self._memory.read_core() if self._memory else None)

        try:
            mcp_path = Config.find_config_file(self._config.tools.mcp_config_path)
        except Exception:
            mcp_path = None
        playwright_unavailable = is_playwright_unavailable(
            mcp_path,
            mcp_globally_enabled=self._config.tools.enable_mcp,
        ) or is_playwright_unavailable_from_env_context(env_context)

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
        expert_context: ExpertSessionContext | None = None,
        artifact_mode: str = "output",
    ) -> str:
        """Build system prompt with conditional mode-specific injection."""
        _MODE_PROMPT_MAP = {
            "data_analysis": "analysis_prompt_path",
            "code_agent": "code_prompt_path",
        }

        use_output_dir = artifact_mode != "project"
        base_prompt = self._system_prompt.replace(
            "{SANDBOX_INFO}",
            build_sandbox_info_prompt(use_output_dir=use_output_dir),
        )
        if artifact_mode == "project":
            project_mode_prompt = (
                "## Project Workspace Mode\n"
                "- This session is editing an existing code/project workspace.\n"
                "- Do not create or use an `output/` folder unless the user explicitly asks for one.\n"
                "- Treat file edits, generated source files, tests, and build results in the project tree as the deliverable."
            )
            base_prompt = f"{base_prompt.rstrip()}\n\n{project_mode_prompt}"
        if workspace is not None:
            base_prompt = f"{base_prompt.rstrip()}\n\n{self._filesystem_access_prompt(workspace, policy)}"

        if session_mode == "code_agent" and workspace is not None:
            base_prompt = f"{base_prompt.rstrip()}\n\n{build_project_startup_context_prompt(workspace)}"

        env_prompt = build_env_context_prompt(env_context)
        if env_prompt:
            base_prompt = f"{base_prompt.rstrip()}\n\n{env_prompt}"

        runtime_context = skill_runtime_context or build_skill_runtime_context(
            sandbox_mode=True,
            env_context=env_context,
        )
        base_prompt = f"{base_prompt.rstrip()}\n\n{build_skill_runtime_prompt(runtime_context)}"

        hints_prompt = self._build_action_hints_prompt(env_context)
        if hints_prompt:
            base_prompt = f"{base_prompt.rstrip()}\n\n{hints_prompt}"

        attr = _MODE_PROMPT_MAP.get(session_mode or "")
        if attr:
            prompt_filename = getattr(self._config.agent, attr, None)
            if prompt_filename:
                mode_path = Config.find_config_file(prompt_filename)
                if mode_path and mode_path.exists():
                    mode_prompt = mode_path.read_text(encoding="utf-8").strip()
                    base_prompt = f"{base_prompt.rstrip()}\n\n{mode_prompt}"
                else:
                    log.warn("session/prompt", message=f"Mode prompt not found: {prompt_filename}")

        if expert_context:
            expert_prompt = expert_context.render_prompt()
            if expert_prompt:
                base_prompt = f"{base_prompt.rstrip()}\n\n{expert_prompt}"
        return base_prompt

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
        plan_detection_text = _latest_user_request_for_plan_detection(user_text)
        prompt_meta = getattr(params, "field_meta", None) or {}
        plan_approval = _plan_approval_from_meta(prompt_meta)
        if plan_approval is None:
            plan_approval = _plan_approval_from_pending_text(
                state.pending_plan_approval,
                plan_detection_text,
            )
        plan_approval_approved = _plan_approval_is_approved(plan_approval)
        if plan_approval_approved:
            state.pending_plan_approval = None
        auto_approve_plan = _meta_bool(
            prompt_meta,
            "auto_approve_plan",
            "autoApprovePlan",
            "skip_plan_approval",
            "skipPlanApproval",
        )
        if auto_approve_plan and state.pending_plan_approval is not None and not plan_approval_approved:
            state.pending_plan_approval = None
        session_force_plan_start = state.force_plan_start
        if session_force_plan_start:
            state.force_plan_start = False
        prompt_requests_plan_start = text_requests_plan_start(plan_detection_text)
        prompt_force_plan_hint = _meta_bool(
            prompt_meta,
            "force_plan_start",
            "forcePlanStart",
        )
        host_plan_hint = session_force_plan_start or prompt_force_plan_hint
        force_plan_start = (
            False
            if plan_approval_approved
            else prompt_requests_plan_start
        )
        require_plan_approval = (
            (force_plan_start and not auto_approve_plan)
            or (
                state.pending_plan_approval is not None
                and not plan_approval_approved
                and not auto_approve_plan
            )
        )
        prompt_goal_request = _goal_request_from_meta(prompt_meta)
        if prompt_goal_request is not None:
            goal_result = self._apply_goal_action(state.agent, prompt_goal_request)
            if "error" in goal_result:
                log.warn(
                    "session/goal_prompt_error",
                    session_id=session_id,
                    message=str(goal_result["error"]),
                )
            else:
                log.info(
                    "session/goal_prompt",
                    session_id=session_id,
                    status=(goal_result.get("goal") or {}).get("status"),
                )

        log.info(
            "session/prompt",
            session_id=session_id,
            message=user_text,
            plan_detection_text=plan_detection_text[:500],
            host_plan_hint=host_plan_hint,
            force_plan_start=force_plan_start,
            require_plan_approval=require_plan_approval,
            plan_approval_approved=plan_approval_approved,
            auto_approve_plan=auto_approve_plan,
        )

        # Ensure background-loaded MCP tools are available before running the turn
        await self._ensure_mcp_loaded()

        # Refresh skills so officev3-authored skills are available mid-session
        if self._skill_loader:
            try:
                self._skill_loader.maybe_reload()
            except Exception as exc:
                log.warn("skills/reload_error", session_id=session_id, message=str(exc))

        # Per-turn skill metadata filter.
        if state.skill_selector is not None:
            try:
                from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL
                current_system = state.agent.messages[0].content
                if SKILL_SLOT_SENTINEL in current_system:
                    state.skill_selector.bind(current_system)
                new_prompt = state.skill_selector.update(user_text)
                if new_prompt is not None:
                    self._set_agent_system_prompt(state.agent, new_prompt)
                    log.info(
                        "skills/filtered",
                        session_id=session_id,
                        matched=",".join(state.skill_selector.matched_skill_names),
                        query_chars=len(state.skill_selector.cumulative_query),
                        prompt_chars=len(new_prompt),
                    )
            except Exception as exc:
                log.warn("skills/filter_error", session_id=session_id, message=str(exc))

            try:
                from box_agent.tools.mcp_loader import ensure_lazy_mcp_loaded
                new_tools = await ensure_lazy_mcp_loaded(state.skill_selector.cumulative_query)
                if new_tools:
                    merge_mcp_tools(self._base_tools, new_tools)
                    for other in self._sessions.values():
                        register_mcp_tools(other.agent.tools, new_tools)
                    log.info(
                        "mcp/lazy_loaded",
                        session_id=session_id,
                        count=len(new_tools),
                        tools=",".join(t.name for t in new_tools),
                    )
            except Exception as exc:
                log.warn("mcp/lazy_load_error", session_id=session_id, message=str(exc))

        completion_gate = (
            None
            if state.artifact_mode == "project"
            else build_auto_completion_gate(user_text, state.agent.workspace_dir)
        )
        if completion_gate is not None:
            log.info(
                "completion_gate/enabled",
                session_id=session_id,
                patterns=",".join(completion_gate.required_changed_artifact_globs),
            )

        if state.skill_selector is not None:
            preload_names = self._document_preload_skill_names(
                state.skill_selector.matched_skill_names,
                completion_gate,
            )
            if preload_names:
                self._apply_auto_loaded_skills(state, session_id, preload_names)
            elif state.preloaded_skill_names:
                self._apply_auto_loaded_skills(state, session_id, [])

        state.agent.add_user_message(user_text)

        # Drain any stale injections from a previous turn
        while not state.inject_queue.empty():
            stale = state.inject_queue.get_nowait()
            log.warn("session/inject_stale", session_id=session_id, text=_inject_item_text(stale)[:80])
        # Reset per-turn inject dedup — IDs are only meaningful within a turn.
        state.seen_injection_ids.clear()

        prompt_start = perf_counter()
        state.turn_active = True
        meter_token = start_token_meter()
        auto_continuations = 0
        auto_budget_exhausted = False
        auto_no_progress_turns = 0
        auto_no_progress_exhausted = False
        auto_enabled = (
            self._config.agent.goal_autopilot_enabled
            and self._config.agent.goal_autopilot_max_turns > 0
        )
        try:
            stop_reason = await self._run_turn(
                state,
                session_id,
                force_plan_start=force_plan_start,
                require_plan_approval=require_plan_approval,
                plan_approval=plan_approval,
                auto_approve_plan=auto_approve_plan,
                completion_gate=completion_gate,
                plan_start_text=plan_detection_text,
            )
            while (
                auto_enabled
                and state.pending_plan_approval is None
                and should_continue_goal_autopilot(state.agent, stop_reason)
            ):
                elapsed = perf_counter() - prompt_start
                if (
                    auto_continuations >= self._config.agent.goal_autopilot_max_turns
                    or elapsed >= self._config.agent.goal_autopilot_max_seconds
                ):
                    auto_budget_exhausted = True
                    break
                if state.cancelled or state.agent.goal is None:
                    break
                auto_continuations += 1
                continuation = goal_autopilot_prompt(
                    state.agent.goal,
                    auto_continuations,
                    self._config.agent.goal_autopilot_max_turns,
                )
                log.info(
                    "goal_autopilot/continue",
                    session_id=session_id,
                    continuation=auto_continuations,
                    max_continuations=self._config.agent.goal_autopilot_max_turns,
                )
                state.agent.add_user_message(continuation)
                before_signature = goal_autopilot_progress_signature(state.agent.goal)
                stop_reason = await self._run_turn(
                    state,
                    session_id,
                    auto_approve_plan=auto_approve_plan,
                    completion_gate=completion_gate,
                    plan_start_text=plan_detection_text,
                )
                after_signature = goal_autopilot_progress_signature(state.agent.goal)
                if should_continue_goal_autopilot(state.agent, stop_reason):
                    if after_signature == before_signature:
                        auto_no_progress_turns += 1
                    else:
                        auto_no_progress_turns = 0
                    if (
                        self._config.agent.goal_autopilot_no_progress_turns > 0
                        and auto_no_progress_turns >= self._config.agent.goal_autopilot_no_progress_turns
                    ):
                        auto_no_progress_exhausted = True
                        break
        finally:
            state.turn_active = False
            turn_meter = get_token_meter()
            reset_token_meter(meter_token)
        turn_total_tokens = turn_meter.total_tokens if turn_meter else 0
        duration_ms = int((perf_counter() - prompt_start) * 1000)

        log.info(
            "session/done",
            session_id=session_id,
            stop_reason=stop_reason,
            duration_ms=duration_ms,
            total_tokens=turn_total_tokens,
            goal_autopilot_continuations=auto_continuations,
            goal_autopilot_budget_exhausted=auto_budget_exhausted,
            goal_autopilot_no_progress_exhausted=auto_no_progress_exhausted,
            goal_autopilot_no_progress_turns=auto_no_progress_turns,
        )
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
        if (
            (auto_budget_exhausted or auto_no_progress_exhausted)
            and state.agent.goal is not None
            and state.agent.goal.status == "active"
        ):
            acp_stop_reason = "max_turn_requests"
        response_meta: dict[str, Any] = {"usage": {"totalTokens": turn_total_tokens}}
        if state.agent.goal is not None or auto_continuations > 0:
            response_meta["goalAutopilot"] = {
                "enabled": auto_enabled,
                "continuations": auto_continuations,
                "budgetExhausted": auto_budget_exhausted,
                "noProgressExhausted": auto_no_progress_exhausted,
                "noProgressTurns": auto_no_progress_turns,
                "lastStopReason": stop_reason,
            }
        # Per-turn token total (multi-step loop + summarization + in-turn
        # memory extraction) for host-side telemetry. Best-effort: fire-and-
        # forget memory extractions that finish after this point are not
        # reflected. See box_agent.llm.token_meter.
        return PromptResponse(
            stopReason=acp_stop_reason,
            field_meta=response_meta,
        )

    async def cancel(self, params: CancelNotification) -> None:
        state = self._sessions.get(params.sessionId)
        if state:
            state.cancelled = True
            log.info("session/cancel", session_id=params.sessionId, message="Cancel requested")

    def _apply_goal_action(self, agent: Agent, params: dict[str, Any]) -> dict[str, Any]:
        action = str(params.get("action") or "get").strip().lower()
        if action == "status":
            action = "get"
        if action == "create":
            action = "set"
        evidence = params.get("evidence")
        progress = params.get("progress")
        blocked_reason = params.get("blocked_reason") or params.get("blockedReason")
        completed_by = params.get("completed_by") or params.get("completedBy")

        if action == "get":
            return {"ok": True, "goal": _goal_payload(agent)}

        if action == "set":
            objective = params.get("objective")
            if not isinstance(objective, str) or not objective.strip():
                return {"error": "empty_objective"}
            try:
                agent.set_goal(
                    objective,
                    evidence=evidence,
                    progress=progress,
                    blocked_reason=blocked_reason,
                    completed_by=completed_by,
                )
            except ValueError:
                return {"error": "empty_objective"}

            raw_status = params.get("status")
            status = raw_status.strip().lower() if isinstance(raw_status, str) else "active"
            if status == "paused":
                agent.pause_goal()
            elif status == "complete":
                agent.complete_goal(evidence=evidence, progress=progress, completed_by=completed_by)
            elif status == "blocked":
                reason = blocked_reason if isinstance(blocked_reason, str) else ""
                if not reason.strip():
                    return {"error": "empty_blocked_reason"}
                agent.block_goal(reason, evidence=evidence, progress=progress)
            elif status not in ("", "active"):
                return {"error": f"invalid_status: {status}"}
            return {"ok": True, "goal": _goal_payload(agent)}

        if action == "pause":
            if agent.pause_goal() is None:
                return {"error": "goal_not_found"}
            return {"ok": True, "goal": _goal_payload(agent)}

        if action == "resume":
            if agent.resume_goal() is None:
                return {"error": "goal_not_found"}
            return {"ok": True, "goal": _goal_payload(agent)}

        if action == "complete":
            if agent.complete_goal(evidence=evidence, progress=progress, completed_by=completed_by) is None:
                return {"error": "goal_not_found"}
            return {"ok": True, "goal": _goal_payload(agent)}

        if action == "progress":
            if agent.update_goal_progress(progress, evidence=evidence) is None:
                return {"error": "goal_not_found"}
            return {"ok": True, "goal": _goal_payload(agent)}

        if action == "block":
            reason = blocked_reason if isinstance(blocked_reason, str) else ""
            if not reason.strip():
                return {"error": "empty_blocked_reason"}
            try:
                goal = agent.block_goal(reason, evidence=evidence, progress=progress)
            except ValueError:
                return {"error": "empty_blocked_reason"}
            if goal is None:
                return {"error": "goal_not_found"}
            return {"ok": True, "goal": _goal_payload(agent)}

        if action == "clear":
            agent.clear_goal()
            return {"ok": True, "goal": None}

        return {"error": f"unknown_action: {action}"}

    async def extMethod(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle custom ACP extension methods (called as ``_<method>``)."""
        if method == "inject":
            session_id = params.get("sessionId", "")
            text = params.get("text", "")
            raw_injection_id = params.get("injectionId")
            injection_id = (
                raw_injection_id
                if isinstance(raw_injection_id, str) and raw_injection_id
                else str(uuid4())
            )
            state = self._sessions.get(session_id)
            if not state:
                return {"error": "session_not_found"}
            if not text:
                return {"error": "empty_text"}
            if not state.turn_active:
                return {"error": "no_active_turn"}
            # Idempotency: a host retrying after a lost/timed-out response with the
            # same injectionId must not enqueue (or re-run) the instruction twice.
            # Covers both still-pending and already-consumed items within the turn.
            if injection_id in state.seen_injection_ids:
                log.info(
                    "session/inject_dedup",
                    session_id=session_id,
                    injection_id=injection_id,
                )
                return {"ok": True, "injectionId": injection_id, "deduplicated": True}
            state.seen_injection_ids.add(injection_id)
            state.inject_queue.put_nowait({"id": injection_id, "content": text})
            log.info(
                "session/inject",
                session_id=session_id,
                injection_id=injection_id,
                text=text[:80],
            )
            return {"ok": True, "injectionId": injection_id}
        if method == "cancel_inject":
            session_id = params.get("sessionId", "")
            injection_id = params.get("injectionId", "")
            state = self._sessions.get(session_id)
            if not state:
                return {"error": "session_not_found"}
            if not injection_id:
                return {"error": "empty_injection_id"}
            removed = _remove_inject_queue_item(state.inject_queue, injection_id)
            # Allow the host to re-inject the same id after an explicit cancel.
            state.seen_injection_ids.discard(injection_id)
            log.info(
                "session/inject_cancel",
                session_id=session_id,
                injection_id=injection_id,
                removed=removed,
            )
            return {"ok": removed}
        if method == "list_skills":
            skills = self._skills_meta()
            if skills is None:
                return {"skills": []}
            log.info("skills/list", count=len(skills))
            return {"skills": skills}
        if method == "goal":
            session_id = params.get("sessionId", "")
            state = self._sessions.get(session_id)
            if not state:
                return {"error": "session_not_found"}
            result = self._apply_goal_action(state.agent, params)
            log.info(
                "session/goal",
                session_id=session_id,
                action=str(params.get("action") or "get"),
                status=(result.get("goal") or {}).get("status") if isinstance(result.get("goal"), dict) else None,
                error=result.get("error"),
            )
            return result
        if method == "memory_proposal_list":
            return await self._memory_proposal_list(params)
        if method == "memory_proposal_apply":
            return self._memory_proposal_apply(params)
        if method == "llm/prompt":
            return await self._llm_prompt(params)
        return {"error": f"unknown_method: {method}"}

    ext_method = extMethod

    async def _llm_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run a single tool-free completion (titles/summaries/rewrites).

        Bypasses ``newSession``: no MCP wait, no skills metadata, no tools, no
        memory recall/extraction, no conversation history. Errors are returned
        as structured ``{"error": {code, message}}`` so callers can fall back
        without parsing free-form text.
        """
        from box_agent.llm.lightweight import (
            LightweightContentFiltered,
            LightweightInvalidArgs,
            LightweightPromptError,
            LightweightTimeout,
            run_lightweight_prompt,
        )

        prompt = params.get("prompt", "")
        system_prompt = params.get("systemPrompt") or None
        timeout_ms = params.get("timeoutMs")
        meta = params.get("_meta") or {}
        purpose = meta.get("purpose") or params.get("purpose") or ""
        raw_session_id = meta.get("session_id")
        session_id = raw_session_id.strip() if isinstance(raw_session_id, str) else ""
        workspace_label = params.get("workspaceLabel") or ""

        if not isinstance(prompt, str) or not prompt.strip():
            return {"error": {"code": "invalid_args", "message": "prompt must be a non-empty string"}}
        if system_prompt is not None and not isinstance(system_prompt, str):
            return {"error": {"code": "invalid_args", "message": "systemPrompt must be a string"}}

        timeout: float = 30.0
        if timeout_ms is not None:
            try:
                timeout = max(0.001, float(timeout_ms) / 1000.0)
            except (TypeError, ValueError):
                return {"error": {"code": "invalid_args", "message": "timeoutMs must be a number"}}

        provider = getattr(self._lite_llm, "provider", None)
        model = getattr(self._lite_llm, "model", "")
        try:
            result = await run_lightweight_prompt(
                self._lite_llm,
                prompt,
                system_prompt=system_prompt,
                session_id=session_id,
                timeout=timeout,
            )
        except LightweightInvalidArgs as exc:
            return {"error": {"code": exc.code, "message": str(exc)}}
        except LightweightContentFiltered as exc:
            # Model refusal, not a failure: log at info and hand the host a
            # stable `content_filter` code so it can fall back to a neutral
            # default (e.g. a generic title) instead of showing this message.
            log.info(
                "llm/prompt_content_filter",
                purpose=purpose,
                workspace=workspace_label,
                provider=str(provider),
                model=model,
            )
            return {"error": {"code": exc.code, "message": str(exc)}}
        except LightweightTimeout as exc:
            log.warn(
                "llm/prompt_timeout",
                purpose=purpose,
                workspace=workspace_label,
                timeout_ms=int(timeout * 1000),
                input_chars=len(prompt),
                provider=str(provider),
                model=model,
            )
            return {"error": {"code": exc.code, "message": str(exc)}}
        except LightweightPromptError as exc:
            log.warn(
                "llm/prompt_error",
                purpose=purpose,
                workspace=workspace_label,
                code=exc.code,
                message=str(exc),
                provider=str(provider),
                model=model,
            )
            return {"error": {"code": exc.code, "message": str(exc)}}

        log.info(
            "llm/prompt_ok",
            purpose=purpose,
            workspace=workspace_label,
            duration_ms=result.duration_ms,
            input_chars=len(prompt),
            output_chars=len(result.text),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            finish_reason=result.finish_reason,
            provider=str(provider),
            model=model,
        )
        return {
            "text": result.text,
            "finishReason": result.finish_reason,
            "usage": {
                "inputTokens": result.input_tokens,
                "outputTokens": result.output_tokens,
            },
            "durationMs": result.duration_ms,
        }

    async def _memory_proposal_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return CONTEXT.md entries eligible for promotion to core.

        Request: ``{sessionId, includeCooldown?: bool, includePlan?: bool}``.
        When ``includeCooldown`` is true, cooldown filtering is bypassed
        (mirrors the CLI ``/memory review`` behaviour). When ``includePlan``
        is true and there is at least one eligible candidate, the response
        also carries a ``plan`` field shaped like the reverse-RPC push
        payload — letting the host's memory review UI render the plan card
        synchronously without waiting for an auto-push. The plan call is
        best-effort: any planner failure (LLM error, bad JSON, oversized
        shrink) is logged and yields a plan-less response.
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

        plan_payload: dict[str, Any] | None = None
        include_plan = bool(params.get("includePlan"))
        if include_plan and entries:
            wanted = {e.id for e in entries}
            try:
                full_entries = [
                    e for e in self._memory._read_context_entries() if e.id in wanted
                ]
            except Exception as exc:
                log.warn(
                    "memory/proposal_list_plan_skipped",
                    session_id=session_id,
                    reason=f"read_context_entries failed: {exc}",
                )
                full_entries = []
            if full_entries:
                try:
                    plan = await self._memory.plan_promotion(full_entries, self._llm)
                except Exception as exc:
                    log.warn(
                        "memory/proposal_list_plan_skipped",
                        session_id=session_id,
                        reason=f"plan_promotion raised: {exc}",
                    )
                    plan = None
                if plan is not None:
                    plan_payload = {
                        "currentCore": plan.current_core,
                        "newCore": plan.new_core,
                        "consumedEntryIds": list(plan.consumed_entry_ids),
                        "rationale": plan.rationale,
                    }
                else:
                    log.info(
                        "memory/proposal_list_plan_skipped",
                        session_id=session_id,
                        reason="plan_promotion returned None",
                    )

        log.info(
            "memory/proposal_list",
            session_id=session_id,
            count=len(candidates),
            include_cooldown=bool(params.get("includeCooldown")),
            include_plan=include_plan,
            has_plan=plan_payload is not None,
        )
        response: dict[str, Any] = {"candidates": candidates}
        if plan_payload is not None:
            response["plan"] = plan_payload
        return response

    def _memory_proposal_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply user decisions to promotion candidates.

        Two schemas are accepted:

        **Legacy (per-candidate)** —
        ``{sessionId, decisions: {id: "pin"|"skip"|"reject"}}``.
        Returns ``{pinned, rejected, skipped, core}``.

        **Plan-mode (delayed decision)** —
        ``{sessionId, plan: {currentCore, newCore, consumedEntryIds,
        rationale}, decision: "apply"|"reject"|"skip"}``.
        Returns ``{applied|rejected|skipped: int, consumed?: int, core}``.
        ``sessionId`` may be an empty string for orphan applies (after
        the originating session has closed) — the server-level memory
        manager is shared across sessions.
        """
        session_id = params.get("sessionId", "")
        if session_id and session_id not in self._sessions:
            return {"error": "session_not_found"}
        if self._memory is None:
            return {"error": "memory_unavailable"}

        # ── Plan-mode branch ──────────────────────────────
        raw_plan = params.get("plan")
        if isinstance(raw_plan, dict):
            from ..events import MemoryPromotionPlan

            decision = str(params.get("decision", "")).lower()
            if decision not in ("apply", "reject", "skip"):
                return {"error": "invalid_decision"}

            raw_ids = raw_plan.get("consumedEntryIds") or []
            if not isinstance(raw_ids, list):
                return {"error": "invalid_plan"}
            new_core = str(raw_plan.get("newCore", ""))
            current_core = str(raw_plan.get("currentCore", ""))
            rationale = str(raw_plan.get("rationale", ""))
            consumed = tuple(str(x) for x in raw_ids)

            if decision in ("apply", "reject") and not consumed:
                return {"error": "invalid_plan"}
            if decision == "apply" and not new_core.strip():
                return {"error": "invalid_plan"}

            plan = MemoryPromotionPlan(
                current_core=current_core,
                new_core=new_core,
                consumed_entry_ids=consumed,
                rationale=rationale,
            )

            if decision == "apply":
                counts = self._memory.apply_promotion_plan(plan)
                log.info(
                    "memory/plan_apply",
                    session_id=session_id,
                    consumed=counts.get("consumed", 0),
                )
                return {
                    "applied": counts.get("applied", 1),
                    "consumed": counts.get("consumed", 0),
                    "core": self._memory.read_core(),
                }
            if decision == "reject":
                counts = self._memory.reject_promotion_plan(plan)
                log.info(
                    "memory/plan_reject",
                    session_id=session_id,
                    rejected=counts.get("rejected", 0),
                )
                return {
                    "rejected": counts.get("rejected", 0),
                    "core": self._memory.read_core(),
                }
            # skip: host is just dropping its cache; nothing to do here.
            log.info("memory/plan_skip", session_id=session_id)
            return {"skipped": 1, "core": self._memory.read_core()}

        # ── Legacy per-candidate branch ───────────────────
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

    async def _run_turn(
        self,
        state: SessionState,
        session_id: str,
        *,
        force_plan_start: bool = False,
        require_plan_approval: bool = False,
        plan_approval: dict[str, Any] | None = None,
        auto_approve_plan: bool = False,
        completion_gate: CompletionGate | None = None,
        plan_start_text: str | None = None,
    ) -> str:
        """Consume the shared execution core and translate events to ACP updates."""
        agent = state.agent

        # Clear prompt-level grants at the start of each prompt
        if state.grant_store:
            state.grant_store.clear_prompt_grants()

        # Build permission negotiator if engine is available
        negotiator = None
        if state.grant_store:
            negotiator = _PermissionNegotiator(
                conn=self._conn,
                session_id=session_id,
                grant_store=state.grant_store,
            )

        if state.expert_context:
            team_progress = state.expert_context.team_progress_payload()
            if team_progress:
                progress_id = f"expert-team-progress-{uuid4().hex[:8]}"
                log.debug("expert_team/progress", session_id=session_id, progress=team_progress)
                try:
                    await self._send(
                        session_id,
                        update_tool_call(progress_id, raw_output=team_progress),
                    )
                except Exception as exc:
                    log.exception("expert_team/progress_send_error", exc, session_id=session_id)

        skill_name_by_tool_call_id: dict[str, str] = {}
        used_skill_names: list[str] = []
        for preloaded_skill_name in state.preloaded_skill_names:
            preloaded_skill_name = preloaded_skill_name.strip()
            if preloaded_skill_name and preloaded_skill_name not in used_skill_names:
                used_skill_names.append(preloaded_skill_name)
        used_tool_counts: dict[str, int] = {}
        used_mcp_tool_counts: dict[tuple[str, str], int] = {}
        turn_token_usage = {
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "calls": 0,
        }
        usage_tool_call_id = f"turn-usage-{uuid4().hex[:8]}"

        def _get_skill_name_from_args(args: Any) -> str | None:
            if not isinstance(args, dict):
                return None
            value = args.get("skill_name") or args.get("skillName") or args.get("name")
            if not isinstance(value, str):
                return None
            value = value.strip()
            return value or None

        def _record_skill_usage(skill_name: str | None) -> dict[str, Any] | None:
            if not skill_name:
                return None
            if skill_name not in used_skill_names:
                used_skill_names.append(skill_name)
            return {
                "type": "skills_usage",
                "skills": list(used_skill_names),
                "current": skill_name,
            }

        def _as_int(mapping: dict[str, Any], *keys: str) -> int:
            for key in keys:
                value = mapping.get(key)
                if isinstance(value, int):
                    return value
                if isinstance(value, float):
                    return int(value)
            return 0

        def _record_token_usage(usage: Any) -> bool:
            if not isinstance(usage, dict):
                return False
            prompt_tokens = _as_int(usage, "prompt_tokens", "promptTokens")
            completion_tokens = _as_int(usage, "completion_tokens", "completionTokens")
            total_tokens = _as_int(usage, "total_tokens", "totalTokens")
            if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
                total_tokens = prompt_tokens + completion_tokens
            if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
                return False
            turn_token_usage["promptTokens"] += prompt_tokens
            turn_token_usage["completionTokens"] += completion_tokens
            turn_token_usage["totalTokens"] += total_tokens
            turn_token_usage["calls"] += 1
            return True

        def _mcp_tool_info(tool_name: str) -> tuple[str, str] | None:
            tool = agent.tools.get(tool_name)
            server_name = ""
            mcp_tool_name = tool_name
            if tool is not None:
                raw_server = getattr(tool, "server_name", None) or getattr(tool, "_server_name", None)
                raw_tool_name = getattr(tool, "tool_name", None) or getattr(tool, "_mcp_tool_name", None)
                if isinstance(raw_server, str):
                    server_name = raw_server.strip()
                if isinstance(raw_tool_name, str) and raw_tool_name.strip():
                    mcp_tool_name = raw_tool_name.strip()

            if not server_name and tool_name.startswith("mcp__"):
                parts = tool_name.split("__", 2)
                if len(parts) == 3 and parts[1] and parts[2]:
                    server_name = parts[1]
                    mcp_tool_name = parts[2]

            if not server_name:
                return None
            return server_name, mcp_tool_name

        def _record_tool_usage(tool_name: str, *, user_visible: bool) -> dict[str, Any] | None:
            if not user_visible or not tool_name or tool_name == "get_skill":
                return None
            mcp_info = _mcp_tool_info(tool_name)
            if mcp_info is not None:
                used_mcp_tool_counts[mcp_info] = used_mcp_tool_counts.get(mcp_info, 0) + 1
                server_name, mcp_tool_name = mcp_info
                return {
                    "type": "mcp",
                    "name": f"{server_name}.{mcp_tool_name}",
                    "server": server_name,
                    "tool": mcp_tool_name,
                }

            used_tool_counts[tool_name] = used_tool_counts.get(tool_name, 0) + 1
            return {
                "type": "tool",
                "name": tool_name,
            }

        def _turn_usage_payload(current: dict[str, Any] | None = None) -> dict[str, Any]:
            meter = get_token_meter()
            token_usage = (
                {
                    "promptTokens": meter.prompt_tokens,
                    "completionTokens": meter.completion_tokens,
                    "totalTokens": meter.total_tokens,
                    "calls": meter.calls,
                }
                if meter is not None and meter.total_tokens > 0
                else dict(turn_token_usage)
            )
            payload: dict[str, Any] = {
                "type": "turn_usage",
                "version": 1,
                "skills": list(used_skill_names),
                "tools": [
                    {"name": name, "count": count}
                    for name, count in used_tool_counts.items()
                ],
                "mcp": [
                    {
                        "server": server_name,
                        "tool": tool_name,
                        "name": f"{server_name}.{tool_name}",
                        "count": count,
                    }
                    for (server_name, tool_name), count in used_mcp_tool_counts.items()
                ],
                "tokenUsage": token_usage,
            }
            if current:
                payload["current"] = current
            return payload

        async def _send_turn_usage(current: dict[str, Any] | None = None) -> None:
            payload = _turn_usage_payload(current)
            log.debug(
                "turn/usage",
                session_id=session_id,
                payload=payload,
            )
            await self._send(
                session_id,
                update_tool_call(usage_tool_call_id, raw_output=payload),
            )

        async def _send_skill_usage(
            tool_call_id: str,
            payload: dict[str, Any],
        ) -> None:
            log.debug(
                "skills/usage",
                session_id=session_id,
                tool_call_id=tool_call_id,
                payload=payload,
            )
            await self._send(
                session_id,
                update_tool_call(tool_call_id, raw_output=payload),
            )

        llm = _ActionHintNormalizingLLM(agent.llm)

        async for event in run_agent_loop(
            llm=llm,
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
            session_id=state.upstream_session_id,
            force_plan_start=force_plan_start,
            require_plan_approval=require_plan_approval,
            plan_approval=plan_approval,
            plan_start_text=plan_start_text,
            pause_after_plan_write=not auto_approve_plan,
            completion_gate=completion_gate,
            truncation_continuation_enabled=agent.truncation_continuation_enabled,
            max_truncation_continuations=agent.max_truncation_continuations,
            artifact_detection_enabled=state.artifact_mode != "project",
            artifact_root_dir=state.output_dir,
        ):
            try:
                match event:
                    case ThinkingEvent() if event._streaming:
                        # Stream thinking deltas in real-time
                        if not event._header and event.content:
                            log.debug("thinking_stream", session_id=session_id, chars=len(event.content))
                            await self._send(session_id, update_agent_thought(text_block(event.content)))

                    case ThinkingEvent(content=text):
                        log.debug("thinking", session_id=session_id, content=text)
                        await self._send(session_id, update_agent_thought(text_block(text)))

                    case ContentEvent() if event._streaming:
                        # Stream content deltas in real-time
                        if not event._header and event.content:
                            log.debug(
                                "content/stream",
                                session_id=session_id,
                                chars=len(event.content),
                                content=event.content,
                            )
                            await self._send(session_id, update_agent_message(text_block(event.content)))

                    case ContentEvent(content=text):
                        log.debug("content/final", session_id=session_id, chars=len(text), content=text)
                        log.debug("content", session_id=session_id, content=text)
                        await self._send(session_id, update_agent_message(text_block(text)))

                    case ProgressEvent(step=s, content=text):
                        payload = {
                            "type": "agent_progress",
                            "step": s,
                            "content": text,
                        }
                        log.debug("progress", session_id=session_id, step=s, content=text)
                        await self._send(
                            session_id,
                            update_tool_call(f"agent-progress-{s}", raw_output=payload),
                        )

                    case PlanSnapshotEvent(payload=payload):
                        log.debug("plan/snapshot", session_id=session_id, payload=payload)
                        _update_pending_plan_approval_from_raw(state, payload)
                        plan_call_id = f"plan-snapshot-start-{uuid4().hex[:8]}"
                        title = str((payload.get("plan") or {}).get("title") or "执行方案")
                        await self._send(
                            session_id,
                            start_tool_call(
                                plan_call_id,
                                title,
                                kind="execute",
                                raw_input={"action": payload.get("action")},
                            ),
                        )
                        await self._send(
                            session_id,
                            update_tool_call(
                                plan_call_id,
                                status="completed",
                                content=[tool_content(text_block(title))],
                                raw_output=payload,
                            ),
                        )

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
                        if _record_token_usage(usage):
                            await _send_turn_usage()

                    case ToolCallStartEvent(tool_call_id=tid, tool_name=name, arguments=args, user_visible=user_visible):
                        log.info(
                            "tool/start",
                            session_id=session_id,
                            tool_call_id=tid,
                            tool_name=name,
                            arguments=args,
                            user_visible=user_visible,
                        )
                        if name == "get_skill":
                            skill_name = _get_skill_name_from_args(args)
                            if skill_name:
                                skill_name_by_tool_call_id[tid] = skill_name
                        tool_usage_current = _record_tool_usage(name, user_visible=user_visible)
                        if tool_usage_current:
                            await _send_turn_usage(tool_usage_current)
                        if not user_visible:
                            continue
                        if name == "sub_agent" and isinstance(args, dict):
                            # Surface the short distinct label as the title so the
                            # host doesn't fall back to the long, near-identical task.
                            sub_title = " ".join(str(args.get("title") or "").split())
                            label = f"🔧 sub_agent: {sub_title}" if sub_title else "🔧 sub_agent()"
                        else:
                            args_preview = (
                                ", ".join(f"{k}={repr(v)[:50]}" for k, v in list(args.items())[:2])
                                if isinstance(args, dict) else ""
                            )
                            label = f"🔧 {name}({args_preview})" if args_preview else f"🔧 {name}()"
                        await self._send(session_id, start_tool_call(tid, label, kind="execute", raw_input=args))

                    case ToolCallResultEvent(
                        tool_call_id=tid,
                        tool_name=tname,
                        success=ok,
                        content=text,
                        error=err,
                        raw_output=raw_output,
                        user_visible=user_visible,
                        policy_decision=policy_decision,
                    ):
                        if ok:
                            log.info("tool/end", session_id=session_id, tool_call_id=tid, tool_name=tname, result=text, user_visible=user_visible)
                        else:
                            log.warn("tool/fail", session_id=session_id, tool_call_id=tid, tool_name=tname, error=err, user_visible=user_visible)
                        _update_pending_plan_approval_from_raw(state, raw_output)
                        skill_usage_payload = (
                            _record_skill_usage(skill_name_by_tool_call_id.get(tid))
                            if tname == "get_skill" and ok
                            else None
                        )
                        if not user_visible:
                            if skill_usage_payload:
                                await _send_skill_usage(tid, skill_usage_payload)
                                await _send_turn_usage(
                                    {"type": "skill", "name": skill_usage_payload["current"]}
                                )
                            continue
                        status = "completed" if ok else "failed"
                        prefix = "[OK]" if ok else "[ERROR]"
                        result_text = f"{prefix} {text if ok else err or 'Tool execution failed'}"
                        output = _tool_result_raw_output(
                            raw_output,
                            result_text,
                            policy_decision,
                            session_id=state.upstream_session_id,
                            output_dir=state.output_dir,
                        )
                        await self._send(
                            session_id,
                            update_tool_call(tid, status=status, content=[tool_content(text_block(result_text))], raw_output=output),
                        )
                        if skill_usage_payload:
                            await _send_skill_usage(tid, skill_usage_payload)
                            await _send_turn_usage(
                                {"type": "skill", "name": skill_usage_payload["current"]}
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
                        artifact_meta = _artifact_envelope(
                            art,
                            state.output_dir,
                            session_id=state.upstream_session_id,
                        )
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

                    case InjectedMessageEvent(content=text, injection_id=injection_id, user_visible=user_visible):
                        log.info(
                            "session/injected",
                            session_id=session_id,
                            injection_id=injection_id,
                            user_visible=user_visible,
                            text=text[:80],
                        )
                        if not user_visible:
                            continue
                        await self._send(
                            session_id,
                            update_agent_message(text_block(_injected_marker(text, injection_id))),
                        )

                    case StepEnd(step=s, elapsed_seconds=el, total_elapsed_seconds=tot):
                        log.debug("step/end", session_id=session_id, step=s, duration_ms=int(el * 1000), total_ms=int(tot * 1000))

                    case DoneEvent(stop_reason=reason):
                        log.debug("done", session_id=session_id, stop_reason=reason.value)
                        await _send_turn_usage()
                        return reason.value

                    case SubAgentEvent(parent_tool_call_id=tid, task_preview=preview, event=inner, sub_agent_id=sub_agent_id, title=sub_title):
                        if (
                            isinstance(inner, ToolCallStartEvent)
                            and inner.tool_name == "get_skill"
                        ):
                            skill_name = _get_skill_name_from_args(inner.arguments)
                            if skill_name:
                                skill_name_by_tool_call_id[inner.tool_call_id] = skill_name
                        if isinstance(inner, ToolCallStartEvent):
                            tool_usage_current = _record_tool_usage(
                                inner.tool_name,
                                user_visible=inner.user_visible,
                            )
                            if tool_usage_current:
                                await _send_turn_usage(tool_usage_current)

                        if (
                            isinstance(inner, ToolCallResultEvent)
                            and inner.tool_name == "get_skill"
                            and inner.success
                        ):
                            skill_usage_payload = _record_skill_usage(
                                skill_name_by_tool_call_id.get(inner.tool_call_id)
                            )
                            if skill_usage_payload:
                                await _send_skill_usage(tid, skill_usage_payload)
                                await _send_turn_usage(
                                    {"type": "skill", "name": skill_usage_payload["current"]}
                                )

                        if isinstance(inner, LLMOutputEvent) and _record_token_usage(inner.usage):
                            await _send_turn_usage()

                        if getattr(inner, "user_visible", True) is False:
                            continue
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
                            "title": sub_title or preview,
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
                                progress["artifact"] = _artifact_envelope(
                                    art,
                                    state.output_dir,
                                    session_id=state.upstream_session_id,
                                )
                            case ErrorEvent(message=msg):
                                progress["event"] = "error"
                                progress["message"] = msg
                            case ProgressEvent(step=s, content=content):
                                progress["event"] = "agent_progress"
                                progress["step"] = s
                                progress["content"] = content
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
        try:
            await asyncio.wait_for(
                self._conn.sessionUpdate(session_notification(session_id, update)),
                timeout=self._SESSION_UPDATE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            log.warn(
                "session/update_timeout",
                session_id=session_id,
                timeout_seconds=self._SESSION_UPDATE_TIMEOUT_SECONDS,
                update_type=getattr(update, "sessionUpdate", type(update).__name__),
            )
            raise TimeoutError(
                f"ACP session update timed out after {self._SESSION_UPDATE_TIMEOUT_SECONDS:g}s"
            ) from exc


class _PermissionNegotiator:
    """In-band permission negotiation via ACP ``session/request_permission`` reverse RPC.

    Wraps the ACP ``AgentSideConnection.requestPermission()`` call with:
    - Grant-table deduplication for filesystem/memory escalation
    - One-shot, non-cached approval for safety confirmations
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
        is_safety_request = scope == "safety"

        # Dedup: filesystem requests check the directory grant table; other
        # capabilities (memory) use the legacy (scope, requested_scope) key.
        # Safety requests are intentionally never cached: every dangerous
        # command needs an explicit one-shot decision.
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
        elif not is_safety_request and self._store.has_grant(scope, requested_scope):
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
        temporary_supported = permission_request.get("temporary_supported", True) is not False
        persistent_supported = permission_request.get("persistent_supported", True) is not False
        tool_call = ToolCall(
            toolCallId=f"perm-{scope}-{requested_scope}",
            rawInput=permission_request,
        )
        options = []
        if temporary_supported:
            options.append(PermissionOption(optionId="approve", name="仅本次允许", kind="allow_once"))
        if persistent_supported:
            persistent_name = permission_request.get("persistent_label") or "始终允许"
            options.append(
                PermissionOption(
                    optionId="approve_session",
                    name=str(persistent_name),
                    kind="allow_always",
                )
            )
        options.append(PermissionOption(optionId="reject", name="拒绝", kind="reject_once"))
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
            if response.outcome.optionId == "reject":
                log.info(
                    "permission/denied",
                    scope=scope,
                    requested_scope=requested_scope,
                )
                return False
            grant_scope = self._OPTION_TO_SCOPE.get(response.outcome.optionId, "prompt")
            if is_safety_request:
                log.info(
                    "permission/granted",
                    scope=scope,
                    requested_scope=requested_scope,
                    grant_scope="one_shot",
                )
                return True
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
    _VALID_PLAN_DECISIONS = {"apply", "reject", "skip"}

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

        plan = getattr(event, "plan", None)
        payload: dict[str, Any] = {
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
        if plan is not None:
            payload["plan"] = {
                "currentCore": plan.current_core,
                "newCore": plan.new_core,
                "consumedEntryIds": list(plan.consumed_entry_ids),
                "rationale": plan.rationale,
            }

        log.info(
            "memory/proposal_request",
            count=len(candidates),
            has_plan=plan is not None,
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

        # Plan-mode response: {"decision": "apply"|"reject"|"skip"}
        if plan is not None and "decision" in response:
            decision_raw = response.get("decision")
            decision = decision_raw.lower() if isinstance(decision_raw, str) else ""
            if decision not in self._VALID_PLAN_DECISIONS:
                return
            try:
                if decision == "apply":
                    counts = self._mgr.apply_promotion_plan(plan)
                    log.info("memory/plan_applied", consumed=counts.get("consumed", 0))
                elif decision == "reject":
                    counts = self._mgr.reject_promotion_plan(plan)
                    log.info("memory/plan_rejected", rejected=counts.get("rejected", 0))
                else:
                    log.info("memory/plan_skipped")
            except Exception as exc:
                log.warn("memory/plan_apply_error", error=str(exc))
            return

        # Legacy per-candidate response
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
            timeout=config.llm.timeout,
        )

        # Lite LLM client for tool-free small tasks (titles / summaries).
        # When `lite_llm:` is absent from config, fall back to the main client
        # so call sites stay uniform.
        if config.lite_llm._present:
            lite_rcfg = config.lite_llm.retry
            lite_provider = (
                LLMProvider.ANTHROPIC
                if config.lite_llm.provider.lower() == "anthropic"
                else LLMProvider.OPENAI
            )
            lite_llm = LLMClient(
                api_key=config.lite_llm.api_key,
                provider=lite_provider,
                api_base=config.lite_llm.api_base,
                model=config.lite_llm.model,
                retry_config=RetryConfigBase(
                    enabled=lite_rcfg.enabled,
                    max_retries=lite_rcfg.max_retries,
                    initial_delay=lite_rcfg.initial_delay,
                    max_delay=lite_rcfg.max_delay,
                    exponential_base=lite_rcfg.exponential_base,
                ),
                max_output_tokens=config.lite_llm.max_output_tokens,
                auth_file=config.lite_llm.auth_file,
                timeout=config.lite_llm.timeout,
            )
        else:
            lite_llm = llm

        # Create memory manager if enabled
        memory_mgr = None
        if config.agent.enable_memory:
            memory_mgr = MemoryManager(
                memory_dir=config.agent.memory_dir,
                dedup_jaccard_threshold=config.agent.memory_dedup_jaccard,
            )

        # Memory bootstrap (one-time OpenClaw import + maintenance) runs OFF the
        # critical path. Both steps can issue slow LLM calls — OpenClaw filtering
        # on first launch, and the maintainer's compact phase on a large
        # CONTEXT.md — which used to blow the host's ACP init timeout: stdio
        # (and thus the `initialize` response) is only set up *after* this block,
        # so an awaited LLM call here delays readiness and the host kills the
        # process before it ever answers. Fire-and-forget instead — errors are
        # logged but never block stdio readiness; results land in memory for
        # subsequent turns. Same pattern as the background MCP loader.
        #
        # Import and maintenance run sequentially within the one task so the
        # maintainer observes freshly-imported content and they never race on
        # MEMORY.md writes.
        memory_bootstrap_task: asyncio.Task | None = None
        if memory_mgr:
            _maintainer_enabled = config.agent.memory_maintainer_enabled

            async def _memory_bootstrap() -> None:
                try:
                    await memory_mgr.import_openclaw(llm)
                except Exception:
                    log.warn("server/start", message="OpenClaw import failed (non-fatal)")
                if _maintainer_enabled:
                    from box_agent.memory_maintainer import MemoryMaintainer

                    try:
                        await MemoryMaintainer(memory_mgr, config.agent, llm=llm).run_if_due()
                    except Exception:
                        log.warn("server/start", message="Memory maintainer failed (non-fatal)")

            memory_bootstrap_task = asyncio.create_task(_memory_bootstrap(), name="memory-bootstrap")

        base_tools, skill_loader, mcp_task = await initialize_base_tools(config, output=_stderr_print, memory_manager=memory_mgr, llm=llm)
        prompt_path = Config.find_config_file(config.agent.system_prompt_path)
        if prompt_path and prompt_path.exists():
            system_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant."

        # SANDBOX_INFO is injected per session because officev3 can mark an ACP
        # session as an existing project workspace instead of output-artifact mode.

        # NOTE: actual skill list is injected per-turn via SkillSelector
        # (keyword-filtered against the cumulative user query). Here we keep a
        # sentinel that the selector replaces with a filtered catalog.
        if skill_loader:
            from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL
            system_prompt = system_prompt.replace("{SKILLS_METADATA}", SKILL_SLOT_SENTINEL)
        else:
            system_prompt = system_prompt.replace("{SKILLS_METADATA}", "")

        log.info("server/start", message=f"LLM: {config.llm.model}, provider: {config.llm.provider}")
        if config.lite_llm._present:
            log.info(
                "server/start",
                message=f"Lite LLM: {config.lite_llm.model or '<server-default>'}, provider: {config.lite_llm.provider}, base: {config.lite_llm.api_base}",
            )
        else:
            log.info("server/start", message="Lite LLM: <fallback to main>")
        log.info("server/start", message=f"Tools loaded: {len(base_tools)} base tools")

        # Restore real stdout for ACP transport, then re-guard sys.stdout
        sys.stdout = _real_stdout
        reader, writer = await stdio_streams_largebuf()

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
        AgentSideConnection(lambda conn: BoxACPAgent(conn, config, llm, base_tools, system_prompt, memory_manager=memory_mgr, hooks=_hooks, skill_loader=skill_loader, mcp_task=mcp_task, lite_llm=lite_llm), writer, reader)

        log.info("server/ready", message="ACP server ready, listening on stdio")
        await asyncio.Event().wait()

    except Exception as exc:
        log.exception("server/error", exc, message="ACP server failed to start")
        raise


def main() -> None:
    asyncio.run(run_acp_server())


__all__ = ["BoxACPAgent", "run_acp_server", "main"]
