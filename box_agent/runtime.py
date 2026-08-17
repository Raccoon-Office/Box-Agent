"""Stable execution and composition boundary for framework consumers.

Application adapters should normally use :class:`box_agent.agent.Agent`.
Framework capabilities that need an isolated message/tool set (for example a
sub-agent) may use ``run_agent_loop`` from this module.  Keeping the import
here prevents integrations from depending on the implementation module
``box_agent.core`` directly. Optional workflow policies are selected here so
the kernel depends only on their stable contract, not concrete workflows.
"""

from collections.abc import AsyncIterator
from functools import wraps
from typing import Any

from .core import run_agent_loop as _run_agent_loop
from .events import AgentEvent
from .loop_guards import CompletionGate
from .workflows import create_workflow_policy


@wraps(_run_agent_loop)
def run_agent_loop(**kwargs: Any) -> AsyncIterator[AgentEvent]:
    """Run the kernel with any completion-gate workflow policy composed in."""
    if kwargs.get("workflow_policy") is None:
        completion_gate = kwargs.get("completion_gate")
        kwargs["workflow_policy"] = create_workflow_policy(
            workflow_kind=(
                completion_gate.workflow_checkpoint_kind
                if completion_gate is not None
                else None
            ),
            workspace_dir=kwargs.get("workspace_dir"),
            artifact_root_dir=kwargs.get("artifact_root_dir"),
            workflow_options=(
                completion_gate.workflow_options
                if completion_gate is not None
                else None
            ),
            available_tool_names=frozenset(kwargs.get("tools", {})),
        )
    return _run_agent_loop(**kwargs)

__all__ = ["CompletionGate", "run_agent_loop"]
