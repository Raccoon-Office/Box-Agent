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
        assert decision.permission_request is None  # no escalation beyond user_home

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
        assert "/tmp/my" in result or len(result) > 0  # best-effort

    def test_dev_null_excluded(self):
        result = extract_absolute_paths("command 2>/dev/null")
        assert "/dev/null" not in result

    def test_no_paths(self):
        assert extract_absolute_paths("ls -la") == []

    def test_relative_paths_ignored(self):
        assert extract_absolute_paths("cat ./foo.txt ../bar.txt") == []

    def test_shell_expansion_not_extracted(self):
        """$HOME and ~ are NOT extracted — phase 1 limitation."""
        result = extract_absolute_paths("cat ~/Desktop/file.txt")
        for p in result:
            assert not p.startswith("~")

    def test_home_var_not_extracted(self):
        """$HOME expansion not extracted."""
        result = extract_absolute_paths("cat $HOME/file.txt")
        for p in result:
            assert "$" not in p


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

    def _make_engine(self, workspace: Path) -> PermissionEngine:
        policy = CapabilityPolicy()  # session_workspace scope
        return PermissionEngine(policy, workspace)

    async def test_absolute_path_outside_workspace_denied(self, workspace: Path):
        """Command with absolute path outside workspace is denied."""
        eng = self._make_engine(workspace)
        result = await self._run_bash("cat /etc/passwd", eng)
        assert result.success is False

    async def test_tilde_path_conservatively_denied(self, workspace: Path):
        """Command with cd ~ (no extractable absolute path) is conservatively denied.

        Note: detect_scope_escape only catches 'cd ~', not 'cat ~' — a known
        phase 1 limitation. We test 'cd ~' which IS caught.
        """
        eng = self._make_engine(workspace)
        result = await self._run_bash("cd ~ && ls", eng)
        assert result.success is False
        assert "phase 1" in result.error.lower() or "cannot verify" in result.error.lower()

    async def test_write_command_uses_write_capability(self, workspace: Path):
        """cp/mv-like commands outside workspace are denied using write capability."""
        eng = self._make_engine(workspace)
        result = await self._run_bash("cp /etc/foo /tmp/bar", eng)
        assert result.success is False

    async def test_workspace_command_allowed(self, workspace: Path):
        """Commands referencing workspace paths are NOT blocked by permission engine."""
        eng = self._make_engine(workspace)
        result = await self._run_bash(f"ls {workspace}", eng)
        assert result.permission_request is None


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
        """Paths outside home return None permission_request (no escalation available)."""
        decision = engine.check(FILESYSTEM_READ, {"path": "/etc/passwd"})
        assert decision.allowed is False
        assert decision.permission_request is None
