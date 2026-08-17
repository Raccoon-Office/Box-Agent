from __future__ import annotations

import json
from pathlib import Path

from box_agent.workflow_checkpoint_store import (
    CHECKPOINT_SCHEMA_VERSION,
    clear_workflow_checkpoint,
    load_workflow_checkpoint,
    save_workflow_checkpoint,
)
from box_agent.workflow_policy import WorkflowCheckpointUpdate
from box_agent.workflows import (
    EXTERNAL_SKILL_WORKFLOW_KIND,
    create_workflow_policy,
    recover_completion_gate,
)


class _PresentationPolicy:
    kind = "controlled_presentation"
    stage = "outline"

    def build_checkpoint(self) -> str:
        return "CONTROLLED_PRESENTATION_STAGE=outline"

    def update_checkpoint(self, text: str) -> WorkflowCheckpointUpdate:
        return WorkflowCheckpointUpdate(text=text, changed=True)


def test_checkpoint_save_is_atomic_versioned_and_loadable(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    artifact = output / "outline.json"
    artifact.write_text('{"slides": []}\n', encoding="utf-8")

    saved = save_workflow_checkpoint(
        _PresentationPolicy(),
        workspace_dir=tmp_path,
        artifact_root_dir=None,
    )

    assert saved is not None
    assert saved.schema_version == CHECKPOINT_SCHEMA_VERSION
    assert saved.stage == "outline"
    assert saved.artifact_count == 1
    assert Path(saved.path).is_file()
    assert not list(Path(saved.path).parent.glob("*.tmp"))
    loaded = load_workflow_checkpoint(
        workspace_dir=tmp_path,
        workflow_kind="controlled_presentation",
    )
    assert loaded == saved
    assert clear_workflow_checkpoint(
        workspace_dir=tmp_path,
        workflow_kind="controlled_presentation",
    )
    assert not Path(saved.path).exists()


def test_checkpoint_load_rejects_corruption_and_workspace_mismatch(tmp_path: Path) -> None:
    tmp_path.joinpath("output").mkdir()
    saved = save_workflow_checkpoint(
        _PresentationPolicy(),
        workspace_dir=tmp_path,
        artifact_root_dir=None,
    )
    assert saved is not None
    path = Path(saved.path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["workspace_identity"] = "wrong"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        load_workflow_checkpoint(
            workspace_dir=tmp_path,
            workflow_kind="controlled_presentation",
        )
        is None
    )


def test_unregistered_workflow_cannot_install_an_executable_adapter(tmp_path: Path) -> None:
    class ThirdPartyPolicy:
        kind = "third_party_skill_sidecar"

    assert (
        save_workflow_checkpoint(
            ThirdPartyPolicy(),
            workspace_dir=tmp_path,
            artifact_root_dir=None,
        )
        is None
    )


def test_builtin_external_skill_adapter_round_trips_data_only_options(
    tmp_path: Path,
) -> None:
    tmp_path.joinpath("output").mkdir()
    policy = create_workflow_policy(
        workflow_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        workflow_options={
            "skill_name": "ppt-master",
            "skill_source": "user",
            "task_text": "/ppt-master 制作一页 PPT",
            "artifact_globs": '["output/**/*.pptx"]',
            "observed_paths": '["/tmp/project"]',
            "last_failures": '["bash: dependency missing"]',
        },
    )
    assert policy is not None

    saved = save_workflow_checkpoint(
        policy,
        workspace_dir=tmp_path,
        artifact_root_dir=None,
    )

    assert saved is not None
    assert saved.workflow_kind == EXTERNAL_SKILL_WORKFLOW_KIND
    assert saved.adapter_id == "box-agent.external-skill.v1"
    assert saved.workflow_options["skill_name"] == "ppt-master"
    assert "dependency missing" in saved.workflow_options["last_failures"]
    recovered = recover_completion_gate(tmp_path)
    assert recovered is not None
    assert recovered.workflow_checkpoint_kind == EXTERNAL_SKILL_WORKFLOW_KIND
    assert recovered.required_changed_artifact_globs == ("output/**/*.pptx",)
    assert recovered.pause_tools == frozenset({"request_user_input", "request_user_decision"})


def test_new_process_policy_injects_validated_resume_metadata_once(tmp_path: Path) -> None:
    tmp_path.joinpath("output").mkdir()
    saved = save_workflow_checkpoint(
        _PresentationPolicy(),
        workspace_dir=tmp_path,
        artifact_root_dir=None,
    )
    assert saved is not None

    policy = create_workflow_policy(
        workflow_kind="controlled_presentation",
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    assert policy is not None
    first = policy.build_checkpoint()
    second = policy.build_checkpoint()

    assert first is not None
    assert "[BOX_AGENT_WORKFLOW_RESUME]" in first
    assert saved.checkpoint_id in first
    assert second is not None
    assert "[BOX_AGENT_WORKFLOW_RESUME]" not in second
