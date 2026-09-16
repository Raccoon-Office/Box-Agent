"""PPT visual review batches must survive bundle sync and byte-limit recovery."""

import hashlib
import json
from pathlib import Path
import runpy

import pytest


REPO = Path(__file__).resolve().parents[1]
SUITE = REPO / "box_agent/skills/presentation-suite"
CONTRACT = "skills/sn-ppt-standard/references/box-agent-tool-contract.md"
OVERLAY = "sequential-ppt-image-inspection"
OLD_POLICY = (
    '视觉模型优先使用 `strategy="native"`，让当前角色直接看新鲜像素。'
    '只有工具明确返回 `IMAGE_NATIVE_UNSUPPORTED` 时才改用 `proxy`，'
    '并在交接中标明这是代理视觉结论。每次最多检查 6 张图；HTML/CSS 修改后必须先重渲再看。'
)


def _sync():
    return runpy.run_path(str(REPO / "scripts/sync_presentation_suite.py"))


def _assert_batch_policy(text):
    assert "每次模型响应最多调用一次 `inspect_images`" in text
    assert "每批最多 4 张图" in text
    assert "不得在同一响应中并行发出多个" in text
    assert "实际返回" in text and "看图形成检查结论" in text
    assert "已覆盖页码或素材 ID" in text and "待检查清单" in text
    assert "后续模型响应" in text
    assert "总览/联系表" in text and "逐页细查" in text
    assert "全部页面" in text
    assert "REQUEST_BODY_TOO_LARGE" in text and "未被模型看到" in text
    assert "2 张、必要时 1 张" in text
    assert "不得降低图片质量" in text
    assert "不得仅为绕过请求字节限制改用 `proxy`" in text
    assert "IMAGE_NATIVE_UNSUPPORTED" in text
    assert "相关高清局部" in text and "尚未检查区域" in text
    assert "局部检查不能冒称整页已覆盖" in text
    assert "每次最多检查 6 张图" not in text


def test_shipped_contract_requires_sequential_review_and_complete_coverage():
    _assert_batch_policy((SUITE / CONTRACT).read_text())


def _previous_bundle(tmp_path):
    sync = _sync()
    bundle = tmp_path / "bundle"
    target = bundle / CONTRACT
    target.parent.mkdir(parents=True)
    target.write_text("# Contract\n\n" + OLD_POLICY + "\n\nKeep overview and detail workflow.\n")
    unchanged = bundle / "untouched.md"
    unchanged.write_text("Preserve other Skill instructions.\n")
    records = {}
    for path in (target, unchanged):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(bundle).as_posix()
        records[relative] = {"source_path": relative, "source_sha256": digest, "sha256": digest}
    provenance = {
        "name": sync["BUNDLE_NAME"], "revision": sync["PINNED_REVISION"],
        "overlays": [name for name in sync["OVERLAYS"] if name != OVERLAY],
        "files": records,
    }
    (bundle / "source.json").write_text(json.dumps(provenance))
    return sync, bundle, provenance


def test_verified_refresh_appends_policy_and_preserves_source_hashes(tmp_path):
    sync, bundle, before = _previous_bundle(tmp_path)
    previous_contract = (bundle / CONTRACT).read_text()
    other_content = (bundle / "untouched.md").read_bytes()
    result = sync["refresh_host_overlays"](bundle)
    _assert_batch_policy((bundle / CONTRACT).read_text())
    assert result["overlays"] == before["overlays"] + [OVERLAY]
    assert result["files"][CONTRACT]["source_sha256"] == before["files"][CONTRACT]["source_sha256"]
    assert result["files"][CONTRACT]["sha256"] == hashlib.sha256((bundle / CONTRACT).read_bytes()).hexdigest()
    assert (bundle / CONTRACT).read_text().startswith(previous_contract.split(OLD_POLICY)[0])
    assert (bundle / CONTRACT).read_text().endswith(previous_contract.split(OLD_POLICY)[1])
    assert (bundle / "untouched.md").read_bytes() == other_content
    first_bytes = {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    assert sync["refresh_host_overlays"](bundle) == result
    assert first_bytes == {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


@pytest.mark.parametrize("drift", ["content", "files", "overlays"])
def test_refresh_rejects_unverified_bundle_without_partial_writes(tmp_path, drift):
    sync, bundle, provenance = _previous_bundle(tmp_path)
    if drift == "content":
        (bundle / CONTRACT).write_text("Local work must remain intact.")
    elif drift == "files":
        (bundle / "local.md").write_text("Local work must remain intact.")
    else:
        provenance["overlays"] = list(reversed(provenance["overlays"]))
        (bundle / "source.json").write_text(json.dumps(provenance))
    before = {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        sync["refresh_host_overlays"](bundle)
    assert before == {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


def test_full_sync_integration_also_applies_batch_policy():
    # Include the pinned text consumed by the existing finalizer overlay too.
    upstream = OLD_POLICY + "\n\n" + (
        "4. 父级重渲并复看。最终 Review 同样由父级先提供新鲜 PNG、Review 子代理集中修复、父级统一重渲/build；"
        "父级统一执行重渲、build、inspect 和 Standard exporter，只有最终 PNG 与 `present.html` 验证通过后才导出，"
        "失败登记 `state.status=partial`。\n导出成功后，按 stdout JSON 的精确 `output` 路径检查文件；"
    )
    result = _sync()["_apply_integration_overlay"](CONTRACT, upstream.encode()).decode()
    _assert_batch_policy(result)
    assert "finalize.py" in result


def test_refresh_rejects_unknown_contract_text_without_partial_writes(tmp_path):
    sync, bundle, provenance = _previous_bundle(tmp_path)
    changed = (bundle / CONTRACT).read_text().replace("每次最多检查 6 张图", "每次最多检查 8 张图")
    (bundle / CONTRACT).write_text(changed)
    provenance["files"][CONTRACT]["sha256"] = hashlib.sha256(changed.encode()).hexdigest()
    (bundle / "source.json").write_text(json.dumps(provenance))
    before = {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="needs review"):
        sync["refresh_host_overlays"](bundle)
    assert before == {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
