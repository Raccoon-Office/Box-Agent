"""Disk-backed boundary regressions for session Skill references."""

import json

import pytest

from box_agent.schema import Message
from box_agent.skill_context import SkillReferenceContext, read_reference, render_reference
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_loader import SkillLoader


def write_skill(tmp_path, name, body, *, required=()):
    path = tmp_path / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    requirements = f"required_skills: {json.dumps(list(required))}\n" if required else ""
    path.write_text(
        f"---\nname: {name}\ndescription: boundary example\n{requirements}---\n{body}\n",
        encoding="utf-8",
    )
    return path


def make_context(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"disabledSkillNames":[]}', encoding="utf-8")
    loader = SkillLoader(
        sources=[(tmp_path / "skills", "user")], skill_settings_path=settings
    )
    loader.discover_skills()
    return SkillReferenceContext(SkillRuntime(loader)), settings


def tool_message(result, call_id):
    return Message(
        role="tool", name="get_skill", tool_call_id=call_id, content=result.model_context
    )


def added_text(projection, user_index=0):
    content = projection.messages[user_index].content
    return "" if isinstance(content, str) else "".join(
        block["text"] for block in content[1:] if block.get("type") == "text"
    )


@pytest.mark.parametrize("invalidation", ["disabled", "deleted", "broken"])
def test_transitive_required_invalidation_retires_previously_read_parent(tmp_path, invalidation):
    write_skill(tmp_path, "parent", "PARENT_METHOD", required=["middle"])
    write_skill(tmp_path, "middle", "MIDDLE_METHOD", required=["leaf"])
    leaf = write_skill(tmp_path, "leaf", "LEAF_METHOD")
    context, settings = make_context(tmp_path)
    messages = [Message(role="user", content="task")]
    context.prepare_request(messages, budget_chars=50000)
    first = context.read("parent")
    assert first.success
    messages.append(tool_message(first, "parent-read"))
    if invalidation == "disabled":
        settings.write_text('{"disabledSkillNames":["leaf"]}', encoding="utf-8")
    elif invalidation == "deleted":
        leaf.unlink()
    else:
        leaf.write_text("---\nname: [invalid\n---\nBROKEN_LEAF\n", encoding="utf-8")

    projection = context.prepare_request(messages, budget_chars=50000)

    assert "parent" not in context.runtime.active_names
    status = "\n".join(projection.diagnostics)
    assert "parent" in status and "leaf" in status and "historical" in status
    assert "historical" in added_text(projection)
    reread = context.read("parent")
    assert not reread.success
    assert reread.raw_output["code"] == {
        "disabled": "SKILL_DISABLED", "deleted": "SKILL_NOT_FOUND", "broken": "SKILL_BROKEN"
    }[invalidation]
    assert "PARENT_METHOD" not in (reread.model_context or "")


def test_selected_reference_with_budget_smaller_than_host_prefix_stays_bounded(tmp_path):
    write_skill(tmp_path, "small", "SMALL_METHOD")
    context, _ = make_context(tmp_path)
    context.runtime.select(["small", "missing"])
    messages = [Message(role="system", content="BASE"), Message(role="user", content="task")]

    projection = context.prepare_request(messages, budget_chars=20)

    assert len(added_text(projection, 1)) <= 20
    assert not projection.references
    assert "SMALL_METHOD" not in added_text(projection, 1)
    assert messages[1].content == "task"
    assert projection.messages[0].content == "BASE"


def test_oversized_selection_keeps_all_selected_skills_readable_without_pinning_short_body(tmp_path):
    write_skill(tmp_path, "long", ("LONG_METHOD_" + "x" * 100 + "\n") * 70)
    write_skill(tmp_path, "short", "SHORT_METHOD")
    context, _ = make_context(tmp_path)
    context.runtime.select(["long", "short"])
    messages = [Message(role="user", content="task")]

    projection = context.prepare_request(messages, budget_chars=2000)

    assert "SHORT_METHOD" not in added_text(projection)
    assert "LONG_METHOD_" not in added_text(projection)
    assert not projection.references
    assert any("long" in diagnostic and "paged" in diagnostic for diagnostic in projection.diagnostics)
    assert "short" in added_text(projection) and "get_skill" in added_text(projection)
    assert context.runtime.log_records() == []
    assert "SHORT_METHOD" in context.read("short").model_context


def test_small_selection_leaves_large_selection_a_path_to_later_pages(tmp_path):
    write_skill(tmp_path, "long", ("LONG_METHOD_" + "x" * 100 + "\n") * 70)
    write_skill(tmp_path, "short", "SHORT_METHOD")
    context, _ = make_context(tmp_path)
    context.runtime.select(["long", "short"])
    messages = [Message(role="user", content="task")]
    offset = 0
    revision = None
    for index in range(100):
        projection = context.prepare_request(messages, budget_chars=2000)
        page = context.read("long", offset=offset, revision=revision)
        assert page.success, (
            f"The selected large Skill cannot advance from line {offset}: {page.error}; "
            f"host projection already spent {len(added_text(projection))}/2000 characters"
        )
        metadata = page.raw_output["skill_reference"]
        messages.append(tool_message(page, f"long-page-{index}"))
        if metadata["complete"]:
            break
        assert metadata["next_offset"] > offset
        offset, revision = metadata["next_offset"], metadata["revision"]
    else:
        pytest.fail("The selected large Skill never completed within 100 pages")


def test_reuse_receipt_also_respects_remaining_reference_budget(tmp_path):
    write_skill(tmp_path, "demo", "VISIBLE_METHOD")
    context, _ = make_context(tmp_path)
    context.prepare_request([], budget_chars=50000)
    original = context.read("demo")
    context.prepare_request([tool_message(original, "visible-body")], budget_chars=20)

    receipt = context.read("demo")

    assert len(receipt.model_context or "") <= 20


def test_selected_same_source_update_and_disable_use_current_status(tmp_path):
    path = write_skill(tmp_path, "demo", "ORIGINAL_METHOD")
    context, settings = make_context(tmp_path)
    context.runtime.select(["demo"])
    messages = [Message(role="user", content="task")]
    first = context.prepare_request(messages, budget_chars=50000)
    old_revision = first.references[0]["revision"]
    path.write_text(path.read_text(encoding="utf-8") + "UPDATED_METHOD\n", encoding="utf-8")

    updated = context.prepare_request(messages, budget_chars=50000)

    assert "UPDATED_METHOD" in added_text(updated)
    assert updated.references[0]["revision"] != old_revision
    assert any("demo" in diagnostic and "changed" in diagnostic for diagnostic in updated.diagnostics)
    assert context.runtime.log_records()[0]["sha256"] == updated.references[0]["revision"]
    settings.write_text('{"disabledSkillNames":["demo"]}', encoding="utf-8")
    disabled = context.prepare_request(messages, budget_chars=50000)
    assert not disabled.references
    assert "UPDATED_METHOD" not in added_text(disabled)
    assert any("demo" in diagnostic and "disabled" in diagnostic for diagnostic in disabled.diagnostics)
    assert "demo" not in context.runtime.active_names
    assert not context.read("demo").success


@pytest.mark.parametrize("summary_format", ["plain", "reference_receipt"])
def test_completed_pages_replaced_by_summary_do_not_count_as_visible_body(tmp_path, summary_format):
    write_skill(tmp_path, "paged", ("PAGE_METHOD_" + "x" * 100 + "\n") * 35)
    context, _ = make_context(tmp_path)
    messages = [Message(role="user", content="task")]
    offset = 0
    revision = None
    for index in range(100):
        context.prepare_request(messages, budget_chars=2000)
        page = context.read("paged", offset=offset, revision=revision)
        assert page.success
        metadata = page.raw_output["skill_reference"]
        messages.append(tool_message(page, f"page-{index}"))
        if metadata["complete"]:
            break
        assert metadata["next_offset"] > offset
        offset, revision = metadata["next_offset"], metadata["revision"]
    else:
        pytest.fail("Skill pagination did not terminate")
    assert index > 0
    record = context.runtime.log_records()[0]
    assert record["deliveredComplete"]
    assert len(record["deliveredRanges"]) > 1
    context.prepare_request(messages, budget_chars=2000)
    assert context.read("paged").raw_output["skill_reference"]["reused"]
    summary = f"Read Skill paged revision {metadata['revision']}; all pages were delivered."
    if summary_format == "reference_receipt":
        summary = render_reference(metadata, summary)
    compacted = [messages[0], Message(role="tool", name="get_skill", tool_call_id="summary", content=summary)]

    context.prepare_request(compacted, budget_chars=2000)
    reread = context.read("paged")

    assert reread.success
    assert not reread.raw_output["skill_reference"].get("reused", False)
    assert not reread.raw_output["skill_reference"]["complete"]
    assert read_reference(reread.model_context) is not None
    assert "PAGE_METHOD_" in reread.model_context


def test_shared_loader_does_not_share_read_coverage_or_budget(tmp_path):
    write_skill(tmp_path, "demo", ("SHARED_METHOD_" + "x" * 100 + "\n") * 30)
    first, _ = make_context(tmp_path)
    second = SkillReferenceContext(SkillRuntime(first.runtime.loader))
    first.prepare_request([], budget_chars=50000)
    complete = first.read("demo")
    assert complete.success and complete.raw_output["skill_reference"]["complete"]
    first.prepare_request([tool_message(complete, "first-session")], budget_chars=50000)
    assert first.read("demo").raw_output["skill_reference"]["reused"]
    first.prepare_request([], budget_chars=20)
    assert not first.read("demo").success
    assert second.runtime.log_records() == []
    assert second.runtime.active_names == ()
    assert second.runtime.turn_deliveries == {}

    second.prepare_request([Message(role="user", content="separate task")], budget_chars=2000)
    page = second.read("demo")

    assert page.success
    assert not page.raw_output["skill_reference"].get("reused", False)
    assert not page.raw_output["skill_reference"]["complete"]
    assert "SHARED_METHOD_" in page.model_context
    assert first.runtime.log_records()[0]["deliveredComplete"]
    assert not second.runtime.log_records()[0]["deliveredComplete"]
