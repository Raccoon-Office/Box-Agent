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
)


def _make_executable(path: Path) -> None:
    _write_executable(path, "#!/bin/sh\nexit 0\n")


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def _clear_python_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BOX_AGENT_PYTHON",
        "BOX_AGENT_PYTHON3",
        "BOX_AGENT_SANDBOX_PYTHON",
        "BOX_AGENT_BUNDLED_PYTHON",
    ):
        monkeypatch.delenv(name, raising=False)


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

    assert called == [python_path]
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


def test_fixed_session_id_hides_session_parameter(tmp_path: Path) -> None:
    tool = JupyterSandboxTool(
        workspace_dir=str(tmp_path / "workspace"),
        fixed_session_id="acp-session-1",
    )

    assert "session_id" not in tool.parameters["properties"]


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
    _write_executable(
        python_path,
        """#!/bin/sh
dir="$(dirname "$0")"
if [ "$1" = "-c" ]; then
  case "$2" in
    *"import pip"*) [ -f "$dir/pip-ready" ] && exit 0 || exit 1 ;;
    *"import ipykernel"*) [ -f "$dir/ipykernel-ready" ] && exit 0 || exit 1 ;;
  esac
fi
if [ "$1" = "-m" ] && [ "$2" = "ensurepip" ]; then
  touch "$dir/pip-ready"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  [ -f "$dir/pip-ready" ] || exit 8
  touch "$dir/ipykernel-ready"
  exit 0
fi
exit 1
""",
    )
    env = SandboxEnvironment(
        base_dir=tmp_path / "sandbox",
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": str(python_path)},
    )

    await env._verify_packages()

    assert (python_path.parent / "pip-ready").exists()
    assert (python_path.parent / "ipykernel-ready").exists()


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
    _write_executable(
        python_path,
        """#!/bin/sh
if [ "$1" = "-c" ]; then
  case "$2" in
    *"import pip"*) exit 0 ;;
    *"import ipykernel"*) exit 1 ;;
  esac
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  echo "pip failed" >&2
  exit 9
fi
exit 1
""",
    )
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


@pytest.mark.asyncio
async def test_fixed_session_id_ignores_caller_supplied_session_id(
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
    tool_a = JupyterSandboxTool(
        workspace_dir=str(tmp_path / "workspace-a"),
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": sys.executable},
        fixed_session_id="acp-session-a",
    )
    tool_b = JupyterSandboxTool(
        workspace_dir=str(tmp_path / "workspace-b"),
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": sys.executable},
        fixed_session_id="acp-session-b",
    )

    try:
        result_a = await tool_a.execute("secret = 41\nprint(secret)", session_id="shared-kernel")
        result_b = await tool_b.execute(
            "print(globals().get('secret', 'missing'))",
            session_id="shared-kernel",
        )
        result_a_again = await tool_a.execute("print(secret)", session_id="other-kernel")
    finally:
        await JupyterSandboxTool.shutdown_all()
        JupyterSandboxTool._sandbox_env = None
        JupyterSandboxTool._sandbox_env_key = None

    assert result_a.success is True
    assert "41" in result_a.content
    assert result_b.success is True
    assert "missing" in result_b.content
    assert result_a_again.success is True
    assert "41" in result_a_again.content


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
