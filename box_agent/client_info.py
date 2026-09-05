"""Validated host client metadata for Raccoon backend requests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlparse


_MAX_HEADER_VALUE_LENGTH = 256
_CURRENT_CLIENT_INFO: ContextVar[ClientInfo | None] = ContextVar(
    "box_agent_client_info",
    default=None,
)


def should_attach_client_headers(url: str) -> bool:
    """Limit product metadata to the Raccoon domain and its subdomains."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and (
        hostname == "xiaohuanxiong.com" or hostname.endswith(".xiaohuanxiong.com")
    )


def _clean_header_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or not cleaned.isascii():
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        return ""
    return cleaned[:_MAX_HEADER_VALUE_LENGTH]


def _read_alias(raw: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        cleaned = _clean_header_value(raw.get(key))
        if cleaned:
            return cleaned
    return ""


@dataclass(frozen=True, slots=True)
class ClientInfo:
    name: str = ""
    platform: str = ""
    version: str = ""
    os_version: str = ""
    channel: str = ""
    device_id: str = ""

    @classmethod
    def from_meta(cls, raw: Any) -> ClientInfo | None:
        if not isinstance(raw, Mapping):
            return None
        client_info = cls(
            name=_read_alias(raw, "name"),
            platform=_read_alias(raw, "platform"),
            version=_read_alias(raw, "version"),
            os_version=_read_alias(raw, "os_version", "osVersion"),
            channel=_read_alias(raw, "channel"),
            device_id=_read_alias(raw, "device_id", "deviceId"),
        )
        return client_info if any(
            (
                client_info.name,
                client_info.platform,
                client_info.version,
                client_info.os_version,
                client_info.channel,
                client_info.device_id,
            )
        ) else None

    def headers_for_url(self, url: str) -> dict[str, str]:
        if not should_attach_client_headers(url):
            return {}
        version = _clean_header_value(self.version)
        if re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
            version = "v" + version.removeprefix("v")
        else:
            version = ""
        values = (
            ("x-client-name", "raccoon"),
            ("x-client-platform", _clean_header_value(self.platform) or "unknown"),
            ("x-client-version", version),
            ("x-client-os-version", self.os_version),
            ("x-client-channel", self.channel),
            ("x-client-device-id", self.device_id),
        )
        return {
            header: cleaned
            for header, value in values
            if (cleaned := _clean_header_value(value))
        }


@contextmanager
def scoped_client_info(client_info: ClientInfo | None) -> Iterator[None]:
    token = _CURRENT_CLIENT_INFO.set(client_info)
    try:
        yield
    finally:
        _CURRENT_CLIENT_INFO.reset(token)


def current_client_headers(url: str) -> dict[str, str]:
    client_info = _CURRENT_CLIENT_INFO.get()
    return (client_info or ClientInfo()).headers_for_url(url)
