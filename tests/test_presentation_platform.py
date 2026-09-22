"""Platform contracts of the shipped presentation scripts.

AST extraction isolates platform behavior from optional image/browser packages;
subprocess checks exercise real file locking and Doctor's renderer invocation.
"""

import ast
import errno
import glob
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import runpy
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
SKILLS = Path(os.environ.get("PRESENTATION_PLATFORM_SKILLS", str(
    REPO / "box_agent/skills/presentation-suite/skills")))
STANDARD = SKILLS / "sn-ppt-standard/scripts"
DAZZLE = SKILLS / "sn-ppt-dazzle/scripts/render_deck.py"
DOCTOR = SKILLS / "sn-ppt-doctor/ppt_doctor/check_environment.py"
LOCK_HELPER = REPO / "scripts/presentation_suite_overlays/file_lock.py"


def functions_from(path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
            and (node.name in names or node.name.startswith("_browser_"))]
    namespace = dict(os=os, sys=sys, Path=Path, re=re, glob=glob, platform=platform,
                     BrowserUnavailable=RuntimeError)
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("script", ["render.py", "image_cutout.py"])
def test_data_lock_imports_work_when_fcntl_is_unavailable(script, monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.syspath_prepend(str(STANDARD))
    tree = ast.parse((STANDARD / script).read_text(encoding="utf-8"))
    imports = [node for node in tree.body if
               (isinstance(node, ast.Import) and any(alias.name == "fcntl" for alias in node.names))
               or (isinstance(node, ast.ImportFrom) and node.module == "file_lock")]
    assert imports, "data lock must use the bundled cross-platform lock helper"
    exec(compile(ast.Module(body=imports, type_ignores=[]), script, "exec"), {})


@pytest.mark.parametrize("function, variable", [("_render_once", "html"), ("audit_player", "present")])
def test_static_navigation_encodes_local_paths(function, variable, tmp_path):
    tree = ast.parse((STANDARD / "render.py").read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function)
    call = next(node for node in ast.walk(node) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "goto")
    local = tmp_path / "演示 文稿 #1.html"
    actual = eval(compile(ast.Expression(call.args[0]), "navigation", "eval"),
                  {variable: str(local), "Path": Path})
    assert actual == local.resolve().as_uri()


def test_import_renderer_preserves_playwright_native_cache_default(monkeypatch, clean_browser_env):
    monkeypatch.syspath_prepend(str(STANDARD))
    monkeypatch.setitem(sys.modules, "render_runtime", SimpleNamespace(
        RenderSession=object, run_renderer=object, supervise=object))
    runpy.run_path(str(STANDARD / "render.py"))
    cache_override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    assert cache_override is None


@pytest.fixture
def clean_browser_env(monkeypatch):
    for key in ("BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
                "PPT_SKILL_BROWSER_EXE", "PLAYWRIGHT_BROWSERS_PATH", "DYNAMIC_PPT_CHROMIUM_EXECUTABLE",
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        monkeypatch.delenv(key, raising=False)


def browser_file(root, relative):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"browser")
    target.chmod(0o755)
    return target


@pytest.mark.parametrize("relative", [
    "chromium-1200/chrome-win64/chrome.exe",
    "chromium-1200/chrome-win/chrome.exe",
    "chromium_headless_shell-1200/chrome-headless-shell-win64/chrome-headless-shell.exe",
    "chromium_headless_shell-1200/chrome-win/headless_shell.exe",
])
def test_standard_discovers_native_windows_cache(relative, tmp_path, monkeypatch, clean_browser_env):
    target = browser_file(tmp_path / "ms-playwright", relative)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    state = functions_from(STANDARD / "render.py", {"_scan_local_chromium", "_prefer_headless_shell"})
    browser = SimpleNamespace(chromium=SimpleNamespace(executable_path=""))
    assert state["_scan_local_chromium"](browser) == str(target)


def test_standard_keeps_host_browser_override_priority(tmp_path, monkeypatch, clean_browser_env):
    host = browser_file(tmp_path, "host.exe")
    other = browser_file(tmp_path, "other.exe")
    monkeypatch.setenv("BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", str(host))
    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", str(other))
    monkeypatch.setenv("PPT_SKILL_BROWSER_EXE", str(other))
    state = functions_from(STANDARD / "render.py", {"_ensure_browser_available"})
    assert state["_ensure_browser_available"](None) == str(host)
    host.unlink()
    with pytest.raises(RuntimeError, match="unavailable"):
        state["_ensure_browser_available"](None)


def test_standard_finds_installer_cache_when_native_expected_browser_is_missing(tmp_path, monkeypatch, clean_browser_env):
    target = browser_file(tmp_path / ".cache/ms-playwright", "chromium-1200/chrome-win64/chrome.exe")
    monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "native"))
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    state = functions_from(STANDARD / "render.py", {"_scan_local_chromium"})
    expected = tmp_path / "native/ms-playwright/chromium-1201/chrome-win64/chrome.exe"
    browser = SimpleNamespace(chromium=SimpleNamespace(executable_path=str(expected)))
    assert state["_scan_local_chromium"](browser) == str(target)


@pytest.mark.parametrize("key", ["BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "PPT_SKILL_BROWSER_EXE"])
def test_dazzle_uses_host_browser_and_rejects_missing_override(key, tmp_path, monkeypatch, clean_browser_env):
    host = browser_file(tmp_path, "host.exe")
    legacy = browser_file(tmp_path, "legacy.exe")
    monkeypatch.setenv(key, str(host))
    monkeypatch.setenv("DYNAMIC_PPT_CHROMIUM_EXECUTABLE", str(legacy))
    picker = functions_from(DAZZLE, {"chromium_executable_path"})["chromium_executable_path"]
    assert picker() == str(host)
    host.unlink()
    with pytest.raises(RuntimeError, match="unavailable"):
        picker()


@pytest.mark.parametrize("relative", [
    "chromium-1200/chrome-win64/chrome.exe",
    "chromium_headless_shell-1200/chrome-win/headless_shell.exe",
    "chromium-1200/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chromium-1200/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
])
def test_dazzle_discovers_configured_platform_cache(relative, tmp_path, monkeypatch, clean_browser_env):
    target = browser_file(tmp_path, relative)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(platform, "system", lambda: "Windows" if ".exe" in relative else "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    picker = functions_from(DAZZLE, {"chromium_executable_path"})["chromium_executable_path"]
    assert picker() == str(target)


def test_dazzle_skips_other_platforms_in_shared_cache(tmp_path, monkeypatch, clean_browser_env):
    target = browser_file(tmp_path, "chromium-1200/chrome-win64/chrome.exe")
    browser_file(tmp_path, "chromium-1200/chrome-linux64/chrome")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    picker = functions_from(DAZZLE, {"chromium_executable_path"})["chromium_executable_path"]
    assert picker() == str(target)


def test_dazzle_preserves_default_playwright_selection(clean_browser_env):
    picker = functions_from(DAZZLE, {"chromium_executable_path"})["chromium_executable_path"]
    assert picker() is None


@pytest.mark.parametrize("exitcode, emit_png", [(0, True), (7, False), (0, False)])
def test_doctor_exercises_renderer_and_propagates_failure(tmp_path, monkeypatch, exitcode, emit_png):
    standard = tmp_path / "standard"
    scripts = standard / "scripts"
    scripts.mkdir(parents=True)
    marker = tmp_path / "renderer-called.json"
    (scripts / "render.py").write_text(
        "import json, pathlib, sys\n"
        "source = pathlib.Path(sys.argv[1])\n"
        "assert source.is_file() and '<html' in source.read_text().lower()\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        + ("pathlib.Path(sys.argv[2]).write_bytes(b'\\x89PNG\\r\\n\\x1a\\nprobe')\n" if emit_png else "")
        + f"print('renderer injected failure', file=sys.stderr)\nsys.exit({exitcode})\n")
    state = runpy.run_path(str(DOCTOR))
    check = state["playwright_chromium_status"]
    monkeypatch.setitem(check.__globals__, "module_available", lambda _: True)
    result = check(standard)
    assert marker.is_file(), result
    args = json.loads(marker.read_text())
    assert not Path(args[0]).exists(), "Doctor temporary inputs must be cleaned"
    assert result["status"] == ("available" if exitcode == 0 and emit_png else "failed"), result
    assert result["launchable"] == (exitcode == 0 and emit_png)
    if exitcode:
        assert "renderer injected failure" in result["detail"]


@pytest.mark.parametrize("layout", ["Scripts/python.exe", "bin/python"])
def test_installer_selects_actual_normalize_interpreter(layout, tmp_path):
    source = (STANDARD / "install.sh").read_text(encoding="utf-8")
    preamble = source.split('TARGETS=("$@")')[0]
    venv = tmp_path / "normalize with spaces"
    interpreter = browser_file(venv, layout)
    script = tmp_path / "probe.sh"
    script.write_text(preamble + '\nprintf "%s\\n" "$PYBIN" "$(normalize_python)"\n',
                      encoding="utf-8", newline="\n")
    env = dict(os.environ, NORMALIZE_VENV=str(venv), BOX_AGENT_PYTHON="/host python.exe", PYBIN="/legacy python")
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    selected = result.stdout.splitlines()[-2:]
    assert selected[0] == "/host python.exe"
    assert Path(selected[1]) == interpreter


def lock_module():
    assert LOCK_HELPER.is_file(), "the cross-platform lock helper must ship with the bundle"
    spec = importlib.util.spec_from_file_location("ppt_test_file_lock", LOCK_HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_file_lock_serializes_real_processes_and_releases(tmp_path):
    locks = lock_module()
    target = tmp_path / "shared.lock"
    code = (
        f"import runpy; m=runpy.run_path({str(LOCK_HELPER)!r}); "
        f"f=open({str(target)!r},'a+b'); m['lock_file'](f,blocking=False); m['unlock_file'](f)"
    )
    with target.open("a+b") as handle:
        locks.lock_file(handle)
        busy = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert busy.returncode != 0 and "BlockingIOError" in busy.stderr
        locks.unlock_file(handle)
    free = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert free.returncode == 0, free.stderr


def test_cutout_catalog_serializes_parallel_writers(tmp_path):
    # Exercise the actual catalog writer without loading unrelated OpenCV code.
    assets = tmp_path / "assets"
    assets.mkdir()
    catalog = assets / "catalog.json"
    catalog.write_text(json.dumps({"assets": [{"path": "assets/source.png", "origin": "upload"}]}))
    code = '''import ast,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from file_lock import lock_file,unlock_file
tree=ast.parse(Path(sys.argv[1],"image_cutout.py").read_text(encoding="utf-8"))
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="_record_derived")
exec(compile(ast.Module(body=[fn],type_ignores=[]),"catalog-writer","exec"))
root=Path(sys.argv[2]); number=sys.argv[3]
(root/(number+".ready")).touch()
_record_derived(root,"assets/cutout-"+number+".png","assets/source.png")
'''
    locks = lock_module()
    processes = []
    try:
        with (assets / ".catalog.lock").open("a+", encoding="utf-8") as handle:
            locks.lock_file(handle)
            for number in (1, 2):
                processes.append(subprocess.Popen([sys.executable, "-c", code, str(STANDARD),
                                                   str(tmp_path), str(number)],
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            deadline = time.monotonic() + 10
            while not all((tmp_path / f"{n}.ready").exists() for n in (1, 2)):
                assert all(p.poll() is None for p in processes)
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert len(json.loads(catalog.read_text())["assets"]) == 1
            locks.unlock_file(handle)
        for process in processes:
            output = process.communicate(timeout=10)
            assert process.returncode == 0, output
        entries = json.loads(catalog.read_text())["assets"]
        assert {e["path"] for e in entries} == {"assets/source.png", "assets/cutout-1.png", "assets/cutout-2.png"}
        assert all(e["source_origin"] == "upload" for e in entries if e["origin"] == "derived")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def fake_windows_lock(monkeypatch, module, callback):
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt", fstat=os.fstat))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=callback))


def test_windows_lock_initializes_and_locks_first_byte(tmp_path, monkeypatch):
    locks = lock_module()
    calls = []
    with (tmp_path / "data.lock").open("a+b") as handle:
        fake_windows_lock(monkeypatch, locks, lambda fd, mode, size: calls.append((handle.tell(), mode, size)))
        locks.lock_file(handle, blocking=False)
        handle.seek(0, 2)
        locks.unlock_file(handle)
    assert (tmp_path / "data.lock").read_bytes() == b"\0"
    assert calls == [(0, 2, 1), (0, 0, 1)]


@pytest.mark.parametrize("blocking", [False, True])
def test_windows_lock_retries_only_contention_with_bounded_wait(tmp_path, monkeypatch, blocking):
    locks = lock_module()
    calls = []
    def busy(*args):
        calls.append(args)
        raise OSError(errno.EACCES, "busy")
    fake_windows_lock(monkeypatch, locks, busy)
    ticks = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(locks, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    with (tmp_path / "data.lock").open("a+b") as handle:
        with pytest.raises(BlockingIOError):
            locks.lock_file(handle, blocking=blocking)
    assert len(calls) == (2 if blocking else 1)


def test_windows_lock_propagates_noncontention_error_without_retry(tmp_path, monkeypatch):
    locks = lock_module()
    calls = []
    def broken(*args):
        calls.append(args)
        raise OSError(errno.EBADF, "invalid handle")
    fake_windows_lock(monkeypatch, locks, broken)
    with (tmp_path / "data.lock").open("a+b") as handle:
        with pytest.raises(OSError) as error:
            locks.lock_file(handle)
    assert error.value.errno == errno.EBADF and len(calls) == 1
