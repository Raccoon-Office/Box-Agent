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
@pytest.mark.parametrize("status_code,headers,html", [
    (200, {}, "<title>环境异常</title><p>当前环境异常，完成验证后即可继续访问。</p><a>去验证</a>"),
    (403, {}, "<h1>环境异常</h1><p>当前环境异常，完成验证后即可继续访问。</p><a>去验证</a>"),
    (200, {}, "<title>Security check</title><p>Please verify you are human to continue.</p>"),
    (200, {}, "<title> Security CHECK </title><p>Please verify\n you are human to continue.</p>"),
    (200, {}, "<title>安全验证</title><p>请完成人机验证后继续访问。</p>"),
    (503, {"cf-mitigated": "challenge"}, "<p>Verifying your browser</p>" + "x" * 6_000),
], ids=["wechat-200", "wechat-403", "english", "whitespace", "chinese", "cloudflare-503"])
async def test_verification_pages_return_browser_handoff_instead_of_article(
    monkeypatch, status_code, headers, html,
) -> None:
    tool = web_extract.WebExtractTool(llm=None)
    url = "https://example.com/article"

    async def fake_send(target, timeout):
        return httpx.Response(status_code, headers=headers, text=html, request=httpx.Request("GET", target))

    async def unexpected_summary(*args, **kwargs):
        raise AssertionError("verification pages must not be summarized")

    monkeypatch.setattr(web_extract, "_send_public_request", fake_send)
    monkeypatch.setattr(tool, "_summarize", unexpected_summary)
    result = await tool.execute(url)

    assert result.success is False
    assert result.content == ""
    assert result.error.startswith("WEB_VERIFICATION_REQUIRED:")
    assert url in result.error
    assert "available browser" in result.error
    assert "take over" in result.error
    assert "confirmation" in result.error
    assert "same browser" in result.error
    assert result.raw_output["status"] == "verification_required"
    assert result.raw_output["url"] == url
    assert result.raw_output["final_url"] == url


@pytest.mark.asyncio
async def test_verification_page_is_not_cached_and_article_can_be_refetched(monkeypatch) -> None:
    tool = web_extract.WebExtractTool(llm=None)
    requested = []

    async def fake_send(url, timeout):
        requested.append(url)
        html = (
            "<title>环境异常</title><p>当前环境异常，完成验证后即可继续访问。</p><a>去验证</a>"
            if len(requested) == 1 else "<article>Actual article body</article>"
        )
        return httpx.Response(200, text=html, request=httpx.Request("GET", url))

    monkeypatch.setattr(web_extract, "_send_public_request", fake_send)
    blocked = await tool.execute("https://example.com/article")
    recovered = await tool.execute("https://example.com/article")
    cached = await tool.execute("https://example.com/article")

    assert blocked.success is False
    assert recovered.success is True
    assert recovered.content.endswith("Actual article body")
    assert recovered.raw_output["from_cache"] is False
    assert cached.success is True
    assert cached.raw_output["from_cache"] is True
    assert len(requested) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_status", [200, 302])
async def test_redirected_verification_preserves_original_browser_target(monkeypatch, redirect_status) -> None:
    original = "https://example.com/article"
    challenge = "https://example.com/verify"

    async def fake_send(url, timeout):
        if url == original:
            return httpx.Response(
                redirect_status, request=httpx.Request("GET", url),
                headers={"location": challenge},
                text='<meta http-equiv="refresh" content="0;url=/verify">',
            )
        assert url == challenge
        return httpx.Response(
            200, request=httpx.Request("GET", url),
            text="<title>环境异常</title><p>当前环境异常，完成验证后即可继续访问。</p><a>去验证</a>",
        )

    monkeypatch.setattr(web_extract, "_send_public_request", fake_send)
    result = await web_extract.WebExtractTool(llm=None).execute(original)

    assert result.success is False
    assert original in result.error
    assert result.raw_output["url"] == original
    assert result.raw_output["final_url"] == challenge


@pytest.mark.asyncio
@pytest.mark.parametrize("html", [
    "<title>验证机制介绍</title><article>人机验证会要求你 verify you are human。</article>",
    "<title>如何解决环境异常</title><article>示例提示：当前环境异常，完成验证后即可继续访问。去验证。</article>",
    "<title>Security check</title><article>This guide explains the banner: Please verify you are human to continue.</article>",
    "<title>环境异常</title><article>本文解释提示：当前环境异常，完成验证后即可继续访问。去验证。</article>",
    "<title>Example article</title><p>Documentation intro.</p><h1>Security check</h1><p>Please verify you are human to continue is example wording.</p>",
    '<title>Normal article</title><article>Article body</article><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>',
], ids=["overview", "wechat-guide", "english-guide", "wechat-title", "article-section", "widget-script"])
async def test_articles_mentioning_verification_remain_readable(monkeypatch, html) -> None:
    async def fake_send(url, timeout):
        return httpx.Response(200, text=html, request=httpx.Request("GET", url))

    monkeypatch.setattr(web_extract, "_send_public_request", fake_send)
    result = await web_extract.WebExtractTool(llm=None).execute("https://example.com/article")

    assert result.success is True
    assert result.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", [
    '<script>const template = "<h1>Security check</h1>";</script>',
    '<style>h1::before {content: "<h1>Security check</h1>"}</style>',
    "<!-- <h1>Security check</h1> -->",
    "<template><h1>Security check</h1></template>",
], ids=["script", "style", "comment", "template"])
async def test_nonvisible_headings_do_not_turn_articles_into_verification_pages(monkeypatch, hidden) -> None:
    async def fake_send(url, timeout):
        return httpx.Response(
            200, request=httpx.Request("GET", url),
            text="<title>Example article</title><p>Please verify you are human to continue.</p>" + hidden,
        )

    monkeypatch.setattr(web_extract, "_send_public_request", fake_send)
    result = await web_extract.WebExtractTool(llm=None).execute("https://example.com/article")

    assert result.success is True
    assert result.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429])
async def test_access_denial_without_verification_keeps_transport_error(monkeypatch, status_code) -> None:
    async def fake_send(url, timeout):
        return httpx.Response(
            status_code, headers={"server": "cloudflare"}, text="<h1>Access denied</h1>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(web_extract, "_send_public_request", fake_send)
    result = await web_extract.WebExtractTool(llm=None).execute("https://example.com/article")

    assert result.success is False
    assert result.error.startswith("Failed to fetch URL:")
    assert "WEB_VERIFICATION_REQUIRED" not in result.error


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
