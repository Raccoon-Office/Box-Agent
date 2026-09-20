"""Cua screenshot normalization for the transient model-input channel.

PIL is imported lazily. Invalid or unsupported images are skipped without
failing the desktop action that produced them.
"""

from __future__ import annotations

import base64
import io
from typing import Any

# Bound the input size of large desktop captures. Model-input token budgeting
# remains the responsibility of the existing transient follow-up channel.
_MAX_LONG_EDGE_PX = 1568
_JPEG_QUALITY = 85
_SUPPORTED_MIME = frozenset({"image/png", "image/jpeg"})

__all__ = [
    "_MAX_LONG_EDGE_PX",
    "_JPEG_QUALITY",
    "resize_and_encode",
    "encode_canonical_cua_image",
]


def resize_and_encode(image: Any, mime_type: str) -> tuple[bytes, int, int]:
    """Downsample ``image`` to the long-edge cap and re-encode.

    ``image`` is an already-opened PIL image. Requires PIL to be importable;
    callers that must not raise should guard the import themselves.
    """
    from PIL import Image

    long_edge = max(image.size)
    if long_edge > _MAX_LONG_EDGE_PX:
        scale = _MAX_LONG_EDGE_PX / long_edge
        size = (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        )
        image = image.resize(size, Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    if mime_type == "image/png":
        image.save(buffer, format="PNG", optimize=True)
    else:
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue(), image.width, image.height


def encode_canonical_cua_image(data_b64: str, mime_type: str) -> dict[str, Any] | None:
    """Normalize a base64 screenshot into a canonical ``input_image`` block.

    Accepts only PNG/JPEG. Decodes the bytes, honors EXIF orientation, and
    downsamples anything above the long-edge cap. Returns a canonical block
    carrying ``width``/``height`` (required for pixel-based token estimation),
    or ``None`` for any unsupported type, bad base64, or decode/encode failure.
    Never raises.
    """
    normalized_mime = (mime_type or "").strip().lower()
    if normalized_mime not in _SUPPORTED_MIME:
        return None
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:
        return None
    if not raw:
        return None
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            image_format = (opened.format or "").upper()
            if image_format not in {"PNG", "JPEG"}:
                return None
            # Trust the decoded format over the declared mime type.
            expected_mime = "image/png" if image_format == "PNG" else "image/jpeg"
            orientation = opened.getexif().get(274, 1)
            opened.load()
            normalized = ImageOps.exif_transpose(opened)
            if max(normalized.size) <= _MAX_LONG_EDGE_PX and orientation == 1:
                encoded = raw
                width, height = normalized.size
            else:
                encoded, width, height = resize_and_encode(normalized, expected_mime)
    except Exception:
        return None
    return {
        "type": "input_image",
        "media_type": expected_mime,
        "data": base64.b64encode(encoded).decode("ascii"),
        "width": width,
        "height": height,
    }
