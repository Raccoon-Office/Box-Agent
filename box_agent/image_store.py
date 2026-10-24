"""Immutable image files owned by one session, separate from request policy."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping, TypedDict

_log = logging.getLogger(__name__)
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg"}


class ImageRef(TypedDict, total=False):
    """JSON-safe occurrence metadata; unavailable images may lack blob fields."""

    imageId: str
    source: str
    contentRef: str
    sha256: str
    media_type: str
    width: int
    height: int
    source_bytes: int
    base64Bytes: int
    createdRound: int
    messageId: str
    messageSeq: int
    toolCallId: str
    toolName: str
    imageIndex: int
    sourceLabel: str
    eligible: bool
    active: bool
    evicted: bool
    evictionReason: str
    delivered: bool
    noticeDelivered: bool
    sourceSessionId: str
    sourceMessageSeq: int
    sourceUserMessageSeq: int
    sourceToolSessionId: str
    sourceToolResultSeq: int
    sourceImageOrder: int


class SessionImageStore:
    """Save exact canonical image bytes and validate session-relative references."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, reference: Mapping[str, Any]) -> Path:
        if not isinstance(reference, Mapping):
            raise ValueError("invalid session image reference")
        digest = reference.get("sha256")
        media_type = reference.get("media_type")
        extension = _EXTENSIONS.get(media_type) if isinstance(media_type, str) else None
        if (not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or extension is None):
            raise ValueError("invalid session image reference")
        expected = f"images/{digest}.{extension}"
        if reference.get("contentRef") != expected:
            raise ValueError("invalid session image path")
        target = self.directory / f"{digest}.{extension}"
        if self.directory.is_symlink() or target.is_symlink():
            raise ValueError("session image paths must not be symbolic links")
        return target

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("session image is not a regular file")
            return handle.read()

    @staticmethod
    def _dimensions(raw: bytes, metadata: Mapping[str, Any]) -> tuple[str, int, int]:
        """Validate exact pixels without changing geometry or image bytes."""
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as opened:
                media_type = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(opened.format)
                if media_type is None or metadata.get("media_type") != media_type:
                    raise ValueError("session image media type mismatch")
                opened.load()
                width, height = opened.size
        except Exception as exc:
            raise ValueError("invalid session image bytes or media type") from exc
        for key, actual in (("width", width), ("height", height)):
            declared = metadata.get(key)
            if declared is not None and (
                isinstance(declared, bool) or not isinstance(declared, int) or declared != actual
            ):
                raise ValueError("session image dimensions do not match its bytes")
        return media_type, width, height

    def persist_block(self, block: dict[str, Any]) -> ImageRef | None:
        """Atomically save bytes before returning a reference; never rewrite a blob."""
        media_type = block.get("media_type")
        extension = _EXTENSIONS.get(media_type) if isinstance(media_type, str) else None
        encoded = block.get("data")
        if extension is None or not isinstance(encoded, str) or not encoded:
            return None
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, TypeError, ValueError):
            return None
        if not raw:
            return None
        try:
            media_type, width, height = self._dimensions(raw, block)
        except ValueError:
            return None
        digest = hashlib.sha256(raw).hexdigest()
        # Only derived, scalar metadata is durable. Arbitrary tool fields may
        # contain nested image data and do not belong in an attachment ref.
        reference = {
            "type": "input_image", "media_type": media_type,
            "contentRef": f"images/{digest}.{extension}", "sha256": digest,
            "source_bytes": len(raw), "base64Bytes": 4 * ((len(raw) + 2) // 3),
            "width": width, "height": height,
        }
        temporary: Path | None = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self._path(reference)
            if target.exists():
                if hashlib.sha256(self._read_bytes(target)).hexdigest() != digest:
                    raise ValueError("session image content hash mismatch")
            else:
                fd, name = tempfile.mkstemp(prefix=".session-image-", dir=self.directory)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if hashlib.sha256(self._read_bytes(self._path(reference))).hexdigest() != digest:
                        raise ValueError("session image content hash mismatch")
                temporary.unlink()
                temporary = None
            # Flush reused blobs too: callers may have imported files from an
            # older producer that did not establish the same durability bound.
            with target.open("rb") as handle:
                os.fsync(handle.fileno())
            if os.name != "nt":
                for directory in (self.directory, self.directory.parent):
                    fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        except (OSError, ValueError):
            _log.warning("session image could not be persisted", exc_info=True)
            return None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return reference

    def load_block(self, reference: Mapping[str, Any]) -> dict[str, Any]:
        """Hydrate exact bytes, rejecting missing, changed or unsafe references."""
        try:
            raw = self._read_bytes(self._path(reference))
        except OSError as exc:
            raise ValueError("session image is missing or unreadable") from exc
        if not raw or hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
            raise ValueError("session image content hash mismatch")
        declared_bytes = reference.get("source_bytes")
        if declared_bytes is not None and declared_bytes != len(raw):
            raise ValueError("session image byte size mismatch")
        encoded_size = 4 * ((len(raw) + 2) // 3)
        declared_encoded = reference.get("base64Bytes")
        if declared_encoded is not None and declared_encoded != encoded_size:
            raise ValueError("session image encoded byte size mismatch")
        media_type, width, height = self._dimensions(raw, reference)
        return {
            "type": "input_image", "media_type": media_type,
            "data": base64.b64encode(raw).decode("ascii"),
            "source_bytes": len(raw), "base64Bytes": encoded_size,
            "width": width, "height": height, "sha256": reference["sha256"],
        }


__all__ = ["ImageRef", "SessionImageStore"]
