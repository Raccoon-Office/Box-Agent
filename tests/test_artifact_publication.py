import hashlib
from pathlib import Path
import shutil

import pytest

from box_agent.artifact_publication import write_metadata, metadata_path
from box_agent.tools.engine.artifact_results import (
    _detect_tool_artifacts, _detect_regex_artifacts, _detect_changed_files,
    _snapshot_workspace_signatures,
)


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("operation", ["rename", "copy", "copy-delete"])
def test_intermediate_identity_survives_later_tool_and_content_preserving_move(tmp_path, parallel, operation):
    original = tmp_path / "素材.png"
    original.write_bytes(b"private illustration")
    # Script-produced legacy markers are enriched during initial discovery.
    write_metadata(original, {"type": "intermediate_asset"})
    assert _detect_tool_artifacts("generate", "bash", "[素材.png]", None, {},
        _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path)) == []
    before = _snapshot_workspace_signatures(str(tmp_path))
    moved = tmp_path / "nested/renamed.png"
    moved.parent.mkdir()
    if operation == "rename":
        original.rename(moved)
    else:
        shutil.copyfile(original, moved)
        if operation == "copy-delete":
            original.unlink()
    overview = tmp_path / "overview.png"
    overview.write_bytes(b"whole deck overview")
    after = _snapshot_workspace_signatures(str(tmp_path))
    text = "[nested/renamed.png] [overview.png]"
    if parallel:
        events, emitted = _detect_regex_artifacts("rename", "bash", text, None, str(tmp_path))
        events += _detect_changed_files("rename", before, after, emitted, str(tmp_path))
    else:
        events = _detect_tool_artifacts("rename", "bash", text, None, before, after, str(tmp_path))
    assert [event.filename for event in events] == ["overview.png"]
    assert moved.read_bytes() == b"private illustration"
    assert metadata_path(moved).is_file()
    # Keep working after the original folder/metadata is removed or a restart.
    metadata_path(original).unlink()
    assert _detect_regex_artifacts("again", "bash", "[nested/renamed.png]", None, str(tmp_path))[0] == []


def test_explicit_publication_overrides_inherited_content_identity(tmp_path):
    original = tmp_path / "old.png"
    original.write_bytes(b"image")
    write_metadata(original, {"type": "intermediate_asset", "size_bytes": 5,
                              "sha256": hashlib.sha256(b"image").hexdigest()})
    delivered = tmp_path / "requested.png"
    delivered.write_bytes(b"image")
    write_metadata(delivered, {"type": "artifact"})
    events = _detect_tool_artifacts("publish", "bash", "[requested.png]", None, {},
                                    _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path))
    assert [event.filename for event in events] == ["requested.png"]


def test_structured_artifact_does_not_publish_renamed_intermediate(tmp_path):
    original = tmp_path / "working.png"
    original.write_bytes(b"image")
    write_metadata(original, {"type": "intermediate_asset"})
    _detect_regex_artifacts("create", "bash", "[working.png]", None, str(tmp_path))
    renamed = tmp_path / "renamed.png"
    original.rename(renamed)

    events, _ = _detect_regex_artifacts(
        "rename", "bash", "", {"type": "artifact", "abs_path": str(renamed)}, str(tmp_path)
    )
    assert events == []


def test_other_workspace_and_same_size_different_content_are_not_suppressed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    source = a / "source.png"
    source.write_bytes(b"private")
    write_metadata(source, {"type": "intermediate_asset", "size_bytes": 7,
                           "sha256": hashlib.sha256(b"private").hexdigest()})
    (a / "different.png").write_bytes(b"public!")
    (b / "same.png").write_bytes(b"private")
    for root, expected in [(a, "different.png"), (b, "same.png")]:
        events = _detect_tool_artifacts("bash", "bash", f"[{expected}]", None, {},
                                        _snapshot_workspace_signatures(str(root)), str(root))
        assert [event.filename for event in events] == [expected]


@pytest.mark.parametrize("output", ["done", "other/report.html.bak", "report.html", "other/"])
def test_workspace_changes_and_publication_marker_do_not_establish_ownership(tmp_path, output):
    report = tmp_path / "other/report.html"
    report.parent.mkdir()
    report.write_text("another task's report")
    write_metadata(report, {"type": "artifact"})
    events = _detect_tool_artifacts(
        "wait", "bash", output, None, {},
        _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    assert events == []


@pytest.mark.parametrize("path_style", ["relative", "absolute", "native-relative", "posix-absolute"])
def test_script_returned_path_selects_only_its_own_changed_artifact(tmp_path, path_style):
    own = tmp_path / "own/报告 with spaces.html"
    other = tmp_path / "other/报告 with spaces.html"
    for path in [own, other]:
        path.parent.mkdir()
        path.write_text("report")
        write_metadata(path, {"type": "artifact"})
    returned = {
        "relative": own.relative_to(tmp_path).as_posix(),
        "absolute": str(own),
        "native-relative": str(own.relative_to(tmp_path)),
        "posix-absolute": own.as_posix(),
    }[path_style]
    events = _detect_tool_artifacts(
        "build", "bash", f'wrote "{returned}"', None, {},
        _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    assert [(e.rel_path, e.placement) for e in events] == [
        (own.relative_to(tmp_path).as_posix(), "primary"),
    ]


@pytest.mark.parametrize("suffix", ["", ".bak", "/child"])
def test_native_relative_script_output_keeps_exact_path_and_publication_rules(tmp_path, suffix):
    report = tmp_path / "projects/供应链月度KPI汇报.html"
    report.parent.mkdir()
    report.write_text("report", encoding="utf-8")
    returned = str(report.relative_to(tmp_path)) + suffix
    events = _detect_tool_artifacts(
        "build", "bash", f"{returned} 10209 8", None, {},
        _snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    assert [(e.rel_path, e.placement) for e in events] == (
        [(report.relative_to(tmp_path).as_posix(), "supporting")] if not suffix else []
    )
