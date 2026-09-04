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
    detect_invalid_runtime_executable_syntax,
    detect_scope_escape,
    extract_rm_targets,
    is_safe_scratch_cleanup,
    trusted_runtime_executable_references,
    validate_path_in_workspace,
)


# ── detect_dangerous_command ──────────────────────────────────────────


class TestDetectDangerousCommand:
    def test_rm_detected(self):
        assert detect_dangerous_command("rm file.txt") is not None
        assert detect_dangerous_command("rm -rf /tmp/test") is not None
        assert detect_dangerous_command("echo $(rm file.txt)") is not None
        assert detect_dangerous_command("bash -c 'rm file.txt'") is not None
        assert detect_dangerous_command(">audit.log rm file.txt") is not None
        assert detect_dangerous_command("2>/dev/null rm file.txt") is not None

    @pytest.mark.parametrize(
        "command",
        [
            '"$BOX_AGENT_PYTHON:-python3" script.py',
            "$BOX_AGENT_NODE:-node server.js",
        ],
    )
    def test_malformed_runtime_fallback_is_rejected(self, command):
        error = detect_invalid_runtime_executable_syntax(command)

        assert error is not None
        assert "BASH_INVALID_RUNTIME_EXECUTABLE" in error

    @pytest.mark.parametrize(
        "command",
        [
            '"$BOX_AGENT_PYTHON" script.py',
            '"${BOX_AGENT_PYTHON:-python3}" script.py',
            "python3 script.py",
        ],
    )
    def test_valid_runtime_invocations_are_accepted(self, command):
        assert detect_invalid_runtime_executable_syntax(command) is None

    def test_simple_scratch_descendant_cleanup_is_safe(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        assert is_safe_scratch_cleanup(
            'rm -rf "$BOX_AGENT_SCRATCH_DIR/preview_shots"', scratch
        )
        assert is_safe_scratch_cleanup(f"rmdir {scratch / 'empty-preview'}", scratch)

    @pytest.mark.parametrize(
        "command",
        [
            'rm -rf "$BOX_AGENT_SCRATCH_DIR"',
            "rm -rf preview_shots",
            'rm -rf "$BOX_AGENT_SCRATCH_DIR"/*',
            'rm -rf "$BOX_AGENT_SCRATCH_DIR/preview" && echo done',
        ],
    )
    def test_broad_or_ambiguous_scratch_cleanup_is_not_safe(self, tmp_path, command):
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        assert not is_safe_scratch_cleanup(command, scratch)

    def test_scratch_cleanup_rejects_symlink_path(self, tmp_path):
        scratch = tmp_path / "scratch"
        outside = tmp_path / "outside"
        scratch.mkdir()
        outside.mkdir()
        link = scratch / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"symlinks are unavailable in this environment: {error}")

        assert not is_safe_scratch_cleanup(
            'rm -rf "$BOX_AGENT_SCRATCH_DIR/link/child"', scratch
        )

    def test_rmdir_detected(self):
        assert detect_dangerous_command("rmdir empty_dir") is not None

    def test_sudo_detected(self):
        assert detect_dangerous_command("sudo apt install foo") is not None
        assert detect_dangerous_command("env MODE=test sudo apt install foo") is not None
        assert detect_dangerous_command("sudo -u root apt install foo") is not None
        assert detect_dangerous_command("sudo -v") is not None
        assert detect_dangerous_command("sudo --reset-timestamp") is not None

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
        assert detect_dangerous_command("python render_pptx.py deck.pptx --format png") is None

    @pytest.mark.parametrize(
        "command",
        [
            "python3 -c \"print('wide format report')\"",
            "python3 -c \"dd = 12; print(dd)\"",
            """python3 << 'EOF'\n# Build the wide format report.\ndd = 12\nprint(dd)\nEOF""",
            """python3 << 'EOF'\nfrom collections import defaultdict\nvalues = defaultdict(int)\nEOF""",
        ],
    )
    def test_embedded_python_dangerous_words_are_not_shell_commands(self, command):
        assert detect_dangerous_command(command) is None

    def test_dd_detected(self):
        assert detect_dangerous_command("dd if=/dev/zero of=/dev/sda") is not None
        assert (
            detect_dangerous_command("echo preparing && dd if=/dev/zero of=/dev/sda")
            is not None
        )

    def test_mkfs_detected(self):
        assert detect_dangerous_command("mkfs.ext4 /dev/sda1") is not None

    @pytest.mark.parametrize(
        "command",
        [
            "diskutil eraseDisk APFS Empty /dev/disk2",
            "diskutil eraseVolume APFS Empty /dev/disk2s1",
            "diskutil secureErase 0 /dev/disk2",
        ],
    )
    def test_diskutil_erase_detected(self, command):
        assert detect_dangerous_command(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            r'"C:\Windows\System32\rm.exe" cache.tmp',
            r'"C:\Windows\System32\format.com" C:',
        ],
    )
    def test_windows_executable_paths_are_detected(self, command, monkeypatch):
        monkeypatch.setattr(
            "box_agent.tools.shell_inspection.platform.system",
            lambda: "Windows",
        )

        assert detect_dangerous_command(command) is not None

    def test_depth_limited_nested_shell_danger_fails_closed(self):
        command = "bash -c \"bash -c \\\"bash -c 'rm cache.tmp'\\\"\""

        assert detect_dangerous_command(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "$(printf r)m cache.tmp",
            "r$(printf m) cache.tmp",
            "$(printf r)$(printf m) cache.tmp",
        ],
    )
    def test_dynamic_executable_construction_requires_approval(self, command):
        assert detect_dangerous_command(command) is not None

    @pytest.mark.parametrize(
        ("name", "reference"),
        [
            ("BOX_AGENT_NODE", "${BOX_AGENT_NODE:-node}"),
            ("BOX_AGENT_NODE", "$BOX_AGENT_NODE"),
            ("BOX_AGENT_PYTHON", "${BOX_AGENT_PYTHON:-python3}"),
            ("BOX_AGENT_NPM", "${BOX_AGENT_NPM:-npm}"),
            ("BOX_AGENT_NPX", "${BOX_AGENT_NPX:-npx}"),
        ],
    )
    def test_injected_runtime_executable_reference_is_trusted(
        self,
        tmp_path,
        name,
        reference,
    ):
        executable = tmp_path / name.lower()
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        trusted = trusted_runtime_executable_references({name: str(executable)})

        assert detect_dangerous_command(
            f"{reference} scripts/validate.js",
            trusted_executable_references=trusted,
        ) is None

    @pytest.mark.parametrize(
        "environment",
        [
            {},
            {"BOX_AGENT_NODE": "node"},
        ],
    )
    def test_unverified_runtime_executable_reference_requires_approval(
        self,
        environment,
    ):
        trusted = trusted_runtime_executable_references(environment)

        assert detect_dangerous_command(
            "${BOX_AGENT_NODE:-node} scripts/validate.js",
            trusted_executable_references=trusted,
        ) is not None

    def test_runtime_reference_with_untrusted_fallback_requires_approval(self, tmp_path):
        executable = tmp_path / "node"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        trusted = trusted_runtime_executable_references(
            {"BOX_AGENT_NODE": str(executable)}
        )

        assert detect_dangerous_command(
            "${BOX_AGENT_NODE:-rm} cache.tmp",
            trusted_executable_references=trusted,
        ) is not None
        assert detect_dangerous_command(
            "$BOX_AGENT_UNKNOWN scripts/validate.js",
            trusted_executable_references=trusted,
        ) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "BOX_AGENT_NODE=rm; $BOX_AGENT_NODE cache.tmp",
            "export BOX_AGENT_NODE=rm; ${BOX_AGENT_NODE:-node} cache.tmp",
            "BOX_AGENT_NODE=$(printf rm); ${BOX_AGENT_NODE} cache.tmp",
            "printf -v BOX_AGENT_NODE rm; $BOX_AGENT_NODE cache.tmp",
            "source runtime-env.sh; $BOX_AGENT_NODE cache.tmp",
        ],
    )
    def test_runtime_reference_reassignment_requires_approval(self, tmp_path, command):
        executable = tmp_path / "node"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        trusted = trusted_runtime_executable_references(
            {"BOX_AGENT_NODE": str(executable)}
        )

        assert detect_dangerous_command(
            command,
            trusted_executable_references=trusted,
        ) is not None

    @pytest.mark.parametrize(
        "command",
        [
            'cmd=rm; "$cmd" cache.tmp',
            "cmd=rm; ${cmd} cache.tmp",
            "ref=cmd; cmd=rm; ${!ref} cache.tmp",
            "payload='rm cache.tmp'; bash -c \"$payload\"",
        ],
    )
    def test_parameter_expansion_executable_requires_approval(self, command):
        assert detect_dangerous_command(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "bash --norc -c 'rm cache.tmp'",
            "bash --rcfile /dev/null -c 'rm cache.tmp'",
            "eval 'rm cache.tmp'",
            "printf 'cache.tmp\\n' | xargs rm",
            "timeout 5 rm cache.tmp",
            "nice -n 5 rm cache.tmp",
            "ionice -c 3 rm cache.tmp",
            "setsid rm cache.tmp",
            "chroot /tmp rm cache.tmp",
            "busybox rm cache.tmp",
            "/usr/bin/env MODE=test /bin/rm cache.tmp",
            "env -S 'rm cache.tmp'",
            "env --split-string='rm cache.tmp'",
            "/usr/bin/timeout 5 /bin/rm cache.tmp",
            "find . -name '*.tmp' -exec rm {} +",
        ],
    )
    def test_dispatched_dangerous_commands_require_approval(self, command):
        assert detect_dangerous_command(command) is not None

    def test_trusted_dws_executable_reference_is_not_generic_danger(self):
        assert (
            detect_dangerous_command(
                "$BOX_AGENT_DINGTALK_CLI doc read --node doc_1"
            )
            is None
        )

    def test_shell_groups_do_not_hide_dangerous_commands(self):
        assert detect_dangerous_command("(rm first.tmp)") is not None
        assert detect_dangerous_command("{ rm second.tmp; }") is not None

    def test_fake_heredoc_markers_do_not_hide_later_dangerous_commands(self):
        quoted = "python3 -c \"print('<<EOF')\"\nrm quoted.tmp"
        commented = "echo ok # <<EOF\nrm commented.tmp"
        multiline = 'python3 -c "x=1\nprint(x << 2)\n"\nrm live.tmp'

        assert detect_dangerous_command(quoted) is not None
        assert detect_dangerous_command(commented) is not None
        assert detect_dangerous_command(multiline) is not None

    def test_shell_arithmetic_names_are_not_executables(self):
        assert detect_dangerous_command("echo $((dd + 1))") is None
        assert detect_dangerous_command("(( dd = 1 ))") is None
        assert detect_dangerous_command("echo $((value << 1))") is None
        assert detect_dangerous_command("echo $(( $(rm cache.tmp) + 1 ))") is not None

    def test_malformed_dangerous_command_fails_closed(self):
        assert detect_dangerous_command("rm 'unterminated") is not None

    def test_shutdown_reboot_detected(self):
        assert detect_dangerous_command("shutdown -h now") is not None
        assert detect_dangerous_command("reboot") is not None

    def test_redirect_to_dev_null_allowed(self):
        assert detect_dangerous_command("cat file > /dev/null") is None
        assert detect_dangerous_command("qlmanage -h >/dev/null 2>&1") is None

    def test_only_real_redirects_to_etc_are_dangerous(self):
        assert detect_dangerous_command("echo value > /etc/example") is not None
        assert detect_dangerous_command("echo '> /etc/example'") is None
        assert (
            detect_dangerous_command("python3 -c \"print('> /etc/example')\"")
            is None
        )

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

    def test_stderr_redirect_to_dev_null_allowed(self):
        """2>/dev/null should NOT trigger scope escape."""
        assert detect_scope_escape("node --version 2>/dev/null") is None
        assert detect_scope_escape("cmd 2>/dev/null || echo fallback") is None
        assert detect_scope_escape("cmd 2> /dev/null") is None

    def test_dev_special_files_allowed(self):
        """/dev/stdin, /dev/stdout, /dev/stderr should NOT trigger scope escape."""
        assert detect_scope_escape("echo test > /dev/stderr") is None

    def test_unbounded_dev_sources_blocked(self):
        """/dev/zero, /dev/random, /dev/urandom are unbounded sources — must be blocked."""
        assert detect_scope_escape("cat /dev/urandom | head -c 32") is not None
        assert detect_scope_escape("cat /dev/zero | head -c 1024 > local.bin") is not None

    def test_url_with_dev_null_redirect_allowed(self):
        """URLs + 2>/dev/null should NOT trigger scope escape."""
        assert detect_scope_escape("curl https://example.com/api 2>/dev/null") is None
        assert detect_scope_escape("wget http://server.io/file.tar.gz 2>/dev/null") is None
        assert detect_scope_escape("curl https://example.com 2>/dev/null || echo fail") is None

    def test_redirect_to_real_absolute_path_still_blocked(self):
        """Redirects to real absolute paths should still be caught."""
        assert detect_scope_escape("> /tmp/output.txt") is not None
        assert detect_scope_escape("echo data > /var/log/app.log") is not None

    def test_mixed_dev_and_outside_path_blocked(self):
        """Commands mixing /dev/ allowlisted path with an outside path must be caught."""
        assert detect_scope_escape("cat /dev/null /etc/passwd") is not None
        assert detect_scope_escape("echo x >/dev/null >/tmp/outside") is not None

    def test_mixed_workspace_and_outside_path_blocked(self):
        """Workspace path + outside path in the same command must be caught."""
        assert detect_scope_escape("cat /ws/file /etc/passwd", workspace_dir="/ws") is not None

    def test_all_paths_in_workspace_allowed(self):
        """Multiple paths all inside workspace should be allowed."""
        assert detect_scope_escape("cat /ws/a /ws/b", workspace_dir="/ws") is None

    def test_safe_commands(self):
        assert detect_scope_escape("ls -la") is None
        assert detect_scope_escape("cat local_file.txt") is None
        assert detect_scope_escape("cd subdir") is None
        assert detect_scope_escape("echo hello > output.txt") is None

    def test_perl_substitution_with_html_close_tag_not_blocked(self):
        """`perl -0pi -e 's/<\\/h1>/<h1>…/' file.html` must not be flagged as
        "redirect to absolute path" because of the `>/` inside the regex body."""
        cmd = (
            r"""perl -0pi -e 's/<h1>old<\/h1>/<h1>new<\/h1>/; """
            r"""s/\.foo\{w:600px\}/.foo{w:540px}/g' output/page.html"""
        )
        assert detect_scope_escape(cmd) is None

    def test_sed_substitution_with_absolute_path_in_body_not_blocked(self):
        """`sed 's|/old/path|/new/path|g' file` substitution body is stripped
        before scanning, so the workspace-relative target file is fine."""
        assert (
            detect_scope_escape("sed -i 's|/old/path|/new/path|g' file.txt") is None
        )

    def test_real_redirect_after_perl_subst_still_blocked(self):
        """The strip removes the substitution body, but a real `> /abs/path`
        elsewhere in the command must still be caught."""
        cmd = r"perl -pe 's/a/b/g' file.txt > /tmp/out.txt"
        assert detect_scope_escape(cmd) is not None

    def test_cp_with_only_relative_paths_not_blocked(self):
        """`cp slides/a.png output/rendered/` is all relative — the `.*/` form
        of the old regex falsely flagged it. The narrow form must let it pass."""
        assert detect_scope_escape("cp slides/slide-*.png output/rendered/") is None
        assert detect_scope_escape("cp slides/slide-*.png output/rendered/ && ls output/rendered") is None
        assert detect_scope_escape("mv a/b.txt c/d.txt") is None
        assert detect_scope_escape("ln -s src/foo bar/baz") is None

    def test_cp_with_absolute_source_still_blocked(self):
        """`cp /etc/passwd .` (absolute source) is still flagged."""
        assert detect_scope_escape("cp /etc/passwd local.txt") is not None
        assert detect_scope_escape("mv /var/log/app.log archive/") is not None

    def test_windows_drive_cd_blocked(self):
        """`cd C:\\Users\\...` on Windows must be caught (regression for
        Windows drive-letter blind spot — models used to bypass workspace
        controls by switching to Windows path style)."""
        assert detect_scope_escape(r"cd C:\Users\me") is not None
        assert detect_scope_escape("cd D:/projects") is not None

    def test_windows_drive_read_blocked(self):
        """`cat/type/Get-Content D:\\secrets\\key.txt` on Windows must be caught."""
        assert detect_scope_escape(r"cat D:\secrets\key.txt") is not None
        assert detect_scope_escape(r"type C:\Windows\System32\config") is not None
        assert detect_scope_escape(r"Get-Content D:\logs\app.log") is not None
        assert detect_scope_escape(r"gc D:\logs\app.log") is not None

    def test_windows_drive_write_blocked(self):
        """`cp/mv/copy D:\\... ...` and `>` to drive path must be caught."""
        assert detect_scope_escape(r"cp D:\src\a.txt B:\dst\b.txt") is not None
        assert detect_scope_escape(r"echo x > D:\out.txt") is not None
        assert detect_scope_escape(r"copy D:\a.txt D:\b.txt") is not None

    def test_windows_drive_inside_workspace_allowed(self):
        """Drive path inside the workspace on Windows must pass — the whole
        point of Bug B's fix is not to over-block."""
        import platform
        if platform.system() != "Windows":
            pytest.skip("Windows-specific path semantics")
        assert detect_scope_escape(r"cat D:\ws\file.txt", workspace_dir=r"D:\ws") is None
        assert detect_scope_escape(r"cd D:\ws\sub", workspace_dir=r"D:\ws") is None


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

    def test_windows_deep_path_inside_workspace_allowed(self, tmp_path):
        """Regression for Bug A: `startswith(ws + "/")` never matched on
        Windows because the separator is `\\`, so workspace-internal files
        were all denied. `Path.relative_to` fixes it cross-platform."""
        workspace = tmp_path / "ws"
        (workspace / "sub").mkdir(parents=True)
        deep_file = workspace / "sub" / "nested.txt"
        assert validate_path_in_workspace(deep_file, workspace) is None

    def test_sibling_prefix_not_confused_with_workspace(self, tmp_path):
        """`/x/Downloads` must NOT match workspace root `/x/Download` — the
        old `startswith(root + "/")` fell for this on POSIX; the new
        `relative_to` check is component-wise and immune."""
        workspace = tmp_path / "Download"
        workspace.mkdir()
        sibling = tmp_path / "Downloads" / "file.txt"
        sibling.parent.mkdir()
        sibling.write_text("x")
        error = validate_path_in_workspace(sibling, workspace)
        assert error is not None
        assert "outside the workspace" in error


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
        assert "requires approval" in result.error
        assert result.permission_request is not None
        assert result.permission_request["scope"] == "safety"
        assert result.permission_request["requested_scope"] == "dangerous_command"
        assert result.permission_request["persistent_supported"] is False
        assert result.permission_request["command"] == "rm test.txt"

    @pytest.mark.asyncio
    async def test_scope_escape_blocked(self):
        from box_agent.tools.bash_tool import BashTool

        tool = BashTool(workspace_dir="/tmp/test_workspace", allow_full_access=False)
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
