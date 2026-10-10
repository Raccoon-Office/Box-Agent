"""Host transitions keep conversation history and finished-run results usable."""

from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from tests.test_acp import DoneLLM, DummyConn


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["switch", "rebind", "restart_adapter"])
async def test_conversations_continue_after_host_transition(tmp_path, transition):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2, workspace_dir=str(workspace), enable_memory=False,
            enable_memory_extraction=False, goal_autopilot_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False, enable_bash=False, enable_mcp=False,
            enable_sub_agent=False, enable_skills=False,
        ),
    )

    def make_adapter():
        return BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")

    async def bind(adapter, product):
        return (await adapter.newSession(SimpleNamespace(
            cwd=str(workspace), field_meta={"session_id": product},
        ))).sessionId

    async def prompt(adapter, session_id, text):
        result = await adapter.prompt(SimpleNamespace(
            sessionId=session_id, prompt=[SimpleNamespace(type="text", text=text)], field_meta={},
        ))
        assert result.stopReason == "end_turn"
        return adapter._sessions[session_id].run_handle

    def users(adapter, session_id):
        return [str(message.content) for message in adapter._sessions[session_id].agent.messages
                if message.role == "user"]

    adapter = make_adapter()
    try:
        first, peer = await bind(adapter, "first"), await bind(adapter, "peer")
        old_handle = await prompt(adapter, first, "alpha-original")
        old_state = old_handle.state
        saved_result = await old_handle.result()
        await prompt(adapter, peer, "beta-original")
        first_history, peer_history = users(adapter, first), users(adapter, peer)

        if transition == "rebind":
            previous_id = first
            first = await bind(adapter, "first")
            assert first != previous_id and previous_id not in adapter._sessions
            assert old_state._closed
        elif transition == "restart_adapter":
            await adapter.aclose()
            assert old_state._closed
            adapter = make_adapter()
            first, peer = await bind(adapter, "first"), await bind(adapter, "peer")

        assert users(adapter, first) == first_history
        assert users(adapter, peer) == peer_history
        # A host retaining the finished handle can still read it after refresh
        # or shutdown, independently of the newly restored session instance.
        assert old_handle.state is old_state
        assert await old_handle.result() is saved_result
        assert saved_result.status == "completed"

        for session_id, own, foreign in ((first, "alpha", "beta"), (peer, "beta", "alpha")):
            await prompt(adapter, session_id, own + "-continued")
            history = users(adapter, session_id)
            assert sum(own + "-original" in text for text in history) == 1
            assert sum(own + "-continued" in text for text in history) == 1
            assert not any(foreign + "-" in text for text in history)
        assert await old_handle.result() is saved_result
    finally:
        await adapter.aclose()
