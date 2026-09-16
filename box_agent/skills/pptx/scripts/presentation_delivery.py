"""Stateless formal Standard build, audit, export and artifact verification.

Mode and required formats are explicit caller-supplied work data. This
adapter neither selects a mode nor treats prose/file existence as a receipt.
The existing deck renderer retains ownership of its detached browser workers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import signal
import tempfile
import time
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
import zipfile



SCRIPTS = Path(__file__).resolve().parents[2] / "presentation-suite/skills/sn-ppt-standard/scripts"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_EXCLUDED = {"renders", "_trace", "_debug", "node_modules", ".git"}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def inspect_pptx(path: Path, *, expected_pages: int) -> dict[str, int]:
    """Inspect real OPC relationships, slide XML and geometry; never just PK."""
    from PIL import Image

    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = {item.filename for item in entries}
            if len(names) != len(entries) or len(entries) > 10000:
                raise ValueError("duplicate or excessive PPTX ZIP entries")
            if sum(item.file_size for item in entries) > 256 * 1024 * 1024:
                raise ValueError("PPTX expanded size exceeds inspection limit")
            if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                   or "\\" in name for name in names):
                raise ValueError("unsafe PPTX ZIP path")

            def xml(name: str) -> ET.Element:
                payload = archive.read(name)
                if b"<!DOCTYPE" in payload or b"<!ENTITY" in payload:
                    raise ValueError("DTD/entity is not allowed in PPTX XML")
                return ET.fromstring(payload)

            types = xml("[Content_Types].xml")
            if not any(item.get("PartName") == "/ppt/presentation.xml" and
                       item.get("ContentType", "").endswith("presentation.main+xml")
                       for item in types):
                raise ValueError("PPTX presentation content type missing")
            root_rels = xml("_rels/.rels")
            if not any(item.get("Type", "").endswith("/officeDocument") and
                       item.get("Target") == "ppt/presentation.xml" and
                       item.get("TargetMode") != "External" for item in root_rels):
                raise ValueError("PPTX root presentation relationship missing")
            presentation = xml("ppt/presentation.xml")
            size = presentation.find(f"{P}sldSz")
            width, height = int(size.get("cx", "0")), int(size.get("cy", "0"))
            if min(width, height) <= 0 or not math.isclose(width / height, 16 / 9, rel_tol=1e-4):
                raise ValueError("PPTX dimensions must be positive 16:9")
            slides = presentation.findall(f"{P}sldIdLst/{P}sldId")
            if len(slides) != expected_pages:
                raise ValueError(f"PPTX page count {len(slides)} != {expected_pages}")
            relationships = {item.get("Id"): item for item in xml("ppt/_rels/presentation.xml.rels")}
            targets = []
            checked_media = set()
            for slide in slides:
                rel = relationships.get(slide.get(f"{R}id"))
                if rel is None or rel.get("TargetMode") == "External" or not rel.get("Type", "").endswith("/slide"):
                    raise ValueError("PPTX slide relationship missing or external")
                target = posixpath.normpath(posixpath.join("ppt", rel.get("Target", "")))
                if not target.startswith("ppt/slides/") or target not in names:
                    raise ValueError("PPTX slide target missing or outside slides")
                targets.append(target)
                tree = xml(target).find(f"{P}cSld/{P}spTree")
                if tree is None:
                    raise ValueError("PPTX slide tree missing")
                pictures = tree.findall(f".//{P}pic")
                if pictures:
                    rel_path = posixpath.join(posixpath.dirname(target), "_rels", posixpath.basename(target) + ".rels")
                    if rel_path not in names:
                        raise ValueError("PPTX picture relationships missing")
                    image_rels = {item.get("Id"): item for item in xml(rel_path)}
                    def inspect_image(blip):
                        embedded = blip.get(f"{R}embed") if blip is not None else None
                        image_rel = image_rels.get(embedded) if embedded else None
                        if (image_rel is None or image_rel.get("TargetMode") == "External" or
                                not image_rel.get("Type", "").endswith("/image")):
                            raise ValueError("PPTX picture requires an embedded image relationship")
                        image_target = unquote(image_rel.get("Target", ""))
                        image_path = posixpath.normpath(posixpath.join(posixpath.dirname(target), image_target))
                        if not image_path.startswith("ppt/media/") or image_path not in names:
                            raise ValueError("PPTX image media target missing or outside media")
                        if image_path not in checked_media:
                            payload = archive.read(image_path)
                            if not payload:
                                raise ValueError("PPTX image media is empty")
                            if image_path.lower().endswith(".svg"):
                                try:
                                    if xml(image_path).tag != "{http://www.w3.org/2000/svg}svg":
                                        raise ValueError("root element is not SVG")
                                except (ET.ParseError, ValueError) as exc:
                                    raise ValueError(f"PPTX SVG media is invalid: {exc}") from exc
                            else:
                                try:
                                    with Image.open(BytesIO(payload)) as image:
                                        if min(image.size) <= 0 or image.width * image.height > 40_000_000:
                                            raise ValueError("image dimensions exceed inspection limit")
                                        image.verify()
                                    with Image.open(BytesIO(payload)) as image:
                                        image.load()
                                except (OSError, ValueError, Image.DecompressionBombError) as exc:
                                    raise ValueError(f"PPTX image media cannot be decoded: {exc}") from exc
                            checked_media.add(image_path)
                    for picture in pictures:
                        blip = picture.find(f"{P}blipFill/{A}blip")
                        inspect_image(blip)
                        for extension in blip.findall(
                            ".//{http://schemas.microsoft.com/office/drawing/2016/SVG/main}svgBlip"
                        ):
                            inspect_image(extension)
                visible = [item for item in tree if item.tag not in {f"{P}nvGrpSpPr", f"{P}grpSpPr"}]
                if len(visible) == 1 and visible[0].tag == f"{P}pic":
                    transform = visible[0].find(f"{P}spPr/{A}xfrm")
                    offset = transform.find(f"{A}off") if transform is not None else None
                    extent = transform.find(f"{A}ext") if transform is not None else None
                    if offset is None or extent is None or any(
                        abs(int(element.get(key, "-1")) - expected) > 10
                        for element, key, expected in ((offset, "x", 0), (offset, "y", 0),
                                                       (extent, "cx", width), (extent, "cy", height))
                    ):
                        raise ValueError("single-picture slide does not cover the canvas")
            actual = {name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)}
            if len(set(targets)) != expected_pages or set(targets) != actual:
                raise ValueError("PPTX slide parts do not match presentation relationships")
            bad = archive.testzip()
            if bad:
                raise ValueError(f"PPTX ZIP CRC failure: {bad}")
            return {"pages": expected_pages, "width_emu": width, "height_emu": height}
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid PPTX package: {exc}") from exc


class _References(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value and (name in {"src", "poster", "xlink:href"} or
                          name == "href" and tag in {"link", "image", "use"}):
                self.references.append(value)


class PresentationDeliveryAdapter:
    def __init__(self, workspace: Path, *, python_bin: Path, node_bin: Path | None = None,
                 env: Mapping[str, str] | None = None, timeout_seconds: float = 600,
                 guard_parent_death: bool = False):
        if not Path(workspace).is_absolute():
            raise ValueError("workspace must be absolute")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("delivery timeout must be finite and positive")
        self.workspace = Path(workspace).resolve(strict=True)
        self.python_bin = Path(python_bin)
        self.node_bin = Path(node_bin) if node_bin else None
        self.env = dict(os.environ if env is None else env)
        self.timeout_seconds = timeout_seconds
        self.guard_parent_death = guard_parent_death

    def _deck(self, deck_dir: str | Path, *, allow_workspace: bool = False) -> Path:
        candidate = Path(deck_dir)
        candidate = candidate if candidate.is_absolute() else self.workspace / candidate
        candidate = Path(os.path.abspath(candidate))
        if not candidate.is_relative_to(self.workspace) or (candidate == self.workspace and not allow_workspace):
            raise ValueError("deck_dir must be a directory inside workspace")
        for part in [candidate, *candidate.parents]:
            if part == self.workspace:
                break
            if part.is_symlink():
                raise ValueError("symlink deck path is not allowed")
        root = candidate.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("deck_dir is not a directory")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"symlink inside deck is not allowed: {path.relative_to(root)}")
        return root

    @staticmethod
    def _formats(required_formats: Sequence[str]) -> list[str]:
        formats = sorted(set(required_formats))
        if "html" not in formats or set(formats) - {"html", "pptx"}:
            raise ValueError("Standard required_formats must include html and optionally pptx")
        return formats

    @staticmethod
    def _pages(root: Path, expected_pages: int | None) -> list[Path]:
        pages = sorted((root / "slides").glob("slide_*.html"))
        pages = [path for path in pages if ".bak." not in path.name]
        count = len(pages) if expected_pages is None else expected_pages
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("expected_pages must be a positive integer")
        expected = [root / f"slides/slide_{index:02}.html" for index in range(1, count + 1)]
        if pages != expected or any(not path.is_file() or path.stat().st_size == 0 for path in pages):
            raise ValueError("HTML page coverage is missing, empty, or non-contiguous")
        return pages

    @staticmethod
    def _inputs(root: Path) -> dict[str, str]:
        result = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part in _EXCLUDED or part.startswith(".presentation-delivery-") for part in relative.parts):
                continue
            if relative.as_posix() == "present.html" or path.suffix.lower() == ".pptx":
                continue
            if path.is_file():
                result[relative.as_posix()] = _sha(path)
        return result

    @staticmethod
    def _assets(root: Path, pages: list[Path]) -> None:
        queue = [*pages, root / "present.html"]
        visited = set()
        while queue:
            owner = queue.pop()
            if owner in visited:
                continue
            visited.add(owner)
            text = owner.read_text(encoding="utf-8")
            parser = _References()
            if owner.suffix.lower() in {".html", ".svg"}:
                parser.feed(text)
            refs = parser.references + re.findall(r"url\(\s*['\"]?([^)'\"]+)['\"]?\s*\)", text)
            for value in refs:
                value = unquote(value.strip())
                if not value or value.startswith(("#", "data:")):
                    continue
                url = urlsplit(value)
                if url.scheme or url.netloc or value.startswith("//"):
                    raise ValueError(f"non-local delivery asset in {owner.name}: {value[:120]}")
                target = (owner.parent / url.path).resolve()
                if not target.is_relative_to(root) or not target.is_file() or not target.stat().st_size:
                    raise ValueError(f"missing or escaping delivery asset: {owner.name}: {value[:120]}")
                if target.suffix.lower() in {".html", ".css", ".svg"}:
                    queue.append(target)
        player = (root / "present.html").read_text(encoding="utf-8")
        if any(path.relative_to(root).as_posix() not in player for path in pages):
            raise ValueError("present.html does not cover all HTML pages")

    @staticmethod
    def _artifact(root: Path, path: Path, format: str, pages: int) -> dict[str, Any]:
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise ValueError(f"required {format} artifact missing or empty")
        result = {"format": format, "path": str(path), "sha256": _sha(path),
                  "bytes": path.stat().st_size, "pages": pages}
        if format == "pptx":
            result.update(inspect_pptx(path, expected_pages=pages))
        return result

    async def _run(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> dict[str, Any]:
        if timeout <= 0:
            return {"returncode": -1, "stdout": "", "stderr": "delivery deadline exceeded", "timed_out": True}
        control = None
        inherited = ()
        if self.guard_parent_death and os.name == "posix":
            reader, control = os.pipe()
            inherited = (reader,)
            argv = [str(self.python_bin), str(Path(__file__).with_name("delivery_process.py")),
                    str(reader), str(timeout), *map(str, argv)]
        try:
            process = await asyncio.create_subprocess_exec(
                *map(str, argv), cwd=self.workspace, env=dict(env), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix", **({"pass_fds": inherited} if inherited else {}),
            )
        except BaseException:
            if control is not None:
                os.close(control)
            raise
        finally:
            for descriptor in inherited:
                os.close(descriptor)

        def group_alive():
            if os.name != "posix":
                return process.returncode is None
            try:
                os.killpg(process.pid, 0)
                return True
            except ProcessLookupError:
                return False

        async def drain(reader):
            tail = bytearray()
            while block := await reader.read(8192):
                tail.extend(block)
                del tail[:-65536]
            return tail.decode("utf-8", "replace")

        async def cleanup():
            nonlocal control
            if control is not None:
                # EOF reaches the detached, invocation-scoped supervisor even
                # if Bash subsequently force-kills this outer CLI process.
                os.close(control)
                control = None
            # Only this invocation's group is signalled. deck.py's #129 renderer
            # owns detached guards; the managed exporter closes its own browser.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 12
                while group_alive() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                if group_alive():
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            elif process.returncode is None:
                # /T is scoped to the known owned PID; no global name matching.
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), 3)
                if process.returncode is None:
                    process.kill()
            await asyncio.wait_for(process.wait(), 3)

        output = asyncio.create_task(drain(process.stdout))
        errors = asyncio.create_task(drain(process.stderr))
        completed = asyncio.ensure_future(asyncio.gather(process.wait(), output, errors))
        timed_out = False
        cleanup_task = None

        async def owned_cleanup():
            nonlocal cleanup_task
            if cleanup_task is None:
                cleanup_task = asyncio.create_task(cleanup())
            await asyncio.shield(cleanup_task)

        try:
            try:
                await asyncio.wait_for(asyncio.shield(completed), timeout)
            except asyncio.TimeoutError:
                timed_out = True
                await owned_cleanup()
            if group_alive():
                await owned_cleanup()
            await asyncio.wait_for(asyncio.shield(completed), 3)
            return {"returncode": process.returncode, "stdout": output.result(),
                    "stderr": errors.result(), "timed_out": timed_out}
        except asyncio.CancelledError:
            # Cancellation can arrive while timeout cleanup is already running.
            # Keep the same bounded cleanup task alive, including repeated cancel.
            while True:
                try:
                    await owned_cleanup()
                    break
                except asyncio.CancelledError:
                    continue
                except Exception as exc:
                    logging.getLogger(__name__).warning("delivery cleanup failed: %s", exc)
                    break
            raise
        finally:
            if control is not None:
                os.close(control)
            if not completed.done():
                completed.cancel()
            await asyncio.gather(completed, return_exceptions=True)

    async def finalize(self, *, deck_dir: str | Path, revision: str,
                       required_formats: Sequence[str], expected_pages: int | None = None) -> dict[str, Any]:
        receipt: dict[str, Any] = {"status": "error", "revision": revision,
                                   "artifacts": [], "steps": [], "warnings": []}
        deadline = time.monotonic() + self.timeout_seconds
        try:
            if not isinstance(revision, str) or not revision.strip():
                raise ValueError("revision is required")
            root = self._deck(deck_dir)
            formats = self._formats(required_formats)
            pages = self._pages(root, expected_pages)
            receipt.update(deck_dir=str(root), required_formats=formats, pages=len(pages))
            environment = {**self.env, "BOX_AGENT_PPTX_NO_INSTALL": "1",
                           "BOX_AGENT_PPTX_MANAGED_DELIVERY": "1"}
            player = root / "present.html"
            previous = player.stat().st_mtime_ns if player.exists() else None
            receipt["initial_input_sha256"] = _digest(self._inputs(root))
            for step in ("build", "audit"):
                result = await self._run([str(self.python_bin), str(SCRIPTS / "deck.py"), step,
                                          str(root), "--expected", str(len(pages))],
                                         env=environment, timeout=deadline - time.monotonic())
                receipt["steps"].append({"stage": step, **result})
                if result["returncode"] != 0 or result["timed_out"]:
                    raise ValueError(f"{step} failed: {result['stderr'] or result['stdout']}")
                if step == "build" and (not player.is_file() or player.stat().st_mtime_ns == previous):
                    raise ValueError("build did not produce a fresh present.html")
            self._deck(root)
            self._assets(root, pages)
            inputs = self._inputs(root)
            receipt.update(inputs=inputs, input_sha256=_digest(inputs))
            review_files = [root / "_trace" / name for name in ("review-issues.md", "content-fidelity.md")]
            if any(not path.is_file() or not path.stat().st_size for path in review_files):
                receipt["warnings"].append(
                    "缺少完整 Review 工件；技术收尾已检查文件与结构，视觉和内容检查尚未核实。")
            elif receipt["initial_input_sha256"] != receipt["input_sha256"]:
                receipt["warnings"].append(
                    "build 更新了制作输入；已有 Review 工件不能证明最终页面已重新完成视觉和内容检查。")
            receipt["artifacts"].append(self._artifact(root, player, "html", len(pages)))
            receipt["status"] = "partial"
            if "pptx" in formats:
                if self.node_bin is None:
                    raise ValueError("managed Node runtime is unavailable; dependencies were not installed")
                output = root / f"{root.name}.pptx"
                with tempfile.TemporaryDirectory(prefix=".presentation-delivery-", dir=root) as directory:
                    staging = Path(directory) / "output.pptx"
                    result = await self._run([str(self.node_bin), str(SCRIPTS / "export_pptx/html_to_pptx.mjs"),
                                              "--deck-dir", str(root), "--pages-dir", str(root / "slides"),
                                              "--output", str(staging), "--force"],
                                             env=environment, timeout=deadline - time.monotonic())
                    receipt["steps"].append({"stage": "export", **result})
                    if result["returncode"] != 0 or result["timed_out"]:
                        raise ValueError(f"export failed: {result['stderr'] or result['stdout']}")
                    self._deck(root)
                    self._artifact(root, staging, "pptx", len(pages))
                    if self._inputs(root) != inputs:
                        raise ValueError("delivery inputs changed during export; receipt is stale")
                    staging.replace(output)
                receipt["artifacts"].append(self._artifact(root, output, "pptx", len(pages)))
            receipt["status"] = "complete"
            verified = self.verify_receipt(receipt, deck_dir=root, revision=revision,
                                           required_formats=formats, expected_pages=len(pages))
            if not verified["valid"]:
                raise ValueError(verified["reason"])
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError, ImportError) as exc:
            receipt["status"] = "partial" if receipt["artifacts"] else "error"
            receipt["error"] = str(exc)
        return receipt

    def verify_receipt(self, receipt: Mapping[str, Any], *, deck_dir: str | Path, revision: str,
                       required_formats: Sequence[str], expected_pages: int | None = None) -> dict[str, Any]:
        try:
            root = self._deck(deck_dir)
            formats = self._formats(required_formats)
            pages = self._pages(root, expected_pages)
            if (receipt.get("status") != "complete" or receipt.get("revision") != revision or
                    receipt.get("deck_dir") != str(root) or receipt.get("required_formats") != formats or
                    receipt.get("pages") != len(pages)):
                raise ValueError("receipt contract/revision does not match")
            inputs = self._inputs(root)
            if receipt.get("inputs") != inputs or receipt.get("input_sha256") != _digest(inputs):
                raise ValueError("receipt inputs changed")
            self._assets(root, pages)
            artifacts = receipt.get("artifacts", [])
            if len(artifacts) != len(formats) or sorted(a["format"] for a in artifacts) != formats:
                raise ValueError("receipt required artifacts missing")
            for item in artifacts:
                expected = root / ("present.html" if item["format"] == "html" else f"{root.name}.pptx")
                if item["path"] != str(expected) or self._artifact(root, expected, item["format"], len(pages)) != item:
                    raise ValueError("receipt artifact changed")
            return {"valid": True, "reason": ""}
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            return {"valid": False, "reason": str(exc)}
