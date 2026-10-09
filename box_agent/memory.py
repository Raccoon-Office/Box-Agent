"""Memory system for cross-session recall, search, and auto-extraction.

Directory layout::

    ~/.box-agent/memory/
    ├── MEMORY.md          # Core memory (always injected into system prompt)
    ├── memory_summary.md  # Lightweight routing summary for deciding memory_search
    └── v2/experiences/    # Topic-sharded searchable context (retrieved on demand)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import uuid
from contextlib import aclosing, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator

from .llm.model_routing import resolve_model_client
from .user_paths import configured_box_agent_home, default_memory_dir, state_path

if TYPE_CHECKING:
    from .config import AgentConfig, ExternalMemoryConfig, MemoryHttpOperation
    from .events import AgentEvent
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
    # Correction-memory fields (R2+R3). Empty defaults keep legacy entries working.
    entry_type: str = ""  # "" | "correction" ("" treated as ordinary context)
    status: str = ""  # "" | "active" | "superseded" | "draft" | "deleted"
    error_fingerprint: str = ""
    subject_kind: str = ""  # skill|tool|path_pattern|env|workflow
    subject_name: str = ""
    subject_version: str = ""
    lesson: str = ""
    symptom: str = ""
    verification: str = ""  # Successful execution evidence; absent on legacy/unverified records.


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


_CORRECTIONS_TOPIC = "corrections"


def _is_inactive_correction(entry: ContextEntry) -> bool:
    """True when *entry* is a correction that should be hidden from default search."""
    if (entry.entry_type or "") != "correction":
        return False
    return (entry.status or "") != "active" or not entry.verification


def _correction_content(lesson: str, symptom: str = "", fingerprint: str = "", subject=None) -> str:
    """Build the searchable markdown body for a correction entry."""
    lesson = (lesson or "").strip()
    symptom = (symptom or "").strip()
    lines = [f"- lesson: {lesson}" if lesson else "- lesson:"]
    if symptom:
        lines.append(f"- symptom: {symptom}")
    if fingerprint:
        lines.append(f"- error: {fingerprint}")
    if subject is not None:
        lines.append(f"- scope: {subject.kind}:{subject.name}@{subject.version or 'unversioned'}")
    return "\n".join(lines)


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
    if e.entry_type:
        parts.append(f"entry_type={_header_value(e.entry_type)}")
    if e.status:
        parts.append(f"status={_header_value(e.status)}")
    if e.error_fingerprint:
        parts.append(f"error_fingerprint={_header_value(e.error_fingerprint)}")
    if e.subject_kind:
        parts.append(f"subject_kind={_header_value(e.subject_kind)}")
    if e.subject_name:
        parts.append(f"subject_name={_header_value(e.subject_name)}")
    if e.subject_version:
        parts.append(f"subject_version={_header_value(e.subject_version)}")
    if e.lesson:
        parts.append(f"lesson={_header_value(e.lesson)}")
    if e.symptom:
        parts.append(f"symptom={_header_value(e.symptom)}")
    if e.verification:
        parts.append(f"verification={_header_value(e.verification, max_len=512)}")
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
                entry_type=meta.get("entry_type") or "",
                status=meta.get("status") or "",
                error_fingerprint=meta.get("error_fingerprint") or "",
                subject_kind=meta.get("subject_kind") or "",
                subject_name=meta.get("subject_name") or "",
                subject_version=meta.get("subject_version") or "",
                lesson=meta.get("lesson") or "",
                symptom=meta.get("symptom") or "",
                verification=meta.get("verification") or "",
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
            if _is_inactive_correction(entry):
                continue
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
        memory_dir: str | None = None,
        *,
        dedup_jaccard_threshold: float = 0.85,
        **_kwargs,
    ):
        self.memory_dir = state_path(
            "memory", memory_dir if memory_dir is not None else default_memory_dir()
        )
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
        self._correction_curator = None

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
    def correction_curator(self):
        """Lazy shared CorrectionCurator for auto-curation of tool failures."""
        if self._correction_curator is None:
            from box_agent.correction import CorrectionCurator

            self._correction_curator = CorrectionCurator(self)
        return self._correction_curator

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
        return "\n".join(e.content for e in entries if e.entry_type != "correction").strip()

    @_serialized_context
    def write_context(self, content: str, *, topic: str = "general") -> None:
        """Overwrite context entries for *topic* with *content*.

        Destructive — drops metadata of existing entries **in that topic only**.
        Other topics are untouched. Used by callers that already hold the
        desired final text (tests, legacy paths). For non-destructive updates
        use ``append_context`` / ``apply_context_operations``.
        """
        slug = _slugify_topic(topic)
        if slug == _CORRECTIONS_TOPIC:
            raise ValueError("Use correction tools to manage the reserved corrections topic")
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
        if _slugify_topic(topic) == _CORRECTIONS_TOPIC:
            raise ValueError("Use correction tools to manage the reserved corrections topic")
        existing = self.read_context()
        filtered = self._dedupe_context_lines(content, existing_context=existing)

        if not filtered:
            return

        existing_entries = self._read_context_entries()
        threshold = self.dedup_jaccard_threshold
        entry_tokens = [tokens(e.content) if e.entry_type != "correction" else set() for e in existing_entries]
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


    # ── Correction memory (R2+R3) ─────────────────────────────

    @_serialized_context
    def write_correction(self, draft, *, status: str = "draft") -> ContextEntry:
        """Persist a correction under topic ``corrections``.

        New proposals are drafts by default. Direct active writes never overwrite
        an existing remedy; evidenced replacement goes through the activation path.
        """
        from box_agent.correction import CorrectionDraft, CorrectionSubject

        if not isinstance(draft, CorrectionDraft):
            raise TypeError("write_correction expects a CorrectionDraft")

        subject: CorrectionSubject = draft.subject
        fingerprint = _header_value(draft.error_fingerprint or "")
        lesson = (draft.lesson or "").strip()
        symptom = (draft.symptom or "").strip()
        if not fingerprint:
            raise ValueError("error_fingerprint is required")
        if not lesson:
            raise ValueError("lesson is required")
        if not subject.name:
            raise ValueError("subject.name is required")

        from box_agent.correction import CorrectionCurator
        CorrectionCurator().reject_if_forbidden(draft)
        status = (status or "draft").strip() or "draft"
        if status not in {"draft", "active", "superseded", "deleted"}:
            raise ValueError("invalid correction status")
        if status == "active" and not draft.verification:
            raise ValueError("verified execution evidence is required before activation")
        topic = _CORRECTIONS_TOPIC
        kind = _header_value(subject.kind)
        name = _header_value(subject.name)
        version = _header_value(subject.version)
        entries = self._topic_store.read_topic(topic)

        if status == "active":
            for existing in entries:
                if (existing.entry_type == "correction" and existing.status == "active"
                        and existing.error_fingerprint == fingerprint
                        and (existing.subject_kind, existing.subject_name, existing.subject_version or "")
                        == (kind, name, version)):
                    return existing

        now = _now_iso()
        entry = ContextEntry(
            id=_new_entry_id(),
            content=_correction_content(lesson, symptom, draft.error_fingerprint, subject),
            created=now,
            last_used=now,
            hits=0,
            source=draft.source or "explicit",
            confidence=1.0,
            topic=topic,
            entry_type="correction",
            status=status,
            error_fingerprint=fingerprint,
            subject_kind=kind,
            subject_name=name,
            subject_version=version,
            lesson=_header_value(lesson),
            symptom=_header_value(symptom),
            verification=draft.verification,
        )
        entries.append(entry)
        self._write_context_topic_entries(entries)
        return entry

    @_serialized_context
    def list_corrections(
        self,
        *,
        status: str | None = None,
        include_inactive: bool = False,
    ) -> list[ContextEntry]:
        """List correction entries from the ``corrections`` topic.

        When *status* is set, only that status is returned. Otherwise active
        corrections are returned unless *include_inactive* is True.
        """
        entries = [
            e
            for e in self._topic_store.read_topic(_CORRECTIONS_TOPIC)
            if (e.entry_type or "") == "correction"
        ]
        if status is not None:
            return [e for e in entries if (e.status or "") == status]
        if include_inactive:
            return entries
        return [e for e in entries if not _is_inactive_correction(e)]

    @_serialized_context
    def recall_corrections(self, subjects, *, query: str = "", limit: int = 3,
                           max_chars: int = 1800) -> list[dict[str, str]]:
        """Return verified remedies for exact, currently known subject versions."""
        keys = {(_header_value(s.kind), _header_value(s.name), _header_value(s.version))
                for s in subjects}
        candidates = []
        terms = _extract_match_terms(query.lower())
        for entry in self.list_corrections():
            if (entry.subject_kind, entry.subject_name, entry.subject_version or "") not in keys:
                continue
            score = _score_memory_match(query.lower(), terms, entry.content.lower()) if terms else 0
            candidates.append((score, entry.last_used, entry))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = []
        remaining = max(0, max_chars)
        for _, _, entry in candidates:
            # JSON makes record boundaries explicit; memory remains non-authoritative data.
            text = json.dumps({"id": entry.id, "subject": f"{entry.subject_kind}:{entry.subject_name}",
                               "version": entry.subject_version, "remedy": entry.content}, ensure_ascii=False)
            if len(text) > remaining:
                continue  # Never truncate a remedy into a different instruction.
            selected.append({"id": entry.id, "text": text})
            remaining -= len(text)
            if len(selected) >= min(3, max(0, limit)):
                break
        return selected if limit > 0 else []

    @_serialized_context
    def supersede_correction(self, entry_id: str, *, reason: str = "fixed") -> ContextEntry:
        """Mark a correction as superseded (hidden from default search)."""
        del reason  # reserved for future audit metadata
        return self._set_correction_status(entry_id, "superseded")

    @_serialized_context
    def delete_correction(self, entry_id: str) -> ContextEntry:
        """Soft-delete a correction (status=deleted; hidden from default search)."""
        return self._set_correction_status(entry_id, "deleted")

    def _set_correction_status(self, entry_id: str, status: str) -> ContextEntry:
        entries = self._topic_store.read_topic(_CORRECTIONS_TOPIC)
        for entry in entries:
            if entry.id == entry_id and (entry.entry_type or "") == "correction":
                entry.status = status
                entry.last_used = _now_iso()
                self._write_context_topic_entries(entries)
                return entry
        raise KeyError(f"correction not found: {entry_id}")

    @_serialized_context
    def supersede_by_subject_upgrade(self, subject, *, fingerprint: str | None = None) -> int:
        """Supersede active corrections for *subject* when its version upgrades.

        Matches on kind+name; if *fingerprint* is provided, only that fingerprint
        is superseded. Entries already at the new version are left alone.
        """
        from box_agent.correction import CorrectionSubject

        if not isinstance(subject, CorrectionSubject):
            raise TypeError("supersede_by_subject_upgrade expects a CorrectionSubject")

        entries = self._topic_store.read_topic(_CORRECTIONS_TOPIC)
        changed = 0
        now = _now_iso()
        kind = _header_value(subject.kind)
        name = _header_value(subject.name)
        new_version = _header_value(subject.version)
        fp = _header_value(fingerprint) if fingerprint is not None else None
        for entry in entries:
            if (entry.entry_type or "") != "correction":
                continue
            if (entry.status or "") != "active":
                continue
            if entry.subject_kind != kind or entry.subject_name != name:
                continue
            if fp is not None and entry.error_fingerprint != fp:
                continue
            # Leave entries already at the new version alone.
            if new_version and (entry.subject_version or "") == new_version:
                continue
            entry.status = "superseded"
            entry.last_used = now
            changed += 1
        if changed:
            self._write_context_topic_entries(entries)
        return changed

    @_serialized_context
    def remember_verified_correction(self, draft) -> ContextEntry:
        """Publish evidenced input atomically without exposing staging to users."""
        if not draft.verification:
            raise ValueError("verified execution evidence is required before activation")
        for existing in self.list_corrections(include_inactive=True):
            if (existing.verification == draft.verification
                    and existing.error_fingerprint == _header_value(draft.error_fingerprint)
                    and (existing.subject_kind, existing.subject_name, existing.subject_version or "")
                    == (_header_value(draft.subject.kind), _header_value(draft.subject.name),
                        _header_value(draft.subject.version))):
                if existing.status != "active":
                    raise ValueError("Revoked evidence cannot reactivate a remedy; validate it again")
                if existing.content == _correction_content(draft.lesson, draft.symptom, draft.error_fingerprint, draft.subject):
                    return existing
                raise ValueError("A different remedy needs fresh verification evidence")
        entry = self.write_correction(draft, status="draft")
        return self.confirm_correction_draft(entry.id)

    @_serialized_context
    def confirm_correction_draft(self, entry_id: str) -> ContextEntry:
        """Promote a draft correction to active (idempotent if already active)."""
        entries = self._topic_store.read_topic(_CORRECTIONS_TOPIC)
        draft_entry = None
        for entry in entries:
            if entry.id == entry_id and (entry.entry_type or "") == "correction":
                draft_entry = entry
                break
        if draft_entry is None:
            raise KeyError(f"correction not found: {entry_id}")
        if not draft_entry.verification:
            raise ValueError("verified execution evidence is required before activation")
        if draft_entry.status == "active":
            return draft_entry
        if draft_entry.status != "draft":
            raise ValueError(
                f"correction {entry_id} is not a draft (status={draft_entry.status})"
            )

        # Preserve the old record and evidence. Only validated activation can
        # supersede a currently active remedy for the same identity.
        for existing in entries:
            if (existing.id != draft_entry.id and existing.entry_type == "correction"
                    and existing.status == "active"
                    and existing.error_fingerprint == draft_entry.error_fingerprint
                    and (existing.subject_kind, existing.subject_name, existing.subject_version)
                    == (draft_entry.subject_kind, draft_entry.subject_name, draft_entry.subject_version)):
                existing.status = "superseded"

        draft_entry.status = "active"
        draft_entry.last_used = _now_iso()
        self._write_context_topic_entries(entries)
        return draft_entry

    @_serialized_context
    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        topic: str | None = None,
        include_inactive_corrections: bool = False,
    ) -> list[str]:
        """Keyword search across v2 experiences, then legacy fallback.

        Returns entry contents (deduped across multi-line entries) ranked by
        exact occurrence and query-term overlap, with historical ``hits`` as a
        tiebreak.  Capped at ``limit`` so noisy keywords cannot flood the model.

        Correction entries with status other than ``active`` are skipped unless
        ``include_inactive_corrections`` is True. Ordinary context is unchanged.

        Side effect: v2 matches increment ``hits`` and refresh ``last_used``.
        Legacy fallback matches are read-only and never become promotion
        evidence.
        """
        if not query:
            return []
        query_lower = query.lower().strip()
        entries, routed_topics, explicit_topic = self._entries_for_query(query, topic=topic)
        results = self._search_entries(
            query_lower,
            entries,
            limit,
            routed_topics,
            include_inactive_corrections=include_inactive_corrections,
        )

        if not results and routed_topics is not None and not explicit_topic:
            entries = self._read_context_entries()
            results = self._search_entries(
                query_lower,
                entries,
                limit,
                None,
                include_inactive_corrections=include_inactive_corrections,
            )

        if not results:
            results = self._search_legacy_context(
                query_lower,
                limit=limit,
                topic=topic,
                include_inactive_corrections=include_inactive_corrections,
            )

        return results

    def _search_legacy_context(
        self,
        query_lower: str,
        *,
        limit: int,
        topic: str | None = None,
        include_inactive_corrections: bool = False,
    ) -> list[str]:
        """Read-only search over pre-v2 context."""
        if topic:
            entries = self._legacy_topic_store.read_topic(topic)
            if _slugify_topic(topic) == "general" and self.legacy_context_file.exists():
                entries.extend(parse_context_file(self.legacy_context_file))
        else:
            entries = self._read_legacy_context_entries()
        return self._search_entries(
            query_lower,
            entries,
            limit,
            None,
            update_usage=False,
            include_inactive_corrections=include_inactive_corrections,
        )

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
        include_inactive_corrections: bool = False,
    ) -> list[str]:
        if not entries:
            return []

        query_terms = _extract_search_terms(query_lower)
        scored: list[tuple[float, int, int, str]] = []  # (score, occurrences, hits, content)
        changed = False
        now = _now_iso()

        for entry in entries:
            if not include_inactive_corrections and _is_inactive_correction(entry):
                continue
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
            if entry.entry_type == "correction":
                continue
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
        if configured_box_agent_home() is not None:
            return ""  # Do not read global memories or write an "imported" marker for this profile.
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
                indices = [i for i, e in enumerate(entries) if e.entry_type != "correction" and e.content.strip() == old]
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
                indices = [i for i, e in enumerate(entries) if e.entry_type != "correction" and e.content.strip() == content]
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
                if op_topic == _CORRECTIONS_TOPIC:
                    continue
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
            if e.entry_type == "correction":
                continue
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
11. Apply a portability test before saving every candidate: would this still be useful if the project name, industry, product, audience, URL, filename, and other concrete nouns were replaced? If not, either generalize it to the reusable method/constraint or return nothing.
12. Separate the reusable rule from the task payload. Remove one-off subject matter, campaign names, brands, links, filenames, example data, and deliverable-specific wording unless the user explicitly made them a durable project constraint. Prefer forms such as "对多格式内容交付，先统一事实与结构，再分别产出各格式并校验一致性" over "家园沟通类内容要先产出设计哲学文档和竖版 PNG".
13. Preserve a concrete domain only when it is itself durable context (for example, an ongoing project boundary or a stable user preference). Do not turn the current task's requested output or attached document instructions into a user preference.

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


def _memory_match_count(term: str, memory: str) -> int:
    """Match Chinese substrings, but require complete ASCII tokens at boundaries."""
    if not term or term not in memory:
        return 0
    # Match the ASCII token alphabet used by _extract_match_terms, including
    # compound names such as box-agent and cache_store. CJK remains unsegmented.
    prefix = r"(?<![a-z0-9_-])" if re.match(r"[a-z0-9_-]", term[0]) else ""
    suffix = r"(?![a-z0-9_-])" if re.match(r"[a-z0-9_-]", term[-1]) else ""
    if not prefix and not suffix:
        return memory.count(term)
    return sum(1 for _ in re.finditer(prefix + re.escape(term) + suffix, memory))


def _score_memory_search(query_lower: str, query_terms: list[str], memory_lower: str) -> tuple[float, int]:
    """Score explicit memory_search matches.

    Exact matches stay dominant for short deliberate queries. Longer
    natural-language queries can still recall entries through several stable
    overlapping terms, but a single weak term such as "ppt" is not enough.
    """
    normalized_query = query_lower.strip()
    occurrences = _memory_match_count(normalized_query, memory_lower)
    if occurrences:
        return float(occurrences * 100), occurrences

    if not query_terms:
        return 0.0, 0

    matched = _dedupe_contained_terms([
        term for term in query_terms if _memory_match_count(term, memory_lower)
    ])
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
    if len(query_lower.strip()) >= 8 and _memory_match_count(query_lower, memory_lower):
        return 10.0

    matched = [term for term in query_terms if _memory_match_count(term, memory_lower)]
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


# 外部后端仍属于现有 memory 能力；生命周期和传输不进入稳定内核。
_MEMORY_HTTP_REQUEST: ContextVar[bool] = ContextVar("memory_http_request", default=False)
MEMORY_CONTEXT_SLOT = "{MEMORY_CONTEXT}"
_MEMORY_START = "--- MEMORY START ---"
_MEMORY_END = "--- MEMORY END ---"
_MEMORY_BLOCK_RE = re.compile(
    r"--- (?:EXTERNAL )?MEMORY START ---.*?--- (?:EXTERNAL )?MEMORY END ---", re.DOTALL,
)


def place_memory_block(prompt: str, block: str, *, reserve: bool = False) -> str:
    """在模板指定位置更新记忆；空槽保留边界，旧模板仍可自动追加。"""
    replacement = block or (f"{_MEMORY_START}\n{_MEMORY_END}" if reserve else "")
    if MEMORY_CONTEXT_SLOT in prompt:
        # 显式占位优先，清除旧块和重复占位，避免记忆不断累积。
        prompt = _MEMORY_BLOCK_RE.sub("", prompt)
        before, _, after = prompt.partition(MEMORY_CONTEXT_SLOT)
        return before + replacement + after.replace(MEMORY_CONTEXT_SLOT, "")
    replaced = False

    def replace_block(match: re.Match) -> str:
        nonlocal replaced
        value = "" if replaced else replacement
        replaced = True
        return value

    updated = _MEMORY_BLOCK_RE.sub(replace_block, prompt)
    if replaced or not replacement:
        return updated
    return prompt.rstrip() + "\n\n" + replacement


class _MemoryHTTPLogFilter(logging.Filter):
    """通用 GET 可能携带记忆正文，保留本能力的脱敏日志而跳过请求 URL 日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _MEMORY_HTTP_REQUEST.get()


_MEMORY_HTTP_LOG_FILTER = _MemoryHTTPLogFilter()


def memory_identity(tenant_id: Any = None, user_id: Any = None) -> tuple[str, str]:
    """独立解析租户和用户，空值使用 default。"""
    return str(tenant_id or "").strip() or "default", str(user_id or "").strip() or "default"


def scoped_memory_dir(directory: str, tenant_id: str, user_id: str) -> str:
    """默认身份保留旧目录，其余身份使用不会被路径字符干扰的独立目录。"""
    if (tenant_id, user_id) == ("default", "default"):
        return directory
    digest = hashlib.sha256(json.dumps([tenant_id, user_id], ensure_ascii=False).encode()).hexdigest()
    return str(Path(directory).expanduser() / "identities" / digest)


def uses_local_memory(manager: Any) -> bool:
    """兼容现有自定义本地 manager，仅外部后端关闭本地提取和维护。"""
    return manager is not None and not isinstance(manager, ExternalMemoryBackend)


def create_memory_backend(settings: AgentConfig, *, manager_factory: Any = MemoryManager) -> Any:
    """统一构造后端，本地存储同时承载独立的纠错记忆能力。"""
    tenant_id, user_id = memory_identity(settings.memory_tenant_id, settings.memory_user_id)
    local = manager_factory(
        memory_dir=scoped_memory_dir(settings.memory_dir, tenant_id, user_id),
        dedup_jaccard_threshold=settings.memory_dedup_jaccard,
    )
    if settings.memory_backend_type == "local":
        return local
    backend_class = MemsenseMemoryBackend if settings.memory_backend_type == "memsense" else ExternalMemoryBackend
    return backend_class(settings.memory_backend_type, settings.memory_external, tenant_id, user_id, local)


class MemoryBackendError(RuntimeError):
    """可对用户展示的错误摘要，不携带服务地址、认证信息或响应正文。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MemoryEditError(ValueError):
    """核心记忆工具可直接返回给模型的参数和修改约束错误。"""


@dataclass(frozen=True)
class _MemsenseCoreEntry:
    """保留条目位置，便于编辑时原样保留未修改的存储内容。"""

    start: int
    end: int
    body: str
    time: str = ""
    priority: str = "1"


class ExternalMemoryBackend:
    """可配置的通用 HTTP 后端，mem0/memu 占位类型复用此实现。"""

    def __init__(self, backend_type: str, config: ExternalMemoryConfig, tenant_id: str,
                 user_id: str, corrections: MemoryManager) -> None:
        self.backend_type = backend_type
        self.config = config
        self.tenant_id, self.user_id = tenant_id, user_id
        self.corrections = corrections
        self.log_context: dict[str, str] = {}
        self.capabilities = frozenset(name for name in ("recall", "read", "search", "save")
                                      if getattr(config, name, None) is not None)
        if backend_type in {"mem0", "memu"}:
            logger.warning("memory backend=%s 使用 generic 映射，尚未提供原生协议适配", backend_type)

    @property
    def correction_curator(self) -> Any:
        """远端模式继续使用同身份的本地纠错机制。"""
        return self.corrections.correction_curator

    def recall_corrections(self, *args: Any, **kwargs: Any) -> Any:
        """纠错记录不发送到远端。"""
        return self.corrections.recall_corrections(*args, **kwargs)

    def auto_match_context(self, query: str, *, limit: int = 3) -> list[dict[str, str]]:
        """外部自动召回由轮次入口负责，避免复用本地经验匹配。"""
        return []

    def recall(self, **kwargs: Any) -> str:
        """会话装配时不访问网络；核心记忆在真实用户轮次开始时读取。"""
        return ""

    def read_core(self) -> str:
        """同步的本地引导提示入口不触发远端读取。"""
        return ""

    def _variables(self, **values: Any) -> dict[str, Any]:
        return {"tenant_id": self.tenant_id, "user_id": self.user_id,
                "agent_id": "box-agent", **self.log_context, **values}

    @staticmethod
    def _render(value: Any, variables: dict[str, Any]) -> Any:
        """仅替换完整的 ${变量} 值，保留对象类型且不执行模板代码。"""
        if isinstance(value, dict):
            return {key: ExternalMemoryBackend._render(item, variables) for key, item in value.items()}
        if isinstance(value, list):
            return [ExternalMemoryBackend._render(item, variables) for item in value]
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            key = value[2:-1]
            if key not in variables:
                raise ValueError("unknown_template_variable")
            return variables[key]
        return value

    @staticmethod
    def _select(value: Any, path: str) -> Any:
        """点分路径选择 JSON 字段，数字段可选择数组元素。"""
        for key in path.split(".") if path else ():
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value

    def log_failure(self, operation: str, reason: str, *, attempt: int = 0,
                    status: int | None = None, **context: Any) -> None:
        """记录定位字段，避免记录请求正文及可能包含凭证的异常字符串。"""
        fields = self._variables(**context)
        logger.warning(
            "memory backend=%s operation=%s tenant=%s user=%s session=%s turn=%s "
            "attempt=%s status=%s reason=%s",
            self.backend_type, operation, self.tenant_id, self.user_id,
            fields.get("session_id", ""), fields.get("turn_id", ""), attempt, status, reason,
        )

    async def _invoke(self, operation: str, spec: MemoryHttpOperation, *, request_payload: dict[str, Any] | None = None,
                      allow_retries: bool = True,
                      **variables: Any) -> Any:
        """有限超时重试；HTTP 和协议错误均由后端记录，调用者决定如何降级。"""
        import httpx

        attempts = self.config.max_retries + 1 if allow_retries else 1
        for attempt in range(1, attempts + 1):
            status = None
            retryable = False
            try:
                if not self.config.base_url.startswith(("http://", "https://")):
                    raise ValueError("missing_or_invalid_base_url")
                payload = (request_payload if request_payload is not None
                           else self._render(spec.request, self._variables(**variables)))
                logging.getLogger("httpx").addFilter(_MEMORY_HTTP_LOG_FILTER)
                token = _MEMORY_HTTP_REQUEST.set(True)
                try:
                    async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                        response = await asyncio.wait_for(client.request(
                            spec.method, self.config.base_url.rstrip("/") + spec.path,
                            headers=self.config.headers,
                            **({"params": payload} if spec.method == "GET" else {"json": payload}),
                        ), timeout=self.config.timeout_seconds)
                finally:
                    _MEMORY_HTTP_REQUEST.reset(token)
                status = response.status_code
                response.raise_for_status()
                if not response.content and spec.success_path is None and not spec.response_path:
                    return None
                data = response.json()
                if spec.success_path is not None and self._select(data, spec.success_path) != spec.success_value:
                    raise ValueError("service_rejected_request")
                return self._select(data, spec.response_path)
            except httpx.HTTPStatusError:
                retryable = status == 429 or (status is not None and status >= 500)
                reason = "http_error"
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                retryable = True
                reason = type(exc).__name__
            except Exception as exc:
                reason = type(exc).__name__
            self.log_failure(operation, reason, attempt=attempt, status=status, **variables)
            if not retryable or attempt == attempts:
                self.log_failure(operation, "abandoned", attempt=attempt, status=status, **variables)
                raise MemoryBackendError(f"{self.backend_type} {operation} 失败（{reason}）", status=status)
            await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 2))

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    async def load_context(self, *, query: str, **context: Any) -> str:
        """通用预加载是可选操作，不假定远端有核心文件。"""
        if "recall" not in self.capabilities:
            return ""
        return self._text(await self._invoke("recall", self.config.recall, query=query, **context))

    async def read_memory(self, path: str = "") -> str:
        """按 read 映射读取资源；资源名称完全由配置和服务约定。"""
        return self._text(await self._invoke("read", self.config.read, path=path))

    async def search_memory(self, query: str, limit: int = 6) -> list[Any]:
        """将查询交给后端，保留原始结果字段供模型使用。"""
        data = await self._invoke("search", self.config.search, query=query, limit=limit)
        return data if isinstance(data, list) else ([] if data is None else [data])

    async def save_turn(self, user: str, assistant: str, **context: Any) -> None:
        """按模板保存最终 QA；只有显式使用身份变量的服务才接收身份字段。"""
        await self._invoke("save", self.config.save, user=user, assistant=assistant,
                           messages=[{"role": "user", "content": user},
                                     {"role": "assistant", "content": assistant}], **context)


class MemsenseMemoryBackend(ExternalMemoryBackend):
    """MemSense 的核心文件、历史资源搜索及 QA 保存协议。"""

    _CORE_PATHS = ("memory://user.md", "memory://memory.md")
    core_tool_paths = ("user.md", "memory.md")
    # 只识别 mem 起始标签，保留正文里的 HTML、转义字符和换行。
    _ENTRY_OPEN_RE = re.compile(r'''<mem(?:\s+[\w:-]+\s*=\s*(?:"[^"]*"|'[^']*'))*\s*>''')
    _ENTRY_RE = re.compile(r"<mem>.*?</mem>", re.DOTALL)
    _STORAGE_ENTRY_RE = re.compile(r"<mem(?P<attrs>\s[^>]*|)>(?P<body>.*?)</mem>", re.DOTALL)
    _ATTRIBUTE_RE = re.compile(r'''(?P<name>[\w:-]+)\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)')''')
    _EDIT_WRITE_RULES = (
        "必须先用 memory_read 显式读取同一路径，并遵守该文件的内容更新规则。\n"
        "每条记忆必须使用完整的 <mem>...</mem>，标签不带属性，正文非空；"
        "正文允许 HTML 和原始 <、>，但不能嵌套 mem 标签。time、priority 由工具维护。\n"
        "优先使用 memory_edit：old_text 为空、new_text 为完整条目时追加；"
        "new_text 为空时删除；否则用一个或多个连续完整条目进行唯一匹配和替换。\n"
        "memory_write 用于创建或完整重写，content 必须包含全部条目，不能只提交新增条目。\n"
        "read 返回 exists=false 时只能用 memory_write 创建，不能 edit。"
        "受保护条目不能修改或删除，含受保护条目的文件不能完整重写。\n"
        "冲突或写入结果不确定时必须重新 memory_read，再决定是否修改；不要绕过保护或版本检查。"
    )
    _PROFILE_UPDATE_RULES = (
        "user.md 保存长期稳定、可复用的用户身份事实、偏好、边界和协作习惯。"
        "必须来自用户明确表达或多轮稳定重复的信息，不得推断用户身份或偏好。"
        "不保存一次性任务要求、临时状态、今日安排、外部资料或工具输出。"
        "用户只是使用某种语言或临时要求某种语言，不据此更新画像。"
        "敏感隐私细节仅在用户明确要求记录时考虑保存。"
        "用户要求忘记某项时，读取后用 edit 删除对应的非保护条目。"
    )
    _LONG_TERM_UPDATE_RULES = (
        "memory.md 保存用户明确确认的长期规则、明确要求长期保存的工作上下文，"
        "以及未来会持续影响协作的已确认结论。"
        "不保存一次性查询、临时任务内容、中间工具输出或模型推断。"
        "仅修改与本次已确认信息相关的条目，避免重复，不得把临时事实升级为长期规则。"
        "用户要求忘记某项时，读取后用 edit 删除对应的非保护条目。"
    )
    _CONTEXT_RULES = (
        "## 使用规则\n"
        "- 当前用户明确要求优先于历史记忆。\n"
        "- 用户画像用于调整称呼、沟通方式、解释深度和协作偏好。\n"
        "- 长期记忆用于参考已确认的长期规则和工作背景。\n"
        "- 日期记忆用于理解近期进展，不代表当前任务指令；日期按 UTC 分区。\n"
        "- 当前环境、文件状态、权限和实时事实需要重新核实。\n"
        "- 不主动使用“根据记忆”“根据用户画像”等措辞；用户询问记忆来源时如实说明。"
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.capabilities = frozenset({"recall", "read", "search", "save", "write", "edit"})
        # 会话装配会复制后端；记录仅用于显式先读校验，不成为新的持久化状态源。
        self._core_read_records: dict[str, dict[str, Any]] = {}
        self._core_file_locks = {path: asyncio.Lock() for path in self.core_tool_paths}

    async def _post(self, operation: str, endpoint: str, payload: dict[str, Any], **context: Any) -> Any:
        from .config import MemoryHttpOperation

        spec = MemoryHttpOperation(path=endpoint, success_path="ok", response_path="data")
        return await self._invoke(operation, spec, request_payload=payload,
                                  allow_retries=operation not in {"write", "edit"}, **context)

    @classmethod
    def _validate_core_tool_path(cls, path: str) -> None:
        """工具路径只接受两个短名称，不能借前缀或相对路径绕过范围。"""
        if not isinstance(path, str) or path not in cls.core_tool_paths:
            raise MemoryEditError("path 必填，只允许 user.md 或 memory.md。")

    @classmethod
    def _core_rule_fields(cls, path: str) -> dict[str, str]:
        """规则只进入显式 read 的返回字段，不进入正文或系统提示。"""
        field = "user_profile_update_rules" if path == "user.md" else "long_term_memory_update_rules"
        return {"memory_edit_write_rules": cls._EDIT_WRITE_RULES,
                field: cls._PROFILE_UPDATE_RULES if path == "user.md" else cls._LONG_TERM_UPDATE_RULES}

    def _core_file_record(self, data: Any, *, path: str, operation: str = "read") -> dict[str, Any]:
        """只有明确的存在状态和可核实的内容版本才能解锁后续修改。"""
        if (not isinstance(data, dict) or not isinstance(data.get("content"), str)
                or data.get("path") != "memory://" + path
                or not isinstance(data.get("exists"), bool)
                or data.get("revision") != "sha256:" + hashlib.sha256(data["content"].encode("utf-8")).hexdigest()
                or (not data["exists"] and data["content"])):
            self.log_failure(operation, "invalid_file_response")
            raise MemoryBackendError("MemSense 返回的文件状态或版本格式无效，请重新 memory_read。")
        return {"content": data["content"], "exists": data["exists"],
                "revision": data["revision"] if data["exists"] else None}

    async def read_core_memory(self, path: str) -> dict[str, Any]:
        """显式读取完整文件并保存原始版本；自动预载不调用此入口。"""
        self._validate_core_tool_path(path)
        async with self._core_file_locks[path]:
            self._core_read_records.pop(path, None)
            data = await self._post("read", "/v1/memory/files/read",
                                    {**self._identity_payload(), "path": "memory://" + path})
            record = self._core_file_record(data, path=path)
            self._core_read_records[path] = record
            return {"path": path, "exists": record["exists"],
                    "content": self._ENTRY_OPEN_RE.sub("<mem>", record["content"]),
                    **self._core_rule_fields(path)}

    @classmethod
    def _parse_core_entries(cls, text: str, *, storage: bool) -> list[_MemsenseCoreEntry]:
        """解析完整条目并验证属性；正文里的其他标签和转义文本保持原样。"""
        error = ("当前存储不是合法的核心记忆格式，无法修改。" if storage else
                 "记忆必须使用一个或多个完整的 <mem>...</mem> 条目，标签不能带属性，正文不能为空。")
        entries = []
        position = 0
        for match in cls._STORAGE_ENTRY_RE.finditer(text):
            body = match.group("body").strip()
            if (text[position:match.start()].strip() or not body
                    or re.search(r"</?\s*mem(?=$|[\s>/])", body, re.IGNORECASE)):
                raise MemoryEditError(error)
            attrs = match.group("attrs")
            values: dict[str, str] = {}
            if storage:
                offset = 0
                for attr in cls._ATTRIBUTE_RE.finditer(attrs):
                    name = attr.group("name")
                    if attrs[offset:attr.start()].strip() or name in values:
                        raise MemoryEditError(error)
                    values[name] = attr.group("double") if attr.group("double") is not None else attr.group("single")
                    offset = attr.end()
                if (attrs[offset:].strip() or set(values) != {"time", "priority"}
                        or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", values["time"])
                        or not re.fullmatch(r"-?\d+", values["priority"])):
                    raise MemoryEditError(error)
                try:
                    datetime.strptime(values["time"], "%Y-%m-%d %H:%M:%S")
                except ValueError as exc:
                    raise MemoryEditError(error) from exc
            elif attrs:
                raise MemoryEditError(error)
            entries.append(_MemsenseCoreEntry(match.start(), match.end(), body,
                                            values.get("time", ""), values.get("priority", "1")))
            position = match.end()
        if text[position:].strip() or (not storage and not entries):
            raise MemoryEditError(error)
        return entries

    @staticmethod
    def _render_core_entries(entries: list[_MemsenseCoreEntry], prior: list[_MemsenseCoreEntry]) -> str:
        """按正文继承未变条目的属性，新条目生成时间和普通优先级。"""
        from collections import defaultdict, deque

        by_body = defaultdict(deque)
        for entry in prior:
            by_body[entry.body].append(entry)
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        rendered = []
        for entry in entries:
            old = by_body[entry.body].popleft() if by_body[entry.body] else None
            rendered.append(f'<mem time="{old.time if old else timestamp}" '
                            f'priority="{old.priority if old else "1"}">{entry.body}</mem>')
        return "\n".join(rendered)

    @classmethod
    def _rewrite_core_content(cls, raw: str, content: str) -> str:
        """完整重写遵循 agent-v3 的保护约束，不能覆盖含保护条目的文件。"""
        prior = cls._parse_core_entries(raw, storage=True)
        if any(int(entry.priority) < 1 for entry in prior):
            raise MemoryEditError("文件含受保护条目，不能完整重写；请用 memory_edit 修改其他非保护条目。")
        return cls._render_core_entries(cls._parse_core_entries(content, storage=False), prior)

    @classmethod
    def _edit_core_content(cls, raw: str, old_text: str, new_text: str) -> str:
        """按唯一连续完整块编辑，未命中的内容连同原始空白一起保留。"""
        old_text, new_text = old_text.strip(), new_text.strip()
        if not old_text and not new_text:
            raise MemoryEditError("old_text 和 new_text 不能同时为空。")
        prior = cls._parse_core_entries(raw, storage=True)
        replacement = cls._parse_core_entries(new_text, storage=False) if new_text else []
        if not old_text:
            appended = cls._render_core_entries(replacement, [])
            separator = "" if not raw or raw.endswith("\n") else "\n"
            return raw + separator + appended
        targets = cls._parse_core_entries(old_text, storage=False)
        bodies = [entry.body for entry in targets]
        matches = [prior[index:index + len(targets)] for index in range(len(prior) - len(targets) + 1)
                   if [entry.body for entry in prior[index:index + len(targets)]] == bodies]
        if not matches:
            raise MemoryEditError("old_text 的连续完整条目未找到，请重新 memory_read。")
        if any(int(entry.priority) < 1 for match in matches for entry in match):
            raise MemoryEditError("目标条目受保护，不允许修改或删除。")
        if len(matches) != 1:
            raise MemoryEditError("old_text 命中多个位置，请提供更多连续完整条目以唯一匹配。")
        block = matches[0]
        if [entry.body for entry in replacement] == bodies:
            return raw
        return raw[:block[0].start] + cls._render_core_entries(replacement, block) + raw[block[-1].end:]

    async def write_core_memory(self, path: str, content: str) -> dict[str, Any]:
        """创建或完整重写已显式读取的核心文件。"""
        return await self._update_core_memory(path, "write", content=content)

    async def edit_core_memory(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        """编辑已显式读取且存在的核心文件。"""
        return await self._update_core_memory(path, "edit", old_text=old_text, new_text=new_text)

    async def _update_core_memory(self, path: str, operation: str, *, content: str = "",
                                  old_text: str = "", new_text: str = "") -> dict[str, Any]:
        """以读取版本提交；发送前撤销旧记录，不确定的结果必须重新读取。"""
        self._validate_core_tool_path(path)
        async with self._core_file_locks[path]:
            prior = self._core_read_records.get(path)
            if prior is None:
                raise MemoryEditError(f"必须先调用 memory_read 读取 {path}，再调用 memory_{operation}。")
            if operation == "edit" and not prior["exists"]:
                raise MemoryEditError("文件不存在，请用 memory_write 创建。")
            updated = (self._rewrite_core_content(prior["content"], content) if operation == "write" else
                       self._edit_core_content(prior["content"], old_text, new_text))
            payload = {**self._identity_payload(), "path": "memory://" + path, "content": updated}
            if prior["exists"]:
                payload["base_revision"] = prior["revision"]
            self._core_read_records.pop(path, None)
            try:
                data = await self._post(operation, "/v1/memory/files/write", payload)
            except MemoryBackendError as exc:
                if exc.status == 409:
                    raise MemoryBackendError("文件版本冲突，请重新 memory_read 后再修改。", status=409) from exc
                raise MemoryBackendError("修改结果无法确认，请重新 memory_read 后再决定是否重试。",
                                         status=exc.status) from exc
            record = self._core_file_record(data, path=path, operation=operation)
            if not record["exists"] or record["content"] != updated:
                self.log_failure(operation, "invalid_write_response")
                raise MemoryBackendError("修改响应与提交内容不一致，请重新 memory_read。")
            self._core_read_records[path] = record
            return {"path": path, "operation": operation, "exists": True,
                    "content": self._ENTRY_OPEN_RE.sub("<mem>", record["content"])}

    def _identity_payload(self) -> dict[str, str]:
        return {"tenant_id": self.tenant_id, "user_id": self.user_id}

    def _session_id(self, session_id: str) -> str:
        """会话文件路径要求 UUID；稳定映射宿主标识，不改变本地会话或日志。"""
        value = str(session_id)
        try:
            return str(uuid.UUID(value))
        except ValueError:
            identity = json.dumps(
                ["box-agent", "memsense", self.tenant_id, self.user_id, value],
                ensure_ascii=False,
            )
            return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))

    async def _read_file(self, path: str, **context: Any) -> str:
        data = await self._post("read", "/v1/memory/files/read",
                                {**self._identity_payload(), "path": path}, **context)
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            self.log_failure("read", "invalid_file_response", **context)
            raise MemoryBackendError("memsense read 返回的文件内容格式无效")
        content = data["content"]
        if path in self._CORE_PATHS:
            return self._ENTRY_OPEN_RE.sub("<mem>", content)
        return content

    def _date_paths(self, timestamp: int | None) -> list[str]:
        """与 MemSense 的 UTC 日分区一致，本轮内部续跑不会改变日期窗口。"""
        now = (datetime.now(timezone.utc) if timestamp is None
               else datetime.fromtimestamp(timestamp / 1000, timezone.utc))
        return [f"memory://date-memory/{(now.date() - timedelta(days=offset)).isoformat()}.md"
                for offset in reversed(range(self.config.date_memory_load_days))]

    @classmethod
    def _clip_context_file(cls, content: str, path: str, budget: int) -> str:
        """截断以完整条目为边界，并保留显式读取完整文件的提示。"""
        if len(content) <= budget:
            return content
        suffix = (f"\n[内容已截断，完整内容请用 memory_read 读取 {path.removeprefix('memory://')}]"
                  if path in cls._CORE_PATHS else "\n[日期摘要内容已截断]")
        if budget <= len(suffix):
            return ""
        cutoff = budget - len(suffix)
        for entry in cls._ENTRY_RE.finditer(content):
            if entry.start() < cutoff < entry.end():
                cutoff = entry.start()
                break
        return content[:cutoff].rstrip() + suffix

    def _format_context(self, files: list[tuple[str, str]]) -> str:
        """后端拥有分区语义；模板和通用后端不感知 MemSense 文件布局。"""
        sections: list[tuple[str, str, str]] = []
        for path, content in files:
            if path in self._CORE_PATHS:
                title = "## User Profile" if path == self._CORE_PATHS[0] else "## Long-term Memory"
            else:
                # 日期已经由分区标题给出，自动注入只取摘要正文。
                content = re.sub(r"\A---\r?\n.*?\r?\n---(?:\r?\n|$)", "", content.strip(),
                                 count=1, flags=re.DOTALL)
                title = f"### {path.rsplit('/', 1)[-1][:-3]}"
            if content.strip():
                sections.append((title, path, content.strip()))
        if not sections:
            return ""
        prefix = "# Memory Context\n\n以下是历史记忆，仅作为背景信息使用。"
        date_heading = "## Recent Date Memory"
        has_dates = any(path not in self._CORE_PATHS for _, path, _ in sections)
        overhead = (len(prefix) + len(self._CONTEXT_RULES) + 4
                    + sum(len(title) + 3 for title, _, _ in sections)
                    + (len(date_heading) + 2 if has_dates else 0))
        budget = max(0, (self.config.context_max_chars - overhead) // len(sections))
        parts = [prefix]
        date_started = False
        for title, path, content in sections:
            clipped = self._clip_context_file(content, path, budget)
            if not clipped:
                continue
            if path not in self._CORE_PATHS and not date_started:
                parts.append(date_heading)
                date_started = True
            parts.append(f"{title}\n{clipped}")
        if len(parts) == 1:
            return ""
        parts.append(self._CONTEXT_RULES)
        return "\n\n".join(parts)

    async def load_context(self, *, query: str, **context: Any) -> str:
        paths = [*self._CORE_PATHS, *self._date_paths(context.get("timestamp"))]
        # 每个文件独立降级，单个文件故障不丢弃其他成功结果。
        values = await asyncio.gather(*(self._read_file(path, **context) for path in paths), return_exceptions=True)
        return self._format_context([(path, value) for path, value in zip(paths, values)
                                     if isinstance(value, str)])

    async def read_memory(self, path: str = "") -> str:
        if not path:
            paths = self._CORE_PATHS
            values = await asyncio.gather(*(self._read_file(item) for item in paths))
            return "\n\n".join(f"[{item}]\n{value}" for item, value in zip(paths, values) if value)
        if not path.startswith("memory://"):
            raise MemoryBackendError("MemSense 读取路径须使用 memory://")
        return await self._read_file(path)

    async def search_memory(self, query: str, limit: int = 6) -> list[Any]:
        data = await self._post("search", "/v1/memory/resource_search", {
            **self._identity_payload(), "query": query, "resource_types": ["date_session_title", "qa_chunk"],
            "filters": {}, "top_k": limit, "mode": "hybrid", "scope": "user",
        })
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            self.log_failure("search", "invalid_search_response")
            raise MemoryBackendError("memsense search 返回的结果格式无效")
        return data["results"]

    async def save_turn(self, user: str, assistant: str, **context: Any) -> None:
        # 时间戳由轮次固定，传输重试复用同一份请求；不伪造服务端幂等能力。
        data = await self._post("save", "/v1/memory/save", {
            **self._identity_payload(), "scope": "user", "session_id": self._session_id(context["session_id"]),
            "agent_id": "box-agent", "source": "box_agent_auto", "type_hint": "qa_chunk",
            "timestamp": context["timestamp"], "content": {"user": user, "assistant": assistant},
        }, **context)
        if not isinstance(data, dict):
            self.log_failure("save", "invalid_save_response", **context)
            raise MemoryBackendError("memsense save 返回的确认格式无效")


@dataclass
class _MemoryTurn:
    user: str
    session_id: str
    turn_id: str
    timestamp: int
    handle: Any = None


class MemoryRuntime:
    """现有 memory 插件的会话资源，拥有上下文刷新和后台保存任务。"""

    def __init__(self, backend: ExternalMemoryBackend) -> None:
        self.backend = backend
        self.active: _MemoryTurn | None = None
        self.tasks: set[asyncio.Task] = set()
        self._save_slots = asyncio.Semaphore(2)
        self._closed = False

    async def refresh(self, session: Any, turn: _MemoryTurn) -> None:
        self.backend.log_context = {"session_id": turn.session_id, "turn_id": turn.turn_id}
        try:
            content = await self.backend.load_context(query=turn.user, timestamp=turn.timestamp,
                                                      **self.backend.log_context)
        except Exception as exc:
            self.backend.log_failure("recall", type(exc).__name__)
            content = ""
        content = content[:self.backend.config.context_max_chars]
        # 外部内容不能伪装成槽位或块边界，避免下一轮替换时误删其他提示。
        for marker in (_MEMORY_START, _MEMORY_END, MEMORY_CONTEXT_SLOT,
                       "--- EXTERNAL MEMORY START ---", "--- EXTERNAL MEMORY END ---"):
            content = content.replace(marker, "[记忆边界]")
        block = (f"{_MEMORY_START}\n"
                 "以下是历史记忆数据；当前用户要求优先，实时事实需要重新验证。\n"
                 + content + f"\n{_MEMORY_END}") if content.strip() else ""
        # 只替换本能力拥有的块；失败时清除旧值，避免身份或内容过期后仍被使用。
        session.agent.set_system_prompt(place_memory_block(session.agent.system_prompt, block, reserve=True))
        session.memory_block = block or None

    def schedule(self, turn: _MemoryTurn) -> None:
        if not turn.user.strip() or turn.handle is None or "save" not in self.backend.capabilities:
            return
        if self._closed or len(self.tasks) >= self.backend.config.save_queue_limit:
            self.backend.log_failure("save", "abandoned_queue_full_or_closed",
                                     session_id=turn.session_id, turn_id=turn.turn_id)
            return
        task = asyncio.create_task(self._save(turn), name="memory-save")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _save(self, turn: _MemoryTurn) -> None:
        context = {"session_id": turn.session_id, "turn_id": turn.turn_id, "timestamp": turn.timestamp}
        try:
            # 结算可能在 DoneEvent 后因清理或传输失败而变更，必须等待最终结果。
            result = await turn.handle.result(consume_events=False)
            if result.status.value != "completed" or not result.final_content.strip():
                return
            async with self._save_slots:
                await self.backend.save_turn(turn.user, result.final_content, **context)
            logger.info("memory backend=%s operation=save session=%s turn=%s saved=true",
                        self.backend.backend_type, turn.session_id, turn.turn_id)
        except asyncio.CancelledError:
            self.backend.log_failure("save", "abandoned_shutdown", **context)
            raise
        except Exception as exc:
            self.backend.log_failure("save", type(exc).__name__, **context)

    async def aclose(self) -> None:
        """关闭时有限等待已接收的任务，保证失败和放弃都有日志。"""
        self._closed = True
        pending = tuple(self.tasks)
        if not pending:
            return
        _, unfinished = await asyncio.wait(pending, timeout=self.backend.config.shutdown_timeout_seconds)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)


@asynccontextmanager
async def memory_user_turn(session: Any, *, user_text: str, session_id: str,
                           turn_id: str) -> AsyncIterator[_MemoryTurn | None]:
    """宿主声明真实用户轮次，内部续跑复用同一边界。"""
    runtime = getattr(session, "memory_runtime", None)
    if runtime is None:
        yield None
        return
    if runtime.active is not None:
        yield runtime.active
        return
    turn = _MemoryTurn(user_text, session_id, turn_id, int(datetime.now(timezone.utc).timestamp() * 1000))
    runtime.active = turn
    try:
        await runtime.refresh(session, turn)
        yield turn
    except BaseException:
        raise
    else:
        runtime.schedule(turn)
    finally:
        runtime.active = None


async def memory_run_events(session: Any, request: Any, options: Any) -> AsyncIterator[AgentEvent]:
    """共享服务运行包装器，普通运行和 CLI/ACP 外层轮次使用同一策略。"""
    user_text = request.user_message if request.user_message is not None else (getattr(options, "current_turn_text", None) or "")
    async with memory_user_turn(session, user_text=user_text, session_id=request.session_id,
                                turn_id=getattr(options, "turn_id", "") or request.run_id) as turn:
        if turn is not None:
            turn.handle = session._run_handle
        async with aclosing(session.run_events(options=options)) as events:
            async for event in events:
                yield event
