"""Directory publication must never disguise a failed runtime installation."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.tools import runtime


@pytest.mark.parametrize("platform,error,failures,succeeds", [
    ("nt", 5, 3, True), ("nt", 32, 1, True), ("nt", 33, 2, True),
    ("nt", 5, 4, False), ("nt", 123, 1, False), ("posix", 5, 1, False),
])
def test_directory_publication_retries_only_bounded_windows_lock_errors(
    tmp_path, monkeypatch, platform, error, failures, succeeds,
):
    source, destination = tmp_path / "staged", tmp_path / "version"
    source.mkdir()
    (source / "node").write_text("verified runtime", encoding="utf-8")
    original = Path.rename
    attempts, pauses = [], []

    def rename(path, target):
        attempts.append(path)
        if len(attempts) <= failures:
            failure = PermissionError("injected file-system error")
            failure.winerror = error
            raise failure
        return original(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(runtime, "time", SimpleNamespace(sleep=pauses.append))
    if succeeds:
        runtime._publish_extracted_directory(source, destination)
        assert not source.exists()
        assert (destination / "node").read_text(encoding="utf-8") == "verified runtime"
        assert len(attempts) == failures + 1
    else:
        with pytest.raises(PermissionError):
            runtime._publish_extracted_directory(source, destination)
        assert (source / "node").read_text(encoding="utf-8") == "verified runtime"
        assert not destination.exists()
        assert len(attempts) == (4 if platform == "nt" and error == 5 else 1)
    assert sum(pauses) <= 0.35 + 1e-9
