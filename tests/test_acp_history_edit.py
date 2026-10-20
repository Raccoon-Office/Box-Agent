"""Edited branches must reach the model without any discarded future history."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent, RequestError
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import StreamEvent
from box_agent.schema import Message
from box_agent.session_history import prompt_fingerprint, revision_request, revise_agent_history


class Connection:
    async def sessionUpdate(self, payload):
        pass


class CaptureLLM:
    def __init__(self):
        self.requests = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append(deepcopy(messages))
        yield StreamEvent(type="text", delta="generated-answer")
        yield StreamEvent(type="finish", finish_reason="stop")


def adapter(tmp_path, llm):
    return BoxACPAgent(Connection(), Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(tmp_path), max_steps=2),
        tools=ToolsConfig(enable_sub_agent=False),
    ), llm, [], "system")


def request(session, text, turn_id, edit=None):
    meta = {"turn_id": turn_id}
    if edit is not None:
        meta["history_edit"] = edit
    return SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": text}], field_meta=meta)


def edit(target, revision="initial"):
    return {"target_turn_id": target, "expected_revision_id": revision}


@pytest.mark.asyncio
async def test_edit_keeps_session_and_jsonl_and_survives_restart(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "same-session"}))
        await host.prompt(request(session, "kept-question", "t1"))
        await host.prompt(request(session, "discarded-question", "t2"))
        state = host._sessions[session.sessionId]
        log = state.agent.session_log
        original = log.path.read_bytes()
        result = await host.prompt(request(session, "edited-question", "t3", edit("t2")))
        assert result.field_meta["history_revision_id"] == "t3"
        assert state.agent.session_log is log
        assert log.path.read_bytes().startswith(original)
        assert log.header["id"] == "same-session"
        assert len([e for e in log.events if e["type"] == "history/revise"]) == 1
        assert "kept-question" in str(llm.requests[-1])
        assert "discarded-question" not in str(llm.requests[-1])
        resumed = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "same-session"}))
        assert resumed.field_meta["history_revision_id"] == "t3"
        await host.prompt(request(resumed, "follow-up", "t4"))
        assert "edited-question" in str(llm.requests[-1])
        assert "discarded-question" not in str(llm.requests[-1])
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_first_turn_and_repeated_edits_use_exact_prefix(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "first"}))
        await host.prompt(request(session, "old-first", "t1"))
        await host.prompt(request(session, "new-first", "t2", edit("t1")))
        assert [m.role for m in llm.requests[-1]] == ["system", "user"]
        await host.prompt(request(session, "newer-first", "t3", edit("t2", "t2")))
        assert [m.role for m in llm.requests[-1]] == ["system", "user"]
        assert "new-first" not in str(llm.requests[-1])
        assert "newer-first" in str(llm.requests[-1])
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_edit_excludes_future_compaction_and_restores_goal_plan_todos(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "compacted"}))
        await host.prompt(request(session, "kept-question", "t1"))
        state = host._sessions[session.sessionId]
        log = state.agent.session_log
        state.agent.set_goal("kept-goal")
        state.agent.pause_goal()
        kept_goal = log.replay().goal
        await host.prompt(request(session, "discarded-question", "t2"))
        state.agent.set_goal("future-goal")
        log.append("plan/write", {"plan": None})
        log.append("todo/write", {"todos": []})
        log.replace_surface([Message(role="user", content="future-summary-of-discarded-question")], turn=2, step=1)
        state.agent.messages[:] = [state.agent.messages[0], *log.replay().messages]
        await host.prompt(request(session, "edited-question", "t3", edit("t2")))
        assert "kept-question" in str(llm.requests[-1])
        assert "future-summary" not in str(llm.requests[-1])
        assert "discarded-question" not in str(llm.requests[-1])
        assert log.replay().goal["objective"] == kept_goal["objective"]
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing-anchor", "stale-revision", "malformed", "old-v2"])
async def test_invalid_edits_do_not_append_input_or_revise_history(tmp_path, kind):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "invalid"}))
        await host.prompt(request(session, "original", "t1"))
        log = host._sessions[session.sessionId].agent.session_log
        before = log.path.read_bytes()
        revision = edit("foreign" if kind == "missing-anchor" else "t1", "stale" if kind == "stale-revision" else "initial")
        if kind == "malformed":
            revision["messages"] = []
        req = request(session, "must-not-run", "t2", revision)
        if kind == "old-v2":
            req.field_meta = {"session_continuation": {"schema_version": "officev3-session-continuation/v2"}}
        with pytest.raises(RequestError, match="HISTORY"):
            await host.prompt(req)
        assert log.path.read_bytes() == before
        assert len(llm.requests) == 1
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_duplicate_edit_cannot_rewind_or_execute_twice(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "dedupe"}))
        await host.prompt(request(session, "old", "t1"))
        req = request(session, "new", "t2", edit("t1"))
        await host.prompt(req)
        log = host._sessions[session.sessionId].agent.session_log
        before = log.path.read_bytes()
        with pytest.raises(RequestError, match="TURN_ALREADY_ACCEPTED"):
            await host.prompt(req)
        assert log.path.read_bytes() == before
        assert len(llm.requests) == 2
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_edit_waits_for_old_prompt_to_stop(tmp_path):
    started = asyncio.Event()
    class SlowLLM(CaptureLLM):
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(deepcopy(messages))
            if "old-running" in str(messages[-1].content):
                started.set()
                await asyncio.Event().wait()
            yield StreamEvent(type="text", delta="answer")
            yield StreamEvent(type="finish", finish_reason="stop")
    llm = SlowLLM()
    host = adapter(tmp_path, llm)
    old = None
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "running"}))
        old = asyncio.create_task(host.prompt(request(session, "old-running", "t1")))
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait_for(host.prompt(request(session, "new", "t2", edit("t1"))), 5)
        assert old.done()
        assert "old-running" not in str(llm.requests[-1])
        assert "new" in str(llm.requests[-1])
    finally:
        if old is not None and not old.done():
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
        await host.aclose()


@pytest.mark.asyncio
async def test_retry_after_revision_commit_and_restart_does_not_revise_twice(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "retry"}))
        await host.prompt(request(session, "old", "t1"))
        state = host._sessions[session.sessionId]
        req = request(session, "new", "t2", edit("t1"))
        revision = revision_request(list(state.agent.session_log.events), target_turn_id="t1",
                                    turn_id="t2", expected_revision_id="initial",
                                    fingerprint=prompt_fingerprint(req.prompt))
        await revise_agent_history(state.agent, revision)
        resumed = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "retry"}))
        await host.prompt(request(resumed, "new", "t2", edit("t1")))
        events = host._sessions[resumed.sessionId].agent.session_log.events
        assert sum(e["type"] == "history/revise" for e in events) == 1
        assert "old" not in str(llm.requests[-1])
        assert len(llm.requests) == 2
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_edit_error_reports_authoritative_revision_without_losing_history(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "error-revision"}))
        await host.prompt(request(session, "old", "t1"))
        with pytest.raises(RequestError) as caught:
            await host.prompt(request(session, "new", "t2", edit("missing")))
        assert caught.value.data["history_revision_id"] == "initial"
        await host.prompt(request(session, "follow-up", "t3"))
        assert "old" in str(llm.requests[-1])
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_stop_during_edit_preparation_does_not_commit_or_execute(tmp_path, monkeypatch):
    import box_agent.acp as acp_module

    entered, release = asyncio.Event(), asyncio.Event()
    original = acp_module.revise_agent_history

    async def preparing(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(acp_module, "revise_agent_history", preparing)
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "stop-edit"}))
        await host.prompt(request(session, "old", "t1"))
        editing = asyncio.create_task(host.prompt(request(session, "new", "t2", edit("t1"))))
        await asyncio.wait_for(entered.wait(), 5)
        await host.cancel(SimpleNamespace(sessionId=session.sessionId))
        release.set()
        with pytest.raises(RequestError, match="HISTORY_CANCELLED"):
            await editing
        log = host._sessions[session.sessionId].agent.session_log
        assert not any(e["type"] == "history/revise" for e in log.events)
        assert len(llm.requests) == 1
        assert "old" in str(log.replay().messages)
    finally:
        release.set()
        await host.aclose()


@pytest.mark.asyncio
async def test_duplicate_edit_does_not_cancel_its_already_running_turn(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingLLM(CaptureLLM):
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(deepcopy(messages))
            if "new-running" in str(messages[-1].content):
                entered.set()
                await release.wait()
            yield StreamEvent(type="text", delta="answer")
            yield StreamEvent(type="finish", finish_reason="stop")

    llm = WaitingLLM()
    host = adapter(tmp_path, llm)
    running = None
    try:
        session = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "duplicate-running"}))
        await host.prompt(request(session, "old", "t1"))
        req = request(session, "new-running", "t2", edit("t1"))
        running = asyncio.create_task(host.prompt(req))
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(RequestError, match="TURN_ALREADY_ACCEPTED"):
            await host.prompt(req)
        assert not host._sessions[session.sessionId].cancelled
        assert not running.done()
        release.set()
        await asyncio.wait_for(running, 5)
        assert len(llm.requests) == 2
    finally:
        release.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await host.aclose()
