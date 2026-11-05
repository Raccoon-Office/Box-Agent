"""Cloud handoff uses the normal prompt, stream and durable history."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from acp import PromptRequest, text_block

from box_agent.acp import BoxACPAgent
from box_agent.cloud_handoff import validate_cloud_handoff
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import StreamEvent
from tests.test_acp import DoneLLM, DummyConn
from tests.acp_helpers import acp_params


class HandoffLLM(DoneLLM):
    def __init__(self, output):
        super().__init__()
        self.output = output

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.last_messages = messages
        yield StreamEvent(type="text", delta=self.output)
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.fixture
async def handoff_session(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2, workspace_dir=str(tmp_path), enable_memory=False,
            enable_memory_extraction=False, goal_autopilot_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False, enable_bash=False, enable_mcp=False,
            enable_sub_agent=False, enable_skills=False,
        ),
    )
    llm = HandoffLLM('{"instruction":"Continue the analysis","dependency_paths":[]}')
    adapter = BoxACPAgent(DummyConn(), config, llm, [], "system")
    session_id = (await adapter.new_session(**acp_params(SimpleNamespace(
        cwd=str(tmp_path), field_meta={"session_id": "handoff-test"},
    )))).session_id
    try:
        yield adapter, session_id, llm
    finally:
        await adapter.aclose()


async def send(adapter, session_id, marker=True, text="Switch to cloud"):
    return await adapter.prompt(**acp_params(PromptRequest(
        sessionId=session_id, prompt=[text_block(text)],
        field_meta={"cloudHandoff": marker},
    )))


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["ui_language", "uiLanguage"])
@pytest.mark.parametrize("language", ["zh", "ZH", "en", "ja", "fr", ""])
async def test_handoff_persists_instruction_in_chinese_or_english(
    handoff_session, key, language,
):
    adapter, session_id, _ = handoff_session
    response = await adapter.prompt(**acp_params(PromptRequest(
        sessionId=session_id, prompt=[text_block("Switch to cloud")],
        field_meta={"cloudHandoff": True, key: language},
    )))
    assert response.field_meta["cloudHandoff"] is True
    user_text = next(
        m.content for m in reversed(adapter._sessions[session_id].agent.messages)
        if m.role == "user"
    )
    if language.lower() == "zh":
        assert user_text.startswith("请基于当前本地对话")
        assert "目标、已有结论、约束、进度和后续工作" in user_text
    else:
        assert user_text.startswith("Prepare an independent cloud task")
    assert '"instruction":"...","dependency_paths":["..."]' in user_text
    assert "Switch to cloud" in user_text


@pytest.mark.asyncio
async def test_handoff_persists_expanded_request_and_answer_and_local_chat_continues(handoff_session):
    adapter, session_id, llm = handoff_session
    response = await send(adapter, session_id)
    assert response.field_meta["cloudHandoff"] is True
    assert response.field_meta["ok"] is True
    state = adapter._sessions[session_id]
    messages = state.agent.messages
    assert any(m.role == "user" and "Prepare an independent cloud task" in m.content
               and "Switch to cloud" in m.content for m in messages)
    assert any(m.role == "assistant" and m.content == llm.output for m in messages)
    # Restore from the actual Session Log, not the adapter's in-memory messages.
    expected = [(m.role, m.content) for m in messages]
    await adapter.load_session(
        cwd=str(state.agent.workspace_dir), session_id=session_id, mcp_servers=[],
    )
    restored = adapter._sessions[session_id].agent.messages
    assert [(m.role, m.content) for m in restored] == expected
    llm.output = "Continue locally"
    response = await send(adapter, session_id, marker=False, text="Follow up locally")
    assert "cloudHandoff" not in response.field_meta
    assert response.field_meta["ok"] is True
    assert "Prepare an independent cloud task" not in [
        m.content for m in adapter._sessions[session_id].agent.messages if m.role == "user"
    ][-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [False, "true", 1, None])
async def test_only_boolean_true_expands_handoff(handoff_session, marker):
    adapter, session_id, _ = handoff_session
    response = await send(adapter, session_id, marker)
    assert "cloudHandoff" not in response.field_meta
    assert not any("Prepare an independent cloud task" in m.content
                   for m in adapter._sessions[session_id].agent.messages if m.role == "user")


@pytest.mark.asyncio
async def test_handoff_preserves_ordered_attachments_and_projects_instruction(
    handoff_session, tmp_path,
):
    adapter, session_id, llm = handoff_session
    path = tmp_path / "reference.txt"
    path.write_text("known dependency", encoding="utf-8")
    blocks = [
        {"type": "text", "text": "Use this reference"},
        {"type": "resource_link", "uri": path.as_uri(), "name": path.name},
        {"type": "text", "text": "Keep the existing constraints"},
    ]
    response = await adapter.prompt(
        session_id=session_id, prompt=blocks, cloudHandoff=True, turn_id="handoff-turn",
    )
    assert response.field_meta["cloudHandoff"] is True
    state = adapter._sessions[session_id]
    saved = next(message for _, message, _ in state.agent.session_log.history_messages()
                 if message.role == "user" and message.input_facts)
    assert saved.input_facts["blocks"][1:] == blocks
    assert saved.input_facts["blocks"][0]["text"].startswith("Prepare an independent cloud task")
    assert saved.input_facts["id"] == "handoff-turn"
    assert any(message.role == "user" and "Prepare an independent cloud task" in str(message.content)
               for message in llm.last_messages)
    # The action is part of the accepted input identity, not only transient metadata.
    with pytest.raises(ValueError, match="TURN_IDEMPOTENCY_CONFLICT"):
        await adapter.prompt(session_id=session_id, prompt=blocks, turn_id="handoff-turn")


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["done", "```json\n{}\n```", '{"instruction":"x","dependency_paths":["missing.txt"]}'])
async def test_invalid_handoff_is_not_acknowledged(handoff_session, output):
    adapter, session_id, llm = handoff_session
    llm.output = output
    response = await send(adapter, session_id)
    assert response.field_meta["cloudHandoff"] is False
    assert response.field_meta["ok"] is False
    assert response.field_meta["errorCode"] == "invalid_cloud_handoff"
    assert response.field_meta["messageDisplay"]["finalMessageIds"] == []
    state = adapter._sessions[session_id]
    record_path = (
        state.agent.workspace_dir / ".box-agent/task-registry/tasks"
        / f"{state.current_task_id}.json"
    )
    assert json.loads(record_path.read_text(encoding="utf-8"))["execution_status"] == "error"
    assert response.field_meta["lastStopReason"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["error", "cancelled", "interrupted", "max_tokens", "max_steps", "waiting_for_user"])
async def test_non_success_stop_never_acknowledges_handoff(handoff_session, monkeypatch, reason):
    adapter, session_id, _ = handoff_session
    async def stopped(*args, **kwargs):
        return reason
    monkeypatch.setattr(adapter, "_run_turn", stopped)
    response = await send(adapter, session_id)
    assert response.field_meta["cloudHandoff"] is False


@pytest.mark.asyncio
async def test_exception_never_returns_handoff_success_and_prompt_can_resume(handoff_session, monkeypatch):
    adapter, session_id, _ = handoff_session
    original = adapter._run_turn
    async def fail(*args, **kwargs):
        raise RuntimeError("test failure")
    monkeypatch.setattr(adapter, "_run_turn", fail)
    with pytest.raises(RuntimeError, match="test failure"):
        await send(adapter, session_id)
    monkeypatch.setattr(adapter, "_run_turn", original)
    assert (await send(adapter, session_id)).field_meta["cloudHandoff"] is True


@pytest.mark.asyncio
async def test_cancelled_request_has_no_success_response_and_can_resume(handoff_session, monkeypatch):
    adapter, session_id, _ = handoff_session
    started = asyncio.Event()
    original = adapter._run_turn
    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(adapter, "_run_turn", blocked)
    task = asyncio.create_task(send(adapter, session_id))
    await asyncio.wait_for(started.wait(), 10)
    adapter._sessions[session_id].cancelled = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(adapter, "_run_turn", original)
    assert (await send(adapter, session_id)).field_meta["cloudHandoff"] is True


@pytest.mark.asyncio
async def test_cancel_notification_during_model_stream_never_acknowledges(handoff_session, monkeypatch):
    adapter, session_id, llm = handoff_session
    started = asyncio.Event()
    original = llm.generate_stream
    async def blocked(*args, **kwargs):
        started.set()
        yield StreamEvent(type="text", delta=llm.output)
        await asyncio.Event().wait()
    monkeypatch.setattr(llm, "generate_stream", blocked)
    task = asyncio.create_task(send(adapter, session_id))
    await asyncio.wait_for(started.wait(), 10)
    await adapter.cancel(**acp_params(SimpleNamespace(sessionId=session_id)))
    response = await asyncio.wait_for(task, 10)
    assert response.stop_reason == "cancelled"
    assert response.field_meta["cloudHandoff"] is False
    monkeypatch.setattr(llm, "generate_stream", original)
    assert (await send(adapter, session_id)).field_meta["cloudHandoff"] is True


@pytest.mark.asyncio
async def test_handoff_preserves_pending_plan_without_requiring_approval(
    handoff_session, monkeypatch,
):
    adapter, session_id, _ = handoff_session
    state = adapter._sessions[session_id]
    pending = {
        "required": True, "state": "pending", "request_id": "existing-request",
        "plan_id": "existing-plan", "title": "Original task",
    }
    state.pending_plan_approval = pending.copy()
    state.force_plan_start = True
    response = await adapter.prompt(**acp_params(PromptRequest(
        sessionId=session_id, prompt=[text_block("Switch to cloud")],
        field_meta={"cloudHandoff": True, "autoApprovePlan": True},
    )))
    assert response.field_meta["cloudHandoff"] is True
    assert response.field_meta["messageDisplay"]["finalMessageIds"]
    assert state.pending_plan_approval == pending
    assert state.force_plan_start is True
    assert not any("Call `plan_write`" in m.content for m in state.agent.messages)

    async def next_local_turn(*args, **kwargs):
        assert kwargs["require_plan_approval"] is True
        return "waiting_for_user"
    monkeypatch.setattr(adapter, "_run_turn", next_local_turn)
    response = await send(adapter, session_id, marker=False, text="Continue locally")
    assert response.field_meta["runStatus"] == "waiting_for_user"


@pytest.mark.asyncio
async def test_task_persistence_failure_revokes_valid_handoff(handoff_session, monkeypatch):
    adapter, session_id, _ = handoff_session
    def fail(*args, **kwargs):
        raise OSError("cannot persist task")
    monkeypatch.setattr("box_agent.acp.finish_task", fail)
    response = await send(adapter, session_id)
    assert response.field_meta["cloudHandoff"] is False
    assert response.field_meta["ok"] is False
    assert response.field_meta["messageDisplay"]["finalMessageIds"] == []


@pytest.mark.asyncio
async def test_success_receipt_accepts_existing_dependency_without_autopilot(handoff_session, tmp_path, monkeypatch):
    adapter, session_id, llm = handoff_session
    path = tmp_path / "资料 input.txt"
    path.write_text("known input", encoding="utf-8")
    llm.output = json.dumps({"instruction": "Analyze the known input", "dependency_paths": [str(path)]})
    adapter._sessions[session_id].config.agent.goal_autopilot_enabled = True
    def unexpected(*args):
        pytest.fail("handoff must not continue the local goal")
    monkeypatch.setattr("box_agent.acp.should_continue_goal_autopilot", unexpected)
    response = await send(adapter, session_id)
    assert response.field_meta["cloudHandoff"] is True
    assert response.field_meta["messageDisplay"]["finalMessageIds"]
    messages = adapter._sessions[session_id].agent.messages
    assert not any(getattr(m, "tool_calls", None) for m in messages)


def test_handoff_accepts_known_absolute_file_with_unicode_and_spaces(tmp_path):
    path = tmp_path / "资料 file.txt"
    path.write_text("input", encoding="utf-8")
    validate_cloud_handoff(json.dumps({"instruction": "Analyze", "dependency_paths": [str(path)]}))


@pytest.mark.parametrize("value", [None, [], {}, {"instruction": "", "dependency_paths": []},
    {"instruction": "x", "dependency_paths": "file"},
    {"instruction": "x", "dependency_paths": [None]},
    {"instruction": "x", "dependency_paths": ["https://example.com/file"]},
    {"instruction": "x", "dependency_paths": [], "name": "extra"}])
def test_handoff_rejects_wrong_shape(value):
    with pytest.raises(ValueError):
        validate_cloud_handoff(json.dumps(value))


def test_handoff_rejects_directory_and_duplicate_fields(tmp_path):
    with pytest.raises(ValueError):
        validate_cloud_handoff(json.dumps({"instruction": "x", "dependency_paths": [str(tmp_path)]}))
    with pytest.raises(ValueError):
        validate_cloud_handoff('{"instruction":"x","instruction":"y","dependency_paths":[]}')
