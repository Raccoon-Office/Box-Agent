"""Vision proxy requests inherit the current parent thinking choice."""

import json

import httpx
from openai import AsyncOpenAI
import pytest

from box_agent.agent import Agent
from box_agent.llm.llm_wrapper import LLMClient
from box_agent.schema import LLMProvider, Message
from box_agent.tools.image_inspection_tool import ImageInspectionTool
from box_agent.tools.setup import add_workspace_tools
from tests.test_image_inspection_tool import _ONE_PIXEL_PNG, ToolConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["same", "catalog", "independent"])
@pytest.mark.parametrize("disabled_effort", [None, "low"])
@pytest.mark.parametrize("initial", [True, False])
async def test_proxy_inherits_current_parent_thinking_before_loop_and_across_turns(
    tmp_path, monkeypatch, binding, disabled_effort, initial,
):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "state"))
    # The temporary profile must not inherit host resource directory overrides.
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_SKILL_TOOLS_ROOT", raising=False)
    (tmp_path / "slide.png").write_bytes(_ONE_PIXEL_PNG)
    requests = []

    async def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={
            "id": "vision-test", "created": 1, "model": body["model"],
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Readable."}}],
        })

    async def client(model, policy):
        llm = LLMClient(
            api_key="test", provider=LLMProvider.OPENAI,
            api_base="https://inference.example/v1", model=model,
            reasoning_effort_when_disabled=policy,
        )
        await llm.aclose()
        llm._client.client = AsyncOpenAI(
            api_key="test", base_url=llm.api_base, max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        )
        return llm

    main = await client("SenseNova-Main", disabled_effort)
    independent = None
    try:
        if binding == "independent":
            # A separately injected vision endpoint retains its own off policy.
            vision_policy = "low" if disabled_effort is None else None
            independent = await client("SenseNova-Vision", vision_policy)
            image_tool = ImageInspectionTool(independent, workspace_dir=str(tmp_path))
            tools = [image_tool]
        else:
            vision_policy = disabled_effort
            main.capabilities = {"image_input": binding == "same"}
            if binding == "catalog":
                main.auto_model_candidates = (
                    {"model": main.model, "tags": ["code"], "abilityLevel": 4},
                    {"model": "SenseNova-Vision", "tags": ["vision"], "abilityLevel": 5},
                )
            tools = []
            add_workspace_tools(
                tools, ToolConfig(), tmp_path, llm=main,
                output=lambda *_: None,
            )
            image_tool = next(tool for tool in tools if tool.name == "inspect_images")

        agent = Agent(
            llm_client=main, tools=tools, system_prompt="Be concise.",
            workspace_dir=str(tmp_path), thinking_enabled=initial,
        )
        for turn, enabled in enumerate((initial, not initial)):
            if turn:
                agent.thinking_enabled = enabled
            await main.generate(
                [Message(role="user", content="Inspect the provided material.")],
                thinking_enabled=agent.thinking_enabled,
            )
            # ACP attachment inspection runs before Agent.run_events().
            result = await image_tool.execute(["slide.png"], "Check readability.")
            assert result.success, result.error
            main_request, image_request = requests[-2:]
            assert main_request["reasoning_effort"] == (
                "high" if enabled else disabled_effort or "none"
            )
            assert image_request["reasoning_effort"] == (
                "high" if enabled else vision_policy or "none"
            )
            assert any(
                block.get("type") == "image_url"
                for message in image_request["messages"]
                if isinstance(message["content"], list)
                for block in message["content"]
            )
        assert main.reasoning_effort_when_disabled == disabled_effort
        assert image_tool.llm.reasoning_effort_when_disabled == vision_policy
        assert image_tool.llm.model == (
            "SenseNova-Main" if binding == "same" else "SenseNova-Vision"
        )
    finally:
        await main.aclose()
        if independent is not None:
            await independent.aclose()
