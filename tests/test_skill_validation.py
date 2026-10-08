"""Request snapshots and background source validation boundaries."""

import asyncio
import json
import logging
import threading
from pathlib import Path

import pytest

from box_agent.context_input import DefaultContextEngine
from box_agent.schema import Message
from box_agent.skill_context import SkillReferenceContext
from box_agent.skill_dependencies import SkillDependencyError
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


def write_skill(root, name="demo", body="OLD_METHOD", required=()):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: example\nrequired_skills: {json.dumps(list(required))}\n---\n{body}\n")
    return path


def make_context(tmp_path):
    path = write_skill(tmp_path / "user")
    loader = SkillLoader(sources=[(tmp_path / "user", "user"), (tmp_path / "builtin", "builtin")],
                         skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    runtime = SkillRuntime(loader)
    runtime.select(["demo"])
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime, session_store=None)
    return path, loader, runtime, engine


async def prepare(engine, messages=None):
    return await engine.aprepare_request(messages or [Message(role="user", content="task")],
                                        prepared_tools=prepare_tools([]), token_limit=20000)


def block_scan(loader, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = loader._source_signature
    calls = []

    def scan(entry):
        calls.append(entry.directory)
        entered.set()
        assert release.wait(3), "test did not release the scan worker"
        return original(entry)

    monkeypatch.setattr(loader, "_source_signature", scan)
    loader.validation_timeout_seconds = 0.02
    return entered, release, calls


async def finish_worker(loader, release):
    release.set()
    if loader._validation_task is not None:
        await asyncio.wait_for(asyncio.shield(loader._validation_task), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_request_reuses_one_validation_per_source_and_skill(tmp_path, monkeypatch, changed):
    path, loader, runtime, engine = make_context(tmp_path)
    first = SkillReferenceContext(runtime).read("demo")
    history = [Message(role="user", content="task")]
    history.extend(Message(role="tool", name="get_skill", tool_call_id=f"read-{i}", content=first.model_context)
                   for i in range(8))
    if changed:
        write_skill(tmp_path / "user", body="NEW_METHOD")
    scans, reads = [], []
    real_scan, real_read = loader._source_signature, Path.read_bytes

    def scan(entry):
        scans.append(entry.directory)
        return real_scan(entry)

    def read(file):
        reads.append(file)
        return real_read(file)

    monkeypatch.setattr(loader, "_source_signature", scan)
    monkeypatch.setattr(Path, "read_bytes", read)
    before = [message.model_dump() for message in history]
    await prepare(engine, history)
    assert scans.count(tmp_path / "user") == 1
    assert scans.count(tmp_path / "builtin") == 1
    # A changed file is parsed once and re-read once for the race guard.
    assert reads.count(path) == (2 if changed else 1)
    assert [message.model_dump() for message in history] == before
    assert runtime._reference_validation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["modified", "deleted", "disabled", "source_replaced", "dependency_broken"])
async def test_next_request_observes_source_and_dependency_changes(tmp_path, change):
    path, loader, runtime, engine = make_context(tmp_path)
    first = await prepare(engine)
    if change == "modified":
        write_skill(tmp_path / "user", body="NEW_METHOD")
    elif change == "deleted":
        path.unlink()
    elif change == "disabled":
        (tmp_path / "settings.json").write_text('{"disabledSkillNames":["demo"]}')
    elif change == "source_replaced":
        write_skill(tmp_path / "builtin", body="BUILTIN_METHOD")
        path.unlink()
    else:
        write_skill(tmp_path / "user", required=["leaf"])
        write_skill(tmp_path / "user", "leaf").write_text("---\nname: [invalid\n---\n")
    next_request = await prepare(engine)
    assert next_request.blocked_reason is None
    if change in {"modified", "source_replaced"}:
        assert "OLD_METHOD" not in str(next_request.messages)
        assert next_request.references[0]["revision"] != first.references[0]["revision"]
        assert ("NEW_METHOD" if change == "modified" else "BUILTIN_METHOD") in str(next_request.messages)
    else:
        assert "OLD_METHOD" not in str(next_request.messages)
        assert not next_request.references


@pytest.mark.asyncio
async def test_timeout_uses_verified_snapshot_only_for_preparation(tmp_path, monkeypatch):
    path, loader, runtime, engine = make_context(tmp_path)
    await prepare(engine)
    write_skill(tmp_path / "user", body="NEW_METHOD")
    entered, release, _ = block_scan(loader, monkeypatch)
    runtime.validation_timeout_seconds = 0.02
    try:
        projected = await prepare(engine)
        assert entered.is_set()
        assert projected.blocked_reason is None
        assert "OLD_METHOD" in str(projected.messages)
        assert "NEW_METHOD" not in str(projected.messages)
        tool_result = await GetSkillTool(loader).execute("demo")
        assert not tool_result.success
        assert tool_result.raw_output["code"] == "SKILL_VALIDATION_TIMEOUT"
        assert runtime._reference_validation is None
    finally:
        await finish_worker(loader, release)
    # The worker completes without touching session delivery/observation facts.
    assert runtime.log_records() == []
    updated = await prepare(engine)
    assert "NEW_METHOD" in str(updated.messages)
    assert "OLD_METHOD" not in str(updated.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("known_invalid", [False, True])
async def test_timeout_does_not_invent_or_revive_unavailable_references(tmp_path, monkeypatch, known_invalid):
    _, loader, runtime, engine = make_context(tmp_path)
    if known_invalid:
        await prepare(engine)
        (tmp_path / "settings.json").write_text('{"disabledSkillNames":["demo"]}')
        loader.discover_skills()
    entered, release, _ = block_scan(loader, monkeypatch)
    runtime.validation_timeout_seconds = 0.02
    try:
        result = await prepare(engine)
        assert entered.is_set()
        assert "OLD_METHOD" not in str(result.messages)
        assert not result.references
        assert result.blocked_reason is None
    finally:
        await finish_worker(loader, release)


@pytest.mark.asyncio
async def test_cancellation_keeps_worker_serialized_and_never_commits_session_facts(tmp_path, monkeypatch):
    _, loader, runtime, engine = make_context(tmp_path)
    await prepare(engine)
    entered, release, calls = block_scan(loader, monkeypatch)
    task = asyncio.create_task(prepare(engine))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        original_worker = loader._validation_task
        runtime.validation_timeout_seconds = 0.02
        result = await prepare(engine)
        assert "OLD_METHOD" in str(result.messages)
        assert loader._validation_task is original_worker
        assert len(calls) == 1
        assert loader.maybe_reload() is False
        assert runtime.log_records() == []
        assert runtime._reference_validation is None
    finally:
        await finish_worker(loader, release)


@pytest.mark.asyncio
async def test_catalog_readers_see_complete_publications_during_reload(tmp_path, monkeypatch):
    _, loader, _, _ = make_context(tmp_path)
    write_skill(tmp_path / "user", "extra", "EXTRA_METHOD")
    entered, release = threading.Event(), threading.Event()
    real_load = loader.load_skill

    def load(path, source="builtin"):
        entered.set()
        assert release.wait(3)
        return real_load(path, source=source)

    monkeypatch.setattr(loader, "load_skill", load)
    worker = asyncio.create_task(asyncio.to_thread(loader.discover_skills))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert loader.list_skills() == ["demo"]
        assert [row["name"] for row in loader.list_skills_metadata()] == ["demo"]
        assert loader.get_skill("demo").content == "OLD_METHOD"
    finally:
        release.set()
        await asyncio.wait_for(worker, 2)
    assert set(loader.list_skills()) == {"demo", "extra"}


@pytest.mark.asyncio
async def test_paged_tool_read_rejects_old_revision_after_preparation(tmp_path):
    _, loader, _, engine = make_context(tmp_path)
    await prepare(engine)
    tool = GetSkillTool(loader)
    first = await tool.execute("demo", limit=2)
    revision = first.raw_output["skill_reference"]["revision"]
    write_skill(tmp_path / "user", body="NEW_METHOD")
    stale = await tool.execute("demo", offset=2, revision=revision)
    assert not stale.success
    assert "changed" in stale.error


@pytest.mark.asyncio
async def test_change_between_parse_and_validation_is_reported(tmp_path, monkeypatch):
    path, loader, runtime, _ = make_context(tmp_path)
    real_read = loader.read_skill_bytes

    def read(file):
        if file == path:
            write_skill(tmp_path / "user", body="NEW_METHOD")
        return real_read(file)

    monkeypatch.setattr(loader, "read_skill_bytes", read)
    validation = await runtime.avalidate_references(("demo",), allow_stale=False)
    with pytest.raises(SkillDependencyError) as error:
        validation.resolve("demo")
    assert error.value.code == "SKILL_SOURCE_CHANGED"
    monkeypatch.setattr(loader, "read_skill_bytes", real_read)
    updated = await runtime.avalidate_references(("demo",), allow_stale=False)
    assert "NEW_METHOD" in updated.resolve("demo").prompt


@pytest.mark.asyncio
async def test_validation_metrics_contain_counts_and_timings_without_skill_body(tmp_path, caplog):
    _, _, _, engine = make_context(tmp_path)
    with caplog.at_level(logging.INFO):
        await prepare(engine)
    records = [record.getMessage() for record in caplog.records if record.name.startswith("box_agent")]
    assert any("source_scan_calls" in record and "file_read_calls" in record for record in records)
    assert any("context/prepare duration_ms=" in record for record in records)
    assert all("OLD_METHOD" not in record and str(tmp_path) not in record for record in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_kind", ["context", "runtime"])
async def test_compatible_reader_never_bypasses_timeout_or_runs_source_io_on_loop(tmp_path, monkeypatch, reader_kind):
    _, loader, runtime, _ = make_context(tmp_path)
    reader = SkillReferenceContext(runtime).read if reader_kind == "context" else runtime.read
    original = loader.read_skill_bytes

    def read(path):
        assert threading.current_thread() is not threading.main_thread()
        return original(path)

    monkeypatch.setattr(loader, "read_skill_bytes", read)
    tool = GetSkillTool(loader)
    result = await tool.invoke({"skill_name": "demo"}, context=ToolInvocationContext(skill_reader=reader))
    assert result.success
    entered, release, _ = block_scan(loader, monkeypatch)
    before = runtime.log_records()
    try:
        result = await tool.invoke({"skill_name": "demo"}, context=ToolInvocationContext(skill_reader=reader))
        assert entered.is_set()
        assert not result.success
        assert result.raw_output["code"] == "SKILL_VALIDATION_TIMEOUT"
        assert runtime.log_records() == before
    finally:
        await finish_worker(loader, release)


@pytest.mark.asyncio
async def test_paged_read_does_not_join_a_snapshot_sampled_before_the_call(tmp_path, monkeypatch):
    path, loader, _, _ = make_context(tmp_path)
    write_skill(tmp_path / "user", "slow")
    loader.discover_skills()
    tool = GetSkillTool(loader)
    first = await tool.execute("demo", limit=1)
    revision = first.raw_output["skill_reference"]["revision"]
    entered, release = threading.Event(), threading.Event()
    original = loader.read_skill_bytes

    def read(file):
        if file.parent.name == "slow":
            entered.set()
            assert release.wait(3)
        return original(file)

    monkeypatch.setattr(loader, "read_skill_bytes", read)
    previous = asyncio.create_task(loader.avalidate_references(("demo", "slow")))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        write_skill(path.parent.parent, body="NEW_METHOD")
        pending_page = asyncio.create_task(tool.execute("demo", offset=1, revision=revision))
        await asyncio.sleep(0)
        release.set()
        result = await asyncio.wait_for(pending_page, 2)
        assert not result.success
        assert "changed" in result.error
    finally:
        release.set()
        await previous
        await finish_worker(loader, release)


@pytest.mark.asyncio
async def test_async_context_preserves_sync_extension_hooks_without_revalidating(tmp_path, monkeypatch):
    _, loader, _, engine = make_context(tmp_path)
    prepared, bound = [], []
    original_prepare, original_bind = engine.prepare_request, engine.bind_history
    original_scan = loader._source_signature

    def scan(entry):
        assert threading.current_thread() is not threading.main_thread()
        return original_scan(entry)

    def prepare_hook(*args, **kwargs):
        prepared.append(True)
        return original_prepare(*args, **kwargs)

    def bind_hook(messages):
        bound.append(True)
        original_bind(messages)

    monkeypatch.setattr(loader, "_source_signature", scan)
    monkeypatch.setattr(engine, "prepare_request", prepare_hook)
    monkeypatch.setattr(engine, "bind_history", bind_hook)
    await engine.abind_history([Message(role="user", content="task")])
    result = await prepare(engine)
    assert prepared == [True] and bound
    assert "OLD_METHOD" in str(result.messages)


@pytest.mark.asyncio
async def test_async_restore_validates_sources_without_moving_session_state_to_worker(tmp_path, monkeypatch):
    _, loader, _, _ = make_context(tmp_path)
    runtime = SkillRuntime(loader)
    records = [{"name": "demo", "sha256": "old", "loadOrder": 1}]
    original_scan = loader._source_signature
    original_restore = runtime.restore_records

    def scan(entry):
        assert threading.current_thread() is not threading.main_thread()
        return original_scan(entry)

    def restore(rows):
        assert threading.current_thread() is threading.main_thread()
        return original_restore(rows)

    monkeypatch.setattr(loader, "_source_signature", scan)
    monkeypatch.setattr(runtime, "restore_records", restore)
    await runtime.arestore_records(records)
    assert runtime.log_records()[0]["name"] == "demo"
    assert runtime._reference_validation is None


@pytest.mark.asyncio
async def test_sub_agent_skill_validation_timeout_keeps_event_loop_responsive(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from box_agent.tools.sub_agent_tool import SubAgentTool

    _, loader, _, _ = make_context(tmp_path)
    tool = SubAgentTool(llm=AsyncMock(), parent_tools={}, workspace_dir=str(tmp_path))
    tool.set_skill_provider(lambda: loader)
    entered, release, _ = block_scan(loader, monkeypatch)
    pending = asyncio.create_task(tool.execute(task="Apply the method", required_tools=[], skills=["demo"]))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.sleep(0), 0.1)
        result = await asyncio.wait_for(pending, 0.5)
        assert not result.success
        assert result.raw_output["code"] == "SKILL_VALIDATION_TIMEOUT"
    finally:
        await finish_worker(loader, release)
        await pending


@pytest.mark.asyncio
@pytest.mark.parametrize("already_restored", [False, True])
@pytest.mark.parametrize("damaged_name", [False, True])
async def test_restore_timeout_preserves_records_for_next_request(tmp_path, monkeypatch, already_restored, damaged_name):
    _, loader, _, _ = make_context(tmp_path)
    runtime = SkillRuntime(loader, allow_partial_restore=True)
    records = [{"name": "demo", "sha256": "old", "loadOrder": 1}]
    inputs = ([{"name": ["invalid"], "sha256": "junk", "loadOrder": 2}, *records]
              if damaged_name else records)
    if already_restored:
        await runtime.arestore_records(inputs)
    before = runtime.log_records()
    entered, release, _ = block_scan(loader, monkeypatch)
    runtime.validation_timeout_seconds = 0.02
    try:
        await runtime.arestore_records(inputs)
        assert entered.is_set()
        assert runtime.log_records() == before
        assert runtime._deferred_restore_records == records
    finally:
        await finish_worker(loader, release)
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    result = await prepare(engine)
    assert "OLD_METHOD" in json.dumps([message.model_dump() for message in result.messages])
    assert runtime.log_records()[0]["name"] == "demo"
    assert runtime._deferred_restore_records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_kind", ["context", "runtime"])
async def test_async_tool_preserves_explicit_caller_references(tmp_path, reader_kind):
    _, loader, runtime, _ = make_context(tmp_path)
    runtime.register_reference("demo", "CALLER_METHOD", persist=False)
    reader = SkillReferenceContext(runtime).read if reader_kind == "context" else runtime.read
    result = await GetSkillTool(loader).invoke({"skill_name": "demo"},
                                              context=ToolInvocationContext(skill_reader=reader))
    assert result.success
    assert "CALLER_METHOD" in result.model_context
    assert "OLD_METHOD" not in result.model_context
    assert result.raw_output["skill_reference"]["source"] == "caller"
    assert runtime.log_records()[0]["source"] == "caller"


@pytest.mark.asyncio
async def test_settings_change_during_discovery_is_detected_by_next_request(tmp_path, monkeypatch):
    import box_agent.tools.skill_loader as loader_module

    _, loader, _, engine = make_context(tmp_path)
    await prepare(engine)
    original = loader_module._read_disabled_skill_names
    edited = False

    def read_settings(path):
        nonlocal edited
        names = original(path)
        if not edited:
            edited = True
            path.write_text('{"disabledSkillNames":["demo"]}')
        return names

    monkeypatch.setattr(loader_module, "_read_disabled_skill_names", read_settings)
    await asyncio.to_thread(loader.discover_skills)
    assert loader.get_skill("demo") is not None
    request = await prepare(engine)
    assert "OLD_METHOD" not in str(request.messages)
    assert not request.references


@pytest.mark.asyncio
@pytest.mark.parametrize("same_name", [False, True])
async def test_deferred_restore_preserves_later_host_references_and_unique_orders(tmp_path, monkeypatch, same_name):
    from box_agent.skill_restore import validate_restore_records

    _, loader, _, _ = make_context(tmp_path)
    runtime = SkillRuntime(loader, allow_partial_restore=True)
    runtime.validation_timeout_seconds = 0.02
    records = [{"name": "demo", "sha256": "old", "loadOrder": 1}]
    _, release, _ = block_scan(loader, monkeypatch)
    try:
        await runtime.arestore_records(records)
        name = "demo" if same_name else "new"
        runtime.register_reference(name, "NEW_CALLER_METHOD", persist=False)
    finally:
        await finish_worker(loader, release)
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    result = await prepare(engine)
    assert "NEW_CALLER_METHOD" in json.dumps([message.model_dump() for message in result.messages])
    assert runtime.selected_names == (name,)
    assert runtime.state.reads[name].source == "caller"
    assert set(runtime.state.reads) == ({"demo"} if same_name else {"demo", "new"})
    validate_restore_records(runtime.log_records())


@pytest.mark.asyncio
async def test_caller_reader_refreshes_required_dependency_scope_before_returning_body(tmp_path):
    _, loader, runtime, _ = make_context(tmp_path)
    runtime.register_reference("demo", "CALLER_METHOD", persist=False)
    write_skill(tmp_path / "user", "leaf")
    write_skill(tmp_path / "user", required=["leaf"])
    tool = GetSkillTool(loader, allowed_skill_names=frozenset({"demo"}))
    result = await tool.invoke({"skill_name": "demo"},
                               context=ToolInvocationContext(skill_reader=SkillReferenceContext(runtime).read))
    assert not result.success
    assert "outside this task" in result.error
    assert "CALLER_METHOD" not in (result.model_context or "")
