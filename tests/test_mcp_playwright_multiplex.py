"""Per-session Playwright BrowserContext multiplexing in the MCP loader.

Covers: the mcp.json gate deciding whether ``playwright`` can be multiplexed,
the owned HTTP server process, ``MCPTool`` routing through a session lease,
and an end-to-end run against a fake Streamable-HTTP MCP server that reports
which server-side session handled each call.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.tools import mcp_loader
from box_agent.tools.browser_runtime_scope import (
    BrowserRuntimeCoordinator,
    release_browser_runtime,
    reset_browser_runtime_owner,
    reset_browser_session_key,
    set_browser_runtime_owner,
    set_browser_session_key,
)
from box_agent.tools.mcp_loader import (
    ManagedHttpServerProcess,
    MCPServerConnection,
    MCPTool,
    PlaywrightIsolationConfig,
    _effective_connection_type,
    playwright_multiplex_blocker,
)
from box_agent.tools.playwright_session_pool import BrowserSessionLimitError


def _pw(args: list[str]) -> dict:
    return {"command": "npx", "args": ["@playwright/mcp@latest", *args]}


# ---------------------------------------------------------------------------
# mcp.json gate
# ---------------------------------------------------------------------------


def test_multiplex_allowed_for_isolated_stdio_playwright():
    assert playwright_multiplex_blocker("playwright", _pw(["--headless", "--isolated"])) is None


@pytest.mark.parametrize(
    ("name", "config", "fragment"),
    [
        ("browser-gateway", _pw(["--isolated"]), "not the playwright"),
        ("playwright", _pw(["--headless"]), "--isolated missing"),
        ("playwright", _pw(["--isolated", "--shared-browser-context"]), "--shared-browser-context"),
        ("playwright", _pw(["--isolated", "--user-data-dir", "/tmp/p"]), "--user-data-dir"),
        ("playwright", _pw(["--isolated", "--port", "8931"]), "--port"),
        ("playwright", _pw(["--isolated", "--port=8931"]), "--port"),
        ("playwright", _pw(["--isolated", "--user-data-dir=/tmp/profile"]), "--user-data-dir"),
        ("playwright", _pw(["--isolated", "--cdp-endpoint=http://localhost:9222"]), "--cdp-endpoint"),
        ("playwright", _pw(["--isolated", "--extension"]), "--extension"),
        ("playwright", _pw(["--isolated", "--config", "browser.json"]), "--config"),
        ("playwright", {"url": "http://localhost:8931/mcp"}, "URL-based"),
        ("playwright", {"args": ["--isolated"]}, "no command"),
    ],
)
def test_multiplex_blockers(name, config, fragment):
    reason = playwright_multiplex_blocker(name, config)
    assert reason is not None and fragment in reason


def test_multiplex_respects_config_switch():
    disabled = PlaywrightIsolationConfig(enabled=False)
    reason = playwright_multiplex_blocker("playwright", _pw(["--isolated"]), disabled)
    assert reason is not None and "playwright_per_session_context" in reason


@pytest.mark.parametrize("setting", ["SHARED_BROWSER_CONTEXT", "CDP_ENDPOINT", "CONFIG"])
def test_multiplex_rejects_environment_browser_overrides(setting):
    config = _pw(["--isolated"])
    key = f"PLAYWRIGHT_MCP_{setting}"
    config["env"] = {key: "configured"}
    assert key in playwright_multiplex_blocker("playwright", config)


async def test_multiplex_shutdown_stops_process_even_if_pool_close_is_cancelled():
    from unittest.mock import AsyncMock

    conn = MCPServerConnection("playwright", connection_type="stdio_http", command="node")
    conn.session_pool = SimpleNamespace(close_all=AsyncMock(side_effect=asyncio.CancelledError))
    process = SimpleNamespace(stop=AsyncMock())
    conn.server_process = process
    with pytest.raises(asyncio.CancelledError):
        await conn._shutdown_multiplexed_server()
    process.stop.assert_awaited_once()


def test_effective_connection_type_switches_playwright_to_stdio_http(monkeypatch):
    monkeypatch.setattr(
        mcp_loader, "_playwright_isolation_config", PlaywrightIsolationConfig(enabled=True)
    )
    assert _effective_connection_type("playwright", _pw(["--isolated"])) == "stdio_http"
    # Fallback keeps today's shared stdio behaviour.
    assert _effective_connection_type("playwright", _pw(["--headless"])) == "stdio"
    # Other servers are untouched.
    assert _effective_connection_type("browser-gateway", _pw(["--isolated"])) == "stdio"


def test_shipped_mcp_example_enables_playwright_multiplex():
    example = (
        Path(__file__).resolve().parents[1] / "box_agent" / "config" / "mcp-example.json"
    )
    playwright = json.loads(example.read_text(encoding="utf-8"))["mcpServers"]["playwright"]
    assert "--isolated" in playwright["args"]
    assert playwright_multiplex_blocker("playwright", playwright) is None


# ---------------------------------------------------------------------------
# MCPTool routing through a session lease
# ---------------------------------------------------------------------------


class _RecordingSession:
    def __init__(self, tag: str):
        self.tag = tag
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return SimpleNamespace(content=[SimpleNamespace(text=self.tag)], isError=False)


def _make_tool(lease, remote_name="browser_navigate", timeout=1.0):
    return MCPTool(
        name=f"managed_{remote_name}",
        remote_name=remote_name,
        description="browser",
        parameters={"type": "object"},
        session=None,
        server_name="playwright",
        execute_timeout=timeout,
        session_lease=lease,
    )


def test_mcp_tool_requires_session_or_lease():
    with pytest.raises(ValueError):
        MCPTool(name="x", description="", parameters={}, session=None, server_name="s")


async def test_leased_playwright_tool_skips_cross_turn_lease_and_routes_per_key():
    sessions = {"A": _RecordingSession("ctx-A"), "B": _RecordingSession("ctx-B")}

    @asynccontextmanager
    async def lease():
        from box_agent.tools.browser_runtime_scope import current_browser_session_key

        yield sessions[current_browser_session_key()]

    tool = _make_tool(lease)

    # Another turn holds the legacy shared-browser lease. A leased tool must
    # not care: each session owns its own BrowserContext now.
    await BrowserRuntimeCoordinator.acquire("someone-else:turn-1")
    try:
        token = set_browser_session_key("A")
        owner = set_browser_runtime_owner("session-a:turn-1")
        try:
            result_a = await asyncio.wait_for(tool.execute(url="https://a"), timeout=0.5)
        finally:
            reset_browser_runtime_owner(owner)
            reset_browser_session_key(token)

        token = set_browser_session_key("B")
        try:
            result_b = await asyncio.wait_for(tool.execute(url="https://b"), timeout=0.5)
        finally:
            reset_browser_session_key(token)
    finally:
        await release_browser_runtime("someone-else:turn-1")

    assert result_a.success and result_a.content == "ctx-A"
    assert result_b.success and result_b.content == "ctx-B"
    assert sessions["A"].calls == [("browser_navigate", {"url": "https://a"})]
    assert sessions["B"].calls == [("browser_navigate", {"url": "https://b"})]


async def test_leased_browser_close_closes_own_tabs_first():
    class TabSession(_RecordingSession):
        def __init__(self):
            super().__init__("closed")
            self.tabs = 2

        async def call_tool(self, name, arguments):
            self.calls.append((name, dict(arguments)))
            if name == "browser_tabs":
                if self.tabs == 0:
                    return SimpleNamespace(content=[], isError=True)
                self.tabs -= 1
                return SimpleNamespace(content=[], isError=False)
            return SimpleNamespace(content=[SimpleNamespace(text="bye")], isError=False)

    session = TabSession()

    @asynccontextmanager
    async def lease():
        yield session

    result = await _make_tool(lease, remote_name="browser_close").execute()

    assert result.success is True
    assert session.tabs == 0
    assert [name for name, _ in session.calls] == [
        "browser_tabs", "browser_tabs", "browser_tabs", "browser_close",
    ]


async def test_leased_tool_reports_session_limit_as_tool_error():
    @asynccontextmanager
    async def lease():
        raise BrowserSessionLimitError(2)
        yield  # pragma: no cover

    result = await _make_tool(lease).execute(url="https://a")

    assert result.success is False
    assert result.error.startswith("BROWSER_SESSION_LIMIT")
    assert "2 agent sessions" in result.error


# ---------------------------------------------------------------------------
# Owned HTTP server process
# ---------------------------------------------------------------------------

_LISTEN_SCRIPT = textwrap.dedent(
    """
    import socket, sys, time
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    host = args[args.index("--host") + 1]
    assert args[args.index("--allowed-hosts") + 1] == f"{host}:{port}", args
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port)); srv.listen()
    sys.stderr.write(f"Listening on http://{host}:{port}\\n"); sys.stderr.flush()
    while True:
        conn, _ = srv.accept(); conn.close()
    """
)


async def test_managed_process_starts_waits_for_port_and_stops():
    proc = ManagedHttpServerProcess(
        name="playwright", command=sys.executable, args=["-c", _LISTEN_SCRIPT], env=None
    )
    await proc.start(ready_timeout=15.0)
    try:
        assert proc.running
        assert proc.url == f"http://127.0.0.1:{proc.port}/mcp"
        assert proc.launch_args()[-6:] == [
            "--port", str(proc.port), "--host", "127.0.0.1",
            "--allowed-hosts", f"127.0.0.1:{proc.port}",
        ]
        assert await proc._port_open()
    finally:
        await proc.stop()
    assert proc.running is False
    assert await proc._port_open() is False


async def test_managed_process_reports_early_exit():
    proc = ManagedHttpServerProcess(
        name="playwright",
        command=sys.executable,
        args=["-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
        env=None,
    )
    with pytest.raises(RuntimeError, match="exited with code 3"):
        await proc.start(ready_timeout=15.0)
    await proc.stop()


async def test_managed_process_times_out_when_port_never_opens():
    proc = ManagedHttpServerProcess(
        name="playwright",
        command=sys.executable,
        args=["-c", "import time; time.sleep(30)"],
        env=None,
    )
    with pytest.raises(TimeoutError):
        await proc.start(ready_timeout=0.5)
    assert proc.running is False


async def test_managed_windows_process_uses_sdk_executable_resolution(monkeypatch):
    from unittest.mock import AsyncMock
    from mcp.os.win32 import utilities

    monkeypatch.setattr(mcp_loader, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    monkeypatch.setattr(utilities, "get_windows_executable_command", lambda command: "C:/node/npx.cmd")
    monkeypatch.setattr(mcp_loader, "_track_server", lambda process: None)
    process = SimpleNamespace(pid=42, returncode=None, stderr=SimpleNamespace(readline=AsyncMock(return_value=b"")))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    proc = ManagedHttpServerProcess("playwright", "npx", ["--isolated"], None, port=8931)
    monkeypatch.setattr(proc, "_port_open", AsyncMock(return_value=True))
    await proc.start(ready_timeout=1)
    await proc._stderr_task
    assert spawn.call_args.args[0] == "C:/node/npx.cmd"


# ---------------------------------------------------------------------------
# Orphan protection: records + stale-server reaping
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def run_state_dir(tmp_path, monkeypatch):
    """Keep server records out of the developer's real ~/.box-agent/run."""
    monkeypatch.setenv("BOX_AGENT_RUN_STATE_DIR", str(tmp_path / "run"))
    return tmp_path / "run"


async def test_managed_process_records_itself_while_running(run_state_dir):
    proc = ManagedHttpServerProcess(
        name="playwright", command=sys.executable, args=["-c", _LISTEN_SCRIPT], env=None
    )
    await proc.start(ready_timeout=15.0)
    try:
        records = mcp_loader._read_server_records()
        assert [(r["owner_pid"], r["port"], r["name"]) for r in records] == [
            (os.getpid(), proc.port, "playwright")
        ]
    finally:
        await proc.stop()
    assert mcp_loader._read_server_records() == []


class _KillEndpointServer:
    """Minimal HTTP server answering GET /killkillkill like @playwright/mcp."""

    def __init__(self) -> None:
        self.hits = 0
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readline()
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        if b"/killkillkill" in request:
            self.hits += 1
            body = b"Killing process"
        else:
            body = b"nope"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    async def __aenter__(self) -> "_KillEndpointServer":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()


async def test_reap_skips_servers_whose_owner_is_alive(run_state_dir):
    async with _KillEndpointServer() as fake:
        mcp_loader._write_server_records(
            [{"name": "playwright", "owner_pid": os.getpid(), "pid": 1, "port": fake.port}]
        )
        reaped = await mcp_loader.reap_stale_managed_servers()
    assert reaped == []
    assert fake.hits == 0
    assert len(mcp_loader._read_server_records()) == 1


async def test_reap_kills_servers_whose_owner_is_gone(run_state_dir, monkeypatch):
    monkeypatch.setattr(mcp_loader, "_pid_alive", lambda pid: False)
    async with _KillEndpointServer() as fake:
        mcp_loader._write_server_records(
            [
                {"name": "playwright", "owner_pid": 999_999, "pid": 1, "port": fake.port},
                # nothing listening → record is simply dropped
                {"name": "playwright", "owner_pid": 999_998, "pid": 2, "port": 1},
            ]
        )
        reaped = await mcp_loader.reap_stale_managed_servers()
    assert [r["port"] for r in reaped] == [fake.port]
    assert fake.hits == 1
    assert mcp_loader._read_server_records() == []


def test_pid_alive_distinguishes_current_process_from_dead_pid():
    assert mcp_loader._pid_alive(os.getpid()) is True
    proc = subprocess.run([sys.executable, "-c", "pass"])
    assert proc.returncode == 0
    # A pid that has exited is reported dead (no immediate reuse in practice).
    assert mcp_loader._pid_alive(2**22 + 12345) is False


# ---------------------------------------------------------------------------
# End-to-end against a fake Streamable HTTP MCP server
# ---------------------------------------------------------------------------

_FAKE_PLAYWRIGHT_SERVER = textwrap.dedent(
    """
    import sys
    from mcp.server.fastmcp import Context, FastMCP

    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    host = args[args.index("--host") + 1]
    mcp = FastMCP("fake-playwright", host=host, port=port, log_level="WARNING")

    @mcp.tool()
    async def browser_navigate(url: str, ctx: Context) -> str:
        \"\"\"Navigate; echoes which server-side session handled the call.\"\"\"
        return f"ctx={id(ctx.session)} url={url}"

    @mcp.tool()
    async def browser_tabs(action: str, index: int | None = None) -> str:
        \"\"\"Tabs; close always fails because this fake has no tabs.\"\"\"
        if action == "close":
            raise RuntimeError("Tab undefined not found")
        return "### Open tabs\\n(none)"

    mcp.run(transport="streamable-http")
    """
)


def _ctx_of(text: str) -> str:
    return text.split(" ")[0]


async def test_stdio_http_connection_gives_each_session_its_own_server_session(monkeypatch):
    monkeypatch.setattr(
        mcp_loader,
        "_playwright_isolation_config",
        PlaywrightIsolationConfig(enabled=True, max_clients=2, idle_timeout=0),
    )
    conn = MCPServerConnection(
        name="playwright",
        connection_type="stdio_http",
        command=sys.executable,
        args=["-c", _FAKE_PLAYWRIGHT_SERVER],
        connect_timeout=30,
        execute_timeout=10,
    )
    assert await conn.connect() is True
    try:
        assert conn.server_process is not None and conn.server_process.running
        assert conn.session_pool is not None
        # Discovery client is gone; nothing is connected until a session calls.
        assert conn.session is None
        assert conn.session_pool.active_keys() == []

        navigate = next(t for t in conn.tools if t.remote_name == "browser_navigate")
        assert navigate.name == "managed_browser_navigate"

        async def call_as(key: str, url: str) -> str:
            token = set_browser_session_key(key)
            try:
                result = await navigate.execute(url=url)
            finally:
                reset_browser_session_key(token)
            assert result.success, result.error
            return result.content

        a1, b1, a2 = await asyncio.gather(
            call_as("A", "https://a/1"), call_as("B", "https://b/1"), call_as("A", "https://a/2")
        )
        assert _ctx_of(a1) == _ctx_of(a2), "same agent session must reuse its client"
        assert _ctx_of(a1) != _ctx_of(b1), "different agent sessions must not share a client"
        assert sorted(conn.session_pool.active_keys()) == ["A", "B"]

        # Third session at max_clients=2: LRU idle client (A or B) is evicted.
        c1 = await call_as("C", "https://c/1")
        assert _ctx_of(c1) not in {_ctx_of(a1), _ctx_of(b1)}
        assert len(conn.session_pool.active_keys()) == 2
        assert "C" in conn.session_pool.active_keys()

        snapshot = conn.session_pool.snapshot()
        assert snapshot["mode"] == "per_session_context"
        assert snapshot["activeClients"] == 2

        # Closing a session drops its client without touching the others.
        assert await conn.session_pool.close("C") is True
        assert "C" not in conn.session_pool.active_keys()
        assert len(conn.session_pool.active_keys()) == 1
    finally:
        await conn.disconnect()

    assert conn.server_process is None
    assert conn.session_pool is None


async def test_derived_sub_agent_keys_get_distinct_server_sessions(monkeypatch):
    """Sibling `{parent}:{sub_agent_id}` keys map to distinct MCP clients."""
    monkeypatch.setattr(
        mcp_loader,
        "_playwright_isolation_config",
        PlaywrightIsolationConfig(enabled=True, max_clients=4, idle_timeout=0),
    )
    conn = MCPServerConnection(
        name="playwright",
        connection_type="stdio_http",
        command=sys.executable,
        args=["-c", _FAKE_PLAYWRIGHT_SERVER],
        connect_timeout=30,
        execute_timeout=10,
    )
    assert await conn.connect() is True
    try:
        navigate = next(t for t in conn.tools if t.remote_name == "browser_navigate")

        async def call_as(key: str, url: str) -> str:
            token = set_browser_session_key(key)
            try:
                result = await navigate.execute(url=url)
            finally:
                reset_browser_session_key(token)
            assert result.success, result.error
            return result.content

        parent = "sess-0"
        apple = f"{parent}:subagent-aaa"
        xiaomi = f"{parent}:subagent-bbb"
        a1, b1 = await asyncio.gather(
            call_as(apple, "https://www.apple.com.cn/iphone/"),
            call_as(xiaomi, "https://www.mi.com/"),
        )
        assert _ctx_of(a1) != _ctx_of(b1), "parallel sub-agents must not share a client"
        assert sorted(conn.session_pool.active_keys()) == [apple, xiaomi]
    finally:
        await conn.disconnect()

    assert conn.server_process is None
    assert conn.session_pool is None


async def test_stdio_http_connect_failure_leaves_no_orphan_process():
    conn = MCPServerConnection(
        name="playwright",
        connection_type="stdio_http",
        command=sys.executable,
        # Listens on the port but never speaks MCP → initialize() times out.
        args=["-c", _LISTEN_SCRIPT],
        connect_timeout=3,
    )
    assert await conn.connect() is False
    assert conn.server_process is None
    assert conn.session_pool is None
    assert conn.tools == []
