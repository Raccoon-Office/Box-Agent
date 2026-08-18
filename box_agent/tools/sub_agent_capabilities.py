"""Capability resolution for explicitly delegated sub-agent tasks.

The parent model describes the capabilities a child task needs.  This module
normalizes that declaration and intersects it with the parent's live tool map,
selected Skills, execution-strategy limits, and hard task constraints.

It deliberately does not execute tools or call an LLM.  The resolved bundle
contains the original live tool instances so their existing PermissionEngine
checks remain the final resource-level authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..config import ToolLimitsConfig
from .base import Tool

_DEFAULT_SUB_AGENT_LIMITS = ToolLimitsConfig().sub_agent
GENERAL_LOOP_MAX_STEPS = _DEFAULT_SUB_AGENT_LIMITS.general_max_steps
GENERAL_LOOP_MAX_TOOL_CALLS = _DEFAULT_SUB_AGENT_LIMITS.general_max_tool_calls
BATCH_FILES_MAX_FILES = 32
BATCH_FILES_MAX_STEPS = 1
BATCH_FILE_MAX_CHARS = 64_000
BATCH_AGGREGATE_MAX_CHARS = 200_000

SUPPORTED_STRATEGIES = frozenset({"general_loop", "batch_files"})
_BATCH_FILES_ALLOWED_TOOLS = frozenset({"read_file"})

MINIMAL_DELEGATION_EXAMPLE: dict[str, Any] = {
    "task": "Read the provided files and summarize them.",
    "capabilities": {"required_tools": ["read_file"]},
}


@dataclass(frozen=True)
class ToolCapabilityMetadata:
    """Security-relevant traits for built-in tools."""

    read: bool = False
    write: bool = False
    network: bool = False
    process: bool = False
    external_side_effect: bool = False


# Keep this map intentionally small and explicit.  Unknown live tools are
# treated conservatively rather than inferred from their description.
BUILTIN_TOOL_CAPABILITIES: dict[str, ToolCapabilityMetadata] = {
    "read_file": ToolCapabilityMetadata(read=True),
    "query_jsonl": ToolCapabilityMetadata(read=True),
    "search_files": ToolCapabilityMetadata(read=True),
    "write_file": ToolCapabilityMetadata(write=True),
    "append_file": ToolCapabilityMetadata(write=True),
    "edit_file": ToolCapabilityMetadata(write=True),
    "bash": ToolCapabilityMetadata(read=True, write=True, network=True, process=True),
    "bash_output": ToolCapabilityMetadata(read=True, process=True),
    "bash_kill": ToolCapabilityMetadata(process=True),
    "execute_code": ToolCapabilityMetadata(read=True, write=True, network=True, process=True),
    "sandbox_status": ToolCapabilityMetadata(read=True, process=True),
    "web_search": ToolCapabilityMetadata(read=True, network=True),
    "web_extract": ToolCapabilityMetadata(read=True, network=True),
    "vision_review": ToolCapabilityMetadata(read=True, network=True),
    "generate_image": ToolCapabilityMetadata(write=True, network=True),
    "get_skill": ToolCapabilityMetadata(read=True),
    "memory_read": ToolCapabilityMetadata(read=True),
    "memory_search": ToolCapabilityMetadata(read=True),
    "memory_write": ToolCapabilityMetadata(write=True),
    "plan_read": ToolCapabilityMetadata(read=True),
    "plan_write": ToolCapabilityMetadata(write=True),
    "todo_read": ToolCapabilityMetadata(read=True),
    "todo_write": ToolCapabilityMetadata(write=True),
    "goal_read": ToolCapabilityMetadata(read=True),
    "goal_write": ToolCapabilityMetadata(write=True),
    "mcp_config": ToolCapabilityMetadata(write=True, external_side_effect=True),
    "create_scheduled_task": ToolCapabilityMetadata(external_side_effect=True),
    "obsidian_create_note": ToolCapabilityMetadata(write=True, external_side_effect=True),
    "obsidian_update_note": ToolCapabilityMetadata(write=True, external_side_effect=True),
    "obsidian_daily_note": ToolCapabilityMetadata(write=True, external_side_effect=True),
}

_PLAYWRIGHT_READ_ONLY_TOOLS = frozenset(
    {
        "browser_console_messages",
        "browser_navigate",
        "browser_navigate_back",
        "browser_network_requests",
        "browser_snapshot",
        "browser_wait_for",
    }
)


def _tool_capability_metadata(name: str, tool: Tool) -> ToolCapabilityMetadata | None:
    metadata = BUILTIN_TOOL_CAPABILITIES.get(name)
    if metadata is not None:
        return metadata

    # MCP tool names alone are not a trustworthy security boundary. The live
    # wrapper also records the server that supplied them, so recognize the
    # managed Playwright server explicitly. Navigation/inspection remains
    # read-only from the user's resource perspective; interaction, arbitrary
    # browser code, uploads, and screenshots require an external-side-effect
    # grant rather than falling through as unknown metadata.
    if getattr(tool, "server_name", "") == "playwright":
        if name in _PLAYWRIGHT_READ_ONLY_TOOLS:
            return ToolCapabilityMetadata(read=True, network=True)
        return ToolCapabilityMetadata(
            read=True,
            network=True,
            external_side_effect=True,
        )
    return None


@dataclass(frozen=True)
class DelegationConstraints:
    read_only: bool = True
    network: bool = True
    write_scope: tuple[str, ...] | None = None
    external_side_effect: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "network": self.network,
            "write_scope": list(self.write_scope) if self.write_scope else None,
            "external_side_effect": self.external_side_effect,
        }


@dataclass(frozen=True)
class DelegationBudget:
    max_steps: int
    max_tool_calls: int

    def to_dict(self) -> dict[str, int]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
        }


@dataclass(frozen=True)
class DelegationSpec:
    title: str | None
    task: str
    strategy: str
    required_tools: tuple[str, ...]
    optional_tools: tuple[str, ...]
    skill_names: tuple[str, ...]
    inputs: dict[str, Any]
    constraints: DelegationConstraints
    budget: DelegationBudget
    defaults_applied: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityFailure:
    """Stable structured failure returned before a child LLM starts."""

    code: str
    message: str
    retryable: bool
    invalid_fields: tuple[str, ...] = ()
    defaults_applied: tuple[str, ...] = ()
    pending_source: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "sub_agent_delegation_error",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "invalid_fields": list(self.invalid_fields),
            "defaults_applied": list(self.defaults_applied),
        }
        if self.pending_source:
            payload["pending_source"] = self.pending_source
        if self.details:
            payload.update(self.details)
        if self.code == "INVALID_DELEGATION_SPEC":
            payload["minimal_valid_example"] = MINIMAL_DELEGATION_EXAMPLE
            payload["retry_limit"] = 1
        return payload


@dataclass(frozen=True)
class ResolvedCapabilityBundle:
    spec: DelegationSpec
    tools: dict[str, Tool]
    skills: tuple[Any, ...]
    skill_added_tools: tuple[str, ...]
    denied_tools: tuple[dict[str, str], ...]

    @property
    def resolved_tool_names(self) -> tuple[str, ...]:
        return tuple(self.tools)

    @property
    def resolved_skill_names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills)

    def diagnostic_payload(self) -> dict[str, Any]:
        return {
            "strategy": self.spec.strategy,
            "requested_tools": {
                "required": list(self.spec.required_tools),
                "optional": list(self.spec.optional_tools),
                "skill_added": list(self.skill_added_tools),
            },
            "resolved_tools": list(self.resolved_tool_names),
            "denied_tools": [dict(item) for item in self.denied_tools],
            "requested_skills": list(self.spec.skill_names),
            "resolved_skills": list(self.resolved_skill_names),
            "constraints": self.spec.constraints.to_dict(),
            "budget": self.spec.budget.to_dict(),
            "defaults_applied": list(self.spec.defaults_applied),
        }


def _normalized_string_list(
    value: Any,
    *,
    field_name: str,
    invalid_fields: list[str],
    required_non_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        invalid_fields.append(field_name)
        return ()
    normalized: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            invalid_fields.append(f"{field_name}[{index}]")
            continue
        normalized.add(item.strip())
    if required_non_empty and not normalized:
        invalid_fields.append(field_name)
    return tuple(sorted(normalized))


def _unknown_fields(value: Mapping[str, Any], allowed: set[str], prefix: str) -> list[str]:
    return [f"{prefix}.{name}" for name in sorted(set(value) - allowed)]


def parse_delegation_spec(
    *,
    task: Any,
    title: Any = None,
    execution: Any = None,
    capabilities: Any,
    inputs: Any = None,
    constraints: Any = None,
    budget: Any = None,
    general_max_steps: int = GENERAL_LOOP_MAX_STEPS,
    general_max_tool_calls: int = GENERAL_LOOP_MAX_TOOL_CALLS,
) -> DelegationSpec | CapabilityFailure:
    """Validate and normalize a new-style delegation declaration."""

    invalid_fields: list[str] = []
    defaults_applied: list[str] = []

    if not isinstance(task, str) or not task.strip():
        invalid_fields.append("task")
        normalized_task = ""
    else:
        normalized_task = task.strip()

    if title is not None and not isinstance(title, str):
        invalid_fields.append("title")
        normalized_title = None
    else:
        normalized_title = title.strip() if isinstance(title, str) and title.strip() else None

    if execution is None:
        execution = {}
        defaults_applied.append("execution.strategy")
    if not isinstance(execution, dict):
        invalid_fields.append("execution")
        execution = {}
    else:
        invalid_fields.extend(_unknown_fields(execution, {"strategy"}, "execution"))
    strategy = execution.get("strategy", "general_loop")
    if "strategy" not in execution and "execution.strategy" not in defaults_applied:
        defaults_applied.append("execution.strategy")
    if not isinstance(strategy, str) or strategy not in SUPPORTED_STRATEGIES:
        invalid_fields.append("execution.strategy")
        strategy = "general_loop"

    if not isinstance(capabilities, dict):
        invalid_fields.append("capabilities")
        capabilities = {}
    else:
        invalid_fields.extend(
            _unknown_fields(
                capabilities,
                {"required_tools", "optional_tools", "skills"},
                "capabilities",
            )
        )

    if "required_tools" not in capabilities:
        invalid_fields.append("capabilities.required_tools")
        required_tools = ()
    else:
        required_tools = _normalized_string_list(
            capabilities.get("required_tools"),
            field_name="capabilities.required_tools",
            invalid_fields=invalid_fields,
            required_non_empty=True,
        )

    if "optional_tools" not in capabilities:
        defaults_applied.append("capabilities.optional_tools")
    optional_tools = _normalized_string_list(
        capabilities.get("optional_tools", []),
        field_name="capabilities.optional_tools",
        invalid_fields=invalid_fields,
    )
    if "skills" not in capabilities:
        defaults_applied.append("capabilities.skills")
    skill_names = _normalized_string_list(
        capabilities.get("skills", []),
        field_name="capabilities.skills",
        invalid_fields=invalid_fields,
    )

    if inputs is None:
        inputs = {}
        defaults_applied.append("inputs")
    if not isinstance(inputs, dict):
        invalid_fields.append("inputs")
        normalized_inputs: dict[str, Any] = {}
    else:
        normalized_inputs = dict(inputs)

    if constraints is None:
        constraints = {}
    if not isinstance(constraints, dict):
        invalid_fields.append("constraints")
        constraints = {}
    else:
        invalid_fields.extend(
            _unknown_fields(
                constraints,
                {"read_only", "network", "write_scope", "external_side_effect"},
                "constraints",
            )
        )

    constraint_values: dict[str, bool] = {}
    for name, default in (
        ("read_only", True),
        ("network", True),
        ("external_side_effect", False),
    ):
        if name not in constraints:
            defaults_applied.append(f"constraints.{name}")
        value = constraints.get(name, default)
        if not isinstance(value, bool):
            invalid_fields.append(f"constraints.{name}")
            value = default
        constraint_values[name] = value

    if "write_scope" not in constraints:
        defaults_applied.append("constraints.write_scope")
    raw_write_scope = constraints.get("write_scope")
    if raw_write_scope is None:
        write_scope = None
    elif isinstance(raw_write_scope, str):
        if raw_write_scope.strip():
            write_scope = (raw_write_scope.strip(),)
        else:
            invalid_fields.append("constraints.write_scope")
            write_scope = None
    elif isinstance(raw_write_scope, list):
        write_scope = _normalized_string_list(
            raw_write_scope,
            field_name="constraints.write_scope",
            invalid_fields=invalid_fields,
            required_non_empty=True,
        )
    else:
        invalid_fields.append("constraints.write_scope")
        write_scope = None

    parsed_constraints = DelegationConstraints(
        read_only=constraint_values["read_only"],
        network=constraint_values["network"],
        write_scope=write_scope,
        external_side_effect=constraint_values["external_side_effect"],
    )

    if budget is None:
        budget = {}
    if not isinstance(budget, dict):
        invalid_fields.append("budget")
        budget = {}
    else:
        invalid_fields.extend(
            _unknown_fields(budget, {"max_steps", "max_tool_calls"}, "budget")
        )

    if strategy == "batch_files":
        if set(required_tools) != {"read_file"}:
            invalid_fields.append("capabilities.required_tools")

        raw_files = normalized_inputs.get("files")
        files = _normalized_string_list(
            raw_files,
            field_name="inputs.files",
            invalid_fields=invalid_fields,
            required_non_empty=True,
        )
        normalized_inputs["files"] = list(files)
        if len(files) > BATCH_FILES_MAX_FILES:
            invalid_fields.append("inputs.files")

        if "max_steps" not in budget:
            defaults_applied.append("budget.max_steps")
        raw_steps = budget.get("max_steps", BATCH_FILES_MAX_STEPS)
        if not isinstance(raw_steps, int) or isinstance(raw_steps, bool) or raw_steps < 1:
            invalid_fields.append("budget.max_steps")
            raw_steps = BATCH_FILES_MAX_STEPS
        max_steps = min(raw_steps, BATCH_FILES_MAX_STEPS)

        if "max_tool_calls" not in budget:
            defaults_applied.append("budget.max_tool_calls")
        raw_tool_calls = budget.get("max_tool_calls", len(files))
        if (
            not isinstance(raw_tool_calls, int)
            or isinstance(raw_tool_calls, bool)
            or raw_tool_calls < len(files)
        ):
            invalid_fields.append("budget.max_tool_calls")
            raw_tool_calls = len(files)
        max_tool_calls = min(raw_tool_calls, len(files), BATCH_FILES_MAX_FILES)
    else:
        if "max_steps" not in budget:
            defaults_applied.append("budget.max_steps")
        raw_steps = budget.get("max_steps", general_max_steps)
        if not isinstance(raw_steps, int) or isinstance(raw_steps, bool) or raw_steps < 1:
            invalid_fields.append("budget.max_steps")
            raw_steps = general_max_steps
        max_steps = min(raw_steps, general_max_steps)

        if "max_tool_calls" not in budget:
            defaults_applied.append("budget.max_tool_calls")
        raw_tool_calls = budget.get("max_tool_calls", general_max_tool_calls)
        if (
            not isinstance(raw_tool_calls, int)
            or isinstance(raw_tool_calls, bool)
            or raw_tool_calls < 1
        ):
            invalid_fields.append("budget.max_tool_calls")
            raw_tool_calls = general_max_tool_calls
        max_tool_calls = min(raw_tool_calls, general_max_tool_calls)

    if invalid_fields:
        return CapabilityFailure(
            code="INVALID_DELEGATION_SPEC",
            message="The sub-agent capability declaration is invalid; fix the listed fields and retry at most once.",
            retryable=True,
            invalid_fields=tuple(sorted(set(invalid_fields))),
            defaults_applied=tuple(sorted(set(defaults_applied))),
        )

    return DelegationSpec(
        title=normalized_title,
        task=normalized_task,
        strategy=strategy,
        required_tools=required_tools,
        optional_tools=optional_tools,
        skill_names=skill_names,
        inputs=normalized_inputs,
        constraints=parsed_constraints,
        budget=DelegationBudget(
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
        ),
        defaults_applied=tuple(sorted(set(defaults_applied))),
    )


def _mcp_state(value: Any) -> str:
    if isinstance(value, str) and value.lower() in {"loading", "ready"}:
        return value.lower()
    if isinstance(value, dict):
        state = value.get("state") or value.get("mcp")
        if isinstance(state, str) and state.lower() in {"loading", "ready"}:
            return state.lower()
    return "ready"


def _tool_denial_reason(
    name: str,
    tool: Tool,
    *,
    strategy: str,
    constraints: DelegationConstraints,
) -> str | None:
    if strategy == "batch_files" and name not in _BATCH_FILES_ALLOWED_TOOLS:
        return "not_allowed_by_strategy"

    metadata = _tool_capability_metadata(name, tool)
    if metadata is None:
        if (
            constraints.read_only
            or not constraints.network
            or not constraints.external_side_effect
            or constraints.write_scope is not None
        ):
            return "unknown_capability_metadata"
        return None

    if constraints.read_only and (metadata.write or metadata.process):
        return "read_only"
    if not constraints.network and metadata.network:
        return "network_disabled"
    if not constraints.external_side_effect and metadata.external_side_effect:
        return "external_side_effect_disabled"
    if constraints.write_scope is not None and metadata.write and name not in {
        "write_file",
        "append_file",
        "edit_file",
    }:
        return "write_scope_unenforceable"
    return None


class CapabilityResolver:
    """Resolve a validated spec against live tools and the current SkillLoader."""

    def resolve(
        self,
        spec: DelegationSpec,
        *,
        parent_tools: Mapping[str, Tool],
        skill_loader: Any | None = None,
        capability_state: Any = "ready",
    ) -> ResolvedCapabilityBundle | CapabilityFailure:
        skills_or_failure = self._resolve_skills(spec, skill_loader)
        if isinstance(skills_or_failure, CapabilityFailure):
            return skills_or_failure
        skills = skills_or_failure

        skill_added: set[str] = set()
        for skill in skills:
            for tool_name in skill.allowed_tools or []:
                if isinstance(tool_name, str) and tool_name.strip():
                    skill_added.add(tool_name.strip())

        requested_required = set(spec.required_tools)
        requested_optional = set(spec.optional_tools)
        if "sub_agent" in requested_required:
            return CapabilityFailure(
                code="CAPABILITY_CONSTRAINT_CONFLICT",
                message="Recursive sub-agent delegation is not allowed.",
                retryable=True,
                details={
                    "tool": "sub_agent",
                    "denied_reason": "recursive_delegation_disabled",
                    "constraints": spec.constraints.to_dict(),
                },
            )
        candidates = requested_required | requested_optional | skill_added

        resolved: dict[str, Tool] = {}
        denied: list[dict[str, str]] = []
        if "sub_agent" in candidates:
            origin = "optional" if "sub_agent" in requested_optional else "skill"
            denied.append(
                {
                    "name": "sub_agent",
                    "origin": origin,
                    "reason": "recursive_delegation_disabled",
                }
            )
            candidates.discard("sub_agent")
        mcp_state = _mcp_state(capability_state)

        for name in sorted(candidates):
            tool = parent_tools.get(name)
            origin = (
                "required"
                if name in requested_required
                else "optional"
                if name in requested_optional
                else "skill"
            )
            if tool is None:
                reason = (
                    "not_ready"
                    if name not in BUILTIN_TOOL_CAPABILITIES and mcp_state == "loading"
                    else "not_found"
                )
                denied.append({"name": name, "origin": origin, "reason": reason})
                if name in requested_required:
                    if reason == "not_ready":
                        return CapabilityFailure(
                            code="REQUIRED_TOOL_NOT_READY",
                            message=f"Required tool '{name}' is not ready while MCP capabilities are loading.",
                            retryable=True,
                            pending_source="mcp",
                            details={"tool": name},
                        )
                    return CapabilityFailure(
                        code="REQUIRED_TOOL_NOT_FOUND",
                        message=f"Required tool '{name}' is not available in the parent session.",
                        retryable=False,
                        details={"tool": name},
                    )
                continue

            reason = _tool_denial_reason(
                name,
                tool,
                strategy=spec.strategy,
                constraints=spec.constraints,
            )
            if reason:
                denied.append({"name": name, "origin": origin, "reason": reason})
                if name in requested_required:
                    return CapabilityFailure(
                        code="CAPABILITY_CONSTRAINT_CONFLICT",
                        message=f"Required tool '{name}' conflicts with the delegation constraints.",
                        retryable=True,
                        details={
                            "tool": name,
                            "denied_reason": reason,
                            "constraints": spec.constraints.to_dict(),
                        },
                    )
                continue
            resolved[name] = tool

        return ResolvedCapabilityBundle(
            spec=spec,
            tools={name: resolved[name] for name in sorted(resolved)},
            skills=tuple(sorted(skills, key=lambda skill: skill.name)),
            skill_added_tools=tuple(sorted(skill_added - requested_required - requested_optional)),
            denied_tools=tuple(denied),
        )

    def _resolve_skills(
        self,
        spec: DelegationSpec,
        skill_loader: Any | None,
    ) -> tuple[Any, ...] | CapabilityFailure:
        if not spec.skill_names:
            return ()
        if skill_loader is None:
            return CapabilityFailure(
                code="SKILL_PROVIDER_UNAVAILABLE",
                message="This sub-agent declared Skills, but no live SkillLoader is available.",
                retryable=False,
            )

        try:
            skill_loader.maybe_reload()
        except Exception as exc:
            return CapabilityFailure(
                code="SKILL_PROVIDER_UNAVAILABLE",
                message=f"The live SkillLoader could not be refreshed: {exc}",
                retryable=True,
            )

        resolved: dict[str, Any] = {}
        visiting: list[str] = []

        def visit(name: str) -> CapabilityFailure | None:
            if name in resolved:
                return None
            if name in visiting:
                cycle = visiting[visiting.index(name) :] + [name]
                return CapabilityFailure(
                    code="SKILL_DEPENDENCY_CYCLE",
                    message="Selected Skill dependencies contain a cycle.",
                    retryable=False,
                    details={"cycle": cycle},
                )

            skill = skill_loader.get_skill(name)
            if skill is None:
                disabled = skill_loader.get_skill(name, include_disabled=True)
                if disabled is not None:
                    return CapabilityFailure(
                        code="SKILL_DISABLED",
                        message=f"Required Skill '{name}' is disabled.",
                        retryable=False,
                        details={"skill": name},
                    )
                return CapabilityFailure(
                    code="SKILL_NOT_FOUND",
                    message=f"Required Skill '{name}' was not found.",
                    retryable=False,
                    details={"skill": name},
                )
            if getattr(skill, "broken", False):
                return CapabilityFailure(
                    code="SKILL_BROKEN",
                    message=f"Required Skill '{name}' is malformed and cannot be loaded.",
                    retryable=False,
                    details={
                        "skill": name,
                        "reason": getattr(skill, "broken_reason", None),
                    },
                )

            visiting.append(name)
            for dependency in sorted(set(skill.required_skills or [])):
                failure = visit(dependency)
                if failure is not None:
                    return failure
            visiting.pop()
            resolved[name] = skill
            return None

        for name in spec.skill_names:
            failure = visit(name)
            if failure is not None:
                return failure
        return tuple(resolved.values())
