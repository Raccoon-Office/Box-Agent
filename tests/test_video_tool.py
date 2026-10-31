"""Video reading: real media decoding plus deterministic provider/permission tests."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.schema import LLMResponse
from box_agent.tools.base import ToolInvocationContext, bind_tool_cancellation
from box_agent.tools.setup import add_workspace_tools
from box_agent.tools import video_tool as module
from box_agent.tools.video_tool import ReadVideoTool, VideoReadError, _executable, _run_process


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")


class Vision:
    capabilities = {"image_input": True}

    def __init__(self):
        self.calls = []

    async def generate(self, messages, tools=None, **kwargs):
        self.calls.append((messages, kwargs))
        return LLMResponse(content="At 1.000s a red frame appears.", finish_reason="stop")


@pytest.fixture
def media(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video input")
    calls = []
    metadata = {"format": {"duration": "12"}, "streams": [
        {"codec_type": "video", "index": 0, "width": 160, "height": 90},
        {"codec_type": "audio", "index": 1},
    ]}

    async def run(args):
        calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps(metadata).encode(), b""
        destination = Path(args[-1])
        if destination.suffix == ".pcm":
            seconds = float(args[args.index("-t") + 1])
            destination.write_bytes(b"\0\0" * int(seconds * 16000))
            return b"", b""
        destination.write_bytes(PNG)
        target = float(args[args.index("-ss") + 1])
        return b"", f"[showinfo] n: 0 pts: 123 pts_time:{target} duration:1".encode()

    monkeypatch.setattr(module, "_run_process", run)
    monkeypatch.setattr(module, "_executable", lambda name, env: name)
    return SimpleNamespace(source=source, calls=calls, metadata=metadata)


def make_tool(tmp_path, llm=None, **kwargs):
    return ReadVideoTool(llm=llm or Vision(), workspace_dir=str(tmp_path),
                         allow_full_access=False, runtime_env={}, **kwargs)


async def test_video_uses_native_images_with_actual_timestamps_and_explicit_audio_status(tmp_path, media):
    llm = Vision()
    tool = make_tool(tmp_path, llm)
    result = await tool.invoke({"path": "clip.mp4", "instruction": "Describe the video"})
    assert result.success, result.error
    assert llm.calls == []
    assert len([b for b in result.image_content if b["type"] == "input_image"]) == 6
    assert result.raw_output["frames"] == [{"timestamp_seconds": t} for t in (1, 3, 5, 7, 9, 11)]
    assert result.raw_output["visual"]["status"] == "native_frames"
    assert result.raw_output["audio"]["status"] == "unavailable"
    assert "do not" in str(result.image_content).lower()
    assert media.source.read_bytes() == b"video input"
    assert set(tmp_path.iterdir()) == {media.source}


async def test_video_frames_reach_next_model_request_and_survive_session_restore(tmp_path, media):
    from box_agent.agent import Agent
    from box_agent.events import ErrorEvent
    from box_agent.schema import FunctionCall, StreamEvent, ToolCall
    from box_agent.session_log import SessionLog

    class RecordingVision(Vision):
        model = "test-vision"
        max_output_tokens = 1024

        def __init__(self, request_video=False):
            super().__init__()
            self.requests = []
            self.request_video = request_video

        async def generate_stream(self, *, messages, **kwargs):
            self.requests.append(deepcopy(messages))
            if self.request_video and len(self.requests) == 1:
                call = ToolCall(id="video-call", type="function", function=FunctionCall(
                    name="read_video", arguments={"path": "clip.mp4", "instruction": "Describe",
                                                  "frame_count": 2, "transcribe": False}))
                yield StreamEvent(type="finish", finish_reason="tool_calls", tool_calls=[call])
            else:
                yield StreamEvent(type="text", delta="Video reviewed.")
                yield StreamEvent(type="finish", finish_reason="stop")

    def images(request):
        return [block for message in request if isinstance(message.content, list)
                for block in message.content if block.get("type") == "input_image"]

    def agent(llm, log, tools):
        return Agent(llm_client=llm, system_prompt="Read videos", tools=tools,
                     workspace_dir=str(tmp_path), session_log=log, enable_builtin_tools=False,
                     deferred_mcp_loading_enabled=False, max_steps=4)

    log = SessionLog.create(tmp_path / "sessions", session_id="video", cwd=tmp_path)
    llm = RecordingVision(request_video=True)
    reader = agent(llm, log, [make_tool(tmp_path, llm)])
    reader.add_user_message("Review clip.mp4")
    events = [event async for event in reader.run_events()]
    assert not [event for event in events if isinstance(event, ErrorEvent) and event.is_fatal]
    assert [len(images(request)) for request in llm.requests] == [0, 2]
    refs = next(e["data"]["images"] for e in log.events if e["type"] == "tool/result")
    assert len(refs) == 2
    assert all(ref["source"] == "image_inspect" and ref["toolCallId"] == "video-call" for ref in refs)
    assert all(block["data"] not in log.path.read_text() for block in images(llm.requests[-1]))
    log.close()

    restored_log = SessionLog.open(tmp_path / "sessions", session_id="video", cwd=tmp_path)
    try:
        restored_llm = RecordingVision()
        restored = agent(restored_llm, restored_log, [])
        restored.add_user_message("What else is visible?")
        _ = [event async for event in restored.run_events()]
        assert len(images(restored_llm.requests[0])) == 2
    finally:
        restored_log.close()


async def test_segment_reads_only_requested_range_and_transcription_uses_desktop_env(tmp_path, media, monkeypatch):
    transcriptions = []

    async def transcribe(path, *, config, start_seconds, duration, on_progress=None):
        transcriptions.append((path, config, start_seconds, duration))
        return {"status": "transcribed", "text": "测试语音", "segments": [
            {"start_seconds": start_seconds + 0.2, "end_seconds": start_seconds + 0.8, "text": "测试语音"}]}

    monkeypatch.setattr(module, "transcribe_pcm", transcribe)
    tool = make_tool(tmp_path)
    tool._runtime_env = {"VOLC_ASR_API_KEY": "private-test-key"}
    result = await tool.invoke({"path": "clip.mp4", "instruction": "What happens?",
                                "start_seconds": 4, "end_seconds": 8, "frame_count": 2,
                                "strategy": "proxy"})
    assert result.success
    assert result.raw_output["frames"] == [{"timestamp_seconds": 5}, {"timestamp_seconds": 7}]
    assert result.raw_output["audio"]["segments"][0]["start_seconds"] == 4.2
    assert transcriptions[0][2:] == (4, 4)
    assert transcriptions[0][1].api_key == "private-test-key"
    pcm_call = next(args for args in media.calls if args[-1].endswith("speech.pcm"))
    assert pcm_call[pcm_call.index("-ss") + 1] == "4"
    assert pcm_call[pcm_call.index("-t") + 1] == "4"
    assert "aresample=async=1:first_pts=0,apad" in pcm_call
    assert "private-test-key" not in result.model_dump_json()
    assert tool.llm.calls[0][1]["call_kind"] == "utility"


async def test_text_main_model_uses_vision_proxy_without_claiming_native_pixels(tmp_path, media):
    main = SimpleNamespace(capabilities={"image_input": False})
    proxy = Vision()
    tool = make_tool(tmp_path, main, proxy_llm_resolver=lambda: proxy)
    result = await tool.invoke({"path": "clip.mp4", "instruction": "Describe", "transcribe": False})
    assert result.success
    assert proxy.calls
    assert result.raw_output["visual"]["status"] == "analyzed"
    assert result.image_context_eligible is False


async def test_cached_media_rechecks_source_and_permissions_and_reuses_successful_transcript(tmp_path, media, monkeypatch):
    count = 0

    async def transcribe(*args, **kwargs):
        nonlocal count
        count += 1
        return {"status": "transcribed", "text": "hello", "segments": []}

    monkeypatch.setattr(module, "transcribe_pcm", transcribe)
    tool = make_tool(tmp_path)
    tool._runtime_env = {"VOLC_ASR_API_KEY": "test"}
    args = {"path": "clip.mp4", "instruction": "Read"}
    await tool.invoke(args)
    before = len(media.calls)
    result = await tool.invoke({**args, "instruction": "What color?"})
    assert result.raw_output["media_cache_hit"] is True
    assert len(media.calls) == before + 1  # metadata probe only
    assert count == 1
    media.source.write_bytes(b"changed video")
    result = await tool.invoke(args)
    assert not result.raw_output["media_cache_hit"]
    assert count == 2


@pytest.mark.parametrize("status", ["no_audio", "disabled"])
async def test_silent_or_disabled_audio_never_calls_asr(tmp_path, media, monkeypatch, status):
    async def unexpected(*args, **kwargs):
        pytest.fail("ASR must not run")

    monkeypatch.setattr(module, "transcribe_pcm", unexpected)
    if status == "no_audio":
        media.metadata["streams"] = media.metadata["streams"][:1]
    result = await make_tool(tmp_path).invoke({"path": "clip.mp4", "instruction": "Read",
                                              "transcribe": status != "disabled"})
    assert result.success
    assert result.raw_output["audio"]["status"] == status
    assert not any(args[-1].endswith(".pcm") for args in media.calls)


async def test_asr_failure_is_partial_and_retryable_without_reextracting_frames(tmp_path, media, monkeypatch):
    async def fail(*args, **kwargs):
        raise module.TranscriptionError("Volc ASR timed out")

    monkeypatch.setattr(module, "transcribe_pcm", fail)
    tool = make_tool(tmp_path)
    tool._runtime_env = {"VOLC_ASR_API_KEY": "test"}
    args = {"path": "clip.mp4", "instruction": "Read"}
    result = await tool.invoke(args)
    assert result.success and result.raw_output["audio"]["status"] == "failed"

    async def succeed(*args, **kwargs):
        return {"status": "transcribed", "text": "Recovered"}

    monkeypatch.setattr(module, "transcribe_pcm", succeed)
    result = await tool.invoke(args)
    assert result.raw_output["media_cache_hit"]
    assert result.raw_output["audio"]["text"] == "Recovered"


async def test_long_video_returns_explicit_continuation_without_claiming_full_coverage(tmp_path, media):
    media.metadata["format"]["duration"] = "1400"
    tool = make_tool(tmp_path)
    first = await tool.invoke({"path": "clip.mp4", "instruction": "Read"})
    assert first.raw_output["end_seconds"] == 600
    assert first.raw_output["has_more"] and first.raw_output["next_start_seconds"] == 600
    last = await tool.invoke({"path": "clip.mp4", "instruction": "Read", "start_seconds": 1200})
    assert last.raw_output["end_seconds"] == 1400
    assert not last.raw_output["has_more"]
    assert last.raw_output["next_start_seconds"] is None


@pytest.mark.parametrize("args", [
    {"start_seconds": -1}, {"start_seconds": 12}, {"start_seconds": float("nan")},
    {"end_seconds": float("inf")}, {"start_seconds": 4, "end_seconds": 3},
    {"frame_count": 7}, {"frame_count": 0}, {"instruction": " "},
])
async def test_invalid_ranges_and_frame_budgets_are_rejected(tmp_path, media, args):
    result = await make_tool(tmp_path).invoke({"path": "clip.mp4", "instruction": "Read", **args})
    assert not result.success
    assert not any(call[0] == "ffmpeg" for call in media.calls)


async def test_outside_path_and_symlink_are_rejected_before_decoding(tmp_path, media):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "linked.mp4").symlink_to(media.source)
    tool = make_tool(workspace)
    for path in (str(media.source), "linked.mp4"):
        result = await tool.invoke({"path": path, "instruction": "Read"})
        assert not result.success
    assert media.calls == []


async def test_permission_request_uses_public_tool_name_and_runs_again_for_cache(tmp_path, media):
    granted = True
    checks = []

    class Permissions:
        def check(self, **kwargs):
            checks.append(kwargs)
            return SimpleNamespace(allowed=granted, reason="approval required", permission_request={"path": str(media.source)})

    tool = make_tool(tmp_path, permission_engine=Permissions())
    args = {"path": "clip.mp4", "instruction": "Read"}
    assert (await tool.invoke(args)).success
    granted = False
    before = len(media.calls)
    result = await tool.invoke(args)
    assert not result.success and result.permission_request
    assert len(media.calls) == before
    assert all(c["tool_name"] == "read_video" for c in checks)


async def test_decoder_failure_and_empty_frames_never_claim_visual_success(tmp_path, media, monkeypatch):
    async def fail(args):
        raise VideoReadError("VIDEO_DECODE_FAILED", "bad video")

    monkeypatch.setattr(module, "_run_process", fail)
    result = await make_tool(tmp_path).invoke({"path": "clip.mp4", "instruction": "Read"})
    assert not result.success
    assert result.raw_output["code"] == "VIDEO_DECODE_FAILED"
    assert result.image_content is None


async def test_missing_vision_and_asr_report_unavailable(tmp_path, media):
    result = await make_tool(tmp_path, proxy_llm_resolver=lambda: None).invoke(
        {"path": "clip.mp4", "instruction": "Read"})
    assert not result.success
    assert result.raw_output["visual"]["status"] == "unavailable"
    assert result.raw_output["audio"]["status"] == "unavailable"


async def test_read_video_emits_progress_and_honors_cancellation_before_io(tmp_path, media):
    tool = make_tool(tmp_path)
    queue = asyncio.Queue()
    await tool.invoke({"path": "clip.mp4", "instruction": "Read"},
                      context=ToolInvocationContext(event_queue=queue, parent_tool_call_id="video"))
    assert queue.qsize() >= 3
    with bind_tool_cancellation(lambda: True):
        # Use the real process runner; it must reject before spawning anything.
        with pytest.raises(Exception, match="TOOL_EXECUTION_CANCELLED"):
            await _run_process(["not-a-real-executable"])


def test_media_runtime_prefers_desktop_paths_and_reports_missing_override(tmp_path):
    executable = Path(sys.executable)
    assert _executable("ffmpeg", {"HYPERFRAMES_FFMPEG_PATH": str(executable)}) == str(executable)
    with pytest.raises(VideoReadError, match="not executable"):
        _executable("ffmpeg", {"BOX_AGENT_FFMPEG_PATH": str(tmp_path / "missing")})


def test_shared_workspace_setup_registers_video_without_bash_or_sandbox(tmp_path):
    config = SimpleNamespace(tools=SimpleNamespace(enable_bash=False, enable_file_tools=False,
                                                   enable_todo=False, enable_sub_agent=False))
    tools = []
    add_workspace_tools(tools, config, tmp_path, llm=Vision(), output=lambda *_: None)
    assert isinstance(next(t for t in tools if t.name == "read_video"), ReadVideoTool)


async def test_shared_workspace_video_inherits_desktop_asr_and_media_paths(tmp_path, media, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("VOLC_ASR_", "DOUBAO_ASR_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("VOLC_ASR_APPID", "desktop-test-app")
    monkeypatch.setenv("VOLC_ASR_AT", "desktop-test-token")
    monkeypatch.setenv("VOLC_ASR_RESOURCE_ID", "desktop-test-resource")
    monkeypatch.setenv("HYPERFRAMES_FFMPEG_PATH", "/desktop/ffmpeg")
    monkeypatch.setenv("HYPERFRAMES_FFPROBE_PATH", "/desktop/ffprobe")
    received = []

    def executable(name, env):
        assert env[f"HYPERFRAMES_{name.upper()}_PATH"] == f"/desktop/{name}"
        return name

    async def transcribe(path, *, config, **kwargs):
        received.append(config)
        return {"status": "transcribed", "text": "desktop speech", "segments": []}

    monkeypatch.setattr(module, "_executable", executable)
    monkeypatch.setattr(module, "transcribe_pcm", transcribe)
    config = SimpleNamespace(tools=SimpleNamespace(enable_bash=False, enable_file_tools=False,
                                                   enable_todo=False, enable_sub_agent=False))
    tools = []
    add_workspace_tools(tools, config, tmp_path, llm=Vision(), output=lambda *_: None)
    video = next(tool for tool in tools if tool.name == "read_video")
    result = await video.invoke({"path": "clip.mp4", "instruction": "Describe"})
    assert result.success, result.error
    assert result.raw_output["audio"]["status"] == "transcribed"
    assert result.raw_output["visual"]["status"] == "native_frames"
    assert received[0].app_id == "desktop-test-app"
    assert received[0].access_key == "desktop-test-token"
    assert received[0].resource_id == "desktop-test-resource"
    assert "desktop-test-token" not in result.model_dump_json()


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe unavailable")
async def test_real_video_frames_and_clipped_pcm_reach_existing_vision_chain(tmp_path, monkeypatch):
    source = tmp_path / "real video.mp4"
    await _run_process([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=160x90:rate=5:duration=4", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=16000:duration=4", "-c:v", "mpeg4",
                        "-c:a", "aac", "-shortest", str(source)])
    original = source.read_bytes()
    received = []

    async def transcribe(path, **kwargs):
        received.append((path.read_bytes(), kwargs))
        return {"status": "transcribed", "text": "mock transcript", "segments": []}

    monkeypatch.setattr(module, "transcribe_pcm", transcribe)
    llm = Vision()
    tool = ReadVideoTool(llm, workspace_dir=str(tmp_path), allow_full_access=False,
                         runtime_env={**os.environ, "VOLC_ASR_API_KEY": "test-only"})
    result = await tool.invoke({"path": source.name, "instruction": "Describe the frames",
                                "start_seconds": 1.1, "end_seconds": 2.5,
                                "frame_count": 3, "strategy": "proxy"})
    assert result.success, result.error
    assert result.raw_output["visual"]["status"] == "analyzed"
    assert [f["timestamp_seconds"] for f in result.raw_output["frames"]] == [1.4, 1.8, 2.4]
    assert len(received[0][0]) == round(1.4 * 32000)
    assert received[0][1]["start_seconds"] == 1.1
    assert llm.calls and llm.calls[0][1]["call_kind"] == "utility"
    assert source.read_bytes() == original


@pytest.mark.parametrize("cancel", [False, True])
async def test_decoder_is_reaped_on_timeout_or_cancellation(tmp_path, monkeypatch, cancel):
    processes = []
    create = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        proc = await create(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "_PROCESS_TIMEOUT", 0.1 if not cancel else 10)
    task = asyncio.create_task(_run_process([sys.executable, "-c", "import time; time.sleep(30)"]))
    if cancel:
        while not processes:
            await asyncio.sleep(0.01)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else VideoReadError):
        await task
    assert processes[0].returncode is not None


async def test_video_proxy_tracks_agent_thinking_choice(tmp_path, media):
    from box_agent.agent import Agent

    llm = Vision()
    tool = make_tool(tmp_path, llm)
    agent = Agent(llm_client=llm, tools=[tool], system_prompt="Test", workspace_dir=str(tmp_path),
                  thinking_enabled=True)
    for enabled in (True, False):
        agent.thinking_enabled = enabled
        result = await tool.invoke({"path": "clip.mp4", "instruction": "Read", "strategy": "proxy"})
        assert result.success
        assert llm.calls[-1][1]["thinking_enabled"] is enabled


async def test_long_transcript_is_preserved_but_context_is_explicitly_bounded(tmp_path, media, monkeypatch):
    long_text = '"\\\n' * 20_000

    async def transcribe(*args, **kwargs):
        return {"status": "transcribed", "text": long_text, "segments": [{"text": long_text}]}

    monkeypatch.setattr(module, "transcribe_pcm", transcribe)
    tool = make_tool(tmp_path)
    tool._runtime_env = {"VOLC_ASR_API_KEY": "test"}
    result = await tool.invoke({"path": "clip.mp4", "instruction": "Read"})
    assert result.raw_output["audio"]["text"] == long_text
    assert len(result.model_context) <= module._MAX_MODEL_CONTEXT_CHARS
    assert json.loads(result.model_context)["model_context_truncated"]
    assert "shorter time range" in result.model_context


async def test_cache_evicts_old_generated_media(tmp_path, media):
    tool = make_tool(tmp_path)
    first_directory = None
    for start in range(4):
        result = await tool.invoke({"path": "clip.mp4", "instruction": "Read", "start_seconds": start})
        assert result.success
        if start == 0:
            first_directory = Path(next(iter(tool._cache.values())).directory.name)
    assert len(tool._cache) == 3
    assert not first_directory.exists()


@pytest.mark.parametrize("same_range", [False, True])
async def test_concurrent_reads_preserve_inflight_frames_and_do_not_duplicate_asr(tmp_path, media, monkeypatch, same_range):
    started, finish = asyncio.Event(), asyncio.Event()
    transcriptions = 0

    async def transcribe(*args, **kwargs):
        nonlocal transcriptions
        transcriptions += 1
        started.set()
        await finish.wait()
        return {"status": "transcribed", "text": "Hello"}

    monkeypatch.setattr(module, "transcribe_pcm", transcribe)
    tool = make_tool(tmp_path)
    tool._runtime_env = {"VOLC_ASR_API_KEY": "test"}
    args = {"path": "clip.mp4", "instruction": "Read"}
    first = asyncio.create_task(tool.invoke(args))
    await asyncio.wait_for(started.wait(), 3)
    if same_range:
        second = asyncio.create_task(tool.invoke(args))
        await asyncio.sleep(0)
    else:
        # Evict the first range while its transcript is still being received.
        for start in (1, 2, 3):
            assert (await tool.invoke({**args, "start_seconds": start, "transcribe": False})).success
    finish.set()
    result = await first
    assert result.success and result.raw_output["visual"]["status"] == "native_frames"
    assert len(result.image_content) >= 6
    if same_range:
        assert (await second).raw_output["audio"]["text"] == "Hello"
    assert transcriptions == 1


def test_video_delegation_is_rejected_until_image_delivery_can_bind_to_child_model(tmp_path):
    from box_agent.tools.sub_agent_capabilities import CapabilityResolver, CapabilityFailure
    from tests.test_sub_agent_capabilities import _parse

    tool = make_tool(tmp_path)
    spec = _parse(required_tools=["read_video"])
    result = CapabilityResolver().resolve(spec, parent_tools={"read_video": tool})
    assert isinstance(result, CapabilityFailure)
    assert result.details["denied_reason"] == "process_not_delegable"


async def test_frame_cache_does_not_cache_model_analysis(tmp_path, media):
    llm = Vision()
    tool = make_tool(tmp_path, llm)
    await tool.invoke({"path": "clip.mp4", "instruction": "Describe", "strategy": "proxy"})
    await tool.invoke({"path": "clip.mp4", "instruction": "Read text", "strategy": "proxy"})
    assert len(llm.calls) == 2
    assert "Read text" in llm.calls[-1][0][1].content[0]["text"]


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe unavailable")
@pytest.mark.parametrize("extension", ["mp4", "mkv", "ts"])
async def test_nonzero_container_timestamps_are_normalized_to_video_start(tmp_path, extension):
    source = tmp_path / f"offset.{extension}"
    await _run_process([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=160x90:rate=5:duration=3", "-c:v", "mpeg4",
                        "-output_ts_offset", "10", str(source)])
    tool = make_tool(tmp_path)
    tool._runtime_env = dict(os.environ)
    result = await tool.invoke({"path": source.name, "instruction": "Read", "frame_count": 2,
                                "start_seconds": 1, "end_seconds": 2})
    assert result.success, result.error
    assert result.raw_output["duration_seconds"] == 3
    assert [f["timestamp_seconds"] for f in result.raw_output["frames"]] == [1.4, 1.8]


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe unavailable")
async def test_variable_frame_rate_reports_decoded_timestamps_instead_of_sample_targets(tmp_path):
    source = tmp_path / "vfr.mp4"
    await _run_process([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=160x90:rate=10:duration=3", "-vf",
                        "select='eq(n,0)+eq(n,3)+eq(n,11)+eq(n,18)+eq(n,29)'",
                        "-fps_mode", "vfr", "-c:v", "mpeg4", str(source)])
    tool = make_tool(tmp_path)
    tool._runtime_env = dict(os.environ)
    result = await tool.invoke({"path": source.name, "instruction": "Read", "frame_count": 2,
                                "start_seconds": 0, "end_seconds": 2})
    assert result.success, result.error
    assert [f["timestamp_seconds"] for f in result.raw_output["frames"]] == [1.1, 1.8]


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe unavailable")
@pytest.mark.parametrize("rate,start,end,last_pts", [(1, 0, 1, 0), (1, 0.2, 0.8, 0), (5, 0, 1, 0.8)])
async def test_single_frame_and_tail_reads_retain_frame_still_displayed_at_sample(tmp_path, rate, start, end, last_pts):
    source = tmp_path / "short.mp4"
    await _run_process([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        f"color=c=red:size=160x90:rate={rate}:duration=1", "-c:v", "mpeg4", str(source)])
    result = await ReadVideoTool(Vision(), workspace_dir=str(tmp_path), runtime_env={}).invoke(
        {"path": source.name, "instruction": "Read", "start_seconds": start, "end_seconds": end})
    assert result.success, result.error
    assert result.raw_output["frames"][-1]["timestamp_seconds"] == last_pts
    assert result.raw_output["frames"][-1]["sample_seconds"] > last_pts
    assert len(result.raw_output["frames"]) == 6


async def test_agent_stop_cancels_video_decoder_through_real_tool_scheduler(tmp_path, media, monkeypatch):
    from box_agent.tools.engine.scheduler import ToolEngine, ToolInvocationCompleted, ToolInvocationRequest

    processes = []
    spawned = asyncio.Event()
    create = asyncio.create_subprocess_exec
    cancelled = False

    async def spawn(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        spawned.set()
        return process

    async def slow_probe(*args):
        await _run_process([sys.executable, "-c", "import time; time.sleep(30)"])

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    tool = make_tool(tmp_path)
    monkeypatch.setattr(tool, "_probe", slow_probe)
    engine = ToolEngine(tools={tool.name: tool}, is_cancelled=lambda: cancelled,
                        activity_interval_seconds=1, event_poll_interval_seconds=0.01,
                        cancel_grace_seconds=1, max_parallel_tools=1, batch_timeout_seconds=None,
                        web_search_concurrency=1, web_search_tool_name="web_search")

    async def invoke():
        return [event async for event in engine.invoke_serial(ToolInvocationRequest(
            call_id="video-stop", tool_name="read_video", arguments={"path": "clip.mp4", "instruction": "Read"}))]

    task = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        cancelled = True
        events = await asyncio.wait_for(task, 2)
        outcome = next(event.result for event in events if isinstance(event, ToolInvocationCompleted))
        assert not outcome.success and "cancel" in outcome.error.lower()
        assert processes[0].returncode is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
