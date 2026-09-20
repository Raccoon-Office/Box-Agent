"""Provider-boundary regressions for hosted credential recovery."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from box_agent.auth import HostedAuthRequiredError, HostedAuthRefreshError, refresh_hosted_auth_token_if_needed
from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.llm.openai_client import OpenAIClient
from box_agent.retry import RetryConfig
from box_agent.schema import Message


class Rejected(Exception):
    status_code = 401


@pytest.fixture
def auth_file(tmp_path):
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps({'access_token': 'old', 'refresh_token': 'refresh'}))
    return path


def client_args(auth_file):
    return dict(api_key='box-agent-auth-json', api_base='https://xiaohuanxiong.com/api/web/llm/v2',
                auth_file=str(auth_file), retry_config=RetryConfig(enabled=False))


@pytest.mark.asyncio
@pytest.mark.parametrize('second_reject', [False, True])
async def test_anthropic_retries_actual_stream_entry(auth_file, monkeypatch, second_reject):
    client = AnthropicClient(**client_args(auth_file))
    sdk = client.client
    refresh = AsyncMock()
    monkeypatch.setattr('box_agent.llm.base.refresh_hosted_auth_token_if_needed', refresh)
    entries = []
    exits = []

    class Stream:
        async def __aenter__(self):
            entries.append(1)
            if len(entries) == 1 or second_reject:
                raise Rejected()
            return self

        async def __aexit__(self, *args):
            exits.append(1)

        def __aiter__(self):
            return self.events()

        async def events(self):
            yield SimpleNamespace(type='message_stop')

    client.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kwargs: Stream()))
    try:
        if second_reject:
            with pytest.raises(HostedAuthRequiredError):
                _ = [event async for event in client.generate_stream([Message(role='user', content='hi')])]
        else:
            events = [event async for event in client.generate_stream([Message(role='user', content='hi')])]
            assert any(event.type == 'finish' for event in events)
            assert exits == [1]
        assert len(entries) == 2
        assert refresh.await_count == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('overrides', [{'api_base': 'https://example.invalid'}, {'auth_token': 'explicit'}, {'api_key': 'real-key'}])
async def test_non_file_credentials_preserve_provider_error(auth_file, monkeypatch, overrides):
    args = client_args(auth_file) | overrides
    client = OpenAIClient(**args)
    refresh = AsyncMock()
    monkeypatch.setattr('box_agent.llm.base.refresh_hosted_auth_token_if_needed', refresh)
    operation = AsyncMock(side_effect=Rejected())
    try:
        with pytest.raises(Rejected):
            await client._call_with_hosted_auth_retry(operation)
        refresh.assert_not_awaited()
        assert operation.await_count == 1
    finally:
        await client.client.close()


@pytest.mark.asyncio
async def test_concurrent_rejections_refresh_once_and_accept_opaque_token(auth_file):
    calls = []
    async def handle(request):
        calls.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, json={'data': {'access_token': 'new-opaque', 'refresh_token': 'rotated'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        results = await asyncio.gather(*[
            refresh_hosted_auth_token_if_needed('https://xiaohuanxiong.com', auth_file,
                force=True, rejected_token='old', http_client=http)
            for _ in range(3)
        ])
    assert results == ['new-opaque'] * 3
    assert len(calls) == 1
    assert json.loads(auth_file.read_text())['refresh_token'] == 'rotated'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', '503', '401'])
async def test_forced_refresh_preserves_transient_failure_type(auth_file, failure):
    def handle(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        return httpx.Response(int(failure))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(HostedAuthRefreshError) as result:
            await refresh_hosted_auth_token_if_needed('https://xiaohuanxiong.com', auth_file,
                force=True, http_client=http)
    assert isinstance(result.value, HostedAuthRequiredError) == (failure == '401')
    assert json.loads(auth_file.read_text())['access_token'] == 'old'


@pytest.mark.asyncio
async def test_openai_business_rejection_retries_same_output_limit(auth_file, monkeypatch):
    client = OpenAIClient(**client_args(auth_file))
    refresh = AsyncMock()
    monkeypatch.setattr('box_agent.llm.base.refresh_hosted_auth_token_if_needed', refresh)
    from unittest.mock import Mock
    consume = Mock(side_effect=[12000, 1000])
    monkeypatch.setattr(client, '_consume_effective_max_tokens', consume)
    responses = [SimpleNamespace(code=200003), SimpleNamespace(code=None)]
    calls = []
    async def create(**params):
        calls.append(params)
        return SimpleNamespace(parse=lambda: responses.pop(0))
    monkeypatch.setattr(client.client.chat.completions, 'with_raw_response', SimpleNamespace(create=create))
    try:
        await client._make_api_request([{'role': 'user', 'content': 'hi'}])
        assert refresh.await_count == 1
        assert [call['max_tokens'] for call in calls] == [12000, 12000]
        assert consume.call_count == 1
    finally:
        await client.client.close()


@pytest.mark.asyncio
async def test_anthropic_does_not_replay_after_content(auth_file, monkeypatch):
    client = AnthropicClient(**client_args(auth_file))
    sdk = client.client
    refresh = AsyncMock()
    monkeypatch.setattr('box_agent.llm.base.refresh_hosted_auth_token_if_needed', refresh)
    exits = []

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            exits.append(1)

        def __aiter__(self):
            return self.events()

        async def events(self):
            yield SimpleNamespace(type='content_block_delta', delta=SimpleNamespace(type='text_delta', text='hello'))
            raise Rejected()

    client.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kwargs: Stream()))
    events = []
    try:
        with pytest.raises(Rejected):
            async for event in client.generate_stream([Message(role='user', content='hi')]):
                events.append(event)
        assert any(event.type == 'text' for event in events)
        refresh.assert_not_awaited()
        assert exits == [1]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_http_200_business_error_from_openai_sdk_recovers(auth_file, monkeypatch):
    client = OpenAIClient(**client_args(auth_file))
    sdk = client.client
    refresh = AsyncMock()
    monkeypatch.setattr('box_agent.llm.base.refresh_hosted_auth_token_if_needed', refresh)
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={'code': 200003, 'message': 'authorization_verify_error'})
        return httpx.Response(200, json={
            'id': 'completion', 'object': 'chat.completion', 'created': 1, 'model': 'test',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
        })
    import openai
    client.client = openai.AsyncOpenAI(api_key='placeholder', base_url=client.api_base,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), max_retries=0)
    try:
        response = await client._make_api_request([{'role': 'user', 'content': 'hi'}])
        assert response.choices[0].message.content == 'ok'
        assert len(requests) == 2
        assert refresh.await_count == 1
    finally:
        await client.client.close()
        await sdk.close()


@pytest.mark.asyncio
async def test_anthropic_stream_entry_timeout_keeps_transport_retry(auth_file, monkeypatch):
    args = client_args(auth_file)
    args['retry_config'] = RetryConfig(enabled=True, max_retries=1, initial_delay=0)
    client = AnthropicClient(**args)
    sdk = client.client
    refresh = AsyncMock()
    monkeypatch.setattr('box_agent.llm.base.refresh_hosted_auth_token_if_needed', refresh)
    entries = []

    class Stream:
        async def __aenter__(self):
            entries.append(1)
            if len(entries) == 1:
                raise httpx.ReadTimeout('temporary timeout')
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self.events()

        async def events(self):
            yield SimpleNamespace(type='message_stop')

    client.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kwargs: Stream()))
    try:
        events = [event async for event in client.generate_stream([Message(role='user', content='hi')])]
        assert any(event.type == 'finish' for event in events)
        assert len(entries) == 2
        refresh.assert_not_awaited()
    finally:
        await sdk.close()
