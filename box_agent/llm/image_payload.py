"""Bound image-bearing request bodies without changing conversation state.

Encoding is deliberately finite: one high-quality JPEG candidate per image,
then smaller batches; only a single image can be resized down to a fixed floor.
"""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import io
import json
from typing import Any

JPEG_VIEW_QUALITY = 90
MIN_VIEW_LONG_EDGE = 1024
HOSTED_MAX_REQUEST_BODY_BYTES = 10_000_000
_MAX_DECODE_BYTES = 20 * 1024 * 1024
_MAX_DECODE_PIXELS = 40_000_000
_JPEG_SAFE_MODES = {"1", "L", "LA", "P", "PA", "RGB", "RGBA"}
_TRANSPORT_KEYS = {"extra_body", "extra_headers", "extra_query", "timeout"}


class RequestBodyTooLargeError(ValueError):
    """Local preflight rejection: no request or partial model response was sent."""

    status_code = 413

    def __init__(self, *, body_bytes: int, budget_bytes: int, image_count: int,
                 non_image_bytes: int = 0):
        self.body_bytes = body_bytes
        self.budget_bytes = budget_bytes
        self.image_count = image_count
        self.non_image_bytes = non_image_bytes
        self.can_retry_images = image_count > 0 and non_image_bytes < budget_bytes
        if not self.can_retry_images:
            action = "reduce non-image request content before retrying"
        elif image_count > 1:
            action = "retry with fewer images in separate model requests"
        else:
            action = "inspect a relevant high-resolution crop; a detail-preserving image copy could not fit"
        self.guidance = action
        super().__init__(f"REQUEST_BODY_TOO_LARGE: {body_bytes} > {budget_bytes} bytes; {action}. "
                         "This request was not sent and its images have not been inspected.")

    def details(self) -> dict[str, Any]:
        return {"code": "REQUEST_BODY_TOO_LARGE", "body_bytes": self.body_bytes,
                "budget_bytes": self.budget_bytes, "image_count": self.image_count,
                "non_image_bytes": self.non_image_bytes, "request_sent": False,
                "guidance": self.guidance}


def encode_view_image(image: Any, media_type: str, *, max_long_edge: int,
                      quality: int = JPEG_VIEW_QUALITY) -> tuple[bytes, int, int]:
    """Encode an in-memory viewing copy, preserving alpha for PNG and ICC color."""
    from PIL import Image

    if max(image.size) > max_long_edge:
        scale = max_long_edge / max(image.size)
        image = image.resize((max(1, round(image.width * scale)),
                              max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    options = {"icc_profile": image.info["icc_profile"]} if image.info.get("icc_profile") else {}
    if media_type == "image/png":
        image.save(output, format="PNG", optimize=True, **options)
    else:
        if image.mode not in _JPEG_SAFE_MODES:
            # CMYK needs color-managed conversion, and I/I;16/F need an
            # explicit bit-depth mapping. Naive RGB conversion corrupts them.
            raise ValueError(f"No safe JPEG conversion for image mode {image.mode}")
        if image.mode not in {"RGB", "L"}:
            image = image.convert("L" if image.mode in {"1", "LA"} else "RGB")
        image.save(output, format="JPEG", quality=quality, subsampling=0, **options)
    return output.getvalue(), image.width, image.height


def request_body_bytes(params: dict[str, Any]) -> int:
    """Match SDK extra_body merge and httpx UTF-8 JSON serialization."""
    body = {key: value for key, value in params.items() if key not in _TRANSPORT_KEYS}
    body.update(params.get("extra_body") or {})
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _images(params: dict[str, Any]) -> list[dict[str, Any]]:
    messages = (params.get("extra_body") or {}).get("messages", params.get("messages", []))
    return [block["image_url"] for message in messages
            if isinstance(message.get("content"), list) for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "image_url"
            and isinstance(block.get("image_url"), dict)]


def _decode(url: str) -> Any | None:
    from PIL import Image, ImageOps

    if not isinstance(url, str) or not url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
        return None
    data = url.split(",", 1)[1]
    if len(data) > 4 * ((_MAX_DECODE_BYTES + 2) // 3):
        return None
    try:
        raw = base64.b64decode(data, validate=True)
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.width * opened.height > _MAX_DECODE_PIXELS:
                return None
            opened.load()
            return ImageOps.exif_transpose(opened)
    except (ValueError, OSError, binascii.Error, Image.DecompressionBombError):
        return None


def _has_transparency(image: Any) -> bool:
    return (("A" in image.getbands() or "transparency" in image.info)
            and image.convert("RGBA").getchannel("A").getextrema()[0] < 255)


def prepare_image_request(params: dict[str, Any], max_body_bytes: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a sendable copy, or a rejection for the caller to recover explicitly."""
    budget = max_body_bytes * 95 // 100
    original_size = request_body_bytes(params)
    stats: dict[str, Any] = {"original_body_bytes": original_size, "body_bytes": original_size,
                             "max_body_bytes": max_body_bytes, "budget_bytes": budget,
                             "image_count": len(_images(params)), "transforms": []}
    if original_size <= budget:
        return params, stats

    prepared = deepcopy(params)
    images = _images(prepared)
    original_urls = [image.get("url", "") for image in images]
    for image in images:
        image["url"] = ""
    non_image_bytes = request_body_bytes(prepared)
    for image, url in zip(images, original_urls, strict=True):
        image["url"] = url
    size = original_size

    def replace(index: int, decoded: Any, media_type: str, edge: int) -> bool:
        nonlocal size
        try:
            encoded, width, height = encode_view_image(decoded, media_type, max_long_edge=edge)
        except (OSError, ValueError):
            # An unencodable candidate must not replace the original or turn a
            # local size rejection into an endlessly retried transport error.
            return False
        candidate = f"data:{media_type};base64," + base64.b64encode(encoded).decode("ascii")
        previous = images[index].get("url", "")
        if len(candidate) >= len(previous):
            return False
        images[index]["url"] = candidate
        size = request_body_bytes(prepared)
        stats["body_bytes"] = size
        stats["transforms"].append({"image_index": index + 1, "media_type": media_type,
                                    "width": width, "height": height, "encoded_bytes": len(encoded),
                                    "quality": JPEG_VIEW_QUALITY if media_type == "image/jpeg" else None})
        return size <= budget

    if non_image_bytes < budget:
        # Largest first avoids re-encoding small images after enough space was freed.
        for index in sorted(range(len(images)), key=lambda i: len(original_urls[i]), reverse=True):
            decoded = _decode(original_urls[index])
            if decoded is not None and not _has_transparency(decoded):
                if replace(index, decoded, "image/jpeg", max(decoded.size)):
                    return prepared, stats

        # Multi-image requests retain resolution; task-level batching comes first.
        if len(images) == 1:
            decoded = _decode(original_urls[0])
            if decoded is not None:
                media_type = ("image/png" if _has_transparency(decoded) or decoded.mode not in _JPEG_SAFE_MODES
                              else "image/jpeg")
                edge = max(decoded.size)
                while edge > MIN_VIEW_LONG_EDGE:
                    edge = max(MIN_VIEW_LONG_EDGE, int(edge * .8))
                    if replace(0, decoded, media_type, edge):
                        return prepared, stats

    error = RequestBodyTooLargeError(body_bytes=size, budget_bytes=budget,
                                     image_count=len(images), non_image_bytes=non_image_bytes)
    error.metrics = stats
    raise error
