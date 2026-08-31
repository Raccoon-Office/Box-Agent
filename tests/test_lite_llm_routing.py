"""Lite LLM routing: fallback when unconfigured, separate client when present."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from box_agent.acp import BoxACPAgent
from box_agent.config import (
    AgentConfig,
    Config,
    LiteLLMConfig,
    LLMConfig,
    ToolsConfig,
)
from box_agent.llm import LLMClient
from box_agent.llm.model_routing import resolve_model_client, select_auto_model
from box_agent.schema import LLMProvider, LLMResponse, StreamEvent


class _DummyLLM:
    """Minimal LLMClient stub; only attributes touched by the ACP layer."""

    def __init__(self, label: str):
        self.label = label
        self.provider = "openai"
        self.model = f"model-{label}"
        self.last_messages = None
        self.last_kwargs = None
        self.max_output_tokens = 80000

    async def generate(self, messages, tools=None, **kwargs):
        self.last_messages = messages
        self.last_kwargs = kwargs
        return LLMResponse(
            content=f"reply-from-{self.label}",
            thinking=None,
            tool_calls=None,
            finish_reason="stop",
        )

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.last_messages = messages
        self.last_kwargs = kwargs
        yield StreamEvent(type="text", delta=f"reply-from-{self.label}")
        yield StreamEvent(type="finish", finish_reason="stop")

    def for_model(self, model: str, *, max_output_tokens: int | None = None):
        client = _DummyLLM(f"bound-{model}")
        client.model = model
        if max_output_tokens is not None:
            client.max_output_tokens = max_output_tokens
        return client


class _DummyConn:
    async def sessionUpdate(self, payload):
        pass


def _make_agent(tmp_path: Path, *, lite: bool):
    config = Config(
        llm=LLMConfig(api_key="main-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    main = _DummyLLM("main")
    if lite:
        lite_client = _DummyLLM("lite")
        agent = BoxACPAgent(
            _DummyConn(),
            config,
            main,
            [],
            "system",
            lite_llm=lite_client,
        )
        return agent, main, lite_client
    agent = BoxACPAgent(_DummyConn(), config, main, [], "system")
    return agent, main, main


def test_lite_llm_aliases_main_when_omitted(tmp_path):
    agent, main, lite = _make_agent(tmp_path, lite=False)
    assert agent._lite_llm is main
    assert agent._lite_llm is lite  # same object


def test_lite_llm_distinct_when_provided(tmp_path):
    agent, main, lite = _make_agent(tmp_path, lite=False)
    other = _DummyLLM("other")
    agent2 = BoxACPAgent(
        _DummyConn(),
        agent._config,
        main,
        [],
        "system",
        lite_llm=other,
    )
    assert agent2._llm is main
    assert agent2._lite_llm is other
    assert agent2._lite_llm is not main


def test_internal_model_resolver_uses_tags_only_with_auto_pool():
    manual = _DummyLLM("manual")
    locked, locked_diagnostic = resolve_model_client(
        manual,
        task="提炼会话标题",
    )
    assert locked is manual
    assert locked_diagnostic == {"mode": "inherit", "reason": "no_auto_model_pool"}

    automatic = _DummyLLM("automatic")
    automatic.auto_model_candidates = (
        {
            "model": "reasoning-model",
            "tags": ["analysis", "reasoning"],
            "abilityLevel": 3,
            "contextWindow": 180_000,
            "maxTokens": 16_000,
        },
        {
            "model": "summary-fast-model",
            "tags": ["summary", "fast"],
            "abilityLevel": 1,
            "contextWindow": 64_000,
            "maxTokens": 8_000,
        },
    )
    routed, diagnostic = resolve_model_client(
        automatic,
        task="提炼会话标题",
        max_output_tokens_cap=4_096,
    )
    assert routed.model == "summary-fast-model"
    assert routed.max_output_tokens == 4_096
    assert diagnostic["mode"] == "auto"
    assert diagnostic["selected_model"] == "summary-fast-model"

    large_context_client, large_context_diagnostic = resolve_model_client(
        automatic,
        task="压缩一个较大的会话上下文",
        max_output_tokens_cap=4_096,
        task_tags=("summary",),
        estimated_input_tokens=100_000,
    )
    assert large_context_client.model == "reasoning-model"
    assert large_context_client.max_output_tokens == 4_096
    assert large_context_diagnostic["estimated_input_tokens"] == 100_000


@pytest.mark.parametrize(
    ("task", "original_tag", "backend_tag", "expected_model"),
    [
        ("制作一个前端页面", "frontend", "html", "html-model"),
        ("制作一个 dashboard", "visualization", "analysis", "analysis-model"),
        ("整理 Excel 表格", "data-analysis", "analysis", "analysis-model"),
        ("制作一份 PPT", "presentation", "office", "office-model"),
        ("联网查资料", "research", "analysis", "analysis-model"),
        ("整理这份 PDF", "document", "office", "office-model"),
    ],
)
def test_task_tags_include_backend_catalog_aliases(
    task,
    original_tag,
    backend_tag,
    expected_model,
):
    selected, diagnostic = select_auto_model(
        [
            {"model": "generic-model", "tags": ["general"], "abilityLevel": 2},
            {"model": "html-model", "tags": ["html"], "abilityLevel": 3},
            {"model": "analysis-model", "tags": ["analysis"], "abilityLevel": 3},
            {"model": "office-model", "tags": ["office"], "abilityLevel": 2},
        ],
        task=task,
    )

    assert selected["model"] == expected_model
    assert original_tag in diagnostic["task_tags"]
    assert backend_tag in diagnostic["task_tags"]


def test_explicit_task_tags_include_backend_catalog_aliases():
    selected, diagnostic = select_auto_model(
        [
            {"model": "generic-model", "tags": ["general"], "abilityLevel": 2},
            {"model": "office-model", "tags": ["office"], "abilityLevel": 2},
        ],
        task="prepare slides",
        task_tags=("presentation",),
    )

    assert selected["model"] == "office-model"
    assert diagnostic["task_tags"] == ["office", "presentation"]


@pytest.mark.parametrize(
    ("task", "expected_ability"),
    [
        ("你好，帮我改写这句话", 1),
        ("制作一份产品介绍 PPT", 2),
        ("整理这份 PDF 合同", 2),
        ("帮我生成一张产品海报图片", 2),
        ("开发一个 React 项目", 3),
        ("联网调研 AI Agent 市场", 3),
        ("分析 Excel 数据并制作图表", 3),
        ("查看截图并排查报错", 3),
        ("证明这道数学题", 3),
    ],
)
def test_automatic_routing_assigns_three_ability_levels(task, expected_ability):
    selected, diagnostic = select_auto_model(
        [
            {"model": "light-model", "tags": ["general", "chat", "rewrite"], "abilityLevel": 1},
            {"model": "standard-model", "tags": ["office"], "abilityLevel": 2},
            {
                "model": "advanced-model",
                "tags": ["analysis", "code", "debug", "reasoning", "vision"],
                "abilityLevel": 3,
            },
        ],
        task=task,
    )

    assert selected is not None
    assert diagnostic["required_ability_level"] == expected_ability


def test_automatic_routing_rejects_missing_hard_vision_capability():
    with pytest.raises(ValueError, match="required capability: vision"):
        select_auto_model(
            [
                {"model": "text-model", "tags": ["analysis"], "abilityLevel": 3},
            ],
            task="识别这张截图的内容",
        )


def test_llm_client_for_model_preserves_endpoint_auth_and_default(tmp_path):
    auth_file = tmp_path / "auth.json"
    client = LLMClient(
        api_key="box-agent-no-auth",
        provider=LLMProvider.OPENAI,
        api_base="https://example.com/v1",
        model="model-main",
        auth_file=str(auth_file),
        max_output_tokens=1234,
        timeout=42,
    )

    bound = client.for_model("model-session")

    assert bound is not client
    assert client.model == "model-main"
    assert bound.model == "model-session"
    assert bound.api_base == client.api_base
    assert bound.api_key == client.api_key
    assert bound.auth_file == client.auth_file
    assert bound.max_output_tokens == client.max_output_tokens
    assert bound.timeout == client.timeout
    assert bound._client is not client._client
    assert bound._client.client is client._client.client
    assert client.for_model("model-main") is not client

    capped = client.for_model("model-session", max_output_tokens=63999)
    assert capped.max_output_tokens == 63999
    assert capped._client.max_output_tokens == 63999
    assert client.max_output_tokens == 1234


@pytest.mark.asyncio
async def test_conversation_sessions_bind_models_without_mutating_each_other(tmp_path):
    agent, main, _lite = _make_agent(tmp_path, lite=True)

    first = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "llm_binding": {"source": "builtin", "model": "model-normal"}
            },
        )
    )
    second = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "llm_binding": {
                    "source": "builtin",
                    "model": "model-lite",
                    "contextWindow": 128000,
                    "maxTokens": 16000,
                }
            },
        )
    )

    first_state = agent._sessions[first.sessionId]
    second_state = agent._sessions[second.sessionId]
    assert main.model == "model-main"
    assert first_state.agent.llm.model == "model-normal"
    assert second_state.agent.llm.model == "model-lite"
    assert first_state.agent.llm is not second_state.agent.llm
    assert first_state.llm_binding == {"source": "builtin", "model": "model-normal"}
    assert second_state.llm_binding == {
        "source": "builtin",
        "model": "model-lite",
        "contextWindow": 128000,
        "maxTokens": 16000,
    }
    assert second_state.agent.llm.max_output_tokens == 16000
    assert first_state.agent.token_limit == 104400
    assert second_state.agent.token_limit == 100800


@pytest.mark.asyncio
async def test_profile_bound_sessions_keep_distinct_endpoints(tmp_path, monkeypatch):
    registry = tmp_path / "model-profiles.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "revision-a": {
                        "profileId": "profile-a",
                        "profileRevision": "revision-a",
                        "provider": "openai",
                        "apiBase": "https://profile-a.example/v1",
                        "apiKey": "key-a",
                        "authFile": "",
                        "defaultModel": "model-a",
                        "contextWindow": 180000,
                        "maxTokens": 16000,
                    },
                    "revision-b": {
                        "profileId": "profile-b",
                        "profileRevision": "revision-b",
                        "provider": "openai",
                        "apiBase": "https://profile-b.example/v1",
                        "apiKey": "key-b",
                        "authFile": "",
                        "defaultModel": "model-b",
                        "contextWindow": 180000,
                        "maxTokens": 16000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))
    agent, _main, _lite = _make_agent(tmp_path, lite=True)

    first = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "llm_binding": {
                    "source": "profile",
                    "version": 2,
                    "profileId": "profile-a",
                    "profileRevision": "revision-a",
                    "routingMode": "manual",
                    "model": "model-a-selected",
                }
            },
        )
    )
    second = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "llm_binding": {
                    "source": "profile",
                    "version": 2,
                    "profileId": "profile-b",
                    "profileRevision": "revision-b",
                    "routingMode": "manual",
                    "model": "model-b-selected",
                }
            },
        )
    )

    first_llm = agent._sessions[first.sessionId].session_llm
    second_llm = agent._sessions[second.sessionId].session_llm
    assert (first_llm.api_base, first_llm.model) == (
        "https://profile-a.example/v1",
        "model-a-selected",
    )
    assert (second_llm.api_base, second_llm.model) == (
        "https://profile-b.example/v1",
        "model-b-selected",
    )


@pytest.mark.asyncio
async def test_session_binding_rejects_invalid_max_tokens(tmp_path):
    agent, _main, _lite = _make_agent(tmp_path, lite=True)

    with pytest.raises(ValueError, match="llm_binding.maxTokens is invalid"):
        await agent.newSession(
            SimpleNamespace(
                cwd=str(tmp_path),
                field_meta={
                    "llm_binding": {
                        "source": "builtin",
                        "model": "model-lite",
                        "maxTokens": 0,
                    }
                },
            )
        )

    with pytest.raises(ValueError, match="llm_binding.contextWindow is invalid"):
        await agent.newSession(
            SimpleNamespace(
                cwd=str(tmp_path),
                field_meta={
                    "llm_binding": {
                        "source": "builtin",
                        "model": "model-lite",
                        "contextWindow": 0,
                    }
                },
            )
        )

    with pytest.raises(
        ValueError,
        match="llm_binding.maxTokens must be smaller than contextWindow",
    ):
        await agent.newSession(
            SimpleNamespace(
                cwd=str(tmp_path),
                field_meta={
                    "llm_binding": {
                        "source": "builtin",
                        "model": "model-lite",
                        "contextWindow": 16000,
                        "maxTokens": 16000,
                    }
                },
            )
        )


@pytest.mark.asyncio
async def test_auto_session_exposes_allowlisted_models_to_sub_agent(tmp_path):
    agent, _main, _lite = _make_agent(tmp_path, lite=True)
    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "llm_binding": {
                    "source": "builtin",
                    "model": "model-normal",
                    "autoRouting": {
                        "models": [
                            {
                                "model": "model-normal",
                                "tags": ["code", "image"],
                                "abilityLevel": 2,
                                "contextWindow": 180000,
                            },
                            {
                                "model": "model-fast",
                                "tags": ["general", "fast"],
                                "abilityLevel": 1,
                                "maxTokens": 63999,
                            },
                        ]
                    },
                }
            },
        )
    )
    state = agent._sessions[session.sessionId]
    sub_agent = state.agent.tools["sub_agent"]

    assert state.session_llm.auto_model_candidates[0]["tags"] == ["code", "vision"]
    assert sub_agent._llm is state.session_llm
    assert len(sub_agent._llm.auto_model_candidates) == 2


@pytest.mark.asyncio
async def test_manual_session_keeps_sub_agent_on_parent_model(tmp_path):
    agent, _main, _lite = _make_agent(tmp_path, lite=True)
    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "llm_binding": {"source": "builtin", "model": "model-normal"}
            },
        )
    )
    state = agent._sessions[session.sessionId]

    assert state.session_llm.auto_model_candidates == ()
    assert state.agent.tools["sub_agent"]._llm is state.session_llm


@pytest.mark.asyncio
async def test_switching_to_manual_binding_clears_child_auto_routing(tmp_path):
    agent, _main, _lite = _make_agent(tmp_path, lite=True)
    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "llm_binding": {
                    "source": "builtin",
                    "model": "model-normal",
                    "autoRouting": {
                        "models": [
                            {
                                "model": "model-normal",
                                "tags": ["general"],
                                "abilityLevel": 2,
                            }
                        ]
                    },
                }
            },
        )
    )
    state = agent._sessions[session.sessionId]
    assert len(state.session_llm.auto_model_candidates) == 1

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "manual now"}],
            field_meta={
                "llm_binding": {"source": "builtin", "model": "model-normal"}
            },
        )
    )

    assert state.session_llm.auto_model_candidates == ()


@pytest.mark.asyncio
async def test_session_switches_model_between_turns_without_recreating_agent(tmp_path):
    agent, main, _lite = _make_agent(tmp_path, lite=True)
    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "llm_binding": {"source": "builtin", "model": "model-normal"}
            },
        )
    )
    state = agent._sessions[session.sessionId]
    original_agent = state.agent
    original_session_llm = state.session_llm

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "hello"}],
            field_meta={
                "llm_binding": {
                    "source": "builtin",
                    "model": "model-lite",
                    "contextWindow": 128000,
                    "maxTokens": 16000,
                }
            },
        )
    )

    assert agent._sessions[session.sessionId] is state
    assert state.agent is original_agent
    assert state.session_llm is original_session_llm
    assert state.agent.llm is original_session_llm
    assert state.agent.llm.model == "model-lite"
    assert state.llm_binding == {
        "source": "builtin",
        "model": "model-lite",
        "contextWindow": 128000,
        "maxTokens": 16000,
    }
    assert state.agent.llm.max_output_tokens == 16000
    assert state.agent.token_limit == 100800
    assert main.model == "model-main"


@pytest.mark.asyncio
async def test_llm_prompt_without_session_binding_inherits_main_client(tmp_path):
    agent, main, lite = _make_agent(tmp_path, lite=True)
    result = await agent._llm_prompt({"prompt": "title this"})
    assert "error" not in result
    assert result["text"] == "reply-from-main"
    assert main.last_messages is not None
    assert lite.last_messages is None


@pytest.mark.asyncio
async def test_llm_prompt_threads_meta_session_id_to_main_client_without_binding(tmp_path):
    agent, main, lite = _make_agent(tmp_path, lite=True)
    result = await agent._llm_prompt({
        "prompt": "title this",
        "_meta": {"session_id": "office-session-1"},
    })

    assert "error" not in result
    assert main.last_kwargs["session_id"] == "office-session-1"
    assert lite.last_kwargs is None


@pytest.mark.asyncio
async def test_llm_prompt_routes_auto_by_tags_and_locks_explicit_binding(tmp_path):
    agent, _main, _lite = _make_agent(tmp_path, lite=True)
    automatic = await agent._llm_prompt(
        {
            "prompt": "请为复杂的 PPT 数据分析、研究和代码任务提炼一个会话标题",
            "_meta": {
                "purpose": "title",
                "llm_binding": {
                    "source": "builtin",
                    "model": "analysis-model",
                    "autoRouting": {
                        "models": [
                            {
                                "model": "analysis-model",
                                "tags": ["analysis", "reasoning"],
                                "abilityLevel": 3,
                            },
                            {
                                "model": "summary-fast-model",
                                "tags": ["summary", "fast"],
                                "abilityLevel": 1,
                            },
                        ]
                    },
                },
            },
        }
    )
    assert automatic["text"] == "reply-from-bound-summary-fast-model"

    manual = await agent._llm_prompt(
        {
            "prompt": "请提炼一个会话标题",
            "_meta": {
                "purpose": "title",
                "llm_binding": {
                    "source": "builtin",
                    "model": "explicit-model",
                },
            },
        }
    )
    assert manual["text"] == "reply-from-bound-explicit-model"


def test_config_lite_llm_absent_marks_not_present():
    cfg = LiteLLMConfig()
    assert cfg._present is False


def test_config_lite_llm_parses_when_block_present(tmp_path):
    yaml_text = {
        "api_key": "main-key",
        "api_base": "https://api.anthropic.com",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "lite_llm": {
            "provider": "openai",
            "api_base": "https://api.openai.com/v1",
            "api_key": "lite-key",
            "model": "gpt-4o-mini",
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(yaml_text), encoding="utf-8")
    cfg = Config.from_yaml(config_path)
    assert cfg.lite_llm._present is True
    assert cfg.lite_llm.api_base == "https://api.openai.com/v1"
    assert cfg.lite_llm.api_key == "lite-key"
    assert cfg.lite_llm.model == "gpt-4o-mini"
    assert cfg.lite_llm.provider == "openai"


def test_config_lite_llm_block_absent_keeps_default(tmp_path):
    yaml_text = {
        "api_key": "main-key",
        "api_base": "https://api.anthropic.com",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(yaml_text), encoding="utf-8")
    cfg = Config.from_yaml(config_path)
    assert cfg.lite_llm._present is False
    assert cfg.lite_llm.api_base == ""


def test_config_lite_llm_requires_api_base(tmp_path):
    yaml_text = {
        "api_key": "main-key",
        "api_base": "https://api.anthropic.com",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "lite_llm": {"provider": "openai", "api_key": "x", "model": "y"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(yaml_text), encoding="utf-8")
    with pytest.raises(ValueError, match="lite_llm.api_base"):
        Config.from_yaml(config_path)


def test_lite_llm_default_max_output_tokens():
    cfg = LiteLLMConfig()
    assert cfg.max_output_tokens == 63999


def test_config_lite_llm_max_output_tokens_parsed(tmp_path):
    yaml_text = {
        "api_key": "main-key",
        "api_base": "https://api.anthropic.com",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "lite_llm": {
            "provider": "openai",
            "api_base": "https://api.openai.com/v1",
            "api_key": "lite-key",
            "model": "gpt-4o-mini",
            "max_output_tokens": 32000,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(yaml_text), encoding="utf-8")
    cfg = Config.from_yaml(config_path)
    assert cfg.lite_llm.max_output_tokens == 32000


def test_config_lite_llm_max_output_tokens_rejects_ceiling(tmp_path):
    yaml_text = {
        "api_key": "main-key",
        "api_base": "https://api.anthropic.com",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "lite_llm": {
            "provider": "openai",
            "api_base": "https://api.openai.com/v1",
            "api_key": "lite-key",
            "model": "gpt-4o-mini",
            "max_output_tokens": 65537,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(yaml_text), encoding="utf-8")
    with pytest.raises(ValueError, match="65536 ceiling"):
        Config.from_yaml(config_path)
