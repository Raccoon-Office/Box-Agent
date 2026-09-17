"""Correction memory curation (R2+R3).

Windowed auto-curation for durable failure lessons. One-shot environment fixes
are never stored. Preference and secret content is rejected.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from box_agent.memory import MemoryManager

SubjectKind = Literal["skill", "tool", "path_pattern", "env", "workflow"]
CorrectionSource = Literal["auto", "explicit", "tool", "extractor", "user"]

_SUBJECT_KINDS = frozenset({"skill", "tool", "path_pattern", "env", "workflow"})

# Paths / timestamps / hex ids that make fingerprints noisy across runs.
_ABS_PATH_RE = re.compile(
    r"(?:[A-Za-z]:)?(?:[/\\][^\s:'\"|,;]+)+",
)
_ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_HEX_ID_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_WS_RE = re.compile(r"\s+")

_SECRET_RE = re.compile(
    r"(?i)\b("
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"password|passwd|secret|bearer|private[_-]?key|client[_-]?secret|"
    r"sk-[a-z0-9]{10,}|ghp_[a-z0-9]{20,}|xox[baprs]-"
    r")\b"
)
_PREFERENCE_RE = re.compile(
    r"(?i)\b("
    r"prefers?|preference|likes?|favorite|favourite|always\s+use|"
    r"i\s+want|用户偏好|偏好|喜欢"
    r")\b"
)


class CorrectionReject(Exception):
    """Raised when a correction draft must not be persisted."""


@dataclass(frozen=True)
class CorrectionSubject:
    """Stable identity of the thing a correction applies to."""

    kind: SubjectKind
    name: str
    version: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _SUBJECT_KINDS:
            raise ValueError(f"invalid subject.kind: {self.kind!r}")
        if not (self.name or "").strip():
            raise ValueError("subject.name is required")

    @property
    def version_or_hash(self) -> str:
        """ARCH_SPEC alias for ``version``."""
        return self.version

    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.name.strip(), self.version or "")


@dataclass(frozen=True)
class CorrectionDraft:
    """Candidate correction ready for MemoryManager.write_correction."""

    lesson: str
    symptom: str
    subject: CorrectionSubject
    error_fingerprint: str
    source: CorrectionSource = "explicit"


@dataclass(frozen=True)
class CorrectionNotice:
    """Structured notice emitted only after an auto-curated write succeeds."""

    lesson: str
    subject_kind: SubjectKind
    subject_name: str


def normalize_error_fingerprint(raw: str) -> str:
    """Normalize noisy error strings into a stable fingerprint.

    Strips absolute paths, timestamps, and long hex/uuid ids; collapses
    whitespace; lowercases. Does not apply a TTL.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    text = _UUID_RE.sub("<id>", text)
    text = _ISO_TS_RE.sub("<ts>", text)
    text = _ABS_PATH_RE.sub("<path>", text)
    text = _HEX_ID_RE.sub("<id>", text)
    text = _WS_RE.sub(" ", text).strip().lower()
    return text


def classify_forbidden_content(text: str) -> str | None:
    """Return ``secret`` / ``preference`` when *text* must not be stored."""
    sample = text or ""
    if _SECRET_RE.search(sample):
        return "secret"
    if _PREFERENCE_RE.search(sample):
        return "preference"
    return None


@dataclass
class CorrectionCurator:
    """Windowed auto-curation for repeated failures.

    Only emits a draft when the same fingerprint + subject fails at least
    ``repeat_count`` times inside ``repeat_window``. One-shot env fixes never
    write. Counters live in memory (not a correction TTL).
    """

    memory_manager: "MemoryManager | None" = None
    repeat_window: timedelta = field(default_factory=lambda: timedelta(hours=6))
    repeat_count: int = 2
    _observations: dict[tuple[str, tuple[str, str, str]], list[datetime]] = field(
        default_factory=lambda: defaultdict(list),
        init=False,
        repr=False,
    )

    def observe_failure(
        self,
        *,
        subject: CorrectionSubject,
        raw_error: str,
        one_shot_env_fix: bool = False,
        lesson: str = "",
        symptom: str = "",
    ) -> CorrectionDraft | None:
        """Record a failure; return a draft only when the repeat threshold is met.

        ``one_shot_env_fix=True`` (e.g. missing font downloaded successfully
        once) returns None and does not count toward the threshold.
        """
        if one_shot_env_fix:
            return None

        fingerprint = normalize_error_fingerprint(raw_error)
        if not fingerprint:
            return None

        key = (fingerprint, subject.key())
        now = datetime.now(timezone.utc)
        window_start = now - self.repeat_window
        recent = [ts for ts in self._observations[key] if ts >= window_start]
        recent.append(now)
        self._observations[key] = recent

        if len(recent) < self.repeat_count:
            return None

        lesson_text = (lesson or "").strip() or (
            f"When seeing '{fingerprint[:120]}' under {subject.kind}:{subject.name}, "
            f"apply the known fix instead of retrying blindly."
        )
        symptom_text = (symptom or "").strip() or fingerprint[:160]
        draft = CorrectionDraft(
            lesson=lesson_text,
            symptom=symptom_text,
            subject=subject,
            error_fingerprint=fingerprint,
            source="auto",
        )
        self.reject_if_forbidden(draft)
        return draft

    def reject_if_forbidden(
        self,
        draft: CorrectionDraft,
        *,
        content_class: str = "",
    ) -> None:
        """Raise ``CorrectionReject`` for preference / secret / empty content."""
        cls = (content_class or "").strip().lower()
        if cls in {"preference", "secret", "one_shot", "one_shot_env_fix"}:
            raise CorrectionReject(f"forbidden content_class={cls}")

        blob = "\n".join(
            [
                draft.lesson or "",
                draft.symptom or "",
                draft.error_fingerprint or "",
                draft.subject.name or "",
            ]
        )
        detected = classify_forbidden_content(blob)
        if detected:
            raise CorrectionReject(f"forbidden {detected} content")

        if not (draft.lesson or "").strip():
            raise CorrectionReject("lesson is required")
        if not (draft.error_fingerprint or "").strip():
            raise CorrectionReject("error_fingerprint is required")


_ONE_SHOT_ENV_FIX_RE = re.compile(
    r"(?is)\b("
    r"(?:font|typeface).{0,80}(?:download(?:ed)?|install(?:ed)?).{0,40}(?:success|ok|complete|done)|"
    r"(?:download(?:ed)?|install(?:ed)?).{0,80}(?:font|typeface).{0,40}(?:success|ok|complete|done)|"
    r"one[_ -]?shot[_ -]?env[_ -]?fix|"
    r"missing\s+font.{0,60}(?:download(?:ed)?|install(?:ed)?)"
    r")\b"
)


def looks_like_one_shot_env_fix(*parts: str) -> bool:
    """Heuristic for one-time environment repairs that must never be stored."""
    blob = "\n".join(p for p in parts if p)
    if not blob.strip():
        return False
    return bool(_ONE_SHOT_ENV_FIX_RE.search(blob))


def notify_tool_failure_for_correction(
    memory_manager: "MemoryManager | None",
    *,
    tool_name: str,
    raw_error: str,
    content: str = "",
) -> CorrectionNotice | None:
    """Observe a failed tool call and return a notice after a successful write.

    Never raises into the tool pipeline. Auto-curation writes ``active`` when the
    repeat threshold is met (explicit remembers still go draft→confirm via tools).
    """
    if memory_manager is None:
        return None
    name = (tool_name or "").strip()
    if not name:
        return None
    try:
        curator = getattr(memory_manager, "correction_curator", None)
        if curator is None:
            curator = CorrectionCurator(memory_manager)
        one_shot = looks_like_one_shot_env_fix(raw_error, content)
        subject = CorrectionSubject(kind="tool", name=name)
        draft = curator.observe_failure(
            subject=subject,
            raw_error=raw_error or content or "",
            one_shot_env_fix=one_shot,
        )
        if draft is None:
            return None
        memory_manager.write_correction(draft, status="active")
        return CorrectionNotice(
            lesson=draft.lesson,
            subject_kind=draft.subject.kind,
            subject_name=draft.subject.name,
        )
    except Exception:
        return None
