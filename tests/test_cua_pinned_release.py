import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import tarfile

import pytest

from box_agent.plugins.cua import pinned_release as release
from box_agent.plugins.cua.config import CuaConfig, CuaRuntimeConfig
from box_agent.plugins.cua.ready_tool import CuaService, create_cua_service


@pytest.fixture
def package(tmp_path, monkeypatch):
    monkeypatch.setattr(release.sys, "platform", "darwin")
    monkeypatch.setattr(release.platform, "machine", lambda: "arm64")
    root = tmp_path / "installed"
    monkeypatch.setenv("RACCOON_CUA_RESOURCE_DIR", str(root))
    driver = tmp_path / "payload/CuaDriver.app/Contents/MacOS/cua-driver"
    driver.parent.mkdir(parents=True)
    driver.write_bytes(b"local driver fixture")
    archive = tmp_path / "driver.tar.gz"
    with tarfile.open(archive, "w:gz") as contents:
        contents.add(driver.parents[2], arcname="CuaDriver.app")
    receipt = dict(schema_version=1, source_commit=release.source_pin(), version="0.38.0",
                   platform="darwin-arm64", signing="adhoc-development",
                   executable_sha256=release.digest(driver), archive_sha256=release.digest(archive),
                   size_bytes=archive.stat().st_size)
    manifest = tmp_path / "driver-release.json"
    manifest.write_text(json.dumps(receipt))
    monkeypatch.setattr(release, "verify_source", Mock())
    monkeypatch.setattr(release.subprocess, "run", Mock())
    return archive, manifest, root


def test_local_install_has_separate_pointer_and_detects_corruption(package):
    archive, manifest, root = package
    root.mkdir()
    old = root / "current.json"
    old.write_text('{"directory":"old-official"}')
    driver = release.install_release(archive, manifest, allow_development=True)
    assert release.installed_pinned_driver() == str(driver)
    assert json.loads(old.read_text())["directory"] == "old-official"
    release.verify_source.assert_called_once()
    driver.write_bytes(b"corrupted")
    assert release.installed_pinned_driver() is None


@pytest.mark.parametrize("field,value,error", [
    ("source_commit", "a" * 40, "source pin"),
    ("archive_sha256", "0" * 64, "archive SHA-256"),
    ("executable_sha256", "0" * 64, "executable SHA-256"),
    ("platform", "linux-arm64", "macOS ARM64"),
])
def test_bad_release_preserves_existing_pointer(package, field, value, error):
    archive, manifest, root = package
    root.mkdir()
    pointer = root / "current-repl.json"
    pointer.write_text("old pointer")
    receipt = json.loads(manifest.read_text())
    receipt[field] = value
    manifest.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match=error):
        release.install_release(archive, manifest, allow_development=True)
    assert pointer.read_text() == "old pointer"


def test_development_signature_requires_explicit_opt_in(package):
    archive, manifest, root = package
    with pytest.raises(ValueError, match="explicit"):
        release.install_release(archive, manifest)
    assert not root.exists()


def test_nondevelopment_release_retains_gatekeeper_verification(package, monkeypatch):
    archive, manifest, root = package
    receipt = json.loads(manifest.read_text())
    receipt["signing"] = "certificate"
    manifest.write_text(json.dumps(receipt))
    verify = Mock(side_effect=ValueError("Gatekeeper rejected"))
    monkeypatch.setattr(release, "verify", verify)
    with pytest.raises(ValueError, match="Gatekeeper"):
        release.install_release(archive, manifest, allow_development=True)
    verify.assert_called_once()
    assert not (root / "current-repl.json").exists()


@pytest.mark.parametrize("mutation", [{"git_sha": "a" * 40}, {"exe_sha256": "b" * 64}, {"version": "0.28.2"}])
def test_same_semver_is_not_enough_to_accept_driver_identity(tmp_path, mutation):
    driver = tmp_path / "driver"
    driver.write_bytes(b"driver")
    build = dict(git_sha=release.source_pin(), version="0.38.0", exe_sha256=release.digest(driver))
    release.require_build(build, driver)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        release.require_build({**build, **mutation}, driver)


def test_doctor_identity_accepts_nonzero_permissions_result_without_fix(tmp_path, monkeypatch):
    driver = tmp_path / "driver"
    driver.write_bytes(b"driver")
    result = SimpleNamespace(returncode=1, stdout=json.dumps({"build": {
        "git_sha": release.source_pin(), "version": "0.38.0", "exe_sha256": release.digest(driver)}}))
    run = Mock(return_value=result)
    monkeypatch.setattr(release.subprocess, "run", run)
    release.verify_source(driver)
    assert run.call_args.args[0] == [str(driver), "doctor", "--json"]


@pytest.mark.asyncio
async def test_missing_pinned_install_never_falls_back_or_starts_daemon(monkeypatch):
    monkeypatch.setattr(release, "installed_pinned_driver", lambda: None)
    service = CuaService(CuaConfig(runtime=CuaRuntimeConfig(enabled=True)))
    service.manager.ensure_ready = AsyncMock()
    try:
        with pytest.raises(RuntimeError, match="not installed"):
            await service.prepare()
        service.manager.ensure_ready.assert_not_awaited()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_host_daemon_mismatch_blocks_before_permission_checks_and_never_restarts(tmp_path, monkeypatch):
    from box_agent.plugins.cua import driver as adapter_module
    driver = tmp_path / "driver"
    driver.write_bytes(b"driver")
    service = CuaService(CuaConfig(runtime=CuaRuntimeConfig(
        enabled=True, mode="external", driver_path=str(driver), endpoint="/host/socket")))
    monkeypatch.setattr(release, "verify_source", Mock())
    service.adapter.probe = AsyncMock(return_value=True)
    service.adapter.ready = AsyncMock()
    service.manager._start = AsyncMock()
    monkeypatch.setattr(adapter_module, "command", AsyncMock(return_value=(0, json.dumps({"build": {
        "version": "0.38.0", "git_sha": "a" * 40, "exe_sha256": release.digest(driver)}}))))
    try:
        with pytest.raises(RuntimeError, match="identity mismatch"):
            await service.prepare()
        service.adapter.ready.assert_not_awaited()
        service.manager._start.assert_not_awaited()
    finally:
        await service.close()


def test_repl_uses_pinned_install_but_preserves_explicit_host_path(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_CUA_RUNTIME_CONFIG", raising=False)
    monkeypatch.setattr(release, "installed_pinned_driver", lambda: "/pinned/driver")
    config = SimpleNamespace(plugins={"repl": {"enabled": True, "trusted_local_code": True}})
    service = create_cua_service(config)
    try:
        assert service.config.driver_path == "/pinned/driver"
    finally:
        service.adapter.cleanup()


    monkeypatch.setenv("BOX_AGENT_CUA_RUNTIME_CONFIG", json.dumps({
        "enabled": True, "mode": "external", "endpoint": "/host/socket", "driver_path": "/host/driver"}))
    service = create_cua_service(config)
    try:
        assert service.config.driver_path == "/host/driver"
        assert service.adapter.endpoint == "/host/socket"
    finally:
        service.adapter.cleanup()


def test_no_browser_repl_configuration_still_uses_pinned_driver(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_CUA_RUNTIME_CONFIG", raising=False)
    monkeypatch.setattr(release, "installed_pinned_driver", lambda: "/pinned/driver")
    service = create_cua_service(SimpleNamespace(plugins={}))
    try:
        assert service.config.driver_path == "/pinned/driver"
    finally:
        service.adapter.cleanup()
