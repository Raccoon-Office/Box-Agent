# Creating PPTX Files With PptxGenJS

Use this native editable PptxGenJS path directly for editing existing `.pptx`
files, preserving a supplied template/master/theme, or narrow modifications to
an existing deck. For newly created decks, this is the exception path, not the
default creation path. Prefer HTML-first editable with `dom-to-pptx` by default.
Keep the generation script readable and deterministic.

Before using this path for a new deck, confirm with the user and state why both
HTML-first editable and the bundled `dom-to-pptx` exporter are not suitable. A
valid confirmation point is that `scripts/check_html_export_env.js` found
missing Playwright/Chromium and no host renderer, and the user chose `PPTX`.
Do not proceed silently with native PptxGenJS for a new deck just because it is
simpler to script or because the user mentioned editability.

Do not replace this path with `python-pptx` for new deck creation. If native
editable creation is confirmed, use PptxGenJS. Use Python only for narrow edits,
inspection, extraction, or QA helpers.

Before writing a deck generation script, verify that PptxGenJS is already available:

```bash
${BOX_AGENT_NODE:-node} -e "require.resolve('pptxgenjs')"
```

If this command fails in Office Raccoon, install PptxGenJS into the managed app
support prefix when possible, for example
`${BOX_AGENT_NPM:-npm} install --prefix "${BOX_AGENT_NODE_PREFIX:-${BOX_AGENT_RUNTIME_PREFIX:-<office-raccoon-prefix>}}" pptxgenjs`.
Do not install globally, do not install inside the deliverable folder, and do
not switch to another generator unless the user explicitly approves that
fallback.

## Baseline Script

```javascript
const pptxgen = require("pptxgenjs");

const pptx = new pptxgen();
const ShapeType = pptx.ShapeType || pptx._shapeType;
if (!ShapeType) throw new Error("PptxGenJS shape constants are unavailable");

pptx.layout = "LAYOUT_WIDE";
pptx.author = "OpenAI";
pptx.subject = "Generated presentation";
pptx.title = "Presentation";
pptx.company = "";
pptx.lang = "en-US";
pptx.theme = {
  headFontFace: "Aptos Display",
  bodyFontFace: "Aptos",
  lang: "en-US",
};

const slide = pptx.addSlide();
slide.background = { color: "FFFFFF" };
slide.addText("Title", {
  x: 0.6,
  y: 0.45,
  w: 12.1,
  h: 0.55,
  margin: 0,
  fontFace: "Aptos Display",
  fontSize: 30,
  bold: true,
  color: "1F2937",
});

await pptx.writeFile({ fileName: "output.pptx" });
```

## Coordinate Rules

- PptxGenJS uses inches.
- Common slide sizes:
  - `LAYOUT_WIDE`: 13.333 x 7.5
  - `LAYOUT_16X9`: 10 x 5.625
  - `LAYOUT_4X3`: 10 x 7.5
- Define constants for slide width, height, margins, gutters, colors, and fonts.
- Do not scale fonts from viewport or slide width. Pick readable sizes and check the render.

## Text

- Use `margin: 0` when aligning labels or titles with shapes.
- Use rich text arrays for mixed bold/regular text.
- Use real line breaks through text runs or separate text boxes; do not cram lists into one long paragraph.
- Avoid Unicode bullets when using PowerPoint bullet formatting. Use PptxGenJS bullet options.
- Keep body text roughly 13-18 pt and titles roughly 28-42 pt, then verify by rendering.

```javascript
slide.addText([
  { text: "Insight: ", options: { bold: true } },
  { text: "Conversion improved after onboarding was shortened." },
], {
  x: 0.8,
  y: 1.4,
  w: 5.4,
  h: 0.45,
  fontSize: 15,
  color: "111827",
});
```

## Images

- Prefer local image files or generated PNGs checked into the artifact directory.
- Use `sizing: { type: "cover" }` only when cropping is acceptable.
- Use `altText` for important images where supported.
- Verify images in the rendered output; missing local files can produce a valid-looking but incomplete deck.

```javascript
slide.addImage({
  path: "assets/product.png",
  x: 7.1,
  y: 1.2,
  w: 5.4,
  h: 4.2,
  sizing: { type: "contain", w: 5.4, h: 4.2 },
  altText: "Product screenshot",
});
```

## Charts And Tables

- Use native charts when the recipient may need to edit numbers in PowerPoint.
- Use rendered chart images when pixel-perfect styling matters more than editability.
- Keep table text short; PowerPoint tables do not protect you from cramped layouts.

## Shapes

PptxGenJS versions differ in where shape constants are exposed. Use runtime constants from the presentation instance, and do not invent names.

```javascript
const ShapeType = pptx.ShapeType || pptx._shapeType;
const safeShape = (name) => {
  if (!ShapeType || !ShapeType[name]) {
    throw new Error(`Unsupported PptxGenJS shape: ${name}`);
  }
  return ShapeType[name];
};

const normalizeLineShapeBox = ({ x1 = 0, y1 = 0, x2 = 0, y2 = 0 } = {}) => {
  return {
    x: Math.min(x1, x2),
    y: Math.min(y1, y2),
    w: Math.abs(x2 - x1),
    h: Math.abs(y2 - y1),
  };
};

slide.addShape(safeShape("rect"), {
  x: 0.5,
  y: 0.5,
  w: 2,
  h: 1,
  fill: { color: "FDE047" },
  line: { color: "14532D", width: 1 },
});

// Keep line geometry non-negative even when direction is encoded as negative delta.
slide.addShape(safeShape("line"), {
  ...normalizeLineShapeBox({ x1: 8.6, y1: 1.6, x2: 6.3, y2: 3.1 }),
  line: { color: "111827", width: 1.5 },
});
```

Safe common names usually include `rect`, `ellipse`, `line`, `arc`, `diamond`, `hexagon`, `pentagon`, `chevron`, and `homePlate`, but always check the current runtime if a shape fails. If `addShape` reports `Missing/Invalid shape parameter`, replace the shape with a supported primitive or compose it from several primitives.

Do not use PowerPoint shapes to draw people, faces, portraits, athletes, celebrities, or realistic human figures. They usually render as low-quality clip art and are difficult to validate across PowerPoint, LibreOffice, and preview renderers. Prefer a real licensed image, a generated PNG/JPG illustration, a silhouette, a jersey/nameplate card, a stat card, or an abstract emblem.

## Common Failure Modes

- Hex colors must not include `#`.
- Never pass `""` as a color, fill, line, transparency, or scheme value. PptxGenJS may warn and coerce it to black. Use a valid 6-digit RGB string such as `"0F172A"`, a documented scheme color, or omit the property entirely.
- Do not ignore stderr from the generation script. PptxGenJS warnings about colors, missing assets, invalid options, or unsupported shapes are QA failures until fixed.
- Do not assume optional methods exist across PptxGenJS versions. For example, call `pptx.defineSection(...)` only after checking `typeof pptx.defineSection === "function"`; otherwise omit section metadata.
- Do not reuse mutable option objects across many shapes. Return a fresh object from a helper.
- Never use negative `w`/`h` for line-like shapes. Encode direction with coordinates, then normalize to non-negative geometry before calling `addShape`.
- Do not call `addShape` with unsupported names such as an assumed `shield`; PptxGenJS will throw before writing the deck.
- In Office Raccoon, avoid shell checks that rely on `rm -rf`, `/dev/null`, absolute redirects, `/tmp`, or heredocs. Keep logs, previews, generated images, and QA outputs in the current workspace or requested output folder.
- Avoid excessive shadows, transparency, and rounded cards unless they are part of the actual design language.
- Render every deck when a render runtime is available. Valid OOXML can still be visually broken.
- Do not stop QA after `unzip -t`, file size, slide count, or one extracted title. Run package validation, full text extraction, placeholder scan, and render/preview generation when possible, then a slide-by-slide visual pass.
- Do not say visual QA passed unless actual rendered slide images or preview images exist. If LibreOffice/Poppler are unavailable and Quick Look did not produce per-slide previews, report rendering as blocked.
- Avoid long streaming narration. Report progress in short blocks or checklists, and keep the final response structured.
