"""Host-neutral workflow policies composed by the shared runtime."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..config import ToolLimitsConfig
from ..loop_guards import CompletionGate
from ..workflow_policy import WorkflowPolicy
from ..workflow_checkpoint_store import load_workflow_checkpoint
from .controlled_presentation import RESEARCH_ROUND_LIMIT, ControlledPresentationPolicy
from .external_skill import (
    EXTERNAL_SKILL_WORKFLOW_KIND,
    ExternalSkillRunPolicy,
    build_external_skill_completion_gate,
    build_external_skill_completion_gate_from_options,
    external_skill_policy_from_options,
    resolve_explicit_skill_invocation,
)
from .presentation_contract import (
    IMAGE_GENERATION_POLICY_OPTION,
    RESEARCH_MODE_OPTION,
    RESEARCH_ROUND_LIMIT_OPTION,
    WORKFLOW_KIND as CONTROLLED_PRESENTATION_WORKFLOW_KIND,
)
from .presentation_preflight import (
    build_presentation_preflight_analysis_text,
    build_presentation_preflight_result,
    build_presentation_recommendation_prompt,
    load_presentation_preflight_config,
)
from .presentation_provider import (
    parse_host_presentation_config,
    resolve_presentation_skill_provider,
)


def create_workflow_policy(
    *,
    workflow_kind: str | None,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
    workflow_options: Mapping[str, Any] | None = None,
    available_tool_names: frozenset[str] | None = None,
) -> WorkflowPolicy | None:
    """Create a per-run policy without exposing implementations to the kernel."""
    if workflow_kind == ControlledPresentationPolicy.kind:
        research_mode = (workflow_options or {}).get(RESEARCH_MODE_OPTION)
        research_round_limit = (workflow_options or {}).get(
            RESEARCH_ROUND_LIMIT_OPTION
        )
        image_generation_policy = (workflow_options or {}).get(
            IMAGE_GENERATION_POLICY_OPTION
        )
        policy = ControlledPresentationPolicy(
            workspace_dir=workspace_dir,
            artifact_root_dir=artifact_root_dir,
            research_mode=(
                research_mode if isinstance(research_mode, str) else None
            ),
            research_round_limit=(
                research_round_limit
                if isinstance(research_round_limit, int)
                and not isinstance(research_round_limit, bool)
                and research_round_limit > 0
                else RESEARCH_ROUND_LIMIT
            ),
            image_generation_policy=(
                image_generation_policy
                if isinstance(image_generation_policy, str)
                else None
            ),
            available_tool_names=available_tool_names,
        )
        resume_checkpoint = load_workflow_checkpoint(
            workspace_dir=workspace_dir,
            workflow_kind=workflow_kind,
        )
        if resume_checkpoint is not None:
            policy.attach_resume_checkpoint(resume_checkpoint)
        return policy
    if workflow_kind == EXTERNAL_SKILL_WORKFLOW_KIND:
        policy = external_skill_policy_from_options(
            workspace_dir=workspace_dir,
            artifact_root_dir=artifact_root_dir,
            workflow_options=workflow_options,
        )
        resume_checkpoint = load_workflow_checkpoint(
            workspace_dir=workspace_dir,
            workflow_kind=workflow_kind,
        )
        if resume_checkpoint is not None:
            policy.attach_resume_checkpoint(resume_checkpoint)
        return policy
    return None


def recover_completion_gate(
    workspace_dir: str | Path,
    tool_limits: ToolLimitsConfig | None = None,
) -> CompletionGate | None:
    """Recover the first incomplete built-in workflow from durable artifacts."""
    from .presentation_recovery import recover_presentation_completion_gate

    controlled = recover_presentation_completion_gate(
        workspace_dir,
        tool_limits=tool_limits,
    )
    if controlled is not None:
        return controlled
    checkpoint = load_workflow_checkpoint(
        workspace_dir=workspace_dir,
        workflow_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
    )
    if checkpoint is None:
        return None
    return build_external_skill_completion_gate_from_options(
        workspace_dir=workspace_dir,
        workflow_options=checkpoint.workflow_options,
        tool_limits=tool_limits,
    )


__all__ = [
    "ControlledPresentationPolicy",
    "CONTROLLED_PRESENTATION_WORKFLOW_KIND",
    "EXTERNAL_SKILL_WORKFLOW_KIND",
    "ExternalSkillRunPolicy",
    "build_external_skill_completion_gate",
    "build_presentation_preflight_analysis_text",
    "build_presentation_preflight_result",
    "build_presentation_recommendation_prompt",
    "create_workflow_policy",
    "load_presentation_preflight_config",
    "parse_host_presentation_config",
    "recover_completion_gate",
    "resolve_explicit_skill_invocation",
    "resolve_presentation_skill_provider",
]
