"""Runtime regeneration preserves provenance and rejects unrelated local edits."""

import hashlib
import json
from pathlib import Path
import runpy

import pytest


@pytest.mark.parametrize("tampered", [False, True])
def test_runtime_refresh_verifies_bundle_before_replacing_owned_input(tmp_path, monkeypatch, tampered):
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/sync_presentation_suite.py"))
    refresh = namespace["refresh_runtime_inputs"]
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "render_runtime_windows.py").write_bytes(b"new reviewed runtime\n")
    monkeypatch.setitem(refresh.__globals__, "RUNTIME_INPUT_DIR", inputs)
    bundle = tmp_path / "bundle"
    relative = "skills/sn-ppt-standard/scripts/render_runtime_windows.py"
    target = bundle / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old runtime\r\n")
    digest = hashlib.sha256(b"old runtime\n").hexdigest()
    provenance = {
        "revision": namespace["PINNED_REVISION"], "overlays": namespace["OVERLAYS"],
        "files": {relative: {"sha256": digest, "source_sha256": digest,
                             "input_path": "scripts/presentation_suite_overlays/render_runtime_windows.py"}},
    }
    marker = bundle / "source.json"
    marker.write_text(json.dumps(provenance), encoding="utf-8")
    before = marker.read_bytes()
    if tampered:
        target.write_bytes(b"unreviewed edit\n")
        with pytest.raises(ValueError, match="differs from its provenance"):
            refresh(bundle)
        assert target.read_bytes() == b"unreviewed edit\n"
        assert marker.read_bytes() == before
    else:
        result = refresh(bundle)
        assert target.read_bytes() == b"new reviewed runtime\n"
        record = result["files"][relative]
        assert record["sha256"] == record["source_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
        saved = marker.read_bytes()
        assert refresh(bundle) == result
        assert marker.read_bytes() == saved


@pytest.mark.parametrize("pid", [None, True, -1, "123", "unrelated"])
def test_windows_registration_rejects_invalid_or_unrelated_identity(tmp_path, monkeypatch, pid):
    import os
    import psutil
    from types import SimpleNamespace

    scripts = Path(__file__).resolve().parents[1] / "scripts/presentation_suite_overlays"
    monkeypatch.syspath_prepend(str(scripts))
    import render_runtime_windows as windows

    supervisor = object.__new__(windows.WindowsSupervisor)
    supervisor.token = "private-test-token"
    supervisor.launcher_identity = psutil.Process(os.getpid())
    supervisor.process = SimpleNamespace(pid=os.getpid())
    connection = object()
    supervisor.peers = {connection: {}}
    supervisor.job = SimpleNamespace(assign=lambda _: pytest.fail("Unrelated PID must not enter the job"))
    monkeypatch.setattr(windows, "_send", lambda *args: pytest.fail("Rejected worker must not receive START"))
    with pytest.raises(RuntimeError, match="invalid render process registration"):
        supervisor._message(connection, {
            "event": "register", "role": "worker", "token": supervisor.token,
            "pid": os.getppid() if pid == "unrelated" else pid,
        })
