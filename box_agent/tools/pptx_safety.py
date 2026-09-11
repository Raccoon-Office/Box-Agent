"""Safety guards for PPTX HTML-first export workflows."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Literal


_BYPASS_ERROR = (
    "PPTX HTML self-check bypass blocked. Use scripts/html_to_editable_pptx.js, "
    "fix qa/html_self_check.json failures, or report "
    "Editable PPTX export: BLOCKED (HTML self-check failed)."
)
_IMAGE_STATUS_ERROR_MESSAGES = {
    "PPTX_IMAGE_STATUS_COMMAND_SHAPE": (
        "Run the synchronizer as the only shell command, optionally preceded by "
        "POSIX 'cd PRESENTATION_DIR &&' or PowerShell "
        "'Set-Location -LiteralPath PRESENTATION_DIR -ErrorAction Stop;'; "
        "do not add pipes, redirects, "
        "other command chaining, or diagnostic suffixes."
    ),
    "PPTX_IMAGE_STATUS_PARSE_ERROR": (
        "Use the exact standalone command form documented by the PPTX skill."
    ),
    "PPTX_IMAGE_STATUS_RUNTIME_CONTEXT": (
        "Start synchronization from an explicit presentation directory or the "
        "tool workspace directory."
    ),
    "PPTX_IMAGE_STATUS_PRESENTATION_DIR": (
        "Use a literal presentation directory without shell-variable expansion."
    ),
    "PPTX_IMAGE_STATUS_NODE_FORM": (
        "Use the platform-specific trusted Node form documented by the PPTX skill."
    ),
    "PPTX_IMAGE_STATUS_SCRIPT_IDENTITY": (
        "Use the loader-expanded bundled synchronizer path without copying or "
        "renaming it."
    ),
    "PPTX_IMAGE_STATUS_MANIFEST_SCOPE": (
        "Use the literal presentation-directory-relative manifest path documented "
        "by the PPTX skill."
    ),
}

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
_TRUSTED_POWERSHELL_NODE_TOKENS = frozenset(
    {
        "node",
        "node.exe",
        "$env:BOX_AGENT_NODE",
        "${env:BOX_AGENT_NODE}",
    }
)
# Preserve quoting until after validating shell expansion. shlex.split() alone
# loses the distinction between '$name', "$name", and an escaped dollar sign.
_POSIX_PATH_WORD = r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\\.|[^\s'\"\\;&|<>])+"
_POSIX_IMAGE_STATUS_COMMAND = re.compile(
    rf"(?:cd\s+(?P<root>{_POSIX_PATH_WORD})\s*&&\s*)?"
    rf"(?P<node>{_POSIX_PATH_WORD})\s+"
    rf"(?P<script>{_POSIX_PATH_WORD})\s+"
    rf"(?P<manifest>{_POSIX_PATH_WORD})",
)
# This is a deliberately small PowerShell grammar, not POSIX shlex: backslashes
# are literal and doubled single quotes represent one apostrophe. Expandable
# strings are allowed only for the trusted Node environment variable.
_POWERSHELL_QUOTED_LITERAL = r"'(?:[^']|'')*'|\"[^\"$`]*\""
_POWERSHELL_PATH_TOKEN = rf"(?:{_POWERSHELL_QUOTED_LITERAL}|[A-Za-z0-9_./:\\\\-]+)"
_POWERSHELL_NODE_VARIABLE = r"\$(?:env:BOX_AGENT_NODE|\{env:BOX_AGENT_NODE\})"
_POWERSHELL_IMAGE_STATUS_COMMAND = re.compile(
    rf"(?:Set-Location\s+-LiteralPath\s+(?P<root>{_POWERSHELL_QUOTED_LITERAL})"
    rf"\s+-ErrorAction\s+Stop\s*;\s*)?"
    rf"(?P<call>&\s+)?(?P<node>node(?:\.exe)?|'node(?:\.exe)?'|"
    rf'"node(?:\.exe)?"|{_POWERSHELL_NODE_VARIABLE}|'
    rf'"{_POWERSHELL_NODE_VARIABLE}")\s+'
    rf"(?P<script>{_POWERSHELL_PATH_TOKEN})\s+"
    rf"(?P<manifest>{_POWERSHELL_PATH_TOKEN})",
)


def _posix_literal_value(token: str) -> str | None:
    """Decode one shell word only when every character is literal.

    Quoted fragments may be concatenated (as in shlex.quote's apostrophe
    escaping). Double-quoted backslashes follow POSIX shell rules, including
    escaped dollars/backticks, which shlex does not fully decode.
    """
    value: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(token):
        char = token[index]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                value.append(char)
        elif char == "\\":
            index += 1
            if index == len(token):
                return None
            escaped = token[index]
            if quote == '"' and escaped not in '$`"\\':
                value.append("\\")
            value.append(escaped)
        elif quote == '"':
            if char == '"':
                quote = None
            elif char in "$`":
                return None
            else:
                value.append(char)
        elif char in "'\"":
            quote = char
        elif char in "$`*?[]{}~#()":
            # Reject substitutions, globs, brace/tilde expansion, and comments.
            # These characters are fine inside literal quotes or when escaped.
            return None
        else:
            value.append(char)
        index += 1
    return "".join(value) if quote is None else None


def _powershell_literal_value(token: str) -> str:
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    return token[1:-1] if token.startswith('"') else token


def _image_status_command_error(reason_code: str) -> str:
    """Return a stable public reason without echoing commands or local paths."""
    guidance = _IMAGE_STATUS_ERROR_MESSAGES[reason_code]
    return (
        "PPTX image-status synchronization blocked. "
        f"reason_code={reason_code}. {guidance}"
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
    shell_style: Literal["posix", "powershell"] = "posix",
) -> str | None:
    """Fail closed for shell calls to the image-status manifest synchronizer.

    ``runtime_env`` remains in the compatibility signature but output-root
    variables in it are deliberately ignored.
    """
    if "sync_image_manifest_status.js" not in command:
        return None
    if any(char in command for char in "\x00\n\r"):
        return _image_status_command_error("PPTX_IMAGE_STATUS_COMMAND_SHAPE")
    supplied_root_token: str | None = None
    uses_powershell_call_operator = False
    if shell_style == "powershell":
        # PowerShell treats typographic quotes as string delimiters too. Reject
        # them rather than pretending they are literal path characters.
        if any(char in command for char in "\x00\u2018\u2019\u201c\u201d"):
            return _image_status_command_error("PPTX_IMAGE_STATUS_COMMAND_SHAPE")
        match = _POWERSHELL_IMAGE_STATUS_COMMAND.fullmatch(command.strip())
        if match is None:
            return _image_status_command_error("PPTX_IMAGE_STATUS_COMMAND_SHAPE")
        if match["root"] is not None:
            supplied_root_token = _powershell_literal_value(match["root"])
        uses_powershell_call_operator = match["call"] is not None
        if match["node"].startswith(("'", '"')) and not uses_powershell_call_operator:
            return _image_status_command_error("PPTX_IMAGE_STATUS_COMMAND_SHAPE")
        tokens = [
            _powershell_literal_value(match[name])
            for name in ("node", "script", "manifest")
        ]
    else:
        try:
            shlex.split(command)
        except ValueError:
            return _image_status_command_error("PPTX_IMAGE_STATUS_PARSE_ERROR")
        match = _POSIX_IMAGE_STATUS_COMMAND.fullmatch(command.strip())
        if match is None:
            return _image_status_command_error("PPTX_IMAGE_STATUS_COMMAND_SHAPE")
        if match["root"] is not None:
            supplied_root_token = _posix_literal_value(match["root"])
            if not supplied_root_token:
                return _image_status_command_error("PPTX_IMAGE_STATUS_PRESENTATION_DIR")
        node_word = match["node"]
        # Only the executable position may expand a trusted runtime variable.
        # Single-quoted/escaped variables are literal executable names instead.
        node_variable = (
            node_word[1:-1]
            if node_word.startswith('"') and node_word.endswith('"')
            else node_word
        )
        if node_variable in _TRUSTED_NODE_TOKENS and node_variable.startswith("$"):
            node_token = node_variable
        else:
            node_token = _posix_literal_value(node_word)
            if node_token not in {"node", "node.exe"}:
                return _image_status_command_error("PPTX_IMAGE_STATUS_NODE_FORM")
        if node_token is None:
            return _image_status_command_error("PPTX_IMAGE_STATUS_NODE_FORM")
        script_token = _posix_literal_value(match["script"])
        if script_token is None:
            return _image_status_command_error("PPTX_IMAGE_STATUS_SCRIPT_IDENTITY")
        manifest_token = _posix_literal_value(match["manifest"])
        if manifest_token is None:
            return _image_status_command_error("PPTX_IMAGE_STATUS_MANIFEST_SCOPE")
        tokens = [node_token, script_token, manifest_token]

    del runtime_env
    workspace_root = (
        Path(workspace_dir).expanduser().resolve(strict=False)
        if workspace_dir
        else None
    )
    if supplied_root_token is not None:
        if not supplied_root_token:
            return _image_status_command_error(
                "PPTX_IMAGE_STATUS_PRESENTATION_DIR"
            )
        supplied_root = Path(
            supplied_root_token.replace("\\", "/")
        ).expanduser()
        if not supplied_root.is_absolute():
            if workspace_root is None:
                return _image_status_command_error(
                    "PPTX_IMAGE_STATUS_RUNTIME_CONTEXT"
                )
            supplied_root = workspace_root / supplied_root
        presentation_dir = supplied_root.resolve(strict=False)
    elif workspace_root is not None:
        presentation_dir = workspace_root
    else:
        return _image_status_command_error("PPTX_IMAGE_STATUS_RUNTIME_CONTEXT")

    node_token, script_token, manifest_token = tokens
    trusted_node_tokens = (
        _TRUSTED_POWERSHELL_NODE_TOKENS
        if shell_style == "powershell"
        else _TRUSTED_NODE_TOKENS
    )
    if node_token not in trusted_node_tokens:
        return _image_status_command_error("PPTX_IMAGE_STATUS_NODE_FORM")
    if (
        shell_style == "powershell"
        and node_token in {"$env:BOX_AGENT_NODE", "${env:BOX_AGENT_NODE}"}
        and not uses_powershell_call_operator
    ):
        return _image_status_command_error("PPTX_IMAGE_STATUS_COMMAND_SHAPE")

    script_path = Path(script_token.replace("\\", "/")).expanduser()
    if (
        not script_path.is_absolute()
        or script_path.resolve(strict=False)
        != _SYNC_IMAGE_STATUS_SCRIPT.resolve(strict=False)
    ):
        return _image_status_command_error("PPTX_IMAGE_STATUS_SCRIPT_IDENTITY")

    manifest_path = Path(manifest_token.replace("\\", "/")).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = presentation_dir / manifest_path
    expected_manifest = presentation_dir / "assets" / "generated" / "manifest.json"
    if manifest_path.resolve(strict=False) != expected_manifest.resolve(strict=False):
        return _image_status_command_error("PPTX_IMAGE_STATUS_MANIFEST_SCOPE")
    return None
