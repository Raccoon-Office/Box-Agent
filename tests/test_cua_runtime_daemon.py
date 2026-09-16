from __future__ import annotations

import pytest

from box_agent.tools import cua_runtime_daemon


class _OwnedProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_ensure_starts_standalone_daemon_and_waits_for_status(monkeypatch):
    process = _OwnedProcess()
    spawned = []
    statuses = iter((False, True))

    async def status_ready(command, socket_path):
        assert command == "/runtime/cua-driver"
        assert socket_path == "/tmp/cua.sock"
        return next(statuses)

    def popen(args, **kwargs):
        spawned.append((args, kwargs))
        return process

    monkeypatch.setattr(cua_runtime_daemon, "_owned_process", None)
    monkeypatch.setattr(cua_runtime_daemon, "_owned_command", None)
    monkeypatch.setattr(cua_runtime_daemon, "_status_ready", status_ready)
    monkeypatch.setattr(cua_runtime_daemon.subprocess, "Popen", popen)
    monkeypatch.setattr(cua_runtime_daemon.atexit, "register", lambda _callback: None)

    ready, error = await cua_runtime_daemon.ensure_standalone_cua_daemon(
        "/runtime/cua-driver",
        socket_path="/tmp/cua.sock",
    )

    assert ready is True
    assert error is None
    assert spawned[0][0] == [
        "/runtime/cua-driver", "serve", "--socket", "/tmp/cua.sock",
    ]
    assert spawned[0][1]["env"]["CUA_DRIVER_RS_UPDATE_CHECK"] == "false"


@pytest.mark.asyncio
async def test_stop_terminates_daemon_owned_by_current_process(monkeypatch):
    process = _OwnedProcess()
    stop_commands = []

    class Stopper:
        async def wait(self):
            return 0

        def kill(self):
            raise AssertionError("responsive stop command must not be killed")

    async def create_subprocess_exec(*args, **kwargs):
        stop_commands.append((args, kwargs))
        return Stopper()

    monkeypatch.setattr(cua_runtime_daemon, "_owned_process", process)
    monkeypatch.setattr(cua_runtime_daemon, "_owned_command", "/runtime/cua-driver")
    monkeypatch.setattr(cua_runtime_daemon, "_owned_socket", "/tmp/owned-cua.sock")
    monkeypatch.setattr(
        cua_runtime_daemon.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )

    await cua_runtime_daemon.stop_standalone_cua_daemon()

    assert stop_commands[0][0] == (
        "/runtime/cua-driver",
        "stop",
        "--socket",
        "/tmp/owned-cua.sock",
    )
    assert process.terminated is True
    assert cua_runtime_daemon._owned_process is None


@pytest.mark.asyncio
async def test_ensure_replaces_stale_owned_runtime_before_starting_new_one(monkeypatch):
    old_process = _OwnedProcess()
    new_process = _OwnedProcess()
    calls = []
    statuses = iter((False, True))

    class Stopper:
        async def wait(self):
            return 0

        def kill(self):
            raise AssertionError("responsive stop command must not be killed")

    async def create_subprocess_exec(*args, **kwargs):
        calls.append(("stop", args))
        return Stopper()

    async def status_ready(command, socket_path):
        assert command == "/new/cua-driver"
        assert socket_path == "/tmp/new.sock"
        return next(statuses)

    def popen(args, **_kwargs):
        calls.append(("start", tuple(args)))
        return new_process

    monkeypatch.setattr(cua_runtime_daemon, "_owned_process", old_process)
    monkeypatch.setattr(cua_runtime_daemon, "_owned_command", "/old/cua-driver")
    monkeypatch.setattr(cua_runtime_daemon, "_owned_socket", "/tmp/old.sock")
    monkeypatch.setattr(
        cua_runtime_daemon.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )
    monkeypatch.setattr(cua_runtime_daemon, "_status_ready", status_ready)
    monkeypatch.setattr(cua_runtime_daemon.subprocess, "Popen", popen)
    monkeypatch.setattr(cua_runtime_daemon.atexit, "register", lambda _callback: None)

    ready, error = await cua_runtime_daemon.ensure_standalone_cua_daemon(
        "/new/cua-driver",
        socket_path="/tmp/new.sock",
    )

    assert ready is True
    assert error is None
    assert calls == [
        ("stop", ("/old/cua-driver", "stop", "--socket", "/tmp/old.sock")),
        ("start", ("/new/cua-driver", "serve", "--socket", "/tmp/new.sock")),
    ]
    assert old_process.terminated is True
    assert cua_runtime_daemon.get_standalone_cua_daemon_runtime() == (
        "/new/cua-driver",
        "/tmp/new.sock",
    )
