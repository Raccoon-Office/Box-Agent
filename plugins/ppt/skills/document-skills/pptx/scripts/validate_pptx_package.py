#!/usr/bin/env python3
"""Lightweight PPTX package validation.

This does not replace opening/rendering the deck, but it catches common broken
zip, missing package parts, missing slide targets, and broken relationships.
"""

from __future__ import annotations

import argparse
import posixpath
import sys
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET


REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def rel_target_to_part(rels_part: str, target: str) -> str | None:
    if target.startswith(("http://", "https://", "mailto:")):
        return None
    if target.startswith("/"):
        return target.lstrip("/")

    rels_path = PurePosixPath(rels_part)
    if rels_path.parent.name == "_rels":
        source_dir = rels_path.parent.parent
    else:
        source_dir = rels_path.parent
    return posixpath.normpath(str(source_dir / target))


def validate(path: str) -> list[str]:
    errors: list[str] = []
    required = {
        "[Content_Types].xml",
        "_rels/.rels",
        "ppt/presentation.xml",
        "ppt/_rels/presentation.xml.rels",
    }

    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            missing = sorted(required - names)
            for part in missing:
                errors.append(f"missing required part: {part}")
            if missing:
                return errors

            presentation = ET.fromstring(zf.read("ppt/presentation.xml"))
            pres_rels = ET.fromstring(zf.read("ppt/_rels/presentation.xml.rels"))
            rid_to_slide = {
                rel.attrib["Id"]: rel.attrib["Target"]
                for rel in pres_rels.findall(f"{REL_NS}Relationship")
                if rel.attrib.get("Type", "").endswith("/slide")
            }

            slide_count = 0
            for sld_id in presentation.findall(f".//{{{P_NS}}}sldId"):
                slide_count += 1
                rid = sld_id.attrib.get(f"{{{R_NS}}}id")
                target = rid_to_slide.get(rid or "")
                if not target:
                    errors.append(f"presentation slide references missing relationship id: {rid}")
                    continue
                part = posixpath.normpath(f"ppt/{target}")
                if part not in names:
                    errors.append(f"presentation relationship points to missing slide: {part}")

            if slide_count == 0:
                errors.append("presentation contains no slides")

            for rels_part in sorted(name for name in names if name.endswith(".rels")):
                try:
                    root = ET.fromstring(zf.read(rels_part))
                except ET.ParseError as exc:
                    errors.append(f"invalid XML in {rels_part}: {exc}")
                    continue
                for rel in root.findall(f"{REL_NS}Relationship"):
                    target = rel.attrib.get("Target", "")
                    mode = rel.attrib.get("TargetMode")
                    if mode == "External":
                        continue
                    part = rel_target_to_part(rels_part, target)
                    if part and part not in names:
                        errors.append(f"{rels_part} points to missing part: {part}")
    except zipfile.BadZipFile:
        errors.append("not a valid zip package")
    except KeyError as exc:
        errors.append(f"missing required part: {exc}")
    except ET.ParseError as exc:
        errors.append(f"invalid package XML: {exc}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate basic PPTX package structure.")
    parser.add_argument("pptx")
    args = parser.parse_args()

    errors = validate(args.pptx)
    if errors:
        print("FAILED")
        for error in errors:
            print(f"- {error}")
        return 1

    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
