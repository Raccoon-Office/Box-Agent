"""Exercise the suite-wide CLI entry used by CI."""

from pathlib import Path
import subprocess
import sys


def test_behavior_evidence_option_is_accepted_from_suite_root(tmp_path):
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
            "-p", "no:cacheprovider", "-k", "behavior_contracts",
            "--contract-evidence-root", str(tmp_path / "evidence"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/behavior_contracts/test_contracts.py::" in result.stdout
