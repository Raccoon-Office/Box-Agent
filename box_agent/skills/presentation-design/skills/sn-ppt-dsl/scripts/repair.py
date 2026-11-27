"""Track bounded, page-scoped repairs across PPTX builds and previews."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from review import file_hash, save_json, _now

MAX_ATTEMPTS = 2


def archive_sources(pptx, base, digest, captured):
    directory = pptx.parent / "qa" / "built-sources"
    objects = directory / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    files = {}
    for path, data in captured.items():
        key = sha256(data).hexdigest()
        dest = objects / (key + ".xml")
        if not dest.is_file() or file_hash(dest) != key:
            with NamedTemporaryFile(dir=objects, delete=False) as f:
                tmp = Path(f.name)
                try:
                    f.write(data)
                    f.flush()
                    tmp.replace(dest)
                finally:
                    tmp.unlink(missing_ok=True)
        files[path.relative_to(base).as_posix()] = key
    save_json(directory / (digest + ".json"), {"pptx_sha256": digest, "sources": files})
    return {k:v for k,v in files.items() if k.startswith("slides/")}, files.get("theme.xml")


def built_sources(pptx, pages=None):
    digest = file_hash(pptx)
    directory = pptx.parent / "qa" / "built-sources"
    manifest = directory / (digest + ".json")
    data = json.loads(manifest.read_text()) if manifest.is_file() else {}
    found = {}
    for name, key in data.get("sources", {}).items():
        match = re.fullmatch(r"slides/slide_(\d+)\.xml", name)
        if not match or data.get("pptx_sha256") != digest or not re.fullmatch(r"[0-9a-f]{64}",key):
            continue
        page = int(match[1])
        if pages is not None and page not in pages:
            continue
        src = directory / "objects" / (key + ".xml")
        if src.is_file() and file_hash(src) == key:
            found[page] = str(src)
    return {"pptx_sha256": digest, "pages": found,
            "unavailable_pages": sorted(set(pages or []) - set(found))}


def exhausted_message(pptx, page):
    source = built_sources(pptx, [page])["pages"].get(page)
    hint = f" 已导出的本页 XML：{source}。" if source else ""
    return (f"第 {page} 页返修预算已用尽，上次成品仍在。对现有 PPTX 继续看图登记；"
            f"若本页仍有缺陷，保留可用 PPTX 并注明本页问题（partial）。{hint}")


def _path(pptx: Path) -> Path:
    return pptx.parent / "qa" / "repair_ledger.json"


def load_ledger(pptx: Path) -> dict:
    path = _path(pptx)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema_version": "ppt_repair_ledger_v1", "max_attempts_per_page": MAX_ATTEMPTS,
            "issues": {}, "updated_at": _now()}


def _save(pptx: Path, ledger: dict) -> None:
    ledger["updated_at"] = _now()
    save_json(_path(pptx), ledger)


def _sources(pptx: Path, page: int, files: list[str] | None) -> dict[str, str]:
    if not files:
        raise ValueError("标记 fix 须提供 --source-file，绑定需修改的 XML 源文件")
    base = pptx.parent.resolve()
    expected = (base / "slides" / f"slide_{page:02}.xml").resolve()
    result = {}
    for raw in files:
        candidate = Path(raw)
        candidate = (candidate if candidate.is_absolute() else base / candidate).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            raise ValueError(f"源文件不存在或不在任务目录内：{raw}")
        result[str(candidate)] = file_hash(candidate)
    if str(expected) not in result:
        raise ValueError(f"第 {page} 页须绑定 slides/slide_{page:02}.xml")
    return result


def record_issue(pptx: Path, page: int, finding: str, source_files: list[str] | None,
                 image_sha256: str) -> dict:
    sources = _sources(pptx, page, source_files)
    ledger = load_ledger(pptx)
    key = str(page)
    issue = ledger["issues"].get(key, {"page": page, "attempts": []})
    if issue.get("status") == "repairing":
        raise ValueError(f"第 {page} 页返修尚未完成构建，不能再次记录检查结果")
    issue.update({"status": "exhausted" if len(issue["attempts"]) >= MAX_ATTEMPTS else "needs_fix",
                  "finding": finding.strip(), "source_files": sources,
                  "detected_pptx_sha256": file_hash(pptx),
                  "detected_image_sha256": image_sha256, "updated_at": _now()})
    ledger["issues"][key] = issue
    _save(pptx, ledger)
    return issue


def begin_repair(pptx: Path, pages: list[int] | None) -> dict:
    if not pages or len(set(pages)) != len(pages):
        raise ValueError("repair-start 须提供不重复的待修页码")
    ledger = load_ledger(pptx)
    for page in pages:
        issue = ledger["issues"].get(str(page))
        if not issue or issue["status"] == "closed":
            raise ValueError(f"第 {page} 页没有待修问题")
        if len(issue["attempts"]) >= MAX_ATTEMPTS:
            raise ValueError(exhausted_message(pptx, page))
        if issue["status"] not in {"needs_fix"}:
            raise ValueError(f"第 {page} 页已有返修正在进行，先完成构建和复验")
    build_path = pptx.parent / "qa" / "build.json"
    build = json.loads(build_path.read_text(encoding="utf-8")) if build_path.is_file() else {}
    baseline = {}
    if build.get("verification", {}).get("sha256") == file_hash(pptx):
        baseline = build.get("slide_source_sha256") or {}
    for page in pages:
        issue = ledger["issues"][str(page)]
        before = {}
        for path in issue["source_files"]:
            relative = Path(path).resolve().relative_to(pptx.parent.resolve()).as_posix()
            asset_baseline = (build.get("page_asset_sha256") or {}).get(str(page), {})
            before[path] = baseline.get(relative, asset_baseline.get(relative, file_hash(Path(path))))
        issue["attempts"].append({"number": len(issue["attempts"]) + 1,
                                  "status": "started", "started_at": _now(),
                                  "before_pptx_sha256": file_hash(pptx),
                                  "before_source_sha256": before})
        issue["status"] = "repairing"
    _save(pptx, ledger)
    return {"ok": True, "pages": pages, "remaining_attempts":
            {str(page): MAX_ATTEMPTS - len(ledger["issues"][str(page)]["attempts"]) for page in pages}}


def source_changes(pptx: Path, *, require_preview: bool = False) -> dict:
    """Return the old hash for every changed page dependency in the managed deck."""
    qa = pptx.parent / "qa"
    if not pptx.is_file() or not (qa / "build.json").is_file():
        return {}
    build = json.loads((qa / "build.json").read_text(encoding="utf-8"))
    previous = build.get("slide_source_sha256") or {}
    if not previous or build.get("verification", {}).get("sha256") != file_hash(pptx):
        return {}
    if require_preview and not (qa / "visual_review.json").is_file():
        return {}
    slides = pptx.parent / "slides"
    current = {item.relative_to(pptx.parent).as_posix(): file_hash(item)
               for item in slides.glob("slide_*.xml")}
    changes = {}
    def note(page, key, old, retained=True):
        change = changes.setdefault(page, {"retained": True, "before": {}})
        change["retained"] &= retained
        change["before"][key] = old

    for name in sorted(set(previous) | set(current)):
        match = re.fullmatch(r"slides/slide_(\d+)\.xml", name)
        if match and previous.get(name) != current.get(name):
            note(int(match.group(1)), name, previous.get(name), name in previous and name in current)
    dependencies = {}
    for page, assets in (build.get("page_asset_sha256") or {}).items():
        for name, old in assets.items():
            dependencies.setdefault(name, []).append((int(page), old))
    if "theme_sha256" in build:
        for name in previous:
            match = re.fullmatch(r"slides/slide_(\d+)\.xml", name)
            if match:
                dependencies.setdefault("theme.xml", []).append((int(match.group(1)), build["theme_sha256"]))
    for name, references in dependencies.items():
        item = (pptx.parent / name).resolve()
        current_hash = file_hash(item) if item.is_relative_to(pptx.parent.resolve()) and item.is_file() else None
        for page, old in references:
            if current_hash != old:
                note(page, name, old)
    return changes


def _hash_or_none(path: str):
    file = Path(path)
    return file_hash(file) if file.is_file() else None


def validate_build(pptx: Path) -> dict:
    """Prepare a ledger update in memory. A failed render or package check spends no implicit attempt."""
    ledger = load_ledger(pptx)
    changes = source_changes(pptx, require_preview=True)
    for page, change in changes.items():
        if not change["retained"]:
            raise ValueError(f"第 {page} 页在预览后增删；请保留已确认的页序")
        key = str(page)
        issue = ledger["issues"].get(key)
        old = {str((pptx.parent / name).resolve()): value for name, value in change["before"].items()}
        if issue is None:
            issue = {"page": page, "attempts": [], "status": "needs_fix", "source_files": {},
                     "finding": "本页源文件已更新，须在新成品中核对画面",
                     "detected_pptx_sha256": file_hash(pptx)}
            ledger["issues"][key] = issue
        if issue["status"] != "repairing":
            if len(issue["attempts"]) >= MAX_ATTEMPTS:
                raise ValueError(exhausted_message(pptx, page))
            issue["attempts"].append({"number": len(issue["attempts"]) + 1, "status": "started",
                   "origin": "observed_source_change", "started_at": _now(),
                   "before_pptx_sha256": file_hash(pptx), "before_source_sha256": {}})
            issue["status"] = "repairing"
        issue["source_files"].update(old)
        issue["attempts"][-1]["before_source_sha256"].update(old)
    for issue in ledger["issues"].values():
        if issue["status"] != "repairing":
            continue
        attempt = issue["attempts"][-1]
        before = attempt["before_source_sha256"]
        if attempt["status"] != "started" or not any(_hash_or_none(path) != digest for path, digest in before.items()):
            raise ValueError(f"第 {issue['page']} 页绑定的源文件未修改，不能计作返修构建")
    return ledger


def record_build(pptx: Path, prepared: dict | None = None) -> None:
    ledger = prepared if prepared is not None else load_ledger(pptx)
    if not ledger["issues"]:
        return
    digest = file_hash(pptx)
    for issue in ledger["issues"].values():
        if issue["status"] == "repairing":
            attempt = issue["attempts"][-1]
            attempt.update({"status": "built", "built_at": _now(), "pptx_sha256": digest,
                            "after_source_sha256": {path: _hash_or_none(path) for path in issue["source_files"]}})
            issue["status"] = "awaiting_review"
        elif issue["status"] == "awaiting_review":
            # Any changed page would have entered a new attempt above. Its outstanding review
            # can be completed on the new package even when another page alone was rebuilt.
            issue["attempts"][-1]["review_pptx_sha256"] = digest
    _save(pptx, ledger)


def validate_pass(pptx: Path, page: int) -> None:
    issue = load_ledger(pptx)["issues"].get(str(page))
    if not issue or issue["status"] == "closed":
        return
    attempts = issue["attempts"]
    if not attempts or attempts[-1]["status"] != "built" or attempts[-1].get("review_pptx_sha256", attempts[-1]["pptx_sha256"]) != file_hash(pptx):
        raise ValueError(f"第 {page} 页仍有待修问题；须修改源文件、重建 PPTX 并预览后才能记 pass")


def close_issue(pptx: Path, page: int) -> None:
    ledger = load_ledger(pptx)
    issue = ledger["issues"].get(str(page))
    if issue and issue["status"] != "closed":
        issue["status"] = "closed"
        issue["resolved_pptx_sha256"] = file_hash(pptx)
        issue["resolved_at"] = _now()
        _save(pptx, ledger)


def unresolved_status(pptx: Path) -> str | None:
    issues = [issue for issue in load_ledger(pptx)["issues"].values() if issue["status"] != "closed"]
    if any(issue["status"] == "exhausted" for issue in issues):
        return "partial"
    return "needs_fix" if issues else None


def unresolved_issues(pptx: Path) -> list[dict]:
    """Return page, finding and spent attempts for an honest partial handoff."""
    return [{"page": issue["page"], "finding": issue["finding"],
             "status": issue["status"], "attempts_used": len(issue["attempts"]),
             "max_attempts": MAX_ATTEMPTS}
            for issue in load_ledger(pptx)["issues"].values() if issue["status"] != "closed"]
