"""Test cases for Bash Tool."""

import asyncio
import unittest.mock

import pytest

from box_agent.tools.bash_tool import BackgroundShellManager, BashKillTool, BashOutputTool, BashTool


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
    result = await bash_tool.execute(command="echo 'stdout message' && echo 'stderr message' >&2")

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


def test_no_sandbox_uses_login_shell():
    """Without sandbox_venv_path, login shell flag should be enabled."""
    import platform
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    tool = BashTool()
    assert tool._use_login_shell is True


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
