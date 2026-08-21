"""Tests for standalone runtime packaging helpers."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from scripts import build_runtime
from scripts.build_runtime import _relativize_node_manifest


def test_linux_runtime_pins_openai_to_utf8_header_compatible_version() -> None:
    project = (build_runtime.PROJECT_ROOT / "pyproject.toml").read_text()
    dockerfile = (
        build_runtime.PROJECT_ROOT / "docker" / "linux-runtime" / "Dockerfile"
    ).read_text()

    assert '"openai==2.8.0"' in project
    assert 'openai.__version__ == "2.8.0"' in dockerfile


def test_relativize_node_manifest_rewrites_paths_under_node_root(tmp_path: Path) -> None:
    node_root = tmp_path / "box-agent-runtime" / "runtimes" / "node"
    bin_dir = node_root / "versions" / "node-v24-test-darwin-arm64" / "bin"
    bin_dir.mkdir(parents=True)
    manifest = {
        "active": {
            "version": "v24-test",
            "node": str(bin_dir / "node"),
            "npm": str(bin_dir / "npm"),
            "npx": str(bin_dir / "npx"),
            "node_modules": str(node_root / "sandbox" / "node_modules"),
        }
    }
    (node_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    _relativize_node_manifest(node_root)

    active = json.loads((node_root / "manifest.json").read_text(encoding="utf-8"))["active"]
    assert active["node"] == "versions/node-v24-test-darwin-arm64/bin/node"
    assert active["npm"] == "versions/node-v24-test-darwin-arm64/bin/npm"
    assert active["npx"] == "versions/node-v24-test-darwin-arm64/bin/npx"
    assert active["node_modules"] == "sandbox/node_modules"


def test_parse_target_accepts_darwin_x64() -> None:
    assert build_runtime.parse_target("darwin-x64") == ("darwin", "x64")


def test_parse_target_accepts_arch_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_runtime, "detect_platform", lambda: ("darwin", "arm64"))

    assert build_runtime.parse_target("x64") == ("darwin", "x64")


def test_parse_target_rejects_unsupported_target() -> None:
    with pytest.raises(ValueError, match="Unsupported target"):
        build_runtime.parse_target("darwin-ppc")


def test_require_supported_build_process_allows_matching_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_runtime, "detect_platform", lambda: ("darwin", "x64"))

    build_runtime.require_supported_build_process("darwin", "x64")


def test_require_supported_build_process_allows_macos_arch_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_runtime, "detect_platform", lambda: ("darwin", "arm64"))

    build_runtime.require_supported_build_process("darwin", "x64")


def test_require_supported_build_process_rejects_mismatched_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_runtime, "detect_platform", lambda: ("darwin", "arm64"))

    with pytest.raises(RuntimeError, match="matching target platform"):
        build_runtime.require_supported_build_process("linux", "arm64")


def test_pyinstaller_target_arch_args_maps_darwin_x64() -> None:
    assert build_runtime.pyinstaller_target_arch_args(plat="darwin", arch="x64") == [
        "--target-arch",
        "x86_64",
    ]


def test_pyinstaller_target_arch_args_omits_non_macos() -> None:
    assert build_runtime.pyinstaller_target_arch_args(plat="linux", arch="x64") == []


def test_bundled_stable_runtime_components_default_platforms() -> None:
    assert build_runtime.bundled_stable_runtime_components(
        plat="darwin",
        arch="arm64",
    ) == ()


def test_bundled_stable_runtime_components_empty_for_external_python_mode() -> None:
    assert build_runtime.bundled_stable_runtime_components(
        plat="darwin",
        arch="arm64",
        external_python_sandbox=True,
    ) == ()


def test_runtime_manifest_advertises_bundled_web_extract_mcp() -> None:
    manifest = build_runtime.build_runtime_manifest(
        version="0.8.87",
        plat="darwin",
        arch="arm64",
        entry_path="bin/box-agent-acp",
        external_python_sandbox=False,
        bundled_components=(),
    )

    assert manifest["entry"] == "bin/box-agent-acp"
    assert manifest["mcp_servers"] == {
        "box-agent-web-extract": {
            "entry": "bin/box-agent-acp",
            "args": ["--web-extract-mcp"],
            "transport": "stdio",
        }
    }


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_linux_uses_host_stable_runtimes_in_external_sandbox_mode(arch: str) -> None:
    assert build_runtime.bundled_stable_runtime_components(
        plat="linux",
        arch=arch,
        external_python_sandbox=True,
    ) == ()


def test_verify_entry_binary_arch_accepts_linux_x64(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entry = tmp_path / "box-agent-acp"
    entry.write_bytes(b"ELF")
    monkeypatch.setattr(
        build_runtime.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=f"{entry}: ELF 64-bit LSB pie executable, x86-64",
            stderr="",
        ),
    )

    build_runtime.verify_entry_binary_arch(entry, plat="linux", arch="x64")


def test_pyinstaller_args_keep_bundled_sandbox_by_default() -> None:
    hidden = build_runtime.pyinstaller_hidden_imports()
    collect = build_runtime.pyinstaller_collect_args()
    exclude = build_runtime.pyinstaller_exclude_args()
    collect_pairs = list(zip(collect[0::2], collect[1::2]))

    assert "ipykernel" in hidden
    assert "pandas" in hidden
    assert "pip._internal.cli.main" in hidden
    assert "box_agent.mcp_servers.web_extract_server" in hidden
    assert ("--collect-all", "ipykernel") in collect_pairs
    assert ("--collect-submodules", "pandas") in collect_pairs
    assert ("--collect-submodules", "pip") in collect_pairs
    assert exclude == []


def test_pyinstaller_args_drop_sandbox_stack_for_external_python_mode() -> None:
    hidden = build_runtime.pyinstaller_hidden_imports(external_python_sandbox=True)
    collect = build_runtime.pyinstaller_collect_args(external_python_sandbox=True)
    exclude = build_runtime.pyinstaller_exclude_args(external_python_sandbox=True)
    collect_pairs = list(zip(collect[0::2], collect[1::2]))
    exclude_pairs = list(zip(exclude[0::2], exclude[1::2]))

    assert "jupyter_client" in hidden
    assert "box_agent.tools.jupyter_tool" in hidden
    assert "jupyter_core" in hidden
    assert "dateutil" in hidden
    assert "dateutil.parser" in hidden
    assert "box_agent.mcp_servers.web_extract_server" in hidden
    assert "ipykernel" not in hidden
    assert "pandas" not in hidden
    assert "pip._internal.cli.main" not in hidden
    assert ("--collect-all", "jupyter_client") in collect_pairs
    assert ("--collect-all", "jupyter_core") in collect_pairs
    assert ("--collect-all", "ipykernel") not in collect_pairs
    assert ("--collect-submodules", "pandas") not in collect_pairs
    assert ("--collect-submodules", "pip") not in collect_pairs
    assert ("--exclude-module", "ipykernel") in exclude_pairs
    assert ("--exclude-module", "pandas") in exclude_pairs
    assert ("--exclude-module", "pip") in exclude_pairs
    assert ("--exclude-module", "jupyter_client") not in exclude_pairs
    assert ("--exclude-module", "dateutil") not in exclude_pairs


def test_resolve_officev3_dir_accepts_explicit_checkout(tmp_path: Path) -> None:
    officev3_dir = tmp_path / "officev3"
    installer = officev3_dir / "scripts" / "install-box-agent-runtime.js"
    installer.parent.mkdir(parents=True)
    installer.write_text("// test installer\n", encoding="utf-8")

    assert build_runtime.resolve_officev3_dir(str(officev3_dir)) == officev3_dir.resolve()


def test_resolve_officev3_dir_uses_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    officev3_dir = tmp_path / "officev3"
    installer = officev3_dir / "scripts" / "install-box-agent-runtime.js"
    installer.parent.mkdir(parents=True)
    installer.write_text("// test installer\n", encoding="utf-8")
    monkeypatch.setenv("BOX_AGENT_OFFICEV3_DIR", str(officev3_dir))

    assert build_runtime.resolve_officev3_dir() == officev3_dir.resolve()


def test_install_runtime_into_officev3_runs_installer_and_checks_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "box-agent-runtime-v0.8.82-darwin-arm64.tar.gz"
    archive.write_bytes(b"runtime")
    officev3_dir = tmp_path / "officev3"
    installer = officev3_dir / "scripts" / "install-box-agent-runtime.js"
    installer.parent.mkdir(parents=True)
    installer.write_text("// test installer\n", encoding="utf-8")
    installed_dir = officev3_dir / "build-resources" / "box-agent-runtime"
    installed_dir.mkdir(parents=True)
    (installed_dir / "manifest.json").write_text(
        json.dumps({"version": "0.8.82"}),
        encoding="utf-8",
    )
    calls: list[tuple[list[str], str]] = []

    monkeypatch.setattr(build_runtime.shutil, "which", lambda _name: "/usr/bin/node")

    def fake_run(command: list[str], *, cwd: str) -> CompletedProcess[str]:
        calls.append((command, cwd))
        return CompletedProcess(command, 0)

    monkeypatch.setattr(build_runtime.subprocess, "run", fake_run)

    result = build_runtime.install_runtime_into_officev3(
        archive,
        officev3_dir,
        expected_version="0.8.82",
    )

    assert result == installed_dir
    assert calls == [
        (
            ["/usr/bin/node", str(installer), str(archive.resolve())],
            str(officev3_dir),
        )
    ]


def test_install_runtime_into_officev3_rejects_installed_version_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "runtime.tar.gz"
    archive.write_bytes(b"runtime")
    officev3_dir = tmp_path / "officev3"
    installer = officev3_dir / "scripts" / "install-box-agent-runtime.js"
    installer.parent.mkdir(parents=True)
    installer.write_text("// test installer\n", encoding="utf-8")
    installed_dir = officev3_dir / "build-resources" / "box-agent-runtime"
    installed_dir.mkdir(parents=True)
    (installed_dir / "manifest.json").write_text(
        json.dumps({"version": "0.8.81"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(build_runtime.shutil, "which", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(
        build_runtime.subprocess,
        "run",
        lambda command, *, cwd: CompletedProcess(command, 0),
    )

    with pytest.raises(RuntimeError, match="version mismatch"):
        build_runtime.install_runtime_into_officev3(
            archive,
            officev3_dir,
            expected_version="0.8.82",
        )
