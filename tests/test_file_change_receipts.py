"""Producer receipts coexist with parallel scripts and version-checked host history."""
import asyncio
import base64
import hashlib
import os
import shlex
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from box_agent.acp import BoxACPAgent, _tool_result_raw_output
from box_agent.config import Config, AgentConfig, ToolsConfig, LLMConfig
from box_agent.schema import StreamEvent, ToolCall, FunctionCall, LLMResponse
from box_agent.tools.base import ToolResult, bind_tool_cancellation
from box_agent.tools.bash_tool import BashTool
from box_agent.tools.file_tools import WriteTool, AppendTool, EditTool
from box_agent.tools.staged_file_write_tool import StagedFileWriteTool
from box_agent.tools.publish_artifact_tool import PublishArtifactTool
from box_agent.tools.file_change_receipts import destination_lock, persist_file_change
from box_agent.tools.browser_result_adapter import _persist_browser_snapshot_output, _persist_browser_screenshot_output


def digest(data):
    return hashlib.sha256(data).hexdigest()


def changes(result):
    assert result.success, result.error
    assert result.raw_output['file_changes_version'] == 2
    return result.raw_output['file_changes']


@pytest.mark.asyncio
async def test_direct_tools_record_their_committed_versions_and_not_replayed_writes(tmp_path):
    writer = WriteTool(workspace_dir=str(tmp_path))
    first = await writer.invoke({'path': 'a.txt', 'content': 'a'})
    assert changes(first) == [{'path': 'a.txt', 'before_sha256': None, 'after_sha256': digest(b'a')}]
    replay = await writer.invoke({'path': 'a.txt', 'content': 'a'})
    assert replay.success and 'file_changes' not in replay.raw_output
    second = await AppendTool(workspace_dir=str(tmp_path)).invoke({'path': 'a.txt', 'content': 'b'})
    assert changes(second)[0] == {'path': 'a.txt', 'before_sha256': digest(b'a'), 'after_sha256': digest(b'ab')}
    third = await EditTool(workspace_dir=str(tmp_path)).invoke({'path': 'a.txt', 'old_str': 'b', 'new_str': 'c'})
    assert changes(third)[0]['after_sha256'] == digest(b'ac')
    noop = await EditTool(workspace_dir=str(tmp_path)).invoke({'path': 'a.txt', 'old_str': 'c', 'new_str': 'c'})
    assert noop.success and not (noop.raw_output or {}).get('file_changes')


@pytest.mark.asyncio
async def test_staged_commit_records_only_its_final_destination(tmp_path):
    writer = StagedFileWriteTool(workspace_dir=str(tmp_path))
    begin = await writer.invoke({'action': 'begin', 'path': 'a.txt', 'expected_chunks': 1})
    write_id = begin.raw_output['write_id']
    appended = await writer.invoke({'action': 'append_text', 'write_id': write_id, 'chunk_index': 0, 'content': 'new'})
    assert 'file_changes' not in appended.raw_output
    result = await writer.invoke({'action': 'commit', 'write_id': write_id})
    assert changes(result) == [{'path': 'a.txt', 'before_sha256': None, 'after_sha256': digest(b'new')}]


@pytest.mark.asyncio
async def test_script_reads_never_adopt_peer_outputs_and_scripts_remain_parallel(tmp_path, monkeypatch, posix_shell):
    from box_agent.tools import file_change_receipts as receipts
    def forbidden(*args, **kwargs):
        raise AssertionError('A script must not hash or acquire destination locks')
    monkeypatch.setattr(receipts, 'file_hash', forbidden)
    monkeypatch.setattr(receipts, 'destination_lock', forbidden)
    a = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    b = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    read, write = await asyncio.wait_for(asyncio.gather(
        a.invoke({'command': 'touch ready; while [ ! -f peer.txt ]; do sleep 0.01; done; cat peer.txt', 'timeout': 5}),
        b.invoke({'command': 'while [ ! -f ready ]; do sleep 0.01; done; printf peer > peer.txt', 'timeout': 5}),
    ), 10)
    assert read.success and write.success
    assert not (read.raw_output or {}).get('file_changes')
    assert not (write.raw_output or {}).get('file_changes')


@pytest.mark.asyncio
@pytest.mark.parametrize('disjoint', [False, True])
async def test_bash_can_call_a_nested_sdk_writer(tmp_path, monkeypatch, disjoint, posix_shell):
    monkeypatch.setenv('BOX_AGENT_HOME', str(tmp_path / 'profile'))
    inner = tmp_path / 'inner' if disjoint else tmp_path
    inner.mkdir(exist_ok=True)
    repo = str(Path(__file__).resolve().parent.parent)
    source = textwrap.dedent(f'''
        import asyncio, sys
        sys.path.insert(0, {repo!r})
        from box_agent.tools.file_tools import WriteTool
        result = asyncio.run(WriteTool(workspace_dir={str(inner)!r}).invoke(
            {{'path': 'nested.txt', 'content': 'nested'}}))
        assert result.success, result.error
        print('nested committed', flush=True)
    ''')
    result = await asyncio.wait_for(BashTool(workspace_dir=str(tmp_path), allow_full_access=True).invoke({
        'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(source), 'timeout': 15,
    }), 20)
    assert result.success, result.error
    assert 'nested committed' in result.content
    assert (inner / 'nested.txt').read_text() == 'nested'
    assert not (result.raw_output or {}).get('file_changes')  # The outer shell cannot claim the nested writer's receipt.


@pytest.mark.asyncio
@pytest.mark.parametrize('kernel', [False, True])
async def test_python_can_call_a_nested_sdk_writer_without_popen_patches(tmp_path, monkeypatch, kernel):
    import subprocess
    from box_agent.tools.jupyter_tool import JupyterSandboxTool, JupyterKernelSession
    monkeypatch.setenv('BOX_AGENT_HOME', str(tmp_path / 'profile'))
    original = subprocess.Popen.__init__
    tool = JupyterSandboxTool(workspace_dir=str(tmp_path))
    env = SimpleNamespace(ensure_ready=AsyncMock())
    monkeypatch.setattr(tool, '_get_sandbox_env', lambda: env)
    repo = str(Path(__file__).resolve().parent.parent)
    invocation = f"WriteTool(workspace_dir={str(tmp_path)!r}).invoke({{'path': 'nested.txt', 'content': 'nested'}})"
    code = f"import asyncio, sys\nsys.path.insert(0, {repo!r})\nfrom box_agent.tools.file_tools import WriteTool\nresult = "
    code += f'await {invocation}' if kernel else f'asyncio.run({invocation})'
    code += '\nassert result.success, result.error'
    if kernel:
        from jupyter_client import KernelManager
        manager = KernelManager()
        await manager._async_start_kernel(cwd=str(tmp_path))
        client = manager.client()
        client.start_channels()
        await asyncio.to_thread(client.wait_for_ready, timeout=15)
        session = JupyterKernelSession('s1', tmp_path, env)
        session._km, session._kc = manager, client
    else:
        def execute(code, timeout):
            exec(code, {})
            return '', [], None
        session = SimpleNamespace(workspace=tmp_path, is_alive=lambda: True, execute=execute, stop=AsyncMock())
    monkeypatch.setattr(tool, '_sessions', {'s1': session})
    try:
        for _ in range(2):
            result = await asyncio.wait_for(tool.invoke({'code': code, 'session_id': 's1', 'timeout': 10}), 20)
            assert result.success, result.error
            assert not (result.raw_output or {}).get('file_changes')
        assert (tmp_path / 'nested.txt').read_text() == 'nested'
        assert subprocess.Popen.__init__ is original
    finally:
        await tool._discard_session('s1')


@pytest.mark.asyncio
async def test_only_same_destination_waits_and_queued_cancellation_never_writes(tmp_path):
    target = tmp_path / 'same.txt'
    target.write_text('before')
    cancelled = False
    async with destination_lock(target):
        with bind_tool_cancellation(lambda: cancelled):
            queued = asyncio.create_task(WriteTool(workspace_dir=str(tmp_path)).invoke({'path': 'same.txt', 'content': 'bad'}))
        # A disjoint native destination remains available while same.txt is locked.
        other = await asyncio.wait_for(WriteTool(workspace_dir=str(tmp_path)).invoke({'path': 'other.txt', 'content': 'other'}), 1)
        assert other.success and not queued.done()
        cancelled = True
        result = await asyncio.wait_for(queued, 1)
        assert not result.success and 'cancel' in result.error.lower()
    assert target.read_text() == 'before'


@pytest.mark.asyncio
async def test_native_same_name_writes_have_distinct_connected_versions(tmp_path):
    results = await asyncio.gather(*[
        WriteTool(workspace_dir=str(tmp_path)).invoke({'path': 'same.txt', 'content': content})
        for content in ['a', 'b', 'c']
    ])
    receipts = [changes(result)[0] for result in results]
    assert {r['after_sha256'] for r in receipts} == {digest(s) for s in [b'a', b'b', b'c']}
    assert sorted(str(r['before_sha256']) for r in receipts) == sorted([str(None), digest(b'a'), digest(b'b')])


@pytest.mark.asyncio
@pytest.mark.parametrize('screenshot', [False, True])
async def test_browser_receipt_comes_from_persisted_payload(tmp_path, screenshot):
    data = b'image' if screenshot else b'page'
    result = ToolResult(success=True, content='page', raw_output={'mcp_inline_images': [{'data': base64.b64encode(data).decode()}]})
    target = tmp_path / ('shot.png' if screenshot else 'page.md')
    adapter = _persist_browser_screenshot_output if screenshot else _persist_browser_snapshot_output
    saved = await persist_file_change(str(tmp_path), target, lambda path: adapter(result, path))
    assert changes(saved) == [{'path': target.name, 'before_sha256': None, 'after_sha256': digest(data)}]


@pytest.mark.asyncio
async def test_image_network_wait_does_not_lock_other_writers(tmp_path, monkeypatch):
    from box_agent.tools.image_generation_tool import GenerateImageTool
    entered, release = asyncio.Event(), asyncio.Event()
    tool = GenerateImageTool(workspace_dir=str(tmp_path), endpoint='https://example.test/generate')
    async def request(**kwargs):
        entered.set()
        await release.wait()
        return b'image', 'image/png'
    monkeypatch.setattr(tool, '_request_image', request)
    image = asyncio.create_task(tool.invoke({'prompt': 'image', 'output_path': 'same.png', 'watermark': False}))
    await asyncio.wait_for(entered.wait(), 1)
    write = await asyncio.wait_for(WriteTool(workspace_dir=str(tmp_path)).invoke({'path': 'same.png', 'content': 'peer'}), 1)
    assert write.success
    release.set()
    assert changes(await image)[0] == {'path': 'same.png', 'before_sha256': digest(b'peer'), 'after_sha256': digest(b'image')}


@pytest.mark.asyncio
async def test_publish_transaction_commits_binary_and_seals_open_script_input(tmp_path):
    tool = PublishArtifactTool(tmp_path)
    begin = await tool.invoke({'action': 'begin', 'path': 'deck.pptx'})
    staging = Path(begin.raw_output['staging_path'])
    data = b'PK\x00\xffbinary'
    with staging.open('wb') as writer:
        writer.write(data)
        writer.flush()
        result = await tool.invoke({'action': 'commit', 'write_id': begin.raw_output['write_id']})
        writer.write(b'late background bytes')
        writer.flush()
    assert changes(result)[0]['after_sha256'] == digest(data)
    assert (tmp_path / 'deck.pptx').read_bytes() == data
    replay = await tool.invoke({'action': 'commit', 'write_id': begin.raw_output['write_id']})
    assert not replay.success
    published = await tool.invoke({'path': 'deck.pptx'})
    assert published.success and 'file_changes' not in published.raw_output


@pytest.mark.asyncio
async def test_publish_transaction_checks_current_permission_and_delegated_scope(tmp_path):
    from box_agent.tools.sub_agent_tool import _WriteScopedTool
    allow = True
    class Permissions:
        def check(self, **kwargs):
            return SimpleNamespace(allowed=allow, reason='denied', permission_request=None)
    tool = PublishArtifactTool(tmp_path, permission_engine=Permissions())
    begin = await tool.invoke({'action': 'begin', 'path': 'a.pdf'})
    Path(begin.raw_output['staging_path']).write_bytes(b'pdf')
    allow = False
    result = await tool.invoke({'action': 'commit', 'write_id': begin.raw_output['write_id']})
    assert not result.success and not (tmp_path / 'a.pdf').exists()
    allow = True
    scoped = _WriteScopedTool(tool, str(tmp_path), ('allowed',))
    refused = await scoped.invoke({'action': 'commit', 'write_id': begin.raw_output['write_id']})
    assert not refused.success and 'WRITE_SCOPE' in refused.error
    refused = await scoped.invoke({'action': 'begin', 'path': 'denied.pdf'})
    assert not refused.success and 'WRITE_SCOPE' in refused.error
    await tool.invoke({'action': 'abort', 'write_id': begin.raw_output['write_id']})


@pytest.mark.asyncio
async def test_publish_destination_symlink_change_is_rejected(tmp_path):
    tool = PublishArtifactTool(tmp_path)
    begin = await tool.invoke({'action': 'begin', 'path': 'a.pdf'})
    Path(begin.raw_output['staging_path']).write_bytes(b'pdf')
    (tmp_path / 'peer.pdf').write_bytes(b'peer')
    (tmp_path / 'a.pdf').symlink_to(tmp_path / 'peer.pdf')
    result = await tool.invoke({'action': 'commit', 'write_id': begin.raw_output['write_id']})
    assert not result.success
    assert (tmp_path / 'peer.pdf').read_bytes() == b'peer'


@pytest.mark.asyncio
async def test_publish_rechecks_destination_after_async_sealing(tmp_path, monkeypatch):
    workspace, outside = tmp_path / 'workspace', tmp_path / 'outside'
    (workspace / 'output').mkdir(parents=True)
    tool = PublishArtifactTool(workspace)
    begin = await tool.invoke({'action': 'begin', 'path': 'output/report.pdf'})
    Path(begin.raw_output['staging_path']).write_bytes(b'pdf')
    original, moved = asyncio.to_thread, False
    async def interleave(function, *args, **kwargs):
        nonlocal moved
        result = await original(function, *args, **kwargs)
        if not moved:
            moved = True
            (workspace / 'output').rename(outside)
            (workspace / 'output').symlink_to(outside, target_is_directory=True)
        return result
    monkeypatch.setattr(asyncio, 'to_thread', interleave)
    result = await tool.invoke({'action': 'commit', 'write_id': begin.raw_output['write_id']})
    assert not result.success and 'destination changed' in result.error
    assert list(outside.iterdir()) == []
    assert 'file_changes' not in (result.raw_output or {})
    tool.cleanup_pending_writes()


@pytest.mark.asyncio
@pytest.mark.parametrize('delegated', [False, True])
async def test_publish_rechecks_permissions_and_scope_after_async_sealing(tmp_path, monkeypatch, delegated):
    from box_agent.tools.file_change_receipts import FileWriteInvocationContext
    allow = True
    class Permissions:
        def check(self, **kwargs):
            return SimpleNamespace(allowed=allow, reason='denied', permission_request=None)
    native = PublishArtifactTool(tmp_path, permission_engine=None if delegated else Permissions())
    def validate_scope(target):
        return None if allow else ToolResult(success=False, error='WRITE_SCOPE: scope changed')
    context = FileWriteInvocationContext(validate_write_target=validate_scope) if delegated else None
    begin = await native.invoke({'action': 'begin', 'path': 'allowed/report.pdf'}, context=context)
    Path(begin.raw_output['staging_path']).write_bytes(b'pdf')
    original = asyncio.to_thread
    async def interleave(function, *args, **kwargs):
        nonlocal allow
        result = await original(function, *args, **kwargs)
        allow = False
        return result
    monkeypatch.setattr(asyncio, 'to_thread', interleave)
    result = await native.invoke({'action': 'commit', 'write_id': begin.raw_output['write_id']}, context=context)
    assert not result.success
    assert not (tmp_path / 'allowed/report.pdf').exists()
    assert not list((tmp_path / 'allowed').iterdir())
    native.cleanup_pending_writes()


def test_acp_receipts_carry_session_task_and_turn():
    payload = _tool_result_raw_output({'file_changes_version': 2, 'file_changes': []}, '', None,
                                     session_id='session', task_id='task', turn_id='turn')
    assert payload['session_id'] == 'session' and payload['task_id'] == 'task' and payload['turn_id'] == 'turn'


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO')
def test_native_write_replaces_fifo_without_blocking_for_optional_evidence(tmp_path):
    import subprocess
    target = tmp_path / 'output.txt'
    os.mkfifo(target)
    source = (
        'import asyncio; from box_agent.tools.file_tools import WriteTool; '
        f'result = asyncio.run(WriteTool(workspace_dir={str(tmp_path)!r}).invoke('
        "{'path':'output.txt','content':'own'})); "
        "assert result.success, result.error; assert 'file_changes' not in (result.raw_output or {})"
    )
    subprocess.run([sys.executable, '-c', source], check=True, timeout=5)
    assert target.read_text() == 'own'


@pytest.mark.asyncio
async def test_acp_wire_binds_actual_write_to_its_original_tool_and_turn(tmp_path):
    class Connection:
        updates = []
        async def sessionUpdate(self, payload):
            self.updates.append(payload.update)
    class Model:
        calls = 0
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type='finish', finish_reason='tool', tool_calls=[
                    ToolCall(id='writer', type='function', function=FunctionCall(name='write_file', arguments={
                        'path': 'own.txt', 'content': 'own',
                    })),
                ])
            else:
                yield StreamEvent(type='text', delta='done')
                yield StreamEvent(type='finish', finish_reason='stop')
        async def generate(self, messages, tools=None, **kwargs):
            return LLMResponse(content='done', finish_reason='stop')
    config = Config(llm=LLMConfig(api_key='test'), agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
                    tools=ToolsConfig(enable_sub_agent=False))
    conn = Connection()
    adapter = BoxACPAgent(conn, config, Model(), [], 'system')
    session = await adapter.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={
        'session_mode': 'code_agent', 'session_id': 'host-session',
    }))
    response = await adapter.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{'text': 'write'}], field_meta={}))
    assert response.field_meta['ok'] is True
    start = next(u for u in conn.updates if u.sessionUpdate == 'tool_call' and u.toolCallId == 'writer')
    finish = next(u for u in conn.updates if u.sessionUpdate == 'tool_call_update' and
                  u.toolCallId == 'writer' and isinstance(u.rawOutput, dict) and u.rawOutput.get('file_changes_version') == 2)
    assert start.model_dump(by_alias=True)['_meta']['tool_name'] == 'write_file'
    assert finish.rawOutput['task_id'] == start.field_meta['task_id']
    assert finish.rawOutput['turn_id'] == start.field_meta['turn_id']
    assert finish.rawOutput['session_id'] == 'host-session'
    assert finish.rawOutput['file_changes'] == [{'path': 'own.txt', 'before_sha256': None, 'after_sha256': digest(b'own')}]


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_name', ['append_file', 'edit_file'])
@pytest.mark.parametrize('placement', ['primary', 'supporting', 'intermediate', 'scope'])
async def test_acp_native_revisions_keep_delivery_policy_without_workspace_snapshots(
    tmp_path, monkeypatch, tool_name, placement,
):
    from box_agent.artifact_publication import write_metadata
    from box_agent.tools.engine.artifact_results import _snapshot_workspace_signatures

    target = tmp_path / 'report.txt'
    target.write_text('initial', encoding='utf-8')
    (tmp_path / 'extra.txt').write_text('scan cap trigger', encoding='utf-8')
    if placement == 'scope':
        (tmp_path / '.artifact-delivery.json').write_text(
            '{"schema_version":1,"default":"intermediate"}', encoding='utf-8',
        )
    elif placement != 'supporting':
        write_metadata(target, {
            'type': 'artifact' if placement == 'primary' else 'intermediate_asset',
        })
    monkeypatch.setenv('BOX_AGENT_ARTIFACT_SCAN_MAX_FILES', '1')
    assert _snapshot_workspace_signatures(str(tmp_path)) is None

    class Connection:
        def __init__(self):
            self.updates = []

        async def sessionUpdate(self, payload):
            self.updates.append(payload.update)

    class Model:
        calls = 0

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls in {1, 3}:
                revision = (self.calls + 1) // 2
                arguments = (
                    {'path': 'report.txt', 'content': f' append-{revision}'}
                    if tool_name == 'append_file' else
                    {'path': 'report.txt', 'old_str': 'initial' if revision == 1 else 'revision-1',
                     'new_str': f'revision-{revision}'}
                )
                yield StreamEvent(type='finish', finish_reason='tool', tool_calls=[
                    ToolCall(id=f'writer-{revision}', type='function', function=FunctionCall(
                        name=tool_name, arguments=arguments,
                    )),
                ])
            else:
                yield StreamEvent(type='text', delta='done')
                yield StreamEvent(type='finish', finish_reason='stop')

        async def generate(self, messages, tools=None, **kwargs):
            return LLMResponse(content='done', finish_reason='stop')

    config = Config(
        llm=LLMConfig(api_key='test'),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False, enable_todo=False),
    )
    conn = Connection()
    adapter = BoxACPAgent(conn, config, Model(), [], 'system')
    session = await adapter.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={
        'session_mode': 'code_agent', 'session_id': 'host-session',
    }))
    original_agent = adapter._sessions[session.sessionId].agent
    if tool_name == 'append_file':
        activated = await original_agent.tools['tool_search'].invoke({'tool_names': [tool_name]})
        assert activated.success
    for revision in (1, 2):
        offset = len(conn.updates)
        response = await adapter.prompt(SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{'text': 'revise the report'}],
            field_meta={'task_id': f'task-{revision}', 'turn_id': f'turn-{revision}'},
        ))
        assert response.field_meta['ok'] is True
        assert adapter._sessions[session.sessionId].agent is original_agent
        writes = [
            update.rawOutput for update in conn.updates[offset:]
            if update.sessionUpdate == 'tool_call_update'
            and update.toolCallId == f'writer-{revision}'
            and isinstance(update.rawOutput, dict)
        ]
        receipt = next(raw for raw in writes if raw.get('file_changes_version') == 2)
        assert receipt['session_id'] == 'host-session'
        assert receipt['task_id'] == f'task-{revision}'
        assert receipt['turn_id'] == f'turn-{revision}'
        assert receipt['file_changes'][0]['after_sha256'] == digest(target.read_bytes())
        artifacts = [raw for raw in writes if raw.get('type') == 'artifact']
        if placement in {'intermediate', 'scope'}:
            assert receipt['type'] == 'intermediate_asset'
            assert artifacts == []
        else:
            # Raw tool results use the existing WriteTool shape; the shared
            # ArtifactEvent adds authoritative producer placement and lineage.
            assert receipt['type'] == 'artifact'
            assert 'placement' not in receipt
            published = [raw for raw in artifacts if 'placement' in raw]
            assert len(published) == 1
            artifact = published[0]
            assert artifact['placement'] == placement
            assert artifact['rel_path'] == 'report.txt'
            assert artifact['session_id'] == receipt['session_id']
            assert artifact['task_id'] == receipt['task_id']
            assert artifact['turn_id'] == receipt['turn_id']
            assert artifact['sha256'] == receipt['file_changes'][0]['after_sha256']


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_name', ['append_file', 'edit_file'])
@pytest.mark.parametrize('outcome', ['noop', 'failure'])
async def test_native_revisions_do_not_deliver_noops_or_failures(tmp_path, tool_name, outcome):
    from box_agent.tools.engine.artifact_results import (
        _detect_tool_artifacts, _snapshot_workspace_signatures,
    )

    target = tmp_path / 'report.txt'
    target.write_bytes(b'initial\r\n')
    (tmp_path / 'directory').mkdir()
    tool = AppendTool(str(tmp_path)) if tool_name == 'append_file' else EditTool(str(tmp_path))
    if tool_name == 'append_file':
        arguments = {'path': 'report.txt' if outcome == 'noop' else 'directory', 'content': ''}
    else:
        arguments = {'path': 'report.txt', 'old_str': 'initial' if outcome == 'noop' else 'missing',
                     'new_str': 'initial'}
    before = _snapshot_workspace_signatures(str(tmp_path))
    result = await tool.invoke(arguments)
    after = _snapshot_workspace_signatures(str(tmp_path))
    assert result.success is (outcome == 'noop')
    assert not (result.raw_output or {}).get('file_changes')
    assert (result.raw_output or {}).get('type') != 'artifact'
    assert after == before
    assert target.read_bytes() == b'initial\r\n'
    assert _detect_tool_artifacts(
        'writer', tool_name, result.content, result.raw_output, before, after, str(tmp_path),
    ) == []


@pytest.mark.asyncio
async def test_empty_append_still_announces_a_new_empty_file(tmp_path):
    result = await AppendTool(str(tmp_path)).invoke({'path': 'empty.txt', 'content': ''})
    assert changes(result) == [{
        'path': 'empty.txt', 'before_sha256': None, 'after_sha256': digest(b''),
    }]
    assert result.raw_output['type'] == 'artifact'
    assert result.raw_output['path'] == str(tmp_path / 'empty.txt')


@pytest.mark.asyncio
async def test_retargeted_image_waits_for_the_new_destination_lock(tmp_path, monkeypatch):
    from box_agent.tools.image_generation_tool import GenerateImageTool
    tool = GenerateImageTool(workspace_dir=str(tmp_path), endpoint='https://example.test/generate')
    monkeypatch.setattr(tool, '_request_image', AsyncMock(return_value=(b'image', 'image/png')))
    original, peer = tmp_path / 'a.png', tmp_path / 'peer.png'
    first, second = destination_lock(original), destination_lock(peer)
    await first.__aenter__()
    await second.__aenter__()
    image = asyncio.create_task(tool.invoke({'prompt': 'image', 'output_path': 'a.png', 'watermark': False}))
    try:
        await asyncio.sleep(0.03)
        original.symlink_to(peer)
        await first.__aexit__(None, None, None)
        await asyncio.sleep(0.03)
        assert not image.done() and not peer.exists()
    finally:
        await second.__aexit__(None, None, None)
        result = await asyncio.wait_for(image, 2)
    assert changes(result)[0]['path'] == 'peer.png'


@pytest.mark.asyncio
async def test_committed_artifact_keeps_its_receipt_when_builder_leaves_side_files(tmp_path):
    tool = PublishArtifactTool(tmp_path)
    begin = await tool.invoke({'action': 'begin', 'path': 'report.pdf'})
    staging = Path(begin.raw_output['staging_path'])
    staging.write_bytes(b'pdf')
    (staging.parent / 'qa.txt').write_text('QA')
    result = await tool.invoke({'action': 'commit', 'write_id': begin.raw_output['write_id']})
    assert changes(result)[0]['after_sha256'] == digest(b'pdf')
    assert (tmp_path / 'report.pdf').read_bytes() == b'pdf'
    pending = await tool.invoke({'action': 'begin', 'path': 'next.pdf'})
    assert tool.cleanup_pending_writes()
    assert not Path(pending.raw_output['staging_path']).parent.exists()

@pytest.mark.asyncio
async def test_append_keeps_legacy_semantics_but_does_not_claim_a_scripts_intervening_bytes(tmp_path, monkeypatch):
    import subprocess
    target = tmp_path / 'a.txt'
    target.write_bytes(b'A')
    original = Path.read_bytes
    def interleaved(path):
        data = original(path)
        if path == target:
            subprocess.run([sys.executable, '-c', 'import sys; open(sys.argv[1], "ab").write(b"P")', str(target)], check=True)
        return data
    monkeypatch.setattr(Path, 'read_bytes', interleaved)
    result = await AppendTool(workspace_dir=str(tmp_path)).invoke({'path': 'a.txt', 'content': 'X'})
    assert result.success
    assert original(target) == b'APX'
    assert not (result.raw_output or {}).get('file_changes')


@pytest.mark.asyncio
async def test_direct_receipts_use_the_canonical_workspace_through_aliases(tmp_path):
    real, alias = tmp_path / 'real', tmp_path / 'alias'
    real.mkdir()
    alias.symlink_to(real, target_is_directory=True)
    (real / 'a.txt').write_text('a')
    append = await AppendTool(workspace_dir=str(alias)).invoke({'path': 'a.txt', 'content': 'b'})
    edit = await EditTool(workspace_dir=str(alias)).invoke({'path': 'a.txt', 'old_str': 'b', 'new_str': 'c'})
    assert changes(append)[0]['path'] == changes(edit)[0]['path'] == 'a.txt'

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['write', 'append', 'edit'])
async def test_delegated_writes_through_workspace_aliases_keep_their_scope(tmp_path, kind):
    from box_agent.tools.sub_agent_tool import _WriteScopedTool
    real, alias = tmp_path / 'real', tmp_path / 'alias'
    (real / 'allowed').mkdir(parents=True)
    alias.symlink_to(real, target_is_directory=True)
    (real / 'allowed/a.txt').write_text('a')
    cls = {'write': WriteTool, 'append': AppendTool, 'edit': EditTool}[kind]
    tool = _WriteScopedTool(cls(workspace_dir=str(alias)), str(alias), ('allowed',))
    arguments = {'path': 'allowed/a.txt', **({'old_str': 'a', 'new_str': 'b'} if kind == 'edit' else {'content': 'b'})}
    assert changes(await tool.invoke(arguments))[0]['path'] == 'allowed/a.txt'
