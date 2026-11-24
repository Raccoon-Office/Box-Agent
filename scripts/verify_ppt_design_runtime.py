"""Export a one-slide deck with a picture from an installed Box-Agent runtime."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE


def verify(runtime: Path, expected_version: str) -> None:
    runtime = runtime.resolve()
    version = (runtime / "VERSION").read_text(encoding="utf-8").strip()
    if version != expected_version:
        raise ValueError(f"Expected installed runtime {expected_version}, found {version}")
    cli = runtime / "skills/presentation-design/skills/sn-ppt-dsl/scripts/dsl.py"
    with tempfile.TemporaryDirectory(prefix="ppt-design-export-") as folder:
        deck = Path(folder)
        assets = deck / "assets"
        assets.mkdir()
        Image.new("RGB", (60, 40), "#4275ac").save(assets / "picture.png")
        (assets / "sources.json").write_text(
            '[{"file":"picture.png","role":"diagram","origin":"user_provided"}]', encoding="utf-8"
        )
        source = deck / "slide.xml"
        source.write_text(
            '<slide xmlns="https://www.larkoffice.com/sml/2.0"><data>'
            '<img src="assets/picture.png" topLeftX="80" topLeftY="80" width="240" height="140"/>'
            '</data></slide>', encoding="utf-8"
        )
        output = deck / "export.pptx"
        subprocess.run(
            [sys.executable, str(cli), "build", str(source), "--output", str(output)],
            check=True, cwd=deck, timeout=120,
            env={**os.environ, "NODE_PATH": "", "npm_config_offline": "true"},
        )
        slides = Presentation(output).slides
        if len(slides) != 1 or not any(
            shape.shape_type == MSO_SHAPE_TYPE.PICTURE and shape.image.ext == "png"
            for shape in slides[0].shapes
        ):
            raise ValueError("Expected one slide with an embedded PNG in the exported PPTX")
    print(f"Verified installed runtime {version}: exported one slide with an embedded PNG")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("expected_version")
    args = parser.parse_args()
    verify(args.runtime, args.expected_version)
