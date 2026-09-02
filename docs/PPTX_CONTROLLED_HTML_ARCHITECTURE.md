# Controlled HTML PPTX Architecture

The controlled PPTX route compiles a structured `DeckDocument` into a
self-contained, editable HTML deck. The default artifact is `index.html`;
`deck.json` remains the reproducible generation model, and editable PPTX is an
optional export.

## Assembly model

`deck.json` is not a fifth visual layer. It is the document model that selects
and supplies the four layers used by the compiler:

1. `theme_id` selects color, typography, shape, surface, and decoration tokens.
2. `design.family + design.variant` select the page-level HTML composition.
3. `slides[].layout_id` selects a registered semantic layout and field contract.
4. `slides[].props` supplies text, media, tables, charts, and other content.

```mermaid
flowchart TD
    O["User request / outline.json"] --> D["deck.json<br/>generation source of truth"]
    D --> V["validateAndNormalizeDeck"]

    D --> T["theme_id"]
    D --> G["design.seed / family / variant"]
    D --> S["slides[]"]
    S --> L["layout_id"]
    S --> P["props / background"]

    T --> TC["Theme catalog<br/>CSS variables and visual tokens"]
    T --> FM["Theme-to-family compatibility"]
    G --> FM
    FM --> DIR["5 user-facing directions<br/>discovery and routing, not persisted"]
    DIR --> C["11 internal composition wrappers<br/>ledger / spread / stage / ..."]
    G --> C

    L --> LR["Layout registry<br/>schema + renderer + editor metadata"]
    P --> LR
    LR --> LD["Layout-owned semantic DOM"]
    LD --> C

    V --> R["renderDocument"]
    TC --> R
    C --> R

    R --> H["index.html"]
    H --> CSS["deck.css + composition.css + theme variables"]
    H --> DOM["#deck-root with rendered slides"]
    H --> MODEL["embedded #deck-document"]
    H --> RT["layout registry + editor + optional ECharts"]

    MODEL --> E["Browser editor"]
    RT --> E
    E --> DOM
    E --> SAVE["Saved HTML with updated embedded model"]
    H --> PPTX["Optional editable PPTX export"]
```

The layout renderer owns field meaning, capacity, DOM, and recoverable data.
The composition wrapper owns the larger reading path and page grammar. Theme
tokens style both layers without changing their semantic field contracts.
The five composition directions are only a user-choice and AI-routing view.
They translate eleven internal families into understandable options, while
`design.family` remains the single persisted runtime decision.

## Source-of-truth lifecycle

During generation:

```text
outline.json -> deck.json -> validate -> render -> index.html
```

For a short factual brief, the Skill prepends one durable research handoff:

```text
output/research/* -> research QA -> outline.json -> deck.json -> index.html
```

Presentation tools run from the artifact root, so their relative `research/`
directory is physically `output/research/` from the host workspace. A resumed
turn uses Session Log context and validates the durable research, outline, deck,
and QA files before deciding the next Skill step. It may reuse URLs from a valid
research handoff instead of researching again. A current 7/7 deck remains
complete and is not reopened. A failed outline report is repaired from the
report's exact issues, the current outline, and allowed research URLs; this is
Skill procedure, not an Agent runtime state machine.

The renderer embeds the normalized document as
`<script type="application/json" id="deck-document">`. The browser editor reads
that model, updates `props` or `layout_id`, and re-renders through the same
embedded layout registry.

After an in-HTML edit and save, the embedded `#deck-document` is authoritative
for that saved HTML artifact. The browser does not silently rewrite a sibling
`deck.json`, so the original reproducible input and the edited HTML may diverge.

## Current theme-to-composition rule

The runtime now uses a default family plus a compatibility allowlist.
`THEME_COMPOSITION_FAMILY` still supplies legacy defaults so existing output
does not change. A theme file may declare the complete policy directly:

```json
{
  "composition": {
    "default_family": "editorial-spread",
    "allowed_families": [
      "editorial-spread",
      "literary-minimal",
      "poster-asymmetric"
    ]
  }
}
```

Default mapping examples:

```text
studio             -> poster-asymmetric
blue-professional  -> institutional-grid
biennale-yellow    -> editorial-spread
retro-windows      -> retro-interface
```

The rules are:

- the default family preserves existing output when no explicit choice exists;
- users see `composition.directions` instead of needing to know eleven family ids;
- AI matches `directions[].families` ids to
  `composition.families[].selection_signals` to resolve one family;
- AI or user selection must stay inside `allowed_families`;
- `inspect_deck_contract.js --family <FAMILY_ID>` persists the selected family
  when scaffolding a new deck;
- a persisted compatible family survives validation, editing, reopening, and
  export;
- an unknown family is an error, while a legacy incompatible persisted value
  is normalized to the default;
- `design.seed` selects a deterministic variant inside the final family.

The current relationship is therefore:

```text
theme -> compatible families -> available directions -> selected family -> seeded variant
```

This provides more creative range without allowing arbitrary theme x family
combinations that have not been visually tested.

## Five user-facing composition directions

| Direction | Internal families | Typical user intent |
| --- | --- | --- |
| `structured-systems` | institutional / analytical / technical | business reporting, decisions, systems |
| `narrative-pages` | editorial / literary | research, long-form, continuous stories |
| `visual-impact` | poster / cinematic | brands, profiles, image-led narratives |
| `interface-modules` | product / retro | product features, digital experience, UI metaphors |
| `expressive-objects` | playful / brutalist | community, strong attitude, experimental expression |

Directions are not stored in `deck.json`. The theme filters incompatible
families first; a user choice or AI inference then selects one concrete family
from the intersection of that direction and the theme allowlist. Users face five
choices while the renderer still receives one precise family out of eleven.

## Eleven composition families

| Family | Reading path | Typical use |
| --- | --- | --- |
| `institutional-grid` | disciplined grid and information rails | enterprise, consulting, research |
| `editorial-spread` | magazine spreads and mixed text scales | trends, brand story, culture |
| `poster-asymmetric` | offset display type and strong anchors | launches, manifestos, creative pitches |
| `playful-collage` | collage, stagger, and light rhythm | education, events, community, consumer |
| `brutalist-frame` | hard frames and graphic blocks | portfolios and challenger brands |
| `retro-interface` | windows, terminals, and pixel panels | games, retro tech, experiments |
| `literary-minimal` | narrow measure, margin notes, whitespace | speeches, essays, long-form summaries |
| `product-showcase` | device stages, browser stories, feature flows | product launches, SaaS, demos, cases |
| `cinematic-canvas` | large imagery, film mattes, chapter cuts | pitch openings, brand films, profiles |
| `analytical-exhibit` | evidence rails, decision boards, exhibit grids | boards, analytics, strategy reviews |
| `technical-schematic` | blueprint grids, nodes, and spec sheets | architecture, engineering, science |

A theme may allow several families, but the cross-product is not arbitrary. A
deck selects one family while its 15 semantic `layout_id` values still define
page meaning and editability.

Variants may own small, variant-specific HTML anchors, but must not duplicate
layout-owned editable fields. Interface metaphors stay semantic: browser chrome
is limited to `browser-story` media pages, system buses to `annotated-system`,
and evidence scales to `evidence-rail`. Restrained information families also
suppress repeated pill/tape labels from Visual DNA, while expressive families
may keep them when those shapes are part of the intended visual language.

## Extension boundaries

Implementation and validation are deliberately separated:

| Change | Implement once | Validate across |
| --- | --- | --- |
| Theme | One theme record plus optional assets | Representative layouts and its compatible families |
| Layout | Schema, renderer, editor metadata, export mapping | Every registered composition family |
| Composition family | HTML wrapper, anchors, CSS, variants | Every registered layout |

The target is additive implementation with cross-product testing, not one
implementation per combination. With 15 layouts and 11 composition families,
the system should contain 26 primary implementations and 165 automated
compatibility checks, not 165 separate renderers.

## Key implementation files

- `scripts/deck_spec_core.js`: DeckDocument validation and normalization.
- `scripts/composition_core.js`: five directions, eleven families, allowlists, and seeded variants.
- `layouts/registry.js`: layout contracts and composition HTML wrappers.
- `scripts/finalize_controlled_deck.js`: one dependency-ordered spec/truth/media validation, HTML compilation, self-check, and runtime-probe pass that stops at the first actionable failure.
- `scripts/render_deck_html.js`: full HTML assembly.
- `runtime/deck-editor.js`: browser editing, re-rendering, and saving.
- `scripts/html_to_editable_pptx.js`: optional editable PPTX export.

The skill-level operational contract remains in
`box_agent/skills/document-skills/pptx/references/controlled-layouts.md`.
