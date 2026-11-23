#!/usr/bin/env python3
"""检查 XML 到 PPTX 的本地生成、预览和可选素材能力。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Any
from urllib import parse


DEFAULT_SEARCH_BASE_URL = "https://google.serper.dev"


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _xml_probe(script: Path, command: str, source: Path) -> dict[str, Any]:
    """执行真实入口，避免体检与生成使用不同的运行时或依赖解析。"""
    try:
        result = subprocess.run(
            [sys.executable, str(script), command, str(source)],
            capture_output=True, text=True, timeout=180, check=False,
        )
        if result.returncode:
            return {"status": "failed", "detail": (result.stderr or result.stdout).strip()[:1000]}
        payload = json.loads(result.stdout)
        return {"status": "available", "slides": payload["slides"]}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as exc:
        return {"status": "failed", "detail": str(exc)[:1000]}


def xml_pptx_status(skills_dir: Path) -> dict[str, Any]:
    """在临时目录实际导出和预览，不联网、不改用户现有演示文稿。"""
    script = skills_dir / "sn-ppt-dsl" / "scripts" / "dsl.py"
    if not script.is_file():
        return {"build": {"status": "missing", "detail": "缺少 XML 编译入口"},
                "preview": {"status": "not_run"}}
    with tempfile.TemporaryDirectory(prefix="sn-ppt-design-doctor-") as temporary:
        folder = Path(temporary)
        source = folder / "probe.xml"
        source.write_text(
            '<slide xmlns="https://www.larkoffice.com/sml/2.0"><data>'
            '<shape type="text" topLeftX="80" topLeftY="180" width="800" height="100">'
            '<content fontFamily="Noto Sans CJK SC" fontSize="32" color="rgb(28,42,48)">'
            '<p>中文排版检查 XML → PPTX</p></content></shape></data></slide>',
            encoding="utf-8",
        )
        build = _xml_probe(script, "build", source)
        preview = (_xml_probe(script, "preview", folder / "presentation.pptx")
                   if build["status"] == "available" else {"status": "not_run"})
    return {"build": build, "preview": preview, "visual_review": "not_performed"}


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _first_env_name(*names: str) -> str | None:
    for name in names:
        if os.environ.get(name, "").strip():
            return name
    return None


def _valid_url(value: str) -> bool:
    parsed = parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _search_url(kind: str) -> tuple[str, str]:
    exact_name = (
        "SN_PPT_WEB_SEARCH_URL" if kind == "web_search" else "SN_PPT_IMAGE_SEARCH_URL"
    )
    exact = _first_env(exact_name)
    if exact:
        return exact, exact_name
    base_env = _first_env_name("SN_PPT_SEARCH_BASE_URL", "SERPER_BASE_URL")
    base = _first_env("SN_PPT_SEARCH_BASE_URL", "SERPER_BASE_URL") or DEFAULT_SEARCH_BASE_URL
    endpoint = "search" if kind == "web_search" else "images"
    return f"{base.rstrip('/')}/{endpoint}", base_env or "default_serper"


def _image_generation_url() -> tuple[str, str | None]:
    exact = _first_env("SN_PPT_IMAGE_GEN_URL")
    if exact:
        return exact, "SN_PPT_IMAGE_GEN_URL"
    base_env = _first_env_name("SN_IMAGE_GEN_BASE_URL", "SN_BASE_URL")
    base = _first_env("SN_IMAGE_GEN_BASE_URL", "SN_BASE_URL")
    return (
        f"{base.rstrip('/')}/images/generations" if base else "",
        base_env,
    )


def _load_ppt_env(skills_dir: Path) -> dict[str, Any]:
    scripts_dir = skills_dir / "sn-ppt-design-tools" / "scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        from env_config import load_ppt_env

        result = load_ppt_env().to_dict()
    except (ImportError, OSError, ValueError) as exc:
        result = {
            "path": None,
            "source": None,
            "exists": False,
            "loaded_keys": [],
            "preserved_process_keys": [],
            "invalid_lines": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            sys.path.remove(str(scripts_dir))
        except ValueError:
            pass
    relevant_prefixes = ("SN_PPT_", "SN_IMAGE_GEN_", "SERPER_")
    relevant_exact = {"SN_API_KEY", "SN_BASE_URL"}
    for field in ("loaded_keys", "preserved_process_keys"):
        result[field] = [
            name
            for name in result.get(field, [])
            if name.startswith(relevant_prefixes) or name in relevant_exact
        ]
    return result


def _probe(script: Path, args: list[str], timeout: float) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "reason": type(exc).__name__, "detail": str(exc)[:300]}
    output = result.stdout.strip().splitlines()
    if output:
        try:
            payload = json.loads(output[-1])
            if isinstance(payload, dict):
                payload.pop("detail", None)
                payload["exit_code"] = result.returncode
                return payload
        except json.JSONDecodeError:
            pass
    return {
        "status": "failed",
        "reason": "invalid probe output",
        "exit_code": result.returncode,
        "stderr": result.stderr.strip()[:300],
    }


def _bundled_media(
    skills_dir: Path,
    args: argparse.Namespace,
    env_load: dict[str, Any],
) -> dict[str, Any]:
    tools_dir = skills_dir / "sn-ppt-design-tools"
    scripts_dir = tools_dir / "scripts"
    scripts = {
        "web_search": scripts_dir / "web_search.py",
        "image_search": scripts_dir / "image_search.py",
        "fetch_image": scripts_dir / "fetch_image.py",
        "image_generation": scripts_dir / "image_generate.py",
    }

    search_key_env = _first_env_name(
        "SN_PPT_WEB_SEARCH_API_KEY",
        "SN_PPT_SEARCH_API_KEY",
        "SERPER_API_KEY",
    )
    image_search_key_env = _first_env_name(
        "SN_PPT_IMAGE_SEARCH_API_KEY",
        "SN_PPT_SEARCH_API_KEY",
        "SERPER_API_KEY",
    )
    image_generation_key_env = _first_env_name(
        "SN_PPT_IMAGE_GEN_API_KEY",
        "SN_IMAGE_GEN_API_KEY",
        "SN_API_KEY",
    )
    image_generation_model_env = _first_env_name(
        "SN_PPT_IMAGE_GEN_MODEL",
        "SN_IMAGE_GEN_MODEL",
    )

    web_url, web_url_env = _search_url("web_search")
    image_search_url, image_search_url_env = _search_url("image_search")
    image_generation_url, image_generation_url_env = _image_generation_url()

    media: dict[str, Any] = {
        "tools_dir": str(tools_dir),
        "config_file": env_load,
        "web_search": {
            "script_present": scripts["web_search"].is_file(),
            "url": web_url,
            "url_source": web_url_env,
            "url_valid": _valid_url(web_url),
            "api_key_configured": bool(search_key_env),
            "api_key_source": search_key_env,
        },
        "image_search": {
            "script_present": scripts["image_search"].is_file(),
            "fetch_script_present": scripts["fetch_image"].is_file(),
            "url": image_search_url,
            "url_source": image_search_url_env,
            "url_valid": _valid_url(image_search_url),
            "api_key_configured": bool(image_search_key_env),
            "api_key_source": image_search_key_env,
        },
        "image_generation": {
            "script_present": scripts["image_generation"].is_file(),
            "url": image_generation_url,
            "url_source": image_generation_url_env,
            "url_valid": _valid_url(image_generation_url),
            "api_key_configured": bool(image_generation_key_env),
            "api_key_source": image_generation_key_env,
            "model_configured": bool(image_generation_model_env),
            "model_source": image_generation_model_env,
        },
    }

    for name in ("web_search", "image_search"):
        item = media[name]
        item["missing"] = [
            field
            for field, missing in (
                ("script", not item["script_present"]),
                ("fetch_script", name == "image_search" and not item["fetch_script_present"]),
                ("url", not item["url_valid"]),
                ("api_key", not item["api_key_configured"]),
            )
            if missing
        ]
        item["status"] = (
            "configured"
            if not item["missing"]
            else "unconfigured"
        )
    generation = media["image_generation"]
    generation["missing"] = [
        field
        for field, missing in (
            ("script", not generation["script_present"]),
            ("url", not generation["url_valid"]),
            ("api_key", not generation["api_key_configured"]),
            ("model", not generation["model_configured"]),
        )
        if missing
    ]
    generation["status"] = (
        "configured"
        if not generation["missing"]
        else "unconfigured"
    )

    if args.probe_search:
        timeout = float(os.environ.get("SN_PPT_TOOL_TIMEOUT_SECONDS", "30")) + 5
        media["web_search"]["probe"] = _probe(
            scripts["web_search"],
            ["PPT capability probe", "--num", "1"],
            timeout,
        )
        media["image_search"]["probe"] = _probe(
            scripts["image_search"],
            ["presentation capability probe", "--num", "1"],
            timeout,
        )

    if args.probe_image_generation:
        output_dir = Path(args.probe_output_dir).expanduser().resolve()
        output_name = f"ppt_doctor_image_probe_{int(time.time())}.png"
        timeout = float(os.environ.get("SN_PPT_IMAGE_GEN_TIMEOUT_SECONDS", "300")) + 10
        media["image_generation"]["probe"] = _probe(
            scripts["image_generation"],
            [
                "--prompt",
                (
                    "A polished square key visual for SenseNova Skills AI presentation creation: "
                    "an editable slide canvas connected to research notes, a narrative outline, "
                    "charts, and image assets; modern professional editorial design, crisp layered "
                    "composition, restrained blue, white, and warm red palette, no logo, no "
                    "watermark, no small text"
                ),
                "--deck-dir",
                str(output_dir),
                "--output",
                output_name,
                "--size",
                "1344x1344",
            ],
            timeout,
        )
    media["configuration_help"] = {
        "edit_file": env_load.get("path"),
        "template": (
            'SN_PPT_SEARCH_API_KEY="<search-api-key>"\n'
            'SN_PPT_IMAGE_GEN_URL="https://your-host/images/generations"\n'
            'SN_PPT_IMAGE_GEN_API_KEY="<image-generation-api-key>"\n'
            'SN_PPT_IMAGE_GEN_MODEL="<image-generation-model>"'
        ),
        "note": "Existing process variables override values loaded from this file.",
    }
    return media


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect PPT Skill capabilities.")
    parser.add_argument(
        "--probe-search",
        action="store_true",
        help="Send one minimal web-search and image-search request.",
    )
    parser.add_argument(
        "--probe-image-generation",
        action="store_true",
        help="Send one real image-generation request; may incur cost.",
    )
    parser.add_argument(
        "--probe-output-dir",
        default="",
        help="Absolute existing output directory required for image-generation probe.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.probe_image_generation:
        probe_dir = Path(args.probe_output_dir).expanduser()
        if not args.probe_output_dir or not probe_dir.is_absolute() or not probe_dir.is_dir():
            raise SystemExit(
                "--probe-image-generation requires --probe-output-dir "
                "pointing to an absolute existing directory"
            )

    skills_dir = Path(__file__).resolve().parents[2]
    env_load = _load_ppt_env(skills_dir)
    workspace = Path.cwd().resolve()
    ppt_decks = workspace / "ppt_decks"
    checks = {
        "workspace": str(workspace),
        "ppt_decks": str(ppt_decks),
        "workspace_writable": workspace.is_dir() and os.access(workspace, os.W_OK),
        "pypdf": module_available("pypdf"),
        "python_docx": module_available("docx"),
        "xml_pptx": xml_pptx_status(skills_dir),
        "native_media": {
            "status": "agent_inspection_required",
            "presence_rule": "registered tool => present_unverified, never available",
            "available_requires": "successful config/health check or real probe",
        },
        "bundled_media": _bundled_media(skills_dir, args, env_load),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
