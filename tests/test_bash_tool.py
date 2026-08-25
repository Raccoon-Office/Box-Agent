"""Test cases for Bash Tool."""

import asyncio
import math
import os
import unittest.mock

import pytest

from box_agent.tools.bash_tool import (
    MAX_BASH_OUTPUT_CHARS,
    BackgroundShellManager,
    BashKillTool,
    BashOutputTool,
    BashTool,
    _truncate_bash_output,
    _truncate_bash_streams,
)
from box_agent.tools.argument_limits import MAX_BASH_COMMAND_CHARS


def test_bash_tools_opt_out_of_shared_result_compression():
    assert math.isinf(BashTool.max_result_size_chars)
    assert math.isinf(BashOutputTool.max_result_size_chars)


@pytest.mark.asyncio
async def test_rejects_oversized_command_before_execution():
    bash_tool = BashTool()

    result = await bash_tool.execute(command="x" * (MAX_BASH_COMMAND_CHARS + 1))

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("BASH_ARGUMENT_TOO_LARGE")
    assert bash_tool.parameters["properties"]["command"]["maxLength"] == MAX_BASH_COMMAND_CHARS
    assert bash_tool.parameters["additionalProperties"] is False


@pytest.mark.asyncio
async def test_foreground_command():
    """Test executing a simple foreground command."""
    print("\n=== Testing Foreground Command ===")

    bash_tool = BashTool()
    result = await bash_tool.execute(command="echo 'Hello from foreground'")

    assert result.success
    assert "Hello from foreground" in result.stdout
    assert result.exit_code == 0
    print(f"Output: {result.content}")


@pytest.mark.asyncio
async def test_foreground_command_with_stderr():
    """Test command that outputs to both stdout and stderr."""
    print("\n=== Testing Stdout/Stderr Separation ===")

    bash_tool = BashTool()
    import platform
    if platform.system() == "Windows":
        command = "Write-Output 'stdout message'; [Console]::Error.WriteLine('stderr message')"
    else:
        command = "echo 'stdout message' && echo 'stderr message' >&2"
    result = await bash_tool.execute(command=command)

    assert result.success
    assert "stdout message" in result.stdout
    assert "stderr message" in result.stderr
    print(f"Stdout: {result.stdout}")
    print(f"Stderr: {result.stderr}")


@pytest.mark.asyncio
async def test_command_failure():
    """Test command that fails with non-zero exit code."""
    print("\n=== Testing Command Failure ===")

    bash_tool = BashTool()
    result = await bash_tool.execute(command="ls /nonexistent_directory_12345")

    assert not result.success
    assert result.exit_code != 0
    assert result.error is not None
    print(f"Error: {result.error}")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="PowerShell has different pipeline semantics")
async def test_pipeline_propagates_upstream_failure():
    bash_tool = BashTool()

    result = await bash_tool.execute(command="false | tail -1")

    assert result.success is False
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_blocks_pptx_self_check_bypass_command():
    bash_tool = BashTool()
    command = (
        "node -e \"const fs=require('fs'); const src='html_to_editable_pptx.js'; "
        "fs.writeFileSync('export_skipcheck.js', fs.readFileSync(src,'utf8').replace('runSelfCheck(htmlPath, opts.width, opts.height, selfCheckReport);',''));\""
    )

    result = await bash_tool.execute(command=command)

    assert not result.success
    assert result.exit_code == 1
    assert "PPTX HTML self-check bypass blocked" in result.error


@pytest.mark.asyncio
async def test_command_timeout():
    """Test command timeout."""
    print("\n=== Testing Command Timeout ===")

    bash_tool = BashTool()
    result = await bash_tool.execute(command="sleep 10", timeout=1)

    assert not result.success
    assert "timed out" in result.error.lower()
    assert result.exit_code == -1
    print(f"Timeout error: {result.error}")


@pytest.mark.asyncio
async def test_background_command():
    """Test running a command in the background."""
    print("\n=== Testing Background Command ===")

    bash_tool = BashTool()
    result = await bash_tool.execute(
        command="for i in 1 2 3; do echo 'Line '$i; sleep 0.5; done", run_in_background=True
    )

    assert result.success
    assert result.bash_id is not None
    assert "Background command started" in result.stdout

    bash_id = result.bash_id
    print(f"Background command started with ID: {bash_id}")

    # Wait a bit for output
    await asyncio.sleep(1)

    # Check output
    bash_output_tool = BashOutputTool()
    output_result = await bash_output_tool.execute(bash_id=bash_id)

    assert output_result.success
    print(f"Output:\n{output_result.content}")

    # Clean up - terminate the background process
    bash_kill_tool = BashKillTool()
    kill_result = await bash_kill_tool.execute(bash_id=bash_id)
    assert kill_result.success
    print("Background process terminated")


@pytest.mark.asyncio
async def test_background_processes_are_scoped_and_cleaned_by_owner():
    owner_a = BashTool(process_owner_id="session-a")
    owner_b = BashTool(process_owner_id="session-b")
    started = await owner_a.execute(command="sleep 100", run_in_background=True)
    assert started.success
    assert started.bash_id is not None

    try:
        hidden = await BashOutputTool(process_owner_id="session-b").execute(
            bash_id=started.bash_id
        )
        assert hidden.success is False
        assert "Available: none" in hidden.error

        cleaned = await owner_a.cleanup_background_processes()
        assert cleaned == [started.bash_id]
        assert BackgroundShellManager.get(started.bash_id) is None
        assert await owner_a.cleanup_background_processes() == []
    finally:
        await owner_a.cleanup_background_processes()
        await owner_b.cleanup_background_processes()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="process-group assertion is POSIX-specific")
async def test_owner_cleanup_kills_grandchild_after_shell_wrapper_exits():
    tool = BashTool(process_owner_id="session-grandchild")
    started = await tool.execute(command="sleep 100 &", run_in_background=True)
    assert started.success
    shell = BackgroundShellManager.get(started.bash_id)
    assert shell is not None
    process_group_id = shell.process.pid

    try:
        await asyncio.sleep(0.1)
        assert await tool.cleanup_background_processes() == [started.bash_id]
        with pytest.raises(ProcessLookupError):
            os.killpg(process_group_id, 0)
    finally:
        await tool.cleanup_background_processes()


@pytest.mark.asyncio
async def test_bash_output_monitoring():
    """Test monitoring background command output."""
    print("\n=== Testing Output Monitoring ===")

    bash_tool = BashTool()

    # Start background command
    result = await bash_tool.execute(
        command="for i in 1 2 3 4 5; do echo 'Line '$i; sleep 0.5; done", run_in_background=True
    )

    assert result.success
    bash_id = result.bash_id
    print(f"Started background command: {bash_id}")

    bash_output_tool = BashOutputTool()

    # Check output multiple times (incremental output)
    for i in range(3):
        await asyncio.sleep(1)
        output_result = await bash_output_tool.execute(bash_id=bash_id)
        assert output_result.success
        print(f"\n--- Check #{i + 1} ---")
        print(f"Output:\n{output_result.content}")

    # Clean up
    bash_kill_tool = BashKillTool()
    await bash_kill_tool.execute(bash_id=bash_id)


@pytest.mark.asyncio
async def test_bash_output_with_filter():
    """Test bash_output with regex filter."""
    print("\n=== Testing Output Filter ===")

    bash_tool = BashTool()

    # Start background command
    result = await bash_tool.execute(
        command="for i in 1 2 3 4 5; do echo 'Line '$i; sleep 0.3; done", run_in_background=True
    )

    assert result.success
    bash_id = result.bash_id

    # Wait for some output
    await asyncio.sleep(2)

    # Get filtered output (only lines with "Line 2" or "Line 4")
    bash_output_tool = BashOutputTool()
    output_result = await bash_output_tool.execute(bash_id=bash_id, filter_str="Line [24]")

    assert output_result.success
    lines = output_result.content
    print(f"Filtered output:\n{output_result.content}")

    # Clean up
    bash_kill_tool = BashKillTool()
    await bash_kill_tool.execute(bash_id=bash_id)


@pytest.mark.asyncio
async def test_bash_kill():
    """Test terminating a background command."""
    print("\n=== Testing Bash Kill ===")

    bash_tool = BashTool()

    # Start a long-running background command
    result = await bash_tool.execute(command="sleep 100", run_in_background=True)

    assert result.success
    bash_id = result.bash_id
    print(f"Started long-running command: {bash_id}")

    # Verify it's running
    await asyncio.sleep(0.5)
    bg_shell = BackgroundShellManager.get(bash_id)
    assert bg_shell is not None
    assert bg_shell.status == "running"

    # Kill it
    bash_kill_tool = BashKillTool()
    kill_result = await bash_kill_tool.execute(bash_id=bash_id)

    assert kill_result.success
    # exit_code -15 means terminated by SIGTERM
    assert kill_result.exit_code == -15 or kill_result.bash_id == bash_id
    print(f"Kill result:\n{kill_result.content}")

    # Verify it's removed from manager
    bg_shell = BackgroundShellManager.get(bash_id)
    assert bg_shell is None


@pytest.mark.asyncio
async def test_bash_kill_nonexistent():
    """Test killing a non-existent bash process."""
    print("\n=== Testing Kill Non-existent Process ===")

    bash_kill_tool = BashKillTool()
    result = await bash_kill_tool.execute(bash_id="nonexistent123")

    assert not result.success
    assert "not found" in result.error.lower()
    print(f"Expected error: {result.error}")


@pytest.mark.asyncio
async def test_bash_output_nonexistent():
    """Test getting output from non-existent bash process."""
    print("\n=== Testing Output From Non-existent Process ===")

    bash_output_tool = BashOutputTool()
    result = await bash_output_tool.execute(bash_id="nonexistent123")

    assert not result.success
    assert "not found" in result.error.lower()
    print(f"Expected error: {result.error}")


@pytest.mark.asyncio
async def test_multiple_background_commands():
    """Test running multiple background commands simultaneously."""
    print("\n=== Testing Multiple Background Commands ===")

    bash_tool = BashTool()

    # Start multiple background commands
    bash_ids = []
    for i in range(3):
        result = await bash_tool.execute(
            command=f"for j in 1 2 3; do echo 'Command {i + 1} Line '$j; sleep 0.5; done", run_in_background=True
        )
        assert result.success
        bash_ids.append(result.bash_id)
        print(f"Started command {i + 1}: {result.bash_id}")

    # Wait and check all commands
    await asyncio.sleep(1)

    bash_output_tool = BashOutputTool()
    for bash_id in bash_ids:
        output_result = await bash_output_tool.execute(bash_id=bash_id)
        assert output_result.success
        print(f"\nOutput for {bash_id}:\n{output_result.content[:100]}...")

    # Clean up all
    bash_kill_tool = BashKillTool()
    for bash_id in bash_ids:
        await bash_kill_tool.execute(bash_id=bash_id)

    print("All background processes cleaned up")


@pytest.mark.asyncio
async def test_timeout_validation():
    """Test timeout parameter validation."""
    print("\n=== Testing Timeout Validation ===")

    bash_tool = BashTool()

    # Test with timeout > 600 (should be capped to 600)
    result = await bash_tool.execute(command="echo 'test'", timeout=1000)
    assert result.success
    print("Timeout > 600 handled correctly")

    # Test with timeout < 1 (should be set to 120)
    result = await bash_tool.execute(command="echo 'test'", timeout=0)
    assert result.success
    print("Timeout < 1 handled correctly")


@pytest.mark.asyncio
async def test_unix_login_shell_attribute():
    """On Unix, BashTool should have _login_shell from $SHELL."""
    import platform
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    bash_tool = BashTool()
    assert hasattr(bash_tool, "_login_shell")
    assert bash_tool._login_shell  # non-empty


@pytest.mark.asyncio
async def test_unix_login_shell_execution():
    """On Unix, commands should run through the login shell."""
    import os
    import platform
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    bash_tool = BashTool()
    # The login shell should provide a functional environment
    result = await bash_tool.execute(command="echo ok")
    assert result.success
    assert "ok" in result.stdout


def test_resolve_login_shell_posix():
    """Known POSIX shells that exist should be used directly."""
    import os
    import platform
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    from box_agent.tools.bash_tool import _resolve_login_shell

    # Only test shells that actually exist on this system
    for shell in ("/bin/bash", "/bin/zsh", "/usr/bin/zsh", "/bin/sh", "/bin/dash"):
        if os.access(shell, os.X_OK):
            with unittest.mock.patch.dict(os.environ, {"SHELL": shell}):
                assert _resolve_login_shell() == shell


def test_resolve_login_shell_non_posix_falls_back():
    """Non-POSIX shells (fish, csh, etc.) should fall back."""
    import os
    import platform
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    from box_agent.tools.bash_tool import _resolve_login_shell

    for shell in ("/usr/bin/fish", "/bin/csh", "/bin/tcsh"):
        with unittest.mock.patch.dict(os.environ, {"SHELL": shell}):
            result = _resolve_login_shell()
            assert result in ("/bin/bash", "/bin/sh")


def test_resolve_login_shell_stale_path_falls_back():
    """Stale/nonexistent $SHELL path should fall back to /bin/bash or /bin/sh."""
    import os
    import platform
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    from box_agent.tools.bash_tool import _resolve_login_shell

    with unittest.mock.patch.dict(os.environ, {"SHELL": "/nix/store/xxx-bash-5.2/bin/bash"}):
        result = _resolve_login_shell()
        assert result in ("/bin/bash", "/bin/sh")


def test_sandbox_venv_skips_login_shell():
    """When sandbox_venv_path is set, login shell flag must be disabled."""
    import platform
    import tempfile
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    with tempfile.TemporaryDirectory() as venv_dir:
        # Create a fake bin dir so the path looks real
        import os
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)

        tool = BashTool(sandbox_venv_path=venv_dir)
        assert tool._use_login_shell is False
        assert tool._subprocess_env is not None
        assert tool._subprocess_env["VIRTUAL_ENV"] == venv_dir
        assert tool._subprocess_env["PATH"].split(os.pathsep)[0] == os.path.join(venv_dir, "bin")


def test_sandbox_runtime_env_injected_when_python_exists():
    """Runtime env exposes the sandbox Python vars without changing venv behavior."""
    import os
    import platform
    import tempfile
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    with tempfile.TemporaryDirectory() as venv_dir:
        bin_dir = os.path.join(venv_dir, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        python_path = os.path.join(bin_dir, "python")
        with open(python_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(python_path, 0o755)

        tool = BashTool(
            sandbox_venv_path=venv_dir,
            runtime_env={
                "BOX_AGENT_PYTHON": python_path,
                "BOX_AGENT_PYTHON3": python_path,
            },
        )

        assert tool._subprocess_env is not None
        assert tool._subprocess_env["BOX_AGENT_PYTHON"] == python_path
        assert tool._subprocess_env["BOX_AGENT_PYTHON3"] == python_path
        assert tool._subprocess_env["PATH"].split(os.pathsep)[0] == bin_dir


def test_empty_runtime_env_does_not_inject_python_vars():
    tool = BashTool(runtime_env={})
    if tool._subprocess_env is not None:
        assert "BOX_AGENT_PYTHON" not in tool._subprocess_env
        assert "BOX_AGENT_PYTHON3" not in tool._subprocess_env


@pytest.mark.asyncio
async def test_verified_runtime_node_reference_runs_without_approval(tmp_path):
    node_path = tmp_path / "node"
    node_path.write_text("#!/bin/sh\nprintf 'runtime-node-ok\\n'\n", encoding="utf-8")
    node_path.chmod(0o755)
    tool = BashTool(runtime_env={"BOX_AGENT_NODE": str(node_path)})

    result = await tool.execute(command="${BOX_AGENT_NODE:-node}")

    assert result.success, result.error
    assert result.stdout.strip() == "runtime-node-ok"
    assert result.permission_request is None


@pytest.mark.asyncio
async def test_non_executable_runtime_node_reference_still_requires_approval(tmp_path):
    node_path = tmp_path / "node"
    node_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node_path.chmod(0o644)
    tool = BashTool(runtime_env={"BOX_AGENT_NODE": str(node_path)})

    result = await tool.execute(command="${BOX_AGENT_NODE:-node}")

    assert not result.success
    assert "Dynamically constructed shell executable" in result.error
    assert result.permission_request is not None


@pytest.mark.asyncio
async def test_runtime_owned_action_approves_only_its_exact_dynamic_command():
    tool = BashTool()
    command = "${BOX_AGENT_NODE:-node} -e \"process.stdout.write('runtime-ok')\""

    tool.approve_runtime_workflow_action(
        "controlled_presentation.scaffold",
        {"command": command},
    )
    result = await tool.execute(command=command)
    changed = await tool.execute(command=f"{command} ")

    assert result.success, result.error
    assert result.stdout == "runtime-ok"
    assert changed.success is False
    assert changed.permission_request is not None


def test_runtime_env_can_be_updated_for_future_subprocesses():
    tool = BashTool()

    tool.update_runtime_env({"BOX_AGENT_SOURCE_TEXT_B64": "c291cmNl"})

    assert tool._subprocess_env is not None
    assert tool._subprocess_env["BOX_AGENT_SOURCE_TEXT_B64"] == "c291cmNl"
    tool.update_runtime_env({"BOX_AGENT_SOURCE_TEXT_B64": None})
    assert "BOX_AGENT_SOURCE_TEXT_B64" not in tool._subprocess_env


def test_no_sandbox_uses_login_shell():
    """Without sandbox_venv_path, login shell flag should be enabled."""
    import platform
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    tool = BashTool()
    assert tool._use_login_shell is True


def test_login_shell_reapplies_box_agent_skill_path_prefix():
    with unittest.mock.patch.dict(
        os.environ,
        {"npm_config_prefix": "/user/npm", "npm_config_cache": "/user/cache"},
    ):
        tool = BashTool(
            runtime_env={
                "BOX_AGENT_SKILL_PATH_PREFIX": "/managed/skill/bin:/managed/node/bin",
                "PATH": "/managed/skill/bin:/managed/node/bin:/user/bin",
                "NPM_CONFIG_PREFIX": "/managed/skill",
                "NPM_CONFIG_CACHE": "/managed/skill/npm-cache",
            }
        )

    wrapped = tool._restore_skill_path_after_login("command -v npm")

    assert wrapped.startswith(
        'PATH="${BOX_AGENT_SKILL_PATH_PREFIX}${PATH:+:${PATH}}"; export PATH; '
    )
    assert wrapped.endswith("command -v npm")
    assert "npm_config_prefix" not in tool._subprocess_env
    assert "npm_config_cache" not in tool._subprocess_env


@pytest.mark.asyncio
async def test_foreground_timeout_kills_grandchild_process(tmp_path):
    """B4 regression: a foreground timeout must tear down the whole process
    tree (shell + grandchildren), not just the shell.

    We spawn a shell that backgrounds a grandchild which appends to a sentinel
    file every 0.2s. Before the fix, the timeout killed only the shell, leaving
    the grandchild orphaned and still writing. After the fix the child is
    spawned in its own session and killpg reaps the whole group, so the file
    stops growing once the command times out.
    """
    import platform

    if platform.system() == "Windows":
        pytest.skip("Unix process-group semantics; Unix-only test")

    sentinel = tmp_path / "ticks.txt"
    # Grandchild loops independently of the parent shell.
    command = (
        f"( while true; do echo tick >> {sentinel}; sleep 0.2; done ) & "
        "wait"
    )

    bash_tool = BashTool()
    result = await bash_tool.execute(command=command, timeout=1)

    assert not result.success
    assert "timed out" in result.error.lower()

    # Give any orphan a chance to keep writing, then confirm it stopped.
    await asyncio.sleep(1.0)
    count_after_kill = len(sentinel.read_text().splitlines()) if sentinel.exists() else 0
    await asyncio.sleep(1.0)
    count_later = len(sentinel.read_text().splitlines()) if sentinel.exists() else 0

    # If the grandchild were orphaned it would have written ~5 more lines.
    assert count_later == count_after_kill, (
        f"grandchild kept writing after timeout kill: {count_after_kill} -> {count_later}"
    )


@pytest.mark.asyncio
async def test_foreground_timeout_kills_grandchild_when_shell_already_exited(tmp_path):
    """B4 regression (shell-exited form): a foreground command backgrounds a
    grandchild and the shell itself returns immediately, but ``communicate()``
    still blocks because the grandchild inherits the stdout pipe. At timeout the
    shell's returncode is already set; the cleanup must STILL kill the process
    group (not bail out early), or the grandchild keeps running.
    """
    import platform

    if platform.system() == "Windows":
        pytest.skip("Unix process-group semantics; Unix-only test")

    sentinel = tmp_path / "ticks.txt"
    # NOTE: no `wait` — the shell exits right after backgrounding the loop.
    command = (
        f"( for i in $(seq 1 100); do echo tick >> {sentinel}; sleep 0.2; done ) &"
    )

    bash_tool = BashTool()
    result = await bash_tool.execute(command=command, timeout=1)

    assert not result.success
    assert "timed out" in result.error.lower()

    await asyncio.sleep(1.0)
    count_after_kill = len(sentinel.read_text().splitlines()) if sentinel.exists() else 0
    await asyncio.sleep(1.0)
    count_later = len(sentinel.read_text().splitlines()) if sentinel.exists() else 0

    assert count_later == count_after_kill, (
        f"grandchild kept writing after shell-exited timeout: "
        f"{count_after_kill} -> {count_later}"
    )


@pytest.mark.asyncio
async def test_foreground_timeout_reaps_process_no_zombie():
    """B4 regression: after a timeout the process must be reaped (returncode set),
    not left as a zombie."""
    bash_tool = BashTool()
    # Wrap _create_subprocess to capture the process object.
    captured = {}
    orig = bash_tool._create_subprocess

    async def _capture(command, *, merge_stderr=False):
        proc = await orig(command, merge_stderr=merge_stderr)
        captured["proc"] = proc
        return proc

    bash_tool._create_subprocess = _capture
    result = await bash_tool.execute(command="sleep 10", timeout=1)

    assert not result.success
    proc = captured.get("proc")
    assert proc is not None
    # Reaped: returncode is set (not None) after _kill_process_tree awaited wait().
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_foreground_timeout_kills_grandchild_windows(tmp_path):
    """Windows regression: a foreground timeout must recurse the subtree.

    Mirrors the Unix grandchild tests but for the ``taskkill /T /F`` path.
    Reproduces the ``find | xargs grep``-style hang: bash or PowerShell
    spawns a child that appends to a sentinel every 200 ms, and we assert
    the sentinel stops growing after the timeout. Before the fix,
    ``process.kill()`` on Windows only killed the wrapper — the grandchild
    kept writing and ``communicate()`` never returned because it held the
    stdout pipe open.
    """
    import platform

    if platform.system() != "Windows":
        pytest.skip("Windows subtree-kill semantics; Windows-only test")

    import time as _time

    sentinel = tmp_path / "ticks.txt"
    sentinel_str = str(sentinel).replace("\\", "/")

    # Use PowerShell (default on Windows without bundled Git-bash). The child
    # process appends a line every 200 ms; the outer command waits on it, so
    # the wrapper is alive when the timeout fires. If subtree kill fails, the
    # background job would keep writing after we kill the wrapper.
    command = (
        f"$job = Start-Job {{ while ($true) {{ "
        f"Add-Content -Path '{sentinel_str}' -Value 'tick'; "
        f"Start-Sleep -Milliseconds 200 }} }}; "
        f"Wait-Job $job"
    )

    bash_tool = BashTool()
    start = _time.monotonic()
    result = await bash_tool.execute(command=command, timeout=2)
    elapsed = _time.monotonic() - start

    assert not result.success
    assert "timed out" in (result.error or "").lower()
    # taskkill /T /F must return in ~10s or less; the whole call must not
    # sit at communicate() forever. Give generous slack for Start-Job spinup.
    assert elapsed < 25.0, f"timeout path took {elapsed:.1f}s (expected < 25s)"

    # Give any orphan a chance to keep writing, then confirm it stopped.
    await asyncio.sleep(1.5)
    count_after_kill = (
        len(sentinel.read_text(encoding="utf-8").splitlines())
        if sentinel.exists() else 0
    )
    await asyncio.sleep(1.5)
    count_later = (
        len(sentinel.read_text(encoding="utf-8").splitlines())
        if sentinel.exists() else 0
    )

    # If the Start-Job worker were orphaned it would have written ~7 more lines.
    assert count_later == count_after_kill, (
        f"Windows grandchild kept writing after timeout kill: "
        f"{count_after_kill} -> {count_later}"
    )


# ── output truncation (P2b) ──────────────────────────────────────────


def test_truncate_bash_output_small_passthrough():
    """Under-limit text is returned unchanged with dropped_chars=0."""
    text = "small output"
    out, dropped = _truncate_bash_output(text, "stdout")
    assert out == text
    assert dropped == 0


def test_truncate_bash_output_empty_passthrough():
    """Empty input short-circuits, no marker."""
    out, dropped = _truncate_bash_output("", "stdout")
    assert out == ""
    assert dropped == 0


def test_truncate_bash_output_at_boundary():
    """Exactly-at-limit text is not truncated (>, not >=)."""
    text = "a" * MAX_BASH_OUTPUT_CHARS
    out, dropped = _truncate_bash_output(text, "stdout")
    assert out == text
    assert dropped == 0


def test_truncate_bash_output_head_tail_shape():
    """Over-limit text: Hermes-style head 40% + tail 60%."""
    limit = 1000  # small custom limit for a readable test
    head_n = limit * 2 // 5  # 400
    tail_n = limit - head_n  # 600

    # Distinct head/tail sentinels so we can verify which slice was kept.
    text = "H" * 2000 + "T" * 2000
    out, dropped = _truncate_bash_output(text, "stdout", limit=limit)

    assert dropped == len(text) - limit == 3000
    assert out.startswith("H" * head_n)
    assert out.endswith("T" * tail_n)
    assert "truncated" in out
    assert "Tip:" in out
    # No leakage of head/tail sentinels into the marker. We can't just check
    # "H not in marker" because the marker text itself contains letters
    # (e.g. "Tip:"); check that no long run of H or T sentinels bled through.
    marker_start = head_n
    marker_end = len(out) - tail_n
    marker = out[marker_start:marker_end]
    assert "HH" not in marker  # any two consecutive H would be a sentinel leak
    assert "TT" not in marker


def test_truncate_bash_output_marker_includes_actionable_hint():
    """The middle marker must carry the guidance, not just a passive notice."""
    text = "x" * (MAX_BASH_OUTPUT_CHARS + 10_000)
    out, dropped = _truncate_bash_output(text, "stdout")
    assert dropped == 10_000
    # Regression: models used to read to the marker and conclude "no matches".
    # The tip inside the marker is what breaks that pattern.
    assert "narrower search patterns" in out or "narrower" in out
    assert "head -N" in out or "rg -m N" in out


def test_truncate_bash_streams_uses_one_shared_budget():
    """stdout and stderr must not receive independent 50K allowances."""
    stdout = "O" * 40_000
    stderr = "E" * 40_000

    bounded_stdout, bounded_stderr, dropped = _truncate_bash_streams(stdout, stderr)

    assert bounded_stderr == ""
    assert dropped > 0
    assert len(bounded_stdout) <= MAX_BASH_OUTPUT_CHARS + 500
    assert bounded_stdout.startswith("O" * 100)
    assert bounded_stdout.endswith("E" * 100)
    assert "combined stdout/stderr truncated" in bounded_stdout


def test_truncate_bash_streams_preserves_small_streams():
    stdout, stderr, dropped = _truncate_bash_streams("out", "err")
    assert (stdout, stderr, dropped) == ("out", "err", 0)


@pytest.mark.asyncio
async def test_foreground_output_truncated_when_oversize():
    """Foreground `rg`-style large output must be truncated at the tool
    boundary — this is the direct regression guard for the 3.7M-char
    context-overflow incident."""
    # Portable way to produce > MAX_BASH_OUTPUT_CHARS of output on both
    # POSIX shells and PowerShell without depending on `rg` / `yes`.
    import platform
    if platform.system() == "Windows":
        # PowerShell: emit a 60000-char string
        command = "$s = 'x' * 60000; Write-Output $s"
    else:
        command = "python3 -c \"print('x' * 60000)\""

    bash_tool = BashTool()
    result = await bash_tool.execute(command=command)

    assert result.success, f"command failed: {result.error}"
    assert len(result.stdout) <= MAX_BASH_OUTPUT_CHARS + 500, (
        f"expected truncated output, got {len(result.stdout)} chars"
    )
    assert "truncated" in result.stdout
    assert result.raw_output is not None
    assert result.raw_output["dropped_chars"] > 0
    assert result.raw_output["original_stdout_chars"] > MAX_BASH_OUTPUT_CHARS
    assert result.raw_output["streams_combined"] is True
    assert result.raw_output["max_output_chars"] == MAX_BASH_OUTPUT_CHARS
    assert result.persistence_content is not None
    assert len(result.persistence_content) > MAX_BASH_OUTPUT_CHARS
    assert "persistence_content" not in result.model_dump()


@pytest.mark.asyncio
async def test_foreground_output_normal_size_no_raw_output():
    """Normal-sized output must not attach the raw_output truncation payload
    (host UIs would otherwise show a "truncated" badge on every command)."""
    bash_tool = BashTool()
    result = await bash_tool.execute(command="echo hello")
    assert result.success
    assert "hello" in result.stdout
    assert result.raw_output is None
    assert result.persistence_content is None


@pytest.mark.asyncio
async def test_foreground_large_stderr_failure_is_bounded_without_error_duplication():
    """A failing command with huge stderr keeps one shared bounded payload."""
    import platform
    if platform.system() == "Windows":
        command = "[Console]::Error.Write('e' * 60000); exit 7"
    else:
        command = "python3 -c \"import sys; sys.stderr.write('e' * 60000); raise SystemExit(7)\""

    result = await BashTool().execute(command=command)

    assert not result.success
    assert result.exit_code == 7
    assert len(result.content) <= MAX_BASH_OUTPUT_CHARS + 600
    assert result.error is not None
    assert result.error.startswith("Command failed with exit code 7")
    assert len(result.error) <= 8_500
    assert "error context truncated" in result.error
    assert result.raw_output is not None
    assert result.raw_output["dropped_chars"] > 0
    assert result.raw_output["original_stderr_chars"] >= 60_000
    assert result.persistence_content is not None
    assert len(result.persistence_content) > MAX_BASH_OUTPUT_CHARS


@pytest.mark.asyncio
async def test_background_output_truncated_when_oversize():
    """`bash_output` reads on a background shell that accumulated a large
    stream must also truncate — otherwise the incident just moves to the
    background path."""
    bg_shell_cls = BackgroundShellManager  # for cleanup access
    bash_tool = BashTool()

    # A quick background command that emits a big single line then exits.
    import platform
    if platform.system() == "Windows":
        command = "$s = 'x' * 60000; Write-Output $s"
    else:
        command = "python3 -c \"print('x' * 60000)\""

    start_result = await bash_tool.execute(command=command, run_in_background=True)
    assert start_result.success
    bash_id = start_result.bash_id
    assert bash_id is not None

    # Wait for the monitor to observe exit and drain the pipe. A bounded poll
    # avoids the fixed-sleep race that originally exposed the missing drain.
    for _ in range(100):
        shell = BackgroundShellManager.get(bash_id)
        if shell is not None and shell.status != "running":
            break
        await asyncio.sleep(0.05)

    bash_output_tool = BashOutputTool()
    output_result = await bash_output_tool.execute(bash_id=bash_id)
    try:
        assert output_result.success
        assert len(output_result.stdout) <= MAX_BASH_OUTPUT_CHARS + 500
        assert "truncated" in output_result.stdout
        assert output_result.raw_output is not None
        assert output_result.raw_output["dropped_chars"] > 0
        assert output_result.persistence_content is not None
        assert len(output_result.persistence_content) > MAX_BASH_OUTPUT_CHARS
    finally:
        # Best-effort cleanup so the test doesn't leak a monitor task.
        try:
            await BashKillTool().execute(bash_id=bash_id)
        except Exception:
            pass
        _ = bg_shell_cls  # silence unused
