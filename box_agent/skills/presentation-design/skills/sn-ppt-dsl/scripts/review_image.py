"""Encode model-facing copies; original assets and slide rendering remain separate."""
import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageOps, UnidentifiedImageError


def jpeg_bytes(image):
    rgb = ImageOps.exif_transpose(image).convert("RGBA")
    background = Image.new("RGB", rgb.size, "white")
    background.paste(rgb, mask=rgb.getchannel("A"))
    background.thumbnail((1568, 1568), Image.Resampling.LANCZOS)
    out = BytesIO()
    background.save(out, format="JPEG", quality=90, subsampling=0, optimize=True)
    return out.getvalue()


def write_copy(image, target):
    data = jpeg_bytes(image)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
    return sha256(data).hexdigest()


def asset_copy(deck, source):
    source = source.resolve()
    if not source.is_relative_to(deck.resolve()):
        raise ValueError("素材须在任务目录内")
    digest = sha256(source.read_bytes()).hexdigest()
    dest = deck / "qa" / "asset-views" / f"{source.stem}-{digest[:20]}.jpg"
    if not dest.is_file():
        try:
            with Image.open(source) as image:
                write_copy(image, dest)
        except UnidentifiedImageError:
            return None
    return str(dest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="为原生生图或附件一次生成看图副本，保留原素材")
    parser.add_argument("deck", type=Path)
    parser.add_argument("files", type=Path, nargs="*")
    args = parser.parse_args()
    deck = args.deck.resolve()
    sources = [deck / path for path in args.files] or sorted(
        path for path in (deck / "assets").rglob("*")
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"})
    print(json.dumps([{"asset": str(path), "inspect": asset_copy(deck, path)} for path in sources],
                     ensure_ascii=False))
