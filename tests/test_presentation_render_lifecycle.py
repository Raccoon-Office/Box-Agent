"""Resource ownership and cancellation contracts of the bundled PPT renderer."""

import atexit
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts"


def load_renderer(monkeypatch):
    registrations = []
    monkeypatch.setattr(atexit, "register", lambda callback, *args: registrations.append(callback))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    namespace = runpy.run_path(str(SCRIPTS / "render.py"))
    return namespace, registrations


def test_importing_renderer_does_not_register_process_termination(monkeypatch):
    _, registrations = load_renderer(monkeypatch)
    assert not registrations, "Import/help must not schedule machine-wide browser cleanup"


def test_batch_cleanup_error_still_stops_driver(tmp_path, monkeypatch):
    namespace, _ = load_renderer(monkeypatch)
    render_batch = namespace["render_batch"]
    state = render_batch.__globals__
    (tmp_path / "slides").mkdir()
    (tmp_path / "slides/slide_01.html").write_text("<html>test</html>")
    events = []

    class Browser:
        def close(self):
            events.append("browser.close")
            raise RuntimeError("injected close failure")

    browser = Browser()
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
    monkeypatch.setitem(state, "_setup_libs", lambda: None)
    manager = SimpleNamespace(start=lambda: playwright, __exit__=lambda *args: events.append("stop"))
    monkeypatch.setitem(state, "_sync_playwright", lambda: lambda: manager)
    monkeypatch.setenv("_PPT_RENDER_DIRECTORY", str(tmp_path))
    monkeypatch.setitem(state, "_ensure_browser_available", lambda _: "/mock/chromium")

    def render(*args, **kwargs):
        raise RuntimeError("primary page failure")

    monkeypatch.setitem(state, "_render_once", render)
    with pytest.raises(RuntimeError):
        render_batch(str(tmp_path))
    assert "stop" in events, events


def test_full_admission_pool_does_not_bypass_configured_limit(tmp_path, monkeypatch):
    import fcntl

    load_renderer(monkeypatch)
    namespace = runpy.run_path(str(SCRIPTS / "render_runtime.py"))
    acquire = namespace["_acquire_render_slot"]
    monkeypatch.setenv("RENDER_GLOBAL_LIMIT", "1")
    monkeypatch.setenv("RENDER_SLOT_TIMEOUT", "5")
    monkeypatch.setenv("RENDER_LOCK_DIR", str(tmp_path))
    with (tmp_path / "slot_0.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TimeoutError):
            acquire(time.monotonic() + 0.1)


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import render_runtime
    return render_runtime


def _fake_renderer(tmp_path, body):
    script = tmp_path / "job with spaces.py"
    script.write_text("def main():\n" + "\n".join("    " + line for line in body.splitlines()) + "\n")
    return script


def _live(pid):
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_supervised_worker_returns_output_and_releases_its_slot(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_GLOBAL_LIMIT", "1")
    monkeypatch.setenv("RENDER_LOCK_DIR", str(tmp_path / "slots"))
    renderer = _fake_renderer(tmp_path, "print('delivered')")
    # This checks output and slot release, not startup latency. Reserve enough
    # budget for worker admission on a busy CI host; timeouts are tested below.
    first = runtime.run_renderer(renderer, [], timeout=20)
    second = runtime.run_renderer(renderer, [], timeout=20)
    assert first.returncode == second.returncode == 0, (first, second)
    assert first.stdout == second.stdout == "delivered\n"


def test_timeout_reaps_registered_detached_browser_and_preserves_unrelated_process(runtime, tmp_path):
    pid_file = tmp_path / "browser.pid"
    child_code = f"import os,time;open({str(pid_file)!r},'w').write(str(os.getpid()));time.sleep(30)"
    renderer = _fake_renderer(tmp_path, f"""import subprocess,sys,render_runtime
guard = subprocess.Popen([sys.executable, render_runtime.__file__, '--browser', sys.executable, '-c', {child_code!r}], start_new_session=True)
guard.wait()""")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    try:
        result = runtime.run_renderer(renderer, [], timeout=2)
        assert result.returncode != 0
        assert "timed out" in result.stderr, result.stderr
        assert pid_file.exists(), result.stderr
        assert not _live(int(pid_file.read_text()))
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_guard_cleans_browser_when_supervisor_is_killed(runtime, tmp_path):
    pid_file = tmp_path / "browser.pid"
    child_code = f"import os,time;open({str(pid_file)!r},'w').write(str(os.getpid()));time.sleep(30)"
    renderer = _fake_renderer(tmp_path, f"""import subprocess,sys,render_runtime
subprocess.Popen([sys.executable, render_runtime.__file__, '--browser', sys.executable, '-c', {child_code!r}], start_new_session=True).wait()""")
    code = f"import sys;sys.path.insert(0,{str(SCRIPTS)!r});from render_runtime import run_renderer;run_renderer({str(renderer)!r},[],timeout=20)"
    parent = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    browser_pid = None
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            assert parent.poll() is None
            time.sleep(0.05)
        assert pid_file.exists()
        browser_pid = int(pid_file.read_text())
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while _live(browser_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _live(browser_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if browser_pid and _live(browser_pid):
            os.kill(browser_pid, signal.SIGKILL)


@pytest.mark.parametrize("fork_on_term", [False, True])
def test_supervisor_reaps_browser_even_when_guard_alone_is_killed(runtime, tmp_path, fork_on_term):
    pid_file = tmp_path / "orphan.pid"
    helper_pid = tmp_path / "shutdown-helper.pid"
    helper_started = tmp_path / "shutdown-started"
    child_code = f"import os,time;open({str(pid_file)!r},'w').write(str(os.getpid()));time.sleep(30)"
    if fork_on_term:
        helper_code = f"import os,time;open({str(helper_pid)!r},'w').write(str(os.getpid()));time.sleep(30)"
        child_code = f'''import os,signal,subprocess,sys,time
def shutdown(*args):
    open({str(helper_started)!r},'w').write('started')
    subprocess.Popen([sys.executable,'-c',{helper_code!r}])
    sys.exit(0)
signal.signal(signal.SIGTERM,shutdown)
open({str(pid_file)!r},'w').write(str(os.getpid()))
time.sleep(30)'''
    renderer = _fake_renderer(tmp_path, f'''import os, signal, subprocess, sys, time, render_runtime
from pathlib import Path
guard = subprocess.Popen([sys.executable, render_runtime.__file__, '--browser', sys.executable, '-c', {child_code!r}], start_new_session=True)
deadline = time.monotonic() + 5
while not Path({str(pid_file)!r}).exists() and time.monotonic() < deadline: time.sleep(.01)
assert Path({str(pid_file)!r}).exists()
os.kill(guard.pid, signal.SIGKILL)
guard.wait()
time.sleep(30)''')
    try:
        result = runtime.run_renderer(renderer, [], timeout=8)
        assert result.returncode == runtime.CLEANUP_EXIT
        assert pid_file.exists()
        assert not _live(int(pid_file.read_text()))
        if helper_started.exists():
            deadline = time.monotonic() + 1
            while not helper_pid.exists() and time.monotonic() < deadline: time.sleep(.01)
        assert not helper_pid.exists() or not _live(int(helper_pid.read_text()))
    finally:
        for path in (pid_file, helper_pid):
            if path.exists() and _live(int(path.read_text())):
                os.kill(int(path.read_text()), signal.SIGKILL)


def test_page_close_error_does_not_allow_next_page(runtime, tmp_path, monkeypatch):
    events = []
    class Context:
        def set_default_timeout(self, value): pass
        def set_default_navigation_timeout(self, value): pass
        def new_page(self): return object()
        def close(self): raise RuntimeError("context close failed")
    browser = SimpleNamespace(new_context=lambda **kwargs: Context(), close=lambda: events.append("browser"))
    session = runtime.RenderSession(None, None, [])
    session.browser = browser
    session.manager = SimpleNamespace(__exit__=lambda *args: events.append("driver"))
    with pytest.raises(runtime.RenderCleanupError):
        try:
            with session.page(100, 100):
                pass
            events.append("next page")
        finally:
            session.__exit__(*sys.exc_info())
    assert events == ["browser", "driver"]


def test_retry_waits_until_previous_worker_exits(runtime, tmp_path):
    marker = tmp_path / "attempt.pid"
    renderer = _fake_renderer(tmp_path, f"""import os
from pathlib import Path
marker = Path({str(marker)!r})
class TargetClosedError(RuntimeError): pass
if not marker.exists():
    marker.write_text(str(os.getpid()))
    raise TargetClosedError('transient browser crash')
try:
    os.kill(int(marker.read_text()), 0)
except ProcessLookupError:
    print('retry after cleanup')
else:
    raise RuntimeError('previous worker still alive')""")
    result = runtime.run_renderer(renderer, [], timeout=15)
    assert result.returncode == 0, result
    assert "retry after cleanup" in result.stdout


@pytest.mark.parametrize("error", ["ValueError('content error')", "RenderCleanupError('cleanup failed')"])
def test_content_or_cleanup_failure_never_retries(runtime, tmp_path, error):
    marker = tmp_path / "attempts"
    renderer = _fake_renderer(tmp_path, f"""from pathlib import Path
from render_runtime import RenderCleanupError
with Path({str(marker)!r}).open('a') as stream: stream.write('attempt\\n')
raise {error}""")
    result = runtime.run_renderer(renderer, [], timeout=5)
    assert result.returncode != 0
    assert marker.read_text() == "attempt\n"


def test_start_failure_cleans_partially_created_manager(runtime):
    events = []
    class Manager:
        def start(self):
            events.append("partial start")
            raise RuntimeError("start failed after driver spawn")
        def __exit__(self, *args):
            events.append("stop")
    with pytest.raises(RuntimeError, match="start failed"):
        with runtime.RenderSession(Manager, None, []):
            raise AssertionError("not started")
    assert events == ["partial start", "stop"]


def test_failure_keeps_diagnostics_written_before_timeout(runtime, tmp_path):
    renderer = _fake_renderer(tmp_path, "import sys,time\nprint('loading slide 7',file=sys.stderr,flush=True)\ntime.sleep(30)")
    result = runtime.run_renderer(renderer, [], timeout=1)
    assert result.returncode != 0
    assert "loading slide 7" in result.stderr
    assert "timed out" in result.stderr


def test_hung_cleanup_respects_callers_total_deadline(runtime, tmp_path):
    renderer = _fake_renderer(tmp_path, "import time\nfrom render_runtime import _phase\n_phase('driver-stop', 5, cleanup=True)\ntime.sleep(30)")
    start = time.monotonic()
    result = runtime.run_renderer(renderer, [], timeout=1)
    assert result.returncode != 0
    assert time.monotonic() - start < 1.5


def test_original_page_error_survives_multiple_cleanup_failures(runtime):
    events = []
    class Context:
        def set_default_timeout(self, value): pass
        def set_default_navigation_timeout(self, value): pass
        def new_page(self): return object()
        def close(self): raise RuntimeError("context close failed")
    def browser_close(): raise RuntimeError("browser close failed")
    session = runtime.RenderSession(None, None, [])
    session.browser = SimpleNamespace(new_context=lambda **kwargs: Context(), close=browser_close)
    session.manager = SimpleNamespace(__exit__=lambda *args: events.append("driver"))
    with pytest.raises(runtime.RenderCleanupError) as caught:
        try:
            with session.page(100, 100):
                raise ValueError("original invalid content")
        finally:
            session.__exit__(*sys.exc_info())
    chain = runtime._error_chain(caught.value)
    assert "original invalid content" in chain
    assert "context close failed" in chain
    assert "browser close failed" in chain
    assert events == ["driver"]


def test_admission_is_held_until_last_process_owner_releases_it(runtime, tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setenv("RENDER_GLOBAL_LIMIT", "1")
    monkeypatch.setenv("RENDER_LOCK_DIR", str(tmp_path))
    fd = runtime._acquire_render_slot()
    guard_fd = os.dup(fd)
    try:
        runtime._release_render_slot(fd)
        with (tmp_path / "slot_0.lock").open("a+") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(guard_fd)


def test_browser_spawn_failure_reports_original_environment_error(runtime, tmp_path):
    executable = tmp_path / "not executable"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o600)
    renderer = _fake_renderer(tmp_path, f"""import subprocess,sys,render_runtime
result = subprocess.run([sys.executable, render_runtime.__file__, '--browser', {str(executable)!r}], start_new_session=True)
raise SystemExit(result.returncode)""")
    result = runtime.run_renderer(renderer, [], timeout=5)
    assert result.returncode != 0
    assert "PermissionError" in result.stderr


def test_launch_flags_are_not_misclassified_as_crashpad_environment_failure(monkeypatch):
    renderer, _ = load_renderer(monkeypatch)
    assert not renderer["_is_fatal_browser_error"]("Browser closed\n<launching> chrome --disable-features=Crashpad\n<process did exit: exitCode=null, signal=SIGKILL>")


@pytest.mark.parametrize("hang", ["context", "browser", "driver"])
def test_primary_error_is_captured_before_cleanup_can_hang(runtime, tmp_path, hang):
    renderer = _fake_renderer(tmp_path, f'''import sys, time
from types import SimpleNamespace
from render_runtime import RenderSession
def close(phase):
    if phase == {hang!r}: time.sleep(30)
context = SimpleNamespace(set_default_timeout=lambda _: None,
    set_default_navigation_timeout=lambda _: None, new_page=lambda: object(),
    close=lambda: close('context'))
session = RenderSession(None, None, [])
session.browser = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: close('browser'))
session.manager = SimpleNamespace(__exit__=lambda *args: close('driver'))
try:
    with session.page(100, 100):
        raise ValueError('original content failure before hung cleanup')
finally:
    session.__exit__(*sys.exc_info())''')
    result = runtime.run_renderer(renderer, [], timeout=2)
    assert result.returncode != 0
    assert "timed out" in result.stderr
    assert "original content failure before hung cleanup" in result.stderr


def test_nested_cleanup_causes_survive_alongside_primary_error(runtime):
    def close():
        try:
            raise OSError("underlying transport failure")
        except OSError as error:
            raise RuntimeError("browser close wrapper") from error
    session = runtime.RenderSession(None, None, [])
    session.browser = SimpleNamespace(close=close)
    original = ValueError("primary page failure")
    with pytest.raises(runtime.RenderCleanupError) as caught:
        session.__exit__(type(original), original, None)
    chain = runtime._error_chain(caught.value)
    assert "underlying transport failure" in chain
    assert "browser close wrapper" in chain
    assert "primary page failure" in chain


def test_disappearing_process_during_cleanup_is_not_a_failure(runtime):
    import psutil
    def status():
        raise psutil.NoSuchProcess(123456789)
    identity = SimpleNamespace(is_running=lambda: True, status=status)
    assert not runtime._live_identity(identity)


def test_player_audit_gives_each_slide_its_own_phase_budget(tmp_path, monkeypatch):
    from contextlib import contextmanager
    renderer, _ = load_renderer(monkeypatch)
    audit = renderer["audit_player"]
    state = audit.__globals__
    clock = {"now": 0, "end": 60}
    class Page:
        def on(self, *args): pass
        def goto(self, *args, **kwargs): pass
        def evaluate(self, *args): pass
        def wait_for_function(self, *args, **kwargs):
            clock["now"] += 9
            assert clock["now"] < clock["end"], "valid later page lost its phase budget"
        def query_selector(self, *args):
            return SimpleNamespace(content_frame=lambda: SimpleNamespace(evaluate=lambda *args: {"rendered": 1}))
    class Session:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def renew_page_deadline(self): clock["end"] = clock["now"] + 60
        @contextmanager
        def page(self, *args, **kwargs): yield Page()
    (tmp_path / "present.html").write_text("<html></html>")
    monkeypatch.setitem(state, "_setup_libs", lambda: None)
    monkeypatch.setitem(state, "_sync_playwright", lambda: None)
    monkeypatch.setitem(state, "RenderSession", Session)
    monkeypatch.setitem(state, "_player_chart_targets", lambda root: [(n, ["chart"], 1) for n in range(1, 8)])
    audit(str(tmp_path))
