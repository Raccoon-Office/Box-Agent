"""Contract and golden coverage for the renderer-neutral roadmap core."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource


SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "box_agent"
    / "skills"
    / "roadmap"
)
SCRIPTS_DIR = SKILL_DIR / "scripts"
FIXTURES_DIR = SKILL_DIR / "examples"
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")


def _run(
    script: str,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if NODE is None:
        pytest.skip("Node.js is required to test the roadmap compiler")
    return subprocess.run(
        [str(NODE), str(SCRIPTS_DIR / script), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=SKILL_DIR,
        env=env,
    )


def _read_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _schema_validator(name: str) -> Draft202012Validator:
    references = SKILL_DIR / "references"
    draft_schema = json.loads(
        (references / "roadmap-draft.schema.json").read_text(encoding="utf-8")
    )
    schema = json.loads((references / name).read_text(encoding="utf-8"))
    registry = Registry().with_resource(
        draft_schema["$id"], Resource.from_contents(draft_schema)
    )
    return Draft202012Validator(schema, registry=registry)


def _runtime_validation(
    tmp_path: Path, value: dict, function_name: str
) -> dict:
    if NODE is None:
        pytest.skip("Node.js is required to test the roadmap validator")
    source = tmp_path / f"{function_name}.json"
    _write_json(source, value)
    script = """
const fs = require('fs');
const core = require(process.argv[1]);
const value = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify(core[process.argv[3]](value)));
"""
    result = subprocess.run(
        [
            str(NODE),
            "-e",
            script,
            str(SCRIPTS_DIR / "roadmap_contract_core.js"),
            str(source),
            function_name,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=SKILL_DIR,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_roadmap_core_has_no_pptx_or_deck_dependency() -> None:
    for script in SCRIPTS_DIR.glob("*.js"):
        source = script.read_text(encoding="utf-8")
        assert "document-skills/pptx" not in source, script
        assert "deck_spec_core" not in source, script


def _compile(tmp_path: Path, fixture: str) -> tuple[subprocess.CompletedProcess[str], dict, Path]:
    output = tmp_path / f"{Path(fixture).stem}-spec.json"
    report = tmp_path / f"{Path(fixture).stem}-report.json"
    result = _run(
        "compile_roadmap_spec.js",
        str(FIXTURES_DIR / fixture),
        "--out",
        str(output),
        "--report",
        str(report),
    )
    return result, json.loads(report.read_text(encoding="utf-8")), output


@pytest.mark.parametrize(
    "fixture",
    [
        "draft-natural-language.json",
        "draft-table.json",
        "draft-image.json",
        "roadmap-spec-v1.json",
    ],
)
def test_supported_inputs_compile_to_roadmap_spec_v1(tmp_path, fixture) -> None:
    result, report, output = _compile(tmp_path, fixture)

    assert result.returncode == 0, result.stderr
    assert report["ok"] is True
    assert report["issues"] == []
    spec = json.loads(output.read_text(encoding="utf-8"))
    assert spec["schema_version"] == 1
    assert spec["kind"] == "roadmap-spec"


def test_v1_spec_migrator_is_lossless_and_rejects_unknown_future_versions(tmp_path) -> None:
    source = FIXTURES_DIR / "roadmap-spec-v1.json"
    output = tmp_path / "migrated.json"

    result = _run("migrate_roadmap_spec.js", str(source), "--out", str(output))

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == _read_fixture("roadmap-spec-v1.json")

    future = _read_fixture("roadmap-spec-v1.json")
    future["schema_version"] = 2
    future_path = tmp_path / "future.json"
    _write_json(future_path, future)
    rejected = _run(
        "migrate_roadmap_spec.js",
        str(future_path),
        "--out",
        str(tmp_path / "must-not-exist.json"),
    )

    assert rejected.returncode == 1
    assert "schema_version: unsupported 2" in rejected.stderr


def test_low_confidence_image_dates_stay_tentative_and_request_confirmation(tmp_path) -> None:
    result, report, output = _compile(tmp_path, "draft-image.json")

    assert result.returncode == 0, result.stderr
    spec = json.loads(output.read_text(encoding="utf-8"))
    assert spec["items"][0]["certainty"] == "tentative"
    assert report["pending_questions"] == [
        {
            "field_path": "items.0.start",
            "prompt": "请确认“预览集成”的起止日期。",
            "reason": "date confidence is below 0.8",
        }
    ]
    assert "normalized to tentative" in report["warnings"][0]


@pytest.mark.parametrize(
    ("fixture", "schema_name", "function_name"),
    [
        (
            "draft-natural-language.json",
            "roadmap-draft.schema.json",
            "validateAndNormalizeRoadmapDraft",
        ),
        (
            "roadmap-spec-v1.json",
            "roadmap-spec.schema.json",
            "validateAndNormalizeRoadmapSpec",
        ),
    ],
)
def test_frozen_schemas_and_runtime_validators_accept_the_same_examples(
    tmp_path, fixture, schema_name, function_name
) -> None:
    value = _read_fixture(fixture)

    assert list(_schema_validator(schema_name).iter_errors(value)) == []
    assert _runtime_validation(tmp_path, value, function_name)["ok"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        "draft-missing-pending-questions",
        "draft-missing-lane-order",
        "draft-missing-item-progress",
        "draft-image-missing-region",
        "draft-lane-id-alias",
        "draft-string-confidence",
        "draft-invalid-calendar-date",
        "spec-legend-extra-field",
        "spec-bar-missing-end",
        "spec-milestone-with-end",
        "spec-invalid-calendar-date",
    ],
)
def test_frozen_schemas_and_runtime_validators_reject_the_same_structural_errors(
    tmp_path, mutation
) -> None:
    if mutation.startswith("draft-"):
        fixture = (
            "draft-image.json"
            if mutation == "draft-image-missing-region"
            else "draft-natural-language.json"
        )
        value = _read_fixture(fixture)
        schema_name = "roadmap-draft.schema.json"
        function_name = "validateAndNormalizeRoadmapDraft"
        if mutation == "draft-missing-pending-questions":
            del value["pending_questions"]
        elif mutation == "draft-missing-lane-order":
            del value["lanes"][0]["order"]
        elif mutation == "draft-image-missing-region":
            del value["items"][0]["source"]["region"]
        elif mutation == "draft-lane-id-alias":
            value["items"][0]["lane_id"] = value["items"][0].pop("lane_ref")
        elif mutation == "draft-string-confidence":
            value["items"][0]["source"]["confidence"] = "0.9"
        elif mutation == "draft-invalid-calendar-date":
            value["items"][0]["start"] = "2026-02-30"
        else:
            del value["items"][0]["progress"]
    else:
        value = _read_fixture("roadmap-spec-v1.json")
        schema_name = "roadmap-spec.schema.json"
        function_name = "validateAndNormalizeRoadmapSpec"
        if mutation == "spec-legend-extra-field":
            value["legend"][0]["extra"] = True
        elif mutation == "spec-bar-missing-end":
            del value["items"][0]["end"]
        elif mutation == "spec-invalid-calendar-date":
            value["items"][0]["start"] = "2026-02-30"
        else:
            milestone = next(item for item in value["items"] if item["kind"] == "milestone")
            milestone["end"] = "2026-09-17"

    assert list(_schema_validator(schema_name).iter_errors(value))
    runtime = _runtime_validation(tmp_path, value, function_name)
    assert runtime["ok"] is False
    assert runtime["issues"]


def test_missing_date_blocks_spec_without_inventing_a_value(tmp_path) -> None:
    draft = _read_fixture("draft-natural-language.json")
    del draft["items"][0]["end"]
    source = tmp_path / "missing-date.json"
    output = tmp_path / "should-not-exist.json"
    report = tmp_path / "missing-date-report.json"
    _write_json(source, draft)

    result = _run(
        "compile_roadmap_spec.js",
        str(source),
        "--out",
        str(output),
        "--report",
        str(report),
    )

    assert result.returncode == 1
    assert not output.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert "items.0.end: required for bar item" in payload["issues"]
    assert payload["pending_questions"] == [
        {
            "field_path": "items.0.end",
            "prompt": "“Roadmap 数据契约”在哪一天结束（结束日不包含）？",
            "reason": "bar interval uses [start, end)",
        }
    ]


def test_source_provenance_requires_image_region(tmp_path) -> None:
    draft = _read_fixture("draft-image.json")
    del draft["items"][0]["source"]["region"]
    source = tmp_path / "missing-image-region.json"
    report = tmp_path / "missing-image-region-report.json"
    _write_json(source, draft)

    result = _run(
        "compile_roadmap_spec.js",
        str(source),
        "--out",
        str(tmp_path / "spec.json"),
        "--report",
        str(report),
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert "items.0.source.region: required for image source" in payload["issues"]


def test_generated_ids_are_stable_across_repeated_compilation(tmp_path) -> None:
    first_result, _, first_output = _compile(tmp_path / "first", "draft-table.json")
    second_result, _, second_output = _compile(tmp_path / "second", "draft-table.json")

    assert first_result.returncode == second_result.returncode == 0
    first = json.loads(first_output.read_text(encoding="utf-8"))
    second = json.loads(second_output.read_text(encoding="utf-8"))
    assert first == second
    assert first["lanes"][0]["id"].startswith("lane-agent-box-")
    assert first["items"][0]["id"].startswith("item-")


@pytest.mark.parametrize(
    ("mutation", "issue"),
    [
        ("timezone", "range.start: expected valid YYYY-MM-DD"),
        ("unknown-lane", "items.0.lane_id: unknown lane"),
        ("milestone-end", "items.2.end: milestone uses start only"),
        ("range-order", "range: expected [start, end) with start before end"),
        ("invalid-continuation", "continuation.before: item must start at range.start"),
        ("too-many-items", "items: expected at most 80"),
    ],
)
def test_roadmap_spec_rejects_contract_violations(tmp_path, mutation, issue) -> None:
    spec = _read_fixture("roadmap-spec-v1.json")
    if mutation == "timezone":
        spec["range"]["start"] = "2026-08-01T00:00:00Z"
    elif mutation == "unknown-lane":
        spec["items"][0]["lane_id"] = "missing"
    elif mutation == "milestone-end":
        spec["items"][2]["end"] = "2026-09-17"
    elif mutation == "range-order":
        spec["range"]["end"] = spec["range"]["start"]
    elif mutation == "invalid-continuation":
        spec["items"][1]["continuation"] = {"before": True}
    elif mutation == "too-many-items":
        template = spec["items"][0]
        spec["items"] = []
        for index in range(81):
            item = deepcopy(template)
            item["id"] = f"item-{index + 1}"
            spec["items"].append(item)
    source = tmp_path / f"invalid-{mutation}.json"
    report = tmp_path / f"invalid-{mutation}-report.json"
    _write_json(source, spec)

    result = _run(
        "compile_roadmap_spec.js",
        str(source),
        "--out",
        str(tmp_path / "spec.json"),
        "--report",
        str(report),
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert any(issue in entry for entry in payload["issues"])


def test_geometry_matches_committed_golden_and_assigns_non_overlapping_tracks(tmp_path) -> None:
    output = tmp_path / "roadmap-geometry.json"
    result = _run(
        "layout_roadmap.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--viewport",
        "1440x900",
        "--out",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    geometry = json.loads(output.read_text(encoding="utf-8"))
    golden = _read_fixture("roadmap-geometry-1440x900.json")
    assert geometry == golden
    assert len(geometry["lanes"]) == 4
    tracks = {bar["id"]: bar["track"] for bar in geometry["bars"]}
    assert tracks["contract-core"] != tracks["geometry-core"]
    assert tracks["qa-window"] != next(
        milestone["track"]
        for milestone in geometry["milestones"]
        if milestone["id"] == "qa-signoff"
    )
    assert geometry["lanes"][0]["track_count"] == 2
    assert {bar["line_style"] for bar in geometry["bars"]} == {"solid", "dashed"}
    assert [marker["direction"] for marker in geometry["continuations"]] == [
        "before",
        "after",
    ]
    assert geometry["bars"][0]["start"] == geometry["headers"][0]["start"]
    assert geometry["bars"][-1]["end"] == geometry["headers"][2]["end"]
    for label in geometry["labels"]:
        assert label["x"] >= geometry["canvas"]["plot_left"]
        assert label["x"] + label["width"] <= geometry["canvas"]["plot_right"]


def test_geometry_rejects_viewports_below_minimum(tmp_path) -> None:
    result = _run(
        "layout_roadmap.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--viewport",
        "639x360",
        "--out",
        str(tmp_path / "roadmap-geometry.json"),
    )

    assert result.returncode == 1
    assert "viewport: expected at least 640x360" in result.stderr


def test_html_renderer_emits_controlled_editable_artifact_metadata(tmp_path) -> None:
    output = tmp_path / "roadmap.html"
    result = _run(
        "render_roadmap_html.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert metadata["mime_type"] == "text/html"
    assert metadata["layout_id"] == "roadmap-swimlane-v1"
    assert metadata["renderer_version"] == 1
    assert metadata["editable"] is True
    html = output.read_text(encoding="utf-8")
    assert '<meta name="generator" content="Box Agent Roadmap Artifact v1"' in html
    assert '<meta name="box-agent-artifact-layout-id" content="roadmap-swimlane-v1"' in html
    assert 'id="deck-document"' in html
    assert 'id="roadmap-geometry"' in html
    assert 'id="roadmap-palette"' in html
    assert 'data-palette-id="roadmap-default-v1"' in html
    assert 'class="roadmap-actions" data-role="roadmap-actions" hidden' in html
    assert '"id": "roadmap-default-v1"' in html
    assert '"#8e6bf2"' in html
    assert 'data-roadmap-runtime="editor"' in html
    assert "roadmap-swimlane-v1" in html
    assert html.count('class="roadmap-lane"') == 4
    assert html.count('class="roadmap-bar"') == 6
    assert html.count('class="roadmap-milestone"') == 3
    assert 'data-role="editor-backdrop" hidden' in html
    assert 'data-progress="doing"' in html
    assert "document-skills/pptx" not in html

    editor_source = (SKILL_DIR / "runtime" / "roadmap-editor.js").read_text(
        encoding="utf-8"
    )
    assert "actions.hidden = !hostEditAvailable" in editor_source
    assert "clone.querySelector('[data-role=\"roadmap-actions\"]').hidden = true" in editor_source


def test_default_palette_is_registered_once_and_shared_with_editor_runtime() -> None:
    registry = json.loads(
        (SKILL_DIR / "runtime" / "registry.json").read_text(encoding="utf-8")
    )
    assert registry["default_palette_id"] == "roadmap-default-v1"
    palette = next(
        entry for entry in registry["palettes"] if entry["id"] == "roadmap-default-v1"
    )
    assert palette["colors"] == [
        "#8e6bf2",
        "#7092fa",
        "#56d9b6",
        "#ebaf78",
        "#f96565",
        "#96dee8",
        "#9f5f8f",
        "#78d3f8",
        "#21d3c5",
        "#f9c955",
        "#ff9999",
    ]
    editor_source = (SKILL_DIR / "runtime" / "roadmap-editor.js").read_text(
        encoding="utf-8"
    )
    assert "#8e6bf2" not in editor_source
    assert '#roadmap-palette' in editor_source


def test_editor_localizes_progress_and_explains_milestone_end_date() -> None:
    editor_source = (SKILL_DIR / "runtime" / "roadmap-editor.js").read_text(
        encoding="utf-8"
    )

    assert '{ value: "planned", label: "计划中" }' in editor_source
    assert '{ value: "doing", label: "进行中" }' in editor_source
    assert '{ value: "done", label: "已完成" }' in editor_source
    assert '{ value: "blocked", label: "受阻" }' in editor_source
    assert 'input.value = isMilestone ? "无需结束日期"' in editor_source
    assert "input.disabled = isMilestone" in editor_source


def test_editor_uses_icon_actions_and_closes_from_the_backdrop() -> None:
    editor_source = (SKILL_DIR / "runtime" / "roadmap-editor.js").read_text(
        encoding="utf-8"
    )
    editor_css = (SKILL_DIR / "runtime" / "roadmap.css").read_text(
        encoding="utf-8"
    )

    assert 'icon: "close", iconOnly: true' in editor_source
    assert 'icon: "plus", variant: "primary"' in editor_source
    assert 'icon: "trash", iconOnly: true, variant: "danger"' in editor_source
    assert 'editorBackdrop.addEventListener("click", () => setEditorOpen(false))' in editor_source
    assert 'node.dataset.progress = item.progress' in editor_source
    assert 'font-size: 12px;' in editor_css
    assert 'font-weight: 540;' in editor_css
    assert '.roadmap-button[data-variant="primary"] svg { width: 14px; height: 14px; }' in editor_css


def test_editor_custom_select_opens_away_from_the_trigger_and_escapes_table_clipping() -> None:
    editor_source = (SKILL_DIR / "runtime" / "roadmap-editor.js").read_text(
        encoding="utf-8"
    )
    editor_css = (SKILL_DIR / "runtime" / "roadmap.css").read_text(
        encoding="utf-8"
    )

    assert 'menu.setAttribute("popover", "manual")' in editor_source
    assert 'menu.style.top = `${rect.bottom + 4}px`' in editor_source
    assert 'menu.style.top = `${rect.top - menuRect.height - 4}px`' in editor_source
    assert 'trigger.setAttribute("role", "combobox")' in editor_source
    assert 'menu.setAttribute("role", "listbox")' in editor_source
    assert 'if (event.defaultPrevented || event.key !== "Escape"' in editor_source
    assert '.roadmap-select-menu {' in editor_css
    assert 'position: fixed;' in editor_css


def test_html_renderer_marks_blocked_bars_with_a_full_status_layer(tmp_path) -> None:
    spec = _read_fixture("roadmap-spec-v1.json")
    blocked_item = next(item for item in spec["items"] if item["kind"] == "bar")
    blocked_item["progress"] = "blocked"
    source = tmp_path / "blocked.json"
    output = tmp_path / "blocked.html"
    _write_json(source, spec)

    result = _run("render_roadmap_html.js", str(source), "--out", str(output))

    assert result.returncode == 0, result.stderr
    html = output.read_text(encoding="utf-8")
    marker = html.index(f'data-item-id="{blocked_item["id"]}"')
    tag_start = html.rfind("<div", 0, marker)
    progress_end = html.index("</div>", marker)
    blocked_markup = html[tag_start:progress_end]
    assert 'data-progress="blocked"' in blocked_markup
    assert "· 受阻" in blocked_markup
    assert 'class="roadmap-bar-progress" style="width:100%"' in blocked_markup


def test_html_renderer_compacts_partial_half_month_header(tmp_path) -> None:
    spec = _read_fixture("roadmap-spec-v1.json")
    spec["range"]["end"] = "2026-11-03"
    release = next(item for item in spec["items"] if item["id"] == "release-candidate")
    release["end"] = "2026-11-03"
    source = tmp_path / "partial-half-month.json"
    output = tmp_path / "partial-half-month.html"
    _write_json(source, spec)

    result = _run("render_roadmap_html.js", str(source), "--out", str(output))

    assert result.returncode == 0, result.stderr
    html = output.read_text(encoding="utf-8")
    assert 'data-kind="half-month" title="上半月"' in html
    assert ">上</div>" in html


def test_html_renderer_escapes_user_text_and_rejects_over_capacity(tmp_path) -> None:
    spec = _read_fixture("roadmap-spec-v1.json")
    spec["title"] = '<img src=x onerror="alert(1)">'
    spec["items"][0]["title"] = "</script><script>alert(2)</script>"
    source = tmp_path / "unsafe.json"
    output = tmp_path / "safe.html"
    _write_json(source, spec)

    result = _run("render_roadmap_html.js", str(source), "--out", str(output))

    assert result.returncode == 0, result.stderr
    html = output.read_text(encoding="utf-8")
    assert '<img src=x onerror="alert(1)">' not in html
    assert "</script><script>alert(2)</script>" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "\\u003c/script>\\u003cscript>alert(2)\\u003c/script>" in html

    over_capacity = _read_fixture("roadmap-spec-v1.json")
    template = over_capacity["items"][0]
    over_capacity["items"] = []
    for index in range(81):
        item = deepcopy(template)
        item["id"] = f"capacity-{index + 1}"
        over_capacity["items"].append(item)
    over_capacity_path = tmp_path / "over-capacity.json"
    _write_json(over_capacity_path, over_capacity)
    rejected = _run(
        "render_roadmap_html.js",
        str(over_capacity_path),
        "--out",
        str(tmp_path / "must-not-exist.html"),
    )
    assert rejected.returncode == 1
    assert "items: expected at most 80" in rejected.stderr


def test_html_runtime_scripts_are_valid_javascript() -> None:
    for script in [
        SCRIPTS_DIR / "build_roadmap_artifact.js",
        SCRIPTS_DIR / "roadmap_html_core.js",
        SCRIPTS_DIR / "render_roadmap_html.js",
        SCRIPTS_DIR / "extract_roadmap_spec.js",
        SKILL_DIR / "runtime" / "roadmap-editor.js",
    ]:
        result = subprocess.run(
            [str(NODE), "--check", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_unified_builder_leaves_only_versioned_html_and_consumes_draft(tmp_path) -> None:
    draft = tmp_path / "roadmap-draft.json"
    shutil.copyfile(FIXTURES_DIR / "draft-natural-language.json", draft)

    result = _run(
        "build_roadmap_artifact.js",
        str(draft),
        "--out",
        str(tmp_path / "product-roadmap.html"),
        "--consume-input",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["filename"] == "product-roadmap-v1.html"
    assert report["generation_version"] == 1
    assert not draft.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["product-roadmap-v1.html"]
    html = (tmp_path / "product-roadmap-v1.html").read_text(encoding="utf-8")
    assert 'name="box-agent-roadmap-generation-version" content="1"' in html
    assert 'data-generation-version="1"' in html


def test_unified_builder_consumes_external_task_scratch_and_cleans_directory(
    tmp_path,
) -> None:
    output_dir = tmp_path / "output"
    scratch_root = tmp_path / ".box-agent-scratch"
    task_scratch = scratch_root / "roadmap-task"
    output_dir.mkdir()
    task_scratch.mkdir(parents=True)
    draft = task_scratch / "roadmap-draft.json"
    helper = task_scratch / "helper.js"
    shutil.copyfile(FIXTURES_DIR / "draft-natural-language.json", draft)
    helper.write_text("// temporary helper", encoding="utf-8")
    env = {
        **os.environ,
        "BOX_AGENT_OUTPUT_DIR": str(output_dir),
        "BOX_AGENT_SCRATCH_DIR": str(scratch_root),
    }

    result = _run(
        "build_roadmap_artifact.js",
        str(draft),
        "--out",
        "roadmap.html",
        "--consume-input",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert (output_dir / "roadmap-v1.html").is_file()
    assert list(output_dir.iterdir()) == [output_dir / "roadmap-v1.html"]
    assert not task_scratch.exists()
    assert scratch_root.is_dir()
    assert not list(scratch_root.iterdir())


def test_unified_builder_cleans_task_scratch_after_build_failure(tmp_path) -> None:
    output_dir = tmp_path / "output"
    scratch_root = tmp_path / ".box-agent-scratch"
    task_scratch = scratch_root / "roadmap-task"
    output_dir.mkdir()
    task_scratch.mkdir(parents=True)
    draft = task_scratch / "roadmap-draft.json"
    draft.write_text('{"kind":"roadmap-draft"}', encoding="utf-8")
    env = {
        **os.environ,
        "BOX_AGENT_OUTPUT_DIR": str(output_dir),
        "BOX_AGENT_SCRATCH_DIR": str(scratch_root),
    }

    result = _run(
        "build_roadmap_artifact.js",
        str(draft),
        "--out",
        "roadmap.html",
        "--consume-input",
        env=env,
    )

    assert result.returncode == 1
    assert not task_scratch.exists()
    assert not list(output_dir.iterdir())


def test_unified_builder_rejects_consumed_input_outside_configured_scratch(
    tmp_path,
) -> None:
    output_dir = tmp_path / "output"
    scratch_root = tmp_path / ".box-agent-scratch"
    outside_dir = tmp_path / "outside"
    output_dir.mkdir()
    scratch_root.mkdir()
    outside_dir.mkdir()
    draft = outside_dir / "roadmap-draft.json"
    shutil.copyfile(FIXTURES_DIR / "draft-natural-language.json", draft)
    env = {
        **os.environ,
        "BOX_AGENT_OUTPUT_DIR": str(output_dir),
        "BOX_AGENT_SCRATCH_DIR": str(scratch_root),
    }

    result = _run(
        "build_roadmap_artifact.js",
        str(draft),
        "--out",
        "roadmap.html",
        "--consume-input",
        env=env,
    )

    assert result.returncode == 1
    assert "must stay within BOX_AGENT_SCRATCH_DIR" in result.stderr
    assert draft.is_file()
    assert not list(output_dir.iterdir())


def test_unified_builder_rejects_scratch_input_symlink_and_cleans_task(
    tmp_path,
) -> None:
    output_dir = tmp_path / "output"
    scratch_root = tmp_path / ".box-agent-scratch"
    task_scratch = scratch_root / "roadmap-task"
    outside_dir = tmp_path / "outside"
    output_dir.mkdir()
    task_scratch.mkdir(parents=True)
    outside_dir.mkdir()
    outside_draft = outside_dir / "roadmap-draft.json"
    shutil.copyfile(FIXTURES_DIR / "draft-natural-language.json", outside_draft)
    linked_draft = task_scratch / "roadmap-draft.json"
    try:
        linked_draft.symlink_to(outside_draft)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")
    env = {
        **os.environ,
        "BOX_AGENT_OUTPUT_DIR": str(output_dir),
        "BOX_AGENT_SCRATCH_DIR": str(scratch_root),
    }

    result = _run(
        "build_roadmap_artifact.js",
        str(linked_draft),
        "--out",
        "roadmap.html",
        "--consume-input",
        env=env,
    )

    assert result.returncode == 1
    assert "input path must not contain a symlink or reparse point" in result.stderr
    assert outside_draft.is_file()
    assert not task_scratch.exists()
    assert not list(output_dir.iterdir())


def test_unified_builder_rejects_unsafe_existing_version_without_retrying(tmp_path) -> None:
    unsafe_version = tmp_path / "product-roadmap-v9007199254740992.html"
    unsafe_version.write_text("existing", encoding="utf-8")

    result = _run(
        "build_roadmap_artifact.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        str(tmp_path / "product-roadmap.html"),
    )

    assert result.returncode != 0
    assert "outside the supported range" in result.stderr
    assert unsafe_version.read_text(encoding="utf-8") == "existing"


def test_unified_builder_reports_low_confidence_confirmation_questions(tmp_path) -> None:
    result = _run(
        "build_roadmap_artifact.js",
        str(FIXTURES_DIR / "draft-image.json"),
        "--out",
        str(tmp_path / "roadmap.html"),
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["pending_questions"] == [
        {
            "field_path": "items.0.start",
            "prompt": "请确认“预览集成”的起止日期。",
            "reason": "date confidence is below 0.8",
        }
    ]
    assert report["status"] == "preview"


def test_unified_builder_preserves_confirmation_gate_across_html_versions(tmp_path) -> None:
    draft = tmp_path / "draft-image.json"
    shutil.copyfile(FIXTURES_DIR / "draft-image.json", draft)
    first = _run(
        "build_roadmap_artifact.js",
        str(draft),
        "--out",
        str(tmp_path / "roadmap.html"),
        "--consume-input",
    )
    assert first.returncode == 0, first.stderr
    assert not draft.exists()

    second = _run(
        "build_roadmap_artifact.js",
        json.loads(first.stdout)["path"],
        "--out",
        str(tmp_path / "roadmap.html"),
    )

    assert second.returncode == 0, second.stderr
    report = json.loads(second.stdout)
    assert report["status"] == "preview"
    assert report["pending_questions"][0]["field_path"] == "items.0.start"
    html = Path(report["path"]).read_text(encoding="utf-8")
    assert 'id="roadmap-pending-questions"' in html


def test_unified_builder_clears_confirmation_gate_for_confirmed_spec(tmp_path) -> None:
    spec = _read_fixture("roadmap-spec-v1.json")
    for item in spec["items"]:
        item["certainty"] = "confirmed"
    spec["items"][0]["certainty"] = "tentative"
    source = tmp_path / "tentative.json"
    _write_json(source, spec)
    preview = _run(
        "build_roadmap_artifact.js",
        str(source),
        "--out",
        str(tmp_path / "roadmap.html"),
    )
    assert json.loads(preview.stdout)["status"] == "preview"

    spec["items"][0]["certainty"] = "confirmed"
    _write_json(source, spec)
    final = _run(
        "build_roadmap_artifact.js",
        str(source),
        "--out",
        str(tmp_path / "confirmed.html"),
    )

    assert final.returncode == 0, final.stderr
    assert json.loads(final.stdout)["status"] == "final"
    assert json.loads(final.stdout)["pending_questions"] == []


def test_saved_html_confirmation_clears_persisted_pending_question(tmp_path) -> None:
    first = _run(
        "build_roadmap_artifact.js",
        str(FIXTURES_DIR / "draft-image.json"),
        "--out",
        str(tmp_path / "roadmap.html"),
    )
    html_path = Path(json.loads(first.stdout)["path"])
    html = html_path.read_text(encoding="utf-8")
    start_marker = '<script type="application/json" id="deck-document">'
    start = html.index(start_marker) + len(start_marker)
    end = html.index("</script>", start)
    spec = json.loads(html[start:end])
    for item in spec["items"]:
        item["certainty"] = "confirmed"
    html_path.write_text(
        html[:start]
        + "\n"
        + json.dumps(spec, ensure_ascii=False, indent=2).replace("<", "\\u003c")
        + "\n  "
        + html[end:],
        encoding="utf-8",
    )

    follow_up = _run(
        "build_roadmap_artifact.js",
        str(html_path),
        "--out",
        str(tmp_path / "roadmap.html"),
    )

    assert follow_up.returncode == 0, follow_up.stderr
    report = json.loads(follow_up.stdout)
    assert report["status"] == "final"
    assert report["pending_questions"] == []


def test_unified_builder_emits_structured_error_report(tmp_path) -> None:
    draft = _read_fixture("draft-natural-language.json")
    del draft["items"][0]["end"]
    source = tmp_path / "missing-date.json"
    _write_json(source, draft)

    result = _run(
        "build_roadmap_artifact.js",
        str(source),
        "--out",
        str(tmp_path / "roadmap.html"),
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert "items.0.end: required for bar item" in report["issues"]
    assert report["pending_questions"][0]["field_path"] == "items.0.end"


def test_roadmap_outputs_cannot_escape_configured_artifact_root(tmp_path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    env = {**os.environ, "BOX_AGENT_OUTPUT_DIR": str(output_dir)}

    traversal = _run(
        "build_roadmap_artifact.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        "../escaped.html",
        env=env,
    )
    absolute = _run(
        "render_roadmap_html.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        str(tmp_path / "absolute.html"),
        env=env,
    )

    assert traversal.returncode == 1
    assert "must stay within BOX_AGENT_OUTPUT_DIR" in traversal.stderr
    assert absolute.returncode == 1
    assert "must stay within BOX_AGENT_OUTPUT_DIR" in absolute.stderr
    assert not (tmp_path / "escaped-v1.html").exists()
    assert not (tmp_path / "absolute.html").exists()


def test_roadmap_outputs_reject_symlink_targets_and_parent_segments(tmp_path) -> None:
    output_dir = tmp_path / "output"
    outside_dir = tmp_path / "outside"
    output_dir.mkdir()
    outside_dir.mkdir()
    outside_file = outside_dir / "original.html"
    outside_file.write_text("do not replace", encoding="utf-8")
    target_link = output_dir / "link.html"
    parent_link = output_dir / "linked-directory"
    try:
        target_link.symlink_to(outside_file)
        parent_link.symlink_to(outside_dir, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")
    env = {**os.environ, "BOX_AGENT_OUTPUT_DIR": str(output_dir)}

    target_result = _run(
        "render_roadmap_html.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        "link.html",
        env=env,
    )
    parent_result = _run(
        "render_roadmap_html.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        "linked-directory/roadmap.html",
        env=env,
    )

    assert target_result.returncode == 1
    assert "symlink or reparse point" in target_result.stderr
    assert parent_result.returncode == 1
    assert (
        "symlink or reparse point" in parent_result.stderr
        or "resolves outside BOX_AGENT_OUTPUT_DIR" in parent_result.stderr
    )
    assert outside_file.read_text(encoding="utf-8") == "do not replace"
    assert not (outside_dir / "roadmap.html").exists()


def test_roadmap_output_rolls_back_when_parent_changes_during_publication(
    tmp_path,
) -> None:
    if NODE is None:
        pytest.skip("Node.js is required to test Roadmap output publication")
    output_dir = tmp_path / "output"
    nested_dir = output_dir / "nested"
    outside_dir = tmp_path / "outside"
    moved_dir = outside_dir / "moved"
    nested_dir.mkdir(parents=True)
    outside_dir.mkdir()
    script = """
const fs = require('fs');
const path = require('path');
const io = require(process.argv[1]);
const outputRoot = process.argv[2];
const nested = path.join(outputRoot, 'nested');
const moved = process.argv[3];
const originalRename = fs.renameSync;
let swapped = false;
fs.renameSync = (source, target) => {
  if (!swapped && path.basename(source).endsWith('.tmp')) {
    swapped = true;
    originalRename(nested, moved);
    fs.symlinkSync(moved, nested, 'dir');
  }
  return originalRename(source, target);
};
try {
  io.writeOutputFileSync('nested/roadmap.html', '<html></html>');
  process.exitCode = 2;
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
"""

    result = subprocess.run(
        [
            str(NODE),
            "-e",
            script,
            str(SCRIPTS_DIR / "roadmap_io.js"),
            str(output_dir),
            str(moved_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=SKILL_DIR,
        env={**os.environ, "BOX_AGENT_OUTPUT_DIR": str(output_dir)},
    )

    assert result.returncode == 1
    assert "output path must not contain a symlink or reparse point" in result.stderr
    assert not (moved_dir / "roadmap.html").exists()


def test_unified_builder_rejects_consuming_symlink_targets_and_parent_segments(
    tmp_path,
) -> None:
    output_dir = tmp_path / "output"
    outside_dir = tmp_path / "outside"
    output_dir.mkdir()
    outside_dir.mkdir()
    outside_draft = outside_dir / "draft.json"
    shutil.copyfile(FIXTURES_DIR / "draft-natural-language.json", outside_draft)
    target_link = output_dir / "draft-link.json"
    parent_link = output_dir / "linked-directory"
    try:
        target_link.symlink_to(outside_draft)
        parent_link.symlink_to(outside_dir, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")
    env = {**os.environ, "BOX_AGENT_OUTPUT_DIR": str(output_dir)}

    target_result = _run(
        "build_roadmap_artifact.js",
        "draft-link.json",
        "--out",
        "target-roadmap.html",
        "--consume-input",
        env=env,
    )
    parent_result = _run(
        "build_roadmap_artifact.js",
        "linked-directory/draft.json",
        "--out",
        "parent-roadmap.html",
        "--consume-input",
        env=env,
    )

    assert target_result.returncode == 1
    assert (
        "input path must not contain a symlink or reparse point"
        in target_result.stderr
    )
    assert parent_result.returncode == 1
    assert (
        "input path must not contain a symlink or reparse point"
        in parent_result.stderr
    )
    assert outside_draft.exists()
    assert not list(output_dir.glob("*-roadmap-v*.html"))


def test_consumed_input_identity_is_rechecked_before_deletion(tmp_path) -> None:
    if NODE is None:
        pytest.skip("Node.js is required to test Roadmap input consumption")
    source = tmp_path / "draft.json"
    replacement = tmp_path / "replacement.json"
    shutil.copyfile(FIXTURES_DIR / "draft-natural-language.json", source)
    replacement.write_text("replacement", encoding="utf-8")
    script = """
const fs = require('fs');
const io = require(process.argv[1]);
const source = process.argv[2];
const replacement = process.argv[3];
const snapshot = io.snapshotArtifactFileSync(source);
fs.unlinkSync(source);
fs.renameSync(replacement, source);
try {
  io.consumeArtifactFileSync(source, snapshot.identity);
  process.exitCode = 2;
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
"""

    result = subprocess.run(
        [
            str(NODE),
            "-e",
            script,
            str(SCRIPTS_DIR / "roadmap_io.js"),
            str(source),
            str(replacement),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=SKILL_DIR,
    )

    assert result.returncode == 1
    assert "consumed input changed before deletion" in result.stderr
    assert source.read_text(encoding="utf-8") == "replacement"


def test_scratch_cleanup_rejects_replaced_task_directory(tmp_path) -> None:
    if NODE is None:
        pytest.skip("Node.js is required to test Roadmap scratch cleanup")
    scratch_root = tmp_path / ".box-agent-scratch"
    task_dir = scratch_root / "roadmap-task"
    moved_dir = scratch_root / "moved-task"
    task_dir.mkdir(parents=True)
    source = task_dir / "draft.json"
    shutil.copyfile(FIXTURES_DIR / "draft-natural-language.json", source)
    script = """
const fs = require('fs');
const path = require('path');
const io = require(process.argv[1]);
const source = process.argv[2];
const taskDir = path.dirname(source);
const movedDir = process.argv[3];
const snapshot = io.snapshotConsumedInputFileSync(source);
fs.renameSync(taskDir, movedDir);
fs.mkdirSync(taskDir);
fs.writeFileSync(path.join(taskDir, 'keep.txt'), 'keep');
try {
  io.cleanupScratchTaskDirectorySync(source, snapshot);
  process.exitCode = 2;
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
"""

    result = subprocess.run(
        [
            str(NODE),
            "-e",
            script,
            str(SCRIPTS_DIR / "roadmap_io.js"),
            str(source),
            str(moved_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=SKILL_DIR,
        env={**os.environ, "BOX_AGENT_SCRATCH_DIR": str(scratch_root)},
    )

    assert result.returncode == 1
    assert "scratch task directory changed before cleanup" in result.stderr
    assert (task_dir / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_roadmap_output_replacement_is_atomic_and_leaves_no_temporary_file(
    tmp_path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output = output_dir / "roadmap.html"
    output.write_text("old content", encoding="utf-8")
    env = {**os.environ, "BOX_AGENT_OUTPUT_DIR": str(output_dir)}

    result = _run(
        "render_roadmap_html.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        "roadmap.html",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "old content" not in output.read_text(encoding="utf-8")
    assert not list(output_dir.glob(".roadmap.html.*.tmp"))


def test_unified_builder_rejects_consuming_persisted_spec(tmp_path) -> None:
    source = tmp_path / "roadmap-spec.json"
    shutil.copyfile(FIXTURES_DIR / "roadmap-spec-v1.json", source)

    result = _run(
        "build_roadmap_artifact.js",
        str(source),
        "--out",
        str(tmp_path / "roadmap.html"),
        "--consume-input",
    )

    assert result.returncode == 1
    assert "--consume-input is only allowed for RoadmapDraft input" in result.stderr
    assert source.exists()
    assert not list(tmp_path.glob("roadmap-v*.html"))


def test_unified_builder_preserves_every_generated_html_version(tmp_path) -> None:
    source = FIXTURES_DIR / "roadmap-spec-v1.json"
    first = _run(
        "build_roadmap_artifact.js",
        str(source),
        "--out",
        str(tmp_path / "product-roadmap.html"),
    )
    assert first.returncode == 0, first.stderr
    first_path = tmp_path / "product-roadmap-v1.html"
    first_bytes = first_path.read_bytes()

    second = _run(
        "build_roadmap_artifact.js",
        str(first_path),
        "--out",
        str(tmp_path / "product-roadmap.html"),
    )

    assert second.returncode == 0, second.stderr
    report = json.loads(second.stdout)
    assert report["filename"] == "product-roadmap-v2.html"
    assert report["generation_version"] == 2
    assert first_path.read_bytes() == first_bytes
    assert (tmp_path / "product-roadmap-v2.html").exists()
    assert not (tmp_path / "product-roadmap.html").exists()


def test_unified_builder_treats_legacy_unversioned_html_as_v1(tmp_path) -> None:
    (tmp_path / "product-roadmap.html").write_text("legacy", encoding="utf-8")

    result = _run(
        "build_roadmap_artifact.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        str(tmp_path / "product-roadmap.html"),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["filename"] == "product-roadmap-v2.html"
    assert (tmp_path / "product-roadmap.html").read_text(encoding="utf-8") == "legacy"


def test_unified_builder_keeps_json_only_when_debug_is_explicit(tmp_path) -> None:
    debug_dir = tmp_path / "debug"

    result = _run(
        "build_roadmap_artifact.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        str(tmp_path / "roadmap.html"),
        "--debug-dir",
        str(debug_dir),
    )

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in debug_dir.iterdir()) == [
        "roadmap-build-report.json",
        "roadmap-geometry.json",
        "roadmap-spec-v1.json",
    ]


def test_saved_html_embedded_spec_is_the_follow_up_source_of_truth(tmp_path) -> None:
    html_path = tmp_path / "roadmap.html"
    rendered = _run(
        "render_roadmap_html.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        str(html_path),
    )
    assert rendered.returncode == 0, rendered.stderr

    stale_sidecar = _read_fixture("roadmap-spec-v1.json")
    stale_sidecar["title"] = "旧 sidecar 标题"
    _write_json(tmp_path / "roadmap-spec.json", stale_sidecar)

    latest = _read_fixture("roadmap-spec-v1.json")
    latest["title"] = "保存后的最新标题"
    html = html_path.read_text(encoding="utf-8")
    start_marker = '<script type="application/json" id="deck-document">'
    start = html.index(start_marker) + len(start_marker)
    end = html.index("</script>", start)
    html_path.write_text(
        html[:start]
        + "\n"
        + json.dumps(latest, ensure_ascii=False, indent=2).replace("<", "\\u003c")
        + "\n  "
        + html[end:],
        encoding="utf-8",
    )

    output = tmp_path / "latest.json"
    extracted = _run(
        "extract_roadmap_spec.js",
        str(html_path),
        "--out",
        str(output),
    )

    assert extracted.returncode == 0, extracted.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["title"] == "保存后的最新标题"
    assert json.loads(extracted.stdout)["source_of_truth"] == "#deck-document"


def test_artifact_event_reads_roadmap_html_metadata(tmp_path) -> None:
    from box_agent.artifacts import make_artifact

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output = output_dir / "roadmap.html"
    result = _run(
        "render_roadmap_html.js",
        str(FIXTURES_DIR / "roadmap-spec-v1.json"),
        "--out",
        str(output),
    )
    assert result.returncode == 0, result.stderr

    artifact = make_artifact("tool-1", output, tmp_path)
    assert artifact.mime == "text/html"
    assert artifact.layout_id == "roadmap-swimlane-v1"


@pytest.mark.parametrize(
    ("item_count", "diagnostic_code"),
    [(30, "capacity.dense"), (80, "capacity.items-at-limit")],
)
def test_dense_capacity_cases_render_with_structured_diagnostics(
    tmp_path, item_count, diagnostic_code
) -> None:
    cases = _read_fixture("capacity-cases.json")
    assert any(case["items"] == item_count for case in cases["cases"])
    spec = _read_fixture("roadmap-spec-v1.json")
    template = spec["items"][0]
    spec["items"] = []
    for index in range(item_count):
        item = deepcopy(template)
        item["id"] = f"dense-{index + 1}"
        spec["items"].append(item)
    source = tmp_path / f"dense-{item_count}.json"
    output = tmp_path / f"dense-{item_count}.html"
    _write_json(source, spec)

    result = _run("render_roadmap_html.js", str(source), "--out", str(output))

    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert diagnostic_code in {entry["code"] for entry in metadata["diagnostics"]}
    assert output.exists()


def test_eighty_item_renderer_stays_below_interaction_budget(tmp_path) -> None:
    spec = _read_fixture("roadmap-spec-v1.json")
    template = spec["items"][0]
    spec["items"] = []
    for index in range(80):
        item = deepcopy(template)
        item["id"] = f"performance-{index + 1}"
        spec["items"].append(item)
    source = tmp_path / "performance-80.json"
    _write_json(source, spec)
    script = """
const fs = require('fs');
const { performance } = require('perf_hooks');
const { renderRoadmapHtml } = require(process.argv[1]);
const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
renderRoadmapHtml(spec);
const samples = [];
for (let index = 0; index < 7; index += 1) {
  const started = performance.now();
  renderRoadmapHtml(spec);
  samples.push(performance.now() - started);
}
samples.sort((left, right) => left - right);
console.log(JSON.stringify({ p90: samples[5], max: samples[6] }));
"""
    result = subprocess.run(
        [
            str(NODE),
            "-e",
            script,
            str(SCRIPTS_DIR / "roadmap_html_core.js"),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=SKILL_DIR,
    )

    assert result.returncode == 0, result.stderr
    timings = json.loads(result.stdout)
    assert timings["p90"] < 100, timings
