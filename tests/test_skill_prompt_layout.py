import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from box_agent.tools.skill_loader import (
    SKILL_SLOT_SENTINEL,
    SkillLoader,
    move_skill_slot_to_end,
)


def test_default_system_prompt_keeps_skill_metadata_slot_at_tail() -> None:
    prompt_path = (
        Path(__file__).resolve().parents[1]
        / "box_agent"
        / "config"
        / "system_prompt.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")

    assert prompt.count("{SKILLS_METADATA}") == 1
    assert prompt.index("{SANDBOX_INFO}") < prompt.index("{SKILLS_METADATA}")
    assert prompt.index("</output_constraints>") < prompt.index("{SKILLS_METADATA}")
    assert prompt.index("## Attention") < prompt.index("{SKILLS_METADATA}")


def test_move_skill_slot_to_end_preserves_prefix_and_single_slot() -> None:
    prompt = f"prefix\n\n{SKILL_SLOT_SENTINEL}\n\nsuffix\n"

    relocated = move_skill_slot_to_end(prompt)

    assert relocated.count(SKILL_SLOT_SENTINEL) == 1
    assert relocated.endswith(SKILL_SLOT_SENTINEL)
    assert relocated.index("prefix") < relocated.index("suffix")
    assert relocated.index("suffix") < relocated.index(SKILL_SLOT_SENTINEL)


def test_pptx_skill_makes_scaffold_and_framework_fallback_executable() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "box_agent"
        / "skills"
        / "document-skills"
        / "pptx"
        / "SKILL.md"
    )
    skill = skill_path.read_text(encoding="utf-8")

    assert (
        "Use `cd '<PRESENTATION_DIR>' && ${BOX_AGENT_NODE:-node}` on that same line"
        in skill
    )
    assert "do not split `cd` and the inspector across lines" in skill
    assert (
        "Removing numbers or rewriting a claim as qualitative prose does not verify it"
        in skill
    )
    assert (
        "the exact unavailable-data placeholder must appear in `message` or `bullets`"
        in skill
    )
    assert "every included series must contain a real numeric value" in skill
    assert "Never pad a gap with" in skill
    assert "or an invented baseline/forecast" in skill


def test_loaded_pptx_sync_example_preserves_literal_task_and_script_paths(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required to execute the documented POSIX example")
    skill_dir = (
        Path(__file__).resolve().parents[1]
        / "box_agent/skills/document-skills/pptx"
    )
    skill = SkillLoader(skill_dir).load_skill(skill_dir / "SKILL.md")
    assert skill is not None and not skill.broken
    prompt = skill.to_prompt()
    command = re.search(
        r"`(cd '<PRESENTATION_DIR>' &&[^\n]*<LOADER_EXPANDED_SYNC_SCRIPT>[^\n]*)`",
        prompt,
    )
    assert command is not None
    task_dir = tmp_path / "deck $HOME `printf wrong` O'Brien [final]"
    task_dir.mkdir()
    script_path = tmp_path / "sync $HOME O'Brien.py"
    script_path.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'cwd': os.getcwd(), 'manifest': sys.argv[1]}))\n",
        encoding="utf-8",
    )
    invocation = command.group(1).replace(
        "'<PRESENTATION_DIR>'", shlex.quote(str(task_dir))
    ).replace("'<LOADER_EXPANDED_SYNC_SCRIPT>'", shlex.quote(str(script_path)))

    result = subprocess.run(
        [bash, "-c", invocation],
        cwd=tmp_path,
        env={**os.environ, "BOX_AGENT_NODE": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "cwd": str(task_dir),
        "manifest": "assets/generated/manifest.json",
    }
    sync_table_row = next(
        line for line in prompt.splitlines()
        if line.startswith("| Sync generated image statuses |")
    )
    assert "including its literal directory prefix" in sync_table_row
    assert "do not add `cd`" not in sync_table_row
