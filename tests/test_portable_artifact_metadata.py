"""Run standalone producers against the shared on-disk metadata contract."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]
FAST = REPO / "box_agent/skills/document-skills/pptx/scripts"
SUITE = REPO / "box_agent/skills/presentation-suite/skills"


def record(root, target, kind="files"):
    canonical = str(target.resolve())
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return json.loads((root / ".box-agent/artifact-metadata" / kind / f"{key}.json").read_text())


def assert_no_sidecars(root):
    assert not list(root.rglob("*.artifact.json"))
    assert not list(root.rglob(".artifact-delivery.json"))


@pytest.mark.parametrize("producer", ["node", "python"])
def test_standalone_producers_write_shared_records_for_external_unicode_outputs(tmp_path, producer):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outputs = tmp_path / "外部输出"
    outputs.mkdir()
    image = outputs / "预览.png"
    image.write_bytes(b"preview")
    final = outputs / "交付.html"
    final.write_text("finished")
    env = {**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(workspace), "PYTHONPATH": ""}
    if producer == "node":
        node = shutil.which("node")
        if not node:
            pytest.skip("Node required")
        code = "const d=require(process.argv[1]); d.declareScope(process.argv[2]); d.markIntermediate(process.argv[3]); d.publishArtifact(process.argv[4]);"
        command = [node, "-e", code, str(FAST / "artifact_delivery.js"), str(outputs), str(image), str(final)]
    else:
        # Load the shipped producer without importing the installed box_agent package.
        script = SUITE / "sn-ppt-dazzle/scripts/render_deck.py"
        code = "import runpy,sys; d=runpy.run_path(sys.argv[1]); d['_declare_delivery_scope'](sys.argv[2]); d['_mark_intermediate_artifact'](sys.argv[3]); d['_publish_delivery_file'](sys.argv[4])"
        command = [sys.executable, "-c", code, str(script), str(outputs), str(image), str(final)]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert record(workspace, image) == {"path": str(image.resolve()), "metadata": {"type": "intermediate_asset"}}
    assert record(workspace, final)["metadata"] == {"type": "artifact"}
    assert record(workspace, outputs, "scopes")["metadata"] == {"schema_version": 1, "default": "intermediate"}
    assert_no_sidecars(tmp_path)
    assert not (outputs / ".box-agent").exists()


@pytest.mark.parametrize("redirect", [".box-agent", ".box-agent/artifact-metadata", ".box-agent/artifact-metadata/files"])
def test_node_producer_rejects_redirected_internal_storage(tmp_path, redirect):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = workspace / redirect
    redirected.parent.mkdir(parents=True, exist_ok=True)
    redirected.symlink_to(outside, target_is_directory=True)
    artifact = workspace / "final.html"
    artifact.write_text("finished")
    result = subprocess.run([node, str(FAST / "artifact_delivery.js"), "publish", str(artifact)],
                            env={**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(workspace)}, capture_output=True, text=True)
    assert result.returncode != 0
    assert not list(outside.iterdir())
    assert_no_sidecars(workspace)


@pytest.mark.parametrize("marker", [None, ".git", ".box-agent"])
def test_node_producer_resolves_standalone_workspace(tmp_path, marker):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    workspace = tmp_path / "workspace"
    output = workspace / "nested"
    output.mkdir(parents=True)
    if marker:
        (workspace / marker).mkdir()
    artifact = output / "final.html"
    artifact.write_text("finished")
    env = dict(os.environ)
    env.pop("BOX_AGENT_WORKSPACE_DIR", None)
    subprocess.run([node, str(FAST / "artifact_delivery.js"), "publish", str(artifact)],
                   cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    assert record(workspace if marker else output, artifact)["metadata"] == {"type": "artifact"}
    assert_no_sidecars(workspace)


def test_offline_regeneration_adds_portable_helpers_and_preserves_local_dependencies(tmp_path):
    import runpy

    bundle = tmp_path / "bundle"
    shutil.copytree(REPO / "box_agent/skills/presentation-suite", bundle,
                    ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    local = bundle / "skills/sn-ppt-standard/scripts/export_pptx/node_modules/local/keep.js"
    local.parent.mkdir(parents=True)
    local.write_text("user dependency")
    sync = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    first = sync["refresh_host_overlays"](bundle)
    assert (bundle / "skills/sn-ppt-dazzle/scripts/artifact_metadata.py").is_file()
    assert local.read_text() == "user dependency"
    before = (bundle / "source.json").read_bytes()
    assert sync["refresh_host_overlays"](bundle) == first
    assert (bundle / "source.json").read_bytes() == before
    for relative, entry in first["files"].items():
        assert hashlib.sha256((bundle / relative).read_bytes()).hexdigest() == entry["sha256"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("action", ["publishArtifact", "markIntermediate"])
def test_node_republication_preserves_chat_retention(tmp_path, legacy, action):
    from box_agent.tools.chat_references import ChatReferences

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    artifact = tmp_path / "report.html"
    artifact.write_text("finished")
    env = {**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(tmp_path)}
    code = "require(process.argv[1])[process.argv[2]](process.argv[3]);"
    command = [node, "-e", code, str(FAST / "artifact_delivery.js"), action, str(artifact)]
    subprocess.run(command, env=env, check=True)
    ChatReferences(tmp_path).retain(artifact)
    if legacy:
        marker = artifact.with_name(".report.html.artifact.json")
        content = '{"retain_for_chat":true}'
        marker.write_text(content)
        for central in (tmp_path / ".box-agent/artifact-metadata/files").glob("*.json"):
            central.unlink()
    subprocess.run(command, env=env, check=True)
    assert record(tmp_path, artifact)["metadata"]["retain_for_chat"] is True
    if legacy:
        assert marker.read_text() == content
    else:
        assert_no_sidecars(tmp_path)


def test_node_producer_excludes_internal_metadata_from_git_status(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    artifact = tmp_path / "report.html"
    artifact.write_text("finished")
    subprocess.run([node, str(FAST / "artifact_delivery.js"), "publish", str(artifact)],
                   cwd=tmp_path, check=True, capture_output=True,
                   env={**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(tmp_path)})
    result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=tmp_path, check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "?? report.html"


def test_offline_migration_converts_legacy_renderer_and_adds_verified_helpers(tmp_path):
    import runpy

    sync = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    old = runpy.run_path(str(REPO / "scripts/presentation_suite_overlays/artifact_publication.py"))["_HELPER"]
    relative = "skills/sn-ppt-standard/scripts/render.py"
    bundle = tmp_path / "bundle"
    target = bundle / relative
    target.parent.mkdir(parents=True)
    target.write_text(old)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    marker = bundle / "source.json"
    marker.write_text(json.dumps({"name": sync["BUNDLE_NAME"], "revision": sync["PINNED_REVISION"],
        "overlays": sync["OVERLAYS"][:sync["OVERLAYS"].index("centralized-artifact-metadata")],
        "files": {relative: {"source_sha256": digest, "sha256": digest}}}))
    first = sync["refresh_host_overlays"](bundle)
    image = tmp_path / "preview.png"
    env = {**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(tmp_path), "PYTHONPATH": ""}
    subprocess.run([sys.executable, "-c",
        "import runpy,sys; runpy.run_path(sys.argv[1])['_mark_intermediate_artifact'](sys.argv[2])",
        str(target), str(image)], env=env, cwd=tmp_path, check=True)
    assert record(tmp_path, image)["metadata"] == {"type": "intermediate_asset"}
    assert first["files"][relative]["source_sha256"] == digest
    before = marker.read_bytes()
    assert sync["refresh_host_overlays"](bundle) == first
    assert marker.read_bytes() == before
    assert_no_sidecars(tmp_path)


def test_shipped_python_helpers_match_shared_storage_source():
    expected = (REPO / "box_agent/artifact_metadata.py").read_bytes()
    for directory in [FAST, *(SUITE / f"sn-ppt-{name}/scripts" for name in ("standard", "dazzle", "entry"))]:
        assert (directory / "artifact_metadata.py").read_bytes() == expected


def test_node_explicit_workspace_overrides_environment_and_nearest_marker(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    root = tmp_path / "explicit"
    root.mkdir()
    other = tmp_path / "environment"
    other.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    (output / ".git").mkdir()
    artifact = output / "report.html"
    artifact.write_text("finished")
    subprocess.run([node, "-e", "require(process.argv[1]).publishArtifact(process.argv[2],process.argv[3]);",
                    str(FAST / "artifact_delivery.js"), str(artifact), str(root)],
                   env={**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(other)}, check=True)
    assert record(root, artifact)["metadata"] == {"type": "artifact"}
    assert not (other / ".box-agent").exists()
    assert not (output / ".box-agent").exists()


def test_node_independent_processes_register_same_basename_without_losing_records(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    targets = []
    for number in range(6):
        target = tmp_path / str(number) / "report.html"
        target.parent.mkdir()
        target.write_text(str(number))
        targets.append(target)
    env = {**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(tmp_path)}
    children = [subprocess.Popen([node, str(FAST / "artifact_delivery.js"), "publish", str(target)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for target in targets]
    for child in children:
        _, errors = child.communicate(timeout=10)
        assert child.returncode == 0, errors
    for target in targets:
        assert record(tmp_path, target) == {"path": str(target.resolve()), "metadata": {"type": "artifact"}}
    assert len(list((tmp_path / ".box-agent/artifact-metadata/files").iterdir())) == len(targets)
    assert_no_sidecars(tmp_path)


@pytest.mark.parametrize("redirect", ["info", "exclude"])
def test_node_git_exclusion_preserves_redirected_optional_files(tmp_path, redirect):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "exclude"
    protected.write_text("local rules\n")
    info = workspace / ".git/info"
    if redirect == "info":
        shutil.rmtree(info)
        info.symlink_to(outside, target_is_directory=True)
    else:
        (info / "exclude").unlink()
        (info / "exclude").symlink_to(protected)
    artifact = workspace / "report.html"
    artifact.write_text("finished")
    result = subprocess.run([node, str(FAST / "artifact_delivery.js"), "publish", str(artifact)],
                            cwd=workspace, env={**os.environ, "BOX_AGENT_WORKSPACE_DIR": str(workspace)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert record(workspace, artifact)["metadata"] == {"type": "artifact"}
    assert protected.read_text() == "local rules\n"


def test_node_standalone_output_ignores_unrelated_cwd_checkout(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    artifact = outputs / "report.html"
    artifact.write_text("finished")
    env = dict(os.environ)
    env.pop("BOX_AGENT_WORKSPACE_DIR", None)
    subprocess.run([node, str(FAST / "artifact_delivery.js"), "publish", str(artifact)],
                   cwd=checkout, env=env, check=True, capture_output=True)
    assert record(outputs, artifact)["metadata"] == {"type": "artifact"}
    assert not (checkout / ".box-agent").exists()


def test_offline_migration_rolls_back_if_a_managed_file_replacement_fails(tmp_path, monkeypatch):
    import runpy

    sync = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    old = runpy.run_path(str(REPO / "scripts/presentation_suite_overlays/artifact_publication.py"))["_HELPER"]
    relative = "skills/sn-ppt-standard/scripts/render.py"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text(old)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    marker = tmp_path / "source.json"
    marker.write_text(json.dumps({"name": sync["BUNDLE_NAME"], "revision": sync["PINNED_REVISION"],
        "overlays": sync["OVERLAYS"][:sync["OVERLAYS"].index("centralized-artifact-metadata")],
        "files": {relative: {"source_sha256": digest, "sha256": digest}}}))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    replace = Path.replace
    injected = False

    def fail_once(source, destination):
        nonlocal injected
        if not injected and Path(destination).name == "artifact_metadata.py":
            injected = True
            raise OSError("injected replacement failure")
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_once)
    with pytest.raises(OSError, match="injected replacement failure"):
        sync["refresh_host_overlays"](tmp_path)
    assert injected
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    # A repaired filesystem can immediately retry the generator.
    assert sync["refresh_host_overlays"](tmp_path)["overlays"] == sync["OVERLAYS"]


def test_runtime_input_refresh_rolls_back_if_provenance_commit_fails(tmp_path, monkeypatch):
    import runpy

    sync = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    files = {}
    for name in ("render_runtime.py", "render_runtime_windows.py"):
        (inputs / name).write_text("new runtime")
        relative = f"skills/sn-ppt-standard/scripts/{name}"
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old runtime")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        files[relative] = {"sha256": digest, "source_sha256": digest,
                           "input_path": f"scripts/presentation_suite_overlays/{name}"}
    marker = bundle / "source.json"
    marker.write_text(json.dumps({"revision": sync["PINNED_REVISION"], "overlays": sync["OVERLAYS"], "files": files}))
    before = {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    monkeypatch.setitem(sync["refresh_runtime_inputs"].__globals__, "RUNTIME_INPUT_DIR", inputs)
    replace = Path.replace

    def fail_provenance(source, destination):
        if Path(destination) == marker:
            raise OSError("injected provenance failure")
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_provenance)
    with pytest.raises(OSError, match="injected provenance failure"):
        sync["refresh_runtime_inputs"](bundle)
    assert {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("existing_parent_record", [False, True])
@pytest.mark.parametrize("obscured_previous_root", [False, True])
@pytest.mark.parametrize("action", ["publishArtifact", "markIntermediate"])
def test_node_republication_in_parent_workspace_preserves_nested_standalone_retention(
    tmp_path, existing_parent_record, obscured_previous_root, action,
):
    from box_agent.artifact_metadata import read_metadata
    from box_agent.artifact_publication import retained_chat_file_in
    from box_agent.tools.chat_references import ChatReferences

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    outputs = tmp_path / ("standalone/outputs" if obscured_previous_root else "outputs")
    outputs.mkdir(parents=True)
    old_workspace = outputs.parent if obscured_previous_root else outputs
    if obscured_previous_root:
        (old_workspace / ".git").mkdir()
    artifact = outputs / "report.html"
    artifact.write_text("finished")
    env = dict(os.environ)
    env.pop("BOX_AGENT_WORKSPACE_DIR", None)
    command = [node, "-e", "require(process.argv[1])[process.argv[2]](process.argv[3]);",
               str(FAST / "artifact_delivery.js"), action, str(artifact)]
    subprocess.run(command, cwd=tmp_path, env=env, check=True)
    ChatReferences(old_workspace).retain(artifact)
    assert record(old_workspace, artifact)["metadata"]["retain_for_chat"] is True
    digest = hashlib.sha256(str(artifact.resolve()).encode("utf-8")).hexdigest()
    if obscured_previous_root:
        closer = outputs / f".box-agent/artifact-metadata/files/{digest}.json"
        closer.parent.mkdir(parents=True)
        closer.write_text(json.dumps({"path": str(artifact.resolve()), "metadata": {"type": "artifact"}}))
    if existing_parent_record:
        central = tmp_path / f".box-agent/artifact-metadata/files/{digest}.json"
        central.parent.mkdir(parents=True)
        central.write_text(json.dumps({"path": str(artifact.resolve()), "metadata": {"type": "artifact"}}))
    subprocess.run(command, cwd=tmp_path, env={**env, "BOX_AGENT_WORKSPACE_DIR": str(tmp_path)}, check=True)
    assert record(tmp_path, artifact)["metadata"]["retain_for_chat"] is True
    assert read_metadata(artifact, tmp_path)["retain_for_chat"] is True
    assert retained_chat_file_in(artifact, tmp_path) == artifact
    assert_no_sidecars(tmp_path)


@pytest.mark.parametrize("action", ["publishArtifact", "markIntermediate"])
def test_node_external_republication_preserves_target_local_standalone_retention(tmp_path, action):
    from box_agent.artifact_metadata import read_metadata
    from box_agent.artifact_publication import retained_chat_file_in
    from box_agent.tools.chat_references import ChatReferences

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    artifact = external / "report.html"
    artifact.write_text("finished")
    env = dict(os.environ)
    env.pop("BOX_AGENT_WORKSPACE_DIR", None)
    command = [node, "-e", "require(process.argv[1])[process.argv[2]](process.argv[3]);",
               str(FAST / "artifact_delivery.js"), action, str(artifact)]
    subprocess.run(command, cwd=tmp_path, env=env, check=True)
    ChatReferences(external).retain(artifact)
    before = {p: p.read_bytes() for p in (external / ".box-agent").rglob("*") if p.is_file()}
    subprocess.run(command, cwd=tmp_path, env={**env, "BOX_AGENT_WORKSPACE_DIR": str(workspace)}, check=True)
    assert record(workspace, artifact)["metadata"]["retain_for_chat"] is True
    assert read_metadata(artifact, workspace)["retain_for_chat"] is True
    assert retained_chat_file_in(artifact, workspace) == artifact
    assert {p: p.read_bytes() for p in (external / ".box-agent").rglob("*") if p.is_file()} == before
    assert_no_sidecars(tmp_path)
