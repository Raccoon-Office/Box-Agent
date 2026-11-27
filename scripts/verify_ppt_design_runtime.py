"""Verify PDF figure lookup and PPTX export using the installed Skill's interpreter."""

import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas


def verify(runtime: Path, expected_version: str, skill_python: Path) -> None:
    runtime = runtime.resolve()
    # Keep the venv path: resolving its executable symlink would select the base Python.
    skill_python = skill_python.absolute()
    version = (runtime / "VERSION").read_text(encoding="utf-8").strip()
    if version != expected_version:
        raise ValueError(f"Expected installed runtime {expected_version}, found {version}")
    skills = runtime / "skills/presentation-design/skills"
    entry = skills / "sn-ppt-design-entry/scripts"
    cli = skills / "sn-ppt-dsl/scripts/dsl.py"
    with tempfile.TemporaryDirectory(prefix="ppt-design-export-") as folder:
        deck = Path(folder)
        assets = deck / "assets"
        assets.mkdir()
        env = {**os.environ, "PYTHONNOUSERSITE": "1", "NODE_PATH": "", "npm_config_offline": "true"}
        for name in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)

        def run(script: Path, *args: str | Path) -> None:
            subprocess.run([skill_python, script, *args], check=True, cwd=deck, env=env, timeout=120)

        pdf = deck / "source.pdf"
        canvas = Canvas(str(pdf), pagesize=(160, 120))
        canvas.drawImage(ImageReader(Image.new("RGB", (60, 40), "red")), 10, 66, 60, 40)
        canvas.setFont("Helvetica", 10)
        canvas.drawString(10, 55, "Figure 1")
        canvas.save()
        materials = deck / "materials.json"
        run(entry / "parse_user_docs.py", "--files", pdf, "--asset-dir", assets, "--output", materials)
        parsed = json.loads(materials.read_text(encoding="utf-8"))
        if parsed["errors"]:
            raise ValueError(f"PDF parsing failed: {parsed['errors']}")
        document, = parsed["documents"]
        if not document.get("figure_index"):
            raise ValueError(f"No figure index under {skill_python}: {document.get('figure_index_warning')}")
        index = Path(document["figure_index"])
        detail = json.loads((index.parent / "p001.json").read_text(encoding="utf-8"))
        figure, = detail["figures"]
        caption, = detail["text"]
        if caption["text"] != "Figure 1" or caption["id"] not in figure["nearby_text"]:
            raise ValueError("The PDF index did not associate the image with its caption")
        run(entry / "pdf_figures.py", "region", "--index", index, "--page", "1",
            "--ids", figure["id"], caption["id"], "--out", assets / "picture.png")
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
        run(cli, "build", source, "--output", output)
        slides = Presentation(output).slides
        if len(slides) != 1:
            raise ValueError("Expected a single slide in the exported PPTX")
        picture, = [shape.image for shape in slides[0].shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
        with Image.open(BytesIO(picture.blob)) as image:
            rgb = image.convert("RGB")
            if rgb.getpixel((rgb.width // 2, rgb.height // 3)) != (255, 0, 0):
                raise ValueError("The exported slide lost the red PDF image")
            caption_band = rgb.crop((0, rgb.height * 2 // 3, rgb.width, rgb.height))
            if caption_band.convert("L").getextrema()[0] >= 50:
                raise ValueError("The exported slide lost the black PDF caption")
    print(f"Verified {version} with {skill_python}: exported the indexed PDF image and caption in one PPTX")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("expected_version")
    parser.add_argument("--skill-python", type=Path, help="Client-managed interpreter for a slim runtime")
    args = parser.parse_args()
    bundled = args.runtime / "runtime/python/python.exe"
    skill_python = args.skill_python or (bundled if bundled.is_file() else os.getenv("BOX_AGENT_PYTHON"))
    if not skill_python:
        parser.error("Supply the installed Skill's interpreter with --skill-python or BOX_AGENT_PYTHON")
    verify(args.runtime, args.expected_version, Path(skill_python))
