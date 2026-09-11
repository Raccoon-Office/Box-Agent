"""Observable Skill reference behavior, independent of a host protocol."""

from hashlib import sha256

import pytest

from box_agent import skill_runtime
from box_agent.schema import Message
from box_agent.skill_context import SkillReferenceContext
from box_agent.tools.skill_loader import SkillLoader


def make_context(tmp_path, body="FIRST_RULE\nSECOND_RULE", extra=""):
    path = tmp_path / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: demo\ndescription: example\n{extra}---\n{body}\n")
    loader = SkillLoader(sources=[(tmp_path / "skills", "user")])
    loader.discover_skills()
    factory = getattr(skill_runtime, "SkillRuntime", None)
    assert factory is not None, "A shared Skill reference runtime is required"
    return SkillReferenceContext(factory(loader)), path


def test_read_returns_full_reference_without_modifying_system(tmp_path):
    context, _ = make_context(tmp_path)
    messages = [Message(role="system", content="BASE"), Message(role="user", content="task")]
    context.prepare_request(messages, budget_chars=50000)
    result = context.read("demo")
    assert result.success and "SECOND_RULE" in result.model_context
    assert result.raw_output["skill_reference"]["complete"] is True
    assert messages[0].content == "BASE"


def test_dedup_requires_body_in_actual_input_and_rereads_after_compaction(tmp_path):
    context, _ = make_context(tmp_path)
    messages = [Message(role="system", content="BASE"), Message(role="user", content="task")]
    context.prepare_request(messages, budget_chars=50000)
    first = context.read("demo")
    messages.append(Message(role="tool", name="get_skill", tool_call_id="call_1", content=first.model_context))
    context.prepare_request(messages, budget_chars=50000)
    duplicate = context.read("demo")
    assert "SECOND_RULE" not in duplicate.model_context
    assert duplicate.raw_output["skill_reference"]["reused"] is True
    messages.pop()
    context.prepare_request(messages, budget_chars=50000)
    assert "SECOND_RULE" in context.read("demo").model_context


def test_selected_reference_survives_multiple_requests_without_changing_user_text(tmp_path):
    context, _ = make_context(tmp_path)
    context.runtime.select(["demo"])
    messages = [Message(role="system", content="BASE"), Message(role="user", content="/demo task")]
    first = context.prepare_request(messages, budget_chars=50000)
    assert "SECOND_RULE" in str(first.messages[-1].content)
    messages.append(Message(role="assistant", content="continue"))
    second = context.prepare_request(messages, budget_chars=50000)
    assert "SECOND_RULE" in str(second.messages[1].content)
    assert messages[1].content == "/demo task"
    assert second.messages[0].content == "BASE"


def test_long_body_is_complete_when_budget_allows_and_page_advances_when_not(tmp_path):
    context, _ = make_context(tmp_path, ("x" * 100 + "\n") * 300 + "MIDDLE_IMPORTANT_RULE")
    context.prepare_request([], budget_chars=50000)
    full = context.read("demo")
    assert "MIDDLE_IMPORTANT_RULE" in full.model_context
    assert full.raw_output["skill_reference"]["complete"]
    context.prepare_request([], budget_chars=2000)
    page = context.read("demo")
    info = page.raw_output["skill_reference"]
    assert page.success and not info["complete"] and info["has_more"]
    assert info["next_offset"] > 0
    assert len(page.model_context) <= 2000
    context.prepare_request([], budget_chars=20)
    exhausted = context.read("demo")
    assert not exhausted.success
    assert "budget" in exhausted.error.lower()


def test_revision_change_rejects_stale_page(tmp_path):
    context, path = make_context(tmp_path)
    context.prepare_request([], budget_chars=50000)
    old = context.read("demo").raw_output["skill_reference"]["revision"]
    path.write_text(path.read_text() + "CHANGED_VERSION\n")
    result = context.read("demo", offset=1, revision=old)
    assert not result.success and "changed" in result.error.lower()


def test_missing_required_returns_diagnostic_without_parent_body(tmp_path):
    context, _ = make_context(tmp_path, extra="required_skills: [missing]\n")
    result = context.read("demo")
    assert not result.success and "missing" in result.error
    assert "FIRST_RULE" not in result.content


def test_same_loader_does_not_share_session_selection(tmp_path):
    context, _ = make_context(tmp_path)
    other = SkillReferenceContext(skill_runtime.SkillRuntime(context.runtime.loader))
    context.runtime.select(["demo"])
    messages = [Message(role="user", content="task")]
    assert "FIRST_RULE" in str(context.prepare_request(messages, budget_chars=50000).messages)
    assert "FIRST_RULE" not in str(other.prepare_request(messages, budget_chars=50000).messages)


def test_large_host_selection_leaves_budget_for_forward_reading(tmp_path):
    context, _ = make_context(tmp_path, ("x" * 100 + "\n") * 70)
    context.runtime.select(["demo"])
    original = [Message(role="user", content="task")]
    projected = context.prepare_request(original, budget_chars=2000)
    added = sum(len(b["text"]) for b in projected.messages[0].content[1:])
    assert added <= 2000
    first = context.read("demo")
    assert first.success
    info = first.raw_output["skill_reference"]
    original.append(Message(role="tool", name="get_skill", tool_call_id="page_1", content=first.model_context))
    context.prepare_request(original, budget_chars=2000)
    next_page = context.read("demo", offset=info["next_offset"], revision=info["revision"])
    assert next_page.success
    assert next_page.raw_output["skill_reference"]["end_offset"] > info["end_offset"]


def test_partial_delivery_has_durable_range_facts(tmp_path):
    context, _ = make_context(tmp_path, ("x" * 100 + "\n") * 70)
    context.prepare_request([], budget_chars=2000)
    page = context.read("demo")
    metadata = page.raw_output["skill_reference"]
    record = context.runtime.log_records()[0]
    assert not record["deliveredComplete"]
    assert record["deliveredRanges"] == [[metadata["offset"], metadata["end_offset"]]]


def test_messages_only_runtime_retains_read_index_after_body_compaction(tmp_path):
    context, _ = make_context(tmp_path)
    result = context.read("demo")
    history = [Message(role="user", content="task"), Message(
        role="tool", name="get_skill", tool_call_id="read", content=result.model_context)]
    restored = SkillReferenceContext(skill_runtime.SkillRuntime(context.runtime.loader))
    restored.observe_history(history)
    assert restored.runtime.active_names == ("demo",)
    compacted = [history[0], Message(role="assistant", content="summary")]
    projection = restored.prepare_request(compacted, budget_chars=50000)
    assert "Use get_skill" in str(projection.messages[0].content)
    assert "FIRST_RULE" not in str(projection.messages)
    assert "FIRST_RULE" in restored.read("demo").model_context


def test_verified_legacy_system_suffix_is_only_removed_from_request_copy(tmp_path):
    from box_agent.tools.skill_preload import build_active_skills_prompt

    context, _ = make_context(tmp_path)
    context.read("demo")
    records = context.runtime.log_records()
    old_system = build_active_skills_prompt("CALLER BASE", {"demo": context.runtime.state.reads["demo"].prompt})
    restored = SkillReferenceContext(skill_runtime.SkillRuntime(context.runtime.loader))
    restored.runtime.restore_records(records)
    restored.runtime.begin_turn()
    history = [Message(role="system", content=old_system), Message(role="user", content="continue")]
    projection = restored.prepare_request(history, budget_chars=50000)
    assert projection.messages[0].content == "CALLER BASE"
    assert "FIRST_RULE" in str(projection.messages[1].content)
    assert history[0].content == old_system
    restored.runtime.begin_turn()
    assert restored.prepare_request(history, budget_chars=50000).messages[0].content == "CALLER BASE"
    unrelated = "CALLER BASE\n\n## Active Skill Instructions\nuser-authored instructions"
    history[0] = Message(role="system", content=unrelated)
    assert restored.prepare_request(history, budget_chars=50000).messages[0].content == unrelated


def test_upgraded_restore_does_not_claim_current_body_verifies_historical_system_suffix(tmp_path):
    from box_agent.tools.skill_preload import build_active_skills_prompt

    context, path = make_context(tmp_path)
    context.read("demo")
    records = context.runtime.log_records()
    old_system = build_active_skills_prompt("CALLER BASE", {"demo": context.runtime.state.reads["demo"].prompt})
    path.write_text(path.read_text().replace("FIRST_RULE", "CURRENT_RULE"))
    context.runtime.loader.maybe_reload()
    current_system = build_active_skills_prompt("CALLER BASE", {"demo": context.runtime.loader.get_skill("demo").to_prompt()})
    restored = SkillReferenceContext(skill_runtime.SkillRuntime(context.runtime.loader))
    restored.runtime.restore_records(records)
    restored.runtime.begin_turn()
    for system in (old_system, current_system):
        history = [Message(role="system", content=system), Message(role="user", content="continue")]
        projection = restored.prepare_request(history, budget_chars=50000)
        assert projection.messages[0].content == system
        assert history[0].content == system
        assert "CURRENT_RULE" in str(projection.messages[1].content)
        assert records[0]["sha256"] in str(projection.messages[1].content)


@pytest.mark.parametrize("invalid", ["missing", "disabled", "broken", "required"])
def test_restore_rejects_unavailable_current_skill_atomically_even_with_caller_fallback(tmp_path, invalid):
    from box_agent.skill_dependencies import SkillDependencyError

    context, path = make_context(tmp_path)
    context.read("demo")
    records = context.runtime.log_records()
    context.runtime.register_reference("retained", "existing reference")
    context.runtime.register_reference("demo", "caller text must not bypass the configured source")
    before = context.runtime.log_records()
    sequence = context.runtime.state.sequence
    selected = context.runtime.state.selected
    if invalid == "missing":
        path.unlink()
    elif invalid == "disabled":
        settings = tmp_path / "settings.json"
        settings.write_text('{"disabledSkillNames":["demo"]}')
        context.runtime.loader = SkillLoader(sources=[(path.parent.parent, "user")], skill_settings_path=settings)
        context.runtime.loader.discover_skills()
    elif invalid == "broken":
        path.write_text("---\nname: [\n---\nbroken\n")
    else:
        path.write_text(path.read_text().replace("description: example", "description: example\nrequired_skills: [missing]"))
    with pytest.raises(SkillDependencyError) as error:
        context.runtime.restore_records(records)
    assert error.value.code == {
        "missing": "SKILL_NOT_FOUND", "disabled": "SKILL_DISABLED",
        "broken": "SKILL_BROKEN", "required": "SKILL_NOT_FOUND",
    }[invalid]
    assert context.runtime.log_records() == before
    assert context.runtime.state.sequence == sequence
    assert context.runtime.state.selected == selected


def test_register_reference_is_idempotent_and_records_actual_upgraded_hash(tmp_path):
    from box_agent.session_log import SessionLog

    log = SessionLog.create(tmp_path / "sessions", session_id="reference", cwd=tmp_path)
    runtime = skill_runtime.SkillRuntime(None, session_log=log)
    runtime.register_reference("demo", "current", expected_hash=sha256(b"historical").hexdigest())
    before = log.path.read_bytes()
    records = runtime.log_records()
    sequence = runtime.state.sequence
    runtime.register_reference("demo", "current")
    assert log.path.read_bytes() == before
    assert runtime.log_records() == records
    assert runtime.state.sequence == sequence
    assert records[0]["sha256"] == sha256(b"current").hexdigest()
    runtime.register_reference("demo", "next")
    assert runtime.state.sequence == sequence + 1
    assert log.replay().skills[0]["sha256"] == sha256(b"next").hexdigest()
    log.close()


def test_restore_preserves_saved_order_and_keeps_range_facts_only_for_unchanged_sources(tmp_path):
    context, path = make_context(tmp_path)
    other_path = path.parent.parent / "other" / "SKILL.md"
    other_path.parent.mkdir()
    other_path.write_text("---\nname: other\ndescription: other\n---\nUNCHANGED_RULE\n")
    context.read("other")
    context.read("demo")
    records = context.runtime.log_records()
    records.reverse()
    path.write_text(path.read_text().replace("FIRST_RULE", "UPGRADED_RULE"))
    restored = SkillReferenceContext(skill_runtime.SkillRuntime(context.runtime.loader))
    restored.runtime.restore_records(records)
    assert list(restored.runtime.state.reads) == ["other", "demo"]
    assert [record["loadOrder"] for record in restored.runtime.log_records()] == [1, 2]
    assert restored.runtime.state.reads["other"].delivered_complete
    assert restored.runtime.state.reads["other"].delivered_ranges
    assert not restored.runtime.state.reads["demo"].delivered_complete
    assert restored.runtime.state.reads["demo"].delivered_ranges == ()
    assert "UPGRADED_RULE" in restored.runtime.state.reads["demo"].prompt


def test_multibyte_body_and_status_obey_actual_reference_budget(tmp_path):
    from box_agent.skill_context import reference_cost

    context, _ = make_context(tmp_path, ("请逐页检查，保留全部原始能力。🙂\n") * 120)
    context.runtime.select(["demo"])
    projection = context.prepare_request([Message(role="user", content="task")], budget_chars=2000)
    page = context.read("demo")
    assert page.success and page.raw_output["skill_reference"]["next_offset"] > 0
    added = "".join(block["text"] for block in projection.messages[0].content[1:])
    assert reference_cost(added) + reference_cost(page.model_context) <= 2000
