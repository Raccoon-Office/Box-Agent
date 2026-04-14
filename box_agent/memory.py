"""Memory system for cross-session recall and auto-extraction.

Directory layout::

    ~/.box-agent/memory/
    └── MEMORY.md          # Persistent memory (Manual + Auto sections)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schema import Message
    from .tools.permissions import PermissionEngine

logger = logging.getLogger(__name__)


class MemoryManager:
    """Markdown-file-based memory with manual notes and session summaries.

    MEMORY.md is organized into three sections:

    - ``## Manual Memory`` — written by the LLM via ``memory_write`` tool
      when the user explicitly asks to remember something.
    - ``## OpenClaw Memory`` — imported read-only from ``~/.openclaw/``.
    - ``## Auto Memory`` — written only by ``MemoryExtractor`` at lifecycle
      trigger points (pre-summarize, step interval, session end).

    ``recall()`` returns all three sections for system-prompt injection.
    """

    _SECTIONS = ("Manual Memory", "Auto Memory")

    def __init__(self, memory_dir: str = "~/.box-agent/memory", recall_days: int = 3):  # noqa: ARG002 — recall_days kept for backward compat
        self.memory_dir = Path(memory_dir).expanduser()
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    # ── MEMORY.md file ──────────────────────────────────────────

    @property
    def memory_file(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    def read_all(self) -> str:
        """Read full MEMORY.md content (all sections). Returns empty string if missing."""
        if not self.memory_file.exists():
            return ""
        return self.memory_file.read_text(encoding="utf-8").strip()

    def write_all(self, content: str) -> None:
        """Overwrite entire MEMORY.md with *content*."""
        self.memory_file.write_text(content.strip() + "\n", encoding="utf-8")

    # Legacy aliases — used by existing callers and tests
    read_manual_memory = read_all
    write_manual_memory = write_all

    # ── Section-based read/write ────────────────────────────────

    def _parse_sections(self) -> dict[str, str]:
        """Parse MEMORY.md into ``{section_heading: body}`` dict.

        Sections start with ``## Heading``.  Content before the first
        ``##`` heading is stored under the key ``"_preamble"``.
        """
        raw = self.read_all()
        if not raw:
            return {}

        sections: dict[str, str] = {}
        current_key = "_preamble"
        lines: list[str] = []

        for line in raw.splitlines():
            if line.startswith("## "):
                # Flush previous section
                sections[current_key] = "\n".join(lines).strip()
                current_key = line[3:].strip()
                lines = []
            else:
                lines.append(line)

        sections[current_key] = "\n".join(lines).strip()
        # Remove empty preamble
        if not sections.get("_preamble"):
            sections.pop("_preamble", None)
        return sections

    def _write_sections(self, sections: dict[str, str]) -> None:
        """Serialize sections dict back to MEMORY.md, preserving order."""
        parts: list[str] = []

        # Preamble first (if any)
        preamble = sections.get("_preamble", "").strip()
        if preamble:
            parts.append(preamble)

        # Known sections in canonical order, then any extras
        ordered = list(self._SECTIONS)
        for key in sections:
            if key != "_preamble" and key not in ordered:
                ordered.append(key)

        for heading in ordered:
            body = sections.get(heading, "").strip()
            if body:
                parts.append(f"## {heading}\n{body}")

        self.write_all("\n\n".join(parts))

    def read_section(self, section: str) -> str:
        """Read content under a ``## {section}`` heading. Returns empty string if absent."""
        return self._parse_sections().get(section, "")

    def write_section(self, section: str, content: str) -> None:
        """Overwrite *only* the given section, preserving all other sections."""
        sections = self._parse_sections()
        sections[section] = content.strip()
        self._write_sections(sections)

    def append_to_section(self, section: str, content: str) -> None:
        """Append content to an existing section (or create it)."""
        existing = self.read_section(section)
        if existing:
            new_content = f"{existing}\n{content.strip()}"
        else:
            new_content = content.strip()
        self.write_section(section, new_content)

    # ── Recall ─────────────────────────────────────────────────

    def recall(self, query: str = "", permission_engine: PermissionEngine | None = None) -> str:  # noqa: ARG002 — query reserved for future semantic search
        """Build a memory block for system-prompt injection.

        Returns an empty string when there is nothing to recall.
        All sections (Manual, Auto, OpenClaw) are included.
        """
        manual = self.read_section("Manual Memory")
        auto = self.read_section("Auto Memory")

        # Legacy fallback: if no sections exist but file has content,
        # treat the entire file as manual memory.
        if not manual and not auto:
            raw = self.read_all()
            if raw and "## " not in raw:
                manual = raw

        # OpenClaw memory import (only when permission engine is present and allows it)
        openclaw_block = ""
        if permission_engine is not None:
            from .tools.permissions import MEMORY_OPENCLAW_IMPORT
            decision = permission_engine.check(MEMORY_OPENCLAW_IMPORT, {"source": "openclaw"})
            if decision.allowed:
                openclaw_block = self._read_openclaw_memory()

        return self.build_memory_block(manual, openclaw_block, auto)

    def _read_openclaw_memory(self) -> str:
        """Read MEMORY.md files from ~/.openclaw/**/MEMORY.md.

        Returns combined content or empty string if none found.
        """
        openclaw_dir = Path.home() / ".openclaw"
        if not openclaw_dir.is_dir():
            return ""

        parts: list[str] = []
        for memory_file in sorted(openclaw_dir.rglob("MEMORY.md")):
            try:
                content = memory_file.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)
            except Exception:
                logger.debug("Failed to read OpenClaw memory: %s", memory_file)
        return "\n\n".join(parts)

    @staticmethod
    def build_memory_block(manual: str, openclaw: str = "", auto: str = "") -> str:
        """Format all memory sections into a prompt block for system-prompt injection."""
        if not manual and not openclaw and not auto:
            return ""

        parts: list[str] = ["--- MEMORY START ---"]

        if manual:
            parts.append("")
            parts.append("[Manual Memory]")
            parts.append(manual)

        if auto:
            parts.append("")
            parts.append("[Auto Memory]")
            parts.append(auto)

        if openclaw:
            parts.append("")
            parts.append("[OpenClaw Memory]")
            parts.append(openclaw)

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


# ── Auto Memory Extraction ────────────────────────────────────────

_EXTRACTION_SYSTEM_PROMPT = "You are a memory extraction assistant. You analyze conversations to identify information worth remembering across sessions."

_EXTRACTION_USER_PROMPT = """\
Analyze the recent conversation below. Extract information worth remembering across sessions.

Categories to look for:
- User info: name, role, team, expertise, background
- Preferences: language, communication style, tools, workflows
- Project context: goals, constraints, key decisions, deadlines
- Behavioral feedback: corrections the user made, approaches that worked

Existing memory (all sections — do NOT duplicate what's already here):
{existing_memory}

Recent conversation:
{transcript}

Rules:
1. Only extract cross-session-valuable information. Ignore ephemeral task details.
2. If new info updates or refines something in Auto Memory, output a merge.
3. If info is genuinely new, output an addition.
4. Do NOT touch Manual Memory or OpenClaw Memory content.
5. Do NOT record code details, git operations, file paths, or anything derivable from the codebase.
6. If there is nothing worth remembering, return empty arrays.

Output ONLY valid JSON (no markdown fences):
{{"additions": ["- bullet point 1", "- bullet point 2"], "merges": [{{"old": "exact old line", "new": "replacement line"}}]}}"""


class MemoryExtractor:
    """Lifecycle-triggered memory extraction from conversation.

    Called at key points in the agent loop to extract cross-session
    knowledge before information is lost (e.g. before context compression).
    Only modifies the ``## Auto Memory`` section of MEMORY.md.
    """

    def __init__(
        self,
        llm,
        memory_manager: MemoryManager,
        *,
        cooldown: int = 300,
        step_interval: int = 10,
    ):
        self._llm = llm
        self._mgr = memory_manager
        self._cooldown = cooldown
        self._step_interval = step_interval
        self._last_time: float = 0.0
        self._steps_since: int = 0

    async def maybe_extract(self, messages: list[Message], trigger: str) -> bool:
        """Check whether extraction should run, then run if needed.

        Args:
            messages: Current conversation messages.
            trigger: ``"pre_summarize"`` | ``"step_interval"`` | ``"session_end"``

        Returns:
            True if extraction was actually performed.
        """
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
        # "session_end" always runs — no cooldown check

        try:
            await self._extract(messages)
            self._last_time = monotonic()
            self._steps_since = 0
            return True
        except Exception:
            logger.exception("Memory extraction failed (trigger=%s)", trigger)
            return False

    async def _extract(self, messages: list[Message]) -> None:
        """Use LLM to analyze messages and update Auto Memory section."""
        from .schema import Message as Msg

        transcript = MemoryManager._build_transcript(messages, max_chars_per_msg=1500)
        if not transcript:
            return

        transcript = transcript[-6000:]  # Keep last ~6k chars

        existing_memory = self._mgr.read_all() or "(empty)"

        prompt = _EXTRACTION_USER_PROMPT.format(
            existing_memory=existing_memory,
            transcript=transcript,
        )

        response = await self._llm.generate(
            messages=[
                Msg(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
                Msg(role="user", content=prompt),
            ]
        )

        self._apply_updates(response.content)

    def _apply_updates(self, llm_output: str) -> None:
        """Parse LLM JSON output and apply to Auto Memory section."""
        # Strip markdown fences if present
        text = llm_output.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = "\n".join(text.split("\n")[:-1])
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Memory extraction returned invalid JSON: %s", text[:200])
            return

        additions: list[str] = data.get("additions", [])
        merges: list[dict] = data.get("merges", [])

        if not additions and not merges:
            return

        auto_memory = self._mgr.read_section("Auto Memory")

        # Apply merges (line-level exact match only)
        if merges:
            lines = auto_memory.splitlines()
            for merge in merges:
                old = merge.get("old", "").strip()
                new = merge.get("new", "").strip()
                if not old or not new:
                    continue
                # Find exact line matches; require exactly one to avoid ambiguity
                indices = [i for i, line in enumerate(lines) if line.strip() == old]
                if len(indices) == 1:
                    lines[indices[0]] = new
                elif len(indices) > 1:
                    logger.warning("Ambiguous memory merge skipped (%d matches): %s", len(indices), old[:80])
                else:
                    logger.debug("Memory merge target not found: %s", old[:80])
            auto_memory = "\n".join(lines)

        # Apply additions
        if additions:
            addition_text = "\n".join(additions)
            if auto_memory:
                auto_memory = f"{auto_memory}\n{addition_text}"
            else:
                auto_memory = addition_text

        self._mgr.write_section("Auto Memory", auto_memory)
