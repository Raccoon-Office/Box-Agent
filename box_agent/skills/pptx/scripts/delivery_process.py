"""One-command parent-death guard using the existing renderer ownership protocol.

This is a short-lived subprocess, never a service. The renderer supervisor owns
workers and registered browser groups; EOF from the invoking CLI cancels it even
when the outer shell force-kills that CLI before cleanup can finish.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading

STANDARD = Path(__file__).resolve().parents[2] / "presentation-suite/skills/sn-ppt-standard/scripts"
sys.path.insert(0, str(STANDARD))
import render_runtime


def main() -> int:
    if sys.argv[1] == "--audit-player":
        # The existing lifecycle uses this marker to select exactly one attempt.
        # It belongs to this worker wrapper, and is never passed to the command.
        environment = dict(os.environ)
        original_limit = environment.pop("PPT_DELIVERY_RENDER_LIMIT", "")
        if original_limit:
            environment["RENDER_GLOBAL_LIMIT"] = original_limit
        else:
            environment.pop("RENDER_GLOBAL_LIMIT", None)
        if any(str(arg).endswith("html_to_pptx.mjs") for arg in sys.argv[2:]):
            wrapper = Path(environment["_PPT_RENDER_DIRECTORY"]) / "export-browser-launch"
            command = " ".join(shlex.quote(value) for value in
                [sys.executable, str(STANDARD / "render_runtime.py"), "--browser"])
            wrapper.write_text('#!/bin/sh\nexec ' + command + ' "$PPT_DELIVERY_BROWSER_EXE" "$@"\n')
            wrapper.chmod(0o700)
            environment["PPT_DELIVERY_BROWSER_GUARD"] = str(wrapper)
        # Inherit this registered worker's group. Its parent-death watcher can
        # reap the command and ordinary descendants without the host's grace.
        raise SystemExit(subprocess.run(sys.argv[2:], env=environment, check=False).returncode)

    parent_fd, timeout = int(sys.argv[1]), float(sys.argv[2])
    os.environ["PPT_DELIVERY_RENDER_LIMIT"] = os.environ.get("RENDER_GLOBAL_LIMIT", "")
    # The formal command is not an additional browser slot. Its original limit
    # is restored in the worker before deck.py starts actual render invocations.
    os.environ["RENDER_GLOBAL_LIMIT"] = "0"

    def watch_parent():
        try:
            while os.read(parent_fd, 1):
                pass
            os.kill(os.getpid(), signal.SIGTERM)
        except OSError:
            pass
        finally:
            os.close(parent_fd)

    threading.Thread(target=watch_parent, daemon=True).start()
    result = render_runtime.run_renderer(__file__, ["--audit-player", *sys.argv[3:]], timeout=timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
