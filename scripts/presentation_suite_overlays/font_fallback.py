"""Keep Standard presentation delivery available when fonts cannot be rebuilt."""


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"SN font fallback overlay needs review: {old[:90]!r}")
    return text.replace(old, new, 1)


_FONT_READ = '''        if not source.is_file() or not source.stat().st_size:
            raise FileNotFoundError(f"custom font source is missing: {item['source_path']}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != str(item.get("sha256") or ""):
            raise ValueError(f"custom font hash mismatch: {item['source_path']}")
        if not _font_source_readable(str(source)):
            raise ValueError(f"custom font is unreadable: {item['source_path']}")
'''

_ACTIVE_TOKENS = '''    source_dirs = _font_source_dirs()
    fallback = "Noto Sans SC"
    if not _family_available(fallback, source_dirs):
        raise FileNotFoundError("Noto Sans SC is required as the portable font fallback")
    resolved = {token: _resolve_token(token, definitions) for token in sorted(tokens)}
    return {
        token: family if _family_available(family, source_dirs) else fallback
        for token, family in resolved.items()
    }
'''

_STAGE_FAMILY = '''def _stage_family_faces(tool, family, custom, source_dirs, temporary_dir, characters_file, delivery):
    """Build all faces before publishing any output for the family."""
    faces = (
        (Face((str(custom["source"]),), custom["weight"], custom["style"]),)
        if custom else FAMILY_FACES[family]
    )
    outputs = []
    for face in faces:
        source = Path(face.source_names[0]) if custom else _find_source(face, source_dirs)
        metadata = _font_source_metadata(str(source))
        provisional = temporary_dir / f"{_slug(family)}-{face.weight.replace(' ', '-')}.woff2"
        _subset(tool, source, provisional, characters_file, delivery, face.weight, face.style)
        outputs.append((face, source, metadata, provisional.read_bytes()))
    return outputs


'''

_OLD_FAMILY_LOOP = '''        for family in sorted(families):
            custom = custom_fonts.get(family)
            faces = (
                (Face((str(custom["source"]),), custom["weight"], custom["style"]),)
                if custom else FAMILY_FACES[family]
            )
            for face in faces:
                source = Path(face.source_names[0]) if custom else _find_source(face, source_dirs)
                source_metadata = _font_source_metadata(str(source))
                license_metadata = (
                    {"license": "user-provided", "source": "user upload"}
                    if custom else FONT_LICENSES[family]
                )
                delivery = delivery_families[family]
                provisional = temporary_dir / f"{_slug(family)}-{face.weight.replace(' ', '-')}.woff2"
                _subset(
                    tool, source, provisional, characters_file,
                    delivery, face.weight, face.style,
                )
                data = provisional.read_bytes()
'''

_FAMILY_LOOP = '''        for family in ordered_families if tool else ():
            if family == alternate and fallback in bundled and family not in families:
                continue
            custom = custom_fonts.get(family)
            delivery = delivery_families[family]
            try:
                outputs = _stage_family_faces(
                    tool, family, custom, source_dirs, temporary_dir, characters_file, delivery,
                )
            except Exception as exc:
                # Font readers and subsetters can raise format-specific errors.
                # Publication and its I/O errors stay outside this recovery path.
                font_warnings.append(f"{family}: could not bundle font ({type(exc).__name__}); using fallback")
                continue
            bundled[family] = delivery
            license_metadata = (
                {"license": "user-provided", "source": "user upload"}
                if custom else FONT_LICENSES[family]
            )
            for face, source, source_metadata, data in outputs:
'''

_OLD_OVERRIDES = '''    fallback_delivery = delivery_families["Noto Sans SC"]
    overrides = "\\n".join(
        f"  {token}: {json.dumps(delivery_families[family])}, "
        # Latin display / mono / English-handwriting / user subsets carry no CJK
        # glyphs → append the bundled Noto CJK fallback so stray Chinese never
        # falls through to a viewer/worker cartoon face (and both authoring and
        # delivery renders resolve CJK to the same metrics, killing the overflow).
        + (f"{json.dumps(fallback_delivery)}, " if not _covers_cjk(family) else "")
        + f"{_generic_for(family)};"
        for token, family in sorted(token_families.items())
    )
'''

_OVERRIDES = '''    fallback_family = next((family for family in bundled if _covers_cjk(family)), None)
    fallback_delivery = bundled.get(fallback_family)
    if tool and not fallback_family:
        font_warnings.append("No portable CJK fallback is available; using system fonts for missing glyphs")
    overrides = []
    for token, requested in sorted(token_families.items()):
        family = requested if requested in bundled else fallback_family
        stack = [json.dumps(bundled[family])] if family else []
        if fallback_delivery and family != fallback_family and not _covers_cjk(family):
            stack.append(json.dumps(fallback_delivery))
        generic = _generic_for(family or requested)
        stack.append(generic)
        token_families[token] = family or generic
        overrides.append(f"  {token}: {', '.join(stack)};")
    overrides = "\\n".join(overrides)
'''


def _font_bundle(text: str) -> str:
    text = _replace_once(text, "def _load_custom_config(root: Path)", (
        "def _load_custom_config(root: Path, *, font_warnings: list[str] | None = None)"
    ))
    text = _replace_once(text, _FONT_READ, (
        "        try:\n" + "".join("    " + line for line in _FONT_READ.splitlines(keepends=True))
        + '            source_family = _font_source_metadata(str(source))["source_family"]\n'
        + "        except (OSError, ValueError) as exc:\n"
        + "            if font_warnings is None:\n"
        + "                raise\n"
        + '            font_warnings.append(f"{exc}; using fallback")\n'
        + "            continue\n"
    ))
    text = _replace_once(text, '            "source_family": _font_source_metadata(str(source))["source_family"],\n',
                         '            "source_family": source_family,\n')
    text = _replace_once(text,
        '                raise ValueError(f"custom font role {role} references a missing font")\n',
        '                message = f"custom font role {role} references a missing font"\n'
        '                if font_warnings is None:\n'
        '                    raise ValueError(message)\n'
        '                font_warnings.append(f"{message}; using fallback")\n'
        '                continue\n')
    text = _replace_once(text, _ACTIVE_TOKENS,
                         '    return {token: _resolve_token(token, definitions) for token in sorted(tokens)}\n')
    text = _replace_once(text, 'def bundle_fonts(\n', _STAGE_FAMILY + 'def bundle_fonts(\n')
    text = _replace_once(text, '    custom_fonts, role_overrides = _load_custom_config(root)\n',
                         '    font_warnings: list[str] = []\n'
                         '    custom_fonts, role_overrides = _load_custom_config(root, font_warnings=font_warnings)\n')
    text = _replace_once(text,
        '    # A portable CJK/symbol fallback is always bundled, including when a user\n'
        '    # font is assigned to body text but does not cover every deck character.\n'
        '    families.add("Noto Sans SC")\n',
        '    fallback, alternate = "Noto Sans SC", "Noto Serif SC"\n')
    text = _replace_once(text, '        raise FileNotFoundError("pyftsubset is required for portable deck fonts")\n',
                         '        font_warnings.append("pyftsubset is unavailable; using system fonts")\n'
                         '    ordered_families = [fallback, alternate, *sorted(families - {fallback, alternate})]\n')
    text = _replace_once(text, '        family: f"Deck-{deck_id}-{_slug(family)}" for family in sorted(families)\n',
                         '        family: f"Deck-{deck_id}-{_slug(family)}" for family in ordered_families\n')
    text = _replace_once(text, '    records: list[dict[str, str]] = []\n',
                         '    bundled: dict[str, str] = {}\n'
                         '    records: list[dict[str, str]] = []\n')
    text = _replace_once(text, _OLD_FAMILY_LOOP, _FAMILY_LOOP)
    text = _replace_once(text, _OLD_OVERRIDES, _OVERRIDES)
    return _replace_once(text, '        "delivery_families": delivery_families,\n',
                         '        "delivery_families": bundled,\n'
                         '        "font_warnings": font_warnings,\n')


def _deck(text: str) -> str:
    text = _replace_once(text, '        manifest = font_manifest if font_manifest is not None else bundle_workspace(Path(base))\n',
                         '        manifest = font_manifest\n'
                         '        if manifest is None or validate_font_bundle(root):\n'
                         '            manifest = bundle_workspace(root)\n')
    text = _replace_once(text, '    freshness_errors = validate_render_freshness(Path(base))\n    if freshness_errors:\n',
                         '    for warning in manifest.get("font_warnings", []):\n'
                         '        print(f"Font fallback: {warning}", file=sys.stderr)\n'
                         '    freshness_errors = validate_render_freshness(Path(base))\n'
                         '    if freshness_errors:\n')
    text = _replace_once(text, 'def _validate_runtime_dependencies(root: Path, expected: int | None = None, *, check_player: bool = True) -> None:',
                         'def _validate_runtime_dependencies(root: Path, expected: int | None = None, *, check_player: bool = True) -> list[str]:')
    text = _replace_once(text, '    owners = list(slides)\n    base_css = root / "base.css"\n',
                         '    font_warnings = []\n'
                         '    owners = list(slides)\n    base_css = root / "base.css"\n')
    text = _replace_once(text,
        '                errors.append(f"{owner.relative_to(root).as_posix()}: {reference} -> {shown} {error}")\n',
        '                message = f"{owner.relative_to(root).as_posix()}: {reference} -> {shown} {error}"\n'
        '                if error == "is missing or empty" and resolved.suffix.lower() in {".woff", ".woff2", ".ttf", ".otf", ".ttc"}:\n'
        '                    font_warnings.append(message + "; using CSS fallback")\n'
        '                else:\n'
        '                    errors.append(message)\n')
    text = _replace_once(text,
        '        raise ValueError("portable runtime dependency audit failed:\\n" + "\\n".join(errors))\n',
        '        raise ValueError("portable runtime dependency audit failed:\\n" + "\\n".join(errors))\n'
        '    for warning in font_warnings:\n'
        '        print(f"Font fallback: {warning}", file=sys.stderr)\n'
        '    return font_warnings\n')
    text = _replace_once(text, '            _validate_runtime_dependencies(root, expected, check_player=False)\n'
                         '            stage = "fonts"\n'
                         '            manifest = bundle_workspace(root)\n',
                         '            stage = "fonts"\n'
                         '            manifest = bundle_workspace(root)\n'
                         '            stage = "validate"\n'
                         '            warnings = _validate_runtime_dependencies(root, expected, check_player=False)\n'
                         '            if warnings:\n'
                         '                manifest.setdefault("font_warnings", []).extend(warnings)\n'
                         '            stage = "fonts"\n')
    text = _replace_once(text, '    if errors:\n        raise ValueError("stale Deck font aliases',
                         '    if errors and manifest.get("font_warnings"):\n'
                         '        manifest["font_warnings"].extend(f"Unavailable deck font alias: {error}" for error in errors)\n'
                         '    elif errors:\n        raise ValueError("stale Deck font aliases')
    return _replace_once(text, '                          "images": images, **paths, "deck_dir": str(root),\n',
                         '                          "images": images, **paths, "deck_dir": str(root),\n'
                         '                          "font_warnings": manifest.get("font_warnings", []),\n')


def _pptx_builder(text: str) -> str:
    text = _replace_once(text, '''  const primary = cssValue.split(',')[0].trim().replace(/^['"]|['"]$/g, '');
  if (mapping.has(primary)) return JSON.stringify(mapping.get(primary));
  if (/^(?:User::|Deck-)/i.test(primary)) {
    throw new Error('Missing source font mapping; rebuild the font bundle before exporting PPTX.');
  }
  return cssValue;
''', '''  let remaining = cssValue;
  while (remaining) {
    const [head] = remaining.split(',', 1);
    const primary = head.trim().replace(/^['"]|['"]$/g, '');
    if (mapping.has(primary)) return JSON.stringify(mapping.get(primary));
    if (!/^(?:User::|Deck-)/i.test(primary)) return remaining;
    remaining = remaining.slice(head.length + 1).trim();
  }
  return 'sans-serif';
''')
    text = _replace_once(text,
        "    throw new Error('Cannot read source font manifest; rebuild the font bundle: ' + error.message);\n",
        "    return mapping;\n")
    text = _replace_once(text,
        "    throw new Error('Invalid source font manifest; rebuild the font bundle before exporting PPTX.');\n",
        "    console.error('[WARN] Invalid source font manifest; using CSS font fallbacks');\n"
        "    return mapping;\n")
    text = _replace_once(text,
        "      throw new Error('Invalid delivery font family; rebuild the font bundle before exporting PPTX.');\n",
        "      console.error('[WARN] Invalid delivery font family; using CSS font fallbacks');\n"
        "      continue;\n")
    return _replace_once(text,
        "      throw new Error('Invalid source font family in assets/fonts/manifest.json; '\n"
        "        + 'rebuild the font bundle from the original uploaded fonts before exporting PPTX.');\n",
        "      console.error('[WARN] Invalid source font family; using CSS font fallbacks');\n"
        "      continue;\n")


def apply(relative: str, data: bytes) -> bytes:
    transforms = {
        "skills/sn-ppt-standard/scripts/font_bundle.py": _font_bundle,
        "skills/sn-ppt-standard/scripts/deck.py": _deck,
        "skills/sn-ppt-standard/scripts/export_pptx/lib/pptx_builder.mjs": _pptx_builder,
    }
    transform = transforms.get(relative)
    return transform(data.decode("utf-8")).encode("utf-8") if transform else data
