"""Tests for session-local context resource coverage."""

from box_agent.context_resources import (
    ContextResourceLedger,
    ResourceClass,
    ResourceDescriptor,
    build_resource_receipt,
    classify_read_resource,
)
from box_agent.schema import Message


def _descriptor(
    *,
    start: int = 1,
    end: int = 100,
    version: str = "a" * 64,
    resource_class: ResourceClass = ResourceClass.RECONSTRUCTABLE,
) -> ResourceDescriptor:
    return ResourceDescriptor(
        resource_id="/workspace/reference.md",
        content_version=version,
        start_line=start,
        end_line=end,
        total_lines=200,
        resource_class=resource_class,
    )


def _tool_message(tool_call_id: str, content: str) -> Message:
    return Message(
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
        name="read_file",
    )


def test_receipt_never_contributes_coverage() -> None:
    ledger = ContextResourceLedger()
    descriptor = _descriptor()
    source_message = _tool_message("source-1", "complete body")
    receipt_message = _tool_message("receipt-1", "short receipt")
    messages = [source_message, receipt_message]

    ledger.register_full_source("source-1", descriptor, "complete body")
    ledger.register_receipt("receipt-1", ["source-1"])
    assert ledger.covering_source_ids(descriptor, messages) == ("source-1",)

    messages.pop(0)
    assert ledger.covering_source_ids(descriptor, messages) == ()
    assert ledger.source_ids == ()
    assert ledger.receipt_ids == ("receipt-1",)


def test_interval_union_requires_live_complete_sources() -> None:
    ledger = ContextResourceLedger()
    first = _descriptor(start=1, end=100)
    second = _descriptor(start=101, end=200)
    requested = _descriptor(start=1, end=200)
    messages = [
        _tool_message("source-1", "page one"),
        _tool_message("source-2", "page two"),
    ]
    ledger.register_full_source("source-1", first, "page one")
    ledger.register_full_source("source-2", second, "page two")

    assert ledger.covering_source_ids(requested, messages) == (
        "source-1",
        "source-2",
    )

    messages[0] = _tool_message("source-1", "compacted")
    assert ledger.covering_source_ids(requested, messages) == ()


def test_changed_version_does_not_hit_old_coverage() -> None:
    ledger = ContextResourceLedger()
    old = _descriptor(version="a" * 64)
    new = _descriptor(version="b" * 64)
    messages = [_tool_message("source-1", "complete body")]
    ledger.register_full_source("source-1", old, "complete body")

    assert ledger.covering_source_ids(new, messages) == ()


def test_summary_epoch_rotation_clears_sources_and_receipts() -> None:
    ledger = ContextResourceLedger()
    descriptor = _descriptor()
    ledger.register_full_source("source-1", descriptor, "complete body")
    ledger.register_receipt("receipt-1", ["source-1"])

    ledger.rotate_epoch()

    assert ledger.epoch == 1
    assert ledger.source_ids == ()
    assert ledger.receipt_ids == ()


def test_resource_receipt_is_bounded() -> None:
    descriptor = ResourceDescriptor(
        resource_id="/workspace/" + "very-long-directory/" * 30 + "reference.md",
        content_version="a" * 64,
        start_line=1,
        end_line=100,
        total_lines=100,
        resource_class=ResourceClass.RECONSTRUCTABLE,
    )

    receipt = build_resource_receipt(descriptor, ["source-1", "source-2"])

    assert len(receipt) <= 300
    assert "refresh=true" in receipt


def test_requested_skill_reference_path_stays_pinned_after_resolution() -> None:
    assert (
        classify_read_resource(
            "/managed/cache/contract.md",
            requested_path="/user/skills/ppt/references/contract.md",
        )
        is ResourceClass.INSTRUCTION_PINNED
    )


def test_requested_skill_workflow_path_stays_pinned_after_resolution() -> None:
    assert (
        classify_read_resource(
            "/managed/cache/generate-pptx.md",
            requested_path="/user/skills/ppt-master/workflows/generate-pptx.md",
        )
        is ResourceClass.INSTRUCTION_PINNED
    )


def test_managed_builtin_skill_instructions_stay_pinned() -> None:
    root = "/profile/builtin-skills/service/snapshots/version/pptx"
    for relative in ("SKILL.md", "references/contract.md", "workflows/export.md"):
        assert classify_read_resource(f"{root}/{relative}") is ResourceClass.INSTRUCTION_PINNED
