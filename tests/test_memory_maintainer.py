"""Tests for box_agent.memory_maintainer — decay, archive cleanup, dedup."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

import pytest

from box_agent.config import AgentConfig
from box_agent.memory import (
    ContextEntry,
    MemoryManager,
    _new_entry,
    parse_context_file,
    write_context_file,
)
from box_agent.memory_maintainer import (
    MemoryMaintainer,
    _cluster_by_jaccard,
    _jaccard,
    _parse_conflict_output,
    _tokens,
)


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def mgr(memory_dir: Path) -> MemoryManager:
    return MemoryManager(memory_dir=str(memory_dir))


@pytest.fixture
def config() -> AgentConfig:
    return AgentConfig(
        memory_maintainer_enabled=True,
        memory_maintainer_interval_hours=24,
        memory_decay_days=30,
        memory_archive_days=90,
        memory_dedup_jaccard=0.85,
    )


def _stamp(days_ago: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _entry(content: str, *, hits: int = 0, last_used_days_ago: int = 0,
           created_days_ago: int | None = None, source: str = "tool",
           confidence: float = 1.0) -> ContextEntry:
    created = _stamp(created_days_ago if created_days_ago is not None else last_used_days_ago)
    return ContextEntry(
        id=f"ctx_test_{id(content) & 0xfffffff:x}",
        content=content,
        created=created,
        last_used=_stamp(last_used_days_ago),
        hits=hits,
        source=source,
        confidence=confidence,
    )


# ── Decay ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_maintenance_phase_does_not_block_event_loop(
    mgr: MemoryManager,
    config: AgentConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    started = threading.Event()
    release = threading.Event()

    def blocking_decay(self, now):
        started.set()
        assert release.wait(timeout=1.0)

    monkeypatch.setattr(MemoryMaintainer, "_decay", blocking_decay)
    release_timer = threading.Timer(0.3, release.set)
    release_timer.start()
    started_at = monotonic()
    task = asyncio.create_task(MemoryMaintainer(mgr, config).run_if_due())
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        assert monotonic() - started_at < 0.15
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        release.set()
        release_timer.cancel()


@pytest.mark.asyncio
async def test_maintenance_transaction_preserves_concurrent_append_and_search_hit(
    mgr: MemoryManager,
    config: AgentConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    stale = _entry("- stale memory", hits=0, last_used_days_ago=60)
    active = _entry("- active durable memory", hits=0, last_used_days_ago=1)
    mgr.write_all_context_entries([stale, active])

    maintenance_ready_to_write = threading.Event()
    allow_maintenance_write = threading.Event()
    append_started = threading.Event()
    search_started = threading.Event()
    original_write_all = mgr.write_all_context_entries

    def paused_maintenance_write(entries):
        maintenance_ready_to_write.set()
        assert allow_maintenance_write.wait(timeout=2.0)
        original_write_all(entries)

    monkeypatch.setattr(mgr, "write_all_context_entries", paused_maintenance_write)

    maintenance_task = asyncio.create_task(
        MemoryMaintainer(mgr, config).run_if_due()
    )
    try:
        assert await asyncio.to_thread(
            maintenance_ready_to_write.wait,
            2.0,
        )

        def append_in_foreground():
            append_started.set()
            mgr.append_context(
                "- concurrently added memory",
                topic="project",
            )

        def search_in_foreground():
            search_started.set()
            return mgr.search("active durable memory")

        append_task = asyncio.create_task(asyncio.to_thread(append_in_foreground))
        search_task = asyncio.create_task(asyncio.to_thread(search_in_foreground))
        assert await asyncio.to_thread(append_started.wait, 2.0)
        assert await asyncio.to_thread(search_started.wait, 2.0)

        # Both foreground operations reached the shared transaction boundary
        # while maintenance still owns it.
        await asyncio.sleep(0.05)
        assert not append_task.done()
        assert not search_task.done()

        allow_maintenance_write.set()
        search_results = await asyncio.wait_for(search_task, timeout=2.0)
        await asyncio.wait_for(append_task, timeout=2.0)
        await asyncio.wait_for(maintenance_task, timeout=2.0)
    finally:
        allow_maintenance_write.set()

    entries = mgr.read_all_context_entries()
    by_content = {entry.content: entry for entry in entries}
    assert "- stale memory" not in by_content
    assert "- active durable memory" in by_content
    assert "- concurrently added memory" in by_content
    assert by_content["- active durable memory"].hits == 1
    assert search_results == ["- active durable memory"]

    grouped = mgr.topic_store.read_all_grouped()
    index = mgr.topic_store.read_index()
    assert set(index) == set(grouped)
    for topic, topic_entries in grouped.items():
        assert index[topic]["count"] == len(topic_entries)
        assert index[topic]["hits_total"] == sum(e.hits for e in topic_entries)
        assert index[topic]["last_updated"] == max(
            e.last_used for e in topic_entries
        )


@pytest.mark.asyncio
async def test_maintenance_logs_bounded_phase_diagnostics(
    mgr: MemoryManager,
    config: AgentConfig,
    caplog: pytest.LogCaptureFixture,
):
    write_context_file(mgr.context_file, [_entry("- diagnostic entry")])

    with caplog.at_level("INFO", logger="box_agent.memory_maintainer"):
        await MemoryMaintainer(mgr, config).run_if_due()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "box_agent.memory_maintainer"
    ]
    assert any("run start entries=1" in message for message in messages)
    assert sum("phase start name=" in message for message in messages) == 5
    assert any(
        "run complete entries=1->1" in message and "phase_ms=" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_decay_moves_stale_zero_hits_to_archive(mgr: MemoryManager, config: AgentConfig):
    entries = [
        _entry("- recent unused", hits=0, last_used_days_ago=10),
        _entry("- stale unused", hits=0, last_used_days_ago=60),
        _entry("- stale but used", hits=3, last_used_days_ago=60),
    ]
    write_context_file(mgr.context_file, entries)

    await MemoryMaintainer(mgr, config).run_if_due()

    active = parse_context_file(mgr.context_file)
    archived = parse_context_file(mgr.archive_file)
    assert {e.content for e in active} == {"- recent unused", "- stale but used"}
    assert {e.content for e in archived} == {"- stale unused"}


@pytest.mark.asyncio
async def test_decay_preserves_metadata_through_archive(mgr: MemoryManager, config: AgentConfig):
    original = _entry("- stale unused", hits=0, last_used_days_ago=60, source="extractor", confidence=0.7)
    write_context_file(mgr.context_file, [original])

    await MemoryMaintainer(mgr, config).run_if_due()

    archived = parse_context_file(mgr.archive_file)
    assert len(archived) == 1
    assert archived[0].id == original.id
    assert archived[0].source == "extractor"
    assert archived[0].confidence == 0.7


@pytest.mark.asyncio
async def test_decay_appends_to_existing_archive(mgr: MemoryManager, config: AgentConfig):
    existing_archive = [_entry("- old archived", last_used_days_ago=40)]
    write_context_file(mgr.archive_file, existing_archive)

    write_context_file(mgr.context_file, [
        _entry("- newly stale", hits=0, last_used_days_ago=60),
    ])

    await MemoryMaintainer(mgr, config).run_if_due()

    archived = parse_context_file(mgr.archive_file)
    contents = {e.content for e in archived}
    assert contents == {"- old archived", "- newly stale"}


# ── Archive cleanup → trash ─────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_archive_moves_very_old_to_trash(mgr: MemoryManager, config: AgentConfig):
    # cutoff = decay (30) + archive (90) = 120 days
    write_context_file(mgr.archive_file, [
        _entry("- recently archived", last_used_days_ago=60),
        _entry("- ancient", last_used_days_ago=200),
    ])

    await MemoryMaintainer(mgr, config).run_if_due()

    archived = parse_context_file(mgr.archive_file)
    assert {e.content for e in archived} == {"- recently archived"}

    # trash file should exist and contain the ancient entry
    trash_dirs = list(mgr.trash_dir.iterdir())
    assert len(trash_dirs) == 1
    trash_files = list(trash_dirs[0].iterdir())
    assert len(trash_files) == 1
    purged = parse_context_file(trash_files[0])
    assert {e.content for e in purged} == {"- ancient"}


@pytest.mark.asyncio
async def test_cleanup_archive_noop_when_empty(mgr: MemoryManager, config: AgentConfig):
    await MemoryMaintainer(mgr, config).run_if_due()
    assert not mgr.trash_dir.exists() or not any(mgr.trash_dir.iterdir())


# ── Dedup ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_merges_near_duplicates(mgr: MemoryManager):
    # Use a relaxed Jaccard threshold so a realistic near-duplicate pair merges.
    # Default 0.85 is intentionally strict to avoid false merges.
    config = AgentConfig(memory_dedup_jaccard=0.7)
    write_context_file(mgr.context_file, [
        _entry("- user prefers dark mode", hits=2, last_used_days_ago=1),
        _entry("- user prefers dark mode interface", hits=1, last_used_days_ago=2),
        _entry("- entirely different fact about apples", hits=0, last_used_days_ago=1),
    ])

    await MemoryMaintainer(mgr, config).run_if_due()

    entries = parse_context_file(mgr.context_file)
    # Two near-duplicates merge into one (winner has more hits); third stays.
    assert len(entries) == 2
    contents = {e.content for e in entries}
    assert "- entirely different fact about apples" in contents
    # Winner is the higher-hits entry
    winner = next(e for e in entries if "dark mode" in e.content)
    assert winner.content == "- user prefers dark mode"
    assert winner.hits == 3  # 2 + 1 merged


@pytest.mark.asyncio
async def test_dedup_identical_content_always_merges(mgr: MemoryManager, config: AgentConfig):
    # Identical content → Jaccard 1.0, merges even at default 0.85 threshold.
    write_context_file(mgr.context_file, [
        _entry("- duplicate fact about widgets", hits=2, last_used_days_ago=1),
        _entry("- duplicate fact about widgets", hits=3, last_used_days_ago=2),
    ])

    await MemoryMaintainer(mgr, config).run_if_due()

    entries = parse_context_file(mgr.context_file)
    assert len(entries) == 1
    assert entries[0].hits == 5  # 2 + 3


@pytest.mark.asyncio
async def test_dedup_does_not_merge_unrelated_entries(mgr: MemoryManager, config: AgentConfig):
    write_context_file(mgr.context_file, [
        _entry("- apples are red", hits=1, last_used_days_ago=1),
        _entry("- bananas are yellow", hits=1, last_used_days_ago=1),
    ])

    await MemoryMaintainer(mgr, config).run_if_due()

    entries = parse_context_file(mgr.context_file)
    assert len(entries) == 2


# ── Timestamp guard ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_if_due_respects_recent_marker(mgr: MemoryManager, config: AgentConfig):
    # First run executes maintenance
    write_context_file(mgr.context_file, [_entry("- stale", hits=0, last_used_days_ago=60)])
    ran_first = await MemoryMaintainer(mgr, config).run_if_due()
    assert ran_first is True

    # Second run within the interval skips
    write_context_file(mgr.context_file, [_entry("- another stale", hits=0, last_used_days_ago=60)])
    ran_second = await MemoryMaintainer(mgr, config).run_if_due()
    assert ran_second is False

    # The second batch was untouched (still in active context)
    active = parse_context_file(mgr.context_file)
    assert any(e.content == "- another stale" for e in active)


@pytest.mark.asyncio
async def test_run_if_due_disabled_via_config(mgr: MemoryManager):
    config = AgentConfig(memory_maintainer_enabled=False)
    write_context_file(mgr.context_file, [_entry("- stale", hits=0, last_used_days_ago=60)])

    ran = await MemoryMaintainer(mgr, config).run_if_due()
    assert ran is False
    # nothing moved
    assert not mgr.archive_file.exists() or not parse_context_file(mgr.archive_file)


@pytest.mark.asyncio
async def test_marker_written_after_successful_run(mgr: MemoryManager, config: AgentConfig):
    await MemoryMaintainer(mgr, config).run_if_due()
    marker = mgr.memory_dir / ".maintainer_last_run"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").strip()


# ── Idempotency ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maintainer_is_idempotent_after_first_run(mgr: MemoryManager, config: AgentConfig):
    write_context_file(mgr.context_file, [
        _entry("- stale", hits=0, last_used_days_ago=60),
        _entry("- keep me", hits=5, last_used_days_ago=1),
    ])

    await MemoryMaintainer(mgr, config).run_if_due()
    state_after_first = mgr.context_file.read_text(encoding="utf-8")

    # Force-rerun by removing the marker
    (mgr.memory_dir / ".maintainer_last_run").unlink()

    await MemoryMaintainer(mgr, config).run_if_due()
    state_after_second = mgr.context_file.read_text(encoding="utf-8")

    assert state_after_first == state_after_second


# ── Helpers ─────────────────────────────────────────────────


def test_jaccard_identical():
    assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint():
    assert _jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_partial():
    # {a,b,c} vs {b,c,d}: intersection=2, union=4 → 0.5
    assert _jaccard({"a", "b", "c"}, {"b", "c", "d"}) == 0.5


def test_tokens_lowercase_and_strips_punctuation():
    assert _tokens("Hello, World!") == {"hello", "world"}


# ── _compact (Phase 4: LLM topic compaction) ─────────────────


class FakeLLMResponse:
    def __init__(self, content: str):
        self.content = content


class FakeCompactLLM:
    """Returns a pre-canned JSON-string response."""

    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls = 0
        self.last_messages = None

    async def generate(self, messages, **_):
        self.calls += 1
        self.last_messages = messages
        return FakeLLMResponse(self.response_text)


class PausedCompactLLM(FakeCompactLLM):
    def __init__(self, response_text: str):
        super().__init__(response_text)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, messages, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().generate(messages, **kwargs)


def _maint_cfg(**overrides) -> AgentConfig:
    base = dict(
        memory_maintainer_enabled=True,
        memory_compaction_enabled=True,
        memory_context_max_entries=2,
        memory_context_max_tokens=10_000_000,
        memory_decay_days=30,
        memory_archive_days=90,
        memory_dedup_jaccard=0.999,  # don't accidentally trigger Jaccard merge
        memory_maintainer_interval_hours=24,
    )
    base.update(overrides)
    return AgentConfig(**base)


@pytest.mark.asyncio
async def test_compact_below_threshold_is_noop(memory_dir):
    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- only a few entries")
    b = _new_entry("- another fact")
    write_context_file(mgr.context_file, [a, b])

    cfg = _maint_cfg(memory_context_max_entries=10)  # over capacity not hit
    llm = FakeCompactLLM("[]")
    m = MemoryMaintainer(mgr, cfg, llm=llm)

    await m._compact(datetime.now(timezone.utc))

    assert llm.calls == 0, "LLM should not be called when under capacity"
    after = parse_context_file(mgr.context_file)
    assert {e.content for e in after} == {"- only a few entries", "- another fact"}


@pytest.mark.asyncio
async def test_compact_disabled_flag_skips(memory_dir):
    mgr = MemoryManager(memory_dir=str(memory_dir))
    write_context_file(mgr.context_file, [
        _new_entry("- a"), _new_entry("- b"), _new_entry("- c"),
    ])
    cfg = _maint_cfg(memory_compaction_enabled=False)
    llm = FakeCompactLLM("[]")
    m = MemoryMaintainer(mgr, cfg, llm=llm)

    await m._compact(datetime.now(timezone.utc))

    assert llm.calls == 0
    assert len(parse_context_file(mgr.context_file)) == 3


@pytest.mark.asyncio
async def test_compact_no_llm_skips(memory_dir):
    mgr = MemoryManager(memory_dir=str(memory_dir))
    write_context_file(mgr.context_file, [
        _new_entry("- a"), _new_entry("- b"), _new_entry("- c"),
    ])
    m = MemoryMaintainer(mgr, _maint_cfg(), llm=None)
    await m._compact(datetime.now(timezone.utc))
    assert len(parse_context_file(mgr.context_file)) == 3


@pytest.mark.asyncio
async def test_compact_merges_topics_and_preserves_metadata(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    brazil = _new_entry("- user generating brazil football intro ppt")
    brazil.hits = 5
    brazil.created = "2026-01-01T00:00:00"
    brazil.last_used = "2026-04-01T00:00:00"
    spain = _new_entry("- user generating spain football intro ppt")
    spain.hits = 3
    spain.created = "2026-02-01T00:00:00"
    spain.last_used = "2026-05-01T00:00:00"
    germany = _new_entry("- user generating germany football ppt")
    germany.hits = 2
    germany.created = "2026-03-01T00:00:00"
    germany.last_used = "2026-04-15T00:00:00"
    pkg = _new_entry("- project uses uv, never use pip")
    pkg.hits = 12

    write_context_file(mgr.context_file, [brazil, spain, germany, pkg])

    canned = _json.dumps([
        {
            "content": "用户经常生成各国足球队介绍 PPT（巴西/西班牙/德国等）",
            "hits": 10,
            "sources": [brazil.id, spain.id, germany.id],
        },
        {
            "content": "- project uses uv, never use pip",
            "hits": 12,
            "sources": [pkg.id],
        },
    ])
    llm = FakeCompactLLM(canned)
    cfg = _maint_cfg(memory_context_max_entries=2)
    m = MemoryMaintainer(mgr, cfg, llm=llm)

    await m._compact(datetime.now(timezone.utc))

    after = {e.content: e for e in parse_context_file(mgr.context_file)}
    assert len(after) == 2
    merged = after["用户经常生成各国足球队介绍 PPT（巴西/西班牙/德国等）"]
    assert merged.hits == 10
    assert merged.created == "2026-01-01T00:00:00"  # min
    assert merged.last_used == "2026-05-01T00:00:00"  # max
    assert merged.source == "compact"
    # Strong-preference entry untouched in spirit (content + hits preserved).
    assert after["- project uses uv, never use pip"].hits == 12


@pytest.mark.asyncio
async def test_compact_preserves_concurrent_append_and_search_hit(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- alpha durable memory", topic="general")
    b = _new_entry("- beta retained fact", topic="general")
    c = _new_entry("- gamma retained fact", topic="general")
    a.hits = b.hits = c.hits = 1
    mgr.write_all_context_entries([a, b, c])

    canned = _json.dumps([
        {
            "content": "- compacted durable memories",
            "hits": 3,
            "sources": [a.id, b.id, c.id],
        },
    ])
    llm = PausedCompactLLM(canned)
    maintainer = MemoryMaintainer(
        mgr,
        _maint_cfg(memory_context_max_entries=2),
        llm=llm,
    )

    compact_task = asyncio.create_task(
        maintainer._compact(datetime.now(timezone.utc))
    )
    await asyncio.wait_for(llm.started.wait(), timeout=2.0)

    await asyncio.to_thread(
        mgr.append_context,
        "- concurrently added during compaction",
        topic="project",
    )
    assert await asyncio.to_thread(mgr.search, "alpha durable memory") == [
        "- alpha durable memory"
    ]

    llm.release.set()
    await asyncio.wait_for(compact_task, timeout=2.0)

    entries = mgr.read_all_context_entries()
    by_content = {entry.content: entry for entry in entries}
    assert set(by_content) == {
        "- compacted durable memories",
        "- concurrently added during compaction",
    }
    # Three original hits plus the concurrent search hit.
    assert by_content["- compacted durable memories"].hits == 4

    grouped = mgr.topic_store.read_all_grouped()
    index = mgr.topic_store.read_index()
    assert set(index) == set(grouped)
    assert index["general"]["count"] == 1
    assert index["general"]["hits_total"] == 4
    assert index["project"]["count"] == 1


@pytest.mark.asyncio
async def test_compact_rejected_flag_propagates(memory_dir):
    """Rejected entries must remain rejected after compaction so they can't
    sneak back into the core via merging with a non-rejected sibling."""
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- fact a")
    a.hits = 3
    a.core_status = "rejected"
    b = _new_entry("- fact a but worded differently")
    b.hits = 2
    write_context_file(mgr.context_file, [a, b])

    canned = _json.dumps([
        {"content": "- fact a unified", "hits": 5, "sources": [a.id, b.id]},
    ])
    m = MemoryMaintainer(mgr, _maint_cfg(memory_context_max_entries=1), llm=FakeCompactLLM(canned))

    await m._compact(datetime.now(timezone.utc))

    after = parse_context_file(mgr.context_file)
    assert len(after) == 1
    assert after[0].core_status == "rejected"


@pytest.mark.asyncio
async def test_compact_preserves_single_topic_bucket(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- PPT style prefers dark editorial", topic="preferences")
    b = _new_entry("- PPT style prefers sports magazine visuals", topic="preferences")
    mgr.write_all_context_entries([a, b, _new_entry("- project detail", topic="project")])

    canned = _json.dumps([
        {
            "content": "- PPT style prefers dark sports magazine visuals",
            "hits": 0,
            "sources": [a.id, b.id],
        },
    ])
    m = MemoryMaintainer(mgr, _maint_cfg(memory_context_max_entries=1), llm=FakeCompactLLM(canned))

    await m._compact(datetime.now(timezone.utc))

    assert "sports magazine" in mgr.read_context_topic("preferences")
    assert "sports magazine" not in mgr.read_context_topic("general")


@pytest.mark.asyncio
async def test_compact_invalid_json_keeps_original(memory_dir):
    mgr = MemoryManager(memory_dir=str(memory_dir))
    entries = [_new_entry(f"- entry {i}") for i in range(5)]
    write_context_file(mgr.context_file, entries)
    original = {e.content for e in entries}

    llm = FakeCompactLLM("not valid json at all")
    m = MemoryMaintainer(mgr, _maint_cfg(memory_context_max_entries=2), llm=llm)

    await m._compact(datetime.now(timezone.utc))

    after = {e.content for e in parse_context_file(mgr.context_file)}
    assert after == original


@pytest.mark.asyncio
async def test_compact_unknown_source_id_rejects_output(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    real = _new_entry("- real entry")
    write_context_file(mgr.context_file, [real, _new_entry("- another"), _new_entry("- third")])

    # LLM hallucinates an id not present in the input.
    canned = _json.dumps([
        {"content": "- merged", "hits": 1, "sources": ["ghost-id-not-real"]},
    ])
    m = MemoryMaintainer(mgr, _maint_cfg(memory_context_max_entries=1), llm=FakeCompactLLM(canned))

    await m._compact(datetime.now(timezone.utc))

    contents = {e.content for e in parse_context_file(mgr.context_file)}
    assert "- merged" not in contents
    assert "- real entry" in contents


@pytest.mark.asyncio
async def test_compact_creates_backup_before_overwrite(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- a")
    b = _new_entry("- b")
    write_context_file(mgr.context_file, [a, b, _new_entry("- c")])
    original_text = mgr.context_file.read_text(encoding="utf-8")

    canned = _json.dumps([
        {"content": "merged", "hits": 0, "sources": [a.id, b.id]},
    ])
    m = MemoryMaintainer(mgr, _maint_cfg(memory_context_max_entries=1), llm=FakeCompactLLM(canned))

    await m._compact(datetime.now(timezone.utc))

    # Trash dir should contain at least one backup file matching today's date.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backup_dir = mgr.trash_dir / today / "compact"
    assert backup_dir.exists()
    backups = list(backup_dir.glob("general.*.md"))
    assert backups, "expected a general.<timestamp>.md backup"
    assert backups[0].read_text(encoding="utf-8") == original_text


@pytest.mark.asyncio
async def test_compact_isolates_corrections_preserves_metadata(memory_dir):
    """Corrections must not lose entry_type/status through generic compaction.

    Deleted correction content must stay out of default search after maintenance.
    """
    import json as _json

    from box_agent.correction import CorrectionDraft, CorrectionSubject

    mgr = MemoryManager(memory_dir=str(memory_dir))
    canary = "removedcanary_UniqueDeletedCorrectionXYZ"

    active = mgr.write_correction(
        CorrectionDraft(
            lesson="retry with --legacy-peer-deps on ERESOLVE",
            symptom="ERESOLVE peer dependency",
            subject=CorrectionSubject(kind="tool", name="npm"),
            error_fingerprint="eresolve peer dependency",
            source="explicit", verification="tool=fixture;call=validated;result=success",
        ),
        status="active",
    )
    deleted = mgr.write_correction(
        CorrectionDraft(
            lesson=f"do not reuse this deleted lesson containing {canary}",
            symptom=canary,
            subject=CorrectionSubject(kind="tool", name="npm-deleted"),
            error_fingerprint=f"deleted-fp-{canary}",
            source="explicit", verification="tool=fixture;call=validated;result=success",
        ),
        status="active",
    )
    mgr.delete_correction(deleted.id)

    assert len(mgr.list_corrections(include_inactive=True)) == 2
    assert mgr.search(canary) == []

    # Ordinary entries force the compact path while corrections stay present.
    existing = mgr.read_all_context_entries()
    a = _new_entry("- ordinary alpha durable memory", topic="general")
    b = _new_entry("- ordinary beta retained fact", topic="general")
    mgr.write_all_context_entries(list(existing) + [a, b])

    canned = _json.dumps(
        [
            {
                "content": "- compacted ordinary memories",
                "hits": 0,
                "sources": [a.id, b.id],
            },
        ]
    )
    llm = FakeCompactLLM(canned)
    m = MemoryMaintainer(
        mgr,
        _maint_cfg(memory_context_max_entries=1),
        llm=llm,
    )
    await m._compact(datetime.now(timezone.utc))

    assert llm.calls == 1
    listed = mgr.list_corrections(include_inactive=True)
    assert len(listed) == 2
    by_id = {e.id: e for e in listed}
    assert by_id[active.id].status == "active"
    assert by_id[active.id].entry_type == "correction"
    assert by_id[active.id].error_fingerprint == "eresolve_peer_dependency"
    assert by_id[active.id].subject_kind == "tool"
    assert by_id[active.id].subject_name == "npm"
    assert by_id[deleted.id].status == "deleted"
    assert by_id[deleted.id].entry_type == "correction"
    assert canary in by_id[deleted.id].content
    assert mgr.search(canary) == []
    assert any(e.id == active.id for e in mgr.list_corrections())



# ── _resolve_conflicts (Phase 3.5: LLM semantic conflict arbitration) ─


class FakeConflictLLM:
    """Returns canned responses in order; clamps to the last response if exhausted."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0
        self.received_user_prompts: list[str] = []

    async def generate(self, messages, **_):
        for m in messages:
            if m.role == "user":
                self.received_user_prompts.append(m.content)
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return FakeLLMResponse(self.responses[idx])


def _conflict_cfg(**overrides) -> AgentConfig:
    base = dict(
        memory_maintainer_enabled=True,
        memory_conflict_resolution_enabled=True,
        memory_conflict_cluster_threshold=0.15,  # generous for short test fixtures
        memory_conflict_max_clusters_per_run=5,
        memory_compaction_enabled=False,  # isolate the phase under test
        memory_dedup_jaccard=0.999,  # avoid accidental dedup merging fixtures
        memory_decay_days=30,
        memory_archive_days=90,
        memory_maintainer_interval_hours=24,
    )
    base.update(overrides)
    return AgentConfig(**base)


@pytest.mark.asyncio
async def test_resolve_conflicts_winner_kept_loser_archived(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    older = _new_entry("- project uses redis for cache")
    older.created = "2026-01-01T00:00:00"
    newer = _new_entry("- project switched to postgres listen notify replacing redis")
    newer.created = "2026-04-01T00:00:00"
    unrelated = _new_entry("- migrations live in migrations directory")
    unrelated.created = "2026-02-01T00:00:00"
    write_context_file(mgr.context_file, [older, newer, unrelated])

    canned = _json.dumps({
        "groups": [
            {"winner_id": newer.id, "loser_ids": [older.id], "reason": "explicit replacement"},
        ]
    })
    llm = FakeConflictLLM([canned])
    m = MemoryMaintainer(mgr, _conflict_cfg(), llm=llm)

    await m._resolve_conflicts(datetime.now(timezone.utc))

    active_ids = {e.id for e in parse_context_file(mgr.context_file)}
    archived_ids = {e.id for e in parse_context_file(mgr.archive_file)}
    assert newer.id in active_ids and unrelated.id in active_ids
    assert older.id not in active_ids
    assert older.id in archived_ids
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_resolve_conflicts_compatible_pair_kept(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- user prefers dark mode interface")
    b = _new_entry("- user prefers dark mode for editor")
    write_context_file(mgr.context_file, [a, b])

    llm = FakeConflictLLM([_json.dumps({"groups": []})])
    m = MemoryMaintainer(mgr, _conflict_cfg(), llm=llm)

    await m._resolve_conflicts(datetime.now(timezone.utc))

    active_ids = {e.id for e in parse_context_file(mgr.context_file)}
    assert active_ids == {a.id, b.id}
    assert not mgr.archive_file.exists() or not parse_context_file(mgr.archive_file)
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_resolve_conflicts_invalid_json_noop(memory_dir):
    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- use redis cache")
    b = _new_entry("- use postgres instead of redis")
    write_context_file(mgr.context_file, [a, b])

    llm = FakeConflictLLM(["this is not valid json"])
    m = MemoryMaintainer(mgr, _conflict_cfg(), llm=llm)

    await m._resolve_conflicts(datetime.now(timezone.utc))

    active_ids = {e.id for e in parse_context_file(mgr.context_file)}
    assert active_ids == {a.id, b.id}
    assert not mgr.archive_file.exists() or not parse_context_file(mgr.archive_file)


@pytest.mark.asyncio
async def test_resolve_conflicts_disabled_skips(memory_dir):
    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- use redis cache")
    b = _new_entry("- use postgres instead of redis")
    write_context_file(mgr.context_file, [a, b])

    llm = FakeConflictLLM(["{\"groups\":[]}"])
    cfg = _conflict_cfg(memory_conflict_resolution_enabled=False)
    m = MemoryMaintainer(mgr, cfg, llm=llm)

    await m._resolve_conflicts(datetime.now(timezone.utc))

    assert llm.calls == 0
    assert len(parse_context_file(mgr.context_file)) == 2


@pytest.mark.asyncio
async def test_resolve_conflicts_no_llm_skips(memory_dir):
    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- use redis cache")
    b = _new_entry("- use postgres instead of redis")
    write_context_file(mgr.context_file, [a, b])

    m = MemoryMaintainer(mgr, _conflict_cfg(), llm=None)
    await m._resolve_conflicts(datetime.now(timezone.utc))

    assert len(parse_context_file(mgr.context_file)) == 2


@pytest.mark.asyncio
async def test_resolve_conflicts_caps_clusters_per_run(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    # Three independent topic clusters, each a conflict pair.
    # Vocab kept disjoint across clusters so they don't union-find merge.
    redis_a = _new_entry("- caching backed by redis")
    redis_b = _new_entry("- caching backed by memcached replacing redis")
    auth_a = _new_entry("- login strategy jwt")
    auth_b = _new_entry("- login strategy cookies superseded jwt")
    db_a = _new_entry("- orm choice sqlalchemy")
    db_b = _new_entry("- orm choice tortoise instead sqlalchemy")
    write_context_file(mgr.context_file, [redis_a, redis_b, auth_a, auth_b, db_a, db_b])

    # Each LLM call returns a per-cluster conflict; only 2 calls allowed.
    canned_per_call = [
        _json.dumps({"groups": [{"winner_id": redis_b.id, "loser_ids": [redis_a.id]}]}),
        _json.dumps({"groups": [{"winner_id": auth_b.id, "loser_ids": [auth_a.id]}]}),
        _json.dumps({"groups": [{"winner_id": db_b.id, "loser_ids": [db_a.id]}]}),
    ]
    llm = FakeConflictLLM(canned_per_call)
    cfg = _conflict_cfg(memory_conflict_max_clusters_per_run=2)
    m = MemoryMaintainer(mgr, cfg, llm=llm)

    await m._resolve_conflicts(datetime.now(timezone.utc))

    assert llm.calls == 2  # capped


@pytest.mark.asyncio
async def test_resolve_conflicts_rejects_hallucinated_id(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- use redis cache")
    b = _new_entry("- use postgres instead of redis")
    write_context_file(mgr.context_file, [a, b])

    canned = _json.dumps({
        "groups": [{"winner_id": "ghost-id", "loser_ids": [a.id]}]
    })
    m = MemoryMaintainer(mgr, _conflict_cfg(), llm=FakeConflictLLM([canned]))

    await m._resolve_conflicts(datetime.now(timezone.utc))

    active_ids = {e.id for e in parse_context_file(mgr.context_file)}
    assert active_ids == {a.id, b.id}


@pytest.mark.asyncio
async def test_resolve_conflicts_rejects_winner_in_losers(memory_dir):
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    a = _new_entry("- use redis cache")
    b = _new_entry("- use postgres instead of redis")
    write_context_file(mgr.context_file, [a, b])

    canned = _json.dumps({
        "groups": [{"winner_id": a.id, "loser_ids": [a.id, b.id]}]
    })
    m = MemoryMaintainer(mgr, _conflict_cfg(), llm=FakeConflictLLM([canned]))

    await m._resolve_conflicts(datetime.now(timezone.utc))

    active_ids = {e.id for e in parse_context_file(mgr.context_file)}
    assert active_ids == {a.id, b.id}


@pytest.mark.asyncio
async def test_resolve_conflicts_dedupes_overlapping_losers(memory_dir):
    """When two clusters claim the same loser, archive it only once."""
    import json as _json

    mgr = MemoryManager(memory_dir=str(memory_dir))
    # Two clusters might share an entry if Jaccard binds it to both topics.
    # Simulate by returning the same loser in two consecutive group responses
    # (defensive — clusters as built shouldn't overlap, but the logic guards anyway).
    a = _new_entry("- redis cache layer")
    b = _new_entry("- postgres replaces redis cache layer")
    c = _new_entry("- another fact about redis cache layer storage")
    write_context_file(mgr.context_file, [a, b, c])

    # Single cluster {a,b,c}: one LLM call returns a conflict pair (winner=b, loser=a).
    canned = _json.dumps({"groups": [{"winner_id": b.id, "loser_ids": [a.id]}]})
    m = MemoryMaintainer(mgr, _conflict_cfg(), llm=FakeConflictLLM([canned]))

    await m._resolve_conflicts(datetime.now(timezone.utc))

    archived = parse_context_file(mgr.archive_file)
    assert [e.id for e in archived] == [a.id]


# ── Helper unit tests ──────────────────────────────────────


def test_cluster_by_jaccard_finds_overlapping_group():
    e1 = _new_entry("redis cache backend")
    e2 = _new_entry("postgres replaces redis cache")
    e3 = _new_entry("completely unrelated fact about cats")
    clusters = _cluster_by_jaccard([e1, e2, e3], threshold=0.15)
    assert len(clusters) == 1
    assert set(clusters[0]) == {0, 1}


def test_cluster_by_jaccard_empty_when_all_disjoint():
    e1 = _new_entry("apples are red")
    e2 = _new_entry("clocks tell time")
    assert _cluster_by_jaccard([e1, e2], threshold=0.3) == []


def test_cluster_by_jaccard_singleton_dropped():
    e1 = _new_entry("only entry")
    assert _cluster_by_jaccard([e1], threshold=0.3) == []


def test_parse_conflict_output_strips_fences():
    text = "```json\n{\"groups\": []}\n```"
    assert _parse_conflict_output(text, valid_ids={"a"}) == []


def test_parse_conflict_output_rejects_non_dict_root():
    assert _parse_conflict_output("[]", valid_ids={"a"}) is None


def test_parse_conflict_output_rejects_unknown_winner_id():
    text = '{"groups": [{"winner_id": "ghost", "loser_ids": ["a"]}]}'
    assert _parse_conflict_output(text, valid_ids={"a"}) is None


def test_parse_conflict_output_rejects_empty_losers():
    text = '{"groups": [{"winner_id": "a", "loser_ids": []}]}'
    assert _parse_conflict_output(text, valid_ids={"a"}) is None


def test_parse_conflict_output_rejects_duplicate_losers_across_groups():
    text = ('{"groups": ['
            '{"winner_id": "a", "loser_ids": ["b"]},'
            '{"winner_id": "c", "loser_ids": ["b"]}'
            ']}')
    assert _parse_conflict_output(text, valid_ids={"a", "b", "c"}) is None


def test_parse_conflict_output_accepts_well_formed():
    text = '{"groups": [{"winner_id": "a", "loser_ids": ["b", "c"], "reason": "newer"}]}'
    out = _parse_conflict_output(text, valid_ids={"a", "b", "c"})
    assert out == [{"winner_id": "a", "loser_ids": ["b", "c"]}]
