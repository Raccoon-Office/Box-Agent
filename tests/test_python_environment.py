from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from box_agent.tools import python_environment as runtime
from box_agent.tools import jupyter_tool as jupyter
from box_agent.tools.runtime_environment_tool import RuntimeEnvironmentTool


@pytest.mark.asyncio
async def test_missing_python_has_specific_error_without_spawning(tmp_path, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
    result = await runtime.probe_python(str(tmp_path / "missing.exe"))
    assert result["error_code"] == "PYTHON_NOT_FOUND"
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing,expected", [([], None), (["ipykernel_launcher"], "IPYKERNEL_UNAVAILABLE"),
                                             (["PIL"], "PYTHON_PACKAGES_UNAVAILABLE")])
async def test_probe_reports_interpreter_and_missing_imports(tmp_path, monkeypatch, missing, expected):
    path = tmp_path / "python.exe"
    path.touch()
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(
        json.dumps({"version": "3.12.13", "missing_modules": missing}).encode(), b"")))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
    result = await runtime.probe_python(str(path), ["PIL"])
    assert result["error_code"] == expected
    assert result["version"] == "3.12.13"
    assert spawn.call_args.args[:3] == (str(path), "-E", "-B")
    assert "shell" not in spawn.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code", [(asyncio.TimeoutError(), "PYTHON_PROBE_TIMEOUT"),
                                        (OSError(), "PYTHON_UNRUNNABLE")])
async def test_probe_cleanup_and_errors_are_bounded(tmp_path, monkeypatch, error, code):
    path = tmp_path / "python.exe"
    path.touch()
    proc = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=error),
                           kill=Mock(), wait=AsyncMock())
    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    result = await runtime.probe_python(str(path))
    assert result["error_code"] == code
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_cancellation_kills_child(tmp_path, monkeypatch):
    path = tmp_path / "python.exe"
    path.touch()
    proc = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=asyncio.CancelledError),
                           kill=Mock(), wait=AsyncMock())
    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    with pytest.raises(asyncio.CancelledError):
        await runtime.probe_python(str(path))
    proc.kill.assert_called_once()


def test_discovery_is_bounded_and_deduplicates_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.os, "environ", {})
    python = tmp_path / "python.exe"
    monkeypatch.setattr(runtime.shutil, "which", lambda *args, **kwargs: str(python))
    candidates = runtime.python_candidates({"BOX_AGENT_SANDBOX_PYTHON": str(python),
                                            "BOX_AGENT_BUNDLED_PYTHON": str(python)})
    assert candidates == [{"path": str(python), "source": "host"}]


@pytest.mark.asyncio
async def test_load_finds_alternative_while_host_installs_without_ensure(tmp_path, monkeypatch):
    import box_agent.tools.runtime_environment_tool as environment_tool
    # macOS temporary paths contain /private; that is not a leaked credential.
    path = str(tmp_path / "private" / "python.exe")
    secret = "test-only-host-token-must-not-leak"
    requester = AsyncMock(return_value={"status": "installing", "progress": [], "env": {"TOKEN": secret}})
    probe = AsyncMock(return_value={"path": path, "status": "ready", "kernel_ready": True})
    monkeypatch.setattr(environment_tool, "probe_python", probe)
    result = await RuntimeEnvironmentTool(requester).execute("python", "load", path, ["matplotlib"])
    body = json.loads(result.content)
    assert result.success and body["status"] == "installing"
    assert body["environments"][0]["source"] == "explicit"
    assert body["environments"][0]["path"] == path
    assert "env" not in body
    assert secret not in result.content
    requester.assert_awaited_once_with({"capability": "python", "action": "status"})
    probe.assert_awaited_once_with(path, ["matplotlib"])


@pytest.fixture
def execution(tmp_path, monkeypatch):
    path = tmp_path / "python.exe"
    path.touch()
    monkeypatch.setattr(jupyter.JupyterSandboxTool, "_sessions", {})
    monkeypatch.setattr(jupyter, "probe_python", AsyncMock(return_value={"status": "ready"}))
    tool = jupyter.JupyterSandboxTool(workspace_dir=str(tmp_path))
    tool.runtime_preparer = AsyncMock(side_effect=RuntimeError("host installing"))
    monkeypatch.setattr(tool, "_get_workspace", lambda key: tmp_path)

    def create(key, workspace, env):
        return SimpleNamespace(sandbox_env=env, workspace=workspace, start=AsyncMock(),
                               is_alive=lambda: True, stop=AsyncMock())

    monkeypatch.setattr(tool, "_create_session", create)
    monkeypatch.setattr(tool, "_execute_session_code", lambda *args: ("2", [], None))
    return tool, path


@pytest.mark.asyncio
async def test_explicit_python_bypasses_host_and_persists_in_session(execution):
    tool, path = execution
    first = await tool.execute("print(1+1)", session_id="chosen", python_path=str(path))
    second = await tool.execute("print(2)", session_id="chosen")
    assert first.success and second.success
    assert tool._sessions["chosen"].sandbox_env.python_path == path
    tool.runtime_preparer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
async def test_child_python_checks_host_readiness_and_uses_refreshed_managed_path(
    execution, monkeypatch, ready,
):
    parent, path = execution

    async def prepare():
        if not ready:
            raise RuntimeError("host installing")
        parent.update_runtime_env({"BOX_AGENT_SANDBOX_PYTHON": str(path)})

    parent.runtime_preparer = AsyncMock(side_effect=prepare)
    monkeypatch.setattr(jupyter.SandboxEnvironment, "ensure_ready", AsyncMock())
    monkeypatch.setattr(jupyter.JupyterSandboxTool, "_sandbox_env", None)
    monkeypatch.setattr(jupyter.JupyterSandboxTool, "_sandbox_env_key", None)
    child = parent.fork_for_child("child")
    for name in ("_create_session", "_execute_session_code", "_get_workspace"):
        monkeypatch.setattr(child, name, getattr(parent, name))

    result = await child.execute("print(2)", session_id="child")

    parent.runtime_preparer.assert_awaited_once()
    assert result.success is ready
    if ready:
        session = child._sessions[child._session_key("child")]
        assert session.sandbox_env.python_path == path
        assert child.runtime_env is not parent.runtime_env
        assert child.runtime_env["BOX_AGENT_SANDBOX_PYTHON"] == str(path)
    else:
        assert "RUNTIME_NOT_READY" in result.error
        assert child._sessions == {}


@pytest.mark.asyncio
async def test_changing_python_requires_new_session_not_silent_state_loss(execution, tmp_path):
    tool, path = execution
    other = tmp_path / "other.exe"
    other.touch()
    assert (await tool.execute("print(1)", session_id="one", python_path=str(path))).success
    rejected = await tool.execute("print(1)", session_id="one", python_path=str(other))
    assert "PYTHON_ENVIRONMENT_CHANGED" in rejected.error
    assert (await tool.execute("print(1)", session_id="two", python_path=str(other))).success
    assert tool._sessions["one"].sandbox_env.python_path == path


@pytest.mark.asyncio
async def test_missing_ipykernel_never_attempts_kernel_or_host_install(execution, monkeypatch):
    tool, path = execution
    monkeypatch.setattr(jupyter, "probe_python", AsyncMock(return_value={
        "status": "missing_packages", "error_code": "IPYKERNEL_UNAVAILABLE", "path": str(path),
    }))
    create = Mock()
    monkeypatch.setattr(tool, "_create_session", create)
    result = await tool.execute("print(1)", python_path=str(path))
    assert not result.success and "IPYKERNEL_UNAVAILABLE" in result.error
    create.assert_not_called()
    tool.runtime_preparer.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_explicit_python_does_not_fall_back_to_host(execution, tmp_path):
    tool, _ = execution
    result = await tool.execute("print(1)", python_path=str(tmp_path / "gone.exe"))
    assert result.error.startswith("PYTHON_NOT_FOUND")
    tool.runtime_preparer.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_python_never_uses_shared_fallback_packages(execution, monkeypatch):
    tool, path = execution
    assert (await tool.execute("print(1)", python_path=str(path), session_id="one")).success
    env = tool._sessions["one"].sandbox_env
    monkeypatch.setenv("PYTHONPATH", "unrelated/site-packages")
    spec = env.get_kernel_spec()
    assert spec["argv"][:2] == [str(path), "-E"]
    assert "env" not in spec
    assert "PYTHONPATH" not in env._subprocess_env()


@pytest.mark.asyncio
async def test_missing_business_package_does_not_modify_user_environment_automatically(execution, monkeypatch):
    tool, path = execution
    monkeypatch.setattr(tool, "_execute_session_code", lambda *args: ("", [], "ModuleNotFoundError: No module named 'pandas'"))
    install = AsyncMock()
    monkeypatch.setattr(jupyter.SandboxEnvironment, "install_packages", install)
    result = await tool.execute("import pandas", python_path=str(path))
    assert not result.success and "pandas" in result.error
    install.assert_not_called()


@pytest.mark.asyncio
async def test_real_selected_python_executes_and_retains_variables_without_host(tmp_path, monkeypatch):
    import sys

    pytest.importorskip("ipykernel")
    monkeypatch.setattr(jupyter, "SANDBOX_BASE_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(jupyter.JupyterSandboxTool, "_sessions", {})
    tool = jupyter.JupyterSandboxTool(workspace_dir=str(tmp_path), process_owner_id="probe")
    tool.runtime_preparer = AsyncMock(side_effect=RuntimeError("host not ready"))
    try:
        first = await tool.execute("import sys\nvalue = 40\nprint(value + 2)\nprint(sys.executable)", python_path=sys.executable,
                                   session_id="real")
        assert first.success, first.error
        assert "42" in first.content
        assert sys.executable.lower() in first.content.lower()
        second = await tool.execute("print(value + 3)", session_id="real")
        assert second.success, second.error
        assert "43" in second.content
        tool.runtime_preparer.assert_not_called()
    finally:
        await tool._discard_session("real")


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_child_kernel_receives_refreshed_host_environment(tmp_path, monkeypatch, explicit):
    import sys

    pytest.importorskip("ipykernel")
    monkeypatch.setattr(jupyter, "SANDBOX_BASE_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(jupyter, "RUNTIME_PACKAGES_DIR", tmp_path / "packages")
    monkeypatch.setattr(jupyter.JupyterSandboxTool, "_sessions", {})
    monkeypatch.setattr(jupyter.JupyterSandboxTool, "_sandbox_env", None)
    monkeypatch.setattr(jupyter.JupyterSandboxTool, "_sandbox_env_key", None)
    monkeypatch.setattr(jupyter.SandboxEnvironment, "_verify_packages", AsyncMock())
    parent = jupyter.JupyterSandboxTool(
        workspace_dir=str(tmp_path / "workspace"),
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": sys.executable},
        process_owner_id="parent",
    )
    parent._get_sandbox_env()
    node = str(tmp_path / "managed-node")
    parent.update_runtime_env({"BOX_AGENT_NODE": node})
    child = parent.fork_for_child("child")
    try:
        result = await child.execute(
            "import os\nprint(os.environ.get('BOX_AGENT_NODE', 'missing-runtime'))",
            session_id="runtime",
            **({"python_path": sys.executable} if explicit else {}),
        )
        assert result.success, result.error
        assert node in result.content
        spec = tmp_path / "sandbox" / "kernelspec" / "box-agent-sandbox" / "kernel.json"
        assert "BOX_AGENT_NODE" not in spec.read_text()
    finally:
        await child.shutdown_owned_sessions()


@pytest.mark.asyncio
async def test_explicit_missing_interpreter_never_resolves_host_fallback(tmp_path):
    host = tmp_path / "host.exe"
    host.touch()
    missing = tmp_path / "deleted.exe"
    env = jupyter.SandboxEnvironment(
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": str(host)}, python_path=missing,
    )
    assert env.python_path == missing
    with pytest.raises(RuntimeError, match="PYTHON_NOT_FOUND"):
        await env.ensure_ready()
    env._ready = True
    with pytest.raises(RuntimeError, match="PYTHON_NOT_FOUND"):
        await env.ensure_ready()


@pytest.mark.asyncio
async def test_load_does_not_probe_host_files_while_installer_is_writing(tmp_path, monkeypatch):
    import box_agent.tools.runtime_environment_tool as environment_tool
    candidate = {"path": str(tmp_path / "host.exe"), "source": "host"}
    monkeypatch.setattr(environment_tool, "python_candidates", lambda *args: [candidate])
    probe = AsyncMock()
    monkeypatch.setattr(environment_tool, "probe_python", probe)
    result = await RuntimeEnvironmentTool(AsyncMock(return_value={"status": "installing"})).execute("python", "load")
    assert json.loads(result.content)["environments"][0]["status"] == "installing"
    probe.assert_not_called()
