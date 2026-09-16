#!/usr/bin/env python3
"""Finalize explicit PPT work data; no Agent imports, intent classifier or task state.

Requirements and task packs are model-authored work data, not user authorization.
The Skill checks conversation evidence before supplying these inputs. This script
only checks their consistency and the actual artifacts, and emits a JSON receipt.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
from pathlib import Path
import sys

# run_path and managed shell invocations need only this copied script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from presentation_artifacts import inspect_existing
from presentation_delivery import PresentationDeliveryAdapter


def validate_inputs(requirements: dict, pack: dict, root: Path) -> dict:
    """Validate an explicit delivery contract, without interpreting user text."""
    mode, output = requirements.get("mode"), requirements.get("output")
    if (mode, output) not in {("fast", "fast_html"), ("design", "static_html"),
                              ("design", "dynamic_html")}:
        raise ValueError("mode/output is missing or inconsistent")
    formats = requirements.get("required_formats")
    if (not isinstance(formats, list) or not all(isinstance(v, str) for v in formats)
            or "html" not in formats or set(formats) - {"html", "pptx"}
            or len(formats) != len(set(formats))):
        raise ValueError("required_formats must explicitly include html and optionally pptx")
    if output == "dynamic_html" and formats != ["html"]:
        raise ValueError("dynamic HTML cannot promise an animated PPTX; clarify the required format")
    revision = requirements.get("revision")
    pages = requirements.get("expected_pages")
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("revision is required")
    if pages is not None and (type(pages) is not int or pages < 1):
        raise ValueError("expected_pages must be a positive integer or null")
    directory = Path(pack.get("deck_dir", ""))
    if not directory.is_absolute() or directory.resolve() != root:
        raise ValueError("task_pack deck_dir differs from the actual deck directory")
    if pack.get("choices", {}).get("output") != output:
        raise ValueError("task_pack output differs from explicit requirements")
    if mode == "design":
        if pack.get("ppt_mode") != ("dazzle" if output == "dynamic_html" else "standard"):
            raise ValueError("task_pack ppt_mode differs from output")
        if pack["choices"].get("static_postprocess") != (["pptx"] if "pptx" in formats else []):
            raise ValueError("task_pack postprocess differs from required_formats")
    return dict(revision=revision, required_formats=sorted(formats), expected_pages=pages)


async def finalize(args) -> dict:
    receipt = {"status": "error", "artifacts": [], "warnings": []}
    root = None
    try:
        environment = dict(os.environ)
        browser = environment.get("BOX_AGENT_BROWSER_EXECUTABLE_PATH") or environment.get("AGENT_BROWSER_EXECUTABLE_PATH")
        if browser:
            environment.setdefault("PPT_SKILL_BROWSER_EXE", browser)
        adapter = PresentationDeliveryAdapter(args.workspace.resolve(strict=True),
            python_bin=Path(environment.get("BOX_AGENT_PYTHON") or sys.executable),
            node_bin=Path(environment["BOX_AGENT_NODE"]) if environment.get("BOX_AGENT_NODE") else None,
            env=environment, timeout_seconds=args.timeout, guard_parent_death=True)
        # Resolve the safe publication directory before reading work data, so
        # missing/malformed inputs also replace a previous successful receipt.
        # Mode-specific workspace-root restrictions are checked after parsing.
        root = adapter._deck(args.deck_dir, allow_workspace=True)
        publish_receipt(root, {"status": "in_progress", "artifacts": [], "warnings": []})
        paths = [args.requirements.resolve(strict=True), args.task_pack.resolve(strict=True)]
        payloads = [path.read_bytes() for path in paths]
        requirements, pack = [json.loads(payload) for payload in payloads]
        if not isinstance(requirements, dict) or not isinstance(pack, dict):
            raise ValueError("requirements and task_pack must be JSON objects")
        root = adapter._deck(args.deck_dir, allow_workspace=requirements.get("mode") == "fast")
        contract = validate_inputs(requirements, pack, root)
        if requirements["output"] == "static_html":
            receipt = await adapter.finalize(deck_dir=root, **contract)
        else:
            receipt = inspect_existing(root, dynamic=requirements["output"] == "dynamic_html", **contract)
        if any(path.read_bytes() != payload for path, payload in zip(paths, payloads)):
            raise ValueError("requirements or task_pack changed during finalization")
        receipt["requirements_sha256"] = hashlib.sha256(payloads[0]).hexdigest()
        receipt["task_pack_sha256"] = hashlib.sha256(payloads[1]).hexdigest()
        receipt["mode"], receipt["output"] = requirements["mode"], requirements["output"]
    except asyncio.CancelledError:
        receipt["status"] = "cancelled"
        receipt["error"] = "formal finalization cancelled"
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        receipt["status"] = "partial" if receipt.get("artifacts") else "error"
        receipt["error"] = str(exc)
    if root is not None:
        try:
            # Recheck the directory before publishing even an unsuccessful
            # receipt: a failed retry must never leave a stale success report.
            adapter._deck(root, allow_workspace=root == adapter.workspace)
            publish_receipt(root, receipt)
        except (OSError, ValueError) as exc:
            receipt["status"] = "partial" if receipt.get("artifacts") else "error"
            receipt["error"] = f"cannot publish current receipt: {exc}"
    return receipt


def publish_receipt(root: Path, receipt: dict) -> None:
    trace = root / "_trace"
    trace.mkdir(exist_ok=True)
    temporary = trace / "finalize-receipt.json.tmp"
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(trace / "finalize-receipt.json")


async def run_cli(args) -> dict:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = []
    if os.name == "posix":
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, task.cancel)
            installed.append(signum)
    try:
        return await finalize(args)
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="workspace path, relative to shell cwd if not absolute")
    parser.add_argument("--deck-dir", required=True, type=Path, help="absolute task directory, or a path relative to --workspace")
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--task-pack", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=600)
    receipt = asyncio.run(run_cli(parser.parse_args()))
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
