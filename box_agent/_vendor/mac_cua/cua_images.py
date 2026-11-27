"""Model-visible image sizes, using capture metadata before encoded headers.

Guest resources use only the standard library. Reading a header does not decode
or transform an image, and cannot establish the image's input coordinate space.
"""
from __future__ import annotations

import base64
import binascii
import struct


def valid_dimensions(value: object) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(n) is int and n > 0 for n in value))


def encoded_dimensions(raw: bytes) -> tuple[int, int] | None:
    """Read PNG, JPEG, GIF or WebP pixel dimensions without decoding pixels."""
    size = None
    if len(raw) >= 24 and raw[:16] == b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR':
        size = struct.unpack_from('>II', raw, 16)
    elif len(raw) >= 10 and raw[:6] in (b'GIF87a', b'GIF89a'):
        size = struct.unpack_from('<HH', raw, 6)
    elif raw.startswith(b'\xff\xd8'):
        offset = 2
        while offset < len(raw):
            if raw[offset] != 0xff:
                break
            while offset < len(raw) and raw[offset] == 0xff:
                offset += 1
            if offset >= len(raw):
                break
            marker = raw[offset]
            offset += 1
            if marker in (0xd9, 0xda):
                break
            if marker == 0x01 or 0xd0 <= marker <= 0xd7:
                continue
            if offset + 2 > len(raw):
                break
            length = int.from_bytes(raw[offset:offset + 2], 'big')
            if length < 2 or offset + length > len(raw):
                break
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                          0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf) and length >= 8:
                height, width = struct.unpack_from('>HH', raw, offset + 3)
                size = (width, height)
                break
            offset += length
    elif len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        end = int.from_bytes(raw[4:8], 'little') + 8
        offset = 12
        while offset + 8 <= end <= len(raw):
            kind = raw[offset:offset + 4]
            length = int.from_bytes(raw[offset + 4:offset + 8], 'little')
            start = offset + 8
            if start + length > end:
                break
            body = raw[start:start + min(length, 10)]
            if kind == b'VP8X' and length >= 10:
                size = (1 + int.from_bytes(body[4:7], 'little'),
                        1 + int.from_bytes(body[7:10], 'little'))
            elif kind == b'VP8L' and length >= 5 and body[0] == 0x2f:
                bits = int.from_bytes(body[1:5], 'little')
                size = (1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff))
            elif kind == b'VP8 ' and length >= 10 and body[3:6] == b'\x9d\x01\x2a':
                width, height = struct.unpack_from('<HH', body, 6)
                size = (width & 0x3fff, height & 0x3fff)
            if size is not None:
                break
            offset = start + length + (length % 2)
    return size if valid_dimensions(size) else None


def image_content(image: dict, dimensions: object = None) -> list[dict]:
    """Keep the original image and put its own size immediately before it.

    Native dimensions must belong to this image, never to the last observation.
    Unknown or malformed images remain visible; missing metadata must not change
    a tool's success/error semantics or hide output preceding an error.
    """
    if not valid_dimensions(dimensions):
        try:
            dimensions = encoded_dimensions(base64.b64decode(image.get('data', ''), validate=True))
        except (ValueError, TypeError, binascii.Error):
            dimensions = None
    text = (f'Image dimensions: width={dimensions[0]} px, height={dimensions[1]} px.'
            if valid_dimensions(dimensions) else
            'Image dimensions unavailable: the image header could not be read.')
    return [{'type': 'text', 'text': text}, image]


def annotate_result(result: dict) -> dict:
    """Enrich MCP image parts, preserving other result and content fields."""
    content = result.get('content')
    if not isinstance(content, list):
        return result
    images = [part for part in content if isinstance(part, dict) and part.get('type') == 'image']
    if not images:
        return result
    metadata = result.get('structuredContent')
    dimensions = ([metadata.get('screenshot_width'), metadata.get('screenshot_height')]
                  if len(images) == 1 and isinstance(metadata, dict) else None)
    output = []
    for part in content:
        if isinstance(part, dict) and part.get('type') == 'image':
            output.extend(image_content(part, dimensions))
        else:
            output.append(part)
    return {**result, 'content': output}
