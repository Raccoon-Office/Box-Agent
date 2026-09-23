"""Prepare failures expose existing planning inputs without a new validator."""

import json
import os
from pathlib import Path
import runpy
import shutil

import pytest


STANDARD = Path(os.environ.get(
    "PRESENTATION_STANDARD_SOURCE",
    Path(__file__).resolve().parents[1]
    / "box_agent/skills/presentation-suite/skills/sn-ppt-standard",
))


@pytest.fixture
def command(tmp_path, monkeypatch):
    skill = tmp_path / "skill with spaces"
    for directory in ("scripts", "references", "assets/vendor"):
        shutil.copytree(STANDARD / directory, skill / directory)
    monkeypatch.syspath_prepend(str(skill / "scripts"))
    module = runpy.run_path(str(skill / "scripts/deck.py"))["main"].__globals__
    # Font downloading is outside this receipt test. All plan validation,
    # speech synchronization, CSS initialization and runtime copying are real.
    monkeypatch.setitem(module, "bundle_workspace", lambda *a, **k: {"faces": []})
    return skill, module["main"]


def page(number, *, speech=True, presentation=None):
    visual = ("- image_opportunity: generated_ok\n"
              f"- presentation: {presentation}\n" if presentation else
              "- image_opportunity: typography_only\n")
    return (f"# Slide {number:02d} — 页面 {number}\n"
            f"## 最终屏显文案\n- 标题：页面 {number}\n"
            f"## 视觉实现\n{visual}"
            "## 来源\n- user-provided\n"
            + (f"## 口语讲稿\n这是第 {number} 页的完整讲述。\n" if speech else ""))


def workspace(tmp_path, **page_options):
    root = tmp_path / "deck with spaces"
    (root / "plan").mkdir(parents=True)
    for number in (1, 2):
        (root / f"plan/slide_{number:02d}.md").write_text(
            page(number, **page_options), encoding="utf-8")
    return root


@pytest.mark.parametrize("expected_args", [[], ["--expected", "2"]])
def test_prepare_reports_all_missing_speech_with_exact_recovery_paths(
    command, tmp_path, capsys, expected_args,
):
    skill, main = command
    root = workspace(tmp_path, speech=False)
    original = {p: p.read_bytes() for p in (root / "plan").iterdir()}
    assert main(["prepare", str(root), *expected_args]) == 1
    output = capsys.readouterr()
    assert "prepare:FAIL" in output.err
    for number in (1, 2):
        assert f"slide_{number:02d}.md: missing spoken script section" in output.err
    guidance = json.loads(output.err.splitlines()[-1])
    assert guidance == {
        "deck_dir": str(root),
        "plan_dir": str(root / "plan"),
        "planning_reference": str(skill / "references/planning-contract.md"),
        "retry_after_fix": ["python", str(skill / "scripts/deck.py"),
                            "prepare", str(root), *expected_args],
    }
    assert Path(guidance["planning_reference"]).is_file()
    assert {p: p.read_bytes() for p in (root / "plan").iterdir()} == original
    assert not (root / "speech.md").exists()
    assert "PASS" not in output.out
    assert "next_instructions" not in output.err
    assert "slide.md" not in output.err
    # After the author fills the reported omissions, the returned argv is
    # accepted by the same CLI and preserves the requested page count.
    for number in (1, 2):
        (root / f"plan/slide_{number:02d}.md").write_text(page(number), encoding="utf-8")
    assert main(guidance["retry_after_fix"][2:]) == 0
    assert "prepare:PASS" in capsys.readouterr().out


def test_prepare_keeps_invalid_presentation_error_and_does_not_normalize_plans(
    command, tmp_path, capsys,
):
    _, main = command
    root = workspace(tmp_path, presentation="split-media")
    original = {p: p.read_bytes() for p in (root / "plan").iterdir()}
    assert main(["prepare", str(root), "--expected", "2"]) == 1
    output = capsys.readouterr()
    for number in (1, 2):
        assert f"plan/slide_{number:02d}.md: raster page requires presentation=" in output.err
    assert json.loads(output.err.splitlines()[-1])["plan_dir"] == str(root / "plan")
    assert {p: p.read_bytes() for p in (root / "plan").iterdir()} == original
    assert not (root / "speech.md").exists()


def test_prepare_success_keeps_existing_contract_and_does_not_request_retry(
    command, tmp_path, capsys,
):
    _, main = command
    root = workspace(tmp_path)
    # A legacy task need not migrate global planning fields to run prepare.
    (root / "plan/deck.md").write_text("language: zh\n", encoding="utf-8")
    assert main(["prepare", str(root), "--expected", "2"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "speech:PASS pages=2 language=zh" in output.out
    assert "prepare:PASS" in output.out
    assert "retry_after_fix" not in output.out
    assert "next_instructions" not in output.out
    speech = (root / "speech.md").read_text(encoding="utf-8")
    for number in (1, 2):
        assert f"这是第 {number} 页的完整讲述。" in speech
    assert not (root / "plan/design-brief.md").exists()


def test_non_prepare_failure_does_not_gain_planning_guidance(command, tmp_path, capsys):
    _, main = command
    root = workspace(tmp_path, speech=False)
    assert main(["sync", str(root), "--expected", "2"]) == 1
    assert capsys.readouterr().err == (
        "sync:FAIL\nslide_01.md: missing spoken script section\n"
        "slide_02.md: missing spoken script section\n"
    )


def test_prepare_without_plans_does_not_invent_them(command, tmp_path, capsys):
    _, main = command
    root = tmp_path / "no plans"
    root.mkdir()
    assert main(["prepare", str(root), "--expected", "2"]) == 1
    output = capsys.readouterr()
    assert "no plan/slide_NN.md files found" in output.err
    assert json.loads(output.err.splitlines()[-1])["plan_dir"] == str(root / "plan")
    assert not (root / "plan").exists()


def test_prepare_font_failure_is_not_reclassified_as_missing_planning(
    command, tmp_path, capsys, monkeypatch,
):
    _, main = command
    root = workspace(tmp_path)

    def failed_fonts(*args, **kwargs):
        raise RuntimeError("fixture font source unavailable")

    monkeypatch.setitem(main.__globals__, "bundle_workspace", failed_fonts)
    assert main(["prepare", str(root), "--expected", "2"]) == 1
    output = capsys.readouterr()
    assert output.err.startswith("prepare:FAIL\nfixture font source unavailable\n")
    assert "missing spoken script" not in output.err
    assert "prepare:PASS" not in output.out
    assert "speech:PASS" in output.out  # The existing stage ordering is retained.
    assert json.loads(output.err.splitlines()[-1])["deck_dir"] == str(root)
