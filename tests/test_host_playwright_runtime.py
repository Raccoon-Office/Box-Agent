"""Hosted presentation renderers must not select legacy workspace Playwright copies."""

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from box_agent.tools.runtime import SkillRuntimeContext
from box_agent.tools.skill_execution_env import build_skill_execution_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "box_agent/skills/document-skills/pptx/scripts"
STANDARD = ROOT / "box_agent/skills/presentation-suite/skills/sn-ppt-standard/scripts"
NODE = shutil.which("node")


def run_node(code, cwd, env):
    if NODE is None:
        pytest.skip("Node is required")
    return subprocess.run(
        [NODE, "--input-type=module", "-e", code], cwd=cwd,
        env={**os.environ, **env}, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("renderer", ["controlled", "standard"])
@pytest.mark.parametrize("missing", [False, True])
def test_host_sdk_overrides_old_workspace_sdk(tmp_path, renderer, missing):
    old = tmp_path / "node_modules/playwright/index.js"
    old.parent.mkdir(parents=True)
    old.write_text("throw new Error('stale SDK was loaded');")
    host = tmp_path / "host bundle/node_modules/playwright/index.js"
    host.parent.mkdir(parents=True)
    if not missing:
        host.write_text("module.exports={chromium:{source:'host'}};")
    workspace = tmp_path / "mnt/task"
    workspace.mkdir(parents=True)
    # Copy the real loader beside the stale ancestor package to exercise Node resolution.
    source = SCRIPTS / "playwright_host.js" if renderer == "controlled" else STANDARD / "export_pptx/lib/browser_picker.mjs"
    loader = workspace / source.name
    shutil.copyfile(source, loader)
    expression = "loader.loadPlaywright().chromium.source" if renderer == "controlled" else "loader.chromium.source"
    result = run_node(
        f"import * as loader from {json.dumps(loader.as_uri())}; console.log({expression});",
        workspace,
        {"BOX_AGENT_PLAYWRIGHT_MODULE_PATH": str(host), "NODE_PATH": str(old.parent.parent)},
    )
    if missing:
        assert result.returncode != 0
        assert str(host) in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "host"
    assert "stale SDK was loaded" not in result.stderr


@pytest.mark.parametrize("renderer", ["controlled", "standard", "python"])
@pytest.mark.parametrize("missing", [False, True])
def test_host_browser_is_authoritative(tmp_path, monkeypatch, renderer, missing):
    host_browser = tmp_path / "host browser"
    if not missing:
        host_browser.touch()
        host_browser.chmod(0o755)
    env = {"BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": str(host_browser)}
    if renderer == "python":
        monkeypatch.syspath_prepend(str(STANDARD))
        monkeypatch.setenv("BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", str(host_browser))
        namespace = runpy.run_path(str(STANDARD / "render.py"))
        if missing:
            with pytest.raises(namespace["BrowserUnavailable"]):
                namespace["_ensure_browser_available"](SimpleNamespace())
        else:
            assert namespace["_ensure_browser_available"](SimpleNamespace()) == str(host_browser)
        return
    if renderer == "controlled":
        loader = SCRIPTS / "playwright_host.js"
        expression = "loader.chromiumLaunchOptions({}).options.executablePath"
    else:
        loader = STANDARD / "export_pptx/lib/browser_picker.mjs"
        sdk = tmp_path / "sdk.js"
        sdk.write_text("module.exports={chromium:{}};")
        env["BOX_AGENT_PLAYWRIGHT_MODULE_PATH"] = str(sdk)
        expression = "loader.pickBrowserExe()"
    result = run_node(
        f"import * as loader from {json.dumps(loader.as_uri())}; console.log({expression});",
        tmp_path, env,
    )
    if missing:
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(host_browser)


@pytest.mark.parametrize("platform,entry,separator", [
    ("darwin", "/Applications/Host App/browser-tools/node_modules/playwright/index.js", ":"),
    ("win32", r"C:\Host App\browser-tools\node_modules\playwright\index.js", ";"),
    ("linux", "/opt/host/browser-tools/node_modules/playwright/index.js", ":"),
])
def test_skill_subprocess_keeps_host_sdk_first(tmp_path, platform, entry, separator):
    env = build_skill_execution_env(
        SkillRuntimeContext(runtimes={}),
        base_env={"BOX_AGENT_PLAYWRIGHT_MODULE_PATH": entry},
        platform_name=platform, home_dir=tmp_path,
    )
    assert env["BOX_AGENT_PLAYWRIGHT_MODULE_PATH"] == entry
    assert env["NODE_PATH"].split(separator)[0].endswith("node_modules")
    if platform == "linux":
        assert env["NODE_PATH"].startswith("/opt/host/")
    else:
        assert "Host App" in env["NODE_PATH"].split(separator)[0]
