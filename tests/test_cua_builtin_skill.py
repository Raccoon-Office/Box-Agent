import json
from pathlib import Path

import pytest

from box_agent.plugins.repl.documentation import manual
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import build_auto_loaded_skills_prompt, turn_preload_skill_names


@pytest.mark.parametrize('skill_name,prompt', [
    ('computer-use', '通过 Driver 打开 Word 文档'),
    ('office-gui-basics', '在 Word 中修改标题并另存为副本'),
    ('office-gui-basics', '在 Excel 中选择单元格范围并设置百分比格式'),
    ('office-gui-basics', '在 PowerPoint 中填写标题和副标题占位框'),
])
def test_explicit_cua_skill_is_discoverable_and_loads_from_builtin_manifest(skill_name, prompt):
    root = Path(__file__).resolve().parents[1] / 'box_agent' / 'skills'
    loader = SkillLoader(sources=[(root, 'builtin')])
    loader.discover_skills()
    names = turn_preload_skill_names((), None, prompt,
                                     selected_skill_names=(skill_name,))
    loaded = build_auto_loaded_skills_prompt(loader, 'base system', names)
    if skill_name == "office-gui-basics":
        assert loaded.loaded_names == (skill_name, "computer-use")
        assert loaded.loaded_attributions[1].dependency_of == skill_name
    else:
        assert loaded.loaded_names == (skill_name,)
    skill = loader.get_skill(skill_name)
    assert skill is not None
    assert skill.content
    assert skill.content in loaded.system_prompt


def test_calculator_discovers_one_native_skill_with_the_repl_manual():
    root = Path(__file__).resolve().parents[1] / "box_agent" / "skills"
    loader = SkillLoader(sources=[(root, "builtin")])
    loader.discover_skills()

    assert loader.get_skill("mac-cua") is None
    skill = loader.filter_by_query("打开计算器")[0]
    assert skill.name == "computer-use"
    assert {"打开计算器", "计算器", "操作飞书", "desktop application"} <= set(skill.keywords)
    assert "cua_repl_js" in skill.content
    assert "await cua.getApp(targetApp)" in skill.content
    assert "R56-" not in skill.content


def test_office_skill_uses_public_app_input_methods():
    root = Path(__file__).resolve().parents[1] / "box_agent" / "skills"
    text = (root / "office-gui-basics" / "SKILL.md").read_text()
    for method in ("setValue", "typeText", "paste", "pressKey", "getAXState"):
        assert f"await app.{method}(" in text
    for retired in ("`set_value`", "`type_text`", "`confirm`", "显式使用前台输入"):
        assert retired not in text
    for checkpoint in ("AXRow", "目标父目录", "原路径", "段落标记", 'app.pressKey("ctrl+g")'):
        assert checkpoint in text
    powerpoint = text.split("## PowerPoint 占位框", 1)[1]
    assert powerpoint.index("文字光标或文字选区") < powerpoint.index("await app.paste(text)")
    assert "外框控制点" in powerpoint
    assert "选中的缩略图和状态栏的页码" in powerpoint


def test_native_guides_require_a_new_screenshot_after_escape_and_coordinate_rejection():
    root = Path(__file__).resolve().parents[1] / "box_agent" / "skills"
    skill = (root / "computer-use" / "SKILL.md").read_text()
    for guide in (skill, manual("cua", None)):
        assert "await app.pressKey('Escape'); await app.getScreenshot();" in guide
        recovery = guide.split("Coordinates require a current screenshot of this window", 1)[1]
        recovery = recovery.split("\n|", 1)[0].split("\n-", 1)[0]
        assert "await app.getScreenshot()" in recovery
        assert "await app.getAXStateAndScreenshot()" in recovery


def test_native_skill_and_dispatched_manual_teach_readback_after_uncertain_axpress():
    root = Path(__file__).resolve().parents[1] / "box_agent" / "skills"
    skill = (root / "computer-use" / "SKILL.md").read_text()
    for guide in (skill, manual("cua", None)):
        recovery = guide.split("-25204", 1)[1].split("\n-", 1)[0].split("\n|", 1)[0]
        assert "await app.getAXState({disableDiffing: true})" in recovery
        assert "await app.getAXStateAndScreenshot()" in recovery


def test_native_guides_show_decodable_call_envelope_and_distinct_recovery_paths():
    root = Path(__file__).resolve().parents[1] / "box_agent" / "skills"
    for guide in ((root / "computer-use/SKILL.md").read_text(), manual("cua", None)):
        example = guide.split("```json\n", 1)[1].split("\n```", 1)[0]
        envelope = json.loads(example)
        assert set(envelope) == {"tool_ref", "arguments_json"}
        target = json.loads(envelope["arguments_json"])
        assert target["code"].startswith(("var targetApp =", "await app."))
        assert target["title"]
        assert "Invalid arguments_json" in guide
        assert "app.typeText(newStem)" in guide
        menu = guide.split("TargetUnverified", 1)[1].split("\n|", 1)[0].split("\n-", 1)[0]
        assert "Down" in menu and "Up" in menu and "Return" in menu


def test_native_guides_observe_finder_panel_before_sending_more_input():
    root = Path(__file__).resolve().parents[1] / "box_agent" / "skills"
    skill = (root / "computer-use/SKILL.md").read_text()
    for guide, stop in ((skill, "停止向该浮层发键"), (manual("cua", None), "stop and report the panel")):
        recovery = guide.split("AttachmentUnverified", 1)[1].split(stop, 1)
        assert len(recovery) == 2
        assert "await app.getAXStateAndScreenshot({disableDiffing: true})" in recovery[0]


def test_office_guide_advances_from_observed_app_checkpoints():
    root = Path(__file__).resolve().parents[1] / "box_agent" / "skills"
    text = (root / "office-gui-basics/SKILL.md").read_text()
    for section, checkpoints in (
        ("WPS 自定义选择器", ("可写字段", "读回完整值", "核对文档标题")),
        ("Word 文本编辑", ("读回新文字", "插入点位于该段", "核对样式名称")),
        ("Excel：", ("键盘焦点已回到工作表", "单独提交", "在公式栏核对公式")),
        ("PowerPoint 占位框", ("原占位框和完整新文字", "继续下一项", "完成后保存")),
    ):
        paragraph = text.split(section, 1)[1].split("\n## ", 1)[0]
        for checkpoint in checkpoints:
            _, paragraph = paragraph.split(checkpoint, 1)


def test_native_guides_rebind_the_requested_app_before_input():
    root = Path(__file__).resolve().parents[1] / "box_agent/skills"
    skill = (root / "computer-use/SKILL.md").read_text()
    for guide, marker in ((skill, "`WindowChanged`，且初始化失败"),
                          (manual("cua", None), "`WindowChanged` during getApp")):
        assert "var app = await cua.getApp(targetApp)" in guide
        recovery = guide.split(marker, 1)[1].splitlines()[0]
        assert "app = await cua.getApp(targetApp)" in recovery
        assert "TextEdit" not in recovery
    example = skill.split("## 示例：", 1)[1].split("## 观察和恢复", 1)[0]
    assert example.index("正文已获得焦点") < example.index("await app.typeText(")
    assert "await app.getAXState(" in example.split("await app.typeText(", 1)[1]


def test_desktop_office_request_can_discover_the_gui_guide_and_its_dependency():
    root = Path(__file__).resolve().parents[1] / "box_agent/skills"
    loader = SkillLoader(sources=[(root, "builtin")])
    loader.discover_skills()
    for prompt in ("请在桌面版 Excel 里打开库存.xlsx，按保质期排序后保存",
                   "用 WPS 打开桌面上的报价单，另存一份"):
        candidates = {skill.name for skill in loader.filter_by_query(prompt)}
        assert {"office-gui-basics", "computer-use"} <= candidates
    assert "office-gui-basics" in loader.get_skill("computer-use").content


def test_office_guide_checks_ambiguous_range_before_return_and_reopens_ambiguous_copy():
    root = Path(__file__).resolve().parents[1] / "box_agent/skills"
    skill = (root / "office-gui-basics/SKILL.md").read_text()
    excel = skill.split("## Excel", 1)[1]
    assert excel.index("名称框中的地址") < excel.index('app.pressKey("Return")') < excel.index("行列高亮")
    save = skill.split("## 保存与另存为", 1)[1].split("## ", 1)[0]
    assert save.index("无法确认父目录") < save.index("完整路径重新打开") < save.index("核对一处本次修改")
