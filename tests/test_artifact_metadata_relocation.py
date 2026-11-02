"""A relocated global profile must not disable publication or retention guards."""

import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from box_agent import artifact_metadata as storage
from box_agent.artifact_publication import is_publication_metadata, retained_chat_file_in
from box_agent.tools.chat_references import ChatReferences, chat_mutation_error
from box_agent.tools.publish_artifact_tool import PublishArtifactTool


def redirect_directory(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True, text=True)
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture(params=["default", "configured"])
def relocated_profile(tmp_path, monkeypatch, request):
    profile = Path.home() / ".box-agent"
    if request.param == "configured":
        profile = tmp_path / "custom-home" / ".box-agent"
        profile.parent.mkdir()
        monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    target = tmp_path / "data-disk" / "用户数据"
    target.mkdir(parents=True)
    redirect_directory(profile, target)
    return profile, target


@pytest.mark.parametrize("descriptor_io", [True, False])
@pytest.mark.parametrize("location", ["profile-parent", "nested-workspace", "profile-workspace"])
async def test_relocated_profile_allows_publication_and_preserves_retention(
    tmp_path, monkeypatch, relocated_profile, descriptor_io, location,
):
    profile, target = relocated_profile
    if not descriptor_io:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    workspace = {"profile-parent": profile.parent,
                 "nested-workspace": profile.parent / "project",
                 "profile-workspace": profile / "workspace"}[location]
    workspace.mkdir(exist_ok=True)
    report = workspace / "report.txt"
    report.write_text("report")

    result = await PublishArtifactTool(workspace).execute("report.txt")

    assert result.success, result.error
    assert storage.read_metadata(report, workspace)["type"] == "artifact"
    storage.write_scope(workspace, {"default": "intermediate"}, workspace)
    assert storage.read_scope(workspace, workspace) == {"default": "intermediate"}
    marker = storage.metadata_path(report, workspace)
    assert list(storage.iter_metadata_records(workspace)) == [marker]
    if location == "profile-parent":
        assert marker.is_relative_to(target)
    ChatReferences(workspace).retain(report)
    assert storage.retention_directory() == target / "artifact-metadata/retained"
    records = list(storage.iter_retention_records(report))
    assert len(records) == 1
    assert storage.read_retention_record(records[0]) == (report.resolve(), workspace.resolve())
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    assert retained_chat_file_in(report.resolve(), other_workspace) == report.resolve()
    assert chat_mutation_error(report.resolve(), "delete", other_workspace) is not None
    assert chat_mutation_error(records[0], "delete", other_workspace) is not None
    assert storage.is_metadata_record(marker)
    assert is_publication_metadata(marker, workspace)
    assert chat_mutation_error(marker, "delete", workspace) is not None
    storage.remove_metadata(report, workspace)
    assert not records[0].exists()
    assert not marker.exists()
    assert report.read_text() == "report"


@pytest.mark.parametrize("child", ["artifact-metadata", "artifact-metadata/retained"])
def test_relocated_profile_still_rejects_redirected_retention_children(
    tmp_path, relocated_profile, child,
):
    _, target = relocated_profile
    outside = tmp_path / "outside"
    outside.mkdir()
    link = target / child
    link.parent.mkdir(parents=True, exist_ok=True)
    redirect_directory(link, outside)

    with pytest.raises(OSError, match="redirected"):
        storage.write_retention(tmp_path / "report.txt", tmp_path)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("child", ["artifact-metadata", "artifact-metadata/files", "artifact-metadata/scopes"])
def test_relocated_profile_still_rejects_redirected_publication_children(
    tmp_path, relocated_profile, child,
):
    profile, target = relocated_profile
    outside = tmp_path / "outside"
    outside.mkdir()
    link = target / child
    link.parent.mkdir(parents=True, exist_ok=True)
    redirect_directory(link, outside)

    with pytest.raises(OSError, match="redirected"):
        if child.endswith("scopes"):
            storage.write_scope(profile.parent, {"default": "intermediate"}, profile.parent)
        else:
            storage.write_metadata(profile.parent / "report.txt", {"type": "artifact"}, profile.parent)

    assert list(outside.iterdir()) == []


def test_relocated_profile_with_missing_target_is_not_recreated(relocated_profile):
    _, target = relocated_profile
    target.rmdir()

    with pytest.raises(OSError):
        storage.write_retention(target.parent / "report.txt", target.parent)

    assert not target.exists()


@pytest.mark.parametrize("child", [".box-agent", ".box-agent/artifact-metadata/files"])
async def test_relocated_profile_does_not_allow_workspace_metadata_redirects(
    tmp_path, relocated_profile, child,
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    report = workspace / "report.txt"
    report.write_text("report")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / child
    link.parent.mkdir(parents=True, exist_ok=True)
    redirect_directory(link, outside)

    result = await PublishArtifactTool(workspace).execute("report.txt")

    assert not result.success
    assert "redirected" in result.error
    assert list(outside.iterdir()) == []


def test_relocated_profile_rejects_non_directory_target(relocated_profile):
    _, target = relocated_profile
    target.rmdir()
    target.write_text("preserve")

    with pytest.raises(OSError):
        storage.write_retention(target.parent / "report.txt", target.parent)

    assert target.read_text() == "preserve"


def test_relocated_profile_rejects_cyclic_target(relocated_profile):
    from box_agent.tools.engine.artifact_results import _detect_regex_artifacts

    profile, target = relocated_profile
    target.rmdir()
    redirect_directory(target, profile)

    with pytest.raises((OSError, RuntimeError)):
        storage.write_retention(target.parent / "report.txt", target.parent)

    assert list(target.parent.iterdir()) == [target]
    report = profile.parent / "report.json"
    report.write_text("{}")
    assert _detect_regex_artifacts("read", "bash", "[report.json]", None, str(profile.parent))[0] == []


@pytest.mark.parametrize("kind", ["files", "scopes"])
def test_workspace_replacement_cannot_redirect_metadata_writes(tmp_path, monkeypatch, kind):
    if os.open not in os.supports_dir_fd or os.rename not in os.supports_dir_fd:
        pytest.skip("Descriptor-relative IO unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    external_store = outside / ".box-agent/artifact-metadata" / kind
    external_store.mkdir(parents=True)
    original_open = os.open
    swapped = False

    def replace_workspace(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path).is_absolute():
            workspace.rename(tmp_path / "original-workspace")
            workspace.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_workspace)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {replace_workspace})
    with pytest.raises(OSError):
        if kind == "files":
            storage.write_metadata(workspace / "report.txt", {"type": "artifact"}, workspace)
        else:
            storage.write_scope(workspace, {"default": "intermediate"}, workspace)

    assert swapped
    assert list(external_store.iterdir()) == []


def test_relocated_producer_retention_protects_its_record_without_shared_cache(relocated_profile):
    profile, _ = relocated_profile
    report = profile.parent / "report.txt"
    report.write_text("report")
    storage.write_metadata(report, {"retain_for_chat": True}, profile.parent)
    marker = storage.metadata_path(report, profile.parent)

    assert not list(storage.iter_retention_records(report))
    for protected in (report, marker, marker.parent):
        assert chat_mutation_error(protected, "delete", profile.parent) is not None


@pytest.mark.parametrize("operation", ["copy-delete", "rename"])
def test_relocated_profile_keeps_moved_intermediates_private(relocated_profile, operation):
    from box_agent.tools.engine.artifact_results import _detect_regex_artifacts, _snapshot_workspace

    profile, _ = relocated_profile
    workspace = profile.parent
    source, moved = workspace / "private.png", workspace / "copy.png"
    source.write_bytes(b"private")
    storage.write_metadata(source, {"type": "intermediate_asset", "size_bytes": 7,
                                   "sha256": hashlib.sha256(b"private").hexdigest()}, workspace)
    if operation == "rename":
        source.rename(moved)
    else:
        moved.write_bytes(source.read_bytes())
        source.unlink()

    assert storage.metadata_path(source, workspace) in _snapshot_workspace(
        str(workspace), include_publication_metadata=True,
    )
    assert _detect_regex_artifacts("copy", "bash", "[copy.png]", None, str(workspace))[0] == []


@pytest.mark.parametrize("limit", ["files", "time"])
def test_relocated_metadata_scan_remains_bounded(relocated_profile, monkeypatch, limit):
    from box_agent.tools.engine import artifact_results

    profile, _ = relocated_profile
    for name in ("first.png", "second.png"):
        storage.write_metadata(profile.parent / name, {"type": "intermediate_asset"}, profile.parent)
    if limit == "files":
        monkeypatch.setenv("BOX_AGENT_ARTIFACT_SCAN_MAX_FILES", "1")
    else:
        monkeypatch.setenv("BOX_AGENT_ARTIFACT_SCAN_TIMEOUT_SECONDS", "1")
        clock = iter((0, 0, 2))
        monkeypatch.setattr(artifact_results, "perf_counter", lambda: next(clock, 2))

    assert artifact_results._snapshot_workspace(
        str(profile.parent), include_publication_metadata=True,
    ) is None


def test_redirected_nested_store_cannot_expose_copied_intermediate(tmp_path):
    from box_agent.tools.engine.artifact_results import _detect_regex_artifacts

    source = tmp_path / "private.png"
    storage.write_metadata(source, {"type": "intermediate_asset", "size_bytes": 7,
                                   "sha256": hashlib.sha256(b"private").hexdigest()}, tmp_path)
    (tmp_path / "copy.png").write_bytes(b"private")
    nested, outside = tmp_path / "nested", tmp_path / "outside"
    nested.mkdir()
    outside.mkdir()
    redirect_directory(nested / ".box-agent", outside)

    assert _detect_regex_artifacts("copy", "bash", "[copy.png]", None, str(tmp_path))[0] == []
