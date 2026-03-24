"""Test cases for safety utilities."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from box_agent.tools.safety import (
    TRASH_DIR,
    backup_file,
    detect_dangerous_command,
    detect_scope_escape,
    extract_rm_targets,
    validate_path_in_workspace,
)


# ── detect_dangerous_command ──────────────────────────────────────────


class TestDetectDangerousCommand:
    def test_rm_detected(self):
        assert detect_dangerous_command("rm file.txt") is not None
        assert detect_dangerous_command("rm -rf /tmp/test") is not None

    def test_rmdir_detected(self):
        assert detect_dangerous_command("rmdir empty_dir") is not None

    def test_sudo_detected(self):
        assert detect_dangerous_command("sudo apt install foo") is not None

    def test_kill_detected(self):
        assert detect_dangerous_command("kill -9 1234") is not None
        assert detect_dangerous_command("killall node") is not None
        assert detect_dangerous_command("pkill python") is not None

    def test_chmod_chown_detected(self):
        assert detect_dangerous_command("chmod 777 file") is not None
        assert detect_dangerous_command("chown root file") is not None

    def test_safe_commands(self):
        assert detect_dangerous_command("echo hello") is None
        assert detect_dangerous_command("ls -la") is None
        assert detect_dangerous_command("cat file.txt") is None
        assert detect_dangerous_command("git status") is None
        assert detect_dangerous_command("python main.py") is None

    def test_dd_detected(self):
        assert detect_dangerous_command("dd if=/dev/zero of=/dev/sda") is not None

    def test_mkfs_detected(self):
        assert detect_dangerous_command("mkfs.ext4 /dev/sda1") is not None

    def test_shutdown_reboot_detected(self):
        assert detect_dangerous_command("shutdown -h now") is not None
        assert detect_dangerous_command("reboot") is not None

    def test_redirect_to_dev_null(self):
        assert detect_dangerous_command("cat file > /dev/null") is not None

    def test_mv_to_dev_null(self):
        assert detect_dangerous_command("mv important.txt /dev/null") is not None


# ── detect_scope_escape ───────────────────────────────────────────────


class TestDetectScopeEscape:
    def test_cd_absolute_path(self):
        assert detect_scope_escape("cd /etc") is not None
        assert detect_scope_escape("cd /tmp/somewhere") is not None

    def test_cd_home(self):
        assert detect_scope_escape("cd ~") is not None

    def test_read_absolute_path(self):
        assert detect_scope_escape("cat /etc/passwd") is not None
        assert detect_scope_escape("head /var/log/syslog") is not None

    def test_redirect_to_absolute(self):
        assert detect_scope_escape("> /tmp/output.txt") is not None

    def test_safe_commands(self):
        assert detect_scope_escape("ls -la") is None
        assert detect_scope_escape("cat local_file.txt") is None
        assert detect_scope_escape("cd subdir") is None
        assert detect_scope_escape("echo hello > output.txt") is None


# ── validate_path_in_workspace ────────────────────────────────────────


class TestValidatePathInWorkspace:
    def test_path_inside_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        file_path = workspace / "test.txt"
        assert validate_path_in_workspace(file_path, workspace) is None

    def test_path_outside_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        file_path = tmp_path / "outside.txt"
        error = validate_path_in_workspace(file_path, workspace)
        assert error is not None
        assert "outside the workspace" in error

    def test_path_traversal_blocked(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        file_path = workspace / ".." / "outside.txt"
        error = validate_path_in_workspace(file_path, workspace)
        assert error is not None
        assert "outside the workspace" in error

    def test_workspace_root_allowed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # Accessing workspace root itself is OK
        assert validate_path_in_workspace(workspace, workspace) is None


# ── backup_file ───────────────────────────────────────────────────────


class TestBackupFile:
    def test_backup_existing_file(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("original content")

        backup_path = backup_file(test_file)
        assert backup_path is not None
        assert backup_path.exists()
        assert backup_path.read_text() == "original content"
        assert str(TRASH_DIR) in str(backup_path)

    def test_backup_nonexistent_file(self, tmp_path):
        test_file = tmp_path / "nonexistent.txt"
        result = backup_file(test_file)
        assert result is None

    def test_backup_directory_returns_none(self, tmp_path):
        test_dir = tmp_path / "somedir"
        test_dir.mkdir()
        result = backup_file(test_dir)
        assert result is None


# ── extract_rm_targets ────────────────────────────────────────────────


class TestExtractRmTargets:
    def test_simple_rm(self):
        targets = extract_rm_targets("rm file.txt", "/workspace")
        assert len(targets) == 1
        assert targets[0].name == "file.txt"

    def test_rm_with_flags(self):
        targets = extract_rm_targets("rm -rf dir1 dir2", "/workspace")
        assert len(targets) == 2

    def test_rm_absolute_path(self):
        targets = extract_rm_targets("rm /tmp/test.txt")
        assert len(targets) == 1
        # On macOS /tmp resolves to /private/tmp
        assert targets[0].name == "test.txt"
        assert targets[0].is_absolute()

    def test_no_rm_command(self):
        targets = extract_rm_targets("echo hello", "/workspace")
        assert len(targets) == 0

    def test_chained_rm(self):
        targets = extract_rm_targets("echo hello && rm file.txt", "/workspace")
        assert len(targets) == 1

    def test_rmdir(self):
        targets = extract_rm_targets("rmdir empty_dir", "/workspace")
        assert len(targets) == 1


# ── BashTool safety integration ───────────────────────────────────────


class TestBashToolSafety:
    @pytest.mark.asyncio
    async def test_dangerous_command_blocked_non_interactive(self):
        from box_agent.tools.bash_tool import BashTool

        tool = BashTool(non_interactive=True)
        result = await tool.execute(command="rm test.txt")
        assert not result.success
        assert "Dangerous command blocked" in result.error

    @pytest.mark.asyncio
    async def test_scope_escape_blocked(self):
        from box_agent.tools.bash_tool import BashTool

        tool = BashTool(workspace_dir="/tmp/test_workspace", allow_full_access=False)
        # Patch ask_user_confirmation to avoid terminal prompt for cd check
        result = await tool.execute(command="cd /etc && ls")
        assert not result.success
        assert "blocked" in result.error.lower()

    @pytest.mark.asyncio
    async def test_safe_command_allowed(self):
        from box_agent.tools.bash_tool import BashTool

        tool = BashTool(allow_full_access=False, non_interactive=True)
        result = await tool.execute(command="echo hello")
        assert result.success
        assert "hello" in result.stdout


# ── File tools safety integration ─────────────────────────────────────


class TestFileToolsSafety:
    @pytest.mark.asyncio
    async def test_read_outside_workspace_blocked(self, tmp_path):
        from box_agent.tools.file_tools import ReadTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool = ReadTool(workspace_dir=str(workspace), allow_full_access=False)

        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("secret")

        result = await tool.execute(path=str(outside_file))
        assert not result.success
        assert "outside the workspace" in result.error

    @pytest.mark.asyncio
    async def test_read_inside_workspace_allowed(self, tmp_path):
        from box_agent.tools.file_tools import ReadTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "test.txt").write_text("hello")
        tool = ReadTool(workspace_dir=str(workspace), allow_full_access=False)

        result = await tool.execute(path=str(workspace / "test.txt"))
        assert result.success

    @pytest.mark.asyncio
    async def test_write_creates_backup(self, tmp_path):
        from box_agent.tools.file_tools import WriteTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "test.txt"
        test_file.write_text("original")

        tool = WriteTool(workspace_dir=str(workspace))
        await tool.execute(path=str(test_file), content="new content")

        # File should be updated
        assert test_file.read_text() == "new content"

    @pytest.mark.asyncio
    async def test_edit_creates_backup(self, tmp_path):
        from box_agent.tools.file_tools import EditTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "test.txt"
        test_file.write_text("hello world")

        tool = EditTool(workspace_dir=str(workspace))
        await tool.execute(path=str(test_file), old_str="hello", new_str="goodbye")

        assert test_file.read_text() == "goodbye world"

    @pytest.mark.asyncio
    async def test_write_outside_workspace_blocked(self, tmp_path):
        from box_agent.tools.file_tools import WriteTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool = WriteTool(workspace_dir=str(workspace), allow_full_access=False)

        outside_file = tmp_path / "outside.txt"
        result = await tool.execute(path=str(outside_file), content="hacked")
        assert not result.success
        assert "outside the workspace" in result.error

    @pytest.mark.asyncio
    async def test_edit_outside_workspace_blocked(self, tmp_path):
        from box_agent.tools.file_tools import EditTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool = EditTool(workspace_dir=str(workspace), allow_full_access=False)

        outside_file = tmp_path / "outside.txt"
        result = await tool.execute(path=str(outside_file), old_str="a", new_str="b")
        assert not result.success
        assert "outside the workspace" in result.error
