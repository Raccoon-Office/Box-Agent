import hashlib
import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

from box_agent.tools import cua_install as installer


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setenv("RACCOON_CUA_RESOURCE_DIR", str(tmp_path / "shared"))
    monkeypatch.delenv("RACCOON_CUA_DOWNLOAD_BASE_URL", raising=False)
    return tmp_path / "shared"


def test_default_resource_directory_does_not_follow_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("RACCOON_CUA_RESOURCE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "desktop-profile"))
    expected = tmp_path / ".box-agent/resources/cua-driver"
    assert installer.shared_root() == expected
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "cli-profile"))
    assert installer.shared_root() == expected


@pytest.mark.parametrize("system,machine,target,suffix", [
    ("darwin", "arm64", "darwin-universal", ".tar.gz"),
    ("darwin", "x86_64", "darwin-universal", ".tar.gz"),
    ("linux", "aarch64", "linux-arm64", "-binary.tar.gz"),
    ("linux", "x86_64", "linux-x86_64", "-binary.tar.gz"),
    ("win32", "AMD64", "windows-x86_64", "-binary.zip"),
    ("win32", "ARM64", "windows-arm64", "-binary.zip"),
])
def test_release_matches_desktop_assets(monkeypatch, system, machine, target, suffix):
    monkeypatch.setattr(installer.sys, "platform", system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    url, digest = installer.release("https://cdn.example/cua")
    assert url == f"https://cdn.example/cua/cua-driver-rs-v0.28.2/cua-driver-rs-0.28.2-{target}{suffix}"
    assert len(digest) == 64
    assert installer.release()[0].startswith("https://github.com/trycua/cua/releases/download/")


@pytest.mark.parametrize("base", ["http://cdn.example", "https://user:secret@cdn.example", "https://cdn.example?q=1", "https://cdn.example/#a"])
def test_unsafe_download_sources_rejected(base):
    with pytest.raises(ValueError):
        installer.release(base)


@pytest.mark.parametrize("name", ["../outside", "/outside", "C:/outside", "a\\b", "a/../../b"])
def test_archive_paths_cannot_escape(name):
    with pytest.raises(ValueError):
        installer.safe_path(name)


def test_shared_pointer_cannot_escape(isolated_root):
    isolated_root.mkdir()
    (isolated_root / "current.json").write_text(json.dumps({"directory": "../outside"}))
    assert installer.shared_driver() is None


def test_cli_discovers_desktop_published_resource(isolated_root, monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    directory = "0.28.2-linux-x64-aabb-1234"
    driver = isolated_root / directory / "cua-driver"
    driver.parent.mkdir(parents=True)
    driver.write_bytes(b"fixture")
    (isolated_root / "current.json").write_text(json.dumps({"directory": directory}))
    from box_agent.plugins.cua.driver import installed_driver
    assert installed_driver() == str(driver)


def test_https_redirect_cannot_downgrade():
    with pytest.raises(ValueError, match="HTTPS"):
        installer.HttpsRedirect().redirect_request(None, None, 302, "", {}, "http://example.com/asset")


def tar_bytes(link=False):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        entry = tarfile.TarInfo("cua-driver")
        if link:
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/outside"
            archive.addfile(entry)
        else:
            entry.size = 7
            archive.addfile(entry, io.BytesIO(b"fixture"))
    return result.getvalue()


def test_tar_links_rejected(tmp_path):
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(tar_bytes(link=True))
    with pytest.raises(ValueError):
        installer.extract(archive, tmp_path / "output")


def test_windows_zip_extracted_without_shell(tmp_path):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("payload/cua-driver.exe", b"fixture")
    installer.extract(archive, tmp_path / "output")
    assert (tmp_path / "output/payload/cua-driver.exe").read_bytes() == b"fixture"


@pytest.mark.parametrize("broken_existing", [False, True])
def test_install_publishes_verified_resource_and_second_install_reuses(isolated_root, monkeypatch, broken_existing):
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    payload = tar_bytes()
    url = "https://cdn.example/archive"
    monkeypatch.setattr(installer, "release", lambda base: (url, hashlib.sha256(payload).hexdigest()))
    checks = []
    previous = isolated_root / "0.28.2-linux-x64-aabb-1234" / "cua-driver"
    if broken_existing:
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b"broken driver")
        (isolated_root / "current.json").write_text(json.dumps({"directory": previous.parent.name}))
    def verify(driver):
        if driver == previous:
            raise ValueError("CUA driver must be version 0.28.2")
        checks.append(driver)
    monkeypatch.setattr(installer, "verify", verify)
    class Response(io.BytesIO):
        pass
    calls = []
    class Opener:
        def open(self, address, timeout):
            calls.append(address)
            response = Response(payload)
            response.url = address
            return response
    monkeypatch.setattr(installer, "build_opener", lambda *args: Opener())
    driver = installer.install_cua()
    assert driver.read_bytes() == b"fixture"
    assert installer.shared_driver() == driver
    assert installer.install_cua() == driver
    assert calls == [url]
    assert len(checks) == 2
    assert not list(isolated_root.glob(".install-*"))
    if broken_existing:
        assert previous.read_bytes() == b"broken driver"


@pytest.mark.parametrize("repair", [False, True])
def test_checksum_failure_does_not_publish_or_fall_back(isolated_root, monkeypatch, repair):
    monkeypatch.setenv("RACCOON_CUA_DOWNLOAD_BASE_URL", "https://cdn.example/cua/")
    pointer = isolated_root / "current.json"
    if repair:
        old = isolated_root / "old-driver"
        old.parent.mkdir()
        old.write_text("broken")
        pointer.write_text('{"directory":"old"}')
        monkeypatch.setattr(installer, "shared_driver", lambda: old)
        def invalid(driver):
            raise ValueError("Invalid driver")
        monkeypatch.setattr(installer, "verify", invalid)
    calls = []
    class Response(io.BytesIO):
        pass
    class Opener:
        def open(self, address, timeout):
            calls.append(address)
            response = Response(b"bad archive")
            response.url = address
            return response
    monkeypatch.setattr(installer, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="SHA-256"):
        installer.install_cua()
    assert len(calls) == 1 and calls[0].startswith("https://cdn.example/")
    if repair:
        assert pointer.read_text() == '{"directory":"old"}'
        assert old.read_text() == "broken"
    else:
        assert not pointer.exists()
    assert not list(isolated_root.glob(".install-*"))


def test_cli_install_runs_without_agent_or_model_config(monkeypatch, capsys):
    from box_agent import cli
    monkeypatch.setattr("sys.argv", ["box-agent", "install-cua"])
    monkeypatch.setattr(installer, "install_cua", lambda: Path("/fixture/cua-driver"))
    assert cli.main() == 0
    assert "CuaDriver ready" in capsys.readouterr().out


def test_cli_install_reports_failure(monkeypatch, capsys):
    from box_agent import cli
    monkeypatch.setattr("sys.argv", ["box-agent", "install-cua"])
    def fail():
        raise ValueError("SHA-256 mismatch")
    monkeypatch.setattr(installer, "install_cua", fail)
    assert cli.main() == 1
    assert "SHA-256 mismatch" in capsys.readouterr().out
