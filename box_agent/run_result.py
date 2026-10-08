"""Host-neutral aggregation of terminal run data."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .api import RunResult, RunStatus
from .events import ArtifactEvent, DoneEvent, ErrorEvent, TokenUsageEvent


@dataclass
class RunResultCollector:
    run_id: str
    usage: dict[str, int] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    result: RunResult | None = None

    def collect(self, payload: Any) -> None:
        if isinstance(payload, TokenUsageEvent):
            self.usage["total_tokens"] = (
                self.usage.get("total_tokens", 0) + payload.total_tokens
            )
        elif isinstance(payload, ArtifactEvent):
            self.artifacts.append(asdict(payload))
        elif isinstance(payload, ErrorEvent):
            self.error = {
                "message": payload.message,
                "error_code": payload.error_code,
                "error_category": payload.error_category,
                "error_details": payload.error_details,
            }
        elif isinstance(payload, DoneEvent):
            if payload.stop_reason is not None:
                status = {
                    "cancelled": RunStatus.CANCELLED,
                    "waiting_for_user": RunStatus.WAITING_FOR_USER,
                    "error": RunStatus.FAILED,
                }.get(payload.stop_reason.value, RunStatus.COMPLETED)
                self.result = RunResult(
                    run_id=self.run_id,
                    status=status,
                    stop_reason=payload.stop_reason.value,
                    final_content=payload.final_content,
                    usage=self.usage,
                    artifacts=tuple(self.artifacts),
                    error=self.error,
                )
