"""Bounded, identity-pinned transport to the trusted desktop MemoryHost."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID

import httpx


_KIB = 1024
_BODY_LIMIT = 512 * _KIB
_CORE_LIMIT = 32 * 1024 * _KIB * 6 + 64 * _KIB
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43,256}\Z")
_ERROR_CODES = frozenset({
    "AUTH_REQUIRED", "IDENTITY_CHANGED", "SESSION_REVOKED", "TURN_REVOKED",
    "MEMORY_DISABLED", "MEMORY_OFFLINE", "MEMORY_NOT_READY", "MEMORY_READ_ONLY",
    "CORE_READ_REQUIRED", "CORE_CONTEXT_TOO_LARGE", "MEMORY_REVISION_CONFLICT",
    "INVALID_ARGUMENT", "NOT_FOUND", "BUDGET_EXCEEDED", "RATE_LIMITED",
    "RECONCILIATION_REQUIRED", "RESULT_UNKNOWN", "INVALID_EXPERIENCE_PLAN", "INVALID_OPERATION",
    "MEMORY_EXPERIENCE_BUSY", "MEMORY_EXPERIENCE_LEASE_EXPIRED", "MEMORY_EXPERIENCE_FROZEN",
    "MEMORY_EXPERIENCE_CONFLICT", "MEMORY_EXPERIENCE_NOT_FOUND", "MEMORY_SESSION_RETIRED", "MODEL_CONTEXT_TOO_LARGE",
})


@dataclass(frozen=True, slots=True)
class _Operation:
    timeout: float = 3.0
    mutation: bool = False
    control: bool = False
    request_limit: int = _BODY_LIMIT
    response_limit: int = _BODY_LIMIT


# The caller chooses a capability, never an arbitrary URL or timeout increase.
_OPERATIONS = {
    "context": _Operation(timeout=0.8, response_limit=256 * _KIB),
    "turn/close": _Operation(mutation=True),
    "search": _Operation(),
    "managed/read": _Operation(),
    "core/read": _Operation(response_limit=_CORE_LIMIT),
    "core/mutate": _Operation(timeout=15, mutation=True, request_limit=_CORE_LIMIT),
    "qa": _Operation(timeout=15, mutation=True),
    "qa/evidence": _Operation(timeout=15, mutation=True, control=True),
    "experience/job": _Operation(timeout=15, mutation=True, control=True),
    "experience/search": _Operation(),
    "experience/entities/get": _Operation(),
    "experience/entities/save": _Operation(timeout=15, mutation=True),
    "experience/facts/get": _Operation(),
    "experience/facts/save": _Operation(timeout=15, mutation=True),
    "experience/observations/get": _Operation(),
    "experience/observations/save": _Operation(timeout=15, mutation=True),
    "experience/facts/invalidate": _Operation(timeout=15, mutation=True),
    "experience/observations/search": _Operation(),
    "visibility/commit": _Operation(timeout=3, mutation=True, control=True),
    "visibility/revoke": _Operation(timeout=3, mutation=True, control=True),
}


class MemoryProxyError(RuntimeError):
    """Safe public failure; never includes a request, address or remote message."""

    def __init__(self, code: str, *, outcome: str = "not_sent", status: int | None = None) -> None:
        super().__init__(code)
        self.code, self.outcome, self.status = code, outcome, status


@dataclass(frozen=True, slots=True, repr=False)
class _Bootstrap:
    base_url: str
    token: str
    control_token: str
    identity_epoch: str
    profile_id: str


def _uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _decode(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json_constant")))


def _read_private_posix_file(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(path.parent, flags | os.O_DIRECTORY)
    try:
        owner = os.getuid()
        parent = os.fstat(directory)
        if parent.st_uid != owner or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError("invalid_bootstrap_directory")
        descriptor = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 64 * _KIB):
                raise ValueError("invalid_bootstrap_file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                body = stream.read(64 * _KIB + 1)
            if len(body) > 64 * _KIB:
                raise ValueError("invalid_bootstrap_size")
            return body
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _read_private_file(path: Path) -> bytes:
    if os.name == "nt":
        from .windows_files import read_private_bootstrap

        return read_private_bootstrap(path, 64 * _KIB)
    return _read_private_posix_file(path)


def _read_bootstrap(path: Path, expected_epoch: str) -> _Bootstrap:
    """Platform-private bytes, followed by the same protocol and identity checks."""
    try:
        if not path.is_absolute() or not _uuid(expected_epoch):
            raise ValueError("invalid_bootstrap")
        body = _read_private_file(path)
        data = _decode(body)
        fields = {"protocol", "base_url", "token", "control_token", "identity_epoch", "profile_id"}
        if not isinstance(data, dict) or set(data) != fields or type(data["protocol"]) is not int or data["protocol"] != 2:
            raise ValueError("invalid_bootstrap_schema")
        if data["identity_epoch"] != expected_epoch or not _uuid(data["profile_id"]):
            raise ValueError("invalid_bootstrap_identity")
        if (not isinstance(data["token"], str) or not _TOKEN.fullmatch(data["token"])
                or not isinstance(data["control_token"], str) or not _TOKEN.fullmatch(data["control_token"])
                or data["token"] == data["control_token"]):
            raise ValueError("invalid_bootstrap_token")
        url = urlsplit(data["base_url"])
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                or url.netloc != f"127.0.0.1:{url.port}" or url.path != "/memory/v2"
                or data["base_url"] != f"http://127.0.0.1:{url.port}/memory/v2"
                or url.query or url.fragment or url.username or url.password):
            raise ValueError("invalid_bootstrap_origin")
        return _Bootstrap(*(data[name] for name in ("base_url", "token", "control_token", "identity_epoch", "profile_id")))
    except Exception:
        raise MemoryProxyError("MEMORY_BOOTSTRAP_UNAVAILABLE") from None


class _PrivateHTTPFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Reuse the memory capability's request-local filter, including httpcore
        # DEBUG connection/response lines which may include private headers.
        from ..memory import _MEMORY_HTTP_REQUEST

        return not _MEMORY_HTTP_REQUEST.get()


_HTTP_FILTER = _PrivateHTTPFilter()


@dataclass(slots=True, repr=False)
class MemoryHostTransport:
    """No I/O at construction; pin the first validated bootstrap for this lifecycle."""

    bootstrap_file: Path = field(repr=False)
    identity_epoch: str
    on_revoked: Callable[[], None] | None = field(default=None, repr=False)
    _bootstrap: _Bootstrap | None = field(default=None, init=False, repr=False)
    _bootstrap_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def _credentials(self) -> _Bootstrap:
        async with self._bootstrap_lock:
            if self._bootstrap is None:
                loaded = await asyncio.to_thread(_read_bootstrap, self.bootstrap_file, self.identity_epoch)
                if self._closed:
                    raise MemoryProxyError("SESSION_REVOKED")
                self._bootstrap = loaded
            return self._bootstrap

    def close(self) -> None:
        """Revoke this local transport; outstanding replies cannot be consumed."""
        self._closed = True
        self._bootstrap = None

    async def request(self, operation: str, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        spec = _OPERATIONS.get(operation) if isinstance(operation, str) else None
        if spec is None:
            raise MemoryProxyError("INVALID_OPERATION")
        if self._closed:
            raise MemoryProxyError("SESSION_REVOKED")
        try:
            if not isinstance(payload, dict):
                raise ValueError("invalid_payload")
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise MemoryProxyError("INVALID_ARGUMENT") from None
        if len(body) > spec.request_limit:
            raise MemoryProxyError("BUDGET_EXCEEDED")
        if timeout is not None and (type(timeout) not in {int, float} or not 0 < timeout <= spec.timeout):
            raise MemoryProxyError("INVALID_ARGUMENT")
        sent = False
        status: int | None = None

        async def send() -> dict[str, Any]:
            nonlocal sent, status
            credentials = await self._credentials()
            from ..memory import _MEMORY_HTTP_REQUEST

            for name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy"):
                logging.getLogger(name).addFilter(_HTTP_FILTER)
            private = _MEMORY_HTTP_REQUEST.set(True)
            try:
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=None) as client:
                    if self._closed:
                        raise MemoryProxyError("SESSION_REVOKED")
                    sent = True
                    token = credentials.control_token if spec.control else credentials.token
                    async with client.stream(
                        "POST", credentials.base_url + "/" + operation, content=body,
                        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                 "Accept-Encoding": "identity",
                                 "X-Memory-Protocol": "2", "X-Memory-Identity-Epoch": self.identity_epoch},
                    ) as response:
                        status = response.status_code
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise ValueError("compressed_proxy_response")
                        parts = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(parts) + len(chunk) > spec.response_limit:
                                raise ValueError("response_too_large")
                            parts.extend(chunk)
                if self._closed:
                    raise MemoryProxyError("SESSION_REVOKED", outcome="unknown" if spec.mutation else "not_sent")
                return self._response(bytes(parts), status, credentials, spec)
            finally:
                _MEMORY_HTTP_REQUEST.reset(private)

        try:
            # wait_for also supports the repository's Python 3.10 floor.
            return await asyncio.wait_for(send(), timeout=spec.timeout if timeout is None else timeout)
        except MemoryProxyError as error:
            if self.on_revoked is not None and error.code in {"AUTH_REQUIRED", "IDENTITY_CHANGED", "SESSION_REVOKED", "TURN_REVOKED", "MEMORY_DISABLED"}:
                self.on_revoked()
            raise
        except asyncio.CancelledError:
            # Caller cancellation stays cancellation. No automatic replay is
            # possible here; Host owns a possibly dispatched operation's journal.
            raise
        except Exception:
            raise MemoryProxyError("MEMORY_PROXY_UNAVAILABLE", status=status,
                                   outcome="unknown" if sent and spec.mutation else "not_sent") from None

    def _response(self, body: bytes, status: int, credentials: _Bootstrap, spec: _Operation) -> dict[str, Any]:
        unknown = "unknown" if spec.mutation else "not_sent"
        try:
            envelope = _decode(body)
            if (not isinstance(envelope, dict) or type(envelope.get("protocol")) is not int
                    or envelope["protocol"] != 2 or envelope.get("identity_epoch") != credentials.identity_epoch
                    or type(envelope.get("ok")) is not bool):
                raise ValueError("invalid_envelope")
            if envelope["ok"] and status == 200 and isinstance(envelope.get("data"), dict):
                return envelope["data"]
            error = envelope.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            # A trusted, well-formed rejection proves no mutation only for
            # explicit 4xx errors. 5xx/redirect/malformed bodies stay unknown.
            if not envelope["ok"] and 400 <= status < 500 and code in _ERROR_CODES:
                raise MemoryProxyError(code, status=status,
                                       outcome="unknown" if code in {"RESULT_UNKNOWN", "RECONCILIATION_REQUIRED"} else "rejected")
        except MemoryProxyError:
            raise
        except Exception:
            pass
        raise MemoryProxyError("MEMORY_PROXY_INVALID_RESPONSE", status=status, outcome=unknown)
