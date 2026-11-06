"""Observe original history, actual provider input, receipts and restart replay."""
from copy import deepcopy

import pytest

from box_agent.api import UserInput
from box_agent.agent import Agent
from box_agent.input_preparation import prepare_user_input
from box_agent.input_resources import verify_input_versions
from box_agent.session_log import SessionLog
from tests.test_acp_history_edit import CaptureLLM, adapter
from tests.test_image_context import image_block


def link(path):
    return {"type": "resource_link", "uri": path.as_uri(), "name": path.name, "mimeType": "text/plain"}


@pytest.mark.asyncio
async def test_resource_image_preparation_result_is_durable_and_replayed_without_execution(tmp_path):
    import base64
    picture = tmp_path / "图片.png"
    picture.write_bytes(base64.b64decode(image_block()["data"]))
    llm = CaptureLLM()
    llm.capabilities = {"image_input": True}
    host = adapter(tmp_path, llm)
    created = await host.new_session(str(tmp_path), session_id="image-result")
    await host.prompt(created.session_id, [{**link(picture), "mimeType": "image/png"}], turn_id="image-turn")
    store = host._sessions[created.session_id].agent.session_log
    message = next(message for message in store.replay().messages if message.input_facts)
    result = message.input_facts["image_tool"]
    assert result["success"] is True
    assert result["arguments"]["image_paths"] == [str(picture)]
    assert image_block()["data"] not in store.path.read_text()
    await host.aclose()
    picture.unlink()
    restored = adapter(tmp_path, llm)
    updates = []
    async def update(session_id, update, **meta):
        updates.append(update.model_dump(by_alias=True, exclude_none=True))
    restored._conn.session_update = update
    calls = len(llm.requests)
    try:
        await restored.load_session(str(tmp_path), created.session_id)
        tool = [item for item in updates if item.get("toolCallId") == result["toolCallId"]]
        assert [item["sessionUpdate"] for item in tool] == ["tool_call", "tool_call_update"]
        assert tool[-1]["status"] == "completed"
        assert len(llm.requests) == calls
    finally:
        await restored.aclose()


@pytest.mark.asyncio
async def test_original_and_order_survive_restart_while_model_gets_projection(tmp_path):
    path = tmp_path / "中文.txt"
    path.write_text("CEDAR-719")
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    response = await host.new_session(str(tmp_path), session_id="input-original")
    blocks = [{"type": "text", "text": "first"}, link(path), {"type": "text", "text": "last"}]
    await host.prompt(response.session_id, blocks, turn_id="t1", ui_language="en")
    state = host._sessions[response.session_id]
    messages = state.agent.session_log.replay().messages
    user = next(message for message in messages if message.input_facts)
    assert user.content == "first\nlast"
    assert user.input_facts["blocks"] == blocks
    assert "Host UI language" not in user.content
    request = next(message for message in llm.requests[-1] if message.input_facts)
    assert str(request.content).index("first") < str(request.content).index("中文.txt") < str(request.content).index("last")
    assert any(event["type"] == "input/adopted" for event in state.agent.session_log.events)
    await host.aclose()
    replay = adapter(tmp_path, llm)
    updates = []
    async def update(session_id, update, **meta):
        updates.append(update)
    replay._conn.session_update = update
    calls = len(llm.requests)
    await replay.load_session(str(tmp_path), response.session_id)
    first = [item.model_dump() for item in updates]
    updates.clear()
    await replay.load_session(str(tmp_path), response.session_id)
    assert [item.model_dump() for item in updates] == first
    assert len(llm.requests) == calls
    await replay.prompt(response.session_id, [{"type": "text", "text": "continue"}])
    assert len(llm.requests) == calls + 1
    await replay.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["resize", "same_size", "delete"])
async def test_lost_injection_response_retry_reports_prior_receipt(tmp_path, mutation):
    path = tmp_path / "file.txt"
    path.write_text("one")
    host = adapter(tmp_path, CaptureLLM())
    response = await host.new_session(str(tmp_path), session_id="receipt")
    state = host._sessions[response.session_id]
    state.turn_active = True
    params = {"sessionId": response.session_id, "injectionId": "i1", "prompt": [link(path)]}
    accepted = await host.ext_method("inject", deepcopy(params))
    assert accepted["ok"]
    if mutation == "delete":
        path.unlink()
    else:
        path.write_text("two" if mutation == "same_size" else "changed length")
    retry = await host.ext_method("inject", deepcopy(params))
    assert retry == {"ok": True, "injectionId": "i1", "deduplicated": True, "status": "pending"}
    changed = deepcopy(params)
    changed["prompt"].append({"type": "text", "text": "different request"})
    assert (await host.ext_method("inject", changed))["error"] == "INJECTION_IDEMPOTENCY_CONFLICT"
    assert state.inject_queue.qsize() == 1
    state.turn_active = False
    await host.aclose()


def test_same_size_resource_replacement_is_detected_before_adoption(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("one")
    record, _ = prepare_user_input(UserInput((link(path),)), tmp_path)
    path.write_text("two")
    with pytest.raises(ValueError, match="INPUT_RESOURCE_VERSION_CHANGED"):
        verify_input_versions(record["context"]["files"], tmp_path)


@pytest.mark.asyncio
async def test_direct_api_keeps_text_image_text_order_and_no_pixels_in_log(tmp_path):
    image = image_block()
    blocks = ({"type": "text", "text": "before-image"},
              {"type": "image", "mimeType": image["media_type"], "data": image["data"]},
              {"type": "text", "text": "after-image"})
    llm = CaptureLLM()
    llm.capabilities = {"image_input": True}
    log = SessionLog.create(tmp_path / "sessions", session_id="ordered-image", cwd=tmp_path)
    agent = Agent(llm_client=llm, system_prompt="system", tools=[], workspace_dir=str(tmp_path), session_log=log)
    await agent.aadd_user_input(UserInput(blocks))
    await agent.run()
    projected = next(message for message in llm.requests[-1] if message.input_facts)
    assert [block["type"] for block in projected.content] == ["text", "input_image", "text"]
    assert image["data"] not in log.path.read_text()
    assert log.replay().messages[0].content == "before-image\nafter-image"
    log.close()


@pytest.mark.asyncio
async def test_unknown_load_never_creates_a_new_log(tmp_path):
    host = adapter(tmp_path, CaptureLLM())
    with pytest.raises(Exception):
        await host.load_session(str(tmp_path), "acp-v1-bWlzc2luZw")
    assert not host._sessions
    await host.aclose()


@pytest.mark.asyncio
async def test_corrupt_load_preserves_original_log_and_does_not_call_provider(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    created = await host.new_session(str(tmp_path), session_id="corrupt-input")
    await host.prompt(created.session_id, [{"type": "text", "text": "saved"}])
    path = host._sessions[created.session_id].agent.session_log.path
    await host.aclose()
    with path.open("ab") as stream:
        stream.write(b"not-json\n")
    original, calls = path.read_bytes(), len(llm.requests)
    restored = adapter(tmp_path, llm)
    try:
        with pytest.raises(Exception, match="SESSION_LOG_CORRUPTED"):
            await restored.load_session(str(tmp_path), created.session_id)
        assert path.read_bytes() == original
        assert len(llm.requests) == calls
        assert not restored._sessions
    finally:
        await restored.aclose()


@pytest.mark.asyncio
async def test_taken_cancel_and_failed_commit_never_acknowledge_effective_input(tmp_path):
    import asyncio
    from box_agent.injections import InjectionStatus
    from box_agent.api import UserInput
    log = SessionLog.create(tmp_path / 'sessions', session_id='cancel-commit', cwd=tmp_path)
    agent = Agent(llm_client=CaptureLLM(), system_prompt='system', tools=[], workspace_dir=str(tmp_path), session_log=log)
    queue = agent.inject_queue
    entered, release = asyncio.Event(), asyncio.Event()
    async def prepare(*_):
        entered.set()
        await release.wait()
    queue.prepare_input = prepare
    agent.inject(UserInput.text('cancelled original', input_id='cancel'))
    history = []
    applying = asyncio.create_task(queue.aapply_next(history, commit=lambda: pytest.fail('cancelled input committed')))
    await entered.wait()
    assert queue.status('cancel') is InjectionStatus.TAKEN
    assert queue.cancel('cancel')
    release.set()
    _, event = await applying
    assert event.status == 'cancelled'
    assert history == []
    queue.prepare_input = None
    agent.inject(UserInput.text('failed original', input_id='failed'))
    def fail():
        raise OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        await queue.aapply_next(history, commit=fail)
    assert queue.status('failed') is InjectionStatus.DISCARDED
    assert not history and not log.replay().messages
    log.close()


@pytest.mark.asyncio
async def test_committed_supplement_can_be_confirmed_after_restart_and_file_deletion(tmp_path):
    from box_agent.injections import submit_user_input
    path = tmp_path / 'same file.csv'
    path.write_text('code,value\nC,719\n')
    host = adapter(tmp_path, CaptureLLM())
    created = await host.new_session(str(tmp_path))
    state = host._sessions[created.session_id]
    value = UserInput((link(path),), input_id='restart-receipt')
    submit_user_input(value, agent=state.agent, queue=state.inject_queue)
    def commit():
        state.agent.session_log.append_unlogged_messages(state.agent.messages[1:], turn=1, step=1)
        state.agent.session_log.flush()
    await state.inject_queue.aapply_next(state.agent.messages, commit=commit)
    await host.aclose()
    path.unlink()
    restored = adapter(tmp_path, CaptureLLM())
    await restored.load_session(str(tmp_path), created.session_id)
    receipt = await restored.ext_method('inject', {'sessionId': created.session_id,
        'injectionId': value.input_id, 'prompt': list(value.blocks)})
    assert receipt['status'] == 'injected' and receipt['deduplicated']
    assert restored._sessions[created.session_id].inject_queue.empty()
    await restored.aclose()


@pytest.mark.asyncio
async def test_load_rejects_wrong_workspace_active_and_interrupted_replay(tmp_path):
    host = adapter(tmp_path, CaptureLLM())
    created = await host.new_session(str(tmp_path))
    state = host._sessions[created.session_id]
    await host.prompt(created.session_id, [{'type': 'text', 'text': 'original'}])
    state.turn_active = True
    with pytest.raises(Exception, match='active task'):
        await host.load_session(str(tmp_path), created.session_id)
    assert host._sessions[created.session_id] is state
    state.turn_active = False
    with pytest.raises(Exception, match='SESSION_WORKSPACE_MISMATCH'):
        await host.load_session(str(tmp_path / 'other'), created.session_id)
    assert host._sessions[created.session_id] is state
    async def disconnect(*_, **__):
        raise ConnectionError('replay interrupted')
    host._conn.session_update = disconnect
    with pytest.raises(ConnectionError, match='interrupted'):
        await host.load_session(str(tmp_path), created.session_id)
    assert created.session_id not in host._sessions
    restored = adapter(tmp_path, CaptureLLM())
    await restored.load_session(str(tmp_path), created.session_id)
    assert restored._sessions[created.session_id].agent.session_log.replay().messages[0].content == 'original'
    await restored.aclose()
    await host.aclose()


@pytest.mark.asyncio
async def test_required_image_budget_failure_never_calls_provider(tmp_path):
    from box_agent.config import ImageContextConfig
    llm = CaptureLLM()
    llm.capabilities = {'image_input': True}
    log = SessionLog.create(tmp_path / 'sessions', session_id='budget', cwd=tmp_path)
    agent = Agent(llm_client=llm, system_prompt='system', tools=[], workspace_dir=str(tmp_path), session_log=log)
    agent.image_context.config = ImageContextConfig(max_encoded_bytes=1)
    block = image_block()
    await agent.aadd_user_input(UserInput(({'type': 'image', 'mimeType': block['media_type'], 'data': block['data']},)))
    events = [event async for event in agent.run_events()]
    assert not llm.requests
    assert 'INPUT_IMAGE_NOT_ADOPTED' in str(events)
    assert not any(event['type'] == 'input/adopted' for event in log.events)
    log.close()


@pytest.mark.asyncio
async def test_normal_prompt_retry_uses_original_identity_after_file_deletion(tmp_path):
    path = tmp_path / 'file.txt'
    path.write_text('original')
    host = adapter(tmp_path, CaptureLLM())
    session = await host.new_session(str(tmp_path))
    blocks = [link(path)]
    await host.prompt(session.session_id, blocks, turn_id='t')
    path.unlink()
    with pytest.raises(ValueError, match='TURN_ALREADY_ACCEPTED'):
        await host.prompt(session.session_id, blocks, turn_id='t')
    await host.aclose()


@pytest.mark.asyncio
async def test_shared_run_uses_ordered_input_and_original_facts(tmp_path):
    from box_agent.agent_service import AgentService
    from box_agent.agent_session import AgentSession
    from box_agent.api import RunRequest
    llm = CaptureLLM()
    log = SessionLog.create(tmp_path / 'sessions', session_id='run-input', cwd=tmp_path)
    agent = Agent(llm_client=llm, system_prompt='system', tools=[], workspace_dir=str(tmp_path), session_log=log)
    session = AgentSession(agent)
    value = UserInput.text('original run input', input_id='input-run')
    handle = await AgentService().start(RunRequest('run', 'session', user_input=value), session=session)
    await handle.result()
    assert log.replay().messages[0].content == 'original run input'
    assert llm.requests and 'original run input' in str(llm.requests[-1])
    await session.aclose()


def test_compaction_keeps_original_display_history_and_shared_image_blob(tmp_path):
    from box_agent.schema import Message
    log = SessionLog.create(tmp_path / 'sessions', session_id='history', cwd=tmp_path)
    agent = Agent(llm_client=CaptureLLM(), system_prompt='system', tools=[], workspace_dir=str(tmp_path), session_log=log)
    block = image_block()
    for number in range(2):
        agent.add_user_input(UserInput(({'type': 'image', 'mimeType': block['media_type'], 'data': block['data']},), input_id=f'input-{number}'))
    originals = log.replay().messages
    refs = [message.input_facts['images'][0] for message in originals]
    assert refs[0]['contentRef'] == refs[1]['contentRef']
    log.append('compaction/summary', {'turn': 1})
    log.replace_surface([Message(role='user', content='derived summary')], turn=1, step=1)
    log.append('compaction/end', {'turn': 1})
    log.flush()
    assert [message for _, message, _ in log.history_messages()] == originals
    assert agent.image_context.store.load_block(refs[1])['data'] == block['data']
    log.close()


def test_provider_retry_keeps_input_version_until_a_valid_response(tmp_path):
    from box_agent.context_input import DefaultContextEngine
    from box_agent.tools.engine.preparation import prepare_tools
    path = tmp_path / 'retry.txt'
    path.write_text('one')
    log = SessionLog.create(tmp_path / 'sessions', session_id='retry-version', cwd=tmp_path)
    agent = Agent(llm_client=CaptureLLM(), system_prompt='system', tools=[], workspace_dir=str(tmp_path), session_log=log)
    agent.add_user_input(UserInput((link(path),), input_id='pending'))
    engine = DefaultContextEngine(image_context=agent.image_context)
    engine.configure_run(session_store=log)
    def prepare():
        return engine.prepare_request(agent.messages, prepared_tools=prepare_tools([]), token_limit=20000)
    first = prepare()
    assert not first.blocked_reason
    first.on_committed()
    path.write_text('two')  # Same length; first request did not produce a valid response.
    assert 'INPUT_RESOURCE_VERSION_CHANGED' in prepare().blocked_reason
    path.write_text('one')
    retry = prepare()
    retry.on_committed()
    assert len([e for e in log.events if e['type'] == 'input/adopted']) == 1
    retry.on_response()
    path.write_text('two')  # A later tool read follows ordinary shared-workspace semantics.
    assert not prepare().blocked_reason
    log.close()


@pytest.mark.asyncio
async def test_failed_input_preparation_does_not_claim_the_turn_was_accepted(tmp_path, monkeypatch):
    host = adapter(tmp_path, CaptureLLM())
    session = await host.new_session(str(tmp_path))
    agent = host._sessions[session.session_id].agent
    add = agent.aadd_user_message
    async def fail(*args, **kwargs):
        raise ValueError('preparation failed')
    monkeypatch.setattr(agent, 'aadd_user_message', fail)
    with pytest.raises(ValueError, match='preparation failed'):
        await host.prompt(session.session_id, [{'type': 'text', 'text': 'original'}], turn_id='retry')
    assert not agent.session_log.has_committed_input('retry')
    monkeypatch.setattr(agent, 'aadd_user_message', add)
    result = await host.prompt(session.session_id, [{'type': 'text', 'text': 'original'}], turn_id='retry')
    assert result.stop_reason == 'end_turn'
    assert len([e for e in agent.session_log.events if e['type'] == 'turn/binding']) == 1
    await host.aclose()
