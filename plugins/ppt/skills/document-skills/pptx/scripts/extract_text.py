#!/usr/bin/env python3
"""Extract readable slide text from a PPTX file."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def slide_order(zf: zipfile.ZipFile) -> list[str]:
    presentation = ET.fromstring(zf.read("ppt/presentation.xml"))
    rels = ET.fromstring(zf.read("ppt/_rels/presentation.xml.rels"))
    rid_to_target = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall(f"{REL_NS}Relationship")
        if rel.attrib.get("Type", "").endswith("/slide")
    }

    slides: list[str] = []
    for sld_id in presentation.findall(".//p:sldId", NS):
        rid = sld_id.attrib.get(f"{{{NS['r']}}}id")
        target = rid_to_target.get(rid or "")
        if target:
            slides.append(f"ppt/{target}")
    return slides


def extract_slide_text(zf: zipfile.ZipFile, slide_path: str) -> list[str]:
    root = ET.fromstring(zf.read(slide_path))
    paragraphs: list[str] = []
    for paragraph in root.findall(".//a:p", NS):
        text = "".join(node.text or "" for node in paragraph.findall(".//a:t", NS)).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract slide text from a PPTX file.")
    parser.add_argument("pptx", type=Path)
    args = parser.parse_args()

    if not args.pptx.is_file() or args.pptx.suffix.lower() != ".pptx":
        print(f"Error: not a .pptx file: {args.pptx}", file=sys.stderr)
        return 2

    try:
        with zipfile.ZipFile(args.pptx) as zf:
            for index, slide_path in enumerate(slide_order(zf), start=1):
                print(f"# Slide {index}: {Path(slide_path).name}")
                lines = extract_slide_text(zf, slide_path)
                if lines:
                    for line in lines:
                        print(line)
                else:
                    print("[no text]")
                print()
    except KeyError as exc:
        print(f"Error: missing required PPTX part: {exc}", file=sys.stderr)
        return 1
    except zipfile.BadZipFile:
        print("Error: file is not a valid zip package", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
