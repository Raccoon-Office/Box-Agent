"""Resolve already-prepared file references without granting filesystem access."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .tools.safety import validate_path_in_workspace


def normalize_file_inputs(
    prompt: list[Any], workspace_dir: str | Path, permission_engine=None,
) -> list[dict[str, Any]]:
    """Validate all links before accepting input; tools recheck access when reading.

    Only files already inside the bound session workspace are references. A URI
    is neither an upload instruction nor a grant to another session's files.
    """
    files = []
    seen: set[Path] = set()
    links = [block.model_dump(by_alias=True) if hasattr(block, "model_dump") else block
             for block in prompt]
    links = [block for block in links if isinstance(block, dict) and block.get("type") == "resource_link"]
    # Existing host limits: 20 uploads plus 500 explicit source selections.
    if len(links) > 520:
        raise ValueError("INVALID_INPUT_RESOURCE:too_many_files")
    for block in links:
        uri, name = block.get("uri"), block.get("name")
        if (not isinstance(uri, str) or not uri or len(uri) > 16_384
                or not isinstance(name, str) or not name.strip() or len(name) > 1024
                or any(ord(char) < 32 for char in uri)
                or re.search(r"%(?![0-9a-fA-F]{2})", uri)):
            raise ValueError("INVALID_INPUT_RESOURCE:invalid_uri_or_name")
        try:
            parts = urlsplit(uri)
            if (parts.scheme != "file" or parts.netloc not in {"", "localhost"}
                    or parts.query or parts.fragment):
                raise ValueError("unsupported_uri")
            decoded = unquote(parts.path, encoding="utf-8", errors="strict")
            if "\x00" in decoded or "\\" in decoded:
                raise ValueError("invalid_path")
            if os.name == "nt" and re.match(r"^/[a-zA-Z]:/", decoded):
                decoded = decoded[1:]
            path = Path(decoded)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("invalid_path")
            if validate_path_in_workspace(path, Path(workspace_dir)):
                raise ValueError("outside_session_workspace")
            path = path.resolve(strict=True)
            info = path.stat()
            if not path.is_file() or info.st_nlink != 1:
                raise ValueError("not_a_regular_owned_file")
            if permission_engine is not None:
                decision = permission_engine.check(
                    capability="filesystem.read", resource={"path": str(path)}, tool_name="read_file",
                )
                if not decision.allowed:
                    raise ValueError("permission_denied")
            # Test actual readability without loading file bodies into the prompt.
            with path.open("rb") as stream:
                stream.read(1)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError(f"INVALID_INPUT_RESOURCE:{exc}") from exc
        if path in seen:
            continue
        seen.add(path)
        item = {"name": name, "path": str(path), "uri": path.as_uri(), "size": info.st_size}
        title = block.get("title")
        if title is not None:
            if not isinstance(title, str) or len(title) > 1024:
                raise ValueError("INVALID_INPUT_RESOURCE:invalid_title")
            item["title"] = title
        mime_type = block.get("mimeType")
        if mime_type is not None:
            if not isinstance(mime_type, str) or len(mime_type) > 256:
                raise ValueError("INVALID_INPUT_RESOURCE:invalid_mime_type")
            item["mimeType"] = mime_type
        files.append(item)
    return files
