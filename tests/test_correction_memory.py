"""Tests for R2+R3 correction memory (C1).

Scenarios required by the brief:
1. Font one-shot success → not stored
2. Repeated failure same fingerprint+subject within window → can store
3. Superseded/invalidated → not hit by default search
4. Reject preference / secrets

Also: header parse/format round-trip for new correction fields.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from box_agent.correction import (
    CorrectionCurator,
    CorrectionDraft,
    CorrectionNotice,
    CorrectionReject,
    CorrectionSubject,
    normalize_error_fingerprint,
)
from box_agent.memory import (
    ContextEntry,
    MemoryManager,
    _format_entry_header,
    _parse_context_text,
    parse_context_file,
    write_context_file,
)
from box_agent.tools.memory_tool import (
    MemoryListCorrectionsTool,
    MemorySupersedeCorrectionTool,
    MemoryWriteCorrectionTool,
)


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def mgr(memory_dir: Path) -> MemoryManager:
    return MemoryManager(memory_dir=str(memory_dir))


def _subject(name: str = "pptx_export", kind: str = "skill", version: str = "1.0") -> CorrectionSubject:
    return CorrectionSubject(kind=kind, name=name, version=version)  # type: ignore[arg-type]


# ── Header round-trip ────────────────────────────────────────


def test_correction_header_roundtrip(memory_dir: Path):
    path = memory_dir / "corrections.md"
    entry = ContextEntry(
        id="ctx_corr_1",
        content="- lesson: reinstall font pack before export\n- symptom: missing CJK glyph",
        created="2026-09-17T10:00:00",
        last_used="2026-09-17T10:00:00",
        hits=1,
        source="explicit",
        confidence=1.0,
        topic="corrections",
        entry_type="correction",
        status="active",
        error_fingerprint="filenotfounderror:_font_not_found",
        subject_kind="skill",
        subject_name="pptx_export",
        subject_version="1.0",
        lesson="reinstall_font_pack_before_export",
        symptom="missing_CJK_glyph",
    )
    write_context_file(path, [entry])
    raw = path.read_text(encoding="utf-8")
    assert "entry_type=correction" in raw
    assert "status=active" in raw
    assert "error_fingerprint=filenotfounderror:_font_not_found" in raw
    assert "subject_kind=skill" in raw
    assert "subject_name=pptx_export" in raw
    assert "subject_version=1.0" in raw
    assert "lesson=reinstall_font_pack_before_export" in raw
    assert "symptom=missing_CJK_glyph" in raw

    parsed = parse_context_file(path)
    assert len(parsed) == 1
    got = parsed[0]
    assert got.entry_type == "correction"
    assert got.status == "active"
    assert got.error_fingerprint == "filenotfounderror:_font_not_found"
    assert got.subject_kind == "skill"
    assert got.subject_name == "pptx_export"
    assert got.subject_version == "1.0"
    assert got.lesson == "reinstall_font_pack_before_export"
    assert got.symptom == "missing_CJK_glyph"

    # format → parse string path
    header = _format_entry_header(got)
    roundtrip = _parse_context_text(header + "\n" + got.content + "\n")
    assert roundtrip[0].entry_type == "correction"
    assert roundtrip[0].status == "active"


def test_legacy_entries_without_correction_keys_still_parse(memory_dir: Path):
    path = memory_dir / "general.md"
    path.write_text(
        "<!-- ctx id=ctx_old created=2026-01-01T00:00:00 last_used=2026-01-01T00:00:00 "
        "hits=0 source=tool confidence=1.00 topic=general -->\n"
        "- ordinary context note\n",
        encoding="utf-8",
    )
    parsed = parse_context_file(path)
    assert len(parsed) == 1
    assert parsed[0].entry_type == ""
    assert parsed[0].status == ""
    assert parsed[0].content == "- ordinary context note"


# ── Scenario 1: font one-shot success never stored ───────────


def test_font_one_shot_env_fix_not_stored(mgr: MemoryManager):
    curator = CorrectionCurator(mgr, repeat_window=timedelta(hours=6), repeat_count=2)
    draft = curator.observe_failure(
        subject=_subject(kind="env", name="fonts"),
        raw_error="FileNotFoundError: /usr/share/fonts/NotoSansCJK.ttc missing",
        one_shot_env_fix=True,
        lesson="download NotoSansCJK once",
    )
    assert draft is None
    assert mgr.list_corrections() == []
    corrections_path = mgr.context_dir / "corrections.md"
    assert not corrections_path.exists() or corrections_path.read_text(encoding="utf-8").strip() == ""


# ── Scenario 2: repeated failure → can store ─────────────────


def test_repeated_failure_same_fingerprint_can_store(mgr: MemoryManager):
    curator = CorrectionCurator(mgr, repeat_window=timedelta(hours=6), repeat_count=2)
    subject = _subject(kind="tool", name="bash", version="")
    err = "PermissionError: [Errno 13] Permission denied: '/tmp/abc123/out.txt' at 2026-09-17T12:00:00"

    first = curator.observe_failure(
        subject=subject,
        raw_error=err,
        lesson="chmod the output directory before writing",
        symptom="permission denied on output path",
    )
    assert first is None

    second = curator.observe_failure(
        subject=subject,
        raw_error="PermissionError: [Errno 13] Permission denied: '/var/tmp/xyz999/out.txt' at 2026-09-17T12:05:00",
        lesson="chmod the output directory before writing",
        symptom="permission denied on output path",
    )
    assert second is not None
    assert second.error_fingerprint
    assert second.subject.name == "bash"

    entry = mgr.write_correction(second)
    assert entry.entry_type == "correction"
    assert entry.status == "active"
    assert entry.topic == "corrections"
    path = mgr.context_dir / "corrections.md"
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    assert "entry_type=correction" in raw
    assert "chmod the output directory" in raw.lower() or "lesson:" in raw

    listed = mgr.list_corrections()
    assert len(listed) == 1
    assert listed[0].id == entry.id

    # Non-skill subjects (tool/path/env/workflow) are supported
    for kind in ("path_pattern", "env", "workflow"):
        d = CorrectionDraft(
            lesson=f"fix for {kind}",
            symptom="boom",
            subject=CorrectionSubject(kind=kind, name=f"s-{kind}"),  # type: ignore[arg-type]
            error_fingerprint=normalize_error_fingerprint(f"{kind} failure xyz"),
            source="explicit",
        )
        e = mgr.write_correction(d)
        assert e.subject_kind == kind


# ── Scenario 3: superseded/deleted skipped by default search ─


def test_superseded_and_deleted_skipped_by_default_search(mgr: MemoryManager):
    draft = CorrectionDraft(
        lesson="retry with --legacy-peer-deps when npm ERESOLVE appears",
        symptom="npm ERESOLVE unable to resolve dependency tree",
        subject=_subject(name="npm_install", kind="workflow"),
        error_fingerprint=normalize_error_fingerprint("npm ERR! ERESOLVE unable to resolve dependency tree"),
        source="explicit",
    )
    entry = mgr.write_correction(draft)
    hits = mgr.search("ERESOLVE")
    assert any("legacy-peer-deps" in h for h in hits)

    mgr.supersede_correction(entry.id)
    assert mgr.search("ERESOLVE") == []
    # Explicit opt-in still finds it
    assert any("legacy-peer-deps" in h for h in mgr.search("ERESOLVE", include_inactive_corrections=True))

    # Re-write a fresh active one, then soft-delete
    entry2 = mgr.write_correction(
        CorrectionDraft(
            lesson="clear npm cache then reinstall",
            symptom="npm ERESOLVE again",
            subject=_subject(name="npm_install", kind="workflow", version="2"),
            error_fingerprint=normalize_error_fingerprint("npm ERR! code ERESOLVE cache"),
            source="explicit",
        )
    )
    assert any("clear npm cache" in h for h in mgr.search("ERESOLVE"))
    mgr.delete_correction(entry2.id)
    assert not any("clear npm cache" in h for h in mgr.search("ERESOLVE"))

    # Ordinary context still searchable
    mgr.append_context("- project uses ERESOLVE workaround docs in README", topic="general")
    assert any("README" in h for h in mgr.search("ERESOLVE"))


def test_supersede_by_subject_upgrade(mgr: MemoryManager):
    old = mgr.write_correction(
        CorrectionDraft(
            lesson="old skill workaround",
            symptom="crash",
            subject=_subject(name="pptx_export", kind="skill", version="1.0"),
            error_fingerprint=normalize_error_fingerprint("KeyError: slide_layout"),
            source="explicit",
        )
    )
    assert old.status == "active"
    n = mgr.supersede_by_subject_upgrade(
        CorrectionSubject(kind="skill", name="pptx_export", version="2.0")
    )
    assert n == 1
    assert mgr.list_corrections() == []
    assert mgr.list_corrections(include_inactive=True)[0].status == "superseded"


# ── Scenario 4: reject preference / secrets ──────────────────


def test_reject_preference_and_secrets(mgr: MemoryManager):
    curator = CorrectionCurator(mgr)

    pref = CorrectionDraft(
        lesson="User prefers dark mode in the IDE",
        symptom="theme",
        subject=_subject(name="editor", kind="tool"),
        error_fingerprint="theme-mismatch",
        source="explicit",
    )
    with pytest.raises(CorrectionReject):
        curator.reject_if_forbidden(pref)

    secret = CorrectionDraft(
        lesson="store api_key sk-abcdefghijklmnopqrst in config",
        symptom="auth failed",
        subject=_subject(name="api", kind="tool"),
        error_fingerprint="401 unauthorized",
        source="explicit",
    )
    with pytest.raises(CorrectionReject):
        curator.reject_if_forbidden(secret)

    with pytest.raises(CorrectionReject):
        curator.reject_if_forbidden(
            CorrectionDraft(
                lesson="ok lesson",
                symptom="ok",
                subject=_subject(),
                error_fingerprint="x",
                source="explicit",
            ),
            content_class="preference",
        )


@pytest.mark.asyncio
async def test_write_correction_tool_refuses_secrets_and_uses_draft_confirm(mgr: MemoryManager):
    tool = MemoryWriteCorrectionTool(mgr)
    empty = await MemoryListCorrectionsTool(mgr).execute()
    assert empty.content == "还没有可复用的纠错记忆。"

    bad = await tool.execute(
        lesson="save the password hunter2 for deploy",
        symptom="auth",
        error_fingerprint="login failed",
        subject_kind="tool",
        subject_name="deploy",
    )
    assert bad.success is False
    assert bad.error == "这条不算可复用纠错（偏好/敏感/一次性），没记下。"

    ok = await tool.execute(
        lesson="pin cryptography==42.0.0 when wheel build fails on musl",
        symptom="wheel build failed",
        error_fingerprint="ERROR: Failed building wheel for cryptography",
        subject_kind="env",
        subject_name="pip",
    )
    assert ok.success is True
    assert ok.content.startswith("纠错记忆草稿已创建：")
    draft_id = ok.raw_output["id"]

    listed = await MemoryListCorrectionsTool(mgr).execute(status="draft")
    assert listed.success is True
    assert draft_id in listed.content

    # Draft not visible to default search
    assert mgr.search("cryptography") == []

    confirmed = await tool.execute(confirm=True, draft_id=draft_id)
    assert confirmed.success is True
    assert confirmed.content.startswith("纠错记忆已确认并生效：")
    assert confirmed.raw_output["status"] == "active"
    assert any("cryptography" in h.lower() for h in mgr.search("cryptography"))

    superseded = await MemorySupersedeCorrectionTool(mgr).execute(entry_id=draft_id)
    assert superseded.success is True
    assert superseded.content.startswith("纠错记忆已作废：")
    assert mgr.search("cryptography") == []


def test_normalize_error_fingerprint_strips_noise():
    a = normalize_error_fingerprint(
        "FileNotFoundError: /home/user/proj/fonts/Noto.ttc missing at 2026-09-17T10:11:12"
    )
    b = normalize_error_fingerprint(
        "FileNotFoundError: /var/other/fonts/Noto.ttc missing at 2026-09-18T01:02:03"
    )
    assert a == b
    assert "/home" not in a
    assert "2026" not in a


# ── Runtime hook: observe_failure via tool-result path ───────


def test_runtime_hook_two_failures_writes_correction(mgr: MemoryManager):
    from box_agent.correction import notify_tool_failure_for_correction

    err = "PermissionError: [Errno 13] Permission denied: '/tmp/out.txt'"
    first_tip = notify_tool_failure_for_correction(mgr, tool_name="bash", raw_error=err)
    assert first_tip is None
    assert mgr.list_corrections() == []

    second_tip = notify_tool_failure_for_correction(mgr, tool_name="bash", raw_error=err)
    listed = mgr.list_corrections()
    assert len(listed) == 1
    assert listed[0].status == "active"
    assert listed[0].subject_kind == "tool"
    assert listed[0].subject_name == "bash"
    assert listed[0].source == "auto"
    assert isinstance(second_tip, CorrectionNotice)
    assert second_tip.lesson.startswith("When seeing '")
    assert second_tip.subject_kind == "tool"
    assert second_tip.subject_name == "bash"


def test_runtime_hook_one_failure_no_write(mgr: MemoryManager):
    from box_agent.correction import notify_tool_failure_for_correction

    tip = notify_tool_failure_for_correction(
        mgr,
        tool_name="bash",
        raw_error="OSError: broken pipe on write",
    )
    assert tip is None
    assert mgr.list_corrections() == []


def test_runtime_hook_one_shot_env_fix_never_writes(mgr: MemoryManager):
    from box_agent.correction import notify_tool_failure_for_correction

    msg = "Missing font NotoSansCJK; downloaded font successfully and retry ok"
    first_tip = notify_tool_failure_for_correction(
        mgr, tool_name="pptx_export", raw_error=msg,
    )
    second_tip = notify_tool_failure_for_correction(
        mgr, tool_name="pptx_export", raw_error=msg,
    )
    assert first_tip is None
    assert second_tip is None
    assert mgr.list_corrections() == []


def test_process_tool_result_invokes_correction_observer(mgr: MemoryManager, tmp_path: Path):
    from box_agent.correction import notify_tool_failure_for_correction
    from box_agent.events import ProgressEvent
    from box_agent.schema import Message
    from box_agent.tool_result_storage import ToolResultStorage
    from box_agent.tools.base import ToolResult
    from box_agent.tools.engine.results import ToolResultPipelineInput, process_tool_result

    def observer(tool_name, result, visible_error):
        return notify_tool_failure_for_correction(
            mgr,
            tool_name=tool_name,
            raw_error=str(visible_error or result.error or ""),
            content=str(result.content or ""),
        )

    storage = ToolResultStorage(str(tmp_path / "results"))
    err = "RuntimeError: repeated widget failure xyz"
    outcomes = []
    for _ in range(2):
        outcomes.append(
            process_tool_result(
                ToolResultPipelineInput(
                    messages=[],
                    tool_call_id="c1",
                    tool_name="widget",
                    arguments={},
                    result=ToolResult(success=False, content="", error=err),
                    visible_content="",
                    visible_error=err,
                    result_storage=storage,
                    correction_observer=observer,
                    step=7,
                )
            )
        )
    listed = mgr.list_corrections()
    assert len(listed) == 1
    assert listed[0].subject_name == "widget"
    assert not any(isinstance(event, ProgressEvent) for event in outcomes[0].events)
    progress = [event for event in outcomes[1].events if isinstance(event, ProgressEvent)]
    assert len(progress) == 1
    assert progress[0].step == 7
    assert progress[0].content.startswith("已记下纠错：")
    assert progress[0].content.endswith(
        " · tool:widget\n说「已修好」可作废；说「我的纠错记忆」可查看。"
    )


def test_memory_manager_lazy_correction_curator(mgr: MemoryManager):
    first = mgr.correction_curator
    second = mgr.correction_curator
    assert first is second
    assert first.memory_manager is mgr
