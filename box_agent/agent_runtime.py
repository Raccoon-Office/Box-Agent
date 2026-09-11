"""Host-neutral runtime assembly helpers.

The public adapters keep ownership of protocol and interaction policy.  This
module only centralizes construction details that must stay identical across
those adapters.  Helpers intentionally accept an explicit factory so CLI and
ACP tests (and host-specific wrappers) can keep patching their existing
``LLMClient`` symbols.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agent import Agent
from .llm.llm_wrapper import LLMClient
from .memory import MemoryExtractor, MemoryManager
from .retry import RetryConfig
from .schema import LLMProvider
from .tools.permissions import PermissionEngine

LLMClientFactory = Callable[..., LLMClient]
RetryCallback = Callable[[Exception, int], Any]
AgentFactory = Callable[..., Agent]
PermissionEngineFactory = Callable[..., PermissionEngine]
MemoryManagerFactory = Callable[..., MemoryManager]
MemoryExtractorFactory = Callable[..., MemoryExtractor]
_UNSET = object()


def build_llm_client(
    *,
    api_key: str,
    provider: LLMProvider,
    api_base: str,
    model: str,
    retry_config: RetryConfig | None,
    max_output_tokens: int,
    auth_file: str,
    timeout: float,
    reasoning_effort_when_disabled: str | None = None,
    client_factory: LLMClientFactory = LLMClient,
    retry_callback: RetryCallback | None = None,
) -> LLMClient:
    """Construct an LLM client without changing host-specific retry policy.

    Callers decide whether retry is enabled and may supply a callback for
    their own UI.  The helper forwards every transport argument unchanged and
    only assigns a callback when one is explicitly provided.
    """

    client = client_factory(
        api_key=api_key,
        provider=provider,
        api_base=api_base,
        model=model,
        retry_config=retry_config,
        max_output_tokens=max_output_tokens,
        auth_file=auth_file,
        timeout=timeout,
        **(
            {"reasoning_effort_when_disabled": reasoning_effort_when_disabled}
            if reasoning_effort_when_disabled is not None
            else {}
        ),
    )
    if retry_callback is not None:
        client.retry_callback = retry_callback
    return client


def build_agent(
    *,
    llm_client: Any,
    system_prompt: str,
    tools: list[Any],
    max_steps: int,
    tool_limits: Any | None,
    workspace_dir: str,
    token_limit: int,
    hooks: list[Any] | None | object = _UNSET,
    plugins: tuple[Any, ...] | object = _UNSET,
    thinking_enabled: bool = False,
    memory_promotion_enabled: bool = False,
    memory_promotion_hit_threshold: int = 5,
    memory_promotion_cooldown_days: int = 14,
    max_parallel_tools: int = 8,
    parallel_tool_timeout_seconds: float | None = 900.0,
    provider_stale_seconds: float | None = None,
    truncation_continuation_enabled: bool = True,
    max_truncation_continuations: int = 3,
    max_truncated_tool_call_retries: int = 3,
    truncated_tool_call_boost_cap: int = 32768,
    context_resource_dedup_enabled: bool = True,
    allowed_connector_ids_provider: Callable[[], frozenset[str]] | None = None,
    deferred_mcp_loading_enabled: bool = True,
    session_log: Any = _UNSET,
    enable_builtin_tools: bool | object = _UNSET,
    skill_runtime: Any = _UNSET,
    session_id: str | None = None,
    agent_factory: AgentFactory = Agent,
) -> Agent:
    """Construct an Agent while keeping adapter-specific state outside it.

    The adapters still resolve their own prompt, tools, and host policy.  This
    helper only forwards the existing constructor options so the two hosts do
    not grow separate lists of runtime knobs.  ``agent_factory`` keeps the
    historical module-level ``Agent`` symbols patchable in tests and hosts.
    """

    kwargs: dict[str, Any] = {
        "llm_client": llm_client,
        "system_prompt": system_prompt,
        "tools": tools,
        "max_steps": max_steps,
        "tool_limits": tool_limits,
        "workspace_dir": workspace_dir,
        "token_limit": token_limit,
        "thinking_enabled": thinking_enabled,
        "memory_promotion_enabled": memory_promotion_enabled,
        "memory_promotion_hit_threshold": memory_promotion_hit_threshold,
        "memory_promotion_cooldown_days": memory_promotion_cooldown_days,
        "max_parallel_tools": max_parallel_tools,
        "parallel_tool_timeout_seconds": parallel_tool_timeout_seconds,
        "provider_stale_seconds": provider_stale_seconds,
        "truncation_continuation_enabled": truncation_continuation_enabled,
        "max_truncation_continuations": max_truncation_continuations,
        "max_truncated_tool_call_retries": max_truncated_tool_call_retries,
        "truncated_tool_call_boost_cap": truncated_tool_call_boost_cap,
        "context_resource_dedup_enabled": context_resource_dedup_enabled,
        "allowed_connector_ids_provider": allowed_connector_ids_provider,
        "deferred_mcp_loading_enabled": deferred_mcp_loading_enabled,
    }
    if plugins is not _UNSET:
        kwargs["plugins"] = plugins
    if hooks is not _UNSET:
        kwargs["hooks"] = hooks
    if session_log is not _UNSET:
        kwargs["session_log"] = session_log
    if enable_builtin_tools is not _UNSET:
        kwargs["enable_builtin_tools"] = enable_builtin_tools
    if skill_runtime is not _UNSET:
        kwargs["skill_runtime"] = skill_runtime
    if session_id is not None:
        kwargs["session_id"] = session_id
    return agent_factory(**kwargs)


def build_permission_engine(
    policy: Any,
    workspace_dir: Path,
    *,
    grant_store: Any | None = None,
    engine_factory: PermissionEngineFactory = PermissionEngine,
) -> PermissionEngine:
    """Construct the shared permission engine with host-resolved inputs.

    ACP and CLI retain responsibility for resolving their policies and for
    negotiating approvals.  This helper only centralizes the common engine
    construction call and forwards the existing objects unchanged.
    """

    return engine_factory(policy, workspace_dir, grant_store=grant_store)


def build_memory_manager(
    *,
    memory_dir: str,
    dedup_jaccard_threshold: float,
    manager_factory: MemoryManagerFactory = MemoryManager,
) -> MemoryManager:
    """Construct a memory manager with the existing storage parameters."""

    return manager_factory(
        memory_dir=memory_dir,
        dedup_jaccard_threshold=dedup_jaccard_threshold,
    )


def build_memory_extractor(
    *,
    llm: Any,
    memory_manager: MemoryManager,
    cooldown: int,
    step_interval: int,
    session_id: str | None = None,
    turn_id: str | None = None,
    extractor_factory: MemoryExtractorFactory = MemoryExtractor,
) -> MemoryExtractor:
    """Construct a memory extractor, preserving optional host bindings."""

    kwargs: dict[str, Any] = {
        "llm": llm,
        "memory_manager": memory_manager,
        "cooldown": cooldown,
        "step_interval": step_interval,
    }
    if session_id is not None:
        kwargs["session_id"] = session_id
    if turn_id is not None:
        kwargs["turn_id"] = turn_id
    return extractor_factory(**kwargs)


__all__ = [
    "AgentFactory",
    "LLMClientFactory",
    "MemoryExtractorFactory",
    "MemoryManagerFactory",
    "PermissionEngineFactory",
    "RetryCallback",
    "build_agent",
    "build_llm_client",
    "build_memory_manager",
    "build_memory_extractor",
    "build_permission_engine",
]
