"""Visual review tool for local image artifacts."""

from __future__ import annotations

import asyncio
import base64
import io
import mimetypes
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from box_agent.schema import Message
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.safety import validate_path_in_workspace

if TYPE_CHECKING:
    from box_agent.tools.permissions import PermissionEngine


_SUPPORTED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
}
# Outer guard against decompression-bomb files; not the primary gate.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
# Long-edge ceiling for downsampling. Matches the provider-side cap most
# multimodal models apply before counting tokens, so staying at/under this is
# lossless for token cost while trimming upload payload and base64 bloat.
_MAX_LONG_EDGE_PX = 1568
# JPEG re-encode quality used only when an image is downsampled.
_JPEG_QUALITY = 85
# Hard ceiling on the single blocking LLM call. Screenshot QA should never wait
# on the SDK default (~600s).
_VISION_REVIEW_TIMEOUT = 120.0
_DEFAULT_OUTPUT_FILENAME = "visual_review.md"
_TERMINAL_VISION_ERROR_MARKERS = (
    "unexpected item type in content",
    "invalid user message",
    "invalid image base64 content",
    "doesn't support the image content block",
    "does not support the image content block",
)
_UNINSPECTED_RESPONSE_PATTERNS = (
    r"\b(?:cannot|could not|unable to)\s+(?:visually\s+)?inspect\b",
    r"\bno visual content\b.{0,40}\bverified\b",
    r"\b(?:image|ocr)\b.{0,50}\baccess\b.{0,30}\b(?:denied|failed)\b",
    r"\bcannot be verified\b",
    r"无法(?:检查|查看|验证).{0,20}(?:图像|图片|幻灯片|页面|视觉|内容)",
)


class VisionReviewTool(Tool):
    """Review local PNG/JPEG screenshots with the configured multimodal LLM."""

    def __init__(
        self,
        llm: Any,
        workspace_dir: str = ".",
        allow_full_access: bool = True,
        permission_engine: PermissionEngine | None = None,
        relative_root_dir: str | None = None,
        native_supported: bool = True,
    ) -> None:
        self.llm = llm
        self.workspace_dir = Path(workspace_dir).absolute()
        self.relative_root_dir = (
            Path(relative_root_dir).absolute() if relative_root_dir else self.workspace_dir
        )
        self.allow_full_access = allow_full_access
        self._perm = permission_engine
        self._terminal_error: str | None = None
        # native strategy attaches images to the MAIN model's context; it is
        # only valid when the main model itself is the vision model. When vision
        # is served by a separate utility model, ``self.llm`` is that utility and
        # native would send images to a text-only main model — so it is refused.
        self._native_supported = native_supported

    @property
    def name(self) -> str:
        return "vision_review"

    @property
    def description(self) -> str:
        return (
            "Visually review local PNG/JPEG screenshots by reading the image files, "
            "sending them as image content to the current multimodal LLM, writing "
            "the markdown report to visual_review.md beside the first input image by default, and returning "
            "per-image PASS/ISSUE findings. Use this when a skill requires real "
            "visual QA; passing image paths in text is not enough. Set strategy=native "
            "to attach the images to your own context and inspect them directly instead "
            "of proxying to a separate vision model."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "Local PNG/JPEG screenshot paths to review. Relative paths resolve "
                        "from the active project/artifact root."
                    ),
                },
                "output_path": {
                    "type": "string",
                    "description": "Markdown report path. Defaults to visual_review.md in the first image's directory.",
                },
                "instructions": {
                    "type": "string",
                    "description": "Optional extra review criteria from the active skill or user request.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["review", "describe"],
                    "default": "review",
                    "description": (
                        "Use review for presentation QA reports; use describe for direct "
                        "image understanding without writing visual_review.md."
                    ),
                },
                "strategy": {
                    "type": "string",
                    "enum": ["proxy", "native"],
                    "default": "proxy",
                    "description": (
                        "proxy (default): a separate vision model inspects the images and "
                        "returns a text review — use when you need a written QA report or "
                        "the main model cannot see images. native: attach the images "
                        "directly to your own (the main model's) context for the next turn "
                        "and inspect them yourself — use when you are a vision-capable model "
                        "and want to reason over the raw pixels. native ignores mode and "
                        "writes no report."
                    ),
                },
            },
            "required": ["image_paths"],
        }

    async def execute(
        self,
        image_paths: list[str],
        output_path: str | None = None,
        instructions: str | None = None,
        mode: str = "review",
        strategy: str = "proxy",
    ) -> ToolResult:
        """Run visual review and write the markdown report."""
        if not image_paths:
            return ToolResult(success=False, error="image_paths must contain at least one image path")
        if strategy not in {"proxy", "native"}:
            return ToolResult(success=False, error="strategy must be 'proxy' or 'native'")
        if mode not in {"review", "describe"}:
            return ToolResult(success=False, error="mode must be 'review' or 'describe'")

        # Native strategy: hand the raw images to the MAIN model instead of
        # proxying them to a side vision call. The images are attached as a
        # follow-up user message (see ToolResult.followup_user_content) so the
        # main model inspects the pixels itself on its next turn. No report is
        # written and no extra LLM call is made.
        if strategy == "native":
            if not self._native_supported:
                return ToolResult(
                    success=False,
                    error=(
                        "native strategy requires a vision-capable main model, but this "
                        "session routes vision to a separate utility model. Re-run with "
                        "strategy='proxy' (the default) to get a written review."
                    ),
                )
            try:
                images = [self._load_image(path) for path in image_paths]
            except ValueError as exc:
                return ToolResult(success=False, error=str(exc))
            blocks = self._build_native_blocks(images, instructions=instructions)
            attached = ", ".join(image["path"] for image in images)
            note = (
                f"Attached {len(images)} image(s) to your context for direct visual "
                f"inspection: {attached}. Inspect them yourself and continue the task; "
                "do not call vision_review again for the same images."
            )
            return ToolResult(
                success=True,
                content=note,
                followup_user_content=blocks,
            )

        if self._terminal_error is not None:
            return ToolResult(success=False, error=self._terminal_error)

        try:
            images = [self._load_image(path) for path in image_paths]
            output_file = (
                self._resolve_output_path(output_path, images[0]["file_path"])
                if mode == "review"
                else None
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        content_blocks = (
            self._build_content_blocks(images, instructions=instructions)
            if mode == "review"
            else self._build_description_blocks(images, instructions=instructions)
        )
        messages = [
            Message(
                role="system",
                content=(
                    "You are a meticulous visual QA reviewer. Review the supplied local screenshots directly. "
                    "Do not claim a page passed unless you inspected the image. Return concise markdown."
                    if mode == "review"
                    else "Inspect the supplied images directly and answer the user's image question accurately and concisely."
                ),
            ),
            Message(role="user", content=content_blocks),
        ]

        try:
            response = await asyncio.wait_for(
                self.llm.generate(messages=messages, tools=None, call_kind="utility"),
                timeout=_VISION_REVIEW_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"Vision review timed out after {_VISION_REVIEW_TIMEOUT:.0f}s",
            )
        except Exception as exc:  # pragma: no cover - exact provider exceptions vary
            error = f"Vision review LLM request failed: {exc}"
            if self._is_terminal_provider_error(str(exc)):
                error = self._terminal_failure(error)
            return ToolResult(success=False, error=error)

        response_content = (response.content or "").strip()
        if mode == "describe":
            if not response_content or self._reports_uninspected_images(response_content):
                return ToolResult(
                    success=False,
                    error=self._terminal_failure(
                        "Image understanding model did not inspect the supplied image."
                    ),
                )
            return ToolResult(success=True, content=response_content)
        if (
            not self._has_per_image_findings(response_content, len(images))
            or self._reports_uninspected_images(response_content)
        ):
            return ToolResult(
                success=False,
                error=self._terminal_failure(
                    "Vision review model did not return one PASS/ISSUE finding per image."
                ),
            )
        report = self._normalize_report(response_content, [img["path"] for img in images])

        try:
            assert output_file is not None
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(report, encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to write visual review report: {exc}")

        rel_output = self._display_path(output_file)
        return ToolResult(
            success=True,
            content=f"Visual review written to {rel_output}\n\n{report}",
        )

    def _load_image(self, path: str) -> dict[str, str]:
        file_path = self._resolve_readable_path(path)
        if not file_path.exists():
            raise ValueError(f"Image file not found: {path}")
        if not file_path.is_file():
            raise ValueError(f"Image path is not a file: {path}")

        mime_type = mimetypes.guess_type(file_path.name)[0]
        if mime_type not in _SUPPORTED_MIME_TYPES:
            raise ValueError(
                f"Unsupported image type for {path}: {mime_type or 'unknown'}; only PNG and JPEG are supported"
            )

        size = file_path.stat().st_size
        if size > _MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image is too large for visual review: {path} ({size} bytes > {_MAX_IMAGE_BYTES} bytes)"
            )

        raw, mime_type = self._encode_image(file_path, mime_type)
        data_url = f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"
        return {
            "path": self._display_path(file_path),
            "file_path": str(file_path),
            "mime_type": mime_type,
            "data_url": data_url,
            "base64": data_url.split(",", 1)[1],
        }

    def _encode_image(self, file_path: Path, mime_type: str) -> tuple[bytes, str]:
        """Return image bytes for review, downsampling oversized images.

        Images whose long edge is within ``_MAX_LONG_EDGE_PX`` are returned
        byte-for-byte (zero re-encode). Larger images are resized down to the
        ceiling and re-encoded in their original family (PNG stays PNG, JPEG
        stays JPEG). Returns ``(bytes, mime_type)``; ``mime_type`` is unchanged
        but returned for symmetry with potential format coercion.
        """
        try:
            from PIL import Image
        except ImportError:
            # Pillow is a declared dependency; if unavailable, fall back to raw.
            return file_path.read_bytes(), mime_type

        try:
            with Image.open(file_path) as im:
                long_edge = max(im.size)
                if long_edge <= _MAX_LONG_EDGE_PX:
                    return file_path.read_bytes(), mime_type

                scale = _MAX_LONG_EDGE_PX / long_edge
                new_size = (
                    max(1, round(im.width * scale)),
                    max(1, round(im.height * scale)),
                )
                resized = im.resize(new_size, Image.LANCZOS)

                buf = io.BytesIO()
                if mime_type == "image/png":
                    resized.save(buf, format="PNG", optimize=True)
                else:
                    # JPEG cannot hold alpha; flatten to RGB before saving.
                    if resized.mode not in ("RGB", "L"):
                        resized = resized.convert("RGB")
                    resized.save(buf, format="JPEG", quality=_JPEG_QUALITY)
                return buf.getvalue(), mime_type
        except Exception:
            # Any decode/resize failure → use the original bytes unchanged.
            return file_path.read_bytes(), mime_type

    def _resolve_output_path(self, output_path: str | None, first_image_path: str) -> Path:
        if output_path and output_path.strip():
            return self._resolve_writable_path(output_path)
        return self._resolve_writable_path(str(Path(first_image_path).parent / _DEFAULT_OUTPUT_FILENAME))

    def _resolve_readable_path(self, path: str) -> Path:
        file_path = self._resolve_from_active_root(path)
        if not file_path.exists() and not Path(path).is_absolute():
            workspace_candidate = self.workspace_dir / path
            if workspace_candidate.exists():
                file_path = workspace_candidate
        file_path = file_path.absolute()

        if self._perm:
            decision = self._perm.check(
                capability="filesystem.read",
                resource={"path": str(file_path)},
                tool_name=self.name,
            )
            if not decision.allowed:
                raise ValueError(decision.reason)
        elif not self.allow_full_access:
            error = validate_path_in_workspace(file_path, self.workspace_dir)
            if error:
                raise ValueError(error)
        return file_path

    def _resolve_writable_path(self, path: str) -> Path:
        file_path = self._resolve_from_active_root(path)
        file_path = file_path.absolute()

        if self._perm:
            decision = self._perm.check(
                capability="filesystem.write",
                resource={"path": str(file_path)},
                tool_name=self.name,
            )
            if not decision.allowed:
                raise ValueError(decision.reason)
        elif not self.allow_full_access:
            error = validate_path_in_workspace(file_path, self.workspace_dir)
            if error:
                raise ValueError(error)
        return file_path

    def _resolve_from_active_root(self, path: str) -> Path:
        file_path = Path(path)
        if file_path.is_absolute():
            return file_path
        try:
            root_from_workspace = self.relative_root_dir.relative_to(self.workspace_dir)
        except ValueError:
            root_from_workspace = None
        if (
            root_from_workspace
            and file_path.parts[: len(root_from_workspace.parts)] == root_from_workspace.parts
        ):
            return self.workspace_dir / file_path
        return self.relative_root_dir / file_path

    def _build_content_blocks(
        self,
        images: list[dict[str, str]],
        *,
        instructions: str | None,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": self._review_prompt(images, instructions=instructions),
            }
        ]
        provider = self._provider_hint()
        for index, image in enumerate(images, start=1):
            blocks.append({"type": "text", "text": f"Image {index}: {image['path']}"})
            if "anthropic" in provider:
                media_type = image["mime_type"]
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image["base64"],
                        },
                    }
                )
            else:
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image["data_url"]},
                    }
                )
        return blocks

    def _build_description_blocks(
        self,
        images: list[dict[str, str]],
        *,
        instructions: str | None,
    ) -> list[dict[str, Any]]:
        prompt = (instructions or "Describe the supplied images.").strip()
        blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            if "anthropic" in self._provider_hint():
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image["mime_type"],
                            "data": image["base64"],
                        },
                    }
                )
            else:
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image["data_url"]},
                    }
                )
        return blocks

    def _build_native_blocks(
        self,
        images: list[dict[str, str]],
        *,
        instructions: str | None,
    ) -> list[dict[str, Any]]:
        """Provider-shaped blocks handed to the MAIN model for native review.

        A leading text block frames the task, then each image is labeled and
        attached in the current provider's wire format (Anthropic ``image`` vs
        OpenAI ``image_url``). The main-model provider serializes these when the
        loop appends them as a follow-up user message.
        """
        lead = (instructions or "").strip()
        intro = "Inspect the following screenshot(s) directly and continue the task."
        text = f"{intro} Extra criteria: {lead}" if lead else intro
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        provider = self._provider_hint()
        for index, image in enumerate(images, start=1):
            blocks.append({"type": "text", "text": f"Image {index}: {image['path']}"})
            if "anthropic" in provider:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image["mime_type"],
                            "data": image["base64"],
                        },
                    }
                )
            else:
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image["data_url"]},
                    }
                )
        return blocks

    def _provider_hint(self) -> str:
        parts = [
            str(getattr(self.llm, "provider", "")),
            self.llm.__class__.__name__,
        ]
        nested = getattr(self.llm, "_client", None)
        if nested is not None:
            parts.append(nested.__class__.__name__)
        return " ".join(parts).lower()

    def _review_prompt(self, images: list[dict[str, str]], *, instructions: str | None) -> str:
        image_list = "\n".join(f"- Image {idx}: {image['path']}" for idx, image in enumerate(images, start=1))
        extra = f"\n\nExtra review criteria:\n{instructions.strip()}" if instructions and instructions.strip() else ""
        return f"""Review these presentation/page screenshots and produce visual_review.md content.

Images:
{image_list}

Required markdown format:
# Visual Review

## Summary
- Overall: PASS or ISSUE
- Reviewed images: {len(images)}

## Per-page findings
| Page | Source image | Status | Findings | Suggested fix |
| --- | --- | --- | --- | --- |

Rules:
- Use PASS only when the page is visually acceptable.
- Use ISSUE for text cutoff, overlap, low contrast, unreadable text, bad alignment, unintended blank areas, clipped media, broken charts/tables, or inconsistent styling.
- Include concrete fixes for each ISSUE.
- If every page passes, still include one table row per image with PASS.
- Do not treat a contact sheet as the final review result; inspect the supplied image content here.{extra}
"""

    def _normalize_report(self, content: str, image_paths: list[str]) -> str:
        report = (content or "").strip()
        if not report:
            rows = "\n".join(f"| {idx} | {path} | ISSUE | LLM returned an empty review. | Re-run visual review. |" for idx, path in enumerate(image_paths, start=1))
            report = f"# Visual Review\n\n## Summary\n- Overall: ISSUE\n- Reviewed images: {len(image_paths)}\n\n## Per-page findings\n| Page | Source image | Status | Findings | Suggested fix |\n| --- | --- | --- | --- | --- |\n{rows}"
        if not report.startswith("# Visual Review"):
            report = "# Visual Review\n\n" + report
        return report + "\n"

    @staticmethod
    def _has_per_image_findings(content: str, image_count: int) -> bool:
        statuses = re.findall(r"\|\s*(?:PASS|ISSUE)\s*\|", content, flags=re.IGNORECASE)
        return len(statuses) >= image_count

    @staticmethod
    def _is_terminal_provider_error(error: str) -> bool:
        normalized = error.lower()
        return any(marker in normalized for marker in _TERMINAL_VISION_ERROR_MARKERS)

    @staticmethod
    def _reports_uninspected_images(content: str) -> bool:
        normalized = content.lower()
        return any(
            re.search(pattern, normalized, flags=re.DOTALL)
            for pattern in _UNINSPECTED_RESPONSE_PATTERNS
        )

    def _terminal_failure(self, detail: str) -> str:
        self._terminal_error = (
            "VISION_REVIEW_UNAVAILABLE: "
            f"{detail} Do not retry vision_review with the same model in this session; "
            "continue with non-visual checks or use a model deployment verified for image input."
        )
        return self._terminal_error

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.relative_root_dir))
        except ValueError:
            try:
                return str(path.relative_to(self.workspace_dir))
            except ValueError:
                return str(path)
