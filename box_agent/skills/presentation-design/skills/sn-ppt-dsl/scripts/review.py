"""生成清晰的分批审阅图，并将视觉记录绑定当前成稿与页面。"""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _now():
    return datetime.now(timezone.utc).isoformat()


def preview_path(pptx: Path, value: str | Path) -> Path:
    """Resolve preview paths relative to the deck, never the caller's cwd."""
    path = Path(value)
    return (path if path.is_absolute() else pptx.parent / path).resolve()


def preview_reference(pptx: Path, preview: Path) -> str:
    """Keep previews inside a deck portable when that deck is moved."""
    path = preview_path(pptx, preview)
    try:
        return path.relative_to(pptx.parent.resolve()).as_posix()
    except ValueError:
        return str(path)


def make_review_images(pptx: Path, output: Path, text_report: dict, charts: list[dict], batch_size=4) -> dict:
    from PIL import Image, ImageDraw
    from review_image import write_copy
    images = sorted(output.glob("slide_*.png"), key=lambda path: int(path.stem.split("_")[-1]))
    pages = []
    chart_pages = {chart["page"] for chart in charts}
    for index, path in enumerate(images, 1):
        risk = []
        if text_report["pages"][index - 1]["risk_fragments"]:
            risk.append("text_flow")
        if index in chart_pages:
            risk.append("chart_rendering")
        with Image.open(path) as image:
            dimensions = list(image.size)
            view = path.with_suffix(".jpg")
            view_hash = write_copy(image, view)
        pages.append({"page": index, "file": path.name, "sha256": file_hash(path),
                      "dimensions": dimensions, "risks": risk, "view_file": view.name, "view_sha256": view_hash})
    count = math.ceil(len(pages) / batch_size)
    sizes = [len(pages) // count + (index < len(pages) % count) for index in range(count)]
    batches = []
    offset = 0
    for number, size in enumerate(sizes, 1):
        members = pages[offset:offset + size]
        columns = 1 if size <= 2 else 2
        # 页码置于图片外；每个页面保留足够像素，避免长条图整体缩小。
        tile_width, tile_height, gap, label_height = 960, 540, 16, 28
        sheet = Image.new("RGB", (columns * (tile_width + gap) + gap,
                                  math.ceil(size / columns) * (tile_height + label_height + gap) + gap), "#E9ECEF")
        draw = ImageDraw.Draw(sheet)
        for index, page in enumerate(members):
            x = gap + index % columns * (tile_width + gap)
            y = gap + index // columns * (tile_height + label_height + gap)
            with Image.open(output / page["file"]) as image:
                thumbnail = image.convert("RGB")
                thumbnail.thumbnail((tile_width, tile_height))
                sheet.paste(thumbnail, (x, y + label_height))
            draw.text((x, y + 6), f"P{page['page']}", fill="black")
        name = f"review_batch_{number:02}.png"
        sheet.save(output / name)
        view = (output / name).with_suffix(".jpg")
        batches.append({"file": name, "pages": [page["page"] for page in members], "sha256": file_hash(output / name),
                        "view_file": view.name, "view_sha256": write_copy(sheet, view)})
        offset += size
    contact = output / "contact-sheet.png"
    with Image.open(contact) as image:
        contact_hash = write_copy(image, contact.with_suffix(".jpg"))
    manifest = {"schema_version": "ppt_preview_v1", "source_file": str(pptx), "source_sha256": file_hash(pptx),
                "created_at": _now(), "slide_count": len(pages), "pages": pages, "batches": batches,
                "contact_sheet": {"file": contact.name, "sha256": file_hash(contact),
                                  "view_file": "contact-sheet.jpg", "view_sha256": contact_hash},
                "risk_pages": [page["page"] for page in pages if page["risks"]], "text_verification": text_report}
    save_json(output / "manifest.json", manifest)
    return manifest


def invalidate_review(pptx: Path) -> None:
    report_path = pptx.parent / "qa" / "visual_review.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("source_sha256") != file_hash(pptx):
            report.update({"status": "stale", "invalidated_at": _now(), "reason": "PPTX 已重建，须确认新预览与审阅覆盖"})
            save_json(report_path, report)


def _manifest(pptx: Path, preview: Path):
    path = preview / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not {"pages", "batches", "contact_sheet", "slide_count", "text_verification"}.issubset(data):
        raise ValueError("预览清单版本不完整，请重新 preview")
    if data.get("source_sha256") != file_hash(pptx):
        raise ValueError("预览对应旧 PPTX，请先重新 preview")
    if not data.get("text_verification", {}).get("ok"):
        raise ValueError("当前预览未通过逐页文字保真检查")
    for page in data["pages"]:
        if file_hash(preview / page["file"]) != page["sha256"]:
            raise ValueError(f"第 {page['page']} 页预览图已改变，请重新 preview")
    for image in data["batches"] + [data["contact_sheet"]]:
        if file_hash(preview / image["file"]) != image["sha256"]:
            raise ValueError("分批图或总览图已改变，请重新 preview")
    for image in [*data["pages"], *data["batches"], data["contact_sheet"]]:
        if "view_file" in image and file_hash(preview / image["view_file"]) != image["view_sha256"]:
            raise ValueError("看图副本已改变，请从这份 PPTX 重新预览")
    return data


def _page_evidence(manifest: dict, page: int, filenames: list[str]) -> list[dict]:
    """Accept only current per-page or small-batch preview images for this page."""
    if not filenames:
        raise ValueError("review 须提供已查看的 --evidence-image；总览图不能作为逐页证据")
    allowed = {item["file"]: (item["sha256"], [item["page"]], "full")
               for item in manifest["pages"]}
    allowed.update({item["file"]: (item["sha256"], item["pages"], "batch")
                    for item in manifest["batches"] if 1 <= len(item["pages"]) <= 4})
    for item in manifest["pages"] + manifest["batches"]:
        if item["file"] in allowed and "view_file" in item:
            _, covered, scale = allowed[item["file"]]
            allowed[item["view_file"]] = (item["view_sha256"], covered, scale)
    evidence = []
    for filename in dict.fromkeys(filenames):
        if filename not in allowed:
            raise ValueError(f"{filename} 不是当前预览的单页图或至多四页审阅图")
        digest, covered, scale = allowed[filename]
        if page in covered:
            evidence.append({"file": filename, "sha256": digest, "scale": scale})
    if not evidence:
        raise ValueError(f"第 {page} 页没有对应的审阅图证据")
    if page in manifest["risk_pages"] and not any(item["scale"] == "full" for item in evidence):
        raise ValueError(f"第 {page} 页是风险页，须查看 slide_NN.png 原尺寸单页图")
    return evidence


def _valid_page_evidence(item: dict, manifest: dict) -> bool:
    evidence = item.get("evidence_images")
    if not isinstance(evidence, list) or not evidence or not str(item.get("finding") or "").strip():
        return False
    try:
        expected = _page_evidence(manifest, item["page"], [part["file"] for part in evidence])
    except (KeyError, TypeError, ValueError):
        return False
    return evidence == expected


def _write_review(pptx: Path, preview: Path, manifest: dict, results: dict) -> dict:
    from repair import unresolved_status
    passed = sorted(page for page, record in results.items() if record["status"] == "pass")
    fixes = sorted(page for page, record in results.items() if record["status"] == "fix")
    pending = sorted(set(range(1, manifest["slide_count"] + 1)) - set(passed) - set(fixes))
    repair_status = unresolved_status(pptx)
    status = "partial" if repair_status == "partial" else "needs_fix" if fixes or repair_status else "partial" if pending else "passed"
    report = {"schema_version": "ppt_visual_review_v2", "status": status, "source_file": str(pptx),
              "source_sha256": manifest["source_sha256"], "preview_directory": preview_reference(pptx, preview),
              "manifest_sha256": file_hash(preview / "manifest.json"), "updated_at": _now(),
              "reviewed_final_pages": passed, "fix_pages": fixes, "pending_pages": pending,
              "page_results": [results[page] for page in sorted(results)]}
    save_json(pptx.parent / "qa" / "visual_review.json", report)
    pack_path = pptx.parent / "task_pack.json"
    if pack_path.exists():
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        state = pack.setdefault("state", {})
        state.update({"status": "partial" if status == "partial" and repair_status else "needs_fix" if status == "needs_fix" else "preview_ready",
                      "current_stage": "visual_review", "updated_at": _now()})
        state.setdefault("artifacts", {}).update({"preview_manifest": str(preview / "manifest.json"),
                                                  "visual_review": str(pptx.parent / "qa" / "visual_review.json"),
                                                  "preview": str(preview), "pdf": str(preview / "presentation.pdf")})
        save_json(pack_path, pack)
    return report


def reconcile_review(pptx: Path, preview: Path) -> dict:
    preview = preview_path(pptx, preview)
    manifest = _manifest(pptx, preview)
    report_path = pptx.parent / "qa" / "visual_review.json"
    old = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    hashes = {page["page"]: page["sha256"] for page in manifest["pages"]}
    # 仅复用相同页码且像素未变的审阅；旧版无哈希的“通过”记录不能复用。
    from repair import unresolved_issues
    waiting = {row['page'] for row in unresolved_issues(pptx) if row['status'] == 'awaiting_review'}
    results = {item["page"]: item for item in old.get("page_results", [])
               if item.get("status") in {"pass", "fix"} and item.get("image_sha256") == hashes.get(item.get("page"))
               and item['page'] not in waiting
               and _valid_page_evidence(item, manifest)}
    return _write_review(pptx, preview, manifest, results)


def record_review(pptx: Path, preview: Path, pages: list[int], status: str, finding: str = "",
                  evidence_images: list[str] | None = None, source_files: list[str] | None = None) -> dict:
    from repair import close_issue, record_issue, validate_pass
    preview = preview_path(pptx, preview)
    manifest = _manifest(pptx, preview)
    if not pages or status not in {"pass", "fix"} or any(page < 1 or page > manifest["slide_count"] for page in pages):
        raise ValueError("review 需要有效页码和 --status pass 或 fix")
    if len(set(pages)) > 4:
        raise ValueError("一次 review 最多记录四页；请按审阅图批次分别写观察")
    if not finding.strip():
        raise ValueError("review 须用 --finding 记录具体观察；pass 也不能留空")
    if status == "fix" and len(set(pages)) != 1:
        raise ValueError("fix 一次只记录一页，以便绑定页码与源文件")
    names = []
    def wrong_preview(image):
        members = [*manifest["pages"], *manifest["batches"], manifest["contact_sheet"]]
        known = {item.get(key) for item in members for key in ("file", "view_file")}
        current = preview / image.name
        if image.name in known and current.is_file():
            return ValueError(f"{image} 属于其他目录。请看当前成品的 {current}，看过后用 {image.name} 登记。")
        return ValueError(f"{image} 不在本份 PPTX 当前预览目录内；当前目录：{preview}")
    for value in evidence_images or []:
        image = Path(value)
        if image.is_absolute():
            if image.resolve().parent != preview:
                raise wrong_preview(image)
            value = image.name
        elif len(image.parts) > 1:
            candidates = [image.resolve(), (pptx.parent / image).resolve(), (preview / image).resolve()]
            if not any(path.parent == preview and path.is_file() for path in candidates):
                raise wrong_preview(image)
            value = image.name
        names.append(value)
    evidence_by_page = {page: _page_evidence(manifest, page, names) for page in set(pages)}
    for page in pages:
        if status == "pass":
            validate_pass(pptx, page)
        elif not source_files:
            raise ValueError("fix 须用 --source-file 绑定页面源文件")
    report = reconcile_review(pptx, preview)
    results = {item["page"]: item for item in report["page_results"]}
    for page, evidence in evidence_by_page.items():
        if status == "fix":
            record_issue(pptx, page, finding, source_files, manifest["pages"][page - 1]["sha256"])
        else:
            close_issue(pptx, page)
        results[page] = {"page": page, "status": status, "image_sha256": manifest["pages"][page - 1]["sha256"],
                         "evidence_images": evidence, "finding": finding.strip(), "reviewed_at": _now()}
    return _write_review(pptx, preview, manifest, results)


def current_review_status(pptx: Path) -> str:
    from repair import unresolved_status
    report_path = pptx.parent / "qa" / "visual_review.json"
    if not report_path.exists():
        return "not_performed"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("source_sha256") != file_hash(pptx) or not report.get("preview_directory"):
        return "stale"
    try:
        preview = preview_path(pptx, report["preview_directory"])
        manifest = _manifest(pptx, preview)
        if report.get("manifest_sha256") != file_hash(preview / "manifest.json"):
            return "stale"
        hashes = {page["page"]: page["sha256"] for page in manifest["pages"]}
        results = report.get("page_results", [])
        if any(item.get("image_sha256") != hashes.get(item.get("page")) for item in results):
            return "stale"
        if any(not _valid_page_evidence(item, manifest) for item in results):
            return "partial"
        if report.get("status") == "passed" and {item["page"] for item in results if item.get("status") == "pass"} != set(hashes):
            return "partial"
    except (OSError, ValueError, KeyError):
        return "stale"
    return unresolved_status(pptx) or report.get("status", "partial")


def managed_preview_name(name: str) -> bool:
    return name in {"presentation.pdf", "contact-sheet.png", "contact-sheet.jpg", "manifest.json"} or bool(re.fullmatch(r"(?:slide_\d+|review_batch_\d+)\.(?:png|jpg)", name))
