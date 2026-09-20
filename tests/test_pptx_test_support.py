"""Missing optional hosts must not hide real PPTX script failures."""

import subprocess
from functools import partial

import pytest

from tests import (
    test_pptx_controlled_deck as controlled,
    test_pptx_design_plan as design,
    test_pptx_html_export as export,
    test_pptx_presentation_system as presentation,
)


@pytest.fixture(params=[controlled._run, design.run, export._run_node, presentation.run, presentation.node])
def runner(request, monkeypatch, tmp_path):
    for module in (controlled, design, export, presentation):
        monkeypatch.setattr(module, "NODE", "node")
    if request.param is design.run:
        return partial(design.run, cwd=tmp_path)
    return request.param


@pytest.mark.parametrize("message", [
    "Cannot find module 'playwright'",
    "Missing dependency: playwright",
    "Missing npm package: playwright",
    "browserType.launch: Executable doesn't exist at /missing/chromium",
    "Playwright Chromium is not available",
    "Chromium executable not found at /missing/chromium",
    "Chromium executable not found under /missing/browsers",
    "Cannot find module '@napi-rs/canvas'",
    "Missing dependency: @napi-rs/canvas",
])
@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_runners_skip_only_unavailable_optional_hosts(runner, monkeypatch, message, channel):
    output = {"stdout": "", "stderr": "", channel: message}
    result = subprocess.CompletedProcess(["node"], 1, **output)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)

    with pytest.raises(pytest.skip.Exception, match="Managed"):
        runner("unused.js")


@pytest.mark.parametrize("message", [
    "TypeError: cannot read properties of undefined",
    "Cannot find module './presentation-system.js'",
    "browserType.launch: Target page, context or browser has been closed",
    "QA failed: text/content overflow",
    "Result: BLOCKED for CLI HTML-to-editable-PPTX export.",
    "Missing bundled converter.",
    "Missing npm package: playwright\nMissing bundled converter.",
    "Chromium executable not found at /missing/chromium\nMissing bundled converter.",
])
def test_runners_preserve_real_script_failures(runner, monkeypatch, message):
    result = subprocess.CompletedProcess(["node"], 1, stdout="", stderr=message)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)

    if runner is presentation.node:
        with pytest.raises(AssertionError, match=message.splitlines()[0]):
            runner("unused.js")
    else:
        assert runner("unused.js") is result


def test_runners_do_not_skip_successful_results(runner, monkeypatch):
    result = subprocess.CompletedProcess(
        ["node"], 0, stdout='{"ok": true}', stderr="Missing dependency: playwright"
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)

    actual = runner("unused.js")
    assert actual == ({"ok": True} if runner is presentation.node else result)
