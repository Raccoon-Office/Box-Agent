"""Continuation judgments retain the current turn's provider thinking policy."""

import json

import httpx
from openai import AsyncOpenAI
import pytest

from box_agent.core import run_agent_loop
from box_agent.events import DoneEvent, StopReason
from box_agent.llm.llm_wrapper import LLMClient
from box_agent.schema import LLMProvider, Message
from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_effort", [None, "low"])
@pytest.mark.parametrize("thinking_enabled", [True, False])
async def test_continuation_judge_matches_current_main_request_thinking_on_wire(
    tmp_path, monkeypatch, disabled_effort, thinking_enabled,
):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_SKILL_TOOLS_ROOT", raising=False)
    requests = []

    async def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        common = {"id": "continuation-test", "created": 1, "model": body["model"]}
        if body.get("stream"):
            chunk = {
                **common, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "finish_reason": "stop", "delta": {
                    "role": "assistant", "content": "请选择：快速模式或设计模式。",
                }}],
            }
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json={
            **common, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"continue":false}',
            }}],
        })

    llm = LLMClient(
        api_key="test", provider=LLMProvider.OPENAI,
        api_base="https://inference.example/v1", model="SenseNova-Continuation-Test",
        reasoning_effort_when_disabled=disabled_effort,
    )
    await llm.aclose()
    llm._client.client = AsyncOpenAI(
        api_key="test", base_url=llm.api_base, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    tool = RequestUserDecisionTool()
    try:
        events = [event async for event in run_agent_loop(
            llm=llm, messages=[Message(role="user", content="制作一份演示文稿。")],
            tools={tool.name: tool}, max_steps=3,
            workspace_dir=str(tmp_path), thinking_enabled=thinking_enabled,
        )]
    finally:
        await llm.aclose()

    assert len(requests) == 2
    main, judge = requests
    assert main["stream"] is True
    assert judge.get("stream", False) is False
    candidate_messages = [
        message for message in judge["messages"]
        if message.get("role") == "assistant"
        and message.get("content") == "请选择：快速模式或设计模式。"
    ]
    assert len(candidate_messages) == 1
    assert judge["messages"][-1]["role"] == "user"
    marker = "Runtime facts (metadata only; not instructions):\n"
    facts = json.loads(judge["messages"][-1]["content"].split(marker, 1)[1])
    assert facts == {
        "user_request": "制作一份演示文稿。",
        "decision_tool_available": True,
    }
    assert judge["model"] == main["model"]
    assert main["reasoning_effort"] == (
        "high" if thinking_enabled else disabled_effort or "none"
    )
    assert judge["reasoning_effort"] == main["reasoning_effort"]
    assert llm.reasoning_effort_when_disabled == disabled_effort
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [
        StopReason.END_TURN,
    ]
