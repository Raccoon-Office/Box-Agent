from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
PPTX_SCRIPTS = (
    REPO_ROOT / "box_agent" / "skills" / "document-skills" / "pptx" / "scripts"
)
SEARCH_SCRIPT = PPTX_SCRIPTS / "search_free_image.py"
INSPECT_SCRIPT = PPTX_SCRIPTS / "inspect_deck_contract.js"
SYNC_SCRIPT = PPTX_SCRIPTS / "sync_image_manifest_status.js"
VALIDATE_SCRIPT = PPTX_SCRIPTS / "validate_image_manifest.js"


def _load_search_module():
    spec = importlib.util.spec_from_file_location("pptx_free_image_search", SEARCH_SCRIPT)
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
                            "tier": "free",
                            "providers": ["openverse", "wikimedia"],
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


def test_free_search_promotes_sourced_image_and_records_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_search_module()
    manifest_path = _write_manifest(tmp_path)
    candidate = module.Candidate(
        provider="openverse",
        title="Neymar playing for Barcelona",
        author="Example Author",
        source_page_url="https://example.test/source",
        download_url="https://example.test/original.jpg",
        license_name="CC0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        width=1600,
        height=1000,
    )
    monkeypatch.setitem(module.PROVIDERS, "openverse", lambda _query, _timeout: [candidate])

    def fake_download(_candidate, target_path, *, placement, timeout):
        assert placement == "fixed-frame"
        assert timeout == 30
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"normalized-image")
        return 1600, 1000

    monkeypatch.setattr(module, "_download_and_normalize", fake_download)

    result = module.process_manifest(manifest_path, provider_names=("openverse",))

    assert result["sourced"] == 1
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = payload["image_plan"][0]
    assert entry["decision"] == "use_existing"
    assert entry["status"] == "ready"
    assert entry["resolved_via"] == "web"
    assert entry["origin"] == "sourced"
    assert entry["output_path"] == "assets/source/slide-01-hero.jpg"
    assert entry["search"]["status"] == "sourced"
    assert entry["source"]["provider"] == "openverse"
    assert entry["source"]["license_tier"] == "no-attribution"
    node = shutil.which("node")
    if node is not None:
        validated = subprocess.run(
            [node, str(VALIDATE_SCRIPT), str(manifest_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert validated.returncode == 0, validated.stdout + validated.stderr
        assert json.loads(validated.stdout)["successfulSourcedCount"] == 1


def test_free_search_exhaustion_preserves_generation_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_search_module()
    manifest_path = _write_manifest(tmp_path)
    monkeypatch.setitem(module.PROVIDERS, "openverse", lambda _query, _timeout: [])

    result = module.process_manifest(manifest_path, provider_names=("openverse",))

    assert result["exhausted"] == 1
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["image_plan"][0]
    assert entry["decision"] == "generate"
    assert entry["status"] == "pending"
    assert entry["resolved_via"] is None
    assert entry["search"]["status"] == "exhausted"
    assert entry["search"]["fallback"] == "generate"
    repeated = module.process_manifest(manifest_path, provider_names=("openverse",))
    assert repeated["exhausted"] == 0
    assert repeated["skipped"] == 1
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
        validated = subprocess.run(
            [node, str(VALIDATE_SCRIPT), str(manifest_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert validated.returncode == 0, validated.stdout + validated.stderr


def test_free_search_unavailability_is_not_reported_as_empty_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_search_module()
    manifest_path = _write_manifest(tmp_path)

    def unavailable(_query, _timeout):
        raise requests.ConnectionError("offline")

    monkeypatch.setitem(module.PROVIDERS, "openverse", unavailable)

    result = module.process_manifest(manifest_path, provider_names=("openverse",))

    assert result["unavailable"] == 1
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["image_plan"][0]
    assert entry["search"]["status"] == "unavailable"
    assert "offline" in entry["search"]["last_error"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_scaffold_routes_real_photography_to_free_search_first(tmp_path: Path) -> None:
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
    assert entry["search"]["tier"] == "free"
    assert entry["search"]["providers"] == ["openverse", "wikimedia"]
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
