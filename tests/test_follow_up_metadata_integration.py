"""Follow-up metadata stays out of ACP replies and durable assistant history."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from box_agent.acp import (
    BoxACPAgent,
    _ActionHintNormalizingLLM,
    _FollowUpSuggestionsExtractingLLM,
)
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import LLMResponse, StreamEvent
from tests.acp_helpers import acp_params


_SUGGESTIONS = ["比较代码里的 ``` 与 </follow_up_suggestions> 字符", "生成风险清单"]
_PAYLOAD = json.dumps({"suggestions": _SUGGESTIONS}, ensure_ascii=False)
_BLOCKS = [
    f"```follow_up_suggestions\n{_PAYLOAD}\n```",
    f"follow_up_suggestions\n{_PAYLOAD}\n</follow_up_suggestions>",
    f"<follow_up_suggestions>\n{_PAYLOAD}\n</follow_up_suggestions>",
    f"```follow_up_suggestions\n{_PAYLOAD}\n</follow_up_suggestions>",
    f"```follow_up_suggestions\r\n{_PAYLOAD}\r\n```",
    *[f"{' ' * indent}```follow_up_suggestions\n{_PAYLOAD}\n{' ' * indent}```"
      for indent in (1, 2, 3)],
]


class _ReplyModel:
    def __init__(self, reply, *, character_chunks=False, finish_event=True):
        self.reply = reply
        self.character_chunks = character_chunks
        self.finish_event = finish_event

    async def generate(self, messages, tools=None, **kwargs):
        content = (
            '{"continue":false}'
            if kwargs.get("call_kind") == "turn_continuation_judge"
            else self.reply
        )
        return LLMResponse(content=content, finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **kwargs):
        chunks = list(self.reply) if self.character_chunks else [self.reply]
        for chunk in chunks:
            yield StreamEvent(type="text", delta=chunk)
            yield StreamEvent(type="activity", activity={"phase": "provider_stream"})
        if self.finish_event:
            yield StreamEvent(type="finish", finish_reason="stop")


class _RecordingConnection:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **meta):
        self.updates.append(update)


def _new_agent(conn, model, tmp_path):
    return BoxACPAgent(
        conn,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(enable_sub_agent=False, enable_mcp=False),
        ),
        model,
        [],
        "system",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("block", _BLOCKS)
@pytest.mark.parametrize("character_chunks", [False, True])
@pytest.mark.parametrize("before", ["已完成。", "Example: <code>don't</code>"])
async def test_metadata_is_structured_without_polluting_reply_or_session_log(
    tmp_path, block, character_chunks, before,
):
    conn = _RecordingConnection()
    agent = _new_agent(
        conn, _ReplyModel(f"{before}\n{block}\r\n补充说明。", character_chunks=character_chunks), tmp_path,
    )
    expected = before + "补充说明。"
    try:
        session = await agent.new_session(**acp_params(SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={"follow_up_suggestions": True, "session_id": "follow-up-history"},
        )))
        response = await agent.prompt(**acp_params(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{"text": "给出结果"}],
        )))
        assert response.stopReason == "end_turn"
        visible = "".join(
            update.content.text for update in conn.updates
            if getattr(update, "sessionUpdate", None) == "agent_message_chunk"
        )
        assert visible.replace("\r", "").replace("\n", "") == expected
        suggestions = [
            update.rawOutput["suggestions"] for update in conn.updates
            if getattr(update, "sessionUpdate", None) == "tool_call_update"
            and isinstance(getattr(update, "rawOutput", None), dict)
            and update.rawOutput.get("type") == "follow_up_suggestions"
        ]
        assert suggestions == [_SUGGESTIONS]
        snapshots = [
            update.rawOutput["content"] for update in conn.updates
            if getattr(update, "sessionUpdate", None) == "tool_call_update"
            and isinstance(getattr(update, "rawOutput", None), dict)
            and update.rawOutput.get("type") == "llm_output"
        ]
        assert snapshots, "the host's full-output fallback must also be exercised"
        assert all(text.replace("\r", "").replace("\n", "") == expected for text in snapshots)
        state = agent._sessions[session.sessionId]
        assert state.follow_up_suggestions_task is None
        for messages in (state.agent.messages, state.agent.session_log.replay().messages):
            assistant_text = "".join(
                str(message.content) for message in messages if message.role == "assistant"
            )
            assert assistant_text.replace("\r", "").replace("\n", "") == expected
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("character_chunks", [False, True])
@pytest.mark.parametrize("reply", [
    'Example: <code>\nfollow_up_suggestions\n{"suggestions":["Example"]}\n</code>',
    'Example: `\nfollow_up_suggestions\n{"suggestions":["Example"]}\n`',
])
async def test_inline_code_example_stays_in_acp_reply_and_session_log(tmp_path, character_chunks, reply):
    conn = _RecordingConnection()
    agent = _new_agent(conn, _ReplyModel(reply, character_chunks=character_chunks), tmp_path)
    try:
        session = await agent.new_session(**acp_params(SimpleNamespace(
            cwd=str(tmp_path), field_meta={"follow_up_suggestions": True},
        )))
        await agent.prompt(**acp_params(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{"text": "Show the example"}],
        )))
        visible = "".join(
            update.content.text for update in conn.updates
            if getattr(update, "sessionUpdate", None) == "agent_message_chunk"
        )
        assert visible == reply
        for update in conn.updates:
            raw = getattr(update, "rawOutput", None)
            if isinstance(raw, dict) and raw.get("type") == "llm_output":
                assert raw["content"] == reply
            if isinstance(raw, dict) and raw.get("type") == "follow_up_suggestions":
                assert raw["suggestions"] == []
        state = agent._sessions[session.sessionId]
        for messages in (state.agent.messages, state.agent.session_log.replay().messages):
            assert "".join(
                str(message.content) for message in messages if message.role == "assistant"
            ) == reply
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("block", _BLOCKS)
@pytest.mark.parametrize("finish_event", [False, True])
async def test_stream_exhaustion_and_nonstream_keep_the_same_metadata_boundary(block, finish_event):
    model = _ReplyModel(f"回答。\n{block}", character_chunks=True, finish_event=finish_event)
    wrapped = _FollowUpSuggestionsExtractingLLM(_ActionHintNormalizingLLM(model))
    events = [event async for event in wrapped.generate_stream([])]
    assert "".join(event.delta or "" for event in events if event.type == "text") == "回答。\n"
    assert wrapped.follow_up_suggestions == _SUGGESTIONS
    assert (await wrapped.generate([])).content == "回答。\n"
    await wrapped.generate([], call_kind="turn_continuation_judge")
    assert wrapped.follow_up_suggestions == _SUGGESTIONS


@pytest.mark.asyncio
@pytest.mark.parametrize("opening", ["```follow_up_suggestions\n", "<follow_up_suggestions>\n"])
async def test_cancel_during_metadata_does_not_leak_or_reuse_suggestions(tmp_path, opening):
    started = asyncio.Event()
    closed = asyncio.Event()

    class StallingModel(_ReplyModel):
        calls = 0

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                try:
                    yield StreamEvent(type="text", delta='已完成。\n' + opening + '{"suggestions":["未结束')
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            else:
                yield StreamEvent(type="text", delta='新回答。\n<follow_up_suggestions>{"suggestions":["新建议"]}</follow_up_suggestions>')
                yield StreamEvent(type="finish", finish_reason="stop")

    conn = _RecordingConnection()
    agent = _new_agent(conn, StallingModel(""), tmp_path)
    pending = None
    try:
        session = await agent.new_session(**acp_params(SimpleNamespace(
            cwd=str(tmp_path), field_meta={"follow_up_suggestions": True},
        )))
        params = acp_params(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "继续"}]))
        pending = asyncio.create_task(agent.prompt(**params))
        await asyncio.wait_for(started.wait(), 10)
        await agent.cancel(**acp_params(SimpleNamespace(sessionId=session.sessionId)))
        assert (await asyncio.wait_for(pending, 2)).stopReason == "cancelled"
        assert closed.is_set()
        visible = "".join(
            update.content.text for update in conn.updates
            if getattr(update, "sessionUpdate", None) == "agent_message_chunk"
        )
        assert visible == "已完成。\n"
        conn.updates.clear()
        assert (await agent.prompt(**params)).stopReason == "end_turn"
        suggestions = [
            update.rawOutput["suggestions"] for update in conn.updates
            if getattr(update, "sessionUpdate", None) == "tool_call_update"
            and isinstance(getattr(update, "rawOutput", None), dict)
            and update.rawOutput.get("type") == "follow_up_suggestions"
        ]
        assert suggestions == [["新建议"]]
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await agent.aclose()
