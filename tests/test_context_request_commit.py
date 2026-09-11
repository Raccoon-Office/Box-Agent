"""Host reference delivery is acknowledged only after the request commits."""

from hashlib import sha256

import pytest

from box_agent.agent import Agent, AgentRunOptions
from box_agent.context_input import DefaultContextEngine
from box_agent.schema import Message
from box_agent.session_log import SessionLog
from box_agent.skill_runtime import SkillRuntime
from box_agent.skill_context import SkillReferenceContext
from box_agent.tools.engine.preparation import prepare_tools
from tests.test_skill_entry_boundaries import CapturingProvider


BODY = "RESTORED_METHOD_EXACT_BODY"


class RecoverableStore:
    """Delegate real persistence, rejecting one write before it reaches disk."""

    def __init__(self, log, fault):
        self.log, self.fault = log, fault
        self.fired = self.context_ready = False

    def __getattr__(self, name):
        return getattr(self.log, name)

    def fail(self, point):
        if point == self.fault and not self.fired:
            self.fired = True
            raise OSError("recoverable store failure at " + point)

    def append(self, kind, data, **kwargs):
        if kind == "request/context":
            self.fail("context_append")
            self.context_ready = True
        return self.log.append(kind, data, **kwargs)

    def flush(self):
        if self.context_ready:
            self.context_ready = False
            self.fail("context_flush")
        return self.log.flush()

    def store_skill_reference(self, text):
        self.fail("snapshot")
        return self.log.store_skill_reference(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["snapshot", "context_append", "context_flush"])
async def test_restored_host_material_survives_failed_request_commit_and_retry(tmp_path, fault):
    log = SessionLog.create(tmp_path / "sessions", session_id="restored", cwd=tmp_path)
    log.append("skill/change", {"skills": [{"name": "demo", "sha256": sha256(BODY.encode()).hexdigest(), "loadOrder": 1}]})
    log.flush()
    store = RecoverableStore(log, fault)
    provider = CapturingProvider()
    agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], session_log=store,
                  workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, max_steps=1)
    agent.restore_active_skill_instructions([("demo", BODY, sha256(BODY.encode()).hexdigest(), 1)])
    try:
        agent.add_user_message("continue")
        with pytest.raises(OSError, match="recoverable store failure"):
            _ = [event async for event in agent.run_events()]
        assert provider.requests == []
        assert agent.skill_runtime._restore_pending == ("demo",)
        assert agent.skill_runtime.turn_deliveries == {}

        agent.add_user_message("retry")
        _ = [event async for event in agent.run_events()]
        assert len(provider.requests) == 1
        assert BODY in str(provider.requests[0])
        assert "get_skill" not in agent.tools
        assert agent.skill_runtime._restore_pending == ()
    finally:
        log.close()


@pytest.mark.asyncio
async def test_restored_material_survives_request_logger_write_failure_and_retry(tmp_path, monkeypatch):
    import builtins
    import box_agent.logger as logger_module

    monkeypatch.setattr(logger_module, "state_path", lambda name: tmp_path / name)
    logger = logger_module.AgentLogger()
    logger.start_new_run()
    failed = False

    def fail_request_open(path, mode="r", *args, **kwargs):
        nonlocal failed
        if path == logger.log_file and mode == "a" and not failed:
            failed = True
            raise OSError("request log disk full")
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(logger_module, "open", fail_request_open, raising=False)
    log = SessionLog.create(tmp_path / "sessions", session_id="request-logger", cwd=tmp_path)
    provider = CapturingProvider()
    agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], session_log=log,
                  workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, max_steps=1)
    agent.restore_active_skill_instructions([("demo", BODY, sha256(BODY.encode()).hexdigest(), 1)])
    options = AgentRunOptions(llm=provider, logger=logger)
    try:
        agent.add_user_message("continue")
        with pytest.raises(OSError, match="request log disk full"):
            _ = [event async for event in agent.run_events(options=options)]
        assert failed and provider.requests == []
        assert agent.skill_runtime._restore_pending == ("demo",)
        assert agent.skill_runtime.turn_deliveries == {}

        agent.add_user_message("retry")
        _ = [event async for event in agent.run_events(options=options)]
        assert len(provider.requests) == 1 and BODY in str(provider.requests[0])
        assert "get_skill" not in agent.tools
        assert agent.skill_runtime._restore_pending == ()
    finally:
        log.close()


def test_prepared_request_defers_delivery_and_commit_callback_is_idempotent(monkeypatch):
    runtime = SkillRuntime(None)
    runtime.restore_records([{"name": "demo", "prompt": BODY, "sha256": sha256(BODY.encode()).hexdigest(), "loadOrder": 1}])
    runtime.begin_turn()
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    deliveries = []
    original = runtime.record_delivery

    def record(snapshot, metadata, *, reason):
        deliveries.append(snapshot.name)
        original(snapshot, metadata, reason=reason)

    monkeypatch.setattr(runtime, "record_delivery", record)
    request = engine.prepare_request([Message(role="user", content="task")],
                                     prepared_tools=prepare_tools([]), token_limit=20000)
    assert BODY in str(request.messages)
    assert deliveries == []
    assert runtime._restore_pending == ("demo",)
    request.on_committed()
    request.on_committed()
    assert deliveries == ["demo"]
    assert runtime._restore_pending == ("demo",)
    request.on_response()
    request.on_response()
    assert runtime._restore_pending == ()


@pytest.mark.asyncio
async def test_successful_request_without_store_acknowledges_restored_material(tmp_path):
    provider = CapturingProvider()
    agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], workspace_dir=str(tmp_path),
                  deferred_mcp_loading_enabled=False, max_steps=1)
    agent.restore_active_skill_instructions([("demo", BODY, sha256(BODY.encode()).hexdigest(), 1)])
    agent.add_user_message("task")
    _ = [event async for event in agent.run_events()]
    assert len(provider.requests) == 1
    assert BODY in str(provider.requests[0])
    assert agent.skill_runtime._restore_pending == ()


def test_standalone_reference_projection_keeps_immediate_delivery_compatibility():
    runtime = SkillRuntime(None)
    runtime.restore_records([{"name": "demo", "prompt": BODY, "sha256": sha256(BODY.encode()).hexdigest(), "loadOrder": 1}])
    runtime.begin_turn()
    request = SkillReferenceContext(runtime).prepare_request([Message(role="user", content="task")], budget_chars=20000)
    assert BODY in str(request.messages)
    assert runtime.turn_deliveries["demo"]["complete"]
    assert runtime._restore_pending == ()
    assert request.on_committed is None


@pytest.mark.asyncio
async def test_kernel_accepts_legacy_context_projection_without_commit_callback(tmp_path, monkeypatch):
    from dataclasses import fields
    from types import SimpleNamespace

    original = DefaultContextEngine.prepare_request

    def prepare(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        return SimpleNamespace(**{field.name: getattr(result, field.name) for field in fields(result)
                                  if field.name != "on_committed"})

    monkeypatch.setattr(DefaultContextEngine, "prepare_request", prepare)
    provider = CapturingProvider()
    agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], workspace_dir=str(tmp_path),
                  deferred_mcp_loading_enabled=False, max_steps=1)
    agent.add_user_message("task")
    _ = [event async for event in agent.run_events()]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_failed_delivery_acknowledgement_does_not_start_provider_or_consume_pending(tmp_path, monkeypatch):
    provider = CapturingProvider()
    agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], workspace_dir=str(tmp_path),
                  deferred_mcp_loading_enabled=False, max_steps=1)
    agent.restore_active_skill_instructions([("demo", BODY, sha256(BODY.encode()).hexdigest(), 1)])
    original = agent.skill_runtime.record_delivery
    failed = False

    def record(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("delivery acknowledgement failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(agent.skill_runtime, "record_delivery", record)
    agent.add_user_message("task")
    with pytest.raises(OSError, match="delivery acknowledgement failed"):
        _ = [event async for event in agent.run_events()]
    assert provider.requests == []
    assert agent.skill_runtime._restore_pending == ("demo",)
    _ = [event async for event in agent.run_events()]
    assert len(provider.requests) == 1
    assert BODY in str(provider.requests[0])
    assert agent.skill_runtime._restore_pending == ()
