"""Shared input preparation, before any history mutation or model request."""

from __future__ import annotations

import base64
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .api.input import UserInput
from .input_resources import normalize_file_inputs


def prepare_user_input(
    value: str | UserInput, workspace_dir: str | Path, permission_engine=None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = UserInput.text(value) if isinstance(value, str) else value
    if not isinstance(value, UserInput):
        raise TypeError("input must be a string or UserInput")
    files = normalize_file_inputs(list(value.blocks), workspace_dir, permission_engine)
    if sum(block["type"] == "image" for block in value.blocks) + sum(
        item.get("mimeType", "").startswith("image/") for item in files
    ) > 6:
        raise ValueError("INVALID_INPUT_IMAGE_COUNT")
    images = []
    blocks = []
    for block in value.blocks:
        copied = deepcopy(block)
        if block["type"] == "image":
            try:
                data = base64.b64decode(block["data"], validate=True)
            except ValueError as exc:
                raise ValueError("INVALID_INPUT_IMAGE") from exc
            if not data or len(data) > 20 * 1024 * 1024:
                raise ValueError("INVALID_INPUT_IMAGE_SIZE")
            from .image_store import SessionImageStore
            SessionImageStore._dimensions(data, {"media_type": block["mimeType"]})
            copied = {"type": "image", "mimeType": block["mimeType"],
                      "sha256": sha256(data).hexdigest(), "imageIndex": len(images)}
            images.append({"type": "input_image", "media_type": block["mimeType"], "data": block["data"]})
        blocks.append(copied)
    context = deepcopy(value.context)
    if files:
        context["files"] = files
    image_files = [item for item in files if item.get("mimeType", "").startswith("image/")]
    positions = {item["path"]: len(images) + index for index, item in enumerate(image_files)}
    resource_images = {block["uri"]: positions[str(Path(url2pathname(urlsplit(block["uri"]).path)).resolve())]
                       for block in blocks if block["type"] == "resource_link"
                       and str(Path(url2pathname(urlsplit(block["uri"]).path)).resolve()) in positions}
    return {"schemaVersion": 1, "id": value.input_id or uuid4().hex,
            "fingerprint": value.fingerprint(), "blocks": blocks, "context": context,
            "workspace": str(Path(workspace_dir).resolve()), "policy": {},
            "resourceImages": resource_images}, images


def resource_image_paths(record: dict[str, Any]) -> list[str]:
    return [item["path"] for item in record["context"].get("files", [])
            if item.get("mimeType", "").startswith("image/")]
