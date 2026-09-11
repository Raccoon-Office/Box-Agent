"""Session-owned Skill read facts; source discovery has no session state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import json
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SkillRead:
    name: str
    source: str
    path: str
    revision: str
    prompt: str
    order: int
    reason: str
    delivered_ranges: tuple[tuple[int, int], ...] = ()
    delivered_complete: bool = False

    def log_record(self) -> dict[str, Any]:
        # These three original fields retain the old restore/hash contract.
        return {"name": self.name, "sha256": self.revision, "loadOrder": self.order,
                "source": self.source, "path": self.path, "reason": self.reason,
                "deliveredRanges": [list(pair) for pair in self.delivered_ranges],
                "deliveredComplete": self.delivered_complete}


@dataclass(frozen=True, slots=True)
class SkillReferenceSnapshot:
    """Immutable effective-source data; resolving it does not imply delivery."""

    name: str
    source: str
    path: str
    revision: str
    prompt: str
    metadata_json: str

    @property
    def skill_path(self) -> Path | None:
        return Path(self.path) if self.path else None

    def to_prompt(self) -> str:
        return self.prompt

    def reference_metadata(self, *, offset: int, reason: str) -> dict[str, Any]:
        metadata = json.loads(self.metadata_json)
        metadata.update(offset=offset, end_offset=offset, reason=reason)
        return metadata


@dataclass(slots=True)
class SkillSessionState:
    reads: dict[str, SkillRead] = field(default_factory=dict)
    selected: tuple[str, ...] = ()
    sequence: int = 0
