import asyncio
from types import SimpleNamespace

import httpx
import pytest
from acp.schema import RequestPermissionResponse

from box_agent.llm.base import LLMClientBase
from box_agent.llm.request_context import current_request_headers, scoped_request_context
from box_agent.tools.image_generation_tool import GenerateImageTool
from tests.test_image_generation_tool import PNG_BYTES, patch_async_client


@pytest.mark.asyncio
@pytest.mark.parametrize('edit', [False, True])
async def test_concurrent_image_calls_keep_their_own_chat_headers(tmp_path, monkeypatch, edit):
    seen = []

    async def handler(request):
        await asyncio.sleep(0)
        seen.append(request)
        return httpx.Response(200, content=PNG_BYTES, headers={'Content-Type': 'image/png'})

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        endpoint='https://xiaohuanxiong.com/api/web/llm/v2/images/gen',
        api_key='fixture-key',
    )
    reference = tmp_path / 'ref.png'
    reference.write_bytes(PNG_BYTES)

    async def call(session_id, turn_id, title):
        with scoped_request_context(session_id=session_id, turn_id=turn_id, title=title):
            await asyncio.sleep(0)
            args = dict(prompt='draw', size='1024x1024', style=None, negative_prompt=None)
            if edit:
                await tool._request_image_edit(**args, reference_paths=[reference])
            else:
                await tool._request_image(**args)

    await asyncio.gather(call('session-a', 'turn-a', '生成苹果'), call('session-b', 'turn-b', '绘制梨子'))
    assert len(seen) == 2
    expected = {'session-a': ('turn-a', '生成苹果'), 'session-b': ('turn-b', '绘制梨子')}
    for request in seen:
        turn, title = expected[request.headers['X-RACCOON-Session-ID']]
        actual = {key.lower(): value for key, value in request.headers.raw}
        chat = LLMClientBase._agent_headers(request.headers['X-RACCOON-Session-ID'], turn, title)
        for key, value in chat.items():
            assert actual[key.lower().encode()] == (value.encode() if isinstance(value, str) else value)
        assert request.url.path.endswith('/images/edits' if edit else '/images/gen')
    assert current_request_headers() == {}


@pytest.mark.asyncio
async def test_custom_image_endpoint_receives_conversation_headers(tmp_path):
    tool = GenerateImageTool(workspace_dir=str(tmp_path), endpoint='https://provider.example/images/gen', api_key='key')
    with scoped_request_context(session_id='private-session', turn_id='private-turn', title='private-title'):
        headers, auth = await tool._request_headers()
        assert auth is None
        assert headers == {
            'Accept': 'application/json, image/*', 'Authorization': 'Bearer key',
            'X-RACCOON-Session-ID': 'private-session',
            'X-RACCOON-Turn-ID': 'private-turn',
            'X-RACCOON-Title': 'private-title',
        }


@pytest.mark.asyncio
async def test_acp_image_headers_match_the_chat_call_business_context(tmp_path, monkeypatch):
    from tests.test_acp import (
        BoxACPAgent, DummyConn, DoneLLM, Config, AgentConfig, ToolsConfig, LLMConfig,
        ImageGenerationConfig, StreamEvent, ToolCall, FunctionCall, acp_params,
    )
    text_headers = []
    image_headers = []

    class ImageLLM(DoneLLM):
        async def generate_stream(self, messages, tools=None, **kwargs):
            text_headers.append(LLMClientBase._agent_headers(
                kwargs.get('session_id', ''), kwargs.get('turn_id', ''), kwargs.get('title', ''),
            ))
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type='finish', finish_reason='tool', tool_calls=[ToolCall(
                    id='image', type='function', function=FunctionCall(
                        name='generate_image', arguments={'prompt': 'draw', 'output_path': 'image.png'},
                    ),
                )])
            else:
                yield StreamEvent(type='text', delta='done')
                yield StreamEvent(type='finish', finish_reason='stop')

    def handler(request):
        image_headers.append(request.headers)
        return httpx.Response(200, content=PNG_BYTES, headers={'Content-Type': 'image/png'})

    patch_async_client(monkeypatch, handler)
    async def approve(**kwargs):
        return RequestPermissionResponse.model_validate({'outcome': {'outcome': 'selected', 'optionId': 'approve'}})
    conn = DummyConn()
    conn.request_permission = approve
    auth_file = tmp_path / 'auth.json'
    auth_file.write_text('{"access_token":"fixture-token"}')
    config = Config(
        llm=LLMConfig(api_key='fixture'),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False, enable_mcp=False, enable_skills=False),
        image_generation=ImageGenerationConfig(
            endpoint='https://xiaohuanxiong.com/api/web/llm/v2/images/gen', auth_file=str(auth_file),
        ),
    )
    agent = BoxACPAgent(conn, config, ImageLLM(), [], 'system')
    session = await agent.new_session(**acp_params(SimpleNamespace(
        cwd=str(tmp_path), field_meta={'session_id': 'business-session', 'permission_mode': 'full_access'},
    )))
    try:
        await agent.prompt(**acp_params(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{'text': 'draw an apple'}],
            field_meta={'turn_id': 'business-turn', 'title': '帮我生成一个苹果的图片'},
        )))
        assert len(image_headers) == 1
        assert text_headers[0] == LLMClientBase._agent_headers(
            'business-session', 'business-turn', '帮我生成一个苹果的图片',
        )
        raw = {key.lower(): value for key, value in image_headers[0].raw}
        for key, value in text_headers[0].items():
            assert raw[key.lower().encode()] == (value.encode() if isinstance(value, str) else value)
        assert current_request_headers() == {}
    finally:
        await agent._sessions[session.sessionId].aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('edit', [False, True])
async def test_redirect_to_external_image_host_preserves_conversation_headers(tmp_path, monkeypatch, edit):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == 'xiaohuanxiong.com':
            return httpx.Response(302, headers={'Location': 'https://cdn.example/result.png'})
        return httpx.Response(200, content=PNG_BYTES, headers={'Content-Type': 'image/png'})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handler),
    ))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        endpoint='https://xiaohuanxiong.com/api/web/llm/v2/images/gen', api_key='fixture',
    )
    reference = tmp_path / 'ref.png'
    reference.write_bytes(PNG_BYTES)
    with scoped_request_context(session_id='s', turn_id='t', title='Private title'):
        args = dict(prompt='draw', size='1024x1024', style=None, negative_prompt=None)
        if edit:
            await tool._request_image_edit(**args, reference_paths=[reference])
        else:
            await tool._request_image(**args)
    assert len(requests) == 2
    assert requests[0].headers['Authorization'] == 'Bearer fixture'
    assert 'Authorization' not in requests[1].headers
    for request in requests:
        assert request.headers['X-RACCOON-Session-ID'] == 's'
        assert request.headers['X-RACCOON-Turn-ID'] == 't'
        assert request.headers['X-RACCOON-Title'] == 'Private title'


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [
    "https://tenant.example/office/images/edits",
    "https://tenant.example/other/images/edits",
    "https://other.example/office/images/edits",
])
async def test_private_image_context_headers_are_available_for_any_endpoint(tmp_path, monkeypatch, endpoint):
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path), endpoint="https://tenant.example/office/images/gen",
        api_key="fixture-key",
    )
    with scoped_request_context(session_id="private-session", turn_id="private-turn", title="Private title"):
        headers, _ = await tool._request_headers(endpoint)
    context_headers = {key: value for key, value in headers.items() if key.startswith("X-RACCOON-")}
    assert context_headers == LLMClientBase._agent_headers("private-session", "private-turn", "Private title")
