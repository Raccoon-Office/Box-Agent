from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from box_agent.workspace_registry import (
    WorkspaceRegistry,
    WorkspaceRegistryError,
    default_workspace_registry_path,
)


def test_default_registry_path_uses_box_agent_config_directory(tmp_path: Path) -> None:
    assert default_workspace_registry_path(tmp_path) == (
        tmp_path / ".box-agent" / "config" / "workspaces.json"
    )


def test_registry_persists_all_workspaces_and_normalizes_paths(tmp_path: Path) -> None:
    registry_path = tmp_path / "config" / "workspaces.json"
    registry = WorkspaceRegistry(registry_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    registry.set(f"{first}/", "code")
    registry.set(second, "general")

    reloaded = WorkspaceRegistry(registry_path)
    assert reloaded.get(first) is not None
    assert reloaded.get(first).task_type == "code"
    assert reloaded.get(second).task_type == "general"
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert {item["path"] for item in payload["workspaces"]} == {
        os.path.normcase(str(first)),
        os.path.normcase(str(second)),
    }


def test_registry_updates_one_workspace_without_dropping_other_metadata(tmp_path: Path) -> None:
    registry_path = tmp_path / "workspaces.json"
    workspace = tmp_path / "project"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "future_top_level": True,
                "workspaces": [
                    {
                        "path": str(workspace),
                        "task_type": "general",
                        "updated_at": "old",
                        "future_field": "kept",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    WorkspaceRegistry(registry_path).set(workspace, "code")

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["future_top_level"] is True
    assert payload["workspaces"][0]["future_field"] == "kept"
    assert payload["workspaces"][0]["task_type"] == "code"


def test_registry_refuses_to_overwrite_invalid_json(tmp_path: Path) -> None:
    registry_path = tmp_path / "workspaces.json"
    registry_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(WorkspaceRegistryError, match="invalid workspace config"):
        WorkspaceRegistry(registry_path).set(tmp_path / "project", "code")

    assert registry_path.read_text(encoding="utf-8") == "not-json"
