# Image and manifest policy

The controlled manifest uses `mode: "auto"` for ordinary decks and
`mode: "creative_image_mode"` only when the brief activates the strict creative
contract below. Ordinary `auto` decks are visual-first: an eligible cover
defaults to one bitmap visual anchor unless the outline is explicitly
typography-, data-, diagram-, or other editable-structure-led. An inner page
with a declared optional fixed media slot also defaults to acquisition when its
visual intent does not call for an editable structure; a project-case layout is
the deliberate exception because its composition already reserves a separate
image frame alongside editable metrics. In either mode, a
layout-required `generate` entry must resolve to a real file before final QA;
`auto` only removes the deck-wide minimum of one generated image for briefs
without a promoted media requirement. Any scaffolded
entry with `required: true` must resolve through `generate` or `use_existing`;
changing it to `skip`/`blocked` fails manifest QA. Investor/fundraising/pitch/
launch/premium briefs and visual stories such as biographies, sports, travel,
or history promote the cover entry in `auto` to this required state unless the
user opts out. The scaffold also reads the bound outline, not only the deck
title: a cover that explicitly calls for a product/client interface, code
window, collaboration nodes, or a system-connection visual becomes a concrete
image job in `auto`; a typography/data-led cover remains image-free.

## 0. Creative image mode

`creative_image_mode` is the strict image-generation mode for explicit
image-rich/creative briefs. Do not activate it solely because a factual topic is
visually interesting; ordinary visual stories stay in `auto` and may still get
a required generated cover.

Rules:

1. The manifest must include `"mode": "creative_image_mode"`.
2. At least one slide, normally the cover, must use `decision: "generate"` and must finish with a real generated file under `assets/generated/`.
3. A generated image counts only when `generate_image` succeeds, `output_path` exists, and the final HTML/PPT references that asset.
4. If no generated image succeeds, the image requirement is `blocked`; do not mark the PPT as completed and do not replace the required generated asset with `draw_in_html`, `skip`, or a decorative CSS-only visual. If the deck structure remains valid, still render and deliver a degraded editable `index.html` with the image failure preserved as an advisory warning.
5. Preserve the scaffolded `decision: "generate"` and record failures with `status: "blocked"`, an updated `decision_reason`, `tool: "generate_image"`, and the attempted prompt/slide role so the user can retry after configuration or service recovery.
6. Full-slide/background generated images must still follow the `layout_contract` rules below. Fixed-frame hero images may satisfy the mandatory generation requirement without a layout contract if they do not sit behind text.
7. Prefer `image-full-bleed-v1` for an explicit entire-slide generated visual. It scaffolds the required background entry, fixed text-safe region, visual-focus region, prompt coordinates, and `wash-dark` treatment. Use `image-feature-v1` for a wide 16:9 image with editable supporting narrative.

## 1. Decision first

1. Every slide must have one explicit `image_plan` entry.
2. On the controlled route, `image_plan.decision` remains the compatibility/execution state `generate`, `use_existing`, or `skip`; `acquire_via` is the source decision `ai`, `web`, `user`, or `none`. They must agree with the inspected slot/background strategies. Use `status: "blocked"` for a failed required generation attempt. `draw_in_html` is a legacy/custom-HTML planning label only; never write it into the controlled scaffold manifest.
3. Prefer a bitmap asset when it makes the slide faster to understand, more memorable, or visually credible. Choose `web` for real people, places, products, evidence, documentary subjects, and ordinary photographic atmosphere; choose `ai` for invented or deliberately stylized scenes, illustration, metaphor, abstract backgrounds, conceptual interfaces, and decorative image treatments. In ordinary `auto`, do not require explicit words such as “插画” when the chosen cover or inner-page media slot already establishes a clear image job.
4. Do not use `skip` as the default. Use it only when the reason says why typography, data, or editable shapes are stronger than any bitmap.
5. Use supplied or web-search source-backed images for factual, screenshot, chart, logo, real-location, or person-accuracy content. When no acceptable result exists, any generated fallback is visibly labelled as a concept and never presented as documentary evidence.
6. Do not create generic decorative filler; generated images need a clear narrative job.
7. A generated visual used in a project/case-study slot is concept art unless the user supplied the real project asset. Set `origin: "generated"` and label its alt/caption explicitly (for example `AI 概念视觉，实际项目图待补充`) so viewers cannot mistake it for documentary evidence.
8. Bind a user-supplied local image during scaffold with repeated
   `--image-asset SLIDE:SLOT=PATH` arguments (for example
   `--image-asset 1:hero=/path/to/client-ui.png`). The helper validates the
   inspected slot strategy, copies PNG/JPG/JPEG/WEBP files under
   `assets/source/`, hashes them, and records `decision: "use_existing"`.
   Reference that portable copied path from `deck.json`; never leave the final
   deck pointing at the user's original machine path.
9. A `web` row calls the registered `web_search` tool with `SearchType=image` before any generation call. A successfully localized result becomes `use_existing`, `resolved_via: web`, and retains its search/source metadata plus `license_status: unverified`; no usable result or tool/provider unavailability keeps the row as `generate` with the terminal search status and fallback reason preserved.

## 2. Trigger rules

1. Use `ai` directly for explicitly generated covers, dividers, posters, campaigns, abstract concepts, future-state scenes, transformations, illustrations, metaphors, and emotionally led closing slides.
2. In `auto`, use `generate` when the bound cover outline explicitly asks for a
   product/client interface, browser/device mockup, code window, collaboration
   nodes, or system-connection visual. Product UI generation is a labelled
   concept illustration, never a claim that it reproduces a real screenshot;
   use `use_existing` when a real screenshot was supplied.
3. For ordinary investor, fundraising, premium B2B, executive-keynote, and factual visual-story pages, try `web` first unless the requested visual is explicitly conceptual or stylized. Product-demo interfaces remain `ai` concepts unless a real screenshot was supplied.
4. Use `web` first for realistic environments, human scenes, real products, locations, portraits, and photographic hero/card visuals. Use `ai` directly only when the brief asks for a constructed or stylized interpretation.
5. Use `ai` when the user asks for illustration, concept art, abstract imagery, visual metaphor, stylized poster/campaign art, or another generated treatment.
6. On the controlled route, use the registered editable chart/table/timeline layout and `skip` bitmap media for dense data, timelines, architecture, process, and tables. A conceptual code/system *cover* requested by the outline is the exception; keep the detailed architecture page editable. A map may be generated when it is explicitly the page's primary visual medium, but structured geographic data must remain recoverable. Use `draw_in_html` only on the explicit legacy/custom-HTML route.
7. Use `skip` for data slides only when charts and text are stronger and no local visual frame would help.
8. Use `use_existing` for supplied product photos, charts, official logos, real locations, screenshots, named people, or source-captured visuals.

## 3. Manifest format

1. Keep the scaffolded deck-level `assets/generated/manifest.json` with its
   `deck` identity block and complete `image_plan`. Do not replace the file or
   invent parallel `deck_context`/`style_anchor` schemas; put visual context in
   each generated prompt and update only the existing plan entries.
2. For every slide that uses a generated full-bleed or full-slide background, record a `layout_contract` before writing the prompt. The contract is the source of truth for text placement; image prompts are derived from it, not guessed independently.
3. Use the fixed slide coordinate system `1920x1080`. Record text regions as `{ x, y, width, height }` in pixels, matching the controlled boxes compiled into `index.html` (or `deck.html` only on the legacy route).
4. If the text layout is not fixed yet, draft the layout first. Do not generate a full-slide image from a vague instruction such as "leave room for title".
5. Small or medium generated hero images placed in fixed frames do not need `layout_contract` by default. Give them an explicit `placement`/frame size and keep them out of text flow; add `layout_contract` only if the image visually overlaps or sits behind text.
6. On the controlled route, bind the resolved asset to the inspected layout contract. Fixed-frame assets use the declared `mediaSlots.slots[].propPath`; full-slide assets use `slide.background`. Record `origin: "generated"` or `origin: "asset"` in the final media object. Do not store unresolved `decision: "generate"` values in `deck.json`.
7. Preserve this scaffolded entry shape for each slide needing visuals. Add a
   `layout_contract` only for a generated full-slide/background image; a
   fixed-frame hero entry stays compact:

```json
{
  "slide": 3,
  "slide_id": "slide-03",
  "layout_id": "image-hero-split-v1",
  "slot": "image",
  "prop_path": "image",
  "required": true,
  "decision": "generate",
  "status": "pending",
  "acquire_via": "ai",
  "resolved_via": null,
  "decision_reason": "The page message benefits from one fixed-frame visual anchor",
  "prompt": "Deck context: AI operating-model transformation for executives. Subject: three abstract data streams converging into one node. Composition: right-side fixed hero; no embedded text or watermark. Style: crisp editorial vector, indigo/cyan/amber palette.",
  "output_path": "assets/generated/slide-03-hero.png",
  "allowed_strategies": ["generate", "use_existing", "skip"]
}
```

1. Use structured prompt fields for `generate`.
2. Put `deck_context` first in every `generate` prompt so the image model sees the whole PPT theme before the slide-specific subject.
3. Keep `avoid` separate from `prompt`.
4. Put text-region coordinates into the `composition` field in human-readable form. Example: `title/body safe area x=120,y=155,w=700,h=565; keep this region calm and low contrast with faint thematic texture, not blank; place visual focus on right`.
5. The final HTML must implement the same `layout_contract.text_regions` values for text-bearing elements. If the HTML positions change, update the manifest and regenerate or revise the image prompt.
6. Each text-bearing HTML element covered by `layout_contract.text_regions` must carry `data-layout-region="<region name>"`. Run `scripts/validate_image_layout_contract.js index.html assets/generated/manifest.json` before HTML self-check to compare actual DOM boxes with the manifest. The validator only requires contracts for generated full-slide/background images; ordinary fixed-frame hero images are not blocked by this gate.

## 4. Parallel generation

1. Emit all independent `generate_image` calls in the same assistant tool-call batch. The tool is parallel-safe and the executor runs the batch concurrently within its configured semaphore.
2. Do not delegate image calls to a sub-agent merely to avoid waiting. The parent must receive the actual output paths before it can update the manifest, bind media props, render, and run QA; fire-and-forget would create a race.
3. A sub-agent is useful only for a genuinely independent task such as image research or prompt planning with deterministic output files. It is not the default image execution path, and a one-image deck gains no latency benefit from it.
4. After the files exist, run the loader-expanded absolute
   `scripts/sync_image_manifest_status.js` once, using the standalone
   platform-specific command in `SKILL.md`. The Bash tool already starts in the
   artifact root: do not prepend `cd`, add redirects/diagnostic suffixes, expand
   `BOX_AGENT_OUTPUT_DIR` into the manifest argument, or manually edit status
   fields after a rejection.

## 4.1 Hosted web image search before generation

For every `acquire_via: web` row, call the registered `web_search` tool with
the exact scaffolded query, `SearchType: "image"`, and `Count: 5` before calling
`generate_image`. Call once per unique query and reuse the returned candidate
batch for any identical-query rows, because the runtime intentionally rejects
duplicate searches. Do not invoke the hosted MCP URL from a script: the Agent tool
call owns authentication, usage logging, budgets, and shared concurrency.

The tool may return normalized `refs[].image_details[]`, Custom
`Result.ImageResults[].Image.Url`, or Global
`Result.Documents[].Snippet[].Image.ImageUrl`. Select one candidate whose
subject, orientation, reported clarity, watermark state, and dimensions fit the
declared slot. Write only that selection to an artifact-root-local receipt:

```json
{
  "slide": 1,
  "query": "Neymar Barcelona portrait",
  "reference_tag": "ref_1",
  "title": "Neymar playing for Barcelona",
  "source_url": "https://example.com/source-page",
  "image": {
    "url": "https://cdn.example.com/image.jpg?signature=...",
    "width": 1600,
    "height": 1000,
    "alt": "Neymar playing football",
    "shape": "横长方形",
    "clarity": "清晰",
    "category": "体育",
    "watermark": "0",
    "description": "Football player on the pitch",
    "style_type": "实拍图"
  }
}
```

Then run:

```bash
${BOX_AGENT_PYTHON:-python3} scripts/localize_web_image.py \
  assets/generated/manifest.json import \
  assets/source/candidates/slide-01.json
```

The helper requires the receipt query to match the scaffold, preserves the
signed image URL exactly, rejects reported blur/watermarks, downloads at most
25 MiB over HTTPS, verifies the actual file and minimum dimensions, normalizes
it to JPEG under `assets/source/`, and atomically records provenance. A search
result does not prove a reusable license, so the source is recorded with
`license_status: unverified` and manifest QA emits a public-distribution
warning until a human or an authorized upstream source verifies the rights.

If all returned candidates fail, record `exhausted`; if `web_search` is absent,
authentication fails, or the provider/transport fails, record `unavailable`:

```bash
${BOX_AGENT_PYTHON:-python3} scripts/localize_web_image.py \
  assets/generated/manifest.json mark --slide 1 \
  --status exhausted --reason "no usable result passed localization checks"
```

Only after one of those terminal states may the row retain `generate` and use
its existing prompt as an explicitly labelled AI fallback. Do not present an
AI fallback for a named real subject as factual evidence.

## 5. Style anchor reuse

1. Derive one style anchor from the selected controlled theme and repeat it in
   every generated prompt. Do not add a separate top-level `style_anchor` to
   the scaffolded controlled manifest.
2. Keep generated prompt styles consistent with deck voice and palette.
3. Avoid arbitrary dimensions.
4. Use preset `2848x1600` for 16:9 hero/background.
5. Use preset `2048x2048` for square spot.

## 6. Output placement

1. Store generated files under `assets/generated/`.
1. Reference files with artifact-root-relative paths inside `index.html`/`deck.json`.
1. Always call `generate_image` with `watermark: false` and `publish_artifact: false` for PPT assets. Keep intermediate images out of user-facing messages; retain their files and manifest references. Only publish individual images when explicitly requested. The deck supplies its own branding/watermark and the `avoid` field already steers the model away from in-image watermarks, so the tool's default "AI 生成" stamp must be suppressed.
1. If generation tooling is unavailable, mark required image-plan entries as `blocked`; on a non-creative controlled deck, an optional slot may use `skip` only when its layout contract permits it. `draw_in_html` remains a legacy-route choice, not a controlled fallback.
1. In `creative_image_mode`, the previous fallback rule is stricter: if the required generated image is unavailable, the image-complete delivery is blocked. Preserve and deliver any structurally valid HTML as a degraded draft; do not claim that it satisfies the requested image-rich result.
