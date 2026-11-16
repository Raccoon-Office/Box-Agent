"""Observable contracts: no imports from production Agent/tool implementations."""
import asyncio
import hashlib
import json

import pytest


def tool(name, **args):
    return {'tool': name, 'args': args}


def observed(engine, value):
    return value in json.dumps(engine.notifications, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('allow', [False, True], ids=['denied', 'allowed'])
async def test_permission_controls_real_external_write(lab, allow):
    root, model, create = lab
    engine = await create()
    workspace = root / 'workspace'
    session = await engine.open(workspace, 'permission')
    target = root / 'external' / 'result.txt'
    target.parent.mkdir()
    model.script('write', tool('write_file', path=str(target), content='AUTHORIZED_WRITE'), {'text': 'Done.'})
    pending = asyncio.create_task(engine.prompt(session, 'write'))
    try:
        request = await engine.permission()
        assert not target.exists(), 'Side effect happened before permission response'
        assert str(target) in json.dumps(request)
        await engine.answer(request, allow=allow)
        result = await pending
        assert result['stopReason'] == 'end_turn'
        if allow:
            assert target.read_text() == 'AUTHORIZED_WRITE'
        else:
            assert not target.exists()
            assert observed(engine, 'failed')
    finally:
        if not pending.done():
            await engine.cancel(session)
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_waiting_for_permission_does_not_write(lab):
    root, model, create = lab
    engine = await create()
    session = await engine.open(root / 'workspace', 'permission-cancel')
    target = root / 'external' / 'never.txt'
    target.parent.mkdir()
    model.script('cancel-permission', tool('write_file', path=str(target), content='NEVER'), {'text': 'Done.'})
    pending = asyncio.create_task(engine.prompt(session, 'cancel-permission'))
    request = None
    try:
        request = await engine.permission()
        assert not target.exists()
        await engine.cancel(session)
        result = await asyncio.wait_for(asyncio.shield(pending), 10)
        assert result['stopReason'] == 'cancelled'
        # A late approval must not revive the settled operation.
        await engine.answer(request, allow=True)
        model.script('after-cancel', {'text': 'Still usable.'})
        await engine.prompt(session, 'after-cancel')
        assert not target.exists()
    finally:
        if not pending.done() and request is not None:
            await engine.answer(request, allow=False)
            await engine.cancel(session)
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_before_model_tool_delivery_has_no_side_effect(lab):
    root, model, create = lab
    engine = await create()
    workspace = root / 'workspace'
    session = await engine.open(workspace, 'cancel-stream')
    model.script('held', dict(tool('write_file', path='never.txt', content='NEVER'), hold=True), {'text': 'Done.'})
    pending = asyncio.create_task(engine.prompt(session, 'held'))
    try:
        await model.wait('held')
        await engine.cancel(session)
        result = await asyncio.wait_for(asyncio.shield(pending), 10)
        assert result['stopReason'] == 'cancelled'
        model.release['held'].set()
        model.script('next', {'text': 'Usable after cancellation.'})
        await engine.prompt(session, 'next')
        assert not (workspace / 'never.txt').exists()
    finally:
        model.release['held'].set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_published_artifact_is_real_and_delivered(lab):
    root, model, create = lab
    engine = await create()
    workspace = root / 'workspace'
    session = await engine.open(workspace, 'artifact')
    model.script('artifact', tool('write_file', path='report.md', content='# Result\nTotal: 180\n'),
                 tool('publish_artifact', path='report.md'), {'text': 'Report delivered.'})
    result = await engine.prompt(session, 'artifact')
    assert result['stopReason'] == 'end_turn'
    assert (workspace / 'report.md').read_text() == '# Result\nTotal: 180\n'
    deliveries = [n['params']['update'].get('rawOutput', {}) for n in engine.notifications
                  if 'update' in n.get('params', {})]
    artifact = next(x for x in deliveries if x.get('type') == 'artifact'
                    and x.get('rel_path') == 'report.md' and x.get('placement') == 'primary')
    assert artifact['sha256'] == hashlib.sha256((workspace / 'report.md').read_bytes()).hexdigest()
    assert artifact['session_id'] == 'artifact'
    assert artifact['size'] == (workspace / 'report.md').stat().st_size


@pytest.mark.asyncio
async def test_completed_session_survives_process_restart(lab):
    root, model, create = lab
    engine = await create()
    workspace = root / 'workspace'
    session = await engine.open(workspace, 'durable-session')
    model.script('save', tool('write_file', path='persist.txt', content='DURABLE_SENTINEL_791'), {'text': 'Saved.'})
    await engine.prompt(session, 'save')
    first_pid = engine.process.pid
    await engine.stop(kill=True)
    await engine.start()
    assert engine.process.pid != first_pid
    await engine.restore(session, workspace)
    model.script('read-back', tool('read_file', path='persist.txt'), {'text': 'Read back.'})
    await engine.prompt(session, 'read-back')
    assert (workspace / 'persist.txt').read_text() == 'DURABLE_SENTINEL_791'
    assert observed(engine, 'DURABLE_SENTINEL_791')
    # Model wire, not the fixture answer, proves tool content reached a request.
    assert 'DURABLE_SENTINEL_791' in json.dumps(model.requests[-1]['messages'])


@pytest.mark.asyncio
async def test_two_concurrent_sessions_keep_workspaces_and_results_separate(lab):
    root, model, create = lab
    engine = await create()
    a, b = root / 'workspace-a', root / 'workspace-b'
    sa, sb = await engine.open(a, 'owner-a'), await engine.open(b, 'owner-b')
    model.script('a', tool('write_file', path='same.txt', content='ONLY_A'), {'text': 'A done.'})
    model.script('b', tool('write_file', path='same.txt', content='ONLY_B'), {'text': 'B done.'})
    await asyncio.gather(engine.prompt(sa, 'a'), engine.prompt(sb, 'b'))
    assert (a / 'same.txt').read_text() == 'ONLY_A'
    assert (b / 'same.txt').read_text() == 'ONLY_B'
    per_session = {sid: json.dumps([n for n in engine.notifications
        if n.get('params', {}).get('sessionId') == sid]) for sid in [sa, sb]}
    assert 'ONLY_B' not in per_session[sa]
    assert 'ONLY_A' not in per_session[sb]


@pytest.mark.asyncio
async def test_restore_rejects_wrong_workspace_without_mutating_files(lab):
    root, model, create = lab
    engine = await create()
    original = root / 'workspace'
    session = await engine.open(original, 'wrong-workspace')
    model.script('initial', {'text': 'Initial turn.'})
    await engine.prompt(session, 'initial')
    await engine.stop(kill=True)
    await engine.start()
    other = root / 'other'
    other.mkdir()
    with pytest.raises(RuntimeError, match='WORKSPACE|cwd|workspace'):
        await engine.restore(session, other)
    await engine.restore(session, original)
    assert not list(other.iterdir())


@pytest.mark.asyncio
async def test_plan_gate_blocks_write_until_explicit_approval(lab):
    root, model, create = lab
    engine = await create()
    workspace = root / 'workspace'
    session = await engine.open(workspace, 'plan-gate')
    model.script('plan', tool('write_file', path='must-wait.txt', content='TOO_EARLY'),
                 tool('plan_write', action='set', title='Contract plan',
                      steps=[{'title': 'Write the approved result'}], verification=['Read the file']),
                 {'text': 'Awaiting approval.'})
    await engine.prompt(session, 'plan', forcePlanStart=True, requirePlanApproval=True)
    assert not (workspace / 'must-wait.txt').exists()
    plans = [n['params']['update'].get('rawOutput', {}) for n in engine.notifications
             if 'update' in n.get('params', {})]
    assert any(p.get('type') == 'plan_snapshot' and
               p.get('approval', {}).get('state') == 'pending' for p in plans)
    model.script('approved', tool('write_file', path='must-wait.txt', content='APPROVED_PLAN'), {'text': 'Done.'})
    await engine.prompt(session, 'approved', planApproval={'decision': 'approved'})
    assert (workspace / 'must-wait.txt').read_text() == 'APPROVED_PLAN'


@pytest.mark.asyncio
async def test_cross_workspace_read_is_denied_without_leaking_content(lab):
    root, model, create = lab
    engine = await create()
    a, b = root / 'workspace-a', root / 'workspace-b'
    sa, sb = await engine.open(a, 'reader-a'), await engine.open(b, 'reader-b')
    secret = b / 'private.txt'
    secret.write_text('PRIVATE_B_SENTINEL_852')
    model.script('cross-read', tool('read_file', path=str(secret)), {'text': 'Finished.'})
    pending = asyncio.create_task(engine.prompt(sa, 'cross-read'))
    try:
        request = await engine.permission()
        assert request['params']['sessionId'] == sa
        await engine.answer(request, allow=False)
        await pending
        assert 'PRIVATE_B_SENTINEL_852' not in json.dumps(model.requests)
        model.script('own-read', tool('read_file', path='private.txt'), {'text': 'Read own file.'})
        await engine.prompt(sb, 'own-read')
        assert 'PRIVATE_B_SENTINEL_852' in json.dumps(model.requests[-1]['messages'])
    finally:
        if not pending.done():
            await engine.cancel(sa)
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_one_session_does_not_stop_another(lab):
    root, model, create = lab
    engine = await create()
    a, b = root / 'workspace-a', root / 'workspace-b'
    sa, sb = await engine.open(a, 'cancel-owner-a'), await engine.open(b, 'running-owner-b')
    model.script('hold-a', dict(tool('write_file', path='never.txt', content='NEVER'), hold=True))
    model.script('run-b', tool('write_file', path='survived.txt', content='B_SURVIVED'), {'text': 'Done.'})
    pending = asyncio.create_task(engine.prompt(sa, 'hold-a'))
    try:
        await model.wait('hold-a')
        await engine.prompt(sb, 'run-b')
        await engine.cancel(sa)
        assert (await pending)['stopReason'] == 'cancelled'
        assert (b / 'survived.txt').read_text() == 'B_SURVIVED'
        assert not (a / 'never.txt').exists()
    finally:
        model.release['hold-a'].set()
        if not pending.done():
            await engine.cancel(sa)
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_interrupted_turn_restores_without_replaying_committed_write(lab):
    root, model, create = lab
    engine = await create()
    workspace = root / 'workspace'
    session = await engine.open(workspace, 'interrupted')
    model.script('interrupted', tool('write_file', path='once.txt', content='ONE_WRITE'),
                 {'text': 'Unsent final answer.', 'hold': True})
    pending = asyncio.create_task(engine.prompt(session, 'interrupted'))
    try:
        await model.wait('interrupted')
        assert (workspace / 'once.txt').read_text() == 'ONE_WRITE'
        before = (workspace / 'once.txt').stat().st_mtime_ns
        await engine.stop(kill=True)
        with pytest.raises(EOFError):
            await pending
        await engine.start()
        await engine.restore(session, workspace)
        model.script('recovered', tool('read_file', path='once.txt'), {'text': 'Recovered.'})
        await engine.prompt(session, 'recovered')
        assert (workspace / 'once.txt').stat().st_mtime_ns == before
        assert 'ONE_WRITE' in json.dumps(model.requests[-1]['messages'])
    finally:
        model.release['interrupted'].set()
        await asyncio.gather(pending, return_exceptions=True)
