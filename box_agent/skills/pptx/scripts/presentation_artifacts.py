"""Read-only receipts for the existing fast and dynamic HTML delivery routes."""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from presentation_delivery import _sha, inspect_pptx


class _Deck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pages = 0
        self.references: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "slide" in (attributes.get("class") or "").split():
            self.pages += 1
        for key in ("src", "href"):
            if attributes.get(key):
                self.references.append(attributes[key])


def _digest(path: Path) -> str:
    if path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("artifact exceeds inspection limit")
    return _sha(path)


def _local_file(root: Path, path: Path) -> Path:
    if not path.is_absolute() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError(f"missing or external presentation resource: {path}")
    return path


def inspect_existing(root: Path, *, revision: str, dynamic: bool,
                     required_formats: list[str], expected_pages: int | None) -> dict:
    receipt = {"status": "error", "revision": revision, "deck_dir": str(root),
               "required_formats": list(required_formats), "artifacts": [], "inputs": {}, "warnings": []}
    try:
        html = root / ("deck.html" if dynamic else "index.html")
        report = None
        if not dynamic and (root / "qa/design_delivery.json").is_file():
            candidate = json.loads((root / "qa/design_delivery.json").read_text())
            artifact = Path(candidate.get("primary_artifact", ""))
            if candidate.get("terminal") is True:
                # A terminal recovery outcome cannot be superseded by an older
                # normal-finalizer receipt merely because its HTML is unbound.
                report = candidate
                receipt["warnings"] = candidate.get("warnings", [])
                receipt["inputs"]["qa/design_delivery.json"] = _digest(root / "qa/design_delivery.json")
                html = _local_file(root, artifact)
                retained_partial = (candidate.get("status") == "partial"
                                    and candidate.get("html_hash") is None)
                if not retained_partial and _digest(html) != candidate.get("html_hash"):
                    raise ValueError("fast fallback receipt is not bound to current HTML")
        if not html.is_file() or html.stat().st_size == 0:
            raise ValueError(f"missing HTML delivery: {html}")
        _local_file(root, html)
        parsed = _Deck()
        parsed.feed(html.read_text(encoding="utf-8"))
        count = parsed.pages
        if count <= 0:
            raise ValueError("HTML contains no presentation pages")
        receipt["pages"] = count
        receipt["artifacts"].append({"path": str(html), "format": "html", "pages": count,
                                     "bytes": html.stat().st_size, "sha256": _digest(html)})
        receipt["status"] = "partial"
        if expected_pages is not None and count != expected_pages:
            raise ValueError(f"HTML page count {count} != required {expected_pages}")
        for reference in parsed.references:
            url = urlsplit(reference)
            if url.scheme in {"http", "https", "data", "mailto"} or url.netloc or not url.path:
                continue
            target = (html.parent / unquote(url.path)).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError(f"missing or external local HTML dependency: {reference}")
            receipt["inputs"][str(target.relative_to(root))] = _digest(target)
        if dynamic:
            manifest = root / "shots/render.json"
            _local_file(root, manifest)
            value = json.loads(manifest.read_text())
            if (value.get("deck") != str(html) or value.get("deck_sha256") != _digest(html)
                    or value.get("mode") != "all" or value.get("n_pages") != count
                    or len(value.get("pages", [])) != count or {p.get("page") for p in value.get("pages", [])}
                    != set(range(1, count + 1)) or manifest.stat().st_mtime_ns < html.stat().st_mtime_ns):
                raise ValueError("dynamic render coverage is missing or stale; render the current deck with --all")
            if value.get("blank_pages") or value.get("console_errors"):
                raise ValueError("dynamic render reported blank pages or console errors")
            receipt["inputs"]["shots/render.json"] = _digest(manifest)
            from PIL import Image
            for page in value["pages"]:
                png = _local_file(root / "shots", Path(page.get("png", "")))
                if png.stat().st_mtime_ns < html.stat().st_mtime_ns:
                    raise ValueError("dynamic screenshot predates the current HTML")
                with Image.open(png) as picture:
                    if picture.format != "PNG" or min(picture.size) <= 0:
                        raise ValueError("dynamic render is not a valid PNG")
                    picture.verify()
                receipt["inputs"][str(png.relative_to(root))] = _digest(png)
        elif report is None:
            binding_path = _local_file(root, root / "qa/delivery_receipt.json")
            binding = json.loads(binding_path.read_text())
            deck = _local_file(root, Path(binding.get("deck", "")))
            if (binding.get("html") != str(html) or binding.get("html_sha256") != _digest(html)
                    or binding.get("deck_sha256") != _digest(deck)):
                raise ValueError("fast finalizer receipt is not bound to current deck and HTML")
            receipt["inputs"][str(deck.relative_to(root))] = _digest(deck)
            receipt["inputs"]["qa/delivery_receipt.json"] = _digest(binding_path)
            for name in ("deck_spec.json", "truth_check.json", "html_self_check.json", "runtime_probe.json"):
                path = _local_file(root, root / "qa" / name)
                if binding.get("report_sha256", {}).get(str(path)) != _digest(path):
                    raise ValueError(f"fast check changed after finalization: {name}")
                value = json.loads(path.read_text())
                if not value.get("ok") and value.get("advisory") is not True:
                    raise ValueError(f"fast delivery check failed: {name}")
                receipt["inputs"][f"qa/{name}"] = _digest(path)
                if value.get("advisory") is True:
                    receipt["warnings"].append(f"{name}: advisory; review not fully verified")
        else:
            content_inputs = report.get("content_inputs")
            if not isinstance(content_inputs, dict) or "outline" not in content_inputs:
                raise ValueError("fast fallback receipt has no current content input binding")
            for name, binding in content_inputs.items():
                path = _local_file(root, Path(binding["path"]))
                digest = _digest(path)
                if digest != binding.get("sha256"):
                    raise ValueError(f"fast fallback content input changed: {name}")
                receipt["inputs"][str(path.relative_to(root))] = digest
            if report.get("status") == "partial" or report.get("page_count_satisfied") is False or report.get("content_complete") is False:
                raise ValueError("fast fallback did not satisfy the requested page/content contract")
        if "pptx" in required_formats:
            candidates = sorted(root.glob("*.pptx"))
            if len(candidates) != 1:
                raise ValueError("requested PPTX is missing or ambiguous; use the existing fast export workflow")
            pptx = candidates[0]
            inspect_pptx(pptx, expected_pages=count)
            if pptx.stat().st_mtime_ns < html.stat().st_mtime_ns:
                raise ValueError("PPTX predates the current HTML; export the updated presentation")
            receipt["artifacts"].append({"path": str(pptx), "format": "pptx", "pages": count,
                                         "bytes": pptx.stat().st_size, "sha256": _digest(pptx)})
        receipt["status"] = "complete"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as exc:
        receipt["error"] = str(exc)
    return receipt
