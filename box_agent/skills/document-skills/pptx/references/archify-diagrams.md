# Archify technical-diagram supplement

Archify is bundled under `../vendor/archify/` relative to this reference. It is
an optional diagram authoring/rendering dependency of PPTX, not another globally
registered Skill. No market installation, `get_skill("archify")`, npm install,
or update check is needed. Use the bundled snapshot; never update it during a deck task.
Its MIT license and third-party notices are included alongside `VENDORED.json`.

## Choose the representation before scaffolding

- Use the existing `technical-diagram-v1` for node/edge editing inside the deck.
- Use Archify when the user requests its visual style, or a technical page benefits
  from its architecture, workflow, sequence, dataflow, or lifecycle renderer and
  does not require node-level editing inside the slide.
- For an embedded Archify diagram, declare the outline layout as `image-feature-v1`
  and describe the visual as a wide image feature containing an Archify technical
  diagram. Keep the actual nodes, edges, and message in the outline content.
  This uses the existing source-backed image contract rather than silently
  converting a requested editable diagram into a picture.
- Keep quantitative charts in the existing editable chart workflow. Archify does
  not replace statistical charts, the deck theme, the deck compiler, or final QA.

## Author and validate

Resolve `PPTX_ROOT` from the loaded PPTX Skill path, and set `ARCHIFY_ROOT` to
`$PPTX_ROOT/vendor/archify`. Use absolute script paths and the host-provided Node
executable (`${BOX_AGENT_NODE:-node}`); keep all outputs in the task output root.

1. Read the matching schema, `schemas/common.schema.json`, and one matching JSON
   example under `ARCHIFY_ROOT`. For additional diagram authoring rules read
   `UPSTREAM_SKILL.md`; the PPTX integration here overrides its standalone viewer
   handoff, update checking, and desktop viewport requirements.
2. Write `assets/diagrams/slide-N.archify.json`. Start with one main path, at most
   12 primary nodes, short meaningful labels, and automatic routing. Set
   `meta.quality_profile: "showcase"`; adapt authored copy to the deck language.
   Use supporting slide text for extra detail rather than crowding the diagram.
3. Run the bundled CLI (replace TYPE with the chosen diagram type):

   ```bash
   "${BOX_AGENT_NODE:-node}" "$ARCHIFY_ROOT/bin/archify.mjs" validate TYPE assets/diagrams/slide-N.archify.json --quality showcase --json
   "${BOX_AGENT_NODE:-node}" "$ARCHIFY_ROOT/bin/archify.mjs" deliver TYPE assets/diagrams/slide-N.archify.json assets/diagrams/slide-N.archify.html --quality showcase --json
   ```

4. Repair only reported subjects using diagnostic evidence and supported fixes.
   If two consecutive repairs do not improve the best error count, preserve the
   source and report the failure; use the native technical-diagram layout when
   it still meets the request. A failed command never proves a successful diagram.

## Embed in the slide

Open the delivered HTML using the existing loopback browser workflow. Use its
canonical Export menu to download the full-diagram PNG, with the color mode that
fits the slide and sufficient resolution for the image region. Do not screenshot
the viewer toolbar, export a Share Card, crop labels, or assume an SVG's external
CSS will survive PPTX export. The interactive HTML itself is a companion artifact,
not an iframe or executable snippet to inject into a slide.

Save the export as `assets/diagrams/slide-N.archify.png`. Patch the scaffolded
`image-feature-v1` image property using its returned contract: local `src`,
descriptive `alt`, and `fit: "contain"`. Keep the slide title and narrative text
editable. Resolve the existing image-plan slot as `use_existing`, record the local
derived asset/source via the existing manifest contract, and synchronize its
status; do not mislabel it as a web photograph or AI-generated image. Do not invent
new manifest fields or invoke image generation for this slot.

Run normal deck finalization and the existing slide/export checks. Inspect the
diagram at actual slide size for legible labels, full containment, and a compatible
background. Archify's standalone validation does not prove slide readability.
Keep the JSON and checked HTML beside the exported image for future edits. Describe
the embedded diagram as an image; node edits require changing JSON and regenerating
the diagram. If browser export is unavailable, use the native editable diagram
when suitable, or report the missing embedded asset while preserving other slides.
