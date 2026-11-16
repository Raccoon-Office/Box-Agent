"""Capability-scoped diagnostic payload omission; model/session data stay intact."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re
from collections.abc import Mapping
from typing import Any


_PRIVATE = ContextVar("private_diagnostic_payload", default=False)

_DIAGNOSTIC_FIELDS = {
    "session_id", "turn_id", "tool_call_id", "tool_name", "tool_id", "server_name",
    "step", "success", "user_visible", "duration_ms", "elapsed_ms", "cancelled",
}
_ERROR_LIMIT = 8192
_CREDENTIAL = re.compile(
    r"(?i)(\b(?:[\w-]*(?:token|password|secret)|api[_-]?key|authorization|cookie)"
    r"[\"']?\s*[:=]\s*)(?:[\"'][^\"']*[\"']|(?:Bearer|Basic)\s+[^\s]+|[^\s]+)"
)


def private_tool_diagnostics(event: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Keep command failure details without retaining the surrounding payload."""
    result = {
        key: value for key, value in fields.items()
        if key in _DIAGNOSTIC_FIELDS and isinstance(value, (str, int, float, bool))
    }
    result["payload_redacted"] = True
    if fields.get("tool_name") not in {"bash", "bash_output"}:
        return result
    if event not in {"tool.response", "tool/fail"}:
        return result
    if event == "tool.response" and fields.get("success") is not False:
        return result
    exit_code = fields.get("exit_code")
    if type(exit_code) is int:
        result["exit_code"] = exit_code
    error = fields.get("error")
    if not isinstance(error, str):
        return result
    match = re.match(r"Command failed with exit code (-?\d+)\b", error)
    if "exit_code" not in result and match:
        result["exit_code"] = int(match.group(1))
    error = re.sub(r"(?im)\b(?:set-cookie|cookie)\s*:[^\r\n]*", "cookie: <redacted>", error)
    error = re.sub(r"(?i)\b(Bearer|Basic)\s+\S+", r"\1 <redacted>", error)
    error = re.sub(r"(?s)-----BEGIN .*?KEY-----.*?-----END .*?KEY-----", "<redacted>", error)
    error = re.sub(r"(https?://)[^\s]+@", r"\1<redacted>@", error)
    error = _CREDENTIAL.sub(r"\1<redacted>", error)
    if len(error) > _ERROR_LIMIT:
        error = (
            error[:_ERROR_LIMIT // 2] + "\n...[error truncated]...\n"
            + error[-_ERROR_LIMIT // 2:]
        )
    result["error"] = error
    return result


def private_payloads() -> bool:
    return _PRIVATE.get()


class _SDKFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not private_payloads()


_FILTER = _SDKFilter()


@contextmanager
def private_payload_scope():
    """Suppress unsafe SDK diagnostics only inside a capability's async scope."""
    # Core, Hook, Bash and MCP adapter diagnostics can contain raw payloads.
    names = {
        "box_agent.core", "box_agent.hooks", "box_agent.tools.bash_tool", "box_agent.tools.mcp_result_hooks",
        "httpx", "httpcore.connection",
        "httpcore.http11", "httpcore.http2", "httpcore.proxy",
        "openai._base_client", "anthropic._base_client",
    }
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.startswith(("openai.", "anthropic.", "httpcore.", "box_agent.llm.")):
            names.add(name)
    for name in names:
        logging.getLogger(name).addFilter(_FILTER)
    token = _PRIVATE.set(True)
    try:
        yield
    finally:
        _PRIVATE.reset(token)
