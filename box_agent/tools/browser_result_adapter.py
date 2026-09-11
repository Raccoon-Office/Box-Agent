"""Browser snapshot/screenshot persistence and safe trace metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..artifacts import artifact_scan_root as _artifact_scan_root
from .base import ToolResult


_BROWSER_SNAPSHOT_OUTPUT_PATH_ERROR = (
    "BROWSER_SNAPSHOT_OUTPUT_PATH_INVALID: relative snapshot filenames must stay "
    "inside the session cwd. Use a path such as "
    "research/page-snapshot.md, or omit filename when no persisted snapshot is needed."
)


def _prepare_browser_snapshot_output(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
) -> tuple[Path | None, str | None]:
    """Turn a Playwright snapshot filename into Box-Agent-managed persistence.

    Standalone Playwright MCP servers run in their own process and therefore do
    not share Box-Agent's workspace cwd.  They also intentionally restrict file
    writes to their own temp roots. For a filename inside the session cwd,
    request an inline snapshot from Playwright and persist that returned
    Markdown in Box-Agent after the tool succeeds.
    """
    if tool_name != "managed_browser_snapshot":
        return None, None
    filename = arguments.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return None, None
    supplied_path = Path(filename).expanduser()
    cwd_root = _artifact_scan_root(workspace_dir)
    if cwd_root is None:
        return None, None
    cwd_root = cwd_root.resolve()
    resolved_path = (
        supplied_path.resolve()
        if supplied_path.is_absolute()
        else (cwd_root / supplied_path).resolve()
    )
    if not resolved_path.is_relative_to(cwd_root):
        if supplied_path.is_absolute():
            return None, None
        return None, _BROWSER_SNAPSHOT_OUTPUT_PATH_ERROR
    arguments.pop("filename", None)
    return resolved_path, None


def _persist_browser_snapshot_output(
    result: ToolResult,
    target_path: Path | None,
) -> ToolResult:
    """Persist an inline browser snapshot to its requested artifact path."""
    if target_path is None or not result.success:
        return result
    content = result.content if isinstance(result.content, str) else ""
    if not content.strip():
        return result.model_copy(
            update={
                "success": False,
                "error": (
                    "managed_browser_snapshot returned no inline content to persist at "
                    f"{target_path}"
                ),
            }
        )
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return result.model_copy(
            update={
                "success": False,
                "error": f"Could not persist browser snapshot at {target_path}: {exc}",
            }
        )
    return result.model_copy(
        update={"content": f"{content.rstrip()}\n\nSnapshot persisted to {target_path}"}
    )


def _prepare_browser_screenshot_output(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_dir: str | None,
) -> tuple[Path | None, str | None]:
    """Request an inline Playwright screenshot for Box-Agent-managed persistence."""
    if tool_name != "managed_browser_take_screenshot":
        return None, None
    filename = arguments.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return None, None
    supplied_path = Path(filename).expanduser()
    cwd_root = _artifact_scan_root(workspace_dir)
    if cwd_root is None:
        return None, None
    cwd_root = cwd_root.resolve()
    resolved_path = (
        supplied_path.resolve()
        if supplied_path.is_absolute()
        else (cwd_root / supplied_path).resolve()
    )
    if not resolved_path.is_relative_to(cwd_root):
        if supplied_path.is_absolute():
            return None, None
        return None, (
            "BROWSER_SCREENSHOT_OUTPUT_PATH_INVALID: filename must stay inside "
            "the session cwd"
        )
    arguments.pop("filename", None)
    return resolved_path, None


def _persist_browser_screenshot_output(
    result: ToolResult,
    target_path: Path | None,
) -> ToolResult:
    """Persist an inline MCP image; persistence failure remains advisory."""
    if target_path is None or not result.success:
        return result
    raw_output = result.raw_output if isinstance(result.raw_output, dict) else {}
    images = raw_output.get("mcp_inline_images")
    image = images[0] if isinstance(images, list) and images else None
    content = (result.content or "").rstrip()
    if not isinstance(image, dict) or not isinstance(image.get("data"), str):
        warning = (
            "Browser screenshot was not returned inline; visual QA may be skipped: "
            f"{target_path}"
        )
        return result.model_copy(update={"content": f"{content}\n\n{warning}".strip()})
    try:
        import base64

        payload = base64.b64decode(image["data"], validate=True)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(payload)
    except (OSError, ValueError) as exc:
        warning = (
            f"Could not persist browser screenshot at {target_path}: {exc}. "
            "Visual QA may be skipped."
        )
        return result.model_copy(update={"content": f"{content}\n\n{warning}".strip()})
    return result.model_copy(
        update={"content": f"{content}\n\nScreenshot persisted to {target_path}".strip()}
    )


def _trace_safe_tool_raw_output(raw_output: Any) -> Any:
    """Keep inline MCP image payloads out of durable JSONL traces."""
    if not isinstance(raw_output, dict) or "mcp_inline_images" not in raw_output:
        return raw_output
    images = raw_output.get("mcp_inline_images")
    metadata = []
    if isinstance(images, list):
        for image in images:
            if isinstance(image, dict):
                data = image.get("data")
                metadata.append(
                    {
                        "mime_type": image.get("mime_type"),
                        "encoded_chars": len(data) if isinstance(data, str) else None,
                    }
                )
    return {**raw_output, "mcp_inline_images": metadata}
