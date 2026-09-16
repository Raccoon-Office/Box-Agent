"""The copied Skill runs in a shell without an installed box_agent package."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import PIL
import pytest

from tests.test_presentation_delivery import deck, pptx
from tests.test_pptx_design_plan import design_case, run

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "box_agent/skills/pptx/scripts"


def invoke(tmp_path, deck, requirements, task_pack, *, workspace=None, mutate_requirements=False, broken_input=None, include_artifact_dependencies=True, failed_stage=None):
    assert (SCRIPTS / "finalize.py").is_file(), "Skill must own a standalone finalizer"
    bundle = tmp_path / "copied skills"
    shutil.copytree(SCRIPTS, bundle / "pptx/scripts", dirs_exist_ok=True)
    exporter = bundle / "presentation-suite/skills/sn-ppt-standard/scripts"
    (exporter / "export_pptx").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / "scripts/presentation_suite_overlays/render_runtime.py", exporter / "render_runtime.py")
    (exporter / "deck.py").write_text('''import os,pathlib,sys
root=pathlib.Path(sys.argv[2])
if os.environ.get('FAILED_STAGE') == sys.argv[1]:
    raise SystemExit(3)
if sys.argv[1]=='build':
    (root/'present.html').write_text('slides/slide_01.html slides/slide_02.html')
''')
    fixture = bundle / "fixture.pptx"
    pptx(fixture)
    (exporter / "export_pptx/html_to_pptx.mjs").write_text('''import os,pathlib,shutil,sys
assert os.environ['BOX_AGENT_PPTX_NO_INSTALL']=='1'
assert os.environ['BOX_AGENT_PPTX_MANAGED_DELIVERY']=='1'
assert os.environ['PPT_SKILL_BROWSER_EXE']==os.environ['BOX_AGENT_BROWSER_EXECUTABLE_PATH']
shutil.copyfile(os.environ['FIXTURE_PPTX'],sys.argv[sys.argv.index('--output')+1])
if os.environ.get('MUTATE_REQUIREMENTS'):
    p=pathlib.Path(os.environ['MUTATE_REQUIREMENTS']); p.write_text(p.read_text()+' ')
''')
    (deck / "requirements.json").write_text(json.dumps(requirements))
    (deck / "task_pack.json").write_text(json.dumps(task_pack))
    if broken_input:
        target = deck / ("requirements.json" if "requirements" in broken_input else "task_pack.json")
        if broken_input.startswith("missing"):
            target.unlink()
        else:
            target.write_text("{bad json")
    # -S skips editable-install .pth hooks. Only Pillow's dependency directory is
    # added; the copied Skill must resolve sibling scripts on its own.
    bootstrap = ("import sys,runpy,importlib.util; "
                 + (f"sys.path.insert(0,{str(Path(PIL.__file__).parent.parent)!r}); "
                    if include_artifact_dependencies else "") +
                 "assert importlib.util.find_spec('box_agent') is None; "
                 "runpy.run_path(sys.argv.pop(1),run_name='__main__')")
    return subprocess.run([sys.executable, "-S", "-c", bootstrap,
        str(bundle / "pptx/scripts/finalize.py"), "--workspace", str(workspace or deck.parent),
        "--deck-dir", str(deck), "--requirements", str(deck / "requirements.json"),
        "--task-pack", str(deck / "task_pack.json")], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": "", "BOX_AGENT_PYTHON": sys.executable,
             "BOX_AGENT_NODE": sys.executable, "FIXTURE_PPTX": str(fixture),
             "BOX_AGENT_BROWSER_EXECUTABLE_PATH": str(tmp_path / "managed-browser"),
             "FAILED_STAGE": failed_stage or "",
             "MUTATE_REQUIREMENTS": str(deck / "requirements.json") if mutate_requirements else ""},
        capture_output=True, text=True, timeout=20)


def contract(deck, *, output="static_html", formats=None, pages=2):
    # Exercise the authoritative Entry work-data schema, including its nested
    # choices contract, rather than creating a second CLI-only pack shape.
    entry = (REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-entry/SKILL.md").read_text()
    example = entry.split("## `task_pack.json`", 1)[1].split("```json\n", 1)[1].split("```", 1)[0]
    pack = json.loads(example)
    formats = formats or ["html", "pptx"]
    pack.update(deck_dir=str(deck), workspace_root=str(deck.parent),
                ppt_mode="dazzle" if output == "dynamic_html" else "standard")
    pack["params"]["page_count"] = pages
    pack["choices"].update(output=output, static_postprocess=["pptx"] if "pptx" in formats else [])
    return ({"mode": "design", "output": output, "required_formats": formats,
             "expected_pages": pages, "revision": "request-1:corrected"}, pack)


def test_standalone_finalizer_builds_audits_exports_and_binds_actual_receipt(tmp_path, deck):
    result = invoke(tmp_path, deck, *contract(deck))
    assert result.returncode == 0, result.stderr + result.stdout
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "complete"
    assert [s["stage"] for s in receipt["steps"]] == ["build", "audit", "export"]
    assert {a["format"] for a in receipt["artifacts"]} == {"html", "pptx"}
    assert all(Path(a["path"]).is_file() for a in receipt["artifacts"])
    assert json.loads((deck / "_trace/finalize-receipt.json").read_text()) == receipt


@pytest.mark.parametrize("change", ["dynamic_vs_static", "format_downgrade", "wrong_directory", "unknown_output", "fast_dynamic", "top_level_only"])
def test_formal_script_rejects_inconsistent_work_data_without_inventing_user_selection(tmp_path, deck, change):
    requirements, pack = contract(deck)
    if change == "dynamic_vs_static": requirements["output"] = "dynamic_html"
    if change == "format_downgrade": pack["choices"]["static_postprocess"] = []
    if change == "wrong_directory": pack["deck_dir"] = str(deck.parent / "other")
    if change == "unknown_output": pack["choices"]["output"] = "unknown"
    if change == "fast_dynamic": requirements.update(mode="fast", output="dynamic_html")
    if change == "top_level_only": pack["static_postprocess"] = pack["choices"].pop("static_postprocess")
    result = invoke(tmp_path, deck, requirements, pack)
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "error" and receipt["error"]
    assert not (deck / "present.html").exists()
    assert not list(deck.glob("*.pptx"))


def test_fast_workflow_keeps_workspace_root_and_html_default(tmp_path, design_case):
    root, _, _, _ = design_case
    prepared = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert prepared.returncode == 0, prepared.stderr
    result = invoke(tmp_path, root,
        {"mode": "fast", "output": "fast_html", "required_formats": ["html"],
         "expected_pages": 3, "revision": "fast:1"},
        {"deck_dir": str(root), "choices": {"output": "fast_html"}}, workspace=root)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "complete" and receipt["warnings"]
    assert [a["format"] for a in receipt["artifacts"]] == ["html"]
    assert not receipt.get("steps") and not (root / "present.html").exists()
    assert not list(root.glob("*.pptx"))


def test_dynamic_workflow_only_verifies_its_current_deck_and_render_evidence(tmp_path, deck):
    from PIL import Image
    html = deck / "deck.html"
    html.write_text('<html><section class="slide">Motion lesson</section></html>')
    shots = deck / "shots"
    shots.mkdir()
    png = shots / "page_01.png"
    Image.new("RGB", (128, 72), "green").save(png)
    (shots / "render.json").write_text(json.dumps({"deck": str(html),
        "deck_sha256": hashlib.sha256(html.read_bytes()).hexdigest(), "mode": "all",
        "n_pages": 1, "pages": [{"page": 1, "png": str(png)}]}))
    result = invoke(tmp_path, deck, *contract(deck, output="dynamic_html", formats=["html"], pages=1))
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["artifacts"][0]["path"] == str(html)
    assert not receipt.get("steps") and not (deck / "present.html").exists()
    assert not list(deck.glob("*.pptx"))


def test_failed_refinalization_replaces_old_success_receipt(tmp_path, deck):
    assert invoke(tmp_path, deck, *contract(deck)).returncode == 0
    result = invoke(tmp_path, deck, *contract(deck), mutate_requirements=True)
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "partial" and "changed" in receipt["error"]
    assert json.loads((deck / "_trace/finalize-receipt.json").read_text()) == receipt


@pytest.mark.parametrize("broken_input", ["missing_requirements", "malformed_requirements", "missing_pack", "malformed_pack"])
def test_input_read_failure_replaces_previous_success_receipt(tmp_path, deck, broken_input):
    assert invoke(tmp_path, deck, *contract(deck)).returncode == 0
    result = invoke(tmp_path, deck, *contract(deck), broken_input=broken_input)
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "error" and receipt["error"]
    assert json.loads((deck / "_trace/finalize-receipt.json").read_text()) == receipt


def test_standalone_help_does_not_require_loading_optional_artifact_dependencies(tmp_path):
    result = subprocess.run([sys.executable, "-S", str(SCRIPTS / "finalize.py"), "--help"],
                            cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--task-pack" in result.stdout


def test_entry_html_only_choice_runs_build_and_audit_without_export(tmp_path, deck):
    result = invoke(tmp_path, deck, *contract(deck, formats=["html"]))
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert [s["stage"] for s in receipt["steps"]] == ["build", "audit"]
    assert [a["format"] for a in receipt["artifacts"]] == ["html"]
    assert not list(deck.glob("*.pptx"))


def test_guarded_audit_failure_keeps_command_exit_status_and_replaces_old_receipt(tmp_path, deck):
    result = invoke(tmp_path, deck, *contract(deck, formats=["html"]), failed_stage="audit")
    assert result.returncode == 1, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "error"
    assert receipt["steps"][-1]["stage"] == "audit"
    assert receipt["steps"][-1]["returncode"] == 3


def test_missing_artifact_dependency_reports_error_and_replaces_old_receipt(tmp_path, deck):
    assert invoke(tmp_path, deck, *contract(deck)).returncode == 0
    result = invoke(tmp_path, deck, *contract(deck), include_artifact_dependencies=False)
    assert result.returncode == 1
    assert result.stdout, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "partial" and "PIL" in receipt["error"]
    assert json.loads((deck / "_trace/finalize-receipt.json").read_text()) == receipt
