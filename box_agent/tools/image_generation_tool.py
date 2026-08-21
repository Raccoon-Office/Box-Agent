"""Image generation tool backed by the configured Box-Agent image service."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import httpx

from box_agent.auth import request_auth_headers
from box_agent.llm.debug_logging import (
    log_image_generation_error_meta,
    log_image_generation_request,
    log_image_generation_response_meta,
)
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.safety import validate_path_in_workspace
from box_agent.tools.watermark import apply_text_watermark

if TYPE_CHECKING:
    from box_agent.tools.permissions import PermissionEngine


_ENDPOINT_ENV = ("BOX_AGENT_IMAGE_GENERATION_ENDPOINT", "BOX_AGENT_IMAGE_GEN_ENDPOINT")
_API_KEY_ENV = ("BOX_AGENT_IMAGE_GENERATION_API_KEY", "BOX_AGENT_IMAGE_GEN_API_KEY")
_TIMEOUT_ENV = "BOX_AGENT_IMAGE_GENERATION_TIMEOUT"
_MAX_DIM_ENV = "BOX_AGENT_IMAGE_MAX_DIM"
_DEFAULT_TIMEOUT = 120.0
_MIN_IMAGE_DIMENSION = 1024
_DEFAULT_MAX_IMAGE_DIMENSION = 1024
_MIN_OPENAI_IMAGE_PIXELS = _MIN_IMAGE_DIMENSION * _MIN_IMAGE_DIMENSION
_HOST_IMAGE_DIMENSION_ALIGNMENT = 16
_CANONICAL_IMAGE_RATIOS: tuple[tuple[int, int], ...] = (
    (1, 1),
    (16, 9),
    (9, 16),
    (4, 3),
    (3, 4),
    (3, 2),
    (2, 3),
    (21, 9),
)
_SEEDREAM_PRESETS: dict[tuple[int, int], list[tuple[int, int]]] = {
    (1, 1): [(2048, 2048), (3072, 3072), (4096, 4096)],
    (3, 4): [(1728, 2304), (2592, 3456), (3520, 4704)],
    (4, 3): [(2304, 1728), (3456, 2592), (4704, 3520)],
    (16, 9): [(2848, 1600), (4096, 2304), (5504, 3040)],
    (9, 16): [(1600, 2848), (2304, 4096), (3040, 5504)],
    (3, 2): [(2496, 1664), (3744, 2496), (4992, 3328)],
    (2, 3): [(1664, 2496), (2496, 3744), (3328, 4992)],
    (21, 9): [(3136, 1344), (4704, 2016), (6240, 2656)],
}
_DEFAULT_LARGE_IMAGE_SIZE = "2048x2048"
_SIZE_RE = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*$", re.IGNORECASE)
_OPENAI_IMAGE_SIZES = {
    "1024x1024": (1024, 1024),
    "1536x1024": (1536, 1024),
    "1024x1536": (1024, 1536),
}
_DEFAULT_MODEL = "gpt-image-1"
_DEFAULT_MIME_TYPE = "image/png"
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}
_BASE64_IMAGE_KEYS = ("b64_json", "base64", "image_base64", "image", "image_data")
_URL_IMAGE_KEYS = ("url", "image_url", "imageUrl", "image_urls", "imageUrls", "output_url", "signed_url")
_NESTED_IMAGE_KEYS = ("data", "images", "output", "outputs", "result", "results")
_EDIT_ENDPOINT_SUFFIXES = ("/images/gen", "/images/generations")
_OPENAI_IMAGE_ENDPOINT_HINTS = ("/images/gen", "/images/generations", "/images/edits")
_HOST_ALIGNED_IMAGE_ENDPOINT_SUFFIXES = ("/images/gen",)
_IMAGE_EDIT_MODES = {"image_to_image", "edit", "image_edit"}


def _is_large_image_service(endpoint: str | None, model: str | None) -> bool:
    needle = f"{endpoint or ''} {model or ''}".lower()
    return (
        "doubao" in needle
        or "seedream" in needle
    )


def _seedream_size_for_ratio(width: int, height: int) -> tuple[str, int, int]:
    if width <= 0 or height <= 0:
        target_ratio = (1, 1)
    else:
        best_ratio: tuple[int, int] = (1, 1)
        best_error = float("inf")
        for ratio_w, ratio_h in _SEEDREAM_PRESETS:
            ratio_error = abs(width * ratio_h - height * ratio_w) / float((width * ratio_h) or 1)
            if ratio_error < best_error:
                best_error = ratio_error
                best_ratio = (ratio_w, ratio_h)
        target_ratio = best_ratio
    candidates = _SEEDREAM_PRESETS[target_ratio]
    if width <= 0 or height <= 0:
        fallback = candidates[0]
        return f"{fallback[0]}x{fallback[1]}", fallback[0], fallback[1]

    for candidate_w, candidate_h in candidates:
        if candidate_w >= width and candidate_h >= height:
            return f"{candidate_w}x{candidate_h}", candidate_w, candidate_h
    fallback = candidates[-1]
    return f"{fallback[0]}x{fallback[1]}", fallback[0], fallback[1]


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def resolve_image_generation_endpoint(
    endpoint: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    configured = str(endpoint or "").strip()
    if configured:
        return configured
    source = os.environ if env is None else env
    for name in _ENDPOINT_ENV:
        value = str(source.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _guess_mime_from_data_url(data_url: str) -> str:
    if data_url.startswith("data:") and ";base64," in data_url:
        return data_url[5 : data_url.index(";base64,")] or _DEFAULT_MIME_TYPE
    return _DEFAULT_MIME_TYPE


def _guess_mime_from_bytes(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return _DEFAULT_MIME_TYPE


def _decode_base64_image(value: str) -> tuple[bytes, str]:
    text = value.strip()
    mime_type = _DEFAULT_MIME_TYPE
    if text.startswith("data:") and ";base64," in text:
        mime_type = _guess_mime_from_data_url(text)
        text = text.split(";base64,", 1)[1]
        return base64.b64decode(text), mime_type

    image_bytes = base64.b64decode(text)
    return image_bytes, _guess_mime_from_bytes(image_bytes)


def _first_string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            found = _first_string(item)
            if found:
                return found
    return ""


def _find_first_image_payload(data: Any) -> tuple[str, str] | None:
    """Return ("base64"|"url", value) from common image API JSON shapes."""
    if not isinstance(data, dict):
        return None

    for key in _BASE64_IMAGE_KEYS:
        value = _first_string(data.get(key))
        if value:
            return "base64", value

    for key in _URL_IMAGE_KEYS:
        value = _first_string(data.get(key))
        if value:
            return "url", value

    for key in _NESTED_IMAGE_KEYS:
        nested = data.get(key)
        if isinstance(nested, list):
            for item in nested:
                found = _find_first_image_payload(item)
                if found:
                    return found
        elif isinstance(nested, dict):
            found = _find_first_image_payload(nested)
            if found:
                return found

    return None


def _openai_image_size(width: int, height: int) -> tuple[str, int, int]:
    """Map requested dimensions to a supported OpenAI Images API size."""
    width = max(int(width), _MIN_IMAGE_DIMENSION)
    height = max(int(height), _MIN_IMAGE_DIMENSION)

    if width > height * 1.2:
        size = "1536x1024"
    elif height > width * 1.2:
        size = "1024x1536"
    else:
        size = "1024x1024"

    normalized_width, normalized_height = _OPENAI_IMAGE_SIZES[size]
    return size, normalized_width, normalized_height


def _is_openai_image_service(endpoint: str | None) -> bool:
    normalized = (endpoint or "").strip().lower().rstrip("/")
    return any(normalized.endswith(suffix) for suffix in _OPENAI_IMAGE_ENDPOINT_HINTS)


def _is_host_aligned_image_service(endpoint: str | None) -> bool:
    normalized = (endpoint or "").strip().lower().rstrip("/")
    return any(
        normalized.endswith(suffix)
        for suffix in _HOST_ALIGNED_IMAGE_ENDPOINT_SUFFIXES
    )


def _round_up_to_alignment(value: float) -> int:
    return (
        math.ceil(value / _HOST_IMAGE_DIMENSION_ALIGNMENT)
        * _HOST_IMAGE_DIMENSION_ALIGNMENT
    )


def _normalize_host_image_size(width: int, height: int) -> tuple[str, int, int]:
    """Preserve common ratios while satisfying the host image API constraints."""
    width = max(int(width), 1)
    height = max(int(height), 1)

    for ratio_width, ratio_height in _CANONICAL_IMAGE_RATIOS:
        if width * ratio_height != height * ratio_width:
            continue
        width_alignment_factor = _HOST_IMAGE_DIMENSION_ALIGNMENT // math.gcd(
            ratio_width,
            _HOST_IMAGE_DIMENSION_ALIGNMENT,
        )
        height_alignment_factor = _HOST_IMAGE_DIMENSION_ALIGNMENT // math.gcd(
            ratio_height,
            _HOST_IMAGE_DIMENSION_ALIGNMENT,
        )
        ratio_multiplier = math.lcm(
            width_alignment_factor,
            height_alignment_factor,
        )
        unit_width = ratio_width * ratio_multiplier
        unit_height = ratio_height * ratio_multiplier
        units = max(
            1,
            math.ceil(width / unit_width),
            math.ceil(height / unit_height),
            math.ceil(
                math.sqrt(
                    _MIN_OPENAI_IMAGE_PIXELS / float(unit_width * unit_height)
                )
            ),
        )
        normalized_width = unit_width * units
        normalized_height = unit_height * units
        return (
            f"{normalized_width}x{normalized_height}",
            normalized_width,
            normalized_height,
        )

    scale = max(
        1.0,
        math.sqrt(_MIN_OPENAI_IMAGE_PIXELS / float(width * height)),
    )
    normalized_width = _round_up_to_alignment(width * scale)
    normalized_height = _round_up_to_alignment(height * scale)
    return (
        f"{normalized_width}x{normalized_height}",
        normalized_width,
        normalized_height,
    )


def _normalize_explicit_size(size: str) -> tuple[str, int, int]:
    match = _SIZE_RE.match(size)
    if not match:
        raise ValueError("Invalid image size format. Use WIDTHxHEIGHT, e.g. 2048x2048")

    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError("Image size width/height must be positive integers")
    return f"{width}x{height}", width, height


def _resolve_max_image_dimension(configured: int | None) -> int:
    """Effective longest-edge cap for generic/custom endpoints.

    Precedence: ``BOX_AGENT_IMAGE_MAX_DIM`` env (operational override) > the
    configured ``image_generation.max_dimension`` > the built-in default.
    A non-positive value disables the clamp; an unparseable env is ignored.
    """
    raw = os.environ.get(_MAX_DIM_ENV, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    if configured is not None:
        return int(configured)
    return _DEFAULT_MAX_IMAGE_DIMENSION


def _clamp_to_max_dimension(width: int, height: int, max_dim: int) -> tuple[str, int, int]:
    """Scale a size down so neither side exceeds ``max_dim``, preserving ratio.

    Only the generic/custom OpenAI-compatible branch uses this: those endpoints
    forward an explicit ``size`` unchanged and can 504 on oversized requests.
    OpenAI, Doubao/Seedream, and host-aligned endpoints keep their own size
    rules. ``max_dim <= 0`` disables the clamp.
    """
    longest = max(width, height)
    if max_dim <= 0 or longest <= max_dim:
        return f"{width}x{height}", width, height
    scale = max_dim / float(longest)
    clamped_width = max(1, int(width * scale))
    clamped_height = max(1, int(height * scale))
    return f"{clamped_width}x{clamped_height}", clamped_width, clamped_height


def _normalize_openai_explicit_size(size: str) -> tuple[str, int, int]:
    normalized_size, width, height = _normalize_explicit_size(size)
    pixel_count = width * height
    if pixel_count >= _MIN_OPENAI_IMAGE_PIXELS:
        return normalized_size, width, height

    scale = math.sqrt(_MIN_OPENAI_IMAGE_PIXELS / float(pixel_count))
    normalized_width = max(width, math.ceil(width * scale))
    normalized_height = max(height, math.ceil(height * scale))
    return (
        f"{normalized_width}x{normalized_height}",
        normalized_width,
        normalized_height,
    )


def _compose_openai_prompt(prompt: str, style: str | None, negative_prompt: str | None) -> str:
    parts = [prompt.strip()]
    if style:
        parts.append(f"Style: {style.strip()}")
    if negative_prompt:
        parts.append(f"Avoid: {negative_prompt.strip()}")
    return "\n\n".join(part for part in parts if part)


def _derive_edit_endpoint(endpoint: str) -> str:
    normalized = endpoint.strip()
    if normalized.rstrip("/").endswith("/images/edits"):
        return normalized
    trimmed = normalized.rstrip("/")
    for suffix in _EDIT_ENDPOINT_SUFFIXES:
        if trimmed.endswith(suffix):
            return f"{trimmed[: -len(suffix)]}/images/edits"
    return f"{trimmed}/edits"


class GenerateImageTool(Tool):
    """Generate an image through a configured HTTP service and save it locally."""

    parallel_safe = True  # independent HTTP calls, output_path per call — no shared state

    def __init__(
        self,
        workspace_dir: str = ".",
        output_dir: str | None = None,
        allow_full_access: bool = True,
        permission_engine: PermissionEngine | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        auth_file: str | None = None,
        timeout: float | None = None,
        max_dimension: int | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).absolute()
        self.output_dir = Path(output_dir).absolute() if output_dir else self.workspace_dir
        self.allow_full_access = allow_full_access
        self._perm = permission_engine
        self.endpoint = resolve_image_generation_endpoint(endpoint)
        self.api_key = api_key if api_key is not None else _first_env(_API_KEY_ENV)
        self.model = model or _DEFAULT_MODEL
        self.auth_file = auth_file or ""
        self.timeout = timeout
        self.max_dimension = _resolve_max_image_dimension(max_dimension)

    @property
    def name(self) -> str:
        return "generate_image"

    @property
    def description(self) -> str:
        return (
            "Standard Box-Agent image tool shared by CLI and ACP. Generate or edit a bitmap image "
            "with the Box-Agent-configured image service, save it inside "
            "the active project/artifact root, and return a local path for use in HTML/PPTX assets. "
            "For explicit native image, illustration, poster, cover, or bitmap infographic requests, "
            "prefer this tool over HTML/CSS, SVG, PIL, or screenshot generation unless the user asks "
            "for one of those editable/rendered formats. Use text-to-image when the user "
            "asks for a new image. Use image-to-image with reference_images when the user asks to modify, "
            "redraw, restyle, or preserve a supplied image/logo/sketch. Use for PPT image_plan items marked "
            "`generate`. If the user forbids fallback and this tool fails, report the request as blocked. "
            "If the service is not configured, report the blocked image generation instead of "
            "pretending an asset exists. Configuration: image_generation.endpoint in config.yaml; optional "
            "image_generation.api_key for a dedicated bearer token. Environment overrides are also supported."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed image prompt. Do not ask the service to render text inside the image unless explicitly required.",
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Project/artifact-root-relative image path, for example "
                        "assets/generated/slide-03-hero.png. In output mode the tool already "
                        "targets the current output root; do not prefix the path with output/."
                    ),
                },
                "size": {
                    "type": "string",
                    "description": "Pixel size to request, e.g. 2048x2048.",
                    "default": _DEFAULT_LARGE_IMAGE_SIZE,
                },
                "width": {
                    "type": "integer",
                    "description": "Legacy requested width. Seedream/Doubao will map this to a supported size.",
                },
                "height": {
                    "type": "integer",
                    "description": "Legacy requested height. Seedream/Doubao will map this to a supported size.",
                },
                "style": {
                    "type": "string",
                    "description": "Optional visual style hint.",
                },
                "alt_text": {
                    "type": "string",
                    "description": "Optional accessibility text for the generated asset.",
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "Optional things to avoid.",
                },
                "image_mode": {
                    "type": "string",
                    "enum": ["text_to_image", "image_to_image"],
                    "description": (
                        "Use image_to_image when editing or restyling supplied reference images. "
                        "Defaults to image_to_image if reference_images is non-empty."
                    ),
                },
                "reference_images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Workspace-relative image paths to preserve or edit. Required for image_to_image tasks "
                        "such as 'change this logo', 'use this sketch', or 'based on this image'."
                    ),
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional metadata such as slide, purpose, placement, kind, or aspect ratio.",
                    "additionalProperties": True,
                },
                "watermark": {
                    "type": "boolean",
                    "description": (
                        "Internal flag for the 'AI 生成' watermark, ON by default. "
                        "DO NOT set this. Omit it for every normal image so the watermark stays on. "
                        "Only pass false when the PPTX skill instructs it, or the user EXPLICITLY says "
                        "to remove/disable the watermark. A plain, solid-color, test, or 'no text' image "
                        "is NOT a reason to disable it."
                    ),
                },
                "watermark_text": {
                    "type": "string",
                    "description": (
                        "Custom watermark text overriding the default 'AI 生成'. "
                        "Only set when the user explicitly asks for specific watermark wording (e.g. a brand name)."
                    ),
                },
            },
            "required": ["prompt", "output_path"],
        }

    async def execute(
        self,
        prompt: str,
        output_path: str,
        width: int = 1024,
        height: int = 1024,
        size: str | None = None,
        style: str | None = None,
        negative_prompt: str | None = None,
        alt_text: str | None = None,
        image_mode: str | None = None,
        reference_images: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        watermark: bool = True,
        watermark_text: str | None = None,
    ) -> ToolResult:
        if not self.endpoint:
            return ToolResult(
                success=False,
                error=(
                    "Image generation service is not configured. Set image_generation.endpoint in "
                    "config.yaml, or provide BOX_AGENT_IMAGE_GENERATION_ENDPOINT / BOX_AGENT_IMAGE_GEN_ENDPOINT."
                ),
            )

        try:
            reference_images = reference_images or []
            normalized_mode = (image_mode or "").strip().lower()
            edit_requested = bool(reference_images) or normalized_mode in _IMAGE_EDIT_MODES
            if edit_requested and not reference_images:
                return ToolResult(
                    success=False,
                    error="Image-to-image editing requires at least one reference image path.",
                )

            requested_width = width
            requested_height = height
            if size and size.strip():
                if _is_host_aligned_image_service(self.endpoint):
                    _, width, height = _normalize_explicit_size(size)
                    size, width, height = _normalize_host_image_size(width, height)
                elif _is_openai_image_service(self.endpoint):
                    size, width, height = _normalize_openai_explicit_size(size)
                else:
                    size, width, height = _normalize_explicit_size(size)
                    # Generic/custom endpoints have no downstream size rule of
                    # their own and would forward an oversized request as-is
                    # (a common cause of 504s). Clamp the longest edge.
                    size, width, height = _clamp_to_max_dimension(
                        width, height, self.max_dimension
                    )
            if _is_large_image_service(self.endpoint, self.model):
                size, width, height = _seedream_size_for_ratio(width, height)
            else:
                if not size or not size.strip():
                    if _is_host_aligned_image_service(self.endpoint):
                        size, width, height = _normalize_host_image_size(width, height)
                    else:
                        size, width, height = _openai_image_size(width, height)

            target = self._resolve_output_path(output_path)
            permission_error = self._check_write_permission(target)
            if permission_error:
                return permission_error

            reference_paths: list[Path] = []
            if edit_requested:
                try:
                    reference_paths = [self._resolve_readable_path(path) for path in reference_images]
                except ValueError as exc:
                    return ToolResult(success=False, error=str(exc))
                image_bytes, mime_type = await self._request_image_edit(
                    prompt=prompt,
                    size=size,
                    style=style,
                    negative_prompt=negative_prompt,
                    reference_paths=reference_paths,
                )
            else:
                image_bytes, mime_type = await self._request_image(
                    prompt=prompt,
                    size=size,
                    style=style,
                    negative_prompt=negative_prompt,
                )

            target = self._ensure_extension(target, mime_type)
            permission_error = self._check_write_permission(target)
            if permission_error:
                return permission_error

            if watermark:
                image_bytes, watermark_status = apply_text_watermark(
                    image_bytes, mime_type, watermark_text
                )
            else:
                watermark_status = {
                    "applied": False,
                    "reason": "disabled by caller",
                    "font": "",
                }

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(image_bytes)

            rel_path = self._display_path(target)
            artifact_rel_path = self._artifact_display_path(target)
            info = {
                "type": "artifact",
                "kind": "image",
                "filename": target.name,
                "rel_path": rel_path,
                "artifact_rel_path": artifact_rel_path,
                "abs_path": str(target),
                "uri": target.as_uri(),
                "mime": mime_type,
                "size_bytes": len(image_bytes),
                "path": artifact_rel_path,
                "absolute_path": str(target),
                "mime_type": mime_type,
                "bytes": len(image_bytes),
                "width": width,
                "height": height,
                "size": size,
                "requested_width": requested_width,
                "requested_height": requested_height,
                "image_mode": "image_to_image" if edit_requested else "text_to_image",
                "reference_images": [self._artifact_display_path(path) for path in reference_paths],
                "alt_text": alt_text or "",
                "metadata": metadata or {},
                "watermark": watermark_status,
            }
            return ToolResult(
                success=True,
                content=(
                    f"Generated image saved to [{artifact_rel_path}]\n"
                    f"mime_type: {mime_type}\n"
                    f"bytes: {len(image_bytes)}\n"
                    f"watermark: {'applied' if watermark_status.get('applied') else 'not applied'}"
                    + (
                        f" ({watermark_status['reason']})"
                        if not watermark_status.get("applied") and watermark_status.get("reason")
                        else ""
                    )
                ),
                raw_output=info,
                model_context=json.dumps(info, ensure_ascii=False),
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"Image generation failed: {exc}")

    def _resolve_output_path(self, output_path: str) -> Path:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            # Accept either the canonical artifact-relative form
            # (assets/generated/x.png) or the workspace-relative form returned
            # by older runtimes (output/assets/generated/x.png) without nesting
            # the artifact root twice.
            try:
                output_from_workspace = self.output_dir.relative_to(self.workspace_dir)
            except ValueError:
                output_from_workspace = None
            if (
                output_from_workspace
                and path.parts[: len(output_from_workspace.parts)] == output_from_workspace.parts
            ):
                path = self.workspace_dir / path
            else:
                path = self.output_dir / path
        return path

    def _check_write_permission(self, target: Path) -> ToolResult | None:
        if self._perm:
            decision = self._perm.check(
                capability="filesystem.write",
                resource={"path": str(target)},
                tool_name=self.name,
            )
            if not decision.allowed:
                return ToolResult(
                    success=False,
                    error=decision.reason,
                    permission_request=decision.permission_request,
                )
        elif not self.allow_full_access:
            error = validate_path_in_workspace(target, self.workspace_dir)
            if error:
                return ToolResult(success=False, error=error)
        return None

    def _resolve_readable_path(self, image_path: str) -> Path:
        path = Path(image_path).expanduser()
        if not path.is_absolute():
            output_candidate = self.output_dir / path
            workspace_candidate = self.workspace_dir / path
            path = output_candidate if output_candidate.exists() else workspace_candidate
        path = path.absolute()

        if self._perm:
            decision = self._perm.check(
                capability="filesystem.read",
                resource={"path": str(path)},
                tool_name=self.name,
            )
            if not decision.allowed:
                raise ValueError(decision.reason)
        elif not self.allow_full_access:
            error = validate_path_in_workspace(path, self.workspace_dir)
            if error:
                raise ValueError(error)
        if not path.exists():
            raise ValueError(f"Reference image not found: {self._display_path(path)}")
        if not path.is_file():
            raise ValueError(f"Reference image is not a file: {self._display_path(path)}")
        return path

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, image/*"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            return headers
        return request_auth_headers(auth_file=self.auth_file, existing=headers, url=self.endpoint)

    def _log_response_meta(self, mode: str, endpoint: str, response: httpx.Response) -> None:
        log_image_generation_response_meta(
            mode=mode,
            endpoint=endpoint,
            status_code=response.status_code,
            headers=response.headers,
        )

    def _log_error_meta(
        self,
        mode: str,
        endpoint: str,
        *,
        response: httpx.Response | None = None,
        exc: BaseException | None = None,
    ) -> None:
        body: str | None = None
        if response is not None:
            try:
                body = response.text
            except Exception:
                body = None
        log_image_generation_error_meta(
            mode=mode,
            endpoint=endpoint,
            status_code=response.status_code if response is not None else None,
            headers=response.headers if response is not None else None,
            response_body=body,
            error=str(exc) if exc is not None else None,
        )

    def _file_log_entry(self, field: str, path: Path, image_bytes: bytes, mime_type: str) -> dict[str, Any]:
        return {
            "field": field,
            "filename": path.name,
            "path": self._display_path(path),
            "mime_type": mime_type,
            "size_bytes": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
            "content": "<binary omitted>",
        }

    @staticmethod
    def _raise_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = response.text.strip()
            if len(body) > 1000:
                body = f"{body[:1000]}..."
            detail = f"{exc}"
            if body:
                detail = f"{detail}; response body: {body}"
            raise ValueError(detail) from exc

    async def _image_from_response(self, client: httpx.AsyncClient, response: httpx.Response) -> tuple[bytes, str]:
        self._raise_status(response)

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type.startswith("image/"):
            return response.content, content_type

        data = response.json()
        found = _find_first_image_payload(data)
        if not found:
            raise ValueError("service response did not contain image bytes, b64_json, base64, url, or image_url")
        kind, value = found
        if kind == "base64":
            return _decode_base64_image(value)

        download = await client.get(value)
        download.raise_for_status()
        download_type = download.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not download_type.startswith("image/"):
            download_type = mimetypes.guess_type(value)[0] or _DEFAULT_MIME_TYPE
        return download.content, download_type

    async def _request_image(
        self,
        *,
        prompt: str,
        size: str,
        style: str | None,
        negative_prompt: str | None,
    ) -> tuple[bytes, str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": _compose_openai_prompt(prompt, style, negative_prompt),
            "size": size,
        }

        timeout = self.timeout if self.timeout is not None else float(os.environ.get(_TIMEOUT_ENV, _DEFAULT_TIMEOUT))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            request = client.build_request(
                "POST",
                self.endpoint,
                json=payload,
                headers=self._request_headers(),
            )
            log_image_generation_request(
                mode="text_to_image",
                endpoint=str(request.url),
                method=request.method,
                headers=request.headers,
                json_payload=payload,
                configured_model=self.model,
                timeout=timeout,
            )
            response: httpx.Response | None = None
            try:
                response = await client.send(request)
                self._log_response_meta("text_to_image", str(request.url), response)
                return await self._image_from_response(client, response)
            except Exception as exc:
                self._log_error_meta(
                    "text_to_image",
                    str(request.url),
                    response=response,
                    exc=exc,
                )
                raise

    async def _request_image_edit(
        self,
        *,
        prompt: str,
        size: str,
        style: str | None,
        negative_prompt: str | None,
        reference_paths: list[Path],
    ) -> tuple[bytes, str]:
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        file_log_entries: list[dict[str, Any]] = []
        for path in reference_paths:
            image_bytes = path.read_bytes()
            mime_type = mimetypes.guess_type(str(path))[0] or _guess_mime_from_bytes(image_bytes)
            if not mime_type.startswith("image/"):
                raise ValueError(f"Unsupported reference image type: {self._display_path(path)}")
            files.append(("image", (path.name, image_bytes, mime_type)))
            file_log_entries.append(self._file_log_entry("image", path, image_bytes, mime_type))

        data = {
            "prompt": _compose_openai_prompt(prompt, style, negative_prompt),
            "size": size,
        }
        timeout = self.timeout if self.timeout is not None else float(os.environ.get(_TIMEOUT_ENV, _DEFAULT_TIMEOUT))
        endpoint = _derive_edit_endpoint(self.endpoint or "")
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            request = client.build_request(
                "POST",
                endpoint,
                data=data,
                files=files,
                headers=self._request_headers(),
            )
            log_image_generation_request(
                mode="image_to_image",
                endpoint=str(request.url),
                method=request.method,
                headers=request.headers,
                multipart_fields=data,
                files=file_log_entries,
                configured_model=self.model,
                timeout=timeout,
            )
            response: httpx.Response | None = None
            try:
                response = await client.send(request)
                self._log_response_meta("image_to_image", str(request.url), response)
                return await self._image_from_response(client, response)
            except Exception as exc:
                self._log_error_meta(
                    "image_to_image",
                    str(request.url),
                    response=response,
                    exc=exc,
                )
                raise

    def _ensure_extension(self, target: Path, mime_type: str) -> Path:
        if target.suffix:
            return target
        return target.with_suffix(_MIME_EXTENSIONS.get(mime_type, ".png"))

    def _display_path(self, target: Path) -> str:
        try:
            return str(target.relative_to(self.workspace_dir))
        except ValueError:
            try:
                return str(target.relative_to(self.output_dir))
            except ValueError:
                return str(target)

    def _artifact_display_path(self, target: Path) -> str:
        try:
            return str(target.relative_to(self.output_dir))
        except ValueError:
            return self._display_path(target)
