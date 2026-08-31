"""Tests for skill runtime discovery and prompt rendering."""

from __future__ import annotations

import json
import os
import hashlib
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

from box_agent.acp.env_context import EnvContext
from box_agent.tools.jupyter_tool import SandboxEnvironment
from box_agent.tools.runtime import (
    DEFAULT_NODE_VERSION,
    NodeRuntimeInstallError,
    NodeRuntimeManager,
    DEFAULT_NODE_RUNTIME_ROOT,
    build_skill_runtime_context,
    build_skill_runtime_prompt,
)
from box_agent.tools.skill_execution_env import build_skill_execution_env


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def _write_node_manifest(root: Path, *, node: Path, npm: Path, npx: Path, node_modules: Path | None = None) -> None:
    active = {
        "version": "v22.99.0-test",
        "node": str(node),
        "npm": str(npm),
        "npx": str(npx),
    }
    if node_modules is not None:
        active["node_modules"] = str(node_modules)
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({"active": active}), encoding="utf-8")


def _make_node_archive(tmp_path: Path, *, version: str = DEFAULT_NODE_VERSION, platform_id: str = "darwin-arm64") -> tuple[Path, str]:
    archive_root = tmp_path / f"node-{version}-{platform_id}"
    for name in ("node", "npm", "npx"):
        _make_executable(archive_root / "bin" / name)
    archive_path = tmp_path / f"{archive_root.name}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(archive_root, arcname=archive_root.name)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    return archive_path, digest


def _fake_node_downloader(*, archive: Path, checksum: str, archive_name: str, calls: list[str] | None = None):
    def _download(url: str, dest: Path) -> None:
        if calls is not None:
            calls.append(url)
        if url.endswith(archive_name):
            dest.write_bytes(archive.read_bytes())
            return
        if url.endswith("SHASUMS256.txt"):
            dest.write_text(f"{checksum}  {archive_name}\n", encoding="utf-8")
            return
        raise AssertionError(f"unexpected download URL: {url}")

    return _download


def test_python_runtime_uses_existing_sandbox_python(tmp_path: Path) -> None:
    sandbox = SandboxEnvironment(base_dir=tmp_path)
    _make_executable(sandbox.python_path)

    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        sandbox_env=sandbox,
        node_runtime_root=tmp_path / "missing-node",
    )
    python = ctx.get("python")

    assert python.status == "available"
    assert python.provider == "box_agent"
    assert python.executable_path == str(sandbox.python_path)
    assert ctx.env()["BOX_AGENT_PYTHON"] == str(sandbox.python_path)
    assert ctx.env()["BOX_AGENT_PYTHON3"] == str(sandbox.python_path)


def test_cli_shell_python_is_separate_from_execute_code_sandbox(tmp_path: Path) -> None:
    sandbox = SandboxEnvironment(base_dir=tmp_path / "sandbox")
    shell_python = tmp_path / "python-runtime" / "bin" / "python3"
    _make_executable(sandbox.python_path)
    _make_executable(shell_python)

    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        sandbox_env=sandbox,
        shell_python_path=shell_python,
        node_runtime_root=tmp_path / "missing-node",
    )

    assert ctx.env()["BOX_AGENT_PYTHON"] == str(shell_python)
    assert ctx.env()["BOX_AGENT_PYTHON3"] == str(shell_python)
    assert ctx.env()["BOX_AGENT_SANDBOX_PYTHON"] == str(sandbox.python_path)


def test_cli_shell_python_is_available_before_sandbox_creation(tmp_path: Path) -> None:
    sandbox = SandboxEnvironment(base_dir=tmp_path / "sandbox")
    shell_python = tmp_path / "python-runtime" / "bin" / "python3"
    _make_executable(shell_python)

    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        sandbox_env=sandbox,
        shell_python_path=shell_python,
        node_runtime_root=tmp_path / "missing-node",
    )

    assert not sandbox.python_path.exists()
    assert ctx.get("python").status == "available"
    assert ctx.env()["BOX_AGENT_PYTHON"] == str(shell_python)
    assert ctx.env()["BOX_AGENT_PYTHON3"] == str(shell_python)
    assert "BOX_AGENT_SANDBOX_PYTHON" not in ctx.env()


def test_frozen_python_runtime_does_not_inject_fake_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("box_agent.tools.runtime.sys.frozen", True, raising=False)

    ctx = build_skill_runtime_context(sandbox_mode=True, node_runtime_root=tmp_path / "missing-node")
    python = ctx.get("python")

    assert python.status == "unavailable"
    assert "BOX_AGENT_PYTHON" not in ctx.env()
    assert "BOX_AGENT_PYTHON3" not in ctx.env()


def test_host_python_runtime_exports_shell_and_sandbox_env(tmp_path: Path) -> None:
    python_path = tmp_path / "officev3" / "python" / "bin" / "python3"
    _make_executable(python_path)
    env_context = EnvContext.from_meta(
        {
            "runtimes": {
                "python": {
                    "path": str(python_path),
                    "ready": True,
                    "provider": "officev3",
                }
            }
        }
    )

    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        env_context=env_context,
        node_runtime_root=tmp_path / "missing-node",
    )
    env = ctx.env()

    assert ctx.get("python").status == "available"
    assert ctx.get("python").provider == "host"
    assert env["BOX_AGENT_PYTHON"] == str(python_path)
    assert env["BOX_AGENT_PYTHON3"] == str(python_path)
    assert env["BOX_AGENT_SANDBOX_PYTHON"] == str(python_path)


def test_host_python_runtime_supports_separate_shell_and_sandbox_paths(tmp_path: Path) -> None:
    shell_path = tmp_path / "officev3" / "python" / "bin" / "python3"
    sandbox_path = tmp_path / "officev3" / "sandbox-python" / "python.exe"
    for path in (shell_path, sandbox_path):
        _make_executable(path)
    env_context = EnvContext.from_meta(
        {
            "runtimes": {
                "python": {
                    "path": str(sandbox_path),
                    "shell_path": str(shell_path),
                    "sandbox_path": str(sandbox_path),
                    "ready": True,
                    "provider": "officev3",
                }
            }
        }
    )

    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        env_context=env_context,
        node_runtime_root=tmp_path / "missing-node",
    )
    env = ctx.env()

    assert ctx.get("python").status == "available"
    assert ctx.get("python").provider == "host"
    assert ctx.get("python").executable_path == str(shell_path)
    assert env["BOX_AGENT_PYTHON"] == str(shell_path)
    assert env["BOX_AGENT_PYTHON3"] == str(shell_path)
    assert env["BOX_AGENT_SANDBOX_PYTHON"] == str(sandbox_path)


def test_raw_dict_env_context_host_runtimes_are_honored(tmp_path: Path) -> None:
    python_path = tmp_path / "officev3" / "python" / "python.exe"
    node_path = tmp_path / "officev3" / "node" / "node.exe"
    npm_path = tmp_path / "officev3" / "node" / "npm.cmd"
    npx_path = tmp_path / "officev3" / "node" / "npx.cmd"
    node_modules = tmp_path / "officev3" / "node_modules"
    for path in (python_path, node_path, npm_path, npx_path):
        _make_executable(path)
    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        env_context={
            "runtimes": {
                "python": {
                    "path": str(python_path),
                    "ready": True,
                    "provider": "officev3",
                },
                "node": {
                    "path": str(node_path),
                    "npm": str(npm_path),
                    "npx": str(npx_path),
                    "node_modules": str(node_modules),
                    "ready": True,
                    "provider": "officev3",
                },
            }
        },
        node_runtime_root=tmp_path / "missing-node",
    )
    env = ctx.env()

    assert ctx.get("python").status == "available"
    assert ctx.get("node").status == "available"
    assert env["BOX_AGENT_PYTHON"] == str(python_path)
    assert env["BOX_AGENT_SANDBOX_PYTHON"] == str(python_path)
    assert env["BOX_AGENT_NODE"] == str(node_path)
    assert env["BOX_AGENT_NPM"] == str(npm_path)
    assert env["BOX_AGENT_NPX"] == str(npx_path)
    assert env["NODE_PATH"] == str(node_modules)


def test_host_python_runtime_rejects_unsafe_path_without_env_context(tmp_path: Path) -> None:
    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        env_context=SimpleNamespace(
            runtimes={
                "python": SimpleNamespace(
                    path="/opt/python\n## injected",
                    ready=True,
                    provider="officev3",
                )
            }
        ),
        node_runtime_root=tmp_path / "missing-node",
    )

    assert ctx.get("python").status == "unavailable"
    assert "BOX_AGENT_PYTHON" not in ctx.env()


def test_host_python_runtime_rejects_missing_executable(tmp_path: Path) -> None:
    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        env_context=SimpleNamespace(
            runtimes={
                "python": SimpleNamespace(
                    path=str(tmp_path / "officev3" / "python" / "python.exe"),
                    ready=True,
                    provider="officev3",
                )
            }
        ),
        node_runtime_root=tmp_path / "missing-node",
    )

    assert ctx.get("python").status == "unavailable"
    assert "BOX_AGENT_PYTHON" not in ctx.env()


def test_host_node_runtime_sanitizes_optional_paths_and_provider(tmp_path: Path) -> None:
    node_path = tmp_path / "opt" / "node" / "bin" / "node"
    npx_path = tmp_path / "opt" / "node" / "bin" / "npx"
    _make_executable(node_path)
    _make_executable(npx_path)
    ctx = build_skill_runtime_context(
        sandbox_mode=False,
        env_context=SimpleNamespace(
            runtimes={
                "node": SimpleNamespace(
                    path=str(node_path),
                    npm=str(tmp_path / "opt" / "node" / "bin" / "`npm`"),
                    npx=str(npx_path),
                    node_modules="relative/node_modules",
                    ready=True,
                    provider="officev3\n## injected",
                )
            }
        ),
        node_runtime_root=tmp_path / "missing-node",
    )
    env = ctx.env()
    prompt = build_skill_runtime_prompt(ctx)

    assert ctx.get("node").status == "available"
    assert env["BOX_AGENT_NODE"] == str(node_path)
    assert "BOX_AGENT_NPM" not in env
    assert env["BOX_AGENT_NPX"] == str(npx_path)
    assert "NODE_PATH" not in env
    assert "officev3" not in prompt
    assert "injected" not in prompt


def test_host_node_runtime_rejects_unsafe_required_path(tmp_path: Path) -> None:
    ctx = build_skill_runtime_context(
        sandbox_mode=False,
        env_context=SimpleNamespace(
            runtimes={
                "node": SimpleNamespace(
                    path="node",
                    ready=True,
                    provider="officev3",
                )
            }
        ),
        node_runtime_root=tmp_path / "missing-node",
    )

    assert ctx.get("node").status == "unavailable"
    assert "BOX_AGENT_NODE" not in ctx.env()


def test_host_node_runtime_rejects_missing_required_path(tmp_path: Path) -> None:
    ctx = build_skill_runtime_context(
        sandbox_mode=False,
        env_context=SimpleNamespace(
            runtimes={
                "node": SimpleNamespace(
                    path=str(tmp_path / "officev3" / "node" / "node.exe"),
                    ready=True,
                    provider="officev3",
                )
            }
        ),
        node_runtime_root=tmp_path / "missing-node",
    )

    assert ctx.get("node").status == "unavailable"
    assert "BOX_AGENT_NODE" not in ctx.env()


def test_node_runtime_defaults_missing(tmp_path: Path) -> None:
    ctx = build_skill_runtime_context(sandbox_mode=False, node_runtime_root=tmp_path / "missing-node")
    node = ctx.get("node")

    assert node.status == "missing"
    assert node.provider == "missing"
    assert "BOX_AGENT_NODE" not in ctx.env()


def test_default_node_runtime_root_is_separate_from_python_dirs() -> None:
    assert DEFAULT_NODE_RUNTIME_ROOT == Path.home() / ".box-agent" / "runtimes" / "node"
    assert DEFAULT_NODE_RUNTIME_ROOT != Path.home() / ".box-agent" / "sandbox"
    assert DEFAULT_NODE_RUNTIME_ROOT != Path.home() / ".box-agent" / "runtime-packages"


def test_self_managed_node_runtime_from_manifest(tmp_path: Path) -> None:
    root = tmp_path / ".box-agent" / "runtimes" / "node"
    version_dir = root / "versions" / "node-v22.99.0-test-darwin-arm64" / "bin"
    node = version_dir / "node"
    npm = version_dir / "npm"
    npx = version_dir / "npx"
    for path in (node, npm, npx):
        _make_executable(path)
    _write_node_manifest(root, node=node, npm=npm, npx=npx)

    ctx = build_skill_runtime_context(sandbox_mode=False, node_runtime_root=root)
    runtime = ctx.get("node")
    env = ctx.env()

    assert runtime.status == "available"
    assert runtime.provider == "box_agent"
    assert runtime.executable_path == str(node)
    assert env["BOX_AGENT_NODE"] == str(node)
    assert env["BOX_AGENT_NPM"] == str(npm)
    assert env["BOX_AGENT_NPX"] == str(npx)
    assert env["NODE_PATH"] == str(root / "sandbox" / "node_modules")
    execution_env = build_skill_execution_env(
        ctx,
        base_env={"PATH": "/usr/bin"},
        home_dir=tmp_path,
    )
    skill_tools = tmp_path / ".box-agent" / "skill-tools"
    assert execution_env["NPM_CONFIG_CACHE"] == str(skill_tools / "npm-cache")
    assert execution_env["NPM_CONFIG_PREFIX"] == str(skill_tools)
    assert execution_env["PATH"].split(":")[0] == str(skill_tools / "bin")


def test_self_managed_node_runtime_accepts_relative_manifest_paths(tmp_path: Path) -> None:
    root = tmp_path / "node-runtime"
    version_dir = root / "versions" / "node-v22-test-darwin-arm64" / "bin"
    for name in ("node", "npm", "npx"):
        _make_executable(version_dir / name)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "active": {
                    "version": "v22-test",
                    "node": "versions/node-v22-test-darwin-arm64/bin/node",
                    "npm": "versions/node-v22-test-darwin-arm64/bin/npm",
                    "npx": "versions/node-v22-test-darwin-arm64/bin/npx",
                    "node_modules": "sandbox/node_modules",
                }
            }
        ),
        encoding="utf-8",
    )

    ctx = build_skill_runtime_context(sandbox_mode=False, node_runtime_root=root)

    assert ctx.get("node").status == "available"
    assert ctx.env()["BOX_AGENT_NODE"] == str(version_dir / "node")


def test_cli_reuses_office_provisioned_node_without_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    default_root = tmp_path / ".box-agent" / "runtimes" / "node"
    office_root = tmp_path / ".box-agent" / "box-agent-runtime" / "runtimes" / "node"
    node_bin = office_root / "versions" / "node-v24.15.0-darwin-arm64" / "bin"
    for name in ("node", "npm", "npx"):
        _make_executable(node_bin / name)
    monkeypatch.setattr("box_agent.tools.runtime.DEFAULT_NODE_RUNTIME_ROOT", default_root)

    ctx = build_skill_runtime_context(
        sandbox_mode=False,
        node_runtime_root=None,
        office_node_runtime_root=office_root,
    )

    # Keep this test independent from the developer's real default runtime.
    default_runtime = NodeRuntimeManager(root=default_root).discover()
    assert default_runtime.status == "missing"
    assert ctx.get("node").status == "available"
    assert ctx.env()["BOX_AGENT_NODE"] == str(node_bin / "node")


def test_frozen_runtime_discovers_bundled_node_and_uses_user_state_dirs(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "box-agent-runtime"
    exe = runtime_dir / "bin" / "box-agent-acp"
    _make_executable(exe)
    node_root = runtime_dir / "runtimes" / "node"
    version_dir = node_root / "versions" / "node-v22-test-darwin-arm64" / "bin"
    for name in ("node", "npm", "npx"):
        _make_executable(version_dir / name)
    (node_root / "manifest.json").write_text(
        json.dumps(
            {
                "active": {
                    "version": "v22-test",
                    "node": "versions/node-v22-test-darwin-arm64/bin/node",
                    "npm": "versions/node-v22-test-darwin-arm64/bin/npm",
                    "npx": "versions/node-v22-test-darwin-arm64/bin/npx",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("box_agent.tools.runtime.sys.frozen", True, raising=False)
    monkeypatch.setattr("box_agent.tools.runtime.sys.executable", str(exe))

    ctx = build_skill_runtime_context(sandbox_mode=False)
    env = ctx.env()

    assert ctx.get("node").status == "available"
    assert env["BOX_AGENT_NODE"] == str(version_dir / "node")
    execution_env = build_skill_execution_env(
        ctx,
        base_env={"PATH": "/usr/bin"},
        home_dir=tmp_path,
    )
    assert execution_env["NPM_CONFIG_CACHE"] == str(
        tmp_path / ".box-agent" / "skill-tools" / "npm-cache"
    )
    assert execution_env["NPM_CONFIG_PREFIX"] == str(
        tmp_path / ".box-agent" / "skill-tools"
    )


def test_install_macos_downloads_verifies_extracts_and_writes_manifest(tmp_path: Path) -> None:
    root = tmp_path / ".box-agent" / "runtimes" / "node"
    archive_name = f"node-{DEFAULT_NODE_VERSION}-darwin-arm64.tar.gz"
    archive, checksum = _make_node_archive(tmp_path, platform_id="darwin-arm64")

    runtime = NodeRuntimeManager(root=root).install_macos(
        platform_id="darwin-arm64",
        downloader=_fake_node_downloader(archive=archive, checksum=checksum, archive_name=archive_name),
    )

    active = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["active"]
    version_dir = root / "versions" / archive_name.removesuffix(".tar.gz")
    assert runtime.status == "available"
    assert runtime.provider == "box_agent"
    assert active["version"] == DEFAULT_NODE_VERSION
    assert active["platform"] == "darwin-arm64"
    assert active["node"] == str(version_dir / "bin" / "node")
    assert active["npm"] == str(version_dir / "bin" / "npm")
    assert active["npx"] == str(version_dir / "bin" / "npx")
    assert runtime.env_vars["BOX_AGENT_NODE"] == active["node"]
    assert not (tmp_path / ".box-agent" / "sandbox").exists()
    assert not (tmp_path / ".box-agent" / "runtime-packages").exists()


def test_install_macos_checksum_mismatch_does_not_write_manifest(tmp_path: Path) -> None:
    root = tmp_path / "node-runtime"
    archive_name = f"node-{DEFAULT_NODE_VERSION}-darwin-arm64.tar.gz"
    archive, _checksum = _make_node_archive(tmp_path, platform_id="darwin-arm64")

    try:
        NodeRuntimeManager(root=root).install_macos(
            platform_id="darwin-arm64",
            downloader=_fake_node_downloader(
                archive=archive,
                checksum="0" * 64,
                archive_name=archive_name,
            ),
        )
    except NodeRuntimeInstallError:
        pass
    else:
        raise AssertionError("checksum mismatch should fail")

    assert not (root / "manifest.json").exists()
    assert not (root / "versions" / archive_name.removesuffix(".tar.gz")).exists()


def test_install_for_current_platform_dispatches_to_windows(monkeypatch, tmp_path: Path) -> None:
    manager = NodeRuntimeManager(root=tmp_path / "node-runtime")
    expected = manager.discover()
    calls: list[str] = []

    monkeypatch.setattr("box_agent.tools.runtime.sys.platform", "win32")
    monkeypatch.setattr(
        manager,
        "install_win",
        lambda *, version: calls.append(version) or expected,
    )

    assert manager.install_for_current_platform(version="v24.1.0") is expected
    assert calls == ["v24.1.0"]


def test_install_for_current_platform_dispatches_to_linux(monkeypatch, tmp_path: Path) -> None:
    manager = NodeRuntimeManager(root=tmp_path / "node-runtime")
    expected = manager.discover()
    calls: list[str] = []

    monkeypatch.setattr("box_agent.tools.runtime.sys.platform", "linux")
    monkeypatch.setattr(
        manager,
        "install_linux",
        lambda *, version: calls.append(version) or expected,
    )

    assert manager.install_for_current_platform(version="v24.1.0") is expected
    assert calls == ["v24.1.0"]


def test_install_linux_downloads_verifies_extracts_and_writes_manifest(tmp_path: Path) -> None:
    root = tmp_path / ".box-agent" / "runtimes" / "node"
    archive_name = f"node-{DEFAULT_NODE_VERSION}-linux-arm64.tar.gz"
    archive, checksum = _make_node_archive(tmp_path, platform_id="linux-arm64")

    runtime = NodeRuntimeManager(root=root).install_linux(
        platform_id="linux-arm64",
        downloader=_fake_node_downloader(
            archive=archive,
            checksum=checksum,
            archive_name=archive_name,
        ),
    )

    active = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["active"]
    assert runtime.status == "available"
    assert active["platform"] == "linux-arm64"
    assert Path(active["node"]).is_file()


def test_install_macos_failure_preserves_existing_manifest(tmp_path: Path) -> None:
    root = tmp_path / "node-runtime"
    old_bin = root / "versions" / "old-node" / "bin"
    old_node = old_bin / "node"
    old_npm = old_bin / "npm"
    old_npx = old_bin / "npx"
    for path in (old_node, old_npm, old_npx):
        _make_executable(path)
    _write_node_manifest(root, node=old_node, npm=old_npm, npx=old_npx)
    old_manifest = (root / "manifest.json").read_text(encoding="utf-8")

    archive_name = f"node-{DEFAULT_NODE_VERSION}-darwin-arm64.tar.gz"

    def broken_downloader(url: str, dest: Path) -> None:
        if url.endswith(archive_name):
            dest.write_text("not a tarball", encoding="utf-8")
            return
        dest.write_text(f"{hashlib.sha256(b'not a tarball').hexdigest()}  {archive_name}\n", encoding="utf-8")

    try:
        NodeRuntimeManager(root=root).install_macos(
            platform_id="darwin-arm64",
            downloader=broken_downloader,
        )
    except NodeRuntimeInstallError:
        pass
    else:
        raise AssertionError("broken archive should fail")

    assert (root / "manifest.json").read_text(encoding="utf-8") == old_manifest
    assert NodeRuntimeManager(root=root).discover().env_vars["BOX_AGENT_NODE"] == str(old_node)


def test_install_macos_skips_download_when_version_already_installed(tmp_path: Path) -> None:
    root = tmp_path / "node-runtime"
    archive_name = f"node-{DEFAULT_NODE_VERSION}-darwin-arm64.tar.gz"
    version_dir = root / "versions" / archive_name.removesuffix(".tar.gz")
    for name in ("node", "npm", "npx"):
        _make_executable(version_dir / "bin" / name)
    calls: list[str] = []

    runtime = NodeRuntimeManager(root=root).install_macos(
        platform_id="darwin-arm64",
        downloader=lambda url, dest: calls.append(url),
    )

    assert calls == []
    assert runtime.status == "available"
    assert (root / "manifest.json").exists()


def test_install_macos_rejects_unsupported_platform(tmp_path: Path) -> None:
    try:
        NodeRuntimeManager(root=tmp_path / "node-runtime").install_macos(platform_id="linux-x64")
    except NodeRuntimeInstallError as exc:
        assert "Unsupported macOS Node platform" in str(exc)
    else:
        raise AssertionError("unsupported platform should fail")


def test_install_macos_rejects_tar_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "node-runtime"
    archive_name = f"node-{DEFAULT_NODE_VERSION}-darwin-arm64.tar.gz"
    archive_path = tmp_path / archive_name
    evil_file = tmp_path / "evil"
    evil_file.write_text("boom", encoding="utf-8")
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(evil_file, arcname="../evil")
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    try:
        NodeRuntimeManager(root=root).install_macos(
            platform_id="darwin-arm64",
            downloader=_fake_node_downloader(archive=archive_path, checksum=checksum, archive_name=archive_name),
        )
    except NodeRuntimeInstallError as exc:
        assert "Unsafe path" in str(exc)
    else:
        raise AssertionError("unsafe tar path should fail")

    assert not (root / "manifest.json").exists()


def test_self_managed_node_runtime_ignores_unsafe_manifest_paths(tmp_path: Path) -> None:
    root = tmp_path / "node-runtime"
    good = root / "versions" / "node" / "bin" / "node"
    _make_executable(good)
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "active": {
                    "version": "v22.99.0-test",
                    "node": str(good),
                    "npm": "relative/npm",
                    "npx": f"{root}/bin/`npx`",
                }
            }
        ),
        encoding="utf-8",
    )

    ctx = build_skill_runtime_context(sandbox_mode=False, node_runtime_root=root)
    runtime = ctx.get("node")

    assert runtime.status == "unavailable"
    assert runtime.provider == "box_agent"
    assert "BOX_AGENT_NODE" not in ctx.env()


def test_self_managed_node_runtime_rejects_paths_outside_runtime_root(tmp_path: Path) -> None:
    root = tmp_path / ".box-agent" / "runtimes" / "node"
    outside = tmp_path / "system-node"
    node = outside / "node"
    npm = outside / "npm"
    npx = outside / "npx"
    for path in (node, npm, npx):
        _make_executable(path)
    _write_node_manifest(root, node=node, npm=npm, npx=npx)

    ctx = build_skill_runtime_context(sandbox_mode=False, node_runtime_root=root)

    assert ctx.get("node").status == "unavailable"
    assert "BOX_AGENT_NODE" not in ctx.env()


def test_self_managed_node_runtime_does_not_touch_python_runtime_dirs(tmp_path: Path) -> None:
    root = tmp_path / ".box-agent" / "runtimes" / "node"
    node = root / "versions" / "node" / "bin" / "node"
    npm = root / "versions" / "node" / "bin" / "npm"
    npx = root / "versions" / "node" / "bin" / "npx"
    for path in (node, npm, npx):
        _make_executable(path)
    _write_node_manifest(root, node=node, npm=npm, npx=npx)

    build_skill_runtime_context(sandbox_mode=False, node_runtime_root=root)

    assert not (tmp_path / ".box-agent" / "sandbox").exists()
    assert not (tmp_path / ".box-agent" / "runtime-packages").exists()


def test_host_node_runtime_can_be_available(tmp_path: Path) -> None:
    node = tmp_path / "opt" / "node" / "bin" / "node"
    npm = tmp_path / "opt" / "node" / "bin" / "npm"
    npx = tmp_path / "opt" / "node" / "bin" / "npx"
    node_modules = tmp_path / "opt" / "node" / "lib" / "node_modules"
    for path in (node, npm, npx):
        _make_executable(path)
    env_context = EnvContext.from_meta(
        {
            "runtimes": {
                "node": {
                    "path": str(node),
                    "npm": str(npm),
                    "npx": str(npx),
                    "node_modules": str(node_modules),
                    "ready": True,
                    "provider": "officev3",
                }
            }
        }
    )

    ctx = build_skill_runtime_context(sandbox_mode=False, env_context=env_context)
    env = ctx.env()

    assert ctx.get("node").status == "available"
    assert ctx.get("node").provider == "host"
    assert env["BOX_AGENT_NODE"] == str(node)
    assert env["BOX_AGENT_NPM"] == str(npm)
    assert env["BOX_AGENT_NPX"] == str(npx)
    assert env["NODE_PATH"] == str(node_modules)


def test_host_node_prompt_only_mentions_available_node_env_vars(tmp_path: Path) -> None:
    node = tmp_path / "opt" / "node" / "bin" / "node"
    _make_executable(node)
    env_context = EnvContext.from_meta(
        {
            "runtimes": {
                "node": {
                    "path": str(node),
                    "ready": True,
                    "provider": "officev3",
                }
            }
        }
    )

    ctx = build_skill_runtime_context(sandbox_mode=False, env_context=env_context)
    env = ctx.env()
    out = build_skill_runtime_prompt(ctx)
    node_line = next(line for line in out.splitlines() if line.startswith("- Node:"))

    assert ctx.get("node").status == "available"
    assert env["BOX_AGENT_NODE"] == str(node)
    assert "BOX_AGENT_NPM" not in env
    assert "BOX_AGENT_NPX" not in env
    assert "$BOX_AGENT_NODE" in node_line
    assert "$BOX_AGENT_NPM" not in node_line
    assert "$BOX_AGENT_NPX" not in node_line


def test_host_node_runtime_takes_precedence_over_self_managed_node(tmp_path: Path) -> None:
    root = tmp_path / "node-runtime"
    node = root / "versions" / "node" / "bin" / "node"
    npm = root / "versions" / "node" / "bin" / "npm"
    npx = root / "versions" / "node" / "bin" / "npx"
    for path in (node, npm, npx):
        _make_executable(path)
    _write_node_manifest(root, node=node, npm=npm, npx=npx)
    host_node = tmp_path / "host" / "node"
    host_npm = tmp_path / "host" / "npm"
    host_npx = tmp_path / "host" / "npx"
    for path in (host_node, host_npm, host_npx):
        _make_executable(path)

    env_context = EnvContext.from_meta(
        {
            "runtimes": {
                "node": {
                    "path": str(host_node),
                    "npm": str(host_npm),
                    "npx": str(host_npx),
                    "ready": True,
                    "provider": "officev3",
                }
            }
        }
    )

    ctx = build_skill_runtime_context(
        sandbox_mode=False,
        env_context=env_context,
        node_runtime_root=root,
    )

    assert ctx.get("node").provider == "host"
    assert ctx.env()["BOX_AGENT_NODE"] == str(host_node)


def test_runtime_prompt_mentions_python_node_and_npm_rules(tmp_path: Path) -> None:
    sandbox = SandboxEnvironment(base_dir=tmp_path)
    _make_executable(sandbox.python_path)

    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        sandbox_env=sandbox,
        node_runtime_root=tmp_path / "missing-node",
    )
    out = build_skill_runtime_prompt(ctx)

    assert "## Skill Runtime Context" in out
    assert "- Python:" in out
    assert "$BOX_AGENT_PYTHON" in out
    assert "- Node:" in out
    assert "不可用" in out
    assert "npm install -g" in out
    assert "$BOX_AGENT_SKILL_TOOLS_ROOT" in out
    assert "标准 `python`/`python3`" in out
    assert "禁止 `sudo`" in out


def test_runtime_prompt_does_not_advertise_execute_code_when_sandbox_is_disabled(
    tmp_path: Path,
) -> None:
    ctx = build_skill_runtime_context(
        sandbox_mode=False,
        node_runtime_root=tmp_path / "missing-node",
    )

    out = build_skill_runtime_prompt(ctx)

    assert "本 session 不可用" in out
    assert "仅 `execute_code` 沙箱可用" not in out


def test_runtime_prompt_mentions_available_self_managed_node(tmp_path: Path) -> None:
    root = tmp_path / "node-runtime"
    node = root / "versions" / "node" / "bin" / "node"
    npm = root / "versions" / "node" / "bin" / "npm"
    npx = root / "versions" / "node" / "bin" / "npx"
    for path in (node, npm, npx):
        _make_executable(path)
    _write_node_manifest(root, node=node, npm=npm, npx=npx)

    ctx = build_skill_runtime_context(sandbox_mode=False, node_runtime_root=root)
    out = build_skill_runtime_prompt(ctx)

    assert "- Node:" in out
    assert "box_agent" in out
    assert "$BOX_AGENT_NODE" in out
    assert "$BOX_AGENT_NPM" in out
    assert "$BOX_AGENT_NPX" in out


def test_runtime_env_only_contains_existing_python_path(tmp_path: Path) -> None:
    sandbox = SandboxEnvironment(base_dir=tmp_path)
    assert not os.path.exists(sandbox.python_path)

    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        sandbox_env=sandbox,
        node_runtime_root=tmp_path / "missing-node",
    )

    assert ctx.get("python").status == "missing"
    assert ctx.env() == {}


def test_skill_execution_env_prefers_managed_tools_and_shared_browser(tmp_path: Path) -> None:
    node_dir = tmp_path / "managed-node" / "bin"
    python_dir = tmp_path / "managed-python" / "bin"
    node = node_dir / "node"
    npm = node_dir / "npm"
    npx = node_dir / "npx"
    python = python_dir / "python3"
    node_modules = tmp_path / "managed-node-modules"
    for executable in (node, npm, npx, python):
        _make_executable(executable)
    browser_root = tmp_path / ".box-agent" / "browsers"
    chromium = (
        browser_root
        / "chromium-1234"
        / "chrome-mac-arm64"
        / "Google Chrome for Testing.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome for Testing"
    )
    _make_executable(chromium)
    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        env_context={
            "runtimes": {
                "node": {
                    "path": str(node),
                    "npm": str(npm),
                    "npx": str(npx),
                    "node_modules": str(node_modules),
                    "ready": True,
                },
                "python": {"path": str(python), "ready": True},
            }
        },
    )

    env = build_skill_execution_env(
        ctx,
        base_env={"PATH": "/user/node/bin:/usr/bin"},
        platform_name="darwin",
        home_dir=tmp_path,
    )
    skill_tools = tmp_path / ".box-agent" / "skill-tools"
    path_entries = env["PATH"].split(":")

    assert path_entries[0] == str(skill_tools / "bin")
    assert path_entries.index(str(node_dir)) < path_entries.index("/user/node/bin")
    assert path_entries.index(str(python_dir)) < path_entries.index("/user/node/bin")
    assert env["NPM_CONFIG_PREFIX"] == str(skill_tools)
    assert env["PYTHONUSERBASE"] == str(skill_tools / "python")
    assert env["PYTHONPATH"].split(":")[0] == str(
        skill_tools
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    assert env["AGENT_BROWSER_EXECUTABLE_PATH"] == str(chromium)
    assert env["NODE_PATH"].split(":")[0] == str(skill_tools / "lib" / "node_modules")


def test_skill_execution_env_uses_windows_global_bin_layout(tmp_path: Path) -> None:
    node_dir = tmp_path / "managed-node"
    python_dir = tmp_path / "managed-python"
    node = node_dir / "node.exe"
    npm = node_dir / "npm.cmd"
    npx = node_dir / "npx.cmd"
    python = python_dir / "python.exe"
    for executable in (node, npm, npx, python):
        _make_executable(executable)
    browser_root = tmp_path / ".box-agent" / "browsers"
    chromium = browser_root / "chromium-4321" / "chrome-win64" / "chrome.exe"
    _make_executable(chromium)
    ctx = build_skill_runtime_context(
        sandbox_mode=True,
        env_context={
            "runtimes": {
                "node": {
                    "path": str(node),
                    "npm": str(npm),
                    "npx": str(npx),
                    "ready": True,
                },
                "python": {"path": str(python), "ready": True},
            }
        },
    )

    env = build_skill_execution_env(
        ctx,
        base_env={"PATH": "C:\\user-node;C:\\Windows"},
        platform_name="win32",
        home_dir=tmp_path,
    )
    skill_tools = tmp_path / ".box-agent" / "skill-tools"
    path_entries = env["PATH"].split(";")

    assert path_entries[0] == str(skill_tools)
    assert str(skill_tools / "python" / "Scripts") in path_entries
    assert path_entries.index(str(node_dir)) < path_entries.index("C:\\user-node")
    assert env["NPM_CONFIG_PREFIX"] == str(skill_tools)
    assert env["NODE_PATH"].split(";")[0] == str(skill_tools / "node_modules")
    assert env["PYTHONPATH"].split(";")[0] == str(
        skill_tools
        / "python"
        / f"Python{sys.version_info.major}{sys.version_info.minor}"
        / "site-packages"
    )
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["AGENT_BROWSER_EXECUTABLE_PATH"] == str(chromium)


def test_skill_pythonpath_supports_dependency_created_after_process_start(
    tmp_path: Path,
) -> None:
    ctx = build_skill_runtime_context(
        sandbox_mode=False,
        node_runtime_root=tmp_path / "missing-node",
        office_node_runtime_root=tmp_path / "missing-office-node",
    )
    env = {
        **os.environ,
        **build_skill_execution_env(
            ctx,
            base_env={"PATH": os.environ.get("PATH", "")},
            home_dir=tmp_path,
        ),
    }
    code = (
        "from pathlib import Path; import os; "
        "site = Path(os.environ['PYTHONPATH'].split(os.pathsep)[0]); "
        "site.mkdir(parents=True, exist_ok=True); "
        "site.joinpath('fresh_skill_dependency.py').write_text('VALUE = 42\\n'); "
        "import fresh_skill_dependency; "
        "assert fresh_skill_dependency.VALUE == 42"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
