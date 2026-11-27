"""Context owns request projection while Skill owns source and read facts."""

import hashlib
import json
import inspect
from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from box_agent.config import Config
from box_agent.context_input import DefaultContextEngine
from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.llm.openai_client import OpenAIClient
from box_agent.schema import Message
from box_agent.session_projection import SessionProjection
from box_agent.skill_context import SkillReferenceContext
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: demo\ndescription: example\n---\nMETHOD_BODY\n")
    loader = SkillLoader(sources=[(tmp_path / "skills", "user")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    return SkillRuntime(loader)


def test_runtime_exposes_immutable_source_without_owning_request_state(runtime):
    snapshot = runtime.resolve_reference("demo")
    assert "METHOD_BODY" in snapshot.prompt
    assert runtime.log_records() == []
    with pytest.raises(FrozenInstanceError):
        snapshot.prompt = "changed"
    assert not hasattr(runtime, "prepare_context")
    assert "messages" not in inspect.signature(SkillRuntime).parameters
    assert {"_messages", "_remaining", "_host_visible", "reference_overhead_chars"}.isdisjoint(vars(runtime))


def test_contexts_share_read_facts_but_never_share_visible_text(runtime):
    first, second = SkillReferenceContext(runtime), SkillReferenceContext(runtime)
    first.prepare_request([], budget_chars=50000)
    result = first.read("demo")
    first.prepare_request([Message(role="tool", name="get_skill", tool_call_id="call-1", content=result.model_context)], budget_chars=50000)
    assert first.read("demo").raw_output["skill_reference"]["reused"]
    second.prepare_request([], budget_chars=50000)
    assert "METHOD_BODY" in second.read("demo").model_context
    assert runtime.log_records()[0]["deliveredComplete"]


def test_context_uses_the_exact_prepared_tool_snapshot_without_filtering(runtime):
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime, session_store=None)
    prepared = prepare_tools([GetSkillTool(runtime.loader)])
    runtime.select(["demo"])
    messages = [Message(role="system", content="BASE"), Message(role="user", content="task")]
    before = [message.model_dump() for message in messages]
    request = engine.prepare_request(messages, prepared_tools=prepared, token_limit=20000)
    assert engine.prepared_tools is prepared
    assert [tool.name for tool in prepared.definitions] == ["get_skill"]
    assert "METHOD_BODY" in str(request.messages)
    assert [message.model_dump() for message in messages] == before


def test_compaction_retains_the_latest_runtime_date():
    engine = DefaultContextEngine()
    system = Message(role="system", content="Use the latest runtime `<current_date>`.")
    old = Message(role="user", source="runtime", name="current_date", content="<current_date>2026-11-22</current_date>")
    latest = old.model_copy(update={"content": "<current_date>2026-11-23</current_date>"})
    user = Message(role="user", content="continue")
    original = [system, old, latest, user]
    compacted = [system, user]

    restored = engine.compact_history(original, compacted)
    assert restored == [*compacted, latest.model_copy(update={"compaction_node": "appended"})]
    assert engine.compact_history(original, [system, latest, user]) == [system, latest, user]
    assert engine.compact_history(original, restored) == restored
    assert original == [system, old, latest, user]


def test_same_day_follow_up_labels_runtime_date_in_provider_requests(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 22)

    monkeypatch.setattr("box_agent.context_input.date", FixedDate)
    prompt = (Config.get_bundled_config_dir() / "system_prompt.md").read_text(encoding="utf-8")
    engine = DefaultContextEngine()
    tools = prepare_tools([])
    history = [Message(role="system", content=prompt), Message(role="user", content="What is today's date?")]
    first = engine.prepare_request(history, prepared_tools=tools, token_limit=20000)
    (date_record,) = first.history_updates
    assert date_record.content == "<current_date>2026-11-22</current_date>"
    quoted_date = "<current_date>2000-01-01</current_date>"
    follow_up = engine.prepare_request(
        [*history, date_record, Message(role="assistant", content="2026-11-22"),
         Message(role="user", content=quoted_date)],
        prepared_tools=tools, token_limit=20000,
    )
    assert not follow_up.history_updates

    labeled_date = f"[Host runtime reference source=runtime kind=current_date]\n{date_record.content}"
    openai = OpenAIClient.__new__(OpenAIClient)
    _, openai_first = openai._convert_messages(first.messages)
    _, openai_follow_up = openai._convert_messages(follow_up.messages)
    assert openai_follow_up[:-2] == openai_first
    assert openai_first[-1] == {"role": "user", "content": labeled_date}
    assert openai_follow_up[-1] == {"role": "user", "content": quoted_date}

    anthropic = AnthropicClient.__new__(AnthropicClient)
    _, anthropic_first = anthropic._convert_messages(first.messages)
    _, anthropic_follow_up = anthropic._convert_messages(follow_up.messages)
    assert anthropic_follow_up[:-2] == anthropic_first
    assert anthropic_first[-1]["content"][-1] == {"type": "text", "text": labeled_date}
    assert anthropic_follow_up[-1] == {"role": "user", "content": quoted_date}


def test_custom_session_store_receives_bounded_inline_reference_metadata(runtime):
    class Store:
        def replay(self):
            return SessionProjection([], None, None, [], [])

        def append(self, *args, **kwargs):
            pass

        def flush(self):
            pass

    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime, session_store=Store())
    runtime.select(["demo"])
    request = engine.prepare_request([Message(role="user", content="task")],
                                     prepared_tools=prepare_tools([]), token_limit=20000)
    ref = request.references[0]
    assert "METHOD_BODY" in ref["inlineContent"]
    assert hashlib.sha256(ref["inlineContent"].encode()).hexdigest() == ref["sha256"]
    assert "contentRef" not in ref


def test_host_projection_charges_serialized_blocks_and_original_user_escaping(runtime):
    from box_agent.kernel.context_engine import _fallback_context_estimate

    path = runtime.loader.get_skill("demo").skill_path
    path.write_text('---\nname: demo\ndescription: example\n---\n' + ('"\\' * 70 + '\n') * 35)
    runtime.select(["demo"])
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    messages = [Message(role="user", content='"\\' * 250)]
    tool = GetSkillTool(runtime.loader)
    request = engine.prepare_request(messages, prepared_tools=prepare_tools([tool]), token_limit=4000)

    assert _fallback_context_estimate(request.messages, {tool.name: tool}) + 1024 <= 4000
    assert "get_skill" in str(request.messages) or "[Skill reference]" in str(request.messages)


def test_selected_material_without_reader_is_bounded_when_no_body_fits(runtime):
    runtime.select(["demo"])
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    request = engine.prepare_request([Message(role="user", content="task")],
                                     prepared_tools=prepare_tools([]), token_limit=1024)

    assert request.blocked_reason is None
    assert runtime.read_facts == ()
    assert runtime.turn_deliveries == {}


def test_stored_or_hook_rewritten_tool_text_never_reuses_delivery_fact(runtime, tmp_path):
    from box_agent.tool_result_storage import ToolResultStorage

    context = SkillReferenceContext(runtime)
    context.prepare_request([], budget_chars=50000)
    result = context.read("demo")
    message = Message(role="tool", name="get_skill", tool_call_id="read-demo", content=result.model_context)
    storage = ToolResultStorage(tmp_path / "results", default_result_limit=10)
    stored = storage.process_message(message, tool=None)
    assert stored.content != message.content
    for final in (stored, message.model_copy(update={"content": "Hook replaced the reference"})):
        context.prepare_request([final], budget_chars=50000)
        reread = context.read("demo")
        assert "METHOD_BODY" in reread.model_context
        assert not reread.raw_output["skill_reference"].get("reused")
    assert runtime.read_facts[0].delivered_complete


def test_tool_budget_includes_pending_envelopes_extra_and_transient(runtime):
    from box_agent.kernel.context_engine import _fallback_context_estimate
    from box_agent.schema import FunctionCall, ToolCall

    runtime.loader.get_skill("demo").skill_path.write_text(
        '---\nname: demo\ndescription: example\n---\n' + ('"\\' * 40 + '\n') * 160)
    tool = GetSkillTool(runtime.loader)
    prepared = prepare_tools([tool])
    extra = Message(role="user", content="EXTRA " * 150)
    transient = Message(role="user", content=[{"type": "text", "text": "TRANSIENT " * 150}])
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    messages = [Message(role="user", content="task")]
    engine.prepare_request(messages, prepared_tools=prepared, token_limit=4500,
                           extra_messages=(extra,), transient_message=transient,
                           transient_tokens=_fallback_context_estimate([transient], {}))
    messages.append(Message(role="assistant", content="", tool_calls=[ToolCall(id="read-1", type="function",
        function=FunctionCall(name="get_skill", arguments={"skill_name": "demo"}))]))
    result = engine.tool_reader("demo")
    assert result.success
    messages.append(Message(role="tool", name="get_skill", tool_call_id="read-1", content=result.model_context))
    assert _fallback_context_estimate([*messages, extra, transient], {tool.name: tool}) + 1024 <= 4500


def test_same_batch_reads_use_final_committed_history_instead_of_preparation_copy(runtime):
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    history = [Message(role="user", content="task")]
    engine.prepare_request(history, prepared_tools=prepare_tools([]), token_limit=20000)
    first = engine.tool_reader("demo")
    history.append(Message(role="tool", name="get_skill", tool_call_id="first", content=first.model_context))
    second = engine.tool_reader("demo")
    assert second.raw_output["skill_reference"]["reused"]
    history[-1] = history[-1].model_copy(update={"content": "Final stored summary only"})
    third = engine.tool_reader("demo")
    assert "METHOD_BODY" in third.model_context
    assert not third.raw_output["skill_reference"].get("reused")


@pytest.mark.asyncio
async def test_context_budget_rejection_stops_kernel_before_provider(runtime):
    from box_agent.events import DoneEvent, ErrorEvent, StopReason
    from box_agent.runtime import run_agent_loop

    class Provider:
        async def generate(self, *args, **kwargs):
            raise AssertionError("Blocked context must not reach the provider")

        async def generate_stream(self, *args, **kwargs):
            raise AssertionError("Blocked context must not reach the provider")
            yield

    runtime.select(["demo"])
    events = [event async for event in run_agent_loop(
        llm=Provider(), messages=[Message(role="system", content="BASE " * 6000), Message(role="user", content="task")],
        tools={}, skill_engine=runtime, token_limit=1024, max_steps=1)]
    assert any(isinstance(event, ErrorEvent) and "budget" in event.message for event in events)
    assert any(isinstance(event, DoneEvent) and event.stop_reason == StopReason.ERROR for event in events)
    assert runtime.read_facts == ()


@pytest.mark.asyncio
async def test_custom_session_store_persists_inline_snapshot_before_provider(runtime):
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import LLMResponse, StreamEvent

    class Store:
        def __init__(self):
            self.records = []
            self.flushed = False

        def replay(self):
            return SessionProjection([], None, None, [], [])

        def append(self, kind, payload, **kwargs):
            self.records.append((kind, payload))
            self.flushed = False

        def append_unlogged_messages(self, *args, **kwargs):
            pass

        def replace_surface(self, *args, **kwargs):
            pass

        def flush(self):
            self.flushed = True

    store = Store()

    class Provider:
        async def generate(self, messages, **kwargs):
            refs = next(payload["skillReferences"] for kind, payload in store.records if kind == "request/context")
            assert store.flushed
            assert "METHOD_BODY" in refs[0]["inlineContent"]
            assert "METHOD_BODY" in str(messages)
            return LLMResponse(content="done", finish_reason="stop")

        async def generate_stream(self, messages, **kwargs):
            result = await self.generate(messages, **kwargs)
            yield StreamEvent(type="text", delta=result.content)
            yield StreamEvent(type="finish", finish_reason="stop")

    runtime.select(["demo"])
    _ = [event async for event in run_agent_loop(
        llm=Provider(), messages=[Message(role="system", content="BASE"), Message(role="user", content="task")],
        tools={}, skill_engine=runtime, session_log=store, session_turn=1, max_steps=1)]
    assert any(kind == "request/context" for kind, payload in store.records)


def test_new_transient_from_current_batch_is_reserved_before_following_skill_read(runtime):
    from box_agent.kernel.context_engine import _fallback_context_estimate
    from box_agent.schema import FunctionCall, ToolCall

    runtime.loader.get_skill("demo").skill_path.write_text(
        '---\nname: demo\ndescription: example\n---\n' + ("METHOD " * 20 + "\n") * 200)
    tool = GetSkillTool(runtime.loader)
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    messages = [Message(role="user", content="task")]
    engine.prepare_request(messages, prepared_tools=prepare_tools([tool]), token_limit=8000)
    # The kernel commits tool calls before execution, letting the reader reserve
    # the pending tool-result envelope as well as the request-only followup.
    messages.append(Message(role="assistant", content="", tool_calls=[ToolCall(id="read-1", type="function",
        function=FunctionCall(name="get_skill", arguments={"skill_name": "demo"}))]))
    blocks = [{"type": "text", "text": "x" * 6000}]
    engine.reserve_followup(blocks)
    result = engine.tool_reader("demo")
    assert result.success
    messages.extend([Message(role="tool", name="get_skill", tool_call_id="read-1", content=result.model_context),
                     Message(role="user", content=blocks)])
    assert _fallback_context_estimate(messages, {tool.name: tool}) + 1024 <= 8000


def test_paging_requires_reader_access_to_selected_skills(runtime):
    runtime.loader.get_skill("demo").skill_path.write_text(
        "---\nname: demo\ndescription: example\n---\n" + "METHOD_BODY\n" * 3000,
    )
    runtime.select(["demo"])
    allowed = set()
    tool = GetSkillTool(runtime.loader, skill_access_filter=lambda skill: skill.name in allowed)
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    messages = [Message(role="user", content="use the selected method")]
    denied = engine.prepare_request(messages, prepared_tools=prepare_tools([tool]), token_limit=5000)
    assert denied.blocked_reason is None
    assert "METHOD_BODY" in str(denied.messages)
    reference = denied.messages[-1].content[1]["text"].split("[Skill reference]\n", 1)[1]
    metadata = json.loads(reference.split("\n", 1)[0])
    assert metadata["path"] == str(runtime.loader.get_skill("demo").skill_path)
    assert runtime.read_facts == ()
    allowed.add("demo")
    permitted = engine.prepare_request(messages, prepared_tools=prepare_tools([tool]), token_limit=5000)
    assert not permitted.blocked_reason
    assert "get_skill" in str(permitted.messages)
    assert runtime.read_facts == ()
