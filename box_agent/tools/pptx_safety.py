"""Safety guards for PPTX HTML-first export workflows."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path


_BYPASS_ERROR = (
    "PPTX HTML self-check bypass blocked. Use scripts/html_to_editable_pptx.js, "
    "fix qa/html_self_check.json failures, or report "
    "Editable PPTX export: BLOCKED (HTML self-check failed)."
)
_IMAGE_STATUS_COMMAND_ERROR = (
    "PPTX image-status synchronization blocked. Run only the bundled "
    "sync_image_manifest_status.js script against "
    "assets/generated/manifest.json with the trusted Node executable."
)

_NON_EXECUTABLE_STYLESHEET_SUFFIXES = {".css", ".less", ".sass", ".scss"}
_SYNC_IMAGE_STATUS_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "document-skills"
    / "pptx"
    / "scripts"
    / "sync_image_manifest_status.js"
)
_TRUSTED_NODE_TOKENS = frozenset(
    {
        "node",
        "node.exe",
        "$BOX_AGENT_NODE",
        "${BOX_AGENT_NODE}",
        "${BOX_AGENT_NODE:-node}",
    }
)


def _has_skipcheck_name(text: str) -> bool:
    return bool(re.search(r"skip[-_ ]?check|skipcheck|bypass", text, re.IGNORECASE))


def _mentions_pptx_exporter(text: str) -> bool:
    lower = text.lower()
    return (
        "html_to_editable_pptx.js" in lower
        or "html-to-pptx.js" in lower
        or "dom-to-pptx.bundle.js" in lower
        or "domtopptx" in lower
        or "exporttopptx" in lower
    )


def _mentions_self_check(text: str) -> bool:
    lower = text.lower()
    return "html_self_check" in lower or "runselfcheck" in lower


def _looks_like_direct_dom_export(text: str) -> bool:
    lower = text.lower()
    return (
        ("dom-to-pptx.bundle.js" in lower or "domtopptx" in lower)
        and "exporttopptx" in lower
        and "html_self_check" not in lower
        and "runselfcheck" not in lower
    )


def _looks_like_self_check_removal(text: str) -> bool:
    lower = text.lower()
    return _mentions_self_check(text) and any(
        token in lower
        for token in (
            "replace",
            "writefilesync",
            "copyfile",
            "remove",
            "delete",
            "splice",
            "skip",
            "bypass",
            "comment out",
            "移除",
            "删除",
            "注释",
            "绕过",
        )
    )


def detect_pptx_self_check_bypass(path: str | None, text: str) -> str | None:
    """Detect attempts to create or execute a PPTX self-check bypass.

    This intentionally targets the bad failure mode observed in PPTX generation:
    creating a temporary exporter that removes ``runSelfCheck`` or calling the
    DOM-to-PPTX bundle directly after self-check fails. Normal inspection of the
    official exporter remains allowed.
    """
    file_path = Path(path) if path else None
    if file_path and file_path.suffix.lower() in _NON_EXECUTABLE_STYLESHEET_SUFFIXES:
        return None

    path_text = str(file_path.name if file_path else "")
    combined = f"{path_text}\n{text}"

    if _has_skipcheck_name(combined) and _mentions_pptx_exporter(combined):
        return _BYPASS_ERROR

    if _looks_like_direct_dom_export(combined):
        return _BYPASS_ERROR

    if _looks_like_self_check_removal(combined) and _mentions_pptx_exporter(combined):
        return _BYPASS_ERROR

    return None


def detect_pptx_image_status_command_bypass(
    command: str,
    *,
    workspace_dir: str | None,
    runtime_env: Mapping[str, str] | None,
) -> str | None:
    """Fail closed for shell calls to the image-status manifest synchronizer."""
    if "sync_image_manifest_status.js" not in command:
        return None
    if "\n" in command or "\r" in command:
        return _IMAGE_STATUS_COMMAND_ERROR
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _IMAGE_STATUS_COMMAND_ERROR

    artifact_root_raw = (
        runtime_env.get("BOX_AGENT_OUTPUT_DIR") if runtime_env is not None else None
    ) or workspace_dir
    if not artifact_root_raw:
        return _IMAGE_STATUS_COMMAND_ERROR
    artifact_root = Path(artifact_root_raw).expanduser().resolve(strict=False)

    if len(tokens) >= 3 and tokens[0] == "cd" and tokens[2] == "&&":
        supplied_root = Path(tokens[1].replace("\\", "/")).expanduser()
        if supplied_root.resolve(strict=False) != artifact_root:
            return _IMAGE_STATUS_COMMAND_ERROR
        tokens = tokens[3:]

    if len(tokens) != 3:
        return _IMAGE_STATUS_COMMAND_ERROR
    node_token, script_token, manifest_token = tokens
    if node_token not in _TRUSTED_NODE_TOKENS:
        return _IMAGE_STATUS_COMMAND_ERROR

    script_path = Path(script_token.replace("\\", "/")).expanduser()
    if (
        not script_path.is_absolute()
        or script_path.resolve(strict=False)
        != _SYNC_IMAGE_STATUS_SCRIPT.resolve(strict=False)
    ):
        return _IMAGE_STATUS_COMMAND_ERROR

    manifest_path = Path(manifest_token.replace("\\", "/")).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = artifact_root / manifest_path
    expected_manifest = artifact_root / "assets" / "generated" / "manifest.json"
    if manifest_path.resolve(strict=False) != expected_manifest.resolve(strict=False):
        return _IMAGE_STATUS_COMMAND_ERROR
    return None
