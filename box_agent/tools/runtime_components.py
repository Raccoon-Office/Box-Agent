"""Structured missing-component results for host-managed installation recovery."""

from .base import ToolResult


def runtime_component_required(component_id: str, *, code: str, message: str) -> ToolResult:
    """Report a requirement; the host owns supported installers and user consent."""
    error = f"{code}: {message}"
    return ToolResult(
        success=False,
        error=error,
        raw_output={
            "type": "runtime_component_required", "schema_version": 1,
            "component_id": component_id, "code": code,
        },
        model_context=(
            f"{error}\nThe host must offer installation of the required component. "
            "Wait for the user to install it and retry. Do not install dependencies with other tools."
        ),
    )
