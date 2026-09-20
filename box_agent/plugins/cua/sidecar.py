"""Content-addressed CUA image sidecars used by the durable Surface."""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_EXT = {"image/png": "png", "image/jpeg": "jpg"}


class CuaImageSidecar:
    """Persist canonical image bytes and hydrate relative references."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def persist_block(self, block: dict[str, Any]) -> dict[str, Any] | None:
        media_type = str(block.get("media_type") or "")
        extension = _EXT.get(media_type)
        encoded = block.get("data")
        if extension is None or not isinstance(encoded, str) or not encoded:
            return None
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, TypeError, ValueError):
            return None
        if not raw:
            return None
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"images/{digest}.{extension}"
        target = self.directory / f"{digest}.{extension}"
        temporary: Path | None = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    return None
            else:
                fd, name = tempfile.mkstemp(prefix=".cua-image-", dir=self.directory)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                        return None
                temporary.unlink(missing_ok=True)
                temporary = None
        except OSError:
            _log.debug("CUA image sidecar write failed", exc_info=True)
            return None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        reference = {
            key: value for key, value in block.items() if key != "data"
        }
        reference.update({
            "type": "input_image",
            "media_type": media_type,
            "contentRef": relative,
            "sha256": digest,
            "source_bytes": len(raw),
        })
        return reference

    def hydrate(self, block: dict[str, Any]) -> dict[str, Any] | None:
        """Load one relative sidecar reference as a provider-ready block."""
        if block.get("type") != "input_image" or "data" in block:
            return dict(block)
        relative = block.get("contentRef")
        digest = block.get("sha256")
        if not isinstance(relative, str) or not relative.startswith("images/"):
            return None
        filename = Path(relative).name
        if Path(relative).parent != Path("images") or not isinstance(digest, str):
            return None
        if filename.split(".", 1)[0] != digest:
            return None
        target = self.directory / filename
        try:
            raw = target.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(raw).hexdigest() != digest:
            return None
        hydrated = dict(block)
        hydrated["data"] = base64.b64encode(raw).decode("ascii")
        hydrated.setdefault("source_bytes", len(raw))
        return hydrated


__all__ = ["CuaImageSidecar"]
