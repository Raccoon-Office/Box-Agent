import asyncio
from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.acp.follow_up_suggestions import (
    FollowUpSuggestionsStreamExtractor,
    build_follow_up_suggestions_generation_system_prompt,
    build_follow_up_suggestions_prompt,
    normalize_follow_up_suggestions,
    parse_follow_up_suggestions_response,
)
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, ToolCall


class _RecordingConn:
    def __init__(self) -> None:
        self.updates = []

    async def sessionUpdate(self, payload):
        self.updates.append(payload)


class _FollowUpLLM:
    async def generate(self, messages, tools=None):
        return LLMResponse(content="完成。", finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **_):
        yield StreamEvent(
            type="text",
            delta=(
                "已完成方案。\n```follow_up_suggestions\n"
                '{"suggestions":["把方案拆成'
            ),
        )
        yield StreamEvent(
            type="activity",
            activity={"protocol": "agent_activity_v1", "phase": "provider_stream"},
        )
        yield StreamEvent(
            type="text",
            delta='今日待办", "给我一份风险清单"]}\n```',
        )
        yield StreamEvent(type="finish", finish_reason="stop")


class _DedicatedFollowUpLLM:
    def __init__(self) -> None:
        self.generate_calls = []
        self.release_suggestions = asyncio.Event()

    async def generate(self, messages, tools=None, **_):
        self.generate_calls.append(_)
        await self.release_suggestions.wait()
        return LLMResponse(
            content='{"suggestions":["核对项目当前的 React 版本", "查看 React 19.2 的新增内容"]}',
            finish_reason="stop",
        )

    async def generate_stream(self, messages, tools=None, **_):
        if _.get("call_kind") == "utility":
            self.generate_calls.append(_)
            await self.release_suggestions.wait()
            yield StreamEvent(
                type="text",
                delta='{"suggestions":["核对项目当前的 React 版本", "查看 React 19.2 的新增内容"]}',
            )
            yield StreamEvent(type="finish", finish_reason="stop")
            return
        yield StreamEvent(type="text", delta="React 当前 npm 稳定最新版是 19.2.7。")
        yield StreamEvent(type="finish", finish_reason="stop")


class _DecisionThenStopLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, tools=None, **_):
        return LLMResponse(content="done", finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="choose-delivery-scope",
                        type="function",
                        function=FunctionCall(
                            name="request_user_decision",
                            arguments={
                                "question": "请选择交付范围。",
                                "decision_kind": "delivery_scope",
                                "options": [
                                    {"id": "full", "label": "保持完整版本"},
                                    {"id": "prototype", "label": "先做精简版本"},
                                ],
                            },
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(
            type="text",
            delta=(
                "请在选择卡片中继续。\n```follow_up_suggestions\n"
                '{"suggestions":["选择完整版本"]}\n```'
            ),
        )
        yield StreamEvent(type="finish", finish_reason="stop")


def test_extractor_hides_split_metadata_block_and_keeps_visible_answer() -> None:
    extractor = FollowUpSuggestionsStreamExtractor()

    first = extractor.push("已整理好执行方案。\n```follow_up_sugg")
    second = extractor.push(
        'estions\n{"suggestions":["把方案拆成今日待办", "给我一份风险清单"]}\n```'
    )
    final = extractor.finish()

    assert "".join(first + second + final) == "已整理好执行方案。\n"
    assert extractor.suggestions == ["把方案拆成今日待办", "给我一份风险清单"]


def test_extractor_drops_invalid_or_unfinished_metadata() -> None:
    extractor = FollowUpSuggestionsStreamExtractor()

    assert extractor.push("回答。\n```follow_up_suggestions\n{bad json}\n```") == ["回答。\n"]
    assert extractor.suggestions == []

    extractor.push("\n```follow_up_suggestions\n")
    assert extractor.finish() == []


def test_normalize_follow_up_suggestions_limits_and_deduplicates() -> None:
    assert normalize_follow_up_suggestions(
        ["  整理成待办  ", "整理成待办", "再生成风险清单", "补充负责人", "第四条"]
    ) == ["整理成待办", "再生成风险清单", "补充负责人"]


def test_parser_accepts_json_and_accidental_fences() -> None:
    assert parse_follow_up_suggestions_response(
        '```json\n{"suggestions":["整理迁移步骤"]}\n```'
    ) == ["整理迁移步骤"]
    assert parse_follow_up_suggestions_response("不是 JSON") == []


def test_prompt_requires_a_structured_follow_up_block_only_after_completion() -> None:
    prompt = build_follow_up_suggestions_prompt()

    assert "```follow_up_suggestions" in prompt
    assert "任务失败" in prompt
    assert '"suggestions"' in build_follow_up_suggestions_generation_system_prompt()


@pytest.mark.asyncio
async def test_acp_strips_model_metadata_across_activity_and_emits_suggestions(tmp_path) -> None:
    conn = _RecordingConn()
    agent = BoxACPAgent(
        conn,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(),
        ),
        _FollowUpLLM(),
        [],
        "system",
    )
    session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={"follow_up_suggestions": True})
    )

    assert agent._sessions[session.sessionId].follow_up_suggestions_enabled is True
    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "做完了吗"}])
    )

    assert response.stopReason == "end_turn"
    visible_text = "".join(
        update.update.content.text
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "agent_message_chunk"
    )
    assert visible_text == "已完成方案。\n"
    assert "follow_up_suggestions" not in visible_text
    suggestion_updates = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "follow_up_suggestions"
    ]
    assert suggestion_updates == [
        {
            "type": "follow_up_suggestions",
            "suggestions": ["把方案拆成今日待办", "给我一份风险清单"],
        }
    ]


@pytest.mark.asyncio
async def test_acp_generates_suggestions_in_background_without_blocking_prompt(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    conn = _RecordingConn()
    llm = _DedicatedFollowUpLLM()
    agent = BoxACPAgent(
        conn,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(),
        ),
        llm,
        [],
        "system",
    )
    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "follow_up_suggestions": True,
                "session_id": "office-session-1",
                "title": "React 版本查询",
            },
        )
    )

    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "React 最新版是多少"}],
            field_meta={"turn_id": "office-turn-1", "title": "React 版本查询"},
        )
    )

    assert response.stopReason == "end_turn"
    suggestion_task = agent._sessions[session.sessionId].follow_up_suggestions_task
    assert suggestion_task is not None
    assert not suggestion_task.done()
    assert not any(
        getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "follow_up_suggestions"
        for update in conn.updates
    )

    llm.release_suggestions.set()
    await suggestion_task
    suggestion_updates = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "follow_up_suggestions"
    ]
    assert suggestion_updates == [
        {
            "type": "follow_up_suggestions",
            "turn_id": "office-turn-1",
            "suggestions": ["核对项目当前的 React 版本", "查看 React 19.2 的新增内容"],
        }
    ]
    assert llm.generate_calls[-1]["session_id"] == "office-session-1"
    assert llm.generate_calls[-1]["turn_id"] == "office-turn-1"
    assert llm.generate_calls[-1]["title"] == "React 版本查询"


@pytest.mark.asyncio
async def test_acp_cancels_stale_background_suggestions_when_next_turn_starts(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    conn = _RecordingConn()
    llm = _DedicatedFollowUpLLM()
    agent = BoxACPAgent(
        conn,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(),
        ),
        llm,
        [],
        "system",
    )
    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={"follow_up_suggestions": True, "session_id": "office-session-1"},
        )
    )

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "第一轮"}],
            field_meta={"turn_id": "office-turn-1"},
        )
    )
    first_task = agent._sessions[session.sessionId].follow_up_suggestions_task
    assert first_task is not None

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "第二轮"}],
            field_meta={"turn_id": "office-turn-2"},
        )
    )
    second_task = agent._sessions[session.sessionId].follow_up_suggestions_task
    assert second_task is not None
    assert second_task is not first_task
    await asyncio.sleep(0)
    assert first_task.done()

    llm.release_suggestions.set()
    await second_task
    suggestion_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "follow_up_suggestions"
    ]
    assert [output["turn_id"] for output in suggestion_outputs] == ["office-turn-2"]


@pytest.mark.asyncio
async def test_acp_does_not_mix_follow_up_suggestions_with_user_decision(tmp_path) -> None:
    conn = _RecordingConn()
    llm = _DecisionThenStopLLM()
    agent = BoxACPAgent(
        conn,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(max_steps=8, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(),
        ),
        llm,
        [],
        "system",
    )
    session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={"follow_up_suggestions": True})
    )

    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "制作演示文稿"}])
    )

    assert response.stopReason == "end_turn"
    assert agent._sessions[session.sessionId].waiting_for_user_input is True
    raw_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
    ]
    assert any(output.get("type") == "user_decision_request" for output in raw_outputs)
    assert not any(output.get("type") == "follow_up_suggestions" for output in raw_outputs)

    resumed = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "我不选这两个方案，改成六页并突出案例。"}],
        )
    )

    assert resumed.stopReason == "end_turn"
    assert agent._sessions[session.sessionId].waiting_for_user_input is False
