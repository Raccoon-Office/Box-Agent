"""Fetch public web pages for the bundled web_extract MCP server."""

from __future__ import annotations

import asyncio
import ipaddress
import random
import re
import socket
from collections import OrderedDict
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from box_agent.llm.buffered_stream import generate_buffered_stream
from box_agent.schema import Message
from box_agent.tools.base import Tool, ToolResult


_SUMMARY_THRESHOLD_CHARS = 5_000
_FETCH_TIMEOUT_SECONDS = 60.0
_SUMMARY_TIMEOUT_SECONDS = 180.0
_BLOCKED_DOMAINS = ("youtube.com", "zhihu.com")
_MAX_REDIRECTS = 10
_CACHE_SIZE = 128
_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
_REFERERS = (
    "https://www.google.com/",
    "https://www.bing.com/",
    "https://duckduckgo.com/",
    "https://www.baidu.com/",
    "",
)
_META_REFRESH_RE = re.compile(
    r'<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*'
    r'content=["\']?\d+;\s*url=([^"\'\s>]+)',
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _FetchedPage:
    content: str
    final_url: str
    from_cache: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    connect_url: httpx.URL
    host_header: str
    sni_hostname: str


class WebExtractTool(Tool):
    """Extract one public URL, summarizing pages larger than 5,000 characters."""

    def __init__(self, llm: Any | None) -> None:
        self.llm = llm
        self._cache: OrderedDict[str, _FetchedPage] = OrderedDict()

    @property
    def name(self) -> str:
        return "web_extract"

    @property
    def description(self) -> str:
        return (
            "Fetch and extract text from one public web page URL. Pages up to "
            "5,000 characters are returned directly; longer pages are summarized "
            "with the current LLM, or with the optional requested model. This uses "
            "direct HTTP fetching and does not execute JavaScript."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Public HTTP or HTTPS URL to extract.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model name for summarization. If omitted, use the "
                        "current Box-Agent model."
                    ),
                },
                "max_output_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Optional output-token capability for the selected model. "
                        "The Box-Agent host supplies this from the current session binding."
                    ),
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        url: str,
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> ToolResult:
        normalized_url, validation_error = _validate_public_url(url)
        if validation_error:
            return ToolResult(success=False, error=validation_error)

        try:
            result = await asyncio.wait_for(
                self._fetch(normalized_url),
                timeout=_FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"Web extraction timed out after {_FETCH_TIMEOUT_SECONDS:.0f}s",
            )
        except Exception as exc:  # pragma: no cover - transport errors vary
            return ToolResult(success=False, error=f"Failed to fetch URL: {exc}")

        content = result.content.strip()
        if not content:
            return ToolResult(
                success=False,
                error="Fetched page contained no extractable text",
            )

        original_chars = len(content)
        effective_model = (model or "").strip() or _current_model_name(self.llm)
        summarized = False

        if original_chars > _SUMMARY_THRESHOLD_CHARS:
            summary, summary_error = await self._summarize(
                content,
                normalized_url,
                requested_model=(model or "").strip(),
                requested_max_output_tokens=max_output_tokens,
            )
            if not summary:
                return ToolResult(
                    success=False,
                    error=summary_error or "Summarization failed",
                )
            content = summary
            summarized = True

        metadata = {
            "type": "web_extract",
            "url": normalized_url,
            "final_url": result.final_url,
            "summarized": summarized,
            "content_truncated": False,
            "model": effective_model if summarized else None,
            "original_chars": original_chars,
            "returned_chars": len(content),
            "from_cache": result.from_cache,
        }

        return ToolResult(
            success=True,
            content=f"[URL]: {normalized_url}\n[Content]:\n{content}",
            raw_output=metadata,
        )

    async def _fetch(self, url: str) -> _FetchedPage:
        cached = self._cache.get(url)
        if cached is not None:
            self._cache.move_to_end(url)
            return _FetchedPage(
                content=cached.content,
                final_url=cached.final_url,
                from_cache=True,
            )

        timeout = httpx.Timeout(
            _FETCH_TIMEOUT_SECONDS,
            connect=10.0,
            read=20.0,
            write=15.0,
            pool=5.0,
        )
        response, final_url = await _get_following_safe_redirects(url, timeout)
        html = response.text

        meta_refresh = _META_REFRESH_RE.search(html)
        if meta_refresh:
            redirect_url = urljoin(final_url, meta_refresh.group(1))
            response, final_url = await _get_following_safe_redirects(
                redirect_url,
                timeout,
            )
            html = response.text

        content = _extract_simple_text(html)
        if not content:
            raise ValueError("Fetched page contained no extractable text")

        page = _FetchedPage(content=content, final_url=final_url)
        self._cache[url] = page
        self._cache.move_to_end(url)
        while len(self._cache) > _CACHE_SIZE:
            self._cache.popitem(last=False)
        return page

    async def _summarize(
        self,
        content: str,
        url: str,
        *,
        requested_model: str,
        requested_max_output_tokens: int | None,
    ) -> tuple[str | None, str | None]:
        if self.llm is None:
            return None, "No LLM is available for summarization"

        summary_llm = self.llm
        summary_model = requested_model or _current_model_name(self.llm)
        summary_max_output_tokens = _positive_int(requested_max_output_tokens)
        if summary_max_output_tokens is None:
            summary_max_output_tokens = _positive_int(
                getattr(self.llm, "max_output_tokens", None)
            )
        if summary_model:
            for_model = getattr(self.llm, "for_model", None)
            if not callable(for_model):
                return None, f"The current LLM cannot select model '{summary_model}'"
            try:
                summary_llm = for_model(
                    summary_model,
                    max_output_tokens=summary_max_output_tokens,
                )
            except Exception as exc:
                return None, f"Could not select model '{summary_model}': {exc}"

        messages = [
            Message(
                role="system",
                content=(
                    "You are an expert content analyst. Summarize the supplied web "
                    "page as concise, well-structured markdown. Preserve important "
                    "facts, figures, quotes, code snippets, and actionable details. "
                    "Do not add facts that are not in the page."
                ),
            ),
            Message(
                role="user",
                content=(
                    f"Source URL: {url}\n\n"
                    "Create a comprehensive markdown summary of this page:\n\n"
                    f"{content}"
                ),
            ),
        ]

        try:
            response = await generate_buffered_stream(
                summary_llm,
                messages=messages,
                tools=None,
                thinking_enabled=False,
                call_kind="utility",
                idle_timeout=_SUMMARY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return None, f"Summarization stream was idle for {_SUMMARY_TIMEOUT_SECONDS:.0f}s"
        except Exception as exc:  # pragma: no cover - provider errors vary
            return None, f"Summarization failed: {exc}"

        summary = (response.content or "").strip()
        if not summary:
            return None, "Summarization returned empty content"
        return summary, None


def _validate_public_url(url: str) -> tuple[str, str | None]:
    normalized = (url or "").strip()
    if not normalized:
        return normalized, "url must not be empty"

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return normalized, "url must be an absolute HTTP or HTTPS URL"
    if parsed.username or parsed.password:
        return normalized, "URLs containing embedded credentials are not allowed"

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return normalized, "Private or local network URLs are not allowed"
    for domain in _BLOCKED_DOMAINS:
        if hostname == domain or hostname.endswith(f".{domain}"):
            return normalized, f"Domain is blocked: {domain}"

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return normalized, "Private or local network URLs are not allowed"

    return normalized, None


async def _get_following_safe_redirects(
    url: str,
    timeout: httpx.Timeout,
) -> tuple[httpx.Response, str]:
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        response = await _send_public_request(current_url, timeout)
        if not response.is_redirect:
            response.raise_for_status()
            return response, current_url

        location = response.headers.get("location", "").strip()
        if not location:
            response.raise_for_status()
            return response, current_url
        current_url = urljoin(current_url, location)
    raise ValueError(f"Too many redirects (maximum {_MAX_REDIRECTS})")


async def _send_public_request(
    url: str,
    timeout: httpx.Timeout,
) -> httpx.Response:
    request = await _build_public_request(url)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        http2=True,
        trust_env=False,
    ) as client:
        return await client.send(request)


async def _build_public_request(url: str) -> httpx.Request:
    target = await _resolve_public_target(url)
    headers = _browser_headers()
    headers["Host"] = target.host_header
    return httpx.Request(
        "GET",
        target.connect_url,
        headers=headers,
        extensions={"sni_hostname": target.sni_hostname},
    )


async def _resolve_public_target(url: str) -> _ResolvedTarget:
    normalized_url, validation_error = _validate_public_url(url)
    if validation_error:
        raise ValueError(validation_error)

    parsed_url = httpx.URL(normalized_url)
    hostname = parsed_url.raw_host.decode("ascii")
    port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
    addresses = await asyncio.to_thread(_resolve_public_addresses, hostname, port)
    connect_url = parsed_url.copy_with(host=addresses[0])
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    if parsed_url.port is not None:
        host_header = f"{host_header}:{parsed_url.port}"
    return _ResolvedTarget(
        connect_url=connect_url,
        host_header=host_header,
        sni_hostname=hostname,
    )


def _resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        literal_address = ipaddress.ip_address(hostname.rstrip("."))
    except ValueError:
        literal_address = None

    if literal_address is not None:
        addresses = [literal_address]
    else:
        try:
            address_info = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve URL hostname: {hostname}") from exc

        addresses = []
        for family, _, _, _, socket_address in address_info:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            try:
                address = ipaddress.ip_address(socket_address[0])
            except ValueError as exc:
                raise ValueError(
                    f"Hostname resolved to an invalid address: {hostname}"
                ) from exc
            if address not in addresses:
                addresses.append(address)

    if not addresses:
        raise ValueError(f"Could not resolve URL hostname: {hostname}")
    if any(not _is_public_unicast_address(address) for address in addresses):
        raise ValueError("Private or local network URLs are not allowed")
    return tuple(str(address) for address in addresses)


def _is_public_unicast_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return address.is_global and not address.is_multicast


def _browser_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": random.choice(_REFERERS),
    }


def _extract_simple_text(html: str) -> str:
    if not html or not html.strip():
        return ""
    text = re.sub(
        r"<script[^>]*>.*?</script>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"<style[^>]*>.*?</style>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _current_model_name(llm: Any | None) -> str | None:
    model = str(getattr(llm, "model", "") or "").strip() if llm is not None else ""
    return model or None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
