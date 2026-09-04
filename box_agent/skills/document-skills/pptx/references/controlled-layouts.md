# Controlled HTML Decks

The controlled route separates semantic content from layout geometry:

```text
outline.json -> deck.json -> deterministic finalize -> index.html -> optional PPTX
```

During generation, `deck.json` is the editable source of truth. `index.html` is
a deterministic, offline-capable view and editor. After a user saves inside the
HTML editor, the embedded `#deck-document` becomes the source of truth for that
edited HTML artifact; the editor does not silently rewrite a sibling
`deck.json`. A layout renderer owns DOM, CSS, geometry,
content capacity, and PPTX-safe behavior; the model only chooses a registered
`layout_id` and fills its declared `props`.

The visual system has four independently resolved layers:

- `layout_id` is the semantic contract: which fields exist and remain editable.
- `design.family + design.variant` changes how those fields are composed.
- `theme_id` supplies a preset for typography, shape, surface grammar, and a
  fallback palette.
- `design_contract.palette` is an optional semantic color-token overlay for
  explicit user colors; it overrides theme color tokens without changing the
  theme's typography or page grammar.

Five composition directions sit above those layers as a discovery and routing
view, not as a fourth persisted visual layer. A direction groups compatible
families in language a user can choose; the concrete family remains the runtime
decision stored in `design`.

`deck.json` is the document model that binds those layers to content. It stores
the theme selection, persisted design seed/family/variant, and each slide's
`layout_id + props + outline_intent`. It also stores `design_contract` when the
source contains explicit palette, geometry, direction, relationship, or item
count requirements; it is not another visual layer. The
persisted intent copies the bound outline page's title, message, layout, and
visual direction so semantic fit remains testable after patching or editor
saves. `render_deck_html.js`
validates that model, renders layout-owned semantic DOM, wraps it with the
selected composition HTML, applies theme variables, and embeds the normalized
document plus the layout/editor runtimes into the final `index.html`.

Before scaffolding, expose `composition.directions` if the user wants a
composition choice. Resolve the chosen or inferred direction through its nested
family ids and the matching `composition.families[].selection_signals`, then
choose only from the selected theme's
`composition.allowed_families`; otherwise keep `composition.default_family`.
Pass the concrete choice with `--family`. The scaffold creates `design.seed`
once and derives a variant inside that family. A new scaffold normally receives
a new seed, so another production of the same brief may use a different variant
and, when the content decision differs, a different compatible family. Never
mutate the family or seed through a content patch while editing, rerendering,
reopening, or exporting an existing deck; the same artifact must stay visually
stable. An explicit user-requested recomposition uses the separate controlled
redesign command and records the new design/layout plan.
`--design-seed` exists only for tests or an intentionally reproducible fresh
scaffold. Legacy decks without `design` receive a deterministic
title/theme-derived default composition at render time.

Normal generation finalizes with one command:

```bash
${BOX_AGENT_NODE:-node} scripts/finalize_controlled_deck.js deck.json --out index.html
```

The patch compiler first reconciles every existing manifest asset to its exact
slide id and `prop_path`. The helper validates the core deck schema as a hard prerequisite,
records image-manifest findings as a delivery advisory, renders `index.html`,
runs HTML self-check and the 1440x900 runtime probe, then records source/truth
findings as another non-blocking advisory. It stops at the first actionable
structural, HTML, or runtime failure. Image, source, URL, and private-fact
findings never prevent a structurally valid HTML artifact from being written;
unmet required images make that artifact degraded rather than complete. Exact
outline title/message binding drift is also preserved as degraded semantic QA
when the core deck schema still passes.
Do not split a successful finalization into separate model-directed commands;
after a focused blocking repair, rerun the finalizer so every downstream report
is refreshed together.

## Output bundle

`output/<deck>/` below denotes the canonical `BOX_AGENT_OUTPUT_DIR` selected by
the host. Commands run inside that root; they must not create another nested
`output/` directory.

```text
output/<deck>/
├── index.html
├── outline.json
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

Keep media artifact-root-relative. Do not use remote URLs, absolute paths, or
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

1. Query by role, density, and media count.
2. Choose the ordered layout id for every slide, including repeats, then
   scaffold once with `scripts/inspect_deck_contract.js <LAYOUT_ID...>
   --theme auto [--family <ALLOWED_FAMILY_ID>] --outline outline.json
   --out deck.json`. The stdout
   deduplicates layout descriptions and returns the bound outline pages; the
   written deck preserves full slide order, `source_outline_page`, and the
   page's immutable `outline_intent`. Strong semantic mismatches are normalized
   before authoring (generic matrix→table, 2×2/quadrant→quadrant matrix, tagged text cover→editorial
   cover), and the report records each normalization.
3. Keep the scaffolded top-level `design` object unchanged during normal content
   patches. It controls only composition and does not change any layout's field
   contract.
4. Fill only `layouts[].fields`; start from `deck_skeleton` or
   `layouts[].editor.defaultProps`. There is no `.props` or `.required_fields`
   contract path.
5. Validate blocking copy budgets, array capacities, and media object shapes.
6. Reconcile existing manifest assets deterministically, record unresolved media
   paths as a delivery advisory, render after structural validation passes, and
   run source/truth review afterward as another advisory.

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

A wording/data correction uses `apply_deck_patch.js`. A request such as “做一版
汇报用的”, “换版式/构图”, or “重新设计” is different: author one
`deck.redesign.json` and run `apply_deck_redesign.js`. That command may change a
registered theme, layout, and/or a compatible composition family, retains the previous
props in `layout_drafts`, restores legacy bound intent, and refuses to write if
the result contradicts the outline visual or its explicit item count.

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
Normal authoring uses `--theme auto`; the contract writes an explainable
`theme_selection` record and may replace a strongly mismatched fallback default.
Auto-selection applies three ordered signal classes: explicit keyword rules,
industry matching against `selection.industry_fit`, and mood matching against
`selection.mood_keywords`. Negative style clauses are removed before positive
industry/mood scoring, so “不要复古手绘” is not treated as a retro request.
Use `--theme <THEME_ID> --lock-theme` only for an exact user-selected id.
The catalog includes at least one
executable theme for every bundled Visual DNA id, plus explicitly curated
variants such as `block-frame-mono-blue`. It ships with the `pptx` skill and is
sufficient for generation on machines that do not have the separate
`html-templates` skill.

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

The technical/product/data shortlist owns three purpose-built themes:

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
seeded variant define tokens and finite geometry choices. The editor must pass
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
`design.seed` selects a deterministic variant inside the selected family:

```text
theme -> compatible directions/families -> content-selected family -> seeded variant
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
decks retain their selected family and seed across editing, reopening, and
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
