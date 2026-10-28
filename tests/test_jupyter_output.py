"""Large Python output stays bounded without losing its recoverable full text."""

from __future__ import annotations

import queue
from pathlib import Path

import pytest

from box_agent.tools.jupyter_output import (
    OUTPUT_PREVIEW_BYTES,
    OUTPUT_REPLY_BYTES,
    OutputCapture,
    bound_output_text,
    output_metadata_fits,
)
from box_agent.tools.jupyter_tool import InProcessKernelSession, JupyterKernelSession


def _saved_path(reply: str) -> Path:
    return Path(reply.split("Full output saved to: ", 1)[1].splitlines()[0])


def test_small_output_remains_verbatim_without_creating_files(tmp_path):
    capture = OutputCapture(tmp_path / "outputs")
    capture.append("hello\n")
    capture.append("中文🙂\n")
    assert capture.finish() == "hello\n中文🙂\n"
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize("single_chunk", [False, True])
def test_large_output_keeps_head_tail_and_complete_utf8_file(tmp_path, single_chunk):
    chunks = ["HEAD\n", *(f"{i}:中文🙂\\x00\n" * 100 for i in range(200)), "TAIL\n"]
    text = "".join(chunks)
    capture = OutputCapture(tmp_path)
    for chunk in [text] if single_chunk else chunks:
        capture.append(chunk)
        assert len(capture._preview) <= OUTPUT_PREVIEW_BYTES
    reply = capture.finish()
    assert len(reply.encode("utf-8")) < OUTPUT_REPLY_BYTES
    assert "HEAD\n" in reply and "TAIL\n" in reply
    assert "middle omitted" in reply
    assert _saved_path(reply).read_bytes() == text.encode("utf-8")
    assert len(list(tmp_path.iterdir())) == 1


def test_persisted_preview_is_not_spilled_again_by_final_text_guard(tmp_path):
    capture = OutputCapture(tmp_path)
    capture.append("x" * 100_000)
    reply = capture.finish()
    assert bound_output_text(reply, tmp_path) == reply
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("failure", ["mkdir", "open", "write", "close"])
def test_output_persistence_failures_keep_draining_and_report_data_loss(
    tmp_path, monkeypatch, failure,
):
    directory = tmp_path / "outputs"
    path_open = Path.open
    path_mkdir = Path.mkdir

    class FailingFile:
        def __init__(self, stream):
            self.stream = stream
            self.writes = 0

        def write(self, data):
            self.writes += 1
            if failure == "write" and self.writes == 3:
                raise OSError("disk full")
            return self.stream.write(data)

        def close(self):
            self.stream.close()
            if failure == "close":
                raise OSError("flush failed")

    def open_file(path, *args, **kwargs):
        if path.parent == directory:
            if failure == "open":
                raise PermissionError("cannot create output")
            return FailingFile(path_open(path, *args, **kwargs))
        return path_open(path, *args, **kwargs)

    def mkdir(path, *args, **kwargs):
        if path == directory and failure == "mkdir":
            raise PermissionError("cannot create output directory")
        return path_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    capture = OutputCapture(directory)
    capture.append("HEAD" + "x" * 100_000)
    capture.append("TAIL")
    reply = capture.finish()
    assert "Full output unavailable" in reply
    assert "Full output saved to:" not in reply
    assert "cannot be recovered" in reply
    assert "HEAD" in reply and "TAIL" in reply
    assert len(reply.encode("utf-8")) < OUTPUT_REPLY_BYTES
    assert capture._stream is None


def test_optional_metadata_rejects_huge_text_cycles_and_non_json_values():
    cycle = {}
    cycle["self"] = cycle
    assert output_metadata_fits({"path": "output.txt", "complete": True, "size": 100})
    for raw in ({"stdout": "x" * OUTPUT_REPLY_BYTES}, cycle, {"x": object()}, {"x": float("nan")}):
        assert not output_metadata_fits(raw)


class _OutputKernelClient:
    def __init__(self, messages):
        self.messages = messages
        self.pending = iter(())

    def execute(self, code, silent=False):
        self.pending = iter([
            *self.messages,
            {"msg_type": "status", "content": {"execution_state": "idle"}},
        ])
        return "msg-1"

    def get_iopub_msg(self, timeout=None):
        try:
            return next(self.pending)
        except StopIteration:
            raise queue.Empty() from None


@pytest.mark.parametrize("session_type", [JupyterKernelSession, InProcessKernelSession])
@pytest.mark.parametrize("channel", ["stdout", "stderr", "error"])
def test_both_kernel_types_bound_large_output_and_preserve_execution_status(
    tmp_path, session_type, channel,
):
    session = (
        session_type("s", tmp_path, object())
        if session_type is JupyterKernelSession
        else session_type("s", tmp_path)
    )
    text = "HEAD\n" + "中文\\x00\n" * 100_000 + "TAIL\n"
    message = (
        {"msg_type": "error", "content": {"ename": "ValueError", "evalue": text}}
        if channel == "error"
        else {"msg_type": "stream", "content": {"name": channel, "text": text}}
    )
    session._kc = _OutputKernelClient([message])
    stdout, images, error = session.execute("print('large output')")
    assert images == []
    reply = error if channel == "error" else stdout
    assert (error is not None) is (channel == "error")
    assert len(reply.encode("utf-8")) < OUTPUT_REPLY_BYTES
    expected = f"ValueError: {text}\n" if channel == "error" else text
    assert _saved_path(reply).read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("session_type", [JupyterKernelSession, InProcessKernelSession])
def test_kernel_keeps_draining_to_idle_when_output_cannot_be_saved(tmp_path, monkeypatch, session_type):
    session = (
        session_type("s", tmp_path, object())
        if session_type is JupyterKernelSession
        else session_type("s", tmp_path)
    )
    session._kc = _OutputKernelClient([
        {"msg_type": "stream", "content": {"name": "stdout", "text": "HEAD" + "x" * 100_000}},
        {"msg_type": "stream", "content": {"name": "stderr", "text": "TAIL"}},
    ])
    path_open = Path.open

    def deny_output(path, *args, **kwargs):
        if path.parent.name == "tool-results":
            raise PermissionError("cannot save output")
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_output)
    stdout, _, error = session.execute("print('large output')")
    assert error is None  # Storage failure must not become a false kernel timeout.
    assert "Full output unavailable" in stdout
    assert "HEAD" in stdout and "TAIL" in stdout


def test_capture_memory_does_not_grow_with_total_stream_size(tmp_path):
    import gc
    import tracemalloc

    capture = OutputCapture(tmp_path)
    chunk = "x" * 8192
    owned_trace = not tracemalloc.is_tracing()
    if owned_trace:
        tracemalloc.start()
    try:
        for _ in range(20):
            capture.append(chunk)
        gc.collect()
        warmed = tracemalloc.get_traced_memory()[0]
        for _ in range(5120):
            capture.append(chunk)
        gc.collect()
        assert tracemalloc.get_traced_memory()[0] - warmed < 128 * 1024
        assert capture.total_bytes > 32 * 1024 * 1024
        assert _saved_path(capture.finish()).stat().st_size == capture.total_bytes
    finally:
        capture.finish()
        if owned_trace:
            tracemalloc.stop()


async def test_real_kernel_large_print_is_recoverable_and_next_call_keeps_state(tmp_path, monkeypatch):
    import sys

    from box_agent.tools.engine.artifact_results import _snapshot_workspace
    from box_agent.tools.file.read_tool import ReadTool
    from box_agent.tools.jupyter_tool import JupyterSandboxTool, SandboxEnvironment

    async def skip_default_package_verification(self, on_progress=None):
        pass

    # Use the test runner's existing interpreter/ipykernel; do not install or
    # prepare a user runtime as part of this source-level integration test.
    monkeypatch.setattr(SandboxEnvironment, "_verify_packages", skip_default_package_verification)
    tool = JupyterSandboxTool(
        workspace_dir=str(tmp_path), runtime_env={"BOX_AGENT_SANDBOX_PYTHON": sys.executable},
        process_owner_id="output-regression",
    )
    try:
        result = await tool.execute(
            "counter = 17\nprint('HEAD\\n' + '中文\\\\x00\\n' * 400000 + 'TAIL')",
            session_id="output-regression", timeout=30,
        )
        assert result.success, result.error
        assert len(result.content.encode("utf-8")) < OUTPUT_REPLY_BYTES
        path = _saved_path(result.content)
        assert path.stat().st_size > 4 * 1024 * 1024
        data = path.read_bytes()
        assert data == ("HEAD\n" + "中文\\x00\n" * 400_000 + "TAIL\n").encode("utf-8")
        page = await ReadTool(str(tmp_path)).execute(str(path), offset=2, limit=1)
        assert page.success and "中文\\x00" in page.content
        assert _snapshot_workspace(str(tmp_path)) == set(), "Internal stdout must not become a generated artifact"
        next_result = await tool.execute("print(counter + 1)", session_id="output-regression", timeout=30)
        assert next_result.success and next_result.content == "18"
        assert len(list(path.parent.iterdir())) == 1
    finally:
        await tool.shutdown_owned_sessions()
