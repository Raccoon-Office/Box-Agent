#!/usr/bin/env python3
"""Resolve PPT image-plan web rows through free, openly licensed providers.

The script deliberately handles only the free tier.  A provider adapter returns
normalized candidates; future paid providers can join ``PROVIDERS`` without
changing the manifest contract.  When the free pool has no acceptable image,
the original ``decision: generate`` row remains pending for the existing image
generation path.
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
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps


USER_AGENT = "Box-Agent-PPT/1.0 (https://github.com/Raccoon-Office/Box-Agent)"
FREE_PROVIDER_CHAIN = ("openverse", "wikimedia")
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Candidate:
    provider: str
    title: str
    author: str
    source_page_url: str
    download_url: str
    license_name: str
    license_url: str
    width: int
    height: int


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _metadata_value(metadata: dict, key: str) -> str:
    raw = metadata.get(key) or {}
    value = raw.get("value", "") if isinstance(raw, dict) else raw
    return _clean_text(re.sub(r"<[^>]+>", " ", str(value or "")))


def _is_no_attribution_license(name: str, url: str = "") -> bool:
    text = f"{name} {url}".casefold()
    rejected = ("by-nc", "by-nd", "noncommercial", "no derivatives")
    if any(token in text for token in rejected):
        return False
    return any(
        token in text
        for token in (
            "cc0",
            "public domain",
            "publicdomain",
            "/publicdomain/",
            "pdm",
        )
    )


def search_openverse(query: str, timeout: int) -> list[Candidate]:
    response = requests.get(
        "https://api.openverse.org/v1/images/",
        params={
            "q": query,
            "page_size": 20,
            "license": "cc0,pdm",
            "size": "large",
        },
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    candidates: list[Candidate] = []
    for item in response.json().get("results", []) or []:
        license_name = _clean_text(item.get("license"))
        license_url = _clean_text(item.get("license_url"))
        download_url = _clean_text(item.get("url") or item.get("thumbnail"))
        source_page_url = _clean_text(
            item.get("foreign_landing_url") or item.get("detail_url")
        )
        if (
            not download_url
            or not source_page_url
            or not _is_no_attribution_license(license_name, license_url)
        ):
            continue
        candidates.append(
            Candidate(
                provider="openverse",
                title=_clean_text(item.get("title")) or "Untitled",
                author=_clean_text(item.get("creator")),
                source_page_url=source_page_url,
                download_url=download_url,
                license_name=license_name or "CC0/Public Domain",
                license_url=license_url,
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
            )
        )
    return candidates


def search_wikimedia(query: str, timeout: int) -> list[Candidate]:
    response = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrnamespace": "6",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrlimit": 20,
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata|mime",
            "iiurlwidth": 1024,
            "iiextmetadatafilter": "LicenseShortName|License|LicenseUrl|Artist",
        },
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    pages = (response.json().get("query") or {}).get("pages") or {}
    candidates: list[Candidate] = []
    for page in pages.values():
        info_rows = page.get("imageinfo") or []
        if not info_rows:
            continue
        info = info_rows[0]
        metadata = info.get("extmetadata") or {}
        license_name = _metadata_value(metadata, "LicenseShortName") or _metadata_value(
            metadata, "License"
        )
        license_url = _metadata_value(metadata, "LicenseUrl")
        download_url = _clean_text(info.get("url"))
        source_page_url = _clean_text(info.get("descriptionurl"))
        if (
            not download_url
            or not source_page_url
            or not _is_no_attribution_license(license_name, license_url)
        ):
            continue
        candidates.append(
            Candidate(
                provider="wikimedia",
                title=_clean_text(page.get("title")).removeprefix("File:") or "Untitled",
                author=_metadata_value(metadata, "Artist"),
                source_page_url=source_page_url,
                download_url=download_url,
                license_name=license_name or "CC0/Public Domain",
                license_url=license_url,
                width=int(info.get("width") or 0),
                height=int(info.get("height") or 0),
            )
        )
    return candidates


PROVIDERS: dict[str, Callable[[str, int], list[Candidate]]] = {
    "openverse": search_openverse,
    "wikimedia": search_wikimedia,
}


def _tokens(value: str) -> set[str]:
    normalized = value.casefold()
    tokens = set(re.findall(r"[a-z0-9]{3,}", normalized))
    for run in re.findall(r"[\u3400-\u9fff]{2,}", normalized):
        tokens.add(run)
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    noise = {
        "image",
        "photo",
        "visual",
        "background",
        "presentation",
        "slide",
        "主视觉",
        "照片",
        "摄影",
        "背景",
    }
    return {token for token in tokens if token not in noise}


def _candidate_score(candidate: Candidate, query: str, placement: str) -> float:
    query_tokens = _tokens(query)
    metadata_tokens = _tokens(
        " ".join((candidate.title, candidate.author, candidate.source_page_url))
    )
    if query_tokens:
        overlap = len(query_tokens & metadata_tokens) / len(query_tokens)
        if overlap == 0:
            return float("-inf")
    else:
        overlap = 1.0
    score = overlap * 10_000
    if candidate.width > candidate.height and placement == "full-slide":
        score += 1_000
    score += min(max(candidate.width, 0) * max(candidate.height, 0) / 1_000, 1_500)
    return score


def _safe_https_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("provider image URL must be absolute HTTPS")
    return value


def _download_and_normalize(
    candidate: Candidate,
    target_path: Path,
    *,
    placement: str,
    timeout: int,
) -> tuple[int, int]:
    response = requests.get(
        _safe_https_url(candidate.download_url),
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        stream=True,
    )
    response.raise_for_status()
    _safe_https_url(str(response.url))
    declared_length = int(response.headers.get("Content-Length") or 0)
    if declared_length > MAX_DOWNLOAD_BYTES:
        raise ValueError("provider image exceeds the 25 MiB download limit")

    payload = bytearray()
    for chunk in response.iter_content(64 * 1024):
        payload.extend(chunk)
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise ValueError("provider image exceeds the 25 MiB download limit")

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
                    f"downloaded image {width}x{height} is below {min_width}x{min_height}"
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
        raise ValueError("image output path must be artifact-root-relative")
    resolved = (artifact_root / path_value).resolve()
    if resolved != artifact_root and artifact_root not in resolved.parents:
        raise ValueError("image output path escapes the artifact root")
    return resolved


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _query_overrides(values: Iterable[str]) -> dict[int, str]:
    overrides: dict[int, str] = {}
    for value in values:
        slide_text, separator, query = value.partition("=")
        if not separator or not slide_text.isdigit() or not query.strip():
            raise ValueError("--query must use SLIDE=SEARCH TERMS")
        overrides[int(slide_text)] = query.strip()
    return overrides


def process_manifest(
    manifest_path: Path,
    *,
    provider_names: Iterable[str] = FREE_PROVIDER_CHAIN,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    query_overrides: dict[int, str] | None = None,
    retry: bool = False,
) -> dict[str, object]:
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image_plan = manifest.get("image_plan") if isinstance(manifest, dict) else None
    if not isinstance(image_plan, list):
        raise ValueError("manifest.image_plan must be an array")
    artifact_root = _artifact_root(manifest_path)
    selected_providers = tuple(provider_names)
    unknown = [name for name in selected_providers if name not in PROVIDERS]
    if unknown:
        raise ValueError(f"unknown free image provider(s): {', '.join(unknown)}")

    sourced = 0
    exhausted = 0
    unavailable = 0
    skipped = 0
    overrides = query_overrides or {}
    for entry in image_plan:
        if not isinstance(entry, dict) or entry.get("acquire_via") != "web":
            skipped += 1
            continue
        if entry.get("decision") != "generate" or entry.get("resolved_via") == "web":
            skipped += 1
            continue
        search = entry.get("search")
        if not isinstance(search, dict) or search.get("tier") != "free":
            raise ValueError(
                f"slide {entry.get('slide', '?')}: web acquisition requires search.tier=free"
            )
        slide_number = int(entry.get("slide") or 0)
        has_query_override = slide_number in overrides
        if (
            search.get("status") in {"exhausted", "unavailable"}
            and not retry
            and not has_query_override
        ):
            skipped += 1
            continue
        query = overrides.get(slide_number, _clean_text(search.get("query")))
        if not query:
            raise ValueError(f"slide {slide_number or '?'}: free image query is empty")
        search["query"] = query
        search["providers"] = list(selected_providers)
        search["attempted_providers"] = []

        candidates: list[Candidate] = []
        provider_errors: list[str] = []
        for provider_name in selected_providers:
            search["attempted_providers"].append(provider_name)
            try:
                candidates.extend(PROVIDERS[provider_name](query, timeout))
            except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
                provider_errors.append(f"{provider_name}: {exc}")

        placement = str(entry.get("placement") or "fixed-frame")
        ranked = sorted(
            (
                (_candidate_score(candidate, query, placement), candidate)
                for candidate in candidates
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        ranked = [item for item in ranked if item[0] != float("-inf")]
        target_path = _resolve_artifact_path(
            artifact_root, str(search.get("output_path") or "")
        )

        selected: tuple[Candidate, tuple[int, int]] | None = None
        candidate_errors: list[str] = []
        for _score, candidate in ranked:
            try:
                dimensions = _download_and_normalize(
                    candidate,
                    target_path,
                    placement=placement,
                    timeout=timeout,
                )
            except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
                candidate_errors.append(f"{candidate.provider}/{candidate.title}: {exc}")
                continue
            selected = candidate, dimensions
            break

        if selected is None:
            search["status"] = "unavailable" if provider_errors else "exhausted"
            search["fallback"] = "generate"
            errors = provider_errors + candidate_errors
            if errors:
                search["last_error"] = "; ".join(errors)[:1000]
            else:
                search["last_error"] = "no relevant no-attribution candidate"
            if search["status"] == "unavailable":
                unavailable += 1
            else:
                exhausted += 1
            _write_json_atomic(manifest_path, manifest)
            continue

        candidate, (width, height) = selected
        entry["decision"] = "use_existing"
        entry["status"] = "ready"
        entry["resolved_via"] = "web"
        entry["origin"] = "sourced"
        entry["output_path"] = str(search["output_path"])
        entry["asset_hash"] = hashlib.sha256(target_path.read_bytes()).hexdigest()
        entry["source"] = {
            "provider": candidate.provider,
            "title": candidate.title,
            "author": candidate.author,
            "source_page_url": candidate.source_page_url,
            "download_url": candidate.download_url,
            "license_name": candidate.license_name,
            "license_url": candidate.license_url,
            "license_tier": "no-attribution",
            "attribution_required": False,
            "selection_method": "metadata-ranked",
            "search_query": query,
            "width": width,
            "height": height,
        }
        search["status"] = "sourced"
        search.pop("last_error", None)
        sourced += 1
        _write_json_atomic(manifest_path, manifest)

    return {
        "ok": True,
        "manifest": str(manifest_path),
        "providers": list(selected_providers),
        "sourced": sourced,
        "exhausted": exhausted,
        "unavailable": unavailable,
        "skipped": skipped,
        "next": "generate remaining decision=generate rows only after free search is exhausted or unavailable",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search free no-attribution image providers for PPT image-plan web rows."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--provider",
        action="append",
        choices=sorted(PROVIDERS),
        default=[],
        help="Free provider to use; repeat to set order. Defaults to Openverse then Wikimedia.",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        metavar="SLIDE=TERMS",
        help="Override and persist one slide's concise search query.",
    )
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT_SECONDS)
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Retry terminal exhausted/unavailable rows without changing their query.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.timeout < 1 or args.timeout > 120:
        parser.error("--timeout must be between 1 and 120 seconds")
    try:
        result = process_manifest(
            args.manifest,
            provider_names=args.provider or FREE_PROVIDER_CHAIN,
            timeout=args.timeout,
            query_overrides=_query_overrides(args.query),
            retry=args.retry,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
