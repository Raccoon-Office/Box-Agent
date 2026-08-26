"""Periodic maintenance for CONTEXT.md: decay, archive cleanup, dedup.

The maintainer is invoked at CLI / ACP server startup. A timestamp guard
(``.maintainer_last_run`` inside the memory dir) caps the work to at most
one run per ``memory_maintainer_interval_hours`` window — repeated startups
are cheap no-ops.

Phases (all idempotent and safe to interrupt — each writes its file only
on change):

1. **decay**    — active entries with ``hits == 0`` and ``last_used``
   older than ``memory_decay_days`` move from CONTEXT.md to
   CONTEXT.archive.md (metadata preserved).
2. **cleanup**  — archive entries with ``last_used`` older than
   ``memory_decay_days + memory_archive_days`` move into
   ``<memory_dir>/trash/<YYYY-MM-DD>/`` as a dated archive snapshot.
3. **dedup**    — token-Jaccard similarity (default 0.85 threshold) on
   CONTEXT.md entries; near-duplicates merge into the entry with higher
   ``hits`` (ties broken by older ``created``). Merged metadata: hits
   summed, created = min, last_used = max, confidence = max.
4. **resolve_conflicts** — LLM-arbitrated semantic conflict pass. Low-threshold
   Jaccard clustering (default 0.3) groups topically related entries; LLM
   inspects each cluster for "fact/decision mutual exclusion" (e.g. *use Redis*
   vs *switch to Postgres*) and returns winner + losers. Losers move to
   archive; the winner is left untouched. Gated by
   ``memory_conflict_resolution_enabled`` and presence of an LLM client.
5. **compact**  — LLM-driven topic-cluster merge when CONTEXT.md exceeds
   ``memory_context_max_entries`` or ``memory_context_max_tokens``.
   Schema-validated JSON output; failure keeps the original. Trash
   backup written before overwrite. Gated by ``memory_compaction_enabled``
   and the presence of an LLM client.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from .llm.buffered_stream import generate_buffered_stream
from .llm.model_routing import resolve_model_client

from .memory import (
    ContextEntry,
    jaccard as _jaccard,
    parse_context_file,
    tokens as _tokens,
    write_context_file,
)

if TYPE_CHECKING:
    from .config import AgentConfig
    from .memory import MemoryManager

logger = logging.getLogger(__name__)


def _parse_iso_utc(stamp: str) -> datetime:
    """Parse a ``_now_iso`` string back to an aware UTC datetime.

    Tolerates absent timezone (treated as UTC) and falls back to ``now``
    on unparseable input so a corrupt entry doesn't block the whole run.
    """
    try:
        dt = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now_iso_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class MemoryMaintainer:
    """Run periodic maintenance on CONTEXT.md / CONTEXT.archive.md.

    Construct once per process and call ``run_if_due()`` at startup.
    Internally all phases are sync I/O wrapped as async methods for
    uniformity with the rest of the codebase; they are fast for typical
    file sizes (≤ a few hundred entries).
    """

    def __init__(self, mgr: "MemoryManager", config: "AgentConfig", llm=None):
        self._mgr = mgr
        self._cfg = config
        self._llm = llm

    # ── Entry point ─────────────────────────────────────────────

    @property
    def _last_run_marker(self) -> Path:
        return self._mgr.memory_dir / ".maintainer_last_run"

    def _due(self, now: datetime) -> bool:
        if not self._cfg.memory_maintainer_enabled:
            return False
        marker = self._last_run_marker
        if not marker.exists():
            return True
        try:
            last = _parse_iso_utc(marker.read_text(encoding="utf-8").strip())
        except OSError:
            return True
        return (now - last) >= timedelta(hours=self._cfg.memory_maintainer_interval_hours)

    async def run_if_due(self) -> bool:
        """Run maintenance phases when the time guard allows it.

        Returns True if maintenance ran, False if skipped (disabled or
        marker too recent). All phases are best-effort: a failure in one
        phase is logged and the rest still execute. Local filesystem and
        CPU-heavy phases run in worker threads so ACP stdio remains responsive.
        """
        now = datetime.now(timezone.utc)
        if not self._due(now):
            return False

        run_started = monotonic()
        try:
            entries_before = await asyncio.to_thread(
                lambda: len(self._mgr.read_all_context_entries())
            )
        except Exception:
            entries_before = -1
            logger.exception("MemoryMaintainer: failed to count entries before run")

        logger.info(
            "MemoryMaintainer: run start entries=%d interval_hours=%d",
            entries_before,
            self._cfg.memory_maintainer_interval_hours,
        )
        phase_durations_ms: dict[str, int] = {}
        for phase_name, phase, run_in_worker in (
            ("decay", self._decay, True),
            ("cleanup_archive", self._cleanup_archive, True),
            ("dedup", self._dedup, True),
            ("resolve_conflicts", self._resolve_conflicts, False),
            ("compact", self._compact, False),
        ):
            logger.info("MemoryMaintainer: phase start name=%s", phase_name)
            phase_started = monotonic()
            try:
                if run_in_worker:
                    await asyncio.to_thread(phase, now)
                else:
                    await phase(now)
            except Exception:
                logger.exception("MemoryMaintainer: %s phase failed", phase_name)
            finally:
                phase_durations_ms[phase_name] = int(
                    (monotonic() - phase_started) * 1000
                )

        try:
            await asyncio.to_thread(
                self._last_run_marker.write_text,
                _now_iso_stamp() + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.exception("MemoryMaintainer: could not write last-run marker")

        try:
            entries_after = await asyncio.to_thread(
                lambda: len(self._mgr.read_all_context_entries())
            )
        except Exception:
            entries_after = -1
            logger.exception("MemoryMaintainer: failed to count entries after run")

        logger.info(
            "MemoryMaintainer: run complete entries=%d->%d duration_ms=%d phase_ms=%s",
            entries_before,
            entries_after,
            int((monotonic() - run_started) * 1000),
            phase_durations_ms,
        )
        return True

    # ── Phase 1: decay ──────────────────────────────────────────

    def _decay(self, now: datetime) -> None:
        with self._mgr.context_transaction():
            self._decay_transaction(now)

    def _decay_transaction(self, now: datetime) -> None:
        threshold = now - timedelta(days=self._cfg.memory_decay_days)
        entries = self._mgr.read_all_context_entries()
        if not entries:
            return

        active: list[ContextEntry] = []
        to_archive: list[ContextEntry] = []
        for e in entries:
            if e.hits == 0 and _parse_iso_utc(e.last_used) < threshold:
                to_archive.append(e)
            else:
                active.append(e)

        if not to_archive:
            return

        existing_archive = parse_context_file(self._mgr.archive_file)
        write_context_file(self._mgr.archive_file, existing_archive + to_archive)
        self._mgr.write_all_context_entries(active)
        logger.info(
            "MemoryMaintainer: decayed %d entries to archive (active=%d)",
            len(to_archive), len(active),
        )

    # ── Phase 2: archive cleanup → trash ────────────────────────

    def _cleanup_archive(self, now: datetime) -> None:
        with self._mgr.context_transaction():
            self._cleanup_archive_transaction(now)

    def _cleanup_archive_transaction(self, now: datetime) -> None:
        cutoff_days = self._cfg.memory_decay_days + self._cfg.memory_archive_days
        threshold = now - timedelta(days=cutoff_days)
        entries = parse_context_file(self._mgr.archive_file)
        if not entries:
            return

        keep: list[ContextEntry] = []
        purge: list[ContextEntry] = []
        for e in entries:
            if _parse_iso_utc(e.last_used) < threshold:
                purge.append(e)
            else:
                keep.append(e)

        if not purge:
            return

        date_dir = self._mgr.trash_dir / now.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        trash_file = date_dir / f"context-archive-{now.strftime('%H%M%S')}.md"
        write_context_file(trash_file, purge)
        write_context_file(self._mgr.archive_file, keep)
        logger.info(
            "MemoryMaintainer: purged %d archive entries to %s",
            len(purge), trash_file,
        )

    # ── Phase 3: dedup ──────────────────────────────────────────

    def _dedup(self, _now: datetime) -> None:
        with self._mgr.context_transaction():
            self._dedup_transaction(_now)

    def _dedup_transaction(self, _now: datetime) -> None:
        threshold = self._cfg.memory_dedup_jaccard
        entries = self._mgr.read_all_context_entries()
        if len(entries) < 2:
            return

        # Greedy: pre-tokenize, then merge low-priority into high-priority.
        # Priority key: (hits desc, created asc) — most-used + oldest wins.
        token_cache: list[set[str]] = [_tokens(e.content) for e in entries]
        indices = sorted(
            range(len(entries)),
            key=lambda i: (-entries[i].hits, entries[i].created),
        )

        merged_into: dict[int, int] = {}  # loser_idx → winner_idx
        for pos, winner in enumerate(indices):
            if winner in merged_into:
                continue
            for loser in indices[pos + 1:]:
                if loser in merged_into:
                    continue
                if _jaccard(token_cache[winner], token_cache[loser]) >= threshold:
                    merged_into[loser] = winner

        if not merged_into:
            return

        # Apply merges: aggregate metadata into winners, drop losers.
        for loser_idx, winner_idx in merged_into.items():
            w = entries[winner_idx]
            l = entries[loser_idx]
            w.hits += l.hits
            if l.created < w.created:
                w.created = l.created
            if l.last_used > w.last_used:
                w.last_used = l.last_used
            if l.confidence > w.confidence:
                w.confidence = l.confidence

        kept = [e for i, e in enumerate(entries) if i not in merged_into]
        self._mgr.write_all_context_entries(kept)
        logger.info(
            "MemoryMaintainer: deduped %d → %d entries (merged %d)",
            len(entries), len(kept), len(merged_into),
        )


# ── Phase 4: LLM-driven topic compaction ──────────────────────

_COMPACT_SYSTEM_PROMPT = """你是一个负责整理用户记忆的助手。

输入：N 条用户事实/偏好记录，每条带 id 和 hits（命中次数）。
任务：把主题相近的记录合并成更概括的一条，输出更少但信息密度更高的列表。

严格规则：
1. **必须保留所有具体偏好、规则、禁止项**（如"禁止 pip 直装"必须完整保留，不能被泛化稀释）
2. **绝对不允许引入原文中未出现的事实或推断**
3. 合并后的 hits = 来源条目 hits 之和
4. 不应合并的孤立事实保持原样（content 复用原文，hits 不变）
5. 输出**纯 JSON 数组**，无注释、无 markdown 围栏，每个元素形如：
   {"content": "<合并后的内容>", "hits": <int>, "sources": ["<原 id1>", "<原 id2>", ...]}

合并示例：
输入：
- id=a, hits=5: user generating brazil football intro ppt
- id=b, hits=3: user generating spain football intro ppt
- id=c, hits=2: user generating germany football ppt
输出（其中一条）：
{"content": "用户经常生成各国足球队介绍 PPT（巴西/西班牙/德国等）", "hits": 10, "sources": ["a", "b", "c"]}

不应合并的示例（强偏好独立保留）：
输入：
- id=d, hits=12: project uses uv for dependency management, never use pip directly
输出（原样保留）：
{"content": "project uses uv for dependency management, never use pip directly", "hits": 12, "sources": ["d"]}
"""


def _estimate_tokens(entries: list[ContextEntry]) -> int:
    """Cheap char-based token estimate. Avoids importing a real tokenizer."""
    return sum(len(e.content) for e in entries) // 3


def _parse_compact_output(text: str, valid_ids: set[str]) -> list[dict] | None:
    """Strip code fences, parse JSON, validate schema. Returns ``None`` on failure."""
    import json as _json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        # drop fence
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = _json.loads(cleaned)
    except (_json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list) or not data:
        return None

    seen_sources: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            return None
        content = item.get("content")
        hits = item.get("hits")
        sources = item.get("sources")
        if not isinstance(content, str) or not content.strip():
            return None
        if not isinstance(hits, int) or hits < 0:
            return None
        if not isinstance(sources, list) or not sources:
            return None
        for sid in sources:
            if not isinstance(sid, str) or sid not in valid_ids:
                return None
            seen_sources.add(sid)
    return data


class MemoryMaintainer_Compact:  # placeholder so the file parses; will be inlined.
    """Phase 4 helpers — methods are bound to ``MemoryMaintainer`` below."""

    async def _compact(self, now: datetime) -> None:
        if not self._cfg.memory_compaction_enabled or self._llm is None:
            return

        entries = await asyncio.to_thread(self._mgr.read_all_context_entries)
        if len(entries) < 2:
            return

        # Trigger only when over capacity (entry count or token budget).
        max_entries = self._cfg.memory_context_max_entries
        max_tokens = self._cfg.memory_context_max_tokens
        if len(entries) <= max_entries and _estimate_tokens(entries) <= max_tokens:
            return

        from .schema import Message as Msg

        bullet_lines = [
            f"- id={e.id}, hits={e.hits}: {e.content.strip()}"
            for e in entries
        ]
        user_prompt = "请整理以下 {n} 条记忆：\n\n{body}".format(
            n=len(entries), body="\n".join(bullet_lines),
        )

        try:
            maintenance_llm, _ = resolve_model_client(
                self._llm,
                task="总结压缩长期记忆",
                strategy="utility",
                task_tags=("summary",),
                required_ability_level=1,
            )
            response = await generate_buffered_stream(
                maintenance_llm,
                messages=[
                    Msg(role="system", content=_COMPACT_SYSTEM_PROMPT),
                    Msg(role="user", content=user_prompt),
                ],
                call_kind="memory_extract",
            )
        except Exception:
            logger.exception("MemoryMaintainer: compact LLM call failed; keeping original")
            return

        valid_ids = {e.id for e in entries}
        parsed = _parse_compact_output(response.content or "", valid_ids)
        if parsed is None:
            logger.warning("MemoryMaintainer: compact output invalid; keeping original")
            return

        trash_dir = self._mgr.trash_dir / now.strftime("%Y-%m-%d") / "compact"
        snapshot_by_id = {e.id: e for e in entries}

        def _commit_compaction() -> tuple[int, int] | None:
            from .memory import _new_entry as _make_entry

            with self._mgr.context_transaction():
                current_entries = self._mgr.read_all_context_entries()
                current_by_id = {e.id: e for e in current_entries}

                # A concurrent replace/drop changes the semantic input that the
                # LLM saw. Keep current data instead of committing stale output.
                for entry_id, snapshot in snapshot_by_id.items():
                    current = current_by_id.get(entry_id)
                    if current is None or current.content != snapshot.content:
                        logger.info(
                            "MemoryMaintainer: compact snapshot changed; "
                            "keeping current memory"
                        )
                        return None

                compacted: list[ContextEntry] = []
                for item in parsed:
                    sources = [current_by_id[sid] for sid in item["sources"]]
                    source_topics = {s.topic or "general" for s in sources}
                    topic = (
                        next(iter(source_topics))
                        if len(source_topics) == 1
                        else "general"
                    )
                    new = _make_entry(item["content"], source="compact", topic=topic)
                    # Recompute metadata from the locked current state so search
                    # hits recorded while the LLM was running are not lost.
                    new.hits = sum(s.hits for s in sources)
                    new.created = min(s.created for s in sources)
                    new.last_used = max(s.last_used for s in sources)
                    new.confidence = max(s.confidence for s in sources)
                    if any(s.core_status == "rejected" for s in sources):
                        new.core_status = "rejected"
                    compacted.append(new)

                # Entries added after the LLM snapshot were never part of its
                # input and must survive the replacement.
                concurrent = [
                    e for e in current_entries if e.id not in snapshot_by_id
                ]
                final_entries = compacted + concurrent

                trash_dir.mkdir(parents=True, exist_ok=True)
                stamp = _now_iso_stamp().replace(":", "-")
                for topic_path in sorted(self._mgr.context_dir.glob("*.md")):
                    backup_path = trash_dir / f"{topic_path.stem}.{stamp}.md"
                    backup_path.write_text(
                        topic_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )

                self._mgr.write_all_context_entries(final_entries)
                return len(current_entries), len(final_entries)

        try:
            committed_counts = await asyncio.to_thread(_commit_compaction)
        except OSError:
            logger.exception("MemoryMaintainer: compact backup failed; aborting")
            return
        if committed_counts is None:
            return

        logger.info(
            "MemoryMaintainer: compacted %d → %d entries (backup_dir=%s)",
            committed_counts[0], committed_counts[1], trash_dir,
        )


# Bind _compact onto the real MemoryMaintainer class (defined above) so the
# phase loop at run_if_due can resolve self._compact uniformly.
MemoryMaintainer._compact = MemoryMaintainer_Compact._compact  # type: ignore[attr-defined]


# ── Phase 3.5: LLM-arbitrated semantic conflict resolution ────

_CONFLICT_SYSTEM_PROMPT = """你是记忆冲突仲裁助手。

输入：N 条主题相近的记忆条目（同一聚类簇，按 token 重叠聚合）。
任务：识别其中**语义互斥**的条目对/组——典型例子是"事实/决策"前后不一致：
  "项目使用 Redis" vs "项目改用 Postgres"
  "用户偏好夜间主题" vs "用户改回白天主题"
  "采用 OAuth 登录" vs "切回密码登录"

严格规则：
1. 只在**事实/决策互斥**时返回 group；相容信息（互补、补充、不同侧面、不同实体的不同属性）一律忽略
2. 同一组内：winner 通常是 created 更新的那一条；当内容含"改用 / 切换到 / 替换为 / 弃用"等明示替换语义时，被替换的一方即使 created 较新也归为 loser
3. 如不存在冲突，返回 {"groups": []}
4. 不要把 winner_id 同时放进 loser_ids
5. 输出**纯 JSON**，无注释、无 markdown 围栏：
   {"groups": [{"winner_id": "<id>", "loser_ids": ["<id1>", "<id2>", ...], "reason": "<≤30字简述>"}]}

示例：
输入：
- id=a, created=2026-01-01, hits=3: 项目使用 Redis 做缓存
- id=b, created=2026-04-01, hits=1: 项目改用 Postgres 自带的 LISTEN/NOTIFY 替代 Redis
- id=c, created=2026-02-01, hits=2: 数据库迁移脚本放在 migrations/ 目录
输出：
{"groups": [{"winner_id": "b", "loser_ids": ["a"], "reason": "明示替换 Redis → Postgres"}]}
（c 与缓存方案不互斥，不入 group）
"""


def _cluster_by_jaccard(entries: list[ContextEntry], threshold: float) -> list[list[int]]:
    """Greedy union-find clustering by token-Jaccard ≥ threshold.

    Returns clusters as lists of entry indices; clusters with < 2 members
    are dropped (singletons can't conflict with anything).
    """
    n = len(entries)
    if n < 2:
        return []
    token_cache = [_tokens(e.content) for e in entries]
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if _jaccard(token_cache[i], token_cache[j]) >= threshold:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) >= 2]


def _parse_conflict_output(text: str, valid_ids: set[str]) -> list[dict] | None:
    """Strip code fences, parse JSON, validate schema. Returns ``None`` on failure.

    Validates: groups is list; each group has winner_id ∈ valid_ids, loser_ids
    all ∈ valid_ids, winner_id not in loser_ids, no duplicate losers.
    """
    import json as _json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = _json.loads(cleaned)
    except (_json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    groups = data.get("groups")
    if not isinstance(groups, list):
        return None

    cleaned_groups: list[dict] = []
    seen_losers: set[str] = set()
    for g in groups:
        if not isinstance(g, dict):
            return None
        winner = g.get("winner_id")
        losers = g.get("loser_ids")
        if not isinstance(winner, str) or winner not in valid_ids:
            return None
        if not isinstance(losers, list) or not losers:
            return None
        for lid in losers:
            if not isinstance(lid, str) or lid not in valid_ids:
                return None
            if lid == winner or lid in seen_losers:
                return None
            seen_losers.add(lid)
        cleaned_groups.append({"winner_id": winner, "loser_ids": list(losers)})
    return cleaned_groups


class MemoryMaintainer_Conflict:  # placeholder — bound onto MemoryMaintainer below.
    """Phase 3.5 helpers — methods are bound to ``MemoryMaintainer``."""

    async def _resolve_conflicts(self, _now: datetime) -> None:
        if not self._cfg.memory_conflict_resolution_enabled or self._llm is None:
            return

        entries = await asyncio.to_thread(self._mgr.read_all_context_entries)
        if len(entries) < 2:
            return

        clusters = await asyncio.to_thread(
            _cluster_by_jaccard,
            entries,
            self._cfg.memory_conflict_cluster_threshold,
        )
        if not clusters:
            return

        # Process the largest clusters first (most likely to harbor conflicts);
        # cap by config to bound LLM cost. Deterministic tiebreak by min index.
        clusters.sort(key=lambda c: (-len(c), min(c)))
        max_clusters = self._cfg.memory_conflict_max_clusters_per_run
        clusters = clusters[:max_clusters]

        from .schema import Message as Msg

        all_loser_ids: set[str] = set()
        winners_touched: set[str] = set()

        for cluster in clusters:
            cluster_entries = [entries[i] for i in cluster]
            bullet_lines = [
                f"- id={e.id}, created={e.created}, hits={e.hits}: {e.content.strip()}"
                for e in cluster_entries
            ]
            user_prompt = "请仲裁以下 {n} 条同主题记忆是否存在冲突：\n\n{body}".format(
                n=len(cluster_entries), body="\n".join(bullet_lines),
            )

            try:
                maintenance_llm, _ = resolve_model_client(
                    self._llm,
                    task="分析仲裁冲突记忆",
                    strategy="utility",
                    task_tags=("analysis", "reasoning"),
                    required_ability_level=2,
                )
                response = await generate_buffered_stream(
                    maintenance_llm,
                    messages=[
                        Msg(role="system", content=_CONFLICT_SYSTEM_PROMPT),
                        Msg(role="user", content=user_prompt),
                    ],
                    call_kind="memory_extract",
                )
            except Exception:
                logger.exception("MemoryMaintainer: conflict LLM call failed; skipping cluster")
                continue

            valid_ids = {e.id for e in cluster_entries}
            parsed = _parse_conflict_output(response.content or "", valid_ids)
            if parsed is None:
                logger.warning("MemoryMaintainer: conflict output invalid; skipping cluster")
                continue

            for group in parsed:
                # Don't double-archive a loser already claimed by an earlier group.
                new_losers = [lid for lid in group["loser_ids"] if lid not in all_loser_ids]
                if not new_losers:
                    continue
                all_loser_ids.update(new_losers)
                winners_touched.add(group["winner_id"])

        if not all_loser_ids:
            return

        snapshot_by_id = {e.id: e for e in entries}

        def _apply_conflict_results() -> int:
            with self._mgr.context_transaction():
                current_entries = self._mgr.read_all_context_entries()
                current_by_id = {e.id: e for e in current_entries}
                decided_ids = all_loser_ids | winners_touched
                for entry_id in decided_ids:
                    snapshot = snapshot_by_id[entry_id]
                    current = current_by_id.get(entry_id)
                    if current is None or current.content != snapshot.content:
                        logger.info(
                            "MemoryMaintainer: conflict snapshot changed; "
                            "keeping current memory"
                        )
                        return 0
                losers = [e for e in current_entries if e.id in all_loser_ids]
                if not losers:
                    return 0
                kept = [e for e in current_entries if e.id not in all_loser_ids]
                existing_archive = parse_context_file(self._mgr.archive_file)
                write_context_file(self._mgr.archive_file, existing_archive + losers)
                self._mgr.write_all_context_entries(kept)
                return len(losers)

        loser_count = await asyncio.to_thread(_apply_conflict_results)
        logger.info(
            "MemoryMaintainer: resolved %d conflict losers across %d winners (clusters scanned=%d)",
            loser_count, len(winners_touched), len(clusters),
        )


MemoryMaintainer._resolve_conflicts = MemoryMaintainer_Conflict._resolve_conflicts  # type: ignore[attr-defined]
