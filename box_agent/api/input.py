"""Protocol-neutral, ordered user input. Resource access is resolved by the session."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any


@dataclass(frozen=True, slots=True)
class UserInput:
    """A user-authored input, before resource preparation or model projection."""

    blocks: tuple[dict[str, Any], ...]
    context: dict[str, Any] = field(default_factory=dict)
    input_id: str | None = None

    def __post_init__(self) -> None:
        from ..input_context import normalize_input_context

        blocks = []
        for item in self.blocks:
            block = (item.model_dump(by_alias=True, exclude_none=True)
                     if hasattr(item, "model_dump") else deepcopy(item))
            if not isinstance(block, dict) or block.get("type") not in {"text", "resource_link", "image"}:
                raise ValueError("INVALID_INPUT_BLOCK")
            if block["type"] == "text" and not isinstance(block.get("text"), str):
                raise ValueError("INVALID_INPUT_TEXT")
            if block["type"] == "image" and (
                not isinstance(block.get("data"), str)
                or not isinstance(block.get("mimeType"), str)
                or not block["mimeType"].startswith("image/")
            ):
                raise ValueError("INVALID_INPUT_IMAGE")
            blocks.append(block)
        if len(blocks) > 1040:
            raise ValueError("INVALID_INPUT_BLOCK_COUNT")
        if self.input_id is not None and (not isinstance(self.input_id, str) or not self.input_id.strip()):
            raise ValueError("INVALID_INPUT_ID")
        object.__setattr__(self, "blocks", tuple(blocks))
        object.__setattr__(self, "context", normalize_input_context(self.context))

    @classmethod
    def text(cls, text: str, *, input_id: str | None = None) -> UserInput:
        return cls(({"type": "text", "text": text},), input_id=input_id)

    @property
    def user_text(self) -> str:
        return "\n".join(block["text"] for block in self.blocks if block["type"] == "text")

    def fingerprint(self) -> str:
        # The identity is of the submitted request, not today's mutable filesystem.
        payload = json.dumps([self.blocks, self.context], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return deepcopy({"blocks": list(self.blocks), "context": self.context, "input_id": self.input_id})
