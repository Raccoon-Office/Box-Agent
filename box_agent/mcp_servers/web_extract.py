"""Fetch public web pages for the bundled web_extract MCP server."""

from __future__ import annotations

import asyncio
import ipaddress
import random
import re
from collections import OrderedDict
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from box_agent.schema import Message
from box_agent.tools.base import Tool, ToolResult


_SUMMARY_THRESHOLD_CHARS = 5_000
_SUMMARY_OUTPUT_CHARS = 5_000
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
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    async def execute(self, url: str, model: str | None = None) -> ToolResult:
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
            return ToolResult(success=False, error="Fetched page contained no extractable text")

        original_chars = len(content)
        effective_model = (model or "").strip() or _current_model_name(self.llm)
        summarized = False
        summary_error: str | None = None

        if original_chars > _SUMMARY_THRESHOLD_CHARS:
            summary, summary_error = await self._summarize(
                content,
                normalized_url,
                requested_model=(model or "").strip(),
            )
            if summary:
                content = _cap_summary(summary)
                summarized = True
            else:
                content = _summary_fallback(content, summary_error)

        metadata = {
            "type": "web_extract",
            "url": normalized_url,
            "final_url": result.final_url,
            "summarized": summarized,
            "model": effective_model if summarized else None,
            "original_chars": original_chars,
            "returned_chars": len(content),
            "from_cache": result.from_cache,
        }
        if summary_error:
            metadata["summary_error"] = summary_error

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
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            http2=True,
            trust_env=True,
        ) as client:
            headers = _browser_headers(url)
            try:
                await client.head(url, headers=headers)
            except httpx.HTTPError:
                pass

            response = await _get_following_safe_redirects(client, url)
            html = response.text
            final_url = str(response.url)

            meta_refresh = _META_REFRESH_RE.search(html)
            if meta_refresh:
                redirect_url = urljoin(final_url, meta_refresh.group(1))
                _, redirect_error = _validate_public_url(redirect_url)
                if redirect_error:
                    raise ValueError(f"Blocked unsafe page redirect: {redirect_error}")
                response = await _get_following_safe_redirects(client, redirect_url)
                html = response.text
                final_url = str(response.url)

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
    ) -> tuple[str | None, str | None]:
        if self.llm is None:
            return None, "No LLM is available for summarization"

        summary_llm = self.llm
        if requested_model:
            for_model = getattr(self.llm, "for_model", None)
            if not callable(for_model):
                return None, f"The current LLM cannot switch to model '{requested_model}'"
            try:
                summary_llm = for_model(requested_model, max_output_tokens=4096)
            except Exception as exc:
                return None, f"Could not select model '{requested_model}': {exc}"

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
            response = await asyncio.wait_for(
                summary_llm.generate(
                    messages=messages,
                    tools=None,
                    thinking_enabled=False,
                    call_kind="utility",
                ),
                timeout=_SUMMARY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return None, f"Summarization timed out after {_SUMMARY_TIMEOUT_SECONDS:.0f}s"
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
    client: httpx.AsyncClient,
    url: str,
) -> httpx.Response:
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        response = await client.get(current_url, headers=_browser_headers(current_url))
        if not response.is_redirect:
            response.raise_for_status()
            return response

        location = response.headers.get("location", "").strip()
        if not location:
            response.raise_for_status()
            return response
        current_url = urljoin(str(response.url), location)
        _, validation_error = _validate_public_url(current_url)
        if validation_error:
            raise ValueError(f"Blocked unsafe HTTP redirect: {validation_error}")
    raise ValueError(f"Too many redirects (maximum {_MAX_REDIRECTS})")


def _browser_headers(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": random.choice(_REFERERS),
        "Host": parsed.netloc,
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


def _cap_summary(summary: str) -> str:
    if len(summary) <= _SUMMARY_OUTPUT_CHARS:
        return summary
    return (
        summary[:_SUMMARY_OUTPUT_CHARS]
        + "\n\n[Summary truncated to 5,000 characters for context management.]"
    )


def _summary_fallback(content: str, error: str | None) -> str:
    fallback = content[:_SUMMARY_OUTPUT_CHARS]
    detail = error or "unknown summarization error"
    return (
        fallback
        + "\n\n[Content truncated to the first 5,000 characters because LLM "
        + f"summarization was unavailable: {detail}]"
    )
