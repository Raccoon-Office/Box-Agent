#!/usr/bin/env python3
"""Download one selected image into an explicitly supplied PPT deck directory."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from env_config import load_ppt_env


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
ENV_LOAD = load_ppt_env()


def _emit(status: str, **payload: Any) -> None:
    print(json.dumps({"status": status, "provider": "bundled", **payload}, ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download an image into a PPT deck.")
    parser.add_argument("url", help="Direct image URL.")
    parser.add_argument("--deck-dir", required=True, help="Absolute existing deck directory.")
    parser.add_argument(
        "--output",
        required=True,
        help="Destination path relative to deck-dir, for example assets/hero.png.",
    )
    parser.add_argument("--referer", default="", help="Optional source-page Referer.")
    return parser


def _destination(deck_dir: str, relative_output: str) -> tuple[Path, Path]:
    deck = Path(deck_dir).expanduser()
    if not deck.is_absolute() or not deck.is_dir():
        raise ValueError("deck-dir must be an absolute existing directory")
    relative = Path(relative_output)
    if relative.is_absolute():
        raise ValueError("output must be relative to deck-dir")
    destination = (deck / relative).resolve()
    try:
        destination.relative_to(deck.resolve())
    except ValueError as exc:
        raise ValueError("output escapes deck-dir") from exc
    if destination == deck.resolve():
        raise ValueError("output must name a file inside deck-dir")
    return deck.resolve(), destination


def _looks_like_image(data: bytes, content_type: str) -> bool:
    head = data[:1024].lstrip()
    binary_image = (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )
    svg_image = content_type.lower().startswith("image/svg") and b"<svg" in head.lower()
    return binary_image or svg_image


def main() -> int:
    _emit("unavailable", reason="offline_package")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
