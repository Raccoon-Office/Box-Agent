"""Session-owned image occurrences and request-only multimodal projections.

Images never become Message content in durable history. The store owns exact
bytes, the SessionLog owns recovery, and this controller owns retention only.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from .config import ImageContextConfig
from .image_store import SessionImageStore
from .llm.capabilities import image_input_support
from .schema import Message


@dataclass(frozen=True)
class ImageProjection:
    messages: list[Message]
    manifest: dict[str, Any]
    input_tokens: int = 0
    blocked_reason: str | None = None
    notices: tuple[str, ...] = ()


class ImageContext:
    def __init__(
        self, session_log: Any = None, config: ImageContextConfig | None = None,
        *, restore: bool = True,
    ):
        self.session_log = session_log
        self.config = config or ImageContextConfig()
        self._temporary = None
        path = getattr(session_log, "path", None)
        if isinstance(path, (str, Path)):
            directory = Path(path).parent / "images"
        else:
            # SDK sessions without a durable Store still have the same policy.
            self._temporary = TemporaryDirectory(prefix="box-agent-images-")
            directory = Path(self._temporary.name) / "images"
        self.store = SessionImageStore(directory)
        self.images: list[dict[str, Any]] = []
        self.model_round = 0
        self._selected: list[str] = []
        self._notice_ids: list[str] = []
        self._notices: dict[str, str] = {}
        replay = getattr(session_log, "replay", None)
        if restore and callable(replay):
            projection = replay()
            self.model_round = getattr(projection, "model_round", 0)
            self.add_references(getattr(projection, "images", []))

    def add_references(self, refs: list[dict[str, Any]]) -> None:
        """Adopt trusted session occurrences, including pending omission notices."""
        for ref in deepcopy(refs):
            self.images.append(ref)
            if ref.get("evicted") and not ref.get("delivered") and not ref.get("noticeDelivered"):
                self._notices[ref["imageId"]] = self._notice(ref, ref.get("evictionReason", "unavailable"))
        # Branch messages may be edited into a different order. Restore the
        # imported prefix's original occurrence order before newer local images.
        prefix_end = next((index for index, ref in enumerate(self.images)
                           if type(ref.get("sourceImageOrder")) is not int), len(self.images))
        self.images[:prefix_end] = sorted(self.images[:prefix_end], key=lambda ref: ref["sourceImageOrder"])

    def reset(self) -> None:
        self.images.clear()
        self._selected.clear()
        self._notice_ids.clear()
        self._notices.clear()

    def register(self, message: Message, blocks: list[dict[str, Any]], *,
                 source: str, eligible: bool = True) -> list[dict[str, Any]]:
        """Snapshot images before associating references with the original event.

        A failed snapshot becomes explicit text and an unavailable occurrence;
        it must never cause a tool's already completed action to run again.
        """
        refs = []
        message_id = uuid4().hex
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "input_image":
                continue
            image_id = uuid4().hex
            try:
                ref = self.store.persist_block(block)
            except (OSError, ValueError):
                ref = None
            failed = ref is None
            ref = ref or {}
            ref.update(imageId=image_id, source=source, createdRound=self.model_round + 1,
                       messageId=message_id, eligible=eligible,
                       active=eligible and not failed, evicted=failed, delivered=False)
            ref["imageIndex"] = len(refs) + 1
            if isinstance(block.get("source_label"), str):
                ref["sourceLabel"] = block["source_label"]
            if message.tool_call_id:
                ref["toolCallId"] = message.tool_call_id
            if message.name:
                ref["toolName"] = message.name
            if failed:
                ref["evictionReason"] = "persistence_failed"
                notice = self._notice(ref, "persistence_failed")
                message.content = f"{message.content or ''}\n{notice}"
            refs.append(ref)
        if refs:
            queue = getattr(self.session_log, "queue_message_images", None)
            if self.session_log is not None and not callable(queue):
                # A custom Store without metadata support cannot satisfy the
                # durability contract. Keep its ordinary text history usable.
                for ref in refs:
                    ref.update(active=False, evicted=True, evictionReason="image_metadata_unsupported")
                    message.content = f"{message.content or ''}\n{self._notice(ref, 'image_metadata_unsupported')}"
            elif callable(queue):
                queue(message, refs)
            self.add_references(refs)
        return refs

    @staticmethod
    def _notice(ref: dict[str, Any], reason: str) -> str:
        return (f"[Image {ref['imageId']} ({ref.get('source', 'unknown')}) was not delivered "
                f"to the model: {reason}. Do not assume its contents were seen.]")

    def prepare(self, messages: list[Message], *, llm: Any = None, tools: Any = None,
                thinking_enabled: bool = False, purpose: str = "main") -> ImageProjection:
        from .kernel.context_engine import _fallback_context_estimate

        request_round = self.model_round + 1
        evicted: list[dict[str, Any]] = []

        def evict(ref: dict[str, Any], reason: str) -> None:
            ref.update(active=False, evicted=True, evictionReason=reason)
            evicted.append({"imageId": ref["imageId"], "reason": reason,
                            "neverDelivered": not ref.get("delivered", False)})
            if not ref.get("delivered", False):
                self._notices[ref["imageId"]] = self._notice(ref, reason)

        candidates = []
        for ref in self.images:
            if not ref.get("active", True) or not ref.get("eligible", True) or ref.get("evicted"):
                continue
            if request_round - ref.get("createdRound", request_round) >= self.config.keep_rounds:
                evict(ref, "round_limit")
            else:
                candidates.append(ref)
        # Edited continuations may reorder messages. Image age still follows
        # its original inference round; equal-round occurrences remain stable.
        candidates.sort(key=lambda ref: ref.get("createdRound", request_round))
        total = sum(ref.get("base64Bytes", 0) for ref in candidates)
        while candidates and total > self.config.max_encoded_bytes:
            ref = candidates.pop(0)
            total -= ref.get("base64Bytes", 0)
            evict(ref, "encoded_byte_budget")

        hydrated = []
        excluded: list[dict[str, Any]] = []
        # A separate text-only summary model must not retire images that are
        # still usable by the main model. Its summary simply omits the pixels.
        image_support = image_input_support(llm)
        skip_images = purpose == "summary" and image_support is not True
        for ref in candidates:
            if skip_images:
                excluded.append({"imageId": ref["imageId"], "reason": "summary_model_without_vision"})
                continue
            if image_support is not True:
                evict(ref, "model_does_not_support_images" if image_support is False
                      else "model_image_support_unknown")
                continue
            try:
                block = self.store.load_block(ref)
            except (OSError, ValueError):
                evict(ref, "missing_or_corrupt_file")
                continue
            for key in ("width", "height", "base64Bytes", "source_bytes"):
                ref[key] = block[key]
            block["preserve_image_bytes"] = True
            hydrated.append((ref, block))
        # Legacy structured references may have omitted encoded sizes. Count
        # the hydrated bytes too before they can enter any actual request.
        total = sum(block["base64Bytes"] for _, block in hydrated)
        while hydrated and total > self.config.max_encoded_bytes:
            ref, block = hydrated.pop(0)
            total -= block["base64Bytes"]
            evict(ref, "encoded_byte_budget")

        def overlay() -> Message | None:
            blocks = []
            if self._notices:
                blocks.append({"type": "text", "text": "\n".join(self._notices.values())})
            for ref, block in hydrated:
                label = (f"Image {ref['imageId']} | source={ref.get('source', 'unknown')} | "
                         f"toolCallId={ref.get('toolCallId', '-')} | "
                         f"index={ref.get('imageIndex', '?')} | "
                         f"size={ref.get('width', '?')}x{ref.get('height', '?')}")
                if ref.get("sourceLabel"):
                    label += f" | {ref['sourceLabel']}"
                blocks.extend(({"type": "text", "text": label}, block))
            return (Message(role="user", source="runtime", content=blocks, trace_redact_content=True)
                    if blocks else None)

        preflight = getattr(llm, "preflight_image_request", None)
        metrics = None
        blocked = None
        while True:
            image_message = overlay()
            provider_messages = [*messages, image_message] if image_message else list(messages)
            if not callable(preflight) or inspect.iscoroutinefunction(preflight):
                break
            metrics = preflight(provider_messages, tools, thinking_enabled=thinking_enabled)
            if not isinstance(metrics, dict):
                if inspect.iscoroutine(metrics):
                    metrics.close()
                metrics = None
                break
            if metrics.get("fits", True):
                break
            if not hydrated:
                blocked = "The complete model request exceeds the provider body-size limit after all images were removed."
                break
            ref, _ = hydrated.pop(0)
            if purpose == "summary":
                excluded.append({"imageId": ref["imageId"], "reason": "provider_body_limit"})
            else:
                evict(ref, "provider_body_limit")

        selected = [{key: ref[key] for key in (
            "imageId", "contentRef", "sha256", "media_type", "width", "height", "base64Bytes",
        ) if key in ref} for ref, _ in hydrated]
        manifest = {"phase": "prepared", "purpose": purpose, "requestRound": request_round,
                    "selected": selected, "encodedBytes": sum(item["base64Bytes"] for item in selected),
                    "evicted": evicted, "excluded": excluded}
        if metrics is not None:
            manifest["requestSize"] = metrics
        if self._notices:
            manifest["notices"] = list(self._notices.values())
            manifest["noticeImageIds"] = list(self._notices)
        # Persist eviction decisions even when token compaction, cancellation,
        # or a local preflight failure prevents a subsequent provider request.
        if evicted and self.session_log is not None:
            self.session_log.append("request/context", {"imageContext": manifest})
            self.session_log.flush()
        if purpose == "main":
            self._selected = [item["imageId"] for item in selected]
            self._notice_ids = list(self._notices)
        return ImageProjection(provider_messages, manifest,
                               _fallback_context_estimate([image_message], {}) if image_message else 0,
                               blocked, tuple(self._notices.values()))

    def mark_response(self, message: Message) -> None:
        """Called only alongside an accepted assistant message commit."""
        next_round = self.model_round + 1
        mark = getattr(self.session_log, "mark_model_response", None)
        if callable(mark):
            mark(message, next_round)
        self.model_round = next_round
        for ref in self.images:
            if ref["imageId"] in self._selected:
                ref["delivered"] = True
            if ref["imageId"] in self._notice_ids:
                ref["noticeDelivered"] = True
        self._notices.clear()
