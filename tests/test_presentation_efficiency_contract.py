"""Executable command and reference contracts for the static production flow."""

import os
import json
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import sys

import pytest


STANDARD = Path(os.environ.get("PRESENTATION_STANDARD_SOURCE", Path(__file__).resolve().parents[1]
    / "box_agent/skills/presentation-suite/skills/sn-ppt-standard"))


def test_public_entry_uses_standard_review_preparation_before_final_pixels():
    entry = (Path(__file__).resolve().parents[1] / "box_agent/skills/pptx/SKILL.md").read_text()
    static_delivery = entry.split("设计模式的静态任务始终交付", 1)[1].split("恢复已有设计模式任务", 1)[0]
    assert "`deck.py review-prep`" in static_delivery
    assert "`deck.py build` 与 `deck.py audit`" not in static_delivery
    assert "最终全册像素" in static_delivery
    assert "不重复" in static_delivery and "验收" in static_delivery
    assert "present.html" in static_delivery and "HTML 链接" in static_delivery


def test_planning_quality_route_names_an_actual_unique_heading():
    plan = (STANDARD / "references/planning-contract.md").read_text()
    quality = (STANDARD / "references/quality-checklist.md").read_text()
    title = re.search(r"^- quality-checklist.md[:：]\s*(.+)$", plan, re.M).group(1)
    assert len(re.findall(r"^#{1,6} " + re.escape(title) + r"$", quality, re.M)) == 1


def test_subject_only_example_matches_existing_visual_field_parser():
    plan = (STANDARD / "references/planning-contract.md").read_text()
    visual = plan.split("## 视觉实现\n", 1)[1].split("## Reference route", 1)[0]
    assert re.search(r"^- subject_only[:：]", visual, re.M)


def test_formal_review_commands_prepare_full_deck_without_bypass():
    for relative in ("SKILL.md", "subagents/review.md", "references/box-agent-tool-contract.md"):
        text = (STANDARD / relative).read_text()
        commands = re.findall(r"^.*(?:python|node).*scripts/[^\n]+$", text, re.M)
        prep = [line for line in commands if "deck.py" in line and "review-prep" in line]
        assert prep, relative
        assert all("--expected" in line and "--pages" not in line for line in prep)
        assert all("--force" not in line for line in commands if "html_to_pptx" in line)
    review = (STANDARD / "subagents/review.md").read_text()
    assert "## Final review contract" in review
    assert "pending_parent_verification" in review


def test_box_delegation_uses_source_files_without_mandatory_input_packaging():
    text = (STANDARD / "references/box-agent-tool-contract.md").read_text()
    commands = re.findall(r"^.*python.*scripts/group_input\.py[^\n]*$", text, re.M)
    assert not commands, "New page groups must not require the formatting-sensitive packager"
    assert "files" in text and "write_scope" in text and "task_pack.deck_dir" in text
    assert "budget" in text and "省略" in text
    assert not re.search(r"max_(?:steps|tool_calls)[\"']?\s*[:=]\s*\d+", text)


@pytest.mark.parametrize("relative", [
    "SKILL.md",
    "subagents/slide.md",
    "references/planning-contract.md",
    "references/quality-checklist.md",
])
def test_production_instructions_do_not_require_parent_approval_between_drafts(relative):
    text = (STANDARD / relative).read_text()
    # These clauses caused a completed child to wait for a parent that cannot
    # resume it. A child with its own renderer can still inspect pages serially.
    conflicting_clauses = (
        "当前页达到 ready 后才进入下一页",
        "当前页 ready 后才进入下一页",
        "最后一次修改尚未被父级用新像素验证时不得进入下一页",
        "当前页由父级判为 ready 后",
        "页组规模应允许同一个 Slide 按页序完成每页独立像素闭环",
    )
    assert not any(clause in text for clause in conflicting_clauses), relative


def test_planning_routes_do_not_require_markdown_identity_for_input_packaging():
    text = (STANDARD / "references/planning-contract.md").read_text()
    route = text.split("### Reference route 的定位要求", 1)[1]
    box_route, other_route = route.split("其他环境使用", 1)
    assert "不要求为输入包装逐字匹配 Markdown 标题" in box_route
    assert "与实际 Markdown 标题逐字匹配" in other_route
    # These real design/export inputs remain required; removing the packager
    # must not remove their existing machine-readable contract.
    for field in ("image_opportunity", "presentation", "subject_only", "asset_id"):
        assert field in text


def test_box_integration_does_not_leave_conflicting_image_batch_limits():
    data = (STANDARD / "references/box-agent-tool-contract.md").read_bytes()
    if "PRESENTATION_STANDARD_SOURCE" in os.environ:
        repo = Path(__file__).resolve().parents[1]
        sync = runpy.run_path(str(repo / "scripts/sync_presentation_suite.py"))
        relative = "skills/sn-ppt-standard/references/box-agent-tool-contract.md"
        data = sync["_apply_integration_overlay"](relative, data)
    bundled = data.decode("utf-8")
    assert "每批最多 4 张图" in bundled
    assert "每次最多 6 张" not in bundled


def test_non_box_input_packaging_keeps_its_existing_command():
    text = (STANDARD / "SKILL.md").read_text()
    production = text.split("### 阶段 4：素材与页面制作", 1)[1].split("### 阶段 5", 1)[0]
    non_box = next(line for line in production.splitlines() if line.startswith("**其他环境的输入准备：**"))
    assert 'scripts/group_input.py" "$DECK_DIR" --expected N' in non_box


def test_box_static_creation_defaults_to_parent_without_changing_design_groups():
    text = (STANDARD / "SKILL.md").read_text()
    assert "### Box-Agent 静态新建的执行方式" in text
    policy = text.split("### Box-Agent 静态新建的执行方式", 1)[1].split("### 1.2", 1)[0]
    assert "主 Agent 默认连续制作" in policy
    assert "设计分组不等于执行分组" in policy
    assert "production_group" in policy and "write_scope" in policy
    assert "所有写页子任务返回后" in policy
    assert "素材" in policy and "最终像素" in policy


@pytest.mark.parametrize("relative", [
    "references/planning-contract.md", "subagents/slide.md",
    "subagents/review.md", "references/box-agent-tool-contract.md",
    "references/quality-checklist.md",
])
def test_box_creation_execution_policy_is_referenced_by_every_role(relative):
    text = (STANDARD / relative).read_text()
    assert "Box-Agent 静态新建的执行方式" in text


def test_box_creation_allows_bounded_batch_reads_and_multi_page_output():
    text = (STANDARD / "subagents/slide.md").read_text()
    assert "允许同一模型回合读取" in text
    assert "连续提交多页" in text
    assert "不将全组逐页计划与参考合成一次大读" not in text
    assert "截断" in text and "新鲜" in text
    contract = (STANDARD / "references/box-agent-tool-contract.md").read_text()
    assert "不将全组计划和参考合成一次大读" not in contract


def test_box_creation_planning_does_not_duplicate_implementation_geometry():
    text = (STANDARD / "references/planning-contract.md").read_text()
    assert "像素级布局" in text
    assert "不重复生成预演报告" in text
    for field in ("最终屏显文案", "视觉实现", "spatial_budget", "production_group",
                  "image_opportunity", "presentation", "subject_only", "asset_id", "口语讲稿"):
        assert field in text


def test_box_creation_parent_repairs_only_after_child_writers_return():
    text = (STANDARD / "references/box-agent-tool-contract.md").read_text()
    assert "所有写页子任务返回后" in text
    assert "主 Agent 集中修复" in text
    assert "重渲" in text and "复看" in text
    assert "prepare" in text and "素材验收" in text


def test_page_workflow_separates_parent_rendering_from_child_return():
    text = (STANDARD / "subagents/slide.md").read_text()
    step = text.split("4. **Box-Agent", 1)[1].split("```bash", 1)[0]
    assert "**Box-Agent 子任务：**" in step
    parent, child = step.split("**Box-Agent 子任务：**", 1)
    assert "主 Agent" in parent and "批量渲染" in parent
    assert "不自行渲染" not in parent
    assert "一次返回" in child and "不自行渲染" in child
    assert "其他具备子内渲染和看图能力的环境" in child


def test_multi_page_submission_does_not_change_other_harness_page_loop():
    text = (STANDARD / "subagents/slide.md").read_text()
    step = next(line for line in text.splitlines() if line.startswith("2. 按 `pages`"))
    assert "Box-Agent 静态新建允许在同一回合连续提交多页" in step
    other = text.split("**其他具备子内渲染和看图能力的环境：**", 1)[1].split("第一次像素检查", 1)[0]
    assert "Slide 渲染当前页并看图" in other
    assert "完成步骤 5 的必要修复与复验后再制作下一页" in other


def test_method_routes_follow_production_stage_not_delegated_role():
    text = (STANDARD / "SKILL.md").read_text()
    routes = text.split("### 1.5 Reference 路由", 1)[1].split("### 1.6", 1)[0]
    for stage, method in (("阶段 4", "subagents/slide.md"),
                          ("阶段 5", "subagents/review.md")):
        row = next(line for line in routes.splitlines() if stage in line and line.startswith("|"))
        assert method in row and "quality-checklist.md" in row
        assert "已有编辑" in row, "Stage routing must retain the existing edit quality route"
        assert (STANDARD / method).is_file()
    production = text.split("### 阶段 4：素材与页面制作", 1)[1].split("### 阶段 5", 1)[0]
    review = text.split("### 阶段 5：全册 Review 与交付", 1)[1].split("## 3. 编辑 PPT", 1)[0]
    assert "先完整读取 `subagents/slide.md`" in production
    assert "先完整读取 `subagents/review.md`" in review
    assert "当前上下文" in production and "复用" in production


def test_planning_finishes_with_preparation_before_html():
    text = (STANDARD / "SKILL.md").read_text()
    planning = text.split("### 阶段 3：全局规划与字体前置", 1)[1].split("### 阶段 4", 1)[0]
    assert "本阶段以 `deck.py prepare` 成功为结束" in planning
    assert "实际字体" in planning and "首张 HTML" in planning
    assert "assets/fonts/manifest.json" in planning
    assert "plan/image-strategy.json" in planning


def test_base_css_init_example_preserves_template_and_existing_deck(tmp_path, monkeypatch):
    text = (STANDARD / "SKILL.md").read_text()
    command = next(line for line in text.splitlines() if 'scripts/deck.py" init ' in line)
    deck = tmp_path / "deck with spaces"
    deck.mkdir()
    expanded = command.replace("$SKILL_ROOT", str(STANDARD)).replace("$DECK_DIR", str(deck))
    from box_agent.tools import safety
    monkeypatch.setattr(safety, "BUILTIN_SKILLS_ROOT", STANDARD)
    assert safety.builtin_skill_command_write_error(expanded, deck) is None
    arguments = shlex.split(expanded)
    arguments[0] = sys.executable
    subprocess.run(arguments, cwd=deck, check=True, capture_output=True)
    copied = deck / "base.css"
    original = (STANDARD / "references/base-template.css").read_bytes()
    assert copied.read_bytes() == original
    copied.write_text("existing user design")
    subprocess.run(arguments, cwd=deck, check=True, capture_output=True)
    assert copied.read_text() == "existing user design"
    assert (STANDARD / "references/base-template.css").read_bytes() == original


def test_script_root_is_owned_by_standard_and_can_be_recovered_directly():
    text = (STANDARD / "references/box-agent-tool-contract.md").read_text()
    paths = text.split("## 1. 路径与执行所有权", 1)[1].split("## 2.", 1)[0]
    assert 'get_skill(skill_name="sn-ppt-standard")' in paths
    assert "Skill Root Directory" in paths
    assert "压缩" in paths and "重新加载" in paths
    assert "pptx" in paths and "Entry" in paths and "Story" in paths


def test_repair_decision_precedes_writes_without_a_new_report_or_tool_quota():
    text = (STANDARD / "subagents/slide.md").read_text()
    repair = text.split("## 7. 返工保护与返回合同", 1)[1]
    assert "先确定同页完整修法，再落盘" in repair
    assert "新鲜像素" in repair and "当前 HTML" in repair
    assert "不新增修复计划文件" in repair
    assert "不相邻" in repair and "多个必要" in repair
    assert "一次合并修复不等于一次工具调用" in repair


def test_final_review_has_one_method_route_instead_of_two_full_diagnoses():
    root = (STANDARD / "SKILL.md").read_text()
    stage = root.split("### 阶段 5：全册 Review 与交付", 1)[1].split("## 3. 编辑 PPT", 1)[0]
    assert "subagents/review.md" in stage
    assert "随后严格两段执行" not in stage
    assert "Review 诊断/有限返修 → review-prep" not in root
    review = (STANDARD / "subagents/review.md").read_text()
    diagnosis = review.split("### A.", 1)[1].split("### B.", 1)[0]
    assert "review-prep" in diagnosis and "prepared" in diagnosis
    assert "review_contact" in diagnosis
    assert "其他环境" in diagnosis, "Non-Box review must keep its existing contact preparation"


def test_repair_verification_does_not_require_another_open_ended_confirmation():
    slide = (STANDARD / "subagents/slide.md").read_text()
    assert "必须先对当前新 PNG 再做一次中性开放式确认" not in slide
    assert "结构性改动必须由两次一致的新鲜像素判断" not in slide
    for name in ("subagents/slide.md", "subagents/review.md"):
        method = (STANDARD / name).read_text()
        assert "review-issues.md" in method
        assert "问题对象" in method and "成立条件" in method
        assert "受影响" in method and "新鲜像素" in method


def test_speech_findings_join_formal_review_instead_of_starting_an_extra_build():
    review = (STANDARD / "subagents/review.md").read_text()
    speech = next(line for line in review.splitlines() if line.startswith("讲稿验收不能"))
    assert "Box-Agent 静态新建" in speech
    assert "账本" in speech and "review-prep" in speech
    assert "其他路径" in speech and "deck.py build" in speech


def test_machine_candidates_do_not_conflict_with_visual_severity():
    checklist = (STANDARD / "references/quality-checklist.md").read_text()
    lint = checklist.split("## 三、", 1)[1].split("## 怎么用", 1)[0]
    assert "必须为零;等分网格" not in lint
    assert "`⚠ COVER-OOB` 为零" not in lint
    assert "中文字后紧跟半角" not in checklist
    assert "4.5:1" in checklist and "3:1" in checklist
    assert "crop_contract" in checklist and "signature_visual" in checklist


def test_generated_recovery_keeps_final_coverage_without_restarting_diagnosis():
    repo = Path(__file__).resolve().parents[1]
    sync = runpy.run_path(str(repo / "scripts/sync_presentation_suite.py"))
    relative = "skills/sn-ppt-standard/references/box-agent-tool-contract.md"
    data = (STANDARD / "references/box-agent-tool-contract.md").read_bytes()
    if "PRESENTATION_STANDARD_SOURCE" in os.environ:
        data = sync["_apply_integration_overlay"](relative, data)
        data = sync["_image_inspection_recovery_overlay"](relative, data)
    text = data.decode()
    assert "预算是“一次总览 + 一次逐页/分批检查”" not in text
    assert "恢复最后一次确定性通过版本并执行 build" not in text
    assert "visual_unverified" in text and "最终" in text
    assert "3 次 Review" in text and "2 次返修" in text


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("model", ["actual-model-from-tool", None])
def test_documented_asset_batch_preserves_source_and_stops_on_failure(tmp_path, missing, model):
    from PIL import Image

    contract = (STANDARD / "references/box-agent-tool-contract.md").read_text()
    assert "unknown (Box-Agent generate_image)" in contract
    assert "不为此搜索配置" in contract
    command = next(block for block in re.findall(r"```bash\n(.*?)\n```", contract, re.S)
                   if "asset-register" in block)
    assert "asset-assign" in command and "asset-contact" in command
    deck = tmp_path / "deck with spaces"
    (deck / "assets").mkdir(parents=True)
    if not missing:
        Image.new("RGB", (160, 90), "white").save(deck / "assets/final hero.png")
    env = dict(os.environ, DECK_DIR=str(deck), ASSET_PATH="assets/final hero.png",
               ASSET_ID="launch-hero", GROUP_ID="campaign-art",
               ORIGINAL_PROMPT="Product hero; original 'quote' and $(not-a-command)")
    env.pop("GENERATOR_MODEL", None)
    if model is not None:
        env["GENERATOR_MODEL"] = model
    command = command.replace("<SKILL_ROOT>", str(STANDARD))
    command = re.sub(r"(?m)^python ", shlex.quote(sys.executable) + " ", command)
    result = subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=30)
    catalog = deck / "assets/catalog.json"
    if missing:
        assert result.returncode != 0
        assert not catalog.exists()
        assert "asset-assign:PASS" not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assets = json.loads(catalog.read_text())["assets"]
        assert len(assets) == 1
        asset = assets[0]
        assert asset["path"] == env["ASSET_PATH"]
        assert asset["group_id"] == env["GROUP_ID"]
        assert asset["asset_id"] == env["ASSET_ID"]
        assert asset["generator_model"] == (model or "unknown (Box-Agent generate_image)")
        assert asset["prompt"] == env["ORIGINAL_PROMPT"]
        assert asset["status"] == "candidate", "Registration cannot grant visual approval"
