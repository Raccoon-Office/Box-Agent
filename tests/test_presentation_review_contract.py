"""Exercise the public Standard export guard, not the Fast exporter."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
STANDARD = Path(os.environ.get(
    "PRESENTATION_STANDARD_SOURCE",
    REPO / "box_agent/skills/presentation-suite/skills/sn-ppt-standard",
))
GUARD = STANDARD / "scripts/export_pptx/lib/cli_guards.mjs"
READY = "## Final review contract\nstatus: ready\nfinal_pixels_inspected: yes\nremaining: none\n"


@pytest.fixture
def deck(tmp_path):
    (tmp_path / "slides").mkdir()
    (tmp_path / "slides/slide_01.html").write_text("<html><h1>Example</h1></html>")
    (tmp_path / "_trace").mkdir()
    return tmp_path


def check(deck, *, force=False, batch=False):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the Standard exporter contract")
    script = """
const {ensureDeckPreconditions} = await import(process.argv[1]);
try {
  const result = ensureDeckPreconditions(process.argv[2], {
    pagesDir: process.argv[2] + '/slides', force: process.argv[3] === 'true', batch: process.argv[4] === 'true'
  });
  console.log(JSON.stringify(result));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
"""
    return subprocess.run(
        [node, "--input-type=module", "-e", script, GUARD.as_uri(), str(deck), str(force).lower(), str(batch).lower()],
        capture_output=True, text=True, check=False,
    )


def test_standard_exports_unique_ready_contract_despite_historical_block_text(deck):
    (deck / "_trace/review-issues.md").write_text("# Review\n旧问题：阻塞，现已修复。\n" + READY)
    result = check(deck)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["htmlFiles"]) == 1


@pytest.mark.parametrize("contract", [
    READY.replace("status: ready", "status: pending_parent_verification"),
    READY.replace("status: ready", "status: blocked"),
    READY.replace("inspected: yes", "inspected: no"),
    READY.replace("remaining: none", "remaining: P7 cropped"),
    READY.replace("final_pixels_inspected: yes\n", ""),
    READY + READY,
    READY + "status: ready\n",
    READY.replace("status: ready", "status:\nready"),
    READY.replace("inspected: yes", "inspected:\nyes"),
    READY.replace("remaining: none", "remaining:\nnone"),
    READY.replace("status: ready", "status: \n\nready"),
    "# Review\nPending; example only:\n```md\n" + READY + "```\n",
    "# Review\n~~~md\n" + READY + "~~~\n",
    "# Review\nAll good, PASS.\n",
])
@pytest.mark.parametrize("force", [False, True])
def test_current_ledger_cannot_be_overridden_by_old_pass_or_force(deck, contract, force):
    (deck / "review.json").write_text('{"status":"PASS"}')
    (deck / "review.md").write_text("status: ready\n")
    (deck / "_trace/review-issues.md").write_text(contract)
    result = check(deck, force=force)
    assert result.returncode != 0
    assert "review-issues.md" in result.stderr


@pytest.mark.parametrize("filename,content", [("review.md", "status: ready\n"), ("review.json", '{"status":"PASS"}')])
def test_legacy_review_remains_supported_when_new_ledger_absent(deck, filename, content):
    (deck / filename).write_text(content)
    result = check(deck)
    assert result.returncode == 0, result.stderr


def test_missing_review_still_blocks_standard_export(deck):
    result = check(deck)
    assert result.returncode != 0
    assert "review" in result.stderr


def test_fenced_example_does_not_duplicate_real_final_contract(deck):
    (deck / "_trace/review-issues.md").write_text("```md\n" + READY + "```\n" + READY)
    result = check(deck)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("force", [False, True])
def test_batch_cannot_bypass_current_pending_review(deck, force):
    (deck / "_trace/review-issues.md").write_text(READY.replace("status: ready", "status: pending_parent_verification"))
    result = check(deck, batch=True, force=force)
    assert result.returncode != 0 and "review-issues.md" in result.stderr


def test_legacy_batch_without_current_ledger_remains_supported(deck):
    result = check(deck, batch=True)
    assert result.returncode == 0, result.stderr
