"""Capability-scoped diagnostic payload omission; model/session data stay intact."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging


_PRIVATE = ContextVar("private_diagnostic_payload", default=False)


def private_payloads() -> bool:
    return _PRIVATE.get()


class _SDKFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not private_payloads()


_FILTER = _SDKFilter()


@contextmanager
def private_payload_scope():
    """Suppress unsafe SDK diagnostics only inside a capability's async scope."""
    names = {"httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy",
             "openai._base_client", "anthropic._base_client"}
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
