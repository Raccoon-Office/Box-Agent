import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp.schema import RequestPermissionResponse

from box_agent.acp.billing import BillingConfirmation
from box_agent.run_control import RunControl
from box_agent.tools.billing import confirmation
from box_agent.tools.image_generation_tool import GenerateImageTool
from box_agent.tools.base import ToolResult


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['approve', 'approve_session', 'reject', 'cancelled'])
@pytest.mark.parametrize('edit', [False, True])
async def test_image_waits_for_host_decision_before_request(tmp_path, monkeypatch, decision, edit):
    waiting = asyncio.Event()
    reply = asyncio.Event()
    requests = []

    async def request_permission(**kwargs):
        requests.append(kwargs)
        waiting.set()
        await reply.wait()
        outcome = (
            {'outcome': 'cancelled'} if decision == 'cancelled'
            else {'outcome': 'selected', 'optionId': decision}
        )
        return RequestPermissionResponse.model_validate({'outcome': outcome})

    conn = SimpleNamespace(request_permission=request_permission)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        endpoint='https://xiaohuanxiong.com/api/web/llm/v2/images/gen',
        api_key='', allow_full_access=True,
    )
    network = AsyncMock(return_value=(b'png', 'image/png'))
    monkeypatch.setattr(tool, '_request_image', network)
    monkeypatch.setattr(tool, '_request_image_edit', network)
    monkeypatch.setattr('box_agent.tools.image_generation_tool.apply_text_watermark', lambda data, *a, **k: (data, {}))
    reference = tmp_path / 'reference.png'
    reference.write_bytes(b'png')
    token = confirmation.set(BillingConfirmation(conn, 'acp-session', 'turn-1', lambda: True, control=RunControl()))
    try:
        task = asyncio.create_task(tool.execute(
            prompt='draw', output_path='out.png',
            reference_images=[str(reference)] if edit else None,
        ))
    finally:
        confirmation.reset(token)
    await asyncio.wait_for(waiting.wait(), 2)
    assert network.await_count == 0
    assert requests[0]['tool_call'].raw_input['turn_id'] == 'turn-1'
    reply.set()
    result = await task
    allowed = decision in {'approve', 'approve_session'}
    assert result.success == allowed
    assert network.await_count == int(allowed)
    assert (tmp_path / 'out.png').exists() == allowed
    assert result.permission_request is None
    if not allowed:
        assert "no image request was sent" in result.error
        assert "call generate_image directly again" in result.error
        assert "request_user_decision" in result.error


@pytest.mark.asyncio
async def test_cancelled_and_inactive_confirmation_do_not_allow_execution(tmp_path, monkeypatch):
    called = asyncio.Event()
    async def request_permission(**kwargs):
        called.set()
        await asyncio.Future()
    callback = BillingConfirmation(
        SimpleNamespace(request_permission=request_permission), 's', 't', lambda: True, control=RunControl(),
    )
    task = asyncio.create_task(callback('image_generation'))
    await called.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    inactive = BillingConfirmation(
        SimpleNamespace(request_permission=AsyncMock()), 's', 't', lambda: False, control=RunControl(),
    )
    assert not await inactive('image_generation')
    inactive.connection.request_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_billing_errors_fail_closed_and_calls_are_not_coalesced():
    conn = SimpleNamespace(request_permission=AsyncMock(side_effect=TimeoutError))
    callback = BillingConfirmation(conn, 's', 't', lambda: True, control=RunControl())
    assert await asyncio.gather(callback('image_generation'), callback('image_generation')) == [False, False]
    assert conn.request_permission.await_count == 2
    calls = conn.request_permission.call_args_list
    assert calls[0].kwargs['tool_call'].tool_call_id != calls[1].kwargs['tool_call'].tool_call_id


@pytest.mark.asyncio
async def test_post_generation_permission_failure_does_not_offer_automatic_regeneration(tmp_path, monkeypatch):
    tool = GenerateImageTool(workspace_dir=str(tmp_path), endpoint='https://images.example/v1/images/generations')
    network = AsyncMock(return_value=(b'png', 'image/png'))
    monkeypatch.setattr(tool, '_request_image', network)
    checks = iter([None, ToolResult(success=False, error='outside workspace', permission_request={'scope': 'filesystem'})])
    monkeypatch.setattr(tool, '_check_write_permission', lambda path: next(checks))
    result = await tool.execute(prompt='draw', output_path='out.png')
    assert not result.success
    assert result.permission_request is None
    assert network.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject", "cancel_without_host_reply"])
async def test_acp_image_permission_waits_and_finishes_in_same_turn_with_full_access(tmp_path, monkeypatch, decision):
    from tests.test_acp import (
        BoxACPAgent, DummyConn, DoneLLM, Config, AgentConfig, ToolsConfig,
        LLMConfig, ImageGenerationConfig, StreamEvent, ToolCall, FunctionCall, acp_params,
    )

    class ImageLLM(DoneLLM):
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    type='finish', finish_reason='tool',
                    tool_calls=[ToolCall(
                        id='image-1', type='function',
                        function=FunctionCall(
                            name='generate_image',
                            arguments={'prompt': 'draw', 'output_path': 'image.png'},
                        ),
                    )],
                )
            else:
                yield StreamEvent(type='text', delta='done')
                yield StreamEvent(type='finish', finish_reason='stop')

    waiting = asyncio.Event()
    reply = asyncio.Event()
    permission_cancelled = asyncio.Event()
    async def permission(**kwargs):
        waiting.set()
        try:
            await reply.wait()
        except asyncio.CancelledError:
            permission_cancelled.set()
            raise
        return RequestPermissionResponse.model_validate({
            "outcome": {"outcome": "selected", "optionId": decision},
        })
    conn = DummyConn()
    conn.request_permission = AsyncMock(side_effect=permission)
    network = AsyncMock(return_value=(b'png', 'image/png'))
    monkeypatch.setattr(GenerateImageTool, '_request_image', network)
    monkeypatch.setattr('box_agent.tools.image_generation_tool.apply_text_watermark', lambda data, *a, **k: (data, {}))
    config = Config(
        llm=LLMConfig(api_key='test'),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False, enable_mcp=False, enable_skills=False),
        image_generation=ImageGenerationConfig(
            endpoint='https://xiaohuanxiong.com/api/web/llm/v2/images/gen',
        ),
    )
    agent = BoxACPAgent(conn, config, ImageLLM(), [], 'system')
    session = await agent.new_session(**acp_params(SimpleNamespace(
        cwd=str(tmp_path), field_meta={'permission_mode': 'full_access'},
    )))
    task = asyncio.create_task(agent.prompt(**acp_params(SimpleNamespace(
        sessionId=session.sessionId, prompt=[{'text': 'draw an image'}],
        field_meta={'turn_id': 'test-turn'},
    ))))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not task.done()
        network.assert_not_awaited()
        assert conn.request_permission.await_count == 1
        raw = conn.request_permission.call_args.kwargs['tool_call'].raw_input
        assert set(raw) == {'scope', 'requested_scope', 'turn_id', 'invocation_id'}
        assert raw['scope'] == 'billing'
        assert raw['turn_id'] == 'test-turn'
        if decision == "cancel_without_host_reply":
            await agent.cancel(session.sessionId)
        else:
            reply.set()
        result = await asyncio.wait_for(task, 3)
        if decision == "cancel_without_host_reply":
            assert result.stopReason == "cancelled"
            assert permission_cancelled.is_set()
            assert not reply.is_set()
        else:
            assert result.stopReason == 'end_turn'
            assert result.field_meta['runStatus'] == 'completed'
        assert network.await_count == int(decision == 'approve')
        assert (tmp_path / 'image.png').exists() == (decision == 'approve')
        assert confirmation.get() is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent._sessions[session.sessionId].aclose()


@pytest.mark.asyncio
async def test_custom_image_service_keeps_its_own_billing_policy(tmp_path, monkeypatch):
    tool = GenerateImageTool(workspace_dir=str(tmp_path), endpoint="https://images.example/v1/images/generations", api_key="custom")
    callback = AsyncMock(return_value=False)
    network = AsyncMock(return_value=(b"png", "image/png"))
    monkeypatch.setattr(tool, "_request_image", network)
    monkeypatch.setattr("box_agent.tools.image_generation_tool.apply_text_watermark", lambda data, *a, **k: (data, {}))
    token = confirmation.set(callback)
    try:
        result = await tool.execute(prompt="draw", output_path="out.png")
    finally:
        confirmation.reset(token)
    assert result.success
    callback.assert_not_awaited()
    network.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_new_image_attempt_checks_runtime_approval_again_after_denial(tmp_path, monkeypatch, edit):
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        endpoint="https://xiaohuanxiong.com/api/web/llm/v2/images/gen", api_key="",
    )
    consent = AsyncMock(side_effect=[False, True])
    network = AsyncMock(return_value=(b"png", "image/png"))
    monkeypatch.setattr(tool, "_request_image", network)
    monkeypatch.setattr(tool, "_request_image_edit", network)
    monkeypatch.setattr(
        "box_agent.tools.image_generation_tool.apply_text_watermark",
        lambda data, *a, **k: (data, {}),
    )
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"png")
    arguments = dict(
        prompt="draw", output_path="out.png",
        reference_images=[str(reference)] if edit else None,
    )
    token = confirmation.set(consent)
    try:
        first = await tool.execute(**arguments)
        assert not first.success
        network.assert_not_awaited()
        second = await tool.execute(**arguments)
        assert second.success
        network.assert_awaited_once()
        assert consent.await_count == 2
    finally:
        confirmation.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_permission_engine", [False, True])
async def test_image_edit_rechecks_reference_permission_after_billing_approval(
    tmp_path, monkeypatch, use_permission_engine,
):
    from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reference = workspace / "reference.png"
    reference.write_bytes(b"original image")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside image")
    replacement = workspace / "replacement.png"
    try:
        replacement.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks are unavailable")
    engine = PermissionEngine(
        CapabilityPolicy(session_workspace_root=str(workspace)), workspace,
    ) if use_permission_engine else None
    tool = GenerateImageTool(
        workspace_dir=str(workspace), allow_full_access=False,
        permission_engine=engine,
        endpoint="https://xiaohuanxiong.com/api/web/llm/v2/images/gen", api_key="",
    )
    headers = AsyncMock(return_value=({}, None))
    network = AsyncMock(side_effect=AssertionError("Unexpected image request"))
    monkeypatch.setattr(tool, "_request_headers", headers)
    monkeypatch.setattr(tool, "_send_with_hosted_auth_retry", network)

    async def approve(capability):
        assert capability == "image_generation"
        replacement.replace(reference)
        return True

    token = confirmation.set(approve)
    try:
        result = await tool.execute(
            prompt="edit", output_path="output.png", reference_images=[str(reference)],
        )
    finally:
        confirmation.reset(token)
    assert not result.success
    assert "outside" in result.error.lower()
    headers.assert_not_awaited()
    network.assert_not_awaited()
    assert not (workspace / "output.png").exists()


@pytest.mark.asyncio
async def test_obsolete_image_resume_metadata_does_not_execute_tools(tmp_path, monkeypatch):
    from tests.test_acp import (
        BoxACPAgent, DummyConn, DoneLLM, Config, AgentConfig, ToolsConfig,
        LLMConfig, ImageGenerationConfig, acp_params,
    )

    conn = DummyConn()
    conn.request_permission = AsyncMock(return_value=RequestPermissionResponse.model_validate({
        "outcome": {"outcome": "selected", "optionId": "approve"},
    }))
    network = AsyncMock(return_value=(b"png", "image/png"))
    monkeypatch.setattr(GenerateImageTool, "_request_image", network)
    config = Config(
        llm=LLMConfig(api_key="test"), agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False, enable_mcp=False, enable_skills=False),
        image_generation=ImageGenerationConfig(endpoint="https://xiaohuanxiong.com/images/gen"),
    )
    agent = BoxACPAgent(conn, config, DoneLLM(), [], "system")
    session = await agent.new_session(**acp_params(SimpleNamespace(
        cwd=str(tmp_path), field_meta={"permission_mode": "full_access"},
    )))
    try:
        await agent.prompt(**acp_params(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{"text": "hello"}],
            field_meta={"turn_id": "ordinary", "billing_resume": {"operation": {
                "tool": "generate_image", "arguments": {"prompt": "obsolete", "output_path": "obsolete.png"},
            }}},
        )))
        conn.request_permission.assert_not_awaited()
        network.assert_not_awaited()
        assert not (tmp_path / "obsolete.png").exists()
    finally:
        await agent._sessions[session.sessionId].aclose()
