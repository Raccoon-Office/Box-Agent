"""CUA screenshot sidecars are referenced by tool text, not conversation images."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

from box_agent.plugins.cua.config import CuaConfig
from box_agent.plugins.cua.sidecar import CuaImageSidecar
from box_agent.plugins.cua.wiring import build_cua_bindings


def test_sidecar_reference_has_no_inline_data(tmp_path):
    raw = b"small-png-payload"
    sidecar = CuaImageSidecar(tmp_path / "images")
    reference = sidecar.persist_block({
        "type": "input_image", "media_type": "image/png",
        "data": base64.b64encode(raw).decode("ascii"),
    })
    assert reference is not None
    digest = hashlib.sha256(raw).hexdigest()
    assert reference["contentRef"] == f"images/{digest}.png"
    assert "data" not in reference
    assert (tmp_path / "images" / f"{digest}.png").read_bytes() == raw


def test_cua_transient_image_is_inline_and_durable_result_is_only_a_path(tmp_path):
    binding = build_cua_bindings(
        llm=SimpleNamespace(capabilities={"image_input": True}),
        config=CuaConfig(server_name="desktop"), sidecar_dir=tmp_path / "images",
    )
    image = {
        "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        "mime_type": "image/png",
    }
    transient = binding.transient_followup_content(
        server_name="desktop", remote_name="screenshot", inline_images=[image],
    )
    durable = binding.persist_image_references(
        server_name="desktop", remote_name="screenshot", inline_images=[image],
    )
    assert transient and transient[0]["data"] == image["data"]
    assert durable and durable[0]["contentRef"].startswith("images/")
    assert "data" not in durable[0]


def test_text_models_receive_neither_cua_image_projection(tmp_path):
    binding = build_cua_bindings(
        llm=SimpleNamespace(capabilities={"image_input": False}),
        config=CuaConfig(server_name="desktop"), sidecar_dir=tmp_path / "images",
    )
    image = {
        "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        "mime_type": "image/png",
    }
    assert binding.transient_followup_content(
        server_name="desktop", remote_name="screenshot", inline_images=[image],
    ) is None
    assert binding.persist_image_references(
        server_name="desktop", remote_name="screenshot", inline_images=[image],
    ) is None
