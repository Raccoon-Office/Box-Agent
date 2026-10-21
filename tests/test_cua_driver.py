import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from box_agent.plugins.cua import driver as module
from box_agent.plugins.cua.config import CuaRuntimeConfig
from box_agent.plugins.cua.lifecycle import CuaRuntimeManager


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    binary = tmp_path / "CuaDriver.app/Contents/MacOS/cua-driver"
    binary.parent.mkdir(parents=True)
    binary.write_text("fixture")
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="darwin"))
    result = module.DriverAdapter(str(binary))
    yield result
    # Tests must explicitly resolve pending ownership before removing resources.
    assert result._pending_handle is None
    result.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", ["open", "probe"])
async def test_cancelled_launch_reaps_late_app_without_touching_other_daemons(adapter, monkeypatch, cancel_phase):
    waiting = asyncio.Event()
    killed = []
    probes = 0
    async def command(*args):
        nonlocal probes
        if args[0] == "/usr/bin/open":
            if cancel_phase == "open":
                waiting.set()
                await asyncio.Event().wait()
            return 0, ""
        if args[0] == "/bin/ps":
            probes += 1
            other = "999 /Applications/CuaDriver.app/Contents/MacOS/cua-driver serve\n"
            own = f"333 {Path(adapter.driver).resolve()} serve --socket {adapter.endpoint}\n"
            return 0, other + (own if probes > 1 and not killed else "")
        waiting.set()
        if probes == 0:
            await asyncio.Event().wait()
        return 0, "Cua Driver daemon is not running"
    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: killed.append(pid))
    manager = CuaRuntimeManager(CuaRuntimeConfig(enabled=True, driver_path=adapter.driver), probe=adapter.ready, start=adapter.start)
    Path(adapter.endpoint).write_text("owned socket fixture")
    task = asyncio.create_task(manager.ensure_ready())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [333]
    assert adapter._pending_handle is None
    assert not Path(adapter.endpoint).exists()
    await manager.close()


@pytest.mark.asyncio
async def test_pending_launch_cleanup_failure_retains_handle_for_retry(adapter, monkeypatch):
    waiting = asyncio.Event()
    exited = False
    async def command(*args):
        if args[0] == "/usr/bin/open":
            return 0, ""
        if args[0] == "/bin/ps":
            return 0, "" if exited else f"333 {Path(adapter.driver).resolve()} serve --socket {adapter.endpoint}"
        waiting.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(module, "command", command)
    def fail(*args):
        raise PermissionError("Cannot signal process")
    monkeypatch.setattr(module.os, "kill", fail)
    task = asyncio.create_task(adapter.start(CuaRuntimeConfig(enabled=True)))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter._pending_handle is not None
    with pytest.raises(RuntimeError, match="preserving private endpoint"):
        adapter.cleanup()
    assert Path(adapter.directory.name).exists()
    def stop(*args):
        nonlocal exited
        exited = True
    monkeypatch.setattr(module.os, "kill", stop)
    await adapter.close_pending()
    assert adapter._pending_handle is None


@pytest.mark.asyncio
async def test_unsettled_launch_timeout_does_not_claim_cleanup_succeeded(adapter, monkeypatch):
    handle = module.DriverHandle(adapter)
    handle.launch_requested = True
    adapter._pending_handle = handle
    monkeypatch.setattr(module, "_MAC_CLEANUP_TIMEOUT", .01)
    monkeypatch.setattr(handle, "_mac_pid", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "probe", AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="retaining its private endpoint"):
        await adapter.close_pending()
    assert adapter._pending_handle is handle
    monkeypatch.setattr(adapter, "probe", AsyncMock(side_effect=[True, False]))
    monkeypatch.setattr(module, "command", AsyncMock(return_value=(0, "")))
    await adapter.close_pending()
    assert adapter._pending_handle is None


@pytest.mark.asyncio
async def test_confirmed_daemon_exit_allows_readiness_to_launch_a_replacement(adapter, monkeypatch):
    alive = False
    launches = []
    killed = []

    async def command(*args):
        nonlocal alive
        if args[0] == "/usr/bin/open":
            launches.append(args)
            alive = True
            return 0, ""
        if args[0] == "/bin/ps":
            own = f"333 {Path(adapter.driver).resolve()} serve --socket {adapter.endpoint}"
            return 0, own if alive else ""
        if args[1:3] == ("call", "check_permissions"):
            return 0, '{"accessibility":true,"screen_recording":true}'
        return 0, "Cua Driver daemon is running" if alive else "Cua Driver daemon is not running"

    def stop(pid, sig):
        nonlocal alive
        killed.append(pid)
        alive = False

    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module.os, "kill", stop)
    manager = CuaRuntimeManager(
        CuaRuntimeConfig(enabled=True, driver_path=adapter.driver),
        probe=adapter.ready, start=adapter.start,
    )
    try:
        assert (await manager.ensure_ready()).owned
        first = manager._owned
        assert first.launch_confirmed
        Path(adapter.endpoint).write_text("stale owned socket")
        alive = False  # The confirmed instance crashed, not a pending launch.
        assert (await manager.ensure_ready()).owned
        assert manager._owned is not first
        assert len(launches) == 2
        assert not Path(adapter.endpoint).exists()
        assert killed == []
    finally:
        await manager.close()
    assert killed == [333]


@pytest.mark.asyncio
async def test_confirmed_daemon_with_failed_status_still_reaps_its_exact_pid(adapter, monkeypatch):
    handle = module.DriverHandle(adapter)
    handle.launch_requested = handle.launch_confirmed = True
    monkeypatch.setattr(handle, "_mac_pid", AsyncMock(side_effect=[333, None]))
    monkeypatch.setattr(adapter, "probe", AsyncMock(return_value=False))
    killed = []
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: killed.append(pid))
    Path(adapter.endpoint).write_text("owned socket")
    await handle.close()
    assert killed == [333]
    assert not Path(adapter.endpoint).exists()


@pytest.mark.asyncio
async def test_confirmed_daemon_shutdown_timeout_preserves_socket_for_retry(adapter, monkeypatch):
    handle = module.DriverHandle(adapter)
    handle.launch_requested = handle.launch_confirmed = True
    monkeypatch.setattr(module, "_MAC_CLEANUP_TIMEOUT", .01)
    monkeypatch.setattr(handle, "_mac_pid", AsyncMock(return_value=333))
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(adapter, "probe", AsyncMock(return_value=False))
    Path(adapter.endpoint).write_text("owned socket")
    with pytest.raises(RuntimeError, match="retaining its private endpoint"):
        await handle.close()
    assert Path(adapter.endpoint).exists()
    assert handle.launch_requested
    monkeypatch.setattr(handle, "_mac_pid", AsyncMock(return_value=None))
    await handle.close()
    assert not Path(adapter.endpoint).exists()
