"""The design presentation route ships exactly its working SN dependency closure."""

import hashlib
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
SUITE = REPO / "box_agent/skills/presentation-suite"
MODULES = {"entry", "story", "tools", "standard", "dazzle", "doctor"}
NAMES = {f"sn-ppt-{name}" for name in MODULES}


def test_six_internal_methods_keep_independent_roots_and_no_unused_backends():
    paths = list((SUITE / "skills").glob("*/SKILL.md"))
    assert {path.parent.name for path in paths} == NAMES
    for path in paths:
        metadata = yaml.safe_load(path.read_text().split("---", 2)[1])
        assert metadata["metadata"]["user_visible"] is False
        assert metadata["metadata"]["allow_override"] is False
    assert not list(SUITE.rglob("workbench-runtime"))


def test_entry_routes_static_and_dynamic_without_missing_methods():
    entry = (SUITE / "skills/sn-ppt-entry/SKILL.md").read_text()
    for retired in ("web_html", "web_postprocess", "旧 `creative`", "恢复旧静态任务"):
        assert retired not in entry
    task_example = json.loads(re.findall(r"```json\n(.*?)\n```", entry, re.S)[0])
    assert task_example["choices"]["output"] == "static_html"
    assert task_example["ppt_mode"] == "standard"
    assert task_example["choices"]["static_postprocess"] == ["pptx"]
    assert "`dynamic_html` -> `sn-ppt-dazzle`" in entry
    assert "`static_html` -> `sn-ppt-standard`" in entry
    assert "ppt-fast" in entry
    assert "不承诺" in entry and "动画" in entry
    assert "pwd -P" in entry and "outline.md" in entry
    for path in (SUITE / "skills").rglob("*.md"):
        references = set(re.findall(r"sn-ppt-[a-z]+", path.read_text()))
        assert references <= NAMES, (path.relative_to(SUITE), references - NAMES)


def test_provenance_covers_every_file_and_records_integration_changes():
    source = json.loads((SUITE / "source.json").read_text())
    assert source["revision"] == "b5ef18018940e63eafe28081460a402ec68632b5"
    assert source["modules"] == sorted(NAMES)
    assert "entry-two-outputs" in source["overlays"]
    assert "/Users/" not in json.dumps(source)
    actual = {p.relative_to(SUITE).as_posix() for p in SUITE.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts}
    assert actual == set(source["files"]) | {"source.json"}
    for relative, record in source["files"].items():
        assert hashlib.sha256((SUITE / relative).read_bytes()).hexdigest() == record["sha256"]
        assert re.fullmatch(r"[a-f0-9]{64}", record["source_sha256"])


def test_fonts_and_exporter_imports_remain_resolvable():
    assert len(list((SUITE / "fonts").glob("*.ttf"))) == 45
    assert (SUITE / "fonts/OFL-1.1.txt").is_file()
    assert (SUITE / "THIRD_PARTY_NOTICES.md").is_file()
    font_helper = runpy.run_path(str(SUITE / "skills/sn-ppt-standard/scripts/font_bundle.py"))
    assert SUITE / "fonts" in font_helper["_font_source_dirs"]()
    exporter = SUITE / "skills/sn-ppt-standard/scripts/export_pptx"
    assert (exporter / "package-lock.json").is_file()
    for module in exporter.rglob("*.mjs"):
        for specifier in re.findall(r"(?:from\s*|import\s*\()['\"](\.[^'\"]+)['\"]", module.read_text()):
            assert (module.parent / specifier).is_file(), (module, specifier)
    for relative in ["skills/sn-ppt-standard/assets/vendor/echarts.min.js", "skills/sn-ppt-dazzle/scripts/render_deck.py"]:
        assert (SUITE / relative).is_file()


def test_static_echarts_notices_ship_with_original_sources_and_resolvable_paths():
    provenance = json.loads((SUITE / "source.json").read_text())
    notice = (SUITE / "THIRD_PARTY_NOTICES.md").read_text()
    license_root = "skills/sn-ppt-standard/assets/licenses/echarts-5.5.0"
    for relative in ("LICENSE", "NOTICE", "licenses/LICENSE-d3"):
        bundled = f"{license_root}/{relative}"
        assert (SUITE / bundled).is_file(), bundled
        record = provenance["files"][bundled]
        assert record["source_url"] == f"https://raw.githubusercontent.com/apache/echarts/5.5.0/{relative}"
        assert (SUITE / bundled).read_bytes() == (REPO / record["input_path"]).read_bytes()
        assert record["source_sha256"] == record["sha256"]
    assert "echarts-5.5.0/LICENSE" in notice
    assert "echarts-5.5.0/NOTICE" in notice
    assert "echarts-5.5.0/licenses/LICENSE-d3" in notice
    for relative in re.findall(r"`([^`]+)`", notice):
        assert (SUITE / relative).exists(), relative
    assert "sn-ppt-web" not in notice
    assert "does not grant a license to SN-owned code" in notice


def test_doctor_checks_the_shipped_static_and_dynamic_renderers(monkeypatch, capsys):
    namespace = runpy.run_path(str(SUITE / "skills/sn-ppt-doctor/ppt_doctor/check_environment.py"))
    main = namespace["main"]
    state = main.__globals__
    renderer_dirs = []
    export_dirs = []

    def browser_probe(directory):
        renderer_dirs.append(directory)
        return {"status": "available", "python_package": True, "browser_present": True, "launchable": True}

    def exporter_probe(directory):
        export_dirs.append(directory)
        return {"status": "available", "script_present": (directory / "html_to_pptx.mjs").is_file()}

    monkeypatch.setitem(state, "playwright_chromium_status", browser_probe)
    monkeypatch.setitem(state, "html_to_pptx_status", exporter_probe)
    monkeypatch.setitem(state, "_load_ppt_env", lambda _: {})
    monkeypatch.setitem(state, "_bundled_media", lambda *_: {})
    monkeypatch.setitem(state, "module_available", lambda _: True)
    monkeypatch.setitem(state, "node_version", lambda: "test-node")
    monkeypatch.setattr(sys, "argv", ["doctor"])
    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert renderer_dirs == [SUITE / "skills/sn-ppt-standard"]
    assert export_dirs == [SUITE / "skills/sn-ppt-standard/scripts/export_pptx"]
    assert report["standard_html"]["status"] == "available"
    assert report["dynamic_renderer"] is True
    assert report["html_to_pptx"] is True
    assert "workbench_runtime" not in report
    assert "python_pptx" not in report


def test_git_source_packaging_includes_all_vendored_resources():
    source = json.loads((SUITE / "source.json").read_text())
    paths = [str((SUITE / relative).relative_to(REPO)) for relative in source["files"]]
    checked = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], cwd=REPO,
                             input="\n".join(paths), text=True, capture_output=True, check=False)
    assert checked.returncode in (0, 1), checked.stderr
    assert not checked.stdout.strip(), checked.stdout


def test_dynamic_method_uses_box_native_tools_and_common_media_fallback():
    dynamic = (SUITE / "skills/sn-ppt-dazzle/SKILL.md").read_text()
    assert "inspect_images" in dynamic and "generate_image" in dynamic
    assert "vision_analyze" not in dynamic
    assert "capability-policy.md" in dynamic


def _sync_function():
    namespace = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    return namespace["sync_suite"]


@pytest.fixture
def source_repo(tmp_path, monkeypatch):
    # Isolate git pinning/publication from the separately tested real content overlays.
    sync = _sync_function()
    monkeypatch.setitem(sync.__globals__, "_apply_integration_overlay", lambda _, data: data)
    monkeypatch.setitem(sync.__globals__, "_host_playwright_overlay", lambda _, data: data)
    monkeypatch.setitem(sync.__globals__, "_image_inspection_recovery_overlay", lambda _, data: data)
    repo = tmp_path / "upstream"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in NAMES:
        path = repo / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: Pinned method\n---\nOriginal upstream body\n")
    for relative in ["webui/bundled/fonts/OFL-1.1.txt", "webui/THIRD_PARTY_NOTICES.md",
                     "skills/sn-ppt-standard/requirements.txt"]:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("tracked resource\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return sync, repo, revision


def test_sync_uses_pinned_tracked_content_and_repeats_without_local_state(source_repo, tmp_path):
    sync, repo, revision = source_repo
    (repo / "skills/sn-ppt-entry/SKILL.md").write_text("uncommitted change")
    (repo / "skills/sn-ppt-entry/local.env").write_text("untracked sentinel")
    destination = tmp_path / "bundle"
    first = sync(repo, revision, destination)
    before = {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    second = sync(repo, revision, destination)
    after = {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    assert first == second and before == after
    assert "Original upstream body" in (destination / "skills/sn-ppt-entry/SKILL.md").read_text()
    assert not (destination / "skills/sn-ppt-entry/local.env").exists()
    assert not (destination / "skills/sn-ppt-web").exists()
    assert (destination / "skills/sn-ppt-standard/requirements.txt").is_file()
    for relative in ("LICENSE", "NOTICE", "licenses/LICENSE-d3"):
        path = f"skills/sn-ppt-standard/assets/licenses/echarts-5.5.0/{relative}"
        record = first["files"][path]
        assert record["source_url"].endswith(f"/5.5.0/{relative}")
        assert (destination / path).read_bytes() == (REPO / record["input_path"]).read_bytes()


def test_modified_license_input_preserves_previous_generated_bundle(source_repo, tmp_path, monkeypatch):
    sync, repo, revision = source_repo
    destination = tmp_path / "bundle"
    sync(repo, revision, destination)
    previous = (destination / "source.json").read_bytes()
    inputs = tmp_path / "license-inputs"
    for relative in ("LICENSE", "NOTICE", "licenses/LICENSE-d3"):
        target = inputs / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / "scripts/presentation_suite_licenses/echarts-5.5.0" / relative).read_bytes())
    (inputs / "NOTICE").write_text("unreviewed license change")
    monkeypatch.setitem(sync.__globals__, "LICENSE_INPUT_DIR", inputs)
    with pytest.raises(ValueError, match="license input needs review"):
        sync(repo, revision, destination)
    assert (destination / "source.json").read_bytes() == previous


def test_sync_refuses_unmanaged_destination_without_touching_it(source_repo, tmp_path):
    sync, repo, revision = source_repo
    destination = tmp_path / "user-directory"
    destination.mkdir()
    sentinel = destination / "user.txt"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="managed"):
        sync(repo, revision, destination)
    assert sentinel.read_text() == "keep"


def test_incomplete_revision_preserves_previous_generated_bundle(source_repo, tmp_path):
    sync, repo, revision = source_repo
    destination = tmp_path / "bundle"
    sync(repo, revision, destination)
    previous = (destination / "source.json").read_bytes()
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "skills/sn-ppt-entry/SKILL.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "incomplete"], check=True)
    incomplete = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    with pytest.raises(ValueError, match="missing required"):
        sync(repo, incomplete, destination)
    assert (destination / "source.json").read_bytes() == previous
    assert (destination / "skills/sn-ppt-entry/SKILL.md").is_file()


def test_upstream_route_drift_requires_review_before_overlay():
    overlay = _sync_function().__globals__["_apply_integration_overlay"]
    with pytest.raises(ValueError, match="needs review"):
        overlay("skills/sn-ppt-story/SKILL.md", b"unreviewed upstream routing changes")


def test_static_runtime_upgrade_requires_matching_license_review():
    overlay = _sync_function().__globals__["_apply_integration_overlay"]
    relative = "skills/sn-ppt-standard/assets/vendor/echarts.min.js"
    data = (SUITE / relative).read_bytes()
    assert overlay(relative, data) == data
    with pytest.raises(ValueError, match="ECharts runtime version needs review"):
        overlay(relative, data.replace(b'5.5.0', b'5.6.0'))


def test_completed_export_directory_refresh_is_idempotent(tmp_path):
    namespace = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    source = json.loads((SUITE / "source.json").read_text())
    paths = ("skills/sn-ppt-standard/scripts/export_pptx/html_to_pptx.mjs",
             "skills/sn-ppt-standard/references/box-agent-tool-contract.md")
    source["files"] = {relative: source["files"][relative] for relative in paths}
    bundle = tmp_path / "bundle"
    for relative in paths:
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SUITE / relative).read_bytes())
    marker = bundle / "source.json"
    marker.write_text(json.dumps(source))
    before = {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    first = namespace["refresh_host_overlays"](bundle)
    second = namespace["refresh_host_overlays"](bundle)
    after = {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    assert first == second == source
    assert before == after


@pytest.mark.parametrize("relative", (
    "html_to_pptx.mjs", "lib/cli_guards.mjs", "lib/dom_extractor.mjs",
    "lib/image_downloader.mjs", "lib/pptx_builder.mjs",
    "test/test_export_contract_regression.mjs",
))
def test_export_directory_overlay_requires_review_on_upstream_drift(relative):
    namespace = runpy.run_path(str(REPO / "scripts/presentation_suite_overlays/export_page_directories.py"))
    path = "skills/sn-ppt-standard/scripts/export_pptx/" + relative
    with pytest.raises(ValueError, match="export page directory overlay needs review"):
        namespace["apply"](path, b"changed upstream source\n")


def test_export_directory_refresh_does_not_bless_unrecorded_local_edits(tmp_path):
    namespace = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    relative = "skills/sn-ppt-standard/scripts/export_pptx/html_to_pptx.mjs"
    original = b"reviewed input\n"
    bundle = tmp_path / "bundle"
    target = bundle / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(original + b"unrecorded local edit\n")
    export_index = namespace["OVERLAYS"].index("source-relative-pptx-page-directories")
    record = {
        "name": namespace["BUNDLE_NAME"], "revision": namespace["PINNED_REVISION"],
        "overlays": namespace["OVERLAYS"][:export_index],
        "files": {relative: {"sha256": hashlib.sha256(original).hexdigest()}},
    }
    marker = bundle / "source.json"
    marker.write_text(json.dumps(record))
    before = marker.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError, match="differs from its provenance"):
        namespace["refresh_host_overlays"](bundle)
    assert (marker.read_bytes(), target.read_bytes()) == before


def _image_recovery_overlay():
    return runpy.run_path(str(REPO / "scripts/presentation_suite_overlays/image_inspection_recovery.py"))


def test_image_recovery_refresh_preserves_rules_and_repeats_without_changes(tmp_path):
    sync = runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))
    overlay = _image_recovery_overlay()
    source = json.loads((SUITE / "source.json").read_text())
    source["overlays"] = source["overlays"][:-1]
    source["files"] = {path: source["files"][path] for path in overlay["REPLACEMENTS"]}
    bundle = tmp_path / "bundle"
    expected = {}
    for relative, (old, new) in overlay["REPLACEMENTS"].items():
        expected[relative] = (SUITE / relative).read_bytes()
        previous = expected[relative].decode().replace(new, old, 1).encode()
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(previous)
        source["files"][relative]["sha256"] = hashlib.sha256(previous).hexdigest()
    marker = bundle / "source.json"
    marker.write_text(json.dumps(source))
    first = sync["refresh_host_overlays"](bundle)
    first_marker = marker.read_bytes()
    assert first["overlays"][-1] == "bounded-image-inspection-recovery"
    for relative, content in expected.items():
        assert (bundle / relative).read_bytes() == content
        assert b"visual_unverified" in content
        assert first["files"][relative]["sha256"] == hashlib.sha256(content).hexdigest()
        assert first["files"][relative]["source_sha256"] == source["files"][relative]["source_sha256"]
    assert sync["refresh_host_overlays"](bundle) == first
    assert marker.read_bytes() == first_marker
    # A later undocumented edit must still fail instead of being blessed.
    target.write_bytes(target.read_bytes() + b"unrecorded change\n")
    with pytest.raises(ValueError, match="differs from its provenance"):
        sync["refresh_host_overlays"](bundle)
    assert marker.read_bytes() == first_marker


def test_full_sync_also_applies_image_recovery_policy(source_repo, tmp_path, monkeypatch):
    sync, repo, _ = source_repo
    overlay = _image_recovery_overlay()
    monkeypatch.setitem(sync.__globals__, "_image_inspection_recovery_overlay", overlay["apply"])
    for relative, (old, _) in overlay["REPLACEMENTS"].items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = path.read_text() if path.exists() else ""
        path.write_text(prefix + old + "\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "recovery fixture"], check=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    destination = tmp_path / "regenerated"
    source = sync(repo, revision, destination)
    for relative in overlay["REPLACEMENTS"]:
        data = (destination / relative).read_bytes()
        assert b"visual_unverified" in data
        assert source["files"][relative]["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("relative", [
    "skills/sn-ppt-standard/SKILL.md",
    "skills/sn-ppt-standard/references/box-agent-tool-contract.md",
])
def test_image_recovery_overlay_rejects_unreviewed_source(relative):
    with pytest.raises(ValueError, match="needs review"):
        _image_recovery_overlay()["apply"](relative, b"changed upstream policy\n")
