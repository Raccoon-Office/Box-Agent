"""Bounded text previews and best-effort streaming output persistence."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


OUTPUT_PREVIEW_BYTES = 32 * 1024
OUTPUT_REPLY_BYTES = 64 * 1024
_CHUNK_CHARS = 8 * 1024
_OMITTED = b"\n...[middle omitted from preview]...\n"


class OutputCapture:
    """Keep a bounded head/tail; spill the complete text only on overflow.

    Storage failures never interrupt kernel output draining. A failed or partial
    file is explicitly distinguished from a complete output file in the reply.
    """

    def __init__(self, directory: Path, label: str = "stdout") -> None:
        self.directory = directory
        self.label = label
        self.total_bytes = 0
        self._preview = b""
        self._stream: BinaryIO | None = None
        self._path: Path | None = None
        self._storage_error: str | None = None

    def append(self, text: str) -> None:
        # A single IOPub message may itself be huge. Do not encode or retain a
        # second complete copy of it while updating the preview/writing the file.
        for offset in range(0, len(text), _CHUNK_CHARS):
            chunk = text[offset:offset + _CHUNK_CHARS].encode("utf-8", errors="replace")
            previous_size = self.total_bytes
            self.total_bytes += len(chunk)
            if self.total_bytes > OUTPUT_PREVIEW_BYTES:
                if previous_size <= OUTPUT_PREVIEW_BYTES and self._storage_error is None:
                    try:
                        self.directory.mkdir(parents=True, exist_ok=True)
                        path = self.directory / f"{self.label}-{uuid4().hex}.txt"
                        self._stream = path.open("xb")
                        self._path = path
                        self._stream.write(self._preview)
                    except Exception as exc:
                        self._fail_storage(exc)
                if self._stream is not None:
                    try:
                        self._stream.write(chunk)
                    except Exception as exc:
                        self._fail_storage(exc)
            combined = self._preview + chunk
            if len(combined) <= OUTPUT_PREVIEW_BYTES:
                self._preview = combined
            else:
                head_size = OUTPUT_PREVIEW_BYTES // 5
                tail_size = OUTPUT_PREVIEW_BYTES - head_size - len(_OMITTED)
                self._preview = combined[:head_size] + _OMITTED + combined[-tail_size:]

    def _fail_storage(self, exc: Exception) -> None:
        # Exception reprs may contain the entire offending output. Record only
        # the type; never let the diagnostic reproduce the original problem.
        self._storage_error = type(exc).__name__
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def finish(self) -> str:
        if self._stream is not None:
            try:
                self._stream.close()
                self._stream = None
            except Exception as exc:
                self._fail_storage(exc)
        # Head/tail byte cuts can fall within a UTF-8 character. Dropping only
        # the incomplete boundary bytes keeps the preview valid and bounded.
        preview = self._preview.decode("utf-8", errors="ignore")
        if self.total_bytes <= OUTPUT_PREVIEW_BYTES:
            return preview
        if self._storage_error is None and self._path is not None:
            status = (
                f"Full output saved to: {self._path}\n"
                "Read this file in bounded pages to recover omitted content."
            )
        else:
            status = (
                f"Full output unavailable (output persistence failed: {self._storage_error}). "
                "Only a head/tail preview is retained; omitted content cannot be recovered. "
                "Re-run a narrower diagnostic if needed; do not blindly repeat side effects."
            )
            if self._path is not None:
                status += f"\nPartial output file (not complete): {self._path}"
        return (
            f"<persisted-output>\nOutput too large ({self.total_bytes} UTF-8 bytes). "
            f"{status}\n\nPreview (head + tail):\n{preview}\n</persisted-output>"
        )


def bound_output_text(
    text: str, directory: Path, label: str = "result", *, inline_limit: int = OUTPUT_REPLY_BYTES,
) -> str:
    """Last defense for final text, including custom kernels and result hooks."""
    if len(text) <= inline_limit // 4:
        return text.encode("utf-8", errors="replace").decode("utf-8")
    if len(text) <= inline_limit:
        size = sum(
            len(text[offset:offset + _CHUNK_CHARS].encode("utf-8", errors="replace"))
            for offset in range(0, len(text), _CHUNK_CHARS)
        )
        if size <= inline_limit:
            return text.encode("utf-8", errors="replace").decode("utf-8")
    capture = OutputCapture(directory, label)
    capture.append(text)
    return capture.finish()
