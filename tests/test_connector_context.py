"""Trusted connector updates keep already sent request prefixes immutable."""

import asyncio

import pytest

from box_agent.agent import Agent
from box_agent.connector_context import append_connector_status, restore_connector_status
from box_agent.context_input import DefaultContextEngine
from box_agent.llm.llm_wrapper import _trace_request_message
from box_agent.schema import Message, StreamEvent
from box_agent.session_log import SessionLog
from box_agent.tools.engine.preparation import prepare_tools


def snapshot(status):
    return f"<connector-status>\npkulaw 北大法宝: {status}\n</connector-status>"


def unavailable():
    return "<connector-status>\nstatus: unavailable\n请勿沿用之前轮次的连接状态。\n</connector-status>"


@pytest.fixture
def agent(tmp_path):
    return Agent(llm_client=object(), system_prompt="system", tools=[], workspace_dir=str(tmp_path))


def request(history):
    prepared = DefaultContextEngine().prepare_request(
        history, prepared_tools=prepare_tools([]), token_limit=20000)
    assert prepared.blocked_reason is None
    return [_trace_request_message(message) for message in prepared.messages]


@pytest.mark.parametrize("states", [
    [snapshot("connected"), snapshot("connected"), snapshot("disconnected"), snapshot("disconnected")],
    [snapshot("connected"), unavailable(), unavailable(), snapshot("connected")],
    [snapshot("connected"), snapshot("disconnected"), snapshot("connected"), snapshot("connected")],
    [snapshot("connected"), "<connector-status>\n</connector-status>", None, snapshot("connected")],
])
def test_first_and_changed_snapshots_are_appended_without_breaking_prefix(agent, states):
    previous_request = []
    previous_status = None
    for index, status in enumerate(states):
        before = [message.model_dump() for message in agent.messages]
        agent.add_user_message(f"你好{index}", host_connector_status=status)
        assert [message.model_dump() for message in agent.messages[:-1]] == before
        current = agent.messages[-1]
        if status is not None and status != previous_status:
            assert current.content.endswith(status)
            if previous_status is not None:
                assert "supersedes all earlier" in current.content
        else:
            assert current.content == f"你好{index}"
        current_request = request(agent.messages)
        assert current_request[:len(previous_request)] == previous_request
        assert "host_connector_status" not in str(current_request)
        previous_request = current_request
        if status is not None:
            previous_status = status
        agent.messages.append(Message(role="assistant", content="你好！"))


def test_reordered_rows_do_not_emit_an_update(agent):
    first = "<connector-status>\nz 名称Z: connected\na 名称A: disconnected\n</connector-status>"
    second = "<connector-status>\na 名称A: disconnected\nz 名称Z: connected\n</connector-status>"
    agent.add_user_message("first", host_connector_status=first)
    before = request(agent.messages)
    agent.add_user_message("second", host_connector_status=second)
    assert agent.messages[-1].content == "second"
    assert request(agent.messages)[:len(before)] == before


def test_user_xml_legacy_history_and_surrounding_context_are_never_deleted(agent):
    example = snapshot("connected")
    agent.messages.extend([Message(role="user", content="legacy" + example),
                           Message(role="user", source="runtime", content="SKILL_MEMORY_GOAL")])
    agent.add_user_message("literal:" + example + "\nAttachment: file.png",
                           host_connector_status=snapshot("connected"))
    before = request(agent.messages)
    agent.add_user_message("changed", host_connector_status=snapshot("disconnected"))
    assert request(agent.messages)[:len(before)] == before
    assert "literal:" + example in agent.messages[-2].content
    assert "Attachment: file.png" in agent.messages[-2].content
    assert restore_connector_status(agent.messages) is agent.messages


def test_sessions_have_independent_change_detection(tmp_path):
    agents = [Agent(llm_client=object(), system_prompt="system", tools=[], workspace_dir=str(tmp_path))
              for _ in range(2)]
    for index in [0, 1, 0, 1]:
        status = snapshot("connected" if index == 0 else "disconnected")
        agents[index].add_user_message("你好", host_connector_status=status)
    assert agents[0].messages[-1].content == agents[1].messages[-1].content == "你好"
    assert "disconnected" not in str(request(agents[0].messages))
    assert any(snapshot("disconnected") in message["content"] for message in request(agents[1].messages))


def test_replay_restores_change_detection_without_rewriting_history(agent, tmp_path):
    agent.add_user_message("first", host_connector_status=snapshot("connected"))
    agent.add_user_message("second", host_connector_status=snapshot("connected"))
    log = SessionLog.create(tmp_path, session_id="connector-context", cwd=tmp_path)
    try:
        log.append_unlogged_messages(agent.messages[1:], turn=1, step=None)
        log.flush()
        replayed = log.replay().messages
        assert replayed[-1].host_connector_status == snapshot("connected")
        assert replayed[-1].content == "second"
        assert append_connector_status(replayed, "third", snapshot("connected")) == "third"
        assert snapshot("disconnected") in append_connector_status(replayed, "third", snapshot("disconnected"))
        assert [message.model_dump() for message in log.replay().messages] == [
            message.model_dump() for message in replayed]
    finally:
        log.close()


def test_compacted_snapshot_recovery_has_stable_anchor_for_later_turns():
    state = snapshot("disconnected")
    history = [Message(role="system", content="system"),
               Message(role="user", content="retained user", host_connector_status=state)]
    before = [message.model_dump() for message in history]
    projected = request(history)
    assert state in projected[-1]["content"]
    assert [message.model_dump() for message in history] == before
    history.extend([Message(role="assistant", content="done"),
                    Message(role="user", content="next", host_connector_status=state)])
    assert request(history)[:len(projected)] == projected
    before_change = request(history)
    changed = snapshot("connected")
    history.append(Message(role="user", content=append_connector_status(history, "changed", changed),
                           host_connector_status=changed))
    assert request(history)[:len(before_change)] == before_change


def test_recovery_preserves_multimodal_blocks():
    state = snapshot("connected")
    blocks = [{"type": "image", "data": "opaque"}, {"type": "text", "text": "user"}]
    history = [Message(role="user", content=blocks, host_connector_status=state)]
    projected = restore_connector_status(history)
    assert projected[0].content[:2] == blocks
    assert history[0].content == blocks
    assert state in projected[0].content[-1]["text"]


def test_failures_fall_back_without_blocking_or_logging_payloads(monkeypatch, caplog):
    def fail(*args):
        raise ValueError("sensitive diagnostic")

    monkeypatch.setattr("box_agent.connector_context._snapshot_key", fail)
    history = [Message(role="user", content="first", host_connector_status=snapshot("connected"))]
    assert snapshot("disconnected") in append_connector_status(history, "second", snapshot("disconnected"))
    assert restore_connector_status(history) is history
    assert DefaultContextEngine().prepare_summary(history, None) is history
    assert "sensitive diagnostic" not in caplog.text


def test_cancellation_is_not_swallowed(monkeypatch):
    def cancel(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr("box_agent.connector_context._snapshot_key", cancel)
    history = [Message(role="user", content="first", host_connector_status=snapshot("connected"))]
    with pytest.raises(asyncio.CancelledError):
        append_connector_status(history, "second", snapshot("connected"))
    with pytest.raises(asyncio.CancelledError):
        restore_connector_status(history)


def test_recovery_failure_discards_partial_projection(monkeypatch):
    import box_agent.connector_context as connector_context

    history = [Message(role="user", content="first", host_connector_status=snapshot("connected")),
               Message(role="user", content="second", host_connector_status=snapshot("disconnected"))]
    before = [message.model_dump() for message in history]
    calls = 0

    def fail_on_second(message):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("recovery failed")
        return False

    monkeypatch.setattr(connector_context, "_contains_snapshot", fail_on_second)
    assert restore_connector_status(history) is history
    assert [message.model_dump() for message in history] == before


@pytest.mark.asyncio
async def test_recovery_failure_still_reaches_model(monkeypatch, agent):
    from box_agent.runtime import run_agent_loop

    agent.add_user_message("first", host_connector_status=snapshot("connected"))

    def fail(*args):
        raise ValueError("diagnostic")

    monkeypatch.setattr("box_agent.connector_context._snapshot_key", fail)
    agent.add_user_message("second", host_connector_status=snapshot("disconnected"))
    before = [_trace_request_message(message) for message in agent.messages]
    requests = []

    class Provider:
        async def generate_stream(self, messages, tools=None, **kwargs):
            requests.append([_trace_request_message(message) for message in messages])
            yield StreamEvent(type="text", delta="continued")
            yield StreamEvent(type="finish", finish_reason="stop")

    events = [event async for event in run_agent_loop(
        llm=Provider(), messages=agent.messages, tools={}, token_limit=20000, max_steps=1)]
    assert requests == [before]
    assert any(getattr(event, "content", None) == "continued" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("token_limit", [2000, 20000])
async def test_real_compaction_retains_current_state_without_summary_cooperation(agent, token_limit):
    from box_agent.runtime import run_agent_loop

    requests = []

    class Provider:
        async def generate_stream(self, messages, tools=None, **kwargs):
            requests.append((kwargs.get("call_kind"), [_trace_request_message(m) for m in messages]))
            delta = "<summary>Earlier work completed.</summary>" if kwargs.get("call_kind") == "context_summary" else "done"
            yield StreamEvent(type="text", delta=delta)
            yield StreamEvent(type="finish", finish_reason="stop")

    agent.add_user_message("first", host_connector_status=snapshot("connected"))
    agent.messages.append(Message(role="assistant", content="x" * 20000))
    agent.add_user_message("latest", host_connector_status=snapshot("connected"))
    assert agent.messages[-1].content == "latest"
    _ = [event async for event in run_agent_loop(
        llm=Provider(), messages=agent.messages, tools={}, token_limit=token_limit, max_steps=1)]
    assert any(kind == "context_summary" for kind, _ in requests) == (token_limit == 2000)
    main = next(messages for kind, messages in requests if kind != "context_summary")
    assert any(snapshot("connected") in message["content"] for message in main)
    agent.add_user_message("after", host_connector_status=snapshot("connected"))
    assert agent.messages[-1].content == "after"
    assert request(agent.messages)[:len(main)] == main
    before_change = request(agent.messages)
    agent.add_user_message("changed after compaction", host_connector_status=snapshot("disconnected"))
    assert request(agent.messages)[:len(before_change)] == before_change
