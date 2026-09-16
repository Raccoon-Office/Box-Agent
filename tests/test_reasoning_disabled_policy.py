"""Endpoint-specific reasoning compatibility is applied at the provider wire."""

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from openai import AsyncOpenAI, UnprocessableEntityError
from pydantic import ValidationError

from box_agent.config import Config
from box_agent.llm import LLMClient
from box_agent.retry import RetryConfig
from box_agent.schema import LLMProvider, Message


MODEL = "SenseNova-Flash-Lite-test"


@asynccontextmanager
async def wire_client(*, policy=None, reject=False, model=MODEL, respond=None):
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if respond is not None:
            return respond(payload)
        if reject:
            return httpx.Response(422, json={"error": {
                "message": "reasoning_effort is invalid; the model service could not complete this request",
                "type": "invalid_request_error",
            }})
        choice = {"index": 0, "finish_reason": "stop"}
        body = {"id": "test-response", "created": 1, "model": model, "choices": [choice]}
        if payload.get("stream"):
            choice["delta"] = {"content": "OK"}
            body["object"] = "chat.completion.chunk"
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n")
        choice["message"] = {"role": "assistant", "content": '{"continue": false}'}
        body["object"] = "chat.completion"
        return httpx.Response(200, json=body)

    client = LLMClient(api_key="test", provider=LLMProvider.OPENAI,
                       api_base="https://inference.example/v1", model=model,
                       retry_config=RetryConfig(max_retries=2, initial_delay=0, max_delay=0),
                       reasoning_effort_when_disabled=policy)
    original = client._client.client
    client._client.client = AsyncOpenAI(api_key="test", base_url=client.api_base,
        max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        yield client, requests
    finally:
        await client._client.client.close()
        await original.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("policy,enabled,expected", [
    (None, False, "none"), ("none", False, "none"), ("low", False, "low"), ("low", True, "high"),
])
async def test_endpoint_policy_changes_only_disabled_reasoning_wire(stream, policy, enabled, expected):
    async with wire_client(policy=policy) as (client, requests):
        bound = client.for_model(MODEL + "-child")
        if stream:
            events = [event async for event in bound.generate_stream(
                [Message(role="user", content="hello")], thinking_enabled=enabled)]
            assert events[-1].type == "finish"
        else:
            await bound.generate([Message(role="user", content="hello")], thinking_enabled=enabled)
        assert len(requests) == 1
        assert requests[0]["reasoning_effort"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_deterministic_422_is_not_retried_even_with_transient_words(stream):
    async with wire_client(policy="low", reject=True) as (client, requests):
        with pytest.raises(UnprocessableEntityError):
            if stream:
                async for _ in client.generate_stream([Message(role="user", content="hello")]):
                    pass
            else:
                await client.generate([Message(role="user", content="hello")])
        assert len(requests) == 1


def test_yaml_endpoint_policy_is_validated_and_lite_endpoint_does_not_inherit(tmp_path):
    path = tmp_path / "config.yaml"
    settings = {"api_key": "test", "api_base": "https://inference.example/v1", "provider": "openai",
                "model": MODEL, "reasoning_effort_when_disabled": "low", "lite_llm": {
                    "api_key": "other", "api_base": "https://other.example/v1", "model": MODEL}}
    import yaml
    path.write_text(yaml.safe_dump(settings))
    config = Config.from_yaml(path)
    assert config.llm.reasoning_effort_when_disabled == "low"
    assert config.lite_llm.reasoning_effort_when_disabled is None
    settings["reasoning_effort_when_disabled"] = "medium"
    path.write_text(yaml.safe_dump(settings))
    with pytest.raises(ValidationError, match="reasoning_effort_when_disabled"):
        Config.from_yaml(path)


def test_runtime_factory_forwards_only_explicit_endpoint_policy():
    from box_agent.agent_runtime import build_llm_client
    received = []
    def factory(**kwargs):
        received.append(kwargs)
        return object()
    for policy in (None, "low"):
        build_llm_client(api_key="test", provider=LLMProvider.OPENAI,
            api_base="https://example.invalid", model=MODEL, retry_config=None,
            max_output_tokens=128, auth_file="", timeout=10, client_factory=factory,
            reasoning_effort_when_disabled=policy)
    assert "reasoning_effort_when_disabled" not in received[0]
    assert received[1]["reasoning_effort_when_disabled"] == "low"


@pytest.mark.asyncio
async def test_profile_switch_uses_its_own_policy_without_leaking_fallback(tmp_path, monkeypatch):
    from box_agent.llm.model_profiles import client_for_model_profile, load_model_profile_revision, ModelProfileUnavailable
    profiles = {}
    for name, policy in (("limited", "low"), ("ordinary", None)):
        profiles[name] = {"profileId": name, "profileRevision": name, "provider": "openai",
            "apiBase": f"https://{name}.example/v1", "apiKey": "test", "defaultModel": MODEL,
            **({"reasoningEffortWhenDisabled": policy} if policy else {})}
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"version": 1, "profiles": profiles}))
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(path))
    async with wire_client(policy="low") as (fallback, _):
        for name, expected in (("limited", "low"), ("ordinary", None)):
            client = client_for_model_profile({"profileId": name, "profileRevision": name, "model": MODEL},
                                              fallback_client=fallback)
            try:
                assert client.reasoning_effort_when_disabled == expected
                assert client._client.reasoning_effort_when_disabled == expected
            finally:
                await client._client.client.close()
    profiles["limited"]["reasoningEffortWhenDisabled"] = "invalid"
    path.write_text(json.dumps({"version": 1, "profiles": profiles}))
    with pytest.raises(ModelProfileUnavailable, match="reasoningEffortWhenDisabled"):
        load_model_profile_revision("limited")


@pytest.mark.asyncio
async def test_utility_image_web_and_continuation_judge_share_provider_policy(tmp_path):
    from PIL import Image
    from box_agent.tools.image_inspection_tool import ImageInspectionTool
    from box_agent.mcp_servers.web_extract import WebExtractTool
    from box_agent.turn_continuation import model_says_continue
    image = tmp_path / "image.png"
    Image.new("RGB", (10, 10), "white").save(image)
    async with wire_client(policy="low") as (client, requests):
        result = await ImageInspectionTool(llm=client, workspace_dir=str(tmp_path)).invoke(
            {"image_paths": [str(image)], "instruction": "Inspect the image"})
        assert result.success, result.error
        assert any(block.get("type") == "image_url" for block in requests[0]["messages"][-1]["content"])
        summary, error = await WebExtractTool(llm=client)._summarize(
            "Evidence text", "https://example.invalid", requested_model="", requested_max_output_tokens=None)
        assert summary and error is None
        assert not await model_says_continue(client, user_request="Do the task", candidate_response="Completed")
        assert len(requests) == 3
        assert [r["reasoning_effort"] for r in requests] == ["low", "low", "low"]


@pytest.mark.asyncio
async def test_rejected_judge_request_is_not_a_completed_verdict():
    from box_agent.turn_continuation import TurnContinuationError, model_says_continue

    async with wire_client(policy="low", reject=True) as (client, requests):
        with pytest.raises(TurnContinuationError, match="completion check failed"):
            await model_says_continue(client, user_request="Do the task", candidate_response="Completed")
        assert len(requests) == 1
        assert requests[0]["reasoning_effort"] == "low"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["deepseek-v4", "qwen-test", "gpt-4.1", "gemini-2.5-pro", "glm-5.3"])
async def test_disabled_override_preserves_other_provider_dialects(model):
    async with wire_client(policy=None, model=model) as (before, before_requests):
        await before.generate([Message(role="user", content="hello")])
    async with wire_client(policy="low", model=model) as (after, after_requests):
        await after.generate([Message(role="user", content="hello")])
    assert before_requests == after_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True], ids=["general_loop", "batch_files"])
@pytest.mark.parametrize("policy", [None, "low"])
async def test_direct_agent_child_thinking_reaches_sdk_wire_across_turns(tmp_path, monkeypatch, batch, policy):
    from pathlib import Path
    from box_agent.agent import Agent
    from box_agent.tools.file.read_tool import ReadTool
    from box_agent.tools.sub_agent_tool import SubAgentTool

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    source = tmp_path / "input.txt"
    source.write_text("Revenue is 42.")
    child_requests = []
    expecting_child = False

    def respond(payload):
        nonlocal expecting_child
        choice = {"index": 0, "finish_reason": "stop"}
        message = {"role": "assistant"}
        is_parent = any(t["function"]["name"] == "sub_agent" for t in payload.get("tools", []))
        if expecting_child:
            child_requests.append(payload)
            message["content"] = "child finished"
            expecting_child = False
        elif is_parent and payload["messages"][-1]["role"] == "user":
            arguments = {"task": "Summarize the assigned material"}
            if batch:
                arguments["files"] = [str(source)]
            message["tool_calls"] = [{"index": 0, "id": f"child-{len(child_requests)}",
                "type": "function", "function": {"name": "sub_agent", "arguments": json.dumps(arguments)}}]
            choice["finish_reason"] = "tool_calls"
            expecting_child = True
        else:
            message["content"] = "parent finished" if is_parent else '{"continue": false}'
        body = {"id": "wire-agent", "created": 1, "model": MODEL, "choices": [choice]}
        if payload.get("stream"):
            choice["delta"] = message
            body["object"] = "chat.completion.chunk"
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n")
        choice["message"] = message
        body["object"] = "chat.completion"
        return httpx.Response(200, json=body)

    async with wire_client(policy=policy, respond=respond) as (client, _):
        read = ReadTool(workspace_dir=str(tmp_path))
        child = SubAgentTool(llm=client, parent_tools={"read_file": read}, workspace_dir=str(tmp_path))
        agent = Agent(llm_client=client, system_prompt="Be concise", tools=[read, child],
            workspace_dir=str(tmp_path), max_steps=3)
        for enabled in (True, False):
            agent.thinking_enabled = enabled
            agent.add_user_message("Delegate this summary")
            events = [event async for event in agent.run_events()]
            assert events[-1].final_content == "parent finished"
    assert [r["reasoning_effort"] for r in child_requests] == ["high", policy or "none"]
    assert [bool(r.get("stream")) for r in child_requests] == [not batch, not batch]


@pytest.mark.asyncio
async def test_cli_doctor_preserves_explicit_endpoint_policy(tmp_path, monkeypatch):
    from box_agent import cli
    path = tmp_path / "config.yaml"
    path.write_text("api_key: test\nprovider: openai\nmodel: SenseNova-Flash-test\n"
                    "api_base: https://inference.example/v1\nreasoning_effort_when_disabled: low\n")
    captured = []

    class ProbeClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    async def probe(client):
        from box_agent.schema import LLMResponse
        return LLMResponse(content="OK", finish_reason="stop")

    monkeypatch.setattr(cli, "LLMClient", ProbeClient)
    monkeypatch.setattr(cli, "_probe_llm_api", probe)
    result = await cli._doctor_api_status(Config.from_yaml(path))
    assert result["status"] == "ok", result
    assert captured[0]["reasoning_effort_when_disabled"] == "low"


@pytest.mark.parametrize("set_key", ["reasoning_effort_when_disabled", "llm.reasoning_effort_when_disabled"])
@pytest.mark.parametrize("get_key", ["reasoning_effort_when_disabled", "llm.reasoning_effort_when_disabled"])
def test_cli_config_reports_endpoint_policy_and_validates_updates(tmp_path, monkeypatch, capsys, set_key, get_key):
    from box_agent import cli
    path = tmp_path / "config.yaml"
    path.write_text("api_key: test\nprovider: openai\nmodel: SenseNova-Flash-test\n")
    monkeypatch.setattr(cli.Config, "find_config_file", lambda _name: path)
    assert cli.cmd_config(set_pair=(set_key, "low")) == 0
    capsys.readouterr()
    assert Config.from_yaml(path).llm.reasoning_effort_when_disabled == "low"
    assert cli.cmd_config(get_key=get_key) == 0
    assert capsys.readouterr().out.strip() == "low"
    previous = path.read_text()
    assert cli.cmd_config(set_pair=(set_key, "medium")) == 1
    assert path.read_text() == previous
