"""One-shot management transport shared by desktop hosts and command-line users.

The selected runtime/profile owns this process. It does not require LLM
configuration, an ACP connection, or an active Agent session.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any

MAX_REQUEST_BYTES = 256 * 1024
PUBLIC_METHODS = frozenset({
    "plugins.list", "plugins.install", "plugins.ui", "config.get", "config.validate",
    "config.save", "instance.set_enabled", "profile.prepare_apply",
    "profile.fail_apply", "profile.rollback", "profile.import", "profile.export",
    "profile.preflight", "activation.status",
})


def handle_request(request: Any) -> dict[str, Any]:
    from .plugins.skill_sources import plugin_settings_service

    if not isinstance(request, dict) or set(request) != {"method", "params"}:
        raise ValueError("Expected a method and params request")
    method, params = request["method"], request["params"]
    if method not in PUBLIC_METHODS or not isinstance(params, dict):
        raise ValueError("Unsupported plugin management request")
    service = plugin_settings_service()
    result = service.dispatch(method, params)
    if method in {"profile.prepare_apply", "profile.rollback"}:
        from types import SimpleNamespace
        from .plugins.locations import plugin_settings
        from .plugins.skill_sources import preflight_plugin_candidate
        status = service.activation_status(activation_id=result["activation_id"])
        activation = status.get("activation") or {}
        if activation.get("status") == "prepared":
            preflight_plugin_candidate(SimpleNamespace(plugin_platform=plugin_settings()), result["activation_id"])
    return result


def main() -> int:
    protocol_stdout = sys.stdout
    try:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw = stream.read(MAX_REQUEST_BYTES + 1)
        size = len(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
        if size > MAX_REQUEST_BYTES:
            raise ValueError("Plugin management request exceeds 256 KiB")
        request = json.loads(raw)
        with contextlib.redirect_stdout(sys.stderr):
            result = handle_request(request)
        response = {"ok": True, "result": result}
    except Exception as error:
        response = {"ok": False, "error": {
            "code": getattr(error, "code", "INVALID_REQUEST"),
            "message": str(error), "details": getattr(error, "details", {}),
        }}
    protocol_stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    protocol_stdout.flush()
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
