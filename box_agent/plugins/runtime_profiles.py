"""Bounded probes of host-provided runtimes, independent of business packages."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any

from .manifest import PluginError

_PYTHON_PROBE = """
import importlib.util, json, sys
missing = [name for name in json.loads(sys.argv[1]) if importlib.util.find_spec(name) is None]
if missing:
    raise RuntimeError('Missing host Python modules: ' + ', '.join(missing))
"""
_NODE_PROBE = r"""
const {createRequire} = require('module');
const path = require('path');
const resolve = createRequire(path.join(process.cwd(), '__box_plugin_probe__.js'));
const request = JSON.parse(process.argv[1]);
(async () => {
  for (const name of request.modules) resolve.resolve(name);
  if (request.browser) {
    const {chromium} = resolve(process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH || 'playwright');
    const executablePath = process.env.BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
    const browser = await chromium.launch({headless: true, ...(executablePath ? {executablePath} : {})});
    try {
      const page = await browser.newPage({viewport: {width: 320, height: 180}});
      await page.setContent('<!doctype html><h1>Box-Agent runtime check</h1>');
      const image = await page.screenshot();
      if (image.length < 100) throw new Error('Browser produced no usable screenshot');
    } finally { await browser.close(); }
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
"""


def _probe(command: list[str], *, timeout: float, profile: str, env: dict[str, str]) -> None:
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env)
        try:
            _, diagnostic = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            import psutil
            try:
                children = psutil.Process(process.pid).children(recursive=True)
                for child in children:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
            finally:
                process.kill()
                process.communicate()
            raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PluginError("runtime_unavailable", f"Runtime profile {profile} could not be verified",
                          {"reason": type(exc).__name__}) from exc
    if process.returncode:
        raise PluginError("runtime_unavailable", f"Runtime profile {profile} failed its host probe",
                          {"diagnostic": diagnostic[-4096:]})


def verify_runtime_profiles(config: Any, required: tuple[str, ...], *,
                            runtime_env: dict[str, str] | None = None) -> None:
    env = {**os.environ, **(runtime_env or {})}
    profiles = getattr(getattr(config, "plugin_platform", None), "runtime_profiles", {})
    for name in required:
        profile = profiles.get(name)
        if profile is None:
            raise PluginError("runtime_profile_missing", f"Host runtime profile is not configured: {name}")
        python_modules = profile.python_modules
        node_modules = profile.node_modules
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", module) for module in python_modules):
            raise PluginError("runtime_profile_invalid", "Invalid Python module declaration")
        if any(not re.fullmatch(r"@?[A-Za-z0-9_][A-Za-z0-9_./@-]{0,255}", module)
               or ".." in module.split("/") for module in node_modules):
            raise PluginError("runtime_profile_invalid", "Invalid Node module declaration")
        if python_modules:
            executable = env.get("BOX_AGENT_PYTHON") or env.get("BOX_AGENT_PYTHON3")
            if not executable and not getattr(sys, "frozen", False):
                executable = sys.executable
            if not executable:
                raise PluginError("runtime_unavailable", "Host Python executable was not provided")
            _probe([executable, "-c", _PYTHON_PROBE, json.dumps(python_modules)],
                   timeout=profile.timeout_seconds, profile=name, env=env)
        if node_modules or profile.playwright_chromium:
            executable = env.get("BOX_AGENT_NODE")
            if not executable:
                raise PluginError("runtime_unavailable", "Host Node executable was not provided")
            _probe([executable, "-e", _NODE_PROBE, json.dumps({"modules": node_modules,
                    "browser": profile.playwright_chromium})], timeout=profile.timeout_seconds, profile=name, env=env)
