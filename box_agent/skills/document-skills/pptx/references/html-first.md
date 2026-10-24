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

## Offline local images

Follow `image-assets.md`: use supplied local files or embedded data only. Use
`local_only` media policy; no search, generation, image download or CDN dependency.
Retain structured editable text, charts and diagrams when media is unavailable.
Keep image geometry within the page and use local relative paths in the delivered
bundle. Explicit image requirements that cannot be fulfilled remain disclosed.

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

If the current process has no provisioned browser host, keep the finished
`deck.html` and report the incomplete offline package:

```text
Editable PPTX export: BLOCKED (missing browser host)
Package repair required: bundle Playwright and Chromium before shipping.
Do not install or download dependencies during this presentation task.
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
