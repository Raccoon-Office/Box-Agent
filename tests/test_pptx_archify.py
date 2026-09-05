import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest


PPTX = Path(__file__).resolve().parents[1] / "box_agent/skills/document-skills/pptx"
ARCHIFY = PPTX / "vendor/archify"


def test_archify_snapshot_preserves_attribution_and_file_integrity():
    manifest = json.loads((ARCHIFY / "VENDORED.json").read_text())
    assert (ARCHIFY / "LICENSE").is_file()
    assert (ARCHIFY / "THIRD_PARTY_NOTICES.md").is_file()
    assert not (ARCHIFY / "SKILL.md").exists()
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((ARCHIFY / name).read_bytes()).hexdigest() == digest, name


def test_pptx_routes_archify_to_documented_image_supplement():
    skill = (PPTX / "SKILL.md").read_text()
    reference = (PPTX / "references/archify-diagrams.md").read_text()
    assert "references/archify-diagrams.md" in skill
    assert "technical-diagram-v1" in reference
    assert 'fit: "contain"' in reference
    assert "No market installation" in reference


@pytest.mark.parametrize("diagram_type,example", [
    ("architecture", "web-app.architecture.json"),
    ("workflow", "agent-tool-call.workflow.json"),
    ("sequence", "cache-miss-request.sequence.json"),
    ("dataflow", "event-stream.dataflow.json"),
    ("lifecycle", "agent-run.lifecycle.json"),
])
def test_bundled_archify_delivers_and_preserves_last_good_output(tmp_path, diagram_type, example):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for bundled Archify rendering")
    source = tmp_path / "diagram.json"
    source.write_bytes((ARCHIFY / "examples" / example).read_bytes())
    target = tmp_path / "diagram.html"
    command = [node, str(ARCHIFY / "bin/archify.mjs"), "deliver", diagram_type,
               str(source), str(target), "--quality", "showcase", "--json"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    good_output = target.read_bytes()
    assert b"<svg" in good_output
    source.write_text('{"diagram_type": "architecture"}')
    failure = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert failure.returncode != 0
    assert target.read_bytes() == good_output
