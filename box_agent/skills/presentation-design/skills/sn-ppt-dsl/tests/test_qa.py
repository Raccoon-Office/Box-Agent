"""验证多语言渲染保真、图表数据和视觉记录的实际行为。"""

from collections import Counter
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
from fixtures import create_deck
from chart_verification import source_charts
from dsl import load_source, to_node
from review import current_review_status
from repair import begin_repair, record_build, record_issue, unresolved_status, validate_build, validate_pass
from review import file_hash
from text_verification import compare_rendered_pages
from verify import verify_pptx


def run(*args):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "dsl.py"), *map(str, args)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def review_evidence(manifest, pages):
    files = []
    for batch in manifest["batches"]:
        if set(batch["pages"]) & set(pages):
            files.append(batch["file"])
    for page in pages:
        if page in manifest["risk_pages"]:
            files.append(manifest["pages"][page - 1]["file"])
    return [part for name in dict.fromkeys(files) for part in ("--evidence-image", name)]


def review_command(pptx, manifest, pages, status, finding):
    return run("review", pptx, "--pages", *pages, "--status", status,
               "--finding", finding, *review_evidence(manifest, pages),
               *(["--source-file", f"slides/slide_{pages[0]:02}.xml"] if status == "fix" else []))


def rewrite_archive(path, changes):
    with zipfile.ZipFile(path) as source:
        contents = {name: source.read(name) for name in source.namelist()}
    for name, mutate in changes.items():
        contents[name] = mutate(contents[name])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
        for name, content in contents.items():
            output.writestr(name, content)


def test_rendered_pages_and_chart_data_cannot_hide_numeric_or_language_changes(tmp_path):
    expected = [["销量 12.5% / PPTX / āi 万辆"], ["叶圣陶"]]
    actual = [unicodedata.normalize("NFD", "销量 12.5\n% / PPTX / āi 万辆"), "叶圣陶"]
    assert compare_rendered_pages(expected, actual)["ok"]
    for broken in [actual[0].replace("12.5", "15.2"), actual[0].replace("PPTX", "PPT"),
                   actual[0].replace("a\u0304", "a\u0301")]:
        assert not compare_rendered_pages(expected, [broken, actual[1]])["ok"]
    # 全册总字符相同仍不能让第一页缺字由第二页补偿。
    assert Counter("荷叶花") == Counter("叶荷花")
    assert not compare_rendered_pages([["荷叶"], ["花"]], ["叶", "荷花"])["ok"]

    deck = create_deck(tmp_path / "deck")
    run("build", deck)
    pptx = deck / "presentation.pptx"
    original = pptx.read_bytes()
    root, origins, _ = load_source(deck)
    expected_charts = source_charts(to_node(root, origins))
    assert verify_pptx(pptx, expected_charts=expected_charts)["chart_verification"][0]["source_checked"]

    def change_workbook(content):
        buffer = BytesIO(content)
        with zipfile.ZipFile(buffer) as book:
            entries = {name: book.read(name) for name in book.namelist()}
        sheet = ET.fromstring(entries["xl/worksheets/sheet1.xml"])
        namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet.find(".//x:c[@r='B2']/x:v", namespace).text = "84"
        entries["xl/worksheets/sheet1.xml"] = ET.tostring(sheet)
        changed = BytesIO()
        with zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as book:
            for name, data in entries.items():
                book.writestr(name, data)
        return changed.getvalue()

    cache = {"ppt/charts/chart1.xml": lambda data: data.replace(b"<c:v>48</c:v>", b"<c:v>84</c:v>")}
    workbook = {"ppt/embeddings/Microsoft_Excel_Worksheet1.xlsx": change_workbook}
    for changes in [cache, workbook, {**cache, **workbook}]:
        pptx.write_bytes(original)
        rewrite_archive(pptx, changes)
        with pytest.raises(ValueError, match="源数据或工作簿不符"):
            verify_pptx(pptx, expected_charts=expected_charts)


def test_preview_reviews_follow_current_pixels_and_remove_managed_stale_pages(tmp_path):
    deck = create_deck(tmp_path / "deck")
    run("build", deck)
    pptx = deck / "presentation.pptx"
    preview = deck / "preview"
    result = run("preview", pptx)
    assert result["preview"]["pending_pages"] == [1, 2, 3, 4, 5]
    manifest = json.loads((preview / "manifest.json").read_text())
    assert sorted(page for batch in manifest["batches"] for page in batch["pages"]) == list(range(1, 6))
    assert all(2 <= len(batch["pages"]) <= 4 for batch in manifest["batches"])
    # 测试输入模拟人工记录；生产流程只有实际看图后才能调用 review。
    review_command(pptx, manifest, [1, 2, 3], "pass", "逐页核对首页、结构页与图表页，未见遮挡或裁切")
    review_command(pptx, manifest, [4, 5], "pass", "逐页核对原生对象与混排页，未见遮挡或裁切")
    assert current_review_status(pptx) == "passed"
    (preview / "user-note.txt").write_text("保留用户文件")
    (preview / "slide_99.png").write_bytes(b"stale")
    (preview / "review_batch_99.png").write_bytes(b"stale")
    review_command(pptx, manifest, [2], "fix", "第二页标题需要局部调整")
    run("repair-start", pptx, "--pages", 2)
    page = deck / "slides" / "slide_02.xml"
    page.write_text(page.read_text().replace("先检查结构，再交付真实成品", "局部修改后重新验证成品"))
    run("build", deck)
    assert current_review_status(pptx) == "stale"
    result = run("preview", pptx, "--output", preview)
    assert result["preview"]["pending_pages"] == [1, 2, 3]
    assert not (preview / "slide_99.png").exists()
    assert not (preview / "review_batch_99.png").exists()
    assert (preview / "user-note.txt").read_text() == "保留用户文件"
    manifest = json.loads((preview / "manifest.json").read_text())
    review_command(pptx, manifest, [2], "fix", "测试待修复项")
    assert current_review_status(pptx) == "needs_fix"
    run("repair-start", pptx, "--pages", 2)
    page.write_text(page.read_text().replace("局部修改后重新验证成品", "局部返修后重新验证成品"))
    run("build", deck)
    run("preview", pptx, "--output", preview)
    manifest = json.loads((preview / "manifest.json").read_text())
    review_command(pptx, manifest, [1, 2, 3], "pass", "第 2 页修改后换行清楚，重新检查同批次页面及图表风险页")
    assert current_review_status(pptx) == "passed"
    batch = preview / "review_batch_01.png"
    saved_batch = batch.read_bytes()
    batch.write_bytes(b"changed-after-review")
    assert current_review_status(pptx) == "stale"
    batch.write_bytes(saved_batch)
    (preview / "slide_02.png").write_bytes(b"changed-after-review")
    assert current_review_status(pptx) == "stale"

    previous_pptx = pptx.read_bytes()
    (deck / "slides" / "slide_05.xml").unlink()
    (deck / "outline.md").write_text("# 验收\n" + "\n".join(f"## 第 {page} 页：验收" for page in range(1, 5)))
    rejected = subprocess.run([sys.executable, str(ROOT / "scripts/dsl.py"), "build", str(deck)],
                              capture_output=True, text=True, timeout=120)
    assert rejected.returncode != 0
    assert "预览后增删" in rejected.stderr
    assert pptx.read_bytes() == previous_pptx


def test_default_preview_uses_new_directory_after_build_and_reuses_current_version(tmp_path):
    deck = create_deck(tmp_path / "deck")
    run("build", deck)
    pptx = deck / "presentation.pptx"
    first = run("preview", pptx)["preview"]
    assert first["directory"] == str((deck / "preview").resolve())
    assert run("preview", pptx)["preview"]["reused"] is True

    manifest = json.loads((deck / "preview/manifest.json").read_text())
    review_command(pptx, manifest, [2], "fix", "第二页标题需要局部调整")
    run("repair-start", pptx, "--pages", 2)
    page = deck / "slides" / "slide_02.xml"
    page.write_text(page.read_text().replace("先检查结构，再交付真实成品", "先检查结构，再交付新版成品"))
    run("build", deck)
    second = run("preview", pptx)["preview"]
    assert second["directory"] != first["directory"]
    assert "/qa/previews/pptx-" in second["directory"]
    manifest = json.loads((Path(second["directory"]) / "manifest.json").read_text())
    review_command(pptx, manifest, [2], "pass", "第二页新版标题可读")
    assert json.loads((deck / "qa/visual_review.json").read_text())["preview_directory"] == Path(second["directory"]).relative_to(deck.resolve()).as_posix()


def test_relative_preview_paths_follow_pptx_directory(tmp_path):
    deck = create_deck(tmp_path / "deck")
    run("build", deck)
    pptx = deck / "presentation.pptx"
    relative = Path("qa/previews/custom")
    result = run("preview", pptx, "--output", relative)
    assert result["preview"]["directory"] == str((deck / relative).resolve())
    manifest = json.loads((deck / relative / "manifest.json").read_text())
    for pages in ([1, 2, 3], [4, 5]):
        run("review", pptx, "--output", relative, "--pages", *pages, "--status", "pass",
            "--finding", "逐页查看文字与布局，未见遮挡", *review_evidence(manifest, pages))
    report = json.loads((deck / "qa/visual_review.json").read_text())
    assert report["preview_directory"] == relative.as_posix()
    assert current_review_status(pptx) == "passed"
    assert run("verify", pptx)["visual_review"] == "passed"


def test_post_preview_edit_is_registered_only_by_a_successful_build(tmp_path):
    deck = create_deck(tmp_path / "deck")
    run("build", deck)
    pptx = deck / "presentation.pptx"
    initial = pptx.read_bytes()
    run("preview", pptx)
    manifest = json.loads((deck / "preview/manifest.json").read_text())
    page = deck / "slides/slide_02.xml"
    page.write_text(page.read_text().replace("先检查结构，再交付真实成品", "先检查结构，再交付新版成品"))
    assert current_review_status(pptx) == "partial"
    published = run("preview", pptx)
    assert published["unbuilt_pages"] == [2]
    assert published["preview"]["reused"] is True
    assert pptx.read_bytes() == initial
    valid = page.read_text()
    page.write_text("<slide")
    failed = subprocess.run([sys.executable, str(ROOT / "scripts/dsl.py"), "build", str(deck)], capture_output=True)
    assert failed.returncode and pptx.read_bytes() == initial
    assert not (deck / "qa/repair_ledger.json").exists()
    page.write_text(valid)
    run("build", deck)
    run("build", deck)
    ledger = json.loads((deck / "qa/repair_ledger.json").read_text())
    assert len(ledger["issues"]["2"]["attempts"]) == 1
    final_preview = Path(run("preview", pptx)["preview"]["directory"])
    current = json.loads((final_preview / "manifest.json").read_text())
    review_command(pptx, current, [2], "pass", "新版标题清晰，未见遮挡")
    assert current_review_status(pptx) == "partial"
    ledger = json.loads((deck / "qa/repair_ledger.json").read_text())
    assert ledger["issues"]["2"]["status"] == "closed"


def test_review_requires_readable_page_evidence_and_observation(tmp_path):
    deck = create_deck(tmp_path / "deck")
    run("build", deck)
    pptx = deck / "presentation.pptx"
    run("preview", pptx)
    manifest = json.loads((deck / "preview/manifest.json").read_text())

    def rejected(*args):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/dsl.py"),
                                 "review", str(pptx), *map(str, args)],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode != 0
        return result.stderr

    assert "最多记录四页" in rejected("--pages", 1, 2, 3, 4, 5, "--status", "pass",
                                      "--finding", "检查全册", "--evidence-image", manifest["batches"][0]["file"])
    assert "--finding" in rejected("--pages", 1, "--status", "pass",
                                     "--evidence-image", manifest["batches"][0]["file"])
    assert "不是当前预览的单页图" in rejected("--pages", 1, "--status", "pass", "--finding", "已检查首页",
                                 "--evidence-image", "contact-sheet.png")
    risk = manifest["risk_pages"][0]
    batch = next(item["file"] for item in manifest["batches"] if risk in item["pages"])
    assert "风险页" in rejected("--pages", risk, "--status", "pass", "--finding", "已检查风险页",
                                 "--evidence-image", batch)
    review_command(pptx, manifest, [risk], "pass", "原尺寸检查文字和图表，未见异常")
    report = json.loads((deck / "qa/visual_review.json").read_text())
    record = next(item for item in report["page_results"] if item["page"] == risk)
    assert record["evidence_images"][-1]["scale"] == "full"
    assert record["finding"]
    del record["evidence_images"]
    (deck / "qa/visual_review.json").write_text(json.dumps(report))
    assert current_review_status(pptx) == "partial"
    run("preview", pptx)
    refreshed = json.loads((deck / "qa/visual_review.json").read_text())
    assert risk in refreshed["pending_pages"]


def test_repair_budget_requires_source_change_and_stops_after_two_attempts(tmp_path):
    deck = tmp_path / "deck"
    slide = deck / "slides" / "slide_01.xml"
    slide.parent.mkdir(parents=True)
    slide.write_text("first")
    pptx = deck / "presentation.pptx"
    pptx.write_bytes(b"first deck")
    record_issue(pptx, 1, "标题遮挡正文", ["slides/slide_01.xml"], "image-1")
    assert validate_build(pptx)["issues"]["1"]["status"] == "needs_fix"

    for attempt in (1, 2):
        begin_repair(pptx, [1])
        with pytest.raises(ValueError, match="源文件未修改"):
            validate_build(pptx)
        slide.write_text(f"repair {attempt}")
        validate_build(pptx)
        pptx.write_bytes(f"deck {attempt}".encode())
        record_build(pptx)
        validate_pass(pptx, 1)
        record_issue(pptx, 1, "仍有遮挡", ["slides/slide_01.xml"], f"image-{attempt + 1}")

    assert unresolved_status(pptx) == "partial"
    with pytest.raises(ValueError, match="预算已用尽"):
        begin_repair(pptx, [1])


def test_referenced_asset_change_requires_each_affected_page_and_asset_binding(tmp_path):
    deck = tmp_path / "deck"
    slides = deck / "slides"
    assets = deck / "assets"
    qa = deck / "qa"
    for folder in (slides, assets, qa):
        folder.mkdir(parents=True, exist_ok=True)
    for page in (1, 2):
        (slides / f"slide_{page:02}.xml").write_text(f"page {page}")
    image = assets / "shared.png"
    image.write_bytes(b"original")
    unused = assets / "unused.png"
    unused.write_bytes(b"unused")
    pptx = deck / "presentation.pptx"
    pptx.write_bytes(b"deck")
    build = {"verification": {"sha256": file_hash(pptx)},
             "slide_source_sha256": {f"slides/slide_{page:02}.xml": file_hash(slides / f"slide_{page:02}.xml")
                                     for page in (1, 2)},
             "page_asset_sha256": {str(page): {"assets/shared.png": file_hash(image)} for page in (1, 2)}}
    (qa / "build.json").write_text(json.dumps(build))
    (qa / "visual_review.json").write_text(json.dumps({"source_sha256": file_hash(pptx)}))
    unused.write_bytes(b"changed but not referenced")
    validate_build(pptx)
    image.write_bytes(b"replacement")
    plan = validate_build(pptx)
    assert set(plan["issues"]) == {"1", "2"}
    for issue in plan["issues"].values():
        assert list(issue["attempts"][0]["before_source_sha256"]) == [str(image)]
    record_issue(pptx, 1, "图片不清晰", ["slides/slide_01.xml"], "image-1")
    begin_repair(pptx, [1])
    plan = validate_build(pptx)
    assert set(plan["issues"]) == {"1", "2"}
    assert all(len(issue["attempts"]) == 1 for issue in plan["issues"].values())
    pptx.write_bytes(b"updated by shared asset")
    record_build(pptx, plan)
    for page in (1, 2):
        validate_pass(pptx, page)
