"""Memory system for cross-session recall and session summarization.

Directory layout::

    ~/.box-agent/memory/
    ├── MEMORY.md                      # Manual (long-term) memory
    └── 2026-03-30/
    │   └── sess-0-a1b2c3d4.md        # Per-session summary
    └── 2026-03-29/
        └── sess-1-b2c3d4e5.md
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schema import Message

logger = logging.getLogger(__name__)


class MemoryManager:
    """Markdown-file-based memory with manual notes and session summaries."""

    def __init__(self, memory_dir: str = "~/.box-agent/memory", recall_days: int = 3):
        self.memory_dir = Path(memory_dir).expanduser()
        self.recall_days = recall_days
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    # ── Manual memory ──────────────────────────────────────────

    @property
    def memory_file(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    def read_manual_memory(self) -> str:
        """Read MEMORY.md content. Returns empty string if file missing."""
        if not self.memory_file.exists():
            return ""
        return self.memory_file.read_text(encoding="utf-8").strip()

    def write_manual_memory(self, content: str) -> None:
        """Overwrite MEMORY.md with *content*."""
        self.memory_file.write_text(content.strip() + "\n", encoding="utf-8")

    # ── Session summaries ──────────────────────────────────────

    def save_session_summary(self, session_id: str, summary: str) -> Path:
        """Save a session summary to ``{date}/{session_id}.md``."""
        today = date.today().isoformat()
        day_dir = self.memory_dir / today
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{session_id}.md"
        path.write_text(summary.strip() + "\n", encoding="utf-8")
        logger.info("Saved session summary: %s", path)
        return path

    def _list_recent_sessions(self) -> list[dict]:
        """List session summaries from the last *recall_days* days."""
        sessions: list[dict] = []
        today = date.today()
        for offset in range(self.recall_days):
            day = today - timedelta(days=offset)
            day_dir = self.memory_dir / day.isoformat()
            if not day_dir.is_dir():
                continue
            for f in sorted(day_dir.glob("*.md")):
                content = f.read_text(encoding="utf-8").strip()
                if content:
                    sessions.append(
                        {
                            "date": day.isoformat(),
                            "session_id": f.stem,
                            "content": content,
                        }
                    )
        return sessions

    # ── Recall ─────────────────────────────────────────────────

    def recall(self, query: str = "") -> str:  # noqa: ARG002 — query reserved for future semantic search
        """Build a memory block for system-prompt injection.

        Returns an empty string when there is nothing to recall.
        """
        manual = self.read_manual_memory()
        sessions = self._list_recent_sessions()
        return self.build_memory_block(manual, sessions)

    @staticmethod
    def build_memory_block(manual: str, sessions: list[dict]) -> str:
        """Format manual memory + recent sessions into a prompt block."""
        if not manual and not sessions:
            return ""

        parts: list[str] = ["--- MEMORY START ---"]

        if manual:
            parts.append("")
            parts.append("[Manual Memory]")
            parts.append(manual)

        if sessions:
            parts.append("")
            parts.append("[Recent Session Memory]")
            for s in sessions:
                parts.append(f"- {s['date']} | session_id={s['session_id']}")
                # Indent multi-line content
                for line in s["content"].splitlines():
                    parts.append(f"  {line}")

        parts.append("")
        parts.append("--- MEMORY END ---")
        return "\n".join(parts)

    # ── Summary generation ─────────────────────────────────────

    async def generate_session_summary(
        self,
        llm,
        messages: list[Message],
        session_id: str,
    ) -> str:
        """Use the LLM to generate a concise session summary, then save it.

        Returns the generated summary text (or empty string on failure).
        """
        # Build a condensed transcript for the LLM
        transcript_parts: list[str] = []
        for msg in messages:
            if msg.role == "system":
                continue
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            prefix = msg.role.capitalize()
            transcript_parts.append(f"{prefix}: {text[:2000]}")
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                names = [tc.function.name for tc in msg.tool_calls]
                transcript_parts.append(f"  → Tools: {', '.join(names)}")

        if not transcript_parts:
            return ""

        transcript = "\n".join(transcript_parts)
        today = datetime.now().strftime("%Y-%m-%d")

        prompt = (
            "Summarize the following agent session into a concise Markdown note.\n\n"
            f"Session ID: {session_id}\n"
            f"Date: {today}\n\n"
            "Conversation transcript (truncated):\n"
            f"{transcript[:8000]}\n\n"
            "Output format:\n"
            "# Session: {session_id}\n"
            "Date: {date}\n"
            "Task: {one-line task description}\n\n"
            "## Summary\n"
            "{2-4 sentence summary}\n\n"
            "## Key Results\n"
            "- {bullet points of outputs/artifacts}\n"
        )

        try:
            from .schema import Message as Msg

            response = await llm.generate(
                messages=[
                    Msg(role="system", content="You are a concise note-taking assistant."),
                    Msg(role="user", content=prompt),
                ]
            )
            summary = response.content
        except Exception:
            logger.exception("Failed to generate session summary via LLM")
            # Fallback: minimal summary
            summary = (
                f"# Session: {session_id}\n"
                f"Date: {today}\n"
                f"Task: (auto-summary failed)\n\n"
                "## Summary\n"
                "Session completed but auto-summary generation failed.\n"
            )

        self.save_session_summary(session_id, summary)
        return summary
