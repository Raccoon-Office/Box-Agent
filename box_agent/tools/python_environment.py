"""Bounded Python discovery and probes shared by environment and execution tools."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Mapping


def python_candidates(runtime_env: Mapping[str, str], workspace: str | None = None) -> list[dict]:
    """Inspect known locations only; never recursively search disks or install."""
    env = {**os.environ, **runtime_env}
    candidates = []
    seen = set()

    def add(value, source):
        if not value:
            return
        path = Path(value).expanduser()
        if not path.is_absolute():
            return
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            candidates.append({"path": str(path), "source": source})

    for name in ("BOX_AGENT_SANDBOX_PYTHON", "BOX_AGENT_BUNDLED_PYTHON",
                 "BOX_AGENT_PYTHON", "BOX_AGENT_PYTHON3"):
        add(env.get(name), "host")
    suffix = Path("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if workspace:
        path = Path(workspace) / ".venv" / suffix
        if path.is_file():
            add(path, "project")
    if env.get("VIRTUAL_ENV"):
        add(Path(env["VIRTUAL_ENV"]) / suffix, "user_venv")
    for name in ("python", "python3"):
        add(shutil.which(name, path=env.get("PATH", "")), "path")
    return candidates


async def probe_python(path: str, modules: list[str] | None = None) -> dict:
    """Run one short-lived process, without installing or importing host fallback packages."""
    modules = list(dict.fromkeys(["ipykernel_launcher", *(modules or [])]))
    if len(modules) > 21 or any(not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", m)
                               for m in modules):
        return {"path": path, "status": "invalid", "error_code": "INVALID_MODULE_NAMES"}
    python = Path(path).expanduser()
    result = {"path": str(python), "status": "unavailable", "executable_ready": False,
              "checked_modules": modules}
    if not python.is_absolute() or not python.is_file():
        return {**result, "error_code": "PYTHON_NOT_FOUND"}
    code = """import importlib, json, sys, sysconfig
missing = []
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)
print(json.dumps({'version': sys.version.split()[0], 'missing_modules': missing,
                  'prefix': sys.prefix, 'packages_path': sysconfig.get_path('purelib')}))
"""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(python), "-E", "-B", "-c", code, json.dumps(modules),
            cwd=str(python.parent),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return {**result, "error_code": "PYTHON_UNRUNNABLE"}
        details = json.loads(stdout.decode("utf-8").splitlines()[-1])
        missing = details["missing_modules"]
        return {
            **result, **details,
            "executable_ready": True,
            "status": "missing_packages" if missing else "ready",
            "kernel_ready": "ipykernel_launcher" not in missing,
            "error_code": ("IPYKERNEL_UNAVAILABLE" if "ipykernel_launcher" in missing
                           else "PYTHON_PACKAGES_UNAVAILABLE" if missing else None),
        }
    except asyncio.TimeoutError:
        return {**result, "error_code": "PYTHON_PROBE_TIMEOUT"}
    except (OSError, ValueError, KeyError, IndexError):
        return {**result, "error_code": "PYTHON_UNRUNNABLE"}
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
