# HTML-First Editable Decks (legacy/custom HTML and optional export)

> New decks use the controlled `deck.json -> index.html` compiler described in
> `controlled-layouts.md`. This reference remains authoritative for the explicit
> free-form HTML escape route and for HTML-to-PPTX exporter behavior. Where this
> file says `deck.html`, controlled decks use their compiled `index.html`.

Use this workflow only for the explicit legacy/custom-HTML escape route. On
that route, author fixed-size HTML and optionally export it to editable PPTX
with `scripts/html_to_editable_pptx.js`, which loads the skill-bundled
`scripts/dom-to-pptx.bundle.js`. Controlled new decks stay on the registered
`deck.json -> index.html` compiler unless the user explicitly requests the
legacy escape route.

Before authoring a new deck when final `.pptx` delivery is expected, preflight
the browser export environment with `scripts/check_html_export_env.js` or the
host app's renderer bridge. A host app's Electron renderer can provide the
browser environment; an agent child process still needs a bridge or Playwright.
If Playwright/Chromium and host renderer are missing, tell the user this blocks
HTML-to-editable-PPTX export and ask them to choose:

- `HTML`: deliver `deck.html` now; editable PPTX export can run later after the
  browser dependency or host renderer is available.
- `PPTX`: switch to native PptxGenJS and create a directly editable PPTX, with
  different HTML/CSS fidelity tradeoffs.

Route-change and self-check bypass rules live in `SKILL.md`. This reference adds
HTML authoring details; do not use it to weaken the top-level workflow.

Do not use `python-pptx` or `from pptx import Presentation` to create the final
PPTX for a new deck. Python may be used for data preparation and QA helpers, but
the deck itself should be created from `deck.html` through the editable
DOM-to-PPTX exporter.

## Contract

- Build a fixed-size HTML slide deck first. Copy `references/starter/common.css`
  to `drafts/common.css` and use its locked `.slide` block (`width: 1920px;
  height: 1080px;`) verbatim — do not re-author these dimensions.
- If the requested deliverable is final `.pptx`, preflight the browser export
  environment before writing the full HTML deck.
- Before writing HTML, create the `outline.json` required by
  `references/outline.md`. For a sufficient page-level user outline, preserve it
  as a lightweight direct mapping; never invent extra claims, data, or strategy
  just to make the artifact look deeper.
- Use one `.slide` element per page. Do not author at `1280px × 720px`,
  `1280px × 760px`, viewport-relative sizes, or scaled wrappers. For a
  deliberately nonstandard size the user must opt in: pass `--canvas WxH` to both
  `html_self_check.js` and `html_to_editable_pptx.js` and set the same `WxH` in
  the `.slide` CSS.
- Run the stricter HTML self-check profile with `--dom-to-pptx`.
- Export with `scripts/html_to_editable_pptx.js`.
- Keep the source HTML and generated preview images beside the output deck for
  QA and future edits.
- Render the exported PPTX when the user requests visual validation or
  structural QA already suggests DOM-to-PPTX drift; otherwise keep the visual
  inspection explicitly unclaimed.
- Add a visible, consistent page number to every non-cover slide. Use one
  placement across the deck, preferably top-right or bottom-right, and keep the
  total count correct after adding/removing slides.

This produces an editable PowerPoint deck from HTML-authored slides. The output
is more editable than an image-only deck, but CSS-to-PPTX mapping can drift.
Rendered QA is required only to claim full visual validation; follow
`SKILL.md` §4.2 for its opt-in triggers. If a triggered render is unavailable or
blocked, report `Rendering: BLOCKED` and continue with a clear limitation.

## Data Charts And ECharts

For data-heavy slides, the HTML preview and the final PPTX must preserve the
same underlying data. Use ECharts in `deck.html` when it helps render a faithful
browser preview, interactions, or complex analytical styling, but treat it as a
preview surface, not the final PPT representation.

The controlled compiler bundles ECharts 6 locally and uses the SVG renderer;
generated `index.html` files with controlled charts therefore remain standalone
and do not depend on a CDN. `chart-bar-v1` covers simple rankings, while
`chart-data-v1` covers animated bar, column, line, area, pie, donut, and radar
charts. Their editable `deck.json` props are converted to ECharts options at
runtime and to native PptxGenJS charts during editable PPTX export.

Default data-display decision:

- If a slide has numbers, percentages, rankings, change over time,
  before/after comparisons, market sizing, benchmarks, KPIs, revenue, costs, or
  operational metrics, choose a visible data display first.
- Prefer a KPI strip for 2-4 headline metrics, a bar chart for ranked or
  categorical comparison, a line/area chart for trends, a matrix or heatmap for
  two-dimensional comparison, and a table only when exact values or labels
  matter.
- Use text-only bullets only when the available data is too sparse, the numbers
  are merely supporting detail, or the user asked for text-only slides.
- If the data is illustrative or assumed, label that in the outline evidence or
  notes; do not make invented values look sourced.

Required chart authoring pattern:

1. Store the chart data/spec in `assets/data/*.json` or in a local JSON script
   tag.
2. Mark the chart root with `data-pptx-chart`.
3. Link the chart root to recoverable data with `data-chart-spec-src`,
   `data-chart-spec`, or a child `<script type="application/json"
   data-chart-spec>`.
   If `assets/data/*.json` exists, `deck.html` must reference the relevant file
   with `data-chart-spec-src` instead of copying values into static SVG,
   absolute-positioned bars, or plain text.
4. When exporting to PPTX, convert available chart data to native PowerPoint
   charts/tables whenever the user may edit the numbers.
5. Never let an ECharts canvas/SVG become part of `assets/bg-capture/*.png`.
   If native chart conversion is unavailable, report chart export as `BLOCKED`
   or use the confirmed native `PptxGenJS` chart route instead of delivering a
   screenshot-only chart.

Example:

```html
<div class="chart-frame"
     data-pptx-chart
     data-chart-spec-src="assets/data/revenue-trend.json">
  <div id="revenue-trend-echarts" class="echarts-for-pptx"></div>
  <script type="application/json" data-chart-spec>
    {"type":"line","title":"Revenue Trend","categories":["Q1","Q2"],"series":[{"name":"Revenue","data":[120,156]}]}
  </script>
</div>
```

## File Layout

Create these files beside the output deck:

```text
deck.html
assets/
  data/
    chart-01.json
  generated/
    manifest.json
    slide-03-hero.png
slides/
  slide-01.png
  slide-02.png
qa/
  html_self_check.json
output.pptx
```

Use one `.slide` element per page:

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    html, body { margin: 0; background: #111; }
    .slide {
      width: 1920px;
      height: 1080px;
      position: relative;
      overflow: hidden;
      page-break-after: always;
      font-family: Aptos, Inter, Arial, sans-serif;
    }
  </style>
</head>
<body>
  <section class="slide" data-title="Title">...</section>
  <section class="slide" data-title="Agenda">...</section>
</body>
</html>
```

## Fragment Drafting

This is only for the explicit legacy/custom-HTML escape route. Controlled decks
render from `deck.json` regardless of slide count. For a large legacy deck:

1. Copy `references/starter/common.css` to `drafts/common.css`; keep the locked
   1920x1080 `.slide` geometry and define shared styles once.
2. Write contiguous fragment files such as `drafts/slides_01_04.html`. Each file
   contains only `<section class="slide" data-slide="NN">` blocks, numbered
   continuously from `01`; no document wrapper, `<style>`, or `<script>`.
3. Keep each fragment to roughly four slides or fewer. Charts in fragments use
   `data-chart-spec` or `data-chart-spec-src`, never inline script tags.
4. Merge once; never read all fragments back and re-serialize them through one
   `write_file` call. Sub-agents, when explicitly used for independent ranges,
   write their own fragment files and the parent only runs the merge command.

The merge command is:

```bash
${BOX_AGENT_NODE:-node} "$PPTX_SKILL_DIR/scripts/merge_html_fragments.js" \
  --css drafts/common.css \
  --out deck.html \
  --title "Deck title" \
  drafts/slides_01_04.html drafts/slides_05_08.html drafts/slides_09_12.html
```

## Generated Images

Plan bitmap acquisition after the outline and before writing final slide HTML.
Use hosted web image search first for real people, places, products, documentary
subjects, and photographic atmosphere. Use image generation directly for
invented or deliberately stylized visuals such as hero illustrations, product
concepts, abstract scenes, textures, and visual metaphors.

Default to `acquire_via: ai` for explicitly generated covers, dividers, posters,
brand campaigns, product concepts, vision/future-state pages, abstract concept
pages, and emotionally led closing pages. Use `acquire_via: web` for real or
ordinary photographic subjects; it calls `web_search` with `SearchType=image`
before retaining `generate` as a labelled concept fallback. Localize the selected
result with `scripts/localize_web_image.py`; web-search reuse rights remain
unverified until the source page is reviewed. Avoid generating
filler images just to dress up text-heavy slides, but
do not let generic caution suppress useful visuals. Every slide must get an
explicit image decision in `assets/generated/manifest.json`; pick `generate`
whenever a bitmap visual would make the message faster to understand, more
memorable, or more visually credible than typography, charts, icons, or
HTML/CSS alone.

### Image Decision Rules

For each slide, choose exactly one primary visual lane:

- `generate`: unresolved bitmap job that will use AI directly or after hosted
  image search is exhausted or unavailable
- `use_existing`: supplied or successfully localized web-search image
- `draw_in_html`: editable chart, diagram, timeline, icon cluster, map-like
  schematic, or shape composition
- `skip`: no image because text/data/editable composition is stronger

Generate an image when any strong trigger is true, unless an accuracy or
editability rule below blocks it:

- the user explicitly asks for an illustration, scene, poster, visual metaphor,
  generated background, or image-rich style
- a cover or divider needs a strong hero visual or atmospheric scene
- an abstract concept needs a visual anchor, such as AI workflow, future city,
  product experience, or system metaphor
- a realistic or semi-realistic object, product mockup, environment, or texture
  would be costly or awkward to build from shapes
- people-heavy or portrait-like content would look poor if built from
  PowerPoint/HTML primitives
- consistent style matters more than showing a real-world source image
- the slide is a single-message emotional, campaign, launch, vision, brand, or
  closing slide where visual memorability matters
- the visual can be placed as a side hero, framed card image, spot
  illustration, or controlled background without making the main text
  unreadable

If only a weak trigger is present, use the scoring rule below. Do not require
two triggers for covers, dividers, campaign/launch/vision slides, abstract
concept slides, or user-requested image-rich decks.

Do not generate an image when:

- the slide is primarily data, table, KPI, roadmap, architecture, or process
  content that should stay editable and information-dense
- the image would be decorative filler and deleting it would not change the
  message
- a real product, real company, real location, real chart, screenshot, or named
  person must be accurate
- a native editable chart/table/diagram is the actual deliverable
- the generated visual would sit behind body text and reduce readability
- the slide can be stronger with typography, charts, icons, or simple vector
  composition

Use this practical scoring rule when unsure:

- `+2`: cover, divider, closing, poster, launch, campaign, or vision slide
- `+2`: user requested image-rich, illustration, poster, scene, visual metaphor,
  or generated background
- `+2`: abstract concept needs a metaphor or atmospheric anchor
- `+1`: realistic object, mockup, environment, texture, or people scene
- `+1`: bitmap can sit in a clear frame/hero area without covering body text
- `-2`: primary value is editable data, table, KPI, roadmap, architecture,
  timeline, process, or matrix
- `-2`: accuracy requires a real product, screenshot, chart, person, company,
  logo, or location
- `-1`: deleting the image would not weaken the message

Choose `generate` at score `1` or higher unless a negative accuracy/editability
rule applies; choose it automatically at score `2` or higher. Choose
`draw_in_html` for editable analytical visuals. Choose `use_existing` for
real/source-backed visuals. Choose `skip` only when the score is below `1` and
the slide is stronger without a bitmap.

### Background vs Local Visual

Prefer local visual assets over full-slide backgrounds. A local visual asset can
be a right-side hero illustration, a card image, a scene crop, a mockup, a
texture tile, or a small conceptual image placed inside the layout.

Use a generated full-slide background only for:

- cover slides
- section dividers
- poster-like single-message slides
- event or campaign pages where the image is the primary message carrier

When using a generated background, reserve a text-safe area before writing the
prompt. The background must leave enough low-detail space for the title and key
copy, and the HTML should add a controlled overlay when needed. Low-detail does
not mean blank: ask for faint environmental continuation, soft texture, pale
linework, atmospheric particles, or abstract shapes in the text-safe area so the
composition feels balanced while preserving readability. Do not use complex
generated backgrounds behind dense body text, data labels, charts, or tables.

The text-safe area must come from a slide layout contract, not from a vague
visual preference. For generated full-slide/background slides, first choose the
slide layout in the fixed `1920x1080` coordinate system, then record title,
body, chart, and page-number boxes in `assets/generated/manifest.json` as
`layout_contract.text_regions`. Use those same coordinates in the final HTML.
The generated-image prompt then derives its composition from the remaining
visual-focus regions. If the text boxes move after drafting HTML, update the
manifest and revise/regenerate the image plan. Small or medium generated hero
images in fixed frames do not require this contract unless they sit behind or
overlap text.

Every text-bearing HTML element covered by the contract must carry
`data-layout-region="<region name>"`, where the name matches the corresponding
`layout_contract.text_regions[].name`. Before HTML self-check, run
`scripts/validate_image_layout_contract.js deck.html assets/generated/manifest.json`
and fix any coordinate drift.

For content slides, prefer:

- no generated image
- a small or medium generated visual in a fixed frame
- an HTML/CSS diagram, chart, icon cluster, or timeline
- a subtle hand-authored texture or gradient rather than a generated background

### Image Plan Manifest

Before generating images, create or update `assets/generated/manifest.json`.
The manifest has a top-level `deck_context`, a top-level `style_anchor` reused
by every `generate` entry, plus an `image_plan` array. Use this shape:

```json
{
  "deck_context": {
    "title": "AI Operating Model Transformation",
    "theme": "How enterprises can move from isolated AI pilots to governed, repeatable AI workflows",
    "audience": "executive and product leadership",
    "narrative": "from fragmented experiments, to unified workflow orchestration, to measurable business impact"
  },
  "style_anchor": {
    "name": "editorial-vector-soft",
    "style": "Editorial vector illustration, clean linework, soft gradient fills, subtle grain texture",
    "palette": "Deep indigo #1E2A5E base, electric cyan #22D3EE accents, warm amber #F59E0B highlights, off-white #F8FAFC background",
    "lighting": "Soft directional light from upper-left, gentle rim light, no harsh shadows",
    "rendering": "High detail, crisp edges, 4K poster quality, magazine-grade finish"
  },
  "image_plan": [
    {
      "slide": "03",
      "decision": "generate",
      "acquire_via": "ai",
      "resolved_via": null,
      "kind": "hero_illustration",
      "reason": "abstract AI workflow concept needs a visual anchor",
      "placement": "right-side hero, ~720x1080 area, title overlays the left third",
      "aspect_ratio": "16:9",
      "target_size": "2848x1600",
      "prompt": {
        "deck_context": "AI Operating Model Transformation deck for executive and product leadership; theme: moving from isolated AI pilots to governed, repeatable AI workflows; narrative arc: fragmented experiments to unified orchestration to measurable business impact",
        "subject": "Three abstract data streams converging into a central neural core, floating geometric nodes orbit the core, translucent flow lines connect them",
        "composition": "Off-center hero on the right two-thirds, left text-safe area stays calm with faint low-contrast linework and atmospheric continuation instead of blank space, eye-level perspective, balanced rule-of-thirds framing",
        "style": "Editorial vector illustration, clean linework, soft gradient fills, subtle grain texture",
        "palette": "Deep indigo #1E2A5E base, electric cyan #22D3EE accents, warm amber #F59E0B highlights, off-white #F8FAFC background",
        "lighting": "Soft directional light from upper-left, gentle rim light on focal node, no harsh shadows",
        "mood": "Optimistic, technical, forward-looking",
        "quality": "High detail, crisp edges, 4K poster quality, magazine-grade finish"
      },
      "avoid": "embedded text, captions, watermarks, signatures, logos, realistic named people, recognizable celebrities, busy background where copy lands, generic stock-illustration look, photo-bashed collage, low-resolution, blurry, jpeg artifacts",
      "output_path": "assets/generated/slide-03-hero.png",
      "alt_text": "Abstract AI workflow illustration with converging data streams"
    },
    {
      "slide": "05",
      "decision": "skip",
      "reason": "data chart should remain the primary visual"
    }
  ]
}
```

The `prompt` field may be either a structured object (preferred) or a single
string. If `generate_image` only accepts a string, flatten the object by
joining fields in this exact order with `. `: `deck_context`, `subject`,
`composition`, `style`, `palette`, `lighting`, `mood`, `quality`. Keep the
structured form in the manifest for auditability even when the call site
flattens it.

### Prompt Template

Write every `generate` prompt with structured fields, not a free-form sentence.
Vague prompts like "modern illustration of AI, blue, professional" are the main
reason image quality looks generic or unrelated to the deck. Use the eight-field
template; keep order consistent so the model weights deck context and subject
before style.

Required fields, in order:

1. **deck_context** — the whole PPT theme, audience, narrative arc, and business/topic domain. Derive it from the user's original brief and keep it in every generated-image prompt.
2. **subject** — what the image is *of*. Concrete nouns, objects, action, environment. It must relate directly to the deck context and slide message. No style words here.
3. **composition** — framing, focal point, layout, where text-safe space lives, perspective.
4. **style** — exactly the `style` clause from the deck `style_anchor`. Do not invent per-slide styles.
5. **palette** — exactly the `palette` clause from the deck `style_anchor`, or the brand palette when supplied. Use hex codes.
6. **lighting** — direction, softness, contrast, time of day for scenes; reuse the anchor unless the slide needs a deliberate variation.
7. **mood** — one or two adjectives tied to the slide message (calm, energetic, optimistic, technical, urgent, premium).
8. **quality** — rendering descriptors that raise fidelity. Reuse the anchor `rendering` clause plus optional per-kind boosters.

Do not call `generate_image` with a prompt that only describes the local slide
object or a generic visual metaphor. The first clause must carry the deck-level
theme so the generated image stays attached to the presentation topic.

Negatives go in the separate `avoid` field, never inside `prompt`.

### Style Anchor

Pick exactly one style anchor for the whole deck and reuse its `style`,
`palette`, `lighting`, and `rendering` clauses across every generated image.
Inconsistent styles look amateur even when individual images are technically
good. Recommended named anchors (pick one, do not mix within a deck):

- **`editorial-vector-soft`** — clean linework, soft gradients, light grain. B2B, finance, strategy, AI/tech, consulting, policy decks.
- **`isometric-tech`** — isometric 3D, vibrant brand colors, subtle ambient occlusion, crisp edges. Product, system, infrastructure, dev-tools decks.
- **`photoreal-product`** — studio-lit photoreal product mockup, shallow depth of field, realistic materials. Launches, marketing, hardware decks.
- **`cinematic-scene`** — cinematic key-art, volumetric light, atmospheric haze, filmic color grade. Vision, brand, campaign, divider covers.
- **`flat-minimal`** — flat geometric shapes, two- or three-tone palette, no gradients, generous whitespace. Analytical, academic, government, scientific decks.
- **`3d-soft-render`** — soft clay-render 3D, pastel palette, rounded forms, gentle ambient light. Consumer, education, healthtech, kid-friendly decks.
- **`abstract-texture`** — abstract gradient mesh, light particles, no figurative content. Dividers, backgrounds, atmosphere-only slides.

If the user supplied brand guidelines, override `palette`, `style`, and
`lighting` with the brand language; keep the named anchor only for kind-level
hints.

### Quality Boosters

Use 3-5 of these in the `quality` field; stacking more makes prompts noisy and
hurts adherence. Skip weak filler like "beautiful", "amazing", "best quality";
modern image models ignore them.

- detail and edges: "high detail", "ultra-detailed", "crisp edges", "sharp focus"
- finish: "4K poster quality", "magazine-grade finish", "editorial quality"
- composition: "balanced composition", "rule-of-thirds framing"
- vector/illustration anchors: "consistent line weight", "even stroke", "limited color count"
- photoreal/3D anchors: "studio lighting", "soft shadows", "global illumination", "subsurface scattering"
- cinematic anchors: "subtle film grain", "muted contrast", "anamorphic flare", "color graded"
- backgrounds: "low-detail text-safe area with faint thematic texture", "smooth gradient where copy lands"

### Required Negatives

Every `generate` entry must include this baseline in `avoid`, plus
slide-specific negatives:

- "embedded text, captions, watermarks, signatures, logos"
- "low-resolution, blurry, jpeg artifacts, pixelation, banding"
- "realistic named people, recognizable celebrities"
- "generic stock-illustration look, clipart, photo-bashed collage"

Add when relevant:

- humans involved: "distorted hands, extra fingers, deformed faces, uncanny anatomy"
- background images: "busy patterns where copy lands, high-contrast detail in the text-safe area, empty blank text-side composition"
- product mockups: "fake brand logos, misspelled labels, plastic-looking materials"
- vector style: "raster artifacts, photographic noise, gradient banding"
- photoreal style: "over-saturated colors, HDR halos, plastic skin"

### Bad vs Good Prompt

Bad (vague, decorative, generic output):

```text
Modern illustration of AI workflow, blue colors, professional style, beautiful, high quality
```

Good (structured, anchored, specific):

```text
Subject: three abstract data streams converging into a glowing neural core, floating geometric nodes orbit the core, translucent flow lines connect them.
Composition: off-center hero on the right two-thirds, left title-safe area remains calm with faint low-contrast circuit-like linework and soft atmospheric continuation instead of blank space, eye-level perspective, rule-of-thirds framing.
Style: editorial vector illustration, clean linework, soft gradient fills, subtle grain texture.
Palette: deep indigo #1E2A5E, electric cyan #22D3EE accents, warm amber #F59E0B highlights, off-white #F8FAFC background.
Lighting: soft directional light from upper-left, gentle rim light on focal node, no harsh shadows.
Mood: optimistic, technical, forward-looking.
Quality: high detail, crisp edges, 4K poster quality, magazine-grade finish, balanced composition.
```

### Per-Kind Prompt Hints

Tune the prompt by image `kind`. Anchor stays the same; subject/composition/quality vary.

- **hero_illustration**: lead with subject + composition; reserve text-safe area on one side, but keep it visually alive with faint thematic texture or atmospheric continuation; pair with `editorial-vector-soft`, `isometric-tech`, or `3d-soft-render`; quality booster: "consistent line weight" or "soft ambient occlusion".
- **full_bleed_background**: emphasize palette + lighting + low-detail center or lower-third band for text; add subtle low-contrast motifs in the safe area instead of pure empty space; pair with `cinematic-scene` or `abstract-texture`; quality booster: "low-detail text-safe area with faint texture", "smooth gradient where copy lands".
- **product_mockup**: pair with `photoreal-product`; specify surface, material, reflection, depth of field, one hero angle, neutral seamless backdrop; quality booster: "studio lighting", "soft shadows", "subsurface scattering".
- **scene**: pair with `cinematic-scene`; specify environment, time of day, atmosphere, single focal subject, foreground/midground/background; quality booster: "volumetric light", "subtle film grain".
- **spot_illustration**: pair with `flat-minimal` or `editorial-vector-soft`; square `2048x2048`; single centered object, neutral background; quality booster: "limited color count", "even stroke".
- **texture_tile**: pair with `abstract-texture`; state seamless or non-seamless, micro-detail scale, no recognizable subject; quality booster: "smooth gradient mesh", "subtle particle highlights".
- **portrait_alternative** (when named people would otherwise be needed): pair with `editorial-vector-soft` or `flat-minimal`; subject must be a silhouette, jersey, nameplate, emblem, or stat card — never a realistic likeness; always include "no facial features, no recognizable likeness" in `avoid`.

Allowed decisions:

- `generate`: call `generate_image` directly for `acquire_via: ai`, or only
  after `acquire_via: web` has a recorded exhausted/unavailable image search.
- `use_existing`: use a supplied image or a web-search image localized with its
  source provenance and explicit license-verification status.
- `draw_in_html`: build the visual as editable HTML/CSS/SVG/chart elements.
- `skip`: no image is needed.
- `blocked`: image generation would be appropriate, but no image-generation
  tool or required input is available.

When calling `generate_image`, use `output_path` under `assets/generated/`, pass
the `size` parameter from the supported preset list below, and include
slide/purpose/kind metadata. Do not request arbitrary dimensions such as
`1600x1024`; choose the closest supported ratio and resolution, then crop or fit
the image in HTML/CSS when placing it on the 1920x1080 slide.

Supported generated-image sizes:

- 1:1 — `2048x2048`, `3072x3072`, `4096x4096`
- 3:4 — `1728x2304`, `2592x3456`, `3520x4704`
- 4:3 — `2304x1728`, `3456x2592`, `4704x3520`
- 16:9 — `2848x1600`, `4096x2304`, `5504x3040`
- 9:16 — `1600x2848`, `2304x4096`, `3040x5504`
- 3:2 — `2496x1664`, `3744x2496`, `4992x3328`
- 2:3 — `1664x2496`, `2496x3744`, `3328x4992`
- 21:9 — `3136x1344`, `4704x2016`, `6240x2656`

For a normal 16:9 slide hero or background, prefer `2848x1600` unless the user
explicitly needs a higher-resolution asset. For square spot illustrations,
prefer `2048x2048`. If the environment does not expose an image-generation
tool, do not pretend an image was generated. Mark the relevant item `blocked` or
use `draw_in_html` / `skip`, then continue with a layout that still works
without generated assets.

Keep generated assets as normal files:

```text
assets/generated/manifest.json
assets/generated/slide-03-hero.png
assets/generated/slide-07-process-bg.png
```

The manifest should record the target slide, purpose, aspect ratio, prompt,
source model/provider when known, file path, and alt text. Reference generated
images from `deck.html` with relative paths, for example:

```html
<img src="assets/generated/slide-03-hero.png" alt="..." />
```

Do not inline large `data:image/...` strings into `deck.html` just to satisfy
the PPTX exporter. The HTML deliverable should stay readable and should open
directly in a browser. The official exporter resolves local relative `<img>`
paths and temporarily converts them to data URLs inside the browser DOM before
calling `dom-to-pptx`, without rewriting the source HTML. For generated bitmap
assets that must survive PPTX export, prefer real `<img>` elements; avoid hiding
local generated files only in CSS `background-image` unless that URL is already
remote/CORS-safe or a small data URL.

## Export

When commands need the skill directory, use the active installed skill path
rather than a machine-specific absolute path. In Office Raccoon this is usually
`$HOME/.box-agent/skills/pptx`, but a host may provide a different path:

```bash
PPTX_SKILL_DIR="${BOX_AGENT_PPTX_SKILL_DIR:-$HOME/.box-agent/skills/pptx}"
OFFICE_RACCOON_NODE_PREFIX="${BOX_AGENT_NODE_PREFIX:-${BOX_AGENT_RUNTIME_PREFIX:-<office-raccoon-prefix>}}"
```

The top-level workflow runs this preflight before writing the full deck. If you
need to re-check whether CLI HTML-to-editable-PPTX export can run:

```bash
${BOX_AGENT_NODE:-node} "$PPTX_SKILL_DIR/scripts/check_html_export_env.js"
```

If this reports missing Playwright/Chromium and no host renderer is available,
follow the route choice in `SKILL.md` before authoring a full deck.

Before export, run the HTML structural self-check with the editable compatibility
profile:

```bash
${BOX_AGENT_NODE:-node} "$PPTX_SKILL_DIR/scripts/html_self_check.js" deck.html --dom-to-pptx --allow-local-images --report qa/html_self_check.json
```

If the command exits non-zero, inspect `qa/html_self_check.json` before deciding
what failed. Summarize concrete failing slides/selectors and follow the bounded
repair rule in `SKILL.md`. The report remains QA evidence but does not control
whether the editable exporter runs.

Then export:

```bash
${BOX_AGENT_NODE:-node} "$PPTX_SKILL_DIR/scripts/html_to_editable_pptx.js" deck.html output.pptx --out slides
```

The export script does not run self-check or write `qa/html_self_check.json`.
It creates `slides/slide-*.png` preview images for visual QA, temporarily
inlines local `<img>` paths in the browser DOM for export, captures a per-slide
background-decoration bitmap (see below), loads
`scripts/dom-to-pptx.bundle.js`, and writes `output.pptx`. It does not rewrite
`deck.html`.

### Background Capture Layer

`html_to_editable_pptx.js` defaults to `--bg-capture always`. For each slide it
takes a 1920x1080 PNG that contains **only** the slide-level background and
pure-decoration nodes; every text container, text node, pill / chip / card
background, `<img>`, `data-pptx-chart` chart node, and `data-pptx-diagram`
diagram root (including all of its descendants) is hidden during capture. The captures are written to
`assets/bg-capture/slide-XX.png` next to `deck.html` and inserted into the
in-memory DOM as `<img class="pptx-bg">` at the back of each slide before
`dom-to-pptx` runs. After capture the exporter removes every node it marked
as pure decoration from the export tree so the bitmap is not painted twice.

A node counts as **pure decoration** (and is therefore baked into the bitmap)
when **all** of these are true:

- it is not an `<img>`, and
- it has no descendant `<img>`, and
- it has no non-whitespace text in any descendant

Plus, regardless of content, these elements are always treated as decoration:
`<svg>` (including any text/tspan inside it), `<hr>`, `<canvas>`, and CSS
`::before` / `::after` pseudo-elements. Exceptions: ECharts/data-chart nodes
marked with `data-pptx-chart`, `data-chart-spec`, or ECharts instance metadata
and technical diagrams marked with `data-pptx-diagram` are treated as
non-decoration so they do not enter the background screenshot. A marked
technical diagram must contain exactly one direct inline `<svg>` root and a
recoverable `data-diagram-spec` or `data-diagram-spec-src`; never route it
through `<img src="*.svg">`, which the image pipeline rasterizes.

Resulting layer order in PPT:

1. background bitmap (one per slide, faithful pixel render of all decoration)
2. existing `<img>` elements, including `assets/generated/*` (kept as native
   PPT pictures and remain individually replaceable)
3. marked technical diagrams (kept as one SVG vector picture per diagram)
4. text containers, pills, chips, cards, badges (kept as native PPT shapes
   with native fills, borders, radii)
5. text (kept as native editable text frames)

Implications for authors:

- Text is never baked. Pills / chips / cards that *wrap* text are also not
  baked — their fills, borders, and rounded corners become native PPT shapes
  and stay editable.
- Decorative SVGs, inline icons, dividers (`<hr>`), and CSS-painted
  decorations *with no text or `<img>` inside them* are baked into the bitmap.
  Do not also expect them as separate editable PPT shapes.
- Slide-level visual changes happen in `deck.html`. The bitmap is regenerated
  on every export, so do not hand-edit `assets/bg-capture/*.png`.
- Use `--bg-capture never` only for purely flat decks where the bitmap layer
  adds no value.
- Keep `assets/bg-capture/` under the deliverable workspace; it is reproducible
  build output and part of the source asset tree.

Do not install the npm `dom-to-pptx` package for this workflow. The editable
export must use this skill's bundled `scripts/dom-to-pptx.bundle.js`, which may
contain local fixes that are not in the published package.

If the current process has no browser host and Playwright/Chromium cannot be
installed after the user chose HTML, keep the finished `deck.html` as the
deliverable and report:

```text
Editable PPTX export: BLOCKED (missing browser host)
Install Playwright: ${BOX_AGENT_NPM:-npm} install --prefix "$OFFICE_RACCOON_NODE_PREFIX" playwright
Download Chromium: "$OFFICE_RACCOON_NODE_PREFIX/node_modules/.bin/playwright" install chromium
```

If the host app exposes an Electron renderer conversion/import path, use that
host route instead of installing Playwright. Do not assume Electron main or a
Node child process has DOM layout APIs.

## QA

HTML self-check is the first QA gate. Keep `qa/html_self_check.json` with the
delivery artifacts. If that report is missing or `"ok"` is not `true`, do not
claim HTML self-check passed.

After the PPTX is created, run package validation, text extraction, placeholder
scan, and render.

## When Not To Use

Use native PptxGenJS instead when:

- the user explicitly confirms direct native PPT generation
- the user chooses `PPTX` after the browser export preflight reports missing
  Playwright/Chromium and no host renderer
- the user provides a `.pptx` template to preserve
- the deck needs native editable PowerPoint charts or tables
- the recipient must use PowerPoint-native chart/table editing
- the task is a narrow edit to an existing deck rather than a new deck
- accessibility or template preservation is more important than HTML-authored
  layout

For a new deck, these exceptions require user confirmation before native
implementation unless the user already provided a template to preserve.
