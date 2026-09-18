"""Keep automatically generated presentation overviews out of delivery cards."""


def apply(relative: str, data: bytes) -> bytes:
    replacements = {
        "skills/sn-ppt-standard/scripts/deck.py": (
            '            _publish_delivery_file(root / "renders/contact-sheet.png")',
            '            _mark_intermediate_artifact(root / "renders/contact-sheet.png")',
        ),
        "skills/sn-ppt-dazzle/scripts/render_deck.py": (
            '            _publish_delivery_file(out_dir / "contact_sheet.png")',
            '            _mark_intermediate_artifact(out_dir / "contact_sheet.png")',
        ),
    }
    if relative not in replacements:
        return data
    old, new = replacements[relative]
    text = data.decode("utf-8")
    if text.count(old) != 1:
        raise ValueError(f"Task artifact overlay needs review: {relative}")
    return text.replace(old, new, 1).encode("utf-8")
