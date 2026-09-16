"""SVG image extensions must remain valid alongside their raster fallback."""

from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "box_agent/skills/pptx/scripts"))

from presentation_delivery import inspect_pptx
from tests.test_presentation_delivery import pptx, rewrite_zip


@pytest.mark.parametrize("fault", [None, "missing", "malformed", "external"])
def test_svg_image_extension_requires_valid_local_media(tmp_path, fault):
    path = tmp_path / "vector-image.pptx"
    pptx(path, pages=1)

    def add_svg(entries):
        slide = "ppt/slides/slide1.xml"
        rels = "ppt/slides/_rels/slide1.xml.rels"
        entries[slide] = entries[slide].replace(
            b'<a:blip r:embed="image1"/>',
            b'<a:blip r:embed="image1"><a:extLst><a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}">'
            b'<asvg:svgBlip xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" '
            b'r:embed="svg1"/></a:ext></a:extLst></a:blip>',
        )
        mode = ' TargetMode="External"' if fault == "external" else ""
        relationship = (
            '<Relationship Id="svg1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"'
            f' Target="../media/image1.svg"{mode}/></Relationships>'
        ).encode()
        entries[rels] = entries[rels].replace(b"</Relationships>", relationship)
        if fault != "missing":
            entries["ppt/media/image1.svg"] = (
                b"not SVG" if fault == "malformed" else
                b'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90"/></svg>'
            )

    rewrite_zip(path, add_svg)
    if fault:
        with pytest.raises(ValueError, match="(?i)(image|media|svg)"):
            inspect_pptx(path, expected_pages=1)
    else:
        assert inspect_pptx(path, expected_pages=1)["pages"] == 1
