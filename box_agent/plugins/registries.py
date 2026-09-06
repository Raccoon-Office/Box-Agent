"""Exact-type registries used while activating static plugins."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeVar, cast


CapabilityT = TypeVar("CapabilityT")


class DuplicateCapabilityError(ValueError):
    """Raised when an exact capability key already has an implementation."""


class CapabilityPolicy(str, Enum):
    """Cardinality required for one exact capability Port type."""

    REQUIRED_SINGLE = "required-single"
    OPTIONAL_SINGLE = "optional-single"
    MULTI = "multi"


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    """Immutable exact Port type and its registration cardinality."""

    port_type: type[Any]
    policy: CapabilityPolicy


@dataclass(frozen=True, slots=True)
class CapabilitySchema:
    """Immutable capability policy supplied explicitly to a plugin host."""

    bindings: tuple[CapabilityBinding, ...]


class ActivatedRegistry(Mapping[type[Any], object]):
    """Immutable exact-Port view returned by one plugin activation."""

    __slots__ = ("_entries", "_multi_entries")

    def __init__(
        self,
        entries: Mapping[type[Any], object],
        multi_entries: Mapping[type[Any], tuple[object, ...]],
    ) -> None:
        self._entries = MappingProxyType(dict(entries))
        self._multi_entries = MappingProxyType(dict(multi_entries))

    @property
    def entries(self) -> Mapping[type[Any], object]:
        return self._entries

    @property
    def multi_entries(self) -> Mapping[type[Any], tuple[object, ...]]:
        return self._multi_entries

    def __getitem__(self, port_type: type[CapabilityT]) -> CapabilityT:
        return cast(CapabilityT, self._entries[port_type])

    def __iter__(self) -> Iterator[type[Any]]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def get(
        self,
        port_type: type[CapabilityT],
        default: CapabilityT | None = None,
    ) -> CapabilityT | None:
        return cast(CapabilityT | None, self._entries.get(port_type, default))

    def require(self, port_type: type[CapabilityT]) -> CapabilityT:
        """Return the implementation registered for this exact Port type."""

        return self[port_type]

    def get_all(self, port_type: type[CapabilityT]) -> tuple[CapabilityT, ...]:
        """Return ordered implementations for an exact multi-binding Port."""

        return cast(tuple[CapabilityT, ...], self._multi_entries.get(port_type, ()))


class TypedRegistry:
    """Mutable activation-time builder for an immutable registry view."""

    __slots__ = ("_entries", "_multi_entries", "_policies")

    def __init__(self, schema: CapabilitySchema) -> None:
        self._entries: dict[type[Any], object] = {}
        self._multi_entries: dict[type[Any], list[object]] = {}
        self._policies = {
            binding.port_type: binding.policy for binding in schema.bindings
        }

    def register(self, port_type: type[CapabilityT], implementation: CapabilityT) -> None:
        policy = self._policies[port_type]
        if policy is CapabilityPolicy.MULTI:
            self._multi_entries.setdefault(port_type, []).append(implementation)
            return
        if port_type in self._entries:
            raise DuplicateCapabilityError(
                f"duplicate capability registration: {port_type.__qualname__}"
            )
        self._entries[port_type] = implementation

    def freeze(self) -> ActivatedRegistry:
        return ActivatedRegistry(
            self._entries,
            {
                port_type: tuple(implementations)
                for port_type, implementations in self._multi_entries.items()
            },
        )


__all__ = [
    "ActivatedRegistry",
    "CapabilityBinding",
    "CapabilityPolicy",
    "CapabilitySchema",
    "DuplicateCapabilityError",
    "TypedRegistry",
]
