"""Structured missing-component results for host-managed installation recovery."""

import os

from .base import ToolResult


def plugin_skill_requirement(
    name: str, loader, *, allow_setup: bool = True, skip_video: bool = False,
) -> ToolResult | None:
    """The desktop opts in; CLI and older hosts keep their existing Skill behavior."""
    if name != "hyperframes-video" or (not skip_video and "BOX_AGENT_PLUGIN_SKILLS_DIR" not in os.environ):
        return None
    skill = loader.get_skill(name)
    if (not skip_video and os.environ.get("BOX_AGENT_VIDEO_RUNTIME_READY") == "1" and skill is not None
            and skill.source == "plugin" and not skill.broken):
        return None
    if skip_video or not allow_setup:
        return ToolResult(
            success=False,
            error="Local video generation is unavailable for this continuation.",
            model_context="Continue the original task with ready tools. Finish any independent work, "
                          "such as the script and storyboard, and explain remaining limits. Do not request setup again.",
        )
    return runtime_component_required(
        "video-generation", code="VIDEO_GENERATION_SETUP_REQUIRED",
        message="Video generation requires its host runtime and plugin-owned hyperframes-video Skill.",
        guidance=("The host will offer installation or repair of the video plugin after this turn. "
                  "Explain the requirement briefly, end this turn and wait for the user to install it. "
                  "Do not install packages, fetch replacement Skills, or render through another tool. "
                  "After the host confirms readiness, continue the original task using existing work."),
    )


def runtime_component_required(
    component_id: str, *, code: str, message: str, guidance: str | None = None,
) -> ToolResult:
    """Report a requirement; the host owns supported installers and user consent."""
    error = f"{code}: {message}"
    guidance = guidance or (
        "The host must offer installation of the required component. "
        "Wait for the user to install it and retry. Do not install dependencies with other tools."
    )
    return ToolResult(
        success=False,
        error=error,
        raw_output={
            "type": "runtime_component_required", "schema_version": 1,
            "component_id": component_id, "code": code,
        },
        model_context=f"{error}\n{guidance}",
    )
