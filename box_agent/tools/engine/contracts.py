"""The definitions and real execution targets offered in one model request."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import json
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...schema import ToolCall
    from ..base import Tool
    from ..mcp_tool_search import MCPToolExposureManager


def _execution_identity(tool: Tool) -> tuple[Any, ...]:
    """Capture existing public/MCP routing metadata, without copying a tool."""

    return tuple(
        getattr(tool, attribute, None)
        for attribute in (
            "name", "mcp_tool_id", "server_name", "_server_name",
            "remote_name", "_remote_name",
        )
    ) + (getattr(tool, "deduplicate_within_batch", False) is True,)


def _without_description(schema: dict[str, Any]) -> dict[str, Any]:
    """Ignore only presentation text, retaining parameter and custom fields."""

    result = dict(schema)
    result.pop("description", None)
    if isinstance(result.get("function"), dict):
        result["function"] = dict(result["function"])
        result["function"].pop("description", None)
    return result


@dataclass(frozen=True, slots=True)
class ToolDefinitionView:
    """Provider-compatible schema view with no tool invocation interface."""

    name: str
    description: str
    aliases: tuple[str, ...]
    server_name: str | None
    _server_name: str | None
    _parameters: dict[str, Any] = field(repr=False)
    _schema: dict[str, Any] = field(repr=False)
    _openai_schema: dict[str, Any] = field(repr=False)
    _identity: tuple[Any, ...] = field(repr=False)
    _compaction_state_reader: Callable[[], tuple[str, str] | None] = field(repr=False)
    _deduplicate_within_batch: bool = field(default=False, repr=False)

    @classmethod
    def from_tool(cls, tool: Tool) -> ToolDefinitionView:
        return cls(
            name=tool.name,
            description=tool.description,
            aliases=tuple(tool.aliases),
            server_name=getattr(tool, "server_name", ""),
            _server_name=getattr(tool, "_server_name", ""),
            _parameters=deepcopy(tool.parameters),
            _schema=deepcopy(tool.to_schema()),
            _openai_schema=deepcopy(tool.to_openai_schema()),
            _identity=_execution_identity(tool),
            _compaction_state_reader=tool.compaction_state,
            _deduplicate_within_batch=getattr(tool, "deduplicate_within_batch", False) is True,
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return deepcopy(self._parameters)

    def to_schema(self) -> dict[str, Any]:
        return deepcopy(self._schema)

    def to_openai_schema(self) -> dict[str, Any]:
        return deepcopy(self._openai_schema)

    def compaction_state(self) -> tuple[str, str] | None:
        """Read current trusted state from the original tool only."""

        return self._compaction_state_reader()

    def matches_target(self, tool: Tool) -> bool:
        return (
            self._identity == _execution_identity(tool)
            and self._parameters == tool.parameters
            and _without_description(self._schema)
            == _without_description(tool.to_schema())
            and _without_description(self._openai_schema)
            == _without_description(tool.to_openai_schema())
        )


@dataclass(frozen=True, slots=True)
class PreparedTools:
    """A request-local snapshot; its targets remain the original live objects."""

    definitions: tuple[ToolDefinitionView, ...]
    targets: Mapping[str, Tool]
    call_names: Mapping[str, str]
    mcp_generations: Mapping[str, int]
    _tool_exposure: MCPToolExposureManager | None = field(default=None, repr=False)
    _registered_names: frozenset[str] = field(default_factory=frozenset, repr=False)
    routed_targets: Mapping[str, Tool] = field(default_factory=dict, repr=False)
    routed_definitions: Mapping[str, ToolDefinitionView] = field(default_factory=dict, repr=False)
    route_refs: Mapping[str, str] = field(default_factory=dict, repr=False)
    route_visible: Callable[[str], bool] | None = field(default=None, repr=False)
    _definitions_by_name: Mapping[str, ToolDefinitionView] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "definitions", tuple(self.definitions))
        for name in ("targets", "call_names", "mcp_generations", "routed_targets", "routed_definitions", "route_refs"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        object.__setattr__(
            self, "_definitions_by_name",
            MappingProxyType({definition.name: definition for definition in self.definitions}),
        )

    def canonicalize_calls(self, calls: Iterable[ToolCall]) -> list[ToolCall]:
        """Normalize execution copies without altering provider responses."""

        normalized = []
        for call in calls:
            execution_call = call.model_copy(deep=True)
            execution_call, _, _ = self.resolve_call(execution_call)
            name = execution_call.function.name
            execution_call.function.name = self.call_names.get(name, name)
            normalized.append(execution_call)
        return normalized

    def resolve_call(self, call: ToolCall) -> tuple[ToolCall, bool, str | None]:
        """Resolve against the request snapshot without modifying provider history."""
        result = call.model_copy(deep=True)
        if result.function.name != "call_tool" or not self.route_refs and "call_tool" not in self.targets:
            return result, False, None
        args = result.function.arguments
        if not isinstance(args, dict) or set(args) != {"tool_ref", "arguments_json"} or not all(isinstance(args[k], str) for k in args):
            return result, True, "Invalid call_tool arguments: expected tool_ref and arguments_json strings."
        name = self.route_refs.get(args["tool_ref"])
        if name is None:
            return result, True, "Unknown or expired tool_ref; search again on a later model step."
        def unique_pairs(pairs):
            output = {}
            for key, value in pairs:
                if key in output:
                    raise ValueError("duplicate key")
                output[key] = value
            return output
        try:
            arguments = json.loads(args["arguments_json"], object_pairs_hook=unique_pairs,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid constant")))
        except (ValueError, TypeError, RecursionError):
            return result, True, "Invalid arguments_json: expected a valid JSON object."
        if not isinstance(arguments, dict):
            return result, True, "Invalid arguments_json: expected a JSON object."
        result.function.name = name
        result.function.arguments = arguments
        return result, True, self.validate_routed_call(name)

    def validate_routed_call(self, name: str) -> str | None:
        target = self.routed_targets.get(name)
        definition = self.routed_definitions.get(name)
        if target is None or definition is None:
            return "Unknown or expired tool_ref; search again."
        if self.route_visible is not None and not self.route_visible(name):
            return f"Tool '{name}' is not available in this conversation."
        if self._tool_exposure is not None:
            error = self._tool_exposure.validate_call(name, self.mcp_generations.get(name), target)
            if error:
                return error
            if not self._tool_exposure.is_current_target(name, target):
                return f"Tool '{name}' changed or is unavailable; search again."
        try:
            if definition.description == target.description and definition.matches_target(target):
                return None
        except Exception:
            pass
        return f"Tool '{name}' definition changed; search again."

    def allows_batch_deduplication(self, name: str) -> bool:
        """Use only the offered target's unchanged, trusted opt-in."""
        name = self.call_names.get(name, name)
        definition = self._definitions_by_name.get(name) or self.routed_definitions.get(name)
        return (
            definition is not None
            and definition._deduplicate_within_batch
            and (self.validate_routed_call(name) if name in self.routed_targets else self.validate_call(name)) is None
        )

    def admission_error(self, name: str) -> str | None:
        """Apply offer restrictions while retaining visible unknown-call errors.

        Without discovery, a completely unknown call is still charged and shown
        as a failed call, as before. It has no execution target: validate_call
        must still reject it at invocation. Hidden registered tools never get
        this compatibility treatment.
        """
        if (
            self._tool_exposure is None
            and name not in self._registered_names
            and name not in self.call_names
        ):
            return None
        return self.validate_call(name)

    def validate_call(self, name: str) -> str | None:
        """Reject unoffered or changed targets before their actual invocation."""

        name = self.call_names.get(name, name)
        target = self.targets.get(name)
        if target is None:
            unknown = (
                f"Unknown tool: {name}. "
                if self._tool_exposure is None and name not in self._registered_names
                else ""
            )
            return (
                unknown +
                f"Tool '{name}' was not offered in this model step. "
                "Use tool_search and call an activated result on the next step."
            )
        generation = self.mcp_generations.get(name)
        if self._tool_exposure is not None and getattr(self._tool_exposure, "stable_prefix", False) and not self._tool_exposure.is_current_target(name, target):
            return f"Tool '{name}' is no longer available."
        if self._tool_exposure is not None:
            error = self._tool_exposure.validate_call(name, generation, target)
            if error is not None:
                return error
        elif (
            generation is not None
            and getattr(target, "mcp_generation", None) != generation
        ):
            return (
                f"MCP tool '{name}' execution target changed after it was offered; "
                "prepare tools again."
            )
        try:
            matches = self._definitions_by_name[name].matches_target(target)
        except Exception:
            matches = False
        if not matches:
            return (
                f"Tool '{name}' definition or execution target changed after it was offered; "
                "prepare tools again before calling it."
            )
        return None

    def target_identity(self, name: str) -> tuple[str | None, str | None]:
        """Return the original MCP identity for execution events and logging."""

        definition = self._definitions_by_name.get(self.call_names.get(name, name)) or self.routed_definitions.get(name)
        if definition is None:
            return None, None
        tool_id, server_name = definition._identity[1:3]
        return (
            tool_id if isinstance(tool_id, str) and tool_id else None,
            server_name if isinstance(server_name, str) and server_name else None,
        )
