"""Host-neutral limits for one run's pending event delivery."""
from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class RunDeliveryOptions:
    max_events: int = 1024
    max_bytes: int = 4 * 1024 * 1024
    congestion_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in ("max_events", "max_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        timeout = self.congestion_timeout_seconds
        if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("congestion_timeout_seconds must be positive and finite")
