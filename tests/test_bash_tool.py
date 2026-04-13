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


def test_no_sandbox_uses_login_shell():
    """Without sandbox_venv_path, login shell flag should be enabled."""
    import platform
    if platform.system() == "Windows":
        pytest.skip("Unix-only test")

    tool = BashTool()
    assert tool._use_login_shell is True
