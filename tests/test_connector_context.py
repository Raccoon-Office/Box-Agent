"""Host connector snapshots are projected per request, never erased from history."""

import pytest

from box_agent.connector_context import project_connector_status
from box_agent.context_input import DefaultContextEngine
from box_agent.schema import Message, StreamEvent
from box_agent.session_log import SessionLog
from box_agent.tools.engine.preparation import prepare_tools


def snapshot(status):
    return f"\n\n<connector-status>\npkulaw 北大法宝: {status}\n</connector-status>"


def host_message(text, status):
    fragment = snapshot(status)
    return Message(role="user", content=text + fragment, host_connector_status=fragment)


def text(messages):
    return "\n".join(str(message.content) for message in messages)


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_projection_failure_keeps_entire_original_history(monkeypatch, caplog, fail_on_call):
    import box_agent.connector_context as connector_context

    history = [host_message("first", "connected"), host_message("second", "connected"),
               host_message("latest", "disconnected")]
    before = [message.model_dump() for message in history]
    remove = connector_context._remove_fragment
    calls = 0

    def fail(content, fragment):
        nonlocal calls
        calls += 1
        if calls == fail_on_call:
            raise RuntimeError("sensitive diagnostic must not enter logs")
        return remove(content, fragment)

    monkeypatch.setattr(connector_context, "_remove_fragment", fail)
    assert project_connector_status(history) is history
    assert [message.model_dump() for message in history] == before
    assert "keeping original history" in caplog.text
    assert "sensitive diagnostic" not in caplog.text


@pytest.mark.asyncio
async def test_projection_failure_still_reaches_provider_and_summary(monkeypatch):
    from box_agent.runtime import run_agent_loop

    def fail(*args):
        raise ValueError("projection failed")

    monkeypatch.setattr("box_agent.connector_context._remove_fragment", fail)
    history = [Message(role="system", content="system"), host_message("first", "connected"),
               Message(role="assistant", content="done"), host_message("latest", "disconnected")]
    before = [message.model_dump() for message in history]
    engine = DefaultContextEngine()
    assert engine.prepare_summary(history, None) is history
    requests = []

    class Provider:
        async def generate_stream(self, messages, tools=None, **kwargs):
            requests.append([message.model_dump() for message in messages])
            yield StreamEvent(type="text", delta="continued")
            yield StreamEvent(type="finish", finish_reason="stop")

    events = [event async for event in run_agent_loop(
        llm=Provider(), messages=history, tools={}, token_limit=20000, max_steps=1)]
    assert requests == [before]
    assert any(getattr(event, "content", None) == "continued" for event in events)


def test_projection_does_not_swallow_cancellation(monkeypatch):
    import asyncio

    def cancel(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr("box_agent.connector_context._remove_fragment", cancel)
    with pytest.raises(asyncio.CancelledError):
        project_connector_status([host_message("latest", "connected")])


@pytest.mark.parametrize("latest", [
    snapshot("connected"), snapshot("disconnected"),
    "\n\n<connector-status>\n</connector-status>",
    "\n\n<connector-status>\nHost connector status unavailable.\n</connector-status>",
])
def test_requests_keep_one_latest_host_snapshot_without_changing_history(latest):
    history = [Message(role="system", content="system"), host_message("first", "connected"),
               Message(role="assistant", content="done"),
               Message(role="user", content="second" + latest, host_connector_status=latest)]
    before = [message.model_dump() for message in history]
    engine = DefaultContextEngine()
    request = engine.prepare_request(history, prepared_tools=prepare_tools([]), token_limit=20000)
    assert request.blocked_reason is None
    assert text(request.messages).count("<connector-status>") == 1
    assert request.messages[1].content == "first"
    assert request.messages[-1].content == "second" + latest
    assert [message.model_dump() for message in history] == before
    assert [message.model_dump() for message in project_connector_status(request.messages)] == [
        message.model_dump() for message in request.messages]
    assert text(engine.prepare_summary(history, None)).count("<connector-status>") == 1


def test_projection_never_deletes_unmarked_user_examples_or_other_roles():
    example = snapshot("connected")
    history = [Message(role="system", content=example), Message(role="user", content=example),
               Message(role="tool", content=example, tool_call_id="call"),
               host_message("first" + example, "connected"), host_message("second", "disconnected")]
    projected = project_connector_status(history)
    assert projected[:3] == history[:3]
    assert projected[3].content == "first" + example
    assert projected[4] is history[4]


def test_projection_preserves_nested_blocks_and_attachment_text():
    fragment = snapshot("connected")
    content = [{"type": "image", "data": "opaque"},
               {"type": "text", "text": "first" + fragment + "\nAttachment: file.png"}]
    history = [Message(role="user", content=content, host_connector_status=fragment),
               host_message("second", "disconnected")]
    projected = project_connector_status(history)
    assert projected[0].content[0] is history[0].content[0]
    assert projected[0].content[1]["text"] == "first\nAttachment: file.png"
    assert history[0].content[1]["text"] == "first" + fragment + "\nAttachment: file.png"


def test_request_projection_preserves_surrounding_context_and_followup_input():
    fragment = snapshot("connected")
    before_status = "[Host UI language: Chinese.]\n\n<user_query>你好</user_query>"
    after_status = "\n\n<attachments>reference.png</attachments>\n<system-reminder>keep</system-reminder>"
    old = Message(role="user", content=before_status + fragment + after_status,
                  host_connector_status=fragment)
    system = Message(role="system", content="CALLER_POLICY")
    skill = Message(role="user", source="runtime", content="<skill>SKILL_BODY</skill>")
    memory = Message(role="user", source="runtime", content="<memory>MEMORY_BODY</memory>")
    followup = Message(role="user", source="runtime", content=[{"type": "text", "text": "FOLLOWUP"}])
    history = [system, old, skill, Message(role="assistant", content="prior answer"),
               host_message("latest", "disconnected")]
    original = [message.model_dump() for message in history]
    engine = DefaultContextEngine()
    request = engine.prepare_request(history, prepared_tools=prepare_tools([]), token_limit=20000,
                                     extra_messages=(memory,), transient_message=followup)
    assert request.blocked_reason is None
    assert request.context_messages[0] is system
    assert request.context_messages[1].content == before_status + after_status
    assert request.context_messages[2] is skill
    assert request.context_messages[-1] is memory
    assert request.messages[-1] is followup
    assert text(request.messages).count("<connector-status>") == 1
    assert [message.model_dump() for message in history] == original


def test_goal_context_and_literal_status_examples_survive_projection(tmp_path):
    from box_agent.agent import Agent

    agent = Agent(llm_client=object(), system_prompt="CALLER_POLICY", tools=[], workspace_dir=str(tmp_path))
    agent.set_goal("Preserve this objective")
    example = "Literal example:" + snapshot("connected")
    agent.add_user_message(example + snapshot("connected"), host_connector_status=snapshot("connected"))
    agent.add_user_message("next" + snapshot("disconnected"), host_connector_status=snapshot("disconnected"))
    original = [message.model_dump() for message in agent.messages]
    projected = project_connector_status(agent.messages)
    assert projected[0] is agent.messages[0]
    assert "Objective: Preserve this objective" in projected[1].content
    assert projected[1].content.endswith(example)
    assert [message.model_dump() for message in agent.messages] == original


def test_interleaved_sessions_and_unmarked_child_history_do_not_share_state():
    engine = DefaultContextEngine()
    first, second = [host_message("one", "connected")], [host_message("two", "disconnected")]
    for history in [first, second, first]:
        projected = engine.project_history(history)
        assert text(projected) == text(history)
    child = [Message(role="user", content="child task")]
    assert engine.project_history(child) == child


def test_latest_snapshot_survives_bounded_message_text():
    message = host_message("latest", "disconnected")
    message = message.model_copy(update={"content": "[bounded content]"})
    projected = project_connector_status([host_message("old", "connected"), message])
    assert text(projected).count("<connector-status>") == 1
    assert projected[-1].content == "[bounded content]" + snapshot("disconnected")
    assert message.content == "[bounded content]"


def test_replay_preserves_snapshot_provenance_and_original_history(tmp_path):
    history = [host_message("first", "connected"), host_message("second", "disconnected")]
    log = SessionLog.create(tmp_path, session_id="connector-context", cwd=tmp_path)
    try:
        log.append_unlogged_messages(history, turn=1, step=None)
        log.flush()
        replayed = log.replay().messages
        assert [message.host_connector_status for message in replayed] == [
            message.host_connector_status for message in history]
        assert text(project_connector_status(replayed)).count("<connector-status>") == 1
        assert text(log.replay().messages).count("<connector-status>") == 2
    finally:
        log.close()


def test_request_trace_contains_only_model_content_not_duplicate_provenance():
    from box_agent.llm.llm_wrapper import _trace_request_message

    payload = _trace_request_message(host_message("latest", "disconnected"))
    assert "host_connector_status" not in payload
    assert str(payload).count("<connector-status>") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("token_limit", [2000, 20000])
async def test_loop_sends_one_snapshot_before_and_after_compaction(token_limit):
    from box_agent.runtime import run_agent_loop

    requests = []

    class Provider:
        async def generate_stream(self, messages, tools=None, **kwargs):
            requests.append((kwargs.get("call_kind"), list(messages)))
            if kwargs.get("call_kind") == "context_summary":
                yield StreamEvent(type="text", delta="<summary>Earlier work completed.</summary>")
            else:
                yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    history = [Message(role="system", content="system"), host_message("old", "connected"),
               Message(role="assistant", content="x" * 20000), host_message("latest", "disconnected")]
    events = [event async for event in run_agent_loop(
        llm=Provider(), messages=history, tools={}, token_limit=token_limit, max_steps=1)]
    assert events
    assert any(kind == "context_summary" for kind, _ in requests) == (token_limit == 2000)
    assert any(kind != "context_summary" for kind, _ in requests)
    for _, messages in requests:
        assert text(messages).count("<connector-status>") == 1
        assert snapshot("disconnected") in text(messages)
        assert snapshot("connected") not in text(messages)
