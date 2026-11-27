"""检查实际 PPTX 结构，并渲染成品以便检查字体与视觉效果。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from chart_verification import verify_charts
from text_verification import compare_rendered_pages, extract_pdf_pages, pptx_text_pages
from review import _manifest, file_hash, make_review_images, managed_preview_name, reconcile_review, save_json

NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main", "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}


def executable(name: str) -> str | None:
    supplied = os.environ.get("BOX_AGENT_" + name.upper())
    if supplied and Path(supplied).is_file():
        return supplied
    if name == "pdftotext" and os.environ.get("BOX_AGENT_PDFTOPPM"):
        raster = Path(os.environ["BOX_AGENT_PDFTOPPM"])
        sibling = raster.with_name("pdftotext" + raster.suffix)
        if sibling.is_file():
            return str(sibling)
    return shutil.which(name)


def verify_pptx(path: Path, expected_pages: int | None = None, expected_texts: list[list[str]] | None = None,
                expected_charts: list | None = None) -> dict:
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"PPTX 压缩包损坏：{bad}")
        names = archive.namelist()
        slides = sorted([name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)], key=lambda name: int(re.search(r"slide(\d+)", name).group(1)))
        if not slides or (expected_pages is not None and len(slides) != expected_pages):
            raise ValueError(f"PPTX 页数不符：实际 {len(slides)}，预期 {expected_pages}")
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        size = presentation.find("p:sldSz", NS)
        canvas = (int(size.get("cx")), int(size.get("cy")))
        pages = []
        for number, name in enumerate(slides):
            root = ET.fromstring(archive.read(name))
            text = "".join(root.itertext())
            # 核对实际写入的文字，避免富文本转换漏掉尾部或行内节点。
            if expected_texts:
                normalized = re.sub(r"\s+", "", text)
                for expected in expected_texts[number]:
                    if re.sub(r"\s+", "", expected) not in normalized:
                        raise ValueError(f"第 {number + 1} 页缺少源文字：{expected[:60]}")
            for transform in root.findall(".//a:xfrm", NS) + root.findall(".//p:xfrm", NS):
                offset, extent = transform.find("a:off", NS), transform.find("a:ext", NS)
                if offset is None or extent is None:
                    continue
                x, y = int(offset.get("x")), int(offset.get("y"))
                w, h = int(extent.get("cx")), int(extent.get("cy"))
                if min(x, y) < -12700 or x + w > canvas[0] + 12700 or y + h > canvas[1] + 12700:
                    raise ValueError(f"第 {number + 1} 页存在越出画布的实际 PPTX 元素")
            pages.append({"page": number + 1, "text_runs": len(root.findall(".//a:t", NS)), "pictures": len(root.findall(".//p:pic", NS)), "tables": len(root.findall(".//a:tbl", NS))})
        charts = [name for name in names if re.fullmatch(r"ppt/charts/chart\d+\.xml", name)]
        chart_check = verify_charts(archive, slides, expected_charts)
        for name in names:
            if name.endswith((".xml", ".rels")):
                ET.fromstring(archive.read(name))
    return {"ok": True, "file": str(path), "slides": len(slides), "native_charts": len(charts), "pages": pages,
            "chart_verification": chart_check, "sha256": file_hash(path), "visual_review": "not_performed"}


def review_targets(output, manifest):
    def target(item):
        name = item.get("view_file", item["file"])
        return {"pages": item["pages"] if "pages" in item else [item["page"]],
                "file": name, "inspect": str(output / name), "sha256": item.get("view_sha256", item["sha256"])}
    overview = manifest["contact_sheet"]
    return {"contact_sheet": str(output / overview.get("view_file", overview["file"])),
            "batches": [target(item) for item in manifest["batches"]],
            "risk_images": [target(item) for item in manifest["pages"] if item["page"] in manifest["risk_pages"]]}


def preview_pptx(path: Path, output: Path, batch_size: int = 4) -> dict:
    output = output.resolve()
    if path.resolve().is_relative_to(output):
        raise ValueError("预览须使用独立目录，不能包含输入 PPTX")
    if output.is_dir():
        try:
            existing = _manifest(path, output)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        else:
            review = reconcile_review(path, output)
            return {"directory": str(output), "slides": existing["slide_count"],
                    **review_targets(output, existing), "manifest": str(output / "manifest.json"),
                    "risk_pages": existing["risk_pages"], "source_sha256": file_hash(path),
                    "visual_review": review["status"], "pending_pages": review["pending_pages"],
                    "reused": True}
    office = executable("soffice")
    if not office:
        app = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        office = str(app) if app.exists() else None
    raster = executable("pdftoppm")
    if not office or not raster:
        raise ValueError("本地预览需要 LibreOffice（soffice）和 Poppler（pdftoppm），PPTX 本身无需它们即可生成")
    output.parent.mkdir(parents=True, exist_ok=True)
    structure = verify_pptx(path)
    with tempfile.TemporaryDirectory(prefix=".sn-ppt-preview-", dir=output.parent) as temp:
        folder = Path(temp)
        staged = folder / "output"
        staged.mkdir()
        profile = (folder / "profile").as_uri()
        env = os.environ.copy()
        env["FONTCONFIG_FILE"] = str(Path(__file__).resolve().parents[1] / "assets" / "fonts" / "fonts.conf")
        result = subprocess.run([office, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf", "--outdir", str(folder), str(path)], text=True, capture_output=True, timeout=120, env=env)
        pdf = folder / (path.stem + ".pdf")
        if result.returncode or not pdf.is_file():
            raise ValueError(f"最终 PPTX 渲染失败：{result.stderr or result.stdout}")
        # 每页单独验收，数字和英文不能再绕过只统计中文的检查。
        extract = executable("pdftotext")
        text_report = compare_rendered_pages(pptx_text_pages(path), extract_pdf_pages(extract, pdf))
        text_report["source_sha256"] = file_hash(path)
        report_path = path.parent / "qa" / "rendered-text.json"
        save_json(report_path, text_report)
        if not text_report["ok"]:
            bad_pages = [page["page"] for page in text_report["pages"] if not page["ok"]]
            raise ValueError(f"第 {bad_pages} 页实际渲染缺字或数值变化，详情：{report_path}")
        subprocess.run([raster, "-png", "-r", "100", str(pdf), str(folder / "slide")], check=True, capture_output=True, timeout=120)
        images = sorted(folder.glob("slide-*.png"), key=lambda item: int(item.stem.split("-")[-1]))
        if len(images) != structure["slides"]:
            raise ValueError("渲染页数与 PPTX 不一致")
        shutil.copy2(pdf, staged / "presentation.pdf")
        from PIL import Image, ImageDraw
        thumbs = []
        for index, image in enumerate(images, 1):
            dest = staged / f"slide_{index:02}.png"
            shutil.copy2(image, dest)
            with Image.open(dest) as source:
                thumb = source.convert("RGB")
                thumb.thumbnail((480, 270))
                thumbs.append(thumb.copy())
        sheet = Image.new("RGB", (1000, ((len(thumbs) + 1) // 2) * 310), "#E9ECEF")
        draw = ImageDraw.Draw(sheet)
        for index, thumb in enumerate(thumbs):
            x, y = 10 + (index % 2) * 500, 10 + (index // 2) * 310
            sheet.paste(thumb, (x, y))
            draw.text((x, y + 276), str(index + 1), fill="black")
        sheet.save(staged / "contact-sheet.png")
        manifest = make_review_images(path, staged, text_report, structure["chart_verification"], batch_size)
        # 保留用户额外放入预览目录的文件，只替换本脚本管理的产物。
        if output.exists():
            for item in output.iterdir():
                if not managed_preview_name(item.name):
                    destination = staged / item.name
                    if item.is_dir() and not item.is_symlink():
                        shutil.copytree(item, destination, symlinks=True)
                    else:
                        shutil.copy2(item, destination, follow_symlinks=False)
            previous = folder / "previous"
            output.replace(previous)
            try:
                staged.replace(output)
            except OSError:
                previous.replace(output)
                raise
        else:
            staged.replace(output)
    review = reconcile_review(path, output)
    return {"directory": str(output), "slides": len(images), **review_targets(output, manifest),
            "manifest": str(output / "manifest.json"),
            "risk_pages": manifest["risk_pages"], "source_sha256": file_hash(path),
            "visual_review": review["status"], "pending_pages": review["pending_pages"]}
