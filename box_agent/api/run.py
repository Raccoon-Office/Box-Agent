"""Stable data contracts for one Agent run."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ControlCommandKind(str, Enum):
    """Commands a host may send to an active run."""

    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"
    INJECT_MESSAGE = "inject_message"
    PERMISSION_RESPONSE = "permission_response"


class RunStatus(str, Enum):
    """Terminal status of a run."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    WAITING_FOR_USER = "waiting_for_user"


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _json_value(value: Any) -> Any:
    """Convert DTOs and event payloads without retaining runtime callbacks."""

    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in fields(value)
            if item.repr
        }
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Pure input data needed to start one run in an existing session."""

    run_id: str
    session_id: str
    user_message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.session_id, "session_id")
        if self.user_message is not None:
            _required_text(self.user_message, "user_message")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible request payload."""

        return _json_value(self)


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """Pure command data sent from a host to an active run."""

    kind: ControlCommandKind
    request_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = self.kind
        if not isinstance(kind, ControlCommandKind):
            try:
                kind = ControlCommandKind(kind)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unsupported control command: {self.kind!r}") from exc
            object.__setattr__(self, "kind", kind)
        if self.request_id is not None:
            _required_text(self.request_id, "request_id")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible command payload."""

        return _json_value(self)

    @classmethod
    def cancel(cls) -> "ControlCommand":
        return cls(ControlCommandKind.CANCEL)

    @classmethod
    def pause(cls) -> "ControlCommand":
        return cls(ControlCommandKind.PAUSE)

    @classmethod
    def resume(cls) -> "ControlCommand":
        return cls(ControlCommandKind.RESUME)

    @classmethod
    def permission_response(
        cls,
        request_id: str,
        *,
        approved: bool | None = None,
        option_id: str | None = None,
    ) -> "ControlCommand":
        _required_text(request_id, "request_id")
        if approved is None and option_id is None:
            raise ValueError("permission response requires approved or option_id")
        if approved is not None and not isinstance(approved, bool):
            raise ValueError("approved must be a boolean")
        if option_id is not None:
            _required_text(option_id, "option_id")
        payload: dict[str, Any] = {}
        if approved is not None:
            payload["approved"] = approved
        if option_id is not None:
            payload["option_id"] = option_id
        return cls(ControlCommandKind.PERMISSION_RESPONSE, request_id, payload)

    @classmethod
    def inject_message(
        cls,
        content: str,
        *,
        injection_id: str | None = None,
    ) -> "ControlCommand":
        _required_text(content, "message")
        return cls(
            ControlCommandKind.INJECT_MESSAGE,
            request_id=injection_id,
            payload={"content": content},
        )


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """One ordered event emitted by a run."""

    run_id: str
    event_id: str
    sequence: int
    payload: Any

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.event_id, "event_id")
        if self.sequence < 1:
            raise ValueError("sequence must be positive")

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable envelope retaining the event's type name."""

        result = _json_value(self)
        result["event_type"] = type(self.payload).__name__
        return result


@dataclass(frozen=True, slots=True)
class RunResult:
    """Aggregated terminal result of one run."""

    run_id: str
    status: RunStatus
    stop_reason: str
    final_content: str
    usage: Mapping[str, int] = field(default_factory=dict)
    artifacts: tuple[Mapping[str, Any], ...] = ()
    error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.stop_reason, "stop_reason")
        if not isinstance(self.status, RunStatus):
            object.__setattr__(self, "status", RunStatus(self.status))
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        object.__setattr__(
            self,
            "artifacts",
            tuple(MappingProxyType(dict(item)) for item in self.artifacts),
        )
        if self.error is not None:
            object.__setattr__(self, "error", MappingProxyType(dict(self.error)))

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible terminal result."""

        return _json_value(self)


__all__ = [
    "ControlCommand",
    "ControlCommandKind",
    "EventEnvelope",
    "RunRequest",
    "RunResult",
    "RunStatus",
]
