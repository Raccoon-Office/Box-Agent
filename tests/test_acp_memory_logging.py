"""Memory diagnostic omission covers ACP stderr/file without changing results."""
import asyncio

import pytest

from box_agent.acp.debug_logger import ACPDebugLogger
from box_agent.payload_privacy import private_payload_scope, private_payloads


@pytest.mark.parametrize('log_format', ['json', 'text'])
@pytest.mark.parametrize('private_scope', [True, False])
def test_memory_tool_payloads_and_raw_errors_never_reach_any_log_level(
    tmp_path, monkeypatch, capsys, log_format, private_scope,
):
    from contextlib import nullcontext
    canary = 'MV2-LOG-PRIVATE-short-body'
    logfile = tmp_path / 'agent.log'
    monkeypatch.setenv('BOX_AGENT_LOG_LEVEL', 'debug')
    monkeypatch.setenv('BOX_AGENT_LOG_FORMAT', log_format)
    monkeypatch.setenv('BOX_AGENT_LOG_FILE', str(logfile))
    monkeypatch.setenv('BOX_AGENT_LOG_FULL_PAYLOAD', '1')
    logger = ACPDebugLogger()
    arguments = {'content': canary, 'source': canary}
    try:
        with private_payload_scope() if private_scope else nullcontext():
            for tool in ['memory_read', 'memory_managed_read', 'memory_write', 'memory_edit', 'memory_save']:
                for method, event in [(logger.debug, 'tool/start'), (logger.info, 'tool/end'), (logger.warn, 'tool/fail'), (logger.error, 'tool/cancel')]:
                    method(event, tool_name=tool, tool_call_id='tc-1', arguments=arguments,
                           result=canary, error=canary, content=canary, raw_output=arguments,
                           source=canary, traceback=canary, user_visible=True, duration_ms=25)
                try:
                    raise ValueError(canary)
                except ValueError as exc:
                    logger.exception('tool/fail', exc, tool_name=tool, tool_call_id='tc-1')
        output = capsys.readouterr().err + logfile.read_text()
        assert canary not in output
        assert 'tc-1' in output and 'memory_read' in output and 'duration_ms' in output
        assert 'payload_redacted' in output
        assert arguments['content'] == canary
    finally:
        logger.close()


async def test_private_acp_scope_covers_unknown_errors_and_cancellation_then_resets(monkeypatch, capsys):
    monkeypatch.setenv('BOX_AGENT_LOG_LEVEL', 'debug')
    monkeypatch.delenv('BOX_AGENT_LOG_FILE', raising=False)
    logger = ACPDebugLogger()
    canary = 'MV2-LOG-PRIVATE-error-body'
    try:
        with pytest.raises(asyncio.CancelledError):
            with private_payload_scope():
                logger.error('session/prompt', error=canary, traceback=canary, session_id='s-1')
                raise asyncio.CancelledError()
        assert not private_payloads()
        assert canary not in capsys.readouterr().err
        logger.info('ordinary', message='ordinary diagnostics remain')
        assert 'ordinary diagnostics remain' in capsys.readouterr().err
    finally:
        logger.close()


@pytest.mark.parametrize('cancelled', [False, True])
async def test_desktop_prompt_failure_and_cancellation_reset_privacy_and_reservation(monkeypatch, capsys, cancelled):
    from types import SimpleNamespace
    from box_agent.acp import BoxACPAgent, SessionState
    monkeypatch.setenv('BOX_AGENT_LOG_LEVEL', 'debug')
    monkeypatch.delenv('BOX_AGENT_LOG_FILE', raising=False)
    logger = ACPDebugLogger()
    canary = 'MV2-PROMPT-PRIVATE-failure'
    config = SimpleNamespace(agent=SimpleNamespace(memory_backend_type='desktop'))
    state = SessionState(agent=SimpleNamespace(session_log=None), config=config)

    async def fail_prompt(*_):
        assert private_payloads()
        try:
            raise asyncio.CancelledError(canary) if cancelled else RuntimeError(canary)
        except BaseException as exc:
            logger.exception('session/prompt', exc, session_id='s-1')
            raise

    stub = SimpleNamespace(_config=config, _require_accepting_requests=lambda: None,
                           _session_bindings_in_progress={}, _sessions={'s-1':state},
                           _prompt_for_session=fail_prompt)
    try:
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
            await BoxACPAgent.prompt(stub, SimpleNamespace(sessionId='s-1'))
        assert not private_payloads()
        assert not state._prompt_in_progress
        assert state._prompt_finished.done()
        assert canary not in capsys.readouterr().err
    finally:
        logger.close()
