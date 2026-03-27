"""Web search tool — lightweight fallback when no MCP search service is configured.

Uses httpx to query public search APIs. Currently supports DuckDuckGo Instant
Answer API (no API key required) and a raw HTTP fetch mode for direct URLs.

When a proper search MCP is configured (e.g. Tavily, Brave, SerpAPI), those
tools take precedence.  This tool serves as a zero-config baseline so the
agent always has *some* web access.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from .base import Tool, ToolResult

_TIMEOUT = 15  # seconds
_MAX_FETCH_BYTES = 50_000  # truncate large pages to keep context small
_USER_AGENT = "BoxAgent/0.3 (https://github.com/Raccoon-Office/Box-Agent)"


class WebSearchTool(Tool):
    """Search the web or fetch a URL."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web for information or fetch a URL.\n\n"
            "Modes:\n"
            "  1. **search** (default): Query DuckDuckGo for a topic. Returns a summary "
            "and related results.\n"
            "  2. **fetch**: Retrieve the text content of a specific URL.\n\n"
            "Use this when you need up-to-date information, weather, news, prices, "
            "documentation, or any knowledge beyond your training data."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query or URL to fetch.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["search", "fetch"],
                    "description": "Operation mode. 'search' queries DuckDuckGo, 'fetch' retrieves a URL. Default: 'search'.",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, mode: str = "search") -> ToolResult:
        if not query or not query.strip():
            return ToolResult(success=False, content="", error="Query cannot be empty.")

        if mode == "fetch":
            return await self._fetch_url(query.strip())
        return await self._search(query.strip())

    # ── DuckDuckGo Instant Answer ────────────────────────────

    async def _search(self, query: str) -> ToolResult:
        """Query DuckDuckGo Instant Answer API + HTML results page."""
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            ) as client:
                # 1) Instant Answer API (structured)
                api_resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                )
                api_resp.raise_for_status()
                data = api_resp.json()

                parts: list[str] = []

                # Abstract (main answer)
                if data.get("Abstract"):
                    parts.append(f"**{data.get('Heading', query)}**")
                    parts.append(data["Abstract"])
                    if data.get("AbstractURL"):
                        parts.append(f"Source: {data['AbstractURL']}")

                # Answer (calculations, conversions, etc.)
                if data.get("Answer"):
                    parts.append(f"Answer: {data['Answer']}")

                # Related topics
                related = data.get("RelatedTopics", [])
                if related:
                    parts.append("\n**Related:**")
                    for item in related[:8]:
                        if isinstance(item, dict) and item.get("Text"):
                            url = item.get("FirstURL", "")
                            text = item["Text"][:200]
                            parts.append(f"- {text}" + (f" ({url})" if url else ""))

                # 2) If API gave nothing useful, fall back to lite HTML
                if not parts:
                    parts.append(f"No instant answer for '{query}'.")
                    parts.append("Tip: try mode='fetch' with a specific URL, or rephrase the query.")

                return ToolResult(success=True, content="\n".join(parts))

        except httpx.HTTPStatusError as e:
            return ToolResult(success=False, content="", error=f"Search HTTP error: {e.response.status_code}")
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Search failed: {e!s}")

    # ── URL fetch ────────────────────────────────────────────

    async def _fetch_url(self, url: str) -> ToolResult:
        """Fetch text content from a URL."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")

                if "json" in content_type:
                    try:
                        text = json.dumps(resp.json(), indent=2, ensure_ascii=False)
                    except Exception:
                        text = resp.text
                elif "html" in content_type:
                    text = self._extract_text_from_html(resp.text)
                else:
                    text = resp.text

                # Truncate
                if len(text) > _MAX_FETCH_BYTES:
                    text = text[:_MAX_FETCH_BYTES] + f"\n\n... [truncated, {len(resp.text)} chars total]"

                return ToolResult(success=True, content=text)

        except httpx.HTTPStatusError as e:
            return ToolResult(success=False, content="", error=f"HTTP {e.response.status_code}: {url}")
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Fetch failed: {e!s}")

    @staticmethod
    def _extract_text_from_html(html: str) -> str:
        """Best-effort HTML to plain text, no extra dependencies."""
        import re

        # Remove script/style blocks
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Strip tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        # Decode common entities
        for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
            text = text.replace(entity, char)
        return text
