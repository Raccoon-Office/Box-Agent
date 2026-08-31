"""Tests for box_agent.memory — MemoryManager (core + context)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from box_agent.memory import MemoryManager


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def mgr(memory_dir: Path) -> MemoryManager:
    return MemoryManager(memory_dir=str(memory_dir))


# ── Core memory (MEMORY.md) ───────────────────────────────────


def test_read_write_core(mgr: MemoryManager):
    assert mgr.read_core() == ""
    mgr.write_core("- user prefers Chinese")
    assert "user prefers Chinese" in mgr.read_core()

    mgr.write_core("- new preference")
    content = mgr.read_core()
    assert "new preference" in content
    assert "user prefers Chinese" not in content


def test_append_core(mgr: MemoryManager):
    mgr.append_core("- item 1")
    mgr.append_core("- item 2")
    content = mgr.read_core()
    assert "item 1" in content
    assert "item 2" in content


def test_read_core_missing_file(mgr: MemoryManager):
    assert mgr.read_core() == ""


# ── Legacy aliases ────────────────────────────────────────────


def test_legacy_aliases(mgr: MemoryManager):
    mgr.write_manual_memory("- legacy content")
    assert "legacy content" in mgr.read_manual_memory()
    assert "legacy content" in mgr.read_all()


# ── OpenClaw import ────────────────────────────────────────────


async def test_openclaw_import_caps_utility_model_output(
    mgr: MemoryManager,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(mgr, "_read_openclaw_raw", lambda: "- prefers concise replies")

    base_llm = MagicMock()
    base_llm.model = "sn-sensenova-6-8-flash-lite"
    base_llm.max_output_tokens = 80_000
    capped_llm = MagicMock()
    response = MagicMock()
    response.content = "- prefers concise replies"
    capped_llm.generate = AsyncMock(return_value=response)
    base_llm.for_model.return_value = capped_llm

    imported = await mgr.import_openclaw(base_llm)

    base_llm.for_model.assert_called_once_with(
        "sn-sensenova-6-8-flash-lite",
        max_output_tokens=4_096,
    )
    assert imported == "- prefers concise replies"
    assert "prefers concise replies" in mgr.read_core()
    assert mgr._openclaw_imported_marker.read_text(encoding="utf-8") == "done\n"


# ── Context memory (CONTEXT.md) ──────────────────────────────


def test_read_write_context(mgr: MemoryManager):
    assert mgr.read_context() == ""
    mgr.write_context("- Q2 goal: dashboard")
    assert "Q2 goal" in mgr.read_context()


def test_append_context(mgr: MemoryManager):
    mgr.append_context("- project A")
    mgr.append_context("- project B")
    content = mgr.read_context()
    assert "project A" in content
    assert "project B" in content


def test_core_and_context_independent(mgr: MemoryManager):
    """Writing to one file doesn't affect the other."""
    mgr.write_core("- user: Alice")
    mgr.write_context("- project: Dashboard")

    assert "Alice" in mgr.read_core()
    assert "Dashboard" not in mgr.read_core()
    assert "Dashboard" in mgr.read_context()
    assert "Alice" not in mgr.read_context()


def test_append_context_dedup_against_core(mgr: MemoryManager):
    """Lines already in Core are filtered out when appending to Context."""
    mgr.write_core("- user: Alice\n- prefers Chinese")
    mgr.append_context("- user: Alice\n- Q2 goal: dashboard\n- prefers Chinese")
    context = mgr.read_context()
    assert "Q2 goal" in context
    assert "Alice" not in context
    assert "Chinese" not in context


def test_append_context_dedup_against_existing_context(mgr: MemoryManager):
    """Lines already in Context are not appended again."""
    mgr.write_context("- Q2 goal: dashboard\n- weekly report format: progress/issues/plan")
    mgr.append_context("- q2 goal: dashboard\n- team lead: Bob\n- weekly report format: progress/issues/plan")

    context = mgr.read_context()
    assert context.count("Q2 goal") == 1
    assert context.count("weekly report format") == 1
    assert "team lead: Bob" in context


def test_append_context_dedup_within_single_append(mgr: MemoryManager):
    """Duplicate lines within one append call are only saved once."""
    mgr.append_context("- project A\n- Project A\n- project B")
    context = mgr.read_context()
    assert context.lower().count("project a") == 1
    assert "project B" in context


def test_append_context_all_filtered(mgr: MemoryManager):
    """If all lines are Core duplicates, nothing is written to Context."""
    mgr.write_core("- user: Alice")
    mgr.append_context("- user: Alice")
    assert mgr.read_context() == ""


def test_apply_context_operations_add_replace_drop_noop(mgr: MemoryManager):
    mgr.write_core("- user: Alice")
    mgr.write_context("- Q2 goal: dashboard\n- old transient detail")

    changed = mgr.apply_context_operations([
        {
            "action": "replace",
            "old": "- Q2 goal: dashboard",
            "new": "- Q2 goal: launch dashboard by 6/30",
        },
        {"action": "add", "content": "- user: Alice"},
        {"action": "add", "content": "- weekly report format: progress/issues/plan"},
        {"action": "drop", "content": "- old transient detail"},
        {"action": "noop", "content": "- ignored"},
    ])

    assert changed is True
    context = mgr.read_context()
    assert "launch dashboard by 6/30" in context
    assert "weekly report format" in context
    assert "old transient detail" not in context
    assert "user: Alice" not in context


async def test_update_context_with_llm_applies_model_plan(mgr: MemoryManager):
    mgr.write_context("- Q2 goal: dashboard")
    llm = MagicMock()
    response = MagicMock()
    response.content = (
        '{"operations": ['
        '{"action": "replace", "old": "- Q2 goal: dashboard", "new": "- Q2 goal: launch dashboard by 6/30", "reason": "more specific"},'
        '{"action": "add", "content": "- weekly report format: progress/issues/plan", "reason": "new template"}'
        ']}'
    )
    llm.generate = AsyncMock(return_value=response)

    status = await mgr.update_context_with_llm("- Q2 goal is to launch dashboard by 6/30", llm)

    assert status == "applied"
    context = mgr.read_context()
    assert "launch dashboard by 6/30" in context
    assert "weekly report format" in context
    assert llm.generate.await_count == 1


async def test_update_context_with_llm_falls_back_to_append_on_bad_json(mgr: MemoryManager):
    llm = MagicMock()
    response = MagicMock()
    response.content = "not json"
    llm.generate = AsyncMock(return_value=response)

    status = await mgr.update_context_with_llm("- project deadline: June", llm)

    assert status == "fallback_appended"
    assert "project deadline: June" in mgr.read_context()


# ── Search ────────────────────────────────────────────────────


def test_search_match(mgr: MemoryManager):
    mgr.write_context("- weekly report format: progress/issues/plan\n- Q2 goal: data dashboard\n- team lead: Bob")
    results = mgr.search("weekly")
    assert len(results) == 1
    assert "weekly report" in results[0]


def test_search_case_insensitive(mgr: MemoryManager):
    mgr.write_context("- Project Alpha is important")
    results = mgr.search("project alpha")
    assert len(results) == 1


def test_search_multiple_matches(mgr: MemoryManager):
    mgr.write_context("- report format A\n- report template B\n- unrelated item")
    results = mgr.search("report")
    assert len(results) == 2


def test_search_no_match(mgr: MemoryManager):
    mgr.write_context("- some content")
    results = mgr.search("nonexistent")
    assert results == []


def test_search_empty_context(mgr: MemoryManager):
    results = mgr.search("anything")
    assert results == []


def test_search_empty_query(mgr: MemoryManager):
    mgr.write_context("- some content")
    results = mgr.search("")
    assert results == []


def test_auto_match_context_matches_related_phrase_conservatively(mgr: MemoryManager):
    mgr.write_context(
        "- PPTX QA环境最新记录：在本轮AI科技公司入职培训PPT中，render_pptx.py 可成功导出PDF。\n"
        "- 会话连续性反馈：用户会以“科技公司入职培训 ppt 做好了吗”等方式追问既有交付状态。\n"
        "- Quick Bar history should default to visible records."
    )

    matches = mgr.auto_match_context("科技公司入职培训 PPT 做好了吗")

    assert [item["text"] for item in matches] == [
        "- PPTX QA环境最新记录：在本轮AI科技公司入职培训PPT中，render_pptx.py 可成功导出PDF。",
        "- 会话连续性反馈：用户会以“科技公司入职培训 ppt 做好了吗”等方式追问既有交付状态。",
    ]


def test_auto_match_context_ignores_weak_single_word_overlap(mgr: MemoryManager):
    mgr.write_context("- PPTX QA环境最新记录：在本轮AI科技公司入职培训PPT中，render_pptx.py 可成功导出PDF。")

    assert mgr.auto_match_context("帮我写一个培训方案") == []


def test_auto_match_context_ignores_host_appended_file_output_rules(mgr: MemoryManager):
    mgr.write_context(
        "- [文件输出规范] 当你生成了多个文件，将它们打包成 ZIP 并提供下载链接。\n"
        "- PPTX QA环境最新记录：在本轮AI科技公司入职培训PPT中，render_pptx.py 可成功导出PDF。"
    )

    matches = mgr.auto_match_context("科技公司入职培训 都需要注意什么\n\n[文件输出规范] 当你生成了多个文件")

    assert [item["text"] for item in matches] == [
        "- PPTX QA环境最新记录：在本轮AI科技公司入职培训PPT中，render_pptx.py 可成功导出PDF。"
    ]


def test_auto_match_context_filters_file_delivery_memory_unless_user_asks_delivery(mgr: MemoryManager):
    mgr.write_context(
        "- 科技公司入职培训文件交付偏好：若生成多个文件必须使用 zip 命令打包。\n"
        "- 会话连续性反馈：用户会以“科技公司入职培训 ppt 做好了吗”等方式追问既有交付状态。"
    )

    assert [
        item["text"] for item in mgr.auto_match_context("科技公司入职培训 都需要注意什么")
    ] == [
        "- 会话连续性反馈：用户会以“科技公司入职培训 ppt 做好了吗”等方式追问既有交付状态。"
    ]
    assert [
        item["text"] for item in mgr.auto_match_context("科技公司入职培训 文件怎么打包交付")
    ][0] == "- 科技公司入职培训文件交付偏好：若生成多个文件必须使用 zip 命令打包。"


def test_auto_match_context_filters_title_meta_memory_unless_user_asks_title(mgr: MemoryManager):
    mgr.write_context(
        "- 会话标题提炼偏好：遇到“查询 科技公司入职培训”时标题可提炼为“科技公司入职培训”。\n"
        "- 会话连续性反馈：用户会以“科技公司入职培训 ppt 做好了吗”等方式追问既有交付状态。"
    )

    assert [
        item["text"] for item in mgr.auto_match_context("科技公司入职培训 都需要注意什么")
    ] == [
        "- 会话连续性反馈：用户会以“科技公司入职培训 ppt 做好了吗”等方式追问既有交付状态。"
    ]
    assert [
        item["text"] for item in mgr.auto_match_context("科技公司入职培训 这个会话标题怎么提炼")
    ][0] == "- 会话标题提炼偏好：遇到“查询 科技公司入职培训”时标题可提炼为“科技公司入职培训”。"


# ── Recall ─────────────────────────────────────────────────────


def test_recall_empty(mgr: MemoryManager):
    assert mgr.recall() == ""


def test_recall_only_core(mgr: MemoryManager):
    mgr.write_core("- always use English")
    mgr.write_context("- project context that should NOT be recalled")
    block = mgr.recall()
    assert "--- MEMORY START ---" in block
    assert "always use English" in block
    assert "[Memory Search Routing]" in block
    assert "project context that should NOT be recalled" not in block


def test_recall_includes_routing_summary_without_context_content(mgr: MemoryManager):
    """Recall may include the routing index, but not full context entries."""
    mgr.write_context("- secret context")
    block = mgr.recall()
    assert "[Memory Search Routing]" in block
    assert "Use `memory_search`" in block
    assert "Do not pass the whole user sentence" in block
    assert "secret context" not in block


def test_memory_summary_file_routes_without_migrating_legacy(memory_dir):
    legacy = memory_dir / "CONTEXT.md"
    legacy.write_text("- old workflow uses make test\n", encoding="utf-8")

    mgr = MemoryManager(memory_dir=str(memory_dir))

    summary = mgr.read_memory_summary()
    assert mgr.memory_summary_file.exists()
    assert "Legacy context: present" in summary
    assert "old workflow uses make test" not in summary
    assert mgr.search("make test") == ["- old workflow uses make test"]


# ── build_memory_block ────────────────────────────────────────


def test_build_memory_block_format():
    block = MemoryManager.build_memory_block("- core item")
    assert block.startswith("--- MEMORY START ---")
    assert block.endswith("--- MEMORY END ---")
    assert "[Core Memory]" in block


def test_build_memory_block_empty():
    assert MemoryManager.build_memory_block("") == ""


def test_build_memory_block_core_only():
    block = MemoryManager.build_memory_block("- core item")
    assert "[Core Memory]" in block
    assert "core item" in block


def test_auto_match_context_does_not_bump_hits_or_last_used(mgr: MemoryManager):
    """auto_match_context is a weak hint, not promotion evidence."""
    from box_agent.memory import _new_entry, write_context_file

    matched = _new_entry("- 科技公司入职培训PPT 已完成 render 导出")
    unmatched = _new_entry("- 完全不相关的另一个事实")
    original_last_used = matched.last_used
    write_context_file(mgr.context_file, [matched, unmatched])

    results = mgr.auto_match_context("科技公司入职培训 PPT 做好了吗")
    assert results, "expected at least one auto-match hit"

    entries = {e.id: e for e in mgr._read_context_entries()}
    assert entries[matched.id].hits == 0
    assert entries[matched.id].last_used == original_last_used
    assert entries[unmatched.id].hits == 0


def test_auto_match_context_no_match_does_not_touch_entries(mgr: MemoryManager):
    from box_agent.memory import _new_entry, write_context_file

    entry = _new_entry("- 不会被匹配到的内容")
    write_context_file(mgr.context_file, [entry])

    assert mgr.auto_match_context("完全无关的话题") == []
    survivor = mgr._read_context_entries()[0]
    assert survivor.hits == 0
    assert survivor.last_used == entry.last_used


# ── append_context Jaccard fuzzy dedup ────────────────────────


def test_append_context_jaccard_merges_paraphrased_line(memory_dir):
    """Paraphrased restatements of an existing entry should bump hits, not
    create a new entry. Without this the promotion gate's hit_threshold is
    permanently sabotaged by LLM extractor wording drift."""
    mgr = MemoryManager(memory_dir=str(memory_dir), dedup_jaccard_threshold=0.6)
    mgr.append_context("- user is generating brazil football introduction ppt")
    before = mgr._read_context_entries()
    assert len(before) == 1
    assert before[0].hits == 0

    # Same fact, different wording — most tokens overlap.
    mgr.append_context("- user generating brazil football ppt introduction")

    after = mgr._read_context_entries()
    assert len(after) == 1, "paraphrase should have merged, not been appended"
    assert after[0].hits == 1
    assert after[0].last_used >= before[0].last_used


def test_append_context_jaccard_keeps_distinct_facts(memory_dir):
    mgr = MemoryManager(memory_dir=str(memory_dir), dedup_jaccard_threshold=0.85)
    mgr.append_context("- user prefers chinese responses")
    mgr.append_context("- project uses uv for dependency management")

    entries = mgr._read_context_entries()
    assert len(entries) == 2
    assert all(e.hits == 0 for e in entries)


def test_append_context_threshold_zero_disables_distinct_lines(memory_dir):
    """At extremely low threshold (0.0) ANY non-empty existing entry would
    catch every new line. Default threshold (0.85) must not behave this way."""
    mgr = MemoryManager(memory_dir=str(memory_dir), dedup_jaccard_threshold=0.85)
    mgr.append_context("- aaa bbb ccc")
    mgr.append_context("- xxx yyy zzz")
    assert len(mgr._read_context_entries()) == 2


# ── Title-generation filter ──────────────────────────────────


def test_auto_match_context_ignores_title_generation_prompts(mgr: MemoryManager):
    """Host-injected title-generation prompts must not bump hits."""
    from box_agent.memory import _new_entry, write_context_file

    entry = _new_entry("- 科技公司入职培训PPT 已完成 render 导出")
    write_context_file(mgr.context_file, [entry])

    # Host prompt — has no interrogative marker, matches title-gen pattern.
    assert mgr.auto_match_context("请为这段对话提炼一个简短标题：科技公司入职培训") == []

    survivor = mgr._read_context_entries()[0]
    assert survivor.hits == 0


def test_auto_match_context_user_question_about_title_still_matches(mgr: MemoryManager):
    """A user question about title-related memory must not be classified as a
    host title-generation prompt — the interrogative-marker guard prevents the
    title-gen pattern matcher from kicking in even when the prompt mentions
    titles."""
    from box_agent.memory import _is_title_generation_query

    # Host prompts (no interrogative) — classified as title-gen.
    assert _is_title_generation_query("请为这段对话提炼一个简短标题")
    assert _is_title_generation_query("为会话生成标题：xxx")

    # User questions about titles — interrogative present, not classified.
    assert not _is_title_generation_query("会话标题怎么提炼")
    assert not _is_title_generation_query("为什么这个会话要提炼标题？")
    assert not _is_title_generation_query("how do I generate a title for this chat?")


# ── LLM promotion plan ──────────────────────────────────────


def _make_planner_llm(payload: dict):
    llm = MagicMock()
    response = MagicMock()
    response.content = json.dumps(payload)
    llm.generate = AsyncMock(return_value=response)
    return llm


async def test_plan_promotion_returns_plan_on_valid_llm_output(mgr: MemoryManager):
    from box_agent.memory import _new_entry, write_context_file

    mgr.write_core("- user prefers Chinese\n- workspace: /tmp/x")
    a = _new_entry("- user likes diagrams")
    b = _new_entry("- weekly cadence Tuesday")
    write_context_file(mgr.context_file, [a, b])

    llm = _make_planner_llm(
        {
            "new_core": (
                "- user prefers Chinese\n- workspace: /tmp/x\n"
                "- user likes diagrams\n- weekly cadence Tuesday"
            ),
            "consumed_entry_ids": [a.id, b.id],
            "rationale": "fold both hot entries",
        }
    )

    plan = await mgr.plan_promotion([a, b], llm)
    assert plan is not None
    assert "user likes diagrams" in plan.new_core
    assert set(plan.consumed_entry_ids) == {a.id, b.id}
    assert llm.generate.await_count == 1


async def test_plan_promotion_returns_none_on_bad_json(mgr: MemoryManager):
    from box_agent.memory import _new_entry, write_context_file

    a = _new_entry("- candidate")
    write_context_file(mgr.context_file, [a])

    llm = MagicMock()
    response = MagicMock()
    response.content = "definitely not json"
    llm.generate = AsyncMock(return_value=response)

    assert await mgr.plan_promotion([a], llm) is None


async def test_plan_promotion_rejects_oversized_core_shrink(mgr: MemoryManager):
    from box_agent.memory import _new_entry, write_context_file

    mgr.write_core("- " + "lorem ipsum dolor sit amet " * 20)
    a = _new_entry("- candidate")
    write_context_file(mgr.context_file, [a])

    llm = _make_planner_llm(
        {
            "new_core": "- tiny",
            "consumed_entry_ids": [a.id],
            "rationale": "gutting core",
        }
    )

    assert await mgr.plan_promotion([a], llm) is None


async def test_plan_promotion_filters_non_candidate_ids(mgr: MemoryManager):
    from box_agent.memory import _new_entry, write_context_file

    a = _new_entry("- candidate")
    other = _new_entry("- unrelated")
    write_context_file(mgr.context_file, [a, other])

    llm = _make_planner_llm(
        {
            "new_core": "- merged content here that grows core",
            "consumed_entry_ids": [a.id, other.id, "ctx_fake_id"],
            "rationale": "ok",
        }
    )

    plan = await mgr.plan_promotion([a], llm)
    assert plan is not None
    # other.id and fake id must be filtered out — only candidate ids allowed
    assert plan.consumed_entry_ids == (a.id,)


async def test_plan_promotion_uses_current_locked_candidate_snapshot(
    mgr: MemoryManager,
):
    from box_agent.memory import _new_entry, write_context_file

    stale = _new_entry("- stale candidate")
    current = _new_entry("- current candidate")
    current.id = stale.id
    write_context_file(mgr.context_file, [current])

    llm = _make_planner_llm(
        {
            "new_core": "- current candidate promoted",
            "consumed_entry_ids": [stale.id],
            "rationale": "use current state",
        }
    )

    plan = await mgr.plan_promotion([stale], llm)

    assert plan is not None
    prompt = llm.generate.await_args.kwargs["messages"][1]["content"]
    assert "- current candidate" in prompt
    assert "- stale candidate" not in prompt


def test_apply_promotion_plan_overwrites_core_and_consumes_entries(mgr: MemoryManager):
    from box_agent.events import MemoryPromotionPlan
    from box_agent.memory import _new_entry, write_context_file

    a = _new_entry("- A")
    b = _new_entry("- B")
    c = _new_entry("- C")
    write_context_file(mgr.context_file, [a, b, c])
    mgr.write_core("- old core")

    plan = MemoryPromotionPlan(
        current_core="- old core",
        new_core="- new core\n- A folded in",
        consumed_entry_ids=(a.id, b.id),
        rationale="test",
    )

    result = mgr.apply_promotion_plan(plan)
    assert result == {"applied": 1, "consumed": 2}
    assert mgr.read_core() == "- new core\n- A folded in"
    remaining = mgr._read_context_entries()
    assert [e.id for e in remaining] == [c.id]


def test_reject_promotion_plan_marks_candidates_rejected(mgr: MemoryManager):
    from box_agent.events import MemoryPromotionPlan
    from box_agent.memory import _new_entry, write_context_file

    a = _new_entry("- A")
    b = _new_entry("- B")
    write_context_file(mgr.context_file, [a, b])
    mgr.write_core("- core stays")

    plan = MemoryPromotionPlan(
        current_core="- core stays",
        new_core="- ignored",
        consumed_entry_ids=(a.id,),
        rationale="test",
    )

    result = mgr.reject_promotion_plan(plan)
    assert result == {"rejected": 1}
    # core untouched
    assert mgr.read_core() == "- core stays"
    entries = {e.id: e for e in mgr._read_context_entries()}
    assert entries[a.id].core_status == "rejected"
    assert entries[b.id].core_status != "rejected"


# ── Self-citation filter ─────────────────────────────────────


def test_auto_match_context_drops_self_citation(mgr: MemoryManager):
    """A memory line that is essentially the user's own prompt must not surface
    as 'referenced memory' — that's a self-echo bug."""
    from box_agent.memory import _new_entry, write_context_file

    user_prompt = "我希望系统支持 PPT 自动生成功能，按需调整页面样式和动画效果"
    # Extractor distilled the user's own prompt into context — near-duplicate.
    echoed = _new_entry("- 用户希望系统支持PPT自动生成功能，按需调整页面样式和动画效果")
    write_context_file(mgr.context_file, [echoed])

    assert mgr.auto_match_context(user_prompt) == []


def test_auto_match_context_keeps_substantive_memory_about_same_topic(mgr: MemoryManager):
    """Self-cite filter must not be so aggressive that it drops every memory
    sharing topic phrases with the prompt — only near-duplicates."""
    from box_agent.memory import _new_entry, write_context_file

    fact = _new_entry(
        "- 用户偏好：PPT 自动生成的页面样式推荐使用蓝白配色"
    )
    write_context_file(mgr.context_file, [fact])

    matches = mgr.auto_match_context("我想配置 PPT 自动生成的页面样式")
    assert len(matches) == 1
    assert "蓝白配色" in matches[0]["text"]


# ── Containment length guard ─────────────────────────────────


def test_score_memory_match_short_containment_not_overweighted(mgr: MemoryManager):
    """Short query strings must not score 10.0 just because they appear
    inside a longer memory line."""
    from box_agent.memory import _extract_match_terms, _score_memory_match

    query = "下载"  # 2 chars — too short for containment to fire
    q_lower = query.lower()
    q_terms = _extract_match_terms(q_lower)
    memory = "- 用户偏好把所有产物打包成 zip 提供下载链接给团队成员".lower()

    score = _score_memory_match(q_lower, q_terms, memory)
    assert score < 10.0  # containment guard must reject short query


def test_score_memory_match_long_containment_still_fires():
    """Substantial query that appears verbatim in memory does score 10.0."""
    from box_agent.memory import _extract_match_terms, _score_memory_match

    query = "科技公司入职培训 ppt 做好了吗"
    q_lower = query.lower()
    q_terms = _extract_match_terms(q_lower)
    memory = f"- 用户曾追问『{query}』表示关心交付状态".lower()

    assert _score_memory_match(q_lower, q_terms, memory) == 10.0


# ── Noise term blacklist ─────────────────────────────────────


def test_extract_match_terms_drops_noise_terms():
    """Common nouns/wildcards must not appear as match terms."""
    from box_agent.memory import _extract_match_terms

    terms = set(_extract_match_terms("帮我看看 这个 项目 的 功能 模块"))
    for noise in ("项目", "功能", "模块", "这个", "帮我看看"):
        assert noise not in terms


def test_extract_match_terms_drops_short_chinese_segments():
    """Chinese segments shorter than 5 characters are no longer emitted."""
    from box_agent.memory import _extract_match_terms

    # Only 4-char segment present — should produce zero Chinese terms.
    assert _extract_match_terms("做好了吗") == []


# ── Ranked search ────────────────────────────────────────────


def test_search_returns_entries_not_individual_lines(mgr: MemoryManager):
    """Search returns entire entry content blocks, not just matched lines."""
    from box_agent.memory import _new_entry, write_context_file

    multi_line = _new_entry("- PPT 模板偏好\n- 标题字号 32pt\n- 正文蓝白配色")
    other = _new_entry("- 完全无关的事实")
    write_context_file(mgr.context_file, [multi_line, other])

    results = mgr.search("ppt")
    assert len(results) == 1
    # Whole entry content, not just the line that contained "ppt".
    assert "标题字号" in results[0]
    assert "蓝白配色" in results[0]


def test_search_ranks_by_occurrence_count_then_hits(mgr: MemoryManager):
    """Entries with more matches rank first; ties break on historical hits."""
    from box_agent.memory import _new_entry, write_context_file

    many = _new_entry("- report A\n- report B\n- report C")  # 3 occurrences
    few = _new_entry("- report D only")  # 1 occurrence
    stale = _new_entry("- nothing here")
    write_context_file(mgr.context_file, [few, stale, many])

    results = mgr.search("report")
    assert len(results) == 2
    # `many` ranks first despite being written last in CONTEXT.md.
    assert "report A" in results[0]
    assert "report D only" in results[1]


def test_search_compound_chinese_action_query_matches_stable_terms(mgr: MemoryManager):
    """Full user action sentences should still recall stable project memories."""
    from box_agent.memory import _new_entry, write_context_file

    target = _new_entry(
        "用户正在为一个AI质检+智能排产平台项目制作融资路演PPT，"
        "面向VC投资人，项目已有30家试点客户"
    )
    weak_ppt = _new_entry("- 公司内部培训类 PPT 偏好：浅色背景、图标、小色块")
    unrelated = _new_entry("- 完全无关的事实")
    write_context_file(mgr.context_file, [weak_ppt, unrelated, target])

    results = mgr.search("排产平台融资 ppt 的演讲稿发我")

    assert results
    assert "智能排产平台项目制作融资路演PPT" in results[0]
    assert not any("公司内部培训" in item for item in results)


def test_search_short_chinese_entity_plus_artifact_matches_memory(mgr: MemoryManager):
    """A short entity plus artifact type is enough for explicit memory_search."""
    from box_agent.memory import _new_entry, write_context_file

    target = _new_entry("用户之前做过庄周 PPT，主题是逍遥游和齐物论。")
    weak_ppt = _new_entry("- 公司内部培训类 PPT 偏好：浅色背景、图标、小色块")
    entity_only = _new_entry("- 用户之前讨论过庄周思想，但没有产物记录。")
    write_context_file(mgr.context_file, [weak_ppt, entity_only, target])

    results = mgr.search("庄周 ppt 的内容返回给我")

    assert results
    assert "庄周 PPT" in results[0]
    assert not any("公司内部培训" in item for item in results)
    assert not any("没有产物记录" in item for item in results)


def test_search_ignores_single_weak_term_from_long_query(mgr: MemoryManager):
    """A lone weak artifact term such as ppt should not recall unrelated entries."""
    mgr.write_context("- 公司内部培训类 PPT 偏好：浅色背景、图标、小色块")

    assert mgr.search("帮我把那个 ppt 发我") == []


def test_search_caps_results_at_limit(mgr: MemoryManager):
    """High-frequency keywords must not flood the response."""
    from box_agent.memory import _new_entry, write_context_file

    entries = [_new_entry(f"- entry {i} mentions report") for i in range(10)]
    write_context_file(mgr.context_file, entries)

    results = mgr.search("report", limit=3)
    assert len(results) == 3
