import hashlib
import asyncio
import threading
import os
import sys
import textwrap
import signal
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from box_agent.acp import _tool_result_raw_output
from box_agent.tools.bash_tool import BashTool
from box_agent.tools.file_tools import AppendTool, EditTool, WriteTool
from box_agent.tools.staged_file_write_tool import StagedFileWriteTool
from box_agent.tools.jupyter_tool import JupyterSandboxTool
from box_agent.tools.image_generation_tool import GenerateImageTool
from box_agent.tools.base import ToolResult
from box_agent.tools.file_change_receipts import persist_file_change
from box_agent.config import Config, AgentConfig, ToolsConfig, LLMConfig
from box_agent.acp import BoxACPAgent
from box_agent.schema import StreamEvent, ToolCall, FunctionCall, LLMResponse

pytestmark = pytest.mark.usefixtures('posix_shell')


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_type', [WriteTool, AppendTool, EditTool])
@pytest.mark.parametrize('retarget_scope_root', [False, True])
async def test_delegated_write_rechecks_scope_after_waiting_for_workspace_lock(
    tmp_path, monkeypatch, tool_type, retarget_scope_root,
):
    from box_agent.tools import file_change_receipts as receipts
    from box_agent.tools.sub_agent_tool import _WriteScopedTool

    allowed, unassigned = tmp_path / 'allowed', tmp_path / 'unassigned'
    allowed.mkdir()
    unassigned.mkdir()
    (allowed / 'out.txt').write_text('before')
    (unassigned / 'out.txt').write_text('peer')
    alias = tmp_path / 'alias'
    alias.symlink_to(allowed, target_is_directory=True)
    tool = _WriteScopedTool(tool_type(workspace_dir=str(tmp_path)), str(tmp_path), ('allowed',))
    arguments = {'path': 'alias/out.txt', 'content': 'child'}
    if tool_type is EditTool:
        arguments = {'path': 'alias/out.txt', 'old_str': 'peer', 'new_str': 'child'}
    waiting = asyncio.Event()
    overlapping = receipts._overlapping_lease

    def announce(root, records):
        result = overlapping(root, records)
        if result:
            waiting.set()
        return result

    async with receipts._workspace_execution_lock(tmp_path):
        monkeypatch.setattr(receipts, '_overlapping_lease', announce)
        queued = asyncio.create_task(tool.invoke(arguments))
        await asyncio.wait_for(waiting.wait(), 5)
        if retarget_scope_root:
            allowed.rename(tmp_path / 'original-allowed')
            allowed.symlink_to(unassigned, target_is_directory=True)
        else:
            alias.unlink()
            alias.symlink_to(unassigned, target_is_directory=True)
    result = await asyncio.wait_for(queued, 5)
    assert not result.success
    assert 'WRITE_SCOPE_VIOLATION' in result.error
    assert (unassigned / 'out.txt').read_text() == 'peer'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['write', 'append', 'edit', 'active_root', 'staged'])
async def test_read_cannot_adopt_a_write_from_another_configured_workspace(tmp_path, kind):
    source, destination = tmp_path / 'source', tmp_path / 'destination'
    source.mkdir()
    destination.mkdir()
    target = destination / 'peer.txt'
    target.write_text('before')
    reader = BashTool(workspace_dir=str(destination), allow_full_access=True)
    started = asyncio.Event()
    create = reader._create_subprocess

    async def announce(*args, **kwargs):
        process = await create(*args, **kwargs)
        started.set()
        return process

    reader._create_subprocess = announce
    if kind == 'staged':
        writer = StagedFileWriteTool(workspace_dir=str(source))
        begin = await writer.invoke({'action': 'begin', 'path': str(target), 'expected_chunks': 1})
        write_id = begin.raw_output['write_id']
        appended = await writer.invoke({'action': 'append_text', 'write_id': write_id,
                                        'chunk_index': 0, 'content': 'peer'})
        assert begin.success and appended.success
        arguments = {'action': 'commit', 'write_id': write_id}
    elif kind == 'edit':
        writer = EditTool(workspace_dir=str(source), allow_full_access=True)
        arguments = {'path': str(target), 'old_str': 'before', 'new_str': 'peer'}
    else:
        cls = AppendTool if kind == 'append' else WriteTool
        writer = cls(workspace_dir=str(source), allow_full_access=True,
                     relative_root_dir=str(destination) if kind == 'active_root' else None)
        arguments = {'path': 'peer.txt' if kind == 'active_root' else str(target), 'content': 'peer'}
    reading = asyncio.create_task(reader.invoke({'command': 'sleep 0.2; cat peer.txt'}))
    await asyncio.wait_for(started.wait(), timeout=5)
    written, read = await asyncio.wait_for(asyncio.gather(writer.invoke(arguments), reading), timeout=5)
    assert written.success and read.success
    assert read.raw_output['file_changes'] == []
    assert target.read_text() == ('beforepeer' if kind == 'append' else 'peer')


@pytest.mark.asyncio
async def test_reciprocal_outside_workspace_writes_do_not_deadlock(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    results = await asyncio.wait_for(asyncio.gather(
        WriteTool(workspace_dir=str(first)).invoke({'path': str(second / 'peer.txt'), 'content': 'first'}),
        WriteTool(workspace_dir=str(second)).invoke({'path': str(first / 'peer.txt'), 'content': 'second'}),
    ), timeout=5)
    assert all(result.success for result in results)
    assert (first / 'peer.txt').read_text() == 'second'
    assert (second / 'peer.txt').read_text() == 'first'


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_type', [WriteTool, AppendTool, EditTool, StagedFileWriteTool])
async def test_destination_lock_does_not_grant_outside_workspace_write_permission(tmp_path, tool_type):
    root = tmp_path / 'workspace'
    root.mkdir()
    target = tmp_path / 'outside.txt'
    target.write_text('before')
    tool = tool_type(workspace_dir=str(root), allow_full_access=False)
    arguments = {'path': str(target), 'content': 'peer'}
    if tool_type is EditTool:
        arguments = {'path': str(target), 'old_str': 'before', 'new_str': 'peer'}
    elif tool_type is StagedFileWriteTool:
        arguments = {'action': 'begin', 'path': str(target)}
    result = await tool.invoke(arguments)
    assert not result.success
    assert target.read_text() == 'before'


@pytest.mark.asyncio
async def test_read_cannot_adopt_staged_replacement_of_an_outside_symlink(tmp_path):
    source, destination, referent = (tmp_path / name for name in ('source', 'destination', 'referent'))
    for root in (source, destination, referent):
        root.mkdir()
    original = referent / 'original.txt'
    original.write_text('before')
    target = destination / 'alias.txt'
    target.symlink_to(original)
    writer = StagedFileWriteTool(workspace_dir=str(source))
    begin = await writer.invoke({'action': 'begin', 'path': str(target), 'expected_chunks': 1})
    write_id = begin.raw_output['write_id']
    await writer.invoke({'action': 'append_text', 'write_id': write_id, 'chunk_index': 0, 'content': 'peer'})
    reader = BashTool(workspace_dir=str(destination), allow_full_access=True)
    started = asyncio.Event()
    create = reader._create_subprocess

    async def announce(*args, **kwargs):
        process = await create(*args, **kwargs)
        started.set()
        return process

    reader._create_subprocess = announce
    reading = asyncio.create_task(reader.invoke({'command': 'sleep 0.2; cat alias.txt'}))
    await asyncio.wait_for(started.wait(), timeout=5)
    written, read = await asyncio.wait_for(asyncio.gather(
        writer.invoke({'action': 'commit', 'write_id': write_id}), reading,
    ), timeout=5)
    assert written.success and read.success
    assert read.raw_output['file_changes'] == []
    assert not target.is_symlink() and target.read_text() == 'peer'
    assert original.read_text() == 'before'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,file_alias', [
    ('write', False), ('append', False), ('edit', False), ('staged', False),
    ('write', True), ('append', True), ('edit', True),
])
async def test_queued_write_rechecks_a_retargeted_symlink(tmp_path, kind, file_alias):
    source, old, destination = (tmp_path / name for name in ('source', 'old', 'destination'))
    for root in (source, old, destination):
        root.mkdir()
        (root / 'peer.txt').write_text('before')
    (source / 'link').symlink_to(old / 'peer.txt' if file_alias else old,
                               target_is_directory=not file_alias)
    path = 'link' if file_alias else 'link/peer.txt'
    reader = BashTool(workspace_dir=str(destination), allow_full_access=True)
    mutator = BashTool(workspace_dir=str(source), allow_full_access=True)
    started = []
    for tool in (reader, mutator):
        event = asyncio.Event()
        create = tool._create_subprocess

        async def announce(*args, create=create, event=event, **kwargs):
            process = await create(*args, **kwargs)
            event.set()
            return process

        tool._create_subprocess = announce
        started.append(event)
    if kind == 'staged':
        writer = StagedFileWriteTool(workspace_dir=str(source))
        begin = await writer.invoke({'action': 'begin', 'path': path, 'expected_chunks': 1})
        write_id = begin.raw_output['write_id']
        await writer.invoke({'action': 'append_text', 'write_id': write_id,
                             'chunk_index': 0, 'content': 'peer'})
        arguments = {'action': 'commit', 'write_id': write_id}
    else:
        cls = {'write': WriteTool, 'append': AppendTool, 'edit': EditTool}[kind]
        writer = cls(workspace_dir=str(source), allow_full_access=True)
        arguments = {'path': path, 'content': 'peer'} if kind != 'edit' else {
            'path': path, 'old_str': 'before', 'new_str': 'peer',
        }
    reading = asyncio.create_task(reader.invoke({'command': 'sleep 0.5; cat peer.txt'}))
    await asyncio.wait_for(started[0].wait(), timeout=5)
    new_target = destination / 'peer.txt' if file_alias else destination
    mutating = asyncio.create_task(mutator.invoke({
        'command': f'sleep 0.1; unlink link; ln -s {shlex.quote(str(new_target))} link',
    }))
    await asyncio.wait_for(started[1].wait(), timeout=5)
    read, mutated, written = await asyncio.wait_for(asyncio.gather(
        reading, mutating, writer.invoke(arguments),
    ), timeout=5)
    assert read.success and mutated.success and written.success
    assert read.raw_output['file_changes'] == []
    assert (destination / 'peer.txt').read_text() == ('beforepeer' if kind == 'append' else 'peer')


@pytest.mark.asyncio
async def test_automatic_receipts_exclude_only_hidden_publication_sidecars(tmp_path):
    result = await BashTool(workspace_dir=str(tmp_path), allow_full_access=True).invoke({
        'command': 'printf own > report.artifact.json; printf own > report.json; '
                   'printf metadata > .report.json.artifact.json',
    })
    assert result.success, result.error
    assert {change['path'] for change in result.raw_output['file_changes']} == {
        'report.artifact.json', 'report.json',
    }


@pytest.mark.asyncio
async def test_script_receipt_excludes_peer_files_and_includes_every_file_type(tmp_path):
    tool = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    names = ['own.xlsx', 'own.pdf', 'own.docx', 'own.png', 'own.bin', 'no-extension']
    result = await tool.invoke({
        'command': "printf own > own.xlsx; printf own > own.pdf; printf own > own.docx; "
                   "printf own > own.png; printf own > own.bin; printf own > no-extension; "
                   "printf peer > peer.pdf",
        'changed_files': names,
    })
    assert result.success, result.error
    assert result.raw_output['file_changes_version'] == 1
    assert {x['path'] for x in result.raw_output['file_changes']} == set(names)
    assert all(x['before_sha256'] is None and x['after_sha256'] == digest(b'own')
               for x in result.raw_output['file_changes'])


@pytest.mark.asyncio
async def test_receipt_handles_modification_deletion_and_unchanged_read(tmp_path):
    for name in ['modify', 'delete', 'read']:
        (tmp_path / name).write_bytes(b'before')
    result = await BashTool(workspace_dir=str(tmp_path), allow_full_access=True,
                            bypass_dangerous_command_approval=True).invoke({
        'command': 'printf after > modify; rm delete; cat read',
        'changed_files': ['modify', 'delete', 'read'],
    })
    assert result.success, result.error
    assert result.raw_output['file_changes'] == [
        {'path': 'modify', 'before_sha256': digest(b'before'), 'after_sha256': digest(b'after')},
        {'path': 'delete', 'before_sha256': digest(b'before'), 'after_sha256': None},
    ]


@pytest.mark.asyncio
async def test_legacy_script_automatically_reports_outputs_and_background_is_unconfirmed(tmp_path):
    tool = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    result = await tool.invoke({'command': 'printf own > own.pdf'})
    assert result.raw_output['file_changes'] == [
        {'path': 'own.pdf', 'before_sha256': None, 'after_sha256': digest(b'own')},
    ]
    result = await tool.invoke({'command': 'printf peer > peer.pdf',
                                'run_in_background': True, 'changed_files': ['peer.pdf']})
    assert not result.success
    assert not (tmp_path / 'peer.pdf').exists()


@pytest.mark.asyncio
async def test_automatic_receipts_include_nested_hidden_and_temporary_files(tmp_path):
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'old.bin').write_bytes(b'before')
    result = await BashTool(workspace_dir=str(tmp_path), allow_full_access=True,
                            bypass_dangerous_command_approval=True).invoke({
        'command': 'printf own > nested/no-extension; printf own > .hidden; printf own > data.tmp; rm old.bin',
    })
    assert result.success, result.error
    assert {change['path'] for change in result.raw_output['file_changes']} == {
        'nested/no-extension', '.hidden', 'data.tmp', 'old.bin',
    }


@pytest.mark.asyncio
async def test_escape_and_symlink_are_rejected_before_execution(tmp_path):
    outside = tmp_path.parent / 'outside-receipt-test'
    (tmp_path / 'alias').symlink_to(outside)
    for name in ['../outside-receipt-test', 'alias']:
        result = await BashTool(workspace_dir=str(tmp_path), allow_full_access=True).invoke({
            'command': 'printf own > marker', 'changed_files': [name],
        })
        assert not result.success
        assert not (tmp_path / 'marker').exists()


@pytest.mark.asyncio
async def test_direct_file_tools_emit_receipts(tmp_path):
    write = WriteTool(workspace_dir=str(tmp_path))
    first = await write.invoke({'path': 'own.txt', 'content': 'before'})
    assert first.raw_output['file_changes'][0]['before_sha256'] is None
    for tool, arguments, expected in [
        (AppendTool(workspace_dir=str(tmp_path)), {'path': 'own.txt', 'content': '!'}, b'before!'),
        (EditTool(workspace_dir=str(tmp_path)), {'path': 'own.txt', 'old_str': 'before', 'new_str': 'after'}, b'after!'),
    ]:
        result = await tool.invoke(arguments)
        assert result.success, result.error
        assert result.raw_output['file_changes'][0]['after_sha256'] == digest(expected)


def test_acp_receipts_carry_session_task_and_turn():
    payload = _tool_result_raw_output({'file_changes_version': 1, 'file_changes': []}, '', None,
                                     session_id='s', task_id='task', turn_id='turn')
    assert (payload['session_id'], payload['task_id'], payload['turn_id']) == ('s', 'task', 'turn')


@pytest.mark.asyncio
async def test_concurrent_read_cannot_adopt_a_peer_write(tmp_path):
    (tmp_path / 'same.pdf').write_bytes(b'before')
    reader = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    writer = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    started = asyncio.Event()
    create = reader._create_subprocess

    async def announce(*args, **kwargs):
        process = await create(*args, **kwargs)
        started.set()
        return process

    reader._create_subprocess = announce
    reading = asyncio.create_task(reader.invoke({'command': 'sleep 0.2; cat same.pdf',
                                                'changed_files': ['same.pdf']}))
    await asyncio.wait_for(started.wait(), timeout=5)
    written = await writer.invoke({'command': 'printf peer > same.pdf',
                                    'changed_files': ['same.pdf']})
    result = await reading
    assert result.raw_output['file_changes'] == []
    assert written.raw_output['file_changes'] == [
        {'path': 'same.pdf', 'before_sha256': digest(b'before'), 'after_sha256': digest(b'peer')},
    ]


@pytest.mark.asyncio
async def test_parent_workspace_read_cannot_adopt_child_workspace_write(tmp_path):
    nested = tmp_path / 'nested'
    nested.mkdir()
    (nested / 'same.txt').write_bytes(b'before')
    reader = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    writer = BashTool(workspace_dir=str(nested), allow_full_access=True)
    started = asyncio.Event()
    create = reader._create_subprocess

    async def announce(*args, **kwargs):
        process = await create(*args, **kwargs)
        started.set()
        return process

    reader._create_subprocess = announce
    reading = asyncio.create_task(reader.invoke({'command': 'sleep 0.2; cat nested/same.txt'}))
    await asyncio.wait_for(started.wait(), timeout=5)
    written = await asyncio.wait_for(writer.invoke({'command': 'printf peer > same.txt'}), timeout=5)
    result = await asyncio.wait_for(reading, timeout=5)
    assert result.success and written.success
    assert result.raw_output['file_changes'] == []
    assert written.raw_output['file_changes'] == [
        {'path': 'same.txt', 'before_sha256': digest(b'before'), 'after_sha256': digest(b'peer')},
    ]


@pytest.mark.asyncio
async def test_disjoint_workspace_script_waits_for_reader(tmp_path):
    read_root, write_root = tmp_path / 'read', tmp_path / 'write'
    read_root.mkdir()
    write_root.mkdir()
    reader = BashTool(workspace_dir=str(read_root), allow_full_access=True)
    started = asyncio.Event()
    create = reader._create_subprocess

    async def announce(*args, **kwargs):
        process = await create(*args, **kwargs)
        started.set()
        return process

    reader._create_subprocess = announce
    reading = asyncio.create_task(reader.invoke({
        'command': 'while [ ! -f release ]; do sleep 0.01; done', 'changed_files': [], 'timeout': 5,
    }))
    writing = None
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        writing = asyncio.create_task(BashTool(workspace_dir=str(write_root)).invoke({
            'command': 'printf own > own.txt',
        }))
        await asyncio.sleep(0.1)
        assert not writing.done(), 'unrestricted scripts must share a profile-wide barrier'
    finally:
        (read_root / 'release').touch()
        await asyncio.wait_for(reading, timeout=5)
        if writing is not None:
            result = await asyncio.wait_for(writing, timeout=5)
    assert result.success, result.error
    assert result.raw_output['file_changes'][0]['after_sha256'] == digest(b'own')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['bash', 'python', 'bash_without_workspace'])
async def test_read_cannot_adopt_script_write_from_disjoint_workspace(tmp_path, monkeypatch, kind):
    source, destination = tmp_path / 'source', tmp_path / 'destination'
    source.mkdir()
    destination.mkdir()
    target = destination / 'peer.txt'
    target.write_bytes(b'before')
    reader = BashTool(workspace_dir=str(destination), allow_full_access=True)
    started = asyncio.Event()
    create = reader._create_subprocess

    async def announce(*args, **kwargs):
        process = await create(*args, **kwargs)
        started.set()
        return process

    monkeypatch.setattr(reader, '_create_subprocess', announce)
    if kind == 'python':
        writer = JupyterSandboxTool(workspace_dir=str(source))
        session = SimpleNamespace(workspace=source, is_alive=lambda: True)
        monkeypatch.setattr(writer, '_sessions', {'s1': session})
        monkeypatch.setattr(writer, '_get_sandbox_env', lambda: SimpleNamespace(ensure_ready=AsyncMock()))

        def execute(*args):
            target.write_bytes(b'peer')
            return '', [], None

        monkeypatch.setattr(writer, '_execute_session_code', execute)
        arguments = {'code': 'write_peer()', 'session_id': 's1', 'changed_files': []}
    else:
        monkeypatch.chdir(source)
        writer = BashTool(workspace_dir=None if kind == 'bash_without_workspace' else str(source),
                          allow_full_access=True)
        arguments = {'command': f'printf peer > {shlex.quote(str(target))}', 'changed_files': []}
    reading = asyncio.create_task(reader.invoke({
        'command': 'sleep 0.2; cat peer.txt', 'changed_files': ['peer.txt'],
    }))
    await asyncio.wait_for(started.wait(), 5)
    read, written = await asyncio.wait_for(asyncio.gather(reading, writer.invoke(arguments)), 5)
    assert read.success and written.success
    assert read.raw_output['file_changes'] == []
    assert target.read_bytes() == b'peer'


@pytest.mark.asyncio
async def test_direct_writes_in_disjoint_workspaces_remain_concurrent(tmp_path, monkeypatch):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    writer = WriteTool(workspace_dir=str(first))
    started, release = asyncio.Event(), asyncio.Event()
    execute = writer.execute

    async def wait_before_write(**arguments):
        started.set()
        await release.wait()
        return await execute(**arguments)

    monkeypatch.setattr(writer, 'execute', wait_before_write)
    pending = asyncio.create_task(writer.invoke({'path': 'first.txt', 'content': 'first'}))
    try:
        await asyncio.wait_for(started.wait(), 5)
        result = await asyncio.wait_for(WriteTool(workspace_dir=str(second)).invoke({
            'path': 'second.txt', 'content': 'second',
        }), 5)
        assert result.success
        assert (second / 'second.txt').read_text() == 'second'
        assert not pending.done()
    finally:
        release.set()
        result = await asyncio.wait_for(pending, 5)
        assert result.success


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_queued', [False, True])
async def test_other_process_script_blocks_disjoint_write_until_it_exits(tmp_path, monkeypatch, cancel_queued):
    source, destination = tmp_path / 'source', tmp_path / 'destination'
    source.mkdir()
    destination.mkdir()
    monkeypatch.setenv('BOX_AGENT_HOME', str(tmp_path / 'profile'))
    script = textwrap.dedent('''
        import asyncio, sys
        from pathlib import Path
        import box_agent.tools.bash_tool as bash_module
        from box_agent.tools.bash_tool import BashTool

        async def main():
            if sys.argv[2]:
                bash_module.bundled_win_bash = lambda: Path(sys.argv[2])
            tool = BashTool(workspace_dir=sys.argv[1])
            create = tool._create_subprocess
            async def announce(*args, **kwargs):
                process = await create(*args, **kwargs)
                print('ready', flush=True)
                return process
            tool._create_subprocess = announce
            result = await tool.invoke({
                'command': 'while [ ! -f release ]; do sleep 0.01; done', 'timeout': 5,
            })
            assert result.success, result.error
        asyncio.run(main())
    ''')
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', script, str(source),
        BashTool(workspace_dir=str(source))._bundled_win_bash or '',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    pending = None
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b'ready\n'
        pending = asyncio.create_task(WriteTool(workspace_dir=str(destination)).invoke({
            'path': 'own.txt', 'content': 'own',
        }))
        await asyncio.sleep(0.1)
        assert not pending.done()
        assert not (destination / 'own.txt').exists()
        if cancel_queued:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    finally:
        (source / 'release').touch()
        _, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        if pending is not None and not cancel_queued:
            assert (await asyncio.wait_for(pending, 5)).success
    if cancel_queued:
        result = await asyncio.wait_for(WriteTool(workspace_dir=str(destination)).invoke({
            'path': 'own.txt', 'content': 'own',
        }), 5)
        assert result.success
    assert (destination / 'own.txt').read_text() == 'own'


@pytest.mark.asyncio
@pytest.mark.parametrize('exclusive', [False, True])
async def test_receipts_resume_after_workspace_lock_owner_crashes(tmp_path, monkeypatch, exclusive):
    monkeypatch.setenv('BOX_AGENT_HOME', str(tmp_path / 'profile'))
    root = tmp_path / 'workspace'
    root.mkdir()
    script = textwrap.dedent('''
        import asyncio, os, sys
        from pathlib import Path
        from box_agent.tools.file_change_receipts import _workspace_execution_lock

        async def main():
            async with _workspace_execution_lock(Path(sys.argv[1]), exclusive=sys.argv[2] == 'True'):
                os._exit(0)

        asyncio.run(main())
    ''')
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, str(root), str(exclusive))
    assert await asyncio.wait_for(process.wait(), timeout=10) == 0
    result = await asyncio.wait_for(BashTool(workspace_dir=str(root)).invoke({
        'command': 'printf own > own.txt',
    }), timeout=5)
    assert result.success, result.error
    assert result.raw_output['file_changes'] == [
        {'path': 'own.txt', 'before_sha256': None, 'after_sha256': digest(b'own')},
    ]


@pytest.mark.asyncio
async def test_receipts_survive_transient_lease_cleanup_failure(tmp_path, monkeypatch):
    from pathlib import Path

    unlink = Path.unlink
    denied = []

    def deny_first_lease_cleanup(path, *args, **kwargs):
        if path.suffix == '.lease' and not denied:
            denied.append(path)
            raise PermissionError('another process still has the lease open')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', deny_first_lease_cleanup)
    tool = BashTool(workspace_dir=str(tmp_path))
    result = await tool.invoke({'command': 'printf own > own.txt'})
    assert result.success, result.error
    assert result.raw_output['file_changes'][0]['after_sha256'] == digest(b'own')
    result = await asyncio.wait_for(tool.invoke({'command': 'cat own.txt'}), timeout=5)
    assert result.success, result.error
    assert result.raw_output['file_changes'] == []
    assert denied and not denied[0].exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == 'nt', reason='POSIX foreground process-group survival contract')
async def test_crashed_agent_foreground_shell_cannot_be_adopted_by_reader(tmp_path, monkeypatch):
    monkeypatch.setenv('BOX_AGENT_HOME', str(tmp_path / 'profile'))
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'same.txt').write_bytes(b'before')
    script = textwrap.dedent('''
        import asyncio, os, sys
        from pathlib import Path
        from box_agent.tools.bash_tool import BashTool

        async def main():
            tool = BashTool(workspace_dir=sys.argv[1], allow_full_access=True)
            create = tool._create_subprocess

            async def crash(*args, **kwargs):
                process = await create(*args, **kwargs)
                Path(sys.argv[2]).write_text(str(process.pid))
                os._exit(0)

            tool._create_subprocess = crash
            await tool.invoke({'command': 'while [ ! -f go ]; do sleep 0.01; done; printf orphan > same.txt; touch done; sleep 30'})

        asyncio.run(main())
    ''')
    pid_file = tmp_path / 'pid'
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, str(root), str(pid_file))
    try:
        assert await asyncio.wait_for(process.wait(), timeout=10) == 0
        result = await BashTool(workspace_dir=str(root), allow_full_access=True).invoke({
            'command': 'touch go; while [ ! -f done ]; do sleep 0.01; done; cat same.txt',
            'changed_files': ['same.txt'], 'timeout': 5,
        })
        assert result.success, result.error
        assert (root / 'same.txt').read_bytes() == b'orphan'
        assert 'file_changes' not in (result.raw_output or {})
    finally:
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_failed_shell_registration_stops_spawned_process(tmp_path, monkeypatch):
    import box_agent.tools.bash_tool as bash_module

    tool = BashTool(workspace_dir=str(tmp_path))
    processes = []
    register = tool._register_subprocess

    async def observe(process):
        processes.append(process)
        return await register(process)

    def fail_registration(*args):
        raise OSError('writer registry unavailable')

    monkeypatch.setattr(tool, '_register_subprocess', observe)
    monkeypatch.setattr(bash_module, 'register_shell_writer', fail_registration)
    result = await asyncio.wait_for(tool.invoke({'command': 'sleep 30'}), timeout=10)
    assert not result.success
    assert 'writer registry unavailable' in result.error
    assert len(processes) == 1 and processes[0].returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('layout', ['same', 'nested', 'disjoint'])
async def test_other_process_background_writer_suppresses_receipts_until_stopped(tmp_path, monkeypatch, layout):
    root = tmp_path / 'workspace'
    root.mkdir()
    background_root = root / 'nested' if layout == 'nested' else (
        tmp_path / 'background' if layout == 'disjoint' else root
    )
    background_root.mkdir(exist_ok=True)
    target = root / 'same.txt'
    target.write_bytes(b'before')
    monkeypatch.setenv('BOX_AGENT_HOME', str(tmp_path / 'profile'))
    script = textwrap.dedent('''
        import asyncio, sys, shlex
        from pathlib import Path
        import box_agent.tools.bash_tool as bash_module
        from box_agent.tools.bash_tool import BashTool, BackgroundShellManager

        if sys.argv[2]:
            bash_module.bundled_win_bash = lambda: Path(sys.argv[2])

        async def main():
            try:
                result = await BashTool(workspace_dir=sys.argv[1], allow_full_access=True).invoke({
                    'command': 'while [ ! -f go ]; do sleep 0.01; done; printf peer > ' + shlex.quote(sys.argv[3]) + '; touch done; sleep 30',
                    'run_in_background': True,
                })
                assert result.success, result.error
                print('ready', flush=True)
                await asyncio.to_thread(sys.stdin.readline)
            finally:
                await BackgroundShellManager.terminate_all()

        asyncio.run(main())
    ''')
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', script, str(background_root),
        BashTool(workspace_dir=str(root))._bundled_win_bash or '',
        str(target),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), timeout=10) == b'ready\n'
        tool = BashTool(workspace_dir=str(root), allow_full_access=True)
        result = await tool.invoke({
            'command': f'touch {shlex.quote(str(background_root / "go"))}; '
                       f'while [ ! -f {shlex.quote(str(background_root / "done"))} ]; do sleep 0.01; done; cat same.txt',
            'changed_files': ['same.txt'],
            'timeout': 5,
        })
        assert result.success, result.error
        assert target.read_bytes() == b'peer'
        assert 'file_changes' not in (result.raw_output or {})
    finally:
        _, stderr = await asyncio.wait_for(process.communicate(b'\n'), timeout=10)
        assert process.returncode == 0, stderr.decode()
    result = await tool.invoke({'command': 'printf own > same.txt', 'changed_files': ['same.txt']})
    assert result.raw_output['file_changes'] == [
        {'path': 'same.txt', 'before_sha256': digest(b'peer'), 'after_sha256': digest(b'own')},
    ]


@pytest.mark.asyncio
async def test_cancelled_python_worker_finishes_before_other_tool_receipts(tmp_path, monkeypatch):
    tool = JupyterSandboxTool(workspace_dir=str(tmp_path))
    session = SimpleNamespace(workspace=tmp_path, is_alive=lambda: True, stop=AsyncMock())
    monkeypatch.setattr(tool, '_sessions', {'s1': session})
    monkeypatch.setattr(tool, '_get_sandbox_env', lambda: SimpleNamespace(ensure_ready=AsyncMock()))
    started, stopped = threading.Event(), threading.Event()

    def execute(*args):
        started.set()
        assert stopped.wait(5)
        (tmp_path / 'same.pdf').write_bytes(b'cancelled-worker')
        return '', [], None

    monkeypatch.setattr(tool, '_execute_session_code', execute)
    task = asyncio.create_task(tool.invoke({'code': 'work()', 'session_id': 's1'}))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    reader = asyncio.create_task(BashTool(workspace_dir=str(tmp_path), allow_full_access=True).invoke({
        'command': 'sleep 0.2; cat same.pdf', 'changed_files': ['same.pdf'],
    }))
    await asyncio.sleep(0.05)
    assert not task.done(), 'cancellation must not release the writer barrier while worker runs'
    stopped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await reader
    assert result.raw_output['file_changes'] == []
    session.stop.assert_awaited_once()
    assert 's1' not in tool._sessions


@pytest.mark.asyncio
async def test_image_generation_cannot_be_adopted_by_a_concurrent_script(tmp_path, monkeypatch):
    reader = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    image = GenerateImageTool(workspace_dir=str(tmp_path), endpoint='https://example.test/images')
    monkeypatch.setattr(image, '_request_image', AsyncMock(return_value=(b'png-bytes', 'image/png')))
    read, generated = await asyncio.gather(
        reader.invoke({'command': 'sleep 0.1'}),
        image.invoke({'prompt': 'test', 'output_path': 'peer.png', 'watermark': False}),
    )
    assert read.raw_output['file_changes'] == []
    assert generated.success, generated.error
    assert generated.raw_output['file_changes'] == [
        {'path': 'peer.png', 'before_sha256': None, 'after_sha256': digest(b'png-bytes')},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('output_kind', ['absolute', 'output_dir', 'mime_symlink'])
async def test_image_write_to_other_workspace_cannot_be_adopted_by_reader(
    tmp_path, monkeypatch, output_kind,
):
    source, destination = tmp_path / 'source', tmp_path / 'destination'
    source.mkdir()
    destination.mkdir()
    (destination / 'peer.jpg').write_bytes(b'before')
    output_path = str(destination / 'peer.jpg')
    output_dir = None
    if output_kind == 'output_dir':
        output_dir, output_path = str(destination), 'peer'
    elif output_kind == 'mime_symlink':
        (source / 'peer.jpg').symlink_to(destination / 'peer.jpg')
        output_path = 'peer'
    image = GenerateImageTool(
        workspace_dir=str(source), output_dir=output_dir, endpoint='https://example.test/images',
    )
    monkeypatch.setattr(image, '_request_image', AsyncMock(return_value=(b'image', 'image/jpeg')))
    started = asyncio.Event()
    reader = BashTool(workspace_dir=str(destination), allow_full_access=True)
    create = reader._create_subprocess

    async def announce(*args, **kwargs):
        process = await create(*args, **kwargs)
        started.set()
        return process

    monkeypatch.setattr(reader, '_create_subprocess', announce)
    reading = asyncio.create_task(reader.invoke({'command': 'sleep 0.3; cat peer.jpg'}))
    await asyncio.wait_for(started.wait(), 5)
    generated, read = await asyncio.wait_for(asyncio.gather(
        image.invoke({'prompt': 'test', 'output_path': output_path, 'watermark': False}), reading,
    ), 5)
    assert generated.success, generated.error
    assert read.success, read.error
    assert read.raw_output['file_changes'] == []
    assert (destination / 'peer.jpg').read_bytes() == b'image'


@pytest.mark.asyncio
@pytest.mark.parametrize('alias_kind', ['file', 'directory', 'chained_file', 'chained_directory'])
async def test_image_symlink_cannot_be_retargeted_during_generation(tmp_path, monkeypatch, alias_kind):
    from box_agent.tools import file_change_receipts as receipts

    source, aliases, first, second = (tmp_path / name for name in ('source', 'aliases', 'first', 'second'))
    for root in (source, aliases, first, second):
        root.mkdir()
    (first / 'image.png').write_bytes(b'first')
    (second / 'image.png').write_bytes(b'second')
    is_directory = alias_kind.endswith('directory')
    alias = aliases / ('link' if is_directory else 'alias.png')
    mutable_alias = alias
    owner = aliases
    if alias_kind.startswith('chained_'):
        owner = tmp_path / 'middle'
        owner.mkdir()
        mutable_alias = owner / alias.name
        alias.symlink_to(mutable_alias, target_is_directory=is_directory)
    mutable_alias.symlink_to(first if is_directory else first / 'image.png',
                             target_is_directory=is_directory)
    target = alias / 'image.png' if is_directory else alias
    image = GenerateImageTool(workspace_dir=str(source), endpoint='https://example.test/images')
    started, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def request(**kwargs):
        started.set()
        await release.wait()
        return b'image', 'image/png'

    monkeypatch.setattr(image, '_request_image', request)
    generating = asyncio.create_task(image.invoke({
        'prompt': 'test', 'output_path': str(target), 'watermark': False,
    }))
    await asyncio.wait_for(started.wait(), 5)
    overlapping = receipts._overlapping_lease

    def announce(root, records):
        result = overlapping(root, records)
        attempted.set()
        return result

    monkeypatch.setattr(receipts, '_overlapping_lease', announce)

    async def retarget():
        async with receipts._workspace_execution_lock(owner):
            mutable_alias.unlink()
            mutable_alias.symlink_to(second if is_directory else second / 'image.png',
                                     target_is_directory=is_directory)

    changing = asyncio.create_task(retarget())
    try:
        await asyncio.wait_for(attempted.wait(), 5)
        assert not changing.done(), 'the symlink owner directory must stay leased during generation'
    finally:
        release.set()
        result, _ = await asyncio.wait_for(asyncio.gather(generating, changing), 5)
    assert result.success, result.error
    assert (first / 'image.png').read_bytes() == b'image'
    assert (second / 'image.png').read_bytes() == b'second'


@pytest.mark.asyncio
async def test_browser_persistence_returns_exact_target_receipt(tmp_path):
    target = tmp_path / 'snapshot.md'

    def persist(target):
        target.write_bytes(b'snapshot')
        return ToolResult(success=True, content='snapshot')

    result = await persist_file_change(str(tmp_path), target, persist)
    assert result.raw_output['file_changes'] == [
        {'path': 'snapshot.md', 'before_sha256': None, 'after_sha256': digest(b'snapshot')},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['snapshot', 'screenshot'])
async def test_browser_persistence_rejects_target_retargeted_outside_workspace(tmp_path, kind):
    import base64
    from box_agent.tools.browser_result_adapter import (
        _prepare_browser_snapshot_output, _persist_browser_snapshot_output,
        _prepare_browser_screenshot_output, _persist_browser_screenshot_output,
    )

    source, destination = tmp_path / 'source', tmp_path / 'destination'
    source.mkdir()
    destination.mkdir()
    alias = source / 'out'
    alias.mkdir()
    if kind == 'snapshot':
        prepare, persist = _prepare_browser_snapshot_output, _persist_browser_snapshot_output
        name, filename = 'managed_browser_snapshot', 'snapshot.md'
        result = ToolResult(success=True, content='browser-write')
    else:
        prepare, persist = _prepare_browser_screenshot_output, _persist_browser_screenshot_output
        name, filename = 'managed_browser_take_screenshot', 'screenshot.png'
        result = ToolResult(success=True, raw_output={'mcp_inline_images': [
            {'data': base64.b64encode(b'image').decode()},
        ]})
    target, error = prepare(name, {'filename': f'out/{filename}'}, str(source))
    assert error is None
    alias.rmdir()
    alias.symlink_to(destination, target_is_directory=True)
    outcome = await persist_file_change(str(source), target, lambda resolved: persist(result, resolved))
    assert not outcome.success
    assert 'BROWSER_PERSISTENCE_OUTPUT_PATH_INVALID' in outcome.error
    assert not (destination / filename).exists()


@pytest.mark.asyncio
async def test_browser_persistence_uses_same_resolved_path_for_write_and_receipt(tmp_path):
    from box_agent.tools.browser_result_adapter import _persist_browser_snapshot_output

    out, nested = tmp_path / 'out', tmp_path / 'nested'
    nested.mkdir()
    out.symlink_to(nested, target_is_directory=True)
    result = ToolResult(success=True, content='snapshot')
    persisted_targets = []

    def persist(target):
        persisted_targets.append(target)
        return _persist_browser_snapshot_output(result, target)

    outcome = await persist_file_change(str(tmp_path), out / 'snapshot.md', persist)
    assert outcome.success
    assert persisted_targets == [nested.resolve() / 'snapshot.md']
    assert (nested / 'snapshot.md').read_text() == 'snapshot'
    assert outcome.raw_output['file_changes'] == [
        {'path': 'nested/snapshot.md', 'before_sha256': None, 'after_sha256': digest(b'snapshot')},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['create', 'replace', 'unchanged', 'failed', 'io_failure'])
async def test_staged_commit_receipt_describes_only_final_target(tmp_path, monkeypatch, mode):
    target = tmp_path / 'report.txt'
    if mode != 'create':
        target.write_bytes(b'report' if mode == 'unchanged' else b'before')
    before = digest(target.read_bytes()) if target.exists() else None
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    begin = await tool.invoke({'action': 'begin', 'path': 'report.txt', 'expected_chunks': 1})
    write_id = begin.raw_output['write_id']
    append = await tool.invoke({
        'action': 'append_text', 'write_id': write_id, 'chunk_index': 0, 'content': 'report',
    })
    assert begin.success and append.success
    assert 'file_changes' not in begin.raw_output
    assert 'file_changes' not in append.raw_output
    arguments = {'action': 'commit', 'write_id': write_id}
    if mode == 'failed':
        arguments['expected_sha256'] = '0' * 64
    elif mode == 'io_failure':
        def fail_replace(*args):
            raise OSError('destination unavailable')

        monkeypatch.setattr('box_agent.tools.staged_file_write_tool.os.replace', fail_replace)
    result = await tool.invoke(arguments)
    if mode in {'failed', 'io_failure'}:
        assert not result.success
        assert ('STAGED_FILE_HASH_MISMATCH' if mode == 'failed' else 'STAGED_FILE_WRITE_FAILED') in result.error
        assert target.read_bytes() == b'before'
        assert not (result.raw_output or {}).get('file_changes')
    else:
        assert result.success, result.error
        assert target.read_bytes() == b'report'
        assert result.raw_output['type'] == 'artifact'
        assert result.raw_output['sha256'] == digest(b'report')
        if mode == 'unchanged':
            assert not result.raw_output.get('file_changes')
        else:
            assert result.raw_output.get('file_changes_version') == 1
            assert result.raw_output['file_changes'] == [{
                'path': 'report.txt', 'before_sha256': before, 'after_sha256': digest(b'report'),
            }]


@pytest.mark.asyncio
async def test_staged_commit_receipt_tracks_symlink_replacement(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    referent = tmp_path / 'original.txt'
    referent.write_bytes(b'before')
    target = root / 'alias.txt'
    target.symlink_to(referent)
    tool = StagedFileWriteTool(workspace_dir=str(root))
    begin = await tool.invoke({'action': 'begin', 'path': 'alias.txt', 'expected_chunks': 1})
    write_id = begin.raw_output['write_id']
    await tool.invoke({'action': 'append_text', 'write_id': write_id, 'chunk_index': 0, 'content': 'report'})
    result = await tool.invoke({'action': 'commit', 'write_id': write_id})
    assert result.success, result.error
    assert not target.is_symlink()
    assert target.read_bytes() == b'report'
    assert referent.read_bytes() == b'before'
    assert result.raw_output.get('file_changes') == [{
        'path': 'alias.txt', 'before_sha256': digest(b'before'), 'after_sha256': digest(b'report'),
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['outside', 'background'])
async def test_staged_commit_remains_unconfirmed_outside_workspace_or_with_background_writer(
    tmp_path, monkeypatch, mode,
):
    from box_agent.tools.bash_tool import BackgroundShellManager

    root = tmp_path / 'workspace'
    root.mkdir()
    target = tmp_path / 'outside.txt' if mode == 'outside' else root / 'report.txt'
    tool = StagedFileWriteTool(workspace_dir=str(root))
    begin = await tool.invoke({'action': 'begin', 'path': str(target), 'expected_chunks': 1})
    write_id = begin.raw_output['write_id']
    await tool.invoke({'action': 'append_text', 'write_id': write_id, 'chunk_index': 0, 'content': 'report'})
    if mode == 'background':
        shell = SimpleNamespace(workspace_dir=str(root), process=SimpleNamespace(returncode=None))
        monkeypatch.setattr(BackgroundShellManager, '_shells', {'background': shell})
    result = await tool.invoke({'action': 'commit', 'write_id': write_id})
    assert result.success, result.error
    assert target.read_bytes() == b'report'
    assert 'file_changes' not in result.raw_output


@pytest.mark.asyncio
async def test_managed_background_writer_suppresses_workspace_receipts(tmp_path, monkeypatch):
    from box_agent.tools.bash_tool import BackgroundShellManager

    shell = SimpleNamespace(workspace_dir=str(tmp_path), process=SimpleNamespace(returncode=None))
    monkeypatch.setattr(BackgroundShellManager, '_shells', {'background': shell})
    result = await BashTool(workspace_dir=str(tmp_path), allow_full_access=True).invoke({
        'command': 'printf own > own.pdf',
    })
    assert result.success, result.error
    assert 'file_changes' not in (result.raw_output or {})


@pytest.mark.asyncio
async def test_incomplete_workspace_capture_never_guesses_a_partial_list(tmp_path, monkeypatch):
    monkeypatch.setenv('BOX_AGENT_ARTIFACT_SCAN_MAX_FILES', '1')
    (tmp_path / 'a').write_bytes(b'old')
    (tmp_path / 'b').write_bytes(b'old')
    result = await BashTool(workspace_dir=str(tmp_path), allow_full_access=True).invoke({
        'command': 'printf own > own.pdf',
    })
    assert result.success, result.error
    assert 'file_changes' not in (result.raw_output or {})


@pytest.mark.asyncio
async def test_acp_script_wire_has_canonical_tool_and_turn_bound_receipts(tmp_path, posix_shell):
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
                    ToolCall(id='script', type='function', function=FunctionCall(name='bash', arguments={
                        'command': 'printf own > own.xlsx; printf own > own.pdf',
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
    response = await adapter.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{'text': 'write test files'}], field_meta={}))
    assert response.field_meta['ok'] is True
    start = next(u for u in conn.updates if u.sessionUpdate == 'tool_call' and u.toolCallId == 'script')
    finish = next(u for u in conn.updates if u.sessionUpdate == 'tool_call_update' and
                  u.toolCallId == 'script' and isinstance(u.rawOutput, dict) and u.rawOutput.get('file_changes_version') == 1)
    assert start.model_dump(by_alias=True)['_meta']['tool_name'] == 'bash'
    assert finish.rawOutput['task_id'] == start.field_meta['task_id']
    assert finish.rawOutput['turn_id'] == start.field_meta['turn_id']
    assert finish.rawOutput['session_id'] == 'host-session'
    assert {change['path'] for change in finish.rawOutput['file_changes']} == {'own.xlsx', 'own.pdf'}
