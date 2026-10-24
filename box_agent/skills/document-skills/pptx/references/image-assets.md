# Offline image and manifest policy

The offline package uses `mode: "local_only"`. Image search, image generation and
remote downloads are unavailable; do not probe them or retry. Creative style does
not imply generated media. Use editable text, charts, diagrams and local shapes
when no supplied image exists. Do not create fake bitmaps or documentary evidence.

## Local assets

1. Identify the supplied local image and its page/slot before selecting an image-required layout.
2. Pass repeated `--image-asset SLIDE:SLOT=PATH` arguments when scaffolding, for example
   `--image-asset 1:hero=/absolute/path/client-ui.png`. PNG/JPG/JPEG/WEBP files are
   copied under `assets/source/`, hashed and recorded as `use_existing` with
   `acquire_via: user`. The final deck must use the portable copied path.
3. Without an asset, choose a registered layout whose media is optional. Such rows
   become `skip` with `acquire_via: none`; no search or generation jobs are created.
4. Required media without a local file remains an unresolved requirement. Ask the
   design role to correct the affected page, or deliver a disclosed degraded draft
   when an explicit requested image cannot be supplied. Do not change the manifest
   to pretend the requirement was met.
5. Use existing structured content patches and synchronization commands from
   `SKILL.md`. Never rewrite the manifest or locked visual design by hand.
6. Run `validate_image_manifest.js` normally. It accepts local assets and rejects
   online acquisition in a `local_only` manifest; missing required media still fails.

## Portable delivery

Keep slide assets and QA screenshots private as intermediate files. Use
`publish_artifact: false` when supported by the local tool; only publish a separate
image when the user explicitly requests that deliverable.

Use only relative local files or embedded data in final HTML. No remote image,
font, stylesheet or script URL may be required for display or export. Keep source
files and reports beside the HTML. Existing source URLs may remain as citations,
but are never fetched. File existence and geometry checks do not prove image
identity; disclose unavailable optional vision without blocking local production.

The legacy `localize_web_image.py` helper is not part of the offline workflow.
Do not use generated-image retries, creative-image minimums or online fallback.
