import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from box_agent.plugins.cua.config import CuaConfig, CuaRuntimeConfig
from box_agent.plugins.cua.lifecycle import CuaRuntimeManager
from box_agent.plugins.cua import _create_bindings


class Daemon:
    endpoint = "owned-endpoint"

    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


def test_runtime_is_opt_in_and_external_requires_endpoint():
    assert CuaConfig().runtime is None
    assert not CuaRuntimeConfig().enabled
    with pytest.raises(ValidationError):
        CuaRuntimeConfig(enabled=True, mode="external")
    with pytest.raises(ValidationError):
        CuaRuntimeConfig(mode="guess")


def test_managed_disabled_runtime_does_not_activate_cua_tools():
    run = SimpleNamespace(
        session=SimpleNamespace(config=SimpleNamespace(plugins={
            "cua": {"runtime": {"enabled": False}},
        })),
        agent=SimpleNamespace(), options=SimpleNamespace(llm=SimpleNamespace()),
    )
    bindings = _create_bindings(SimpleNamespace(context=run))
    assert not bindings.mcp_active
    assert not bindings.active


@pytest.mark.asyncio
async def test_external_failure_never_launches_and_close_never_stops_host():
    async def probe(endpoint):
        return False

    async def start(config):
        pytest.fail("external must not launch")

    manager = CuaRuntimeManager(
        CuaRuntimeConfig(enabled=True, mode="external", endpoint="host"),
        probe=probe, start=start,
    )
    assert (await manager.ensure_ready()).state == "unavailable"
    await manager.close()
    assert (await manager.ensure_ready()).state == "closed"


@pytest.mark.asyncio
async def test_concurrent_ensure_starts_once_and_owned_close_is_idempotent():
    daemon = Daemon()
    calls = 0

    async def start(config):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return daemon

    async def probe(endpoint):
        return True

    manager = CuaRuntimeManager(
        CuaRuntimeConfig(enabled=True, driver_path="trusted-driver"),
        probe=probe, start=start,
    )
    results = await asyncio.gather(*(manager.ensure_ready() for _ in range(5)))
    assert all(result.state == "ready" and result.owned for result in results)
    assert calls == 1
    await manager.close()
    await manager.close()
    assert daemon.closed == 1


@pytest.mark.asyncio
async def test_disabled_runtime_does_no_io():
    async def forbidden(*args):
        pytest.fail("disabled runtime must not perform I/O")

    manager = CuaRuntimeManager(CuaRuntimeConfig(), probe=forbidden, start=forbidden)
    assert (await manager.ensure_ready()).state == "disabled"
    await manager.close()


@pytest.mark.asyncio
async def test_borrowed_standalone_is_not_owned():
    async def probe(endpoint):
        return True

    async def start(config):
        pytest.fail("existing daemon should be reused")

    manager = CuaRuntimeManager(
        CuaRuntimeConfig(enabled=True, endpoint="existing"), probe=probe, start=start,
    )
    ready = await manager.ensure_ready()
    assert ready.state == "ready" and not ready.owned
    await manager.close()


@pytest.mark.asyncio
async def test_failed_readiness_reaps_owned_daemon():
    daemon = Daemon()

    async def start(config):
        return daemon

    async def probe(endpoint):
        return False

    manager = CuaRuntimeManager(
        CuaRuntimeConfig(enabled=True, driver_path="trusted-driver"),
        probe=probe, start=start,
    )
    assert (await manager.ensure_ready()).state == "unavailable"
    assert daemon.closed == 1
    await manager.close()
    assert daemon.closed == 1


@pytest.mark.asyncio
async def test_cancelled_readiness_reaps_child_and_preserves_cancellation():
    daemon = Daemon()
    probing = asyncio.Event()

    async def start(config):
        return daemon

    async def probe(endpoint):
        probing.set()
        await asyncio.Event().wait()

    manager = CuaRuntimeManager(
        CuaRuntimeConfig(enabled=True, driver_path="trusted-driver"),
        probe=probe, start=start,
    )
    task = asyncio.create_task(manager.ensure_ready())
    await probing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert daemon.closed == 1
    await manager.close()
    assert daemon.closed == 1


@pytest.mark.asyncio
async def test_readiness_timeout_cleans_child():
    daemon = Daemon()

    async def start(config):
        return daemon

    async def probe(endpoint):
        await asyncio.Event().wait()

    manager = CuaRuntimeManager(
        CuaRuntimeConfig(enabled=True, driver_path="trusted-driver"),
        probe=probe, start=start, timeout=0.01,
    )
    assert (await manager.ensure_ready()).state == "unavailable"
    assert daemon.closed == 1
    await manager.close()


@pytest.mark.asyncio
async def test_failed_cleanup_keeps_child_for_shutdown_retry():
    daemon = Daemon()
    original_close = daemon.close

    async def flaky_close():
        if daemon.closed == 0:
            daemon.closed += 1
            raise RuntimeError("close failed")
        await original_close()

    daemon.close = flaky_close

    async def start(config):
        return daemon

    async def probe(endpoint):
        return False

    manager = CuaRuntimeManager(
        CuaRuntimeConfig(enabled=True, driver_path="trusted-driver"),
        probe=probe, start=start,
    )
    assert (await manager.ensure_ready()).state == "unavailable"
    await manager.close()
    assert daemon.closed == 2
    await manager.close()
    assert daemon.closed == 2
