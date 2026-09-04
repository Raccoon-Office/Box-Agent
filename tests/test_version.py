from box_agent import _frozen_runtime_version, __version__


def test_source_runtime_keeps_package_version(monkeypatch):
    monkeypatch.delattr("sys.frozen", raising=False)

    assert _frozen_runtime_version("1.2.3") == "1.2.3"
    assert __version__ == "0.9.8"


def test_frozen_runtime_reads_outer_bundle_version(tmp_path, monkeypatch):
    runtime = tmp_path / "box-agent-runtime"
    binary = runtime / "bin" / "box-agent-acp"
    binary.parent.mkdir(parents=True)
    binary.touch()
    (runtime / "VERSION").write_text("0.8.83\n", encoding="utf-8")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(binary))

    assert _frozen_runtime_version("0.8.85") == "0.8.83"
