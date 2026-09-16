"""Adopted methods survive compaction without a domain lifecycle."""

import json

import pytest

from box_agent.context_input import DefaultContextEngine
from box_agent.schema import Message
from box_agent.session_log import SessionLog
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


def setup(tmp_path, *, store=None):
    for name, body in (("method", "KEEP_EXACT_METHOD\nSECOND_RULE\n"),
                       ("reference", "REFERENCE_ONLY\n"), ("next", "NEXT_METHOD\n")):
        path = tmp_path / name / "SKILL.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: test\n---\n{body}")
    loader = SkillLoader(sources=[(tmp_path, "user")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    runtime = SkillRuntime(loader, session_log=store)
    tool = GetSkillTool(loader)
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime, session_store=store)
    history = [Message(role="system", content="System"),
               Message(role="user", content="Make the report in Chinese, 8 pages, with all illustrations.")]
    return runtime, tool, engine, history


def prepare(engine, tool, history, limit=12000):
    return engine.prepare_request(history, prepared_tools=prepare_tools([tool]), token_limit=limit)


async def read(engine, tool, name, **kwargs):
    return await tool.invoke({"skill_name": name, **kwargs},
                             context=ToolInvocationContext(skill_reader=engine.tool_reader))


@pytest.mark.asyncio
async def test_used_method_is_restored_as_exact_input_without_model_reread(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    first = await read(engine, tool, "method")
    assert first.success
    history[:] = [history[0], Message(role="user", source="runtime", content="Summary: working on report.")]
    rebuilt = prepare(engine, tool, history)
    assert rebuilt.blocked_reason is None
    assert any(isinstance(message.content, str) and "KEEP_EXACT_METHOD\nSECOND_RULE" in message.content
               for message in rebuilt.messages)
    assert "8 pages" in str(rebuilt.messages)
    assert history[-1].content == "Summary: working on report."


@pytest.mark.asyncio
async def test_reference_read_is_not_a_required_method(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    result = await read(engine, tool, "reference", usage="reference")
    assert result.success
    history[:] = [history[0], Message(role="user", source="runtime", content="Summary")]
    assert "REFERENCE_ONLY" not in str(prepare(engine, tool, history).messages)


def test_candidate_task_checkpoint_preserves_legacy_restoration_after_restart(tmp_path):
    store = SessionLog.create(tmp_path / "sessions", session_id="legacy-candidate", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        snapshot = runtime.resolve_reference("method")
        records = [{"name": snapshot.name, "sha256": snapshot.revision, "loadOrder": 1}]
        store.append("skill/change", {"skills": records})
        store.flush()
        runtime.restore_records(records)
        runtime.observe_task_history(history)
        assert store.replay().skills == records
        store.close()
        store = SessionLog.open(tmp_path / "sessions", session_id="legacy-candidate", cwd=tmp_path)
        restored = SkillRuntime(runtime.loader, session_log=store)
        restored.restore_records(store.replay().skills)
        restored.begin_turn()
        assert restored.restoring_names == ("method",)
        context = DefaultContextEngine()
        context.configure_run(skill_engine=restored, session_store=store)
        assert "KEEP_EXACT_METHOD" in str(prepare(context, tool, history).messages)
        assert restored.task.user_inputs == [history[-1].content]
    finally:
        store.close()


@pytest.mark.parametrize("initialized", [None, False, True])
def test_task_semantics_roundtrip_distinguishes_candidate_from_old_empty_task(tmp_path, initialized):
    from box_agent.skill_task import SkillTaskState

    runtime, tool, engine, history = setup(tmp_path)
    task = {"schema": 1, "task_id": "", "methods": [], "user_inputs": []}
    if initialized is not None:
        task["methods_initialized"] = initialized
    runtime.restore_task(SkillTaskState.restore(task).record())
    snapshot = runtime.resolve_reference("method")
    runtime.restore_records([{"name": "method", "sha256": snapshot.revision, "loadOrder": 1}])
    runtime.begin_turn()
    # Existing schema-1 snapshots already meant use/reference/release was
    # initialized, even when no methods remained.
    assert runtime.restoring_names == (("method",) if initialized is False else ())


@pytest.mark.asyncio
async def test_late_store_binding_restores_released_task_before_read_history(tmp_path):
    from box_agent.agent import Agent

    store = SessionLog.create(tmp_path / "sessions", session_id="late-bind", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        prepare(engine, tool, history)
        assert (await read(engine, tool, "method")).success
        assert (await read(engine, tool, "method", usage="release")).success
        assert (await read(engine, tool, "reference", usage="reference")).success
        fresh = SkillRuntime(runtime.loader)
        agent = Agent(object(), "system", [tool], skill_runtime=fresh, session_log=store,
                      enable_builtin_tools=False)
        fresh.begin_turn()
        context = DefaultContextEngine()
        context.configure_run(skill_engine=fresh, session_store=store)
        result = prepare(context, tool, [Message(role="user", content="New request.")])
        assert "KEEP_EXACT_METHOD" not in str(result.messages)
        assert "REFERENCE_ONLY" not in str(result.messages)
        assert agent.skill_runtime is fresh
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reference_lookup_then_restart_retains_request_for_later_adoption(tmp_path):
    from box_agent.agent import Agent

    store = SessionLog.create(tmp_path / "sessions", session_id="pending-task", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        store.append_unlogged_messages(history[1:], turn=1, step=None)
        store.flush()
        prepare(engine, tool, history)
        assert (await read(engine, tool, "reference", usage="reference")).success
        restored = Agent(object(), "system", [tool], session_log=store, enable_builtin_tools=False)
        restored.add_user_message("Use the detailed method.")
        context = DefaultContextEngine()
        context.configure_run(skill_engine=restored.skill_runtime, session_store=store)
        prepare(context, tool, restored.messages)
        assert (await read(context, tool, "method")).success
        compressed = [history[0], Message(role="user", source="runtime", content="Summary")]
        result = prepare(context, tool, compressed)
        assert "8 pages" in str(result.messages)
        assert "Use the detailed method." in str(result.messages)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_late_empty_store_binding_persists_existing_task_semantics(tmp_path):
    from box_agent.agent import Agent

    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    store = SessionLog.create(tmp_path / "sessions", session_id="late-empty", cwd=tmp_path)
    try:
        Agent(object(), "system", [tool], session_log=store, skill_runtime=runtime, enable_builtin_tools=False)
        assert (await read(engine, tool, "reference", usage="reference")).success
        restored = SkillRuntime(runtime.loader, session_log=store)
        restored.restore_records(store.replay().skills)
        restored.begin_turn()
        context = DefaultContextEngine()
        context.configure_run(skill_engine=restored, session_store=store)
        result = prepare(context, tool, [Message(role="user", content="Continue.")])
        assert "REFERENCE_ONLY" not in str(result.messages)
        assert history[-1].content in restored.task.user_inputs
    finally:
        store.close()


@pytest.mark.asyncio
async def test_replay_recovers_user_written_after_last_task_snapshot(tmp_path):
    from box_agent.agent import Agent

    store = SessionLog.create(tmp_path / "sessions", session_id="unobserved-user", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        store.append_unlogged_messages(history[1:], turn=1, step=None)
        prepare(engine, tool, history)
        assert (await read(engine, tool, "method")).success
        correction = Message(role="user", content="Do not include confidential rows.")
        store.append_unlogged_messages([*history[1:], correction], turn=2, step=None)
        store.flush()
        restored = Agent(object(), "system", [tool], session_log=store, enable_builtin_tools=False)
        assert correction.content in restored.skill_runtime.task.user_inputs
    finally:
        store.close()


@pytest.mark.asyncio
async def test_task_snapshot_and_surface_rewrites_preserve_real_input_occurrences(tmp_path):
    store = SessionLog.create(tmp_path / "sessions", session_id="input-identities", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        # The snapshot can precede the ordinary user event.
        prepare(engine, tool, history)
        assert (await read(engine, tool, "method")).success
        store.append_unlogged_messages(history[1:], turn=1, step=None)
        store.flush()
        assert store.replay().skill_task["user_inputs"] == [history[-1].content]
        repeat = Message(role="user", content=history[-1].content)
        store.append_unlogged_messages([*history[1:], repeat], turn=2, step=None)
        store.flush()
        assert store.replay().skill_task["user_inputs"] == [history[-1].content] * 2
        runtime.release_method("method")
        store.replace_surface([history[-1], repeat], turn=2, step=0)
        store.flush()
        assert store.replay().skill_task["user_inputs"] == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_user_correction_retries_after_recoverable_store_failure(tmp_path, monkeypatch):
    store = SessionLog.create(tmp_path / "sessions", session_id="retry-facts", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        prepare(engine, tool, history)
        assert (await read(engine, tool, "method")).success
        correction = "Never include the confidential rows."
        history.append(Message(role="user", content=correction))
        original_append = store.append
        fail_once = True

        def append(event, payload):
            nonlocal fail_once
            if event == "skill/change" and "task" in payload and fail_once:
                fail_once = False
                raise OSError("recoverable external store failure")
            return original_append(event, payload)

        monkeypatch.setattr(store, "append", append)
        with pytest.raises(OSError):
            prepare(engine, tool, history)
        prepare(engine, tool, history)
        assert correction in SkillRuntime(runtime.loader, session_log=store).task.user_inputs
        assert runtime.task.user_inputs.count(correction) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_first_reference_read_cannot_become_legacy_active_method(tmp_path, monkeypatch):
    from box_agent.skill_context import SkillReferenceContext

    store = SessionLog.create(tmp_path / "sessions", session_id="reference-failure", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        first_context = SkillReferenceContext(runtime)
        first_context.prepare_request(history, budget_chars=50000)
        original_append = store.append

        def append(event, payload):
            if event == "skill/change" and "task" in payload:
                raise OSError("task write unavailable")
            return original_append(event, payload)

        monkeypatch.setattr(store, "append", append)
        with pytest.raises(OSError):
            await tool.invoke({"skill_name": "reference", "usage": "reference"},
                              context=ToolInvocationContext(skill_reader=first_context.read))
        monkeypatch.setattr(store, "append", original_append)
        restored = SkillRuntime(runtime.loader, session_log=store)
        restored.restore_records(store.replay().skills)
        restored.begin_turn()
        context = DefaultContextEngine()
        context.configure_run(skill_engine=restored, session_store=store)
        assert "REFERENCE_ONLY" not in str(prepare(context, tool, history).messages)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_replacement_and_release_update_required_methods(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    assert (await read(engine, tool, "next", replace=["method"])).success
    rebuilt = prepare(engine, tool, history)
    assert "NEXT_METHOD" in str(rebuilt.messages)
    assert "KEEP_EXACT_METHOD" not in str(rebuilt.messages)
    assert (await read(engine, tool, "next", usage="release")).success
    assert "NEXT_METHOD" not in str(prepare(engine, tool, history).messages)


@pytest.mark.asyncio
async def test_failed_replacement_keeps_previous_method(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    assert not (await read(engine, tool, "missing", replace=["method"])).success
    assert "KEEP_EXACT_METHOD" in str(prepare(engine, tool, history).messages)


@pytest.mark.asyncio
async def test_changed_source_blocks_instead_of_silently_switching_revision(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    path = tmp_path / "method/SKILL.md"
    path.write_text(path.read_text().replace("KEEP_EXACT_METHOD", "CHANGED_METHOD"))
    rebuilt = prepare(engine, tool, history)
    assert rebuilt.blocked_reason and "changed" in rebuilt.blocked_reason.lower()
    assert "CHANGED_METHOD" not in str(rebuilt.messages)


@pytest.mark.asyncio
async def test_required_body_cannot_be_silently_omitted_when_budget_is_small(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    rebuilt = prepare(engine, tool, history, limit=800)
    assert rebuilt.blocked_reason
    assert rebuilt.budget_blocked


@pytest.mark.asyncio
async def test_selected_adopted_method_does_not_require_budget_for_a_second_copy(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    compressed = [history[0], Message(role="user", source="runtime", content="Summary")]
    limit = next(limit for limit in range(1500, 6000, 10)
                 if prepare(engine, tool, compressed, limit=limit).blocked_reason is None)
    runtime.select(["method"])
    projected = prepare(engine, tool, compressed, limit=limit)
    assert projected.blocked_reason is None
    assert "KEEP_EXACT_METHOD" in str(projected.messages)


@pytest.mark.asyncio
async def test_original_request_and_ordered_corrections_survive_compaction(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    history.extend([Message(role="user", content="Use blue."),
                    Message(role="user", content="Use red."),
                    Message(role="user", content="Use blue."),
                    Message(role="user", source="runtime", content="INTERNAL_RECOVERY_IS_NOT_A_REQUEST")])
    prepare(engine, tool, history)
    history[:] = [history[0], Message(role="user", source="runtime", content="Summary")]
    rebuilt = prepare(engine, tool, history)
    text = json.dumps([m.content for m in rebuilt.messages], ensure_ascii=False)
    assert "8 pages" in text
    assert text.count("Use blue.") == 2 and text.count("Use red.") == 1
    assert "INTERNAL_RECOVERY_IS_NOT_A_REQUEST" not in text


@pytest.mark.asyncio
async def test_restored_user_attachment_keeps_multimodal_shape_and_pixel_budget(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    block = {"type": "input_image", "media_type": "image/png", "data": "A" * 200000,
             "width": 64, "height": 64}
    history[-1] = Message(role="user", content=[{"type": "text", "text": "Use this chart."}, block])
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    compressed = [history[0], Message(role="user", source="runtime", content="Summary")]
    restored = prepare(engine, tool, compressed, limit=6000)
    assert restored.blocked_reason is None
    blocks = [item for message in restored.messages if isinstance(message.content, list) for item in message.content]
    assert block in blocks
    assert any(item.get("text") == "Use this chart." for item in blocks)


@pytest.mark.asyncio
async def test_adopted_task_restores_from_real_session_log(tmp_path):
    store = SessionLog.create(tmp_path / "sessions", session_id="method-task", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        prepare(engine, tool, history)
        assert (await read(engine, tool, "method")).success
        task = store.replay().skill_task
        assert task["methods"][0]["name"] == "method"
        restored = SkillRuntime(runtime.loader, session_log=store)
        other = DefaultContextEngine()
        other.configure_run(skill_engine=restored, session_store=store)
        compressed = [history[0], Message(role="user", source="runtime", content="Summary")]
        result = prepare(other, tool, compressed)
        assert "KEEP_EXACT_METHOD" in str(result.messages)
        assert "8 pages" in str(result.messages)
        assert restored.task.task_id == runtime.task.task_id
    finally:
        store.close()


@pytest.mark.asyncio
async def test_new_task_releases_previous_method_and_requirements(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    previous_id = runtime.task.task_id
    history.append(Message(role="user", content="Different task: summarize the dataset."))
    prepare(engine, tool, history)
    assert (await read(engine, tool, "next", new_task=True)).success
    history[:] = [history[0], Message(role="user", source="runtime", content="Summary")]
    result = prepare(engine, tool, history)
    assert "Different task" in str(result.messages)
    assert "8 pages" not in str(result.messages)
    assert "KEEP_EXACT_METHOD" not in str(result.messages)
    assert previous_id != runtime.task.task_id


@pytest.mark.asyncio
async def test_explicit_range_restores_required_text_only(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    lines = runtime.resolve_reference("method").prompt.splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if "SECOND_RULE" in line)
    assert (await read(engine, tool, "method", offset=start, limit=1)).success
    history[:] = [history[0], Message(role="user", source="runtime", content="Summary")]
    result = prepare(engine, tool, history)
    assert "SECOND_RULE" in str(result.messages)
    assert "KEEP_EXACT_METHOD" not in str(result.messages)


@pytest.mark.asyncio
async def test_repeated_pages_preserve_other_methods_and_merge_ranges(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method", limit=8)).success
    assert (await read(engine, tool, "next")).success
    assert (await read(engine, tool, "method", offset=8)).success
    result = prepare(engine, tool, history)
    assert "KEEP_EXACT_METHOD" in str(result.messages)
    assert "NEXT_METHOD" in str(result.messages)


def test_explicit_host_method_keeps_original_task_across_short_reply(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    runtime.select(["method"])
    first = prepare(engine, tool, history)
    assert first.on_committed
    first.on_committed()
    history.append(Message(role="user", content="Use the second option."))
    prepare(engine, tool, history)
    history[:] = [history[0], Message(role="user", source="runtime", content="Summary")]
    restored = prepare(engine, tool, history)
    assert "8 pages" in str(restored.messages)
    assert "Use the second option." in str(restored.messages)


@pytest.mark.asyncio
async def test_released_method_is_not_reactivated_by_session_read_history(tmp_path):
    store = SessionLog.create(tmp_path / "sessions", session_id="released-task", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        prepare(engine, tool, history)
        assert (await read(engine, tool, "method")).success
        assert (await read(engine, tool, "method", usage="release")).success
        restored = SkillRuntime(runtime.loader, session_log=store)
        restored.restore_records(store.replay().skills)
        restored.begin_turn()
        other = DefaultContextEngine()
        other.configure_run(skill_engine=restored, session_store=store)
        projection = prepare(other, tool, [history[0], Message(role="user", content="New task.")])
        assert "KEEP_EXACT_METHOD" not in str(projection.messages)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_method_replacement_retires_host_selection_too(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    runtime.select(["method"])
    prepare(engine, tool, history).on_committed()
    assert (await read(engine, tool, "next", replace=["method"])).success
    history[:] = [history[0], Message(role="user", source="runtime", content="Summary")]
    projection = prepare(engine, tool, history)
    assert "NEXT_METHOD" in str(projection.messages)
    assert "KEEP_EXACT_METHOD" not in str(projection.messages)


@pytest.mark.asyncio
async def test_reference_only_read_stays_inactive_after_restart(tmp_path):
    store = SessionLog.create(tmp_path / "sessions", session_id="reference-task", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        prepare(engine, tool, history)
        assert (await read(engine, tool, "reference", usage="reference")).success
        restored = SkillRuntime(runtime.loader, session_log=store)
        restored.restore_records(store.replay().skills)
        restored.begin_turn()
        other = DefaultContextEngine()
        other.configure_run(skill_engine=restored)
        assert "REFERENCE_ONLY" not in str(prepare(other, tool, history).messages)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_deleted_inactive_reference_does_not_prevent_task_restore(tmp_path):
    store = SessionLog.create(tmp_path / "sessions", session_id="deleted-reference", cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        prepare(engine, tool, history)
        assert (await read(engine, tool, "method")).success
        assert (await read(engine, tool, "reference", usage="reference")).success
        (tmp_path / "reference/SKILL.md").unlink()
        restored = SkillRuntime(runtime.loader, session_log=store)
        restored.restore_records(store.replay().skills)
        restored.begin_turn()
        other = DefaultContextEngine()
        other.configure_run(skill_engine=restored)
        result = prepare(other, tool, history)
        assert result.blocked_reason is None
        assert "KEEP_EXACT_METHOD" in str(result.messages)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_method_adopted_after_clarification_preserves_original_request(tmp_path):
    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    history.append(Message(role="user", content="Use the detailed method."))
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    history[:] = [history[0], Message(role="user", source="runtime", content="Summary")]
    result = prepare(engine, tool, history)
    assert "8 pages" in str(result.messages)
    assert "Use the detailed method." in str(result.messages)


@pytest.mark.asyncio
async def test_redacted_new_task_read_keeps_previous_method_and_user_scope(tmp_path):
    from box_agent.tools.engine.results import ToolResultPipelineInput, process_tool_result
    from box_agent.tool_result_storage import ToolResultStorage

    runtime, tool, engine, history = setup(tmp_path)
    prepare(engine, tool, history)
    assert (await read(engine, tool, "method")).success
    before = runtime.task.record()
    result = await tool.invoke(
        {"skill_name": "next", "new_task": True, "replace": ["method"]},
        context=ToolInvocationContext(parent_tool_call_id="next", skill_reader=engine.tool_reader),
    )
    assert result.success
    assert runtime.task.record() == before
    process_tool_result(ToolResultPipelineInput(
        messages=history, tool_call_id="next", tool_name="get_skill", arguments={},
        result=result, visible_content="REDACTED", visible_error=None,
        result_storage=ToolResultStorage(tmp_path), hook_text_modified=True,
    ))
    assert runtime.task.record() == before


def test_host_reference_adoption_can_retry_after_task_store_failure(tmp_path, monkeypatch):
    store = SessionLog.create(tmp_path / "sessions", session_id="host-retry", cwd=tmp_path)
    try:
        runtime = SkillRuntime(None, session_log=store)
        runtime.register_reference("host-method", "ORIGINAL_HOST_METHOD")
        runtime.adopt_method(runtime.resolve_reference("host-method"))
        append = store.append
        fail_once = True

        def flaky_append(event, payload, **kwargs):
            nonlocal fail_once
            if event == "skill/change" and "task" in payload and fail_once:
                fail_once = False
                raise OSError("temporary store failure")
            return append(event, payload, **kwargs)

        monkeypatch.setattr(store, "append", flaky_append)
        with pytest.raises(OSError, match="temporary store failure"):
            runtime.register_reference("host-method", "UPDATED_HOST_METHOD")
        runtime.register_reference("host-method", "UPDATED_HOST_METHOD")
        assert runtime.task.methods["host-method"].revision == runtime.resolve_reference("host-method").revision
        assert store.replay().skill_task == runtime.task.record()
    finally:
        store.close()


@pytest.mark.parametrize("usage,transition", [("use", "initial"), ("reference", "initial"),
                                               ("use", "replace"), ("use", "new_task")])
@pytest.mark.parametrize("rewritten", [False, True])
def test_committed_skill_adoption_survives_process_exit_before_callback(tmp_path, usage, transition, rewritten):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent('''
        import asyncio, os, sys
        from pathlib import Path
        from box_agent.schema import Message
        from box_agent.session_log import SessionLog
        from box_agent.skill_runtime import SkillRuntime
        from box_agent.skill_context import SkillReferenceContext
        from box_agent.tools.skill_loader import SkillLoader
        from box_agent.tools.engine.results import ToolResultPipelineInput, process_tool_result
        from box_agent.tool_result_storage import ToolResultStorage
        from box_agent.kernel.tool_messages import ToolMessageCommitter
        root, usage, transition = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
        for name in ['old', 'next']:
            path = root / 'skills' / name / 'SKILL.md'
            path.parent.mkdir(parents=True)
            path.write_text(f'---\\nname: {name}\\ndescription: method\\n---\\n' + 'EXACT_METHOD_RULE\\n' * 900)
        loader = SkillLoader(root / 'skills')
        loader.discover_skills()
        store = SessionLog.create(root / 'sessions', session_id='commit-exit', cwd=root)
        runtime = SkillRuntime(loader, session_log=store)
        history = [Message(role='user', content='Original report with illustrations')]
        runtime.observe_task_history(history)
        context = SkillReferenceContext(runtime)
        if transition != 'initial':
            assert context.read('old', usage='use').success
            history.append(Message(role='user', content='New report in Chinese'))
            runtime.observe_task_history(history)
        store.append_unlogged_messages(history, turn=1, step=1)
        store.flush()
        if sys.argv[4] == 'True':
            history[:] = [Message(role='user', source='runtime', content='Compressed task history')]
        result = SkillReferenceContext(runtime).read(
            'next', usage=usage, _defer_adoption=True,
            replace=['old'] if transition=='replace' else None,
            new_task=transition=='new_task')
        assert result.success
        committer = ToolMessageCommitter(history, store, 1)
        def commit_then_exit(message, event, step):
            committer.commit_result(message, event, step)
            os._exit(0)
        process_tool_result(ToolResultPipelineInput(
            messages=history, tool_call_id='read', tool_name='get_skill', arguments={},
            result=result, visible_content=result.content, visible_error=None,
            result_storage=ToolResultStorage(root), commit_result=commit_then_exit))
    ''')
    completed = subprocess.run([sys.executable, "-c", code, str(tmp_path), usage, transition, str(rewritten)],
                               cwd=Path(__file__).resolve().parents[1],
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    store = SessionLog.open(tmp_path / "sessions", session_id="commit-exit", cwd=tmp_path)
    try:
        projection = store.replay()
        assert "EXACT_METHOD_RULE" in projection.messages[-1].content
        from box_agent.skill_task import SkillTaskState
        task = SkillTaskState.restore(projection.skill_task)
        if usage == "reference":
            assert not task.methods
        else:
            assert set(task.methods) == {"next"}
            assert task.methods["next"].ranges
            assert task.user_inputs == (["New report in Chinese"] if transition == "new_task" else
                                        ["Original report with illustrations", "New report in Chinese"]
                                        if transition == "replace" else ["Original report with illustrations"])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_postcommit_checkpoint_cannot_restore_retired_method_on_continue(tmp_path, monkeypatch):
    from box_agent.kernel.tool_messages import ToolMessageCommitter
    from box_agent.tools.engine.results import ToolResultPipelineInput, process_tool_result
    from box_agent.tool_result_storage import ToolResultStorage

    store = SessionLog.create(tmp_path / 'sessions', session_id='committed-retry', cwd=tmp_path)
    try:
        runtime, tool, engine, history = setup(tmp_path, store=store)
        prepare(engine, tool, history)
        assert (await read(engine, tool, 'method')).success
        store.append_unlogged_messages(history[1:], turn=1, step=1)
        store.flush()
        result = await tool.invoke(
            {'skill_name': 'next', 'replace': ['method']},
            context=ToolInvocationContext(parent_tool_call_id='next', skill_reader=engine.tool_reader),
        )
        append, fail_once = store.append, True

        def flaky_append(event, payload, **kwargs):
            nonlocal fail_once
            if event == 'skill/change' and 'task' in payload and fail_once:
                fail_once = False
                raise OSError('checkpoint failed after tool commit')
            return append(event, payload, **kwargs)

        monkeypatch.setattr(store, 'append', flaky_append)
        committer = ToolMessageCommitter(history, store, 1)
        with pytest.raises(OSError, match='checkpoint failed after tool commit'):
            process_tool_result(ToolResultPipelineInput(
                messages=history, tool_call_id='next', tool_name='get_skill', arguments={},
                result=result, visible_content=result.content, visible_error=None,
                result_storage=ToolResultStorage(tmp_path), commit_result=committer.commit_result,
            ))
        assert set(runtime.task.methods) == {'next'}
        assert store.replay().skill_task == runtime.task.record()
        history.append(Message(role='user', content='Continue'))
        runtime.observe_task_history(history)
        assert store.replay().skill_task == runtime.task.record()
        assert set(runtime.task.methods) == {'next'}
    finally:
        store.close()
