"""Tests for host-provided Python runtime handling in the sandbox."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from box_agent.tools import jupyter_tool
from box_agent.tools.jupyter_tool import (
    InProcessKernelSession,
    JupyterKernelSession,
    JupyterSandboxTool,
    SandboxEnvironment,
    _communicate_sandbox_process,
    _start_sandbox_kernel,
)


def _make_executable(path: Path) -> None:
    _write_executable(path, "#!/bin/sh\nexit 0\n")


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def _write_python_executable(path: Path, body: str, monkeypatch) -> None:
    """Launch a synthetic executable protocol with the current Python on any OS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    spawn = jupyter_tool.asyncio.create_subprocess_exec

    async def create_process(executable, *args, **kwargs):
        if str(executable) == str(path):
            return await spawn(sys.executable, str(path), *args, **kwargs)
        return await spawn(executable, *args, **kwargs)

    monkeypatch.setattr(jupyter_tool.asyncio, "create_subprocess_exec", create_process)


def _clear_python_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BOX_AGENT_PYTHON",
        "BOX_AGENT_PYTHON3",
        "BOX_AGENT_SANDBOX_PYTHON",
        "BOX_AGENT_BUNDLED_PYTHON",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_sandbox_bootstrap_process_timeout_terminates_child() -> None:
    proc = await jupyter_tool.asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=jupyter_tool.asyncio.subprocess.PIPE,
        stderr=jupyter_tool.asyncio.subprocess.PIPE,
    )

    with pytest.raises(RuntimeError, match="probe timed out"):
        await _communicate_sandbox_process(
            proc,
            operation="Sandbox test probe",
            timeout=0.01,
        )

    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_sandbox_kernel_startup_timeout_is_bounded() -> None:
    class HangingKernelManager:
        def __init__(self) -> None:
            self.shutdown_called = False

        async def _async_start_kernel(self) -> None:
            await jupyter_tool.asyncio.Event().wait()

        async def _async_shutdown_kernel(self, now: bool = False) -> None:
            self.shutdown_called = now

    manager = HangingKernelManager()

    with pytest.raises(RuntimeError, match="kernel startup timed out"):
        await _start_sandbox_kernel(manager, timeout=0.01)

    assert manager.shutdown_called is True


def test_sandbox_env_accepts_host_python_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_python_runtime_env(monkeypatch)
    python_path = tmp_path / "runtime" / "python" / "bin" / "python"
    _make_executable(python_path)
    monkeypatch.setenv("BOX_AGENT_SANDBOX_PYTHON", str(python_path))
    monkeypatch.setattr(jupyter_tool.sys, "platform", "darwin")

    env = SandboxEnvironment(base_dir=tmp_path / "sandbox")

    assert env.python_path == python_path
    assert env._bundled_override is True


@pytest.mark.asyncio
async def test_frozen_host_python_verifies_packages_before_bundled_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_python_runtime_env(monkeypatch)
    python_path = tmp_path / "runtime" / "python" / "bin" / "python"
    _make_executable(python_path)
    monkeypatch.setenv("BOX_AGENT_SANDBOX_PYTHON", str(python_path))
    monkeypatch.setattr(jupyter_tool, "IS_FROZEN", True)
    called: list[Path] = []

    async def fake_verify(self: SandboxEnvironment, on_progress=None) -> None:
        called.append(self.python_path)

    monkeypatch.setattr(SandboxEnvironment, "_verify_packages", fake_verify)
    env = SandboxEnvironment(base_dir=tmp_path / "sandbox")

    await env.ensure_ready()

    assert called == ([] if sys.platform == "win32" else [python_path])
    assert env._ready is True


def test_frozen_host_python_uses_subprocess_kernel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_python_runtime_env(monkeypatch)
    python_path = tmp_path / "runtime" / "python" / "bin" / "python"
    _make_executable(python_path)
    monkeypatch.setenv("BOX_AGENT_SANDBOX_PYTHON", str(python_path))
    monkeypatch.setattr(jupyter_tool, "IS_FROZEN", True)
    env = SandboxEnvironment(base_dir=tmp_path / "sandbox")
    tool = JupyterSandboxTool(workspace_dir=str(tmp_path / "workspace"))

    session = tool._create_session("session", tmp_path / "workspace", env)

    assert isinstance(session, JupyterKernelSession)


def test_sandbox_tool_accepts_runtime_env_python_without_process_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_python_runtime_env(monkeypatch)
    python_path = tmp_path / "runtime" / "python" / "bin" / "python"
    _make_executable(python_path)
    tool = JupyterSandboxTool(
        workspace_dir=str(tmp_path / "workspace"),
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": str(python_path)},
    )

    env = tool._get_sandbox_env()

    assert env.python_path == python_path
    assert env.python_override_path == python_path
    assert env._bundled_override is True


def test_sandbox_subprocess_env_includes_host_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_python_runtime_env(monkeypatch)
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.setattr(jupyter_tool, "RUNTIME_PACKAGES_DIR", tmp_path / "runtime-packages")
    python_path = tmp_path / "runtime" / "python" / "bin" / "python"
    _make_executable(python_path)
    env = SandboxEnvironment(
        base_dir=tmp_path / "sandbox",
        runtime_env={
            "BOX_AGENT_SANDBOX_PYTHON": str(python_path),
            "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
        },
    )

    subprocess_env = env._subprocess_env()

    assert subprocess_env is not None
    assert subprocess_env["PIP_INDEX_URL"] == "https://pypi.tuna.tsinghua.edu.cn/simple"
    assert subprocess_env["PYTHONPATH"].split(jupyter_tool.os.pathsep)[0] == str(
        tmp_path / "runtime-packages"
    )


def test_required_modules_cover_officev3_provisioned_package_surface() -> None:
    required = SandboxEnvironment._REQUIRED_MODULES

    for module_name, package_name in {
        "ipykernel": "ipykernel",
        "requests": "requests",
        "yaml": "pyyaml",
        "docx": "python-docx",
        "pypdf": "pypdf",
        "pdfplumber": "pdfplumber",
        "reportlab": "reportlab",
        "pptx": "python-pptx",
        "pip": "pip",
    }.items():
        assert required[module_name] == package_name


def test_local_sandbox_prefers_uv_for_package_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    uv_path = tmp_path / "bin" / "uv"
    monkeypatch.setattr(
        jupyter_tool.shutil,
        "which",
        lambda *_args, **_kwargs: str(uv_path),
    )
    env = SandboxEnvironment(base_dir=tmp_path / "sandbox")

    command = env._venv_install_command(["pandas", "numpy"])

    assert command == [
        str(uv_path),
        "pip",
        "install",
        "--python",
        str(env.python_path),
        "--quiet",
        "pandas",
        "numpy",
    ]


@pytest.mark.asyncio
async def test_host_python_bootstraps_pip_before_installing_missing_packages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_python_runtime_env(monkeypatch)
    monkeypatch.setattr(jupyter_tool, "RUNTIME_PACKAGES_DIR", tmp_path / "runtime-packages")
    monkeypatch.setattr(
        SandboxEnvironment,
        "_REQUIRED_MODULES",
        {"pip": "pip", "ipykernel": "ipykernel"},
    )
    python_path = tmp_path / "runtime" / "python" / "bin" / "python"
    _write_python_executable(python_path, """
import sys
from pathlib import Path
root = Path(__file__).parent
args = sys.argv[1:]
if args[0] == "-c":
    package = args[1].split()[-1]
    sys.exit(0 if (root / (package + "-ready")).exists() else 1)
if args == ["-m", "ensurepip"] or args[:2] == ["-m", "ensurepip"]:
    (root / "pip-ready").touch()
    sys.exit(0)
if args[:2] == ["-m", "pip"]:
    if not (root / "pip-ready").exists():
        sys.exit(8)
    (root / "ipykernel-ready").touch()
    sys.exit(0)
sys.exit(1)
""", monkeypatch)
    env = SandboxEnvironment(
        base_dir=tmp_path / "sandbox",
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": str(python_path)},
    )

    await env._verify_packages()

    assert (python_path.parent / "pip-ready").exists()
    assert (python_path.parent / "ipykernel-ready").exists()


@pytest.mark.asyncio
async def test_sandbox_python_uses_uv_when_ensurepip_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python_path = tmp_path / "sandbox" / "venv" / "bin" / "python"
    _write_python_executable(python_path, """
import sys
from pathlib import Path
args = sys.argv[1:]
if args == ["-c", "import pip"]:
    sys.exit(0 if (Path(__file__).parent / "pip-ready").exists() else 1)
print("No module named ensurepip", file=sys.stderr)
sys.exit(1)
""", monkeypatch)
    uv_path = tmp_path / "bin" / "uv"
    _write_python_executable(uv_path, """
import sys
from pathlib import Path
assert sys.argv[1:3] == ["pip", "install"]
python = Path(sys.argv[sys.argv.index("--python") + 1])
(python.parent / "pip-ready").touch()
""", monkeypatch)
    monkeypatch.setattr(
        jupyter_tool.shutil,
        "which",
        lambda *_args, **_kwargs: str(uv_path),
    )
    env = SandboxEnvironment(base_dir=tmp_path / "sandbox")
    env.python_path = python_path

    await env._ensure_pip_available(None, None)

    assert (python_path.parent / "pip-ready").exists()


@pytest.mark.asyncio
async def test_host_python_missing_package_install_failure_blocks_sandbox_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_python_runtime_env(monkeypatch)
    monkeypatch.setattr(jupyter_tool, "RUNTIME_PACKAGES_DIR", tmp_path / "runtime-packages")
    monkeypatch.setattr(
        SandboxEnvironment,
        "_REQUIRED_MODULES",
        {"pip": "pip", "ipykernel": "ipykernel"},
    )
    python_path = tmp_path / "runtime" / "python" / "bin" / "python"
    _write_python_executable(python_path, """
import sys
args = sys.argv[1:]
if args[0] == "-c":
    sys.exit(0 if args[1] == "import pip" else 1)
if args[:2] == ["-m", "pip"]:
    print("pip failed", file=sys.stderr)
    sys.exit(9)
sys.exit(1)
""", monkeypatch)
    env = SandboxEnvironment(
        base_dir=tmp_path / "sandbox",
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": str(python_path)},
    )

    with pytest.raises(RuntimeError, match="Failed to install missing host python"):
        await env._verify_packages()


@pytest.mark.asyncio
async def test_execute_code_runs_with_runtime_env_python(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_python_runtime_env(monkeypatch)
    monkeypatch.setattr(jupyter_tool, "SANDBOX_BASE_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(jupyter_tool, "RUNTIME_PACKAGES_DIR", tmp_path / "runtime-packages")
    monkeypatch.setattr(SandboxEnvironment, "_REQUIRED_MODULES", {"ipykernel": "ipykernel"})
    await JupyterSandboxTool.shutdown_all()
    JupyterSandboxTool._sandbox_env = None
    JupyterSandboxTool._sandbox_env_key = None
    tool = JupyterSandboxTool(
        workspace_dir=str(tmp_path / "workspace"),
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": sys.executable},
    )

    try:
        result = await tool.execute("print('external-python-ok')", session_id="runtime-env")
    finally:
        await JupyterSandboxTool.shutdown_all()
        JupyterSandboxTool._sandbox_env = None
        JupyterSandboxTool._sandbox_env_key = None

    assert result.success is True
    assert "external-python-ok" in result.content


def test_frozen_without_host_python_keeps_in_process_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_python_runtime_env(monkeypatch)
    monkeypatch.setattr(jupyter_tool, "IS_FROZEN", True)
    env = SandboxEnvironment(base_dir=tmp_path / "sandbox")
    tool = JupyterSandboxTool(workspace_dir=str(tmp_path / "workspace"))

    session = tool._create_session("session", tmp_path / "workspace", env)

    assert isinstance(session, InProcessKernelSession)
