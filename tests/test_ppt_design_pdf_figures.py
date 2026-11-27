"""Exercise PDF attachment parsing and figure export via the built-in Skill's CLI."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image
import pytest


SCRIPTS = (
    Path(__file__).resolve().parents[1] / "box_agent/skills/presentation-design"
    / "skills/sn-ppt-design-entry/scripts"
)


def run_script(script, *args, env=None, check=True):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *map(str, args)],
        text=True, capture_output=True, env=env, check=check,
    )


def parse_pdf(source, deck, env=None):
    output = deck / "materials.json"
    run_script("parse_user_docs.py", "--files", source, "--asset-dir", deck / "assets",
               "--output", output, env=env)
    result = json.loads(output.read_text())
    assert not result["errors"]
    return result["documents"][0]


@pytest.fixture
def indexed_pdf(tmp_path):
    pdf = pytest.importorskip("pymupdf")
    pytest.importorskip("pypdf")
    source = tmp_path / "original.pdf"
    bitmap = io.BytesIO()
    Image.new("RGB", (36, 24), "red").save(bitmap, "PNG")
    with pdf.open() as doc:
        page = doc.new_page(width=200, height=140)
        image_id = page.insert_image(pdf.Rect(10, 10, 70, 50), stream=bitmap.getvalue())
        page.insert_image(pdf.Rect(110, 10, 170, 50), xref=image_id)
        page.insert_text((10, 65), "Measured area")
        second = doc.new_page(width=200, height=140)
        second.draw_rect(pdf.Rect(10, 10, 90, 70), fill=(0, 0, 1))
        second.set_rotation(90)
        doc.save(source)
    materials = parse_pdf(source, tmp_path / "deck")
    assert materials["pages"] == 2 and "Measured area" in materials["text"]
    index = Path(materials["figure_index"])
    assert index.is_relative_to(tmp_path / "deck/assets")
    return source, index


def test_repeated_image_placements_and_caption_can_be_selected(indexed_pdf, tmp_path):
    _, index = indexed_pdf
    page = json.loads((index.parent / "p001.json").read_text())
    left, right = page["figures"]
    assert [left["id"], right["id"]] == ["F1", "F2"]
    assert left["xref"] == right["xref"] and left["box"] != right["box"]
    assert "T1" in left["nearby_text"]
    for operation, name, extra in (
        ("image", "source.png", ["--ids", "F1"]),
        ("region", "with-caption.png", ["--ids", "F1", "T1"]),
        ("sheet", "numbered.jpg", []),
    ):
        run_script("pdf_figures.py", operation, "--index", index, "--page", 1,
                   "--out", tmp_path / name, *extra)
    with Image.open(tmp_path / "source.png") as image:
        assert image.size == (36, 24) and image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    with Image.open(tmp_path / "with-caption.png") as crop:
        assert crop.convert("L").getextrema()[0] < 50  # black caption and red image
        assert crop.convert("RGB").getpixel((12, 12)) == (255, 0, 0)
    with Image.open(tmp_path / "numbered.jpg") as sheet:
        assert sheet.format == "JPEG" and sheet.width > sheet.height


def test_grid_uses_the_displayed_orientation_and_rejects_invalid_selections(indexed_pdf, tmp_path):
    source, index = indexed_pdf
    output = tmp_path / "rotated.png"
    args = ["--index", index, "--page", 2]
    run_script("pdf_figures.py", "region", *args, "--grid", "A1-F8", "--out", output)
    with Image.open(output) as region:
        assert region.height > region.width
        assert region.convert("RGB").getpixel((region.width * 7 // 10, region.height // 5)) == (0, 0, 255)
    missing = tmp_path / "invalid.png"
    bad = run_script("pdf_figures.py", "region", *args, "--grid", "A2-G4", "--out", missing, check=False)
    assert bad.returncode and "A1 through F8" in bad.stderr and not missing.exists()
    with source.open("ab") as stream:
        stream.write(b"\n% changed since indexing\n")
    stale = run_script("pdf_figures.py", "sheet", *args, "--out", missing, check=False)
    assert stale.returncode and "source PDF has changed" in stale.stderr and not missing.exists()


def test_missing_figure_backend_keeps_basic_pdf_image_extraction(tmp_path):
    pytest.importorskip("pypdf")
    source = tmp_path / "scan.pdf"
    Image.new("RGB", (60, 40), "green").save(source, "PDF")
    # Simulate a client whose Python has the basic reader but no figure backend.
    (tmp_path / "pymupdf.py").write_text("raise ImportError('figure backend unavailable')\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")])
    materials = parse_pdf(source, tmp_path / "deck", env)
    assert materials["pages"] == 1 and "figure backend unavailable" in materials["figure_index_warning"]
    assert "figure_index" not in materials
    assert Path(materials["inherited_images"][0]["path"]).is_file()
