"""Tests for workflow-policy composition outside the agent kernel."""

import inspect

import box_agent.runtime as runtime_module
from box_agent.core import run_agent_loop as core_run_agent_loop
from box_agent.loop_guards import CompletionGate
from box_agent.runtime import run_agent_loop
from box_agent.workflows import (
    EXTERNAL_SKILL_WORKFLOW_KIND,
    ExternalSkillRunPolicy,
    create_workflow_policy,
)
from box_agent.workflows.controlled_presentation import ControlledPresentationPolicy
from box_agent.workflows.presentation_checkpoint import (
    CONTROLLED_PRESENTATION_CHECKPOINT_MARKER,
)


def test_runtime_bridge_preserves_kernel_signature() -> None:
    assert inspect.signature(run_agent_loop) == inspect.signature(core_run_agent_loop)


def test_runtime_bridge_passes_available_tool_names_to_policy(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_workflow_policy(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(
        runtime_module,
        "create_workflow_policy",
        fake_create_workflow_policy,
    )
    gate = CompletionGate(workflow_checkpoint_kind="controlled_presentation")

    runtime_module.run_agent_loop(
        llm=object(),
        messages=[],
        tools={"web_search": object(), "write_file": object()},
        completion_gate=gate,
    )

    assert captured["available_tool_names"] == frozenset(
        {"web_search", "write_file"}
    )


def test_factory_builds_controlled_presentation_policy(tmp_path) -> None:
    policy = create_workflow_policy(
        workflow_kind="controlled_presentation",
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "artifacts",
        workflow_options={"research_mode": "deep"},
        available_tool_names=frozenset({"web_search", "write_file"}),
    )

    assert isinstance(policy, ControlledPresentationPolicy)
    assert policy.research_mode == "deep"
    assert policy.available_tool_names == frozenset({"web_search", "write_file"})


def test_completion_gate_uses_generic_workflow_options_contract(tmp_path) -> None:
    gate = CompletionGate(
        workflow_checkpoint_kind="controlled_presentation",
        workflow_options={"research_mode": "deep"},
    )

    policy = create_workflow_policy(
        workflow_kind=gate.workflow_checkpoint_kind,
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        workflow_options=gate.workflow_options,
    )

    assert "workflow_options" in inspect.signature(CompletionGate).parameters
    assert "presentation_research_mode" not in inspect.signature(
        CompletionGate
    ).parameters
    assert isinstance(policy, ControlledPresentationPolicy)
    assert policy.research_mode == "deep"


def test_factory_ignores_unknown_workflow(tmp_path) -> None:
    policy = create_workflow_policy(
        workflow_kind="third_party_workflow",
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )

    assert policy is None


def test_factory_builds_builtin_policy_for_external_skill(tmp_path) -> None:
    policy = create_workflow_policy(
        workflow_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        workflow_options={
            "skill_name": "ppt-master",
            "skill_source": "user",
            "skill_root": str(tmp_path / "skills" / "ppt-master"),
            "task_text": "/ppt-master 制作一页 PPT",
            "artifact_globs": '["output/**/*.pptx"]',
        },
    )

    assert isinstance(policy, ExternalSkillRunPolicy)
    assert policy.skill_name == "ppt-master"
    assert policy.artifact_globs == ("output/**/*.pptx",)
    assert policy.suppresses_generic_final_summary() is True


def test_checkpoint_update_uses_generic_contract_fields(tmp_path) -> None:
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )

    first = policy.update_checkpoint(
        f"checkpoint\n{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline\n"
    )
    repeated = policy.update_checkpoint(first.text)

    assert first.changed is True
    assert first.recovered_evidence_urls == frozenset()
    assert repeated.changed is False
    assert policy.stage == "outline"
