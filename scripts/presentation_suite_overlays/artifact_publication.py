"""Keep SN render/QA images on disk without publishing them as deliverables."""

from __future__ import annotations


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"SN artifact publication overlay needs review: {old!r}")
    return text.replace(old, new, 1)


_HELPER = '''def _mark_intermediate_artifact(filename):
    """Mark this exact QA output for Box-Agent automatic artifact discovery."""
    from pathlib import Path

    target = Path(filename)
    target.with_name(f".{target.name}.artifact.json").write_text(
        '{"type":"intermediate_asset"}\\n', encoding="utf-8"
    )


'''


def apply(relative: str, data: bytes) -> bytes:
    targets = {
        "skills/sn-ppt-standard/scripts/render.py",
        "skills/sn-ppt-standard/scripts/deck.py",
        "skills/sn-ppt-dazzle/scripts/render_deck.py",
    }
    if relative not in targets:
        return data
    text = data.decode("utf-8")
    # Insert before the first top-level function, after imports/constants.
    position = text.index("\ndef ") + 1
    text = text[:position] + _HELPER + text[position:]
    if relative.endswith("/render.py"):
        text = _replace_once(text, "        pg.screenshot(path=out)",
                             "        _mark_intermediate_artifact(out)\n        pg.screenshot(path=out)")
    elif relative.endswith("/deck.py"):
        for statement in (
            '        path = render_dir / "contact-sheet-focus.png"',
            '            path = render_dir / f"contact-sheet-review-{index:02d}.png"',
        ):
            indent = statement[:len(statement) - len(statement.lstrip())]
            text = _replace_once(text, statement, statement + "\n" + indent + "_mark_intermediate_artifact(path)")
    else:
        for indent in ("        ", "            "):
            statement = indent + 'out.write_bytes(s["png"])'
            # Include the newline so the shorter indentation does not match twice.
            text = _replace_once(text, "\n" + statement,
                                 "\n" + indent + "_mark_intermediate_artifact(out)\n" + statement)
    return text.encode("utf-8")
