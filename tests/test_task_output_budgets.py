"""Auxiliary budgets are isolated from execution, routing, and durable memory."""

from copy import copy
from datetime import datetime, timezone
import json

import anthropic
import httpx
import pytest

from box_agent.cli import _probe_llm_api
from box_agent.config import AgentConfig
from box_agent.kernel.context_engine import _create_summary
from box_agent.llm.lightweight import LightweightPromptError, run_lightweight_prompt
from box_agent.llm.model_routing import resolve_model_client
from box_agent.llm import LLMClient
from box_agent.memory import MemoryExtractor, MemoryManager
from box_agent.memory_maintainer import MemoryMaintainer
from box_agent.mcp_servers.web_extract import WebExtractTool
from box_agent.schema import LLMResponse, Message, StreamEvent
from box_agent.tools.file.read_tool import ReadTool
from box_agent.tools.sub_agent_tool import SubAgentTool
from box_agent.tools.image_inspection_tool import ImageInspectionTool
from box_agent.turn_continuation import model_says_continue
from tests.test_image_inspection_tool import _ONE_PIXEL_PNG
from tests.test_lightweight_prompt import _StubAgent


class BudgetLLM:
    model = "test-model"
    provider = "openai"

    def __init__(self, content="done", finish_reason="stop", max_output_tokens=100_000):
        self.content = content
        self.finish_reason = finish_reason
        self.max_output_tokens = max_output_tokens
        self.calls = []

    def for_model(self, model, *, max_output_tokens=None):
        cloned = copy(self)
        cloned.model = model
        if max_output_tokens is not None:
            cloned.max_output_tokens = max_output_tokens
        return cloned

    def _record(self, kwargs):
        self.calls.append({"max_tokens": self.max_output_tokens, "model": self.model, **kwargs})

    async def generate(self, messages=None, tools=None, **kwargs):
        self._record(kwargs)
        return LLMResponse(content=self.content, finish_reason=self.finish_reason)

    async def generate_stream(self, messages=None, tools=None, **kwargs):
        self._record(kwargs)
        yield StreamEvent(type="text", delta=self.content)
        yield StreamEvent(type="finish", finish_reason=self.finish_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose, expected", [
    ("title", 8_000), ("local_agent_title", 8_000),
    ("openclaw-memory", 8_192), ("questionnaire-memory", 8_192),
    ("tag-extract", 8_192), ("voice_transcript_cleanup", 8_192),
    ("voice_summary", 8_192), ("local_conversation_context_summary", 8_192),
    ("local_share_summary", 8_192), ("expert-draft", 8_192),
    ("schedule-draft", 8_192), ("summary", 8_192), ("rewrite", 8_192),
    ("unknown-purpose", 100_000),
])
async def test_known_lightweight_purposes_cap_only_their_own_request(purpose, expected):
    llm = BudgetLLM()
    result = await _StubAgent(llm).extMethod(
        "llm/prompt", {"prompt": "hello", "_meta": {"purpose": purpose}},
    )
    assert result["text"] == "done"
    assert llm.calls[-1]["max_tokens"] == expected
    assert llm.calls[-1]["thinking_enabled"] is False
    await llm.generate()
    assert llm.calls[-1]["max_tokens"] == 100_000


@pytest.mark.asyncio
@pytest.mark.parametrize("size, expected", [(100, 8_192), (4_000, 16_384), (10_000, 100_000)])
async def test_full_text_transforms_allow_longer_outputs_without_truncating_input(size, expected):
    llm = BudgetLLM()
    result = await _StubAgent(llm).extMethod("llm/prompt", {
        "prompt": "文" * size, "_meta": {"purpose": "voice_transcript_cleanup"},
    })
    assert result["text"] == "done"
    assert llm.calls[-1]["max_tokens"] == expected
    assert llm.max_output_tokens == 100_000


@pytest.mark.parametrize("routing", ["manual", "auto", "auto_without_limit"])
def test_auxiliary_cap_preserves_smaller_model_limits_and_the_execution_client(routing):
    llm = BudgetLLM(max_output_tokens=2_048)
    if routing != "manual":
        llm.auto_model_candidates = [{
            "model": "fast", "tags": ["summary"], "abilityLevel": 1,
            **({"maxTokens": 1_024} if routing == "auto" else {}),
        }]
    bounded, _ = resolve_model_client(
        llm, task="summary", task_tags=("summary",), max_output_tokens_cap=8_192,
    )
    assert bounded is not llm
    assert bounded.max_output_tokens == (1_024 if routing == "auto" else 2_048)
    assert bounded.model == (llm.model if routing == "manual" else "fast")
    assert llm.max_output_tokens == 2_048


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["stop", "length", "max_tokens"])
async def test_summary_caps_its_request_and_rejects_truncated_envelopes(finish_reason):
    llm = BudgetLLM("<summary>valid-looking partial summary</summary>", finish_reason)
    if finish_reason == "stop":
        assert await _create_summary(llm, [Message(role="user", content="hello")], 1)
    else:
        with pytest.raises(RuntimeError, match="output token limit"):
            await _create_summary(llm, [Message(role="user", content="hello")], 1)
    assert llm.calls[-1]["max_tokens"] == 8_192
    assert llm.max_output_tokens == 100_000


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "max_tokens"])
async def test_lightweight_rejects_provider_truncation_even_with_valid_json(finish_reason):
    with pytest.raises(LightweightPromptError) as error:
        await run_lightweight_prompt(BudgetLLM('{"value":"partial"}', finish_reason), "hello")
    assert error.value.code == "output_truncated"


@pytest.mark.asyncio
async def test_cli_probe_caps_only_the_probe():
    llm = BudgetLLM()
    await _probe_llm_api(llm)
    assert llm.calls[-1]["max_tokens"] == 8_192
    assert llm.max_output_tokens == 100_000


@pytest.mark.asyncio
async def test_memory_extraction_rejects_truncated_json_without_saving(tmp_path):
    manager = MemoryManager(memory_dir=str(tmp_path))
    manager.write_core("- original core")
    llm = BudgetLLM('{"core_additions":["- must not save partial output"]}', "length")
    extractor = MemoryExtractor(llm=llm, memory_manager=manager)
    assert not await extractor.maybe_extract(
        [Message(role="user", content="Remember I prefer concise replies")], "loop_end",
    )
    assert llm.calls[-1]["max_tokens"] == 8_192
    assert manager.read_core() == "- original core"
    assert manager.read_context() == ""
    assert llm.max_output_tokens == 100_000


@pytest.mark.asyncio
async def test_memory_update_caps_planning_without_overwriting_execution_budget(tmp_path):
    manager = MemoryManager(memory_dir=str(tmp_path))
    llm = BudgetLLM('{"operations":[{"action":"add","content":"- prefers concise replies"}]}')
    await manager.update_context_with_llm("- prefers concise replies", llm)
    assert llm.calls[-1]["max_tokens"] == 8_192
    assert "prefers concise replies" in manager.read_context()
    assert llm.max_output_tokens == 100_000


@pytest.mark.asyncio
async def test_openclaw_truncation_keeps_core_and_does_not_mark_import_complete(tmp_path, monkeypatch):
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    manager = MemoryManager(memory_dir=str(tmp_path))
    manager.write_core("- original core")
    monkeypatch.setattr(manager, "_read_openclaw_raw", lambda: "- candidate memory")
    llm = BudgetLLM("- partial memory", "length")
    assert await manager.import_openclaw(llm) == ""
    assert llm.calls[-1]["max_tokens"] == 8_192
    assert manager.read_core() == "- original core"
    assert not manager._openclaw_imported_marker.exists()


@pytest.mark.asyncio
async def test_truncated_memory_update_does_not_apply_even_valid_operations(tmp_path):
    manager = MemoryManager(memory_dir=str(tmp_path))
    manager.append_context("- original context")
    llm = BudgetLLM(json.dumps({"operations": [{
        "action": "replace", "old": "- original context", "new": "- partial replacement",
    }]}), "length")
    await manager.update_context_with_llm("- complete original candidate", llm)
    context = manager.read_context()
    assert "original context" in context
    assert "partial replacement" not in context
    assert llm.calls[-1]["max_tokens"] == 8_192


@pytest.mark.asyncio
async def test_web_summary_caps_server_default_and_rejects_truncated_result():
    llm = BudgetLLM("partial summary", "length")
    summary, error = await WebExtractTool(llm=llm)._summarize(
        "full page", "https://example.com", requested_model="", requested_max_output_tokens=None,
    )
    assert summary is None
    assert "output token limit" in error
    assert llm.calls[-1]["max_tokens"] == 8_192
    assert llm.max_output_tokens == 100_000


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [False, True])
async def test_anthropic_auxiliary_wire_budget_preserves_native_thinking_and_main_limit(thinking):
    requests = []

    async def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={
            "id": "message-test", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": '{"continue":false}'}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    llm = LLMClient(api_key="test", provider="anthropic", model="claude-test", max_output_tokens=100_000)
    await llm.aclose()
    llm._client.client = anthropic.AsyncAnthropic(
        api_key="test", max_retries=0, timeout=30,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    try:
        await llm.generate([Message(role="user", content="hello")], thinking_enabled=thinking)
        assert not await model_says_continue(
            llm, user_request="create a file", candidate_response="I will create the file",
            thinking_enabled=thinking,
        )
    finally:
        await llm.aclose()
    assert len(requests) == 2
    main, judge = requests
    assert main["max_tokens"] == 100_000
    assert judge["max_tokens"] == (12_288 if thinking else 8_192)
    assert judge.get("thinking") == main.get("thinking")
    if thinking:
        assert judge["max_tokens"] > judge["thinking"]["budget_tokens"] == 8_000
    assert llm.max_output_tokens == 100_000


@pytest.mark.asyncio
async def test_truncated_proxy_vision_is_rejected_and_native_mode_has_no_auxiliary_call(tmp_path):
    (tmp_path / "image.png").write_bytes(_ONE_PIXEL_PNG)
    llm = BudgetLLM("partial findings", "length")
    tool = ImageInspectionTool(llm, workspace_dir=str(tmp_path), native_supported=True)
    result = await tool.execute(["image.png"], "inspect", strategy="native")
    assert result.success
    assert llm.calls == []
    result = await tool.execute(["image.png"], "inspect", strategy="proxy")
    assert not result.success
    assert llm.calls[-1]["max_tokens"] == 8_192
    assert llm.max_output_tokens == 100_000


@pytest.mark.asyncio
@pytest.mark.parametrize("core_size, expected", [(100, 8_192), (4_000, 16_384), (10_000, 100_000)])
async def test_promotion_preserves_room_for_a_full_core_replacement(tmp_path, core_size, expected):
    manager = MemoryManager(memory_dir=str(tmp_path))
    manager.write_core("文" * core_size)
    manager.append_context("- prefers concise replies")
    candidates = manager.read_all_context_entries()
    llm = BudgetLLM(json.dumps({
        "new_core": manager.read_core(), "consumed_entry_ids": [candidates[0].id],
        "rationale": "retained facts",
    }), "length")
    assert await manager.plan_promotion(candidates, llm) is None
    assert llm.calls[-1]["max_tokens"] == expected
    assert manager.read_core() == "文" * core_size
    assert manager.read_all_context_entries()[0].id == candidates[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, expected", [("compact", 16_384), ("resolve_conflicts", 8_192)])
async def test_memory_maintenance_caps_requests_and_keeps_data_on_truncation(tmp_path, operation, expected):
    manager = MemoryManager(memory_dir=str(tmp_path))
    manager.append_context("- project uses Redis for cache")
    manager.append_context("- project now uses Postgres for cache")
    original = manager.read_context()
    llm = BudgetLLM('[]' if operation == "compact" else '{"groups":[]}', "length")
    maintainer = MemoryMaintainer(manager, AgentConfig(
        memory_context_max_entries=1, memory_conflict_cluster_threshold=0.01,
    ), llm=llm)
    await getattr(maintainer, f"_{operation}")(datetime.now(timezone.utc))
    assert llm.calls and llm.calls[-1]["max_tokens"] == expected
    assert manager.read_context() == original
    assert llm.max_output_tokens == 100_000


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("automatic", [False, True])
async def test_both_subagent_execution_strategies_keep_the_original_output_budget(tmp_path, batch, automatic):
    llm = BudgetLLM()
    if automatic:
        llm.auto_model_candidates = [{
            "model": "child-model", "tags": ["summary", "document", "office", "general"],
            "abilityLevel": 3, "maxTokens": 96_000,
        }]
    read = ReadTool(workspace_dir=str(tmp_path))
    source = tmp_path / "input.txt"
    source.write_text("Revenue is 42.")
    child = SubAgentTool(llm=llm, parent_tools={"read_file": read}, workspace_dir=str(tmp_path))
    result = await child.execute(
        task="Summarize the material", required_tools=[],
        **({"files": [str(source)]} if batch else {}),
    )
    assert result.success, result.error
    executions = [call for call in llm.calls if call.get("call_kind") == "subagent_step"]
    expected = 96_000 if automatic else 100_000
    assert executions and all(call["max_tokens"] == expected for call in executions)
    assert llm.max_output_tokens == 100_000
