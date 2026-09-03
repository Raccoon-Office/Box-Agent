# HTML-First Editable Export (optional PPTX export)

> New decks default to controlled `index.html + deck.json` delivery. Use this
> reference when the user explicitly requests PPTX or when working on the
> legacy/custom-HTML escape route. The controlled renderer already supplies the
> required `.slide` structure and editor runtime.

On the legacy route, author the deck as `deck.html`, then
export the same `.slide` DOM elements to editable PowerPoint objects with the
skill-bundled `scripts/dom-to-pptx.bundle.js`.

This is the optional editable-PPTX export path for controlled or legacy HTML
decks; it is not required for the default controlled HTML delivery.

If final `.pptx` output is expected, run the browser export environment
preflight before writing the full HTML deck. If Playwright/Chromium and host
renderer are missing, tell the user this blocks HTML-to-editable-PPTX export and
ask them to choose `HTML` or `PPTX`: `HTML` means keep the existing controlled
`index.html + deck.json` deliverable (or `deck.html` on the explicit legacy
route) and export later after setup; `PPTX` means switch to native PptxGenJS
with different HTML/CSS fidelity tradeoffs.

Route-change and self-check bypass rules live in `SKILL.md`. This reference adds
editable-export details; do not use it to weaken the top-level workflow.

## Authoring Profile

Before export, use the existing source HTML (`index.html` for controlled decks,
`deck.html` for the legacy route) with one `.slide` element per page. Controlled
HTML is compiler output: fix `deck.json` and re-render rather than hand-editing
it. The following authoring profile applies when writing legacy/custom HTML;
the controlled renderer already enforces its applicable geometry and markup:

- Copy `references/starter/common.css` to `drafts/common.css` and use its locked
  `.slide` block verbatim — every `.slide` is fixed at `width: 1920px;
  height: 1080px;`. Do not re-author these dimensions.
- Put `.slide` directly under `<body>` or a plain non-transformed wrapper.
- Set every `.slide` to `position: relative; overflow: hidden`.
- Prefer inline styles for slide content; keep `<style>` for page chrome only.
- Use fixed `px` units on slide content; avoid `vh`, `vw`, `vmin`, `vmax`.
- Use absolute `left/top/width/height` or flex/grid final layout. Do not use
  `transform: translate/scale/skew/matrix`; `rotate()` is acceptable.
- Use `linear-gradient`; avoid radial/conic gradients.
- Do not use `backdrop-filter`, `clip-path`, `mix-blend-mode`, animations,
  transitions, or text-shadow on slide content.
- Images may use readable local relative paths such as
  `assets/generated/slide-03-hero.png`, `https?://...` with CORS, or
  `data:image/...`. Prefer local relative paths for generated or packaged assets
  so `deck.html` stays readable and opens normally; the official exporter
  temporarily converts local `<img>` assets to data URLs before calling
  `dom-to-pptx`. Generated bitmap assets that must survive PPTX export should
  be real `<img>` elements, not only local CSS `background-image` URLs. Avoid
  `srcset` and `loading="lazy"`.
- Google Fonts links must include `crossorigin="anonymous"` and should have a
  web-safe fallback such as `Arial, sans-serif`.
- Leave text safety slack. Browser text that fits by only 1-2px may wrap in
  PowerPoint because PPT and Chrome use different font metrics. Make text boxes
  at least 16-24px wider than the browser line needs, or reduce font size
  slightly. Do not rely on exact-fit single-line text.
- For badges, pills, buttons, tags, or other short text with a background color,
  never use vertical padding (`padding-top`, `padding-bottom`, or
  `padding: Ypx Xpx`) to simulate vertical centering. This commonly shifts or
  clips text after dom-to-pptx conversion. Use a fixed `width`/`height` outer
  container with the background, radius, and display:flex; align-items:center;
  justify-content:center; and use `<div>`children — `<span>`.
- For Chinese text, prefer fonts that exist or embed reliably across Office
  environments, for example `Microsoft YaHei`, `Noto Sans CJK SC`, then
  `Arial, sans-serif`. If a web font is used, ensure it embeds; fallback fonts
  can change line width and cause unexpected wraps.

Recommended badge pattern:

```html
<div
  style="
    width: 160px;
    height: 48px;
    background: #0066cc;
    border-radius: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
  "
>
  <span
    style="margin: 0; padding: 0; line-height: 1; font-size: 18px; color: #ffffff;"
  >
    进行中
  </span>
</div>
```

## Command

Run:

```bash
PPTX_SKILL_DIR="${BOX_AGENT_PPTX_SKILL_DIR:-$HOME/.box-agent/skills/pptx}"
SOURCE_HTML=index.html # use deck.html only on the explicit legacy/custom route
${BOX_AGENT_NODE:-node} "$PPTX_SKILL_DIR/scripts/check_html_export_env.js"
${BOX_AGENT_NODE:-node} "$PPTX_SKILL_DIR/scripts/html_to_editable_pptx.js" "$SOURCE_HTML" output.pptx --out slides
```

If `check_html_export_env.js` reports missing Playwright/Chromium and no host
renderer is available, ask the user to choose before authoring or exporting:
`HTML` keeps the existing source bundle as the deliverable (`index.html +
deck.json` for controlled decks, `deck.html` for legacy decks); `PPTX` switches
to native PptxGenJS.

The script creates `slides/slide-*.png` preview images for visual QA, loads the
skill-local `scripts/dom-to-pptx.bundle.js`, temporarily inlines local `<img>`
paths in the browser DOM for export, and writes `output.pptx`. It does not run
HTML self-check, write `qa/html_self_check.json`, or rewrite the source HTML.
Run `html_self_check.js` separately when that QA evidence is required. The
exporter passes `autoEmbedFonts: true`
and defaults `svgAsVector: false` so SVGs are rasterized for pixel fidelity,
closer to the in-browser button export path. Pass `--svg-vector true` only when
PowerPoint vector editability is more important than visual fidelity. If
the source contains any `[data-pptx-diagram]`, the exporter overrides that
default and forces `svgAsVector: true` for the whole export. Every marked
diagram must provide recoverable DiagramSpec data and exactly one direct inline
`<svg>` root; `<img src="*.svg">` is not a supported technical-diagram path.
The export summary records `diagramCount` and `diagramVectorExport: true`. If
the separately generated `qa/html_self_check.json` is missing, do not say HTML
self-check passed.

If a separately run self-check fails, read its report before deciding whether
to repair the HTML source. An export failure is independent and should be
diagnosed from the exporter error.

Do not install the npm `dom-to-pptx` package for this workflow. The editable
export must use this skill's bundled `scripts/dom-to-pptx.bundle.js`, which may
contain local fixes that are not in the published package.

If there is no browser host after the user chose HTML, `dom-to-pptx` cannot run
from the CLI. Finish and deliver the existing controlled bundle (or the legacy
`deck.html`), report editable PPTX export as `BLOCKED`, and include the
install/download commands:

```text
OFFICE_RACCOON_NODE_PREFIX="${BOX_AGENT_NODE_PREFIX:-${BOX_AGENT_RUNTIME_PREFIX:-<office-raccoon-prefix>}}"
Install Playwright: ${BOX_AGENT_NPM:-npm} install --prefix "$OFFICE_RACCOON_NODE_PREFIX" playwright
Download Chromium: "$OFFICE_RACCOON_NODE_PREFIX/node_modules/.bin/playwright" install chromium
```

If the host app exposes an Electron renderer conversion/import path, that can
serve as the browser host. Do not assume Electron main or a Node child process
has DOM layout APIs.

## QA Requirements

Run the same required package/text QA as other PPTX outputs. Rendered visual
inspection follows the opt-in triggers in `SKILL.md` §4.2.

Additional editable-export checks:

- Confirm `qa/html_self_check.json` exists, is non-empty, and has `"ok": true`.
- Treat aggregated text-slack warnings as diagnostics, not blockers by
  themselves. Fix them when an exported PPTX actually reflows or when a
  structural overflow issue confirms the risk.
- When visual inspection is triggered, render the exported PPTX; if that
  runtime is unavailable, set `Rendering: BLOCKED`.
- Check especially for text reflow, missing gradients, missing images,
  incorrect SVG conversion, wrong z-order, and shifted card/chart positions.
- If render shows issues, fix `deck.json` and re-render `index.html` for a
  controlled deck; fix `deck.html` directly only on the legacy route. Then
  rerun `html_to_editable_pptx.js`.

Do not claim full fidelity from `dom-to-pptx` without rendered slide images.
If runtime is blocked, report `Rendering: BLOCKED` and keep the limitation explicit.
