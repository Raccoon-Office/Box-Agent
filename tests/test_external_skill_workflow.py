from __future__ import annotations

from pathlib import Path

from box_agent.config import ToolLimitsConfig
from box_agent.tools.base import ToolResult
from box_agent.tools.skill_loader import Skill, SkillLoader
from box_agent.workflows.external_skill import (
    EXTERNAL_SKILL_WORKFLOW_KIND,
    ExternalSkillRunPolicy,
    build_external_skill_completion_gate,
    explicit_skill_invocation_name,
    infer_skill_delivery_globs,
    resolve_explicit_skill_invocation,
)


def _skill(tmp_path: Path, **overrides: object) -> Skill:
    values: dict[str, object] = {
        "name": "ppt-master",
        "description": "Generate editable PPTX presentations.",
        "content": "Follow the presentation workflow.",
        "source": "user",
        "skill_path": tmp_path / "ppt-master" / "SKILL.md",
    }
    values.update(overrides)
    return Skill(**values)  # type: ignore[arg-type]


def test_explicit_skill_invocation_accepts_an_exact_standalone_slash_token() -> None:
    assert explicit_skill_invocation_name("/ppt-master 内马尔") == "ppt-master"
    assert explicit_skill_invocation_name("  /PPT.Master\n执行") == "PPT.Master"
    assert explicit_skill_invocation_name("请用 /ppt-master 制作") == "ppt-master"
    assert explicit_skill_invocation_name("/ppt-master-extra") == "ppt-master-extra"
    assert explicit_skill_invocation_name("https://ppt-master.example.com") is None
    assert explicit_skill_invocation_name("/ppt-master/project/file") is None


def test_resolve_explicit_skill_requires_an_installed_nonbroken_skill(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path)
    loader.loaded_skills["ppt-master"] = _skill(tmp_path)
    loader.loaded_skills["broken"] = _skill(
        tmp_path,
        name="broken",
        broken=True,
    )

    assert resolve_explicit_skill_invocation(loader, "/PPT-MASTER topic") is not None
    assert resolve_explicit_skill_invocation(loader, "/missing topic") is None
    assert resolve_explicit_skill_invocation(loader, "/broken topic") is None


def test_delivery_contract_is_inferred_only_from_static_authoring_metadata(
    tmp_path: Path,
) -> None:
    assert infer_skill_delivery_globs(_skill(tmp_path)) == ("output/**/*.pptx",)
    assert (
        infer_skill_delivery_globs(
            _skill(
                tmp_path,
                description="Explain PPTX file internals without creating files.",
            )
        )
        == ()
    )
    assert infer_skill_delivery_globs(
        _skill(
            tmp_path,
            name="video-maker",
            description="Render an MP4 video and export a ZIP package.",
        )
    ) == ("output/**/*.mp4", "output/**/*.zip")


def test_external_skill_gate_has_bounded_host_lifecycle(tmp_path: Path) -> None:
    gate = build_external_skill_completion_gate(
        user_text="/ppt-master 内马尔最辉煌的赛季",
        workspace_dir=tmp_path,
        skill=_skill(tmp_path),
    )

    assert gate.workflow_checkpoint_kind == EXTERNAL_SKILL_WORKFLOW_KIND
    assert gate.required_changed_artifact_globs == ("output/**/*.pptx",)
    assert gate.max_tool_calls == 128
    assert gate.completion_reserve_tool_calls == 10
    assert gate.pause_tools == frozenset({"request_user_input", "request_user_decision"})
    assert gate.workflow_options["skill_name"] == "ppt-master"
    assert gate.workflow_options["skill_source"] == "user"


def test_external_skill_gate_uses_configured_tool_limits(tmp_path: Path) -> None:
    gate = build_external_skill_completion_gate(
        user_text="/ppt-master 内马尔最辉煌的赛季",
        workspace_dir=tmp_path,
        skill=_skill(tmp_path),
        tool_limits=ToolLimitsConfig(
            external_skill={
                "max_tool_calls": 96,
                "completion_reserve_calls": 18,
            }
        ),
    )

    assert gate.max_tool_calls == 96
    assert gate.completion_reserve_tool_calls == 18


def test_policy_tracks_only_existing_workspace_or_skill_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skill_root = tmp_path / "skills" / "ppt-master"
    workspace.mkdir()
    skill_root.mkdir(parents=True)
    project = skill_root / "projects" / "neymar"
    project.mkdir(parents=True)
    render_script = project / "render.js"
    render_script.write_text("// render\n", encoding="utf-8")
    workspace_file = workspace / "output" / "deck.pptx"
    workspace_file.parent.mkdir()
    workspace_file.write_bytes(b"pptx")
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    policy = ExternalSkillRunPolicy(
        workspace_dir=str(workspace),
        artifact_root_dir=None,
        skill_name="ppt-master",
        skill_source="user",
        skill_root=str(skill_root),
        task_text="topic",
        artifact_globs=("output/**/*.pptx",),
    )

    policy.record_tool_result(
        "bash",
        {"command": f"node {render_script}"},
        ToolResult(
            success=False,
            content=f"created {workspace_file}; ignored {outside}",
            error="browser dependency missing",
        ),
    )

    assert str(render_script) in policy.observed_paths
    assert str(workspace_file) in policy.observed_paths
    assert str(outside) not in policy.observed_paths
    assert policy.last_failures == ["bash: browser dependency missing"]
    checkpoint = policy.build_checkpoint()
    assert "request_user_input" in checkpoint
    assert "request_user_decision" in checkpoint
    assert "publish final user-facing files to artifact_root" in checkpoint
