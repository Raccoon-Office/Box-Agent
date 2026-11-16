"""Resume's inclusive turn cursor shares load's effective-history wire format."""

import asyncio

import pytest

from box_agent.acp import RequestError
from tests.test_acp_history_edit import CaptureLLM, adapter


async def seeded(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    session = await host.new_session(str(tmp_path))
    for turn in ("t1", "t2", "t3"):
        await host.prompt(session.session_id, [{"type": "text", "text": turn}], turn_id=turn)
    updates = []

    async def collect(session_id, update, **meta):
        updates.append(update.model_dump(by_alias=True, exclude_none=True))

    host._conn.session_update = collect
    return host, llm, session.session_id, updates


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["t1", "t2", "t3"])
async def test_resume_replays_exact_load_suffix_without_running_model(tmp_path, cursor):
    host, llm, session_id, updates = await seeded(tmp_path)
    try:
        await host.load_session(str(tmp_path), session_id)
        start = next(i for i, update in enumerate(updates) if update['_meta']['turnId'] == cursor)
        expected = updates[start:]
        updates.clear()
        count = len(llm.requests)
        for _ in range(2):
            result = await host.resume_session(str(tmp_path), session_id, replayFromTurnId=cursor)
            assert result.field_meta['replayed'] is True
            assert result.field_meta['replayFromTurnId'] == cursor
            assert updates == expected
            assert updates[0]['sessionUpdate'] == 'user_message_chunk'
            assert len(llm.requests) == count
            updates.clear()
        await host.resume_session(str(tmp_path), session_id)
        assert not updates
        await host.prompt(session_id, [{'type': 'text', 'text': 'next'}], turn_id='t4')
        assert len(llm.requests) == count + 1
        # Filtering the replay never truncates the restored model context.
        assert {'t1', 't2', 't3'} <= {
            m.input_facts['id'] for m in llm.requests[-1] if m.input_facts
        }
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('cursor', [None, '', '   ', 123, True, {}, []])
async def test_malformed_cursor_preserves_session_without_sending_history(tmp_path, cursor):
    host, llm, session_id, updates = await seeded(tmp_path)
    state = host._sessions[session_id]
    try:
        with pytest.raises(RequestError, match='non-empty string'):
            await host.resume_session(str(tmp_path), session_id, replayFromTurnId=cursor)
        assert host._sessions[session_id] is state
        assert not updates
        assert len(llm.requests) == 3
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['unknown', 'revised', 'reset'])
async def test_unavailable_cursor_never_sends_partial_history_and_allows_retry(tmp_path, kind):
    host, llm, session_id, updates = await seeded(tmp_path)
    try:
        cursor = 'absent' if kind == 'unknown' else 't2'
        if kind == 'revised':
            await host.prompt(session_id, [{'type': 'text', 'text': 'replacement'}], turn_id='t4',
                              history_edit={'target_turn_id': 't2', 'expected_revision_id': 'initial'})
        if kind == 'reset':
            host._sessions[session_id].agent.session_log.append('surface/reset', {})
            host._sessions[session_id].agent.session_log.flush()
        updates.clear()
        count = len(llm.requests)
        with pytest.raises(RequestError, match='REPLAY_CURSOR_NOT_FOUND'):
            await host.resume_session(str(tmp_path), session_id, replayFromTurnId=cursor)
        assert not updates
        assert len(llm.requests) == count
        # Error cleanup releases the writer lock; full recovery remains possible.
        await host.load_session(str(tmp_path), session_id)
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_active_session_rejects_cursor_resume_without_replacing_state(tmp_path):
    host, llm, session_id, updates = await seeded(tmp_path)
    state = host._sessions[session_id]
    try:
        state.turn_active = True
        with pytest.raises(RequestError, match='active task'):
            await host.resume_session(str(tmp_path), session_id, replayFromTurnId='t2')
        assert host._sessions[session_id] is state
        assert not updates
        assert len(llm.requests) == 3
    finally:
        state.turn_active = False
        await host.aclose()


@pytest.mark.asyncio
async def test_replay_rejects_interleaved_prompt_and_resume_then_releases_guard(tmp_path):
    host, llm, session_id, updates = await seeded(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_send(*args, **kwargs):
        entered.set()
        await release.wait()

    host._conn.session_update = blocked_send
    task = asyncio.create_task(host.resume_session(str(tmp_path), session_id, replayFromTurnId='t2'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(RequestError, match='active task'):
            await host.prompt(session_id, [{'type': 'text', 'text': 'must not run'}])
        with pytest.raises(RequestError, match='active task'):
            await host.resume_session(str(tmp_path), session_id, replayFromTurnId='t2')
        assert len(llm.requests) == 3
        release.set()
        await task
        assert not host._sessions[session_id]._prompt_in_progress
    finally:
        release.set()
        await task
        await host.aclose()


@pytest.mark.asyncio
async def test_interrupted_cursor_replay_can_be_retried(tmp_path):
    host, llm, session_id, updates = await seeded(tmp_path)

    async def disconnect(*args, **kwargs):
        raise ConnectionError('disconnected')

    collect = host._conn.session_update
    host._conn.session_update = disconnect
    try:
        with pytest.raises(ConnectionError):
            await host.resume_session(str(tmp_path), session_id, replayFromTurnId='t2')
        assert session_id not in host._sessions
        host._conn.session_update = collect
        await host.resume_session(str(tmp_path), session_id, replayFromTurnId='t2')
        assert {update['_meta']['turnId'] for update in updates} == {'t2', 't3'}
    finally:
        await host.aclose()
