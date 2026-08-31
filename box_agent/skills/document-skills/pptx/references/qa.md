# PPTX QA Reference

Use the applicable part of this checklist after creating a controlled HTML deck
or creating/editing a `.pptx` export.

## Required Checks

Keep every temporary report, helper script, extracted text file, and rendered
image inside the current canonical delivery root selected by the runtime. Do
not add another `output/` prefix, and do not write to `/tmp`, `/var/tmp`, or an
unrelated absolute temp path.

For the default controlled HTML route, the blocking checks are
`qa/outline_check.json`, `qa/deck_contract.json`, `qa/deck_spec.json`,
`qa/image_manifest.json`, `qa/html_self_check.json`, and
`qa/runtime_probe.json`; they must exist and pass. Run self-check against
`index.html`. Generate `qa/truth_check.json` afterward as a source advisory.
Its missing sources, unverified URLs, private-fact gaps, or `"ok": false` result
must not block, invalidate, or reopen an otherwise usable `index.html`.
Report research quality separately from presentation QA: a `partial` or
`framework` research handoff may produce a valid deck, but must not be described
as a full-quality research pass.
`deck_spec` also verifies bound `outline_intent`: every contract-v2 page keeps
the exact outline title/message/layout/visual metadata, uses a compatible
registered layout, and honors explicit visual counts such as three stages,
four tags, four milestones, or four quadrants. A schema-valid deck that fails
this semantic check is not complete.
If a spec issue class repeats twice, stop automatic repair and follow the
structural repair rules in `SKILL.md`. Never start a repair loop for a
source/truth advisory; keep scaffolded `source_facts`/`research_facts` and
summarize only its user-visible impact under the localized usage-note label
defined in `SKILL.md` §6.

For HTML-first decks exported with `scripts/html_to_editable_pptx.js` and
`dom-to-pptx`, inspect both the source HTML preview PNGs and the rendered PPTX
when renderer runtime exists. If rendering is blocked (missing `soffice`/PDF
renderer), continue with the rest of QA and report render as blocked.

Editable export can reflow text, shift layers, or lose CSS effects, so source
previews alone are not enough.

0. HTML self-check for HTML-first decks:
   - Run `${BOX_AGENT_NODE:-node} scripts/html_self_check.js index.html --dom-to-pptx --allow-local-images --report qa/html_self_check.json` for controlled decks before export (use `deck.html` only on the legacy/custom route), or rely on `scripts/html_to_editable_pptx.js` which writes the same check internally.
   - Always use the stricter `--dom-to-pptx` compatibility profile for new HTML-first decks.
   - Confirm every `.slide` reports exactly `1920x1080` unless the user explicitly requested a nonstandard output size.
   - Confirm `qa/html_self_check.json` exists, is non-empty, and has `"ok": true`. If it is missing, report HTML self-check as `BLOCKED`.
   - For every `[data-pptx-diagram]`, confirm the report found a non-empty
     `data-diagram-spec` or `data-diagram-spec-src`, exactly one direct inline
     SVG root, and no decoration classification. Confirm export reports
     `diagramCount` and `diagramVectorExport: true` when diagrams exist.
   - Fix failures before export. This catches DOM/CSS layout bugs such as progress `.fill` elements left as `display:inline`, zero-size bars/charts, text overflow, missing images, and content outside the slide.
   - If the command exits non-zero, inspect the report file before concluding the error has no detail. Summarize concrete failures and fix the HTML; route-change and bypass rules live in `SKILL.md`.
   - This is a preflight gate, not visual QA. Passing it does not mean the slide looks good.

1. Package validation:
   - Run `${BOX_AGENT_PYTHON:-python3} scripts/validate_pptx_package.py output.pptx`.
   - Fix zip, relationship, missing part, or invalid XML errors before delivery.
   - For a technical-diagram export, inspect the slide relationship/drawing:
     the primary picture must use an SVG image relationship (PowerPoint may
     also package a compatibility fallback PNG), and the diagram must not be
     present in the `pptx-bg` background bitmap.

2. Text extraction:
   - Run `${BOX_AGENT_PYTHON:-python3} scripts/extract_text.py output.pptx`.
   - Verify slide count, slide order, expected titles, and requested content.
   - Verify visible page numbers against actual slide order. Use a consistent
     `NN / TOTAL` style such as `03 / 08`; non-cover slides should not silently
     omit folios. A cover may omit the page number only when the deck uses that
     convention intentionally and the next slide starts at `02 / TOTAL`.

3. Placeholder scan:
   - Check for `lorem`, `ipsum`, `todo`, `placeholder`, `xxxx`, and template instructions.
   - Treat hits in notes, masters, and layouts as warnings unless the user asked to edit them.
   - Reject empty or failed files for required blocking QA. A 0-byte
     JSON/TXT/MD report is not a pass; rerun the check, replace it with a real
     report, or mark that blocking check `BLOCKED`. A missing/empty source
     advisory is a limitation, not an HTML blocker.

4. Render:
   - Run `${BOX_AGENT_PYTHON:-python3} scripts/render_pptx.py output.pptx --out rendered`.
   - Do not pre-check `soffice` and skip this command. The render script owns renderer discovery, Quick Look fallback, and missing-LibreOffice messaging.
   - If this command fails due to dependency/runtime missing, treat render as blocked and continue: `Rendering: BLOCKED`.
   - If rendering is blocked by permissions or missing runtime after the deck changed, previous render images are stale. Do not use old renders as final proof for the new deck.
   - Do not pass `--format png` unless a PNG is explicitly required; the default JPG output is intentionally compressed to reduce file size.
   - The preferred path is `soffice` PPTX-to-PDF plus `pdftoppm` PDF-to-PNG.
   - If Poppler is unavailable, the script may use Node pdf.js with `pdfjs-dist` and `@napi-rs/canvas`.
   - On macOS, Quick Look is only a lightweight fallback.

## Blocked Rendering

If no per-slide image or preview is produced, report:

```text
Rendering: BLOCKED
Reason: missing soffice, missing PDF renderer, or conversion failure
```

Still report package validation, text extraction, and placeholder scan results.
Do not say "OOXML checks ensure quality" or "rendering is complete" when
`render_pptx.py` was not run. OOXML checks are structural checks only.
If `soffice` is missing, tell the user that LibreOffice is required for
PPTX-to-PDF conversion and provide this official download page:
`https://www.libreoffice.org/download/download-libreoffice/`.
Do not imply that Node pdf.js can replace LibreOffice; Node pdf.js only handles
PDF-to-PNG after a PDF already exists.
This requirement also applies when macOS Quick Look succeeds, because Quick
Look is only a lightweight fallback and does not provide full per-slide QA.
On Windows/Linux, Quick Look is not available for this role. If `soffice` is
missing, emit `Rendering: BLOCKED`.

## Office Raccoon Command Safety

The permission engine may block common shell habits. Avoid them.

- Do not write QA logs to `/tmp`, `/dev/null`, `/var/tmp`, or absolute output paths.
- Do not use shell redirects for QA output. The permission engine can block
  redirects before it proves whether the path is safe, especially for absolute
  paths. Instead, run helpers that write files directly or pass explicit output
  arguments such as `--out rendered`.
- Do not use inline heredocs such as `python3 - <<'PY'`.
- Do not chain a long command that mixes `unzip`, `cat`, `tail`, and inline Python.
- Do not call Node `execFileSync()` with a full shell command string such as
  `execFileSync("unzip -l deck.pptx")`; Node treats the whole string as the
  executable name. Use `execFileSync("unzip", ["-l", "deck.pptx"])` or the
  provided Python helper scripts.
- Do not manually probe Quick Look with `qlmanage -h >/dev/null`; run
  `scripts/render_pptx.py` and let it decide whether Quick Look is available.
- Prefer the provided helper scripts. If custom logic is needed, create a short
  `.py` or `.js` helper file inside the workspace, run it, and let that helper
  write outputs to paths such as `qa/package_check.txt`,
  `qa/text_extract.txt`, or `rendered/`.

For package tests, prefer Python `zipfile` or Node zip readers over extracting
the deck to a temporary directory.

## Deliverable Package Hygiene

Before final handoff, inspect the output folder.

- Remove `.DS_Store`, editor caches, temporary scratch scripts, failed downloads,
  and unreferenced intermediate files.
- Remove or replace empty QA artifacts such as 0-byte `.json`, `.txt`, or `.md`
  files.
- Keep the final deck, source file(s), and rendered previews.
- Do not create a ZIP archive unless the user asked for one. Normal delivery
  should list the `.pptx`, source file(s), and speaker notes paths.

## Reporting Format

QA reports remain machine-readable files under `qa/`. Retain their summary
counts and relevant paths under the generation-details heading localized for
the active response language. Render `qa_ok` and `qa_warnings` with the matching
QA labels from `SKILL.md` §6; never expose those raw key names.
Keep technical artifact names such as `deck.patch.json` and directories such as
`qa/` and `research/` literal. Do not enumerate validator or script names.

Translate QA into concise user impact in the active response language:

1. Localized complete status: preview and editing are available and no finding requires
   user action.
2. Localized complete status + usage note: preview and editing are available, but a
   concrete note changes how the result should be published or completed.
3. Localized editable-draft status: a usable artifact exists, but a named presentation issue
   may affect display, editing, or export.
4. Localized incomplete status: no trustworthy primary presentation artifact exists.

Purely structural or diagnostic warnings may contribute to the localized QA
notice count in generation details, but they do not require a usage-note explanation.
When a finding matters, state whether it affects preview, editing, download,
export, or public use and tell the user what action is recommended. Technical
evidence remains available in the QA files and the host's collapsed process
details.
