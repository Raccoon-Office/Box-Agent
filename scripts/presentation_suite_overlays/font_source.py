"""Checked overlays preserving uploaded fonts' original names in PPTX output."""


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"SN font source overlay needs review: expected one occurrence of {old[:90]!r}")
    return text.replace(old, new, 1)


_OLD_METADATA = '''@lru_cache(maxsize=None)
def _font_source_metadata(path: str) -> dict[str, str]:
    """Read human-facing copyright/license fields retained in the source font."""
    from fontTools.ttLib import TTFont

    font = TTFont(path, lazy=False)
    try:
        names = font["name"]
        return {
            "copyright": names.getDebugName(0) or "",
            "license_description": names.getDebugName(13) or "",
            "license_url": names.getDebugName(14) or "",
        }
    finally:
        font.close()
'''

_NEW_METADATA = '''def _valid_source_family(value) -> bool:
    if not isinstance(value, str):
        return False
    family = value.strip()
    return bool(family and any(char.isalnum() for char in family)
        and not family.lower().startswith(("user::", "deck-"))
        and not any(ord(char) < 32 or 127 <= ord(char) <= 159
                    or char == "\\ufffd" for char in family))


def _original_font_family(names) -> str:
    """Prefer typographic family (16), then legacy family (1), across locales."""
    for name_id in (16, 1):
        records = [record for record in names.names if record.nameID == name_id]
        # Prefer an English name when available, then other Unicode/local names.
        # Font IDs, configured display labels and filenames are not family names.
        records.sort(key=lambda record: (
            not ((record.platformID == 3 and record.langID & 0x3FF == 9)
                 or (record.platformID == 1 and record.langID == 0)),
            not record.isUnicode(), record.platformID, record.platEncID, record.langID,
        ))
        for record in records:
            try:
                family = record.toUnicode(errors="strict").strip()
            except (UnicodeError, LookupError):
                continue
            if _valid_source_family(family):
                return family
    raise ValueError("source font has no usable family in its original name table")


def _font_source_metadata(path: str) -> dict[str, str]:
    """Read original family and license metadata before the subset is renamed."""
    from fontTools.ttLib import TTFont

    font = TTFont(path, lazy=False)
    try:
        try:
            names = font["name"]
            family = _original_font_family(names)
        except Exception as exc:
            raise ValueError("source font has no usable family in its original name table") from exc
        return {
            "source_family": family,
            "copyright": names.getDebugName(0) or "",
            "license_description": names.getDebugName(13) or "",
            "license_url": names.getDebugName(14) or "",
        }
    finally:
        font.close()
'''

_OLD_MAPPING = '''  try {
    const manifest = JSON.parse(readFileSync(path, 'utf-8'));
    for (const face of manifest.faces || []) {
      if (typeof face.delivery_family === 'string' && typeof face.source_family === 'string') {
        mapping.set(face.delivery_family, face.source_family);
      }
    }
  } catch (error) {
    console.error(`[WARN] 无法读取 PPTX 字体映射: ${error.message}`);
  }
  return mapping;
'''

_NEW_MAPPING = r'''  let manifest;
  try {
    manifest = JSON.parse(readFileSync(path, 'utf-8'));
  } catch (error) {
    console.error(`[WARN] 无法读取 PPTX 字体映射: ${error.message}`);
    throw new Error('Cannot read source font manifest; rebuild the font bundle: ' + error.message);
  }
  if (!manifest || !Array.isArray(manifest.faces)) {
    throw new Error('Invalid source font manifest; rebuild the font bundle before exporting PPTX.');
  }
  for (const face of manifest.faces || []) {
    if (!face || typeof face.delivery_family !== 'string' || !face.delivery_family.trim()) {
      throw new Error('Invalid delivery font family; rebuild the font bundle before exporting PPTX.');
    }
    const family = typeof face.source_family === 'string' ? face.source_family.trim() : '';
    if (!family || /^(?:User::|Deck-)/i.test(family)
        || /[\u0000-\u001f\u007f-\u009f\ufffd]/.test(family)
        || !/[\p{L}\p{N}]/u.test(family)) {
      throw new Error('Invalid source font family in assets/fonts/manifest.json; '
        + 'rebuild the font bundle from the original uploaded fonts before exporting PPTX.');
    }
    mapping.set(face.delivery_family, family);
  }
  return mapping;
'''

_OLD_FAMILY_LIST = r'''  // 按逗号分割，去掉引号，trim
  const families = cssValue.split(',').map(f => f.trim().replace(/^['"]|['"]$/g, ''));
'''
_NEW_FAMILY_LIST = r'''  // Commas inside quoted font names are not fallback separators.
  const tokens = cssValue.match(/(?:[^,'"\\]|\\.|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')+/g) || [];
  const families = tokens.map(token => {
    let name = token.trim();
    if ((name[0] === '"' || name[0] === "'") && name.endsWith(name[0])) name = name.slice(1, -1);
    return name.replace(/\\([0-9a-f]{1,6})\s?|\\(.)/gi, (_, hex, char) => {
      if (!hex) return char;
      const code = parseInt(hex, 16);
      return String.fromCodePoint(code && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? code : 0xfffd);
    });
  });
'''

_FONT_HELPERS = r'''// PptxGenJS 3.12 interpolates fontFace directly into XML attributes.
function pptxFontFace(cssValue) {
  const family = parseFontFamily(cssValue);
  return family == null ? family : family.replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/'/g, '&apos;');
}

function sourceFontFamily(cssValue, mapping) {
  // Browser subset aliases are generated identifiers, without quotes/commas.
  const primary = cssValue.split(',')[0].trim().replace(/^['"]|['"]$/g, '');
  if (mapping.has(primary)) return JSON.stringify(mapping.get(primary));
  if (/^(?:User::|Deck-)/i.test(primary)) {
    throw new Error('Missing source font mapping; rebuild the font bundle before exporting PPTX.');
  }
  return cssValue;
}

'''


def apply(relative: str, data: bytes) -> bytes:
    """Apply only reviewed source-family changes; fail closed on upstream drift."""
    if relative == "skills/sn-ppt-standard/scripts/font_bundle.py":
        text = _replace_once(data.decode("utf-8"), _OLD_METADATA, _NEW_METADATA)
        text = _replace_once(text, '            "source": source,\n',
                             '            "source": source,\n'
                             '            "source_family": _font_source_metadata(str(source))["source_family"],\n')
        text = _replace_once(text, '                        "source_family": family,\n',
                             '                        "source_family": custom["source_family"] if custom else family,\n')
        text = _replace_once(text, '    for record in manifest.get("faces", []):\n',
                             '    for record in manifest.get("faces", []):\n'
                             '        if not _valid_source_family(record.get("source_family")):\n'
                             '            errors.append("invalid source font family; rebuild the font bundle from the original fonts")\n')
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/lib/pptx_builder.mjs":
        text = _replace_once(data.decode("utf-8"), _OLD_MAPPING, _NEW_MAPPING)
        for expression in ("s.fontFamily", "run.fontFamily", "item.styles?.fontFamily", "styleSpec.typography.font_family"):
            text = _replace_once(text, f"parseFontFamily({expression})", f"pptxFontFace({expression})")
        text = _replace_once(text, "function readSourceFontFamilies(deckDir) {",
                             _FONT_HELPERS + "function readSourceFontFamilies(deckDir) {")
        text = _replace_once(text, "if (!mapping.size || !value || typeof value !== 'object')",
                             "if (!value || typeof value !== 'object')")
        text = _replace_once(text,
                             r'''JSON.stringify(mapping.get(item.split(',')[0].trim().replace(/^['"]|['"]$/g, ''))) || item''',
                             "sourceFontFamily(item, mapping)")
        text = _replace_once(text, "  const sourceFontFamilies = readSourceFontFamilies(deckDir);",
                             "  const sourceFontFamilies = readSourceFontFamilies(deckDir);\n"
                             "  const exportPages = pages.map(page => page.ir\n"
                             "    ? { ...page, ir: withSourceFontFamilies(page.ir, sourceFontFamilies) } : page);")
        text = _replace_once(text, "  for (const page of pages) {", "  for (const page of exportPages) {")
        text = _replace_once(text, "buildSlideFromIR(pptx, withSourceFontFamilies(page.ir, sourceFontFamilies), deckDir)",
                             "buildSlideFromIR(pptx, page.ir, deckDir)")
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/lib/style_parser.mjs":
        return _replace_once(data.decode("utf-8"), _OLD_FAMILY_LIST, _NEW_FAMILY_LIST).encode("utf-8")
    return data
