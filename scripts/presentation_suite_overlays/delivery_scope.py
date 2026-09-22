"""Declare production workspaces and explicitly register finished deliveries."""


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"SN delivery overlay needs review: {old!r}")
    return text.replace(old, new, 1)


HELPERS = '''def _write_delivery_record(target, content):
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                     prefix=".delivery-", suffix=".tmp", delete=False) as stream:
        temporary = stream.name
        stream.write(content)
    try:
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _declare_delivery_scope(root):
    from pathlib import Path
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _write_delivery_record(root / ".artifact-delivery.json",
        '{"schema_version":1,"default":"intermediate"}\\n')


def _publish_delivery_file(filename):
    from pathlib import Path
    target = Path(filename)
    if not target.is_file():
        raise ValueError(f"Delivery file does not exist: {target}")
    _write_delivery_record(target.with_name(f".{target.name}.artifact.json"),
        '{"type":"artifact"}\\n')


'''


def apply(relative, data):
    targets = {
        "skills/sn-ppt-standard/scripts/deck.py",
        "skills/sn-ppt-entry/scripts/parse_user_docs.py",
        "skills/sn-ppt-dazzle/scripts/render_deck.py",
        "skills/sn-ppt-standard/scripts/export_pptx/html_to_pptx.mjs",
        "skills/sn-ppt-standard/references/box-agent-tool-contract.md",
    }
    if relative not in targets:
        return data
    text = data.decode("utf-8")
    if relative.endswith(".md"):
        return (text + '\n\n## Artifact delivery boundary\n\n'
            '`deck.py prepare` declares the task directory as a working-file scope. '
            'Images generated there default to intermediate, even when `publish_artifact` is omitted. '
            'Do not set it to true for PPT illustrations. `asset-contact` sheets are internal QA files. '
            '`deck.py build` registers only the finished `present.html` and whole-deck overview; '
            'the PPTX exporter registers the finished PPTX. For extra files explicitly requested by '
            'the user, run `deck.py publish "$DECK_DIR" --path <relative-file>` after generating them. '
            'Do not publish individual assets or review sheets as a routine final step.\n').encode("utf-8")
    if relative.endswith(".py"):
        position = text.index("\ndef ") + 1
        text = text[:position] + HELPERS + text[position:]
    if relative.endswith("/deck.py"):
        text = replace_once(text, '        "asset-assign", "asset-contact", "asset-review", "material-figure",\n',
            '        "asset-assign", "asset-contact", "asset-review", "material-figure", "publish",\n')
        text = replace_once(text, '        if name == "asset-register":\n',
            '        if name == "publish":\n'
            '            command.add_argument("--path", action="append", required=True)\n'
            '        if name == "asset-register":\n')
        text = replace_once(text, '        if args.command == "sync":\n',
            '        if args.command == "publish":\n'
            '            files = [(root / value).resolve() for value in args.path]\n'
            '            for file in files:\n'
            '                file.relative_to(root)\n'
            '                if not file.is_file():\n'
            '                    raise ValueError(f"Delivery file does not exist: {file}")\n'
            '            for file in files:\n'
            '                _publish_delivery_file(file)\n'
            '                print(f"[{file}]")\n'
            '        elif args.command == "sync":\n')
        text = replace_once(text, '    root = Path(args.root).resolve()\n',
                            '    root = Path(args.root).resolve()\n    _declare_delivery_scope(root)\n')
        text = replace_once(text, '    _build_contact(root, expected)\n',
            '    _build_contact(root, expected)\n'
            '    _publish_delivery_file(root / "present.html")\n'
            '    _publish_delivery_file(root / "renders/contact-sheet.png")\n')
    elif relative.endswith("/parse_user_docs.py"):
        text = replace_once(text, '    asset_root = Path(args.asset_dir).expanduser().resolve() if args.asset_dir else None\n',
            '    if args.output:\n        _declare_delivery_scope(Path(args.output).expanduser().resolve().parent)\n'
            '    asset_root = Path(args.asset_dir).expanduser().resolve() if args.asset_dir else None\n')
    elif relative.endswith("/render_deck.py"):
        text = replace_once(text, '    out_dir = Path(args.out_dir)\n',
                            '    _declare_delivery_scope(html_path.resolve().parent)\n    out_dir = Path(args.out_dir)\n')
        text = replace_once(text, '    for p in paths:\n        print(p)\n',
            '    if args.all:\n'
            '        _publish_delivery_file(html_path)\n'
            '        if (out_dir / "contact_sheet.png").is_file():\n'
            '            _publish_delivery_file(out_dir / "contact_sheet.png")\n'
            '    for p in paths:\n        print(p)\n')
    else:
        # Only register after successful export and non-empty-file validation.
        text = replace_once(text, "  console.log(JSON.stringify({",
            "  if (result.failCount === 0) writeFileSync(resolve(dirname(outputPath), `.${basename(outputPath)}.artifact.json`), "
            "'{\"type\":\"artifact\"}\\n');\n  console.log(JSON.stringify({")
    return text.encode("utf-8")
