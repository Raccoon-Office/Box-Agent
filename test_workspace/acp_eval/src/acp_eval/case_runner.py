"""Orchestrate one self-contained offline ACP evaluation attempt."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, monotonic_ns
from typing import Any, Callable, Mapping, Sequence

from acp_eval import SCHEMA_VERSION
from acp_eval.effect_client import EffectEvaluationConfig, evaluate_attempt
from acp_eval.ids import new_attempt_id
from acp_eval.lifecycle import ProcessRecorder, drain_stream, stop_process
from acp_eval.models import AttemptManifest, RunResult
from acp_eval.protocol import ACPAccumulator, ProtocolRecorder
from acp_eval.snapshots import build_artifact_inventory, write_snapshot
from acp_eval.stderr_scan import scan_stderr, summarize_stderr
from acp_eval.storage import atomic_write_json, sha256_file


# This is transport backpressure capacity, not a frame-size ceiling. Frames are
# assembled from persisted 64 KiB chunks and may be arbitrarily larger.
ACP_STREAM_BUFFER_LIMIT = 4 * 1024 * 1024
_AUTO_ROUTE_TAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("presentation", re.compile(r"PPT|PPTX|演示文稿|幻灯片|slides?|presentation", re.I)),
    ("data-analysis", re.compile(r"数据|表格|CSV|Excel|统计|data|spreadsheet", re.I)),
    ("code", re.compile(r"代码|开发|网页|网站|HTML|code|develop|website", re.I)),
    ("document", re.compile(r"文档|文件|报告|PDF|Word|document|report", re.I)),
    ("analysis", re.compile(r"分析|比较|评估|analysis|compare|evaluate", re.I)),
    ("reasoning", re.compile(r"推理|论证|证明|reasoning|proof", re.I)),
)


@dataclass(frozen=True)
class CaseConfig:
    repo_root: Path
    evaluation_dir: Path
    dataset_root: Path
    timeout_seconds: float
    python_executable: str
    effect_eval_url: str | None = None
    effect_eval_timeout_seconds: float = 180.0
    model: str | None = None
    model_max_tokens: int | None = None
    model_binding: dict[str, Any] | None = None


@dataclass
class _AttemptState:
    acp_status: str = "error"
    process_exit_code: int | None = None
    error: str | None = None
    upstream_session_id: str | None = None
    acp_session_id: str | None = None
    stdout_terminal: bool = False
    collection_error: bool = False
    finalization_errors: list[str] = field(default_factory=list)


class _ACPStdoutReader:
    """Persist arbitrary chunks before incrementally extracting JSONL frames."""

    def __init__(self, stream: asyncio.StreamReader, protocol: ProtocolRecorder) -> None:
        self.stream = stream
        self.protocol = protocol
        self.buffer = bytearray()
        self.buffer_offset = 0
        self.eof = False

    async def read_frame(
        self, deadline: float | None = None
    ) -> tuple[bool, dict[str, Any] | None]:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                frame_size = newline + 1
                raw_line = bytes(self.buffer[:frame_size])
                byte_offset = self.buffer_offset
                del self.buffer[:frame_size]
                self.buffer_offset += frame_size
                return False, self.protocol.record_persisted_received(
                    raw_line, byte_offset
                )

            if self.eof:
                if self.buffer:
                    raw_data = bytes(self.buffer)
                    byte_offset = self.buffer_offset
                    self.buffer.clear()
                    self.buffer_offset += len(raw_data)
                    self.protocol.record_incomplete_received(raw_data, byte_offset)
                return True, None

            read = self.stream.read(64 * 1024)
            if deadline is None:
                chunk = await read
            else:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                chunk = await asyncio.wait_for(read, timeout=remaining)
            if not chunk:
                self.eof = True
                continue
            chunk_offset = self.protocol.record_received_chunk(chunk)
            if not self.buffer:
                self.buffer_offset = chunk_offset
            self.buffer.extend(chunk)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_run_id(evaluation_dir: Path) -> str:
    manifest_path = evaluation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_id = manifest.get("run_id") if isinstance(manifest, Mapping) else None
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(f"evaluation manifest has no valid run_id: {manifest_path}")
    return run_id


def _case_id(record: Mapping[str, Any]) -> str:
    value = record.get("id")
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("case id must be a non-empty direct-directory name")
    return value


def _query(record: Mapping[str, Any]) -> str:
    value = record.get("query")
    if not isinstance(value, str) or not value:
        raise ValueError("case query must be a non-empty string")
    return value


def _input_paths(record: Mapping[str, Any]) -> Sequence[str]:
    value = record.get("input_files", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("case input_files must be a list of paths")
    return value


def _copy_inputs(dataset_root: Path, workspace: Path, paths: Sequence[str]) -> None:
    root = dataset_root.resolve()
    destination_names: set[str] = set()
    for relative_path in paths:
        if not relative_path or "\x00" in relative_path:
            raise ValueError(f"invalid input path: {relative_path!r}")
        source = (root / relative_path).resolve()
        try:
            source.relative_to(root)
        except ValueError as error:
            raise ValueError(f"input escapes dataset root: {relative_path}") from error
        if not source.is_file():
            raise FileNotFoundError(f"missing input file: {source}")
        if source.name in destination_names:
            raise ValueError(f"duplicate input basename: {source.name}")
        shutil.copy2(source, workspace / source.name)
        destination_names.add(source.name)


def _new_attempt_dir(case_dir: Path) -> tuple[str, Path]:
    attempts = case_dir / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        attempt_id = new_attempt_id()
        attempt_dir = attempts / attempt_id
        try:
            attempt_dir.mkdir()
        except FileExistsError:
            continue
        return attempt_id, attempt_dir
    raise RuntimeError("could not allocate a unique attempt id")


def _session_params(
    workspace: Path,
    case_id: str,
    model: str | None = None,
    model_max_tokens: int | None = None,
    model_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    metadata: dict[str, Any] = {
        "title": "acp",
        "session_id": f"eval-acp-{case_id}",
        "permission_mode": "default",
        "filesystem_policy": {
            "session_workspace_root": str(workspace),
            "allowed_directories": [],
            "filesystem_scope": "session_workspace",
        },
        "workspace_layout": {
            "artifact_root_dir": str(workspace / "output"),
        },
    }
    binding: dict[str, Any] | None = None
    if model_binding is not None:
        binding = dict(model_binding)
    elif model:
        binding = {"source": "builtin", "model": model}
        if model_max_tokens is not None:
            binding["maxTokens"] = model_max_tokens
    if binding is not None:
        metadata.update(
            {
                "llm_binding": binding,
                "enableBuiltinModelRouting": True,
            }
        )
    return {
        "cwd": str(workspace),
        "mcpServers": [],
        "_meta": metadata,
    }


def _resolve_model_binding(
    model_binding: Mapping[str, Any] | None,
    query: str,
) -> dict[str, Any] | None:
    if model_binding is None:
        return None
    binding = dict(model_binding)
    mode = binding.pop("evaluationMode", "manual")
    if mode == "manual":
        return binding
    if mode != "auto":
        raise ValueError("unsupported evaluation model routing mode")
    auto_routing = binding.get("autoRouting")
    candidates = (
        auto_routing.get("models", [])
        if isinstance(auto_routing, Mapping)
        else []
    )
    task_tags = {
        tag for tag, pattern in _AUTO_ROUTE_TAGS if pattern.search(query)
    } or {"general", "chat"}
    eligible = [candidate for candidate in candidates if isinstance(candidate, Mapping)]
    if not eligible:
        raise ValueError("automatic model routing has no eligible model")
    selected = max(
        eligible,
        key=lambda candidate: (
            len(task_tags.intersection(candidate.get("tags", []))),
            int(candidate.get("abilityLevel", 0)),
        ),
    )
    if not isinstance(selected.get("model"), str) or not selected["model"]:
        raise ValueError("automatic model routing selected an invalid model")
    binding["model"] = selected["model"]
    for key in ("contextWindow", "maxTokens"):
        if key in selected:
            binding[key] = selected[key]
        else:
            binding.pop(key, None)
    return binding


def _prompt_params(session_id: str, query: str, case_id: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": query}],
        "_meta": {"title": "acp", "turnId": f"eval-acp-{case_id}-turn-1"},
    }


async def _send(
    process: asyncio.subprocess.Process,
    recorder: ProtocolRecorder,
    message: Mapping[str, Any],
) -> None:
    if process.stdin is None:
        raise RuntimeError("ACP stdin is unavailable")
    raw = recorder.record_sent(message)
    process.stdin.write(raw)
    await process.stdin.drain()


def _permission_reply(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "result": {"outcome": {"outcome": "cancelled"}},
    }


async def _read_until_response(
    process: asyncio.subprocess.Process,
    stdout_reader: _ACPStdoutReader,
    protocol: ProtocolRecorder,
    accumulator: ACPAccumulator,
    process_recorder: ProcessRecorder,
    state: _AttemptState,
    expected_id: int,
    deadline: float,
) -> dict[str, Any]:
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        eof, message = await stdout_reader.read_frame(deadline=deadline)
        if eof:
            process_recorder.write("stream.eof", stream="stdout")
            state.stdout_terminal = True
            raise EOFError("ACP process closed stdout")
        if message is None:
            continue
        accumulator.consume(message)
        if message.get("id") == expected_id and "method" not in message:
            return message
        if "id" in message and "method" in message:
            if message.get("method") == "session/request_permission":
                reply = _permission_reply(message)
            else:
                reply = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Unsupported reverse RPC"},
                }
            await _send(process, protocol, reply)


async def _drain_stdout(
    stdout_reader: _ACPStdoutReader,
    accumulator: ACPAccumulator,
    recorder: ProcessRecorder,
    state: _AttemptState,
) -> None:
    try:
        while True:
            eof, message = await stdout_reader.read_frame()
            if eof:
                break
            if message is not None:
                accumulator.consume(message)
    except asyncio.CancelledError:
        recorder.write("stream.error", stream="stdout", cancelled=True)
        state.collection_error = True
        raise
    except Exception as error:
        recorder.write("stream.error", stream="stdout", error=str(error))
        state.collection_error = True
        raise
    recorder.write("stream.eof", stream="stdout")
    state.stdout_terminal = True


def _response_status(response: Mapping[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, Mapping):
        return "error"
    result = response.get("result")
    if not isinstance(result, Mapping):
        return "error"
    stop_reason = result.get("stopReason")
    metadata = result.get("_meta", result.get("meta", {}))
    if isinstance(metadata, Mapping):
        if metadata.get("ok") is False or metadata.get("runStatus") == "error":
            return "error"
        if metadata.get("completed") is False:
            return "incomplete"
    if stop_reason == "cancelled":
        return "cancelled"
    if stop_reason in {"max_tokens", "max_turn_requests"}:
        return "incomplete"
    return "completed"


async def _exchange(
    process: asyncio.subprocess.Process,
    stdout_reader: _ACPStdoutReader,
    protocol: ProtocolRecorder,
    accumulator: ACPAccumulator,
    process_recorder: ProcessRecorder,
    state: _AttemptState,
    workspace: Path,
    case_id: str,
    query: str,
    timeout_seconds: float,
    model: str | None = None,
    model_max_tokens: int | None = None,
    model_binding: Mapping[str, Any] | None = None,
) -> None:
    deadline = monotonic() + timeout_seconds
    session_params = _session_params(
        workspace,
        case_id,
        model,
        model_max_tokens,
        _resolve_model_binding(model_binding, query),
    )
    session_meta = session_params.get("_meta")
    upstream_session_id = (
        session_meta.get("session_id")
        if isinstance(session_meta, Mapping)
        else None
    )
    if not isinstance(upstream_session_id, str) or not upstream_session_id:
        raise RuntimeError("ACP session/new request has no upstream session id")
    state.upstream_session_id = upstream_session_id
    await _send(
        process,
        protocol,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "box-agent-eval", "version": "1.0"},
                "protocolVersion": 1,
            },
        },
    )
    initialize = await _read_until_response(
        process,
        stdout_reader,
        protocol,
        accumulator,
        process_recorder,
        state,
        1,
        deadline,
    )
    if "error" in initialize:
        raise RuntimeError(f"ACP initialize failed: {initialize['error']}")

    await _send(
        process,
        protocol,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": session_params,
        },
    )
    new_session = await _read_until_response(
        process,
        stdout_reader,
        protocol,
        accumulator,
        process_recorder,
        state,
        2,
        deadline,
    )
    result = new_session.get("result")
    acp_session_id = result.get("sessionId") if isinstance(result, Mapping) else None
    if not isinstance(acp_session_id, str) or not acp_session_id:
        raise RuntimeError("ACP session/new response has no sessionId")
    state.acp_session_id = acp_session_id

    await _send(
        process,
        protocol,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": _prompt_params(acp_session_id, query, case_id),
        },
    )
    response = await _read_until_response(
        process,
        stdout_reader,
        protocol,
        accumulator,
        process_recorder,
        state,
        3,
        deadline,
    )
    state.acp_status = _response_status(response)
    process_recorder.write("acp.completed", status=state.acp_status)


def _signal_process_group(
    process_group_id: int,
    signal_number: signal.Signals,
    recorder: ProcessRecorder,
    reason: str,
) -> bool:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        recorder.write(
            "signal.not_sent",
            signal=signal_number.name,
            target="process_group",
            process_group_id=process_group_id,
            initiator="capture",
            reason=reason,
            process_already_exited=True,
        )
        return False
    recorder.write(
        "signal.sent",
        signal=signal_number.name,
        target="process_group",
        process_group_id=process_group_id,
        initiator="capture",
        reason=reason,
    )
    return True


async def _finish_stream_tasks(
    tasks: list[asyncio.Task[None]],
    process_group_id: int,
    recorder: ProcessRecorder,
    state: _AttemptState,
) -> None:
    """Bound inherited-pipe cleanup, escalating against the child process group."""

    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=0.2)
    if pending:
        _signal_process_group(
            process_group_id,
            signal.SIGTERM,
            recorder,
            "inherited-stream-timeout",
        )
        _, pending = await asyncio.wait(pending, timeout=0.2)
    if pending:
        _signal_process_group(
            process_group_id,
            signal.SIGKILL,
            recorder,
            "process-group-term-timeout",
        )
        _, pending = await asyncio.wait(pending, timeout=0.5)
    if pending:
        state.collection_error = True
        for task in pending:
            task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    if any(isinstance(outcome, BaseException) for outcome in outcomes):
        state.collection_error = True


async def _run_process(
    config: CaseConfig,
    attempt_dir: Path,
    workspace: Path,
    case_id: str,
    query: str,
    protocol: ProtocolRecorder,
    accumulator: ACPAccumulator,
    process_recorder: ProcessRecorder,
    state: _AttemptState,
) -> None:
    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    stdout_task: asyncio.Task[None] | None = None
    process_group_id: int | None = None
    command = [config.python_executable, "-m", "box_agent.acp.server"]
    environment = os.environ.copy()
    environment["BOX_AGENT_SESSION_TRACE_DIR"] = str(attempt_dir / "agent")
    try:
        process_recorder.write("process.starting", command=command, cwd=str(config.repo_root))
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=config.repo_root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=ACP_STREAM_BUFFER_LIMIT,
        )
        process_group_id = process.pid
        process_recorder.write("process.started", pid=process.pid, command=command)
        if process.stderr is None or process.stdout is None:
            raise RuntimeError("ACP subprocess pipes are unavailable")
        stderr_task = asyncio.create_task(
            drain_stream(process.stderr, attempt_dir / "stderr.log", process_recorder, "stderr")
        )
        stdout_reader = _ACPStdoutReader(process.stdout, protocol)
        try:
            await _exchange(
                process,
                stdout_reader,
                protocol,
                accumulator,
                process_recorder,
                state,
                workspace,
                case_id,
                query,
                config.timeout_seconds,
                config.model,
                config.model_max_tokens,
                config.model_binding,
            )
        except asyncio.TimeoutError:
            state.acp_status = "timeout"
            state.error = "ACP attempt timed out"
            process_recorder.write("attempt.timeout", timeout_seconds=config.timeout_seconds)
            if state.acp_session_id is not None:
                try:
                    await _send(
                        process,
                        protocol,
                        {
                            "jsonrpc": "2.0",
                            "method": "session/cancel",
                            "params": {"sessionId": state.acp_session_id},
                        },
                    )
                except (BrokenPipeError, ConnectionResetError):
                    process_recorder.write("cancel.not_sent", reason="stdin-closed")
        except Exception as error:
            state.acp_status = "error"
            state.error = f"{type(error).__name__}: {error}"
            process_recorder.write("attempt.error", error=state.error, terminal=False)
        finally:
            if not state.stdout_terminal:
                stdout_task = asyncio.create_task(
                    _drain_stdout(
                        stdout_reader,
                        accumulator,
                        process_recorder,
                        state,
                    )
                )
            process_recorder.write("process.stop_requested", initiator="capture")
            state.process_exit_code = await stop_process(
                process,
                process_recorder,
                natural_exit_seconds=0.5,
                term_seconds=1.0,
            )
    except Exception as error:
        state.acp_status = "error"
        state.error = f"{type(error).__name__}: {error}"
        process_recorder.write("attempt.error", error=state.error, terminal=True)
    finally:
        tasks = [task for task in (stdout_task, stderr_task) if task is not None]
        if process_group_id is not None:
            await _finish_stream_tasks(
                tasks,
                process_group_id,
                process_recorder,
                state,
            )
        elif tasks:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            state.collection_error = True


def _process_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _trace_validation(
    agent_dir: Path,
    attempt_id: str,
    upstream_session_id: str | None,
    acp_session_id: str | None,
) -> tuple[list[str], bool]:
    traces = sorted(agent_dir.glob("*.jsonl"))
    if not traces:
        return ["agent_trace_missing"], False
    invalid = False
    mismatch = False
    upstream_mismatch = False
    acp_mismatch = False
    for trace in traces:
        lines = trace.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            invalid = True
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid = True
                continue
            if not isinstance(event, Mapping):
                invalid = True
                continue
            observed = event.get("attempt_id")
            if isinstance(observed, str) and observed != attempt_id:
                mismatch = True
            if (
                upstream_session_id is not None
                and event.get("session_id") != upstream_session_id
            ):
                upstream_mismatch = True
            if (
                acp_session_id is not None
                and event.get("acp_session_id") != acp_session_id
            ):
                acp_mismatch = True
    issues = []
    if invalid:
        issues.append("agent_trace_invalid")
    if mismatch:
        issues.append("agent_trace_attempt_mismatch")
    if upstream_mismatch:
        issues.append("identity_mismatch:agent_trace:session_id")
    if acp_mismatch:
        issues.append("identity_mismatch:agent_trace:acp_session_id")
    return issues, bool(issues)


def _read_json_mapping(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _protocol_validation(
    path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], bool]:
    sent: list[dict[str, Any]] = []
    received: list[dict[str, Any]] = []
    if not path.exists() or path.stat().st_size == 0:
        return sent, received, ["protocol_jsonl_empty"], False
    invalid = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            invalid = True
            continue
        if not isinstance(entry, Mapping):
            invalid = True
            continue
        direction = entry.get("direction")
        message = entry.get("message")
        if direction not in {"sent", "received"} or not isinstance(message, Mapping):
            invalid = True
            continue
        (sent if direction == "sent" else received).append(dict(message))
    issues = ["protocol_jsonl_invalid"] if invalid else []
    return sent, received, issues, invalid


def _raw_messages(path: Path) -> tuple[list[dict[str, Any]], bool]:
    try:
        data = path.read_bytes()
    except OSError:
        return [], True
    invalid = bool(data and not data.endswith(b"\n"))
    messages: list[dict[str, Any]] = []
    for raw_line in data.splitlines(keepends=True):
        if not raw_line.endswith(b"\n"):
            continue
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid = True
            continue
        if not isinstance(value, Mapping):
            invalid = True
            continue
        messages.append(dict(value))
    return messages, invalid


def _raw_protocol_validation(
    attempt_dir: Path,
    protocol_sent: list[dict[str, Any]],
    protocol_received: list[dict[str, Any]],
) -> tuple[list[str], bool]:
    stdin_messages, stdin_invalid = _raw_messages(attempt_dir / "acp-stdin.raw")
    stdout_messages, stdout_invalid = _raw_messages(attempt_dir / "acp-stdout.raw")
    issues: list[str] = []
    if stdin_invalid:
        issues.append("raw_stdin_invalid")
    if stdout_invalid:
        issues.append("raw_stdout_invalid")
    if not stdin_invalid and stdin_messages != protocol_sent:
        issues.append("raw_stdin_protocol_mismatch")
    if not stdout_invalid and stdout_messages != protocol_received:
        issues.append("raw_stdout_protocol_mismatch")
    return issues, bool(issues)


def _snapshot_validation(path: Path, workspace: Path) -> tuple[list[str], bool]:
    if not path.exists():
        return [f"snapshot_missing:{path.name}"], False
    document = _read_json_mapping(path)
    files = document.get("files") if document is not None else None
    if not isinstance(files, list):
        return [f"snapshot_invalid:{path.name}"], True
    issues: list[str] = []
    workspace_root = workspace.resolve()
    for value in files:
        if not isinstance(value, Mapping):
            issues.append(f"snapshot_invalid:{path.name}")
            continue
        relative_path = value.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            issues.append(f"snapshot_invalid:{path.name}")
            continue
        candidate = workspace / relative_path
        try:
            candidate.resolve().relative_to(workspace_root)
        except (OSError, RuntimeError, ValueError):
            issues.append(f"snapshot_path_invalid:{relative_path}")
            continue
        if value.get("kind") != "file":
            continue
        expected_hash = value.get("sha256")
        if not candidate.is_file():
            issues.append(f"snapshot_file_missing:{relative_path}")
        elif not isinstance(expected_hash, str) or sha256_file(candidate) != expected_hash:
            issues.append(f"snapshot_hash_mismatch:{relative_path}")
    return issues, bool(issues)


def _identity_validation(
    attempt_dir: Path,
    case_dir: Path,
    run_id: str,
    case_id: str,
    attempt_id: str,
    protocol_sent: list[dict[str, Any]],
    protocol_received: list[dict[str, Any]],
    state: _AttemptState,
) -> tuple[list[str], bool]:
    issues: list[str] = []
    expected = {
        "run_id": run_id,
        "case_id": case_id,
        "attempt_id": attempt_id,
    }
    for name in ("manifest.json", "run.json"):
        path = attempt_dir / name
        if not path.exists():
            continue
        document = _read_json_mapping(path)
        if document is None:
            issues.append(f"invalid_json:{name}")
            continue
        for key, value in expected.items():
            if document.get(key) != value:
                issues.append(f"identity_mismatch:{name}:{key}")
        if name == "run.json":
            if (
                state.upstream_session_id is not None
                and document.get("session_id") != state.upstream_session_id
            ):
                issues.append("identity_mismatch:run.json:session_id")
            if (
                state.acp_session_id is not None
                and document.get("acp_session_id") != state.acp_session_id
            ):
                issues.append("identity_mismatch:run.json:acp_session_id")

    expected_turn_id: str | None = None
    for message in protocol_sent:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, Mapping):
            continue
        if method == "session/new" and state.upstream_session_id is not None:
            metadata = params.get("_meta")
            requested = (
                metadata.get("session_id")
                if isinstance(metadata, Mapping)
                else None
            )
            if requested != state.upstream_session_id:
                issues.append("identity_mismatch:protocol:upstream_session_id")
        if method == "session/prompt":
            if (
                state.acp_session_id is not None
                and params.get("sessionId") != state.acp_session_id
            ):
                issues.append("identity_mismatch:protocol:prompt_acp_session_id")
            metadata = params.get("_meta")
            turn_id = metadata.get("turnId") if isinstance(metadata, Mapping) else None
            if isinstance(turn_id, str):
                expected_turn_id = turn_id

    run_document = _read_json_mapping(attempt_dir / "run.json")
    if run_document is not None and expected_turn_id is not None:
        if run_document.get("turn_id") != expected_turn_id:
            issues.append("identity_mismatch:run.json:turn_id")
    for message in protocol_received:
        if message.get("id") == 2 and "method" not in message:
            new_result = message.get("result")
            returned_session_id = (
                new_result.get("sessionId")
                if isinstance(new_result, Mapping)
                else None
            )
            if (
                state.acp_session_id is not None
                and returned_session_id != state.acp_session_id
            ):
                issues.append("identity_mismatch:protocol:new_session_acp_session_id")

        method = message.get("method")
        params = message.get("params")
        if isinstance(params, Mapping) and "sessionId" in params:
            if (
                method == "session/update"
                and params.get("sessionId") != state.acp_session_id
            ):
                issues.append("identity_mismatch:protocol:update_acp_session_id")
            elif (
                method == "session/request_permission"
                and params.get("sessionId") != state.acp_session_id
            ):
                issues.append("identity_mismatch:protocol:request_acp_session_id")
        if method == "session/update" and isinstance(params, Mapping):
            update = params.get("update")
            raw_output = update.get("rawOutput") if isinstance(update, Mapping) else None
            if isinstance(raw_output, Mapping) and raw_output.get("type") == "turn_usage":
                observed_upstream = raw_output.get(
                    "sessionId", raw_output.get("session_id")
                )
                observed_acp = raw_output.get("acpSessionId")
                observed_turn = raw_output.get("turnId", raw_output.get("turn_id"))
                if (
                    isinstance(observed_upstream, str)
                    and observed_upstream != state.upstream_session_id
                ):
                    issues.append("identity_mismatch:protocol:upstream_session_id")
                if (
                    isinstance(observed_acp, str)
                    and observed_acp != state.acp_session_id
                ):
                    issues.append("identity_mismatch:protocol:update_acp_session_id")
                if (
                    expected_turn_id is not None
                    and isinstance(observed_turn, str)
                    and observed_turn != expected_turn_id
                ):
                    issues.append("identity_mismatch:protocol:turn_id")

        result = message.get("result")
        if not isinstance(result, Mapping):
            continue
        metadata = result.get("_meta")
        usage = metadata.get("usage") if isinstance(metadata, Mapping) else None
        observed_upstream = (
            usage.get("sessionId", usage.get("session_id"))
            if isinstance(usage, Mapping)
            else None
        )
        observed_turn = (
            usage.get("turnId", usage.get("turn_id"))
            if isinstance(usage, Mapping)
            else metadata.get("turnId")
            if isinstance(metadata, Mapping)
            else None
        )
        if (
            isinstance(observed_upstream, str)
            and observed_upstream != state.upstream_session_id
        ):
            issues.append("identity_mismatch:protocol:upstream_session_id")
        if (
            expected_turn_id is not None
            and isinstance(observed_turn, str)
            and observed_turn != expected_turn_id
        ):
            issues.append("identity_mismatch:protocol:turn_id")

    case_input = _read_json_mapping(case_dir / "input.json")
    if case_input is None:
        issues.append("invalid_json:input.json")
    elif case_input.get("id") != case_id:
        issues.append("identity_mismatch:input.json:case_id")
    issues = list(dict.fromkeys(issues))
    return issues, bool(issues)


def _completeness(
    attempt_dir: Path,
    case_dir: Path,
    workspace: Path,
    run_id: str,
    case_id: str,
    attempt_id: str,
    protocol: ProtocolRecorder,
    state: _AttemptState,
    validate_final_documents: bool = True,
) -> dict[str, Any]:
    issues, corrupt = _trace_validation(
        attempt_dir / "agent",
        attempt_id,
        state.upstream_session_id,
        state.acp_session_id,
    )
    events = _process_events(attempt_dir / "process.jsonl")
    event_names = [event.get("event") for event in events]
    eof_streams = {
        event.get("stream") for event in events if event.get("event") == "stream.eof"
    }
    if "process.exited" not in event_names:
        issues.append("process_exit_missing")
    for stream in ("stdout", "stderr"):
        if stream not in eof_streams:
            issues.append(f"{stream}_eof_missing")
    if state.collection_error:
        issues.append("stream_collection_error")
    for stage in state.finalization_errors or []:
        issues.append(f"finalization_error:{stage}")

    required = [
        "acp-stdin.raw",
        "acp-stdout.raw",
        "protocol.jsonl",
        "stderr.log",
        "process.jsonl",
        "files-before.json",
        "files-after.json",
        "artifacts.json",
        "assistant.txt",
    ]
    if validate_final_documents:
        required.extend(("run.json", "manifest.json"))
    missing = [name for name in required if not (attempt_dir / name).exists()]
    issues.extend(f"missing:{name}" for name in missing)

    protocol_sent, protocol_received, protocol_issues, protocol_corrupt = (
        _protocol_validation(attempt_dir / "protocol.jsonl")
    )
    issues.extend(protocol_issues)
    corrupt = corrupt or protocol_corrupt
    raw_issues, raw_corrupt = _raw_protocol_validation(
        attempt_dir, protocol_sent, protocol_received
    )
    issues.extend(raw_issues)
    corrupt = corrupt or raw_corrupt
    snapshot_issues, snapshot_corrupt = _snapshot_validation(
        attempt_dir / "files-after.json", workspace
    )
    issues.extend(snapshot_issues)
    corrupt = corrupt or snapshot_corrupt
    if validate_final_documents:
        identity_issues, identity_corrupt = _identity_validation(
            attempt_dir,
            case_dir,
            run_id,
            case_id,
            attempt_id,
            protocol_sent,
            protocol_received,
            state,
        )
        issues.extend(identity_issues)
        corrupt = corrupt or identity_corrupt

    parse_errors = [asdict(error) for error in protocol.parse_errors]
    if parse_errors or corrupt:
        status = "corrupt"
    elif issues:
        status = "incomplete"
    else:
        status = "complete"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "issues": issues,
        "parse_errors": parse_errors,
        "unsupported": ["provider_internal_retries", "unemitted_cleanup_steps"],
    }


def _run_payload(
    result: RunResult,
    accumulator: ACPAccumulator,
    state: _AttemptState,
) -> dict[str, Any]:
    payload = result.to_dict()
    metadata = accumulator.final_response_metadata or {}
    usage = metadata.get("usage")
    turn_id = metadata.get("turnId", metadata.get("turn_id"))
    if not isinstance(turn_id, str) and isinstance(usage, Mapping):
        turn_id = usage.get("turnId", usage.get("turn_id"))
    payload.update(
        {
            "session_id": state.upstream_session_id,
            "acp_session_id": state.acp_session_id,
            "turn_id": turn_id if isinstance(turn_id, str) else None,
            "token_usage": accumulator.token_usage,
            "permission_request_count": accumulator.permission_request_count,
            "assistant_text_length": len(accumulator.assistant_text),
            "response_metadata": accumulator.final_response_metadata,
            "error": state.error,
        }
    )
    return payload


def _record_finalization_error(
    stage: str,
    error: BaseException,
    recorder: ProcessRecorder,
    state: _AttemptState,
) -> None:
    if stage not in state.finalization_errors:
        state.finalization_errors.append(stage)
    detail = f"{type(error).__name__}: {error}"
    state.error = detail if state.error is None else f"{state.error}; {detail}"
    recorder.write(
        "attempt.error",
        stage=stage,
        error=detail,
        terminal=True,
    )


def _try_finalize(
    stage: str,
    operation: Callable[[], Any],
    recorder: ProcessRecorder,
    state: _AttemptState,
    default: Any = None,
) -> Any:
    try:
        return operation()
    except Exception as error:
        _record_finalization_error(stage, error, recorder, state)
        return default


def _incomplete_fallback(state: _AttemptState) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "incomplete",
        "issues": [
            f"finalization_error:{stage}" for stage in state.finalization_errors
        ],
        "parse_errors": [],
        "unsupported": ["provider_internal_retries", "unemitted_cleanup_steps"],
    }


def _run_status_can_be_synchronized(
    attempt_dir: Path,
    completeness: Mapping[str, Any],
) -> bool:
    """Return whether rewriting run status would preserve corruption evidence."""

    run_document = _read_json_mapping(attempt_dir / "run.json")
    if run_document is None:
        return False
    issues = completeness.get("issues")
    if not isinstance(issues, list):
        return False
    unsafe_prefixes = (
        "invalid_json:run.json",
        "identity_mismatch:run.json",
        "missing:run.json",
        "finalization_error:run",
    )
    return not any(
        isinstance(issue, str) and issue.startswith(unsafe_prefixes)
        for issue in issues
    )


def run_case(record: Mapping[str, Any], config: CaseConfig) -> RunResult:
    """Run one ACP case and return its observed result without raising child errors."""

    evaluation_dir = Path(config.evaluation_dir)
    run_id = _read_run_id(evaluation_dir)
    case_id = _case_id(record)
    query = _query(record)
    case_dir = evaluation_dir / "cases" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(case_dir / "input.json", dict(record))
    attempt_id, attempt_dir = _new_attempt_dir(case_dir)
    workspace = attempt_dir / "workspace"
    agent_dir = attempt_dir / "agent"
    workspace.mkdir()
    agent_dir.mkdir()
    for name in ("acp-stdin.raw", "acp-stdout.raw", "protocol.jsonl", "stderr.log"):
        (attempt_dir / name).touch()

    started_at = _iso_now()
    manifest = AttemptManifest(
        run_id=run_id,
        case_id=case_id,
        attempt_id=attempt_id,
        started_at=started_at,
        status="starting",
    )
    atomic_write_json(attempt_dir / "manifest.json", manifest.to_dict())
    process_recorder = ProcessRecorder(attempt_dir / "process.jsonl")
    process_recorder.write(
        "attempt.started",
        run_id=run_id,
        case_id=case_id,
        attempt_id=attempt_id,
    )
    protocol = ProtocolRecorder(
        attempt_dir,
        wall_clock=lambda: datetime.now(timezone.utc),
        monotonic_ns=monotonic_ns,
    )
    accumulator = ACPAccumulator()
    state = _AttemptState()

    try:
        _copy_inputs(Path(config.dataset_root), workspace, _input_paths(record))
        write_snapshot(workspace, attempt_dir / "files-before.json")
        asyncio.run(
            _run_process(
                config,
                attempt_dir,
                workspace,
                case_id,
                query,
                protocol,
                accumulator,
                process_recorder,
                state,
            )
        )
    except Exception as error:
        state.acp_status = "error"
        state.error = f"{type(error).__name__}: {error}"
        process_recorder.write("attempt.error", error=state.error, terminal=True)

    _try_finalize(
        "assistant",
        lambda: (attempt_dir / "assistant.txt").write_text(
            accumulator.assistant_text, encoding="utf-8"
        ),
        process_recorder,
        state,
    )
    if not (attempt_dir / "files-before.json").exists():
        _try_finalize(
            "files-before",
            lambda: write_snapshot(workspace, attempt_dir / "files-before.json"),
            process_recorder,
            state,
        )
    _try_finalize(
        "files-after",
        lambda: write_snapshot(workspace, attempt_dir / "files-after.json"),
        process_recorder,
        state,
    )
    _try_finalize(
        "artifacts",
        lambda: atomic_write_json(
            attempt_dir / "artifacts.json",
            build_artifact_inventory(attempt_dir / "protocol.jsonl", workspace),
        ),
        process_recorder,
        state,
    )
    findings = _try_finalize(
        "stderr-scan",
        lambda: scan_stderr(attempt_dir / "stderr.log"),
        process_recorder,
        state,
        default=[],
    )
    stderr_counts = summarize_stderr(findings)
    finished_at = _iso_now()

    base_completeness = _try_finalize(
        "base-completeness-validation",
        lambda: _completeness(
            attempt_dir,
            case_dir,
            workspace,
            run_id,
            case_id,
            attempt_id,
            protocol,
            state,
            validate_final_documents=False,
        ),
        process_recorder,
        state,
        default=_incomplete_fallback(state),
    )

    candidate_status = base_completeness["status"]
    if state.finalization_errors and candidate_status == "complete":
        candidate_status = "incomplete"
    result = RunResult(
        run_id=run_id,
        case_id=case_id,
        attempt_id=attempt_id,
        started_at=started_at,
        finished_at=finished_at,
        acp_status=state.acp_status,
        process_exit_code=state.process_exit_code,
        stderr_counts=stderr_counts,
        completeness_status=candidate_status,
    )
    errors_before_run = len(state.finalization_errors)
    _try_finalize(
        "run-final",
        lambda: atomic_write_json(
            attempt_dir / "run.json", _run_payload(result, accumulator, state)
        ),
        process_recorder,
        state,
    )
    if len(state.finalization_errors) != errors_before_run:
        if result.completeness_status == "complete":
            result.completeness_status = "incomplete"
        _try_finalize(
            "run-retry",
            lambda: atomic_write_json(
                attempt_dir / "run.json", _run_payload(result, accumulator, state)
            ),
            process_recorder,
            state,
        )

    manifest.finished_at = finished_at
    manifest.status = (
        "finished"
        if (
            state.process_exit_code is not None
            and state.error is None
            and not state.finalization_errors
        )
        else "incomplete"
    )
    errors_before_manifest = len(state.finalization_errors)
    _try_finalize(
        "manifest-final",
        lambda: atomic_write_json(attempt_dir / "manifest.json", manifest.to_dict()),
        process_recorder,
        state,
    )
    if len(state.finalization_errors) != errors_before_manifest:
        manifest.status = "incomplete"
        if result.completeness_status == "complete":
            result.completeness_status = "incomplete"
        _try_finalize(
            "manifest-retry",
            lambda: atomic_write_json(attempt_dir / "manifest.json", manifest.to_dict()),
            process_recorder,
            state,
        )
        _try_finalize(
            "run-after-manifest-error",
            lambda: atomic_write_json(
                attempt_dir / "run.json", _run_payload(result, accumulator, state)
            ),
            process_recorder,
            state,
        )

    errors_before_final_validation = len(state.finalization_errors)
    completeness = _try_finalize(
        "final-completeness-validation",
        lambda: _completeness(
            attempt_dir,
            case_dir,
            workspace,
            run_id,
            case_id,
            attempt_id,
            protocol,
            state,
            validate_final_documents=True,
        ),
        process_recorder,
        state,
        default=None,
    )
    if completeness is None:
        completeness = _incomplete_fallback(state)
    if len(state.finalization_errors) != errors_before_final_validation:
        result.completeness_status = "incomplete"
        _try_finalize(
            "run-after-validation-error",
            lambda: atomic_write_json(
                attempt_dir / "run.json", _run_payload(result, accumulator, state)
            ),
            process_recorder,
            state,
        )
        manifest.status = "incomplete"
        _try_finalize(
            "manifest-after-validation-error",
            lambda: atomic_write_json(attempt_dir / "manifest.json", manifest.to_dict()),
            process_recorder,
            state,
        )
        revalidated = _try_finalize(
            "final-completeness-revalidation",
            lambda: _completeness(
                attempt_dir,
                case_dir,
                workspace,
                run_id,
                case_id,
                attempt_id,
                protocol,
                state,
                validate_final_documents=True,
            ),
            process_recorder,
            state,
            default=None,
        )
        completeness = (
            revalidated if revalidated is not None else _incomplete_fallback(state)
        )
    result.completeness_status = completeness["status"]
    run_document = _read_json_mapping(attempt_dir / "run.json")
    stored_run_status = (
        run_document.get("completeness_status")
        if run_document is not None
        else None
    )
    if (
        stored_run_status != result.completeness_status
        and _run_status_can_be_synchronized(attempt_dir, completeness)
    ):
        errors_before_run_sync = len(state.finalization_errors)
        _try_finalize(
            "run-completeness-sync",
            lambda: atomic_write_json(
                attempt_dir / "run.json", _run_payload(result, accumulator, state)
            ),
            process_recorder,
            state,
        )
        if len(state.finalization_errors) == errors_before_run_sync:
            synchronized = _try_finalize(
                "run-completeness-sync-validation",
                lambda: _completeness(
                    attempt_dir,
                    case_dir,
                    workspace,
                    run_id,
                    case_id,
                    attempt_id,
                    protocol,
                    state,
                    validate_final_documents=True,
                ),
                process_recorder,
                state,
                default=None,
            )
            completeness = (
                synchronized
                if synchronized is not None
                else _incomplete_fallback(state)
            )
            result.completeness_status = completeness["status"]
        else:
            manifest.status = "incomplete"
            _try_finalize(
                "manifest-after-run-sync-error",
                lambda: atomic_write_json(
                    attempt_dir / "manifest.json", manifest.to_dict()
                ),
                process_recorder,
                state,
            )
    errors_before_completeness = len(state.finalization_errors)
    _try_finalize(
        "completeness-write",
        lambda: atomic_write_json(attempt_dir / "completeness.json", completeness),
        process_recorder,
        state,
    )
    if len(state.finalization_errors) != errors_before_completeness:
        result.completeness_status = "incomplete"
        _try_finalize(
            "run-after-completeness-error",
            lambda: atomic_write_json(
                attempt_dir / "run.json", _run_payload(result, accumulator, state)
            ),
            process_recorder,
            state,
        )
        manifest.status = "incomplete"
        _try_finalize(
            "manifest-after-completeness-error",
            lambda: atomic_write_json(attempt_dir / "manifest.json", manifest.to_dict()),
            process_recorder,
            state,
        )
        completeness = _try_finalize(
            "final-completeness-revalidation",
            lambda: _completeness(
                attempt_dir,
                case_dir,
                workspace,
                run_id,
                case_id,
                attempt_id,
                protocol,
                state,
                validate_final_documents=True,
            ),
            process_recorder,
            state,
            default=_incomplete_fallback(state),
        )
        result.completeness_status = completeness["status"]
        _try_finalize(
            "completeness-retry",
            lambda: atomic_write_json(attempt_dir / "completeness.json", completeness),
            process_recorder,
            state,
        )
    if config.effect_eval_url:
        # This additive integration runs only after the original ACP evidence
        # package has reached its terminal state. Service failures are captured
        # in effect_evaluation.json and never change ACP/completeness outcomes.
        try:
            evaluate_attempt(
                attempt_dir,
                record,
                EffectEvaluationConfig(
                    config.effect_eval_url,
                    timeout_seconds=config.effect_eval_timeout_seconds,
                ),
            )
        except Exception:
            # The evaluator is an optional observer. Even a local persistence
            # failure must not rewrite the already-finalized ACP outcome.
            pass
    return result
