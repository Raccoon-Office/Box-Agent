"""Runtime constraints follow the target interpreter across installation backends."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from box_agent.tools import jupyter_tool as jupyter
from box_agent.tools.runtime_versions import (
    RUNTIME_PYTHON_VERSION, constraints_path, validate_packages,
)
from scripts import build_runtime


def test_all_default_and_allowed_packages_have_exact_pins():
    requirements = [Requirement(line) for line in constraints_path().read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
    assert all(len(r.specifier) == 1 and next(iter(r.specifier)).operator == "=="
               for r in requirements)
    required = jupyter.ALLOWED_RUNTIME_PACKAGES | set(jupyter.SANDBOX_DEFAULT_PACKAGES)
    assert required | {"ipykernel", "pip"} <= {canonicalize_name(r.name) for r in requirements}
    assert build_runtime.PYTHON_STANDALONE_VERSION == RUNTIME_PYTHON_VERSION


@pytest.mark.parametrize("package", ["numpy[extra]", "numpy; python_version > '3'",
                                     "numpy @ https://example.com/numpy.whl", "--upgrade"])
def test_unsupported_requirement_forms_are_rejected(package):
    with pytest.raises(ValueError):
        validate_packages([package])


def test_version_specifiers_are_left_for_the_package_resolver():
    validate_packages(["scikit_learn", "numpy==0.0.1", "unknown-package"])


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["3.10.19", "3.11.15", "3.12.6", "3.12.13"])
async def test_constraints_use_target_version_and_cache_only_within_environment(tmp_path, monkeypatch, version):
    monkeypatch.setattr(jupyter, "IS_FROZEN", False)
    monkeypatch.setattr(jupyter.platform, "python_version", lambda: "3.13.0")
    output = json.dumps([int(v) for v in version.split(".")]).encode()
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(output, b"")))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(jupyter.asyncio, "create_subprocess_exec", spawn)
    target = tmp_path / "python"
    env = jupyter.SandboxEnvironment(python_path=target)
    expected = ["--constraint", str(constraints_path())] if version == RUNTIME_PYTHON_VERSION else []
    assert await env._install_constraints() == expected
    assert await env._install_constraints() == expected
    spawn.assert_awaited_once()
    assert spawn.call_args.args[:3] == (str(target), "-E", "-c")
    # A later launch selects the same policy without relying on a new-environment flag.
    assert await jupyter.SandboxEnvironment(python_path=target)._install_constraints() == expected
    assert spawn.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("output,returncode", [(b"garbage", 0), (b'"3.12.6"', 0), (b"", 1)])
async def test_unreadable_python_version_stops_installation(tmp_path, monkeypatch, output, returncode):
    proc = SimpleNamespace(returncode=returncode, communicate=AsyncMock(return_value=(output, b"error")))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(jupyter.asyncio, "create_subprocess_exec", spawn)
    env = jupyter.SandboxEnvironment(python_path=tmp_path / "python")
    success, message = await env.install_packages(["numpy"])
    assert not success
    assert "Python version" in message
    spawn.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", [False, True])
@pytest.mark.parametrize("version", ["3.10.19", "3.11.15", RUNTIME_PYTHON_VERSION])
async def test_dynamic_install_constrains_only_matching_target(tmp_path, monkeypatch, host, version):
    env = jupyter.SandboxEnvironment(python_path=tmp_path / "python")
    env._explicit_python = False
    env._bundled_override = host
    env._install_python_version = version
    monkeypatch.setattr(jupyter, "IS_FROZEN", False)
    monkeypatch.setattr(jupyter, "RUNTIME_PACKAGES_DIR", tmp_path / "packages")
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"ok", b"")))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(jupyter.asyncio, "create_subprocess_exec", spawn)
    success, _ = await env.install_packages(["numpy"])
    assert success
    command = spawn.call_args.args
    assert ("--constraint" in command) is (version == RUNTIME_PYTHON_VERSION)
    if "--constraint" in command:
        assert command[command.index("--constraint") + 1] == str(constraints_path())
    assert "numpy" in command
    assert ("--target" in command) is host


@pytest.mark.asyncio
async def test_fixed_runtime_rejects_packages_without_maintained_constraints(tmp_path, monkeypatch):
    env = jupyter.SandboxEnvironment(base_dir=tmp_path)
    env._install_python_version = RUNTIME_PYTHON_VERSION
    spawn = AsyncMock()
    monkeypatch.setattr(jupyter.asyncio, "create_subprocess_exec", spawn)
    success, message = await env.install_packages(["unknown-package"])
    assert not success
    assert "PACKAGE_VERSION_NOT_ALLOWED" in message
    spawn.assert_not_called()


@pytest.mark.parametrize("version", ["3.10.19", RUNTIME_PYTHON_VERSION])
def test_frozen_pip_constrains_only_matching_target(tmp_path, monkeypatch, version):
    pip_cli = pytest.importorskip("pip._internal.cli.main")
    commands = []
    monkeypatch.setattr(pip_cli, "main", lambda args: commands.append(args) or 0)
    monkeypatch.setattr(jupyter.platform, "python_version", lambda: version)
    monkeypatch.setattr(jupyter, "RUNTIME_PACKAGES_DIR", tmp_path / "packages")
    monkeypatch.setattr(sys, "path", sys.path.copy())
    success, _ = jupyter.SandboxEnvironment._frozen_pip_install_sync(["numpy"])
    assert success
    assert ("--constraint" in commands[0]) is (version == RUNTIME_PYTHON_VERSION)
    assert "numpy" in commands[0]


def test_windows_preinstall_uses_runtime_constraints(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(build_runtime.subprocess, "run", lambda args, **kwargs:
                        commands.append(args) or SimpleNamespace(returncode=0))
    build_runtime._install_sandbox_packages_win(tmp_path / "python.exe")
    assert commands[0][commands[0].index("--constraint") + 1] == str(constraints_path())
    assert set(build_runtime._SANDBOX_BUNDLED_PACKAGES) <= set(commands[0])


def test_windows_build_extras_use_builder_python_and_fail_before_packaging(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(build_runtime, "require_supported_build_process", lambda *args: None)
    monkeypatch.setattr(build_runtime.shutil, "which", lambda _: "/tools/uv")
    monkeypatch.setattr(build_runtime.subprocess, "run", lambda args, **kwargs:
                        commands.append(args) or SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="runtime extras"):
        build_runtime.build_runtime("0.0.0", tmp_path, target="win32-x64")
    assert len(commands) == 2
    assert commands[0][commands[0].index("--python") + 1] == sys.executable
    assert commands[1][:4] == [sys.executable, "-m", "pip", "install"]
    assert all("--constraint" not in command for command in commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("current,uv_path", [(RUNTIME_PYTHON_VERSION, "/tools/uv"),
                                             ("3.11.0", "/tools/uv"), ("3.11.0", None)])
async def test_new_sandbox_uses_pinned_python_with_or_without_uv_on_path(tmp_path, monkeypatch, current, uv_path):
    monkeypatch.setattr(jupyter.platform, "python_version", lambda: current)
    monkeypatch.setattr(jupyter.shutil, "which", lambda _: uv_path)
    monkeypatch.setattr(jupyter.SandboxEnvironment, "_ensure_pip_available", AsyncMock())
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(jupyter.asyncio, "create_subprocess_exec", spawn)
    env = jupyter.SandboxEnvironment(base_dir=tmp_path / "sandbox")
    await env._create_venv()
    command = spawn.call_args.args
    if current == RUNTIME_PYTHON_VERSION:
        assert command == (sys.executable, "-m", "venv", "--without-pip", str(env.venv_dir))
    else:
        prefix = (uv_path,) if uv_path else (sys.executable, "-m", "uv")
        assert command == (*prefix, "venv", "--python", RUNTIME_PYTHON_VERSION, str(env.venv_dir))


@pytest.mark.asyncio
async def test_existing_sandbox_is_reused_without_version_probe_or_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(jupyter, "IS_FROZEN", False)
    for name in ("BOX_AGENT_SANDBOX_PYTHON", "BOX_AGENT_BUNDLED_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    env = jupyter.SandboxEnvironment(base_dir=tmp_path / "sandbox")
    env.python_path.parent.mkdir(parents=True)
    env.python_path.write_text("existing-interpreter")
    create = AsyncMock()
    probe = AsyncMock()
    monkeypatch.setattr(env, "_create_venv", create)
    monkeypatch.setattr(env, "_install_constraints", probe)
    monkeypatch.setattr(env, "_ensure_ipykernel", AsyncMock())
    monkeypatch.setattr(env, "_verify_packages", AsyncMock())
    await env.ensure_ready()
    create.assert_not_called()
    probe.assert_not_called()
    assert env.python_path.read_text() == "existing-interpreter"


@pytest.mark.asyncio
async def test_frozen_allowlist_accepts_versioned_requirement(tmp_path, monkeypatch):
    env = jupyter.SandboxEnvironment(base_dir=tmp_path)
    monkeypatch.setattr(jupyter, "IS_FROZEN", True)
    monkeypatch.setattr(env, "_frozen_pip_install_sync", lambda packages: (True, "ok"))
    assert env._blocked_runtime_packages(["numpy==2.4.4"]) == []
    assert env._blocked_runtime_packages(["pip==26.0.1"]) != []
    success, _ = await env.install_packages(["numpy==2.4.4"])
    assert success


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["3.10.19", "3.11.15", RUNTIME_PYTHON_VERSION])
async def test_host_missing_package_repair_follows_target_version(tmp_path, monkeypatch, version):
    env = jupyter.SandboxEnvironment(python_path=tmp_path / "python")
    env._explicit_python = False
    env._install_python_version = version
    monkeypatch.setattr(jupyter, "RUNTIME_PACKAGES_DIR", tmp_path / "packages")
    monkeypatch.setattr(env, "_missing_required_packages", AsyncMock(side_effect=[["numpy"], []]))
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(jupyter.asyncio, "create_subprocess_exec", spawn)
    await env._verify_packages()
    command = spawn.call_args.args
    assert ("--constraint" in command) is (version == RUNTIME_PYTHON_VERSION)
    assert command[command.index("--target") + 1] == str(tmp_path / "packages")
    assert "numpy" in command


@pytest.mark.asyncio
async def test_local_pip_fallback_retains_version_constraints(tmp_path, monkeypatch):
    monkeypatch.setattr(jupyter.shutil, "which", lambda _: None)
    env = jupyter.SandboxEnvironment(base_dir=tmp_path)
    env._install_python_version = RUNTIME_PYTHON_VERSION
    command = await env._venv_install_command(["numpy"])
    assert command[:4] == [str(env.python_path), "-m", "pip", "install"]
    assert command[command.index("--constraint") + 1] == str(constraints_path())
    assert "numpy" in command
