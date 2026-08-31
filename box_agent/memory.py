"""Memory system for cross-session recall, search, and auto-extraction.

Directory layout::

    ~/.box-agent/memory/
    ├── MEMORY.md          # Core memory (always injected into system prompt)
    ├── memory_summary.md  # Lightweight routing summary for deciding memory_search
    └── v2/experiences/    # Topic-sharded searchable context (retrieved on demand)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Iterator

from .llm.model_routing import resolve_model_client

if TYPE_CHECKING:
    from .schema import Message

logger = logging.getLogger(__name__)


_OPENCLAW_IMPORT_MAX_OUTPUT_TOKENS = 4_096


# ── Token / Jaccard helpers (shared with MemoryMaintainer) ──────

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> set[str]:
    """Lowercase word-character tokens for Jaccard similarity.

    Unicode-aware: handles CJK runs as single tokens, so 中文+英文
    混排 content lines remain comparable.
    """
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def jaccard(a: set[str], b: set[str]) -> float:
    """Symmetric set-overlap ratio. 0.0 when either side is empty."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


# ── Context entry: metadata-bearing record stored in CONTEXT.md ──

@dataclass
class ContextEntry:
    """One context-memory record with metadata.

    Stored under {memory_dir}/context/{topic}.md as an HTML comment header
    followed by a content block. ``hits``/``last_used`` mutate over time;
    ``topic`` is stable once assigned (re-classification happens via
    maintainer, not in-place edits).
    """
    id: str
    content: str
    created: str  # ISO-8601 UTC, second precision
    last_used: str
    hits: int = 0
    source: str = "tool"  # "tool" | "extractor" | "legacy" | "user"
    confidence: float = 1.0
    topic: str = "general"  # slug; routes the entry to context/{topic}.md
    session_id: str = ""  # host-owned conversation/session id, if available
    turn_id: str = ""  # host-owned user-visible turn id, if available
    trigger: str = ""  # extraction/write trigger, e.g. "loop_end"
    # Promotion-to-core tracking. ``core_status`` is "none" by default;
    # set to "rejected" after the user permanently declines promotion.
    # ``last_proposed`` is bumped each time the entry is offered for
    # core promotion so the cooldown skips noisy candidates.
    core_status: str = "none"  # "none" | "rejected"
    last_proposed: str = ""  # ISO-8601 UTC or empty if never proposed


_ENTRY_HEADER_RE = re.compile(r"^\s*<!--\s*ctx\s+(.+?)\s*-->\s*$")
_ENTRY_KV_RE = re.compile(r"(\w+)=(\S+)")


def _now_iso() -> str:
    """UTC ISO-8601 timestamp at second precision (no microseconds, no TZ suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _new_entry_id() -> str:
    """Sortable, collision-resistant id: ``ctx_<utc>_<rand6>``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"ctx_{stamp}_{uuid.uuid4().hex[:6]}"


def _header_value(value: Any, *, max_len: int = 256) -> str:
    """Normalize metadata values for the whitespace-delimited ctx header."""

    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"\s+", "_", text)[:max_len]


def _new_entry(content: str, *, source: str = "tool", confidence: float = 1.0,
               topic: str = "general", session_id: str = "",
               turn_id: str = "", trigger: str = "") -> ContextEntry:
    now = _now_iso()
    return ContextEntry(
        id=_new_entry_id(),
        content=content.strip(),
        created=now,
        last_used=now,
        hits=0,
        source=source,
        confidence=confidence,
        topic=topic or "general",
        session_id=_header_value(session_id),
        turn_id=_header_value(turn_id),
        trigger=_header_value(trigger),
    )


def _format_entry_header(e: ContextEntry) -> str:
    parts = [
        f"id={e.id}",
        f"created={e.created}",
        f"last_used={e.last_used}",
        f"hits={e.hits}",
        f"source={e.source}",
        f"confidence={e.confidence:.2f}",
        f"topic={e.topic or 'general'}",
    ]
    if e.session_id:
        parts.append(f"session_id={_header_value(e.session_id)}")
    if e.turn_id:
        parts.append(f"turn_id={_header_value(e.turn_id)}")
    if e.trigger:
        parts.append(f"trigger={_header_value(e.trigger)}")
    if e.core_status and e.core_status != "none":
        parts.append(f"core_status={e.core_status}")
    if e.last_proposed:
        parts.append(f"last_proposed={e.last_proposed}")
    return "<!-- ctx " + " ".join(parts) + " -->"


def _parse_entry_header(line: str) -> dict[str, str] | None:
    m = _ENTRY_HEADER_RE.match(line)
    if not m:
        return None
    return dict(_ENTRY_KV_RE.findall(m.group(1)))


def parse_context_file(path: Path) -> list[ContextEntry]:
    """Parse CONTEXT.md into entries. Auto-detects legacy line-based format.

    Legacy format (no ``<!-- ctx ... -->`` headers): each non-empty line
    becomes a separate entry with default metadata and ``source="legacy"``.
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return _parse_context_text(text)


def _parse_context_text(text: str) -> list[ContextEntry]:
    lines = text.splitlines()
    has_headers = any(_ENTRY_HEADER_RE.match(l) for l in lines)

    if not has_headers:
        return [
            _new_entry(line, source="legacy")
            for line in lines
            if line.strip()
        ]

    entries: list[ContextEntry] = []
    i = 0
    while i < len(lines):
        meta = _parse_entry_header(lines[i])
        if meta is None:
            i += 1
            continue
        i += 1
        content_lines: list[str] = []
        while i < len(lines) and _parse_entry_header(lines[i]) is None:
            content_lines.append(lines[i])
            i += 1
        while content_lines and not content_lines[-1].strip():
            content_lines.pop()
        while content_lines and not content_lines[0].strip():
            content_lines.pop(0)
        content = "\n".join(content_lines).strip()
        if not content:
            continue
        try:
            entries.append(ContextEntry(
                id=meta.get("id") or _new_entry_id(),
                content=content,
                created=meta.get("created") or _now_iso(),
                last_used=meta.get("last_used") or meta.get("created") or _now_iso(),
                hits=int(meta.get("hits", "0")),
                source=meta.get("source") or "legacy",
                confidence=float(meta.get("confidence", "1.0")),
                topic=meta.get("topic") or "general",
                session_id=meta.get("session_id") or meta.get("sessionId") or "",
                turn_id=meta.get("turn_id") or meta.get("turnId") or "",
                trigger=meta.get("trigger") or "",
                core_status=meta.get("core_status") or "none",
                last_proposed=meta.get("last_proposed") or "",
            ))
        except (ValueError, TypeError):
            logger.warning("Bad ContextEntry metadata, using defaults: %s", meta)
            entries.append(_new_entry(content, source="legacy"))
    return entries


def write_context_file(path: Path, entries: list[ContextEntry]) -> None:
    """Serialize entries to CONTEXT.md in the metadata-bearing format."""
    if not entries:
        path.write_text("", encoding="utf-8")
        return
    parts: list[str] = []
    for e in entries:
        parts.append(_format_entry_header(e))
        parts.append(e.content)
        parts.append("")  # blank line between entries
    text = "\n".join(parts).rstrip() + "\n"
    path.write_text(text, encoding="utf-8")


# ── Topic-aware storage ─────────────────────────────────────

_TOPIC_SLUG_FORBIDDEN_RE = re.compile(r"[\s/\\:*?\"<>|.,;]+")
_TOPIC_INDEX_FILENAME = "_index.json"
_V2_DIRNAME = "v2"
_EXPERIENCES_DIRNAME = "experiences"
_V2_STATE_FILENAME = "state.json"
_MEMORY_SUMMARY_FILENAME = "memory_summary.md"
_MEMORY_SUMMARY_MAX_TOPICS = 12
_MEMORY_SUMMARY_MAX_TERMS_PER_TOPIC = 16


def _slugify_topic(text: str, max_len: int = 64) -> str:
    """Convert a free-form topic label to a filesystem-safe slug.

    Non-ASCII characters (e.g. Chinese) are preserved — modern filesystems
    handle them — but whitespace and FS-unsafe punctuation collapse to ``-``.
    Empty / all-punctuation input falls back to ``"general"``.
    """
    s = (text or "").strip().lower()
    s = _TOPIC_SLUG_FORBIDDEN_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        return "general"
    return s[:max_len]


class TopicStore:
    """Storage layer for CONTEXT entries split across {context_dir}/{topic}.md.

    Each per-topic file uses the same metadata-header format as the legacy
    monolithic CONTEXT.md, so :func:`parse_context_file` /
    :func:`write_context_file` round-trip unchanged at the per-file level.
    """

    def __init__(self, context_dir: Path):
        self._dir = context_dir

    @property
    def context_dir(self) -> Path:
        return self._dir

    @property
    def index_file(self) -> Path:
        return self._dir / _TOPIC_INDEX_FILENAME

    def _topic_path(self, topic: str) -> Path:
        return self._dir / f"{_slugify_topic(topic)}.md"

    def list_topics(self) -> list[str]:
        if not self._dir.exists():
            return []
        return sorted(
            p.stem
            for p in self._dir.glob("*.md")
            if p.is_file() and not p.stem.startswith("_")
        )

    def read_topic(self, topic: str) -> list[ContextEntry]:
        path = self._topic_path(topic)
        if not path.exists():
            return []
        entries = parse_context_file(path)
        slug = _slugify_topic(topic)
        # Topic field is authoritative from the header; backfill if missing.
        for e in entries:
            if not e.topic:
                e.topic = slug
        return entries

    def read_all(self) -> list[ContextEntry]:
        out: list[ContextEntry] = []
        for slug in self.list_topics():
            out.extend(self.read_topic(slug))
        return out

    def read_topics(self, topics: list[str]) -> list[ContextEntry]:
        out: list[ContextEntry] = []
        seen: set[str] = set()
        for topic in topics:
            slug = _slugify_topic(topic)
            if slug in seen:
                continue
            seen.add(slug)
            out.extend(self.read_topic(slug))
        return out

    def read_all_grouped(self) -> dict[str, list[ContextEntry]]:
        return {slug: self.read_topic(slug) for slug in self.list_topics()}

    def ensure_index(self) -> None:
        """Rebuild the sidecar index when it is missing or pre-vocabulary."""
        topics = self.list_topics()
        if not topics:
            return
        index = self.read_index()
        if (
            set(index.keys()) != set(topics)
            or any("terms" not in index.get(slug, {}) for slug in topics)
        ):
            self._write_index(self.read_all_grouped())

    def write_all(self, entries: list[ContextEntry]) -> None:
        """Persist *entries* to per-topic files; remove topics now empty.

        ``entry.topic`` is normalized to its slug form before grouping so a
        round-trip is stable. The sidecar index is rebuilt to match.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        grouped: dict[str, list[ContextEntry]] = {}
        for e in entries:
            slug = _slugify_topic(e.topic or "general")
            e.topic = slug
            grouped.setdefault(slug, []).append(e)

        existing = {p.stem for p in self._dir.glob("*.md") if p.is_file()}
        for slug, group in grouped.items():
            write_context_file(self._dir / f"{slug}.md", group)
        for stale in existing - set(grouped.keys()):
            if stale.startswith("_"):
                continue
            try:
                (self._dir / f"{stale}.md").unlink()
            except OSError:
                pass

        self._write_index(grouped)

    def write_topics(self, entries: list[ContextEntry]) -> None:
        """Persist entries for their topics without touching unrelated topics."""
        self._dir.mkdir(parents=True, exist_ok=True)
        grouped: dict[str, list[ContextEntry]] = {}
        for e in entries:
            slug = _slugify_topic(e.topic or "general")
            e.topic = slug
            grouped.setdefault(slug, []).append(e)

        for slug, group in grouped.items():
            write_context_file(self._dir / f"{slug}.md", group)
        self._merge_index(grouped)

    def delete_topic(self, topic: str) -> bool:
        path = self._topic_path(topic)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError:
            return False
        self._remove_from_index(_slugify_topic(topic))
        return True

    def read_index(self) -> dict[str, dict[str, Any]]:
        if not self.index_file.exists():
            return {}
        try:
            import json as _json

            data = _json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def match_topics(self, query: str) -> list[str]:
        """Return topic slugs whose index vocabulary overlaps the query."""
        query_lower = query.lower()
        query_terms = set(_extract_match_terms(query_lower))
        if not query_terms and not query_lower:
            return []

        index = self.read_index()
        scored: list[tuple[int, int, str]] = []
        for slug in self.list_topics():
            item = index.get(slug, {})
            terms = {str(t) for t in item.get("terms", []) if isinstance(t, str)}
            score = 0
            slug_terms = {
                slug,
                slug.replace("-", "_"),
                slug.replace("_", "-"),
                slug.replace("-", " "),
                slug.replace("_", " "),
            }
            if any(term and term in query_lower for term in slug_terms):
                score += 8
            overlap = query_terms & terms
            score += len(overlap) * 2
            if score > 0:
                scored.append((score, int(item.get("hits_total", 0) or 0), slug))

        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [slug for _, _, slug in scored]

    def _write_index(self, grouped: dict[str, list[ContextEntry]]) -> None:
        import json as _json
        try:
            index = {slug: self._index_record(slug, group) for slug, group in grouped.items()}
            self.index_file.write_text(
                _json.dumps(index, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.exception("TopicStore: failed to write _index.json")

    def _merge_index(self, grouped: dict[str, list[ContextEntry]]) -> None:
        import json as _json

        index = self.read_index()
        for slug, group in grouped.items():
            index[slug] = self._index_record(slug, group)
        try:
            self.index_file.write_text(
                _json.dumps(index, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.exception("TopicStore: failed to update _index.json")

    def _remove_from_index(self, slug: str) -> None:
        import json as _json

        index = self.read_index()
        if slug not in index:
            return
        index.pop(slug, None)
        try:
            self.index_file.write_text(
                _json.dumps(index, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.exception("TopicStore: failed to update _index.json")

    @staticmethod
    def _index_record(slug: str, group: list[ContextEntry]) -> dict[str, Any]:
        terms: set[str] = set(_extract_match_terms(slug.replace("-", " ")))
        for entry in group:
            terms.update(_extract_match_terms(entry.content.lower()))
        return {
            "count": len(group),
            "last_updated": max((e.last_used for e in group), default=""),
            "hits_total": sum(e.hits for e in group),
            "terms": sorted(terms)[:120],
        }


def _serialized_context(method):
    """Run one complete context-memory operation under the manager transaction."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.context_transaction():
            return method(self, *args, **kwargs)

    return wrapped


class MemoryManager:
    """Two-tier memory: MEMORY.md (core) + topic-sharded searchable context.

    - **MEMORY.md** — user identity, preferences, writing style.
      Always injected into the system prompt via ``recall()``.
      Written by LLM via ``memory_write(category="core")``.

    - **memory_summary.md** — compact routing guide injected with core memory so
      the model can decide when ``memory_search`` is worth calling without an
      extra memory model pass.

    - **v2/experiences/<topic>.md** — project context, task patterns,
      behavioral feedback.
      Retrieved on demand via topic-routed ``memory_search``.
      Written by ``memory_write(category="context")`` and ``MemoryExtractor``.
    """

    def __init__(
        self,
        memory_dir: str = "~/.box-agent/memory",
        *,
        dedup_jaccard_threshold: float = 0.85,
        **_kwargs,
    ):
        self.memory_dir = Path(memory_dir).expanduser()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.dedup_jaccard_threshold = dedup_jaccard_threshold
        self._context_transaction_lock = threading.RLock()

        # v2 is an overlay, not a migration.  New context/experience writes go
        # here; old context files remain untouched and are searched only as a
        # fallback for explicit memory_search calls.
        self._legacy_context_dir = self.memory_dir / "context"
        self._v2_dir = self.memory_dir / _V2_DIRNAME
        self._context_dir = self._v2_dir / _EXPERIENCES_DIRNAME
        self._context_dir.mkdir(parents=True, exist_ok=True)
        self._topic_store = TopicStore(self._context_dir)
        self._legacy_topic_store = TopicStore(self._legacy_context_dir)

        self._ensure_v2_state()
        self._topic_store.ensure_index()
        self.refresh_memory_summary()

    @contextmanager
    def context_transaction(self) -> Iterator[None]:
        """Serialize one manager's context-memory transaction.

        This lock is instance-local; coordinating multiple managers or processes
        that share ``memory_dir`` requires a separate file-locking boundary.
        """

        with self._context_transaction_lock:
            yield

    # ── File paths ──────────────────────────────────────────────

    @property
    def memory_file(self) -> Path:
        """MEMORY.md — core memory, always injected."""
        return self.memory_dir / "MEMORY.md"

    @property
    def context_dir(self) -> Path:
        """Directory holding v2 per-topic experience markdown files."""
        return self._context_dir

    @property
    def memory_summary_file(self) -> Path:
        """memory_summary.md — lightweight routing guide for searchable memory."""
        return self.memory_dir / _MEMORY_SUMMARY_FILENAME

    @property
    def legacy_context_dir(self) -> Path:
        """Read-only fallback directory for pre-v2 context markdown files."""
        return self._legacy_context_dir

    @property
    def topic_store(self) -> "TopicStore":
        """Topic-sharded storage for CONTEXT entries."""
        return self._topic_store

    @property
    def context_file(self) -> Path:
        """Backward-compat shim: path of the v2 ``general`` topic file.

        Prefer :meth:`topic_store` / :meth:`read_all_context_entries` for new
        code. Direct reads/writes here only affect the default v2 topic.
        """
        return self._context_dir / "general.md"

    @property
    def legacy_context_file(self) -> Path:
        """Fallback path for old monolithic/top-level CONTEXT.md."""
        return self.memory_dir / "CONTEXT.md"

    @property
    def archive_file(self) -> Path:
        """CONTEXT.archive.md — cold storage for decayed entries (not injected, not searched)."""
        return self.memory_dir / "CONTEXT.archive.md"

    @property
    def trash_dir(self) -> Path:
        """Soft-delete root for purged entries. Lazy-created on first write."""
        return self.memory_dir / "trash"

    @property
    def v2_state_file(self) -> Path:
        """Marker describing the v2 no-migration cutover policy."""
        return self._v2_dir / _V2_STATE_FILENAME

    # ── Core memory (MEMORY.md) ─────────────────────────────────

    def read_core(self) -> str:
        """Read MEMORY.md content. Returns empty string if missing."""
        if not self.memory_file.exists():
            return ""
        return self.memory_file.read_text(encoding="utf-8").strip()

    def write_core(self, content: str) -> None:
        """Overwrite MEMORY.md with *content*."""
        self.memory_file.write_text(content.strip() + "\n", encoding="utf-8")

    def append_core(self, content: str) -> None:
        """Append to MEMORY.md."""
        existing = self.read_core()
        if existing:
            self.write_core(f"{existing}\n{content.strip()}")
        else:
            self.write_core(content)

    def append_core_dedup(self, content: str) -> bool:
        """Append non-duplicate lines to MEMORY.md. Returns True if changed."""
        core = self.read_core()
        core_lines_norm = {
            line.strip().lower()
            for line in core.splitlines()
            if line.strip()
        }
        to_append: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            norm = stripped.lower()
            if not norm or norm in core_lines_norm:
                continue
            core_lines_norm.add(norm)
            to_append.append(stripped)

        if not to_append:
            return False
        if core:
            self.write_core(core + "\n" + "\n".join(to_append))
        else:
            self.write_core("\n".join(to_append))
        return True

    # Legacy aliases — backward compat for existing callers/tests
    read_all = read_core
    write_all = write_core
    read_manual_memory = read_core
    write_manual_memory = write_core

    # ── Context memory (CONTEXT.md) ─────────────────────────────

    @_serialized_context
    def read_context(self) -> str:
        """Read v2 context plus legacy fallback as joined plain text."""
        entries = self._read_context_entries() + self._read_legacy_context_entries()
        if not entries:
            return ""
        return "\n".join(e.content for e in entries).strip()

    @_serialized_context
    def write_context(self, content: str, *, topic: str = "general") -> None:
        """Overwrite context entries for *topic* with *content*.

        Destructive — drops metadata of existing entries **in that topic only**.
        Other topics are untouched. Used by callers that already hold the
        desired final text (tests, legacy paths). For non-destructive updates
        use ``append_context`` / ``apply_context_operations``.
        """
        slug = _slugify_topic(topic)
        replacement = [
            _new_entry(line, source="tool", topic=slug)
            for line in content.splitlines()
            if line.strip()
        ]
        all_entries = [e for e in self._read_context_entries() if (e.topic or "general") != slug]
        all_entries.extend(replacement)
        self._write_context_entries(all_entries)

    @_serialized_context
    def append_context(self, content: str, *, topic: str = "general") -> None:
        """Append to CONTEXT.md, skipping lines already present in Core or Context.

        Two-tier dedup:

        1. Exact line-level (case-insensitive) — catches verbatim repeats.
        2. Token-Jaccard fuzzy match against existing entries — catches
           paraphrased restatements of the same fact. On match, the existing
           entry's ``hits`` is bumped and ``last_used`` refreshed instead of
           adding a new entry.

        New entries are written under *topic* (default ``"general"``). Existing
        entries' metadata (hits, created, etc., and original topic) is preserved
        on fuzzy match.
        """
        existing = self.read_context()
        filtered = self._dedupe_context_lines(content, existing_context=existing)

        if not filtered:
            return

        existing_entries = self._read_context_entries()
        threshold = self.dedup_jaccard_threshold
        entry_tokens = [tokens(e.content) for e in existing_entries]
        topic_slug = _slugify_topic(topic)

        new_entries: list[ContextEntry] = []
        merged_any = False
        now = _now_iso()

        for line in filtered:
            line_tokens = tokens(line)
            best_idx = -1
            best_score = 0.0
            if line_tokens:
                for idx, et in enumerate(entry_tokens):
                    score = jaccard(line_tokens, et)
                    if score > best_score:
                        best_score = score
                        best_idx = idx

            if best_idx >= 0 and best_score >= threshold:
                existing_entries[best_idx].hits += 1
                existing_entries[best_idx].last_used = now
                merged_any = True
            else:
                entry = _new_entry(line, source="tool", topic=topic_slug)
                new_entries.append(entry)
                existing_entries.append(entry)
                entry_tokens.append(line_tokens)

        if not new_entries and not merged_any:
            return

        self._write_context_entries(existing_entries)

    @_serialized_context
    def _read_context_entries(self) -> list[ContextEntry]:
        """Parse all v2 experience topic files into a flat entry list."""
        return self._topic_store.read_all()

    def _read_legacy_context_entries(self) -> list[ContextEntry]:
        """Read pre-v2 context without mutating it.

        Legacy entries are preserved for explicit search fallback only.  They do
        not participate in auto-match, promotion, extraction writes, or
        maintainer rewrites.
        """
        entries: list[ContextEntry] = []
        entries.extend(self._legacy_topic_store.read_all())
        if self.legacy_context_file.exists():
            entries.extend(parse_context_file(self.legacy_context_file))
        return entries

    def _write_context_entries(self, entries: list[ContextEntry]) -> None:
        """Persist *entries* across topic files."""
        self._topic_store.write_all(entries)
        self.refresh_memory_summary()

    def _write_context_topic_entries(self, entries: list[ContextEntry]) -> None:
        """Persist entries for touched topics only."""
        self._topic_store.write_topics(entries)
        self.refresh_memory_summary()

    @_serialized_context
    def read_all_context_entries(self) -> list[ContextEntry]:
        """Public: read every context entry across all topics."""
        return self._topic_store.read_all()

    @_serialized_context
    def write_all_context_entries(self, entries: list[ContextEntry]) -> None:
        """Public: replace all context entries (sharded by ``entry.topic``)."""
        self._topic_store.write_all(entries)
        self.refresh_memory_summary()

    @_serialized_context
    def list_topics(self) -> list[str]:
        """Return the list of known topic slugs (excluding the JSON sidecar)."""
        return self._topic_store.list_topics()

    @_serialized_context
    def read_context_topic(self, topic: str) -> str:
        """Return the joined v2 content of one topic, or empty if unknown."""
        entries = self._topic_store.read_topic(topic)
        if not entries:
            return ""
        return "\n".join(e.content for e in entries).strip()

    @_serialized_context
    def read_memory_summary(self) -> str:
        """Return the generated memory routing summary, refreshing it first."""
        return self.refresh_memory_summary()

    @_serialized_context
    def refresh_memory_summary(self) -> str:
        """Generate and persist a compact memory routing summary.

        This file is an index, not a migration target.  It gives the model the
        Codex-style decision boundary for when to call ``memory_search`` and a
        small topic/term map for v2 experiences.  It never rewrites legacy
        context files and it does not include full context entries.
        """
        summary = self._build_memory_summary()
        try:
            if summary:
                current = self.memory_summary_file.read_text(encoding="utf-8") if self.memory_summary_file.exists() else ""
                if current != summary:
                    self.memory_summary_file.write_text(summary, encoding="utf-8")
            elif self.memory_summary_file.exists():
                current = self.memory_summary_file.read_text(encoding="utf-8")
                empty_summary = self._empty_memory_summary()
                if current != empty_summary:
                    self.memory_summary_file.write_text(empty_summary, encoding="utf-8")
        except OSError:
            logger.exception("Failed to refresh memory_summary.md")
        return summary

    def _build_memory_summary(self) -> str:
        topics = self._topic_store.list_topics()
        legacy_present = self._has_legacy_context()
        if not topics and not legacy_present:
            return ""

        lines = [
            "# Memory Routing Summary",
            "",
            "Use `memory_search` when the current request may depend on saved user preferences, historical decisions, repo or workflow conventions, previously verified fixes, specific paths, recurring errors, or prior task experience.",
            "Skip memory for clearly one-off simple questions, trivial rewrites, current time/date, or requests fully answered by the visible conversation.",
            "",
            "When calling `memory_search`:",
            "- Do not pass the whole user sentence when it contains action words such as send, write, help, or give me.",
            "- Search 1-3 short durable keys instead: project/product name, repo/module/path, exact error, workflow, artifact type, or prior decision.",
            "- Prefer noun phrases over commands. For Chinese prompts, split compound intents into separate searches.",
            "- If a narrow search misses, retry with a broader stable term or an explicit topic.",
            "- Examples: `排产平台融资 ppt 的演讲稿发我` -> `排产平台`, `融资路演`, `ppt`; `上次 EACCES 怎么修` -> `EACCES`, `npm cache`, `runtime install`.",
            "",
            "Search behavior:",
            "- v2 experiences are searched first.",
            "- Legacy context is read-only explicit-search fallback only; it is not auto-matched or promoted.",
            "- Automatic matches are weak hints and do not count as promotion evidence.",
            "",
        ]

        if topics:
            index = self._topic_store.read_index()
            lines.append("Searchable v2 experience topics:")
            for slug in topics[:_MEMORY_SUMMARY_MAX_TOPICS]:
                record = index.get(slug, {})
                count = int(record.get("count", 0) or 0)
                raw_terms = record.get("terms", [])
                terms = [
                    str(term)
                    for term in raw_terms
                    if isinstance(term, str) and term.strip()
                ][:_MEMORY_SUMMARY_MAX_TERMS_PER_TOPIC]
                term_text = ", ".join(terms) if terms else "no indexed terms"
                lines.append(f"- `{slug}`: {count} entr{'y' if count == 1 else 'ies'}; terms: {term_text}")
            if len(topics) > _MEMORY_SUMMARY_MAX_TOPICS:
                lines.append(f"- ... {len(topics) - _MEMORY_SUMMARY_MAX_TOPICS} more topic(s) omitted from the routing summary.")
            lines.append("")

        if legacy_present:
            lines.append("Legacy context: present. Use `memory_search` only when the summary or user request suggests older saved context may matter.")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _empty_memory_summary() -> str:
        return (
            "# Memory Routing Summary\n\n"
            "No searchable context memory is saved yet.\n"
        )

    def _has_legacy_context(self) -> bool:
        if self.legacy_context_file.exists():
            return True
        if not self._legacy_context_dir.exists():
            return False
        return any(
            p.is_file() and p.suffix == ".md" and not p.stem.startswith("_")
            for p in self._legacy_context_dir.iterdir()
        )

    def _ensure_v2_state(self) -> None:
        """Create a small marker for the no-migration v2 cutover."""
        if self.v2_state_file.exists():
            return
        try:
            self._v2_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 2,
                "cutover_at": _now_iso(),
                "legacy_context_policy": "explicit_search_fallback_only",
                "legacy_promotion_policy": "disabled",
            }
            self.v2_state_file.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.exception("Failed to create memory v2 state marker")

    def _dedupe_context_lines(self, content: str, *, existing_context: str | None = None) -> list[str]:
        """Return non-empty context lines not already present in Core or Context.

        Deduplication is intentionally line-level and case-insensitive so exact
        saved facts are not repeated while still allowing a later LLM merge to
        refine or replace older context lines.
        """
        core = self.read_core()
        existing_context = self.read_context() if existing_context is None else existing_context

        seen = {
            line.strip().lower()
            for source in (core, existing_context)
            for line in source.splitlines()
            if line.strip()
        }

        filtered: list[str] = []
        for line in content.strip().splitlines():
            normalized = line.strip().lower()
            if normalized and normalized not in seen:
                filtered.append(line)
                seen.add(normalized)
        return filtered

    # ── Search ──────────────────────────────────────────────────

    @_serialized_context
    def search(self, query: str, *, limit: int = 5, topic: str | None = None) -> list[str]:
        """Keyword search across v2 experiences, then legacy fallback.

        Returns entry contents (deduped across multi-line entries) ranked by
        exact occurrence and query-term overlap, with historical ``hits`` as a
        tiebreak.  Capped at ``limit`` so noisy keywords cannot flood the model.

        Side effect: v2 matches increment ``hits`` and refresh ``last_used``.
        Legacy fallback matches are read-only and never become promotion
        evidence.
        """
        if not query:
            return []
        query_lower = query.lower().strip()
        entries, routed_topics, explicit_topic = self._entries_for_query(query, topic=topic)
        results = self._search_entries(query_lower, entries, limit, routed_topics)

        if not results and routed_topics is not None and not explicit_topic:
            entries = self._read_context_entries()
            results = self._search_entries(query_lower, entries, limit, None)

        if not results:
            results = self._search_legacy_context(query_lower, limit=limit, topic=topic)

        return results

    def _search_legacy_context(self, query_lower: str, *, limit: int, topic: str | None = None) -> list[str]:
        """Read-only search over pre-v2 context."""
        if topic:
            entries = self._legacy_topic_store.read_topic(topic)
            if _slugify_topic(topic) == "general" and self.legacy_context_file.exists():
                entries.extend(parse_context_file(self.legacy_context_file))
        else:
            entries = self._read_legacy_context_entries()
        return self._search_entries(query_lower, entries, limit, None, update_usage=False)

    @_serialized_context
    def auto_match_context(self, query: str, *, limit: int = 3) -> list[dict[str, str]]:
        """Return high-confidence context-memory matches for a user prompt.

        This is intentionally conservative: it only returns context lines that
        share strong phrase/token overlap with the prompt.  The result is meant
        to provide *possibly relevant* context, never to force the model to
        treat a new request as a continuation of an old task.

        Auto-match is read-only: it does not increment hits or create
        promotion evidence. Explicit ``memory_search`` is the usage signal.
        """
        query = _sanitize_auto_match_query(query)
        if _is_title_generation_query(query):
            return []
        if not query:
            return []

        query_lower = query.lower()
        query_terms = _extract_match_terms(query_lower)
        if not query_terms:
            return []

        entries, routed_topics, explicit_topic = self._entries_for_query(query)
        if not entries:
            return []

        top = self._auto_match_entries(
            query,
            query_lower,
            query_terms,
            entries,
            routed_topics,
            limit,
            update_usage=False,
        )
        if not top and routed_topics is not None and not explicit_topic:
            entries = self._read_context_entries()
            top = self._auto_match_entries(
                query,
                query_lower,
                query_terms,
                entries,
                None,
                limit,
                update_usage=False,
            )

        return [
            {
                "id": f"context:{line_no}",
                "source": "context",
                "category": "context",
                "text": text,
            }
            for _, line_no, text, _ in top
        ]

    @_serialized_context
    def _entries_for_query(
        self,
        query: str,
        *,
        topic: str | None = None,
    ) -> tuple[list[ContextEntry], list[str] | None, bool]:
        if topic:
            slug = _slugify_topic(topic)
            return self._topic_store.read_topic(slug), [slug], True

        topics = self._topic_store.match_topics(query)
        if topics:
            return self._topic_store.read_topics(topics), topics, False
        return self._read_context_entries(), None, False

    def _search_entries(
        self,
        query_lower: str,
        entries: list[ContextEntry],
        limit: int,
        routed_topics: list[str] | None,
        *,
        update_usage: bool = True,
    ) -> list[str]:
        if not entries:
            return []

        query_terms = _extract_search_terms(query_lower)
        scored: list[tuple[float, int, int, str]] = []  # (score, occurrences, hits, content)
        changed = False
        now = _now_iso()

        for entry in entries:
            content_lower = entry.content.lower()
            score, occurrences = _score_memory_search(query_lower, query_terms, content_lower)
            if score <= 0:
                continue
            scored.append((score, occurrences, entry.hits, entry.content))
            if update_usage:
                entry.hits += 1
                entry.last_used = now
                changed = True

        if changed and update_usage:
            if routed_topics is None:
                self._write_context_entries(entries)
            else:
                self._write_context_topic_entries(entries)

        scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))
        return [content for _, _, _, content in scored[:limit]]

    def _auto_match_entries(
        self,
        query: str,
        query_lower: str,
        query_terms: list[str],
        entries: list[ContextEntry],
        routed_topics: list[str] | None,
        limit: int,
        *,
        update_usage: bool = True,
    ) -> list[tuple[float, int, str, int]]:
        # (score, line_no, text, entry_index) — line_no is scoped to the searched
        # topic set; callers treat it as a lightweight display id.
        scored: list[tuple[float, int, str, int]] = []
        line_no = 0
        for entry_idx, entry in enumerate(entries):
            for raw_line in entry.content.splitlines():
                line_no += 1
                text = raw_line.strip()
                if not text:
                    continue
                if _should_skip_auto_match_line(query_lower, text.lower()):
                    continue
                if _is_self_citation(query, text):
                    continue
                score = _score_memory_match(query_lower, query_terms, text.lower())
                if score >= 3.5:
                    scored.append((score, line_no, text, entry_idx))

        scored.sort(key=lambda item: (-item[0], item[1]))
        top = scored[:limit]

        if top and update_usage:
            now = _now_iso()
            hit_entry_indices = {entry_idx for _, _, _, entry_idx in top}
            for idx in hit_entry_indices:
                entries[idx].hits += 1
                entries[idx].last_used = now
            if routed_topics is None:
                self._write_context_entries(entries)
            else:
                self._write_context_topic_entries(entries)

        return top

    # ── Recall (system prompt injection) ────────────────────────

    @_serialized_context
    def recall(self, **_kwargs) -> str:
        """Build a memory block for system-prompt injection.

        Injects MEMORY.md (core) plus the lightweight ``memory_summary.md``
        routing guide. Full context entries remain on demand via
        ``memory_search``.
        """
        core = self.read_core()
        summary = self.read_memory_summary()
        if not core and not summary:
            return ""
        return self.build_memory_block(core, memory_summary=summary)

    # ── OpenClaw import ─────────────────────────────────────────

    @property
    def _openclaw_imported_marker(self) -> Path:
        return self.memory_dir / ".openclaw_imported"

    def _read_openclaw_raw(self) -> str:
        """Read MEMORY.md and USER.md files from ~/.openclaw/. Returns empty if none."""
        openclaw_dir = Path.home() / ".openclaw"
        if not openclaw_dir.is_dir():
            return ""

        parts: list[str] = []

        # USER.md — user identity and preferences
        for user_file in sorted(openclaw_dir.rglob("USER.md")):
            try:
                content = user_file.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"[Source: {user_file.relative_to(openclaw_dir)}]\n{content}")
            except Exception:
                logger.debug("Failed to read OpenClaw file: %s", user_file)

        # MEMORY.md — session memories
        for memory_file in sorted(openclaw_dir.rglob("MEMORY.md")):
            try:
                content = memory_file.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"[Source: {memory_file.relative_to(openclaw_dir)}]\n{content}")
            except Exception:
                logger.debug("Failed to read OpenClaw file: %s", memory_file)

        return "\n\n".join(parts)

    async def import_openclaw(self, llm) -> str:
        """One-time LLM-filtered import of OpenClaw data into Core.

        Reads ``~/.openclaw/**/USER.md`` and ``**/MEMORY.md``, asks LLM
        to extract useful user info (identity, preferences, habits),
        appends to MEMORY.md, and marks as imported so it won't run again.

        Returns the imported content, or empty string if nothing to import.
        """
        if self._openclaw_imported_marker.exists():
            return ""

        raw = self._read_openclaw_raw()
        if not raw:
            self._openclaw_imported_marker.write_text("no-content\n", encoding="utf-8")
            return ""

        existing_core = self.read_core()

        from .schema import Message as Msg

        prompt = (
            "Extract ONLY the useful user information from the following content.\n\n"
            "Keep:\n"
            "- User identity (name, role, department, company)\n"
            "- Preferences (language, writing style, tools)\n"
            "- Work habits and behavioral patterns\n\n"
            "Discard:\n"
            "- Ephemeral task details, file paths, code snippets\n"
            "- Session logs, timestamps, debugging info\n"
            "- Anything already present in existing memory\n\n"
            f"Existing core memory:\n{existing_core or '(empty)'}\n\n"
            f"Content to filter:\n{raw[:8000]}\n\n"
            "Output ONLY the useful bullet points (markdown format), nothing else. "
            "If nothing is useful, output exactly: (empty)"
        )

        try:
            memory_llm, _ = resolve_model_client(
                llm,
                task="从外部笔记中提炼长期用户记忆",
                strategy="utility",
                task_tags=("summary", "analysis"),
                required_ability_level=1,
                max_output_tokens_cap=_OPENCLAW_IMPORT_MAX_OUTPUT_TOKENS,
            )
            response = await memory_llm.generate(
                messages=[
                    Msg(role="system", content="You extract structured user information from raw notes."),
                    Msg(role="user", content=prompt),
                ],
                call_kind="memory_extract",
            )
            filtered = response.content.strip()
        except Exception:
            logger.exception("Failed to filter OpenClaw memory via LLM")
            return ""

        if filtered and filtered != "(empty)":
            self.append_core(filtered)
            logger.info("Imported OpenClaw memory into core: %d chars", len(filtered))

        self._openclaw_imported_marker.write_text("done\n", encoding="utf-8")
        return filtered

    @staticmethod
    def build_memory_block(core: str, *, memory_summary: str = "") -> str:
        """Format core memory into a prompt block."""
        if not core and not memory_summary:
            return ""

        parts: list[str] = ["--- MEMORY START ---"]
        parts.append("")
        if core:
            parts.append("[Core Memory]")
            parts.append(core)
            parts.append("")
        if memory_summary:
            parts.append("[Memory Search Routing]")
            parts.append(memory_summary.strip())
            parts.append("")
        parts.append("--- MEMORY END ---")
        return "\n".join(parts)

    # ── Shared helpers ─────────────────────────────────────────

    @staticmethod
    def _build_transcript(messages: list[Message], *, max_chars_per_msg: int = 2000) -> str:
        """Build a condensed text transcript from messages, skipping system messages."""
        parts: list[str] = []
        for msg in messages:
            if msg.role == "system":
                continue
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            parts.append(f"{msg.role.capitalize()}: {text[:max_chars_per_msg]}")
        return "\n".join(parts)

    async def update_context_with_llm(self, content: str, llm, *, topic: str = "general") -> str:
        """Ask an LLM how to merge candidate context, then safely apply it.

        The model decides semantic add/replace/drop/noop operations, while this
        method enforces exact-match mutations and line-level duplicate guards.
        ``topic`` is the default bucket for any operation that doesn't carry its
        own ``topic`` field, and is also used by the fallback append on planner
        failure.

        Returns:
            A short status label: ``"applied"``, ``"no_change"``, or
            ``"fallback_appended"``.
        """
        if not content.strip():
            return "no_change"

        from .schema import Message as Msg

        context = await asyncio.to_thread(self.read_context)
        prompt = _CONTEXT_UPDATE_USER_PROMPT.format(
            core_memory=await asyncio.to_thread(self.read_core) or "(empty)",
            context_memory=context or "(empty)",
            candidate=content.strip(),
        )

        try:
            memory_llm, _ = resolve_model_client(
                llm,
                task="分析并合并长期上下文记忆",
                strategy="utility",
                task_tags=("summary", "analysis"),
                required_ability_level=1,
            )
            response = await memory_llm.generate(
                messages=[
                    Msg(role="system", content=_CONTEXT_UPDATE_SYSTEM_PROMPT),
                    Msg(role="user", content=prompt),
                ],
                call_kind="memory_extract",
            )
            data = json.loads(_strip_json_fences(response.content))
        except Exception:
            logger.exception("Context memory update planning failed; falling back to append")
            before = await asyncio.to_thread(self.read_context)
            await asyncio.to_thread(self.append_context, content, topic=topic)
            after = await asyncio.to_thread(self.read_context)
            return "fallback_appended" if after != before else "no_change"

        operations = data.get("operations", [])
        # Stamp default topic on add operations that didn't specify one.
        for op in operations:
            if isinstance(op, dict) and op.get("action") == "add" and not op.get("topic"):
                op["topic"] = topic
        changed = await asyncio.to_thread(self.apply_context_operations, operations)
        return "applied" if changed else "no_change"

    @_serialized_context
    def apply_context_operations(self, operations: list[dict]) -> bool:
        """Safely apply model-planned context memory operations.

        ``replace`` and ``drop`` require exactly one entry whose content matches
        the target string. ``add`` uses the same Core/Context dedupe guard as
        direct appends. Entry metadata (hits, created) is preserved across
        replace/drop; only ``last_used`` bumps on replace.
        """
        entries = self._read_context_entries()
        core_norm = {
            line.strip().lower()
            for line in self.read_core().splitlines()
            if line.strip()
        }
        changed = False

        for op in operations:
            action = str(op.get("action", "")).strip().lower()

            if action == "replace":
                old = str(op.get("old", "")).strip()
                new = str(op.get("new", "")).strip()
                if not old or not new:
                    continue
                indices = [i for i, e in enumerate(entries) if e.content.strip() == old]
                if len(indices) != 1:
                    if len(indices) > 1:
                        logger.warning("Ambiguous context memory replace skipped (%d matches): %s", len(indices), old[:80])
                    else:
                        logger.debug("Context memory replace target not found: %s", old[:80])
                    continue
                new_norm = new.lower()
                others_norm = {
                    e.content.strip().lower()
                    for i, e in enumerate(entries) if i != indices[0]
                }
                if new_norm in others_norm or new_norm in core_norm:
                    entries.pop(indices[0])
                else:
                    entries[indices[0]].content = new
                    entries[indices[0]].last_used = _now_iso()
                changed = True

            elif action == "drop":
                content = str(op.get("content", "")).strip()
                if not content:
                    continue
                indices = [i for i, e in enumerate(entries) if e.content.strip() == content]
                if len(indices) != 1:
                    if len(indices) > 1:
                        logger.warning("Ambiguous context memory drop skipped (%d matches): %s", len(indices), content[:80])
                    continue
                entries.pop(indices[0])
                changed = True

            elif action == "add":
                content = str(op.get("content", "")).strip()
                if not content:
                    continue
                op_topic = _slugify_topic(str(op.get("topic", "") or "general"))
                op_source = _header_value(op.get("source") or "tool")
                op_session_id = _header_value(op.get("session_id") or op.get("sessionId"))
                op_turn_id = _header_value(op.get("turn_id") or op.get("turnId"))
                op_trigger = _header_value(op.get("trigger"))
                existing_norm = {e.content.strip().lower() for e in entries}
                for line in content.splitlines():
                    norm = line.strip().lower()
                    if not norm or norm in existing_norm or norm in core_norm:
                        continue
                    existing_norm.add(norm)
                    entries.append(
                        _new_entry(
                            line,
                            source=op_source or "tool",
                            topic=op_topic,
                            session_id=op_session_id,
                            turn_id=op_turn_id,
                            trigger=op_trigger,
                        )
                    )
                    changed = True

            elif action == "noop":
                continue

        if changed:
            self._write_context_entries(entries)
        return changed

    # ── Core-promotion candidates ───────────────────────────────

    @_serialized_context
    def list_promotion_candidates(
        self,
        *,
        hit_threshold: int,
        cooldown_days: int,
    ) -> list[ContextEntry]:
        """Return CONTEXT.md entries eligible for promotion to MEMORY.md (core).

        Filters:
        - ``hits >= hit_threshold``
        - ``core_status != "rejected"`` (rejection is permanent)
        - ``last_proposed`` either empty or older than ``cooldown_days``
        - ``source != "core"`` (never re-propose core-originated material)
        - terse enough for always-injected core memory
        """
        if hit_threshold <= 0:
            return []
        entries = self._read_context_entries()
        if not entries:
            return []
        now = datetime.now(timezone.utc)
        cooldown = timedelta(days=max(cooldown_days, 0))
        candidates: list[ContextEntry] = []
        for e in entries:
            if e.hits < hit_threshold:
                continue
            if e.core_status == "rejected":
                continue
            if e.source == "core":
                continue
            if not _is_core_promotion_worthy(e):
                continue
            if e.last_proposed:
                try:
                    last = datetime.fromisoformat(e.last_proposed)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if now - last < cooldown:
                        continue
                except (TypeError, ValueError):
                    pass
            candidates.append(e)
        return candidates

    @_serialized_context
    def mark_proposed(self, candidate_ids: list[str]) -> None:
        """Bump ``last_proposed`` on the given entries and persist."""
        if not candidate_ids:
            return
        entries = self._read_context_entries()
        wanted = set(candidate_ids)
        now = _now_iso()
        changed = False
        for e in entries:
            if e.id in wanted:
                e.last_proposed = now
                changed = True
        if changed:
            self._write_context_entries(entries)

    @_serialized_context
    def consume_core_proposal(self, decisions: dict[str, str]) -> dict[str, int]:
        """Apply user decisions to promotion candidates.

        ``decisions`` maps entry id → ``"pin"``, ``"skip"``, or ``"reject"``.

        - ``pin``: remove entry from CONTEXT.md, append its content to
          MEMORY.md (skipping if the same line already exists in core).
        - ``reject``: keep entry in CONTEXT.md but flip
          ``core_status="rejected"`` so it is never proposed again.
        - ``skip``: no-op. ``last_proposed`` was already bumped at emit
          time, so the cooldown carries the user past this candidate.

        Returns counts for each action ({"pinned": int, "rejected": int,
        "skipped": int}).
        """
        if not decisions:
            return {"pinned": 0, "rejected": 0, "skipped": 0}

        entries = self._read_context_entries()
        if not entries:
            return {"pinned": 0, "rejected": 0, "skipped": 0}

        pinned_contents: list[str] = []
        keep: list[ContextEntry] = []
        counts = {"pinned": 0, "rejected": 0, "skipped": 0}

        for e in entries:
            decision = decisions.get(e.id)
            if decision == "pin":
                pinned_contents.append(e.content)
                counts["pinned"] += 1
                continue
            if decision == "reject":
                e.core_status = "rejected"
                counts["rejected"] += 1
            elif decision == "skip":
                counts["skipped"] += 1
            keep.append(e)

        if pinned_contents:
            core = self.read_core()
            core_lines_norm = {
                line.strip().lower()
                for line in core.splitlines()
                if line.strip()
            }
            to_append: list[str] = []
            for content in pinned_contents:
                for line in content.splitlines():
                    norm = line.strip().lower()
                    if not norm or norm in core_lines_norm:
                        continue
                    core_lines_norm.add(norm)
                    to_append.append(line)
            if to_append:
                if core:
                    self.write_core(core + "\n" + "\n".join(to_append))
                else:
                    self.write_core("\n".join(to_append))

        if counts["pinned"] or counts["rejected"]:
            self._write_context_entries(keep)
        return counts

    # ── LLM-drafted promotion plan ──────────────────────────────

    async def plan_promotion(
        self,
        candidates: list[ContextEntry],
        llm,
    ) -> "MemoryPromotionPlan | None":
        """Ask the LLM to draft a single core rewrite consuming *candidates*.

        Returns ``None`` if:
        - the LLM call raises,
        - the response is not parseable JSON,
        - the proposed ``new_core`` shrinks the current core by >50% (a
          safety bound — promotion should grow or refine core, never gut it),
        - the planner is given no candidates.

        ``consumed_entry_ids`` in the returned plan is filtered to only
        contain ids actually present in *candidates*, so even a hallucinated
        id list can't delete unrelated entries on apply.
        """
        from .events import MemoryPromotionPlan

        if not candidates:
            return None

        def _snapshot_inputs() -> tuple[str, list[ContextEntry]]:
            with self.context_transaction():
                return self.read_core(), self._read_context_entries()

        current_core, context_entries = await asyncio.to_thread(_snapshot_inputs)
        context_by_id = {entry.id: entry for entry in context_entries}
        snapshot_candidates = [
            context_by_id[candidate.id]
            for candidate in candidates
            if candidate.id in context_by_id
        ]
        if not snapshot_candidates:
            return None

        candidate_ids = {entry.id for entry in snapshot_candidates}
        other_entries = [
            e for e in context_entries if e.id not in candidate_ids
        ]
        other_context = "\n".join(
            f"{e.id}: {e.content.strip()}" for e in other_entries
        )
        candidates_block = "\n".join(
            f"{e.id} (hits={e.hits}, confidence={e.confidence}):\n{e.content.strip()}"
            for e in snapshot_candidates
        )

        user_prompt = _PROMOTION_PLAN_USER_PROMPT.format(
            current_core=current_core or "(empty)",
            candidates=candidates_block,
            other_context=other_context or "(none)",
        )

        try:
            memory_llm, _ = resolve_model_client(
                llm,
                task="分析记忆候选并生成晋升摘要",
                strategy="utility",
                task_tags=("summary", "analysis"),
                required_ability_level=1,
            )
            response = await memory_llm.generate(
                messages=[
                    {"role": "system", "content": _PROMOTION_PLAN_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                tools=[],
                thinking_enabled=False,
                call_kind="memory_extract",
            )
        except Exception as exc:  # noqa: BLE001 — planner is best-effort
            logger.warning("plan_promotion: LLM call failed: %s", exc)
            return None

        raw = getattr(response, "content", "") or ""
        try:
            data = json.loads(_strip_json_fences(raw))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("plan_promotion: bad JSON from LLM: %s", exc)
            return None

        new_core = str(data.get("new_core", "")).strip()
        rationale = str(data.get("rationale", "")).strip()
        raw_ids = data.get("consumed_entry_ids", []) or []
        if not isinstance(raw_ids, list):
            logger.warning(
                "plan_promotion: consumed_entry_ids is not a list (got %s)",
                type(raw_ids).__name__,
            )
            return None
        consumed_ids = tuple(str(x) for x in raw_ids if str(x) in candidate_ids)
        if not consumed_ids or not new_core:
            logger.warning(
                "plan_promotion: empty plan rejected "
                "(consumed_ids=%d/%d, new_core_len=%d, raw_ids=%s, candidates=%s)",
                len(consumed_ids),
                len(raw_ids),
                len(new_core),
                raw_ids,
                sorted(candidate_ids),
            )
            return None

        # Safety: refuse plans that gut more than half of the current core.
        if current_core:
            if len(new_core) < len(current_core) * 0.5:
                logger.warning(
                    "plan_promotion: rejecting plan that shrinks core by >50%% "
                    "(%d -> %d chars)",
                    len(current_core), len(new_core),
                )
                return None

        return MemoryPromotionPlan(
            current_core=current_core,
            new_core=new_core,
            consumed_entry_ids=consumed_ids,
            rationale=rationale,
        )

    @_serialized_context
    def apply_promotion_plan(self, plan: "MemoryPromotionPlan") -> dict[str, int]:
        """Apply *plan*: overwrite MEMORY.md, drop consumed CONTEXT entries.

        Returns ``{"applied": 1, "consumed": N}``.
        """
        self.write_core(plan.new_core)
        consumed_set = set(plan.consumed_entry_ids)
        entries = self._read_context_entries()
        keep = [e for e in entries if e.id not in consumed_set]
        removed = len(entries) - len(keep)
        if removed:
            self._write_context_entries(keep)
        return {"applied": 1, "consumed": removed}

    @_serialized_context
    def reject_promotion_plan(self, plan: "MemoryPromotionPlan") -> dict[str, int]:
        """Mark every consumed candidate ``core_status="rejected"`` (no core change)."""
        consumed_set = set(plan.consumed_entry_ids)
        entries = self._read_context_entries()
        rejected = 0
        for e in entries:
            if e.id in consumed_set and e.core_status != "rejected":
                e.core_status = "rejected"
                rejected += 1
        if rejected:
            self._write_context_entries(entries)
        return {"rejected": rejected}


# ── Auto Memory Extraction ────────────────────────────────────────

# Controlled vocabulary for auto-extracted context topics. Keeping this set
# small prevents topic explosion (one file per stray label) — anything the LLM
# emits outside the set is folded back into "general".
_EXTRACTION_TOPICS = ("user_profile", "preferences", "project", "feedback", "general")


def _normalize_extraction_topic(raw: object) -> str:
    """Map an LLM-supplied topic label onto the controlled vocabulary.

    Unknown / empty labels fall back to ``"general"``. Dash and underscore are
    treated interchangeably so ``"user profile"`` / ``"user-profile"`` /
    ``"user_profile"`` all resolve to the same bucket.
    """
    slug = _slugify_topic(str(raw or "general")).replace("-", "_")
    return slug if slug in _EXTRACTION_TOPICS else "general"


_EXTRACTION_SYSTEM_PROMPT = "You are a memory extraction assistant. You analyze conversations to identify information worth remembering across sessions."

_EXTRACTION_USER_PROMPT = """\
Analyze the recent conversation below. Extract information worth remembering across sessions.

Categories to look for:
- User info: name, role, team, expertise, background, usual/default city, location, timezone
- Preferences: language, communication style, tools, workflows
- Project context: goals, constraints, key decisions, deadlines
- Behavioral feedback: corrections the user made, approaches that worked

Existing core memory (MEMORY.md — do NOT duplicate):
{core_memory}

Existing context memory (CONTEXT.md — check for duplicates before adding):
{context_memory}

Recent conversation:
{transcript}

Output buckets:
- "core_additions": explicit user-stated identity/profile facts, stable preferences, durable rules, and local defaults that should be available in every future session. Examples: name, role, preferred language/style, usual/default city or timezone.
- "additions": project context, task patterns, historical notes, and feedback that should stay searchable but not always injected.
- "merges": replacements for existing context memory only.

Rules:
1. Only extract cross-session-valuable information. Ignore ephemeral task details.
2. If new info updates or refines something in context memory, output a merge.
3. If info is genuinely new, output an addition.
4. Do NOT record code details, git operations, file paths, or anything derivable from the codebase.
5. If there is nothing worth remembering, return empty arrays.
6. Distill to abstract facts, preferences, or constraints. NEVER quote, copy, or near-paraphrase the user's exact sentences — the user must not see their own input echoed back as "memory". If you can only restate what the user just said, return empty arrays.
7. If the conversation is mostly a one-off task description, request, or in-progress work without a stable cross-session fact emerging, return empty arrays.
8. For local facts, only save explicit self-statements. If the user says "I am in Beijing" / "我在北京" while asking weather, directions, delivery, or other local services, save a cautious default such as "- 用户用于本地查询的默认城市是北京"; do not infer residence or permanence. If the wording is clearly temporary travel, do not save it to core.
9. Tag each context addition with exactly one topic from this fixed set (pick the closest; use "general" if none fit):
   - "user_profile": identity, role, team, expertise, background
   - "preferences": language, communication style, tool/workflow preferences
   - "project": goals, constraints, key decisions, deadlines
   - "feedback": corrections and approaches the user endorsed
   - "general": anything cross-session-useful that fits none of the above
10. Write memory bullets in the user's dominant language when it is clear; otherwise use concise English.

Output ONLY valid JSON (no markdown fences):
{{"core_additions": ["- core memory bullet"], "additions": [{{"text": "- context bullet point 1", "topic": "preferences"}}, {{"text": "- context bullet point 2", "topic": "project"}}], "merges": [{{"old": "exact old line", "new": "replacement line"}}]}}"""

_CONTEXT_UPDATE_SYSTEM_PROMPT = (
    "You are a long-term memory curator. You update persistent context memory "
    "by preserving useful project/task context, merging semantic duplicates, "
    "and discarding ephemeral details."
)

_CONTEXT_UPDATE_USER_PROMPT = """\
Decide how to update CONTEXT.md using the candidate memory.

Existing core memory (MEMORY.md — do NOT duplicate into context):
{core_memory}

Existing context memory (CONTEXT.md):
{context_memory}

Candidate memory to save:
{candidate}

Rules:
1. Keep only cross-session-useful project context, task patterns, decisions, deadlines, or behavioral feedback.
2. If the candidate duplicates existing context semantically, do not add it.
3. If the candidate refines an existing line, replace that exact old line with one better line.
4. Do not duplicate core memory into context.
5. Do not rewrite the whole file. Prefer minimal add/replace/drop/noop operations.
6. For replace/drop, old/content MUST exactly match one full existing context line.

Output ONLY valid JSON (no markdown fences):
{{"operations": [
  {{"action": "add", "content": "- new memory line", "reason": "why it should be saved"}},
  {{"action": "replace", "old": "- exact old line", "new": "- improved line", "reason": "why it refines old memory"}},
  {{"action": "drop", "content": "- exact old line", "reason": "why existing line should be removed"}},
  {{"action": "noop", "content": "- candidate line", "reason": "why nothing should change"}}
]}}"""


_PROMOTION_PLAN_SYSTEM_PROMPT = (
    "You are a memory curator promoting hot context-memory entries into core "
    "(MEMORY.md). Core is always injected into the system prompt of every "
    "session, so it must stay terse, deduplicated, and high-signal."
)

_PROMOTION_PLAN_USER_PROMPT = """\
A few context-memory entries have been accessed often enough to be candidates
for promotion into core. Decide how to integrate them.

Current core memory (MEMORY.md — your new_core will REPLACE this entirely):
{current_core}

Promotion candidates (each may be merged with existing core lines, summarized
with related context, or kept as-is):
{candidates}

Other context-memory entries (DO NOT remove these from CONTEXT.md, but you may
summarize-and-fold any that are tightly related into core — list those ids
under consumed_entry_ids too):
{other_context}

Rules:
1. Output the FULL replacement core in `new_core` — preserve every existing
   useful line that you do not explicitly intend to remove or fold.
2. If a candidate refines or extends an existing core line, merge them — don't
   leave both.
3. If multiple candidates plus a related context entry describe one fact, fold
   them into a single core line.
4. Every id in `consumed_entry_ids` will be DELETED from CONTEXT.md on apply.
   Include every candidate id you have folded into new_core, plus any related
   context entries you also folded.
5. Never shrink core by more than half — additions and refinements only.
6. Keep core bullets one-line, lowercase-y prose, no headings unless already
   present.

Output ONLY valid JSON (no markdown fences):
{{
  "new_core": "<full replacement MEMORY.md text>",
  "consumed_entry_ids": ["ctx_...", "ctx_..."],
  "rationale": "<1-2 sentence summary of what changed and why>"
}}"""


_CORE_PROMOTION_MAX_CHARS = 360
_CORE_PROMOTION_MAX_LINES = 2
_CORE_PROMOTION_MAX_SUMMARY_SEPARATORS = 8
_CORE_PROMOTION_TOPICS: frozenset[str] = frozenset({"user_profile", "preferences"})

_TASK_HISTORY_PHRASES: frozenset[str] = frozenset({
    "工作项目包括",
    "已做项目包括",
    "近期关注",
    "已完成",
    "已交付",
    "上线计划",
    "报告类任务",
    "checklist",
})


def _strip_json_fences(text: str) -> str:
    """Strip optional markdown fences around model JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    return text.strip()


def _is_core_promotion_sized(content: str) -> bool:
    """True when a context entry is terse enough to review as core memory."""
    text = content.strip()
    if not text:
        return False
    if len(text) > _CORE_PROMOTION_MAX_CHARS:
        return False
    non_empty_lines = [line for line in text.splitlines() if line.strip()]
    if len(non_empty_lines) > _CORE_PROMOTION_MAX_LINES:
        return False
    return True


def _looks_like_task_history_summary(content: str) -> bool:
    """Detect dense task-history notes that should remain searchable context."""
    text = content.strip().lower()
    if not text:
        return False

    separator_count = sum(text.count(mark) for mark in ("；", ";", "，", ",", "、", "。"))
    if len(text) > 180 and separator_count > _CORE_PROMOTION_MAX_SUMMARY_SEPARATORS:
        return True

    phrase_hits = sum(1 for phrase in _TASK_HISTORY_PHRASES if phrase in text)
    return len(text) > 160 and phrase_hits >= 2


def _is_core_promotion_worthy(entry: ContextEntry) -> bool:
    """Return whether a hot context entry may be offered for core promotion.

    ``hits`` answers "was this useful to retrieve?".  Core promotion also needs
    a stronger shape check because core memory is injected into every session.
    Long conversation/task summaries stay in searchable context and are handled
    by context compaction, not direct user pinning.
    """
    if _slugify_topic(entry.topic or "general") not in _CORE_PROMOTION_TOPICS:
        return False
    if not _is_core_promotion_sized(entry.content):
        return False
    if _looks_like_task_history_summary(entry.content):
        return False
    return True


_NOISE_TERMS: frozenset[str] = frozenset({
    # generic nouns that appear in almost any prompt — windowing turns these
    # into match-everything wildcards when they slip into the term set.
    "项目", "用户", "功能", "系统", "模块", "文件", "数据",
    "内容", "信息", "方法", "工具", "需要", "可以", "怎么",
    "什么", "为什么", "如何", "这个", "那个", "这样", "那样",
    "今天", "明天", "昨天", "现在", "之前", "之后", "一些",
    "请帮我", "帮我看看", "请帮忙", "我希望", "请问一下",
})


_SEARCH_NOISE_TERMS: frozenset[str] = _NOISE_TERMS | frozenset({
    "帮我", "请帮", "请你", "给我", "发我", "写个", "写一",
    "做个", "做一", "一下", "这个", "那个", "发给", "帮忙",
    "返回", "返回给", "返回给我", "输出", "展示", "内容返",
    "内容返回", "内容返回给", "内容返回给我", "的内容", "的内",
    "send", "write", "help", "give", "make", "create",
})

_SEARCH_ARTIFACT_TERMS: frozenset[str] = frozenset({
    "ppt", "pptx", "deck", "slides", "slide", "pdf", "doc", "docx",
    "md", "html", "xlsx", "xls", "csv",
})

_SEARCH_ACTION_MARKERS: tuple[str, ...] = (
    "返回", "发我", "发给", "给我", "帮我", "输出", "展示",
)

_SEARCH_MIN_TERM_SCORE = 2.5


def _extract_match_terms(text: str) -> list[str]:
    """Extract conservative phrase-like terms from a prompt or memory line."""
    terms: set[str] = set()

    for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text):
        if token not in _NOISE_TERMS:
            terms.add(token)

    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if len(segment) < 5:
            # Short Chinese spans (2–4 chars) are too ambiguous as match terms
            # — they're typically common words ("培训", "公司") that produce
            # widespread false-positive hits.
            continue
        if len(segment) <= 6:
            if segment not in _NOISE_TERMS:
                terms.add(segment)
        else:
            for size in (6, 5):
                for idx in range(0, len(segment) - size + 1):
                    candidate = segment[idx:idx + size]
                    if candidate not in _NOISE_TERMS:
                        terms.add(candidate)

    return sorted(terms, key=lambda term: (-len(term), term))


def _extract_search_terms(text: str) -> list[str]:
    """Extract explicit-search terms from a query.

    ``memory_search`` is model-initiated, so it can be less conservative than
    auto-match.  We still strip common command words, but keep shorter durable
    Chinese terms like "融资" and file/tool tokens like "ppt" so compound user
    requests can find related memories even when the whole sentence is not a
    substring of the saved entry.
    """
    terms: set[str] = set(_extract_match_terms(text))

    for token in re.findall(r"[a-z0-9][a-z0-9_-]{1,}", text):
        if not _is_search_noise_term(token):
            terms.add(token)

    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if not _is_search_noise_term(segment) and len(segment) <= 12:
            terms.add(segment)
        for size in (4, 3, 2):
            if len(segment) < size:
                continue
            for idx in range(0, len(segment) - size + 1):
                candidate = segment[idx:idx + size]
                if not _is_search_noise_term(candidate):
                    terms.add(candidate)

    return sorted(
        {t for t in terms if t.strip() and not _is_search_noise_term(t)},
        key=lambda term: (-len(term), term),
    )


def _dedupe_contained_terms(terms: set[str] | list[str]) -> list[str]:
    """Keep the longest useful terms while preserving independent short terms."""
    ordered = sorted({t for t in terms if t.strip()}, key=lambda term: (-len(term), term))
    kept: list[str] = []
    for term in ordered:
        if any(term != other and term in other for other in kept):
            continue
        kept.append(term)
    return kept


def _search_term_weight(term: str) -> float:
    if re.search(r"[\u4e00-\u9fff]", term):
        if len(term) >= 5:
            return 3.0
        if len(term) == 4:
            return 2.0
        if len(term) == 3:
            return 1.2
        return 0.7
    if len(term) >= 5:
        return 1.7
    if len(term) == 4:
        return 1.3
    return 1.0


def _is_search_noise_term(term: str) -> bool:
    return term in _SEARCH_NOISE_TERMS or any(marker in term for marker in _SEARCH_ACTION_MARKERS)


def _is_search_artifact_term(term: str) -> bool:
    return term.lower() in _SEARCH_ARTIFACT_TERMS


def _is_short_cjk_entity_term(term: str) -> bool:
    return bool(
        re.search(r"[\u4e00-\u9fff]", term)
        and len(term) >= 2
        and not _is_search_noise_term(term)
    )


def _score_memory_search(query_lower: str, query_terms: list[str], memory_lower: str) -> tuple[float, int]:
    """Score explicit memory_search matches.

    Exact substring matches stay dominant for short deliberate queries.  Longer
    natural-language queries can still recall entries through several stable
    overlapping terms, but a single weak term such as "ppt" is not enough.
    """
    normalized_query = query_lower.strip()
    occurrences = memory_lower.count(normalized_query) if normalized_query else 0
    if occurrences:
        return float(occurrences * 100), occurrences

    if not query_terms:
        return 0.0, 0

    matched = _dedupe_contained_terms([term for term in query_terms if term in memory_lower])
    if not matched:
        return 0.0, 0

    score = sum(_search_term_weight(term) for term in matched)
    has_entity_artifact_pair = any(_is_short_cjk_entity_term(term) for term in matched) and any(
        _is_search_artifact_term(term) for term in matched
    )
    strong_matches = [
        term for term in matched
        if (
            (re.search(r"[\u4e00-\u9fff]", term) and len(term) >= 4)
            or (not re.search(r"[\u4e00-\u9fff]", term) and len(term) >= 4)
        )
    ]
    if not strong_matches and len(matched) < 2:
        return 0.0, 0
    if has_entity_artifact_pair:
        score = max(score, _SEARCH_MIN_TERM_SCORE)
    if score < _SEARCH_MIN_TERM_SCORE:
        return 0.0, 0

    memory_terms = _extract_search_terms(memory_lower)
    if memory_terms:
        matched_in_memory = len(set(matched) & set(memory_terms))
        if matched_in_memory > 0 and len(memory_terms) > 4 * matched_in_memory:
            score *= 0.75

    return score, 0


_TITLE_GENERATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"为(这段|该|此)?(对话|会话|聊天)\s*(提炼|生成|起|拟|取|命名|总结)"),
    re.compile(r"(对话|会话|聊天)\s*(标题|名称)\s*(提炼|生成|总结|是)"),
    re.compile(r"(提炼|生成|拟定|起)\s*一?个?\s*(对话|会话|聊天)?\s*标题"),
    re.compile(r"summari[sz]e\s+(this|the)\s+(conversation|chat|session)\s+(into|as)\s+a\s+title", re.IGNORECASE),
    re.compile(r"generate\s+a\s+(short\s+)?title", re.IGNORECASE),
)

_INTERROGATIVE_MARKERS: tuple[str, ...] = (
    "怎么", "如何", "什么", "为什么", "为何", "?", "？", "how", "what", "why",
)


def _is_title_generation_query(query: str) -> bool:
    """True if *query* looks like a host title-generation prompt (not a user question).

    Title-generation prompts are injected by the host (e.g. "请为这段对话
    提炼一个简短标题") and must not bump context-memory hit counts.  But a
    legitimate user question about titles ("会话标题怎么提炼") should still
    flow through auto-match, so we require no interrogative marker.
    """
    if not query:
        return False
    lower = query.lower()
    if any(marker in lower for marker in _INTERROGATIVE_MARKERS):
        return False
    return any(p.search(query) for p in _TITLE_GENERATION_PATTERNS)


def _sanitize_auto_match_query(query: str) -> str:
    """Remove host-appended operational instructions before auto matching."""
    text = query.strip()
    if not text:
        return ""

    markers = (
        "[文件输出规范]",
        "文件输出规范",
        "通用文件输出规范",
        "文件交付偏好",
        "通用文件交付规则",
    )
    cut = len(text)
    for marker in markers:
        idx = text.find(marker)
        if idx > 0:
            cut = min(cut, idx)
    return text[:cut].strip()


def _should_skip_auto_match_line(query_lower: str, memory_lower: str) -> bool:
    """Filter broad operational memories unless the user explicitly asks for them."""
    operational_markers = (
        "文件输出规范",
        "文件交付",
        "zip",
        "下载链接",
        "打包",
    )
    if any(marker in memory_lower for marker in operational_markers):
        user_asks_delivery = any(marker in query_lower for marker in operational_markers)
        if not user_asks_delivery:
            return True

    meta_markers = (
        "会话标题",
        "标题提炼",
        "第一条输入",
        "元指令前缀",
        "查询/记忆中查询",
    )
    if any(marker in memory_lower for marker in meta_markers):
        user_asks_title = any(marker in query_lower for marker in ("标题", "命名", "提炼"))
        if not user_asks_title:
            return True

    return False


def _score_memory_match(query_lower: str, query_terms: list[str], memory_lower: str) -> float:
    """Score prompt/context overlap with a high threshold for auto matching."""
    # Full containment is a strong signal but only when the query is
    # substantial.  Short queries like "下载" would otherwise score 10 against
    # any memory line that mentions the word.
    if query_lower and len(query_lower.strip()) >= 8 and query_lower in memory_lower:
        return 10.0

    matched = [term for term in query_terms if term in memory_lower]
    if not matched:
        return 0.0

    score = 0.0
    long_matches = 0
    for term in matched:
        if re.search(r"[\u4e00-\u9fff]", term):
            if len(term) >= 6:
                score += 2.5
                long_matches += 1
            elif len(term) >= 5:
                score += 1.25
            else:
                score += 0.35
        else:
            score += 1.5
            long_matches += 1

    # One short Chinese overlap such as "培训" or "公司" is too weak.
    if score < 2.0 and long_matches == 0 and len(matched) < 2:
        return 0.0

    # Precision factor: when matched terms are a clear minority of the
    # memory's distinct terms (i.e. the memory is mostly unrelated content
    # with a small overlap), apply a mild penalty.  Keeps tightly-focused
    # memories at full score while pushing sprawling lines down the ranking.
    memory_terms = _extract_match_terms(memory_lower)
    if memory_terms:
        matched_in_memory = len(set(matched) & set(memory_terms))
        if matched_in_memory > 0 and len(memory_terms) > 2 * matched_in_memory:
            score *= 0.6

    return score


def _ngrams(text: str, n: int = 4) -> set[str]:
    """Whitespace-stripped lowercase character n-grams (used for near-duplicate detection)."""
    cleaned = re.sub(r"\s+", "", text.lower())
    if len(cleaned) < n:
        return set()
    return {cleaned[i:i + n] for i in range(len(cleaned) - n + 1)}


def _is_self_citation(query: str, memory: str, *, threshold: float = 0.7) -> bool:
    """True if *memory* looks like a near-verbatim slice of *query*.

    Compares 4-gram coverage of memory inside query.  When the memory is
    short and most of its n-grams come from the query, the memory was almost
    certainly extracted from this same prompt (or a near-duplicate of it) and
    surfacing it as "referenced memory" would be self-citation.
    """
    q = _ngrams(query)
    m = _ngrams(memory)
    if not q or not m:
        return False
    return len(q & m) / len(m) >= threshold


_EXTRACTION_HIGH_SIGNAL_TERMS: tuple[str, ...] = (
    "remember",
    "remember that",
    "i prefer",
    "my preference",
    "default",
    "from now on",
    "next time",
    "root cause",
    "verified",
    "tests passed",
    "记住",
    "以后",
    "下次",
    "偏好",
    "我喜欢",
    "我不喜欢",
    "默认",
    "不要",
    "必须",
    "根因",
    "修复",
    "验证",
    "测试通过",
)


def _has_high_signal_for_loop_end(messages: list[Message]) -> bool:
    """Return whether loop-end extraction is worth spending on this turn.

    Stop events are frequent and often represent trivial one-off exchanges.
    Keep loop-end extraction for turns with explicit preference/profile signal,
    tool-backed work, or enough conversation substance to justify curation.
    """
    non_system = [m for m in messages if m.role != "system"]
    if not non_system:
        return False

    if any(m.role == "tool" for m in non_system):
        return True
    if any(m.role == "assistant" and m.tool_calls for m in non_system):
        return True

    transcript = MemoryManager._build_transcript(non_system, max_chars_per_msg=1000)
    lower = transcript.lower()
    if any(term in lower for term in _EXTRACTION_HIGH_SIGNAL_TERMS):
        return True

    user_turns = sum(1 for m in non_system if m.role == "user")
    return user_turns >= 2 and len(transcript) >= 1200


class MemoryExtractor:
    """Lifecycle-triggered memory extraction from conversation.

    Called at key points in the agent loop to extract cross-session
    knowledge before information is lost (e.g. before context compression).
    Writes explicit profile/preferences/local defaults to MEMORY.md and
    project/task history to topic-sharded context memory.
    """

    def __init__(
        self,
        llm,
        memory_manager: MemoryManager,
        *,
        session_id: str = "",
        turn_id: str = "",
        cooldown: int = 300,
        step_interval: int = 10,
    ):
        self._llm = llm
        self._mgr = memory_manager
        self._session_id = session_id
        self._turn_id = turn_id
        self._cooldown = cooldown
        self._step_interval = step_interval
        self._last_time: float = 0.0
        self._steps_since: int = 0

    def set_turn_id(self, turn_id: str) -> None:
        """Update the current host-owned turn id for subsequent extractions."""

        self._turn_id = _header_value(turn_id)

    async def maybe_extract(
        self,
        messages: list[Message],
        trigger: str,
        *,
        turn_id: str | None = None,
    ) -> bool:
        """Check whether extraction should run, then run if needed.

        Args:
            messages: Current conversation messages.
            trigger: ``"pre_summarize"`` | ``"step_interval"`` | ``"loop_end"``
            turn_id: Optional user-visible turn id snapshot from the caller.

        Returns:
            True if extraction was actually performed.
        """
        extraction_turn_id = _header_value(self._turn_id if turn_id is None else turn_id)
        now = monotonic()

        if trigger == "step_interval":
            self._steps_since += 1
            if self._steps_since < self._step_interval:
                return False
            if now - self._last_time < self._cooldown:
                return False
        elif trigger == "pre_summarize":
            if now - self._last_time < self._cooldown:
                return False
        elif trigger == "loop_end":
            if not _has_high_signal_for_loop_end(messages):
                return False

        try:
            await self._extract(messages, trigger, turn_id=extraction_turn_id)
            self._last_time = monotonic()
            self._steps_since = 0
            return True
        except Exception:
            logger.exception("Memory extraction failed (trigger=%s)", trigger)
            return False

    async def _extract(self, messages: list[Message], trigger: str, *, turn_id: str) -> None:
        """Use LLM to analyze messages and update CONTEXT.md."""
        from .schema import Message as Msg

        transcript = MemoryManager._build_transcript(messages, max_chars_per_msg=1500)
        if not transcript:
            return

        transcript = transcript[-6000:]  # Keep last ~6k chars

        core_memory, context_raw = await asyncio.gather(
            asyncio.to_thread(self._mgr.read_core),
            asyncio.to_thread(self._mgr.read_context),
        )
        core_memory = core_memory or "(empty)"

        # Only send last ~100 lines of Context for dedup reference (not the whole file).
        # Code-level dedup in append_context() handles Core overlap regardless.
        if context_raw:
            context_lines = context_raw.splitlines()
            context_memory = "\n".join(context_lines[-100:])
        else:
            context_memory = "(empty)"

        prompt = _EXTRACTION_USER_PROMPT.format(
            core_memory=core_memory,
            context_memory=context_memory,
            transcript=transcript,
        )

        memory_llm, _ = resolve_model_client(
            self._llm,
            task="从会话中分析提炼长期记忆",
            strategy="utility",
            task_tags=("summary", "analysis"),
            required_ability_level=1,
        )
        response = await memory_llm.generate(
            messages=[
                Msg(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
                Msg(role="user", content=prompt),
            ],
            session_id=self._session_id,
            turn_id=turn_id,
            call_kind="memory_extract",
        )

        await asyncio.to_thread(
            self._apply_updates,
            response.content,
            trigger=trigger,
            turn_id=turn_id,
        )

    def _apply_updates(self, llm_output: str, *, trigger: str, turn_id: str) -> None:
        """Parse LLM JSON output and apply to CONTEXT.md.

        Routed through ``apply_context_operations`` so entry metadata
        (hits, created, last_used) survives merges.
        """
        text = _strip_json_fences(llm_output)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Memory extraction returned invalid JSON: %s", text[:200])
            return

        core_additions = data.get("core_additions", [])
        if not isinstance(core_additions, list):
            core_additions = []
        additions: list = data.get("additions", [])
        merges: list[dict] = data.get("merges", [])

        if not core_additions and not additions and not merges:
            return

        core_lines: list[str] = []
        for item in core_additions:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("text", "")).strip()
            else:
                continue
            if text:
                core_lines.append(text)
        if core_lines:
            self._mgr.append_core_dedup("\n".join(core_lines))

        operations: list[dict] = []
        for merge in merges:
            old = str(merge.get("old", "")).strip()
            new = str(merge.get("new", "")).strip()
            if old and new:
                operations.append({"action": "replace", "old": old, "new": new})

        # Additions may be plain strings (legacy → "general") or objects
        # carrying a topic. Group by topic so each bucket lands in its own file.
        by_topic: dict[str, list[str]] = {}
        for item in additions:
            if isinstance(item, str):
                text, topic = item, "general"
            elif isinstance(item, dict):
                text = str(item.get("text", "")).strip()
                topic = _normalize_extraction_topic(item.get("topic"))
            else:
                continue
            if text and text.strip():
                by_topic.setdefault(topic, []).append(text)

        for topic, lines in by_topic.items():
            joined = "\n".join(lines)
            if joined:
                operations.append(
                    {
                        "action": "add",
                        "content": joined,
                        "topic": topic,
                        "source": "extractor",
                        "session_id": self._session_id,
                        "turn_id": turn_id,
                        "trigger": trigger,
                    }
                )

        if operations:
            self._mgr.apply_context_operations(operations)
