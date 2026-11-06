"""Real 0.12.1 router/stdio and process restart, with deterministic local tools."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pytest

from tests.acp_host.probe import AcpHostProbe


@pytest.mark.asyncio
async def test_sdk_restart_replays_originals_and_tools_before_load_response_twice():
    repository = Path(__file__).resolve().parents[1]
    workspace = repository / 'workspace'
    workspace.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='input-sdk-', dir=workspace) as directory:
        root = Path(directory)
        cwd = root / 'files'
        cwd.mkdir()
        file = cwd / '中文 空格.csv'
        file.write_text('TOKEN=SDK-719\nVALUE=719\n')
        def probe():
            return AcpHostProbe(command=[sys.executable, '-m', 'tests.desktop_runtime_fixture'], cwd=repository,
                env={'BOX_AGENT_DESKTOP_TEST_ROOT': str(root), 'BOX_AGENT_DESKTOP_TEST_FILE_INPUTS': '1'}, timeout_s=30)
        first = probe()
        await first.start()
        try:
            initialized = await first.initialize()
            assert initialized['agentCapabilities']['loadSession'] is True
            identifier = await first.session_new(cwd=str(cwd))
            await first.session_prompt(identifier, [{'type': 'text', 'text': 'fixture:file_inputs'},
                {'type': 'resource_link', 'uri': file.as_uri(), 'name': file.name}], meta={'turn_id': 'original-turn'})
            lifecycle = [json.loads(line) for line in (root / 'lifecycle.jsonl').read_text().splitlines()]
            result = next(event for event in lifecycle if event['event'] == 'file_read_result')
            assert result['tokens'] == ['SDK-719'] and result['sum'] == 719
        finally:
            await first.stop()
        second = probe()
        await second.start()
        try:
            await second.initialize()
            def calls():
                return [json.loads(line) for line in (root / 'lifecycle.jsonl').read_text().splitlines()
                        if json.loads(line)['event'] in {'file_model_input', 'file_read_result'}]
            before = calls()
            for _ in range(2):
                await second.request('session/load', {'sessionId': identifier, 'cwd': str(cwd), 'mcpServers': []})
                updates = [note['params']['update'] for note in second.drain_notifications()
                           if note.get('method') == 'session/update']
                assert updates and all(update['_meta']['replay'] for update in updates)
                assert len({update['_meta']['eventId'] for update in updates}) == len(updates)
                types = [update['sessionUpdate'] for update in updates]
                assert 'user_message_chunk' in types and 'tool_call' in types and 'tool_call_update' in types
                assert any(update.get('content', {}).get('uri') == file.as_uri() for update in updates)
                assert calls() == before
            await second.session_prompt(identifier, 'continue', meta={'turn_id': 'continued-turn'})
        finally:
            await second.stop()
