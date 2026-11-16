"""CUA screenshot sidecars are referenced by tool text, not conversation images."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

from box_agent.plugins.cua.config import CuaConfig
from box_agent.plugins.cua.sidecar import CuaImageSidecar
from box_agent.plugins.cua.wiring import build_cua_bindings


def test_sidecar_reference_has_no_inline_data(tmp_path):
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    sidecar = CuaImageSidecar(tmp_path / "images")
    reference = sidecar.persist_block({
        "type": "input_image", "media_type": "image/png",
        "data": base64.b64encode(raw).decode("ascii"),
    })
    assert reference is not None
    digest = hashlib.sha256(raw).hexdigest()
    assert reference["contentRef"] == f"images/{digest}.png"
    assert reference["path"] == str((tmp_path / "images" / f"{digest}.png").resolve())
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


def test_text_models_persist_images_without_visual_input(tmp_path):
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
    references = binding.persist_image_references(
        server_name="desktop", remote_name="screenshot", inline_images=[image],
    )
    assert references and references[0]["contentRef"].startswith("images/")
    assert (tmp_path / references[0]["contentRef"]).is_file()


def test_image_persistence_can_be_disabled_independently(tmp_path):
    binding = build_cua_bindings(
        llm=SimpleNamespace(capabilities={"image_input": True}),
        config=CuaConfig(server_name="desktop", persist_images=False),
        sidecar_dir=tmp_path / "images",
    )
    image = {
        "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        "mime_type": "image/png",
    }
    kwargs = dict(server_name="desktop", remote_name="screenshot", inline_images=[image])
    assert binding.transient_followup_content(**kwargs)
    assert binding.persist_image_references(**kwargs) is None
    assert not (tmp_path / "images").exists()


def test_result_scoped_image_reuse_preserves_bytes_and_does_not_leak(tmp_path, monkeypatch):
    from box_agent.plugins.cua import wiring
    from box_agent.tools.mcp_result_hooks import mcp_result_processing_scope

    binding = build_cua_bindings(
        llm=SimpleNamespace(capabilities={"image_input": True}),
        config=CuaConfig(server_name="desktop"), sidecar_dir=tmp_path / "images",
    )
    image = {"data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "mime_type": "image/png"}
    calls = []
    encode = wiring.encode_canonical_cua_image

    def counted(*args, **kwargs):
        calls.append(args)
        return encode(*args, **kwargs)

    monkeypatch.setattr(wiring, "encode_canonical_cua_image", counted)
    kwargs = dict(server_name="desktop", remote_name="screenshot", inline_images=[image])
    with mcp_result_processing_scope():
        durable = binding.persist_image_references(**kwargs)
        first = binding.transient_followup_content(**kwargs)
        assert len(calls) == 1
        assert first[0]["data"] == image["data"]
        assert durable[0]["sha256"] == first[0]["sha256"]
        first[0]["width"] = 999
        assert binding.transient_followup_content(**kwargs)[0]["width"] == 1
        with mcp_result_processing_scope():
            assert binding.transient_followup_content(**kwargs)[0]["width"] == 1
        assert len(calls) == 2
        binding.transient_followup_content(**kwargs)
        assert len(calls) == 2
    with mcp_result_processing_scope():
        binding.transient_followup_content(**kwargs)
    assert len(calls) == 3
    binding.transient_followup_content(**kwargs)
    assert len(calls) == 4


def test_result_image_cache_is_released_on_failure():
    import pytest
    from box_agent.tools.mcp_result_hooks import (
        mcp_result_processing_scope, mcp_result_processing_cache,
    )
    owner = object()
    with pytest.raises(RuntimeError):
        with mcp_result_processing_scope():
            mcp_result_processing_cache(owner)["private"] = "image"
            raise RuntimeError("stop")
    assert mcp_result_processing_cache(owner) is None
