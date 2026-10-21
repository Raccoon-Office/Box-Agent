from __future__ import annotations

import socket
from types import SimpleNamespace

import httpx
import pytest

from box_agent.mcp_servers import web_extract


class _SummaryLLM:
    def __init__(self, model: str = "sn-sensenova-6-8-flash-lite") -> None:
        self.model = model
        self.selected: tuple[str, int] | None = None

    def for_model(self, model: str, *, max_output_tokens: int):
        self.selected = (model, max_output_tokens)
        return self

    async def generate(self, **kwargs):
        return SimpleNamespace(content="Summary")


def _address_info(address: str, port: int) -> tuple:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    socket_address = (
        (address, port, 0, 0)
        if family == socket.AF_INET6
        else (address, port)
    )
    return (family, socket.SOCK_STREAM, 6, "", socket_address)


def test_resolver_rejects_nonstandard_loopback_notation(monkeypatch) -> None:
    def fake_getaddrinfo(hostname, port, **kwargs):
        assert hostname == "2130706433"
        return [_address_info("127.0.0.1", port)]

    monkeypatch.setattr(web_extract.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="Private or local network"):
        web_extract._resolve_public_addresses("2130706433", 80)


def test_resolver_rejects_mixed_public_and_private_answers(monkeypatch) -> None:
    monkeypatch.setattr(
        web_extract.socket,
        "getaddrinfo",
        lambda hostname, port, **kwargs: [
            _address_info("93.184.216.34", port),
            _address_info("169.254.169.254", port),
        ],
    )

    with pytest.raises(ValueError, match="Private or local network"):
        web_extract._resolve_public_addresses("mixed.example", 443)


@pytest.mark.parametrize(
    "hostname",
    [
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "::",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
    ],
)
def test_resolver_rejects_non_public_ip_literals(hostname: str) -> None:
    with pytest.raises(ValueError, match="Private or local network"):
        web_extract._resolve_public_addresses(hostname, 80)


@pytest.mark.asyncio
async def test_tool_rejects_private_literal_before_fetch(monkeypatch) -> None:
    tool = web_extract.WebExtractTool(llm=None)

    async def unexpected_fetch(url: str):
        raise AssertionError("private URL must be rejected before fetch")

    monkeypatch.setattr(tool, "_fetch", unexpected_fetch)

    result = await tool.execute("http://127.0.0.1/internal")

    assert result.success is False
    assert result.error == "Private or local network URLs are not allowed"


@pytest.mark.asyncio
async def test_tool_returns_short_extracted_page_without_summarization(monkeypatch) -> None:
    tool = web_extract.WebExtractTool(llm=None)

    async def fake_fetch(url: str):
        return web_extract._FetchedPage(
            content="Verified page content",
            final_url="https://example.com/final",
        )

    monkeypatch.setattr(tool, "_fetch", fake_fetch)

    result = await tool.execute("https://example.com/start")

    assert result.success is True
    assert result.content == (
        "[URL]: https://example.com/start\n"
        "[Content]:\nVerified page content"
    )
    assert result.raw_output == {
        "type": "web_extract",
        "url": "https://example.com/start",
        "final_url": "https://example.com/final",
        "summarized": False,
        "content_truncated": False,
        "model": None,
        "original_chars": 21,
        "returned_chars": 21,
        "from_cache": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("max_output_tokens", [2_048, 8_192, 63_999, 100_000])
async def test_summary_caps_injected_session_output_tokens(max_output_tokens: int) -> None:
    llm = _SummaryLLM()
    tool = web_extract.WebExtractTool(llm=llm)

    summary, error = await tool._summarize(
        "Long page content",
        "https://example.com/page",
        requested_model="",
        requested_max_output_tokens=max_output_tokens,
    )

    assert error is None
    assert summary == "Summary"
    assert llm.selected == (
        "sn-sensenova-6-8-flash-lite",
        min(max_output_tokens, 8_192),
    )


@pytest.mark.asyncio
async def test_long_summary_is_returned_without_truncation(monkeypatch) -> None:
    tool = web_extract.WebExtractTool(llm=_SummaryLLM())

    async def fake_fetch(url: str):
        return web_extract._FetchedPage(
            content="p" * 5_001,
            final_url=url,
        )

    async def fake_summarize(*args, **kwargs):
        return "s" * 6_000, None

    monkeypatch.setattr(tool, "_fetch", fake_fetch)
    monkeypatch.setattr(tool, "_summarize", fake_summarize)

    result = await tool.execute("https://example.com/long")

    assert result.success is True
    assert result.content.endswith("s" * 6_000)
    assert "truncated" not in result.content.lower()
    assert result.raw_output["summarized"] is True
    assert result.raw_output["content_truncated"] is False
    assert result.raw_output["returned_chars"] == 6_000


@pytest.mark.asyncio
async def test_summary_failure_returns_tool_error(monkeypatch) -> None:
    tool = web_extract.WebExtractTool(llm=_SummaryLLM())

    async def fake_fetch(url: str):
        return web_extract._FetchedPage(
            content="p" * 5_001,
            final_url=url,
        )

    async def fake_summarize(*args, **kwargs):
        return None, "provider rejected max_tokens"

    monkeypatch.setattr(tool, "_fetch", fake_fetch)
    monkeypatch.setattr(tool, "_summarize", fake_summarize)

    result = await tool.execute("https://example.com/long")

    assert result.success is False
    assert result.content == ""
    assert result.error == "provider rejected max_tokens"
    assert result.raw_output is None


@pytest.mark.asyncio
async def test_public_request_pins_address_and_preserves_authority(monkeypatch) -> None:
    monkeypatch.setattr(
        web_extract,
        "_resolve_public_addresses",
        lambda hostname, port: ("93.184.216.34",),
    )

    request = await web_extract._build_public_request(
        "https://example.com:8443/path?item=1"
    )

    assert str(request.url) == "https://93.184.216.34:8443/path?item=1"
    assert request.headers["host"] == "example.com:8443"
    assert request.extensions["sni_hostname"] == "example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
async def test_redirect_target_is_resolved_before_second_request(
    monkeypatch,
    status_code: int,
) -> None:
    def fake_resolve(hostname: str, port: int) -> tuple[str, ...]:
        if hostname == "public.example":
            return ("93.184.216.34",)
        raise ValueError("Private or local network URLs are not allowed")

    monkeypatch.setattr(web_extract, "_resolve_public_addresses", fake_resolve)
    requested_urls: list[str] = []

    async def fake_send(url: str, timeout: httpx.Timeout) -> httpx.Response:
        await web_extract._build_public_request(url)
        requested_urls.append(url)
        return httpx.Response(
            status_code,
            headers={"location": "http://internal.example/secret"},
        )

    monkeypatch.setattr(web_extract, "_send_public_request", fake_send)

    with pytest.raises(ValueError, match="Private or local network"):
        await web_extract._get_following_safe_redirects(
            "https://public.example/start",
            httpx.Timeout(10.0),
        )

    assert requested_urls == ["https://public.example/start"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "gopher://example.com/resource",
        "data:text/plain,secret",
        "ws://example.com/socket",
        "wss://example.com/socket",
    ],
)
async def test_redirect_rejects_non_http_schemes(monkeypatch, location: str) -> None:
    requested_urls: list[str] = []

    async def fake_send(url: str, timeout: httpx.Timeout) -> httpx.Response:
        await web_extract._build_public_request(url)
        requested_urls.append(url)
        return httpx.Response(302, headers={"location": location})

    monkeypatch.setattr(
        web_extract,
        "_resolve_public_addresses",
        lambda hostname, port: ("93.184.216.34",),
    )
    monkeypatch.setattr(web_extract, "_send_public_request", fake_send)

    with pytest.raises(ValueError, match="absolute HTTP or HTTPS"):
        await web_extract._get_following_safe_redirects(
            "https://public.example/start",
            httpx.Timeout(10.0),
        )

    assert requested_urls == ["https://public.example/start"]


@pytest.mark.asyncio
async def test_meta_refresh_reuses_safe_redirect_path(monkeypatch) -> None:
    requested_urls: list[str] = []

    async def fake_get(url, timeout):
        requested_urls.append(url)
        if len(requested_urls) == 1:
            return (
                httpx.Response(
                    200,
                    text=(
                        '<meta http-equiv="refresh" '
                        'content="0; url=http://internal.example/secret">'
                    ),
                ),
                url,
            )
        raise ValueError("Private or local network URLs are not allowed")

    monkeypatch.setattr(web_extract, "_get_following_safe_redirects", fake_get)

    with pytest.raises(ValueError, match="Private or local network"):
        await web_extract.WebExtractTool(llm=None)._fetch(
            "https://public.example/start"
        )

    assert requested_urls == [
        "https://public.example/start",
        "http://internal.example/secret",
    ]
