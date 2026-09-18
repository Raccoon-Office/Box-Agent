"""Only evidenced, confirmed, applicable remedies reach execution context."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from box_agent.config import AgentConfig
from box_agent.context_input import DefaultContextEngine
from box_agent.core import run_agent_loop
from box_agent.correction import CorrectionDraft, CorrectionSubject
from box_agent.memory import MemoryManager
from box_agent.memory_maintainer import MemoryMaintainer
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.memory_tool import MemoryWriteCorrectionTool


class ProbeTool(Tool):
    name = "export_probe"
    version = "v1"
    description = "Export a local test artifact."
    parameters = {"type": "object", "properties": {"mode": {"type": "string"}}}

    async def execute(self, mode="fixed"):
        return ToolResult(success=mode == "fixed", content="validated output" if mode == "fixed" else "",
                          error=None if mode == "fixed" else "unsupported layout")


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(memory_dir=str(tmp_path / "memory"))


def observed_repair(manager, *, scope="run", skill_subjects=()):
    curator = manager.correction_curator
    subject = CorrectionSubject("tool", "export_probe", "v1")
    for number in range(2):
        assert curator.observe_result(scope=scope, subject=subject, call_id=f"failed-{number}",
            arguments={"mode": "broken"}, success=False, error="unsupported layout") is None
    return curator.observe_result(scope=scope, subject=subject, call_id="validated-call",
        arguments={"mode": "fixed"}, success=True, content="validated output", skill_subjects=skill_subjects)


def stored_remedy(manager, *, subject=None, lesson="Use the supported grid layout.", fingerprint="unsupported layout"):
    # A trusted persisted-evidence fixture; runtime evidence minting is tested separately.
    draft = CorrectionDraft(lesson, "", subject or CorrectionSubject("tool", "export_probe", "v1"),
                            fingerprint, verification="tool=export_probe;call=validated-call;result=success")
    entry = manager.write_correction(draft)
    return manager.confirm_correction_draft(entry.id)


@pytest.mark.asyncio
async def test_real_evidence_activates_and_reloads_without_user_staging(manager):
    tool = MemoryWriteCorrectionTool(manager)
    args = dict(lesson="Use the supported grid layout.", error_fingerprint="unsupported layout",
                subject_name="export_probe", subject_version="v1")
    assert not (await tool.execute(**args, verification_id="invented")).success
    assert manager.list_corrections(include_inactive=True) == []
    evidence = observed_repair(manager)
    result = await tool.execute(**args, verification_id=evidence.verification_id)
    assert result.success and result.raw_output["status"] == "active"
    assert "草稿" not in result.content and "确认" not in result.content
    repeated = await tool.execute(**args, verification_id=evidence.verification_id)
    assert repeated.success and repeated.raw_output["id"] == result.raw_output["id"]
    changed = await tool.execute(**{**args, "lesson": "An unrelated replacement."}, verification_id=evidence.verification_id)
    assert not changed.success
    assert len(manager.list_corrections(include_inactive=True)) == 1
    loaded = MemoryManager(memory_dir=str(manager.memory_dir))
    assert loaded.list_corrections()[0].verification
    assert loaded.search("unsupported layout")


def test_evidence_does_not_cross_runs_replayed_calls_versions_or_failures(manager):
    subject = CorrectionSubject("tool", "export_probe", "v1")
    curator = manager.correction_curator
    for _ in range(2):
        curator.observe_result(scope="one", subject=subject, call_id="duplicate",
            arguments={"mode": "broken"}, success=False, error="unsupported layout")
    assert curator.observe_result(scope="one", subject=subject, call_id="success",
        arguments={"mode": "fixed"}, success=True) is None
    notice = observed_repair(manager, scope="other")
    for different in (CorrectionSubject("tool", "other", "v1"), CorrectionSubject("tool", "export_probe", "v2")):
        with pytest.raises(Exception, match="subject/version"):
            curator.verification_for(notice.verification_id, different, "unsupported layout")
    with pytest.raises(Exception, match="failure"):
        curator.verification_for(notice.verification_id, subject, "unrelated failure")
    curator._verifications[notice.verification_id] = (
        datetime.now(timezone.utc) - timedelta(days=1), *curator._verifications[notice.verification_id][1:])
    with pytest.raises(Exception, match="expired"):
        curator.verification_for(notice.verification_id, subject, "unsupported layout")


def test_repeated_failure_or_direct_active_write_cannot_replace_a_verified_remedy(manager):
    old = stored_remedy(manager)
    original = manager.list_corrections()[0].content
    for number in range(5):
        manager.correction_curator.observe_result(scope="new", subject=CorrectionSubject("tool", "export_probe", "v1"),
            call_id=str(number), arguments={}, success=False, error="unsupported layout")
    assert manager.list_corrections()[0].content == original
    competing = CorrectionDraft("A different remedy", "", CorrectionSubject("tool", "export_probe", "v1"),
                                 "unsupported layout", verification="tool=fixture;result=success")
    assert manager.write_correction(competing, status="active").id == old.id
    assert manager.list_corrections()[0].content == original
    draft = manager.write_correction(competing)
    manager.confirm_correction_draft(draft.id)
    assert manager.list_corrections()[0].id == draft.id
    assert manager.list_corrections(status="superseded")[0].id == old.id


def test_maintenance_preserves_recreated_active_correction_and_all_states(manager):
    old = stored_remedy(manager)
    manager.search("unsupported layout")
    manager.delete_correction(old.id)
    active = stored_remedy(manager)
    draft = manager.write_correction(CorrectionDraft("Inspect a candidate fix", "", CorrectionSubject("tool", "draft"), "draft"))
    for entry in manager.read_all_context_entries():
        entry.last_used = "2020-01-01T00:00:00"
        manager._write_context_topic_entries([e if e.id != entry.id else entry for e in manager.read_all_context_entries()])
    before = manager.list_corrections(include_inactive=True)
    maintainer = MemoryMaintainer(manager, AgentConfig(memory_maintainer_enabled=True))
    maintainer._dedup(datetime.now(timezone.utc))
    maintainer._decay(datetime.now(timezone.utc))
    assert manager.list_corrections(include_inactive=True) == before
    assert [e.id for e in manager.list_corrections()] == [active.id]
    assert manager.list_corrections(status="draft")[0].id == draft.id


def test_generic_context_writers_and_core_promotion_cannot_change_corrections(manager):
    entry = stored_remedy(manager)
    before = manager.list_corrections(include_inactive=True)
    for method in (manager.write_context, manager.append_context):
        with pytest.raises(ValueError, match="reserved"):
            method("replacement", topic="corrections")
    assert not manager.apply_context_operations([
        {"action": "replace", "old": entry.content, "new": "corrupted"},
        {"action": "drop", "content": entry.content},
        {"action": "add", "topic": "corrections", "content": "fake"},
    ])
    for _ in range(5):
        manager.search("unsupported layout")
    assert manager.list_promotion_candidates(hit_threshold=1, cooldown_days=0) == []
    assert "supported grid" not in manager.read_context()
    assert manager.list_corrections()[0].content == before[0].content


@pytest.mark.parametrize("status", ["draft", "deleted", "superseded"])
def test_inactive_unverified_and_mismatched_versions_never_auto_recall(manager, status):
    subject = CorrectionSubject("tool", "export_probe", "v1")
    entry = stored_remedy(manager)
    assert manager.recall_corrections([subject])
    assert manager.recall_corrections([replace(subject, version="v2")]) == []
    assert manager.recall_corrections([replace(subject, version="")]) == []
    manager._set_correction_status(entry.id, status)
    assert manager.recall_corrections([subject]) == []
    assert manager.auto_match_context("Use the supported grid layout") == []


def test_request_projection_is_bounded_ephemeral_and_refreshes_after_revocation(manager):
    entry = stored_remedy(manager)
    context = DefaultContextEngine()
    context.configure_run()
    context.bind_memory(manager)
    messages = [Message(role="system", content="system"), Message(role="user", content="Create an artifact")]
    prepared = prepare_tools([ProbeTool()])
    first = context.prepare_request(messages, prepared_tools=prepared, token_limit=20000)
    assert "supported grid layout" in str(first.messages)
    assert "supported grid layout" not in str(messages)
    manager.delete_correction(entry.id)
    second = context.prepare_request(messages, prepared_tools=prepared, token_limit=20000)
    assert "supported grid layout" not in str(second.messages)
    for number in range(6):
        stored_remedy(manager, lesson=f"Use variant {number} for the exported artifact.", fingerprint=str(number))
    matches = manager.recall_corrections([CorrectionSubject("tool", "export_probe", "v1")], max_chars=800)
    assert 0 < len(matches) <= 3
    assert sum(len(item["text"]) for item in matches) <= 800


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_kernel_first_request_receives_matching_remedy_before_any_failure(manager, tmp_path, enabled):
    stored_remedy(manager)
    requests = []
    class LLM:
        model = "test"
        max_output_tokens = 1000
        async def generate_stream(self, *, messages, **kwargs):
            requests.append(messages)
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")
    messages = [Message(role="system", content="system"), Message(role="user", content="Create an artifact")]
    _ = [event async for event in run_agent_loop(llm=LLM(), tools={"export_probe": ProbeTool()},
        messages=messages, memory_manager=manager if enabled else None, workspace_dir=str(tmp_path))]
    assert ("supported grid layout" in str(requests[0])) is enabled
    assert "Relevant verified correction records" not in str(messages)


def test_skill_recall_requires_selected_source_revision(manager, tmp_path):
    from box_agent.skill_runtime import SkillRuntime
    from box_agent.tools.skill_loader import SkillLoader
    from box_agent.tools.skill_tool import GetSkillTool

    path = tmp_path / "skills/demo/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: demo\ndescription: test method\n---\nCURRENT_METHOD\n")
    loader = SkillLoader(sources=[(path.parents[1], "user")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    runtime = SkillRuntime(loader)
    revision = runtime.resolve_reference("demo").revision
    stored_remedy(manager, subject=CorrectionSubject("skill", "demo", revision), lesson="SKILL_SPECIFIC_REMEDY")
    context = DefaultContextEngine()
    context.configure_run(skill_engine=runtime)
    context.bind_memory(manager)
    messages = [Message(role="system", content="system"), Message(role="user", content="Create an artifact")]
    tools = prepare_tools([GetSkillTool(loader)])
    assert "SKILL_SPECIFIC_REMEDY" not in str(context.prepare_request(messages, prepared_tools=tools, token_limit=20000).messages)
    runtime.select(["demo"])
    assert "SKILL_SPECIFIC_REMEDY" in str(context.prepare_request(messages, prepared_tools=tools, token_limit=20000).messages)
    path.write_text(path.read_text().replace("CURRENT_METHOD", "CHANGED_METHOD"))
    loader.discover_skills()
    assert "SKILL_SPECIFIC_REMEDY" not in str(context.prepare_request(messages, prepared_tools=tools, token_limit=20000).messages)


@pytest.mark.asyncio
async def test_real_kernel_failure_repair_mints_evidence_but_does_not_invent_lesson(manager, tmp_path):
    captures = []
    class LLM:
        model = "test"
        max_output_tokens = 1000
        calls = 0
        async def generate_stream(self, *, messages, **kwargs):
            self.calls += 1
            captures.append(str(messages))
            if self.calls <= 3:
                yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[ToolCall(
                    id=f"probe-{self.calls}", type="function", function=FunctionCall(
                        name="export_probe", arguments={"mode": "fixed" if self.calls == 3 else "broken"}))])
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")
    _ = [event async for event in run_agent_loop(llm=LLM(), tools={"export_probe": ProbeTool()},
        messages=[Message(role="system", content="system"), Message(role="user", content="export")],
        memory_manager=manager, workspace_dir=str(tmp_path), max_steps=30)]
    assert "verification_id" in captures[-1]
    assert manager.list_corrections(include_inactive=True) == []
    assert len(manager.correction_curator._verifications) == 1


def test_success_in_another_run_or_same_arguments_cannot_verify_a_repair(manager):
    curator = manager.correction_curator
    subject = CorrectionSubject("tool", "export_probe", "v1")
    for number in range(2):
        curator.observe_result(scope="one", subject=subject, call_id=str(number), arguments={"mode": "broken"},
                               success=False, error="unsupported layout")
    assert curator.observe_result(scope="two", subject=subject, call_id="success", arguments={"mode": "fixed"}, success=True) is None
    assert curator.observe_result(scope="one", subject=subject, call_id="success", arguments={"mode": "broken"}, success=True) is None
    assert manager.list_corrections(include_inactive=True) == []


def test_unrelated_shell_success_is_not_repair_evidence(manager):
    curator = manager.correction_curator
    subject = CorrectionSubject("tool", "bash")
    for number in range(2):
        curator.observe_result(scope="one", subject=subject, call_id=str(number),
            arguments={"command": "python export.py --mode broken"}, success=False, error="unsupported layout")
    assert curator.observe_result(scope="one", subject=subject, call_id="unrelated",
        arguments={"command": "ls"}, success=True) is None
    assert curator.observe_result(scope="one", subject=subject, call_id="other-script",
        arguments={"command": "python unrelated.py"}, success=True) is None
    assert curator.observe_result(scope="one", subject=subject, call_id="repair",
        arguments={"command": "python export.py --mode supported"}, success=True) is not None


@pytest.mark.parametrize("success_command", [
    "python export.py --help", "python export.py --version", "python export.py --dry-run",
    "python export.py --input other.pptx", "uv run python unrelated.py --input same.pptx",
])
def test_diagnostic_or_different_operation_never_verifies_a_failure(manager, success_command):
    curator = manager.correction_curator
    subject = CorrectionSubject("tool", "bash")
    for number in range(2):
        curator.observe_result(scope="one", subject=subject, call_id=str(number),
            arguments={"command": "uv run python export.py --input same.pptx --mode broken"},
            success=False, error="unsupported layout")
    assert curator.observe_result(scope="one", subject=subject, call_id="unrelated",
        arguments={"command": success_command}, success=True) is None


def test_npm_run_retains_script_identity(manager):
    curator = manager.correction_curator
    subject = CorrectionSubject("tool", "bash")
    for number in range(2):
        curator.observe_result(scope="one", subject=subject, call_id=str(number),
            arguments={"command": "npm run export -- --mode broken"}, success=False, error="unsupported layout")
    assert curator.observe_result(scope="one", subject=subject, call_id="other",
        arguments={"command": "npm run unrelated"}, success=True) is None
    assert curator.observe_result(scope="one", subject=subject, call_id="fixed",
        arguments={"command": "npm run export -- --mode supported"}, success=True) is not None


def test_script_edit_then_unchanged_rerun_can_verify_repair(manager, tmp_path):
    curator = manager.correction_curator
    subject = CorrectionSubject("tool", "bash")
    args = {"command": "uv run python export.py --input same.pptx"}
    for number in range(2):
        curator.observe_result(scope="one", subject=subject, call_id=str(number), arguments=args,
            success=False, error="unsupported layout", workspace=str(tmp_path))
    curator.observe_result(scope="one", subject=CorrectionSubject("tool", "edit_file"), call_id="wrong-edit",
        arguments={"path": str(tmp_path / "unrelated.py")}, success=True, workspace=str(tmp_path))
    assert curator.observe_result(scope="one", subject=subject, call_id="no-fix", arguments=args,
        success=True, workspace=str(tmp_path)) is None
    curator.observe_result(scope="one", subject=CorrectionSubject("tool", "edit_file"), call_id="repair",
        arguments={"path": str(tmp_path / "export.py")}, success=True, workspace=str(tmp_path))
    assert curator.observe_result(scope="one", subject=subject, call_id="validated", arguments=args,
        success=True, workspace=str(tmp_path)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["delete_correction", "supersede_correction"])
async def test_revoked_receipt_cannot_restore_or_replace_a_remedy(manager, revoke):
    receipt = observed_repair(manager)
    tool = MemoryWriteCorrectionTool(manager)
    args = dict(lesson="Use the supported grid layout.", error_fingerprint="unsupported layout",
                subject_name="export_probe", subject_version="v1", verification_id=receipt.verification_id)
    first = await tool.execute(**args)
    assert first.success
    getattr(manager, revoke)(first.raw_output["id"])
    assert not (await tool.execute(**args)).success
    assert not (await tool.execute(**{**args, "lesson": "A completely different method."})).success
    assert manager.list_corrections() == []


@pytest.mark.asyncio
async def test_successful_retry_cannot_publish_placeholder_advice(manager):
    receipt = observed_repair(manager)
    result = await MemoryWriteCorrectionTool(manager).execute(
        lesson="Apply the known fix instead of retrying blindly.", error_fingerprint="unsupported layout",
        subject_name="export_probe", subject_version="v1", verification_id=receipt.verification_id)
    assert not result.success
    assert manager.list_corrections(include_inactive=True) == []


@pytest.mark.asyncio
async def test_learn_then_next_task_recalls_without_user_approval_step(manager, tmp_path):
    import json
    class LearningLLM:
        model = "test"
        max_output_tokens = 1000
        calls = 0
        async def generate_stream(self, *, messages, **kwargs):
            self.calls += 1
            if self.calls <= 3:
                name = "export_probe"
                arguments = {"mode": "fixed" if self.calls == 3 else "broken"}
            elif self.calls == 4:
                evidence_message = next(m.content for m in messages if "Evidence data: " in str(m.content))
                evidence = json.loads(evidence_message.split("Evidence data: ", 1)[1])
                name = "memory_write_correction"
                arguments = {**evidence, "lesson": "Use fixed mode for this exporter's unsupported layout."}
            else:
                yield StreamEvent(type="text", delta="Exported and remembered the repair.")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[ToolCall(
                id=f"learning-{self.calls}", type="function", function=FunctionCall(name=name, arguments=arguments))])
    _ = [event async for event in run_agent_loop(
        llm=LearningLLM(), tools={"export_probe": ProbeTool(), "memory_write_correction": MemoryWriteCorrectionTool(manager)},
        messages=[Message(role="system", content="system"), Message(role="user", content="Export the artifact")],
        memory_manager=manager, workspace_dir=str(tmp_path), max_steps=30)]
    assert len(manager.list_corrections()) == 1
    assert manager.list_corrections()[0].subject_version == "v1"
    loaded = MemoryManager(memory_dir=str(manager.memory_dir))
    requests = []
    class NextTaskLLM:
        model = "test"
        max_output_tokens = 1000
        async def generate_stream(self, *, messages, **kwargs):
            requests.append(str(messages))
            yield StreamEvent(type="text", delta="Use the earlier repair before exporting.")
            yield StreamEvent(type="finish", finish_reason="stop")
    _ = [event async for event in run_agent_loop(
        llm=NextTaskLLM(), tools={"export_probe": ProbeTool()},
        messages=[Message(role="system", content="system"), Message(role="user", content="Make another artifact")],
        memory_manager=loaded, workspace_dir=str(tmp_path))]
    assert "Use fixed mode" in requests[0]


def _synthetic_authentication_samples():
    # Generate inert credential-shaped data at test time; never commit literal
    # credential strings or use real account material in negative tests.
    import base64
    import json
    from uuid import uuid4
    nonce = uuid4().hex
    def encoded(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return [
        "Authorization: Basic " + base64.b64encode(f"synthetic:{nonce}".encode()).decode(),
        f'Authorization: Digest username="synthetic", response="{nonce}"',
        f"Cookie: session={nonce}",
        json.dumps({"token": nonce}),
        "https://" + f"synthetic:{nonce}" + "@example.invalid/api",
        "-----BEGIN RSA PRIVATE KEY-----\nSYNTHETIC\n-----END RSA PRIVATE KEY-----",
        ".".join([encoded({"alg": "none"}), encoded({"sub": "synthetic-test"}), nonce]),
    ]



@pytest.mark.asyncio
@pytest.mark.parametrize("value", _synthetic_authentication_samples(),
                         ids=["basic", "digest", "cookie", "token", "url-userinfo", "private-key", "jwt"])
async def test_authentication_material_cannot_enter_evidence_or_durable_memory(manager, value):
    from box_agent.correction import classify_forbidden_content
    assert classify_forbidden_content(value) == "secret"
    receipt = observed_repair(manager)
    before = manager.list_corrections(include_inactive=True)
    result = await MemoryWriteCorrectionTool(manager).execute(
        lesson="Supply " + value + " for this request.", error_fingerprint="unsupported layout",
        subject_name="export_probe", subject_version="v1", verification_id=receipt.verification_id)
    assert not result.success
    assert result.error == "这条不算可复用纠错（偏好/敏感/一次性），没记下。"
    assert manager.list_corrections(include_inactive=True) == before
    subject = CorrectionSubject("tool", "auth_probe")
    for number in range(2):
        assert manager.correction_curator.observe_result(
            scope="auth", subject=subject, call_id=f"auth-{number}", arguments={"mode": "broken"},
            success=False, error=value) is None
    assert manager.correction_curator.observe_result(
        scope="auth", subject=subject, call_id="auth-success", arguments={"mode": "fixed"}, success=True) is None
    assert not any(value in path.read_text() for path in manager.memory_dir.rglob("*.md"))
