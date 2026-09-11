"""Capability resolution for delegated sub-agent tasks.

The public request stays intentionally small.  This module normalizes it,
derives a fail-closed child policy, and intersects the requested capabilities
with the parent's live tool map and selected Skills.

It deliberately does not execute tools or call an LLM.  The resolved bundle
contains the original live tool instances so their existing PermissionEngine
checks remain the final resource-level authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .. import skill_dependencies
from ..config import ToolLimitsConfig
from .base import Tool

_DEFAULT_SUB_AGENT_LIMITS = ToolLimitsConfig().sub_agent
GENERAL_LOOP_MAX_STEPS = _DEFAULT_SUB_AGENT_LIMITS.general_max_steps
GENERAL_LOOP_MAX_TOOL_CALLS = _DEFAULT_SUB_AGENT_LIMITS.general_max_tool_calls
BATCH_FILES_MAX_FILES = 32
BATCH_FILES_MAX_STEPS = 1
BATCH_FILE_MAX_CHARS = 64_000
BATCH_AGGREGATE_MAX_CHARS = 200_000

_BATCH_FILES_ALLOWED_TOOLS = frozenset({"read_file"})
DEFAULT_SAFE_TOOL_NAMES = frozenset({"query_jsonl", "read_file", "rg", "search_files"})
PATH_SCOPED_WRITE_TOOLS = frozenset({"append_file", "edit_file", "write_file"})
TRUSTED_NETWORK_TOOL_NAMES = frozenset(
    {"generate_image", "inspect_images", "web_extract", "web_search"}
)
TRUSTED_UNSCOPED_WRITE_TOOLS = frozenset({"generate_image"})
PERMISSION_GATED_PROCESS_TOOL_NAMES = frozenset({"bash"})

MINIMAL_DELEGATION_EXAMPLE: dict[str, Any] = {
    "task": "Read the provided files and summarize them.",
    "required_tools": ["read_file"],
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
    "rg": ToolCapabilityMetadata(read=True),
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
    "inspect_images": ToolCapabilityMetadata(read=True, network=True),
    "generate_image": ToolCapabilityMetadata(write=True, network=True),
    "get_skill": ToolCapabilityMetadata(read=True),
    "list_skills": ToolCapabilityMetadata(read=True),
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
        "managed_browser_console_messages",
        "managed_browser_navigate",
        "managed_browser_navigate_back",
        "managed_browser_network_requests",
        "managed_browser_snapshot",
        "managed_browser_wait_for",
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
    network: bool = False
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
    skill_names: tuple[str, ...]
    files: tuple[str, ...]
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
            field_corrections: dict[str, Any] = {}
            if any(field == "budget" or field.startswith("budget.") for field in self.invalid_fields):
                field_corrections["budget"] = {
                    "message": "Pass budget as a JSON object, never as a JSON string.",
                    "example": {"max_steps": 12, "max_tool_calls": 25},
                }
            if any(
                field == "write_scope" or field.startswith("write_scope[")
                for field in self.invalid_fields
            ):
                field_corrections["write_scope"] = {
                    "message": (
                        "Path-based write tools require an exact, non-empty output "
                        "scope for this child. Parallel children need disjoint scopes."
                    ),
                    "example": ["research/dim01.md"],
                }
            if field_corrections:
                payload["field_corrections"] = field_corrections
        elif self.code == "CAPABILITY_CONSTRAINT_CONFLICT" and self.details:
            denied_reason = self.details.get("denied_reason")
            denied_tool = self.details.get("tool")
            if denied_reason == "write_scope_required" and denied_tool in PATH_SCOPED_WRITE_TOOLS:
                payload["correction_hint"] = (
                    "Retry once with an exact session-cwd-relative write_scope for "
                    "only this child's output. Parallel children must use disjoint scopes."
                )
        return payload


@dataclass(frozen=True)
class ResolvedCapabilityBundle:
    spec: DelegationSpec
    tools: dict[str, Tool]
    skills: tuple[Any, ...]
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
            "requested_tools": list(self.spec.required_tools),
            "resolved_tools": list(self.resolved_tool_names),
            "denied_tools": [dict(item) for item in self.denied_tools],
            "requested_skills": list(self.spec.skill_names),
            "resolved_skills": list(self.resolved_skill_names),
            "files": list(self.spec.files),
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
    title: Any = "",
    skills: Any = None,
    required_tools: Any = None,
    files: Any = None,
    write_scope: Any = None,
    budget: Any = None,
    default_required_tools: tuple[str, ...] = (),
    general_max_steps: int = GENERAL_LOOP_MAX_STEPS,
    general_max_tool_calls: int = GENERAL_LOOP_MAX_TOOL_CALLS,
) -> DelegationSpec | CapabilityFailure:
    """Validate a flat request and derive its internal child policy."""

    invalid_fields: list[str] = []
    defaults_applied: list[str] = []

    if not isinstance(task, str) or not task.strip():
        invalid_fields.append("task")
        normalized_task = ""
    else:
        normalized_task = task.strip()

    if title is None:
        title = ""
    if not isinstance(title, str):
        invalid_fields.append("title")
        normalized_title = None
    else:
        normalized_title = title.strip() if isinstance(title, str) and title.strip() else None
        if normalized_title is None:
            defaults_applied.append("title")

    if skills is None:
        skills = []
        defaults_applied.append("skills")
    skill_names = _normalized_string_list(
        skills,
        field_name="skills",
        invalid_fields=invalid_fields,
    )

    if files is None:
        normalized_files = ()
    else:
        normalized_files = _normalized_string_list(
            files,
            field_name="files",
            invalid_fields=invalid_fields,
        )
        if not normalized_files:
            invalid_fields.append("files")
        if len(normalized_files) > BATCH_FILES_MAX_FILES:
            invalid_fields.append("files")
    if required_tools is None:
        normalized_required_tools = (
            ("read_file",)
            if normalized_files
            else tuple(
                sorted(set(default_required_tools) & DEFAULT_SAFE_TOOL_NAMES)
            )
        )
        defaults_applied.append("required_tools")
    else:
        normalized_required_tools = _normalized_string_list(
            required_tools,
            field_name="required_tools",
            invalid_fields=invalid_fields,
        )
    # ``files`` is neutral task input for both execution paths. Keep the
    # bounded one-shot optimization only for file-only reads; any additional
    # capability requires the ordinary child agent loop.
    strategy = (
        "batch_files"
        if normalized_files and set(normalized_required_tools) == {"read_file"}
        else "general_loop"
    )
    if "sub_agent" in normalized_required_tools:
        invalid_fields.append("required_tools")
    if strategy == "batch_files" and set(normalized_required_tools) != {"read_file"}:
        invalid_fields.append("required_tools")

    if write_scope is None:
        normalized_write_scope = None
    elif isinstance(write_scope, str):
        if write_scope.strip():
            normalized_write_scope = (write_scope.strip(),)
        else:
            invalid_fields.append("write_scope")
            normalized_write_scope = None
    elif isinstance(write_scope, list):
        normalized_write_scope = _normalized_string_list(
            write_scope,
            field_name="write_scope",
            invalid_fields=invalid_fields,
            required_non_empty=True,
        )
    else:
        invalid_fields.append("write_scope")
        normalized_write_scope = None

    requested_path_writes = set(normalized_required_tools) & PATH_SCOPED_WRITE_TOOLS
    if requested_path_writes and not normalized_write_scope:
        invalid_fields.append("write_scope")
    if normalized_write_scope and not requested_path_writes:
        invalid_fields.append("write_scope")
    if strategy == "batch_files" and normalized_write_scope:
        invalid_fields.append("write_scope")

    parsed_constraints = DelegationConstraints(
        read_only=not bool(
            requested_path_writes
            or set(normalized_required_tools) & TRUSTED_UNSCOPED_WRITE_TOOLS
            or set(normalized_required_tools) & PERMISSION_GATED_PROCESS_TOOL_NAMES
        ),
        network=bool(
            set(normalized_required_tools)
            & (
                TRUSTED_NETWORK_TOOL_NAMES
                | _PLAYWRIGHT_READ_ONLY_TOOLS
                | PERMISSION_GATED_PROCESS_TOOL_NAMES
            )
        ),
        write_scope=normalized_write_scope,
        external_side_effect=False,
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
        if "max_steps" not in budget:
            defaults_applied.append("budget.max_steps")
        raw_steps = budget.get("max_steps", BATCH_FILES_MAX_STEPS)
        if not isinstance(raw_steps, int) or isinstance(raw_steps, bool) or raw_steps < 1:
            invalid_fields.append("budget.max_steps")
            raw_steps = BATCH_FILES_MAX_STEPS
        max_steps = min(raw_steps, BATCH_FILES_MAX_STEPS)

        if "max_tool_calls" not in budget:
            defaults_applied.append("budget.max_tool_calls")
        raw_tool_calls = budget.get("max_tool_calls", len(normalized_files))
        if (
            not isinstance(raw_tool_calls, int)
            or isinstance(raw_tool_calls, bool)
            or raw_tool_calls < len(normalized_files)
        ):
            invalid_fields.append("budget.max_tool_calls")
            raw_tool_calls = len(normalized_files)
        max_tool_calls = min(
            raw_tool_calls,
            len(normalized_files),
            BATCH_FILES_MAX_FILES,
        )
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
        required_tools=normalized_required_tools,
        skill_names=skill_names,
        files=normalized_files,
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
    permission_negotiator_available: bool,
) -> str | None:
    if strategy == "batch_files" and name not in _BATCH_FILES_ALLOWED_TOOLS:
        return "not_allowed_by_strategy"

    metadata = _tool_capability_metadata(name, tool)
    if metadata is None:
        return "unknown_capability_metadata"

    permission_gated_process = name in PERMISSION_GATED_PROCESS_TOOL_NAMES
    if metadata.process:
        if not permission_gated_process:
            return "process_not_delegable"
        if not permission_negotiator_available:
            return "permission_negotiator_unavailable"

    if constraints.read_only and (metadata.write or metadata.process):
        return "read_only"
    if not constraints.network and metadata.network:
        return "network_disabled"
    if not constraints.external_side_effect and metadata.external_side_effect:
        return "external_side_effect_disabled"
    if (
        constraints.write_scope is not None
        and metadata.write
        and not permission_gated_process
        and name not in {"write_file", "append_file", "edit_file"}
    ):
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
        skill_access_filter: Callable[[Any], bool] | None = None,
        capability_state: Any = "ready",
        permission_negotiator_available: bool = False,
    ) -> ResolvedCapabilityBundle | CapabilityFailure:
        skills_or_failure = self._resolve_skills(spec, skill_loader, skill_access_filter)
        if isinstance(skills_or_failure, CapabilityFailure):
            return skills_or_failure
        skills = skills_or_failure

        requested_required = set(spec.required_tools)
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
        candidates = set(requested_required)

        resolved: dict[str, Tool] = {}
        denied: list[dict[str, str]] = []
        mcp_state = _mcp_state(capability_state)

        for name in sorted(candidates):
            tool = parent_tools.get(name)
            origin = "required"
            if tool is None:
                reason = (
                    "not_ready"
                    if name not in BUILTIN_TOOL_CAPABILITIES and mcp_state == "loading"
                    else "not_found"
                )
                denied.append({"name": name, "origin": origin, "reason": reason})
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

            reason = _tool_denial_reason(
                name,
                tool,
                strategy=spec.strategy,
                constraints=spec.constraints,
                permission_negotiator_available=permission_negotiator_available,
            )
            if reason:
                denied.append({"name": name, "origin": origin, "reason": reason})
                return CapabilityFailure(
                    code="CAPABILITY_CONSTRAINT_CONFLICT",
                    message=f"Required tool '{name}' conflicts with the derived child policy.",
                    retryable=True,
                    details={
                        "tool": name,
                        "denied_reason": reason,
                        "constraints": spec.constraints.to_dict(),
                    },
                )
            resolved[name] = tool

        return ResolvedCapabilityBundle(
            spec=spec,
            tools={name: resolved[name] for name in sorted(resolved)},
            skills=tuple(sorted(skills, key=lambda skill: skill.name)),
            denied_tools=tuple(denied),
        )

    def _resolve_skills(
        self,
        spec: DelegationSpec,
        skill_loader: Any | None,
        skill_access_filter: Callable[[Any], bool] | None,
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

        try:
            resolved = skill_dependencies.resolve_required_skills(skill_loader, spec.skill_names)
            for skill in resolved:
                if skill_access_filter is not None and not skill_access_filter(skill):
                    return CapabilityFailure(
                        code="SKILL_NOT_ENABLED_FOR_CONVERSATION",
                        message=f"Required Skill '{skill.name}' is not enabled for this conversation.",
                        retryable=False, details={"skill": skill.name},
                    )
            return resolved
        except skill_dependencies.SkillDependencyError as exc:
            return CapabilityFailure(
                code=exc.code,
                message=exc.message,
                retryable=False,
                details=exc.details,
            )
