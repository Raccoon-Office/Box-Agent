from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from box_agent.tools.runtime_environment_tool import RuntimeEnvironmentTool
from box_agent.tools.jupyter_tool import JupyterSandboxTool, SandboxEnvironment


def test_environment_guidance_reads_more_logs_only_when_needed():
    description = RuntimeEnvironmentTool(AsyncMock()).description
    assert "logTail is only a bounded recent excerpt" in description
    assert "If it is insufficient to diagnose a failure" in description
    assert "read_file on the returned logPath with offset/limit" in description
    assert "do not load the whole log" in description
    assert "normal file permissions" in description
    assert "not instructions or proof of current readiness" in description


def test_environment_and_python_tool_guidance_uses_only_managed_runtimes():
    environment = RuntimeEnvironmentTool(AsyncMock())
    python = JupyterSandboxTool()
    assert "Use only application-managed Python, Node and Git" in environment.description
    assert "diagnostic information, not permission to switch environments" in environment.description
    assert "Do not use or install alternate runtimes" in environment.description
    assert "Use only application-managed Python" in python.description
    assert 'local_environment(action="ensure", capability="python")' in python.description
    assert "Do not use or install alternate runtimes" in python.description
    for tool in (environment, python):
        assert "application-managed Python" in tool.parameters["properties"]["python_path"]["description"]
    assert "Bypasses host preparation" not in python.parameters["properties"]["python_path"]["description"]


@pytest.mark.parametrize("is_windows,bundled_bash", [(True, None), (True, "managed/bash.exe"), (False, None)])
def test_shell_guidance_uses_managed_runtimes_without_changing_shell_syntax(tmp_path, is_windows, bundled_bash):
    from box_agent.tools.bash_tool import BashTool

    tool = BashTool(workspace_dir=str(tmp_path))
    tool.is_windows = is_windows
    tool._bundled_win_bash = bundled_bash
    description = tool.description
    shell = "PowerShell" if is_windows and bundled_bash is None else "bash"
    assert description.startswith(f"Execute {shell} commands")
    assert "Use only application-managed Python, Node and Git" in description
    assert 'local_environment(action="ensure")' in description
    assert "Do not use or install alternate runtimes" in description
    assert "otherwise check for an available Python executable" not in description


@pytest.mark.asyncio
async def test_ready_status_omits_logs_without_installing_or_exposing_environment():
    requester = AsyncMock(return_value={
        "status": "ready", "logTail": "installation verified", "env": {
            "BOX_AGENT_SANDBOX_PYTHON": "C:/runtime/python.exe", "API_TOKEN": "private",
        },
    })
    tool = RuntimeEnvironmentTool(requester)
    applied = []
    tool.apply_environment = applied.append
    result = await tool.execute("python")
    assert result.success
    assert "logTail" not in json.loads(result.content)
    assert "env" not in json.loads(result.content)
    assert applied == [{"BOX_AGENT_SANDBOX_PYTHON": "C:/runtime/python.exe"}]
    requester.assert_awaited_once_with({"capability": "python", "action": "status"})


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["python", "node", "git", "browser", "chromium"])
@pytest.mark.parametrize("action", ["status", "load", "ensure"])
async def test_ready_results_keep_paths_and_shell_without_installer_details(monkeypatch, capability, action):
    import box_agent.tools.runtime_environment_tool as environment_module

    monkeypatch.setattr(environment_module, "python_candidates", lambda *args: [])
    monkeypatch.setattr(environment_module.shutil, "which", lambda *args, **kwargs: None)
    response = {
        "status": "ready", "capability": capability, "installerRunning": False,
        "components": [{"id": capability, "ready": True}], "progress": [{"phase": "all-done"}],
        "logTail": "old download details", "logPath": "installer.log", "error": None,
        "env": {"PATH": "internal-path"},
    }
    if capability == "browser":
        response.update(mcp={"state": "connected"}, environments=[{"path": "browser.exe"}])
    if capability == "chromium":
        response["environments"] = [{"path": "chromium.exe", "source": "host"}]
    requester = AsyncMock(return_value=response)
    tool = RuntimeEnvironmentTool(requester)
    shell = {"name": "bash", "instruction": "Use bash syntax."}
    tool.apply_environment = lambda env: shell
    result = await tool.execute(capability, action)
    body = json.loads(result.content)
    assert result.success
    assert body["status"] == "ready"
    assert body["shell"] == shell
    assert "environments" in body
    assert not {"components", "progress", "logTail", "logPath", "error", "env"} & body.keys()
    assert response["logTail"] == "old download details"
    requester.assert_awaited_once_with({"capability": capability, "action": "status"})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["missing", "installing", "failed"])
async def test_not_ready_status_retains_diagnostics(status):
    diagnostics = {
        "status": status, "components": [{"id": "git", "ready": False}],
        "progress": [{"phase": "download"}], "logTail": "diagnostic details",
        "logPath": "installer.log", "error": "install failed" if status == "failed" else None,
    }
    result = await RuntimeEnvironmentTool(AsyncMock(return_value=diagnostics)).execute("git")
    body = json.loads(result.content)
    assert all(body[key] == value for key, value in diagnostics.items())


@pytest.mark.asyncio
async def test_load_retains_probe_failure_when_host_is_ready(monkeypatch):
    import box_agent.tools.runtime_environment_tool as environment_module

    monkeypatch.setattr(environment_module, "probe_python", AsyncMock(return_value={
        "status": "missing_packages", "missing_modules": ["ipykernel_launcher"],
    }))
    requester = AsyncMock(return_value={"status": "ready", "logTail": "diagnostic details"})
    result = await RuntimeEnvironmentTool(requester).execute("python", "load", python_path="python.exe")
    body = json.loads(result.content)
    assert body["logTail"] == "diagnostic details"
    assert body["environments"][0]["missing_modules"] == ["ipykernel_launcher"]


@pytest.mark.asyncio
async def test_ensure_checks_status_then_joins_host_installation():
    requester = AsyncMock(side_effect=[{"status": "installing"}, {
        "status": "ready", "logTail": "download complete", "progress": [{"phase": "all-done"}],
    }])
    result = await RuntimeEnvironmentTool(requester).execute("python", "ensure")
    assert result.success
    assert "logTail" not in json.loads(result.content)
    assert "progress" not in json.loads(result.content)
    assert [call.args[0]["action"] for call in requester.await_args_list] == ["status", "ensure"]


@pytest.mark.asyncio
async def test_failed_preparation_returns_reason_and_log_without_looping():
    requester = AsyncMock(side_effect=[{"status": "missing"}, {
        "status": "failed", "error": "archive verification failed", "logTail": "sha256 mismatch",
    }])
    result = await RuntimeEnvironmentTool(requester).execute("python", "ensure")
    assert not result.success
    assert result.error == "archive verification failed"
    assert "sha256 mismatch" in result.content
    assert requester.await_count == 2


@pytest.mark.asyncio
async def test_cancellation_propagates_without_claiming_installation_failed():
    requester = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await RuntimeEnvironmentTool(requester).ensure_python()


@pytest.mark.asyncio
async def test_rejects_arbitrary_capability_without_contacting_host():
    requester = AsyncMock()
    result = await RuntimeEnvironmentTool(requester).execute("https://example.org/installer", "ensure")
    assert not result.success
    requester.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "load"])
async def test_browser_reports_installation_and_connection_separately_without_installing(action):
    requester = AsyncMock(return_value={
        "status": "ready", "installationReady": True, "installerRunning": False,
        "mcp": {"state": "failed", "error": "connection timeout"},
        "logTail": "download verified", "environments": [{"path": "C:/browser/chrome.exe"}],
        "env": {"BOX_AGENT_BROWSER_EXECUTABLE_PATH": "C:/browser/chrome.exe", "API_TOKEN": "private"},
    })
    tool = RuntimeEnvironmentTool(requester)
    applied = []
    tool.apply_environment = applied.append
    result = await tool.execute("browser", action)
    body = json.loads(result.content)
    assert result.success
    assert body["installationReady"] is True
    assert body["mcp"]["state"] == "failed"
    assert body["logTail"] == "download verified"
    assert body["environments"][0]["path"] == "C:/browser/chrome.exe"
    assert "private" not in result.content
    assert applied == [{"BOX_AGENT_BROWSER_EXECUTABLE_PATH": "C:/browser/chrome.exe"}]
    requester.assert_awaited_once_with({"capability": "browser", "action": "status"})


@pytest.mark.asyncio
async def test_browser_ensure_joins_the_host_installer():
    requester = AsyncMock(side_effect=[{"status": "installing"}, {
        "status": "ready", "installationReady": True, "mcp": {"state": "connecting"},
    }])
    result = await RuntimeEnvironmentTool(requester).execute("browser", "ensure")
    assert result.success
    assert json.loads(result.content)["mcp"]["state"] == "connecting"
    assert [call.args[0] for call in requester.await_args_list] == [
        {"capability": "browser", "action": "status"},
        {"capability": "browser", "action": "ensure"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["installing", "missing", "failed"])
async def test_execute_code_returns_facts_without_waiting_or_installing(monkeypatch, status):
    tool = JupyterSandboxTool()
    requester = AsyncMock(return_value={
        "status": status, "installerRunning": status == "installing",
        "components": [{"id": "python-packages", "ready": False, "missing": ["ipykernel"]}],
        "logTail": "pip pending" if status == "installing" else "offline",
    })
    environment = RuntimeEnvironmentTool(requester)
    tool.runtime_preparer = environment.ensure_python
    get_env = AsyncMock()
    monkeypatch.setattr(tool, '_get_sandbox_env', get_env)
    result = await tool.execute('print(1)')
    assert not result.success
    facts = json.loads(result.content)
    assert facts["status"] == status
    assert facts["components"][0]["missing"] == ["ipykernel"]
    assert facts["logTail"]
    assert "action='ensure'" in result.error
    assert "Do not use or install alternate runtimes" in result.error
    assert "separate existing environment" not in result.error
    requester.assert_awaited_once_with({"capability": "python", "action": "status"})
    get_env.assert_not_called()


def test_restored_python_invalidates_failed_environment_cache(tmp_path, monkeypatch):
    for key in ('BOX_AGENT_SANDBOX_PYTHON', 'BOX_AGENT_BUNDLED_PYTHON'):
        monkeypatch.delenv(key, raising=False)
    python = tmp_path / 'python.exe'
    tool = JupyterSandboxTool(runtime_env={'BOX_AGENT_SANDBOX_PYTHON': str(python)})
    monkeypatch.setattr(JupyterSandboxTool, '_sandbox_env', None)
    monkeypatch.setattr(JupyterSandboxTool, '_sandbox_env_key', None)
    before = tool._get_sandbox_env()
    assert not before._bundled_override
    python.write_bytes(b'fixture')
    tool.update_runtime_env({'BOX_AGENT_SANDBOX_PYTHON': str(python)})
    after = tool._get_sandbox_env()
    assert after is not before
    assert after.python_path == python
    assert after._bundled_override


def test_runtime_path_refresh_replaces_cached_python_environment(tmp_path, monkeypatch):
    python = tmp_path / "python"
    python.touch()
    monkeypatch.setattr(JupyterSandboxTool, "_sandbox_env", None)
    monkeypatch.setattr(JupyterSandboxTool, "_sandbox_env_key", None)
    tool = JupyterSandboxTool(runtime_env={
        "BOX_AGENT_SANDBOX_PYTHON": str(python), "PATH": "before-install",
    })
    before = tool._get_sandbox_env()
    tool.update_runtime_env({"PATH": "after-install", "BOX_AGENT_NODE": "/managed/node"})
    after = tool._get_sandbox_env()
    assert after is not before
    assert after.python_path == before.python_path
    assert after._subprocess_env()["PATH"] == "after-install"
    assert after._subprocess_env()["BOX_AGENT_NODE"] == "/managed/node"


@pytest.mark.asyncio
async def test_python_preparation_success_continues_same_call(monkeypatch, tmp_path):
    tool = JupyterSandboxTool()
    order = []
    python = tmp_path / 'python.exe'
    python.write_bytes(b'fixture')

    async def prepare():
        order.append('prepared')
        tool.update_runtime_env({'BOX_AGENT_SANDBOX_PYTHON': str(python)})

    async def ensure_ready():
        order.append('kernel-init')

    tool.runtime_preparer = prepare
    monkeypatch.setattr(JupyterSandboxTool, '_sessions', {})
    monkeypatch.setattr(JupyterSandboxTool, '_sandbox_env', None)
    monkeypatch.setattr(JupyterSandboxTool, '_sandbox_env_key', None)
    monkeypatch.setattr(SandboxEnvironment, 'ensure_ready', AsyncMock())
    session = SimpleNamespace(start=ensure_ready, workspace=tmp_path)
    monkeypatch.setattr(tool, '_get_workspace', lambda key: tmp_path)
    monkeypatch.setattr(tool, '_create_session', lambda key, workspace, env: session)
    monkeypatch.setattr(tool, '_execute_session_code', lambda session, code, timeout: ('1', [], None))
    result = await tool.execute('print(1)')
    assert order == ['prepared', 'kernel-init']
    assert result.success
    assert result.content == '1'


@pytest.mark.asyncio
async def test_chromium_waits_for_install_then_refreshes_paths_for_next_shell(tmp_path):
    from box_agent.tools.bash_tool import BashTool
    import shlex
    import sys

    started = asyncio.Event()
    installed = asyncio.Event()
    executable = str(tmp_path / "chromium")
    calls = []

    async def request(payload):
        calls.append(payload)
        assert payload["capability"] == "chromium"
        if payload["action"] == "status":
            return {"status": "missing"}
        started.set()
        await installed.wait()
        return {"status": "ready", "environments": [{"path": executable, "source": "host"}],
                "env": {"PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                        "BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable}}

    shell = BashTool(workspace_dir=str(tmp_path), runtime_env={
        "BOX_AGENT_PLAYWRIGHT_MODULE_PATH": "electron-adapter",
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": "",
    })
    environment = RuntimeEnvironmentTool(request)
    environment.apply_environment = shell.update_runtime_env
    pending = asyncio.create_task(environment.execute("chromium", "ensure"))
    await asyncio.wait_for(started.wait(), 2)
    assert not pending.done()
    installed.set()
    result = await pending
    assert result.success
    assert json.loads(result.content)["environments"] == [{"path": executable, "source": "host"}]
    script = "import os; print(os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH']); print(os.environ['BOX_AGENT_PLAYWRIGHT_MODULE_PATH'])"
    if shell.shell_name == "PowerShell":
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        command = f"& {quote(sys.executable)} -c {quote(script)}"
    else:
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    executed = await shell.execute(command)
    assert executed.success, executed.error
    assert executable in executed.content
    assert "electron-adapter" in executed.content
    assert [c["action"] for c in calls] == ["status", "ensure"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "timeout", "unavailable"])
async def test_chromium_failure_does_not_apply_environment_or_retry_in_a_loop(status):
    requester = AsyncMock(side_effect=[{"status": "missing"}, {"status": status}])
    tool = RuntimeEnvironmentTool(requester)
    applied = []
    tool.apply_environment = applied.append
    result = await tool.execute("chromium", "ensure")
    assert not result.success
    assert not applied
    assert requester.await_count == 2


@pytest.mark.asyncio
async def test_chromium_cancellation_propagates_without_continuing():
    requester = AsyncMock(side_effect=[{"status": "installing"}, asyncio.CancelledError()])
    tool = RuntimeEnvironmentTool(requester)
    applied = []
    tool.apply_environment = applied.append
    with pytest.raises(asyncio.CancelledError):
        await tool.execute("chromium", "ensure")
    assert not applied


def test_chromium_guidance_preserves_original_workflow_on_preparation_failure():
    description = RuntimeEnvironmentTool(AsyncMock()).description
    for rule in ("when supported", "retain the original rendering workflow", "use your judgment",
                 "Check installer status", "avoid competing", "respect user cancellation",
                 "actual rendering results", "existing acceptance gates"):
        assert rule in description


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unsupported", "failed", "timeout"])
async def test_chromium_preparation_failure_preserves_existing_shell_environment(tmp_path, failure):
    from box_agent.tools.bash_tool import BashTool

    if failure == "unsupported":
        requester = AsyncMock(side_effect=RuntimeError("Invalid runtime request"))
    else:
        terminal = asyncio.TimeoutError() if failure == "timeout" else {"status": "failed"}
        requester = AsyncMock(side_effect=[{"status": "missing"}, terminal])
    existing = {"PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": "/managed/existing-chromium",
                "BOX_AGENT_PLAYWRIGHT_MODULE_PATH": "/managed/electron-adapter"}
    shell = BashTool(workspace_dir=str(tmp_path), runtime_env=existing)
    tool = RuntimeEnvironmentTool(requester)
    tool.apply_environment = shell.update_runtime_env
    result = await tool.execute("chromium", "ensure")
    assert not result.success
    expected = "unavailable" if failure == "unsupported" else failure
    assert json.loads(result.content)["status"] == expected
    assert requester.await_count == (1 if failure == "unsupported" else 2)
    for key, value in existing.items():
        assert shell._subprocess_env[key] == value


def _model_history(result, capability="chromium"):
    from box_agent.tools.file_result_adapter import _tool_message_content_for_model
    return _tool_message_content_for_model(
        tool_name="local_environment", arguments={"capability": capability, "action": "ensure"},
        result=result, visible_content=result.content, visible_error=result.error,
    )


@pytest.mark.asyncio
async def test_chromium_failure_diagnostics_survive_model_history_with_bounded_redaction():
    response = {"status": "failed", "error": "Install failed", "installerRunning": False, "errorKind": [],
                "progress": [{"phase": "checking"}, {"phase": "downloading"}],
                "logTail": "x" * 20000 + "\ndownload verification failed\nAuthorization: Bearer fixture-secret\n"
                           "https://user:fixture-secret@example.invalid/file?signature=fixture-secret\n",
                "logPath": "/tmp/chromium-install.log",
                "env": {"PRIVATE_VALUE": "fixture-env-secret"},
                "unrelated": "fixture-unknown-secret"}
    requester = AsyncMock(side_effect=[{"status": "missing"}, response])
    result = await RuntimeEnvironmentTool(requester).execute("chromium", "ensure")
    history = _model_history(result)
    assert not result.success
    assert history.startswith("Error: Chromium runtime diagnostics: ")
    diagnostic = json.loads(history.split("diagnostics: ", 1)[1])
    assert diagnostic["category"] == "installation_failed"
    assert diagnostic["status"] == "failed"
    assert diagnostic["phases"] == ["checking", "downloading"]
    assert diagnostic["installerRunning"] is False
    assert diagnostic["logPath"] == "/tmp/chromium-install.log"
    assert "download verification failed" in diagnostic["logTail"]
    assert "[truncated]" in diagnostic["logTail"]
    assert "fixture-secret" not in history
    assert "fixture-env-secret" not in history
    assert "fixture-unknown-secret" not in history
    assert len(history) < 3000
    assert requester.await_count == 2


@pytest.mark.asyncio
async def test_chromium_rejection_and_connection_failure_are_distinct_in_model_history():
    from acp import RequestError
    histories = []
    for error in (RequestError(-32602, "fixture-secret", {"private": "fixture-secret"}),
                  ConnectionError("fixture-secret")):
        requester = AsyncMock(side_effect=error)
        result = await RuntimeEnvironmentTool(requester).execute("chromium", "ensure")
        history = _model_history(result)
        assert "fixture-secret" not in history
        histories.append(history)
        assert requester.await_count == 1
    assert histories[0] != histories[1]
    assert '"category": "request_rejected"' in histories[0]
    assert '"rpcCode": -32602' in histories[0]
    assert "unsupported" not in histories[0]  # Invalid params is not proof of an unsupported capability.
    assert '"category": "transport_error"' in histories[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,category", [
    (asyncio.TimeoutError(), "timeout"),
    ({"status": "failed", "error": "CHROMIUM_CANCELLED", "progress": [{"phase": "cancelled"}]}, "cancelled"),
    ([], "invalid_response"),
])
async def test_chromium_timeout_cancellation_and_invalid_reply_reach_model_history(failure, category):
    requester = AsyncMock(side_effect=[{"status": "missing"}, failure])
    tool = RuntimeEnvironmentTool(requester)
    applied = []
    tool.apply_environment = applied.append
    result = await tool.execute("chromium", "ensure")
    assert not result.success
    assert f'"category": "{category}"' in _model_history(result)
    if category == "timeout":
        assert "background installation may still be running" in _model_history(result)
    assert not applied
    assert requester.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["python", "node", "git", "browser"])
async def test_non_chromium_failure_history_keeps_existing_contract(capability):
    requester = AsyncMock(side_effect=[{"status": "missing"}, {
        "status": "failed", "error": "Install failed", "logTail": "details",
    }])
    result = await RuntimeEnvironmentTool(requester).execute(capability, "ensure")
    assert result.error == "Install failed"
    assert _model_history(result, capability) == "Error: Install failed"
    unavailable = await RuntimeEnvironmentTool(AsyncMock(side_effect=ConnectionError("private"))).execute(capability, "ensure")
    assert unavailable.error == "Host runtime service is unavailable."
