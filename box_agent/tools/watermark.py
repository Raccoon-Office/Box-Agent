"""Text watermark rendering for generated bitmap images.

This module adds a white footer (default ``AI 生成``) and AIGC metadata to a
generated image before it is written to disk. It is shared by both the
text-to-image and image-to-image paths of :class:`GenerateImageTool`, so a
single call site covers CLI and ACP runtimes alike.

Design constraints:
- Pillow is an optional (``runtime`` extra) dependency, so it is imported
  lazily and any failure degrades gracefully — the original bytes are returned
  unchanged and the caller is told the watermark was skipped. Generating an
  image must never fail because watermarking failed.
- All visual parameters are hard-coded module constants; there is no config.
- Vector (SVG) and animated (GIF) formats are skipped.
"""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL import ImageFont


_DEFAULT_TEXT = "AI 生成"
_OPACITY = 1.0  # watermark fill alpha, 0.0–1.0
_COLOR = "#666666"  # watermark text color (hex)
_FONT_SIZE_RATIO = 0.022  # font size = short_edge * ratio
_MIN_FONT_SIZE = 12
_MARGIN_RATIO = 0.02  # margin from the corner = short_edge * ratio

# Only true bitmaps are rasterizable by Pillow. SVG is vector, GIF is animated.
_RASTERIZABLE_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp"}

# Bundled CJK font so non-Latin (e.g. Chinese) custom watermark text renders
# consistently across macOS/Windows/Linux and inside the frozen runtime. The
# canvas-fonts directory ships with the package (pyproject ``skills/**/*`` and
# build_runtime ``--add-data box_agent/skills``).
_CANVAS_FONTS_DIR = (
    Path(__file__).resolve().parent.parent / "skills" / "canvas-design" / "canvas-fonts"
)
_BUNDLED_FONT = _CANVAS_FONTS_DIR / "NotoSansSC-Regular.otf"

# Platform system-font fallbacks (only consulted if the bundled font is missing).
_SYSTEM_FONT_CANDIDATES = (
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/arial.ttf",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def apply_text_watermark(
    image_bytes: bytes,
    mime_type: str,
    text: str | None = None,
    *,
    visible: bool = True,
) -> tuple[bytes, dict[str, Any]]:
    """Add a footer outside the original pixels and embed AIGC metadata.

    Args:
        image_bytes: Raw encoded image bytes.
        mime_type: MIME type of ``image_bytes`` (e.g. ``image/png``).
        text: Optional additional text; the ``AI 生成`` label is always retained.
        visible: False for intermediate assets that only need embedded metadata.

    Returns:
        ``(out_bytes, status)`` where ``status`` is
        visible/metadata application flags, actual output dimensions on success,
        a reason and font information. On any
        problem (unsupported MIME, Pillow missing, decode/encode error) the
        original ``image_bytes`` are returned with ``applied=False`` — this
        function never raises.
    """
    custom_text = (text or "").strip()
    watermark_text = _DEFAULT_TEXT
    if custom_text and custom_text != _DEFAULT_TEXT:
        watermark_text += " · " + custom_text

    if mime_type not in _RASTERIZABLE_MIME:
        return image_bytes, {
            "applied": False,
            "metadata_applied": False,
            "reason": f"unsupported mime type for watermark: {mime_type}",
            "font": "",
        }

    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        return image_bytes, {
            "applied": False,
            "metadata_applied": False,
            "reason": "Pillow is not installed; watermark skipped",
            "font": "",
        }

    decoded = False
    try:
        source = Image.open(io.BytesIO(image_bytes))
        if getattr(source, "is_animated", False):
            return image_bytes, {
                "applied": False, "metadata_applied": False,
                "reason": "animated images are not supported", "font": "",
            }
        source.load()
        decoded = True
        source = ImageOps.exif_transpose(source)
        base = source.convert("RGBA")
        short_edge = min(base.size)
        font_size = max(_MIN_FONT_SIZE, int(short_edge * _FONT_SIZE_RATIO))
        font, font_name = _resolve_font(font_size)
        margin = max(1, int(short_edge * _MARGIN_RATIO))
        composited = base
        if visible:
            draw = ImageDraw.Draw(base)
            bbox = draw.textbbox((0, 0), watermark_text, font=font)
            if custom_text and bbox[2] - bbox[0] + 2 * margin > base.width:
                # A long optional brand must not make the mandatory AI label tiny.
                watermark_text = _DEFAULT_TEXT
                bbox = draw.textbbox((0, 0), watermark_text, font=font)
            # Render to a separate strip so even narrow images cannot clip into
            # the source. Fit the strip for unusually narrow provider responses.
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            strip = Image.new("RGBA", (text_w + 2 * margin, text_h + 2 * margin), "white")
            ImageDraw.Draw(strip).text(
                (margin - bbox[0], margin - bbox[1]), watermark_text,
                font=font, fill=_hex_to_rgba(_COLOR, _OPACITY),
            )
            if strip.width > base.width:
                strip = strip.resize((base.width, max(1, round(strip.height * base.width / strip.width))))
            composited = Image.new("RGBA", (base.width, base.height + strip.height), "white")
            composited.paste(base, (0, 0))  # preserve alpha, including transparent pixels
            composited.paste(strip, (base.width - strip.width, base.height))

        xmp, aigc = _aigc_metadata(source.info)
        out_bytes = _encode_target(composited, mime_type, Image, source.info, xmp, aigc)
        return out_bytes, {
            "applied": visible, "metadata_applied": True,
            "reason": None if visible else "disabled for intermediate asset",
            "font": font_name, "width": composited.width, "height": composited.height,
        }
    except Exception as exc:  # pragma: no cover - defensive: never fail a render
        return image_bytes, {
            "applied": False,
            "metadata_applied": False,
            "reason": f"watermark error: {exc}",
            "labeling_failed": decoded,
            "font": "",
        }


def _resolve_font(size: int) -> tuple["ImageFont.FreeTypeFont | ImageFont.ImageFont", str]:
    """Resolve a font with a three-level fallback chain.

    1. Bundled CJK font (primary; guarantees consistent cross-platform + frozen).
    2. System font probing (best-effort enhancement).
    3. ``ImageFont.load_default(size=...)`` (Pillow 10+); never fails.

    Returns ``(font, font_name)`` where ``font_name`` identifies the source for
    diagnostics ("load_default" means non-Latin glyphs may render as tofu).
    """
    from PIL import ImageFont

    if _BUNDLED_FONT.exists():
        try:
            return ImageFont.truetype(str(_BUNDLED_FONT), size), _BUNDLED_FONT.name
        except OSError:
            pass

    for candidate in _SYSTEM_FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size), Path(candidate).name
            except OSError:
                continue

    try:
        return ImageFont.load_default(size=size), "load_default"
    except TypeError:  # pragma: no cover - very old Pillow without size kwarg
        return ImageFont.load_default(), "load_default"


def _text_size(draw: Any, text: str, font: Any) -> tuple[int, int]:
    """Measure rendered text using ``textbbox`` (Pillow 10+ compatible)."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _anchor_xy(
    img_size: tuple[int, int],
    text_size: tuple[int, int],
    margin: int,
) -> tuple[int, int]:
    """Bottom-right anchor coordinates for the text box."""
    img_w, img_h = img_size
    text_w, text_h = text_size
    # textbbox of multi-line/descender text can exceed the nominal box; pad a
    # little extra on the bottom so descenders are not clipped at the edge.
    x = max(margin, img_w - text_w - margin)
    y = max(margin, img_h - text_h - margin - int(text_h * 0.3))
    return x, y


def _hex_to_rgba(color: str, opacity: float) -> tuple[int, int, int, int]:
    """Parse a ``#RRGGBB`` color into RGBA; fall back to white on error."""
    alpha = int(max(0.0, min(1.0, opacity)) * 255)
    value = (color or "").strip().lstrip("#")
    try:
        if len(value) == 3:
            value = "".join(ch * 2 for ch in value)
        r = int(value[0:2], 16)
        g = int(value[2:4], 16)
        b = int(value[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 255, 255, 255
    return r, g, b, alpha


def _contrast_stroke(fill: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Return a stroke color that contrasts the fill (dark text→light stroke)."""
    r, g, b, a = fill
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    stroke_rgb = (0, 0, 0) if luminance > 140 else (255, 255, 255)
    return (*stroke_rgb, a)


# Same service code as raccoon-lab's existing export metadata implementation.
_SERVICE_CODE = "00191310104MAC2G6EG4100000003"
_TC260 = "http://www.tc260.org.cn/ns/AIGC/1.0/"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XMP_KEY = "XML:com.adobe.xmp"


def _aigc_metadata(info: dict[str, Any]) -> tuple[bytes, str]:
    """Update only AIGC in XMP; keep upstream provenance and other properties."""
    raw = info.get("xmp") or info.get(_XMP_KEY)
    root = ET.fromstring(raw) if raw else ET.Element("{adobe:ns:meta/}xmpmeta")
    rdf = root.find(f".//{{{_RDF}}}RDF")
    if rdf is None:
        rdf = ET.SubElement(root, f"{{{_RDF}}}RDF")
    description = rdf.find(f"{{{_RDF}}}Description")
    if description is None:
        description = ET.SubElement(rdf, f"{{{_RDF}}}Description", {f"{{{_RDF}}}about": ""})
    # Both namespace spellings occur in the TC260 guide and existing producers.
    tags = {f"{{{ns}}}AIGC" for ns in (_TC260, _TC260.rstrip("/"))}
    previous = info.get("AIGC")
    for element in root.iter():
        for key in list(element.attrib):
            if key in tags:
                previous = element.attrib.pop(key)
        for child in list(element):
            if child.tag in tags:
                previous = child.text
                element.remove(child)
    existing: dict[str, Any] = {}
    if previous:
        try:
            parsed = json.loads(previous)
            if isinstance(parsed, dict):
                existing = parsed.get("AIGC", parsed)
                if not isinstance(existing, dict):
                    existing = {}
        except (ValueError, TypeError):
            pass
    content_id = uuid4().hex
    metadata = {
        "Label": "1", "ContentProducer": _SERVICE_CODE,
        "ProduceID": content_id, "ReservedCode1": "",
        "ContentPropagator": _SERVICE_CODE, "PropagateID": content_id,
        "ReservedCode2": "",
    }
    if all(isinstance(existing.get(key), str) and existing[key]
           for key in ("ContentProducer", "ProduceID")):
        for key in ("ContentProducer", "ProduceID", "ReservedCode1"):
            if isinstance(existing.get(key), str):
                metadata[key] = existing[key]
    aigc = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))
    ET.SubElement(description, f"{{{_TC260}}}AIGC").text = aigc
    return ET.tostring(root, encoding="utf-8"), aigc


def _encode_target(
    image: Any, mime_type: str, image_module: Any,
    original_info: dict[str, Any], xmp: bytes, aigc: str,
) -> bytes:
    """Write labels during the final encode, preserving EXIF/ICC and PNG text."""
    buffer = io.BytesIO()
    normalized = mime_type.lower()
    options = {key: original_info[key] for key in ("exif", "icc_profile") if original_info.get(key)}
    if normalized in ("image/jpeg", "image/jpg"):
        background = image_module.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        background.save(buffer, format="JPEG", quality=95, subsampling=0, **options)
        # Insert APP1 directly for compatibility with Pillow 10 (no JPEG xmp save).
        packet = b"http://ns.adobe.com/xap/1.0/\0" + xmp
        if len(packet) + 2 > 65535:
            raise ValueError("XMP exceeds JPEG APP1 capacity")
        data = buffer.getvalue()
        return data[:2] + b"\xff\xe1" + (len(packet) + 2).to_bytes(2, "big") + packet + data[2:]
    if normalized == "image/webp":
        image.save(buffer, format="WEBP", lossless=True, xmp=xmp, **options)
    else:
        from PIL.PngImagePlugin import PngInfo

        pnginfo = PngInfo()
        for key, value in original_info.items():
            if isinstance(value, str) and key not in ("AIGC", _XMP_KEY):
                pnginfo.add_itxt(key, value)
        pnginfo.add_itxt(_XMP_KEY, xmp.decode("utf-8"))
        # Retain the legacy PNG reader contract in addition to standard XMP.
        pnginfo.add_text("AIGC", aigc)
        image.save(buffer, format="PNG", pnginfo=pnginfo, **options)
    return buffer.getvalue()
