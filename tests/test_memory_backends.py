"""记忆后端的协议、身份隔离和完整用户轮次回归。"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from box_agent.agent_service import AgentService
from box_agent.agent_session import AgentSession
from box_agent.api import RunRequest
from box_agent.config import AgentConfig, Config, ExternalMemoryConfig, MemoryHttpOperation
from box_agent.events import DoneEvent, StopReason
from box_agent.memory import (
    MemoryBackendError, MemoryManager, MemoryRuntime,
    create_memory_backend, memory_user_turn,
)
from box_agent.session_context import HostBindings, SessionOptions
from box_agent.tools.memory_tool import create_memory_tools
from tests.test_agent_session import session_config
from tests.test_session_plugins import RecordingLLM


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """测试不得访问用户的真实记忆目录。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


@pytest.fixture
def memory_http(monkeypatch):
    """通过真实 httpx 编解码验证线协议，同时阻断外部服务访问。"""
    server = SimpleNamespace(calls=[], profile="用户偏好中文", handler=None)

    def respond(request):
        payload = json.loads(request.content) if request.content else dict(request.url.params)
        server.calls.append((request.url.path, payload))
        if server.handler is not None:
            response = server.handler(request, payload)
            if response is not None:
                return response
        if request.url.path.endswith("/files/read"):
            data = {"content": server.profile if payload["path"].endswith("user.md") else "长期项目资料", "exists": True}
        elif request.url.path.endswith("/resource_search"):
            data = {"results": [{"resource_type": "qa_chunk", "content": "历史问答", "path": "memory://qa/example"}]}
        else:
            data = {"source_id": "source-1"}
        return httpx.Response(200, json={"ok": True, "data": data})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    return server


def backend_settings(tmp_path, **kwargs):
    """构造临时隔离的 MemSense 配置。"""
    return AgentConfig(memory_dir=str(tmp_path / "memory"), memory_backend_type="memsense",
                       memory_external=ExternalMemoryConfig(base_url="http://memory.test"), **kwargs)


async def managed_session(tmp_path, settings=None):
    """使用现有插件装配和真实 AgentService，避免只测试保存辅助函数。"""
    config = session_config(tmp_path)
    config.agent = settings or backend_settings(tmp_path)
    config.agent.enable_memory_extraction = False
    backend = create_memory_backend(config.agent)
    llm = RecordingLLM("最终回答")
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=llm, system_prompt="系统提示", tools=create_memory_tools(backend), memory_manager=backend,
    ), options=SessionOptions(workspace_dir=tmp_path))
    return session, llm


async def run(session, text, run_id="run-1"):
    handle = await AgentService().start(RunRequest(run_id, "session-1", text), session=session,
                                        options=session.build_run_options(logger=None, turn_id=run_id))
    async with handle:
        _ = [item async for item in handle.events()]
        return await handle.result()


def test_local_default_reuses_existing_storage_and_other_identities_are_isolated(tmp_path):
    root = tmp_path / "memory"
    old = MemoryManager(str(root))
    old.write_core("旧记忆")
    default = create_memory_backend(AgentConfig(memory_dir=str(root)))
    assert default.read_core() == "旧记忆"
    assert default.memory_dir == root
    managers = [create_memory_backend(AgentConfig(memory_dir=str(root), memory_tenant_id=tenant,
                                                 memory_user_id=user))
                for tenant, user in [("tenant-a", "u"), ("tenant-b", "u"), ("tenant-a", "../u"), ("tenant-a", "u2")]]
    for index, manager in enumerate(managers):
        assert manager.read_core() == ""
        assert manager.memory_dir.is_relative_to(root)
        manager.write_core(str(index))
    assert len({manager.memory_dir for manager in managers}) == 4
    assert [manager.read_core() for manager in managers] == ["0", "1", "2", "3"]
    assert default.read_core() == "旧记忆"


def test_yaml_loads_backend_configuration_and_defaults_empty_identity(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("api_key: test\nmemory_backend_type: memsense\nmemory_tenant_id: null\n"
                    "memory_user_id: ''\nmemory_external:\n  base_url: http://memory.test\n", encoding="utf-8")
    config = Config.from_yaml(path)
    assert config.agent.memory_backend_type == "memsense"
    assert config.agent.memory_tenant_id == config.agent.memory_user_id == "default"
    assert config.agent.memory_external.base_url == "http://memory.test"
    with pytest.raises(ValueError):
        AgentConfig(memory_backend_type="unknown")


@pytest.mark.asyncio
async def test_memsense_read_search_and_save_use_distinct_protocols(tmp_path, memory_http):
    backend = create_memory_backend(backend_settings(tmp_path, memory_tenant_id="tenant", memory_user_id="user"))
    tools = {tool.name: tool for tool in create_memory_tools(backend)}
    assert "memory_write" not in tools and "memory_edit" not in tools
    assert "memory_write_correction" in tools
    assert (await tools["memory_read"].execute(path="memory://user.md")).success
    assert (await tools["memory_search"].execute(query="历史项目", limit=4)).success
    session_id = "674d479c-940f-47f5-b314-5eecb8495d17"
    await backend.save_turn("${tenant_id}", "最终答复", session_id=session_id, turn_id="turn", timestamp=123)
    assert memory_http.calls == [
        ("/v1/memory/files/read", {"tenant_id": "tenant", "user_id": "user", "path": "memory://user.md"}),
        ("/v1/memory/resource_search", {"tenant_id": "tenant", "user_id": "user", "query": "历史项目",
                                       "resource_types": ["date_session_title", "qa_chunk"], "filters": {},
                                       "top_k": 4, "mode": "hybrid", "scope": "user"}),
        ("/v1/memory/save", {"tenant_id": "tenant", "user_id": "user", "scope": "user",
                             "session_id": session_id, "agent_id": "box-agent", "source": "box_agent_auto",
                             "type_hint": "qa_chunk", "timestamp": 123,
                             "content": {"user": "${tenant_id}", "assistant": "最终答复"}}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["cli-0123456789abcdef", "sess-1-badc0ffe", "自定义/会话"])
async def test_memsense_session_files_receive_stable_uuid_for_host_session_ids(tmp_path, memory_http, session_id):
    """MemSense 文件读取只接受 UUID，映射必须跨后端实例稳定且按身份隔离。"""
    settings = backend_settings(tmp_path)
    for _ in range(2):
        backend = create_memory_backend(settings)
        await backend.save_turn("问题", "回答", session_id=session_id, turn_id="turn", timestamp=123)
    await backend.save_turn("问题", "回答", session_id=session_id + "-other", turn_id="turn", timestamp=123)
    other = create_memory_backend(settings.model_copy(update={"memory_user_id": "other-user"}))
    await other.save_turn("问题", "回答", session_id=session_id, turn_id="turn", timestamp=123)
    identifiers = [body["session_id"] for path, body in memory_http.calls if path.endswith("/save")]
    assert all(str(uuid.UUID(value)) == value for value in identifiers)
    assert identifiers[0] == identifiers[1]
    assert len(set(identifiers)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_type", ["generic", "mem0", "memu"])
async def test_generic_and_placeholder_types_do_not_force_memsense_protocol(tmp_path, memory_http, backend_type):
    memory_http.handler = lambda request, payload: httpx.Response(200, json={"items": ["通用结果"]})
    settings = AgentConfig(memory_backend_type=backend_type, memory_dir=str(tmp_path / "mem"),
        memory_external=ExternalMemoryConfig(base_url="http://memory.test", search=MemoryHttpOperation(
            path="/lookup", method="GET", request={"q": "${query}", "n": "${limit}"}, response_path="items"),
            save=MemoryHttpOperation(path="/remember", request={"messages": "${messages}"})))
    backend = create_memory_backend(settings)
    assert backend.capabilities == {"search", "save"}
    assert "memory_read" not in {tool.name for tool in create_memory_tools(backend)}
    assert await backend.load_context(query="任意") == ""
    assert await backend.search_memory("问题", 2) == ["通用结果"]
    await backend.save_turn("用户", "答复", session_id="s", turn_id="t")
    assert memory_http.calls == [
        ("/lookup", {"q": "问题", "n": "2"}),
        ("/remember", {"messages": [{"role": "user", "content": "用户"}, {"role": "assistant", "content": "答复"}]}),
    ]


@pytest.mark.asyncio
async def test_core_files_refresh_once_per_user_turn_and_do_not_accumulate(tmp_path, memory_http):
    session, llm = await managed_session(tmp_path)
    try:
        await run(session, "问题一")
        memory_http.profile = "偏好已更新"
        await run(session, "问题二", "run-2")
    finally:
        await session.aclose()
    assert "用户偏好中文" in llm.requests[0]["messages"][0].content
    prompt = llm.requests[1]["messages"][0].content
    assert "偏好已更新" in prompt and "用户偏好中文" not in prompt
    assert prompt.count("--- EXTERNAL MEMORY START ---") == 1
    assert len([call for call in memory_http.calls if call[0].endswith("/files/read")]) == 4
    saves = [body for path, body in memory_http.calls if path.endswith("/save")]
    assert [body["content"]["user"] for body in saves] == ["问题一", "问题二"]
    assert all(body["content"]["assistant"] == "最终回答" for body in saves)


@pytest.mark.asyncio
async def test_outer_user_turn_saves_only_final_continuation_answer(tmp_path, memory_http):
    session, llm = await managed_session(tmp_path)
    outer_session_id = "da973cc2-86e7-412b-9f9d-44d77b70aabb"
    try:
        async with memory_user_turn(session, user_text="真实用户请求", session_id=outer_session_id, turn_id="outer-turn"):
            llm.answer = "阶段进度"
            await run(session, "真实用户请求", "inner-1")
            llm.answer = "最后的交付"
            await run(session, "自动续跑的内部指令", "inner-2")
    finally:
        await session.aclose()
    assert len([call for call in memory_http.calls if call[0].endswith("/files/read")]) == 2
    saves = [body for path, body in memory_http.calls if path.endswith("/save")]
    assert len(saves) == 1
    assert saves[0]["session_id"] == outer_session_id
    assert saves[0]["content"] == {"user": "真实用户请求", "assistant": "最后的交付"}


@pytest.mark.asyncio
async def test_save_failure_has_bounded_retries_and_does_not_change_main_result(tmp_path, memory_http, caplog):
    secret = "不应写到日志的 QA 和密钥"
    memory_http.handler = lambda request, payload: (httpx.Response(503, text=secret)
                                                    if request.url.path.endswith("/save") else None)
    session, _ = await managed_session(tmp_path)
    try:
        result = await run(session, secret)
        assert result.status.value == "completed" and result.final_content == "最终回答"
    finally:
        await session.aclose()
    assert len([call for call in memory_http.calls if call[0].endswith("/save")]) == 2
    assert "operation=save" in caplog.text and "status=503" in caplog.text and "abandoned" in caplog.text
    assert "tenant=default user=default session=session-1 turn=run-1" in caplog.text
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_read_failure_clears_stale_context_and_preserves_main_task(tmp_path, memory_http, caplog):
    session, llm = await managed_session(tmp_path)
    try:
        await run(session, "第一次")
        memory_http.handler = lambda request, payload: (httpx.Response(400, json={"ok": False})
                                                        if request.url.path.endswith("/files/read") else None)
        result = await run(session, "第二次", "run-2")
        assert result.final_content == "最终回答"
        assert "用户偏好中文" not in llm.requests[-1]["messages"][0].content
        assert "--- EXTERNAL MEMORY START ---" not in llm.requests[-1]["messages"][0].content
    finally:
        await session.aclose()
    assert "operation=read" in caplog.text and "status=400" in caplog.text


@pytest.mark.asyncio
async def test_failed_cleanup_after_done_does_not_save_successful_looking_qa(tmp_path, memory_http):
    session, _ = await managed_session(tmp_path)

    async def failed_stream(**kwargs):
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="看起来成功")
        raise RuntimeError("清理失败")

    session.run_events = failed_stream
    try:
        with pytest.raises(RuntimeError, match="清理失败"):
            await run(session, "用户请求")
    finally:
        await session.aclose()
    assert not [call for call in memory_http.calls if call[0].endswith("/save")]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [StopReason.CANCELLED, StopReason.ERROR, StopReason.WAITING_FOR_USER])
async def test_incomplete_runs_do_not_save_qa(tmp_path, memory_http, reason):
    session, _ = await managed_session(tmp_path)

    async def incomplete_stream(**kwargs):
        yield DoneEvent(stop_reason=reason, final_content="未完成片段")

    session.run_events = incomplete_stream
    try:
        await run(session, "请求")
    finally:
        await session.aclose()
    assert not [call for call in memory_http.calls if call[0].endswith("/save")]


@pytest.mark.asyncio
async def test_session_identity_rebinds_borrowed_memory_tools(tmp_path, memory_http):
    config = session_config(tmp_path)
    config.agent = backend_settings(tmp_path, memory_tenant_id="tenant")
    process_backend = create_memory_backend(config.agent)
    sessions = []
    try:
        for user in ("alice", "bob"):
            session = await AgentSession.open(config=config, host=HostBindings(
                llm_client=RecordingLLM(), system_prompt="system", base_tools=create_memory_tools(process_backend),
                memory_manager=process_backend, output=lambda _: None),
                options=SessionOptions(profile="acp", workspace_dir=tmp_path, memory_user_id=user))
            sessions.append(session)
            assert session.memory_extractor is None
            assert "memory_write" not in session.agent.tools
            await session.agent.tools["memory_search"].execute(query="test")
        assert config.agent.memory_user_id == "default"
        searches = [body for path, body in memory_http.calls if path.endswith("/resource_search")]
        assert [body["user_id"] for body in searches] == ["alice", "bob"]
        assert all(body["tenant_id"] == "tenant" for body in searches)
        assert sessions[0].memory_manager.corrections.memory_dir != sessions[1].memory_manager.corrections.memory_dir
        assert sessions[0].memory_runtime is not sessions[1].memory_runtime
    finally:
        for session in sessions:
            await session.aclose()


@pytest.mark.asyncio
async def test_disabled_memory_removes_borrowed_tools_and_performs_no_http(tmp_path, memory_http):
    config = session_config(tmp_path)
    config.agent = backend_settings(tmp_path, enable_memory=False)
    backend = create_memory_backend(config.agent)
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=RecordingLLM(), system_prompt="system", base_tools=create_memory_tools(backend),
        memory_manager=backend, output=lambda _: None), options=SessionOptions(profile="acp", workspace_dir=tmp_path))
    try:
        assert session.memory_runtime is None
        assert not any(name.startswith("memory_") for name in session.agent.tools)
        await run(session, "无需记忆")
    finally:
        await session.aclose()
    assert memory_http.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "search", "save"])
async def test_malformed_external_response_reports_failure(tmp_path, memory_http, caplog, operation):
    memory_http.handler = lambda request, payload: httpx.Response(200, json={"ok": True, "data": "错误类型"})
    backend = create_memory_backend(backend_settings(tmp_path))
    if operation == "save":
        with pytest.raises(MemoryBackendError):
            await backend.save_turn("问题", "回答", session_id="s", turn_id="t", timestamp=1)
    else:
        tool = next(tool for tool in create_memory_tools(backend) if tool.name == "memory_" + operation)
        result = await tool.execute(**({"query": "项目"} if operation == "search" else {"path": "memory://user.md"}))
        assert not result.success
    expected = "invalid_file_response" if operation == "read" else f"invalid_{operation}_response"
    assert expected in caplog.text


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_save_with_diagnostic(tmp_path, caplog):
    backend = create_memory_backend(backend_settings(tmp_path))
    backend.config.shutdown_timeout_seconds = 0.01
    runtime = MemoryRuntime(backend)

    async def unfinished_result(**kwargs):
        await asyncio.Event().wait()

    from box_agent.memory import _MemoryTurn

    runtime.schedule(_MemoryTurn("用户", "session", "turn", 1, SimpleNamespace(result=unfinished_result)))
    await runtime.aclose()
    assert not runtime.tasks
    assert "abandoned_shutdown" in caplog.text


@pytest.mark.asyncio
async def test_acp_autopilot_saves_latest_user_request_with_session_identity(tmp_path, memory_http, capsys):
    from box_agent.acp import BoxACPAgent
    from tests.test_acp import DummyConn, GoalAutopilotCompleteLLM

    config = session_config(tmp_path)
    config.agent = backend_settings(tmp_path, max_steps=3, goal_autopilot_max_turns=2)
    config.agent.enable_memory_extraction = False
    host = BoxACPAgent(DummyConn(), config, GoalAutopilotCompleteLLM(), [], "system")
    try:
        info = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={
            "session_mode": "general", "memory": {"tenant_id": "tenant-acp", "user_id": "user-acp"},
            "goal": {"objective": "完成请求", "status": "active"},
        }))
        response = await host.prompt(SimpleNamespace(sessionId=info.sessionId, field_meta={"ui_language": "zh"},
            prompt=[{"text": "此前聊天：旧问题与旧回答\n当前用户问题：真实请求"}]))
        assert response.stopReason == "end_turn"
        assert response.field_meta["goalAutopilot"]["continuations"] == 1
        state = host._sessions[info.sessionId]
        assert "memory_write" not in state.agent.tools
        assert (await host.ext_method("memory_proposal_list", {"sessionId": info.sessionId}))["candidates"] == []
        assert (await host.ext_method("memory_proposal_apply", {"sessionId": info.sessionId}))["error"] == "memory_unavailable"
    finally:
        await host.aclose()
    assert len([call for call in memory_http.calls if call[0].endswith("/files/read")]) == 2
    saves = [body for path, body in memory_http.calls if path.endswith("/save")]
    assert len(saves) == 1
    assert saves[0]["tenant_id"] == "tenant-acp" and saves[0]["user_id"] == "user-acp"
    assert str(uuid.UUID(saves[0]["session_id"])) == saves[0]["session_id"]
    assert saves[0]["content"] == {"user": "真实请求", "assistant": "goal done"}
    assert capsys.readouterr().out == ""


def test_cli_autopilot_refreshes_once_and_saves_only_final_qa(tmp_path, memory_http, monkeypatch):
    import box_agent.cli as cli

    path = tmp_path / "config.yaml"
    path.write_text("api_key: test\n", encoding="utf-8")
    config = session_config(tmp_path)
    config.agent = backend_settings(tmp_path, goal_autopilot_max_turns=2)
    config.agent.enable_memory_extraction = False
    config.agent.memory_maintainer_enabled = False
    config.tools.allow_full_access = True
    calls = []

    async def base_tools(*args, **kwargs):
        return [], None, None, None

    async def agent_run(self, **kwargs):
        calls.append(self.system_prompt)
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content=f"回复{len(calls)}")

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _: config))
    monkeypatch.setattr(cli.Config, "find_config_file", staticmethod(lambda _: None))
    monkeypatch.setattr(cli, "LLMClient", lambda **kwargs: RecordingLLM())
    monkeypatch.setattr(cli, "initialize_base_tools", base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.Agent, "run_events", agent_run)
    monkeypatch.setattr(cli, "should_continue_goal_autopilot", lambda *args: len(calls) < 2)
    result = asyncio.run(cli.run_agent(tmp_path, task="用户任务", initial_goal="完成用户任务",
                                       sandbox_mode=False, verify_api=False))
    assert result == 0 and len(calls) == 2
    assert all("用户偏好中文" in prompt for prompt in calls)
    assert len([call for call in memory_http.calls if call[0].endswith("/files/read")]) == 2
    saves = [body for path, body in memory_http.calls if path.endswith("/save")]
    assert len(saves) == 1
    assert saves[0]["content"] == {"user": "用户任务", "assistant": "回复2"}
    assert str(uuid.UUID(saves[0]["session_id"])) == saves[0]["session_id"]


@pytest.mark.asyncio
async def test_generic_optional_recall_read_identity_mapping_and_error_flag(tmp_path, memory_http):
    memory_http.handler = lambda request, payload: httpx.Response(200, json={"success": True, "value": "通用内容"})
    config = ExternalMemoryConfig(base_url="http://memory.test",
        recall=MemoryHttpOperation(path="/context", request={"owner": "${user_id}"}, response_path="value"),
        read=MemoryHttpOperation(path="/get", request={"key": "${path}"}, response_path="value",
                                 success_path="success"))
    settings = AgentConfig(memory_dir=str(tmp_path / "mem"), memory_backend_type="generic",
                           memory_user_id="user-a", memory_external=config)
    backend = create_memory_backend(settings)
    assert await backend.load_context(query="q") == "通用内容"
    assert await backend.read_memory("profile-key") == "通用内容"
    assert memory_http.calls == [("/context", {"owner": "user-a"}), ("/get", {"key": "profile-key"})]
    memory_http.handler = lambda request, payload: httpx.Response(200, json={"success": False, "value": "失败"})
    with pytest.raises(MemoryBackendError):
        await backend.read_memory("profile-key")


@pytest.mark.asyncio
async def test_read_tool_reports_failure_and_missing_core_file_is_empty(tmp_path, memory_http):
    backend = create_memory_backend(backend_settings(tmp_path))
    tool = next(tool for tool in create_memory_tools(backend) if tool.name == "memory_read")
    memory_http.handler = lambda request, payload: httpx.Response(200, json={"ok": True, "data": {"content": "", "exists": False}})
    assert (await tool.execute()).success
    memory_http.handler = lambda request, payload: httpx.Response(400, json={"ok": False})
    assert not (await tool.execute()).success


@pytest.mark.asyncio
async def test_background_save_does_not_take_over_delayed_event_consumer(tmp_path, memory_http):
    session, _ = await managed_session(tmp_path)
    try:
        handle = await AgentService().start(RunRequest("run-delayed", "s", "用户请求"), session=session,
                                            options=session.build_run_options(logger=None))
        # 等待后台保存完成，宿主随后仍应能读取完整事件流。
        for _ in range(100):
            if any(path.endswith("/save") for path, _ in memory_http.calls):
                break
            await asyncio.sleep(0.01)
        assert any(path.endswith("/save") for path, _ in memory_http.calls)
        events = [item async for item in handle.events()]
        assert any(isinstance(item.payload, DoneEvent) for item in events)
        assert (await handle.result()).final_content == "最终回答"
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_prepared_tools_honor_identity_override_without_expanding_catalog(tmp_path, memory_http):
    config = session_config(tmp_path)
    config.agent = backend_settings(tmp_path)
    backend = create_memory_backend(config.agent)
    search = next(tool for tool in create_memory_tools(backend) if tool.name == "memory_search")
    session = await AgentSession.open(config=config, host=HostBindings(llm_client=RecordingLLM(),
        tools=[search], system_prompt="system", memory_manager=backend),
        options=SessionOptions(workspace_dir=tmp_path, memory_tenant_id="tenant-new", memory_user_id="user-new"))
    try:
        assert "memory_read" not in session.agent.tools
        assert session.memory_manager.tenant_id == "tenant-new"
        await session.agent.tools["memory_search"].execute(query="任务")
        assert memory_http.calls[-1][1]["tenant_id"] == "tenant-new"
        assert memory_http.calls[-1][1]["user_id"] == "user-new"
        assert backend.tenant_id == backend.user_id == "default"
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_save_runs_in_background_and_close_flushes_accepted_work(tmp_path, memory_http):
    session, _ = await managed_session(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    saved = []

    async def slow_save(user, assistant, **context):
        started.set()
        await release.wait()
        saved.append((user, assistant))

    session.memory_runtime.backend.save_turn = slow_save
    try:
        result = await asyncio.wait_for(run(session, "请求"), timeout=1)
        assert result.final_content == "最终回答"
        await asyncio.wait_for(started.wait(), timeout=1)
        assert saved == []
    finally:
        release.set()
        await session.aclose()
    assert saved == [("请求", "最终回答")]


@pytest.mark.asyncio
async def test_generic_accepts_no_content_save_response_and_keeps_qa_out_of_http_logs(tmp_path, memory_http, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    memory_http.handler = lambda request, payload: httpx.Response(204)
    settings = AgentConfig(memory_dir=str(tmp_path / "mem"), memory_backend_type="generic",
        memory_external=ExternalMemoryConfig(base_url="http://memory.test", save=MemoryHttpOperation(
            path="/remember", method="GET", request={"input": "${user}", "output": "${assistant}"})))
    backend = create_memory_backend(settings)
    await backend.save_turn("private-question", "private-answer", session_id="s", turn_id="t")
    assert memory_http.calls[-1] == ("/remember", {"input": "private-question", "output": "private-answer"})
    assert "private-question" not in caplog.text and "private-answer" not in caplog.text
    async with httpx.AsyncClient() as client:
        await client.get("http://memory.test/unrelated")
    assert "unrelated" in caplog.text


@pytest.mark.asyncio
async def test_memory_request_total_timeout_is_bounded_and_logged(tmp_path, monkeypatch, caplog):
    async def respond(request):
        await asyncio.Event().wait()

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    settings = backend_settings(tmp_path)
    settings.memory_external.timeout_seconds = 0.01
    settings.memory_external.max_retries = 0
    backend = create_memory_backend(settings)
    with pytest.raises(MemoryBackendError):
        await asyncio.wait_for(backend.search_memory("请求"), timeout=1)
    assert "TimeoutError" in caplog.text and "abandoned" in caplog.text


@pytest.mark.asyncio
async def test_full_save_queue_logs_abandonment_without_blocking_next_turn(tmp_path, memory_http, caplog):
    session, _ = await managed_session(tmp_path)
    session.memory_runtime.backend.config.save_queue_limit = 1
    started, release = asyncio.Event(), asyncio.Event()
    saved = []

    async def slow_save(user, assistant, **context):
        started.set()
        await release.wait()
        saved.append(user)

    session.memory_runtime.backend.save_turn = slow_save
    try:
        await run(session, "第一轮", "one")
        await asyncio.wait_for(started.wait(), timeout=1)
        result = await asyncio.wait_for(run(session, "第二轮", "two"), timeout=1)
        assert result.final_content == "最终回答"
    finally:
        release.set()
        await session.aclose()
    assert saved == ["第一轮"]
    assert "abandoned_queue_full_or_closed" in caplog.text and "turn=two" in caplog.text
