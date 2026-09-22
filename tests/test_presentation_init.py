"""The initial CSS copy is independent of planning, fonts, rendering and QA."""

import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys

import pytest


STANDARD = Path(os.environ.get(
    "PRESENTATION_STANDARD_SOURCE",
    Path(__file__).resolve().parents[1]
    / "box_agent/skills/presentation-suite/skills/sn-ppt-standard",
))


@pytest.fixture(params=[False, True], ids=["plain", "box-overlays"])
def skill(tmp_path, request):
    target = tmp_path / "read only skill"
    shutil.copytree(STANDARD / "scripts", target / "scripts")
    (target / "references").mkdir()
    shutil.copyfile(STANDARD / "references/base-template.css",
                    target / "references/base-template.css")
    if request.param and "PRESENTATION_STANDARD_SOURCE" in os.environ:
        repo = Path(__file__).resolve().parents[1]
        sync = runpy.run_path(str(repo / "scripts/sync_presentation_suite.py"))
        relative = "skills/sn-ppt-standard/scripts/deck.py"
        data = (target / "scripts/deck.py").read_bytes()
        for overlay in ("_apply_integration_overlay", "_host_playwright_overlay",
                        "_image_inspection_recovery_overlay"):
            data = sync[overlay](relative, data)
        (target / "scripts/deck.py").write_bytes(data)
    return target


def init(skill, root, cwd):
    return subprocess.run(
        [sys.executable, str(skill / "scripts/deck.py"), "init", str(root)],
        cwd=cwd, capture_output=True, text=True, timeout=15,
    )


def test_init_copies_exact_template_from_any_cwd_without_producing_plans(skill, tmp_path):
    root = tmp_path / "deck with spaces"
    root.mkdir()
    result = init(skill, root, tmp_path)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == {
        "deck_dir": str(root.resolve()), "base_css": str(root / "base.css"),
        "created": True, "qa": "not-run",
    }
    assert (root / "base.css").read_bytes() == (skill / "references/base-template.css").read_bytes()
    assert sorted(p.name for p in root.iterdir()) == ["base.css"]
    assert "PASS" not in result.stdout


def test_init_preserves_existing_user_css_even_without_template(skill, tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    css = root / "base.css"
    css.write_bytes(b"/* user design */\r\n:root{--canvas-w:900px;}\r\n")
    original = css.read_bytes()
    modified_at = css.stat().st_mtime_ns
    (skill / "references/base-template.css").unlink()
    for _ in range(2):
        result = init(skill, root, tmp_path)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["created"] is False
        assert css.read_bytes() == original
        assert css.stat().st_mtime_ns == modified_at


@pytest.mark.parametrize("kind", ["symlink", "broken-symlink", "directory"])
def test_init_rejects_non_regular_css_without_writing_elsewhere(skill, tmp_path, kind):
    root = tmp_path / "deck"
    root.mkdir()
    target = tmp_path / "outside.css"
    target.write_text("outside user design")
    css = root / "base.css"
    if kind == "directory":
        css.mkdir()
    else:
        css.symlink_to(target if kind == "symlink" else tmp_path / "absent.css")
    result = init(skill, root, tmp_path)
    assert result.returncode != 0
    assert "base.css" in result.stderr
    assert target.read_text() == "outside user design"
    assert not (tmp_path / "absent.css").exists()


def test_init_missing_template_fails_without_placeholder(skill, tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    (skill / "references/base-template.css").unlink()
    result = init(skill, root, tmp_path)
    assert result.returncode != 0
    assert "base-template.css" in result.stderr
    assert not (root / "base.css").exists()


def test_init_requires_existing_absolute_deck(skill, tmp_path):
    for root in (".", tmp_path / "not-created-by-entry"):
        result = init(skill, root, tmp_path)
        assert result.returncode != 0
    assert not (tmp_path / "not-created-by-entry").exists()
    assert not (tmp_path / "base.css").exists()


@pytest.mark.asyncio
async def test_init_runs_through_bash_permission_engine_with_read_only_skill(skill, tmp_path, monkeypatch):
    from box_agent.tools import safety
    from box_agent.tools.bash_tool import BashTool
    from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine

    root = tmp_path / "deck with spaces"
    root.mkdir()
    monkeypatch.setattr(safety, "BUILTIN_SKILLS_ROOT", skill)
    permissions = PermissionEngine(CapabilityPolicy(
        filesystem_scope="session_workspace",
        allowed_directories=(str(Path(sys.executable).resolve().parent),),
    ), tmp_path)
    permissions._builtin_skills_dir = skill
    tool = BashTool(workspace_dir=str(tmp_path), permission_engine=permissions)
    # The former cp example passed the source guard alone, but this second
    # permission layer classifies its Skill source as a write target.
    denied = await tool.execute(command=shlex.join([
        "cp", "-n", str(skill / "references/base-template.css"), str(root / "base.css"),
    ]))
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    command = shlex.join([sys.executable, str(skill / "scripts/deck.py"), "init", str(root)])
    result = await tool.execute(command=command)
    assert result.success, result.error or result.stderr
    assert json.loads(result.stdout)["created"] is True
    assert (root / "base.css").read_bytes() == (skill / "references/base-template.css").read_bytes()
    assert not result.permission_request


def test_init_does_not_call_prepare_or_font_pipeline(skill, tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(skill / "scripts"))
    module = runpy.run_path(str(skill / "scripts/deck.py"))["main"].__globals__

    def unexpected(*args, **kwargs):
        pytest.fail("init must not prepare, parse plans, render or audit")

    for name in ("_prepare_workspace", "_sync_speech", "bundle_workspace", "render_all",
                 "_ensure_canvas_reset", "_audit_workspace"):
        monkeypatch.setitem(module, name, unexpected)
    assert module["main"](["init", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True
