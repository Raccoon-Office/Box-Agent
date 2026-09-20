"""Session behavior through the real ACP subprocess."""
import asyncio
import os
import signal

import pytest

from .probe import RpcError, collect_session_updates, message_text_from_updates


@pytest.mark.asyncio
async def test_prompt_returns_visible_reply(live_probe):
    probe, session_id = live_probe
    response = await probe.session_prompt(session_id, 'Reply with the word hello.')
    updates = collect_session_updates(probe.drain_notifications())
    assert response.get('stopReason') == 'end_turn'
    assert response.get('_meta', {}).get('ok') is True
    assert message_text_from_updates(updates).strip()


@pytest.mark.asyncio
async def test_cancel_during_response_returns_cancelled(live_probe):
    probe, session_id = live_probe
    pending = asyncio.create_task(probe.session_prompt(
        session_id, 'Write a detailed essay of at least 5000 words about computing.'))
    try:
        # Cancel only after an observed response, rather than an arbitrary delay.
        async def wait_for_text():
            while not pending.done():
                updates = collect_session_updates(probe._updates.notifications)
                if message_text_from_updates(updates):
                    return
                await asyncio.sleep(0.02)
        await asyncio.wait_for(wait_for_text(), timeout=60)
        if pending.done():
            response = await pending
            assert response.get('_meta', {}).get('ok') is True
            pytest.skip('Provider completed before cancellation could be sent')
        await probe.session_cancel(session_id)
        response = await asyncio.wait_for(pending, timeout=30)
        assert response.get('stopReason') == 'cancelled'
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_killed_process_fails_pending_request(connected_probe):
    probe, _ = connected_probe
    if not hasattr(signal, 'SIGSTOP'):
        pytest.skip('Pending-request kill probe requires POSIX SIGSTOP')
    os.kill(probe._proc.pid, signal.SIGSTOP)
    pending = asyncio.create_task(probe.initialize())
    try:
        await asyncio.sleep(0.05)
        assert not pending.done()
        await probe.kill()
        with pytest.raises(RpcError) as error:
            await pending
        assert error.value.code == 'eof'
    finally:
        await probe.kill()
        await asyncio.gather(pending, return_exceptions=True)
