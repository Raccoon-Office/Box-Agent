"""ACP cancellation interrupts provider waits without restarting shared runtime."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.kernel import stream_controller
from box_agent.llm import LLMClient
from box_agent.schema import LLMProvider, StreamEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_output", [False, True])
async def test_acp_cancel_wakes_stalled_model_and_keeps_sessions_usable(tmp_path, partial_output):
    started, closed = asyncio.Event(), asyncio.Event()
    other_started, release_other = asyncio.Event(), asyncio.Event()

    class Model:
        calls = 0

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                try:
                    if partial_output:
                        yield StreamEvent(type="text", delta="partial")
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            elif self.calls == 2:
                other_started.set()
                await release_other.wait()
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    class Conn:
        async def sessionUpdate(self, payload):
            pass

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False, enable_mcp=False),
    )
    agent = BoxACPAgent(Conn(), config, Model(), [], "system")
    first = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
    second = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))

    async def prompt(session_id):
        return await agent.prompt(SimpleNamespace(sessionId=session_id, prompt=[{"text": "hello"}]))

    pending = asyncio.create_task(prompt(first.sessionId))
    other_pending = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        other_pending = asyncio.create_task(prompt(second.sessionId))
        await asyncio.wait_for(other_started.wait(), 2)
        await agent.cancel(SimpleNamespace(sessionId=first.sessionId))
        response = await asyncio.wait_for(asyncio.shield(pending), 1)
        assert response.stopReason == "cancelled"
        assert closed.is_set()
        state = agent._sessions[first.sessionId]
        assert not state._prompt_in_progress
        assert not state.run_handle.is_active
        assert not other_pending.done(), "cancelling one run must not interrupt another"
        release_other.set()
        other = await asyncio.wait_for(other_pending, 2)
        assert other.stopReason == "end_turn"
        # No runtime restart or replacement session is needed to send again.
        resumed = await asyncio.wait_for(prompt(first.sessionId), 2)
        assert resumed.stopReason == "end_turn"
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        if other_pending is not None:
            other_pending.cancel()
            await asyncio.gather(other_pending, return_exceptions=True)
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_error", [False, True])
@pytest.mark.parametrize("suggestions_enabled", [False, True])
@pytest.mark.parametrize("startup_delay", [0, 2.1])
async def test_acp_cancel_chunk_race_waits_for_http_cleanup(
    tmp_path, monkeypatch, cleanup_error, suggestions_enabled, startup_delay,
):
    stream_started, close_started, release_close, closed = (asyncio.Event() for _ in range(4))
    requests = 0

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            stream_started.set()
            chunk = {
                "id": "response-1", "object": "chat.completion.chunk",
                "created": 0, "model": "test-model",
                "choices": [{"index": 0, "delta": {"content": "hello"},
                             "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            await asyncio.Event().wait()

        async def aclose(self):
            close_started.set()
            await release_close.wait()
            closed.set()
            if cleanup_error:
                raise OSError("fixture response close failure")

    async def handler(request):
        nonlocal requests
        await asyncio.sleep(startup_delay)
        requests += 1
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Body(),
        )

    class Conn:
        async def sessionUpdate(self, payload):
            pass

    llm = LLMClient(
        api_key="test", provider=LLMProvider.OPENAI,
        api_base="https://example.com/v1", model="test-model",
    )
    await llm._client.client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        llm._client.client = llm._client.client.with_options(http_client=http_client)
        config = Config(
            llm=LLMConfig(api_key="test"),
            agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(enable_sub_agent=False, enable_mcp=False),
        )
        agent = BoxACPAgent(Conn(), config, llm, [], "system")
        session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
        agent._sessions[session.sessionId].follow_up_suggestions_enabled = suggestions_enabled
        real_wait = asyncio.wait

        async def complete_read_and_cancel(tasks, **kwargs):
            is_stream_wait = any(
                "RunControl.wait_cancelled" in getattr(task.get_coro(), "__qualname__", "")
                for task in tasks
            )
            if not is_stream_wait:
                return await real_wait(tasks, **kwargs)
            # Complete the read before cancelling, so cleanup must traverse
            # suspended wrappers rather than propagate through a pending read.
            await real_wait(tasks, **kwargs)
            await agent.cancel(SimpleNamespace(sessionId=session.sessionId))
            return await real_wait(tasks)

        monkeypatch.setattr(stream_controller.asyncio, "wait", complete_read_and_cancel)
        pending = asyncio.create_task(agent.prompt(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{"text": "hello"}],
        )))
        try:
            # Cold session/provider startup is separate from the cleanup deadline.
            await asyncio.wait_for(stream_started.wait(), 10)
            await asyncio.wait_for(close_started.wait(), 2)
            assert not pending.done(), "cancelled response must await HTTP cleanup"
            release_close.set()
            response = await asyncio.wait_for(asyncio.shield(pending), 2)
            assert response.stopReason == "cancelled"
            assert closed.is_set()
            assert requests == 1, "cancellation must not retry the model request"
        finally:
            release_close.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await agent.aclose()
