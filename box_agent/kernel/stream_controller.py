"""Provider stream liveness control for the stable agent kernel."""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import AsyncIterator
from time import perf_counter

from ..schema import StreamEvent


def resolve_provider_stale_seconds(
    config_value: float | None = None,
    *,
    default_stale_seconds: float,
    environment_variable_name: str,
) -> float:
    """Resolve the provider-stale cutoff from environment, config, or default."""
    raw = os.environ.get(environment_variable_name)
    if raw is not None and raw.strip():
        try:
            parsed = float(raw)
        except ValueError:
            parsed = 0.0
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    if config_value is not None and math.isfinite(config_value) and config_value > 0:
        return float(config_value)
    return default_stale_seconds


async def stream_with_activity(
    stream: AsyncIterator[StreamEvent],
    *,
    stale_seconds: float,
    activity_interval_seconds: float,
) -> AsyncIterator[StreamEvent]:
    """Add bounded host heartbeats and stop a provider stream that is stale."""
    iterator = stream.__aiter__()
    next_chunk: asyncio.Task[StreamEvent] | None = None
    last_provider_chunk = perf_counter()
    try:
        next_chunk = asyncio.create_task(iterator.__anext__())
        while True:
            done, _ = await asyncio.wait(
                {next_chunk}, timeout=activity_interval_seconds
            )
            if not done:
                stale_seconds_elapsed = perf_counter() - last_provider_chunk
                if stale_seconds_elapsed >= stale_seconds:
                    yield StreamEvent(
                        type="finish",
                        finish_reason="provider_stale",
                        activity={
                            "protocol": "agent_activity_v1",
                            "phase": "provider_wait",
                            "seconds_since_provider_chunk": round(stale_seconds_elapsed, 1),
                        },
                    )
                    return
                yield StreamEvent(
                    type="activity",
                    activity={
                        "protocol": "agent_activity_v1",
                        "phase": "provider_wait",
                        "seconds_since_provider_chunk": round(stale_seconds_elapsed, 1),
                    },
                )
                continue
            try:
                chunk = next_chunk.result()
            except StopAsyncIteration:
                return
            last_provider_chunk = perf_counter()
            yield chunk
            next_chunk = asyncio.create_task(iterator.__anext__())
    finally:
        if next_chunk is not None and not next_chunk.done():
            next_chunk.cancel()
            try:
                await next_chunk
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
        closer = getattr(iterator, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except (RuntimeError, asyncio.CancelledError):
                pass
