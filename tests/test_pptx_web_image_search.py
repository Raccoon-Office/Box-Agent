from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PPTX_SCRIPTS = (
    REPO_ROOT / "box_agent" / "skills" / "document-skills" / "pptx" / "scripts"
)
LOCALIZE_SCRIPT = PPTX_SCRIPTS / "localize_web_image.py"
INSPECT_SCRIPT = PPTX_SCRIPTS / "inspect_deck_contract.js"
SYNC_SCRIPT = PPTX_SCRIPTS / "sync_image_manifest_status.js"
VALIDATE_SCRIPT = PPTX_SCRIPTS / "validate_image_manifest.js"


def _load_localize_module():
    spec = importlib.util.spec_from_file_location(
        "pptx_web_image_localize",
        LOCALIZE_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_manifest(tmp_path: Path) -> Path:
    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "mode": "auto",
                "image_plan": [
                    {
                        "slide": 1,
                        "slide_id": "slide-01",
                        "decision": "generate",
                        "status": "pending",
                        "acquire_via": "web",
                        "resolved_via": None,
                        "placement": "fixed-frame",
                        "output_path": "assets/generated/slide-01-hero.png",
                        "search": {
                            "provider": "web_search",
                            "search_type": "image",
                            "count": 5,
                            "query": "Neymar Barcelona portrait",
                            "output_path": "assets/source/slide-01-hero.jpg",
                            "status": "pending",
                            "fallback": "generate",
                            "fallback_output_path": "assets/generated/slide-01-hero.png",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest_path


def _write_candidate(tmp_path: Path) -> Path:
    candidate_path = tmp_path / "assets" / "source" / "candidates" / "slide-01.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(
        json.dumps(
            {
                "slide": 1,
                "query": "Neymar Barcelona portrait",
                "reference_tag": "ref_1",
                "title": "Neymar playing for Barcelona",
                "source_url": "https://example.test/source",
                "image": {
                    "url": "https://example.test/original.jpg?signature=a%2Bb",
                    "width": 1600,
                    "height": 1000,
                    "alt": "Neymar playing football",
                    "shape": "横长方形",
                    "clarity": "清晰",
                    "category": "体育",
                    "watermark": "0",
                    "description": "Football player on the pitch",
                    "style_type": "实拍图",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return candidate_path


def test_web_search_candidate_is_localized_and_records_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_localize_module()
    manifest_path = _write_manifest(tmp_path)
    candidate_path = _write_candidate(tmp_path)

    def fake_download(image_url, target_path, *, placement, timeout):
        assert image_url.endswith("?signature=a%2Bb")
        assert placement == "fixed-frame"
        assert timeout == 30
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"normalized-image")
        return 1600, 1000

    monkeypatch.setattr(module, "_download_and_normalize", fake_download)

    result = module.import_candidate(manifest_path, candidate_path)

    assert result["ok"] is True
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = payload["image_plan"][0]
    assert entry["decision"] == "use_existing"
    assert entry["status"] == "ready"
    assert entry["resolved_via"] == "web"
    assert entry["origin"] == "sourced"
    assert entry["output_path"] == "assets/source/slide-01-hero.jpg"
    assert entry["search"]["status"] == "sourced"
    assert entry["source"]["provider"] == "web_search"
    assert entry["source"]["license_status"] == "unverified"
    assert entry["source"]["download_url"].endswith("?signature=a%2Bb")
    assert entry["source"]["width"] == 1600
    assert entry["source"]["height"] == 1000

    node = shutil.which("node")
    if node is not None:
        validated = subprocess.run(
            [node, str(VALIDATE_SCRIPT), str(manifest_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert validated.returncode == 0, validated.stdout + validated.stderr
        report = json.loads(validated.stdout)
        assert report["successfulSourcedCount"] == 1
        assert any("reuse rights are unverified" in item for item in report["warnings"])


def test_web_search_terminal_status_preserves_generation_fallback(tmp_path: Path) -> None:
    module = _load_localize_module()
    manifest_path = _write_manifest(tmp_path)

    result = module.mark_search(
        manifest_path,
        slide=1,
        status="exhausted",
        reason="no usable image passed localization checks",
    )

    assert result["status"] == "exhausted"
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["image_plan"][0]
    assert entry["decision"] == "generate"
    assert entry["status"] == "pending"
    assert entry["resolved_via"] is None
    assert entry["search"]["fallback"] == "generate"
    assert "localization" in entry["search"]["last_error"]

    generated_path = tmp_path / "assets" / "generated" / "slide-01-hero.png"
    generated_path.write_bytes(b"generated-fallback")
    node = shutil.which("node")
    if node is not None:
        synced = subprocess.run(
            [node, str(SYNC_SCRIPT), str(manifest_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert synced.returncode == 0, synced.stdout + synced.stderr
        fallback = json.loads(manifest_path.read_text(encoding="utf-8"))["image_plan"][0]
        assert fallback["resolved_via"] == "ai"
        assert fallback["fallback_used"] is True


def test_web_search_candidate_query_must_match_manifest(tmp_path: Path) -> None:
    module = _load_localize_module()
    manifest_path = _write_manifest(tmp_path)
    candidate_path = _write_candidate(tmp_path)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["query"] = "different subject"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly match"):
        module.import_candidate(manifest_path, candidate_path)

    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["image_plan"][0]
    assert entry["search"]["status"] == "pending"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_scaffold_routes_real_photography_to_web_search_first(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            str(INSPECT_SCRIPT),
            "cover-hero-v1",
            "--title",
            "Neymar Barcelona documentary portrait",
            "--image-mode",
            "auto",
            "--out",
            str(deck_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    entry = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )["image_plan"][0]
    assert entry["decision"] == "generate"
    assert entry["acquire_via"] == "web"
    assert entry["search"]["provider"] == "web_search"
    assert entry["search"]["search_type"] == "image"
    assert entry["search"]["count"] == 5
    assert entry["search"]["fallback"] == "generate"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_scaffold_routes_stylized_illustration_directly_to_generation(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            str(INSPECT_SCRIPT),
            "cover-hero-v1",
            "--title",
            "抽象未来数据浪潮插画",
            "--image-mode",
            "auto",
            "--out",
            str(deck_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    entry = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )["image_plan"][0]
    assert entry["decision"] == "generate"
    assert entry["acquire_via"] == "ai"
    assert "search" not in entry
