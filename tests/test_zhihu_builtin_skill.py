from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from box_agent.tools.skill_loader import SkillLoader


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "box_agent" / "skills"


def test_zhihu_skill_is_a_marketplace_source_not_builtin() -> None:
    manifest = json.loads((SKILLS_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    entries = {item["name"]: item for item in manifest["skills"]}

    assert "zhihu" not in entries
    assert (SKILLS_ROOT / "zhihu" / "SKILL.md").is_file()

    loader = SkillLoader(sources=[(SKILLS_ROOT, "builtin")])
    loader.discover_skills()
    assert loader.get_skill("zhihu") is None

    loader = SkillLoader(sources=[(SKILLS_ROOT / "zhihu", "user")])
    loader.discover_skills()
    skill = loader.get_skill("zhihu")

    assert skill is not None
    assert skill.source == "user"
    assert skill.skill_path == SKILLS_ROOT / "zhihu" / "SKILL.md"
    assert (SKILLS_ROOT / "zhihu" / "scripts" / "run.ps1").is_file()
    assert (SKILLS_ROOT / "zhihu" / "scripts" / "run.sh").is_file()


def test_zhihu_skill_uses_the_officev3_managed_cli_and_credentials() -> None:
    skill_root = SKILLS_ROOT / "zhihu"
    package_manifest = json.loads((skill_root / "manifest.json").read_text(encoding="utf-8"))
    skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert package_manifest["distribution"] == {
        "mode": "host-bundled",
        "host": "officev3",
    }
    assert "update_manifest_url" not in package_manifest["cli"]
    assert "Officev3 卡片配置" in skill_text
    assert "Agent 通过标准输入交给 CLI" in skill_text
    assert "第三方数据 → 其他 → 知乎" in skill_text

    all_docs = "\n".join(
        path.read_text(encoding="utf-8") for path in skill_root.rglob("*.md")
    )
    assert "在对话中提供 Secret" in all_docs
    assert "auth set --secret-stdin" in all_docs
    assert "模型上下文之外" not in all_docs

    for script_name in ("setup.ps1", "setup.sh"):
        setup_text = (skill_root / "scripts" / script_name).read_text(encoding="utf-8")
        assert "HOST_MANAGED_INSTALL" in setup_text
        assert "developer-cdn.zhihu.com" not in setup_text

    for script_name in ("run.ps1", "setup.ps1"):
        script_text = (skill_root / "scripts" / script_name).read_text(encoding="utf-8")
        assert script_text.isascii(), "Windows PowerShell 5 requires BOM-less scripts to be ASCII"


def test_zhihu_posix_scripts_are_syntax_valid_and_report_host_repair(tmp_path: Path) -> None:
    shell = shutil.which("sh")
    if shell is None:
        return

    scripts_dir = SKILLS_ROOT / "zhihu" / "scripts"
    for script_name in ("run.sh", "setup.sh"):
        subprocess.run([shell, "-n", str(scripts_dir / script_name)], check=True)

    env = os.environ.copy()
    env.pop("ZHIHU_CLI_HOME", None)
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [shell, str(scripts_dir / "run.sh"), "status"],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["installed"] is False
    assert payload["update_check"]["status"] == "host_managed"
    assert payload["next_action"] == "repair_host_install"
