"""Tests for the plugin-owned Cua screenshot encoder."""

from __future__ import annotations

import base64
import io

import pytest

from box_agent.plugins.cua.image_encoding import (
    _MAX_LONG_EDGE_PX,
    encode_canonical_cua_image,
)

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _png_b64(width: int, height: int, color=(10, 20, 30)) -> str:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _jpeg_b64(width: int, height: int, color=(200, 100, 50)) -> str:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_png_returns_canonical_block_with_dimensions():
    block = encode_canonical_cua_image(_png_b64(320, 200), "image/png")
    assert block is not None
    assert block["type"] == "input_image"
    assert block["media_type"] == "image/png"
    assert block["width"] == 320
    assert block["height"] == 200
    # data must be valid base64
    base64.b64decode(block["data"], validate=True)


def test_jpeg_returns_canonical_block():
    block = encode_canonical_cua_image(_jpeg_b64(100, 100), "image/jpeg")
    assert block is not None
    assert block["media_type"] == "image/jpeg"
    assert block["width"] == 100 and block["height"] == 100


def test_4k_input_is_downsampled_to_cap():
    block = encode_canonical_cua_image(_png_b64(3840, 2160), "image/png")
    assert block is not None
    assert max(block["width"], block["height"]) <= _MAX_LONG_EDGE_PX
    # aspect ratio preserved (16:9)
    assert block["width"] == _MAX_LONG_EDGE_PX
    assert block["height"] == round(2160 * (_MAX_LONG_EDGE_PX / 3840))


def test_declared_mime_ignored_in_favor_of_decoded_format():
    # A PNG payload declared as jpeg is still normalized to png.
    block = encode_canonical_cua_image(_png_b64(64, 64), "image/jpeg")
    assert block is not None
    assert block["media_type"] == "image/png"


def test_unsupported_mime_returns_none():
    assert encode_canonical_cua_image(_png_b64(10, 10), "image/gif") is None
    assert encode_canonical_cua_image(_png_b64(10, 10), "") is None


def test_bad_base64_returns_none():
    assert encode_canonical_cua_image("not-base64!!!", "image/png") is None


def test_empty_data_returns_none():
    assert encode_canonical_cua_image("", "image/png") is None


def test_non_image_bytes_returns_none():
    junk = base64.b64encode(b"this is not an image").decode("ascii")
    assert encode_canonical_cua_image(junk, "image/png") is None
