"""Session image files are immutable, verifiable and independently relocatable."""

import base64
import hashlib
import shutil

import pytest

import box_agent.image_store as image_store_module
from box_agent.image_store import SessionImageStore


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def image_block(raw=None):
    raw = base64.b64decode(PNG) if raw is None else raw
    return {"type": "input_image", "media_type": "image/png",
            "data": base64.b64encode(raw).decode("ascii"), "width": 1, "height": 1}


def test_image_store_round_trip_has_exact_bytes_without_inline_reference_data(tmp_path):
    store = SessionImageStore(tmp_path / "images")
    block = image_block()
    reference = store.persist_block(block)
    raw = base64.b64decode(block["data"])
    assert reference["sha256"] == hashlib.sha256(raw).hexdigest()
    assert reference["source_bytes"] == len(raw)
    assert reference["base64Bytes"] == len(block["data"])
    assert reference["contentRef"] == f"images/{reference['sha256']}.png"
    assert "data" not in reference
    loaded = store.load_block(reference)
    assert loaded["data"] == block["data"]
    assert (loaded["width"], loaded["height"]) == (1, 1)
    assert store.persist_block(block) == reference
    assert len(list((tmp_path / "images").iterdir())) == 1


def test_session_image_references_survive_moving_the_session_directory(tmp_path):
    original = tmp_path / "original"
    reference = SessionImageStore(original / "images").persist_block(image_block())
    moved = tmp_path / "moved"
    shutil.copytree(original, moved)
    shutil.rmtree(original)
    assert SessionImageStore(moved / "images").load_block(reference)["data"] == image_block()["data"]


@pytest.mark.parametrize("change", ["missing", "modified", "size", "encoded-size"])
def test_missing_or_modified_images_raise_value_error(tmp_path, change):
    store = SessionImageStore(tmp_path / "images")
    reference = store.persist_block(image_block())
    path = tmp_path / reference["contentRef"]
    if change == "missing":
        path.unlink()
    elif change == "modified":
        path.write_bytes(b"changed")
    elif change == "size":
        reference["source_bytes"] += 1
    else:
        reference["base64Bytes"] -= 4
    with pytest.raises(ValueError):
        store.load_block(reference)


@pytest.mark.parametrize("path", ["../other.png", "/tmp/other.png", "images/../other.png"])
def test_image_reference_cannot_escape_its_session(tmp_path, path):
    store = SessionImageStore(tmp_path / "images")
    reference = store.persist_block(image_block())
    reference["contentRef"] = path
    with pytest.raises(ValueError, match="path"):
        store.load_block(reference)


def test_image_store_refuses_symlink_files_and_directories(tmp_path):
    store = SessionImageStore(tmp_path / "session" / "images")
    reference = store.persist_block(image_block())
    target = tmp_path / "session" / reference["contentRef"]
    external = tmp_path / "external"
    target.rename(external)
    target.symlink_to(external)
    with pytest.raises(ValueError, match="symbolic"):
        store.load_block(reference)
    assert store.persist_block(image_block()) is None
    target.unlink()
    (tmp_path / "session" / "images").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "session" / "images").symlink_to(outside)
    assert store.persist_block(image_block()) is None
    assert list(outside.iterdir()) == []


def test_image_store_does_not_replace_corrupted_existing_content(tmp_path):
    store = SessionImageStore(tmp_path / "images")
    reference = store.persist_block(image_block())
    path = tmp_path / reference["contentRef"]
    path.write_bytes(b"corruption must remain observable")
    assert store.persist_block(image_block()) is None
    assert path.read_bytes() == b"corruption must remain observable"


def test_image_write_failure_does_not_return_a_durable_reference(tmp_path, monkeypatch):
    def fail_sync(fd):
        raise OSError("disk failure")
    monkeypatch.setattr(image_store_module.os, "fsync", fail_sync)
    store = SessionImageStore(tmp_path / "images")
    assert store.persist_block(image_block()) is None
    assert list((tmp_path / "images").iterdir()) == []


@pytest.mark.parametrize("data", ["", "not base64!", None])
def test_invalid_image_bytes_do_not_create_files(tmp_path, data):
    block = {**image_block(), "data": data}
    assert SessionImageStore(tmp_path / "images").persist_block(block) is None
    assert not (tmp_path / "images").exists()


@pytest.mark.parametrize("change", [{"width": 12}, {"height": True}, {"media_type": "image/jpeg"}])
def test_declared_image_geometry_and_mime_must_match_actual_bytes(tmp_path, change):
    assert SessionImageStore(tmp_path / "images").persist_block({**image_block(), **change}) is None
    assert not (tmp_path / "images").exists()


def test_image_metadata_is_derived_from_pixels_and_excludes_unknown_nested_fields(tmp_path):
    store = SessionImageStore(tmp_path / "images")
    block = {**image_block(), "extra": {"data": "do not persist this"}}
    del block["width"]
    del block["height"]
    reference = store.persist_block(block)
    assert (reference["width"], reference["height"]) == (1, 1)
    assert set(reference) == {"type", "media_type", "contentRef", "sha256", "source_bytes",
                              "base64Bytes", "width", "height"}
    assert store.persist_block(image_block(b"not an image")) is None
