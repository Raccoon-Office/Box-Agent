"""Private, one-attempt history importer. No model, tools or ACP server startup."""
from __future__ import annotations

import json
import os
import select
import sys
import threading
import time


def _watch_parent(parent: int) -> None:
    """Arm parent-death detection before accepting any write authority."""
    if os.getppid() != parent:
        raise RuntimeError("Import parent changed before startup")
    if sys.platform == "darwin":
        queue = select.kqueue()
        queue.control([select.kevent(parent, filter=select.KQ_FILTER_PROC,
                                     flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                     fflags=select.KQ_NOTE_EXIT)], 0, 0)

        def watch():
            queue.control(None, 1, None)
            os._exit(1)

        threading.Thread(target=watch, daemon=True, name="import-parent").start()
    elif sys.platform.startswith("linux"):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, 9, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG, SIGKILL
            raise OSError(ctypes.get_errno(), "Unable to guard import parent")
        if os.getppid() != parent:
            os._exit(1)
    # Windows workers are assigned to the parent's kill-on-close Job before
    # this initialization frame is sent. They cannot start work before it.


def main() -> None:
    output = sys.stdout
    sys.stdout = sys.stderr

    def send(message):
        output.write(json.dumps(message, separators=(",", ":")) + "\n")
        output.flush()

    raw = sys.stdin.buffer.readline(4096)
    if not raw.endswith(b"\n"):
        return
    request = json.loads(raw)
    from .session_import import (
        HistoryImportCancelled, _locked_staging, _remove_staging, _uuid, import_staged_history,
    )
    from .session_log import SessionLogInUseError
    from .user_paths import state_path

    token, attempt = _uuid(request["token"]), _uuid(request["attemptId"])
    _watch_parent(request["parentPid"])
    stopped = threading.Event()

    def control():
        while True:
            line = sys.stdin.buffer.readline(4096)
            if not line:
                # The owner is gone: do not finish publishing behind its back.
                os._exit(1)
            if not line.endswith(b"\n"):
                os._exit(1)
            try:
                if json.loads(line) == {"cancel": attempt}:
                    stopped.set()
            except (ValueError, TypeError):
                stopped.set()

    threading.Thread(target=control, daemon=True, name="import-control").start()
    last_sent = 0.0
    counts: dict[str, int] = {}

    def progress(stage, completed):
        nonlocal last_sent
        if completed <= counts.get(stage, 0):
            return
        counts[stage] = completed
        now = time.monotonic()
        if now - last_sent >= .25:
            send({"progress": {"stage": stage, "completed": completed}})
            last_sent = now

    try:
        marker = state_path("history-import-jobs") / f"{token}.json"
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _locked_staging(marker) as staging:
            try:
                with marker.open("w", encoding="utf-8") as stream:
                    json.dump({"token": token, "attemptId": attempt, "pid": os.getpid(),
                               "parentPid": request["parentPid"]}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                result = import_staged_history(token, report=progress, cancelled=stopped.is_set,
                                               worker_staging=staging)
            finally:
                _remove_staging(staging)
                marker.unlink(missing_ok=True)
        send({"result": result})
    except HistoryImportCancelled:
        send({"error": "HISTORY_IMPORT_CANCELLED"})
    except SessionLogInUseError:
        send({"error": "HISTORY_IMPORT_BUSY"})
    except Exception:
        send({"error": "HISTORY_IMPORT_FAILED"})
    # Avoid joining a stdin reader at interpreter shutdown. All owned files and
    # locks have already been closed; a blocked cleanup is handled by the parent.
    output.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
