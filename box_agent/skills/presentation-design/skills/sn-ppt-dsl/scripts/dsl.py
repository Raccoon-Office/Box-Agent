#!/usr/bin/env python3
"""校验 SML 页面并通过本地渲染器生成可编辑演示文稿。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "doubao"
sys.path.insert(0, str(VENDOR / "scripts"))
import xml_lint

NAMESPACE = "https://www.larkoffice.com/sml/2.0"
ET.register_namespace("", NAMESPACE)
ET.register_namespace("svg", "http://www.w3.org/2000/svg")


class DeckError(ValueError):
    """携带可定位错误，禁止把不完整成品标为成功。"""


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_xml(path: Path, captured=None) -> ET.Element:
    raw = path.read_bytes()
    if captured is not None:
        captured[path] = raw
    text = raw.decode("utf-8")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise DeckError(f"{path}：页面源文件不支持 DTD 或外部实体")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise DeckError(f"{path}：XML 解析失败：{exc}") from exc


def natural_key(path: Path) -> tuple:
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name))


def load_source(path: Path, captured=None, selected_page=None) -> tuple[ET.Element, dict[int, Path], list[Path]]:
    """目录中的逐页 XML 是修改基线，不引入第二种作者格式。"""
    origins: dict[int, Path] = {}
    if path.is_file():
        root = read_xml(path, captured)
        if local_name(root.tag) == "slide":
            deck = ET.Element(f"{{{NAMESPACE}}}presentation", {"width": "960", "height": "540"})
            deck.append(root)
            root = deck
        if local_name(root.tag) != "presentation":
            raise DeckError("输入必须是 presentation 或 slide XML")
        for node in root.iter():
            origins[id(node)] = path.parent
        return root, origins, [path]
    slide_dir = path / "slides"
    pages = [selected_page] if selected_page else sorted(slide_dir.glob("slide_*.xml"), key=natural_key)
    if not pages:
        raise DeckError(f"没有找到 {slide_dir}/slide_*.xml")
    root = ET.Element(f"{{{NAMESPACE}}}presentation", {"width": "960", "height": "540"})
    theme = path / "theme.xml"
    files = []
    if theme.exists():
        element = read_xml(theme, captured)
        if local_name(element.tag) != "theme":
            raise DeckError("theme.xml 的根元素必须是 theme")
        root.append(element)
        files.append(theme)
    for page in pages:
        element = read_xml(page, captured)
        if local_name(element.tag) != "slide":
            raise DeckError(f"{page}：逐页文件根元素必须是 slide")
        root.append(element)
        for node in element.iter():
            origins[id(node)] = page.parent
    outline = path / "outline.md"
    if outline.exists() and not selected_page:
        count = len(re.findall(r"^## 第\s*\d+\s*页", outline.read_text(encoding="utf-8"), re.M))
        if count and count != len(pages):
            raise DeckError(f"大纲要求 {count} 页，但 XML 只有 {len(pages)} 页")
    return root, origins, files + pages


def to_node(element: ET.Element, origins: dict[int, Path], address: str = "") -> dict:
    tag = local_name(element.tag)
    address = address or "/presentation"
    node = {"tag": tag, "attrs": dict(element.attrib), "text": element.text or "", "tail": element.tail or "", "path": address}
    if element.tag.startswith("{http://www.w3.org/2000/svg}"):
        # 独立 SVG 必须保留自己的命名空间，避免被误当成页面元素。
        raw = ET.tostring(element, encoding="unicode")
        raw = raw.replace("svg:", "").replace('xmlns:svg=', 'xmlns=')
        node["svg"] = raw
        return node
    if tag == "img":
        source = element.get("src", "")
        if not source or re.match(r"^[a-zA-Z][\w+.-]*:", source):
            raise DeckError(f"{address}：img.src 必须是本地路径，不使用 URL 或飞书 file_token")
        asset = Path(source).expanduser()
        asset = asset if asset.is_absolute() else origins[id(element)] / asset
        asset = asset.resolve()
        if not asset.is_file():
            raise DeckError(f"{address}：图片不存在：{asset}")
        node["asset"] = str(asset)
        node["asset_sha256"] = hashlib.sha256(asset.read_bytes()).hexdigest()
    children = []
    counts: dict[str, int] = {}
    for child in element:
        name = local_name(child.tag)
        counts[name] = counts.get(name, 0) + 1
        children.append(to_node(child, origins, f"{address}/{name}[{counts[name]}]"))
    node["children"] = children
    return node


def find_node() -> str:
    configured = os.environ.get("BOX_AGENT_NODE")
    node = configured if configured and Path(configured).is_file() else shutil.which("node")
    if not node:
        raise DeckError("缺少 Node.js，请安装 Node.js 22 或 24 后运行 npm ci")
    return node


def check_image_sources(source: Path, payload: dict) -> None:
    """核对页面引用与素材来源，阻止程序场景画冒充生图或事实照片。"""
    images = []
    stack = [payload]
    while stack:
        item = stack.pop()
        if item["tag"] == "img":
            images.append(item)
        stack.extend(item.get("children", []))
    if not images:
        return

    deck = source if source.is_dir() else source.parent.parent if source.parent.name == "slides" else source.parent
    assets = (deck / "assets").resolve()
    manifest = assets / "sources.json"
    if not manifest.is_file():
        raise DeckError(f"引用了图片，但缺少素材来源清单：{manifest}")
    try:
        records = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DeckError(f"素材来源清单不是有效 JSON：{manifest}") from exc
    if not isinstance(records, list):
        raise DeckError("assets/sources.json 必须是数组")
    by_file = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("file"), str):
            raise DeckError("素材来源记录必须包含 file 字符串")
        name = record["file"]
        if name in by_file:
            raise DeckError(f"素材来源清单重复记录：{name}")
        by_file[name] = record

    permitted = {
        "fact_photo": {"user_provided", "web_search"},
        "scene_illustration": {"user_provided", "web_search", "image_generation"},
        "diagram": {"user_provided", "web_search", "programmatic_vector"},
        "icon": {"user_provided", "web_search", "programmatic_vector"},
    }
    for image in images:
        image_path = Path(image["asset"])
        try:
            name = image_path.relative_to(assets).as_posix()
        except ValueError as exc:
            raise DeckError(f"{image['path']}：图片必须位于当前任务的 assets/ 目录") from exc
        record = by_file.get(name)
        if record is None:
            raise DeckError(f"{image['path']}：assets/sources.json 缺少 {name} 的来源记录")
        role = record.get("role")
        origin = record.get("origin")
        if role not in permitted or origin not in permitted[role]:
            raise DeckError(f"{image['path']}：{name} 的图片用途 {role!r} 与来源 {origin!r} 不相符")
        if origin == "web_search":
            url = str(record.get("source_url") or "")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or not record.get("license"):
                raise DeckError(f"{image['path']}：搜图素材 {name} 缺少有效来源页或许可")
        elif origin == "image_generation":
            prompt = str(record.get("prompt_file") or "")
            prompt_path = (deck / prompt).resolve()
            if not record.get("model") or not prompt or not prompt_path.is_file() or not prompt_path.is_relative_to(assets):
                raise DeckError(f"{image['path']}：生图素材 {name} 缺少模型或 assets/ 内的提示词文件")


def check_source(path: Path, report_path: Path, captured=None, selected_page=None) -> tuple[dict, dict]:
    root, origins, files = load_source(path, captured, selected_page)
    from xml_equivalence import normalize_chart_containers, normalize_shape_fill
    equivalents = normalize_chart_containers(root) + normalize_shape_fill(root)
    report = xml_lint.lint_xml(ET.tostring(root, encoding="unicode"), str(path))
    report['applied_equivalences'] = equivalents
    report["source_files"] = [str(file) for file in files]
    save_json(report_path, report)
    if report["summary"]["error_count"]:
        errors = report["document"]["errors"] + [issue for slide in report["slides"] for issue in slide["errors"]]
        messages = " ｜ ".join(issue["message"] for issue in errors[:2])
        raise DeckError(f"静态校验发现 {report['summary']['error_count']} 个错误：{messages}。详情：{report_path}")
    payload = to_node(root, origins)
    check_image_sources(path, payload)
    payload["source_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload, report


def run_renderer(payload: dict, output: Path | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="sn-ppt-dsl-") as temp:
        source = Path(temp) / "source.json"
        save_json(source, payload)
        command = [find_node(), str(ROOT / "scripts" / "render.mjs"), str(source)]
        command += [str(output)] if output else ["--check"]
        env = os.environ.copy()
        env["FONTCONFIG_FILE"] = str(ROOT / "assets" / "fonts" / "fonts.conf")
        result = subprocess.run(command, text=True, capture_output=True, timeout=180, env=env)
        if result.returncode:
            raise DeckError(result.stderr.strip() or result.stdout.strip() or "本地编译失败")
        return json.loads(result.stdout)


def check_pages(deck: Path, numbers: list[int]) -> dict:
    if not deck.is_dir() or any(n < 1 for n in numbers):
        raise DeckError("按页检查需要任务目录和正整数页码")
    by_number = {}
    for path in (deck / "slides").glob("slide_*.xml"):
        match = re.fullmatch(r"slide_(\d+)\.xml", path.name)
        if match:
            by_number.setdefault(int(match[1]), []).append(path)
    results = []
    for number in dict.fromkeys(numbers):
        record = {"page": number, "ok": False}
        report_path = deck / "qa" / f"slide_{number:02d}.json"
        before = report_path.stat().st_mtime_ns if report_path.is_file() else None
        try:
            paths = by_number.get(number, [])
            if len(paths) != 1:
                raise DeckError("页面文件缺失" if not paths else "有多个文件使用同一页码")
            payload, report = check_source(deck, report_path, selected_page=paths[0])
            run_renderer(payload, None)
            record.update(ok=True, warnings=report["summary"]["warning_count"])
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            record["error"] = str(exc)
            if report_path.is_file() and report_path.stat().st_mtime_ns != before:
                details = json.loads(report_path.read_text(encoding="utf-8"))
            else:
                details = {"page": number}
            details["check_error"] = str(exc)
            save_json(report_path, details)
        record["report"] = str(report_path)
        results.append(record)
    return {"ok": all(r["ok"] for r in results), "pages": results}


def active_preview(pptx: Path) -> Path:
    """Use the review's current preview, or a fresh path after a rebuild."""
    from review import _manifest, file_hash, preview_path
    report = pptx.parent / "qa" / "visual_review.json"
    if report.is_file():
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            preview = preview_path(pptx, data["preview_directory"])
            if data.get("source_sha256") == file_hash(pptx) and preview.is_dir():
                _manifest(pptx, preview)
                return preview
        except (OSError, ValueError, KeyError, TypeError):
            pass
    initial = pptx.parent / "preview"
    if not initial.exists():
        return initial
    prefix = file_hash(pptx)[:12]
    for number in range(1, 100):
        suffix = "" if number == 1 else f"-{number:02}"
        candidate = pptx.parent / "qa" / "previews" / f"pptx-{prefix}{suffix}"
        if not candidate.exists():
            return candidate
    raise DeckError("预览版本目录已满，请人工清理当前任务的旧预览")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "build", "verify", "preview", "review", "repair-start", "page-source"])
    parser.add_argument("input", type=Path, help="任务目录、XML 或最终 PPTX 的路径")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--batch-size", type=int, choices=[2, 3, 4], default=4)
    parser.add_argument("--pages", nargs="+", type=int)
    parser.add_argument("--status", choices=["pass", "fix"])
    parser.add_argument("--finding", default="")
    parser.add_argument("--evidence-image", action="append", default=[])
    parser.add_argument("--source-file", action="append", default=[])
    args = parser.parse_args()
    source = args.input.expanduser().resolve()
    try:
        if args.command == "check" and args.pages:
            result = check_pages(source, args.pages)
            if args.report:
                output = args.report.resolve()
                if any(output == Path(item["report"]) for item in result["pages"] if "report" in item):
                    raise DeckError("汇总报告与单页详情须使用不同路径")
                save_json(output, result)
            print(json.dumps(result, ensure_ascii=False), file=sys.stdout if result["ok"] else sys.stderr)
            return 0 if result["ok"] else 1
        if args.command == "page-source":
            from repair import built_sources
            result = built_sources(source, args.pages)
            if args.pages:
                excerpts = {}
                for page, path in result["pages"].items():
                    root = read_xml(Path(path))
                    lines = ["".join(node.itertext()).strip() for node in root.findall(".//{*}data//{*}p")]
                    text = "\n".join(line for line in lines if line)
                    excerpts[page] = text[:800] + ("…" if len(text) > 800 else "")
                result["visible_text_excerpt"] = excerpts
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "repair-start":
            from repair import begin_repair
            result = begin_repair(source, args.pages)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command in {"verify", "preview", "review"}:
            from verify import verify_pptx, preview_pptx
            from review import current_review_status, record_review, preview_path
            result = verify_pptx(source, args.expected_pages)
            from repair import built_sources, source_changes
            changed = source_changes(source)
            if changed:
                result['unbuilt_pages'] = sorted(changed)
                result['editing_note'] = '以下审阅绑定当前 PPTX；未导出修改单列，不能用此结论确认修改后的画面。'
                result['published_sources'] = built_sources(source, sorted(changed))['pages']
            if args.command == "preview":
                preview = preview_path(source, args.output) if args.output else active_preview(source)
                result["preview"] = preview_pptx(source, preview, args.batch_size)
            elif args.command == "review":
                preview = preview_path(source, args.output) if args.output else active_preview(source)
                result.update(record_review(source, preview, args.pages, args.status,
                                            args.finding, args.evidence_image, args.source_file))
            result["visual_review"] = current_review_status(source)
            from repair import unresolved_issues
            result["repair_issues"] = unresolved_issues(source)
            if args.command != "review":
                if result["visual_review"] in {"passed", "partial", "needs_fix"}:
                    review = json.loads((source.parent / "qa" / "visual_review.json").read_text(encoding="utf-8"))
                    result.update({key: review[key] for key in ("reviewed_final_pages", "fix_pages", "pending_pages")})
                else:
                    result.update({"reviewed_final_pages": [], "fix_pages": [], "pending_pages": list(range(1, result["slides"] + 1))})
            if args.command == "preview":
                save_json(args.report or source.parent / "qa" / "preview.json", result)
            if args.report:
                save_json(args.report, result)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        base = source if source.is_dir() else source.parent
        report_path = (args.report or base / "qa" / "xml-lint.json").resolve()
        captured = {} if args.command == "build" and source.is_dir() else None
        payload, report = check_source(source, report_path, captured)
        if args.command == "check":
            result = run_renderer(payload, None)
            result.pop("texts", None)
            result.update({"report": str(report_path), "summary": report["summary"], "source_sha256": payload["source_sha256"]})
        else:
            output = (args.output or base / "presentation.pptx").resolve()
            if output.suffix.lower() != ".pptx":
                raise DeckError("输出文件扩展名必须是 .pptx，防止覆盖 XML 源文件")
            output.parent.mkdir(parents=True, exist_ok=True)
            from repair import validate_build, record_build
            repair_plan = validate_build(output)
            # 临时成品先验收，失败时保留用户上一份可用文件。
            with tempfile.TemporaryDirectory(prefix=".dsl-build-", dir=output.parent) as temp:
                pending = Path(temp) / output.name
                result = run_renderer(payload, pending)
                from verify import verify_pptx
                from chart_verification import source_charts
                result["verification"] = verify_pptx(pending, result["slides"], result.get("texts"), source_charts(payload))
                if captured is not None:
                    from repair import archive_sources
                    result["slide_source_sha256"], result["theme_sha256"] = archive_sources(
                        output, base, result["verification"]["sha256"], captured)
                pending.replace(output)
            from review import invalidate_review
            invalidate_review(output)
            record_build(output, repair_plan)
            result.pop("texts", None)
            result.update({"output": str(output), "source_sha256": payload["source_sha256"], "report": str(report_path)})
            result["verification"]["file"] = str(output)
            result.setdefault("slide_source_sha256", {})
            if source.is_dir():
                slide_nodes = [node for node in payload.get("children", []) if node["tag"] == "slide"]
                result["page_asset_sha256"] = {}
                for page, slide_node in enumerate(slide_nodes, 1):
                    assets = {}
                    stack = [slide_node]
                    while stack:
                        node = stack.pop()
                        if node["tag"] == "img":
                            name = Path(node["asset"]).relative_to(base).as_posix()
                            assets[name] = node["asset_sha256"]
                        stack.extend(node.get("children", []))
                    result["page_asset_sha256"][str(page)] = assets
            save_json(base / "qa" / "build.json", result)
            if source.is_dir() and (source / "task_pack.json").exists():
                pack = json.loads((source / "task_pack.json").read_text(encoding="utf-8"))
                # 历史任务完成 XML 构建后，后续恢复也必须指向同一生成流程。
                pack["ppt_mode"] = "dsl"
                choices = pack.setdefault("choices", {})
                choices["output"] = "xml_pptx"
                choices.pop("static_postprocess", None)
                state = pack.setdefault("state", {})
                state.setdefault("artifacts", {}).update({"pptx": str(output), "xml_pages": str(source / "slides"), "xml_qa": str(report_path)})
                from repair import unresolved_status
                state.update({"current_stage": "xml_pptx",
                              "status": "needs_fix" if unresolved_status(output) else "rendered", "last_error": None})
                save_json(source / "task_pack.json", pack)
        if report['applied_equivalences']:
            result['applied_equivalences'] = report['applied_equivalences']
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (DeckError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
