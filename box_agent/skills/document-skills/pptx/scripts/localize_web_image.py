#!/usr/bin/env python3
"""Localize one selected ``web_search(SearchType=image)`` result for PPT use.

Search remains an Agent tool call so it uses the runtime's configured MCP,
authentication, limits, logging, and concurrency.  This helper accepts only a
small selected-result receipt, validates and downloads the image, then updates
the scaffolded image manifest atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps


USER_AGENT = "Box-Agent-PPT/1.0 (https://github.com/Raccoon-Office/Box-Agent)"
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
TERMINAL_SEARCH_STATUSES = {"exhausted", "unavailable"}


@dataclass(frozen=True)
class WebImageCandidate:
    slide: int
    query: str
    reference_tag: str
    title: str
    source_page_url: str
    image_url: str
    reported_width: int
    reported_height: int
    alt: str
    shape: str
    clarity: str
    category: str
    watermark: str
    description: str
    style_type: str


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _load_candidate(path: Path) -> WebImageCandidate:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidate must be a JSON object")
    image = payload.get("image")
    if not isinstance(image, dict):
        raise ValueError("candidate.image must be an object")

    slide = _positive_int(payload.get("slide"))
    query = _clean_text(payload.get("query"))
    image_url = _clean_text(image.get("url"))
    if not slide:
        raise ValueError("candidate.slide must be a positive integer")
    if not query:
        raise ValueError("candidate.query is required")
    if not image_url:
        raise ValueError("candidate.image.url is required")

    return WebImageCandidate(
        slide=slide,
        query=query,
        reference_tag=_clean_text(payload.get("reference_tag")),
        title=_clean_text(payload.get("title")),
        source_page_url=_clean_text(payload.get("source_url")),
        image_url=image_url,
        reported_width=_positive_int(image.get("width")),
        reported_height=_positive_int(image.get("height")),
        alt=_clean_text(image.get("alt")),
        shape=_clean_text(image.get("shape")),
        clarity=_clean_text(image.get("clarity")),
        category=_clean_text(image.get("category")),
        watermark=_clean_text(image.get("watermark")),
        description=_clean_text(image.get("description")),
        style_type=_clean_text(image.get("style_type")),
    )


def _safe_https_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("web image URL must be absolute HTTPS")
    return value


def _reject_reported_quality(candidate: WebImageCandidate) -> None:
    if candidate.clarity and "模糊" in candidate.clarity:
        raise ValueError(f"search result is reported as {candidate.clarity}")
    if candidate.watermark.casefold() not in {"", "0", "false", "none", "无"}:
        raise ValueError("search result reports a visible watermark")


def _download_and_normalize(
    image_url: str,
    target_path: Path,
    *,
    placement: str,
    timeout: int,
) -> tuple[int, int]:
    response = requests.get(
        _safe_https_url(image_url),
        headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
        timeout=timeout,
        stream=True,
    )
    response.raise_for_status()
    _safe_https_url(str(response.url))
    declared_length = _positive_int(response.headers.get("Content-Length"))
    if declared_length > MAX_DOWNLOAD_BYTES:
        raise ValueError("web image exceeds the 25 MiB download limit")

    payload = bytearray()
    for chunk in response.iter_content(64 * 1024):
        payload.extend(chunk)
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise ValueError("web image exceeds the 25 MiB download limit")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.stem}.", suffix=".tmp", dir=str(target_path.parent)
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_bytes(payload)
        with Image.open(temp_path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            width, height = image.size
            min_width, min_height = (
                (1200, 675) if placement == "full-slide" else (800, 500)
            )
            if width < min_width or height < min_height:
                raise ValueError(
                    f"downloaded image {width}x{height} is below "
                    f"{min_width}x{min_height}"
                )
            if image.mode != "RGB":
                background = Image.new("RGB", image.size, "white")
                if image.mode in {"RGBA", "LA"}:
                    rgba = image.convert("RGBA")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    background.paste(image.convert("RGB"))
                image = background
            image.save(temp_path, format="JPEG", quality=90, optimize=True)
        os.replace(temp_path, target_path)
        return width, height
    finally:
        temp_path.unlink(missing_ok=True)


def _artifact_root(manifest_path: Path) -> Path:
    resolved = manifest_path.resolve()
    if len(resolved.parents) < 3:
        raise ValueError("manifest path cannot resolve an artifact root")
    return resolved.parents[2]


def _resolve_artifact_path(artifact_root: Path, relative_path: str) -> Path:
    path_value = Path(str(relative_path or ""))
    if not relative_path or path_value.is_absolute() or ".." in path_value.parts:
        raise ValueError("path must be artifact-root-relative")
    resolved = (artifact_root / path_value).resolve()
    if resolved != artifact_root and artifact_root not in resolved.parents:
        raise ValueError("path escapes the artifact root")
    return resolved


def _write_json_atomic(path: Path, payload: dict) -> None:
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _read_manifest(manifest_path: Path) -> tuple[dict, list[dict], Path]:
    resolved = manifest_path.resolve()
    manifest = json.loads(resolved.read_text(encoding="utf-8"))
    image_plan = manifest.get("image_plan") if isinstance(manifest, dict) else None
    if not isinstance(image_plan, list):
        raise ValueError("manifest.image_plan must be an array")
    return manifest, image_plan, _artifact_root(resolved)


def _web_entry(image_plan: list[dict], slide: int) -> dict:
    entry = next(
        (
            item
            for item in image_plan
            if isinstance(item, dict) and _positive_int(item.get("slide")) == slide
        ),
        None,
    )
    if entry is None:
        raise ValueError(f"slide {slide}: image-plan entry not found")
    if entry.get("acquire_via") != "web" or entry.get("decision") != "generate":
        raise ValueError(f"slide {slide}: entry is not a pending web image job")
    search = entry.get("search")
    if not isinstance(search, dict):
        raise ValueError(f"slide {slide}: web acquisition requires search metadata")
    if search.get("provider") != "web_search" or search.get("search_type") != "image":
        raise ValueError(f"slide {slide}: search must use web_search SearchType=image")
    return entry


def import_candidate(
    manifest_path: Path,
    candidate_path: Path,
    *,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, object]:
    manifest_path = manifest_path.resolve()
    manifest, image_plan, artifact_root = _read_manifest(manifest_path)
    candidate_resolved = candidate_path.resolve()
    if artifact_root not in candidate_resolved.parents:
        raise ValueError("candidate file must be inside the presentation artifact root")
    candidate = _load_candidate(candidate_resolved)
    entry = _web_entry(image_plan, candidate.slide)
    search = entry["search"]
    if " ".join(candidate.query.casefold().split()) != " ".join(
        str(search.get("query") or "").casefold().split()
    ):
        raise ValueError("candidate.query must exactly match the scaffolded search query")

    _reject_reported_quality(candidate)
    target_path = _resolve_artifact_path(
        artifact_root,
        str(search.get("output_path") or ""),
    )
    width, height = _download_and_normalize(
        candidate.image_url,
        target_path,
        placement=str(entry.get("placement") or "fixed-frame"),
        timeout=timeout,
    )

    entry["decision"] = "use_existing"
    entry["status"] = "ready"
    entry["resolved_via"] = "web"
    entry["origin"] = "sourced"
    entry["output_path"] = str(search["output_path"])
    entry["asset_hash"] = hashlib.sha256(target_path.read_bytes()).hexdigest()
    entry["source"] = {
        "provider": "web_search",
        "search_type": "image",
        "title": candidate.title,
        "alt": candidate.alt,
        "source_page_url": candidate.source_page_url or candidate.image_url,
        "download_url": candidate.image_url,
        "reference_tag": candidate.reference_tag,
        "search_query": candidate.query,
        "selection_method": "model-selected",
        "license_status": "unverified",
        "attribution_required": None,
        "reported_width": candidate.reported_width,
        "reported_height": candidate.reported_height,
        "width": width,
        "height": height,
        "shape": candidate.shape,
        "clarity": candidate.clarity,
        "category": candidate.category,
        "watermark": candidate.watermark,
        "description": candidate.description,
        "style_type": candidate.style_type,
    }
    search["status"] = "sourced"
    search["selected_reference_tag"] = candidate.reference_tag
    search.pop("last_error", None)
    _write_json_atomic(manifest_path, manifest)
    return {
        "ok": True,
        "manifest": str(manifest_path),
        "slide": candidate.slide,
        "output_path": str(search["output_path"]),
        "width": width,
        "height": height,
        "license_status": "unverified",
    }


def mark_search(
    manifest_path: Path,
    *,
    slide: int,
    status: str,
    reason: str,
) -> dict[str, object]:
    if status not in TERMINAL_SEARCH_STATUSES:
        raise ValueError("status must be exhausted or unavailable")
    manifest_path = manifest_path.resolve()
    manifest, image_plan, _artifact_root_path = _read_manifest(manifest_path)
    entry = _web_entry(image_plan, slide)
    search = entry["search"]
    search["status"] = status
    search["fallback"] = "generate"
    search["last_error"] = _clean_text(reason)[:1000] or status
    _write_json_atomic(manifest_path, manifest)
    return {
        "ok": True,
        "manifest": str(manifest_path),
        "slide": slide,
        "status": status,
        "next": "generate the existing fallback image job",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Localize a selected web_search image result for a PPT image plan."
    )
    parser.add_argument("manifest", type=Path)
    subparsers = parser.add_subparsers(dest="action", required=True)

    import_parser = subparsers.add_parser("import", help="download one selection")
    import_parser.add_argument("candidate", type=Path)
    import_parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT_SECONDS)

    mark_parser = subparsers.add_parser("mark", help="record terminal search status")
    mark_parser.add_argument("--slide", type=int, required=True)
    mark_parser.add_argument(
        "--status",
        choices=sorted(TERMINAL_SEARCH_STATUSES),
        required=True,
    )
    mark_parser.add_argument("--reason", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.action == "import":
            if args.timeout < 1 or args.timeout > 120:
                parser.error("--timeout must be between 1 and 120 seconds")
            result = import_candidate(
                args.manifest,
                args.candidate,
                timeout=args.timeout,
            )
        else:
            result = mark_search(
                args.manifest,
                slide=args.slide,
                status=args.status,
                reason=args.reason,
            )
    except (OSError, ValueError, json.JSONDecodeError, requests.RequestException) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
