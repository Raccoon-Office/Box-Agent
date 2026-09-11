from __future__ import annotations

from box_agent.agent_runtime import (
    build_agent,
    build_llm_client,
    build_memory_extractor,
    build_memory_manager,
    build_permission_engine,
)
from box_agent.tools.permissions import CapabilityPolicy, GrantStore
from box_agent.schema import LLMProvider


def test_build_llm_client_forwards_all_transport_arguments() -> None:
    captured: dict[str, object] = {}

    class CaptureClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    client = build_llm_client(
        client_factory=CaptureClient,
        api_key="key",
        provider=LLMProvider.OPENAI,
        api_base="https://example.test/v1/",
        model="model-a",
        retry_config=None,
        max_output_tokens=123,
        auth_file="auth.json",
        timeout=17.5,
    )

    assert isinstance(client, CaptureClient)
    assert captured == {
        "api_key": "key",
        "provider": LLMProvider.OPENAI,
        "api_base": "https://example.test/v1/",
        "model": "model-a",
        "retry_config": None,
        "max_output_tokens": 123,
        "auth_file": "auth.json",
        "timeout": 17.5,
    }


def test_build_llm_client_assigns_retry_callback_only_when_requested() -> None:
    class CaptureClient:
        def __init__(self, **_kwargs: object) -> None:
            self.retry_callback = None

    callback = object()
    client = build_llm_client(
        client_factory=CaptureClient,
        api_key="key",
        provider=LLMProvider.ANTHROPIC,
        api_base="https://example.test",
        model="model-a",
        retry_config=None,
        max_output_tokens=123,
        auth_file="",
        timeout=17.5,
        retry_callback=callback,
    )

    assert client.retry_callback is callback


def test_build_permission_engine_forwards_policy_workspace_and_grants(tmp_path) -> None:
    captured: dict[str, object] = {}

    class CaptureEngine:
        def __init__(self, policy, workspace_dir, *, grant_store=None) -> None:
            captured.update(
                policy=policy,
                workspace_dir=workspace_dir,
                grant_store=grant_store,
            )

    policy = CapabilityPolicy(session_workspace_root=str(tmp_path))
    grant_store = GrantStore()
    engine = build_permission_engine(
        policy,
        tmp_path,
        grant_store=grant_store,
        engine_factory=CaptureEngine,
    )

    assert isinstance(engine, CaptureEngine)
    assert captured == {
        "policy": policy,
        "workspace_dir": tmp_path,
        "grant_store": grant_store,
    }


def test_build_memory_manager_forwards_storage_arguments() -> None:
    captured: dict[str, object] = {}

    class CaptureManager:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    manager = build_memory_manager(
        memory_dir="memory-dir",
        dedup_jaccard_threshold=0.75,
        manager_factory=CaptureManager,
    )

    assert isinstance(manager, CaptureManager)
    assert captured == {
        "memory_dir": "memory-dir",
        "dedup_jaccard_threshold": 0.75,
    }


def test_build_memory_extractor_preserves_optional_session_binding() -> None:
    captured: dict[str, object] = {}

    class CaptureExtractor:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    llm = object()
    memory_manager = object()
    extractor = build_memory_extractor(
        llm=llm,
        memory_manager=memory_manager,
        cooldown=3,
        step_interval=4,
        session_id="session-1",
        extractor_factory=CaptureExtractor,
    )

    assert isinstance(extractor, CaptureExtractor)
    assert captured == {
        "llm": llm,
        "memory_manager": memory_manager,
        "cooldown": 3.0,
        "step_interval": 4,
        "session_id": "session-1",
    }


def test_build_agent_forwards_shared_constructor_options(tmp_path) -> None:
    captured: dict[str, object] = {}

    class CaptureAgent:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    llm = object()
    tools = [object()]
    hooks = [object()]
    tool_limits = object()
    session_log = object()
    allowed_connector_ids_provider = lambda: frozenset({"pkulaw"})
    agent = build_agent(
        agent_factory=CaptureAgent,
        llm_client=llm,
        system_prompt="system",
        tools=tools,
        max_steps=7,
        tool_limits=tool_limits,
        workspace_dir=str(tmp_path),
        token_limit=123,
        hooks=hooks,
        thinking_enabled=True,
        memory_promotion_enabled=True,
        memory_promotion_hit_threshold=8,
        memory_promotion_cooldown_days=9,
        max_parallel_tools=4,
        parallel_tool_timeout_seconds=12.5,
        provider_stale_seconds=4.5,
        truncation_continuation_enabled=False,
        max_truncation_continuations=2,
        max_truncated_tool_call_retries=1,
        truncated_tool_call_boost_cap=256,
        context_resource_dedup_enabled=False,
        allowed_connector_ids_provider=allowed_connector_ids_provider,
        deferred_mcp_loading_enabled=False,
        session_log=session_log,
    )

    assert isinstance(agent, CaptureAgent)
    assert captured == {
        "llm_client": llm,
        "system_prompt": "system",
        "tools": tools,
        "max_steps": 7,
        "tool_limits": tool_limits,
        "workspace_dir": str(tmp_path),
        "token_limit": 123,
        "hooks": hooks,
        "thinking_enabled": True,
        "memory_promotion_enabled": True,
        "memory_promotion_hit_threshold": 8,
        "memory_promotion_cooldown_days": 9,
        "max_parallel_tools": 4,
        "parallel_tool_timeout_seconds": 12.5,
        "provider_stale_seconds": 4.5,
        "truncation_continuation_enabled": False,
        "max_truncation_continuations": 2,
        "max_truncated_tool_call_retries": 1,
        "truncated_tool_call_boost_cap": 256,
        "context_resource_dedup_enabled": False,
        "allowed_connector_ids_provider": allowed_connector_ids_provider,
        "deferred_mcp_loading_enabled": False,
        "session_log": session_log,
    }


def test_build_agent_preserves_explicit_none_optional_arguments() -> None:
    captured: dict[str, object] = {}

    class CaptureAgent:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    build_agent(
        agent_factory=CaptureAgent,
        llm_client=object(),
        system_prompt="system",
        tools=[],
        max_steps=1,
        tool_limits=None,
        workspace_dir="workspace",
        token_limit=100,
        hooks=None,
        session_log=None,
    )

    assert captured["hooks"] is None
    assert captured["session_log"] is None
