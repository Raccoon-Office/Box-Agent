import copy
import json

import pytest

from box_agent.tools.completion_checks import CompletionChecks, CompletionContext
from box_agent.events import DoneEvent, ErrorEvent, StopReason, ToolCallResult
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.file.validate_table_tool import ValidateTableTool, _input_paths
from tests.test_table_validation import FIELDS, row, write_table


async def setup_gate(tmp_path, name="input.csv", rows=None):
    path = tmp_path / name
    write_table(path, rows or [row(1), row(2)])
    tool = ValidateTableTool(str(tmp_path), allow_full_access=False)
    messages = [Message(role="user", content=f"Process `{path}` completely")]
    gate = await tool.create_completion_gate(CompletionContext(tuple(messages), lambda: False))
    return tool, gate, path


@pytest.mark.asyncio
async def test_whole_source_receipt_is_required_and_matches_actual_counts(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    assert (await gate.check()).code == "TABLE_VALIDATION_MISSING"
    result = await tool.invoke({"path": str(path), "fields": FIELDS}, context=ToolInvocationContext(completion_gate=gate))
    assert result.success and result.raw_output["records"] == 2
    assert await gate.check() is None
    assert gate.receipts[path].source_sha256 == result.raw_output["source_sha256"]
    result.raw_output["valid"] = False
    result.raw_output["fields"]["amount"]["checked"] = 0
    assert await gate.check() is None  # public payload is an independent copy


@pytest.mark.asyncio
async def test_public_json_and_standalone_validator_cannot_credit_active_run(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    standalone = await tool.execute(str(path), FIELDS)
    standalone.raw_output["passed"] = True
    assert standalone.success
    assert (await gate.check()).code == "TABLE_VALIDATION_MISSING"


@pytest.mark.asyncio
async def test_all_attached_inputs_require_independent_receipts(tmp_path):
    tool, _, path = await setup_gate(tmp_path)
    other = tmp_path / "refunds.csv"
    write_table(other, [row(3)])
    gate = await tool.create_completion_gate(CompletionContext((Message(role="user", content=json.dumps([
        {"name": path.name, "path": str(path)}, {"path": str(other)},
    ])),), lambda: False))
    await tool.invoke({"path": str(path), "fields": FIELDS}, context=ToolInvocationContext(completion_gate=gate))
    assert str(other) in (await gate.check()).message


@pytest.mark.asyncio
async def test_diagnosis_completes_with_invalid_data_but_preserves_every_field_check(tmp_path):
    rows = [row(i) for i in range(2200)]
    rows[0] = row(0, amount="NaN")
    rows[2010] = row(2010, amount="-1.00", status="cancelled")
    rows += [row(0, revision="2"), row(2201, id="", status="bogus", date="2026-02-30", is_test="maybe")]
    tool, gate, path = await setup_gate(tmp_path, rows=rows)
    result = await tool.invoke(
        {"path": str(path), "fields": FIELDS, "purpose": "diagnosis"},
        context=ToolInvocationContext(completion_gate=gate),
    )
    assert result.success and result.error is None
    assert result.raw_output["validation_complete"] is True
    assert result.raw_output["data_valid"] is False and result.raw_output["valid"] is False
    assert result.raw_output["records"] == 2202
    assert result.raw_output["invalid_records"] == 3 and result.raw_output["error_count"] == 6
    assert all(stats["checked"] == 2202 for stats in result.raw_output["fields"].values())
    assert await gate.check() is None
    altered_fields = copy.deepcopy(FIELDS)
    altered_fields["status"]["values"].append("bogus")
    changed = await tool.invoke(
        {"path": str(path), "fields": altered_fields, "purpose": "diagnosis"},
        context=ToolInvocationContext(completion_gate=gate),
    )
    assert not changed.success and "TABLE_CONTRACT_CHANGED" in changed.error
    assert gate.receipts[path].error_count == 6


@pytest.mark.asyncio
async def test_computation_stays_blocked_and_cannot_switch_purpose_after_invalid_data(tmp_path):
    tool, gate, path = await setup_gate(tmp_path, rows=[row(1, amount="NaN")])
    context = ToolInvocationContext(completion_gate=gate)
    result = await tool.invoke({"path": str(path), "fields": FIELDS}, context=context)
    assert result.success and result.raw_output["validation_complete"] is True
    assert result.raw_output["purpose"] == "computation" and result.raw_output["data_valid"] is False
    assert (await gate.check()).code == "TABLE_VALIDATION_FAILED"
    changed = await tool.invoke({"path": str(path), "fields": FIELDS, "purpose": "diagnosis"}, context=context)
    assert not changed.success and "TABLE_PURPOSE_CHANGED" in changed.error
    result.raw_output["purpose"] = "diagnosis"
    result.raw_output["data_valid"] = True
    assert (await gate.check()).code == "TABLE_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_diagnosis_cannot_complete_with_a_partial_parse(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    with path.open("a") as stream:
        stream.write('"unterminated')
    # Capture this malformed source as the original input for a fresh run.
    gate = await tool.create_completion_gate(CompletionContext((Message(role="user", content=str(path)),), lambda: False))
    result = await tool.invoke(
        {"path": str(path), "fields": FIELDS, "purpose": "diagnosis"},
        context=ToolInvocationContext(completion_gate=gate),
    )
    assert not result.success
    assert result.raw_output["validation_complete"] is False
    assert result.raw_output["data_valid"] is None
    assert (await gate.check()).code == "TABLE_VALIDATION_INCOMPLETE"


@pytest.mark.asyncio
async def test_diagnosis_requires_all_original_inputs_and_inherits_the_locked_purpose(tmp_path):
    tool, _, path = await setup_gate(tmp_path, rows=[row(1, amount="NaN")])
    other = tmp_path / "other.csv"
    write_table(other, [row(2, amount="-1")])
    gate = await tool.create_completion_gate(CompletionContext((Message(role="user", content=f"{path} {other}"),), lambda: False))
    context = ToolInvocationContext(completion_gate=gate)
    first = await tool.invoke({"path": str(path), "fields": FIELDS, "purpose": "diagnosis"}, context=context)
    assert first.success and (await gate.check()).code == "TABLE_VALIDATION_MISSING"
    wrong_purpose = await tool.invoke({"path": str(other), "fields": FIELDS, "purpose": "computation"}, context=context)
    assert not wrong_purpose.success and "TABLE_PURPOSE_CHANGED" in wrong_purpose.error
    assert other not in gate.receipts
    second = await tool.invoke({"path": str(other), "fields": FIELDS}, context=context)
    assert second.success and second.raw_output["purpose"] == "diagnosis"
    assert second.raw_output["data_valid"] is False and await gate.check() is None
    write_table(other, [row(2)])
    assert (await gate.check()).code == "TABLE_SOURCE_UNVERIFIED"


@pytest.mark.asyncio
async def test_invalid_old_or_cancelled_fields_block_completion_and_schema_relaxation(tmp_path):
    tool, gate, path = await setup_gate(tmp_path, rows=[row(1, amount="NaN"), row(1, revision="2"), row(2, amount="-1", status="cancelled")])
    context = ToolInvocationContext(completion_gate=gate)
    result = await tool.invoke({"path": str(path), "fields": FIELDS}, context=context)
    assert result.success and result.raw_output["invalid_records"] == 2
    assert result.raw_output["data_valid"] is False
    from box_agent.tools.file_result_adapter import _tool_message_content_for_model
    history = _tool_message_content_for_model(
        tool_name=tool.name, arguments={}, result=result,
        visible_content=result.content, visible_error=result.error,
    )
    assert '"invalid_records": 2' in history and '"NaN"' in history
    assert (await gate.check()).code == "TABLE_VALIDATION_FAILED"
    relaxed = copy.deepcopy(FIELDS)
    relaxed["status"]["values"] = ["paid", "cancelled", "bogus"]
    result = await tool.invoke({"path": str(path), "fields": relaxed}, context=context)
    assert not result.success and "TABLE_CONTRACT_CHANGED" in result.error
    assert not (await gate.check()).repairable


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_scan", "after_scan"])
async def test_modified_source_cannot_use_old_or_new_receipt_to_complete(tmp_path, when):
    tool, gate, path = await setup_gate(tmp_path)
    context = ToolInvocationContext(completion_gate=gate)
    if when == "after_scan":
        assert (await tool.invoke({"path": str(path), "fields": FIELDS}, context=context)).success
    write_table(path, [row(2)])
    result = await tool.invoke({"path": str(path), "fields": FIELDS}, context=context)
    assert not result.success and "TABLE_SOURCE_CHANGED" in result.error
    assert not (await gate.check()).repairable


@pytest.mark.asyncio
async def test_edit_role_is_locked_and_cannot_adopt_an_already_modified_source(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    context = ToolInvocationContext(completion_gate=gate)
    assert (await tool.invoke({"path": str(path), "fields": FIELDS}, context=context)).success
    changed = await tool.invoke({"path": str(path), "fields": FIELDS, "source_mode": "edit_target"}, context=context)
    assert not changed.success and "TABLE_SOURCE_MODE_CHANGED" in changed.error
    tool, gate, path = await setup_gate(tmp_path, name="another.csv")
    write_table(path, [row(3)])
    result = await tool.invoke(
        {"path": str(path), "fields": FIELDS, "source_mode": "edit_target"},
        context=ToolInvocationContext(completion_gate=gate),
    )
    assert not result.success and "TABLE_SOURCE_CHANGED" in result.error
    assert not gate.receipts and not (await gate.check()).repairable


@pytest.mark.asyncio
async def test_edit_target_retains_original_errors_and_requires_a_valid_final_version(tmp_path):
    tool, gate, path = await setup_gate(tmp_path, rows=[row(1, amount="NaN")])
    context = ToolInvocationContext(completion_gate=gate)
    original = await tool.invoke(
        {"path": str(path), "fields": FIELDS, "source_mode": "edit_target", "purpose": "diagnosis"},
        context=context,
    )
    assert original.success and not original.raw_output["data_valid"]
    assert (await gate.check()).code == "TABLE_VALIDATION_FAILED"
    write_table(path, [row(1)])
    issue = await gate.check()
    assert issue.code == "TABLE_EDIT_VALIDATION_MISSING" and issue.repairable
    final = await tool.invoke({"path": str(path), "fields": FIELDS}, context=context)
    assert final.success and final.raw_output["source_mode"] == "edit_target"
    assert await gate.check() is None
    assert gate.original_receipts[path].source_sha256 == original.raw_output["source_sha256"]
    assert not gate.original_receipts[path].valid and gate.receipts[path].valid
    assert gate.receipts[path].source_sha256 != gate.original_receipts[path].source_sha256
    write_table(path, [row(2)])
    assert (await gate.check()).code == "TABLE_EDIT_VALIDATION_MISSING"


@pytest.mark.asyncio
async def test_edit_target_does_not_relax_other_inputs_or_allow_partial_final_scan(tmp_path):
    tool, _, path = await setup_gate(tmp_path)
    other = tmp_path / "source.csv"
    write_table(other, [row(3)])
    gate = await tool.create_completion_gate(CompletionContext((Message(role="user", content=f"Edit {path} using {other}"),), lambda: False))
    context = ToolInvocationContext(completion_gate=gate)
    for source, mode in [(path, "edit_target"), (other, "input")]:
        assert (await tool.invoke({"path": str(source), "fields": FIELDS, "source_mode": mode}, context=context)).success
    with path.open("a") as stream:
        stream.write('"unterminated')
    result = await tool.invoke({"path": str(path), "fields": FIELDS}, context=context)
    assert not result.success and (await gate.check()).code == "TABLE_VALIDATION_INCOMPLETE"
    write_table(path, [row(2)])
    assert (await tool.invoke({"path": str(path), "fields": FIELDS}, context=context)).success
    write_table(other, [row(4)])
    assert (await gate.check()).code == "TABLE_SOURCE_UNVERIFIED"


@pytest.mark.asyncio
async def test_partial_contract_or_sample_copy_cannot_satisfy_original_input(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    context = ToolInvocationContext(completion_gate=gate)
    result = await tool.invoke({"path": str(path), "fields": {"amount": FIELDS["amount"]}}, context=context)
    assert not result.success
    sample = tmp_path / "sample.csv"
    write_table(sample, [row(1)])
    assert (await tool.invoke({"path": str(sample), "fields": FIELDS}, context=context)).success
    assert str(path) in (await gate.check()).message


@pytest.mark.asyncio
async def test_validating_generated_output_does_not_freeze_it_as_original_input(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    context = ToolInvocationContext(completion_gate=gate)
    assert (await tool.invoke({"path": str(path), "fields": FIELDS}, context=context)).success
    output = tmp_path / "output.csv"
    write_table(output, [row(1)])
    assert (await tool.invoke({"path": str(output), "fields": FIELDS}, context=context)).success
    write_table(output, [row(2)])
    assert set(gate.sources) == {path} and await gate.check() is None


@pytest.mark.asyncio
async def test_permission_denial_does_not_read_or_produce_receipt(tmp_path, monkeypatch):
    import box_agent.tools.file.validate_table_tool as module
    path = tmp_path / "secret.csv"
    write_table(path, [row(1)])
    tool = ValidateTableTool(str(tmp_path / "scope"), allow_full_access=False)
    def forbidden(*args, **kwargs):
        pytest.fail("permission-denied content must not be opened")
    monkeypatch.setattr(module, "fingerprint", forbidden)
    monkeypatch.setattr(module, "scan_table", forbidden)
    gate = await tool.create_completion_gate(CompletionContext((Message(role="user", content=str(path)),), lambda: False))
    result = await tool.invoke({"path": str(path), "fields": FIELDS}, context=ToolInvocationContext(completion_gate=gate))
    assert not result.success and not gate.receipts
    assert (await gate.check()).code == "TABLE_SOURCE_UNVERIFIED"


def test_explicit_paths_support_spaces_links_structured_content_and_ignore_output_names(tmp_path):
    path = tmp_path / "输入 表.csv"
    write_table(path, [row(1)])
    tool = ValidateTableTool(str(tmp_path))
    for content in [f'"{path}"', f"[input]({path})", [{"type": "resource", "resource": {"uri": path.as_uri()}}]]:
        assert _input_paths(content, tool) == {path}
    assert _input_paths("save new-output.csv; https://example.com/a.csv", tool) == set()


@pytest.mark.parametrize("reference", [
    '"customer data.csv"', "`customer data.csv`", "<customer data.csv>",
    "[input](customer data.csv)", "[data.csv](customer data.csv)",
])
def test_quoted_input_does_not_require_an_unrelated_trailing_filename(tmp_path, reference):
    requested = tmp_path / "customer data.csv"
    unrelated = tmp_path / "data.csv"
    write_table(requested, [row(1)])
    write_table(unrelated, [row(2)])
    tool = ValidateTableTool(str(tmp_path))
    assert _input_paths(f"Analyze {reference}.", tool) == {requested}
    assert _input_paths(f"Analyze {reference} and data.csv.", tool) == {requested, unrelated}


@pytest.mark.asyncio
async def test_only_current_user_inputs_activate_gate_and_previous_receipts_are_not_replayed(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    await tool.invoke({"path": str(path), "fields": FIELDS}, context=ToolInvocationContext(completion_gate=gate))
    current = Message(role="user", content=f"Process {path}")
    fresh = await tool.create_completion_gate(CompletionContext((current,), lambda: False))
    assert (await fresh.check()).code == "TABLE_VALIDATION_MISSING"
    unrelated = (current, Message(role="user", content="Hello"), Message(role="user", source="runtime", content=str(path)))
    assert await tool.create_completion_gate(CompletionContext(unrelated, lambda: False)) is None


@pytest.mark.asyncio
async def test_cancelled_scan_cannot_credit_completion(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    gate.context = CompletionContext((), lambda: True)
    result = await tool.invoke({"path": str(path), "fields": FIELDS}, context=ToolInvocationContext(completion_gate=gate))
    assert not result.success and not gate.receipts


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose,invalid,completed", [
    ("computation", False, True), ("computation", True, False), ("diagnosis", True, True),
])
async def test_session_log_separates_completed_validation_data_quality_and_completion(tmp_path, purpose, invalid, completed):
    from box_agent.session_log import SessionLog
    tool, _, path = await setup_gate(tmp_path, rows=[row(1, amount="NaN" if invalid else "1.25")])
    log = SessionLog.create(tmp_path / "logs", session_id="audit", cwd=tmp_path)
    try:
        checks = await CompletionChecks.create({tool.name: tool}, CompletionContext(
            (Message(role="user", content=str(path)),), lambda: False, log, "turn-16",
        ))
        result = await tool.invoke(
            {"path": str(path), "fields": FIELDS, "purpose": purpose},
            context=ToolInvocationContext(completion_gate=checks.gates[tool.name]),
        )
        assert result.success
        assert bool(await checks.check()) is not completed
        purpose_event = next(e["data"] for e in log.events if e["type"] == "table.purpose")
        receipt = next(e["data"] for e in log.events if e["type"] == "table.validation")
        completion = next(e["data"] for e in log.events if e["type"] == "completion.check")
        assert purpose_event == {"purpose": purpose, "turn_id": "turn-16"}
        assert receipt["records"] == 1 and receipt["complete"] and receipt["validation_complete"]
        assert receipt["data_valid"] is not invalid and receipt["valid"] is not invalid
        assert receipt["purpose"] == purpose and completion["passed"] is completed
        assert receipt["turn_id"] == "turn-16" and len(receipt["source_sha256"]) == 64
        assert receipt["contract"]["fields"]["amount"]["minimum"] == "0"
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ["memory", "log", "reopen"])
async def test_inactive_completion_checks_preserve_receipts_and_ignore_new_inputs(tmp_path, monkeypatch, storage):
    from unittest.mock import AsyncMock
    from box_agent.session_log import SessionLog

    tool, _, path = await setup_gate(tmp_path)
    log = SessionLog.create(tmp_path / "logs", session_id="inactive", cwd=tmp_path) if storage != "memory" else None
    messages = [Message(role="user", content=f"Process {path}")]
    context = CompletionContext(tuple(messages), lambda: False, log, "original", messages)
    try:
        checks = await CompletionChecks.create({tool.name: tool}, context)
        gate = checks.gates[tool.name]
        assert (await tool.invoke(
            {"path": str(path), "fields": FIELDS}, context=ToolInvocationContext(completion_gate=gate),
        )).success
        checks.repairs = 1
        checks.suspend()
        checkpoint = gate.snapshot()
        if storage == "reopen":
            log.close()
            log = SessionLog.open(tmp_path / "logs", session_id="inactive", cwd=tmp_path)
            tool = ValidateTableTool(str(tmp_path))
        original_events = copy.deepcopy(log.events) if log is not None else None
        other = tmp_path / "new.csv"
        write_table(other, [row(3)])
        messages.append(Message(role="user", content=f"Summarize {other}"))
        context = CompletionContext(tuple(messages), lambda: False, log, "summary", messages)
        factory = AsyncMock(wraps=tool.create_completion_gate)
        restore = AsyncMock(wraps=tool.restore_completion_gate)
        monkeypatch.setattr(tool, "create_completion_gate", factory)
        monkeypatch.setattr(tool, "restore_completion_gate", restore)
        inactive = await CompletionChecks.create({tool.name: tool}, context, enabled=False)
        await inactive.include_inputs({tool.name: tool}, context)
        assert await inactive.check() == []
        inactive.suspend()
        inactive.close()
        factory.assert_not_awaited()
        restore.assert_not_awaited()
        if log is not None:
            assert log.events == original_events

        # Resuming the actual task retains its receipts and repair budget.
        messages.append(Message(role="user", content="Continue the original task"))
        resumed = await CompletionChecks.create({tool.name: tool}, CompletionContext(
            tuple(messages), lambda: False, log, "resume", messages,
        ))
        assert resumed.repairs == 1
        assert resumed.gates[tool.name].snapshot() == checkpoint
        assert await resumed.check() == []
    finally:
        if log is not None:
            log.close()


@pytest.mark.asyncio
async def test_zero_tool_allowance_keeps_completion_checks_enabled(tmp_path, monkeypatch):
    from dataclasses import replace
    from box_agent.agent import Agent
    from box_agent.turn_continuation import TurnContinuationController
    from unittest.mock import AsyncMock

    monkeypatch.setattr(TurnContinuationController, "evaluate", AsyncMock(return_value=None))
    tool, _, path = await setup_gate(tmp_path)
    agent = Agent(
        llm_client=ScriptedLLM([LLMResponse(content="Done", finish_reason="stop")] * 3),
        system_prompt="system", tools=[tool], workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False, max_steps=3,
    )
    agent.add_user_message(f"Process {path}")
    events = [event async for event in agent.run_events(
        options=replace(agent.default_run_options(), max_tool_calls=0),
    )]
    assert next(e for e in events if isinstance(e, DoneEvent)).stop_reason == StopReason.ERROR
    assert any(isinstance(e, ErrorEvent) and "TABLE_VALIDATION_MISSING" in e.message for e in events)


class ScriptedLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self, responses):
        self.responses = iter(responses)

    async def generate_stream(self, *args, **kwargs):
        response = next(self.responses)
        if response.content:
            yield StreamEvent(type="text", delta=response.content)
        yield StreamEvent(type="finish", finish_reason=response.finish_reason, tool_calls=response.tool_calls)


def scripted_call(name, arguments, call_id):
    return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
        id=call_id, type="function", function=FunctionCall(name=name, arguments=arguments),
    )])


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ["memory", "log", "reopen"])
@pytest.mark.parametrize("state", ["missing", "invalid", "valid", "diagnosis", "changed"])
async def test_agent_resumes_original_table_requirements_after_user_interaction(
    tmp_path, monkeypatch, storage, state,
):
    from box_agent.agent import Agent
    from box_agent.session_log import SessionLog
    from box_agent.tools.request_user_input_tool import RequestUserInputTool
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None

    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    tool, _, path = await setup_gate(tmp_path, rows=[row(1, amount="-1" if state in {"invalid", "diagnosis"} else "1")])
    log = SessionLog.create(tmp_path / "logs", session_id="resume", cwd=tmp_path) if storage != "memory" else None
    purpose = "diagnosis" if state == "diagnosis" else "computation"
    before = []
    if state != "missing":
        before.append(scripted_call(tool.name, {"path": str(path), "fields": FIELDS, "purpose": purpose,
                                              "profile": "transaction_v1"}, "original"))
    before.append(scripted_call("request_user_input", {"question": "Continue?", "missing_fields": ["decision"]}, "ask"))
    after = []
    if state == "invalid":
        after.append(scripted_call(tool.name, {"path": str(path), "fields": FIELDS, "purpose": "diagnosis"}, "downgrade"))
    after += [LLMResponse(content="Done", finish_reason="stop")] * 5

    def agent_with(tool, responses):
        return Agent(llm_client=ScriptedLLM(responses), system_prompt="system", tools=[tool, RequestUserInputTool()],
                     workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, session_log=log, max_steps=6)

    agent = agent_with(tool, before + after)
    agent.add_user_message(f"Process {path} completely")
    try:
        first = [event async for event in agent.run_events()]
        assert next(e for e in first if isinstance(e, DoneEvent)).stop_reason == StopReason.WAITING_FOR_USER
        if state == "changed":
            write_table(path, [row(2)])
        if storage == "reopen":
            log.close()
            log = SessionLog.open(tmp_path / "logs", session_id="resume", cwd=tmp_path)
            agent = agent_with(ValidateTableTool(str(tmp_path)), after)
        agent.add_user_message("好的，继续")
        resumed = [event async for event in agent.run_events()]
        assert next(e for e in resumed if isinstance(e, DoneEvent)).stop_reason == (
            StopReason.END_TURN if state in {"valid", "diagnosis"} else StopReason.ERROR
        )
        if state == "invalid":
            result = next(e for e in resumed if isinstance(e, ToolCallResult))
            assert not result.success and "TABLE_PURPOSE_CHANGED" in result.error
        if log is not None:
            assert any(e["type"] == "completion.paused" for e in log.events)
            assert any(e["type"] == "completion.closed" for e in log.events)
        # The completed/failed task must not contaminate a later independent turn.
        agent.add_user_message("Hello")
        independent = [event async for event in agent.run_events()]
        assert next(e for e in independent if isinstance(e, DoneEvent)).stop_reason == StopReason.END_TURN
    finally:
        if log is not None:
            log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,retarget", [
    ("edit_target", None), ("replace_target", None), ("delete_target", None),
    ("move_target", None), ("move_target", False), ("move_target", True),
])
async def test_reopened_session_retains_planned_file_operations_and_final_schema(tmp_path, monkeypatch, mode, retarget):
    from box_agent.agent import Agent
    from box_agent.session_log import SessionLog
    from box_agent.tools.request_user_input_tool import RequestUserInputTool
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None

    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    path = tmp_path / "table.csv"
    path.write_text("old\nvalue\n")
    destination = tmp_path / "moved.csv"
    if retarget is not None:
        (tmp_path / "archive").mkdir()
        (tmp_path / "current").symlink_to(tmp_path / "archive", target_is_directory=True)
        destination = tmp_path / "current" / "moved.csv"
    tool = ValidateTableTool(str(tmp_path))
    arguments = {"path": str(path), "source_mode": mode}
    if mode == "edit_target":
        arguments.update(fields={"old": {"type": "text"}}, output_fields={"new": {"type": "text"}})
    elif mode == "replace_target":
        arguments["fields"] = {"new": {"type": "text"}}
    elif mode == "move_target":
        arguments.update(fields={"old": {"type": "text"}}, destination_path=str(destination))
    log = SessionLog.create(tmp_path / "logs", session_id="operation", cwd=tmp_path)
    first_model = ScriptedLLM([
        scripted_call(tool.name, arguments, "prepare"),
        scripted_call("request_user_input", {"question": "Apply the requested change?", "missing_fields": ["decision"]}, "ask"),
    ])
    agent = Agent(llm_client=first_model, system_prompt="system", tools=[tool, RequestUserInputTool()],
                  workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, session_log=log)
    agent.add_user_message(f"{mode} {path}")
    first = [event async for event in agent.run_events()]
    assert next(e for e in first if isinstance(e, DoneEvent)).stop_reason == StopReason.WAITING_FOR_USER
    log.close()
    if mode == "delete_target":
        path.unlink()
    elif mode == "move_target":
        path.rename(destination)
        if retarget:
            (tmp_path / "other").mkdir()
            (tmp_path / "other" / "moved.csv").write_text("old\nunvalidated\n")
            destination.parent.unlink()
            destination.parent.symlink_to(tmp_path / "other", target_is_directory=True)
    else:
        path.write_text("new\nvalue\n")
    log = SessionLog.open(tmp_path / "logs", session_id="operation", cwd=tmp_path)
    responses = [] if mode in {"delete_target", "move_target"} else [scripted_call(tool.name, {"path": str(path)}, "final")]
    responses.extend([LLMResponse(content="Done", finish_reason="stop")] * 4)
    try:
        resumed_agent = Agent(llm_client=ScriptedLLM(responses), system_prompt="system", tools=[ValidateTableTool(str(tmp_path))],
                              workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, session_log=log, max_steps=6)
        resumed_agent.add_user_message(
            f"Moved to {destination}, continue." if mode == "move_target" and not retarget
            else "Applied the change, continue."
        )
        resumed = [event async for event in resumed_agent.run_events()]
        assert next(e for e in resumed if isinstance(e, DoneEvent)).stop_reason == (
            StopReason.ERROR if retarget else StopReason.END_TURN
        )
        if retarget:
            assert any(isinstance(e, ErrorEvent) and e.error_code == "COMPLETION_GATE_UNSATISFIED" for e in resumed)
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("declared,remove", [(True, True), (True, False), (False, True)])
async def test_declared_file_deletion_completes_only_when_the_file_is_actually_gone(
    tmp_path, monkeypatch, posix_shell, declared, remove,
):
    import shlex
    from box_agent.tools.bash_tool import BashTool
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None

    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    path = tmp_path / "obsolete.csv"
    path.write_text('not even a parseable CSV\n"unterminated')
    tool = ValidateTableTool(str(tmp_path))
    bash = BashTool(workspace_dir=str(tmp_path))
    approvals = []

    class Approver:
        async def negotiate(self, request):
            approvals.append(request)
            if "chat_reference_delete" in request:
                assert request["chat_reference_delete"]["paths"] == [str(path)]
            return True
    responses = [scripted_call(tool.name, {"path": str(path), "source_mode": "delete_target"}, "prepare")] if declared else []
    if remove:
        responses.append(scripted_call(bash.name, {"command": f"rm -- {shlex.quote(str(path))}"}, "delete"))
    responses += [LLMResponse(content="Done", finish_reason="stop")] * 4
    events = [event async for event in run_agent_loop(
        llm=ScriptedLLM(responses), messages=[Message(role="user", content=f"Delete {path}")],
        tools={tool.name: tool, bash.name: bash}, workspace_dir=str(tmp_path), max_steps=7,
        permission_negotiator=Approver(),
    )]
    if remove:
        deletion = next(e for e in events if isinstance(e, ToolCallResult) and e.tool_name == bash.name)
        assert deletion.success is declared
        if declared:
            assert any("chat_reference_delete" in request for request in approvals)
        else:
            assert "TABLE_VALIDATION_REQUIRED_BEFORE_EXECUTION" in deletion.error
            assert not approvals
    assert next(e for e in events if isinstance(e, DoneEvent)).stop_reason == (
        StopReason.END_TURN if declared and remove else StopReason.ERROR
    )
    assert path.exists() is not (declared and remove)
    if declared:
        result = next(e for e in events if isinstance(e, ToolCallResult) and e.tool_name == tool.name)
        assert result.success and result.raw_output["operation_ready"]
        assert result.raw_output["validation_complete"] is None and result.raw_output["data_valid"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
@pytest.mark.parametrize("subdirectory", [False, True])
async def test_validated_table_rename_completes_after_external_move(
    tmp_path, monkeypatch, suffix, delimiter, subdirectory,
):
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None

    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    path = tmp_path / f"sales.{suffix}"
    path.write_text(f"id{delimiter}amount\n1{delimiter}12.50\n2{delimiter}20.00\n")
    destination = tmp_path / "archive" / f"sales history.{suffix}" if subdirectory else tmp_path / f"archive.{suffix}"
    destination.parent.mkdir(exist_ok=True)
    before = path.read_bytes()
    tool = ValidateTableTool(str(tmp_path))

    class MovingLLM(ScriptedLLM):
        calls = 0

        async def generate_stream(self, *args, **kwargs):
            if self.calls == 1:
                # The user completes the filesystem move after the original
                # scan. This tests completion, without bypassing writer policy.
                path.rename(destination)
            self.calls += 1
            async for event in super().generate_stream(*args, **kwargs):
                yield event

    responses = [
        scripted_call(tool.name, {
            "path": path.name, "source_mode": "move_target",
            "destination_path": str(destination.relative_to(tmp_path)),
            "fields": {"id": {"type": "integer"}, "amount": {"type": "decimal"}},
        }, "validate"),
    ] + [LLMResponse(content="Done", finish_reason="stop")] * 4
    events = [event async for event in run_agent_loop(
        llm=MovingLLM(responses), messages=[Message(role="user", content=f"Validate {path}; I will rename it to {destination}.")],
        tools={tool.name: tool}, workspace_dir=str(tmp_path), max_steps=6,
    )]
    assert not path.exists() and destination.read_bytes() == before
    assert next(e for e in events if isinstance(e, DoneEvent)).stop_reason == StopReason.END_TURN
    scan = next(e for e in events if isinstance(e, ToolCallResult) and e.tool_name == tool.name)
    assert scan.success and scan.raw_output["validation_complete"] and scan.raw_output["data_valid"]
    assert scan.raw_output["records"] == 2
    assert all(field["checked"] == 2 for field in scan.raw_output["fields"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("state,code", [
    ("unchanged", "TABLE_MOVE_PENDING"), ("copied", "TABLE_MOVE_PENDING"),
    ("missing_target", "TABLE_MOVE_UNVERIFIED"), ("changed_content", "TABLE_SOURCE_CHANGED"),
    ("target_link", "TABLE_MOVE_UNVERIFIED"), ("source_link", "TABLE_MOVE_PENDING"),
    ("parent_link", "TABLE_MOVE_UNVERIFIED"),
])
async def test_move_completion_rejects_missing_or_changed_file_state(tmp_path, state, code):
    tool, gate, path = await setup_gate(tmp_path)
    destination = tmp_path / "archive" / "moved.csv"
    destination.parent.mkdir()
    context = ToolInvocationContext(completion_gate=gate)
    result = await tool.invoke({
        "path": str(path), "fields": FIELDS, "source_mode": "move_target", "destination_path": str(destination),
    }, context=context)
    assert result.success and path.exists() and not destination.exists()
    if state == "copied":
        destination.write_bytes(path.read_bytes())
    elif state != "unchanged":
        path.rename(destination)
        if state == "missing_target":
            destination.unlink()
        elif state == "changed_content":
            write_table(destination, [row(1), row(2, amount="99.00")])
        elif state == "target_link":
            destination.rename(tmp_path / "real.csv")
            destination.symlink_to(tmp_path / "real.csv")
        elif state == "source_link":
            path.symlink_to(tmp_path / "missing.csv")
        elif state == "parent_link":
            destination.parent.rename(tmp_path / "relocated")
            destination.parent.symlink_to(tmp_path / "relocated", target_is_directory=True)
    assert (await gate.check()).code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("validation", ["missing", "partial_contract", "partial_parse", "invalid", "diagnosis"])
async def test_move_retains_full_scan_and_data_validity_requirements(tmp_path, validation):
    rows = [row(1, amount="NaN"), row(2)] if validation in {"invalid", "diagnosis"} else None
    tool, _, path = await setup_gate(tmp_path, rows=rows)
    if validation == "partial_parse":
        with path.open("a") as stream:
            stream.write('"unterminated')
    gate = await tool.create_completion_gate(CompletionContext((Message(role="user", content=str(path)),), lambda: False))
    destination = tmp_path / "moved.csv"
    if validation != "missing":
        fields = {"amount": FIELDS["amount"]} if validation == "partial_contract" else FIELDS
        result = await tool.invoke({
            "path": str(path), "fields": fields, "source_mode": "move_target", "destination_path": str(destination),
            "purpose": "diagnosis" if validation == "diagnosis" else "computation",
        }, context=ToolInvocationContext(completion_gate=gate))
        assert result.success == (validation in {"invalid", "diagnosis"})
        if validation in {"invalid", "diagnosis"}:
            assert not result.raw_output["data_valid"]
            assert all(field["checked"] == 2 for field in result.raw_output["fields"].values())
    path.rename(destination)
    issue = await gate.check()
    assert (issue is None) == (validation == "diagnosis")
    if validation == "invalid":
        assert issue.code == "TABLE_VALIDATION_FAILED"
    elif validation == "partial_parse":
        assert issue.code == "TABLE_VALIDATION_INCOMPLETE"


@pytest.mark.asyncio
@pytest.mark.parametrize("destination_kind", ["missing_argument", "same_path", "existing", "dangling_link"])
async def test_move_requires_an_absent_distinct_destination(tmp_path, destination_kind):
    tool, gate, path = await setup_gate(tmp_path)
    destination = tmp_path / "target.csv"
    if destination_kind == "existing":
        destination.write_text("do not overwrite")
    elif destination_kind == "dangling_link":
        destination.symlink_to(tmp_path / "missing.csv")
    arguments = {"path": str(path), "fields": FIELDS, "source_mode": "move_target"}
    if destination_kind != "missing_argument":
        arguments["destination_path"] = str(path if destination_kind == "same_path" else destination)
    result = await tool.invoke(arguments, context=ToolInvocationContext(completion_gate=gate))
    assert not result.success and path not in gate.contracts and path not in gate.source_modes
    assert path.is_file()
    if destination_kind == "existing":
        assert destination.read_text() == "do not overwrite"


@pytest.mark.asyncio
async def test_move_destination_and_role_stay_locked_and_other_inputs_stay_required(tmp_path):
    tool, _, path = await setup_gate(tmp_path)
    other = tmp_path / "other.csv"
    write_table(other, [row(3)])
    gate = await tool.create_completion_gate(CompletionContext((Message(role="user", content=f"{path} {other}"),), lambda: False))
    destination = tmp_path / "moved.csv"
    context = ToolInvocationContext(completion_gate=gate)
    arguments = {"path": str(path), "fields": FIELDS, "source_mode": "move_target", "destination_path": str(destination)}
    assert (await tool.invoke(arguments, context=context)).success
    assert (await tool.invoke({"path": str(path)}, context=context)).success
    changed = await tool.invoke({**arguments, "destination_path": str(tmp_path / "different.csv")}, context=context)
    assert not changed.success and "TABLE_CONTRACT_CHANGED" in changed.error
    changed = await tool.invoke({"path": str(path), "source_mode": "delete_target"}, context=context)
    assert not changed.success and "TABLE_SOURCE_MODE_CHANGED" in changed.error
    path.rename(destination)
    await gate.include_inputs(CompletionContext((Message(role="user", content=f"Continue with {destination} and {other}"),), lambda: False))
    assert set(gate.sources) == {path, other}
    assert (await gate.check()).code == "TABLE_VALIDATION_MISSING"
    assert (await tool.invoke({"path": str(other), "fields": FIELDS}, context=context)).success
    assert await gate.check() is None
    write_table(destination, [row(5)])
    assert (await gate.check()).code == "TABLE_SOURCE_CHANGED"


@pytest.mark.asyncio
async def test_move_checks_destination_read_permission_before_opening_it(tmp_path, monkeypatch):
    import box_agent.tools.file.validate_table_tool as module
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool, gate, path = await setup_gate(workspace)
    destination = tmp_path / "outside.csv"
    result = await tool.invoke({
        "path": str(path), "fields": FIELDS, "source_mode": "move_target", "destination_path": str(destination),
    }, context=ToolInvocationContext(completion_gate=gate))
    assert result.success
    path.rename(destination)

    def forbidden(*args, **kwargs):
        pytest.fail("permission-denied destination must not be opened")

    monkeypatch.setattr(module, "fingerprint", forbidden)
    assert (await gate.check()).code == "TABLE_MOVE_UNVERIFIED"


@pytest.mark.asyncio
@pytest.mark.parametrize("retarget_during_hash", [False, True])
async def test_move_rejects_retargeted_declared_parent_link(tmp_path, monkeypatch, retarget_during_hash):
    import box_agent.tools.file.validate_table_tool as module

    tool, gate, path = await setup_gate(tmp_path)
    (tmp_path / "archive").mkdir()
    (tmp_path / "other").mkdir()
    link = tmp_path / "current"
    link.symlink_to(tmp_path / "archive", target_is_directory=True)
    destination = link / "moved.csv"
    context = ToolInvocationContext(completion_gate=gate)
    result = await tool.invoke({
        "path": str(path), "fields": FIELDS, "source_mode": "move_target",
        "destination_path": "current/moved.csv",
    }, context=context)
    assert result.success
    rebound = await tool.invoke({
        "path": str(path), "destination_path": str(tmp_path / "archive" / "moved.csv"),
    }, context=context)
    assert not rebound.success and "TABLE_CONTRACT_CHANGED" in rebound.error
    assert (await tool.invoke({"path": str(path)}, context=context)).success
    path.rename(destination)
    assert await gate.check() is None
    write_table(tmp_path / "other" / "moved.csv", [row(99)])

    def retarget():
        link.unlink()
        link.symlink_to(tmp_path / "other", target_is_directory=True)

    if retarget_during_hash:
        original_fingerprint = module.fingerprint

        def fingerprint_then_retarget(*args, **kwargs):
            result = original_fingerprint(*args, **kwargs)
            retarget()
            return result

        monkeypatch.setattr(module, "fingerprint", fingerprint_then_retarget)
    else:
        retarget()
    assert (await gate.check()).code == "TABLE_MOVE_UNVERIFIED"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["edit_target", "replace_target"])
@pytest.mark.parametrize("verify_final", [False, True])
async def test_schema_edit_and_replacement_require_the_predeclared_final_contract(
    tmp_path, monkeypatch, mode, verify_final,
):
    from box_agent.tools.file_tools import WriteTool
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None

    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    path = tmp_path / "change.csv"
    path.write_text("old_col,value\nA,1\nB,2\n" if mode == "edit_target" else 'bad old file\n"unterminated')
    fields = {"old_col": {"type": "text"}, "value": {"type": "integer"}}
    output_fields = {"new_col": {"type": "text"}, "value": {"type": "integer"}}
    final = "new_col,value\nA,1\nB,2\n"
    tool, writer = ValidateTableTool(str(tmp_path)), WriteTool(workspace_dir=str(tmp_path))
    arguments = {"path": str(path), "source_mode": mode, "fields": fields if mode == "edit_target" else output_fields}
    if mode == "edit_target":
        arguments["output_fields"] = output_fields
    responses = [scripted_call(tool.name, arguments, "prepare"),
                 scripted_call(writer.name, {"path": str(path), "content": final}, "write")]
    if verify_final:
        responses.append(scripted_call(tool.name, {"path": str(path)}, "final"))
    responses += [LLMResponse(content="Done", finish_reason="stop")] * 4
    events = [event async for event in run_agent_loop(
        llm=ScriptedLLM(responses), messages=[Message(role="user", content=f"{mode} {path}, rename old_col to new_col")],
        tools={tool.name: tool, writer.name: writer}, workspace_dir=str(tmp_path), max_steps=8,
    )]
    assert next(e for e in events if isinstance(e, DoneEvent)).stop_reason == (
        StopReason.END_TURN if verify_final else StopReason.ERROR
    )
    assert path.read_text() == final
    results = [e for e in events if isinstance(e, ToolCallResult) and e.tool_name == tool.name]
    assert all(e.success for e in results)
    if verify_final:
        assert results[-1].raw_output["records"] == 2 and results[-1].raw_output["data_valid"]
        assert set(results[-1].raw_output["fields"]) == set(output_fields)
        if mode == "edit_target":
            assert results[0].raw_output["records"] == 2
            assert set(results[0].raw_output["fields"]) == set(fields)


@pytest.mark.asyncio
async def test_file_roles_and_final_schemas_cannot_be_changed_after_their_declaration(tmp_path):
    import asyncio
    tool, gate, path = await setup_gate(tmp_path)
    context = ToolInvocationContext(completion_gate=gate)
    first, second = await asyncio.gather(
        tool.invoke({"path": str(path), "fields": FIELDS}, context=context),
        tool.invoke({"path": str(path), "source_mode": "delete_target"}, context=context),
    )
    assert first.success and not second.success and "TABLE_SOURCE_MODE_CHANGED" in second.error
    changed = await tool.invoke({"path": str(path), "fields": FIELDS, "source_mode": "replace_target"}, context=context)
    assert not changed.success and "TABLE_SOURCE_MODE_CHANGED" in changed.error
    tool, gate, path = await setup_gate(tmp_path, name="replace.csv")
    context = ToolInvocationContext(completion_gate=gate)
    ready = await tool.invoke({"path": str(path), "fields": FIELDS, "source_mode": "replace_target"}, context=context)
    assert ready.success and not gate.receipts
    relaxed = copy.deepcopy(FIELDS)
    relaxed["amount"]["minimum"] = "-100"
    result = await tool.invoke({"path": str(path), "fields": relaxed}, context=context)
    assert not result.success and "TABLE_CONTRACT_CHANGED" in result.error
    write_table(path, [row(2, amount="-1")])
    result = await tool.invoke({"path": str(path)}, context=context)
    assert result.success and not result.raw_output["data_valid"]
    assert (await gate.check()).code == "TABLE_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_deletion_does_not_accept_a_dangling_symlink_or_exempt_other_inputs(tmp_path):
    tool, _, path = await setup_gate(tmp_path)
    other = tmp_path / "other.csv"
    write_table(other, [row(2)])
    gate = await tool.create_completion_gate(CompletionContext((Message(
        role="user", content=f"Delete {path} and analyze {other}",
    ),), lambda: False))
    context = ToolInvocationContext(completion_gate=gate)
    result = await tool.invoke({"path": str(path), "source_mode": "delete_target"}, context=context)
    assert result.success
    path.unlink()
    assert (await gate.check()).code == "TABLE_VALIDATION_MISSING"
    assert (await tool.invoke({"path": str(other), "fields": FIELDS}, context=context)).success
    assert await gate.check() is None
    path.symlink_to(tmp_path / "missing.csv")
    assert (await gate.check()).code == "TABLE_DELETE_PENDING"


@pytest.mark.asyncio
async def test_schema_edit_cannot_add_a_new_final_contract_after_original_scan(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    context = ToolInvocationContext(completion_gate=gate)
    assert (await tool.invoke({"path": str(path), "fields": FIELDS, "source_mode": "edit_target"}, context=context)).success
    relaxed = copy.deepcopy(FIELDS)
    relaxed["amount"]["minimum"] = "-100"
    result = await tool.invoke({"path": str(path), "fields": FIELDS, "output_fields": relaxed}, context=context)
    assert not result.success and "TABLE_CONTRACT_CHANGED" in result.error


@pytest.mark.asyncio
async def test_failed_deletion_preparation_cannot_credit_a_later_removal(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    write_table(path, [row(4)])
    result = await tool.invoke({"path": str(path), "source_mode": "delete_target"},
                               context=ToolInvocationContext(completion_gate=gate))
    assert not result.success and "TABLE_SOURCE_CHANGED" in result.error
    assert path not in gate.source_modes
    path.unlink()
    issue = await gate.check()
    assert issue.code == "TABLE_SOURCE_UNVERIFIED" and not issue.repairable


@pytest.mark.asyncio
async def test_original_scan_cannot_certify_a_declared_final_schema_without_the_edit(tmp_path):
    tool, gate, path = await setup_gate(tmp_path)
    final_fields = {key: value for key, value in FIELDS.items() if key != "note"}
    result = await tool.invoke(
        {"path": str(path), "fields": FIELDS, "output_fields": final_fields, "source_mode": "edit_target"},
        context=ToolInvocationContext(completion_gate=gate),
    )
    assert result.success and result.raw_output["data_valid"]
    issue = await gate.check()
    assert issue.code == "TABLE_EDIT_VALIDATION_MISSING" and issue.repairable


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
@pytest.mark.parametrize("validation", ["valid", "invalid_last_column", "omitted_last_column"])
async def test_wide_original_table_requires_every_column_before_completion(
    tmp_path, monkeypatch, suffix, delimiter, validation,
):
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None

    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    path = tmp_path / f"wide.{suffix}"
    fields = {f"column_{index}": {"type": "integer"} for index in range(257)}
    values = ["1"] * len(fields)
    final_values = [*values[:-1], "invalid" if validation == "invalid_last_column" else "2"]
    path.write_text("\n".join(delimiter.join(row) for row in [list(fields), values, final_values]) + "\n")
    contract = dict(fields)
    if validation == "omitted_last_column":
        contract.pop("column_256")
    tool = ValidateTableTool(str(tmp_path))
    responses = [LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
        id="wide", type="function", function=FunctionCall(
            name=tool.name, arguments={"path": str(path), "fields": contract},
        ),
    )])] + [LLMResponse(content="Done", finish_reason="stop")] * 4
    events = [event async for event in run_agent_loop(
        llm=ScriptedLLM(responses), messages=[Message(role="user", content=f"Analyze {path}")],
        tools={tool.name: tool}, workspace_dir=str(tmp_path), max_steps=6,
    )]
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.stop_reason == (StopReason.END_TURN if validation == "valid" else StopReason.ERROR)
    result = next(e for e in events if isinstance(e, ToolCallResult))
    if validation == "omitted_last_column":
        assert not result.success and "all 257 columns" in result.error
    else:
        assert result.success and result.raw_output["validation_complete"]
        assert result.raw_output["data_valid"] == (validation == "valid")
        assert result.raw_output["records"] == 2
        assert set(result.raw_output["fields"]) == set(fields)
        assert all(stats["checked"] == 2 for stats in result.raw_output["fields"].values())
        if validation == "invalid_last_column":
            assert result.raw_output["fields"]["column_256"]["invalid"] == 1
    if validation != "valid":
        assert any(isinstance(e, ErrorEvent) and e.error_code == "COMPLETION_GATE_UNSATISFIED" for e in events)


@pytest.mark.asyncio
async def test_generic_ledger_completes_with_negative_amount_zero_revision_and_extra_precision(tmp_path, monkeypatch):
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None

    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    path = tmp_path / "ledger.csv"
    path.write_text("id,status,revision,amount\nA,draft,0,-1.2345\n")
    fields = {"id": {"type": "text"}, "status": {"type": "text"},
              "revision": {"type": "integer", "minimum": "0"},
              "amount": {"type": "decimal", "minimum": "-100", "scale": 4}}
    tool = ValidateTableTool(str(tmp_path))
    responses = [LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
        id="ledger", type="function", function=FunctionCall(name=tool.name, arguments={"path": str(path), "fields": fields}),
    )]), LLMResponse(content="Done", finish_reason="stop")]
    events = [event async for event in run_agent_loop(
        llm=ScriptedLLM(responses), messages=[Message(role="user", content=f"Analyze {path}")],
        tools={tool.name: tool}, workspace_dir=str(tmp_path), max_steps=3,
    )]
    assert next(e for e in events if isinstance(e, DoneEvent)).stop_reason == StopReason.END_TURN
    result = next(e for e in events if isinstance(e, ToolCallResult))
    assert result.raw_output["data_valid"] and result.raw_output["profile"] == "declared_fields"


@pytest.mark.asyncio
async def test_explicit_transaction_profile_is_inherited_and_cannot_be_downgraded(tmp_path):
    tool, gate, path = await setup_gate(tmp_path, rows=[row(1, amount="-1")])
    context = ToolInvocationContext(completion_gate=gate)
    arguments = {"path": str(path), "fields": FIELDS}
    first = await tool.invoke({**arguments, "profile": "transaction_v1"}, context=context)
    assert first.success and not first.raw_output["data_valid"]
    inherited = await tool.invoke(arguments, context=context)
    assert inherited.raw_output["profile"] == "transaction_v1"
    changed = await tool.invoke({**arguments, "profile": "declared_fields"}, context=context)
    assert not changed.success and "TABLE_CONTRACT_CHANGED" in changed.error
    assert (await gate.check()).code == "TABLE_VALIDATION_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
@pytest.mark.parametrize("operation", ["write", "edit", "append"])
@pytest.mark.parametrize("validation", ["both", "original_only", "final_only"])
async def test_in_place_table_writes_require_both_versions_and_preserve_write_receipts(
    tmp_path, monkeypatch, suffix, delimiter, operation, validation,
):
    import hashlib
    from box_agent.session_log import SessionLog
    from box_agent.tools.file_tools import WriteTool, AppendTool, EditTool
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None

    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    path = tmp_path / f"orders.{suffix}"
    before = f"id{delimiter}price\n1{delimiter}10\n"
    after = before + f"2{delimiter}20\n" if operation == "append" else before.replace("10", "15")
    path.write_text(before)
    tool = ValidateTableTool(str(tmp_path))
    writer = {"write": WriteTool, "edit": EditTool, "append": AppendTool}[operation](workspace_dir=str(tmp_path))
    fields = {"id": {"type": "integer"}, "price": {"type": "decimal"}}

    def call(name, arguments, call_id):
        return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id=call_id, type="function", function=FunctionCall(name=name, arguments=arguments),
        )])

    responses = []
    if validation != "final_only":
        responses.append(call(tool.name, {"path": str(path), "fields": fields, "source_mode": "edit_target"}, "original"))
    write_args = {"path": str(path)}
    if operation == "edit":
        write_args.update(old_str=f"1{delimiter}10", new_str=f"1{delimiter}15")
    else:
        write_args["content"] = after[len(before):] if operation == "append" else after
    responses.append(call(writer.name, write_args, "write"))
    if validation != "original_only":
        responses.append(call(tool.name, {"path": str(path), "fields": fields, "source_mode": "edit_target"}, "final"))
    responses += [LLMResponse(content="Done", finish_reason="stop")] * 4
    log = SessionLog.create(tmp_path / "logs", session_id="edit", cwd=tmp_path)
    try:
        events = [event async for event in run_agent_loop(
            llm=ScriptedLLM(responses),
            messages=[Message(role="user", content=f"Please {operation} {path} in place.")],
            tools={tool.name: tool, writer.name: writer}, max_steps=8,
            workspace_dir=str(tmp_path), session_log=log,
        )]
        done = next(e for e in events if isinstance(e, DoneEvent))
        assert done.stop_reason == (StopReason.END_TURN if validation == "both" else StopReason.ERROR)
        result = next(e for e in events if isinstance(e, ToolCallResult) and e.tool_name == writer.name)
        assert result.success and path.read_text() == after
        assert result.raw_output["file_changes"] == [{
            "path": path.name,
            "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
        }]
        if validation == "both":
            scans = [e["data"] for e in log.events if e["type"] == "table.validation"]
            assert len(scans) == 2 and scans[0]["source_sha256"] != scans[1]["source_sha256"]
            assert [s["records"] for s in scans] == [1, 2 if operation == "append" else 1]
            assert all(s["complete"] and all(f["checked"] == s["records"] for f in s["fields"].values()) for s in scans)
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_input", [False, True])
@pytest.mark.parametrize("validate_added", [False, True])
async def test_supplemented_table_requires_a_new_receipt_without_losing_existing_validation(
    tmp_path, monkeypatch, initial_input, validate_added,
):
    import asyncio
    from box_agent.turn_continuation import TurnContinuationController

    async def no_judge(*args, **kwargs):
        return None

    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    tool, _, first = await setup_gate(tmp_path)
    added = tmp_path / "added.csv"
    write_table(added, [row(3)])
    queue = asyncio.Queue()

    def validate(path, call_id):
        return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id=call_id, type="function", function=FunctionCall(
                name=tool.name, arguments={"path": str(path), "fields": FIELDS},
            ),
        )])

    class SupplementedLLM(ScriptedLLM):
        supplied = False

        async def generate_stream(self, *args, **kwargs):
            if not self.supplied:
                queue.put_nowait(f"Also process {added}")
                self.supplied = True
            async for event in super().generate_stream(*args, **kwargs):
                yield event

    responses = [validate(first, "first")]
    if validate_added:
        responses.append(validate(added, "added"))
    responses += [LLMResponse(content="Done", finish_reason="stop")] * 4
    events = [event async for event in run_agent_loop(
        llm=SupplementedLLM(responses),
        messages=[Message(role="user", content=str(first) if initial_input else "Start the task")],
        tools={tool.name: tool}, inject_queue=queue, max_steps=6, workspace_dir=str(tmp_path),
    )]
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == (StopReason.END_TURN if validate_added else StopReason.ERROR)
    if not validate_added:
        assert any(isinstance(event, ErrorEvent) and event.error_code == "COMPLETION_GATE_UNSATISFIED"
                   for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("validate,max_steps,invalid,purpose", [
    (False, 1, False, "computation"), (False, 5, False, "computation"),
    (True, 5, False, "computation"), (True, 5, True, "computation"),
    (False, 5, True, "diagnosis"), (True, 5, False, "diagnosis"), (True, 5, True, "diagnosis"),
])
async def test_runtime_completion_requires_actual_validator_invocation(tmp_path, monkeypatch, validate, max_steps, invalid, purpose):
    from box_agent.turn_continuation import TurnContinuationController
    async def no_judge(*args, **kwargs):
        return None
    monkeypatch.setattr(TurnContinuationController, "evaluate", no_judge)
    tool, _, path = await setup_gate(tmp_path, rows=[row(1, amount="NaN" if invalid else "1.25"), row(1, revision="2")])
    responses = []
    if validate:
        responses.append(LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id="validate-1", type="function", function=FunctionCall(name=tool.name, arguments={"path": str(path), "fields": FIELDS, "purpose": purpose}),
        )]))
    responses += [LLMResponse(content="All records and all fields were validated; done.", finish_reason="stop")] * 4
    events = [event async for event in run_agent_loop(
        llm=ScriptedLLM(responses), messages=[Message(role="user", content=f"Process {path}")],
        tools={tool.name: tool}, max_steps=max_steps, workspace_dir=str(tmp_path),
    )]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    completed = validate and (not invalid or purpose == "diagnosis")
    assert done[0].stop_reason == (StopReason.END_TURN if completed else StopReason.ERROR)
    if not completed:
        assert any(isinstance(e, ErrorEvent) and e.error_code == "COMPLETION_GATE_UNSATISFIED" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("table_input", [False, True])
async def test_provider_refusal_cannot_bypass_active_completion_gate(tmp_path, monkeypatch, table_input):
    import box_agent.llm.error_messages as errors
    monkeypatch.setattr(errors, "structured_llm_error", lambda *a, **k: {"category": "content_filter", "message": "Refused"})
    class RefusingLLM:
        async def generate_stream(self, *args, **kwargs):
            raise RuntimeError("refused")
            yield  # async generator contract
    tool, _, path = await setup_gate(tmp_path)
    events = [e async for e in run_agent_loop(
        llm=RefusingLLM(), messages=[Message(role="user", content=str(path) if table_input else "hello")],
        tools={tool.name: tool}, workspace_dir=str(tmp_path), max_steps=1,
    )]
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.stop_reason == (StopReason.ERROR if table_input else StopReason.END_TURN)


@pytest.mark.parametrize("enabled", [False, True])
def test_shared_tool_setup_registers_validator_only_with_file_capability(tmp_path, enabled):
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from box_agent.tools.setup import add_workspace_tools
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(), tools=ToolsConfig(
        enable_file_tools=enabled, enable_bash=False, enable_todo=False, enable_sub_agent=False,
    ))
    tools = []
    add_workspace_tools(tools, config, tmp_path, output=lambda *a: None)
    validators = [t for t in tools if t.name == "validate_table"]
    assert len(validators) == int(enabled)
    if enabled:
        from pathlib import Path
        snapshot = json.loads((Path(__file__).parent / "fixtures/tool_engine/table_validation_schema.json").read_text())
        assert validators[0].to_schema() == snapshot["validate_table"]["schema"]
