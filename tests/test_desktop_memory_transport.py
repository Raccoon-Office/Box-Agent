"""Desktop proxy boundaries using disposable bootstrap files and HTTP transports."""

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import uuid

import httpx
import pytest

from box_agent.desktop_memory import transport as module
from box_agent.desktop_memory.transport import MemoryHostTransport, MemoryProxyError


@pytest.fixture
def bootstrap(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "bootstrap.json"
    data = {"protocol": 2, "base_url": "http://127.0.0.1:49123/memory/v2",
            "token": secrets.token_urlsafe(32), "control_token": secrets.token_urlsafe(32),
            "identity_epoch": str(uuid.uuid4()), "profile_id": str(uuid.uuid4())}

    def write(**updates):
        data.update(updates)
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)

    write()
    return path, data, write


def install_http(monkeypatch, handler):
    client = httpx.AsyncClient
    options = []

    def create(**kwargs):
        options.append(kwargs)
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", create)
    return options


def envelope(data, **updates):
    return {"protocol": 2, "identity_epoch": data["identity_epoch"], "ok": True, "data": {"items": []}, **updates}


async def test_no_bootstrap_io_or_network_at_construction_and_missing_file_is_retryable(bootstrap, monkeypatch):
    path, data, write = bootstrap
    path.unlink()
    calls = []
    install_http(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json=envelope(data)))
    transport = MemoryHostTransport(path, data["identity_epoch"])
    assert calls == []
    with pytest.raises(MemoryProxyError, match="MEMORY_BOOTSTRAP_UNAVAILABLE"):
        await transport.request("context", {})
    assert calls == []
    write()
    assert await transport.request("context", {}) == {"items": []}
    assert len(calls) == 1


async def test_fixed_protocol_private_tokens_and_first_identity_pinned(bootstrap, monkeypatch):
    path, data, write = bootstrap
    original = dict(data)
    calls = []
    options = install_http(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json=envelope(original)))
    transport = MemoryHostTransport(path, data["identity_epoch"])
    assert await transport.request("search", {"query": "中文"}) == {"items": []}
    write(identity_epoch=str(uuid.uuid4()), token=secrets.token_urlsafe(32), control_token=secrets.token_urlsafe(32))
    await transport.request("visibility/commit", {"read_id": str(uuid.uuid4())})
    assert calls[0].url.path == "/memory/v2/search"
    assert calls[0].headers["authorization"] == "Bearer " + original["token"]
    assert calls[1].headers["authorization"] == "Bearer " + original["control_token"]
    assert all(request.headers["x-memory-identity-epoch"] == original["identity_epoch"] for request in calls)
    assert json.loads(calls[0].content) == {"query": "中文"}
    assert all(option == {"trust_env": False, "follow_redirects": False, "timeout": None} for option in options)
    assert original["token"] not in repr(transport)
    assert str(path) not in repr(transport)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:49123/memory/v2", "http://localhost:49123/memory/v2",
    "http://127.0.0.1/memory/v2", "http://127.0.0.1:49123/memory/v2/",
    "http://127.0.0.1:49123/memory/v2?token=secret", "http://127.0.0.1:49123/memory/v2#secret",
    "http://secret@127.0.0.1:49123/memory/v2", "http://127.0.0.1:49123/elsewhere",
    "http://[::1]:49123/memory/v2", "http://127.0.0.1:0/memory/v2",
    "http://127.0.0.1:049123/memory/v2", "http://127.0.0.1:49123/memory/v2?",
])
async def test_untrusted_bootstrap_origin_is_rejected_before_http(bootstrap, monkeypatch, url):
    path, data, write = bootstrap
    write(base_url=url)
    calls = []
    install_http(monkeypatch, lambda request: calls.append(request))
    with pytest.raises(MemoryProxyError, match="MEMORY_BOOTSTRAP_UNAVAILABLE"):
        await MemoryHostTransport(path, data["identity_epoch"]).request("context", {})
    assert calls == []


@pytest.mark.parametrize("case", ["file_mode", "parent_mode", "symlink", "parent_symlink", "hardlink", "fifo",
                                  "duplicate", "large", "wrong_epoch", "protocol", "tokens", "profile", "extra"])
async def test_unsafe_or_malformed_bootstrap_cannot_grant_access(bootstrap, monkeypatch, case):
    path, data, write = bootstrap
    if case == "file_mode":
        path.chmod(0o644)
    elif case == "parent_mode":
        path.parent.chmod(0o755)
    elif case == "symlink":
        target = path.with_name("target")
        path.rename(target)
        path.symlink_to(target)
    elif case == "parent_symlink":
        link = path.parent.with_name("linked-parent")
        link.symlink_to(path.parent, target_is_directory=True)
        path = link / path.name
    elif case == "hardlink":
        os.link(path, path.with_name("second-link"))
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif case == "duplicate":
        path.write_text(json.dumps(data)[:-1] + ', "protocol":2}')
    elif case == "large":
        path.write_bytes(b" " * (64 * 1024 + 1))
    elif case == "wrong_epoch":
        write(identity_epoch=str(uuid.uuid4()))
    elif case == "protocol":
        write(protocol=True)
    elif case == "tokens":
        write(control_token=data["token"])
    elif case == "profile":
        write(profile_id="not-a-profile")
    elif case == "extra":
        write(arbitrary_url="https://remote.test")
    calls = []
    install_http(monkeypatch, lambda request: calls.append(request))
    # wrong_epoch deliberately uses a separate expected identity.
    expected = str(uuid.uuid4()) if case == "wrong_epoch" else data["identity_epoch"]
    with pytest.raises(MemoryProxyError) as failure:
        await MemoryHostTransport(path, expected).request("context", {})
    assert failure.value.code == "MEMORY_BOOTSTRAP_UNAVAILABLE"
    assert failure.value.outcome == "not_sent"
    assert str(path) not in str(failure.value)
    assert calls == []


@pytest.mark.parametrize("operation", ["https://remote.test", "../qa", "qa?x=1", "experience/records/delete"])
async def test_unknown_operations_never_open_bootstrap_or_network(operation, monkeypatch):
    monkeypatch.setattr(module, "_read_bootstrap", lambda *_: pytest.fail("bootstrap must not be opened"))
    with pytest.raises(MemoryProxyError, match="INVALID_OPERATION"):
        await MemoryHostTransport(Path("/missing"), str(uuid.uuid4())).request(operation, {})


async def test_context_deadline_includes_bootstrap_loading(bootstrap, monkeypatch):
    path, data, _ = bootstrap

    async def slow_credentials(self):
        await asyncio.sleep(0.1)
        pytest.fail("expired context cannot continue")

    monkeypatch.setattr(MemoryHostTransport, "_credentials", slow_credentials)
    started = asyncio.get_running_loop().time()
    with pytest.raises(MemoryProxyError) as failure:
        await MemoryHostTransport(path, data["identity_epoch"]).request("context", {}, timeout=0.01)
    assert asyncio.get_running_loop().time() - started < 0.1
    assert failure.value.outcome == "not_sent"


class _DelayedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"secret":"'
        await asyncio.sleep(0.2)
        yield b'secret"}'


async def test_dispatched_write_timeout_has_unknown_outcome_and_no_retry(bootstrap, monkeypatch):
    path, data, _ = bootstrap
    calls = []
    install_http(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, stream=_DelayedBody()))
    with pytest.raises(MemoryProxyError) as failure:
        await MemoryHostTransport(path, data["identity_epoch"]).request("qa", {"user": "private"}, timeout=0.03)
    assert failure.value.outcome == "unknown"
    assert failure.value.code == "MEMORY_PROXY_UNAVAILABLE"
    assert len(calls) == 1
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize("kind", ["redirect", "wrong_epoch", "bad_protocol", "duplicate", "bad_json", "large", "compressed", "server_error"])
async def test_untrusted_write_reply_stays_unknown_and_is_not_retried(bootstrap, monkeypatch, kind):
    path, data, _ = bootstrap
    calls = []

    def respond(request):
        calls.append(request)
        if kind == "redirect":
            return httpx.Response(307, headers={"location": "https://remote.test/secret"})
        if kind == "wrong_epoch":
            return httpx.Response(200, json=envelope(data, identity_epoch=str(uuid.uuid4())))
        if kind == "bad_protocol":
            return httpx.Response(200, json=envelope(data, protocol=True))
        if kind == "duplicate":
            return httpx.Response(200, content=json.dumps(envelope(data))[:-1] + ', "ok":true}')
        if kind == "bad_json":
            return httpx.Response(200, content="private invalid JSON")
        if kind == "large":
            return httpx.Response(200, content=b"x" * (512 * 1024 + 1))
        if kind == "compressed":
            # A compressed payload is refused before a decoder can expand it.
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_DelayedBody())
        return httpx.Response(503, json=envelope(data, ok=False, error={"code": "INVALID_ARGUMENT"}))

    install_http(monkeypatch, respond)
    with pytest.raises(MemoryProxyError) as failure:
        await MemoryHostTransport(path, data["identity_epoch"]).request("qa", {})
    assert failure.value.outcome == "unknown"
    assert len(calls) == 1
    assert "private" not in str(failure.value)


async def test_only_known_4xx_rejection_proves_no_write(bootstrap, monkeypatch):
    path, data, _ = bootstrap
    install_http(monkeypatch, lambda _: httpx.Response(409, json=envelope(
        data, ok=False, error={"code": "MEMORY_REVISION_CONFLICT", "message": "private"})))
    with pytest.raises(MemoryProxyError) as failure:
        await MemoryHostTransport(path, data["identity_epoch"]).request("core/mutate", {})
    assert failure.value.code == "MEMORY_REVISION_CONFLICT"
    assert failure.value.outcome == "rejected"
    assert str(failure.value) == "MEMORY_REVISION_CONFLICT"


async def test_close_revokes_late_response(bootstrap, monkeypatch):
    path, data, _ = bootstrap
    transport = MemoryHostTransport(path, data["identity_epoch"])

    def respond(_):
        transport.close()
        return httpx.Response(200, json=envelope(data))

    install_http(monkeypatch, respond)
    with pytest.raises(MemoryProxyError) as failure:
        await transport.request("qa", {})
    assert failure.value.code == "SESSION_REVOKED"
    assert failure.value.outcome == "unknown"
    with pytest.raises(MemoryProxyError, match="SESSION_REVOKED"):
        await transport.request("context", {})


async def test_oversized_or_invalid_requests_are_not_sent(bootstrap, monkeypatch):
    path, data, _ = bootstrap
    calls = []
    install_http(monkeypatch, lambda request: calls.append(request))
    transport = MemoryHostTransport(path, data["identity_epoch"])
    for payload, code in [({"query": "x" * (512 * 1024)}, "BUDGET_EXCEEDED"),
                          ({"query": float("nan")}, "INVALID_ARGUMENT")]:
        with pytest.raises(MemoryProxyError, match=code):
            await transport.request("search", payload)
    with pytest.raises(MemoryProxyError, match="INVALID_ARGUMENT"):
        await transport.request("context", {}, timeout=5)
    assert calls == []


async def test_core_json_escaping_does_not_reduce_the_raw_content_limit(bootstrap, monkeypatch):
    path, data, _ = bootstrap
    calls = []
    install_http(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json=envelope(data)))
    # Raw text remains below 32 MiB, while quoting doubles the JSON wire size.
    content = '"' * (17 * 1024 * 1024)
    result = await MemoryHostTransport(path, data["identity_epoch"]).request("core/mutate", {"content": content})
    assert result == {"items": []}
    assert len(calls) == 1 and len(calls[0].content) > 32 * 1024 * 1024
    assert json.loads(calls[0].content)["content"] == content


async def test_request_headers_bodies_and_private_errors_do_not_reach_http_logs(bootstrap, monkeypatch, caplog):
    path, data, _ = bootstrap

    def respond(request):
        logging.getLogger("httpcore.http11").debug("response headers %s", data["token"])
        raise httpx.ConnectError("private-token=" + data["token"], request=request)

    install_http(monkeypatch, respond)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(MemoryProxyError) as failure:
        await MemoryHostTransport(path, data["identity_epoch"]).request("qa", {"user": "private-user-text"})
    assert failure.value.outcome == "unknown"
    assert data["token"] not in caplog.text + str(failure.value)
    assert "private-user-text" not in caplog.text + str(failure.value)
    logging.getLogger("httpcore.http11").debug("ordinary request survives")
    assert "ordinary request survives" in caplog.text


async def test_real_loopback_request_ignores_environment_proxy(bootstrap, monkeypatch):
    path, data, write = bootstrap
    received = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            body = await reader.readexactly(length)
            received.append((head, body))
            response = json.dumps(envelope(data)).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(response)).encode() + b"\r\nConnection: close\r\n\r\n" + response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        write(base_url=f"http://127.0.0.1:{port}/memory/v2")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        assert await MemoryHostTransport(path, data["identity_epoch"]).request("context", {"query": "中文"}) == {"items": []}
    assert len(received) == 1
    assert received[0][0].startswith(b"POST /memory/v2/context HTTP/1.1")
    assert json.loads(received[0][1]) == {"query": "中文"}
