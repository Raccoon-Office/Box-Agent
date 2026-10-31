# Built-in video reading

`read_video` is registered by shared workspace-tool setup for CLI, ACP and SDK
sessions, including sessions without Bash or Jupyter. It reads local files and
reuses `inspect_images` for visual understanding. It does not change the source
video. Remote video URLs and native video-model APIs are outside this version.

```json
{
  "path": "recording.mp4",
  "instruction": "Explain the failed operation and cite timestamps.",
  "start_seconds": 30,
  "end_seconds": 60
}
```

- Times are seconds from the start of the original video; the end is exclusive.
- Defaults: start at zero, end at video duration, six frames, speech transcription
  enabled. `frame_count` accepts 1–6; `transcribe: false` skips speech processing.
- Each call covers at most 600 seconds. Longer requested ranges return
  `has_more: true` and `next_start_seconds`; continue with that start and the
  original requested end. One page is not a full-video review.
- Frames are sampled at equal intervals and labeled with decoder timestamps.
  If a sample is after the last frame's timestamp, the frame still displayed at
  that sample is used; `sample_seconds` distinguishes the sample from its earlier
  frame timestamp (which may precede the requested start).
  Sampling may miss rapid events or small text. Reread a narrower interval for
  details. The tool does not claim continuous video coverage.
- `strategy` has the same `native` / `proxy` meanings as `inspect_images`. Native
  frames use the existing image retention policy; proxy analysis uses a vision
  model and returns written findings. The main model may be text-only when a
  vision proxy is configured. Proxy calls follow the session's thinking setting.
- This version runs in the main session. Sub-agent delegation is rejected until
  native image delivery can be bound to the child model rather than the parent.

## Runtime and speech service

The tool needs ffmpeg and ffprobe. Discovery order for each executable is
`BOX_AGENT_FFMPEG_PATH` / `BOX_AGENT_FFPROBE_PATH`, the desktop's
`HYPERFRAMES_FFMPEG_PATH` / `HYPERFRAMES_FFPROBE_PATH`, then `PATH`. It does not
install or download binaries. A missing runtime returns
`VIDEO_RUNTIME_UNAVAILABLE`. Existing host media-runtime provisioning can supply
the binaries; standalone installations must supply them themselves.

Speech uses the same Volc ASR configuration and binary WebSocket protocol as the
desktop voice-input client. No LLM API key is repurposed for ASR. Supported
environment variables match that client:

| Setting | Environment |
| --- | --- |
| API key (preferred) | `VOLC_ASR_API_KEY` or `DOUBAO_ASR_API_KEY` |
| Legacy app ID | `VOLC_ASR_APP_ID`, `VOLC_ASR_APPID`, or corresponding `DOUBAO_ASR_*` names |
| Legacy access key | `VOLC_ASR_ACCESS_KEY`, `VOLC_ASR_ACCESS_TOKEN`, `VOLC_ASR_AT`, `VOLC_ASR_TOKEN`, or corresponding `DOUBAO_ASR_*` names |
| Endpoint | `VOLC_ASR_WS_URL`, default `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` |
| Resource | `VOLC_ASR_RESOURCE_ID`, default `volc.seedasr.sauc.duration` |

Credentials belong in the host process environment, not tool arguments, prompts
or committed config. The desktop Agent process inherits its environment. CLI
users must provide the same configuration in their own environment. This tool
requires a secure `wss` endpoint without credentials in the URL.

Raccoon Lab's Electron bootstrap loads `apps/office-desktop/.env.electron` into
the host environment, with `.env.electron.local` overrides in development.
`createAgentEnv` passes that environment to Box-Agent, including the legacy
`VOLC_ASR_APPID` / `VOLC_ASR_AT` names. Box-Agent does not search for or copy the
client's credential files; standalone CLI launches must receive those variables.

The selected audio interval is decoded to mono 16 kHz signed 16-bit PCM. Its
first audio track is sent in paced 200 ms chunks, matching the desktop streaming
service. **Transcription therefore takes approximately the selected audio
duration**, plus connection/finalization time, and incurs that service's usage.
Progress is emitted every ten audio seconds. This version does not substitute a
separate asynchronous file-transcription service. Utterance timestamps are
offset back into the original video's timeline; non-speech sounds are not
analyzed.

## Results, failures and retention

Inspect `visual.status` (`native_frames`, `analyzed`, `unavailable`) and
`audio.status` (`transcribed`, `no_audio`, `disabled`, `unavailable`, `failed`).
An available visual result can be returned when speech fails, and a transcript
can be returned when no vision model is available. These are partial results;
do not claim to have heard or seen an unavailable modality. An interrupted
partial ASR response is never labeled a completed transcript.

Source reads, including cache hits, use existing filesystem permission checks.
Files are limited to 2 GiB; supported containers include MP4/MOV, MKV/WebM, AVI,
FLV, MPEG/TS and OGV. Decoding uses local-only protocols and a container allowlist,
bounded output and timeouts. Cancellation reaps decoders and closes ASR sockets.

The session caches at most three preparations, keyed by resolved path, file
identity/version, range and frame count. Successful transcripts are reused;
failed transcription and model analysis are not cached. Temporary media is
removed after eviction once in-flight reads finish, and on session scratch cleanup.
Concurrent reads of the same preparation share a successful transcription.
Full tool results retain the
transcript; model context is bounded to 24,000 characters and explicitly marks
omission with `model_context_truncated` and a shorter-range continuation hint.
Durable image bytes/references follow the existing image-store contract.

## Verification

```bash
uv run --no-sync pytest -q tests/test_video_tool.py tests/test_video_transcription.py
```

The tests cover real ffmpeg clipping/extraction when binaries are available,
native/proxy visual paths, permissions, pagination, cache invalidation, malformed
ASR packets, a local WebSocket protocol round trip, partial failures and
cancellation. A local simulated ASR server proves protocol integration, not
production speech accuracy, credentials, billing or installed-client behavior.

References: [Volc streaming ASR protocol](https://docs.volcengine.com/docs/doubao-voice/large-model-streaming-automatic-speech-recognition-api-1?lang=en)
and the desktop `runtime/electron/main/voice/volcAsrClient.ts` /
`volcAsrProtocol.ts` implementations in the Raccoon Lab repository.
