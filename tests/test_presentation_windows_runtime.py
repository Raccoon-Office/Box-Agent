"""Windows renderer contracts. Native tests never pass by mocking Win32 APIs."""

import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts"
native_windows = pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows")


def test_renderer_runtime_import_does_not_require_unix_modules_on_windows():
    code = '''import builtins, runpy, sys, tempfile, subprocess, socket, selectors
sys.path.insert(0, sys.argv[1])
sys.platform = 'win32'
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError("No module named 'fcntl'", name='fcntl')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
runpy.run_path(sys.argv[1] + '/render_runtime.py', run_name='__import_probe__')
print('imported')
'''
    result = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import render_runtime
    return render_runtime


def worker(tmp_path, body):
    path = tmp_path / "worker 中文 with spaces.py"
    path.write_text("def main():\n" + "\n".join("    " + line for line in body.splitlines()) + "\n", encoding="utf-8")
    return path


def alive(pid):
    import psutil
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_file(path, parent, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        assert parent.poll() is None, parent.communicate()
        time.sleep(0.05)
    pytest.fail(f"worker did not create {path}")


@native_windows
def test_windows_worker_returns_utf8_and_releases_admission(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_GLOBAL_LIMIT", "1")
    monkeypatch.setenv("RENDER_LOCK_DIR", str(tmp_path / "locks"))
    script = worker(tmp_path, "print('渲染完成')")
    for _ in range(2):
        result = runtime.run_renderer(script, [], timeout=15)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "渲染完成"


@native_windows
def test_windows_timeout_reaps_descendants_and_preserves_unrelated_process(runtime, tmp_path):
    pidfile = tmp_path / "child.pid"
    script = worker(tmp_path, f"""import subprocess,sys,time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'])
Path({str(pidfile)!r}).write_text(str(child.pid))
time.sleep(120)""")
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
    try:
        started = time.monotonic()
        result = runtime.run_renderer(script, [], timeout=8)
        assert result.returncode != 0
        assert "timed out" in result.stderr
        assert time.monotonic() - started < 12
        assert pidfile.exists(), result.stderr
        assert not alive(int(pidfile.read_text()))
        assert other.poll() is None
    finally:
        other.kill()
        other.wait(timeout=5)


@native_windows
def test_windows_hard_supervisor_exit_reaps_orphan_descendant(tmp_path, monkeypatch):
    pidfile = tmp_path / "orphan.pid"
    # Intermediate exits before the supervisor: PID-tree enumeration alone can
    # no longer discover the grandchild, but Job membership must retain it.
    child = f"import subprocess,sys;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);Path({str(pidfile)!r}).write_text(str(p.pid))"
    script = worker(tmp_path, f"import subprocess,sys,time\nsubprocess.run([sys.executable,'-c',{child!r}],check=True)\ntime.sleep(120)")
    code = "import sys;sys.path.insert(0,sys.argv[1]);from render_runtime import run_renderer;run_renderer(sys.argv[2],[],timeout=60)"
    parent = subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS), str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pid = None
    try:
        pid = wait_file(pidfile, parent)
        parent.kill()
        parent.communicate(timeout=5)
        end = time.monotonic() + 5
        while alive(pid) and time.monotonic() < end:
            time.sleep(0.05)
        assert not alive(pid), f"orphan survived supervisor termination: {pid}"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.communicate(timeout=5)
        if pid and alive(pid):
            import psutil
            psutil.Process(pid).kill()


@native_windows
def test_windows_render_reports_do_not_claim_success_if_child_leaks(runtime, tmp_path):
    script = worker(tmp_path, """import subprocess,sys,json
subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])
print(json.dumps({'status':'rendered','qa':'not-run'}))""")
    result = runtime.run_renderer(script, ["--batch"], timeout=15)
    assert result.returncode == runtime.CLEANUP_EXIT, result.stderr
    assert '"status": "rendered"' not in result.stdout


@native_windows
def test_windows_job_setup_failure_prevents_worker_execution(runtime, tmp_path, monkeypatch):
    import render_runtime_windows
    marker = tmp_path / "must-not-run"
    script = worker(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()")
    def denied(*_args, **_kwargs):
        raise OSError("injected AssignProcessToJobObject failure")
    monkeypatch.setattr(render_runtime_windows.WindowsJob, "assign", denied)
    result = runtime.run_renderer(script, [], timeout=10)
    assert result.returncode != 0
    assert "AssignProcessToJobObject" in result.stderr
    assert not marker.exists()


@native_windows
def test_windows_admission_does_not_bypass_full_pool(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_GLOBAL_LIMIT", "1")
    monkeypatch.setenv("RENDER_LOCK_DIR", str(tmp_path / "slots"))
    handle = runtime._acquire_render_slot(time.monotonic() + 1)
    try:
        with pytest.raises(TimeoutError, match="admission timed out"):
            runtime._acquire_render_slot(time.monotonic() + 0.2)
    finally:
        runtime._release_render_slot(handle)
    handle = runtime._acquire_render_slot(time.monotonic() + 1)
    runtime._release_render_slot(handle)


@native_windows
def test_windows_cancel_reaps_children_before_releasing_admission(tmp_path, monkeypatch):
    import signal

    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("RENDER_GLOBAL_LIMIT", "1")
    monkeypatch.setenv("RENDER_LOCK_DIR", str(tmp_path / "slots"))
    script = worker(tmp_path, f"""import subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])
Path({str(pidfile)!r}).write_text(str(p.pid))
time.sleep(120)""")
    code = "import sys;sys.path.insert(0,sys.argv[1]);from render_runtime import run_renderer;r=run_renderer(sys.argv[2],[],timeout=30);print(r.stderr);sys.exit(r.returncode)"
    parent = subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS), str(script)],
                              creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        pid = wait_file(pidfile, parent)
        parent.send_signal(signal.CTRL_BREAK_EVENT)
        output = parent.communicate(timeout=8)
        assert parent.returncode == 130, output
        assert not alive(pid)
        result = subprocess.run([sys.executable, "-c", code, str(SCRIPTS),
                                 str(worker(tmp_path, "print('next admitted')"))],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.communicate(timeout=5)


@native_windows
def test_windows_transient_failure_retries_only_after_worker_exit(runtime, tmp_path):
    marker = tmp_path / "first-worker.pid"
    script = worker(tmp_path, f"""import os,psutil
from pathlib import Path
marker=Path({str(marker)!r})
class TargetClosedError(RuntimeError): pass
if not marker.exists():
    marker.write_text(str(os.getpid()))
    raise TargetClosedError('transient')
assert not psutil.pid_exists(int(marker.read_text()))
print('retry after cleanup')""")
    result = runtime.run_renderer(script, ["--batch"], timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "retry after cleanup"
