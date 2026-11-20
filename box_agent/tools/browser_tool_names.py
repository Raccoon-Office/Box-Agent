"""Stable names for the internal managed and user Browser MCP backends."""

from __future__ import annotations

import copy
import re
from typing import Any


MANAGED_BROWSER_PREFIX = "managed_browser_"
USER_BROWSER_PREFIX = "user_browser_"

_USER_BROWSER_REMOTE_TO_PUBLIC = {
    "browser_open_url": "user_browser_open_tab_and_read",
    "browser_read_page": "user_browser_read_page",
    "browser_read_article": "user_browser_read_article",
    "browser_read_current_page": "user_browser_read_current_page",
    "browser_read_section": "user_browser_read_section",
    "browser_extract_structured_data": "user_browser_extract_structured_data",
    "browser_connector_snapshot": "user_browser_snapshot",
    "browser_connector_click": "user_browser_click",
    "browser_connector_fill": "user_browser_fill",
    "browser_connector_submit": "user_browser_submit",
    "browser_session_start": "user_browser_session_start",
    "browser_session_call": "user_browser_session_call",
    "browser_session_end": "user_browser_session_end",
}

_MANAGED_BROWSER_REMOTE_NAMES = frozenset(
    {
        "browser_click",
        "browser_close",
        "browser_console_messages",
        "browser_drag",
        "browser_drop",
        "browser_evaluate",
        "browser_file_upload",
        "browser_fill_form",
        "browser_handle_dialog",
        "browser_hover",
        "browser_navigate",
        "browser_navigate_back",
        "browser_network_request",
        "browser_network_requests",
        "browser_press_key",
        "browser_resize",
        "browser_run_code",
        "browser_select_option",
        "browser_snapshot",
        "browser_tabs",
        "browser_take_screenshot",
        "browser_type",
        "browser_wait_for",
    }
)

_BROWSER_TOOL_REFERENCE_RE = re.compile(r"\bbrowser_[A-Za-z0-9_]+\b")


def public_browser_tool_name(server_name: str, remote_name: str) -> str:
    """Return the backend name used by Browser REPL for a browser MCP tool."""
    if server_name == "playwright" and remote_name.startswith("browser_"):
        return f"{MANAGED_BROWSER_PREFIX}{remote_name.removeprefix('browser_')}"
    if server_name == "browser-gateway":
        public_name = _USER_BROWSER_REMOTE_TO_PUBLIC.get(remote_name)
        if public_name is not None:
            return public_name
        if remote_name.startswith("browser_connector_"):
            return f"{USER_BROWSER_PREFIX}{remote_name.removeprefix('browser_connector_')}"
        if remote_name.startswith("browser_"):
            return f"{USER_BROWSER_PREFIX}{remote_name.removeprefix('browser_')}"
    return remote_name


def public_browser_tool_description(
    server_name: str,
    description: str,
    remote_name: str = "",
) -> str:
    """Add controller context and rewrite transport names for public tools."""
    if server_name == "playwright":
        rewritten = public_browser_tool_text(server_name, description)
        if remote_name == "browser_take_screenshot":
            rewritten += (
                " Screenshots are saved in the session cwd (also when filename is omitted); "
                "use the returned absolute path for local reads. Inline MCP previews can be "
                "downsampled independently of the saved original. Never change page dimensions "
                "or layout to satisfy a preview's pixel limit; inspect the saved image or a crop."
            )
        return (
            "Managed browser controlled through Playwright MCP. "
            f"{rewritten}"
        ).strip()
    if server_name != "browser-gateway":
        return description
    rewritten = public_browser_tool_text(server_name, description)
    return (
        "User browser controlled through browser-gateway. "
        f"{rewritten}"
    ).strip()


def public_browser_tool_text(server_name: str, text: str) -> str:
    """Rewrite browser transport identifiers in model-facing text."""
    if server_name not in {"playwright", "browser-gateway"}:
        return text

    def replace(match: re.Match[str]) -> str:
        remote_name = match.group(0)
        if server_name == "playwright" or remote_name in _MANAGED_BROWSER_REMOTE_NAMES:
            return f"{MANAGED_BROWSER_PREFIX}{remote_name.removeprefix('browser_')}"
        return public_browser_tool_name(server_name, remote_name)

    return _BROWSER_TOOL_REFERENCE_RE.sub(replace, text)


def browser_tool_fixed_arguments(
    server_name: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Return transport arguments that enforce the public browser boundary."""
    properties = parameters.get("properties")
    if (
        server_name == "browser-gateway"
        and isinstance(properties, dict)
        and "source_preference" in properties
    ):
        return {"source_preference": "browser_connector"}
    return {}


def public_browser_tool_parameters(
    parameters: dict[str, Any],
    fixed_arguments: dict[str, Any],
) -> dict[str, Any]:
    """Hide transport-fixed arguments from the model-facing schema."""
    if not fixed_arguments:
        return parameters
    public_parameters = copy.deepcopy(parameters)
    properties = public_parameters.get("properties")
    if isinstance(properties, dict):
        for name in fixed_arguments:
            properties.pop(name, None)
    required = public_parameters.get("required")
    if isinstance(required, list):
        public_parameters["required"] = [
            name for name in required if name not in fixed_arguments
        ]
    return public_parameters


def is_browser_tool_name(name: str) -> bool:
    """Return whether a name belongs to either Browser backend."""
    return name.startswith((MANAGED_BROWSER_PREFIX, USER_BROWSER_PREFIX))
