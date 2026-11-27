"""Regression coverage for the Windows-specific runtime builder."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from unittest.mock import Mock

import pytest

from scripts import build_runtime, build_win_runtime


RENDERER = Path("skills/presentation-design/skills/sn-ppt-dsl")


@pytest.fixture(autouse=True)
def resource_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "source"
    config = project / "box_agent/config"
    config.mkdir(parents=True)
    for name in build_runtime._RUNTIME_CONFIG_FILES:
        (config / name).write_text("new resource " + name, encoding="utf-8")
    skills = project / "box_agent/skills"
    skills.mkdir()
    (skills / "_manifest.json").write_text('{"skills": []}', encoding="utf-8")
    (skills / "SKILL.md").write_text("new Skill", encoding="utf-8")
    renderer = project / "box_agent" / RENDERER
    renderer.mkdir(parents=True)
    (renderer / "package-lock.json").write_text("{}", encoding="utf-8")

    def install_test_renderer(runtime_dir: Path, plat: str, arch: str) -> None:
        module = runtime_dir / RENDERER
        assert (module / "package-lock.json").is_file()
        dependencies = module / "node_modules"
        dependencies.mkdir()
        (dependencies / "target.txt").write_text(f"{plat}-{arch}", encoding="utf-8")

    monkeypatch.setattr(build_runtime, "install_ppt_design_dependencies", install_test_renderer)
    monkeypatch.setattr(build_win_runtime, "PROJECT_ROOT", project)
    return project


@pytest.mark.parametrize("has_uv, use_pip_fallback", [(True, False), (False, True)])
def test_windows_installers_use_locked_versions_for_the_runtime_and_freezer(
    monkeypatch, has_uv, use_pip_fallback
):
    installs = []
    bootstraps = []
    monkeypatch.setattr(build_win_runtime.shutil, "which", lambda name: "uv" if has_uv else None)

    def run(command, **kwargs):
        if command[-1] == "uv":
            bootstraps.append(command)
        elif "export" in command:
            assert "--frozen" in command and "--no-emit-project" in command
            assert kwargs["cwd"] == build_win_runtime.PROJECT_ROOT
            Path(command[command.index("--output-file") + 1]).write_text("package==1.0")
        else:
            constraints = Path(command[command.index("--constraint") + 1])
            assert constraints.read_text() == "package==1.0"
            assert f"{build_win_runtime.PROJECT_ROOT}[runtime]" in command and "pyinstaller" in command
            installs.append(constraints)
            if use_pip_fallback and "--python" in command:
                return CompletedProcess(command, 1)
        return CompletedProcess(command, 0)

    monkeypatch.setattr(build_win_runtime.subprocess, "run", run)
    build_win_runtime._install_runtime_extras()
    assert len(bootstraps) == (0 if has_uv else 1)
    assert len(installs) == (2 if use_pip_fallback else 1)
    assert len(set(installs)) == 1


def test_windows_builder_uses_shared_pyinstaller_contract() -> None:
    hidden = build_win_runtime._windows_pyinstaller_hidden_imports()
    collect = build_win_runtime._windows_pyinstaller_collect_args()

    assert hidden == build_runtime.pyinstaller_hidden_imports(
        external_python_sandbox=True
    )
    assert collect == build_runtime.pyinstaller_collect_args(
        external_python_sandbox=True
    )
    assert "box_agent.mcp_servers" in hidden
    assert "box_agent.mcp_servers.web_extract" in hidden
    assert "box_agent.mcp_servers.web_extract_server" in hidden
    assert "PIL.Image" in hidden
    assert "ipykernel" not in hidden
    assert "sklearn" not in collect


@pytest.mark.parametrize("user_install", [False, True])
def test_build_interpreter_can_import_paths_added_by_new_wheels(tmp_path, monkeypatch, user_install):
    system_site = tmp_path / "site-packages"
    user_site = tmp_path / "user-site"
    wheel_site = user_site if user_install else system_site
    library = wheel_site / "win32" / "lib"
    library.mkdir(parents=True)
    (library / "windows_wheel_probe.py").touch()
    (wheel_site / "probe.pth").write_text("win32/lib\n", encoding="utf-8")

    monkeypatch.setattr(build_win_runtime.sys, "path", list(build_win_runtime.sys.path))
    monkeypatch.setattr(build_win_runtime.sysconfig, "get_path", lambda name: str(system_site))
    monkeypatch.setattr(build_win_runtime.site, "ENABLE_USER_SITE", user_install)
    monkeypatch.setattr(build_win_runtime.site, "getusersitepackages", lambda: str(user_site))
    assert importlib.util.find_spec("windows_wheel_probe") is None
    build_win_runtime._load_build_dependencies()
    assert importlib.util.find_spec("windows_wheel_probe").origin == str(library / "windows_wheel_probe.py")


def test_windows_legacy_bundle_remains_explicitly_available() -> None:
    assert build_win_runtime._windows_pyinstaller_hidden_imports(
        external_python_sandbox=False
    ) == build_runtime.pyinstaller_hidden_imports(external_python_sandbox=False)
    assert build_win_runtime._windows_pyinstaller_collect_args(
        external_python_sandbox=False
    ) == build_runtime.pyinstaller_collect_args(external_python_sandbox=False)


def test_windows_pyinstaller_command_includes_web_extract_server(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: list[str] = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        dist_path = Path(command[command.index("--distpath") + 1])
        output = dist_path / "box-agent-acp"
        output.mkdir(parents=True)
        (output / "box-agent-acp.exe").write_bytes(b"exe")
        return CompletedProcess(command, 0)

    monkeypatch.setattr(build_win_runtime.subprocess, "run", fake_run)
    bin_dir = tmp_path / "box-agent-runtime" / "bin"

    build_win_runtime._run_pyinstaller(bin_dir)

    hidden_pairs = list(zip(captured, captured[1:]))
    assert (
        "--hidden-import",
        "box_agent.mcp_servers.web_extract_server",
    ) in hidden_pairs
    assert (bin_dir / "box-agent-acp.exe").is_file()
    assert ("--exclude-module", "ipykernel") in hidden_pairs
    assert ("--exclude-module", "sklearn") in hidden_pairs
    assert ("--hidden-import", "PIL.Image") in hidden_pairs
    assert "--add-data" not in captured
    assert ("--collect-data", "box_agent.mcp_servers") in hidden_pairs


def test_windows_manifest_advertises_bundled_web_extract_mcp(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "box-agent-runtime"
    runtime_dir.mkdir()

    build_win_runtime._write_manifest(runtime_dir, "0.9.7")

    manifest = json.loads(
        (runtime_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["platform"] == "win32"
    assert manifest["arch"] == "x64"
    assert manifest["entry"] == "bin/box-agent-acp.exe"
    assert manifest["managed_mcp_config_version"] == 1
    assert manifest["connector_skill_sources_version"] == 1
    assert manifest["mcp_multi_source_version"] == 1
    assert "connector_mcp_proxy_version" not in manifest
    assert manifest["external_python_sandbox"] is True
    assert manifest["bundled_stable_runtimes"] == []
    assert manifest["mcp_servers"] == {
        "box-agent-web-extract": {
            "entry": "bin/box-agent-acp.exe",
            "args": ["--web-extract-mcp"],
            "transport": "stdio",
        }
    }
    assert (runtime_dir / "VERSION").read_text(encoding="utf-8") == "0.9.7\n"


def test_windows_legacy_manifest_lists_its_bundled_tools(tmp_path: Path) -> None:
    build_win_runtime._write_manifest(
        tmp_path, "0.9.7", external_python_sandbox=False
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["external_python_sandbox"] is False
    assert manifest["bundled_stable_runtimes"] == ["portable_git", "python", "node"]


@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize("exe_only", [False, True])
def test_windows_build_entry_preserves_selected_profile_and_existing_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundled: bool, exe_only: bool
) -> None:
    runtime_dir = tmp_path / "output" / "box-agent-runtime"
    check_pdf = Mock()
    monkeypatch.setattr(build_win_runtime, "_check_bundled_pdf_lookup", check_pdf)
    python_exe = runtime_dir / "runtime" / "python" / "python.exe"
    old_node = runtime_dir / "runtimes" / "node" / "existing.txt"
    if exe_only:
        runtime_dir.mkdir(parents=True)
        build_win_runtime._write_manifest(
            runtime_dir, "0.9.6", external_python_sandbox=not bundled
        )
        if bundled:
            python_exe.parent.mkdir(parents=True)
            python_exe.write_bytes(b"existing python")
            old_node.parent.mkdir(parents=True)
            old_node.write_bytes(b"existing node")

    def install_python(_runtime_dir: Path) -> None:
        python_exe.parent.mkdir(parents=True)
        python_exe.write_bytes(b"bundled python")

    def fake_build(bin_dir, **kwargs):
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "box-agent-acp.exe").write_bytes(b"rebuilt exe")

    build = Mock(side_effect=fake_build)
    installers = {
        "_install_portable_git_win": Mock(),
        "_install_portable_python_win": Mock(side_effect=install_python),
        "_install_sandbox_packages_win": Mock(),
        "_install_node_win": Mock(),
    }
    monkeypatch.setattr(build_win_runtime, "_ensure_win", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_install_runtime_extras", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_load_build_dependencies", lambda: None)
    monkeypatch.setattr(build_win_runtime, "BUILD_TOOLS_CACHE", tmp_path / "cache", raising=False)
    monkeypatch.setattr(build_win_runtime, "_run_pyinstaller", build)
    for name, installer in installers.items():
        monkeypatch.setattr(build_win_runtime, name, installer, raising=False)
    args = [
        "build_win_runtime.py", "--version", "0.9.7",
        "--output", str(runtime_dir.parent), "--no-tar",
    ]
    if bundled:
        args.append("--bundled-python-sandbox")
    if exe_only:
        args.append("--exe-only")
    monkeypatch.setattr(build_win_runtime.sys, "argv", args)

    build_win_runtime.main()

    build.assert_called_once()
    called_bin = build.call_args.args[0]
    assert called_bin.name == "bin"
    if not exe_only:
        assert called_bin == runtime_dir / "bin"
    assert build.call_args.kwargs == {"external_python_sandbox": not bundled}
    assert (runtime_dir / RENDERER / "node_modules/target.txt").read_text() == "win32-x64"
    assert (runtime_dir / "skills/SKILL.md").read_text() == "new Skill"
    assert (runtime_dir / "config/config-example.yaml").read_text() == "new resource config-example.yaml"
    manifest = json.loads((runtime_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["external_python_sandbox"] is not bundled
    expected_components = ["portable_git", "python", "node"] if bundled else []
    assert manifest["bundled_stable_runtimes"] == expected_components
    for name, installer in installers.items():
        if bundled and not exe_only:
            target = python_exe if name == "_install_sandbox_packages_win" else runtime_dir
            installer.assert_called_once_with(target)
        else:
            installer.assert_not_called()
    if exe_only and bundled:
        check_pdf.assert_called_once_with(python_exe)
        assert python_exe.read_bytes() == b"existing python"
        assert old_node.read_bytes() == b"existing node"
    elif not bundled:
        assert not (runtime_dir / "runtime").exists()
        assert not (runtime_dir / "runtimes").exists()


@pytest.mark.parametrize("existing_external", [True, False])
@pytest.mark.parametrize("mismatch_at", ["output", "install-to"])
def test_exe_only_rejects_cross_profile_rebuild_without_changing_artifacts(
    tmp_path, monkeypatch, capsys, existing_external, mismatch_at
):
    output_dir = tmp_path / "output"
    runtime_dir = output_dir / "box-agent-runtime"
    target_dir = tmp_path / "installed-runtime"
    requested_external = not existing_external
    roots = [runtime_dir] if mismatch_at == "output" else [runtime_dir, target_dir]
    before = {}
    for root in roots:
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "box-agent-acp.exe").write_bytes(b"original exe")
        profile = requested_external if mismatch_at == "install-to" and root == runtime_dir else existing_external
        build_win_runtime._write_manifest(root, "0.9.6", external_python_sandbox=profile)
        if not profile:
            for name in ("runtime/PortableGit/usr/bin/bash.exe", "runtime/python/python.exe", "runtimes/node/manifest.json"):
                component = root / name
                component.parent.mkdir(parents=True, exist_ok=True)
                component.write_bytes(b"existing component")
        before[root] = {file.relative_to(root): file.read_bytes() for file in root.rglob("*") if file.is_file()}

    install_extras = Mock()

    def fake_build(bin_dir, **kwargs):
        bin_dir.mkdir(parents=True)
        (bin_dir / "box-agent-acp.exe").write_bytes(b"rebuilt exe")

    build = Mock(side_effect=fake_build)
    monkeypatch.setattr(build_win_runtime, "_ensure_win", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_install_runtime_extras", install_extras)
    monkeypatch.setattr(build_win_runtime, "_load_build_dependencies", lambda: None)
    monkeypatch.setattr(build_win_runtime, "BUILD_TOOLS_CACHE", tmp_path / "cache", raising=False)
    monkeypatch.setattr(build_win_runtime, "_run_pyinstaller", build)
    args = ["build_win_runtime.py", "--exe-only", "--no-tar", "--output", str(output_dir)]
    if not requested_external:
        args.append("--bundled-python-sandbox")
    if mismatch_at == "install-to":
        args.extend(["--install-to", str(target_dir)])
    monkeypatch.setattr(build_win_runtime.sys, "argv", args)

    with pytest.raises(SystemExit) as exc:
        build_win_runtime.main()

    assert exc.value.code == 2
    assert "without --exe-only" in capsys.readouterr().err
    install_extras.assert_not_called()
    build.assert_not_called()
    for root in roots:
        assert {file.relative_to(root): file.read_bytes() for file in root.rglob("*") if file.is_file()} == before[root]


@pytest.mark.parametrize("raw_manifest", [None, "{invalid", "[]", "{}", '{"external_python_sandbox": "false"}'])
def test_exe_only_rejects_unknown_profiles_before_modifying_the_runtime(
    tmp_path, monkeypatch, capsys, raw_manifest
):
    runtime_dir = tmp_path / "box-agent-runtime"
    (runtime_dir / "bin").mkdir(parents=True)
    executable = runtime_dir / "bin" / "box-agent-acp.exe"
    executable.write_bytes(b"original exe")
    manifest_path = runtime_dir / "manifest.json"
    if raw_manifest is not None:
        manifest_path.write_text(raw_manifest, encoding="utf-8")
    install_extras = Mock()
    monkeypatch.setattr(build_win_runtime, "_ensure_win", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_install_runtime_extras", install_extras)
    monkeypatch.setattr(build_win_runtime.sys, "argv", [
        "build_win_runtime.py", "--exe-only", "--output", str(tmp_path),
    ])

    with pytest.raises(SystemExit) as exc:
        build_win_runtime.main()

    assert exc.value.code == 2
    assert "without --exe-only" in capsys.readouterr().err
    install_extras.assert_not_called()
    assert executable.read_bytes() == b"original exe"
    if raw_manifest is not None:
        assert manifest_path.read_text(encoding="utf-8") == raw_manifest
    else:
        assert not manifest_path.exists()


@pytest.mark.parametrize("external", [True, False])
def test_exe_only_install_preserves_tools_in_a_matching_target(tmp_path, monkeypatch, external):
    runtime_dir = tmp_path / "output" / "box-agent-runtime"
    target_dir = tmp_path / "installed-runtime"
    check_pdf = Mock()
    monkeypatch.setattr(build_win_runtime, "_check_bundled_pdf_lookup", check_pdf)
    for root in (runtime_dir, target_dir):
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "box-agent-acp.exe").write_bytes(b"original exe")
        build_win_runtime._write_manifest(root, "0.9.6", external_python_sandbox=external)
        if not external:
            component = root / "runtime" / "PortableGit" / "usr" / "bin" / "bash.exe"
            component.parent.mkdir(parents=True)
            component.write_bytes(b"existing bash")

    def fake_build(bin_dir, **kwargs):
        bin_dir.mkdir(parents=True)
        (bin_dir / "box-agent-acp.exe").write_bytes(b"rebuilt exe")

    monkeypatch.setattr(build_win_runtime, "_ensure_win", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_install_runtime_extras", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_load_build_dependencies", lambda: None)
    monkeypatch.setattr(build_win_runtime, "BUILD_TOOLS_CACHE", tmp_path / "cache", raising=False)
    monkeypatch.setattr(build_win_runtime, "_run_pyinstaller", fake_build)
    args = [
        "build_win_runtime.py", "--exe-only", "--no-tar", "--version", "0.9.7",
        "--output", str(runtime_dir.parent), "--install-to", str(target_dir),
    ]
    if not external:
        args.append("--bundled-python-sandbox")
    monkeypatch.setattr(build_win_runtime.sys, "argv", args)

    build_win_runtime.main()

    assert (target_dir / "bin" / "box-agent-acp.exe").read_bytes() == b"rebuilt exe"
    assert (target_dir / RENDERER / "node_modules/target.txt").read_text() == "win32-x64"
    assert (target_dir / "skills/SKILL.md").read_text() == "new Skill"
    assert (target_dir / "config/config-example.yaml").read_text() == "new resource config-example.yaml"
    manifest = json.loads((target_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.9.7"
    assert manifest["external_python_sandbox"] is external
    if external:
        check_pdf.assert_not_called()
        assert not (target_dir / "runtime").exists()
        assert not (target_dir / "runtimes").exists()
    else:
        assert [call.args[0] for call in check_pdf.call_args_list] == [
            root / "runtime/python/python.exe" for root in (runtime_dir, target_dir)
        ]
        assert (target_dir / "runtime" / "PortableGit" / "usr" / "bin" / "bash.exe").read_bytes() == b"existing bash"


def test_exe_only_rejects_standalone_python_without_pdf_lookup_before_build(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "box-agent-runtime"
    runtime_dir.mkdir()
    build_win_runtime._write_manifest(runtime_dir, "old", external_python_sandbox=False)
    manifest = (runtime_dir / "manifest.json").read_bytes()
    monkeypatch.setattr(build_win_runtime, "_ensure_win", lambda: None)
    monkeypatch.setattr(build_win_runtime.sys, "argv", [
        "build_win_runtime.py", "--exe-only", "--bundled-python-sandbox", "--output", str(tmp_path),
    ])
    build = Mock()
    monkeypatch.setattr(build_win_runtime, "_install_runtime_extras", build)
    with pytest.raises(SystemExit) as error:
        build_win_runtime.main()
    assert error.value.code == 2
    assert (runtime_dir / "manifest.json").read_bytes() == manifest
    build.assert_not_called()


def test_exe_only_failed_renderer_install_keeps_both_existing_copies(tmp_path, monkeypatch):
    output = tmp_path / "output"
    installed = tmp_path / "installed"
    existing = (output / "box-agent-runtime", installed)
    for root in existing:
        root.mkdir(parents=True)
        build_win_runtime._write_manifest(root, "previous")
        dependencies = root / RENDERER / "node_modules"
        dependencies.mkdir(parents=True)
        (dependencies / "target.txt").write_text("previous renderer")

    def fail_install(*args):
        raise CalledProcessError(1, "npm ci")

    monkeypatch.setattr(build_win_runtime, "_ensure_win", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_install_runtime_extras", lambda: None)
    monkeypatch.setattr(build_win_runtime, "_load_build_dependencies", lambda: None)
    monkeypatch.setattr(build_win_runtime, "BUILD_TOOLS_CACHE", tmp_path / "cache", raising=False)
    monkeypatch.setattr(build_win_runtime, "_run_pyinstaller", Mock())
    monkeypatch.setattr(build_runtime, "install_ppt_design_dependencies", fail_install)
    monkeypatch.setattr(build_win_runtime.sys, "argv", [
        "build_win_runtime.py", "--exe-only", "--output", str(output), "--install-to", str(installed),
    ])
    with pytest.raises(CalledProcessError):
        build_win_runtime.main()

    for root in existing:
        assert (root / "VERSION").read_text().strip() == "previous"
        assert (root / RENDERER / "node_modules/target.txt").read_text() == "previous renderer"
    assert not list(output.glob("*.tar.gz"))
