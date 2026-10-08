"""Skill runtime discovery and prompt/env rendering."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from box_agent.tools.jupyter_tool import SandboxEnvironment

from box_agent.user_paths import configured_box_agent_home, state_path

RuntimeKind = Literal["python", "node"]
RuntimeProvider = Literal["box_agent", "host", "missing"]
RuntimeStatus = Literal["available", "missing", "unavailable"]

DEFAULT_NODE_RUNTIME_ROOT = state_path('runtimes/node')
DEFAULT_NODE_VERSION = "v24.15.0"
NODE_DIST_BASE_URL = "https://nodejs.org/dist"
_MAX_RUNTIME_PATH_LEN = 1024


def _publish_extracted_directory(source: Path, destination: Path) -> None:
    """Keep atomic publication while tolerating brief Windows scanner locks."""
    for attempt in range(4):
        try:
            source.rename(destination)
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 3:
                raise
            time.sleep(0.05 * 2 ** attempt)


@dataclass(frozen=True)
class SkillRuntime:
    kind: RuntimeKind
    status: RuntimeStatus
    provider: RuntimeProvider
    executable_path: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.status == "available"


@dataclass(frozen=True)
class SkillRuntimeContext:
    runtimes: dict[RuntimeKind, SkillRuntime]
    sandbox_mode: bool = False

    def get(self, kind: RuntimeKind) -> SkillRuntime:
        return self.runtimes[kind]

    def env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for runtime in self.runtimes.values():
            env.update(runtime.env_vars)
        return env


def build_skill_runtime_context(
    *,
    sandbox_mode: bool,
    env_context: Any | None = None,
    sandbox_env: SandboxEnvironment | None = None,
    node_runtime_root: Path | None = None,
    office_node_runtime_root: Path | None = None,
    shell_python_path: Path | None = None,
) -> SkillRuntimeContext:
    """Discover runtimes available to skills for this session."""
    host_runtimes = _env_context_runtimes(env_context)
    return SkillRuntimeContext(
        sandbox_mode=sandbox_mode,
        runtimes={
            "python": _build_python_runtime(
                sandbox_mode,
                host_runtimes,
                sandbox_env,
                shell_python_path=shell_python_path,
            ),
            "node": _build_node_runtime(
                host_runtimes,
                node_runtime_root=node_runtime_root,
                office_node_runtime_root=office_node_runtime_root,
            ),
        }
    )


def resolve_cli_shell_python() -> Path | None:
    """Return a non-venv Python suitable for third-party shell skills.

    Box-Agent's execute_code sandbox remains isolated.  Shell skills commonly
    run ``python -m pip install --user``; that command is rejected inside a
    normal virtualenv, so standalone CLI sessions prefer the interpreter that
    created the current environment when it is available.
    """
    if getattr(sys, "frozen", False):
        return bundled_win_python()
    for raw in (getattr(sys, "_base_executable", None), sys.executable):
        if isinstance(raw, str) and _is_executable_file(raw):
            return Path(raw)
    return None


def build_skill_runtime_prompt(ctx: SkillRuntimeContext) -> str:
    """Render runtime facts and rules for the ACP/CLI system prompt."""
    python = ctx.get("python")
    node = ctx.get("node")

    lines = ["## Skill Runtime Context"]

    if python.available:
        py_line = (
            f"- Python: {python.provider}，标准 `python`/`python3` 已指向受管运行时"
            "（也可用 `$BOX_AGENT_PYTHON`）"
        )
    elif ctx.sandbox_mode:
        py_line = "- Python: 仅 `execute_code` 沙箱可用，无 shell python"
    else:
        py_line = "- Python: 本 session 不可用；不要调用 `execute_code` 或 shell python"
    if python.notes:
        py_line += f" — {'; '.join(python.notes)}"
    lines.append(py_line)

    if node.available:
        node_env_vars = [
            name
            for name in ("BOX_AGENT_NODE", "BOX_AGENT_NPM", "BOX_AGENT_NPX")
            if name in node.env_vars
        ]
        via = " / ".join(f"`${name}`" for name in node_env_vars) or "configured runtime"
        node_line = (
            f"- Node: {node.provider}，标准 `node`/`npm`/`npx` 已指向受管运行时"
            f"（内部路径：{via}）"
        )
    else:
        node_line = "- Node: 不可用——skill 若依赖 Node 应直接报告依赖缺失，不要回退系统 node"
    if node.notes:
        node_line += f" — {'; '.join(node.notes)}"
    lines.append(node_line)

    lines.append(
        "- Rules: 第三方 skill 可使用标准运行时命令；仅在确认依赖缺失时安装。"
        "`npm install -g` 已被重定向到 `$BOX_AGENT_SKILL_TOOLS_ROOT`，"
        "禁止 `sudo` 或用 `--prefix` 绕到系统目录。"
    )
    return "\n".join(lines)


def _build_python_runtime(
    sandbox_mode: bool,
    host_runtimes: Any,
    sandbox_env: SandboxEnvironment | None,
    *,
    shell_python_path: Path | None = None,
) -> SkillRuntime:
    host = _runtime(host_runtimes, "python")
    if host is not None and bool(_runtime_field(host, "ready", False)) and _runtime_field(host, "path"):
        path = _safe_host_executable_path(_runtime_field(host, "path"))
        shell_path = _safe_host_executable_path(_runtime_field(host, "shell_path")) or path
        sandbox_path = _safe_host_executable_path(_runtime_field(host, "sandbox_path")) or path
        if path is None or shell_path is None or sandbox_path is None:
            return SkillRuntime(
                kind="python",
                status="unavailable",
                provider="host",
                notes=("Host Python runtime path was rejected or is not executable.",),
            )
        return SkillRuntime(
            kind="python",
            status="available",
            provider="host",
            executable_path=shell_path,
            env_vars={
                "BOX_AGENT_PYTHON": shell_path,
                "BOX_AGENT_PYTHON3": shell_path,
                "BOX_AGENT_SANDBOX_PYTHON": sandbox_path,
            },
            notes=_host_note(host),
        )

    if not sandbox_mode:
        return SkillRuntime(
            kind="python",
            status="missing",
            provider="missing",
            notes=("No Python runtime is configured for shell skills.",),
        )

    if getattr(sys, "frozen", False):
        # Windows still ships a portable Python in legacy/fat runtimes. macOS
        # and Linux frozen ACP runtimes receive Python from their host.
        if sys.platform == "win32":
            bundled = bundled_win_python()
            if bundled is not None:
                path = str(bundled)
                return SkillRuntime(
                    kind="python",
                    status="available",
                    provider="box_agent",
                    executable_path=path,
                    env_vars={
                        "BOX_AGENT_PYTHON": path,
                        "BOX_AGENT_PYTHON3": path,
                        "BOX_AGENT_SANDBOX_PYTHON": path,
                    },
                    notes=("Bundled Windows portable Python.",),
                )
        return SkillRuntime(
            kind="python",
            status="unavailable",
            provider="box_agent",
            notes=("Frozen runtime has no separate shell Python executable; use execute_code for Python code execution.",),
        )

    shell_path = (
        str(shell_python_path)
        if shell_python_path is not None
        and _is_executable_file(str(shell_python_path))
        else None
    )
    env = sandbox_env or SandboxEnvironment()
    python_path = Path(env.python_path)
    if python_path.is_file() and os.access(python_path, os.X_OK):
        sandbox_path = str(python_path)
        effective_shell_path = shell_path or sandbox_path
        return SkillRuntime(
            kind="python",
            status="available",
            provider="box_agent",
            executable_path=effective_shell_path,
            env_vars={
                "BOX_AGENT_PYTHON": effective_shell_path,
                "BOX_AGENT_PYTHON3": effective_shell_path,
                "BOX_AGENT_SANDBOX_PYTHON": sandbox_path,
            },
        )

    if shell_path is not None:
        return SkillRuntime(
            kind="python",
            status="available",
            provider="box_agent",
            executable_path=shell_path,
            env_vars={
                "BOX_AGENT_PYTHON": shell_path,
                "BOX_AGENT_PYTHON3": shell_path,
            },
            notes=(
                "Shell Python is available; execute_code will initialize its "
                "isolated sandbox on first use.",
            ),
        )

    return SkillRuntime(
        kind="python",
        status="missing",
        provider="box_agent",
        notes=("Sandbox venv Python executable does not exist yet; use execute_code for Python code execution.",),
    )


class NodeRuntimeManager:
    """Discover Box-Agent's self-managed Node runtime from a manifest."""

    def __init__(self, root: Path | None = None):
        if root is None and configured_box_agent_home() is not None:
            root = state_path("runtimes/node")
        self.root = (root or _bundled_node_runtime_root() or DEFAULT_NODE_RUNTIME_ROOT).expanduser()
        self.manifest_path = self.root / "manifest.json"
        self.downloads_dir = self.root / "downloads"
        self.versions_dir = self.root / "versions"
        state_root = DEFAULT_NODE_RUNTIME_ROOT if _is_bundled_node_runtime_root(self.root) else self.root
        self.sandbox_dir = state_root / "sandbox"
        self.node_modules_dir = self.sandbox_dir / "node_modules"

    def install_macos(
        self,
        *,
        version: str = DEFAULT_NODE_VERSION,
        platform_id: str | None = None,
        downloader: Any | None = None,
        base_url: str = NODE_DIST_BASE_URL,
    ) -> SkillRuntime:
        """Install the pinned official Node.js macOS runtime.

        This only supports Darwin arm64/x64. It downloads the official Node
        archive and SHASUMS256.txt, verifies the archive, extracts into this
        manager's ``versions`` directory, then atomically updates manifest.json.
        """
        target = platform_id or _detect_node_macos_platform()
        if target not in {"darwin-arm64", "darwin-x64"}:
            raise NodeRuntimeInstallError(f"Unsupported macOS Node platform: {target}")
        if not version.startswith("v"):
            raise NodeRuntimeInstallError("Node version must include the leading 'v'.")

        archive_name = f"node-{version}-{target}.tar.gz"
        version_dir = self.versions_dir / archive_name.removesuffix(".tar.gz")
        node = version_dir / "bin" / "node"
        npm = version_dir / "bin" / "npm"
        npx = version_dir / "bin" / "npx"

        if all(_is_executable_file(str(path)) for path in (node, npm, npx)):
            self._write_manifest(
                version=version,
                platform_id=target,
                node=node,
                npm=npm,
                npx=npx,
                node_modules=self.node_modules_dir,
            )
            return self.discover()

        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.downloads_dir / archive_name
        shasums_path = self.downloads_dir / f"SHASUMS256-{version}.txt"
        version_url = f"{base_url.rstrip('/')}/{version}"
        fetch = downloader or _download_url
        fetch(f"{version_url}/{archive_name}", archive_path)
        fetch(f"{version_url}/SHASUMS256.txt", shasums_path)

        expected = _checksum_for_archive(shasums_path.read_text(encoding="utf-8"), archive_name)
        actual = _sha256_file(archive_path)
        if actual != expected:
            raise NodeRuntimeInstallError(
                f"Checksum mismatch for {archive_name}: expected {expected}, got {actual}"
            )

        temp_dir = self.versions_dir / f".{version_dir.name}.tmp"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            _safe_extract_tar(archive_path, temp_dir)
            extracted = temp_dir / version_dir.name
            if not extracted.is_dir():
                raise NodeRuntimeInstallError(f"Node archive did not contain {version_dir.name}")
            if version_dir.exists():
                shutil.rmtree(version_dir)
            version_dir.parent.mkdir(parents=True, exist_ok=True)
            _publish_extracted_directory(extracted, version_dir)
        except Exception as exc:
            if version_dir.exists() and not all(_is_executable_file(str(path)) for path in (node, npm, npx)):
                shutil.rmtree(version_dir)
            if isinstance(exc, NodeRuntimeInstallError):
                raise
            raise NodeRuntimeInstallError(f"Failed to extract Node runtime archive: {exc}") from exc
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

        missing = [name for name, path in (("node", node), ("npm", npm), ("npx", npx)) if not _is_executable_file(str(path))]
        if missing:
            raise NodeRuntimeInstallError(f"Installed Node runtime is incomplete: missing {', '.join(missing)}")

        self._write_manifest(
            version=version,
            platform_id=target,
            node=node,
            npm=npm,
            npx=npx,
            node_modules=self.node_modules_dir,
        )
        return self.discover()

    def install_win(
        self,
        *,
        version: str = DEFAULT_NODE_VERSION,
        platform_id: str | None = None,
        downloader: Any | None = None,
        base_url: str = NODE_DIST_BASE_URL,
    ) -> SkillRuntime:
        """Install the pinned official Node.js Windows runtime.

        Mirrors :meth:`install_macos` but for ``win-x64``/``win-arm64``: pulls
        the official Node ``.zip`` archive plus ``SHASUMS256.txt``, verifies
        SHA-256, extracts into ``versions/``, then atomically writes
        ``manifest.json``.
        """
        target = platform_id or _detect_node_win_platform()
        if target not in {"win-x64", "win-arm64"}:
            raise NodeRuntimeInstallError(f"Unsupported Windows Node platform: {target}")
        if not version.startswith("v"):
            raise NodeRuntimeInstallError("Node version must include the leading 'v'.")

        archive_name = f"node-{version}-{target}.zip"
        version_dir = self.versions_dir / archive_name.removesuffix(".zip")
        node = version_dir / "node.exe"
        npm = version_dir / "npm.cmd"
        npx = version_dir / "npx.cmd"

        if all(_path_exists(path) for path in (node, npm, npx)):
            self._write_manifest(
                version=version,
                platform_id=target,
                node=node,
                npm=npm,
                npx=npx,
                node_modules=self.node_modules_dir,
            )
            return self.discover()

        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.downloads_dir / archive_name
        shasums_path = self.downloads_dir / f"SHASUMS256-{version}.txt"
        version_url = f"{base_url.rstrip('/')}/{version}"
        fetch = downloader or _download_url
        fetch(f"{version_url}/{archive_name}", archive_path)
        fetch(f"{version_url}/SHASUMS256.txt", shasums_path)

        expected = _checksum_for_archive(shasums_path.read_text(encoding="utf-8"), archive_name)
        actual = _sha256_file(archive_path)
        if actual != expected:
            raise NodeRuntimeInstallError(
                f"Checksum mismatch for {archive_name}: expected {expected}, got {actual}"
            )

        temp_dir = self.versions_dir / f".{version_dir.name}.tmp"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            _safe_extract_zip(archive_path, temp_dir)
            extracted = temp_dir / version_dir.name
            if not extracted.is_dir():
                raise NodeRuntimeInstallError(f"Node archive did not contain {version_dir.name}")
            if version_dir.exists():
                shutil.rmtree(version_dir)
            version_dir.parent.mkdir(parents=True, exist_ok=True)
            # ``Path.rename`` is a raw MoveFileEx on Win and trips WinError 5
            # if Defender / Search Indexer briefly holds a handle on the freshly
            # extracted node.exe. ``shutil.move`` falls back to copy+delete and
            # is more tolerant; retry once after a short pause as a final
            # safety net.
            try:
                shutil.move(str(extracted), str(version_dir))
            except PermissionError:
                import time
                time.sleep(2)
                shutil.move(str(extracted), str(version_dir))
        except Exception as exc:
            if version_dir.exists() and not all(_path_exists(path) for path in (node, npm, npx)):
                shutil.rmtree(version_dir)
            if isinstance(exc, NodeRuntimeInstallError):
                raise
            raise NodeRuntimeInstallError(f"Failed to extract Node runtime archive: {exc}") from exc
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

        missing = [name for name, path in (("node.exe", node), ("npm.cmd", npm), ("npx.cmd", npx)) if not _path_exists(path)]
        if missing:
            raise NodeRuntimeInstallError(f"Installed Node runtime is incomplete: missing {', '.join(missing)}")

        self._write_manifest(
            version=version,
            platform_id=target,
            node=node,
            npm=npm,
            npx=npx,
            node_modules=self.node_modules_dir,
        )
        return self.discover()

    def install_linux(
        self,
        *,
        version: str = DEFAULT_NODE_VERSION,
        platform_id: str | None = None,
        downloader: Any | None = None,
        base_url: str = NODE_DIST_BASE_URL,
    ) -> SkillRuntime:
        """Install the pinned official Node.js Linux runtime."""
        target = platform_id or _detect_node_linux_platform()
        if target not in {"linux-x64", "linux-arm64"}:
            raise NodeRuntimeInstallError(f"Unsupported Linux Node platform: {target}")
        if not version.startswith("v"):
            raise NodeRuntimeInstallError("Node version must include the leading 'v'.")

        archive_name = f"node-{version}-{target}.tar.gz"
        version_dir = self.versions_dir / archive_name.removesuffix(".tar.gz")
        node = version_dir / "bin" / "node"
        npm = version_dir / "bin" / "npm"
        npx = version_dir / "bin" / "npx"

        if all(_is_executable_file(str(path)) for path in (node, npm, npx)):
            self._write_manifest(
                version=version,
                platform_id=target,
                node=node,
                npm=npm,
                npx=npx,
                node_modules=self.node_modules_dir,
            )
            return self.discover()

        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.downloads_dir / archive_name
        shasums_path = self.downloads_dir / f"SHASUMS256-{version}.txt"
        version_url = f"{base_url.rstrip('/')}/{version}"
        fetch = downloader or _download_url
        fetch(f"{version_url}/{archive_name}", archive_path)
        fetch(f"{version_url}/SHASUMS256.txt", shasums_path)

        expected = _checksum_for_archive(
            shasums_path.read_text(encoding="utf-8"), archive_name
        )
        actual = _sha256_file(archive_path)
        if actual != expected:
            raise NodeRuntimeInstallError(
                f"Checksum mismatch for {archive_name}: expected {expected}, got {actual}"
            )

        temp_dir = self.versions_dir / f".{version_dir.name}.tmp"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            _safe_extract_tar(archive_path, temp_dir)
            extracted = temp_dir / version_dir.name
            if not extracted.is_dir():
                raise NodeRuntimeInstallError(
                    f"Node archive did not contain {version_dir.name}"
                )
            if version_dir.exists():
                shutil.rmtree(version_dir)
            version_dir.parent.mkdir(parents=True, exist_ok=True)
            _publish_extracted_directory(extracted, version_dir)
        except Exception as exc:
            if version_dir.exists() and not all(
                _is_executable_file(str(path)) for path in (node, npm, npx)
            ):
                shutil.rmtree(version_dir)
            if isinstance(exc, NodeRuntimeInstallError):
                raise
            raise NodeRuntimeInstallError(
                f"Failed to extract Node runtime archive: {exc}"
            ) from exc
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

        missing = [
            name
            for name, path in (("node", node), ("npm", npm), ("npx", npx))
            if not _is_executable_file(str(path))
        ]
        if missing:
            raise NodeRuntimeInstallError(
                f"Installed Node runtime is incomplete: missing {', '.join(missing)}"
            )

        self._write_manifest(
            version=version,
            platform_id=target,
            node=node,
            npm=npm,
            npx=npx,
            node_modules=self.node_modules_dir,
        )
        return self.discover()

    def install_for_current_platform(
        self, *, version: str = DEFAULT_NODE_VERSION
    ) -> SkillRuntime:
        """Install the managed Node build matching the current desktop platform."""
        if sys.platform == "darwin":
            return self.install_macos(version=version)
        if sys.platform == "win32":
            return self.install_win(version=version)
        if sys.platform.startswith("linux"):
            return self.install_linux(version=version)
        raise NodeRuntimeInstallError(
            f"Node auto-install does not support {sys.platform}."
        )

    def discover(self) -> SkillRuntime:
        if not self.manifest_path.exists():
            discovered = self._discover_from_versions()
            if discovered is not None:
                return discovered
            return SkillRuntime(
                kind="node",
                status="missing",
                provider="missing",
                notes=("No Box-Agent managed Node runtime manifest was found.",),
            )

        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return SkillRuntime(
                kind="node",
                status="unavailable",
                provider="box_agent",
                notes=(f"Box-Agent managed Node runtime manifest is unreadable: {exc}.",),
            )

        active = raw.get("active") if isinstance(raw, dict) else None
        if not isinstance(active, dict):
            return SkillRuntime(
                kind="node",
                status="unavailable",
                provider="box_agent",
                notes=("Box-Agent managed Node runtime manifest has no active runtime.",),
            )

        node = self._managed_path(active.get("node"))
        npm = self._managed_path(active.get("npm"))
        npx = self._managed_path(active.get("npx"))
        node_modules = self._managed_path(active.get("node_modules")) or str(self.node_modules_dir)

        missing = [
            name
            for name, path in (("node", node), ("npm", npm), ("npx", npx))
            if not path or not _is_executable_file(path)
        ]
        if missing:
            return SkillRuntime(
                kind="node",
                status="unavailable",
                provider="box_agent",
                notes=(f"Box-Agent managed Node runtime is incomplete: missing {', '.join(missing)}.",),
            )

        env_vars = self._env_vars(
            node=node,
            npm=npm,
            npx=npx,
            node_modules=node_modules,
        )
        version = active.get("version")
        notes = ()
        if isinstance(version, str) and version:
            notes = (f"Box-Agent managed Node version: {version}.",)
        return SkillRuntime(
            kind="node",
            status="available",
            provider="box_agent",
            executable_path=node,
            env_vars=env_vars,
            notes=notes,
        )

    def _discover_from_versions(self) -> SkillRuntime | None:
        """Read an Office-provisioned Node tree that predates runtime manifests."""
        if not self.versions_dir.is_dir():
            return None
        for version_dir in sorted(self.versions_dir.iterdir(), reverse=True):
            if not version_dir.is_dir():
                continue
            if sys.platform == "win32":
                node = version_dir / "node.exe"
                npm = version_dir / "npm.cmd"
                npx = version_dir / "npx.cmd"
                usable = all(_path_exists(path) for path in (node, npm, npx))
            else:
                node = version_dir / "bin" / "node"
                npm = version_dir / "bin" / "npm"
                npx = version_dir / "bin" / "npx"
                usable = all(
                    _is_executable_file(str(path)) for path in (node, npm, npx)
                )
            if not usable:
                continue
            return SkillRuntime(
                kind="node",
                status="available",
                provider="box_agent",
                executable_path=str(node),
                env_vars=self._env_vars(
                    node=str(node),
                    npm=str(npm),
                    npx=str(npx),
                    node_modules=str(self.node_modules_dir),
                ),
                notes=(f"Box-Agent managed Node tree: {version_dir.name}.",),
            )
        return None

    def _env_vars(self, *, node: str, npm: str, npx: str, node_modules: str) -> dict[str, str]:
        return {
            "BOX_AGENT_NODE": node,
            "BOX_AGENT_NPM": npm,
            "BOX_AGENT_NPX": npx,
            "NODE_PATH": node_modules,
        }

    def _managed_path(self, raw: Any) -> str | None:
        path = _safe_manifest_path(raw, base=self.root)
        if path is None:
            return None
        try:
            Path(path).resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        return path

    def _write_manifest(
        self,
        *,
        version: str,
        platform_id: str,
        node: Path,
        npm: Path,
        npx: Path,
        node_modules: Path,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "active": {
                "version": version,
                "platform": platform_id,
                "node": str(node),
                "npm": str(npm),
                "npx": str(npx),
                "node_modules": str(node_modules),
            }
        }
        tmp_path = self.manifest_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.manifest_path)


def _build_node_runtime(
    host_runtimes: Any,
    *,
    node_runtime_root: Path | None = None,
    office_node_runtime_root: Path | None = None,
) -> SkillRuntime:
    host = _runtime(host_runtimes, "node")
    if host is not None and bool(_runtime_field(host, "ready", False)) and _runtime_field(host, "path"):
        node_path = _safe_host_executable_path(_runtime_field(host, "path"))
        if node_path is None:
            return SkillRuntime(
                kind="node",
                status="unavailable",
                provider="host",
                notes=("Host Node runtime path was rejected or is not executable.",),
            )
        env_vars = {"BOX_AGENT_NODE": node_path}
        npm_path = _safe_host_executable_path(_runtime_field(host, "npm"))
        npx_path = _safe_host_executable_path(_runtime_field(host, "npx"))
        node_modules_path = _safe_host_runtime_path(_runtime_field(host, "node_modules"))
        if npm_path:
            env_vars["BOX_AGENT_NPM"] = npm_path
        if npx_path:
            env_vars["BOX_AGENT_NPX"] = npx_path
        if node_modules_path:
            env_vars["NODE_PATH"] = node_modules_path
        return SkillRuntime(
            kind="node",
            status="available",
            provider="host",
            executable_path=env_vars["BOX_AGENT_NODE"],
            env_vars=env_vars,
            notes=_host_note(host),
        )

    runtime = NodeRuntimeManager(root=node_runtime_root).discover()
    if node_runtime_root is not None or runtime.status != "missing":
        return runtime
    office_root = office_node_runtime_root
    if office_root is None:
        configured_office_root = os.environ.get("BOX_AGENT_OFFICE_NODE_RUNTIME_ROOT")
        office_root = (
            Path(configured_office_root).expanduser()
            if configured_office_root
            else state_path('box-agent-runtime/runtimes/node')
        )
    office_runtime = NodeRuntimeManager(root=office_root).discover()
    return office_runtime if office_runtime.available else runtime


def _env_context_runtimes(env_context: Any | None) -> Any:
    if env_context is None:
        return {}
    if isinstance(env_context, dict):
        return env_context.get("runtimes", {})
    return getattr(env_context, "runtimes", {})


def _runtime(host_runtimes: Any, kind: RuntimeKind) -> Any | None:
    if isinstance(host_runtimes, dict):
        return host_runtimes.get(kind)
    return getattr(host_runtimes, kind, None)


def _runtime_field(runtime: Any, field: str, default: Any = None) -> Any:
    if isinstance(runtime, dict):
        return runtime.get(field, default)
    return getattr(runtime, field, default)


def _host_note(host: Any) -> tuple[str, ...]:
    provider = _safe_host_runtime_provider(_runtime_field(host, "provider"))
    if provider:
        return (f"Host runtime provider: {provider}.",)
    return ()


def _safe_host_runtime_path(raw: Any) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    if not raw or len(raw) > _MAX_RUNTIME_PATH_LEN:
        return None
    if "`" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    if raw.startswith("/") or (
        len(raw) >= 3 and raw[1] == ":" and raw[2] in "\\/"
    ):
        return raw
    return None


def _safe_host_executable_path(raw: Any) -> str | None:
    path = _safe_host_runtime_path(raw)
    if path is None:
        return None
    return path if _is_executable_file(path) else None


def _safe_host_runtime_provider(raw: Any) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    if not raw or len(raw) > 64:
        return None
    if "`" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    if all(ch.isalnum() or ch in "_-" for ch in raw):
        return raw
    return None


def _safe_manifest_path(raw: Any, *, base: Path) -> str | None:
    if not isinstance(raw, str):
        return None
    if not raw or len(raw) > _MAX_RUNTIME_PATH_LEN:
        return None
    if "`" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    if not path.is_absolute():
        return None
    return str(path)


def _is_executable_file(path: str) -> bool:
    return Path(path).is_file() and os.access(path, os.X_OK)


def _sandbox_python_runtime_from_env() -> Path | None:
    for env_name in ("BOX_AGENT_SANDBOX_PYTHON", "BOX_AGENT_BUNDLED_PYTHON"):
        raw = os.environ.get(env_name)
        if raw and _is_executable_file(raw):
            return Path(raw)
    return None


def _bundled_node_runtime_root() -> Path | None:
    # Dev override: when ``BOX_AGENT_NODE_RUNTIME_ROOT`` is set and points at
    # a directory containing ``manifest.json``, use it. Lets dev (non-frozen)
    # runs reuse the Node runtime that the Electron host's
    # managedNodeDependencies.ts already extracted under
    # ``officev3/build-resources/box-agent-runtime/runtimes/node`` instead of
    # requiring a separate PyInstaller bundle.
    override = os.environ.get("BOX_AGENT_NODE_RUNTIME_ROOT")
    if override:
        override_path = Path(override)
        if (override_path / "manifest.json").exists():
            return override_path
    if not getattr(sys, "frozen", False):
        return None
    candidate = Path(sys.executable).resolve().parent.parent / "runtimes" / "node"
    if (candidate / "manifest.json").exists():
        return candidate
    return None


def _bundled_runtime_root() -> Path | None:
    """Return the PyInstaller frozen runtime root directory, or None if not frozen.

    Layout assumed: ``<root>/bin/box-agent-acp(.exe)`` so root is the parent
    of the directory containing ``sys.executable``. Win-only bundled tools
    (PortableGit / portable Python) live under ``<root>/runtime/``.
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent.parent


def bundled_win_bash() -> Path | None:
    """Locate the Windows-bundled PortableGit ``bash.exe`` if present.

    Dev override: when ``BOX_AGENT_BUNDLED_BASH`` is set and points at an
    existing ``bash.exe``, use it. Lets dev (non-frozen) runs exercise the
    bundled-bash code path without rebuilding the PyInstaller bundle.
    """
    if sys.platform != "win32":
        return None
    override = os.environ.get("BOX_AGENT_BUNDLED_BASH")
    if override:
        override_path = Path(override)
        if override_path.is_file():
            return override_path
    root = _bundled_runtime_root()
    if root is None:
        return None
    candidate = root / "runtime" / "PortableGit" / "usr" / "bin" / "bash.exe"
    return candidate if candidate.is_file() else None


def bundled_win_python() -> Path | None:
    """Locate the Windows-bundled portable ``python.exe`` if present."""
    if sys.platform != "win32":
        return None
    override = _sandbox_python_runtime_from_env()
    if override is not None:
        return override
    root = _bundled_runtime_root()
    if root is None:
        return None
    candidate = root / "runtime" / "python" / "python.exe"
    return candidate if candidate.is_file() else None


def _is_bundled_node_runtime_root(root: Path) -> bool:
    bundled = _bundled_node_runtime_root()
    if bundled is None:
        return False
    try:
        return root.resolve() == bundled.resolve()
    except OSError:
        return False


class NodeRuntimeInstallError(RuntimeError):
    """Node runtime installation failed without changing the active runtime."""


def _detect_node_win_platform() -> str:
    if sys.platform != "win32":
        raise NodeRuntimeInstallError(f"Node Win install requires Windows, got {sys.platform}.")
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "win-x64"
    if machine in {"arm64", "aarch64"}:
        return "win-arm64"
    raise NodeRuntimeInstallError(f"Unsupported Windows CPU architecture for Node runtime: {machine}")


def _detect_node_linux_platform() -> str:
    if not sys.platform.startswith("linux"):
        raise NodeRuntimeInstallError(
            f"Node Linux install requires Linux, got {sys.platform}."
        )
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux-x64"
    if machine in {"arm64", "aarch64"}:
        return "linux-arm64"
    raise NodeRuntimeInstallError(
        f"Unsupported Linux CPU architecture for Node runtime: {machine}"
    )


def _path_exists(path: Path) -> bool:
    return path.is_file()


def _detect_node_macos_platform() -> str:
    if sys.platform != "darwin":
        raise NodeRuntimeInstallError(f"Node auto-install currently supports macOS only, got {sys.platform}.")
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "darwin-arm64"
    if machine in {"x86_64", "amd64"}:
        return "darwin-x64"
    raise NodeRuntimeInstallError(f"Unsupported macOS CPU architecture for Node runtime: {machine}")


def _download_url(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as f:
            shutil.copyfileobj(response, f)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def _checksum_for_archive(shasums: str, archive_name: str) -> str:
    for line in shasums.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1] == archive_name:
            return parts[0].lower()
    raise NodeRuntimeInstallError(f"No SHA256 entry found for {archive_name}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_tar(archive_path: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError as exc:
                raise NodeRuntimeInstallError(f"Unsafe path in Node archive: {member.name}") from exc
            if member.issym() or member.islnk():
                link_target = (target.parent / member.linkname).resolve()
                try:
                    link_target.relative_to(dest_resolved)
                except ValueError as exc:
                    raise NodeRuntimeInstallError(f"Unsafe link in Node archive: {member.name}") from exc
        tar.extractall(dest)


def _safe_extract_zip(archive_path: Path, dest: Path) -> None:
    import zipfile

    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError as exc:
                raise NodeRuntimeInstallError(f"Unsafe path in Node archive: {member}") from exc
        zf.extractall(dest)
