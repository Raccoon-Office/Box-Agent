# Controlled HTML Decks

The main agent owns facts, near-final copy, outline, media acquisition and tool
execution. An isolated designer chooses a registered theme preset and each page's
layout, visual options and source bindings. Ordinary authoring follows:

```text
outline.json -> design_plan.js prepare -> design_input.json
-> isolated references/design-role.md -> child response in Session Log
-> design_plan.js accept -> design_plan.json -> inspect_deck_contract.js --design-plan
-> deck.json -> content patch -> finalize_controlled_deck.js -> index.html
```

`prepare` runs outline validation and returns compact file paths/reuse status;
a compact brief/index is read by the isolated role; theme/layout details are
separate small files. The program imports the actual completed child response
from Session Log, supplies version/hashes/page numbers and default content
bindings, and rejects hand-written replacements. Plan input and catalog
hashes prevent stale reuse. A content-only correction that fits existing fields
uses the existing deck directly; it does not request another aesthetic choice.

When the real user request is available, outline layout/visual are planning
hints. Verbatim per-page `hard_requirements` and actual user colors remain hard;
changing only planning hints does not reset the request or correction budget.
Model image inspection remains optional and never gates this workflow.

The plan uses `theme_id` for either a base theme or a registered complete preset
such as `blue-professional@rail-grid`. Presets bind a compatible family and a named
whole-deck variant. New documents persist `design: {version: 2, family, variant}`
without a seed. Every original family/variant remains available to maintenance
and the composition gallery. Legacy version-1 documents preserve their saved
variant during migration; legacy seed resolution is used only if it is absent.

The main agent receives only selected content-field contracts. It cannot change
visual enum fields through an ordinary content patch. Explicit redesign uses
`design_plan.js apply`; users may still change any supported layout/option/page
inside the HTML editor. User edits invalidate AI-plan reuse without being blocked
or overwritten by the proposal.

`finalize_controlled_deck.js` binds available media, checks the core schema,
renders HTML and runs deterministic QA. Core schema/design and rendering failures
block. Outline-binding drift blocks unless the explicit degraded-outline switch
is enabled. Images, post-render HTML/runtime and truth findings preserve usable
HTML as advisories. A pre-content design proposal does not imply a post-content
model review; no second routine reviewer is required for plan-owned decks.

The remaining sections describe compiler/editor capabilities and maintenance,
not additional main-agent design decisions. Follow SKILL.md for normal authoring.

## Output bundle

`<PRESENTATION_DIR>/` below denotes the absolute directory selected in the
conversation. Commands run inside that directory; they must not create an
automatic nested `output/` directory.

```text
<PRESENTATION_DIR>/
├── index.html
├── outline.json
├── design_input.json
├── design_plan.json
├── deck.json
├── assets/
│   ├── generated/
│   │   └── manifest.json
│   └── data/
└── qa/
    ├── outline_check.json
    ├── deck_contract.json
    ├── deck_spec.json
    ├── truth_check.json
    ├── image_manifest.json
    ├── html_self_check.json
    └── runtime_probe.json
```

When the image manifest contains a full-slide/background `layout_contract`, add
`qa/image_layout_contract.json` and require it to pass as well.

Keep media presentation-directory-relative. Do not use remote URLs, absolute paths, or
`..` segments. The standalone editor may embed a newly selected image as a data
URL when the user downloads an updated HTML copy; normal generated decks keep
large images in `assets/`.

## Truth contract

`truth_contract.source_facts` contains only verbatim user/source facts.
`truth_contract.research_facts` contains factual statements captured after an
actual external research step and is forbidden for strict source-only requests.
Both buckets are immutable to content patches. If the user explicitly
authorizes illustrative data, store it separately in
`truth_contract.assumptions`; every affected slide must visibly say `假设` or
`示意`. Assumptions may support disclosed metrics/scenarios, not invented proper
nouns, dates, team facts, awards, or documentary claims. If a necessary fact is
missing and assumptions were not authorized, use a source-appropriate explicit
placeholder (`暂无可验证公开数据`, `待补充`, or `待客户确认`) and continue from the
existing artifacts without pausing.
Source review runs only as a post-generation advisory after `index.html`
exists. Unverified URLs, missing private facts, and unsupported optional claims
may be reported, omitted, neutralized, or represented by a visible placeholder;
they never block the HTML or require a repair pass.

## Layout selection contract

For a plan-owned deck the isolated designer chooses registered layouts from the
prepared catalog. `design_plan.js validate` performs a dry scaffold check;
`inspect_deck_contract.js --design-plan` writes the ordered skeleton and selected
content contracts. A semantic mismatch returns a field error to the designer;
no layout promotion, automatic split or aesthetic fallback changes its choices.

Legacy CLI callers may still use positional layout IDs and `--theme`/`--family`;
that compatibility path retains its old semantic normalization. It is not the
normal main-agent workflow. Both routes share field/capacity checks, media
binding, rendering and the HTML editor. After validation, preserve the returned
schema and fill actual content rather than copying illustrative defaults.

High-frequency professional visuals have dedicated editable contracts. Use
`factory-process-line-v1` for production stations and quality metrics,
`legal-case-logic-v1` for issue/rule/analysis/conclusion reasoning,
`property-factsheet-v1` for site zones and asset facts,
`commerce-funnel-v1` for retail conversion stages, and `supply-network-v1`
for logistics nodes, statuses, and fulfillment metrics. These layouts express
domain relationships; do not replace them with generic cards merely to add
variety.

Scaffold the complete deck once. A failed blocking structural/media validation
is a patch operation: change only the paths named by the report. Source-advisory
paths are not repair instructions; preserve the generated HTML and report them
afterward. Do not regenerate the other slides, and do not grep
`layouts/registry.js` for a second interpretation of the contract. The
structural validator lists registered themes and allowed fields in each
relevant error.

A wording/data correction uses `apply_deck_patch.js`. A plan-owned deck rejects
visual enum changes in that command and rejects direct `apply_deck_redesign.js`
invocation. An explicit design change uses a validated revised plan with
`design_plan.js apply`, sharing the existing layout migration and outline checks.
Previous props remain in `layout_drafts`; user HTML actions stay unrestricted.

The manifest is generated from `layouts/registry.js`. Never hand-edit
`layouts/manifest.json`; update the registry and rebuild the manifest.
Each registered layout also declares editor metadata and a complete, valid
`editor.defaultProps` payload. The same pure-JavaScript registry is consumed by
the Node compiler and embedded into generated HTML, so adding a page in the
browser cannot drift to a second set of templates.

`statement-focus-v1` supports short metrics and sentence-like proof points.
Leave `proof_style` as `auto` unless the content strategy requires an explicit
mode: compact numeric or uppercase values use `metrics`; sentence-like CJK or
prose values use the wrapping `points` treatment.

The registry includes two distinct cover choices: use `cover-hero-v1` when a
concrete subject deserves a fixed-frame image, and `cover-editorial-v1` when
typography, up to six editable tags, or a generated background should carry the
opening. An unresolved optional hero is shown only as a neutral editor affordance;
it is not rendered as a fake chart or decorative presentation graphic. Use
`closing-next-steps-v1` for a real close with actions/contact instead of forcing
closing content into a generic statement page.

Use `text-columns-v1` for two or three sustained text sections. It is not a card
grid: sections are separated by whitespace and local rules so the page reads as
continuous analysis. Use `cards-grid-v1` for genuinely parallel, scannable
items. In the `numbered` cards variant, the layout supplies the ordinal; leave a
numeric `kicker` empty. The renderer also suppresses a duplicate numeric kicker
from older/generated specs.

Use `chart-bar-v1` for a simple categorical comparison, ranking, or
distribution with three to seven non-negative values. Use `chart-data-v1` for
bar, column, line, area, pie, donut, or radar charts with two to twelve
categories and up to four series. Both layouts keep a normalized
`data-chart-spec`; the HTML renders it with the locally bundled ECharts 6 SVG
runtime, and the `调整` panel edits the underlying labels and values rather than
the generated SVG. Presentation mode replays chart animation when the chart
slide becomes current. The editable PPTX exporter maps the same controlled
spec to a native PptxGenJS/PowerPoint chart instead of copying the ECharts SVG.

Both controlled chart layouts expose two independent design controls.
`chart_style` is semantic rather than decorative: `cool-ordinal` uses a
single-hue lightness sequence, `botanical-categorical` separates a small set of
independent categories, and `ink-focus` combines neutral ink with one dominant
signal. `reading_mode` controls visual grammar: `glance` favors immediate
comparison with wider marks and stronger labels, while `editorial` favors
hairline grids, open markers, endpoint labels, and slower reading. `auto`
resolves both controls from the chart type, series count, category count, and
traction presentation. The renderer writes the resolved profile and light/dark
palettes onto each `data-pptx-chart` root so HTML ECharts and native PowerPoint
export consume the same colors.

Use `image-feature-v1` when one wide 16:9 image should dominate the page while
the title, explanation, and caption remain editable below it. Use
`image-full-bleed-v1` for an explicit full-slide visual, cinematic poster,
campaign, divider, or future-state scene. The full-bleed layout requires a
generated or source-backed background and publishes a fixed 1920×1080
`layout_contract`: the left copy region stays calm while the primary visual
focus remains on the right. `--no-images` deterministically falls back to
`statement-focus-v1`.
For bar/column data that visibly mixes units (for example minutes, percentages,
and scores), the renderer automatically uses independently scaled small
multiples. Each panel remains an animated ECharts view backed by the same
editable data grid and exports as its own native editable PowerPoint chart.

Use `technical-diagram-v1` for architecture, system-integration, and data-
pipeline pages. Select `diagram_kind` as `architecture`, `integration`, or
`pipeline`; author stable node ids plus explicit edges in its DiagramSpec, then
let the bundled ELK runtime compute the SVG layout. The HTML editor changes the
recoverable nodes/edges and can add, delete, or relayout them. The PPTX route
exports each marked diagram as one SVG vector picture, not as node-level native
PowerPoint shapes.

Use `table-data-v1` when exact labels and values matter more than trend. Its
`gantt` variant supports one task column plus up to five phase columns and up
to twelve work packages; represent inactive schedule cells with `—`, not an
empty string.
Use `heatmap-matrix-v1` for a semantic risk or intensity matrix. It supports
three to six columns and two to eight editable rows; cell values such as low,
medium, high, critical, or numeric ranges map to five presentation-safe color
levels while the source text remains editable in HTML and PPTX.
Use `quadrant-matrix-v1` for a true editable 2×2 priority matrix. Its four
items are placed against explicit horizontal and vertical axes; item order is
high-high, high-low, low-high, low-low. Do not substitute `table-data-v1`,
`heatmap-matrix-v1`, or a generic four-card grid when the outline explicitly
asks for quadrants, impact-versus-urgency, or a 2×2 matrix.
Use `swimlane-process-v1` when roles must be crossed with delivery phases and
each handoff remains editable. Use `customer-journey-map-v1` when each stage
needs behavior, touchpoint, emotion, pain, and opportunity fields rather than a
simple ordered timeline. Use `maturity-model-v1` for level criteria plus current
and target states. Use `cause-tree-v1` for one problem branching into cause
categories and contributing factors; do not approximate these relationships
with generic cards or `technical-diagram-v1`.
Scatter, bubble, combo, sankey, map, and tables beyond these
capacities still use the data-backed legacy HTML route until a controlled
native-PPTX mapping is registered. Never flatten recoverable data into a
bitmap.

## Built-in theme contract

The controlled compiler owns a versioned theme catalog under `themes/`.
`layouts/manifest.json` and `scripts/inspect_deck_contract.js` expose
`default_theme_id` plus every theme's selection signals, palette, typography,
shape tokens, compatible composition families, and finite visual-style axes.
Normal authoring calls `design_plan.js prepare`; only the isolated design role
reads the resulting catalog. It chooses a base theme or complete named preset,
with a complete exact-hex palette (background, text, primary, accent, secondary,
accent_usage; optional heading). User-given role colors are locked; missing roles
are supplied by the designer. Both paths produce `design_contract.palette.version=2`
with per-role provenance and frozen derived tokens. The program rejects invalid selections and
returns field errors without fallback. User-selected themes and explicit colors
win. Plan-owned decks do not require a second post-content semantic reviewer;
program QA still runs, and never claims an unperformed model review.
Legacy `--design-catalog` / `--theme-model-choice` calls remain available for
compatibility and maintenance, but are not main-agent design responsibilities.
Existing HTML and legacy decks remain readable. Previously accepted design inputs
must run prepare again to adopt the new palette contract; this does not overwrite
saved user-edited HTML.
The catalog includes at least one
executable theme for every bundled Visual DNA id, plus explicitly curated
variants such as `block-frame-mono-blue`. It ships with the `pptx` skill and is
sufficient for generation on machines that do not have the separate
`html-templates` skill.

Layout semantics are validated independently of item count. A quadrant needs
real x/y relationship evidence; four parallel labels fall back to cards. A
timeline needs at least two ordered time or phase signals; parallel policies
fall back to cards. Composition compatibility also participates in
normalization: `editorial-spread` does not keep a split statement when the
title/message is long or the page carries three parallel proof points. For plan-owned decks an incompatible choice is returned to the designer before
scaffolding; the legacy CLI retains normalization for compatibility.

### Scenario theme and layout pairings

Use these as starting points when the brief matches. They are registered themes
available in `--design-catalog` and the default theme gallery. Preserve explicit
user theme/palette choices; do not force a scenario solely from one keyword or
copy the gallery's illustrative figures into a real deck.

| Scenario / theme id | Visual language | Suitable existing layouts |
| --- | --- | --- |
| Sustainability, ESG, resource efficiency / `impact-field` | Forest ink, evidence bands, baseline/target comparisons; default `analytical-exhibit` | `kpi-grid-v1` for supplied metrics, `chart-data-v1` for comparable quantities, `timeline-horizontal-v1` for dated commitments, `technical-diagram-v1` for resource flows |
| Match review, athlete profile, training / `stadium-score` | Night-blue score panels, ice-blue timing bars, condensed headings and tabular scores; default `poster-asymmetric` | `image-full-bleed-v1` for licensed action photos, `kpi-grid-v1` for results, `comparison-two-column-v1` for tactics, `cards-grid-v1` for training actions |
| Destination introduction, cultural tourism, itinerary / `destination-atlas` | Sea-blue labels, postcard frames and dashed route annotations; default `editorial-spread` | `image-feature-v1` for sourced scenery, `timeline-horizontal-v1` for ordered stops, `comparison-two-column-v1` for route options, `table-data-v1` for supplied logistics |
| Restaurant concept, food brand, seasonal menu / `tasting-menu` | Berry ink, cream stock, menu rules and serif headings; default `literary-minimal` | `image-hero-split-v1` for dishes, `text-columns-v1` for ingredient stories, `comparison-two-column-v1` for menu concepts, `table-data-v1` for supplied prices |

Keep facts separate from visual styling: ESG targets are not verified outcomes;
sports scores need sources; travel times/opening hours require current evidence;
food origin, nutrition, allergen, and certification claims need supplied or
verified information. Missing quantities do not authorize invented KPI cards.
The preview pages explicitly label sample data and conceptual content.

### Shared composition and palette behavior

For a deck-wide request such as “卡片采用三列网格” or “3列高密度”, preserve
`design_contract.style_overrides.card_columns: "3"` (or `"2"`). The scaffold
infers this from explicit card/grid wording, or accepts
`--style-override card_columns=3`. It outranks ledger/featured card variants and
survives editor rerenders. Table-column counts and page-specific wording are
not interpreted as a deck-wide card setting. HTML self-check verifies the
actual rendered column count when this contract exists.

Keep accent fills/decoration separate from accent text. The renderer resolves
a readable `accent_text` color for ordinary surfaces; solid primary-colored
cards use their own readable foreground, including auxiliary text. Contrast
probing composites translucent colors over ancestor backgrounds. A transparent
image/gradient without an opaque backing is reported as unmeasured and needs
visual inspection, rather than being treated as black or marked fully checked.

`sketch-whiteboard` is the hand-drawn / Excalidraw-like whiteboard theme for
brainstorming, workshops, teaching and product concepts. Choose it for explicit
手绘线条、白板草图、双笔触、圈注 or hand-drawn/sketch requests. It uses deterministic
SVG double strokes for uneven frames, hand-drawn comparison arrows, circled
labels and marker underlines. Body text stays editable and readable; chart data
and technical diagram node/edge geometry remain precise inside a sketch frame.
The theme can also add loose squares, circles, stars, curved trails and short
hatching in clear areas. It measures actual text and media bounds before placing
these optional geometric marks; crowded slides omit them.
Pair it with `cards-grid-v1`, `comparison-two-column-v1`,
`timeline-horizontal-v1`, and `technical-diagram-v1` as the content requires.
The decoration layer follows editor changes and resizing. Existing background
capture flattens only these decorative strokes for PPTX export, preserving the
text as text. Keep the default background-capture export path for this theme;
do not promise individually editable PowerPoint pen strokes. `decorations=off`
restores plain native borders. Handwriting fonts use local fallbacks; no font
download is required for the linework.

Composition families publish their actual content-left gutter through
`--deck-content-left`. Theme guide lines and similar edge decorations derive
their position from that shared gutter instead of using an independent
percentage, so a theme cannot draw through layout-owned content. Repeated
structures also adapt to semantic cardinality where the geometry would
otherwise create a conspicuous empty column (for example three- and four-step
timelines). These are deterministic renderer rules, not extra model review
rounds.

The existing 1440x900 runtime probe checks both the slide background/base-text
pair and sampled text on local card, timeline, label, and chart surfaces. The
renderer computes a readable foreground token for each palette-backed surface.
`primary_text` means emphasis ink on the page, card, and tinted surfaces;
`inverse` means ink on a solid `primary` fill. They are resolved independently
for both built-in and overridden palettes. Readable identity-colored emphasis
is retained; muted copy and alternate-page emphasis account for their actual
surface palette. Chart/note surfaces keep their own foreground roles.
The probe evaluates actual statement text rather than an unused container
color, and includes total failure count/affected pages alongside bounded details.
Local contrast findings remain advisory and keep the rendered HTML deliverable;
they do not add a hard threshold, retry loop, or separate finalization stage.

`html-templates` is an optional, richer Visual DNA matcher. When present, its
`template_id` selects the corresponding executable base theme (for example
`signal` or `block-frame`); an explicit user palette is applied as a semantic
token overlay instead of forcing an unrelated style preset. When absent, select directly from the
built-in `selection` metadata. Never copy the whole Visual DNA library into a
deck and never use an unregistered Visual DNA id as `theme_id`.

`comic-panel` is the executable comic/storyboard theme. It derives its stable
panel geometry from the bundled block-frame reference but owns a distinct
Visual DNA id and selection contract. Use it for 漫画、分镜、对话气泡、拟声词、
halftone, manga, comic-book, or graphic-novel briefs. DiagramSpec pages retain
their professional node and edge rendering inside the comic outer frame.

`8-bit-orbit` is the executable pixel-arcade theme. Use it for 像素风、8-bit、
16-bit、街机、CRT、retro-game, or pixel-art briefs. It owns a dedicated CRT
grid, neon stepped frames, status labels, pixel shadows, and retro-interface
composition instead of inheriting only generic theme-axis styling. DiagramSpec
pages retain clean professional SVG nodes and edges inside the pixel monitor
frame.

The technical/product/data catalog includes three purpose-built themes:

- `technical-blueprint` defaults to `technical-schematic` for architecture,
  infrastructure, integration, runtime, and data-pipeline briefs. Its CSS adds
  coordinate grids, specification rails, and an outer blueprint stage without
  styling DiagramSpec descendants.
- `product-console` defaults to `product-showcase` for SaaS, software-product,
  product-launch, feature-demo, and UI briefs. Its CSS adds browser chrome,
  app-shell panels, status chips, and product screenshot stages.
- `data-intelligence` defaults to `analytical-exhibit` for KPI, operating
  analysis, BI, finance, analytics, and decision-dashboard briefs. Its CSS adds
  high-density KPI, evidence, table, chart, and data-flow treatments.

`signal` and `soft-editorial` also own dedicated CSS beyond token substitution.
`signal` uses an institutional editorial ledger with navy/bone/gold rules;
`soft-editorial` uses warm paper, magazine rules, asymmetric rhythm, and softly
colored editorial blocks.

When the user explicitly asks to browse or choose themes before authoring, run
`scripts/render_theme_gallery.js --out theme-previews/index.html`. The default
gallery renders a representative cross-family shortlist with the real compiler;
`--all` renders the full catalog. This discovery artifact is created before the
outline/scaffold artifacts and does not alter the normal auto-matched path.

Theme previews are not a reliable way to compare page grammar because palette,
type, and surface styling can dominate the result. When the user asks to compare
composition types or says several types look alike, run
`scripts/render_composition_gallery.js --out composition-previews/index.html`.
It renders matched content across all eleven families and their thirty-three
registered variants, grouped into five user-facing directions. Treat this atlas
as disposable discovery output, not as canonical deck state.

## HTML composition layer

The top-level `design.family` selects one of eleven registered HTML composition
templates: ledger, spread, stage, collage, frame, window, article, device,
cinema, exhibit, or schematic. They emit
different semantic tags, nesting, and composition anchors around the same
layout-owned editable fields. `layout_id` still defines meaning and capacity;
the HTML composition template defines the larger page grammar; the theme and
saved variant define tokens and finite geometry choices. The editor must pass
the persisted `design` object whenever it re-renders a page so the structure is
stable across edits and saves.

Variants may own small, variant-specific HTML anchors when the page grammar
requires them, but they must never duplicate layout-owned editable fields. Keep
interface metaphors semantic rather than decorative: browser chrome belongs to
`browser-story` media pages, system buses belong to `annotated-system`, and
evidence scales belong to `evidence-rail`. Restrained information families
(`institutional-grid`, `literary-minimal`, `product-showcase`,
`analytical-exhibit`, and `technical-schematic`) suppress repeated pill/tape
label chrome even when the selected Visual DNA offers it; expressive families
may retain those shapes when they are part of the intended visual language.

The five directions are `structured-systems`, `narrative-pages`,
`visual-impact`, `interface-modules`, and `expressive-objects`. They are defined
once in `composition_core.js`, filtered by the selected theme's allowlist, and
published through the layout manifest and scaffold contract. They are not saved
to `deck.json`; `direction` is always derived from `design.family`.

### Theme and composition compatibility

Current runtime behavior uses a stable default plus a tested allowlist. Legacy
themes receive their default from `THEME_COMPOSITION_FAMILY`; a theme file may
declare `composition.default_family` and `composition.allowed_families`
directly. Multiple themes may share a family. AI/user selection is accepted
only inside the allowlist, and a compatible persisted `design.family` is kept.
`design.variant` directly names a registered variant inside the selected family:

```text
theme -> compatible directions/families -> theme preset -> saved family/variant
```

For example:

```json
{
  "default_family": "editorial-spread",
  "allowed_families": [
    "editorial-spread",
    "literary-minimal",
    "poster-asymmetric"
  ]
}
```

The default preserves existing output. An unknown family is rejected; an
incompatible family is rejected during scaffold selection, while a legacy
persisted mismatch is normalized back to the theme default. Persisted compatible
decks retain their selected family and variant across editing, reopening, and
export.

### Extension matrix

- A new theme is one JSON definition plus optional assets. It declares
  `composition.default_family` and `composition.allowed_families` in that same
  file and is smoke-tested with representative layouts.
- A new layout is implemented once in the layout registry, then validated
  across every composition family. Do not fork one renderer per family.
- A new composition family is implemented once as an HTML wrapper, anchors,
  CSS, variants, and one direction assignment, then validated across every
  registered layout. Do not reimplement layouts inside the family.

The intended cost is additive implementation plus cross-product testing. With
18 layouts and 11 families, keep 29 primary implementations and the automated
compatibility checks rather than 198 separate renderers.

## Media decision contract

`inspect_layout.js` exposes three related media contracts:

- `mediaSlots.decision`: the narrative rule for choosing media automatically.
- `mediaSlots.slots`: fixed-frame props such as the optional cover `hero` or the
  required image-led `image`, including placement, ratio, and allowed strategies.
- `mediaSlots.background`: the optional slide-level full-bleed background policy,
  its recommended usage, treatments, and registered `data-layout-region` names.

Resolve each decision before writing final `deck.json`. `generate` means call the
image tool, write the result under `assets/generated/`, and store the local path
with `origin: "generated"`. `use_existing` means localize a source-backed or fixed
asset and use `origin: "asset"`. `skip` means omit an optional media prop and let
the layout use its typography/geometry fallback. Never leave `generate` as an
unresolved value inside the deck spec.

Fixed-frame media stays inside layout `props`. A materialized full-slide image is
stored on the slide itself:

```json
"background": {
  "src": "assets/generated/cover-background.png",
  "alt": "Abstract workflow atmosphere",
  "origin": "generated",
  "fit": "cover",
  "position": "center",
  "treatment": "wash-light"
}
```

Generated backgrounds still require an image `layout_contract`. Use only the
visible names declared by `mediaSlots.background.textRegionNames`; the renderer
emits matching `data-layout-region` nodes. Dense cards, comparisons, KPI pages,
and tables should normally skip backgrounds or use only a faint low-detail
texture. Prefer one dominant media treatment: a hero or a background, not both,
unless the background is intentionally subordinate.

## Editor boundary

The embedded editor supports plain-text edits, image replacement, adding a page
from a registered layout, changing the current page's registered layout, page
reorder, duplicate, delete, and saving an updated HTML document. It updates the
embedded deck state through `data-prop-path`; arbitrary DOM/CSS editing is not
supported. New pages are inserted after the current page. Before a layout
change, the editor stores the current props in `slide.layout_drafts`; switching
back restores those props, and serialization retains the drafts across save and
reopen. Layout changes map shared titles, summaries, media, cards, proof points,
metrics, and steps into the target contract without bypassing validation.

The current-page `调整` panel is also registry-driven. Each layout may expose
named enum controls such as media side, alignment, emphasis, or visual variant,
plus declared collections such as proofs, cards, comparison points, KPIs, and
timeline steps. Collection add/delete/reorder actions use the field contract's
`minItems` and `maxItems`; the editor never creates an item outside the layout's
validated shape. These controls re-render the current slide immediately while
keeping the semantic props editable and serializable.

Chart layouts additionally expose a data grid in `调整`. `chart-data-v1` can
add/remove categories and series within its declared capacity; changing a cell,
series name, chart type, legend, labels, stacking, or animation updates
`deck.json` props and the rendered ECharts chart together. Saved HTML removes
transient ECharts-generated SVG nodes and recreates them from the embedded spec
when reopened.

The toolbar keeps the selected page number and total visible; the title remains
visible when space allows. Frequent navigation, edit, playback, export, and save
actions stay on the bar. Lower-frequency layout controls live under the hover /
focus `设计` menu, while add, reorder, duplicate, and delete actions live under
`页面`. Page moves retain selection and report the before/after position. The
compact layout picker uses real rendered previews and a neutral current-state
marker rather than a theme-colored slide border. Browser automation and PPTX
export automatically hide editor chrome.

`播放` enters a one-slide-per-viewport presentation mode from the current page.
It scales the fixed 1920x1080 canvas without changing export geometry, hides all
editing chrome, and supports arrow keys, space/PageDown, Home/End, click-to-
advance, on-screen previous/next controls, and Escape/`退出播放`. Fullscreen is
requested when the host allows it; viewport presentation remains functional when
an embedded office host denies fullscreen.

The editor dispatches `box-agent:deck-change` and exposes
`window.__deckRuntime`. In the trusted officev3 workspace preview it uses the
versioned `box-agent-controlled-deck` / `officev3-controlled-deck-host`
`postMessage` bridge to save in place. The host recognizes the generator marker,
constrains writes to the active `.html`/`.htm` file, compares an optimistic
SHA-256 hash, enforces a size limit, and writes atomically. Outside that host the
same control downloads a copy and does not claim that the original was saved.

## Legacy escape route

Use free-form HTML only when the registered library cannot express a required
page. Keep that page or deck on the existing fragment/self-check pipeline and
report that it is not structurally editable through controlled props. Do not
silently mix arbitrary DOM into a controlled layout renderer.
# Expressive page variants

All registered themes support these optional per-page compositions through the
existing layout fields; no new deck schema or custom HTML is required.

| Page task | Layout and prop | Use and limits |
| --- | --- | --- |
| Memorable opening | `cover-editorial-v1`, `composition: "poster"` | Large editable title, theme primary background and computed readable foreground. Short titles have the strongest effect; long titles use smaller type. `standard` retains the theme shell. |
| Image-led story | `image-feature-v1`, `composition: "editorial"` | Dominant image plus narrow narrative/caption. Resolve the existing required image slot. `standard` retains the wide-image layout. |
| Case with evidence | `project-case-study-v1`, `composition: "editorial"` | Dominant image, side notes and an unboxed proof strip. Retains all 2–3 supplied metrics and `media_side`; `split`/`poster` remain available. |
| One leading metric | `kpi-grid-v1`, `variant: "spotlight"` | First supplied item is visually dominant; all 3–6 items remain. Choose only when the narrative identifies a leading metric, not for equal-weight comparisons. |
| Thesis with evidence | `statement-focus-v1`, `composition: "open"` | Large thesis, supporting narrative and optional unboxed proof strip. |
| Parallel points | `cards-grid-v1`, `composition: "open"` | Three points use a title sidebar and open rows; denser content uses open columns. Explicit 2/3-column requests take priority. Optional body copy can be empty. |
| Comparison | `comparison-two-column-v1`, `composition: "open"` | Prominent opposing headings and lists; no implied process arrow. Preserves symmetric/contrast/stacked choices. |
| Ordered process | `timeline-horizontal-v1`, `composition: "open"` | Connected nodes retain left-to-right order; short copy alternates around the rail, dense copy stays below. |
| Explanation | `text-columns-v1`, `composition: "open"` | Editorial rows separate section headings, prose and optional bullets without enclosing cards. |
| Conclusion and actions | `closing-next-steps-v1`, `composition: "open"` | Large conclusion and open action strip. Details are optional; no compulsory filler or END watermark. |
| Other registered pages | `composition: "open"` where offered | Shared lighter shell; tables, charts, matrices and technical diagrams preserve their meaningful internal geometry. |

New outline-backed scaffolds use these open/expressive variants unless
the outline explicitly asks for standard geometry. Spotlight is selected only
for an explicit primary-metric intent. Legacy decks without these props keep
their old defaults. The editor exposes the same enum controls, and content
patches preserve them. All 33 layouts offer a canvas composition, across all
themes; this does not mean every specialized diagram is redesigned. Changing
layouts or adding a page in an open deck keeps a compatible canvas composition.

Large display text uses ordinary tracking and roomy text boxes to reduce PPTX
wrap drift. Text and metrics remain native editable elements; images remain
image objects. When an editable PPTX is requested and a local renderer is
available, inspect its actual replay as well as the HTML. A successful HTML
screenshot alone does not verify PowerPoint font substitution or line wrapping.

Controlled HTML fits marked headings and KPI values to their actual rendered
font and available width. Numeric values retain their units on one line; short
titles can reduce slightly to stay intact, while longer headings use balanced
lines. The runtime refits after font loading and editor changes without changing
the source text. Model-based visual review remains optional: unsupported models
or unreadable-image responses mean “unverified”, not a blocked delivery.
