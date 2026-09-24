"""The group input CLI preserves source text and confines its reads and writes."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get(
    "PPT_GROUP_INPUT_SCRIPT",
    REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts/group_input.py",
))


def test_group_input_entry_point_exists():
    assert SCRIPT.is_file(), "The approved group_input.py entry point is missing"


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def case(tmp_path):
    assert SCRIPT.is_file(), "The approved group_input.py entry point is missing"
    skill = tmp_path / "Skill 安装目录"
    script = skill / "scripts/group_input.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    put(skill / "subagents/slide.md", "# Slide instructions\n完整职责，不缩写。\n")
    put(skill / "references/layout.md", "# Layouts\n## Shared\n共同设计原文。\n### Child\n子段不能遗漏。\n## Other\n其他组规则。\n")
    root = tmp_path / "任务 有空格"
    put(root / "task_pack.json", json.dumps({"deck_dir": str(root), "request": {"query": "原始主题"}}, ensure_ascii=False) + "\n")
    put(root / "plan/deck.md", "# Narrative\nGlobal context\n## Production groups\n### bookends\n- pages: 01,03\n- purpose: 首尾呼应\n- boundary_handoff: 完整进入退出约束\n  多行约束\n\n### body\n- pages: 02\n- boundary_handoff: 正文承接\n## Risks\n风险原文\n")
    put(root / "plan/design-brief.md", "# Design brief\n开头不可遗漏。\n## Style Lock\n锁定设计。\n## Beyond style\n结尾不可遗漏。\n")
    put(root / "base.css", ":root { --font-body: '中文字体'; }\n/* 完整 CSS */\n")
    for page, group in [(1, "bookends"), (2, "body"), (3, "bookends")]:
        put(root / f"plan/slide_{page:02d}.md", f"# Slide {page:02d}\n## 页面导演\n- production_group：{group}\n## 最终屏显文案\n页面 {page} 全文\n## Reference route\n- layout.md：Shared\n## 口语讲稿\n不能丢掉的讲稿 {page}\n")
    contract = tmp_path / "Explicit tool contract.md"
    put(contract, "# Tool contract\n调用方明确指定的完整契约。\n")
    return root, skill, script, contract


def run(case, *args, root=None):
    deck, skill, script, _ = case
    return subprocess.run(
        [sys.executable, str(script), str(root or deck), "--expected", "3", *args],
        cwd=skill, text=True, capture_output=True, check=False,
    )


def success(result):
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def rendered(group):
    return "".join(Path(path).read_text(encoding="utf-8") for path in group["inputs"])


def reject(result, *messages):
    assert result.returncode != 0
    assert not result.stdout.strip(), result.stdout
    for message in messages:
        assert message.lower() in result.stderr.lower(), result.stderr


def test_full_original_materials_sources_and_absolute_write_scope(case):
    root, skill, _, contract = case
    before = {p: p.read_bytes() for d in [root, skill] for p in d.rglob("*") if p.is_file()}
    data = success(run(case, "--tool-contract", str(contract)))
    assert data["deck_dir"] == str(root)
    assert [g["group"] for g in data["groups"]] == ["bookends", "body"]
    group = data["groups"][0]
    assert group["pages"] == [1, 3]
    assert group["outputs"] == [str(root / f"slides/slide_{p:02d}.html") for p in [1, 3]]
    text = rendered(group)
    for path in [root / "task_pack.json", root / "plan/design-brief.md", root / "base.css", root / "plan/slide_01.md", root / "plan/slide_03.md", skill / "subagents/slide.md", contract]:
        assert path.read_text(encoding="utf-8") in text
        assert {"path": str(path), "start_line": 1, "end_line": len(path.read_text().splitlines())} in group["sources"]
    assert "- boundary_handoff: 完整进入退出约束\n  多行约束\n" in text
    assert "页面 2 全文" not in text
    assert text.count("共同设计原文。") == 1
    assert "子段不能遗漏。" in text
    assert "其他组规则。" not in text
    assert group["part_count"] == len(group["parts"]) == len(group["inputs"])
    assert group["total_chars"] == sum(p["chars"] for p in group["parts"])
    assert not any(key in json.dumps(data) for key in ['"budget"', '"model"', '"tools"'])
    assert all(p.read_bytes() == original for p, original in before.items())
    assert all(p.is_relative_to(root / "_trace/group-input") for p in root.rglob("*") if p.is_file() and p not in before)


def test_optional_contract_and_single_group_are_supported(case):
    group = success(run(case, "--group", "body"))["groups"]
    assert len(group) == 1 and group[0]["pages"] == [2]
    assert "Tool contract" not in rendered(group[0])
    assert not (case[0] / "_trace/group-input/bookends").exists()


@pytest.mark.parametrize("old,new,reason", [
    ("- pages: 02", "- pages: 01,02", "more than one"),
    ("- pages: 01,03", "- pages: 01", "missing pages"),
    ("- pages: 02", "- pages: 02,04", "expected"),
    ("- pages: 02", "- pages: 02,02", "more than one"),
    ("- pages: 02", "- pages: 01-02", "pages"),
    ("### body", "### ../escape", "group"),
    ("### body", "### bookends", "duplicate"),
    ("## Production groups", "## Production groups old", "Production groups"),
])
def test_complete_group_partition_validated_before_selected_group(case, old, new, reason):
    path = case[0] / "plan/deck.md"
    put(path, path.read_text().replace(old, new))
    reject(run(case, "--group", "bookends"), reason, "deck.md")
    assert not (case[0] / "_trace").exists()


def test_other_group_plan_mismatch_is_not_hidden_by_selection(case):
    path = case[0] / "plan/slide_02.md"
    put(path, path.read_text().replace("production_group：body", "production_group：bookends"))
    reject(run(case, "--group", "bookends"), "production_group", "slide_02.md")


@pytest.mark.parametrize("route,reason", [("Missing", "not found"), ("Duplicate", "ambiguous")])
def test_reference_errors_identify_plan_line_and_source(case, route, reason):
    root, skill, _, _ = case
    put(skill / "references/layout.md", "# Layouts\n## A\n### Duplicate\nOne\n## B\n### Duplicate\nTwo\n")
    path = root / "plan/slide_01.md"
    put(path, path.read_text().replace("layout.md：Shared", f"layout.md：{route}"))
    reject(run(case), str(path) + ":7", "layout.md", reason)


def test_unique_heading_path_ignores_fenced_headings_and_deduplicates_overlap(case):
    root, skill, _, _ = case
    put(skill / "references/layout.md", "# Layouts\n## A\n### Shared\nA content\n```md\n## Fake\n### Shared\n```\n## B\n### Shared\nB content\n")
    for page in [1, 2, 3]:
        path = root / f"plan/slide_{page:02d}.md"
        put(path, path.read_text().replace("layout.md：Shared", "layout.md：A > Shared\n- layout.md: A"))
    group = success(run(case))["groups"][0]
    text = rendered(group)
    assert text.count("A content") == 1
    assert "## Fake\n### Shared\n" in text
    assert "B content" not in text


def test_exact_heading_keeps_literal_trailing_hash(case):
    root, skill, _, _ = case
    put(skill / "references/layout.md", "# Rules\n## C#\nExact language heading\n## Next ###\nClosing hashes are syntax\n")
    for page in [1, 2, 3]:
        path = root / f"plan/slide_{page:02d}.md"
        put(path, path.read_text().replace("layout.md：Shared", "layout.md: C#\n- layout.md: Next"))
    text = rendered(success(run(case))["groups"][0])
    assert "## C#\nExact language heading\n" in text
    assert "## Next ###\nClosing hashes are syntax\n" in text


@pytest.mark.parametrize("target", ["task_pack.json", "plan/deck.md", "plan/design-brief.md", "base.css", "plan/slide_02.md"])
def test_missing_required_material_fails_before_writing(case, target):
    (case[0] / target).unlink()
    reject(run(case), target)
    assert not (case[0] / "_trace").exists()


def test_root_must_be_absolute_and_match_task_pack(case):
    root = case[0]
    reject(run(case, root=Path("../任务 有空格")), "absolute")
    put(root / "task_pack.json", json.dumps({"deck_dir": str(root.parent / "old-root")}))
    reject(run(case), "deck_dir", "match")


@pytest.mark.parametrize("target", ["base.css", "plan", "_trace", "slides", "task_pack.json"])
def test_task_symlinks_are_rejected_without_outside_reads_or_writes(case, target, tmp_path):
    root = case[0]
    outside = tmp_path / "outside"
    if target in {"plan", "_trace", "slides"}:
        outside.mkdir()
    else:
        put(outside, "DO NOT READ OUTSIDE")
    path = root / target
    if path.is_dir():
        path.rename(root / "saved-plan")
    elif path.exists():
        path.unlink()
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    reject(run(case), "symlink", target)
    assert not (outside / "group-input").exists()


def test_existing_part_symlink_cannot_be_overwritten(case, tmp_path):
    outside = tmp_path / "precious.md"
    put(outside, "keep me")
    path = case[0] / "_trace/group-input/bookends/part-01.md"
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)
    reject(run(case), "symlink", "part-01.md")
    assert outside.read_text() == "keep me"


@pytest.mark.parametrize("route", ["../outside.md", "/outside.md", "references/../../outside.md"])
def test_reference_traversal_rejected(case, route):
    path = case[0] / "plan/slide_01.md"
    put(path, path.read_text().replace("layout.md：Shared", route + ": Shared"))
    reject(run(case), "filename", "slide_01.md")


def test_symlink_reference_is_rejected(case, tmp_path):
    ref = case[1] / "references/layout.md"
    ref.unlink()
    outside = tmp_path / "outside.md"
    put(outside, "## Shared\nsecret\n")
    ref.symlink_to(outside)
    reject(run(case), "symlink", "layout.md")


def test_related_catalog_is_original_and_asset_paths_are_checked(case):
    root = case[0]
    path = root / "plan/slide_01.md"
    put(path, path.read_text().replace("## Reference route", "- asset_id: hero_a → assets/hero.png + origin + crop_contract\n## Reference route"))
    catalog = {"schema_version": 2, "assets": [{"asset_id": "hero_a", "path": "assets/hero.png", "origin": "generated", "generator_model": "provenance", "status": "ready", "crop_contract": {"protected_parts": ["whole subject"]}}]}
    catalog_text = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    put(root / "assets/catalog.json", catalog_text)
    put(root / "assets/hero.png", "a fixture image")
    group = success(run(case))["groups"][0]
    assert catalog_text in rendered(group)
    catalog["assets"][0]["path"] = "../../outside.png"
    put(root / "assets/catalog.json", json.dumps(catalog))
    reject(run(case), "asset", "path")


def test_missing_asset_catalog_is_actionable(case):
    path = case[0] / "plan/slide_01.md"
    put(path, path.read_text().replace("## Reference route", "- asset_id: hero\n## Reference route"))
    reject(run(case), "assets/catalog.json")


def test_parts_respect_both_limits_preserve_all_lines_and_report_source_ranges(case):
    root = case[0]
    original = "".join(f"LINE-{i:04d} " + "原文" * 70 + "\n" for i in range(900))
    put(root / "base.css", original)
    group = success(run(case, "--group", "bookends"))["groups"][0]
    assert len(group["parts"]) >= 4
    chunks = []
    for part in group["parts"]:
        text = Path(part["path"]).read_text()
        assert len(text) == part["chars"] <= 40000
        assert len(text.splitlines()) == part["lines"] <= 450
        for match in re.finditer(r'<!-- source: ([^\n]+) -->\n(.*?)<!-- end-source -->', text, re.S):
            source, raw = json.loads(match[1]), match[2]
            if source["path"] == str(root / "base.css"):
                lines = original.splitlines(keepends=True)
                assert raw == "".join(lines[source["start_line"] - 1:source["end_line"]])
                chunks.append(raw)
    assert "".join(chunks) == original


def test_line_limit_is_independent_of_character_limit(case):
    put(case[0] / "base.css", "x\n" * 1000)
    group = success(run(case, "--group", "bookends"))["groups"][0]
    assert len(group["parts"]) >= 3
    assert rendered(group).count("x\n") == 1000
    assert all(part["lines"] <= 450 for part in group["parts"])


def test_single_oversized_line_fails_without_truncation_or_writes(case):
    put(case[0] / "base.css", "x" * 40000 + "\n")
    reject(run(case), "base.css:1", "single line")
    assert not (case[0] / "_trace").exists()


def test_regeneration_is_deterministic_includes_changes_and_preserves_old_files(case):
    root = case[0]
    put(root / "base.css", "x\n" * 1000)
    first = success(run(case, "--group", "body"))
    snapshots = {p: Path(p).read_bytes() for p in first["groups"][0]["inputs"]}
    assert success(run(case, "--group", "body")) == first
    assert all(Path(p).read_bytes() == data for p, data in snapshots.items())
    other = root / "_trace/group-input/body/user-notes.md"
    put(other, "retain")
    put(root / "base.css", "new CSS\n")
    latest = success(run(case, "--group", "body"))["groups"][0]
    assert "new CSS\n" in rendered(latest)
    assert len(latest["inputs"]) < len(snapshots)
    assert all(Path(p).exists() for p in snapshots)
    assert other.read_text() == "retain"


def test_unknown_group_and_invalid_expected_fail(case):
    reject(run(case, "--group", "missing"), "unknown group")
    reject(run(case, "--expected", "0"), "expected")


def test_real_skill_resources_are_read_relative_to_script_from_another_cwd(case):
    root = case[0]
    for page in [1, 2, 3]:
        path = root / f"plan/slide_{page:02d}.md"
        put(path, path.read_text().replace("layout.md：Shared", "quality-checklist.md: 一、单页检查"))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--expected", "3"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    group = success(result)["groups"][0]
    paths = {source["path"] for source in group["sources"]}
    assert str(SCRIPT.parents[1] / "subagents/slide.md") in paths
    assert str(SCRIPT.parents[1] / "references/quality-checklist.md") in paths
    assert all(part["lines"] <= 450 and part["chars"] <= 40000 for part in group["parts"])


def test_source_crlf_and_unterminated_last_line_are_not_normalized(case):
    original = b":root {\r\n --x: 1;\r\n}"
    path = case[0] / "base.css"
    path.write_bytes(original)
    group = success(run(case, "--group", "body"))["groups"][0]
    parts = [Path(path).read_bytes() for path in group["inputs"]]
    assert any(original in part for part in parts)
    assert path.read_bytes() == original
