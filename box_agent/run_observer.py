"""Protocol-independent observers for one Agent run."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


def _as_int(mapping: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return 0


@dataclass(slots=True)
class TokenUsageAccumulator:
    """Accumulate provider usage while preserving legacy key aliases."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def add(self, usage: Any) -> bool:
        """Add one provider usage mapping and report whether it was useful."""

        if not isinstance(usage, dict):
            return False
        prompt_tokens = _as_int(usage, "prompt_tokens", "promptTokens")
        completion_tokens = _as_int(usage, "completion_tokens", "completionTokens")
        total_tokens = _as_int(usage, "total_tokens", "totalTokens")
        if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
            total_tokens = prompt_tokens + completion_tokens
        if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
            return False
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += total_tokens
        self.calls += 1
        return True

    def as_payload(self) -> dict[str, int]:
        return {
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
            "calls": self.calls,
        }


@dataclass(slots=True)
class RunObserver:
    """Shared usage and trace observation for one protocol-neutral turn.

    The observer deliberately accepts a duck-typed trace writer.  This keeps
    session trace persistence out of the runtime layer while preserving the
    writer's existing keyword contract for ACP and future adapters.
    """

    trace_writer: Any | None = None
    turn_id: str = ""
    usage: TokenUsageAccumulator = field(default_factory=TokenUsageAccumulator)

    def record_usage(self, value: Any) -> bool:
        return self.usage.add(value)

    def trace(
        self,
        event: str,
        *,
        turn_id: str | None = None,
        step: int | None = None,
        llm_call_id: str | None = None,
        tool_call_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        if self.trace_writer is None:
            return
        kwargs: dict[str, Any] = {
            "turn_id": self.turn_id if turn_id is None else turn_id,
        }
        if step is not None:
            kwargs["step"] = step
        if llm_call_id is not None:
            kwargs["llm_call_id"] = llm_call_id
        if tool_call_id is not None:
            kwargs["tool_call_id"] = tool_call_id
        if data is not None:
            kwargs["data"] = data
        self.trace_writer.write(event, **kwargs)


@dataclass(frozen=True, slots=True)
class ArtifactObservation:
    """Result of protocol-neutral artifact registration."""

    lineage: Any | None = None
    error: Exception | None = None


@dataclass(slots=True)
class ArtifactObserver:
    """Run artifact registration without owning host-specific envelopes."""

    workspace_dir: Any
    task_context: Any
    register_revision: Callable[..., Any] | None = None

    def observe(self, artifact: Any) -> ArtifactObservation:
        if self.register_revision is None:
            return ArtifactObservation()
        try:
            lineage = self.register_revision(
                self.workspace_dir,
                self.task_context,
                artifact,
            )
        except Exception as exc:
            return ArtifactObservation(error=exc)
        return ArtifactObservation(lineage=lineage)


@dataclass(frozen=True, slots=True)
class TurnCleanupResult:
    """Observable results from best-effort end-of-turn resource cleanup."""

    terminated_bash_ids: list[str] = field(default_factory=list)
    discarded_paths: list[str] = field(default_factory=list)
    removed_scratch_paths: list[str] = field(default_factory=list)
    errors: list[tuple[str, Exception]] = field(default_factory=list)


async def cleanup_turn_resources(
    *,
    bash_tool: Any | None = None,
    write_tool: Any | None = None,
    skill_scratch_dir: Any | None = None,
    browser_owner: str | None = None,
    bash_lifetime: str | None = None,
    cleanup_scratch: Callable[[Any], list[str]] | None = None,
    release_browser: Callable[[str], Awaitable[None]] | None = None,
    on_success: Callable[[str, list[str]], None] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
) -> TurnCleanupResult:
    """Perform the common end-of-turn cleanup sequence.

    Every step is attempted in legacy order and failures are returned to the
    adapter for its existing logging policy.  The helper intentionally does
    not import ACP/CLI types or decide how errors should be rendered.
    """

    terminated_bash_ids: list[str] = []
    discarded_paths: list[str] = []
    removed_scratch_paths: list[str] = []
    errors: list[tuple[str, Exception]] = []

    def notify_success(kind: str, values: list[str]) -> None:
        if on_success is None:
            return
        try:
            on_success(kind, values)
        except Exception:
            # Logging/reporting must not change cleanup behavior.
            return

    def notify_error(kind: str, error: Exception) -> None:
        if on_error is None:
            return
        try:
            on_error(kind, error)
        except Exception:
            return

    if bash_tool is not None:
        cleanup = getattr(bash_tool, "cleanup_background_processes", None)
        if callable(cleanup):
            try:
                terminated_bash_ids = list(
                    await cleanup(lifetime=bash_lifetime)
                )
                notify_success("bash", terminated_bash_ids)
            except Exception as exc:
                errors.append(("bash", exc))
                notify_error("bash", exc)

    if write_tool is not None:
        cleanup = getattr(write_tool, "cleanup_pending_writes", None)
        if callable(cleanup):
            try:
                discarded_paths = list(cleanup())
                notify_success("write_file", discarded_paths)
            except Exception as exc:
                errors.append(("write_file", exc))
                notify_error("write_file", exc)

    if skill_scratch_dir is not None and cleanup_scratch is not None:
        try:
            removed_scratch_paths = list(cleanup_scratch(skill_scratch_dir))
            notify_success("skill_scratch", removed_scratch_paths)
        except Exception as exc:
            errors.append(("skill_scratch", exc))
            notify_error("skill_scratch", exc)

    if browser_owner is not None and release_browser is not None:
        try:
            await release_browser(browser_owner)
            notify_success("browser", [])
        except Exception as exc:
            errors.append(("browser", exc))
            notify_error("browser", exc)

    return TurnCleanupResult(
        terminated_bash_ids=terminated_bash_ids,
        discarded_paths=discarded_paths,
        removed_scratch_paths=removed_scratch_paths,
        errors=errors,
    )


__all__ = [
    "ArtifactObservation",
    "ArtifactObserver",
    "RunObserver",
    "TokenUsageAccumulator",
    "TurnCleanupResult",
    "cleanup_turn_resources",
]
