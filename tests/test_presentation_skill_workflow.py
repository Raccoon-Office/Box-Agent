"""Workflow contract fixtures; semantic compliance also needs model evaluation."""
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "box_agent/skills"
NAMES = ["pptx", *[f"presentation-suite/skills/sn-ppt-{name}" for name in
                  ("entry", "story", "standard", "dazzle", "tools", "doctor")]]


@pytest.mark.parametrize("name", NAMES)
def test_workflow_explains_generic_adoption_and_reference_only_reads(name):
    content = (SKILLS / name / "SKILL.md").read_text()
    for parameter in ('usage="use"', 'usage="reference"', 'usage="release"', 'replace=', 'new_task=True'):
        assert parameter in content
    assert "presentation_delivery" not in content
    assert "已核验的运行时选择" not in content


def test_formal_finalizer_instructions_carry_explicit_inputs_without_authorization_claim():
    content = (SKILLS / "pptx/SKILL.md").read_text()
    for flag in ("--workspace", "--deck-dir", "--requirements", "--task-pack"):
        assert flag in content
    assert "finalize-receipt.json" in content
    assert "不能证明用户选择" in content
    assert "不依赖安装 box_agent" in content


def test_workflow_scenarios_preserve_semantic_and_recovery_cases():
    cases = json.loads((REPO / "tests/fixtures/presentation_workflow_cases.json").read_text())
    assert len(cases) >= 18
    assert {c["action"] for c in cases} >= {"choose", "clarify", "dynamic", "resume", "reference"}
    content = (SKILLS / "pptx/SKILL.md").read_text()
    for case in cases:
        assert case["request"] and case["reason"]
        assert case["policy_anchor"] in content, case["id"]


def test_box_finalizer_precedes_final_pixels_and_does_not_repeat_manual_export():
    standard = (SKILLS / "presentation-suite/skills/sn-ppt-standard/SKILL.md").read_text()
    entry = (SKILLS / "presentation-suite/skills/sn-ppt-entry/SKILL.md").read_text()
    assert "最终像素检查前" in standard
    assert "Box 第 6 步的 finalize 已调用" in standard
    assert "不再手工重复 build/audit/export" in entry
    assert "每次失败由运行时恢复" not in standard
    assert "不依赖宿主自动恢复" in standard


def test_required_tool_contract_uses_same_finalizer_order_and_receipt_paths():
    reference = (SKILLS / "presentation-suite/skills/sn-ppt-standard/references/box-agent-tool-contract.md").read_text()
    assert "最终 PNG 与 `present.html` 验证通过后才导出" not in reference
    assert "最终像素检查前" in reference
    assert "finalize-receipt.json" in reference
    assert "artifacts[].path" in reference
    assert "stdout JSON 的精确 `output`" not in reference
