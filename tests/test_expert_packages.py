"""Official download tickets prepare shared, verified expert resources."""
import asyncio
import hashlib
import io
import json
import logging
import multiprocessing
import os
import shutil
import stat
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

import box_agent.expert_packages as packages
from box_agent.expert_package_format import ExpertPackageError
from box_agent.payload_privacy import DiagnosticFormatter


URL = "https://desktop-skills.tos-s3-cn-shanghai.volces.com/package.zip?signature=TEST-SIGNED-URL-SECRET"


def archive_bytes(kind="expert", *, prefix="", index_change=None, member_change=None, with_index=True):
    manifest = {
        "schemaVersion": "1.0", "packageId": "archive-package-id", "expertType": "agent" if kind == "expert" else "team",
        "entryAgent": "lead", "agents": [{"id": "lead", "file": "agents/lead.md", "skillBindings": [{"key": "method", "required": True}]}],
        "skills": [{"key": "method", "source": "BUNDLED", "path": "skills/skill-0"}],
    }
    members = {
        "agents/lead.md": b"---\nname: lead\n---\nOriginal expert rules.\n",
        "skills/skill-0/SKILL.md": b"---\nname: real-method\ndescription: Example method\n---\nRead the reference.\n",
        "skills/skill-0/references/example.txt": b"Original reference content.\n",
    }
    if kind == "team":
        members.update({"agents/member.md": b"Member rules.\n", "workflows/team.md": b"Delegate and synthesize.\n"})
        manifest["agents"].append({"id": "member", "file": "agents/member.md", "skillBindings": [{"key": "method"}]})
        manifest["team"] = {"leadAgent": "lead", "memberAgents": ["member"], "executionMode": "orchestrated", "workflow": "workflows/team.md"}
    members[".box-agent-plugin/plugin.json"] = json.dumps(manifest).encode()
    records = [{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for path, data in members.items()]
    if index_change:
        records = index_change(records)
    if with_index:
        members["files.json"] = json.dumps(records).encode()
    if member_change:
        member_change(members)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in members.items():
            archive.writestr(prefix + path, data)
    return stream.getvalue()


def reference(body, *, ident="catalog-id", name="Selected expert", **ticket_changes):
    ticket = {"downloadUrl": URL, "digest": hashlib.sha256(body).hexdigest(), "size": len(body)}
    ticket.update(ticket_changes)
    return {"id": ident, "name": name, "packageDownload": ticket}


@pytest.fixture(autouse=True)
def profile_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(home))
    return home


def transport_for(body, requests=None):
    def handle(request):
        if requests is not None:
            requests.append(request)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=body)
    return httpx.MockTransport(handle)


def assert_no_staging(profile_home):
    assert not list((profile_home / "experts/packages").glob(".preparing-*"))


def _prepare_same_archive_in_process(home, body, counter, result_file, gate):
    """Spawn-safe worker: real cache/lock code, deterministic network-free bytes."""
    os.environ["BOX_AGENT_HOME"] = home
    result = Path(result_file)

    async def fixture_download(ticket, destination, transport):
        with Path(counter).open("a", encoding="utf-8") as stream:
            stream.write(str(os.getpid()) + "\n")
        await asyncio.sleep(0.05)
        destination.write_bytes(body)

    packages._download = fixture_download
    result.with_suffix(".ready").write_text("ready")
    try:
        deadline = time.monotonic() + 15
        while not Path(gate).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Cross-process preparation start gate timed out")
            time.sleep(0.01)

        async def run():
            return await asyncio.wait_for(packages.prepare_expert_meta({"expert": reference(body)}), 10)

        profile = asyncio.run(run())["expert"]
        result.write_text(json.dumps({"directory": profile["packageSnapshot"]["directory"], "id": profile["id"]}))
    except BaseException as error:
        result.write_text(json.dumps({"error_type": type(error).__name__, "error": str(error)}))
        raise


@pytest.mark.asyncio
@pytest.mark.parametrize("key,kind", [("expert", "expert"), ("expert_team", "team"), ("expertTeam", "team")])
@pytest.mark.parametrize("prefix", ["", "wrapper/"])
async def test_real_archive_format_builds_full_profile_in_shared_cache(profile_home, key, kind, prefix):
    body = archive_bytes(kind, prefix=prefix)
    selection = reference(body)
    meta = {key: selection, "unrelated": {"preserved": True}}
    result = await packages.prepare_expert_meta(meta, transport=transport_for(body))
    profile = result[key]
    assert meta[key] is selection and "packageSnapshot" not in selection
    assert result["unrelated"] is meta["unrelated"]
    assert profile["id"] == selection["id"]
    assert profile["revision"] == "package:" + selection["packageDownload"]["digest"]
    snapshot = profile["packageSnapshot"]
    root = Path(snapshot["directory"])
    assert root.is_relative_to(profile_home / "experts/packages")
    assert root.name == ("wrapper" if prefix else "content")
    assert (root / "skills/skill-0/references/example.txt").read_text() == "Original reference content.\n"
    assert snapshot["skills"][0]["slug"] == "real-method"
    assert snapshot["skills"][0]["directory"] == str(root / "skills/skill-0")
    assert "packageDownload" not in profile
    assert URL not in json.dumps(result)
    if kind == "expert":
        assert profile["instructions"] == ["Original expert rules."]
    else:
        assert profile["leader"]["id"] == "lead"
        assert [member["id"] for member in profile["members"]] == ["member"]
        assert profile["workflow"] == ["Delegate and synthesize."]
    assert_no_staging(profile_home)


@pytest.mark.asyncio
async def test_cache_reuse_does_not_repeat_download_and_rebuilds_selection_metadata(profile_home):
    body = archive_bytes()
    requests = []
    transport = transport_for(body, requests)
    first = await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport)
    second = await packages.prepare_expert_meta({"expert": reference(body, ident="another-id", name="Another name", downloadUrl=URL + "-renewed")}, transport=transport)
    assert len(requests) == 1
    assert first["expert"]["packageSnapshot"]["directory"] == second["expert"]["packageSnapshot"]["directory"]
    assert second["expert"]["id"] == "another-id"
    assert second["expert"]["name"] == "Another name"
    assert_no_staging(profile_home)


@pytest.mark.asyncio
async def test_four_concurrent_sessions_download_one_shared_archive(profile_home):
    body = archive_bytes()
    requests = []
    async def handle(request):
        requests.append(request)
        await asyncio.sleep(0.03)
        return httpx.Response(200, content=body)
    transport = httpx.MockTransport(handle)
    results = await asyncio.gather(*(packages.prepare_expert_meta({"expert": reference(body, ident=f"selection-{i}")}, transport=transport) for i in range(4)))
    assert len(requests) == 1
    assert len({r["expert"]["packageSnapshot"]["directory"] for r in results}) == 1
    assert [r["expert"]["id"] for r in results] == [f"selection-{i}" for i in range(4)]
    assert_no_staging(profile_home)


def test_two_spawned_processes_prepare_one_shared_archive(profile_home, tmp_path):
    body = archive_bytes()
    counter = tmp_path / "download-count.txt"
    gate = tmp_path / "start-preparation"
    result_files = [tmp_path / f"worker-{index}.json" for index in range(2)]
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(
        target=_prepare_same_archive_in_process,
        args=(str(profile_home), body, str(counter), str(result), str(gate)),
    ) for result in result_files]
    try:
        for process in processes:
            process.start()
        deadline = time.monotonic() + 20
        while not all(result.with_suffix(".ready").is_file() for result in result_files):
            assert all(process.exitcode is None for process in processes), "Preparation worker exited before start"
            assert time.monotonic() < deadline, "Preparation workers did not become ready"
            time.sleep(0.01)
        gate.write_text("start")
        for process in processes:
            process.join(timeout=max(0, deadline - time.monotonic()))
            assert not process.is_alive(), "Preparation worker exceeded the test deadline"
            assert process.exitcode == 0
        results = [json.loads(result.read_text()) for result in result_files]
        assert all(result.get("id") == "catalog-id" and "error" not in result for result in results)
        assert len({result["directory"] for result in results}) == 1
        assert len(counter.read_text().splitlines()) == 1
        assert_no_staging(profile_home)
    finally:
        for process in processes:
            if process.pid is None:
                continue
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
            process.close()


@pytest.mark.asyncio
async def test_existing_complete_objects_are_not_downloaded_or_rewritten():
    meta = {"expert": {"id": "custom-id", "name": "User expert", "instructions": ["Original user rules."]},
            "expert_team": {"id": "user-team", "name": "User team", "workflow": ["User workflow."]}}
    def unexpected(_):
        raise AssertionError("Complete objects must not download")
    assert await packages.prepare_expert_meta(meta, transport=httpx.MockTransport(unexpected)) is meta


@pytest.mark.asyncio
async def test_nonempty_snake_team_takes_precedence_over_ignored_camel_ticket():
    ignored = {"id": "bad", "name": "Ignored", "packageDownload": {"downloadUrl": "http://127.0.0.1/private"}}
    full = {"id": "team", "name": "Full team", "workflow": ["Existing workflow."]}
    meta = {"expert_team": full, "expertTeam": ignored}
    result = await packages.prepare_expert_meta(meta)
    assert result is meta
    assert result["expert_team"] is full


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["expert", "expert_team", "expertTeam"])
async def test_id_alone_is_rejected_instead_of_becoming_a_plain_session(key):
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({key: {"id": "catalog-id"}})
    assert error.value.code == "EXPERT_PACKAGE_REFERENCE_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://desktop-skills.tos-s3-cn-shanghai.volces.com/p.zip",
    "https://untrusted.example/p.zip", "https://127.0.0.1/p.zip",
    "https://desktop-skills.tos-s3-cn-shanghai.volces.com:444/p.zip",
    "https://user:password@desktop-skills.tos-s3-cn-shanghai.volces.com/p.zip",
    "https://user@desktop-skills.tos-s3-cn-shanghai.volces.com/p.zip",
    "https://desktop-skills.tos-s3-cn-shanghai.volces.com/p.zip#fragment",
    "https://desktop-skills.tos-s3-cn-shanghai.volces.com:not-a-port/p.zip",
    "https://desktop-skills.tos-s3-cn-shanghai.volces.com/p.zip\nsecret",
])
async def test_unsafe_download_destinations_are_rejected_before_request(url):
    body = archive_bytes()
    def unexpected(_):
        raise AssertionError("Invalid URL must not make a request")
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body, downloadUrl=url)}, transport=httpx.MockTransport(unexpected))
    assert error.value.code == "EXPERT_PACKAGE_INVALID_REQUEST"
    assert url not in str(error.value)


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_not_followed(profile_home):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
    with pytest.raises(ExpertPackageError):
        await packages.prepare_expert_meta({"expert": reference(archive_bytes())}, transport=httpx.MockTransport(handle))
    assert len(requests) == 1
    assert_no_staging(profile_home)


@pytest.mark.asyncio
async def test_allowed_redirect_does_not_forward_storage_cookies():
    body = archive_bytes()
    requests = []
    def handle(request):
        requests.append(request)
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://sta-volc.xiaohuanxiong.com/package.zip", "set-cookie": "private=do-not-forward"})
        return httpx.Response(200, content=body)
    result = await packages.prepare_expert_meta({"expert": reference(body)}, transport=httpx.MockTransport(handle))
    assert result["expert"]["id"] == "catalog-id"
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"digest": "bad"}, {"digest": "A" * 64}, {"size": 0}, {"size": -1}, {"size": True},
    {"size": 1.0}, {"size": "100"}, {"size": packages._MAX_ARCHIVE_BYTES + 1}, {"id": "other-id"},
])
async def test_ticket_identity_hash_and_size_are_validated_before_download(changes):
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(archive_bytes(), **changes)})
    assert error.value.code == "EXPERT_PACKAGE_INVALID_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("id", "../invalid"), ("name", ""), ("source", "user"), ("description", 1), ("tags", [1])])
async def test_download_reference_metadata_is_not_an_arbitrary_profile(field, value):
    selected = reference(archive_bytes())
    selected[field] = value
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": selected})
    assert error.value.code == "EXPERT_PACKAGE_INVALID_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "instructions", "leader", "members", "packageSnapshot", "defaultSkills",
    "reviewRules", "outputFormat", "teamPersona", "qualityGates", "blockedConditions", "packageReference",
])
async def test_download_ticket_cannot_silently_override_or_merge_full_profile(field):
    selected = reference(archive_bytes())
    selected[field] = []
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": selected})
    assert error.value.code == "EXPERT_PACKAGE_INVALID_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"digest": "0" * 64}, {"size_delta": -1}, {"size_delta": 1}])
async def test_received_archive_must_match_declared_hash_and_size(profile_home, changes):
    body = archive_bytes()
    changes = dict(changes)
    if "size_delta" in changes:
        changes["size"] = len(body) + changes.pop("size_delta")
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body, **changes)}, transport=transport_for(body))
    assert error.value.code == "EXPERT_PACKAGE_HASH_MISMATCH"
    assert_no_staging(profile_home)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not-a-zip", archive_bytes(with_index=False), archive_bytes(index_change=lambda records: {})])
async def test_invalid_archives_and_missing_file_manifests_do_not_publish_cache(profile_home, body):
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body))
    assert error.value.code == "EXPERT_PACKAGE_INVALID"
    assert not (profile_home / "experts/packages" / hashlib.sha256(body).hexdigest()).exists()
    assert_no_staging(profile_home)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    archive_bytes(index_change=lambda records: [{**r, "sha256": "0" * 64} for r in records]),
    archive_bytes(index_change=lambda records: [{**r, "size": r["size"] + 1} for r in records]),
    archive_bytes(index_change=lambda records: [{**records[0], "path": "../outside"}]),
])
async def test_file_index_checksums_sizes_and_paths_are_enforced(profile_home, body):
    with pytest.raises(ExpertPackageError):
        await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body))
    assert_no_staging(profile_home)


@pytest.mark.asyncio
async def test_wrong_expert_type_is_not_published_in_shared_cache(profile_home):
    body = archive_bytes("team")
    with pytest.raises(ExpertPackageError, match="type"):
        await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body))
    assert not (profile_home / "experts/packages" / hashlib.sha256(body).hexdigest()).exists()
    assert_no_staging(profile_home)


def unsafe_archive(name, *, mode=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo(name)
        if mode is not None:
            info.create_system = 3
            info.external_attr = mode << 16
        archive.writestr(info, b"unsafe member")
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,mode", [
    ("../outside", None), ("/absolute", None), ("a\\b", None), ("C:/host", None),
    ("a/../outside", None), ("./relative", None), ("a//b", None),
    ("link", stat.S_IFLNK | 0o777), ("fifo", stat.S_IFIFO | 0o600),
])
async def test_zip_paths_and_special_members_cannot_escape_extraction(profile_home, name, mode):
    body = unsafe_archive(name, mode=mode)
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body))
    assert error.value.code == "EXPERT_PACKAGE_INVALID"
    assert_no_staging(profile_home)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,value", [("_MAX_EXPANDED_BYTES", 50), ("_MAX_FILE_BYTES", 8), ("_MAX_ENTRIES", 1)])
async def test_archive_expansion_bounds_are_checked_before_extracting(profile_home, monkeypatch, limit, value):
    body = archive_bytes()
    monkeypatch.setattr(packages, limit, value)
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body))
    assert error.value.code == "EXPERT_PACKAGE_INVALID"
    assert_no_staging(profile_home)


@pytest.mark.asyncio
async def test_network_failure_does_not_leak_signed_url_or_keep_partial_cache(profile_home, caplog, monkeypatch):
    # Match the ACP/CLI diagnostic output; raw log records intentionally stay unchanged.
    monkeypatch.setattr(caplog.handler, "formatter", DiagnosticFormatter(delegate=caplog.handler.formatter))
    body = archive_bytes()
    def handle(request):
        raise httpx.ConnectError("failed fetching " + str(request.url), request=request)
    with caplog.at_level(logging.DEBUG), pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body)}, transport=httpx.MockTransport(handle))
    assert error.value.code == "EXPERT_PACKAGE_DOWNLOAD_FAILED"
    assert "TEST-SIGNED-URL-SECRET" not in str(error.value)
    assert "TEST-SIGNED-URL-SECRET" not in caplog.text
    assert_no_staging(profile_home)


@pytest.mark.asyncio
async def test_http_failure_is_safe_and_later_retry_can_acquire_same_lock(profile_home, caplog, monkeypatch):
    # Match the ACP/CLI diagnostic output; raw log records intentionally stay unchanged.
    monkeypatch.setattr(caplog.handler, "formatter", DiagnosticFormatter(delegate=caplog.handler.formatter))
    body = archive_bytes()
    with caplog.at_level(logging.DEBUG), pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body)}, transport=httpx.MockTransport(lambda _: httpx.Response(403)))
    assert error.value.code == "EXPERT_PACKAGE_DOWNLOAD_FAILED"
    assert "TEST-SIGNED-URL-SECRET" not in caplog.text + str(error.value)
    assert any(record.name == "httpx" for record in caplog.records)
    assert "<private diagnostic payload omitted>" in caplog.text
    assert_no_staging(profile_home)
    result = await asyncio.wait_for(packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body)), 3)
    assert result["expert"]["id"] == "catalog-id"


@pytest.mark.asyncio
async def test_preparation_timeout_removes_staging_and_releases_lock(profile_home, monkeypatch):
    body = archive_bytes()
    async def slow(_):
        await asyncio.sleep(10)
        return httpx.Response(200, content=body)
    monkeypatch.setattr(packages, "_PREPARE_TIMEOUT", 0.02)
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body)}, transport=httpx.MockTransport(slow))
    assert error.value.code == "EXPERT_PACKAGE_PREPARATION_TIMEOUT"
    assert_no_staging(profile_home)
    monkeypatch.setattr(packages, "_PREPARE_TIMEOUT", 3)
    assert (await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body)))["expert"]["id"] == "catalog-id"


@pytest.mark.asyncio
async def test_cancelled_download_removes_staging_and_releases_lock(profile_home):
    body = archive_bytes()
    started = asyncio.Event()
    async def slow(_):
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, content=body)
    task = asyncio.create_task(packages.prepare_expert_meta({"expert": reference(body)}, transport=httpx.MockTransport(slow)))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_no_staging(profile_home)
    assert (await asyncio.wait_for(packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body)), 3))["expert"]["id"] == "catalog-id"


@pytest.mark.asyncio
async def test_cancelled_partial_stream_is_removed_before_retry(profile_home):
    body = archive_bytes()
    yielded = asyncio.Event()

    class PartialStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body[:100]
            yielded.set()
            await asyncio.sleep(10)
            yield body[100:]

    task = asyncio.create_task(packages.prepare_expert_meta(
        {"expert": reference(body)},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=PartialStream())),
    ))
    await asyncio.wait_for(yielded.wait(), 2)
    partials = list((profile_home / "experts/packages").glob(".preparing-*/package.zip"))
    assert len(partials) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_no_staging(profile_home)
    assert (await asyncio.wait_for(packages.prepare_expert_meta(
        {"expert": reference(body)}, transport=transport_for(body),
    ), 3))["expert"]["id"] == "catalog-id"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["open", "write", "close"])
@pytest.mark.parametrize("cancel", [False, True], ids=["complete", "cancel"])
async def test_slow_archive_io_keeps_loop_responsive_and_owns_stream_until_cleanup(
    profile_home, monkeypatch, phase, cancel,
):
    body = archive_bytes()
    entered, release = threading.Event(), threading.Event()
    closed = threading.Event()
    main_thread = threading.get_ident()
    io_threads = []
    original_open = Path.open

    def wait_for_release():
        io_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3), "Archive I/O blocked the event loop"

    class Stream:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def write(self, chunk):
            if phase == "write":
                wait_for_release()
            assert not self.wrapped.closed
            return self.wrapped.write(chunk)

        def close(self):
            if phase == "close":
                wait_for_release()
            self.wrapped.close()
            closed.set()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    def open_archive(path, *args, **kwargs):
        opened = original_open(path, *args, **kwargs)
        if path.name != "package.zip" or args != ("xb",):
            return opened
        wrapped = Stream(opened)
        if phase == "open":
            wait_for_release()
        return wrapped

    monkeypatch.setattr(Path, "open", open_archive)
    task = asyncio.create_task(packages.prepare_expert_meta(
        {"expert": reference(body)}, transport=transport_for(body),
    ))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        # This coroutine must advance while the disk worker is still blocked.
        assert not task.done()
        assert io_threads and main_thread not in io_threads
        assert not closed.is_set()
        if cancel:
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            assert list((profile_home / "experts/packages").glob(".preparing-*"))
            assert not closed.is_set()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert not (profile_home / "experts/packages" / hashlib.sha256(body).hexdigest()).exists()
        else:
            result = await asyncio.wait_for(task, 3)
            assert result["expert"]["id"] == "catalog-id"
        assert closed.is_set()
        assert_no_staging(profile_home)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    # Closing and removing the cancelled writer must release the shared cache lock.
    monkeypatch.setattr(Path, "open", original_open)
    assert (await asyncio.wait_for(packages.prepare_expert_meta(
        {"expert": reference(body)}, transport=transport_for(body),
    ), 3))["expert"]["id"] == "catalog-id"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2, 3])
async def test_cancelled_disk_worker_finishes_before_staging_is_removed(profile_home, monkeypatch, cancel_count):
    body = archive_bytes()
    entered, release = threading.Event(), threading.Event()
    original = packages._extract
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(packages, "_extract", blocked)
    task = asyncio.create_task(packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body)))
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
    for _ in range(cancel_count):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert_no_staging(profile_home)
    monkeypatch.setattr(packages, "_extract", original)
    assert (await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body)))["expert"]["id"] == "catalog-id"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["archive", "resource", "resource-missing", "extra-script"])
async def test_tampered_cache_is_rejected_without_trusting_its_receipt(profile_home, change):
    body = archive_bytes()
    result = await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body))
    root = Path(result["expert"]["packageSnapshot"]["directory"])
    if change == "archive":
        target = profile_home / "experts/packages" / hashlib.sha256(body).hexdigest() / "package.zip"
        raw = target.read_bytes()
        target.write_bytes(b"X" + raw[1:])
    elif change == "resource":
        target = root / "skills/skill-0/references/example.txt"
        raw = target.read_bytes()
        target.write_bytes(b"X" + raw[1:])
    elif change == "resource-missing":
        (root / "skills/skill-0/references/example.txt").unlink()
    else:
        scripts = root / "skills/skill-0/scripts"
        scripts.mkdir()
        (scripts / "injected.py").write_text("print('not in original package')")
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body)})
    assert error.value.code == "EXPERT_PACKAGE_CACHE_INVALID"


@pytest.mark.asyncio
async def test_cache_entry_cannot_be_a_symlink_to_another_directory(profile_home, tmp_path):
    body = archive_bytes()
    cache = profile_home / "experts/packages"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    (cache / hashlib.sha256(body).hexdigest()).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body)})
    assert error.value.code == "EXPERT_PACKAGE_CACHE_INVALID"


@pytest.mark.asyncio
async def test_wrapped_cache_content_cannot_escape_through_a_root_symlink(profile_home, tmp_path):
    body = archive_bytes(prefix="wrapper/")
    await packages.prepare_expert_meta({"expert": reference(body)}, transport=transport_for(body))
    content = profile_home / "experts/packages" / hashlib.sha256(body).hexdigest() / "content"
    outside = tmp_path / "outside-content"
    shutil.move(str(content), outside)
    content.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExpertPackageError) as error:
        await packages.prepare_expert_meta({"expert": reference(body)})
    assert error.value.code == "EXPERT_PACKAGE_CACHE_INVALID"
