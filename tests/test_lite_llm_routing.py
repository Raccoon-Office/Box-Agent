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


@pytest.mark.parametrize("vision_context,available", [(32_000, True), (1_000, False)])
def test_optional_vision_capacity_does_not_abort_text_session_setup(tmp_path, vision_context, available):
    from box_agent.tools.setup import add_workspace_tools

    llm = _DummyLLM("text")
    llm.max_output_tokens = 64_000
    llm.capabilities = {"image_input": False}
    llm.auto_model_candidates = (
        {"model": llm.model, "tags": ["code"], "abilityLevel": 4,
         "contextWindow": 180_000, "maxTokens": 64_000},
        {"model": "vision", "tags": ["vision"], "abilityLevel": 4,
         "contextWindow": vision_context},
    )
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(), tools=ToolsConfig(
        enable_bash=False, enable_todo=False, enable_sub_agent=False,
    ))
    tools = []
    add_workspace_tools(tools, config, tmp_path, llm=llm, output=lambda *a: None)
    vision = next((tool for tool in tools if tool.name == "inspect_images"), None)
    assert (vision is not None) is available
    if available:
        assert vision.llm.model == "vision"
        assert vision.llm.max_output_tokens == 8192
    assert llm.max_output_tokens == 64_000 and llm.model == "model-text"
    assert any(tool.name == "read_file" for tool in tools)


def test_routing_diagnostic_explains_scores_and_each_exclusion_without_task_contents():
    selected, diagnostic = select_auto_model([
        {"model": "small", "tags": ["code"], "abilityLevel": 1, "contextWindow": 1_000},
        {"model": "code", "tags": ["code"], "abilityLevel": 3, "contextWindow": 20_000},
        {"model": "other", "tags": [], "abilityLevel": 3, "contextWindow": 20_000},
    ], task="编写代码 private-task-marker", estimated_input_tokens=900)
    assert selected["model"] == "code"
    assert diagnostic["reason"] == "highest_score"
    assert diagnostic["schema_version"] == 1
    assert diagnostic["candidates"][0]["rejection_reasons"] == ["context_capacity", "ability"]
    assert diagnostic["candidates"][0]["score"] is None
    assert diagnostic["candidates"][1]["score"] == 160
    assert diagnostic["candidates"][1]["selected"] is True
    assert diagnostic["candidates"][2]["score"] == 60
    assert "private-task-marker" not in json.dumps(diagnostic)


def test_routing_failure_carries_the_exclusion_diagnostic():
    with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED") as caught:
        select_auto_model([
            {"model": "small", "tags": [], "abilityLevel": 3, "contextWindow": 1_000},
            {"model": "unknown", "tags": ["vision"], "abilityLevel": 3},
        ], task="private-task-marker", estimated_input_tokens=900, task_tags=["vision"])
    diagnostic = caught.value.diagnostic
    assert diagnostic["reason"] == "no_context_candidate"
    assert diagnostic["status"] == "rejected"
    assert diagnostic["candidates"][0]["rejection_reasons"] == ["context_capacity", "vision"]
    assert diagnostic["candidates"][1]["rejection_reasons"] == ["context_unknown"]
    assert "private-task-marker" not in json.dumps(diagnostic)


def test_routing_records_correlated_decision_and_binding_in_session_trace(tmp_path):
    from box_agent.session_trace import SessionTraceWriter, set_session_trace_writer, reset_session_trace_writer
    writer = SessionTraceWriter(session_id="routing-session", acp_session_id="acp-routing", trace_dir=tmp_path, enabled=True)
    token = set_session_trace_writer(writer, turn_id="turn-routing")
    llm = _DummyLLM("automatic")
    llm.auto_model_candidates = [{"model": "chosen", "tags": [], "abilityLevel": 3, "contextWindow": 20_000, "maxTokens": 1_000}]
    try:
        client, _ = resolve_model_client(llm, task="private-task-marker")
    finally:
        reset_session_trace_writer(token)
    records = [json.loads(line) for line in writer.file_path.read_text().splitlines()]
    assert [item["event"] for item in records] == ["model.routing", "model.routing"]
    assert [item["data"]["phase"] for item in records] == ["decision", "bound"]
    assert len({item["data"]["routing_id"] for item in records}) == 1
    assert all(item["turn_id"] == "turn-routing" and item["session_id"] == "routing-session" for item in records)
    assert records[-1]["data"]["actual_model"] == client.model == "chosen"
    assert "private-task-marker" not in json.dumps(records)


def test_routing_diagnostics_bound_candidates_and_keep_the_winner():
    pool = [{"model": f"model-{index}", "tags": ["code"] if index == 70 else [], "abilityLevel": 3} for index in range(71)]
    selected, diagnostic = select_auto_model(pool, task="code")
    assert selected["model"] == "model-70"
    assert diagnostic["candidate_count"] == 71
    assert diagnostic["candidates_truncated"] == 7
    assert len(diagnostic["candidates"]) == 64
    assert diagnostic["candidates"][-1]["selected"] is True
    selected, diagnostic = select_auto_model(pool[:2], task="hello")
    assert selected["model"] == "model-0"
    assert diagnostic["reason"] == "catalog_order_tiebreak"


def test_routing_logging_failure_does_not_change_the_result(monkeypatch):
    from box_agent.llm import model_routing
    import box_agent.session_trace as trace
    def fail(*args, **kwargs):
        raise OSError("fixture private-log-error")
    monkeypatch.setattr(model_routing.logger, "info", fail)
    monkeypatch.setattr(trace, "emit_session_trace", fail)
    llm = _DummyLLM("automatic")
    llm.auto_model_candidates = [{"model": "chosen", "tags": [], "abilityLevel": 3}]
    routed, _ = resolve_model_client(llm, task="hello")
    assert routed.model == "chosen"


def test_routing_logs_rejections_without_a_trace_context_or_stdout(caplog, capsys):
    llm = _DummyLLM("automatic")
    llm.auto_model_candidates = [
        {"model": "small", "tags": [], "abilityLevel": 3, "contextWindow": 1_000}
    ]
    with caplog.at_level("INFO", logger="box_agent.llm.model_routing"):
        with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED"):
            resolve_model_client(llm, task="private-task-marker", estimated_input_tokens=900)
    records = [
        json.loads(record.message.removeprefix("model.routing "))
        for record in caplog.records if record.name == "box_agent.llm.model_routing"
    ]
    assert len(records) == 1
    assert records[0]["phase"] == "decision"
    assert records[0]["decision"]["reason"] == "no_context_candidate"
    assert "private-task-marker" not in json.dumps(records)
    assert capsys.readouterr().out == ""


def test_routing_inheritance_records_actual_model_and_distinct_attempts(caplog):
    llm = _DummyLLM("manual")
    with caplog.at_level("INFO", logger="box_agent.llm.model_routing"):
        for _ in range(2):
            client, diagnostic = resolve_model_client(llm, task="hello")
            assert client is llm
            assert diagnostic == {"mode": "inherit", "reason": "no_auto_model_pool"}
    records = [
        json.loads(record.message.removeprefix("model.routing "))
        for record in caplog.records if record.name == "box_agent.llm.model_routing"
    ]
    assert [record["phase"] for record in records] == ["decision", "inherited"] * 2
    assert records[0]["routing_id"] == records[1]["routing_id"]
    assert records[2]["routing_id"] == records[3]["routing_id"]
    assert records[0]["routing_id"] != records[2]["routing_id"]
    assert records[1]["actual_model"] == records[3]["actual_model"] == "model-manual"


@pytest.mark.parametrize("failure", ["capacity", "binding", "unexpected"])
def test_routing_failure_trace_never_reports_a_successful_binding(tmp_path, monkeypatch, failure):
    from box_agent.session_trace import SessionTraceWriter, set_session_trace_writer, reset_session_trace_writer
    writer = SessionTraceWriter(session_id="routing-session", acp_session_id="acp-routing", trace_dir=tmp_path, enabled=True)
    token = set_session_trace_writer(writer, turn_id="turn-failed")
    llm = _DummyLLM("automatic")
    binding_failure = failure != "capacity"
    llm.auto_model_candidates = [{"model": "chosen", "tags": [], "abilityLevel": 3, "contextWindow": 2_000 if binding_failure else 1_000, "maxTokens": 100}]
    if binding_failure:
        def fail(*args, **kwargs):
            error_type = RuntimeError if failure == "unexpected" else ValueError
            raise error_type("private-provider-secret")
        monkeypatch.setattr(llm, "for_model", fail)
    try:
        with pytest.raises(RuntimeError if failure == "unexpected" else ValueError):
            resolve_model_client(llm, task="private-task-marker", estimated_input_tokens=900)
    finally:
        reset_session_trace_writer(token)
    records = [json.loads(line) for line in writer.file_path.read_text().splitlines()]
    assert [item["data"]["phase"] for item in records] == (["decision", "binding_failed"] if binding_failure else ["decision"])
    assert all("actual_model" not in item["data"] for item in records)
    assert "private-provider-secret" not in json.dumps(records)
    assert "private-task-marker" not in json.dumps(records)


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


@pytest.mark.parametrize("tokens, expected", [(800, "small"), (801, "large"), (1_600, "large")])
def test_auto_routing_obeys_input_capacity_boundary(tokens, expected):
    selected, diagnostic = select_auto_model(
        [
            {"model": "small", "tags": ["fast"], "abilityLevel": 3, "contextWindow": 1_000},
            {"model": "large", "tags": [], "abilityLevel": 3, "contextWindow": 2_000},
        ],
        task="hello",
        estimated_input_tokens=tokens,
    )
    assert selected["model"] == expected
    assert diagnostic["context_capacity"] == "estimated"


@pytest.mark.parametrize("explicit_estimate", [False, True])
@pytest.mark.parametrize("include_unknown", [False, True])
def test_auto_routing_rejects_insufficient_capacity_without_largest_or_unknown_fallback(
    explicit_estimate, include_unknown,
):
    candidates = [
        {"model": "small", "tags": [], "abilityLevel": 3, "contextWindow": 1_000},
        {"model": "large", "tags": [], "abilityLevel": 3, "contextWindow": 2_000},
    ]
    if include_unknown:
        candidates.append({"model": "unknown", "tags": ["fast"], "abilityLevel": 3})
    with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED"):
        select_auto_model(
            candidates,
            task="x" * 3_202,
            **({"estimated_input_tokens": 1_601} if explicit_estimate else {}),
        )


def test_auto_routing_prefers_known_capacity_and_marks_all_unknown_legacy_pools():
    unknown = {"model": "unknown", "tags": ["fast"], "abilityLevel": 3}
    known = {"model": "known", "tags": [], "abilityLevel": 3, "contextWindow": 1_000}
    selected, diagnostic = select_auto_model([unknown, known], task="hello")
    assert selected["model"] == "known"
    assert diagnostic["context_capacity"] == "estimated"
    selected, diagnostic = select_auto_model([unknown], task="hello", estimated_input_tokens=50_000)
    assert selected["model"] == "unknown"
    assert diagnostic["context_capacity"] == "unknown"


@pytest.mark.parametrize("constraint", ["vision", "ability"])
def test_auto_routing_does_not_relax_capabilities_to_fit_input(constraint):
    with pytest.raises(ValueError, match=constraint):
        select_auto_model(
            [
                {"model": "small", "tags": ["vision"], "abilityLevel": 3, "contextWindow": 1_000},
                {"model": "large", "tags": [] if constraint == "vision" else ["vision"],
                 "abilityLevel": 1 if constraint == "ability" else 3, "contextWindow": 2_000},
            ],
            task="hello", task_tags=["vision"], required_ability_level=3,
            estimated_input_tokens=801,
        )


def test_model_resolver_stops_before_cloning_when_no_candidate_can_fit(monkeypatch):
    llm = _DummyLLM("automatic")
    llm.auto_model_candidates = [
        {"model": "small", "tags": [], "abilityLevel": 3, "contextWindow": 1_000},
    ]
    calls = []
    monkeypatch.setattr(llm, "for_model", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED"):
        resolve_model_client(llm, task="hello", estimated_input_tokens=801)
    assert calls == []
    assert llm.model == "model-automatic"


@pytest.mark.parametrize("output, budget", [(100, 810), (400, 540)])
def test_auto_routing_reserves_output_and_the_agent_safety_margin(output, budget):
    candidates = [{"model": "small", "tags": [], "abilityLevel": 3,
                   "contextWindow": 1_000, "maxTokens": output}]
    selected, diagnostic = select_auto_model(candidates, task="hello", estimated_input_tokens=budget)
    assert selected["model"] == "small"
    assert diagnostic["input_token_budget"] == budget
    with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED"):
        select_auto_model(candidates, task="hello", estimated_input_tokens=budget + 1)


@pytest.mark.parametrize("output", [1_000, 1_001])
def test_auto_routing_rejects_output_limits_that_leave_no_input_capacity(output):
    with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED"):
        select_auto_model(
            [{"model": "small", "tags": [], "abilityLevel": 3,
              "contextWindow": 1_000, "maxTokens": output}],
            task="a",
        )


def test_routing_uses_the_actual_auxiliary_output_cap_without_changing_the_pool():
    llm = _DummyLLM("automatic")
    candidate = {"model": "large", "tags": ["summary"], "abilityLevel": 3,
                 "contextWindow": 1_000, "maxTokens": 400}
    llm.auto_model_candidates = [candidate]
    routed, diagnostic = resolve_model_client(
        llm, task="hello", estimated_input_tokens=800, max_output_tokens_cap=100,
    )
    assert routed.model == "large"
    assert routed.max_output_tokens == 100
    assert diagnostic["input_token_budget"] == 810
    assert candidate["maxTokens"] == 400
    with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED"):
        resolve_model_client(llm, task="hello", estimated_input_tokens=811, max_output_tokens_cap=100)


def test_routing_reserves_the_inherited_output_limit_when_the_catalog_omits_it():
    llm = _DummyLLM("automatic")
    llm.max_output_tokens = 200
    llm.auto_model_candidates = [{"model": "small", "tags": [], "abilityLevel": 3, "contextWindow": 1_000}]
    routed, diagnostic = resolve_model_client(llm, task="hello", estimated_input_tokens=700, max_output_tokens_cap=800)
    assert routed.max_output_tokens == 200
    assert diagnostic["input_token_budget"] == 720
    with pytest.raises(ValueError, match="AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED"):
        resolve_model_client(llm, task="hello", estimated_input_tokens=721, max_output_tokens_cap=800)


@pytest.mark.parametrize("failure", ["unavailable", "raises"])
def test_capacity_checked_routing_does_not_fall_back_to_the_original_client_on_binding_failure(monkeypatch, failure):
    llm = _DummyLLM("small")
    llm.max_output_tokens = 100
    llm.auto_model_candidates = [
        {"model": "model-small", "tags": [], "abilityLevel": 3, "contextWindow": 1_000, "maxTokens": 100},
        {"model": "large", "tags": [], "abilityLevel": 3, "contextWindow": 2_000, "maxTokens": 100},
    ]
    def fail(*args, **kwargs):
        raise ValueError("fixture binding rejected")
    monkeypatch.setattr(llm, "for_model", None if failure == "unavailable" else fail)
    with pytest.raises(ValueError, match="AUTO_MODEL_BINDING_FAILED"):
        resolve_model_client(llm, task="hello", estimated_input_tokens=900)
    assert llm.model == "model-small"


@pytest.mark.asyncio
async def test_acp_session_with_large_context_can_prepare_its_bounded_compaction_model(tmp_path, monkeypatch):
    agent, main, _ = _make_agent(tmp_path, lite=True)
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    binding = {
        "source": "builtin", "model": "large", "contextWindow": 1_000_000, "maxTokens": 100_000,
        "autoRouting": {"models": [{
            "model": "large", "tags": ["summary"], "abilityLevel": 3,
            "contextWindow": 1_000_000, "maxTokens": 100_000,
        }]},
    }
    try:
        session = await agent.newSession(SimpleNamespace(
            cwd=str(tmp_path), field_meta={"llm_binding": binding},
        ))
        state = agent._sessions[session.sessionId]
        assert state.agent.token_limit == 810_000
        assert state.summary_llm.model == "large"
        assert state.summary_llm.max_output_tokens == 8_192
        assert main.last_messages is None
    finally:
        await agent.aclose()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["title", "summary", "rewrite", "expert", "other"])
async def test_host_default_routes_unbound_utilities_without_using_process_model(tmp_path, purpose):
    agent, main, _ = _make_agent(tmp_path, lite=True)
    await agent.initialize(SimpleNamespace(field_meta={
        "llm_binding": {"source": "builtin", "model": "visible-model"},
    }))
    result = await agent._llm_prompt({"prompt": "hello", "_meta": {"purpose": purpose}})
    assert result["text"] == "reply-from-bound-visible-model"
    assert main.last_messages is None


@pytest.mark.asyncio
async def test_host_default_auto_pool_is_used_by_titles_and_unbound_sessions(tmp_path, monkeypatch):
    agent, main, _ = _make_agent(tmp_path, lite=True)
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    binding = {
        "source": "builtin", "model": "analysis-model",
        "autoRouting": {"models": [
            {"model": "analysis-model", "tags": ["analysis"], "abilityLevel": 3},
            {"model": "summary-model", "tags": ["summary", "fast"], "abilityLevel": 1},
        ]},
    }
    await agent.initialize(SimpleNamespace(field_meta={"llm_binding": binding}))
    result = await agent._llm_prompt({"prompt": "hello", "_meta": {"purpose": "title"}})
    assert result["text"] == "reply-from-bound-summary-model"
    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
    state = agent._sessions[session.sessionId]
    assert state.session_llm.model == "analysis-model"
    assert len(state.session_llm.auto_model_candidates) == 2
    assert agent._summary_llm_for_session(
        session_llm=state.session_llm, session_id="test", title="test", client_info=None,
    ).model == "summary-model"
    assert main.last_messages is None
    await agent.aclose()


@pytest.mark.asyncio
async def test_session_utilities_and_compaction_keep_their_model_after_default_changes(tmp_path, monkeypatch):
    agent, main, _ = _make_agent(tmp_path, lite=True)
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    await agent.initialize(SimpleNamespace(field_meta={
        "llm_binding": {"source": "builtin", "model": "default-model"},
    }))
    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={
        "session_id": "original-session",
        "llm_binding": {"source": "builtin", "model": "session-model"},
    }))
    state = agent._sessions[session.sessionId]
    result = await agent._llm_prompt({"prompt": "title", "_meta": {
        "session_id": "original-session", "purpose": "title",
    }})
    assert result["text"] == "reply-from-bound-session-model"
    assert agent._summary_llm_for_session(
        session_llm=state.session_llm, session_id="test", title="test", client_info=None,
    ).model == "session-model"
    result = await agent._llm_prompt({"prompt": "title", "_meta": {
        "purpose": "title", "llm_binding": {"source": "builtin", "model": "new-default"},
    }})
    assert result["text"] == "reply-from-bound-new-default"
    assert state.session_llm.model == "session-model"
    assert main.last_messages is None
    await agent.aclose()


@pytest.mark.asyncio
async def test_memory_bootstrap_waits_for_host_binding_and_starts_once(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    agent, main, _ = _make_agent(tmp_path, lite=True)
    memory = SimpleNamespace(import_openclaw=AsyncMock())
    agent._memory = memory
    agent._config.agent.memory_maintainer_enabled = True
    maintenance = AsyncMock(return_value=True)
    captured = []

    def maintainer(memory_arg, config, *, llm):
        captured.append(llm.model)
        return SimpleNamespace(run_if_due=maintenance)

    monkeypatch.setattr("box_agent.memory_maintainer.MemoryMaintainer", maintainer)
    assert agent._memory_bootstrap_task is None
    await agent.initialize(SimpleNamespace(field_meta={
        "llm_binding": {"source": "builtin", "model": "visible-model"},
    }))
    await agent._memory_bootstrap_task
    agent._start_memory_bootstrap()
    memory.import_openclaw.assert_awaited_once()
    assert memory.import_openclaw.call_args.args[0].model == "visible-model"
    assert captured == ["visible-model"]
    maintenance.assert_awaited_once()
    assert main.last_messages is None
    await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_memory_bootstrap_releases_its_profile_client_on_completion_or_shutdown(tmp_path, monkeypatch, cancel):
    import asyncio
    from unittest.mock import AsyncMock

    agent, main, _ = _make_agent(tmp_path, lite=True)
    client = _DummyLLM("profile")
    client.aclose = AsyncMock()
    main.aclose = AsyncMock()
    monkeypatch.setattr("box_agent.acp.client_for_model_profile", lambda *args, **kwargs: client)
    started = asyncio.Event()

    async def import_memory(llm):
        started.set()
        if cancel:
            await asyncio.Event().wait()

    agent._memory = SimpleNamespace(import_openclaw=import_memory)
    agent._config.agent.memory_maintainer_enabled = False
    await agent.initialize(SimpleNamespace(field_meta={"llm_binding": {
        "source": "profile", "version": 2, "profileId": "profile",
        "profileRevision": "revision", "routingMode": "manual", "model": "visible-model",
    }}))
    await started.wait()
    if not cancel:
        await agent._memory_bootstrap_task
    await agent.aclose()
    client.aclose.assert_awaited_once()
    main.aclose.assert_not_awaited()


@pytest.mark.parametrize("source", ["builtin", "profile"])
@pytest.mark.parametrize("snake_case", [False, True])
@pytest.mark.parametrize("pool", [
    {"models": [{"model": "other-model", "tags": ["summary"], "abilityLevel": 1}]},
    {"models": []},
    "invalid-pool",
])
def test_manual_binding_ignores_even_malformed_auto_pool(source, snake_case, pool):
    from box_agent.acp import _normalize_llm_binding

    binding = {
        "source": source, "model": "chosen-model", "version": 2,
        "profileId": "hosted", "profileRevision": "revision",
        "routing_mode" if snake_case else "routingMode": "manual",
        "auto_routing" if snake_case else "autoRouting": pool,
    }
    normalized = _normalize_llm_binding({"llm_binding": binding})
    assert normalized["model"] == "chosen-model"
    assert "autoRouting" not in normalized
    if source == "profile":
        assert normalized["routingMode"] == "manual"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["builtin", "profile"])
async def test_manual_pool_cannot_switch_sessions_children_or_utilities(tmp_path, monkeypatch, source):
    agent, main, _ = _make_agent(tmp_path, lite=True)
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    monkeypatch.setattr(
        "box_agent.acp.client_for_model_profile",
        lambda binding, **kwargs: main.for_model(binding["model"]),
    )
    binding = {
        "source": source, "version": 2, "profileId": "hosted",
        "profileRevision": "revision", "routingMode": "manual", "model": "chosen-model",
        "autoRouting": {"models": [
            {"model": "other-model", "tags": ["summary", "code"], "abilityLevel": 3},
        ]},
    }
    try:
        await agent.initialize(SimpleNamespace(field_meta={"llm_binding": binding}))
        result = await agent._llm_prompt({"prompt": "summarize", "_meta": {"purpose": "title"}})
        assert result["text"] == "reply-from-bound-chosen-model"
        session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
        state = agent._sessions[session.sessionId]
        assert state.session_llm.model == "chosen-model"
        assert state.session_llm.auto_model_candidates == ()
        assert state.agent.tools["sub_agent"]._llm is state.session_llm
        assert agent._summary_llm_for_session(
            session_llm=state.session_llm, session_id="test", title="test", client_info=None,
        ).model == "chosen-model"

        # First enable automatic routing, then reuse its pool with an explicit manual choice.
        auto = {**binding, "routingMode": "auto"}
        await agent.prompt(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{"text": "hello"}],
            field_meta={"llm_binding": auto},
        ))
        assert len(state.session_llm.auto_model_candidates) == 1
        await agent.prompt(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{"text": "debug this complex code"}],
            field_meta={"llm_binding": binding},
        ))
        assert state.session_llm.model == "chosen-model"
        assert state.session_llm.auto_model_candidates == ()
        result = await agent._llm_prompt({
            "prompt": "summarize", "_meta": {"purpose": "summary", "llm_binding": binding},
        })
        assert result["text"] == "reply-from-bound-chosen-model"
    finally:
        await agent.aclose()
