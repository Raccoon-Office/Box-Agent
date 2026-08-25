"""Tests for box_agent.tools.permissions — capability-based permission engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.config import (
    Config,
    FilesystemPermissions,
    LLMConfig,
    AgentConfig,
    MemoryPermissions,
    Officev3Config,
    Officev3Paths,
    Officev3Permissions,
    ToolsConfig,
)
from box_agent.tools.permissions import (
    FILESYSTEM_READ,
    FILESYSTEM_WRITE,
    MEMORY_OPENCLAW_IMPORT,
    CapabilityPolicy,
    PermissionDecision,
    PermissionEngine,
    extract_absolute_paths,
)


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def default_policy() -> CapabilityPolicy:
    return CapabilityPolicy()


@pytest.fixture
def engine(workspace: Path, default_policy: CapabilityPolicy) -> PermissionEngine:
    return PermissionEngine(default_policy, workspace)


# ── CapabilityPolicy ─────────────────────────────────────────


class TestCapabilityPolicy:
    def test_from_config(self):
        config = Config(
            llm=LLMConfig(api_key="test"),
            agent=AgentConfig(),
            tools=ToolsConfig(),
            officev3=Officev3Config(
                permissions=Officev3Permissions(
                    filesystem=FilesystemPermissions(scope="user_home"),
                    memory=MemoryPermissions(openclaw_import=False),
                ),
                paths=Officev3Paths(session_workspace_root="/tmp/ws"),
            ),
        )
        policy = CapabilityPolicy.from_config(config)
        assert policy.filesystem_scope == "user_home"
        assert policy.openclaw_import_enabled is False
        assert policy.session_workspace_root == "/tmp/ws"

    def test_from_config_defaults(self):
        """Default config produces session_workspace scope."""
        config = Config(
            llm=LLMConfig(api_key="test"),
            agent=AgentConfig(),
            tools=ToolsConfig(),
        )
        policy = CapabilityPolicy.from_config(config)
        assert policy.filesystem_scope == "session_workspace"
        assert policy.openclaw_import_enabled is True

    def test_with_overrides_scope(self):
        base = CapabilityPolicy(filesystem_scope="session_workspace")
        overridden = base.with_overrides({"filesystem": {"scope": "user_home"}})
        assert overridden.filesystem_scope == "user_home"
        # Original unchanged
        assert base.filesystem_scope == "session_workspace"

    def test_with_overrides_memory(self):
        base = CapabilityPolicy(openclaw_import_enabled=True)
        overridden = base.with_overrides({"memory": {"openclaw_import": False}})
        assert overridden.openclaw_import_enabled is False
        assert base.openclaw_import_enabled is True  # original unchanged

    def test_with_overrides_no_change(self):
        base = CapabilityPolicy()
        result = base.with_overrides({})
        assert result is base  # same instance returned when nothing changes

    def test_with_overrides_ignores_bad_types(self):
        base = CapabilityPolicy()
        result = base.with_overrides({"filesystem": "not_a_dict"})
        assert result is base

    def test_with_overrides_preserves_other_fields(self):
        base = CapabilityPolicy(session_workspace_root="/sws", openclaw_import_enabled=False)
        overridden = base.with_overrides({"filesystem": {"scope": "user_home"}})
        assert overridden.session_workspace_root == "/sws"
        assert overridden.openclaw_import_enabled is False

    def test_with_overrides_unknown_key_ignored(self):
        """Unknown keys in override dict are ignored, no crash."""
        base = CapabilityPolicy()
        result = base.with_overrides({"filesystem": {"scope": "user_home", "unknown_key": "x"}})
        assert result.filesystem_scope == "user_home"


# ── PermissionEngine: filesystem.read ────────────────────────


class TestFilesystemRead:
    def test_read_workspace_allowed(self, engine: PermissionEngine, workspace: Path):
        f = workspace / "data.csv"
        f.touch()
        decision = engine.check(FILESYSTEM_READ, {"path": str(f)})
        assert decision.allowed is True

    def test_read_workspace_subdir_allowed(self, engine: PermissionEngine, workspace: Path):
        d = workspace / "sub" / "dir"
        d.mkdir(parents=True)
        f = d / "file.txt"
        f.touch()
        decision = engine.check(FILESYSTEM_READ, {"path": str(f)})
        assert decision.allowed is True

    def test_read_outside_workspace_denied_with_escalation(self, engine: PermissionEngine):
        home = Path.home()
        decision = engine.check(FILESYSTEM_READ, {"path": str(home / "Desktop" / "a.txt")})
        assert decision.allowed is False
        assert decision.permission_request is not None
        # Canonical format: scope + requested_scope + path
        assert decision.permission_request["scope"] == "filesystem"
        assert decision.permission_request["requested_scope"] == "user_home"
        assert "path" in decision.permission_request

    def test_read_outside_home_denied_no_escalation(self, engine: PermissionEngine):
        decision = engine.check(FILESYSTEM_READ, {"path": "/etc/passwd"})
        assert decision.allowed is False
        assert decision.permission_request is None
        assert "outside all allowed scopes" in decision.reason

    def test_read_user_home_scope(self, workspace: Path):
        policy = CapabilityPolicy(filesystem_scope="user_home")
        eng = PermissionEngine(policy, workspace)
        home = Path.home()
        decision = eng.check(FILESYSTEM_READ, {"path": str(home / "Desktop" / "a.txt")})
        assert decision.allowed is True

    def test_read_user_home_scope_outside_home_denied(self, workspace: Path):
        policy = CapabilityPolicy(filesystem_scope="user_home")
        eng = PermissionEngine(policy, workspace)
        decision = eng.check(FILESYSTEM_READ, {"path": "/etc/passwd"})
        assert decision.allowed is False
        assert decision.permission_request is None  # implicit POSIX system paths never escalate

    def test_read_session_workspace_root(self, workspace: Path, tmp_path: Path):
        sws_root = tmp_path / "sws_root"
        sws_root.mkdir()
        policy = CapabilityPolicy(session_workspace_root=str(sws_root))
        eng = PermissionEngine(policy, workspace)
        f = sws_root / "report.pdf"
        f.touch()
        decision = eng.check(FILESYSTEM_READ, {"path": str(f)})
        assert decision.allowed is True

    def test_no_home_prefix_match_false_positive(self, workspace: Path):
        """Ensure /Users/abc is not matched by /Users/a prefix rule."""
        policy = CapabilityPolicy(filesystem_scope="user_home")
        home = Path.home()
        fake_sibling = Path(str(home) + "_other") / "file.txt"
        eng = PermissionEngine(policy, workspace)
        decision = eng.check(FILESYSTEM_READ, {"path": str(fake_sibling)})
        assert decision.allowed is False

    def test_symlink_outside_workspace_denied(self, engine: PermissionEngine, workspace: Path):
        """Symlink inside workspace pointing outside should follow symlink and deny."""
        import os
        target = workspace / "link_to_etc"
        try:
            os.symlink("/etc", str(target))
            decision = engine.check(FILESYSTEM_READ, {"path": str(target / "passwd")})
            assert decision.allowed is False
            assert decision.permission_request is None  # /etc is outside home, no escalation
        except (OSError, PermissionError):
            pytest.skip("Cannot create symlink in this environment")

    def test_read_application_dir_allowed(self, engine: PermissionEngine):
        """Read-only probing of app/executable install roots is allowed."""
        decision = engine.check(
            FILESYSTEM_READ,
            {"path": "/Applications/LibreOffice.app/Contents/MacOS/soffice"},
        )
        assert decision.allowed is True

    def test_bash_read_builtin_skill_dir_allowed(
        self, engine: PermissionEngine, tmp_path: Path
    ):
        """Bash may run trusted scripts shipped inside the Box-Agent package."""
        skills_dir = tmp_path / "packaged-runtime" / "box_agent" / "skills"
        script = skills_dir / "document-skills" / "pptx" / "scripts" / "validate_outline.js"
        script.parent.mkdir(parents=True)
        script.touch()
        engine._builtin_skills_dir = skills_dir.resolve()
        engine._home_dir = (tmp_path / "fake-home").resolve()

        decision = engine.check(
            FILESYSTEM_READ,
            {"path": str(script)},
            tool_name="bash",
        )

        assert decision.allowed is True

    def test_bash_read_unrelated_application_dir_still_denied(
        self, engine: PermissionEngine
    ):
        """The Bash exception stays narrower than the generic application roots."""
        decision = engine.check(
            FILESYSTEM_READ,
            {"path": "/Applications/LibreOffice.app/Contents/MacOS/soffice"},
            tool_name="bash",
        )
        assert decision.allowed is False


# ── PermissionEngine: filesystem.write ───────────────────────


class TestFilesystemWrite:
    def test_write_workspace_allowed(self, engine: PermissionEngine, workspace: Path):
        f = workspace / "output.csv"
        decision = engine.check(FILESYSTEM_WRITE, {"path": str(f)})
        assert decision.allowed is True

    def test_write_outside_workspace_denied(self, engine: PermissionEngine):
        home = Path.home()
        decision = engine.check(FILESYSTEM_WRITE, {"path": str(home / "Documents" / "x.txt")})
        assert decision.allowed is False
        assert decision.permission_request is not None
        assert decision.permission_request["scope"] == "filesystem"
        assert decision.permission_request["requested_scope"] == "user_home"

    def test_write_nonexistent_path_resolves_parent(self, engine: PermissionEngine, workspace: Path):
        f = workspace / "new_dir" / "new_file.txt"
        decision = engine.check(FILESYSTEM_WRITE, {"path": str(f)})
        assert decision.allowed is True

    def test_write_nonexistent_outside_workspace(self, engine: PermissionEngine):
        """Non-existing path outside workspace correctly denied."""
        home = Path.home()
        new_path = home / "nonexistent_dir_xyz" / "new_file.txt"
        decision = engine.check(FILESYSTEM_WRITE, {"path": str(new_path)})
        assert decision.allowed is False
        assert decision.permission_request is not None  # under home → escalation suggested

    def test_write_os_temp_allowed(self, engine: PermissionEngine):
        """Transient shell output under the OS temp root is allowed."""
        decision = engine.check(FILESYSTEM_WRITE, {"path": "/tmp/box_agent_check.txt"})
        assert decision.allowed is True

    def test_write_application_dir_denied(self, engine: PermissionEngine):
        """Application install roots are probeable but not writable."""
        decision = engine.check(
            FILESYSTEM_WRITE,
            {"path": "/Applications/LibreOffice.app/Contents/MacOS/soffice"},
        )
        assert decision.allowed is False
        assert decision.permission_request is None

    def test_bash_write_builtin_skill_dir_denied(
        self, engine: PermissionEngine, tmp_path: Path
    ):
        """Trusted packaged skills are read-only even when invoked by Bash."""
        skills_dir = tmp_path / "packaged-runtime" / "box_agent" / "skills"
        script = skills_dir / "document-skills" / "pptx" / "scripts" / "validate_outline.js"
        script.parent.mkdir(parents=True)
        script.touch()
        engine._builtin_skills_dir = skills_dir.resolve()
        engine._home_dir = (tmp_path / "fake-home").resolve()

        decision = engine.check(
            FILESYSTEM_WRITE,
            {"path": str(script)},
            tool_name="bash",
        )

        assert decision.allowed is False
        assert decision.permission_request is None


# ── PermissionEngine: memory.openclaw_import ─────────────────


class TestMemoryOpenclaw:
    def test_openclaw_enabled(self, workspace: Path):
        policy = CapabilityPolicy(openclaw_import_enabled=True)
        eng = PermissionEngine(policy, workspace)
        decision = eng.check(MEMORY_OPENCLAW_IMPORT, {"source": "openclaw"})
        assert decision.allowed is True

    def test_openclaw_disabled(self, workspace: Path):
        policy = CapabilityPolicy(openclaw_import_enabled=False)
        eng = PermissionEngine(policy, workspace)
        decision = eng.check(MEMORY_OPENCLAW_IMPORT, {"source": "openclaw"})
        assert decision.allowed is False
        assert decision.permission_request is not None
        # Canonical format for memory permission_request
        assert decision.permission_request["scope"] == "memory"
        assert decision.permission_request["requested_scope"] == "openclaw_import"

    def test_unknown_capability(self, engine: PermissionEngine):
        decision = engine.check("unknown.capability", {})
        assert decision.allowed is False
        assert "Unknown capability" in decision.reason


# ── extract_absolute_paths ───────────────────────────────────


class TestExtractAbsolutePaths:
    def test_single_path(self):
        assert extract_absolute_paths("cat /etc/hosts") == ["/etc/hosts"]

    def test_multiple_paths(self):
        result = extract_absolute_paths("cp /tmp/a.txt /home/user/b.txt")
        assert "/tmp/a.txt" in result
        assert "/home/user/b.txt" in result

    def test_quoted_path(self):
        result = extract_absolute_paths('cat "/tmp/my file.txt"')
        assert "/tmp/my file.txt" in result

    def test_single_quoted_path_with_space(self):
        result = extract_absolute_paths("cat '/tmp/my file.txt'")
        assert "/tmp/my file.txt" in result

    def test_quoted_workspace_path_with_spaces_and_cjk(self):
        """Workspace paths with spaces + non-ASCII must be kept whole.

        Regression: `/Users/WorkBuddy/02 小浣熊工作区/file.txt` was being
        truncated to `/Users/WorkBuddy/02` by the whitespace-bounded char
        class in `_ABS_PATH_RE`, which then failed the workspace check.
        """
        result = extract_absolute_paths(
            'cat "/Users/WorkBuddy/02 小浣熊工作区/file.txt"'
        )
        assert "/Users/WorkBuddy/02 小浣熊工作区/file.txt" in result
        assert "/Users/WorkBuddy/02" not in result

    def test_quoted_windows_drive_letter_with_space(self):
        """`"C:\\Users\\foo bar\\file.txt"` is extracted whole."""
        result = extract_absolute_paths(
            'Get-Content "C:\\Users\\foo bar\\file.txt"'
        )
        assert "C:\\Users\\foo bar\\file.txt" in result

    def test_quoted_windows_forward_slash_drive(self):
        """`"C:/Users/foo bar/file.txt"` (forward slash) is extracted whole."""
        result = extract_absolute_paths('cat "C:/Users/foo bar/file.txt"')
        assert "C:/Users/foo bar/file.txt" in result

    def test_unquoted_windows_drive_backslash(self):
        """`type C:\\Windows\\System32\\hosts` (no quotes) is extracted.

        Without this, the permission engine would silently miss every
        unquoted Windows-drive path on Windows hosts.
        """
        result = extract_absolute_paths("type C:\\Windows\\System32\\hosts")
        assert "C:\\Windows\\System32\\hosts" in result

    def test_unquoted_windows_drive_forward_slash(self):
        """Unquoted `C:/Users/admin/secret.txt` is extracted."""
        result = extract_absolute_paths("cat C:/Users/admin/secret.txt")
        assert "C:/Users/admin/secret.txt" in result

    def test_unquoted_windows_drive_stops_at_shell_separator(self):
        """Unquoted Windows path terminates at `;`, `|`, `&`, whitespace."""
        result = extract_absolute_paths("cd C:\\tmp; ls")
        assert "C:\\tmp" in result
        assert "C:\\tmp;" not in result

    def test_cmd_switches_are_not_treated_as_absolute_paths(self):
        command = (
            'cmd.exe /d /c "rmdir \\"D:\\fly\\delete-me\\" '
            '&& if exist \\"D:\\fly\\delete-me\\" (exit /b 2)"'
        )

        result = extract_absolute_paths(command)

        assert "/d" not in result
        assert "/c" not in result
        assert "/b" not in result
        assert "D:\\fly\\delete-me\\" in result

    def test_direct_cmd_exit_switch_is_not_treated_as_absolute_path(self):
        command = (
            'rmdir "D:\\fly\\delete-me" && '
            'if exist "D:\\fly\\delete-me" '
            '(echo DELETE_FAILED & exit /b 1) else (echo DELETED)'
        )

        result = extract_absolute_paths(command)

        assert "/b" not in result
        assert result == ["D:\\fly\\delete-me"]

    def test_url_scheme_not_treated_as_windows_drive(self):
        """`https:`, `file:`, `git:` etc. (multi-char schemes) are NOT drives."""
        assert extract_absolute_paths("curl https://example.com/x.txt") == []
        assert extract_absolute_paths("open file:///etc/hosts") == []
        assert extract_absolute_paths("git clone git://host/x.git") == []

    def test_drive_letter_without_path_separator_not_extracted(self):
        """`D:00:00` (time) and `TODO:` (label) must not be parsed as drives."""
        assert extract_absolute_paths("echo D:00:00") == []
        assert extract_absolute_paths("echo TODO: fix") == []

    def test_quoted_tilde_path_with_space(self):
        home = str(Path.home())
        result = extract_absolute_paths('cat "~/Box 工作区/file.txt"')
        assert f"{home}/Box 工作区/file.txt" in result

    def test_quoted_home_var_path_with_space(self):
        home = str(Path.home())
        result = extract_absolute_paths('cat "$HOME/Box 工作区/file.txt"')
        assert f"{home}/Box 工作区/file.txt" in result

    def test_quoted_dev_null_excluded(self):
        """Even inside quotes, `/dev/null` is not a real target."""
        result = extract_absolute_paths('echo hi > "/dev/null"')
        assert "/dev/null" not in result

    def test_dev_null_excluded(self):
        result = extract_absolute_paths("command 2>/dev/null")
        assert "/dev/null" not in result

    def test_inline_absolute_path_variables_are_expanded(self):
        result = extract_absolute_paths(
            'ROOT=/tmp/deck\nASSETS="$ROOT/assets"\nmkdir -p "$ASSETS" 2>/dev/null'
        )

        assert "/tmp/deck/assets" in result
        assert "/dev/null" not in result

    def test_no_paths(self):
        assert extract_absolute_paths("ls -la") == []

    def test_relative_paths_ignored(self):
        assert extract_absolute_paths("cat ./foo.txt ../bar.txt") == []

    def test_shell_expansion_not_extracted(self):
        """~ paths are expanded to real home directory."""
        result = extract_absolute_paths("cat ~/Desktop/file.txt")
        home = str(Path.home())
        assert f"{home}/Desktop/file.txt" in result

    def test_home_var_not_extracted(self):
        """$HOME paths are expanded to real home directory."""
        result = extract_absolute_paths("cat $HOME/file.txt")
        home = str(Path.home())
        assert f"{home}/file.txt" in result

    def test_tilde_path_extracted(self):
        """ls ~/Downloads expands to home/Downloads."""
        home = str(Path.home())
        result = extract_absolute_paths("ls ~/Downloads")
        assert f"{home}/Downloads" in result

    def test_bare_tilde_extracted(self):
        """ls ~ expands to home directory."""
        home = str(Path.home())
        result = extract_absolute_paths("ls ~")
        assert home in result

    def test_home_var_path_extracted(self):
        """cat $HOME/file.txt expands to home/file.txt."""
        home = str(Path.home())
        result = extract_absolute_paths("cat $HOME/file.txt")
        assert f"{home}/file.txt" in result

    def test_bare_home_var_extracted(self):
        """echo $HOME expands to home directory."""
        home = str(Path.home())
        result = extract_absolute_paths("echo $HOME")
        assert home in result

    def test_tilde_not_in_word(self):
        """file~bak should not be matched as a tilde path."""
        result = extract_absolute_paths("cat file~bak")
        home = str(Path.home())
        for p in result:
            assert not p.startswith(home)

    def test_mixed_paths(self):
        """Command with both absolute and tilde paths returns both."""
        home = str(Path.home())
        result = extract_absolute_paths("cp /etc/hosts ~/backup/hosts")
        assert "/etc/hosts" in result
        assert f"{home}/backup/hosts" in result

    def test_deduplication(self):
        """Duplicate paths are deduplicated."""
        result = extract_absolute_paths("cat /etc/hosts /etc/hosts")
        assert result.count("/etc/hosts") == 1

    def test_bare_root_dropped(self):
        """`cd /; ls` should not extract bare `/` — that was a false positive
        producing 'write to / outside all allowed scopes'."""
        result = extract_absolute_paths("cd /; ls")
        assert "/" not in result

    def test_bare_system_root_dropped(self):
        """Bare system roots like /etc, /usr, /opt are not real targets."""
        for cmd in ("cd /etc; ls", "ls /usr ", "echo /opt;"):
            result = extract_absolute_paths(cmd)
            assert "/etc" not in result
            assert "/usr" not in result
            assert "/opt" not in result

    def test_subpath_under_system_root_kept(self):
        """A real subpath under a system root is still extracted."""
        assert "/etc/hosts" in extract_absolute_paths("cat /etc/hosts")
        assert "/usr/local/bin/foo" in extract_absolute_paths(
            "cp x /usr/local/bin/foo"
        )

    def test_sed_substitution_not_extracted(self):
        """The regex bodies of `sed 's/.../.../g'` must not yield fake paths."""
        cmd = r"sed 's/(\w+)/<strong>$1<\/strong>/g' /tmp/page.html"
        result = extract_absolute_paths(cmd)
        # The real file argument is kept; the substitution body is stripped.
        assert "/tmp/page.html" in result
        for fake in result:
            assert "<" not in fake and ">" not in fake and "$" not in fake

    def test_sed_alt_delimiter_not_extracted(self):
        """`sed 's|a|b|g'` with `|` delimiter also gets stripped."""
        cmd = "sed 's|/old/path|/new/path|g' /tmp/in.txt"
        result = extract_absolute_paths(cmd)
        assert result == ["/tmp/in.txt"]

    def test_sed_hash_delimiter_not_extracted(self):
        """`sed 's#a#b#'` with `#` delimiter — bodies stripped, file kept."""
        cmd = "sed 's#/a/b#/c/d#' /tmp/in.txt"
        result = extract_absolute_paths(cmd)
        assert result == ["/tmp/in.txt"]

    def test_path_with_html_brackets_rejected(self):
        """An ad-hoc fragment like `/<strong>...</strong>/g` is not a path."""
        result = extract_absolute_paths("echo /<strong>$1</strong>/g")
        assert result == []

    def test_css_and_html_comments_are_not_treated_as_root_paths(self):
        workspace = "/workspace/output"
        command = (
            f"cd {workspace} && python3 - <<'PY'\n"
            "css = '''/* ---------- P2 三个问题 ---------- */'''\n"
            "html = '<div><!-- /.stage --></div>'\n"
            "PY"
        )

        assert extract_absolute_paths(command) == [workspace]

    def test_javascript_line_comment_marker_is_root_noise(self):
        workspace = "/workspace/output"
        command = (
            f"cd {workspace} && cat > script.js <<'JS'\n"
            "// 使用 CDP 滚动并截图\n"
            "JS"
        )

        assert extract_absolute_paths(command) == [workspace]

    def test_real_path_inside_source_payload_remains_enforced(self):
        command = "python3 - <<'PY'\npath = '/etc/hosts'\nPY"

        assert extract_absolute_paths(command) == ["/etc/hosts"]

    def test_real_path_is_enforced_when_same_text_also_appears_in_comment(self):
        command = (
            "python3 - <<'PY'\n"
            "css = '/* example /etc/hosts */'\n"
            "path = '/etc/hosts'\n"
            "PY"
        )

        assert extract_absolute_paths(command) == ["/etc/hosts"]

    def test_word_prefix_s_not_treated_as_sed(self):
        """A token ending in `s/` like `ls/foo` must not trigger the sed strip."""
        # `users/` is not a sed substitution; the real path must survive.
        result = extract_absolute_paths("ls /Users/alice/projects")
        assert result == ["/Users/alice/projects"]

    def test_quoted_variable_glob_not_extracted_as_path(self):
        """`"$SRC"/slide-*.png` must not yield a phantom `/slide-*.png` path.

        The closing `"` of a `"$VAR"` (or `"${VAR}"`) is followed by `/`, but
        the slash belongs to the expanded variable — it is not a fresh
        absolute path."""
        cmd = (
            'SRC="/Users/me/data"; '
            'cp "$SRC"/slide-*.png /tmp/out/'
        )
        result = extract_absolute_paths(cmd)
        assert "/slide-*.png" not in result
        # The legit absolute paths are still extracted.
        assert "/Users/me/data" in result
        assert "/tmp/out/" in result

    def test_quoted_braced_variable_glob_not_extracted(self):
        """Same fix must cover `"${SRC}"/file.png`."""
        cmd = 'cp "${SRC}"/slide-*.png /tmp/out/'
        result = extract_absolute_paths(cmd)
        assert "/slide-*.png" not in result
        assert "/tmp/out/" in result

    def test_opening_quote_path_still_extracted(self):
        """An *opening* quote in front of an absolute path must still work."""
        result = extract_absolute_paths('cp "/Users/me/file.txt" /tmp/')
        assert "/Users/me/file.txt" in result
        assert "/tmp/" in result


# ── CapabilityPolicy.with_filesystem_overrides ───────────────


class TestFilesystemOverrides:
    def test_session_workspace_root_override(self):
        base = CapabilityPolicy(session_workspace_root="/old/root")
        new = base.with_filesystem_overrides(session_workspace_root="/new/root")
        assert new.session_workspace_root == "/new/root"
        assert base.session_workspace_root == "/old/root"  # immutable

    def test_allowed_directories_merged(self):
        base = CapabilityPolicy(allowed_directories=("/a",))
        new = base.with_filesystem_overrides(allowed_directories=["/b", "/a"])
        assert "/a" in new.allowed_directories
        assert "/b" in new.allowed_directories
        # No duplicate
        assert list(new.allowed_directories).count("/a") == 1

    def test_allowed_directories_replaced_for_session_policy(self):
        base = CapabilityPolicy(allowed_directories=("/global",))
        new = base.with_filesystem_overrides(
            allowed_directories=["/session", "/session"],
            replace_allowed_directories=True,
        )

        assert new.allowed_directories == ("/session",)
        assert base.allowed_directories == ("/global",)

    def test_no_args_returns_self(self):
        base = CapabilityPolicy(session_workspace_root="/r")
        new = base.with_filesystem_overrides()
        assert new is base


# ── Config YAML parsing ─────────────────────────────────────


class TestConfigParsing:
    def test_officev3_absent(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'api_key: "test-key"\n'
            'model: "test-model"\n',
            encoding="utf-8",
        )
        config = Config.from_yaml(config_file)
        assert config.officev3._present is False

    def test_officev3_present_defaults(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'api_key: "test-key"\n'
            'model: "test-model"\n'
            'officev3:\n'
            '  permissions:\n'
            '    filesystem:\n'
            '      scope: session_workspace\n',
            encoding="utf-8",
        )
        config = Config.from_yaml(config_file)
        assert config.officev3._present is True
        assert config.officev3.permissions.filesystem.scope == "session_workspace"

    def test_officev3_user_home(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'api_key: "test-key"\n'
            'model: "test-model"\n'
            'officev3:\n'
            '  permissions:\n'
            '    filesystem:\n'
            '      scope: user_home\n'
            '    memory:\n'
            '      openclaw_import: false\n'
            '  paths:\n'
            '    session_workspace_root: /tmp/sws\n',
            encoding="utf-8",
        )
        config = Config.from_yaml(config_file)
        assert config.officev3._present is True
        assert config.officev3.permissions.filesystem.scope == "user_home"
        assert config.officev3.permissions.memory.openclaw_import is False
        assert config.officev3.paths.session_workspace_root == "/tmp/sws"

    def test_officev3_malformed_block(self, tmp_path: Path):
        """officev3 block present but value is not a dict — parsed as absent."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'api_key: "test-key"\n'
            'model: "test-model"\n'
            'officev3: "not_a_dict"\n',
            encoding="utf-8",
        )
        config = Config.from_yaml(config_file)
        assert config.officev3._present is False

    def test_officev3_empty_block(self, tmp_path: Path):
        """officev3: {} — present with all defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'api_key: "test-key"\n'
            'model: "test-model"\n'
            'officev3: {}\n',
            encoding="utf-8",
        )
        config = Config.from_yaml(config_file)
        assert config.officev3._present is True
        assert config.officev3.permissions.filesystem.scope == "session_workspace"

    def test_officev3_partial_block_memory_only(self, tmp_path: Path):
        """officev3 block with only memory section — filesystem gets defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'api_key: "test-key"\n'
            'model: "test-model"\n'
            'officev3:\n'
            '  permissions:\n'
            '    memory:\n'
            '      openclaw_import: false\n',
            encoding="utf-8",
        )
        config = Config.from_yaml(config_file)
        assert config.officev3._present is True
        assert config.officev3.permissions.filesystem.scope == "session_workspace"
        assert config.officev3.permissions.memory.openclaw_import is False

    def test_present_flag_survives_model_copy(self, tmp_path: Path):
        """_present PrivateAttr must survive model_copy."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'api_key: "test-key"\n'
            'model: "test-model"\n'
            'officev3:\n'
            '  permissions:\n'
            '    filesystem:\n'
            '      scope: user_home\n',
            encoding="utf-8",
        )
        config = Config.from_yaml(config_file)
        assert config.officev3._present is True
        copied = config.officev3.model_copy()
        assert copied._present is True


# ── ToolResult.permission_request ────────────────────────────


class TestToolResultPermissionRequest:
    def test_default_none(self):
        from box_agent.tools.base import ToolResult
        result = ToolResult(success=True, content="ok")
        assert result.permission_request is None

    def test_with_request(self):
        from box_agent.tools.base import ToolResult
        result = ToolResult(
            success=False,
            error="denied",
            permission_request={
                "type": "permission_request",
                "scope": "filesystem",
                "requested_scope": "user_home",
            },
        )
        assert result.permission_request is not None
        assert result.permission_request["scope"] == "filesystem"


# ── PermissionRequestEvent ───────────────────────────────────


class TestPermissionRequestEvent:
    def test_create(self):
        from box_agent.events import PermissionRequestEvent
        evt = PermissionRequestEvent(
            tool_call_id="tc-1",
            scope="filesystem",
            requested_scope="user_home",
            path="/home/user/file.txt",
            reason="Path is outside session_workspace",
        )
        assert evt.scope == "filesystem"
        assert evt.requested_scope == "user_home"
        assert evt.path == "/home/user/file.txt"
        assert evt.temporary_supported is True
        assert evt.persistent_supported is True

    def test_in_agent_event_union(self):
        from box_agent.events import AgentEvent, PermissionRequestEvent
        import typing
        args = typing.get_args(AgentEvent)
        assert PermissionRequestEvent in args

    def test_memory_event_no_path(self):
        """Memory permission events have empty path."""
        from box_agent.events import PermissionRequestEvent
        evt = PermissionRequestEvent(
            tool_call_id="tc-1",
            scope="memory",
            requested_scope="openclaw_import",
            reason="disabled",
        )
        assert evt.path == ""

    def test_payload_shape(self):
        """Verify canonical payload matches box-agent-permissions.md."""
        from box_agent.events import PermissionRequestEvent
        evt = PermissionRequestEvent(
            tool_call_id="tc-1",
            scope="filesystem",
            requested_scope="user_home",
            path="/Users/me/Downloads/report.pdf",
            reason="Path is outside session_workspace",
        )
        # Simulate what acp/__init__.py sends
        payload = {
            "type": "permission_request",
            "scope": evt.scope,
            "requested_scope": evt.requested_scope,
            "path": evt.path,
            "reason": evt.reason,
            "temporary_supported": evt.temporary_supported,
            "persistent_supported": evt.persistent_supported,
        }
        assert payload["type"] == "permission_request"
        assert payload["scope"] == "filesystem"
        assert payload["requested_scope"] == "user_home"
        assert payload["path"] == "/Users/me/Downloads/report.pdf"
        assert "capability" not in payload   # old field must be gone
        assert "resource" not in payload     # old field must be gone


# ── Bash tool phase 1 limitation tests ──────────────────────


class TestBashPermissionPhase1:
    """Verify bash phase 1 conservative denial behavior."""

    async def _run_bash(self, command: str, perm_engine: PermissionEngine):
        from box_agent.tools.bash_tool import BashTool
        tool = BashTool(
            workspace_dir="/tmp",
            allow_full_access=False,
            non_interactive=True,
            permission_engine=perm_engine,
        )
        return await tool.execute(command)

    async def test_full_access_still_requires_dangerous_command_approval(self, workspace: Path):
        from box_agent.tools.bash_tool import BashTool

        tool = BashTool(
            workspace_dir=str(workspace),
            allow_full_access=True,
            non_interactive=True,
            permission_engine=None,
        )
        result = await tool.execute(f'rm -rf "{workspace / "missing-target"}"')

        assert result.permission_request is not None
        assert result.permission_request["scope"] == "safety"
        assert result.permission_request["persistent_supported"] is False
        assert "Approval required" in (result.stderr or "")

    def _make_engine(self, workspace: Path) -> PermissionEngine:
        policy = CapabilityPolicy()  # session_workspace scope
        eng = PermissionEngine(policy, workspace)
        # /usr/bin is in _app_read_dirs; on Linux /bin → /usr/bin symlink makes
        # absolute binary paths (e.g. /bin/echo) pass the read check. Clear it
        # so the "outside binary denied" assertion fires correctly.
        eng._app_read_dirs = ()
        return eng

    async def test_absolute_path_outside_workspace_denied(self, workspace: Path):
        """Command with absolute path outside workspace is denied."""
        eng = self._make_engine(workspace)
        result = await self._run_bash("cat /etc/passwd", eng)
        assert result.success is False

    async def test_tilde_path_conservatively_denied(self, workspace: Path):
        """Command with ~ triggers permission engine with proper permission_request.

        Now that ~ is expanded, commands like 'cd ~' and 'ls ~' are properly
        handled by the permission engine with extractable paths.
        """
        eng = self._make_engine(workspace)
        result = await self._run_bash("cd ~ && ls", eng)
        assert result.success is False

    async def test_tilde_path_denied_with_permission_request(self, workspace: Path):
        """ls ~/Downloads triggers permission engine, returns permission_request."""
        eng = self._make_engine(workspace)
        result = await self._run_bash("ls ~/Downloads", eng)
        assert result.success is False
        assert result.permission_request is not None
        assert result.permission_request["type"] == "permission_request"
        assert result.permission_request["scope"] == "filesystem"
        assert result.permission_request["requested_scope"] == "user_home"

    async def test_home_var_denied_with_permission_request(self, workspace: Path):
        """cat $HOME/file triggers permission engine, returns permission_request."""
        eng = self._make_engine(workspace)
        result = await self._run_bash("cat $HOME/file.txt", eng)
        assert result.success is False
        assert result.permission_request is not None
        assert result.permission_request["type"] == "permission_request"
        assert result.permission_request["scope"] == "filesystem"
        assert result.permission_request["requested_scope"] == "user_home"

    async def test_write_command_uses_write_capability(self, workspace: Path):
        """cp/mv-like commands outside workspace are denied using write capability."""
        eng = self._make_engine(workspace)
        result = await self._run_bash("cp /etc/foo /tmp/bar", eng)
        assert result.success is False

    async def test_tmp_redirect_allowed(self, workspace: Path):
        """Temporary shell reports under /tmp are allowed."""
        eng = self._make_engine(workspace)
        result = await self._run_bash("printf ok >/tmp/box_agent_perm_test.txt && tail -n 1 /tmp/box_agent_perm_test.txt", eng)
        assert result.success is True
        assert "ok" in result.stdout

    async def test_tmp_redirect_without_later_read_allowed(self, workspace: Path):
        """A bare redirect target like >/tmp/file is extractable and allowed."""
        eng = self._make_engine(workspace)
        result = await self._run_bash("printf ok >/tmp/box_agent_perm_redirect_only.txt", eng)
        assert result.success is True

    async def test_workspace_command_allowed(self, workspace: Path):
        """Commands referencing workspace paths are NOT blocked by permission engine."""
        eng = self._make_engine(workspace)
        result = await self._run_bash(f"ls {workspace}", eng)
        assert result.permission_request is None

    async def test_stderr_redirect_not_blocked(self, workspace: Path):
        """2>/dev/null should not trigger the permission engine."""
        eng = self._make_engine(workspace)
        result = await self._run_bash("echo test 2>/dev/null", eng)
        assert result.success is True

    async def test_inline_workspace_path_variable_is_checked_and_allowed(
        self,
        workspace: Path,
    ):
        eng = self._make_engine(workspace)
        target = workspace / "assets"
        result = await self._run_bash(
            f'OUT={target}\nmkdir -p "$OUT"\nprintf ok > "$OUT/result.txt"',
            eng,
        )

        assert result.success is True
        assert (target / "result.txt").read_text(encoding="utf-8") == "ok"

    async def test_fd_redirect_does_not_upgrade_builtin_skill_read_to_write(
        self, workspace: Path, tmp_path: Path
    ):
        """A diagnostic 2>&1 keeps a packaged script path read-only."""
        skills_dir = tmp_path / "packaged-runtime" / "box_agent" / "skills"
        script = skills_dir / "document-skills" / "pptx" / "scripts" / "inspect_deck_contract.js"
        script.parent.mkdir(parents=True)
        script.write_text("packaged-script", encoding="utf-8")
        eng = self._make_engine(workspace)
        eng._builtin_skills_dir = skills_dir.resolve()
        eng._home_dir = (tmp_path / "fake-home").resolve()

        result = await self._run_bash(f"cat {script} 2>&1", eng)

        assert result.success is True
        assert result.stdout.strip() == "packaged-script"

    async def test_redirect_write_does_not_upgrade_other_paths(
        self, workspace: Path, tmp_path: Path
    ):
        """Only the redirect target needs write access; the packaged script stays read."""
        skills_dir = tmp_path / "packaged-runtime" / "box_agent" / "skills"
        script = skills_dir / "document-skills" / "pptx" / "scripts" / "inspect_deck_contract.js"
        script.parent.mkdir(parents=True)
        script.write_text("packaged-script", encoding="utf-8")
        denied_target = tmp_path / "outside" / "result.txt"
        denied_target.parent.mkdir()
        eng = self._make_engine(workspace)
        eng._builtin_skills_dir = skills_dir.resolve()
        eng._home_dir = (tmp_path / "fake-home").resolve()

        result = await self._run_bash(f"cat {script} > {denied_target}", eng)

        assert result.success is False
        assert f"write to {denied_target}" in (result.error or "")

    async def test_dev_null_plus_outside_binary_still_denied(self, workspace: Path):
        """Absolute binary path + 2>/dev/null: /dev/null is safe but /bin/echo is not.

        The full-command scan detects /bin/echo as an unsafe path, so the
        escape check correctly fires and the permission engine denies it.
        """
        eng = self._make_engine(workspace)
        result = await self._run_bash("/bin/echo hello 2>/dev/null", eng)
        assert result.success is False


# ── ACP override integration ─────────────────────────────────


class TestAcpPermissionOverride:
    """Verify session-level policy overrides work correctly."""

    def test_base_deny_override_allow(self, workspace: Path):
        """Base policy denies; session override expands to user_home → allow."""
        base_policy = CapabilityPolicy(filesystem_scope="session_workspace")
        overridden = base_policy.with_overrides({"filesystem": {"scope": "user_home"}})
        eng = PermissionEngine(overridden, workspace)
        home = Path.home()
        decision = eng.check(FILESYSTEM_READ, {"path": str(home / "Desktop" / "report.pdf")})
        assert decision.allowed is True

    def test_base_allow_unchanged_when_override_does_not_touch_scope(self, workspace: Path):
        """Override that only changes memory does not affect filesystem scope."""
        base_policy = CapabilityPolicy(filesystem_scope="user_home")
        overridden = base_policy.with_overrides({"memory": {"openclaw_import": False}})
        eng = PermissionEngine(overridden, workspace)
        home = Path.home()
        decision = eng.check(FILESYSTEM_READ, {"path": str(home / "Desktop" / "report.pdf")})
        assert decision.allowed is True  # scope still user_home

    def test_memory_override_disable(self, workspace: Path):
        """Override disables openclaw import."""
        base_policy = CapabilityPolicy(openclaw_import_enabled=True)
        overridden = base_policy.with_overrides({"memory": {"openclaw_import": False}})
        eng = PermissionEngine(overridden, workspace)
        decision = eng.check(MEMORY_OPENCLAW_IMPORT, {"source": "openclaw"})
        assert decision.allowed is False

    def test_permission_request_payload_shape(self, engine: PermissionEngine):
        """Verify the permission_request dict has all required fields for ACP protocol."""
        home = Path.home()
        decision = engine.check(FILESYSTEM_READ, {"path": str(home / "Desktop" / "a.txt")})
        req = decision.permission_request
        assert req is not None
        required_keys = {"type", "scope", "requested_scope", "path", "reason",
                         "temporary_supported", "persistent_supported"}
        assert required_keys.issubset(req.keys())
        assert req["type"] == "permission_request"
        assert req["scope"] == "filesystem"
        assert isinstance(req["temporary_supported"], bool)

    def test_no_escalation_request_is_none(self, engine: PermissionEngine):
        """Implicit system-root paths outside home have no escalation option."""
        decision = engine.check(FILESYSTEM_READ, {"path": "/etc/passwd"})
        assert decision.allowed is False
        assert decision.permission_request is None

    def test_explicit_windows_path_reports_request_to_host(self, engine: PermissionEngine):
        """A user-named drive path reaches officev3 for directory approval."""
        requested = r"Z:\external-project"
        decision = engine.check(FILESYSTEM_READ, {"path": requested})

        assert decision.allowed is False
        assert decision.permission_request is not None
        assert decision.permission_request["scope"] == "filesystem"
        assert decision.permission_request["requested_scope"] == "user_home"
        assert decision.permission_request["path"] == requested

    def test_user_home_explicit_windows_path_reports_request_to_host(
        self, workspace: Path, tmp_path: Path
    ):
        """user_home can still request a path-specific external directory grant."""
        policy = CapabilityPolicy(
            filesystem_scope="user_home",
            session_workspace_root=str(workspace),
        )
        engine = PermissionEngine(policy, workspace)
        # On POSIX, ``Z:\\...`` is parsed as a relative path below the process
        # cwd. Use an isolated home so the test exercises lexical Windows-path
        # escalation instead of accidentally inheriting the CI checkout's home.
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        engine._home_dir = fake_home.resolve()
        requested = r"Z:\external-project"

        decision = engine.check(FILESYSTEM_READ, {"path": requested})

        assert decision.allowed is False
        assert decision.permission_request is not None
        assert decision.permission_request["scope"] == "filesystem"
        assert decision.permission_request["requested_scope"] == "user_home"
        assert decision.permission_request["path"] == requested

    def test_unknown_scope_explicit_windows_path_does_not_escalate(
        self, workspace: Path
    ):
        """Unknown scopes remain fail-closed for explicit external paths."""
        policy = CapabilityPolicy(filesystem_scope="unknown")
        engine = PermissionEngine(policy, workspace)

        decision = engine.check(FILESYSTEM_READ, {"path": r"Z:\external-project"})

        assert decision.allowed is False
        assert decision.permission_request is None


# ── Allowed directories + custom/user_home scopes ────────────


class TestAllowedDirectories:
    """Spec-driven tests for the new scope semantics.

    The four scenarios mirror box-agent-acp section 6 of the user's request:
    session_workspace + allowed_directories, custom + allowed_directories,
    user_home, and path safety (symlinks + sibling false-positive).
    """

    @pytest.fixture
    def downloads(self, tmp_path: Path) -> Path:
        d = tmp_path / "Downloads"
        d.mkdir()
        return d

    @pytest.fixture
    def documents(self, tmp_path: Path) -> Path:
        d = tmp_path / "Documents"
        d.mkdir()
        return d

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        ws = tmp_path / "workspace"
        ws.mkdir()
        return ws

    def _engine(
        self, workspace: Path, scope: str, allowed_dirs: list[str], home: Path | None = None
    ) -> PermissionEngine:
        policy = CapabilityPolicy(
            filesystem_scope=scope,
            allowed_directories=tuple(allowed_dirs),
            session_workspace_root=str(workspace),
        )
        eng = PermissionEngine(policy, workspace)
        if home is not None:
            eng._home_dir = home.resolve()
        # Clear it so deny assertions work correctly in scope-enforcement tests.
        return eng

    # ── 1. session_workspace + allowed_directories ──

    def test_session_workspace_allows_listed_dir(self, workspace: Path, downloads: Path):
        eng = self._engine(workspace, "session_workspace", [str(downloads)])
        f = downloads / "a.pdf"
        f.touch()
        assert eng.check(FILESYSTEM_READ, {"path": str(f)}).allowed is True

    def test_session_workspace_denies_unlisted_dir(
        self, workspace: Path, downloads: Path, documents: Path, tmp_path: Path
    ):
        # Treat tmp_path as the "home" so Documents looks like a home subdir
        # → the engine suggests user_home escalation.
        eng = self._engine(
            workspace, "session_workspace", [str(downloads)], home=tmp_path
        )
        f = documents / "a.pdf"
        f.touch()
        decision = eng.check(FILESYSTEM_READ, {"path": str(f)})
        assert decision.allowed is False
        assert decision.permission_request is not None
        assert decision.permission_request["requested_scope"] == "user_home"
        assert decision.permission_request["persistent_label"] == "始终允许此目录"

    # ── 2. custom + allowed_directories ──

    def test_custom_scope_allows_workspace(self, workspace: Path, downloads: Path):
        eng = self._engine(workspace, "custom", [str(downloads)])
        f = workspace / "report.csv"
        assert eng.check(FILESYSTEM_WRITE, {"path": str(f)}).allowed is True

    def test_custom_scope_allows_listed_dir(self, workspace: Path, downloads: Path):
        eng = self._engine(workspace, "custom", [str(downloads)])
        f = downloads / "a.pdf"
        f.touch()
        assert eng.check(FILESYSTEM_READ, {"path": str(f)}).allowed is True

    def test_custom_scope_denies_other_home_subdir(
        self, workspace: Path, downloads: Path, documents: Path, tmp_path: Path
    ):
        eng = self._engine(workspace, "custom", [str(downloads)], home=tmp_path)
        f = documents / "secret.txt"
        f.touch()
        decision = eng.check(FILESYSTEM_READ, {"path": str(f)})
        assert decision.allowed is False
        # Documents sits under fake "home" (tmp_path) so escalation is offered.
        assert decision.permission_request is not None
        assert decision.permission_request["requested_scope"] == "user_home"

    # ── 3. user_home scope ──

    def test_user_home_allows_desktop(self, workspace: Path):
        eng = self._engine(workspace, "user_home", [])
        f = Path.home() / "Desktop" / "a.txt"
        assert eng.check(FILESYSTEM_READ, {"path": str(f)}).allowed is True

    def test_user_home_allows_documents(self, workspace: Path):
        eng = self._engine(workspace, "user_home", [])
        f = Path.home() / "Documents" / "a.txt"
        assert eng.check(FILESYSTEM_READ, {"path": str(f)}).allowed is True

    def test_user_home_denies_etc(self, workspace: Path):
        eng = self._engine(workspace, "user_home", [])
        decision = eng.check(FILESYSTEM_READ, {"path": "/etc/passwd"})
        assert decision.allowed is False
        assert decision.permission_request is None  # implicit POSIX system paths never escalate

    def test_user_home_allows_additional_directory(
        self, workspace: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        report = external / "report.txt"
        report.write_text("ok")

        eng = self._engine(
            workspace,
            "user_home",
            [str(external)],
            home=home,
        )

        assert eng.check(FILESYSTEM_READ, {"path": str(report)}).allowed is True

    def test_unknown_scope_denies_additional_directory(
        self, workspace: Path, downloads: Path
    ):
        report = downloads / "report.txt"
        report.write_text("private")
        eng = self._engine(workspace, "unknown", [str(downloads)])

        assert eng.check(FILESYSTEM_READ, {"path": str(report)}).allowed is False

    # ── 4. Path safety ──

    def test_symlink_escape_denied(self, workspace: Path, downloads: Path, tmp_path: Path):
        """A symlink inside an allowed dir pointing OUT of allowed scope must
        be denied (resolved real path is what the engine checks)."""
        import os
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("private")

        link = downloads / "escape"
        try:
            os.symlink(str(secret), str(link))
        except (OSError, PermissionError):
            pytest.skip("Cannot create symlink in this environment")

        eng = self._engine(
            workspace, "session_workspace", [str(downloads)], home=tmp_path
        )
        # Reading via the symlink resolves to the real `outside` path, which
        # is not in the allow-list. The engine must deny.
        decision = eng.check(FILESYSTEM_READ, {"path": str(link)})
        assert decision.allowed is False

    def test_sibling_prefix_false_positive_blocked(self, workspace: Path, tmp_path: Path):
        """`/Users/x/Download` must NOT be matched by allowed `/Users/x/Downloads`."""
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        sibling = tmp_path / "Downloads2"
        sibling.mkdir()
        f = sibling / "a.pdf"
        f.touch()

        eng = self._engine(
            workspace, "session_workspace", [str(downloads)], home=tmp_path
        )
        decision = eng.check(FILESYSTEM_READ, {"path": str(f)})
        assert decision.allowed is False

    # ── Tilde expansion of allowed_directories ──

    def test_tilde_in_allowed_directories_expanded(self, workspace: Path):
        """Entries like `~/Downloads` must be expanded before storage."""
        policy = CapabilityPolicy(
            filesystem_scope="session_workspace",
            allowed_directories=("~/Downloads",),
            session_workspace_root=str(workspace),
        )
        eng = PermissionEngine(policy, workspace)
        # The engine should treat the literal string "~/Downloads" as
        # the resolved user-home subdirectory.
        expected = (Path.home() / "Downloads").resolve()
        assert expected in eng._allowed_dirs


# ── GrantStore directory-level grants ────────────────────────


class TestDirectoryGrants:
    """Verify the dir-grant table introduced for spec section 4."""

    def test_dir_grant_allows_files_under_dir(self, tmp_path: Path):
        from box_agent.tools.permissions import GrantStore
        ws = tmp_path / "ws"
        ws.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        f = elsewhere / "a.pdf"
        f.touch()

        store = GrantStore()
        policy = CapabilityPolicy(
            filesystem_scope="session_workspace",
            session_workspace_root=str(ws),
        )
        eng = PermissionEngine(policy, ws, grant_store=store)
        eng._home_dir = tmp_path.resolve()  # so escalation is offered

        # Without grant — denied with escalation.
        assert eng.check(FILESYSTEM_READ, {"path": str(f)}).allowed is False

        # Grant the directory.
        store.add_filesystem_dir_grant(elsewhere, "prompt")

        # Now allowed.
        assert eng.check(FILESYSTEM_READ, {"path": str(f)}).allowed is True

        # Sibling file outside the granted dir is still denied.
        sibling_dir = tmp_path / "other"
        sibling_dir.mkdir()
        sibling = sibling_dir / "x.pdf"
        sibling.touch()
        assert eng.check(FILESYSTEM_READ, {"path": str(sibling)}).allowed is False

    def test_prompt_dir_grant_cleared(self, tmp_path: Path):
        from box_agent.tools.permissions import GrantStore
        ws = tmp_path / "ws"
        ws.mkdir()
        d = tmp_path / "d"
        d.mkdir()

        store = GrantStore()
        store.add_filesystem_dir_grant(d, "prompt")
        assert store.has_filesystem_dir_grant(d)

        store.clear_prompt_grants()
        assert not store.has_filesystem_dir_grant(d)

    def test_session_dir_grant_persists_across_clear(self, tmp_path: Path):
        from box_agent.tools.permissions import GrantStore
        d = tmp_path / "d"
        d.mkdir()

        store = GrantStore()
        store.add_filesystem_dir_grant(d, "session")
        store.clear_prompt_grants()
        assert store.has_filesystem_dir_grant(d)

    def test_dir_grant_does_not_match_sibling_prefix(self, tmp_path: Path):
        """Granting `/x/Downloads` must not allow `/x/Downloads2`."""
        from box_agent.tools.permissions import GrantStore
        a = tmp_path / "Downloads"
        a.mkdir()
        b = tmp_path / "Downloads2"
        b.mkdir()
        f = b / "x.txt"
        f.touch()

        store = GrantStore()
        store.add_filesystem_dir_grant(a, "prompt")
        assert not store.has_filesystem_dir_grant(f.resolve())


# ── ~/.box-agent always-allowed ──────────────────────────────


class TestBoxAgentDirAlwaysAllowed:
    """~/.box-agent is engine-owned data — always allowed regardless of scope."""

    def _engine_with_box_dir(self, workspace: Path, box_dir: Path, scope: str = "session_workspace") -> PermissionEngine:
        policy = CapabilityPolicy(
            filesystem_scope=scope,
            session_workspace_root=str(workspace),
        )
        eng = PermissionEngine(policy, workspace)
        eng._box_agent_dir = box_dir.resolve()
        return eng

    def test_box_agent_skill_dir_allowed_session_workspace(
        self, workspace: Path, tmp_path: Path
    ):
        box_dir = tmp_path / "box-agent"
        skill_file = box_dir / "skills" / "html-ppt" / "references" / "presenter-mode.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.touch()
        eng = self._engine_with_box_dir(workspace, box_dir)
        decision = eng.check(FILESYSTEM_READ, {"path": str(skill_file)})
        assert decision.allowed is True

    def test_box_agent_dir_allowed_user_home_scope(
        self, workspace: Path, tmp_path: Path
    ):
        box_dir = tmp_path / "box-agent"
        f = box_dir / "log" / "session.log"
        f.parent.mkdir(parents=True)
        f.touch()
        eng = self._engine_with_box_dir(workspace, box_dir, scope="user_home")
        # Force home outside tmp_path so .box-agent is the only thing allowing it
        eng._home_dir = (tmp_path / "fake-home").resolve()
        decision = eng.check(FILESYSTEM_READ, {"path": str(f)})
        assert decision.allowed is True

    def test_box_agent_write_allowed(self, workspace: Path, tmp_path: Path):
        box_dir = tmp_path / "box-agent"
        box_dir.mkdir()
        eng = self._engine_with_box_dir(workspace, box_dir)
        target = box_dir / "runtime-packages" / "new.whl"
        decision = eng.check(FILESYSTEM_WRITE, {"path": str(target)})
        assert decision.allowed is True

    def test_sibling_to_box_agent_still_denied(self, workspace: Path, tmp_path: Path):
        """`<home>/.box-agent2` must NOT be matched by `<home>/.box-agent` prefix."""
        box_dir = tmp_path / "box-agent"
        box_dir.mkdir()
        sibling = tmp_path / "box-agent2" / "leaked.txt"
        sibling.parent.mkdir()
        sibling.touch()
        eng = self._engine_with_box_dir(workspace, box_dir)
        # Move home outside tmp_path so the sibling doesn't get user_home grant
        eng._home_dir = (tmp_path / "fake-home").resolve()
        decision = eng.check(FILESYSTEM_READ, {"path": str(sibling)})
        assert decision.allowed is False
