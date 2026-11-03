"""Unit tests for the text watermark renderer."""

import builtins
import io
import json
import xml.etree.ElementTree as ET

import pytest
from PIL import Image, ImageChops, PngImagePlugin

from box_agent.tools import watermark
from box_agent.tools.watermark import apply_text_watermark


def _make_png(size=(640, 480), color=(30, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _make_jpeg(size=(400, 300), color=(220, 220, 220)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _make_webp(size=(300, 300), color=(200, 50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "WEBP")
    return buf.getvalue()


def test_watermark_footer_preserves_original_pixels() -> None:
    original = _make_png()
    out, status = apply_text_watermark(original, "image/png")

    assert status["applied"] is True
    assert status["reason"] is None
    assert out != original
    # Source area is pixel-identical, with a white footer below it.
    reopened = Image.open(io.BytesIO(out))
    assert reopened.width == 640
    assert reopened.height > 480
    assert reopened.crop((0, 0, 640, 480)).convert("RGB").tobytes() == Image.open(io.BytesIO(original)).tobytes()
    footer = reopened.crop((0, 480, 640, reopened.height)).convert("RGB")
    assert footer.getpixel((0, 0)) == (255, 255, 255)
    assert ImageChops.difference(footer, Image.new("RGB", footer.size, "white")).getbbox()
    assert status["metadata_applied"] is True


def test_watermark_custom_text_applies() -> None:
    original = _make_png()
    out, status = apply_text_watermark(original, "image/png", "品牌名 2026")

    assert status["applied"] is True
    assert out != original


def test_watermark_skips_svg() -> None:
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    out, status = apply_text_watermark(svg, "image/svg+xml")

    assert status["applied"] is False
    assert "unsupported mime" in status["reason"]
    assert out == svg


def test_watermark_skips_gif() -> None:
    out, status = apply_text_watermark(b"GIF89a-bytes", "image/gif")

    assert status["applied"] is False
    assert out == b"GIF89a-bytes"


def test_watermark_jpeg_output_has_no_alpha() -> None:
    original = _make_jpeg()
    out, status = apply_text_watermark(original, "image/jpeg")

    assert status["applied"] is True
    reopened = Image.open(io.BytesIO(out))
    assert reopened.mode == "RGB"  # flattened, no alpha


def test_watermark_webp_applies() -> None:
    original = _make_webp()
    out, status = apply_text_watermark(original, "image/webp")

    assert status["applied"] is True
    assert out != original


def test_watermark_corrupt_bytes_degrades_gracefully() -> None:
    out, status = apply_text_watermark(b"not an image", "image/png")

    assert status["applied"] is False
    assert "watermark error" in status["reason"]
    assert out == b"not an image"


def test_watermark_missing_pillow_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"\x89PNG fake"
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out, status = apply_text_watermark(original, "image/png")

    assert status["applied"] is False
    assert "Pillow" in status["reason"]
    assert out == original


def test_resolve_font_uses_bundled_cjk_font() -> None:
    # The bundled Noto Sans SC font ships with the package.
    assert watermark._BUNDLED_FONT.exists()
    font, name = watermark._resolve_font(40)
    assert name == watermark._BUNDLED_FONT.name


def test_resolve_font_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Bundled font missing and no system candidate exists → load_default, no crash.
    monkeypatch.setattr(watermark, "_BUNDLED_FONT", tmp_path / "missing-font.otf")
    monkeypatch.setattr(watermark, "_SYSTEM_FONT_CANDIDATES", ())

    font, name = watermark._resolve_font(40)
    assert name == "load_default"
    assert font is not None


def test_hex_to_rgba_parses_and_clamps() -> None:
    assert watermark._hex_to_rgba("#FFFFFF", 0.5) == (255, 255, 255, 127)
    assert watermark._hex_to_rgba("#000000", 1.0) == (0, 0, 0, 255)
    # invalid hex → white fallback
    assert watermark._hex_to_rgba("not-a-color", 1.0) == (255, 255, 255, 255)
    # opacity clamped to [0, 1]
    assert watermark._hex_to_rgba("#FFFFFF", 5.0)[3] == 255
    assert watermark._hex_to_rgba("#FFFFFF", -1.0)[3] == 0


def test_contrast_stroke_picks_opposite() -> None:
    # light fill → dark stroke
    assert watermark._contrast_stroke((255, 255, 255, 200))[:3] == (0, 0, 0)
    # dark fill → light stroke
    assert watermark._contrast_stroke((0, 0, 0, 200))[:3] == (255, 255, 255)


def _read_aigc(image):
    xmp = image.info.get("xmp") or image.info["XML:com.adobe.xmp"]
    root = ET.fromstring(xmp)
    element = root.find(".//{http://www.tc260.org.cn/ns/AIGC/1.0/}AIGC")
    assert element is not None
    return json.loads(element.text), root


@pytest.mark.parametrize("mime,make", [
    ("image/png", _make_png), ("image/jpeg", _make_jpeg), ("image/webp", _make_webp),
])
def test_saved_formats_have_readable_aigc_metadata(mime, make):
    out, status = apply_text_watermark(make(), mime)
    assert status["metadata_applied"]
    reopened = Image.open(io.BytesIO(out))
    metadata, _ = _read_aigc(reopened)
    assert metadata["Label"] == "1"
    assert metadata["ContentProducer"] == "00191310104MAC2G6EG4100000003"
    assert metadata["ContentPropagator"] == metadata["ContentProducer"]
    assert metadata["ProduceID"] and metadata["PropagateID"]
    assert set(metadata) == {"Label", "ContentProducer", "ProduceID", "ReservedCode1",
                             "ContentPropagator", "PropagateID", "ReservedCode2"}
    if mime == "image/png":
        assert json.loads(reopened.info["AIGC"]) == metadata


@pytest.mark.parametrize("visible", [True, False])
def test_labels_keep_alpha_and_upstream_metadata(visible):
    original = Image.new("RGBA", (160, 100), (20, 80, 160, 40))
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("AIGC", json.dumps({"ContentProducer": "upstream", "ProduceID": "original-id"}))
    pnginfo.add_itxt("Title", "原图标题")
    data = io.BytesIO()
    original.save(data, "PNG", pnginfo=pnginfo)
    out, status = apply_text_watermark(data.getvalue(), "image/png", visible=visible)
    assert status["metadata_applied"]
    reopened = Image.open(io.BytesIO(out))
    assert reopened.crop((0, 0, 160, 100)).tobytes() == original.tobytes()
    assert reopened.info["Title"] == "原图标题"
    metadata, _ = _read_aigc(reopened)
    assert metadata["ContentProducer"] == "upstream"
    assert metadata["ProduceID"] == "original-id"
    assert metadata["ContentPropagator"] == "00191310104MAC2G6EG4100000003"
    if not visible:
        assert reopened.size == original.size
        assert not status["applied"]


@pytest.mark.parametrize("attribute", [True, False])
def test_xmp_keeps_other_properties_and_upstream_provenance(attribute):
    root = ET.Element("{adobe:ns:meta/}xmpmeta")
    rdf = ET.SubElement(root, "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF")
    desc = ET.SubElement(rdf, "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    desc.set("{http://www.tc260.org.cn/ns/AIGC/1.0/}Other", "keep-me")
    key = "{http://www.tc260.org.cn/ns/AIGC/1.0}AIGC"
    value = json.dumps({"ContentProducer": "original", "ProduceID": "123", "ReservedCode1": "proof"})
    if attribute:
        desc.set(key, value)
    else:
        ET.SubElement(desc, key).text = value
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_itxt("XML:com.adobe.xmp", ET.tostring(root).decode())
    data = io.BytesIO()
    Image.new("RGB", (100, 100)).save(data, "PNG", pnginfo=pnginfo)
    out, status = apply_text_watermark(data.getvalue(), "image/png")
    assert status["metadata_applied"]
    metadata, parsed = _read_aigc(Image.open(io.BytesIO(out)))
    assert metadata["ContentProducer"] == "original"
    assert metadata["ProduceID"] == "123"
    assert metadata["ReservedCode1"] == "proof"
    assert any(e.get("{http://www.tc260.org.cn/ns/AIGC/1.0/}Other") == "keep-me" for e in parsed.iter())
    assert sum(e.tag.endswith("}AIGC") for e in parsed.iter()) == 1


def test_tiny_image_and_long_custom_text_stay_below_source():
    original = _make_png(size=(8, 10))
    out, status = apply_text_watermark(original, "image/png", "very long brand name" * 5)
    assert status["applied"]
    image = Image.open(io.BytesIO(out))
    assert image.width == 8 and image.height > 10
    assert image.crop((0, 0, 8, 10)).convert("RGB").tobytes() == Image.open(io.BytesIO(original)).tobytes()


def test_custom_text_retains_ai_label(monkeypatch):
    calls = []
    from PIL import ImageDraw
    original = ImageDraw.ImageDraw.text
    def capture(self, xy, text, *args, **kwargs):
        calls.append(text)
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    _, status = apply_text_watermark(_make_png(), "image/png", "品牌")
    assert status["applied"]
    assert "AI 生成 · 品牌" in calls



def test_malformed_xmp_reports_label_failure():
    info = PngImagePlugin.PngInfo()
    info.add_itxt("XML:com.adobe.xmp", "<broken")
    data = io.BytesIO()
    Image.new("RGB", (100, 100)).save(data, "PNG", pnginfo=info)
    output, status = apply_text_watermark(data.getvalue(), "image/png")
    assert output == data.getvalue()
    assert not status["metadata_applied"]
    assert not status["applied"]
    assert status["reason"]


def test_animated_webp_is_not_flattened():
    data = io.BytesIO()
    Image.new("RGB", (80, 80), "red").save(
        data, "WEBP", save_all=True, append_images=[Image.new("RGB", (80, 80), "blue")],
        duration=100, loop=0,
    )
    output, status = apply_text_watermark(data.getvalue(), "image/webp")
    assert output == data.getvalue()
    assert not status["metadata_applied"]
    assert "animated" in status["reason"]


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_footer_follows_display_orientation(format, mime):
    source = Image.new("RGB", (120, 80), "red")
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "Original artist"
    buffer = io.BytesIO()
    source.save(buffer, format, exif=exif)
    out, status = apply_text_watermark(buffer.getvalue(), mime)
    assert status["applied"] and status["metadata_applied"]
    image = Image.open(io.BytesIO(out))
    assert image.width == 80 and image.height > 120
    assert image.getexif().get(274, 1) == 1
    assert image.getexif()[315] == "Original artist"
    assert image.convert("RGB").getpixel((0, image.height - 1)) == (255, 255, 255)
