"""CUA sidecar persistence stays outside the core SessionLog serializer."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

from box_agent.plugins.cua.config import CuaConfig
from box_agent.plugins.cua.sidecar import CuaImageSidecar
from box_agent.plugins.cua.wiring import build_cua_bindings
from box_agent.schema import Message


def test_sidecar_reference_has_no_inline_data_and_hydrates_latest(tmp_path):
    raw = b"small-png-payload"
    block = {
        "type": "input_image",
        "media_type": "image/png",
        "data": base64.b64encode(raw).decode("ascii"),
        "source_bytes": len(raw),
    }
    sidecar = CuaImageSidecar(tmp_path / "images")

    reference = sidecar.persist_block(block)

    assert reference is not None
    assert "data" not in reference
    digest = hashlib.sha256(raw).hexdigest()
    assert reference["contentRef"] == f"images/{digest}.png"
    assert (tmp_path / "images" / f"{digest}.png").read_bytes() == raw
    hydrated = sidecar.hydrate(reference)
    assert hydrated is not None
    assert hydrated["data"] == base64.b64encode(raw).decode("ascii")


def test_sidecar_rejects_tampered_reference(tmp_path):
    raw = b"payload"
    sidecar = CuaImageSidecar(tmp_path / "images")
    reference = sidecar.persist_block({
        "type": "input_image",
        "media_type": "image/png",
        "data": base64.b64encode(raw).decode("ascii"),
    })
    assert reference is not None
    path = tmp_path / "images" / reference["contentRef"].split("/", 1)[1]
    path.write_bytes(b"tampered")
    assert sidecar.hydrate(reference) is None


def test_provider_adapter_hydrates_latest_and_filters_old_refs(tmp_path):
    raw_old, raw_latest = b"old", b"latest"
    sidecar = CuaImageSidecar(tmp_path / "images")
    refs = [sidecar.persist_block({
        "type": "input_image", "media_type": "image/png",
        "data": base64.b64encode(raw).decode("ascii"),
    }) for raw in (raw_old, raw_latest)]
    assert all(refs)
    binding = build_cua_bindings(
        llm=SimpleNamespace(capabilities={"image_input": True}),
        config=CuaConfig(server_name="desktop"),
        sidecar_dir=tmp_path / "images",
    )
    messages = [
        Message(role="user", source="runtime", content=[refs[0]]),
        Message(role="user", source="runtime", content=[refs[1]]),
    ]
    adapted = binding.adapt_provider_messages(messages)
    assert len(adapted) == 1
    assert adapted[0].content[0]["data"] == base64.b64encode(raw_latest).decode("ascii")
